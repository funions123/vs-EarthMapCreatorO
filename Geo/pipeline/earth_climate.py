"""Write EMCL v2 from CHELSA V2.1 1981–2010 monthly climatologies.

CHELSA tas is scaled kelvin; pr and pet are scaled kg/m²/month (mm/month).
Each sample also stores the mean final-map terrain Y of its 32-block cell and
the header stores a temperature lapse per source block, so the mod can cool
air above that terrain (including the world-top points the snow simulation
samples) and warm it below.
"""
from pathlib import Path
import os
import struct

import numpy as np
import rasterio
from affine import Affine
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window
from scipy.ndimage import distance_transform_edt, uniform_filter

from util.raster import download_file

SPACING = 32  # final-map blocks per sample
VERSION = 2
HEADER = struct.Struct("<4siiiif")  # magic, version, width, height, spacing, lapse
BASE_URL = "https://os.unil.cloud.switch.ch/chelsa02/chelsa/global/climatologies"


def _raster(datasets_dir: Path, variable: str, month: int) -> Path:
    name = f"CHELSA_{variable}_{month:02d}_1981-2010_V.2.1.tif"
    path = datasets_dir / name
    if not path.is_file():
        temporary = path.with_suffix(".tif.tmp")
        try:
            download_file(f"{BASE_URL}/{variable}/1981-2010/{name}",
                          str(temporary), desc=name)
            with rasterio.open(temporary) as src:
                if src.count != 1 or src.nodata is None or src.crs is None:
                    raise ValueError(f"Invalid CHELSA raster: {name}")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def _month(path: Path, shape, transform, crs):
    result = np.full(shape, np.nan, dtype=np.float32)
    with rasterio.open(path) as src:
        reproject(rasterio.band(src, 1), result,
                  src_transform=src.transform, src_crs=src.crs,
                  src_nodata=src.nodata, dst_transform=transform, dst_crs=crs,
                  dst_nodata=np.nan, resampling=Resampling.bilinear,
                  init_dest_nodata=True)
        scale, offset = src.scales[0], src.offsets[0]
    valid = np.isfinite(result)
    if not valid.any():
        raise ValueError(f"No CHELSA data overlaps the map ({path.name})")
    # CHELSA masks the ocean; extend nearest coastal land value over water.
    if not valid.all():
        nearest = distance_transform_edt(~valid, return_distances=False, return_indices=True)
        result = result[tuple(nearest)]
    result *= scale
    result += offset
    return result


def _reference_heights(build_dir: Path, shape, sea_level):
    """Mean terrain Y of each sample's 32x32 cell; ocean counts as sea level."""
    rows, cols = shape
    result = np.empty(shape, dtype=np.float32)
    names = ("heightmap", "landmask", "lake_mask")
    sources = [rasterio.open(build_dir / f"{name}.png") for name in names]
    try:
        width, height = sources[0].width, sources[0].height
        half = SPACING // 2
        for z in range(rows):
            top, bottom = max(0, z * SPACING - half), min(height, z * SPACING + half)
            window = Window(0, top, width, bottom - top)
            terrain, land, lake = (src.read(1, window=window) for src in sources)
            surface = np.where((land > 0) | (lake > 0), terrain, sea_level).astype(np.float32)
            # Column sums at cell edges; each sample covers [x*32-16, x*32+16).
            prefix = np.concatenate(([0.0], np.cumsum(surface.sum(axis=0, dtype=np.float64))))
            left = np.clip(np.arange(cols) * SPACING - half, 0, width)
            right = np.clip(np.arange(cols) * SPACING + half, 0, width)
            result[z] = (prefix[right] - prefix[left]) / ((right - left) * (bottom - top))
    finally:
        for src in sources:
            src.close()
    return result


def _lapse_per_block(annual, reference):
    """Local regression of CHELSA temperature on terrain Y (°C per source block)."""
    dt = annual - uniform_filter(annual, size=5, mode="nearest")
    dh = reference - uniform_filter(reference, size=5, mode="nearest")
    denominator = float((dh * dh).sum())
    if denominator < 1e-6:
        return 0.0  # flat map: no measurable altitude signal
    return float(np.clip((dh * dt).sum() / denominator, -0.5, 0.0))


def run(build_dir: Path, datasets_dir: Path, grid, cfg):
    width, height = cfg.FINAL_WIDTH, cfg.FINAL_LENGTH
    if width <= 0 or height <= 0 or width % 512 or height % 512:
        raise ValueError("Climate output dimensions must be positive multiples of 512")
    if not cfg.RESIZE_MAP and (width, height) != (grid.width, grid.height):
        raise ValueError("Climate dimensions must match the final PNG/region grid")
    shape = ((height - 1) // SPACING + 1, (width - 1) // SPACING + 1)
    # Source grid -> final image pixels -> sample centers at (x*SPACING,z*SPACING).
    final = grid.transform * Affine.scale(grid.width / width, grid.height / height)
    transform = final * Affine.translation((1 - SPACING) / 2, (1 - SPACING) / 2) * Affine.scale(SPACING)
    datasets_dir.mkdir(parents=True, exist_ok=True)
    samples = np.empty((*shape, 15), dtype="<i2")
    annual_t = np.zeros(shape, dtype=np.float32)
    annual_p = np.zeros(shape, dtype=np.float32)
    annual_pet = np.zeros(shape, dtype=np.float32)
    for month in range(1, 13):
        temp = _month(_raster(datasets_dir, "tas", month), shape, transform, grid.crs)
        rain = _month(_raster(datasets_dir, "pr", month), shape, transform, grid.crs)
        pet = _month(_raster(datasets_dir, "pet", month), shape, transform, grid.crs)
        samples[:, :, month - 1] = np.rint(np.clip((temp - 273.15) * 10, -32768, 32767)).astype("<i2")
        annual_t += (temp - 273.15) / 12
        annual_p += np.maximum(rain, 0)
        annual_pet += np.maximum(pet, 0)
        print(f"[earth climate] month {month:02d}/12")
    samples[:, :, 12] = np.rint(np.clip(annual_p, 0, 65535)).astype("<u2").view("<i2")
    samples[:, :, 13] = np.rint(np.clip(annual_pet, 0, 65535)).astype("<u2").view("<i2")
    reference = _reference_heights(build_dir, shape, cfg.TERRAIN_SEA_LEVEL_Y)
    samples[:, :, 14] = np.rint(np.clip(reference * 10, 0, 65535)).astype("<u2").view("<i2")
    lapse = _lapse_per_block(annual_t, reference)
    print(f"[earth climate] Lapse {lapse:.4f} °C per terrain block")
    destination = build_dir / "earthclimate.bin"
    temporary = destination.with_suffix(".bin.tmp")
    build_dir.mkdir(parents=True, exist_ok=True)
    try:
        with temporary.open("wb") as output:
            output.write(HEADER.pack(b"EMCL", VERSION, width, height, SPACING, lapse))
            output.write(samples.tobytes(order="C"))
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print("[earth climate] Wrote", destination)
