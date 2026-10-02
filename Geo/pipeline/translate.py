"""Build tiled final-grid layers and export immutable PNG previews."""
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from PIL import Image
from rasterio.shutil import copy as copy_raster
from util.working import create_layer

# Final maps intentionally exceed Pillow's generic decompression-bomb threshold.
Image.MAX_IMAGE_PIXELS = None

from util.projection import MasterGrid
from pipeline.coast import write_coastal_maps
from pipeline.lakes import write_lake_maps
from pipeline.rivers import write_river_maps
from pipeline.topography import _encode_terrain_y


def run(work_dir: Path, grid: MasterGrid, bounds, cfg):
    """
    Produces 9 aligned PNGs in work_dir/build/:
      bathymetry_heightmap.png, heightmap.png, lake_mask.png, lake_depth.png,
      river.png, river_surface.png, river_depth.png, landmask.png,
      vegetation.png
    """
    build_dir = work_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "complete_topo.png").unlink(missing_ok=True)
    (build_dir / "climate.png").unlink(missing_ok=True)

    out_w = cfg.FINAL_WIDTH if cfg.RESIZE_MAP else None
    out_h = cfg.FINAL_LENGTH if cfg.RESIZE_MAP else None

    # 1. Bathymetry (Byte, no rescale needed)
    _resize_byte_layer(
        work_dir / "bathymetry.tif",
        build_dir / "bathymetry_heightmap.tif",
        out_w, out_h,
    )

    _write_heightmap(work_dir / "cropped_dem.tif", build_dir / "heightmap.tif",
                     out_w, out_h, cfg)

    _resize_byte_layer(
        work_dir / "land_osm_mask.tif",
        build_dir / "landmask.tif",
        out_w, out_h,
        resample=Image.Resampling.NEAREST,
    )
    write_lake_maps(work_dir, build_dir, grid, cfg)
    write_coastal_maps(build_dir, cfg)

    # Smooth class membership, not numeric biome IDs.
    _resize_byte_layer(work_dir / "vegetation.tif", build_dir / "vegetation.tif",
                       out_w, out_h, resample=Image.Resampling.NEAREST)
    _feather_vegetation(build_dir)
    (build_dir / "tree.png").unlink(missing_ok=True)

    # Local HydroRIVERS lines operate on the final grid after lakes establish precedence.
    write_river_maps(work_dir, build_dir, grid, bounds, cfg)
    from pipeline.region_store import LAYERS
    for name in LAYERS:
        copy_raster(build_dir / f"{name}.tif", build_dir / f"{name}.png", driver="PNG")

    print("[translate] All PNGs written to", build_dir)


def _feather_vegetation(build_dir: Path):
    """Feather categorical PNV edges in row bands while preserving water."""
    target = build_dir / "vegetation.tif"
    temporary = build_dir / "vegetation.feathered.tif"
    try:
        with rasterio.open(target) as source, rasterio.open(build_dir / "landmask.tif") as land, create_layer(temporary, source.width, source.height) as output:
            if (source.width, source.height) != (land.width, land.height):
                raise ValueError("PNV and final land mask dimensions do not match")
            for y in range(0, source.height, 256):
                rows = min(256, source.height - y)
                start = max(0, y - 4)
                stop = min(source.height, y + rows + 4)
                window = rasterio.windows.Window(0, start, source.width, stop - start)
                classes = source.read(1, window=window)
                valid = land.read(1, window=window) != 0
                classes[~valid] = 0
                output.write(_feather_pnv_band(classes, valid, y - start, rows, y),
                             1, window=rasterio.windows.Window(0, y, source.width, rows))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _feather_pnv_band(classes, valid, offset, rows, world_y):
    """Sample nearby class labels with deterministic, triangular jitter."""
    interior = slice(offset, offset + rows)
    output = classes[interior].copy()
    on_land = valid[interior]
    output[~on_land] = 0
    first = classes.flat[0]
    if np.all(classes == first):
        return output

    # The sum of two [-2, 2] offsets favors nearby pixels and reaches four
    # blocks across 6-block source tiles. World-coordinate hashing prevents
    # row stripes and region edges from introducing their own seams.
    x = np.arange(classes.shape[1], dtype=np.uint32)[None, :]
    z = np.arange(world_y, world_y + rows, dtype=np.uint32)[:, None]
    noise = (x * np.uint32(0x9E3779B1)) ^ (z * np.uint32(0x85EBCA77))
    noise ^= noise >> 16
    noise *= np.uint32(0x7FEB352D)
    noise ^= noise >> 15
    noise *= np.uint32(0x846CA68B)
    noise ^= noise >> 16
    dx = ((noise & 255) % 5).astype(np.int8) + (((noise >> 8) & 255) % 5).astype(np.int8) - 4
    dz = (((noise >> 16) & 255) % 5).astype(np.int8) + ((noise >> 24) % 5).astype(np.int8) - 4
    sample_x = np.clip(np.arange(classes.shape[1])[None, :] + dx, 0, classes.shape[1] - 1)
    sample_z = np.clip(np.arange(offset, offset + rows)[:, None] + dz, 0, classes.shape[0] - 1)
    sampled = classes[sample_z, sample_x]
    use_sample = on_land & (sampled != 0)
    output[use_sample] = sampled[use_sample]
    return output




# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _write_heightmap(src: Path, dst: Path, out_w, out_h, cfg):
    """Fit the final-grid peak to Y=255 without holding the world in memory."""
    with rasterio.open(src) as dem:
        width = out_w or dem.width
        height = out_h or dem.height
        def rows(y, count):
            window = rasterio.windows.Window(0, y * dem.height / height,
                                               dem.width, count * dem.height / height)
            return dem.read(1, window=window, out_shape=(count, width),
                            resampling=Resampling.bilinear, masked=True).filled(-32768)

        peak = 0
        has_data = False
        for y in range(0, height, 512):
            band = rows(y, min(512, height - y))
            valid = band[band > -32768]
            if valid.size:
                has_data = True
                peak = max(peak, int(valid.max()))
        if not has_data:
            raise ValueError("No valid elevations on final output grid")

        with create_layer(dst, width, height) as output:
            for y in range(0, height, 512):
                count = min(512, height - y)
                band = _encode_terrain_y(rows(y, count), cfg, peak)
                output.write(band, 1, window=rasterio.windows.Window(0, y, width, count))
    print(f"[topo] Peak elevation {peak} m -> Y={255 if peak > 0 else int(cfg.TERRAIN_SEA_LEVEL_Y)}")


def _resize_byte_layer(src: Path, dst: Path, out_w, out_h, resample=None):
    """Bound output memory while preserving Pillow's global byte resampling.

    Horizontal resizing uses Pillow. Vertical bilinear weights use its 22-bit
    fixed-point rounding and global coordinates, avoiding float32 band boxes.
    """
    with rasterio.open(src) as source:
        pixels = source.read(1)
    height, width = out_h or pixels.shape[0], out_w or pixels.shape[1]
    resample = Image.Resampling.BILINEAR if resample is None else resample
    with Image.fromarray(pixels) as image:
        horizontal = np.asarray(image.resize((width, pixels.shape[0]), resample))
    source_height = horizontal.shape[0]
    scale = source_height / height
    support = max(1.0, scale)
    with create_layer(dst, width, height) as output:
        for y in range(0, height, 512):
            count = min(512, height - y)
            if resample == Image.Resampling.NEAREST:
                indices = np.minimum(((np.arange(y, y + count) + 0.5) * scale).astype(int), source_height - 1)
                band = horizontal[indices]
            else:
                band = np.empty((count, width), dtype=np.uint8)
                for row in range(count):
                    center = (y + row + 0.5) * scale
                    start = max(0, int(center - support + 0.5))
                    stop = min(source_height, int(center + support + 0.5))
                    weights = np.maximum(0, 1 - np.abs((np.arange(start, stop) - center + 0.5) / support))
                    weights /= weights.sum()
                    coefficients = (weights * (1 << 22) + 0.5).astype(np.int32)
                    values = np.full(width, 1 << 21, dtype=np.int32)
                    for index, coefficient in zip(range(start, stop), coefficients):
                        values += horizontal[index].astype(np.int32) * coefficient
                    band[row] = np.clip(values >> 22, 0, 255).astype(np.uint8)
            output.write(band, 1, window=rasterio.windows.Window(0, y, width, count))

