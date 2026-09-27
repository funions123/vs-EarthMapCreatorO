"""Create lake masks, surfaces, beds, and banks on the final block grid."""
from pathlib import Path

import fiona
import numpy as np
from affine import Affine
from PIL import Image
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.windows import Window, from_bounds, transform as window_transform
from scipy.ndimage import distance_transform_cdt
from shapely.geometry import shape
from shapely.ops import transform as project, unary_union

from util.projection import MasterGrid


# Depth and bank slope are in VS blocks, after the source DEM has been resized.
MIN_DEPTH = 1
MAX_DEPTH = 12
SHORE_WIDTH = 6
SHORE_SLOPE = 1
SEA_LEVEL = 92
# Encoded in lake_mask.png and the LakeMask region plane. Zero is dry; the
# Vintage Story terrain generator uses the saline value for saltwater blocks.
FRESH_LAKE = 255
SALINE_LAKE = 128


def _lake_geometries(gpkg: Path, grid: MasterGrid, saline_lake_names):
    if not gpkg.exists():
        raise FileNotFoundError(f"Lake polygons not found: {gpkg}")
    transformer = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    saline_names = set(saline_lake_names)
    with fiona.open(str(gpkg)) as src:
        if "featurecla" not in src.schema["properties"]:
            raise ValueError("Lake polygons are missing featurecla")
        # Union touching polygons so one continuous lake gets one surface level.
        geometries = [(project(transformer.transform, shape(feature["geometry"])).buffer(20),
                       feature["properties"]["featurecla"] == "Alkaline Lake"
                       or feature["properties"].get("name") in saline_names)
                      for feature in src if feature["geometry"]]
    geometries = [(geom, saline) for geom, saline in geometries if not geom.is_empty]
    merged = unary_union([geom for geom, _ in geometries])
    if merged.is_empty:
        return []
    saline = unary_union([geom for geom, is_saline in geometries if is_saline])
    components = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
    return [(geom, geom.intersects(saline)) for geom in components]


def _pixel_window(geom, transform, width, height):
    win = from_bounds(*geom.bounds, transform=transform)
    halo = max(MAX_DEPTH, SHORE_WIDTH) + 2
    x0 = max(0, int(np.floor(win.col_off)) - halo)
    y0 = max(0, int(np.floor(win.row_off)) - halo)
    x1 = min(width, int(np.ceil(win.col_off + win.width)) + halo)
    y1 = min(height, int(np.ceil(win.row_off + win.height)) + halo)
    return x0, y0, x1, y1


def _surface_byte(pixels):
    # The existing normalized DEM supplies the elevation; reject isolated outliers.
    counts = np.bincount(pixels, minlength=256)
    return int(np.searchsorted(np.cumsum(counts), (pixels.size + 1) // 2))


def build_lake_maps(gpkg: Path, grid: MasterGrid, height: np.ndarray,
                    land: np.ndarray, saline_lake_names=()):
    """Mutate height/land arrays and return classified lake mask and uint8 depths.

    Every output uses the identical final-grid polygon footprint. Water columns
    have one constant height per connected polygon and a bed below that height.
    Dry columns touching the lake meet its waterline, then rejoin natural terrain.
    """
    if height.shape != land.shape or height.dtype != np.uint8 or land.dtype != np.uint8:
        raise ValueError("height and land must be matching uint8 final-grid images")

    rows, cols = height.shape
    transform = grid.transform @ Affine.scale(grid.width / cols, grid.height / rows)
    original = height.copy()
    mask = np.zeros_like(height)
    depth = np.zeros_like(height)
    nearest_shore = np.full_like(height, 255)

    for geom, saline in _lake_geometries(gpkg, grid, saline_lake_names):
        x0, y0, x1, y1 = _pixel_window(geom, transform, cols, rows)
        if x0 == x1 or y0 == y1:
            continue
        section = np.s_[y0:y1, x0:x1]
        window = Window(x0, y0, x1 - x0, y1 - y0)
        water = rasterize(
            [(geom.__geo_interface__, 1)], out_shape=(y1 - y0, x1 - x0),
            transform=window_transform(window, transform), dtype=np.uint8,
        ).astype(bool)
        if not water.any():
            continue

        level = _surface_byte(original[section][water])
        height_part = height[section]
        mask_part = mask[section]
        mask_part[water] = SALINE_LAKE if saline else FRESH_LAKE
        land[section][water] = 255  # Lake must not enter the ocean terrain branch.
        height_part[water] = level

        # Synthetic bathymetry is defined relative to this lake's surface,
        # never by subtracting an unrelated GEBCO/GMTED heightmap.
        distance = distance_transform_cdt(water, metric="chessboard")
        depth[section][water] = np.clip(distance[water] * SHORE_SLOPE,
                                        MIN_DEPTH, MAX_DEPTH).astype(np.uint8)

        # Outside the polygon, meet water Y at the first dry pixel. Taper high
        # banks down and low banks up over a narrow bounded band. The closest
        # lake owns a dry pixel where two bands overlap.
        dry_distance = distance_transform_cdt(~water, metric="taxicab")
        nearest = nearest_shore[section]
        dry = (~water) & (mask_part == 0) & (dry_distance <= SHORE_WIDTH) & (dry_distance < nearest)
        if not dry.any():
            continue
        d = dry_distance[dry]
        natural = original[section][dry].astype(np.int16)
        surface = level
        upper = np.minimum(natural, surface + d * SHORE_SLOPE)
        # A low pixel directly beside the water must not form a hole in its bank.
        blend = (d - 1) / (SHORE_WIDTH - 1)
        lower = np.rint(surface + (natural - surface) * blend)
        bank = np.where(natural < surface, lower, upper)
        gray = np.clip(np.rint(bank), 1, 255).astype(np.uint8)
        gray[d == 1] = level  # Exact same decoded Y as the waterline.
        gray[d == SHORE_WIDTH] = original[section][dry][d == SHORE_WIDTH]
        height_part[dry] = gray
        nearest[dry] = d.astype(np.uint8)

    return mask, depth


def write_lake_maps(work_dir: Path, build_dir: Path, grid: MasterGrid, cfg):
    with Image.open(build_dir / "heightmap.png") as img:
        height = np.array(img.convert("L"), dtype=np.uint8)
    with Image.open(build_dir / "landmask.png") as img:
        land = np.array(img.convert("L"), dtype=np.uint8)
    mask, depth = build_lake_maps(work_dir / "crop_lakes.gpkg", grid, height, land,
                                  cfg.SALINE_LAKE_NAMES)
    Image.fromarray(height).save(build_dir / "heightmap.png")
    Image.fromarray(land).save(build_dir / "landmask.png")
    Image.fromarray(mask).save(build_dir / "lake_mask.png")
    Image.fromarray(depth).save(build_dir / "lake_depth.png")
