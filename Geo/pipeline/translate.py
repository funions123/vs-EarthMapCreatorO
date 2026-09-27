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
    Produces 10 aligned PNGs in work_dir/build/:
      bathymetry_heightmap.png, heightmap.png, lake_mask.png, lake_depth.png,
      river.png, river_surface.png, river_depth.png, landmask.png, climate.png,
      tree.png
    """
    build_dir = work_dir / "build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "complete_topo.png").unlink(missing_ok=True)

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

    # 6. Climate / Köppen RGB (3-band Byte)
    koppen_rgb = work_dir / "koppen_climate_rgb.tif"
    _tif_to_png(koppen_rgb, build_dir / "climate.png", out_w, out_h,
                multiband=True, resample=Image.Resampling.NEAREST)

    # 7. Tree (Byte)
    _tif_to_png(work_dir / "tree.tif", build_dir / "tree.png", out_w, out_h)

    # Local HydroRIVERS lines operate on the final grid after lakes establish precedence.
    write_river_maps(work_dir, build_dir, grid, bounds, cfg)

    print("[translate] All PNGs written to", build_dir)


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
            bands = [r.read(i + 1).astype(np.float64) for i in range(3)]
            if src_range:
                bands = [
                    np.clip(
                        (b - src_range[0]) / (src_range[1] - src_range[0]) * (dst_range[1] - dst_range[0]) + dst_range[0],
                        dst_range[0], dst_range[1],
                    )
                    for b in bands
                ]
            arr_rgb = np.stack([b.astype(np.uint8) for b in bands], axis=-1)
            img = Image.fromarray(arr_rgb, mode="RGB")
        else:
            arr = r.read(1).astype(np.float64)
            if src_range:
                lo, hi = src_range
                t_lo, t_hi = dst_range
                if hi == lo:
                    arr = np.full_like(arr, t_lo)
                else:
                    arr = (arr - lo) / (hi - lo) * (t_hi - t_lo) + t_lo
            arr = np.clip(arr, 0, 255).astype(np.uint8)
            img = Image.fromarray(arr, mode="L")

    if out_w and out_h:
        if resample is None:
            resample = Image.Resampling.LANCZOS if img.mode == "RGB" else Image.Resampling.BILINEAR
        img = img.resize((out_w, out_h), resample)

    img.save(str(dst))

