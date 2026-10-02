"""Create lake masks, surfaces, beds, and banks on the final block grid."""
from pathlib import Path
import tempfile

import fiona
import numpy as np
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.features import rasterize
from rasterio.windows import Window, from_bounds, transform as window_transform
from scipy.ndimage import distance_transform_cdt
from shapely.geometry import shape
from shapely.ops import transform as project, unary_union

from util.projection import MasterGrid
from pipeline.lake_classification import is_natural_lake
from util.working import create_layer, expanded, windows


# Depth and bank slope are in VS blocks, after the source DEM has been resized.
MIN_DEPTH = 1
MAX_DEPTH = 12
SHORE_WIDTH = 6
SHORE_SLOPE = 1
# Encoded in lake_mask.tif and the LakeMask region plane. Zero is dry; the
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
        # Recheck classification because an existing crop_lakes.gpkg may predate
        # source-stage filtering.
        geometries = [(project(transformer.transform, shape(feature["geometry"])).buffer(20),
                       feature["properties"]["featurecla"] == "Alkaline Lake"
                       or feature["properties"].get("name") in saline_names)
                      for feature in src
                      if feature["geometry"] and is_natural_lake(feature["properties"])]
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


def _surface_from_counts(counts, size):
    return int(np.searchsorted(np.cumsum(counts), (size + 1) // 2))


def _surface_byte(pixels):
    # The existing normalized DEM supplies the elevation; reject isolated outliers.
    return _surface_from_counts(np.bincount(pixels, minlength=256), pixels.size)


def _depth_map(water, all_water):
    """Return capped depth while preserving scipy's whole-footprint all-water result."""
    result = np.zeros(water.shape, dtype=np.uint8)
    if not water.any():
        return result
    if all_water:
        result[water] = MIN_DEPTH
    elif water.all():
        # No shore occurs within this bounded tile. Its core is farther than the
        # depth cap from the global polygon shore.
        result[water] = MAX_DEPTH
    else:
        distance = distance_transform_cdt(water, metric="chessboard")
        result[water] = np.clip(distance[water] * SHORE_SLOPE,
                                MIN_DEPTH, MAX_DEPTH).astype(np.uint8)
    return result


def _bank_bytes(natural, distance, level):
    natural_int = natural.astype(np.int16)
    upper = np.minimum(natural_int, level + distance * SHORE_SLOPE)
    blend = (distance - 1) / (SHORE_WIDTH - 1)
    lower = np.rint(level + (natural_int - level) * blend)
    bank = np.where(natural_int < level, lower, upper)
    gray = np.clip(np.rint(bank), 1, 255).astype(np.uint8)
    gray[distance == 1] = level
    gray[distance == SHORE_WIDTH] = natural[distance == SHORE_WIDTH]
    return gray


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

        depth_part = depth[section]
        depth_part[water] = _depth_map(water, water.all())[water]

        # Outside the polygon, meet water Y at the first dry pixel. Taper high
        # banks down and low banks up over a narrow bounded band. The closest
        # lake owns a dry pixel where two bands overlap.
        dry_distance = distance_transform_cdt(~water, metric="taxicab")
        nearest = nearest_shore[section]
        dry = (~water) & (mask_part == 0) & (dry_distance <= SHORE_WIDTH) & (dry_distance < nearest)
        if not dry.any():
            continue
        d = dry_distance[dry]
        height_part[dry] = _bank_bytes(original[section][dry], d, level)
        nearest[dry] = d.astype(np.uint8)

    return mask, depth


def _rasterized_water(geom, window, transform):
    return rasterize(
        [(geom.__geo_interface__, 1)],
        out_shape=(int(window.height), int(window.width)),
        transform=window_transform(window, transform),
        dtype=np.uint8,
    ).astype(bool)


def _lake_level(original, geom, transform, bounds):
    counts = np.zeros(256, dtype=np.int64)
    water_pixels = 0
    for window in windows(original.width, original.height, bounds):
        water = _rasterized_water(geom, window, transform)
        count = int(np.count_nonzero(water))
        if count:
            values = original.read(1, window=window)[water]
            counts += np.bincount(values, minlength=256)
            water_pixels += count
    if not water_pixels:
        return None, False
    footprint_pixels = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
    return _surface_from_counts(counts, water_pixels), water_pixels == footprint_pixels


def _write_lake(geom, saline, level, all_water, transform, bounds,
                original, height, land, mask, depth, nearest):
    halo = max(MAX_DEPTH, SHORE_WIDTH) + 2
    lake_value = SALINE_LAKE if saline else FRESH_LAKE
    for core_window in windows(height.width, height.height, bounds):
        outer_window, core = expanded(core_window, height.width, height.height, halo)
        water_outer = _rasterized_water(geom, outer_window, transform)
        if not water_outer.any():
            continue
        water = water_outer[core]
        mask_part = mask.read(1, window=core_window)
        height_part = height.read(1, window=core_window)
        land_part = land.read(1, window=core_window)
        depth_part = depth.read(1, window=core_window)

        if water.any():
            mask_part[water] = lake_value
            height_part[water] = level
            land_part[water] = 255
            depth_part[water] = _depth_map(water_outer, all_water)[core][water]

        dry_distance = distance_transform_cdt(~water_outer, metric="taxicab")
        dry_distance = dry_distance[core]
        nearest_part = nearest.read(1, window=core_window)
        dry = ((~water) & (mask_part == 0)
               & (dry_distance <= SHORE_WIDTH) & (dry_distance < nearest_part))
        if dry.any():
            natural = original.read(1, window=core_window)
            distance = dry_distance[dry]
            height_part[dry] = _bank_bytes(natural[dry], distance, level)
            nearest_part[dry] = distance.astype(np.uint8)

        height.write(height_part, 1, window=core_window)
        land.write(land_part, 1, window=core_window)
        mask.write(mask_part, 1, window=core_window)
        depth.write(depth_part, 1, window=core_window)
        nearest.write(nearest_part, 1, window=core_window)


def _validate_layer(dataset, path, width=None, height=None):
    if dataset.count != 1 or dataset.dtypes[0] != "uint8":
        raise ValueError(f"{path} must be a one-band uint8 raster")
    if width is not None and (dataset.width != width or dataset.height != height):
        raise ValueError("heightmap.tif and landmask.tif dimensions must match")


def write_lake_maps(work_dir: Path, build_dir: Path, grid: MasterGrid, cfg):
    """Apply lakes using bounded raster windows and emit tiled working layers."""
    height_path = build_dir / "heightmap.tif"
    land_path = build_dir / "landmask.tif"
    with tempfile.TemporaryDirectory(prefix="lakes-", dir=build_dir) as temporary:
        temporary = Path(temporary)
        original_path = temporary / "original-height.tif"
        nearest_path = temporary / "nearest-shore.tif"

        with rasterio.open(height_path) as source:
            _validate_layer(source, height_path)
            width, height_rows = source.width, source.height
            with create_layer(original_path, width, height_rows) as original:
                for window in windows(width, height_rows):
                    original.write(source.read(1, window=window), 1, window=window)
        with rasterio.open(land_path) as source:
            _validate_layer(source, land_path, width, height_rows)

        transform = grid.transform @ Affine.scale(grid.width / width,
                                                   grid.height / height_rows)
        geometries = _lake_geometries(work_dir / "crop_lakes.gpkg", grid,
                                      cfg.SALINE_LAKE_NAMES)
        with (rasterio.open(original_path) as original,
              rasterio.open(height_path, "r+") as height,
              rasterio.open(land_path, "r+") as land,
              create_layer(build_dir / "lake_mask.tif", width, height_rows) as mask,
              create_layer(build_dir / "lake_depth.tif", width, height_rows) as depth,
              create_layer(nearest_path, width, height_rows, fill=255) as nearest):
            for geom, saline in geometries:
                bounds = _pixel_window(geom, transform, width, height_rows)
                if bounds[0] == bounds[2] or bounds[1] == bounds[3]:
                    continue
                level, all_water = _lake_level(original, geom, transform, bounds)
                if level is None:
                    continue
                _write_lake(geom, saline, level, all_water, transform, bounds,
                            original, height, land, mask, depth, nearest)
