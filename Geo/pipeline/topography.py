"""
pipeline/topography.py — GEBCO + GMTED bathymetry/topography processing.
Replaces topography.sh, aligner.py, topography_processor.py.
"""
import zipfile
from pathlib import Path

import numpy as np
from rasterio.warp import Resampling
from rasterio.features import rasterize as rio_rasterize
from shapely.geometry import box, shape
from shapely.ops import transform as shp_transform
from pyproj import Transformer
import fiona

from util.projection import MasterGrid, Bounds4326
from util.raster import warp_to_grid, save_array

from pipeline.lake_classification import is_natural_lake

def run(work_dir: Path, datasets_dir: Path, grid: MasterGrid, bounds: Bounds4326, cfg):
    """
    Produces:
      - work_dir/bathymetry/crop.tif              (Int16, master grid, GEBCO)
      - work_dir/bathymetry.tif                   (Byte, scaled ocean depths)
      - work_dir/dem/crop_gmted_for_lakes.tif     (Int16, master grid)
      - work_dir/cropped_dem.tif                  (Int16, merged signed elevations)
    """
    bathy_dir = work_dir / "bathymetry"
    dem_dir = work_dir / "dem"
    bathy_dir.mkdir(parents=True, exist_ok=True)
    dem_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 1. Warp GEBCO to master grid (Int16)
    # ------------------------------------------------------------------ #
    gebco_crop = bathy_dir / "crop.tif"
    gebco_nc = _extract_gebco(datasets_dir)
    # GEBCO NetCDF: use forward slashes in GDAL virtual path (Windows-safe)
    gebco_nc_fwd = str(gebco_nc).replace("\\", "/")
    gebco_src = f"NETCDF:{gebco_nc_fwd}:elevation"

    gebco_arr = warp_to_grid(
        gebco_src,
        str(gebco_crop),
        grid,
        dtype="int16",
        resampling=Resampling.cubic_spline,
        nodata=-32768,
    )

    # ------------------------------------------------------------------ #
    # 2. Warp GMTED to master grid (Int16, exact pixel match via -ts)
    # ------------------------------------------------------------------ #
    gmted_crop = dem_dir / "crop_gmted_for_lakes.tif"
    gmted_adf = _extract_gmted(datasets_dir)

    gmted_arr = warp_to_grid(
        str(gmted_adf),
        str(gmted_crop),
        grid,
        dtype="int16",
        resampling=Resampling.cubic_spline,
        nodata=-32768,
    )

    # ------------------------------------------------------------------ #
    # 3. Bathymetry map (ocean depths → Byte)
    # ------------------------------------------------------------------ #
    bathy_raw_arr = gebco_arr.copy().astype(np.float64)
    bathy_raw_arr[bathy_raw_arr >= 0] = 0  # keep only negatives

    ocean_mask = gebco_arr < 0
    if ocean_mask.any():
        min_val = float(gebco_arr[ocean_mask].min())
    else:
        min_val = -1.0  # no ocean in area

    # Save raw bathymetry for reference
    save_array(
        bathy_raw_arr.astype(np.int16),
        str(bathy_dir / "bathymetry_raw.tif"),
        grid,
        dtype="int16",
        nodata=0,
    )

    bathy_scaled = _scale_bathymetry(bathy_raw_arr, min_val, ocean_mask, cfg)

    save_array(bathy_scaled, str(work_dir / "bathymetry.tif"), grid, dtype="uint8", nodata=0)

    # Merge before scaling so the peak is measured only from the source used
    # for each map pixel. Keep signed metres until the final output grid is set.
    lakes_mask_arr = _rasterize_lakes(work_dir / "crop_lakes.gpkg", grid, bounds)
    elevation = np.where(lakes_mask_arr == 1, gmted_arr, gebco_arr)
    save_array(elevation, str(work_dir / "cropped_dem.tif"), grid,
               dtype="int16", nodata=-32768)
    print("[topo] Topography processing done.")


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _extract_gebco(datasets_dir: Path) -> str:
    """
    Locate the GEBCO NetCDF file.
    Checks: standalone .nc in datasets/, then zip extraction.
    """
    # 1. Standalone .nc already available in datasets/
    nc_direct = list(datasets_dir.glob("*.nc"))
    if nc_direct:
        return str(nc_direct[0])

    # 2. Extract from zip
    zip_path = datasets_dir / "gebco_2025_sub_ice_topo.zip"
    if not zip_path.exists():
        raise FileNotFoundError(
            f"GEBCO dataset not found. "
            "Place gebco_2025_sub_ice_topo.zip or GEBCO_2025_sub_ice.nc in datasets/."
        )

    extract_dir = datasets_dir / "gebco_extracted"
    nc_glob = list(extract_dir.glob("*.nc")) if extract_dir.exists() else []

    if not nc_glob:
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)
        nc_glob = list(extract_dir.glob("*.nc"))

    if not nc_glob:
        raise FileNotFoundError(f"No .nc file found after extracting GEBCO zip to {extract_dir}")

    return str(nc_glob[0])


def _extract_gmted(datasets_dir: Path) -> Path:
    """Extract GMTED ADF from zip, return path to w001000.adf."""
    zip_path = datasets_dir / "ds75_grd.zip"
    if not zip_path.exists():
        raise FileNotFoundError(
            f"GMTED dataset not found at {zip_path}. "
            "Download ds75_grd.zip and place it in datasets/."
        )

    extract_dir = datasets_dir / "gmted_extracted"
    adf_glob = list(extract_dir.rglob("w001000.adf")) if extract_dir.exists() else []

    if not adf_glob:
        extract_dir.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extract_dir)
        adf_glob = list(extract_dir.rglob("w001000.adf"))

    if not adf_glob:
        raise FileNotFoundError(f"w001000.adf not found after extracting GMTED zip to {extract_dir}")

    return adf_glob[0]


def _scale_bathymetry(
    bathy_raw: np.ndarray,
    min_val: float,
    ocean_mask: np.ndarray,
    cfg,
) -> np.ndarray:
    """
    Apply linear or piecewise scaling to ocean depths.
    Input: negative-only float array (0 = not ocean).
    Output: uint8 array with BATHY_SCALE_MAXDEPTH..TERRAIN_SEA_LEVEL_Y range.
    """
    to_high = float(cfg.TERRAIN_SEA_LEVEL_Y)
    to_low = float(cfg.BATHY_SCALE_MAXDEPTH)
    if not 1 <= to_low < to_high < 255:
        raise ValueError("BATHY_SCALE_MAXDEPTH must be between 1 and TERRAIN_SEA_LEVEL_Y")
    if cfg.BATHY_USE_PIECEWISE_SCALE and not to_low <= cfg.BATHY_EXAGGERATE_MIDPOINT <= to_high:
        raise ValueError("BATHY_EXAGGERATE_MIDPOINT must lie between ocean depth and sea level")
    arr = bathy_raw.astype(np.float64)

    if not cfg.BATHY_USE_PIECEWISE_SCALE:
        # Linear: map [min_val, 0] -> [to_low, to_high]
        abs_min = abs(min_val)
        scaled = ((arr + abs_min) / abs_min) * (to_high - to_low) + to_low
    else:
        threshold = float(cfg.BATHY_EXAGGERATE_THRESHOLD)  # e.g. -100
        mid = float(cfg.BATHY_EXAGGERATE_MIDPOINT)         # e.g. 80

        # Shallow: arr in (threshold, 0) → map to [mid, to_high]
        shallow_range = 0.0 - threshold
        shallow = mid + ((arr - threshold) / shallow_range) * (to_high - mid)

        # Deep: arr in [min_val, threshold] → map to [to_low, mid]
        deep_range = threshold - min_val
        if abs(deep_range) < 1e-9:
            deep = np.full_like(arr, to_low)
        else:
            deep = to_low + ((arr - min_val) / deep_range) * (mid - to_low)

        scaled = np.where(arr > threshold, shallow, deep)

    # Only apply to ocean pixels; set everything else to 0
    result = np.where(ocean_mask, scaled, 0.0)
    return np.clip(result, 0, 255).astype(np.uint8)


def _encode_terrain_y(elevation_metres: np.ndarray, cfg, peak=None) -> np.ndarray:
    """Map the highest positive elevation to Y=255, anchored at the sea datum."""
    elevation = np.asarray(elevation_metres)
    invalid = (elevation <= -32768) | ~np.isfinite(elevation)
    valid = elevation[~invalid]
    sea = float(cfg.TERRAIN_SEA_LEVEL_Y)
    if not 1 <= sea < 255:
        raise ValueError("TERRAIN_SEA_LEVEL_Y must be between 1 and 254")
    if peak is None:
        if not valid.size:
            raise ValueError("No valid elevations in source DEM")
        peak = max(0.0, float(valid.max()))
    # With no positive terrain, zero metres remains at sea level.
    scale = (255 - sea) / peak if peak > 0 else 0.0
    world_y = np.clip(sea + np.rint(elevation.astype(np.float32) * scale), 1, 255)
    world_y[invalid] = 0
    return world_y.astype(np.uint8)

def _rasterize_lakes(gpkg_path: Path, grid: MasterGrid, bounds: Bounds4326) -> np.ndarray:
    """Rasterize natural lake polygons onto the master grid (1=lake, 0=no lake)."""
    if not gpkg_path.exists():
        print("  Warning: crop_lakes.gpkg not found, lake mask will be empty.")
        return np.zeros((grid.height, grid.width), dtype=np.uint8)

    lon_min, lat_min, lon_max, lat_max = bounds
    transformer = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)

    shapes = []
    with fiona.open(str(gpkg_path)) as src:
        if "featurecla" not in src.schema["properties"]:
            raise ValueError("Lake polygons are missing featurecla")
        for feat in src:
            if not feat["geometry"] or not is_natural_lake(feat["properties"]):
                continue
            geom = shape(feat["geometry"])
            # Buffer by ~20m equivalent (matches bash: ST_Buffer(geom, 20) in proj CRS)
            proj_geom = shp_transform(transformer.transform, geom)
            buffered = proj_geom.buffer(20)
            shapes.append((buffered.__geo_interface__, 1))

    if not shapes:
        return np.zeros((grid.height, grid.width), dtype=np.uint8)

    return rio_rasterize(
        shapes,
        out_shape=(grid.height, grid.width),
        transform=grid.transform,
        fill=0,
        dtype=np.uint8,
    )


