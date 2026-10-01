"""Build final-grid river maps from local HydroRIVERS centerlines."""
import math
from pathlib import Path

import fiona
import numpy as np
from affine import Affine
from PIL import Image
from pyproj import Transformer
from rasterio.features import rasterize
from scipy.ndimage import binary_dilation, distance_transform_cdt, distance_transform_edt, maximum_filter, median_filter, minimum_filter
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import transform as project

from util.projection import MasterGrid
from pipeline.river_profiles import fitted_river_surface

def river_width_metres(discharge, pixel_size, cfg):
    """Scale a channel from its minimum width, capped in output pixels."""
    minimum = float(cfg.RIVER_MIN_WIDTH_PIXELS) * pixel_size
    reference = float(cfg.RIVER_WIDTH_REFERENCE_FLOW_CMS)
    maximum = float(cfg.RIVER_MAX_WIDTH_PIXELS) * pixel_size
    if reference <= 0 or minimum <= 0 or maximum < minimum:
        raise ValueError("River widths must be positive, max >= minimum, and reference flow positive")
    flow = float(discharge) if discharge is not None else 0.0
    if not math.isfinite(flow) or flow <= 0:
        return minimum
    return min(maximum, minimum + pixel_size * math.sqrt(flow / reference))


def load_river_lines(datasets_dir: Path, bounds, grid: MasterGrid, output_shape, cfg):
    """Select, clip and generalize reaches for both mask and waterline fitting."""
    source = datasets_dir / "HydroRIVERS_v10_na_shp" / "HydroRIVERS_v10_na.shp"
    if not source.is_file():
        source = Path.home() / "Downloads" / "HydroRIVERS_v10_na_shp" / "HydroRIVERS_v10_na.shp"
    if not source.is_file():
        raise FileNotFoundError(
            "Extract HydroRIVERS_v10_na_shp.zip into Geo/datasets/HydroRIVERS_v10_na_shp/ "
            "(keep .shp, .shx, .dbf and .prj together)."
        )

    area = box(bounds.lon_min, bounds.lat_min, bounds.lon_max, bounds.lat_max)
    transformer = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)
    scale = min(abs(grid.transform.a * grid.width / output_shape[1]),
                abs(grid.transform.e * grid.height / output_shape[0]))
    tolerance = scale / 2
    min_area = float(cfg.RIVER_MIN_UPSTREAM_AREA_SQKM)
    if min_area < 0:
        raise ValueError("RIVER_MIN_UPSTREAM_AREA_SQKM must be nonnegative")
    if cfg.RIVER_AUTO_SCALE_UPSTREAM_AREA:
        # The 25,600-block preset defines the configured threshold. Use the
        # coarsest output axis so non-square maps do not retain tiny tributaries.
        min_area *= max(1.0, 25600 / min(output_shape))
    min_flow = float(cfg.RIVER_MIN_FLOW_CMS)
    if not math.isfinite(min_flow) or min_flow < 0:
        raise ValueError("RIVER_MIN_FLOW_CMS must be finite and nonnegative")
    reaches = []
    outlets = []
    with fiona.open(source) as rivers:
        if rivers.crs != fiona.crs.CRS.from_epsg(4326):
            raise ValueError(f"Expected EPSG:4326 HydroRIVERS data: {source}")
        for feature in rivers.filter(bbox=area.bounds):
            upstream_area = feature["properties"]["UPLAND_SKM"]
            if upstream_area is None or upstream_area < min_area:
                continue
            discharge = feature["properties"]["DIS_AV_CMS"]
            if discharge is None or not math.isfinite(discharge) or discharge < min_flow:
                continue
            geometry = shape(feature["geometry"])
            if not geometry.intersects(area):
                continue
            projected = project(transformer.transform, geometry.intersection(area))
            for line in (projected.geoms if projected.geom_type == "MultiLineString" else (projected,)):
                if line.geom_type != "LineString":
                    continue
                simplified = line.simplify(tolerance, preserve_topology=True)
                if simplified.length >= scale:
                    if (geometry.geom_type == "LineString"
                            and feature["properties"]["NEXT_DOWN"] == 0
                            and area.covers(Point(geometry.coords[-1]))
                            and Point(simplified.coords[-1]).distance(
                                project(transformer.transform, Point(geometry.coords[-1]))) <= scale):
                        outlets.append((simplified.coords[-1], discharge, upstream_area))
                    reaches.append((simplified, discharge))
    if not reaches:
        raise ValueError(f"No HydroRIVERS reaches intersect {area.bounds}")
    print(f"  HydroRIVERS reaches: {len(reaches)} (upstream area >= {min_area:g} km², "
          f"mean flow >= {min_flow:g} m³/s); output pixel ~{scale:.0f} m", flush=True)
    return reaches, scale, outlets



def _coastal_connectors(outlets, grid, height, land, lake_mask, cfg):
    """Join mapped terminal reaches to nearby coast only across sea-level land."""
    rows, cols = height.shape
    transform = grid.transform @ Affine.scale(grid.width / cols, grid.height / rows)
    inverse = ~transform
    pixel_width, pixel_height = abs(transform.a), abs(transform.e)
    limit = float(cfg.RIVER_COASTAL_CONNECTION_MAX_DISTANCE_METRES)
    if limit <= 0:
        return []
    radius = math.ceil(limit / min(pixel_width, pixel_height)) + 2
    sea_level = int(cfg.TERRAIN_SEA_LEVEL_Y)
    connectors = []
    for endpoint, discharge, upstream_area in outlets:
        if upstream_area < cfg.RIVER_COASTAL_CONNECTION_MIN_UPSTREAM_AREA_SQKM:
            continue
        x, z = (round(value - 0.5) for value in inverse * endpoint)
        if not (0 <= x < cols and 0 <= z < rows):
            continue
        if land[z, x] == 0 or lake_mask[z, x] or height[z, x] > sea_level:
            continue
        x0, x1 = max(0, x - radius), min(cols, x + radius + 1)
        z0, z1 = max(0, z - radius), min(rows, z + radius + 1)
        coastal_land = land[z0:z1, x0:x1]
        if np.all(coastal_land):
            continue
        distances, nearest = distance_transform_edt(
            coastal_land > 0, sampling=(pixel_height, pixel_width),
            return_indices=True)
        row, col = z - z0, x - x0
        distance = distances[row, col]
        if distance <= max(pixel_width, pixel_height) * 2 or distance > limit:
            continue
        coast_z, coast_x = nearest[:, row, col]
        count = math.ceil(math.hypot(coast_x - col, coast_z - row))
        path_z = np.rint(np.linspace(row, coast_z, count + 1)).astype(np.int32)
        path_x = np.rint(np.linspace(col, coast_x, count + 1)).astype(np.int32)
        # Do not cut a ridge, an existing lake, or a second channel to get
        # through a delta. Reaches without a level corridor stay unchanged.
        if (np.any(height[z0 + path_z[:-1], x0 + path_x[:-1]] > sea_level)
                or np.any(lake_mask[z0 + path_z, x0 + path_x])
                or np.any(coastal_land[path_z[:-1], path_x[:-1]] == 0)):
            continue
        coast_point = transform * (x0 + coast_x + 0.5, z0 + coast_z + 0.5)
        connectors.append((LineString([endpoint, coast_point]), discharge))
    return connectors


def _grade_banks(height, river, surface, land, width, slope):
    covered = river.copy()
    propagated = np.where(river, surface, 255)
    for distance in range(1, int(width) + 1):
        # A bank can touch several water columns. Grade from the highest one
        # on the first ring so a lower neighbor cannot cut below its waterline.
        if distance == 1:
            next_surface = maximum_filter(np.where(river, surface, 0), size=3, mode="nearest")
            next_surface[minimum_filter(propagated, size=3, mode="nearest") == 255] = 255
        else:
            next_surface = minimum_filter(propagated, size=3, mode="nearest")
        ring = (~covered) & land & (next_surface < 255)
        if not ring.any():
            break
        # Each ring is visited once, so these cells still hold natural terrain.
        natural = height[ring].astype(np.int16)
        target = next_surface[ring].astype(np.int16)
        # Cut high banks down gradually; never build a berm above low terrain.
        height[ring] = np.minimum(natural, target + distance * int(slope)).astype(np.uint8)
        propagated[ring] = next_surface[ring]
        covered[ring] = True
    return height


def build_river_maps(reaches, grid: MasterGrid, height, land, lake_mask, cfg):
    """Build binary mask, absolute-Y surface, and block depth on the final grid."""
    rows, cols = height.shape
    transform = grid.transform @ Affine.scale(grid.width / cols, grid.height / rows)
    pixel_size = min(abs(transform.a), abs(transform.e))
    # Select cell centers; all_touched expands an eight-block channel to nine.
    mask = rasterize(
        ((line.buffer(river_width_metres(flow, pixel_size, cfg) / 2), 1)
         for line, flow in reaches),
        out_shape=(rows, cols), transform=transform, fill=0,
        all_touched=False, dtype=np.uint8,
    ).astype(bool) if reaches else np.zeros((rows, cols), dtype=bool)
    river = mask & (lake_mask == 0) & (land > 0)
    del mask
    lake = lake_mask > 0
    # Water cannot stand above an adjacent dry bank; downstream fitting
    # carries this limit back through the connected river.
    dry = (land > 0) & ~river & ~lake
    shore_ceiling = minimum_filter(np.where(dry, height, 255), size=3)
    del dry
    fit_height = height.copy()
    fit_height[river] = np.minimum(height[river], shore_ceiling[river])
    local_floor = minimum_filter(fit_height, size=int(cfg.RIVER_SURFACE_WINDOW_BLOCKS), mode="nearest")
    smoothed = median_filter(local_floor, size=3, mode="nearest")
    river_surface = np.zeros_like(height)
    river_surface[river] = np.minimum(smoothed[river], local_floor[river])
    del local_floor, smoothed

    lake_neighbor_surface = minimum_filter(
        np.where(lake, height, 255), size=3, mode="nearest")
    lake_join = river & binary_dilation(lake, iterations=1)
    river_surface[lake_join] = lake_neighbor_surface[lake_join]
    del lake_neighbor_surface, lake_join
    river_surface = fitted_river_surface([line for line, _ in reaches], transform, fit_height,
                                        river, lake, land > 0, river_surface, shore_ceiling)
    del fit_height, lake, shore_ceiling
    shore_distance = distance_transform_cdt(river, metric="chessboard")
    river_depth = np.zeros_like(height)
    river_depth[river] = np.clip(
        shore_distance[river], int(cfg.RIVER_MIN_DEPTH_BLOCKS),
        int(cfg.RIVER_MAX_DEPTH_BLOCKS),
    ).astype(np.uint8)
    del shore_distance

    # Fitted water fills the excavated channel; dry banks are only cut, not raised.
    height[river] = river_surface[river]
    _grade_banks(height, river, river_surface, land > 0,
                 cfg.RIVER_BANK_WIDTH_BLOCKS, cfg.RIVER_BANK_SLOPE)
    mask = river.astype(np.uint8)
    mask *= 255
    return mask, river_surface, river_depth


def write_river_maps(work_dir: Path, build_dir: Path, grid: MasterGrid, bounds, cfg):
    with Image.open(build_dir / "heightmap.png") as image:
        height = np.array(image.convert("L"), dtype=np.uint8)
    with Image.open(build_dir / "landmask.png") as image:
        land = np.array(image.convert("L"), dtype=np.uint8)
    with Image.open(build_dir / "lake_mask.png") as image:
        lake_mask = np.array(image.convert("L"), dtype=np.uint8)

    reaches, _, outlets = load_river_lines(
        work_dir.parent / "datasets", bounds, grid, height.shape, cfg)
    connectors = _coastal_connectors(outlets, grid, height, land, lake_mask, cfg)
    if connectors:
        print(f"  Coastal river connectors: {len(connectors)}", flush=True)
        reaches.extend(connectors)
    mask, surface, depth = build_river_maps(reaches, grid, height, land, lake_mask, cfg)
    Image.fromarray(height).save(build_dir / "heightmap.png")
    Image.fromarray(mask).save(build_dir / "river.png")
    Image.fromarray(surface).save(build_dir / "river_surface.png")
    Image.fromarray(depth).save(build_dir / "river_depth.png")
