"""
pipeline/topography.py — GEBCO + GMTED bathymetry/topography processing.
Replaces topography.sh, aligner.py, topography_processor.py.
"""
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.features import rasterize as rio_rasterize
from shapely.geometry import box, shape, mapping
from shapely.ops import transform as shp_transform
from pyproj import Transformer
import fiona

from util.projection import MasterGrid, Bounds4326
from util.raster import warp_to_grid, rescale, align_to_grid, save_array


def run(work_dir: Path, datasets_dir: Path, grid: MasterGrid, bounds: Bounds4326, cfg):
    """
    Produces:
      - work_dir/bathymetry/crop.tif              (Int16, master grid, GEBCO)
      - work_dir/bathymetry.tif                   (Byte, scaled ocean depths)
      - work_dir/dem/crop_gmted_for_lakes.tif     (Int16, master grid)
      - work_dir/cropped_dem.tif                  (UInt16, baseline land/lake elevations)
    """
    bathy_dir = work_dir / "bathymetry"
    dem_dir = work_dir / "dem"
    bathy_dir.mkdir(parents=True, exist_ok=True)
    dem_dir.mkdir(parents=True, exist_ok=True)

    lon_min, lat_min, lon_max, lat_max = bounds

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

    # ------------------------------------------------------------------ #
    # 4. Encode signed elevations as absolute Vintage Story world Y.
    #    This preserves depressions such as the Dead Sea below Y=92.
    # ------------------------------------------------------------------ #
    gebco_y = _encode_terrain_y(gebco_arr, cfg)
    gmted_y = _encode_terrain_y(gmted_arr, cfg)

    gebco_norm_path = bathy_dir / "gebco_land_normalized.tif"
    gmted_norm_path = dem_dir / "dem_land_normalized.tif"
    save_array(gebco_y, str(gebco_norm_path), grid, dtype="uint8", nodata=0)
    save_array(gmted_y, str(gmted_norm_path), grid, dtype="uint8", nodata=0)

    # ------------------------------------------------------------------ #
    # 5. Build initial lake mask from crop_lakes.gpkg
    # ------------------------------------------------------------------ #
    lakes_mask_arr = _rasterize_lakes(
        work_dir / "crop_lakes.gpkg", grid, bounds
    )
    save_array(
        lakes_mask_arr,
        str(work_dir / "lakes_mask_initial.tif"),
        grid,
        dtype="uint8",
        nodata=0,
    )

    # ------------------------------------------------------------------ #
    # 6. Align GMTED and lake mask to GEBCO reference grid
    #    (inline port of aligner.py — they're already on the same master grid,
    #    so this is a no-op but kept for correctness if resampling drifted)
    # ------------------------------------------------------------------ #
    # The gebco_norm is already the reference. GMTED was warped to the same
    # grid in step 2, so alignment is guaranteed. Copy to _aligned paths.
    gebco_aligned = bathy_dir / "gebco_land_aligned.tif"
    import shutil
    shutil.copy(gebco_norm_path, gebco_aligned)

    gmted_aligned = dem_dir / "dem_land_aligned.tif"
    align_to_grid(str(gmted_norm_path), str(gmted_aligned), grid)

    lakes_mask_aligned_path = work_dir / "lakes_mask_initial_aligned.tif"
    align_to_grid(str(work_dir / "lakes_mask_initial.tif"), str(lakes_mask_aligned_path), grid)

    # ------------------------------------------------------------------ #
    # 7. Use GMTED at inland lakes to seed their surface estimates. The final
    # polygon footprint, level, bed and bank are computed after PNG resizing.
    # ------------------------------------------------------------------ #
    _merge_topography(
        gmted_aligned,
        gebco_aligned,
        lakes_mask_aligned_path,
        work_dir / "cropped_dem.tif",
        grid,
    )
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
    Output: uint8 array with BATHY_SCALE_MAXDEPTH..BATHY_SCALE_SEALEVEL range.
    """
    to_high = float(cfg.BATHY_SCALE_SEALEVEL)
    to_low = float(cfg.BATHY_SCALE_MAXDEPTH)
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


def _encode_terrain_y(elevation_metres: np.ndarray, cfg) -> np.ndarray:
    """Encode signed metres as absolute world Y; zero remains the sea datum."""
    elevation = elevation_metres.astype(np.float64)
    invalid = (elevation <= -32768) | ~np.isfinite(elevation)
    world_y = float(cfg.TERRAIN_SEA_LEVEL_Y) + np.rint(
        elevation / float(cfg.TERRAIN_METRES_PER_BLOCK)
    )
    world_y = np.clip(world_y, 1, int(cfg.TERRAIN_MAX_Y)).astype(np.uint8)
    world_y[invalid] = 0
    return world_y

def _rasterize_lakes(gpkg_path: Path, grid: MasterGrid, bounds: Bounds4326) -> np.ndarray:
    """Rasterize lake polygons onto the master grid (1=lake, 0=no lake)."""
    if not gpkg_path.exists():
        print("  Warning: crop_lakes.gpkg not found, lake mask will be empty.")
        return np.zeros((grid.height, grid.width), dtype=np.uint8)

    lon_min, lat_min, lon_max, lat_max = bounds
    bbox_geom = box(lon_min, lat_min, lon_max, lat_max)
    transformer = Transformer.from_crs("EPSG:4326", grid.crs, always_xy=True)

    shapes = []
    with fiona.open(str(gpkg_path)) as src:
        for feat in src:
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


def _merge_topography(
    gmted_path: Path,
    gebco_path: Path,
    mask_path: Path,
    out_dem_path: Path,
    grid: MasterGrid,
):
    """Use GMTED world Y within every lake footprint, GEBCO elsewhere."""
    with rasterio.open(str(gmted_path)) as src:
        dem_arr = src.read(1, masked=True).astype(np.int32).filled(0)
    with rasterio.open(str(gebco_path)) as src:
        gebco_arr = src.read(1, masked=True).astype(np.int32).filled(0)
    with rasterio.open(str(mask_path)) as src:
        mask_arr = src.read(1, masked=True).filled(0).astype(np.uint8)

    surface_map = gebco_arr.copy()
    np.copyto(surface_map, dem_arr, where=mask_arr == 1)
    surface_map = np.clip(surface_map, 0, 255).astype(np.uint8)

    save_array(surface_map, str(out_dem_path), grid, dtype="uint8", nodata=0)
