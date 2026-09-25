"""Fetch OSM rivers and build final-grid river masks, surfaces, depths, and banks."""
import json
import math
import time
from pathlib import Path

import numpy as np
import requests
from affine import Affine
from PIL import Image
from pyproj import Transformer
from rasterio.features import rasterize
from scipy.ndimage import binary_dilation, distance_transform_cdt, median_filter, minimum_filter
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, transform as project, unary_union

from util.projection import MasterGrid


OVERPASS_MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.nchc.org.tw/api/interpreter",
)


def _cache_name(south, west, north, east):
    value = f"osm_rivers_{south:.5f}_{west:.5f}_{north:.5f}_{east:.5f}.json"
    return value.replace("-", "m")


def _fetch_tile(cache_dir: Path, south, west, north, east):
    cache = cache_dir / _cache_name(south, west, north, east)
    if cache.exists() and cache.stat().st_size > 0:
        return json.loads(cache.read_text(encoding="utf-8")).get("elements", [])

    query = f"""
    [out:json][timeout:180];
    (
      way["waterway"="river"]({south},{west},{north},{east});
      way["natural"="water"]["water"="river"]({south},{west},{north},{east});
      relation["natural"="water"]["water"="river"]({south},{west},{north},{east});
    );
    out geom;
    """
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": "EarthMapCreatorO/0.9 (Vintage Story map pipeline)",
    }
    last_error = None
    for attempt in range(6):
        url = OVERPASS_MIRRORS[attempt % len(OVERPASS_MIRRORS)]
        try:
            response = requests.post(url, data=query.encode("utf-8"), headers=headers, timeout=300)
            response.raise_for_status()
            data = response.json()
            cache.write_text(json.dumps(data), encoding="utf-8")
            return data.get("elements", [])
        except Exception as error:
            last_error = error
            print(f"  Overpass attempt {attempt + 1}/6 failed ({url}): {error}", flush=True)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"All Overpass mirrors failed: {last_error}")


def fetch_osm_rivers(bounds, cache_dir: Path, tile_degrees=1.0):
    """Fetch cached OSM river ways and multipolygons in bounded API tiles."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    west, south, east, north = bounds.lon_min, bounds.lat_min, bounds.lon_max, bounds.lat_max
    elements = {}
    lat = south
    while lat < north:
        tile_north = min(north, lat + tile_degrees)
        lon = west
        while lon < east:
            tile_east = min(east, lon + tile_degrees)
            for element in _fetch_tile(cache_dir, lat, lon, tile_north, tile_east):
                elements[(element.get("type"), element.get("id"))] = element
            lon = tile_east
        lat = tile_north
    print(f"  OSM river elements: {len(elements)}", flush=True)
    return list(elements.values())


def _parse_width_metres(tags, cfg):
    value = tags.get("width")
    if value:
        try:
            # OSM width is metres unless explicitly suffixed; accept common decimal forms.
            token = value.lower().replace("meters", "").replace("metres", "").replace("m", "").strip()
            width = float(token.split(";")[0].strip())
            if math.isfinite(width) and width > 0:
                return min(width, float(cfg.RIVER_MAX_LINE_WIDTH_METRES))
        except (ValueError, TypeError):
            pass
    return float(cfg.RIVER_DEFAULT_WIDTH_METRES)


def prepare_river_geometries(elements, dst_crs, cfg):
    """Return projected river footprints; polygons keep true banks, lines use metre widths."""
    transformer = Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True)
    geometries = []
    for element in elements:
        tags = element.get("tags", {})
        if element.get("type") == "relation":
            outers, inners = [], []
            for member in element.get("members", []):
                points = member.get("geometry") or []
                if len(points) < 2:
                    continue
                line = LineString([(point["lon"], point["lat"]) for point in points])
                (inners if member.get("role") == "inner" else outers).append(line)
            rings = list(polygonize(outers))
            if not rings:
                continue
            geometry = unary_union(rings)
            holes = list(polygonize(inners))
            if holes:
                geometry = geometry.difference(unary_union(holes))
        else:
            points = element.get("geometry") or []
            if len(points) < 2:
                continue
            coordinates = [(point["lon"], point["lat"]) for point in points]
            is_polygon = (tags.get("natural") == "water" and len(coordinates) >= 4
                          and coordinates[0] == coordinates[-1])
            geometry = Polygon(coordinates) if is_polygon else LineString(coordinates)

        if geometry.is_empty or not geometry.is_valid:
            continue
        geometry = project(transformer.transform, geometry)
        if geometry.geom_type in ("LineString", "MultiLineString"):
            geometry = geometry.buffer(_parse_width_metres(tags, cfg) / 2.0)
        if not geometry.is_empty:
            geometries.append(geometry)
    return geometries


def _grade_banks(height, river, surface, land, width, slope):
    covered = river.copy()
    propagated = np.where(river, surface, 255).astype(np.uint8)
    original = height.copy()
    for distance in range(1, int(width) + 1):
        next_surface = minimum_filter(propagated, size=3, mode="nearest")
        ring = (~covered) & land & (next_surface < 255)
        if not ring.any():
            break
        natural = original[ring].astype(np.int16)
        target = next_surface[ring].astype(np.int16)
        upper = np.minimum(natural, target + distance * int(slope))
        blend = (distance - 1) / max(1, int(width) - 1)
        lower = np.rint(target + (natural - target) * blend)
        bank = np.where(natural < target, lower, upper)
        height[ring] = np.clip(np.rint(bank), 1, 255).astype(np.uint8)
        propagated[ring] = next_surface[ring]
        covered[ring] = True
    return height


def build_river_maps(geometries, grid: MasterGrid, height, land, lake_mask, cfg):
    """Build binary mask, absolute-Y surface, and block depth on the final grid."""
    rows, cols = height.shape
    transform = grid.transform @ Affine.scale(grid.width / cols, grid.height / rows)
    mask = rasterize(
        [(geometry.__geo_interface__, 1) for geometry in geometries],
        out_shape=(rows, cols), transform=transform, fill=0,
        all_touched=True, dtype=np.uint8,
    ).astype(bool) if geometries else np.zeros((rows, cols), dtype=bool)
    river = mask & (lake_mask == 0) & (land > 0)

    local_floor = minimum_filter(height, size=int(cfg.RIVER_SURFACE_WINDOW_BLOCKS), mode="nearest")
    smoothed = median_filter(local_floor, size=3, mode="nearest")
    river_surface = np.zeros_like(height)
    river_surface[river] = np.minimum(smoothed[river], local_floor[river])

    lake = lake_mask > 0
    lake_neighbor_surface = minimum_filter(
        np.where(lake, height, 255).astype(np.uint8), size=3, mode="nearest")
    lake_join = river & binary_dilation(lake, iterations=1)
    river_surface[lake_join] = lake_neighbor_surface[lake_join]

    shore_distance = distance_transform_cdt(river, metric="chessboard")
    river_depth = np.zeros_like(height)
    river_depth[river] = np.clip(
        shore_distance[river], int(cfg.RIVER_MIN_DEPTH_BLOCKS),
        int(cfg.RIVER_MAX_DEPTH_BLOCKS),
    ).astype(np.uint8)

    _grade_banks(height, river, river_surface, land > 0,
                 cfg.RIVER_BANK_WIDTH_BLOCKS, cfg.RIVER_BANK_SLOPE)
    return river.astype(np.uint8) * 255, river_surface, river_depth


def write_river_maps(work_dir: Path, build_dir: Path, grid: MasterGrid, bounds, cfg):
    elements = fetch_osm_rivers(bounds, work_dir / "osm_processing" / "overpass_rivers")
    geometries = prepare_river_geometries(elements, grid.crs, cfg)
    print(f"  OSM river footprints: {len(geometries)}", flush=True)

    with Image.open(build_dir / "heightmap.png") as image:
        height = np.array(image.convert("L"), dtype=np.uint8)
    with Image.open(build_dir / "landmask.png") as image:
        land = np.array(image.convert("L"), dtype=np.uint8)
    with Image.open(build_dir / "lake_mask.png") as image:
        lake_mask = np.array(image.convert("L"), dtype=np.uint8)

    mask, surface, depth = build_river_maps(geometries, grid, height, land, lake_mask, cfg)
    Image.fromarray(height).save(build_dir / "heightmap.png")
    Image.fromarray(mask).save(build_dir / "river.png")
    Image.fromarray(surface).save(build_dir / "river_surface.png")
    Image.fromarray(depth).save(build_dir / "river_depth.png")
