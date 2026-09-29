"""Download and project categorical potential natural vegetation classes."""
from pathlib import Path

import numpy as np
from scipy.ndimage import distance_transform_edt
import rasterio
from rasterio.warp import Resampling

from util.projection import MasterGrid, Bounds4326
from util.raster import download_file, save_array, warp_to_grid


PNV_CLASS_IDS = frozenset((
    1, 2, 3, 4, 7, 8, 9, 13, 14, 15, 16, 17, 18, 19, 20, 22,
    27, 28, 30, 31, 32,
))


def _validate_raster(path: Path) -> None:
    with rasterio.open(path) as source:
        if source.count != 1 or source.crs is None:
            raise ValueError(f"Invalid PNV raster: {path.name}")


def _get_dataset(datasets_dir: Path, cfg) -> Path:
    """Use the local pinned PNV GeoTIFF, downloading it atomically if absent."""
    source = datasets_dir / cfg.PNV_DATASET_FILENAME
    if source.is_file():
        _validate_raster(source)
        return source

    datasets_dir.mkdir(parents=True, exist_ok=True)
    temporary = source.with_suffix(source.suffix + ".tmp")
    try:
        download_file(cfg.PNV_DATASET_URL, str(temporary), desc=source.name)
        _validate_raster(temporary)
        temporary.replace(source)
    finally:
        temporary.unlink(missing_ok=True)
    return source


def _validate_classes(values: np.ndarray) -> None:
    present = set(np.unique(values))
    unknown = present.difference(PNV_CLASS_IDS, (0,))
    if unknown:
        raise ValueError(f"Unknown nonzero PNV class IDs: {sorted(unknown)}")


def _fill_nodata_land(vegetation: np.ndarray, land: np.ndarray) -> int:
    """Copy the nearest categorical PNV class into every unclassified land cell."""
    missing = (vegetation == 0) & (land != 0)
    count = int(np.count_nonzero(missing))
    if not count:
        return 0
    if not np.any(vegetation):
        raise ValueError("Cannot fill PNV NoData land without a classified PNV cell")
    nearest = distance_transform_edt(vegetation == 0, return_distances=False,
                                     return_indices=True)
    vegetation[missing] = vegetation[nearest[0, missing], nearest[1, missing]]
    return count


def run(work_dir: Path, datasets_dir: Path, grid: MasterGrid,
        bounds: Bounds4326, cfg):
    """Write master-grid ``vegetation.tif`` with PNV IDs and zero for water."""
    source = _get_dataset(datasets_dir, cfg)
    with rasterio.open(source) as raster:
        source_nodata = raster.nodata

    intermediate = work_dir / "vegetation.projected.tif"
    try:
        vegetation = warp_to_grid(
            str(source), str(intermediate), grid, dtype="uint8",
            resampling=Resampling.nearest, nodata=0, src_nodata=source_nodata,
        )
        if source_nodata is not None:
            vegetation[vegetation == source_nodata] = 0
        _validate_classes(vegetation)

        with rasterio.open(work_dir / "land_osm_mask.tif") as land_source:
            if (land_source.width, land_source.height) != (grid.width, grid.height):
                raise ValueError("Land mask does not match the PNV master grid")
            land = land_source.read(1)
        vegetation[land == 0] = 0
        filled = _fill_nodata_land(vegetation, land)
        print(f"[vegetation] Filled {filled} PNV NoData land cells from nearest classes")
        save_array(vegetation, str(work_dir / "vegetation.tif"), grid,
                   dtype="uint8", nodata=0)
    finally:
        intermediate.unlink(missing_ok=True)

    (work_dir / "tree.tif").unlink(missing_ok=True)
    print("[vegetation] Potential natural vegetation done.")
