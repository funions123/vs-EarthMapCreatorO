"""
util/projection.py — CRS selection and master grid computation.
Replaces getutm_epsg.py and getbbox_nativeproj_fromraster.py.
"""
import math
from collections import namedtuple

from pyproj import CRS
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
from rasterio.crs import CRS as RioCRS
from rasterio.transform import from_origin
from rasterio.transform import array_bounds
from rasterio.warp import transform_bounds
from pyproj import Transformer

MasterGrid = namedtuple("MasterGrid", ["transform", "width", "height", "crs"])
Bounds4326 = namedtuple("Bounds4326", ["lon_min", "lat_min", "lon_max", "lat_max"])


def get_projection(cfg) -> str:
    """
    Return a CRS authority string for the project projection.
    Uses FORCE_FINAL_PROJ if set, otherwise auto-selects UTM (span < 13°)
    or EPSG:3857 (wider areas). Mirrors getutm_epsg.py logic.
    """
    if cfg.FORCE_FINAL_PROJ:
        return cfg.FORCE_FINAL_PROJ

    lat_center = (cfg.LAT_MAX + cfg.LAT_MIN) / 2
    lon_center = (cfg.LON_MAX + cfg.LON_MIN) / 2

    if (cfg.LON_MAX - cfg.LON_MIN) < 13:
        utm_list = query_utm_crs_info(
            datum_name="WGS 84",
            area_of_interest=AreaOfInterest(
                west_lon_degree=lon_center,
                south_lat_degree=lat_center,
                east_lon_degree=lon_center,
                north_lat_degree=lat_center,
            ),
        )
        return str(CRS.from_epsg(utm_list[0].code))
    else:
        return "EPSG:3857"


def get_master_grid(cfg, proj_crs: str):
    """
    Compute the master raster grid from the config bbox and projection.
    Returns (MasterGrid, Bounds4326).

    Replaces the dummy.tif creation + gdalinfo dimension extraction.
    """
    rio_crs = RioCRS.from_user_input(proj_crs)

    west, south, east, north = transform_bounds(
        "EPSG:4326", rio_crs, cfg.LON_MIN, cfg.LAT_MIN,
        cfg.LON_MAX, cfg.LAT_MAX, densify_pts=21,
    )
    width = math.ceil((east - west) / cfg.FINAL_RES)
    height = math.ceil((north - south) / cfg.FINAL_RES)
    transform = from_origin(west, north, cfg.FINAL_RES, cfg.FINAL_RES)

    grid = MasterGrid(transform=transform, width=width, height=height, crs=rio_crs)
    bounds = get_actual_bounds_4326(grid)
    return grid, bounds


def get_actual_bounds_4326(grid: MasterGrid) -> Bounds4326:
    """
    Return the actual EPSG:4326 bounding box of the master grid after projection.
    Replaces getbbox_nativeproj_fromraster.py.
    """
    left, bottom, right, top = array_bounds(grid.height, grid.width, grid.transform)

    # Transform all four corners back to 4326 and take min/max
    transformer = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
    corners_x = [left, left, right, right]
    corners_y = [bottom, top, bottom, top]
    lons, lats = transformer.transform(corners_x, corners_y)

    return Bounds4326(
        lon_min=min(lons),
        lat_min=min(lats),
        lon_max=max(lons),
        lat_max=max(lats),
    )
