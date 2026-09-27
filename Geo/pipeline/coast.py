"""Convert defective coastal pixels to shallow ocean on the final block grid."""
from contextlib import ExitStack
from pathlib import Path

import numpy as np
import rasterio
from rasterio.shutil import copy as copy_raster
from rasterio.windows import Window
from scipy.ndimage import distance_transform_cdt


LAND_BAND = 16
ROWS = 512


def repair_coast(height, land, lake, bathy, sea, minimum_depth_y):
    """Replace bad ocean depths and submerged coastal land with one-block water."""
    ocean = (land == 0) & (lake == 0)
    if not ocean.any():
        return

    # Valid source bathymetry starts at minimum_depth_y. Resizing across
    # no-data zeros also produces spurious positive depths below that value.
    bathy[ocean & (bathy < minimum_depth_y)] = sea

    # Only convert the submerged dry strip next to existing ocean. Keep lakes
    # and inland depressions (including Death Valley) as they were.
    dry_distance = distance_transform_cdt(land > 0, metric="chessboard")
    flooded = ((land > 0) & (lake == 0) & (height < sea)
               & (dry_distance > 0) & (dry_distance <= LAND_BAND))
    land[flooded] = 0
    bathy[flooded] = sea
    height[flooded] = sea


def write_coastal_maps(build_dir: Path, cfg):
    """Replace defective shoreline pixels before river fitting and region bake."""
    names = ("heightmap", "bathymetry_heightmap", "landmask")
    scratch = [build_dir / f"{name}.coast.tmp.{ext}" for name in names for ext in ("tif", "png")]
    sea = int(cfg.TERRAIN_SEA_LEVEL_Y)
    depth = int(cfg.BATHY_SCALE_MAXDEPTH)
    halo = LAND_BAND
    try:
        with ExitStack() as stack:
            sources = {name: stack.enter_context(rasterio.open(build_dir / f"{name}.png"))
                       for name in (*names, "lake_mask")}
            width, height = sources["heightmap"].width, sources["heightmap"].height
            if any((src.width, src.height) != (width, height) for src in sources.values()):
                raise ValueError("Coastal raster dimensions do not match")
            outputs = {name: stack.enter_context(rasterio.open(
                build_dir / f"{name}.coast.tmp.tif", "w", driver="GTiff",
                width=width, height=height, count=1, dtype="uint8", compress="LZW"))
                for name in names}
            for z in range(0, height, ROWS):
                start = max(0, z - halo)
                end = min(height, z + ROWS + halo)
                window = Window(0, start, width, end - start)
                arrays = {name: source.read(1, window=window) for name, source in sources.items()}
                repair_coast(arrays["heightmap"], arrays["landmask"], arrays["lake_mask"],
                             arrays["bathymetry_heightmap"], sea, depth)
                count = min(ROWS, height - z)
                core = slice(z - start, z - start + count)
                for name in names:
                    outputs[name].write(arrays[name][core], 1, window=Window(0, z, width, count))

        for name in names:
            copy_raster(build_dir / f"{name}.coast.tmp.tif",
                        build_dir / f"{name}.coast.tmp.png", driver="PNG")
        for name in names:
            (build_dir / f"{name}.coast.tmp.png").replace(build_dir / f"{name}.png")
    finally:
        for path in scratch:
            path.unlink(missing_ok=True)
    print("[coast] Converted defective shore pixels to shallow ocean", flush=True)
