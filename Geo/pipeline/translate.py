"""Convert intermediate rasters and build final-grid lake and river PNGs."""
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from PIL import Image

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
    _tif_to_png(
        work_dir / "bathymetry.tif",
        build_dir / "bathymetry_heightmap.png",
        out_w, out_h,
    )

    _write_heightmap(work_dir / "cropped_dem.tif", build_dir / "heightmap.png",
                     out_w, out_h, cfg)

    _tif_to_png(
        work_dir / "land_osm_mask.tif",
        build_dir / "landmask.png",
        out_w, out_h,
        resample=Image.Resampling.NEAREST,
    )
    write_lake_maps(work_dir, build_dir, grid, cfg)
    write_coastal_maps(build_dir, cfg)

    # Smooth class membership, not numeric biome IDs.
    _tif_to_png(work_dir / "vegetation.tif", build_dir / "vegetation.png",
                out_w, out_h, resample=Image.Resampling.NEAREST)
    _feather_vegetation(build_dir)
    (build_dir / "tree.png").unlink(missing_ok=True)

    # Local HydroRIVERS lines operate on the final grid after lakes establish precedence.
    write_river_maps(work_dir, build_dir, grid, bounds, cfg)

    print("[translate] All PNGs written to", build_dir)


def _feather_vegetation(build_dir: Path):
    """Feather categorical PNV edges in row bands while preserving water."""
    target = build_dir / "vegetation.png"
    temporary = build_dir / "vegetation.feathered.png"
    try:
        with rasterio.open(target) as source, rasterio.open(build_dir / "landmask.png") as land:
            if (source.width, source.height) != (land.width, land.height):
                raise ValueError("PNV and final land mask dimensions do not match")
            image = Image.new("L", (source.width, source.height))
            for y in range(0, source.height, 256):
                rows = min(256, source.height - y)
                start = max(0, y - 4)
                stop = min(source.height, y + rows + 4)
                window = rasterio.windows.Window(0, start, source.width, stop - start)
                classes = source.read(1, window=window)
                valid = land.read(1, window=window) != 0
                classes[~valid] = 0
                image.paste(Image.fromarray(
                    _feather_pnv_band(classes, valid, y - start, rows, y)), (0, y))
            image.save(temporary)
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

        # Pillow's mapped image is written bandwise; each source read is bounded.
        image = Image.new("L", (width, height))
        for y in range(0, height, 512):
            count = min(512, height - y)
            band = _encode_terrain_y(rows(y, count), cfg, peak)
            image.paste(Image.fromarray(band), (0, y))
        image.save(dst)
    print(f"[topo] Peak elevation {peak} m -> Y={255 if peak > 0 else int(cfg.TERRAIN_SEA_LEVEL_Y)}")


def _tif_to_png(
    src: Path,
    dst: Path,
    out_w, out_h,
    src_range=None,
    dst_range=None,
    multiband: bool = False,
    resample=None,
):
    """Read a TIF, optionally rescale, optionally resize, save as PNG."""
    if not src.exists():
        print(f"  Warning: {src.name} not found, skipping.")
        return

    with rasterio.open(str(src)) as r:
        if multiband and r.count >= 3:
            bands = [r.read(i + 1) for i in range(3)]
            if src_range:
                bands = [b.astype(np.float64) for b in bands]
                bands = [
                    np.clip(
                        (b - src_range[0]) / (src_range[1] - src_range[0]) * (dst_range[1] - dst_range[0]) + dst_range[0],
                        dst_range[0], dst_range[1],
                    )
                    for b in bands
                ]
            arr_rgb = np.stack([b.astype(np.uint8, copy=False) for b in bands], axis=-1)
            img = Image.fromarray(arr_rgb, mode="RGB")
        else:
            arr = r.read(1)
            if src_range:
                arr = arr.astype(np.float64)
                lo, hi = src_range
                t_lo, t_hi = dst_range
                if hi == lo:
                    arr = np.full_like(arr, t_lo)
                else:
                    arr = (arr - lo) / (hi - lo) * (t_hi - t_lo) + t_lo
            if arr.dtype != np.uint8:
                arr = np.clip(arr, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr, mode="L")

    if out_w and out_h:
        if resample is None:
            resample = Image.Resampling.LANCZOS if img.mode == "RGB" else Image.Resampling.BILINEAR
        resized = img.resize((out_w, out_h), resample)
        img.close()
        img = resized

    img.save(str(dst))

