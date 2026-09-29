"""Write bounded-memory EMCL v2 data from CHELSA V2.1 climatologies.

CHELSA tas is scaled kelvin; pr and pet are scaled kg/m²/month (mm/month).
Records remain z-major and contain 12 monthly temperatures, annual P/PET, and
the mean final-map terrain Y around each sample.  Reprojection and output are
striped; the only map-sized working arrays are disk-backed scratch maps.
"""
from pathlib import Path
import os
import struct
import tempfile

import numpy as np
import rasterio
from affine import Affine
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window
from scipy.ndimage import distance_transform_edt, uniform_filter

from util.raster import download_file

SPACING = 8  # final-map blocks per sample
VERSION = 2
HEADER = struct.Struct("<4siiiif")  # magic, version, width, height, spacing, lapse
SAMPLE = np.dtype([
    ("temperature", "<i2", (12,)),
    ("precipitation", "<u2"),
    ("pet", "<u2"),
    ("terrain_y", "<u2"),
])
STRIPE_ROWS = 128
# The previous 5 samples at spacing 32 covered approximately 160 source
# blocks.  Use an odd 21-sample window at spacing 8 so its filter remains
# centered and retains that geographic support instead of shrinking to 40.
LAPSE_WINDOW_BLOCKS = 160
LAPSE_WINDOW_SAMPLES = 2 * round(LAPSE_WINDOW_BLOCKS / SPACING / 2) + 1
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


def _stripes(rows):
    for start in range(0, rows, STRIPE_ROWS):
        yield start, min(start + STRIPE_ROWS, rows)


def _warp_month(path, shape, transform, crs, values, invalid, indices):
    """Reproject one month by stripe and prepare an exact global coastal fill."""
    valid_count = 0
    with rasterio.open(path) as src:
        scale, offset = src.scales[0], src.offsets[0]
        for start, stop in _stripes(shape[0]):
            destination = values[start:stop]
            destination.fill(np.nan)
            reproject(
                rasterio.band(src, 1), destination,
                src_transform=src.transform, src_crs=src.crs,
                src_nodata=src.nodata,
                dst_transform=transform * Affine.translation(0, start),
                dst_crs=crs, dst_nodata=np.nan,
                resampling=Resampling.bilinear, init_dest_nodata=True,
            )
            stripe_valid = np.isfinite(destination)
            np.logical_not(stripe_valid, out=invalid[start:stop])
            valid_count += int(np.count_nonzero(stripe_valid))

    if not valid_count:
        raise ValueError(f"No CHELSA data overlaps the map ({Path(path).name})")
    invalid_count = values.size - valid_count
    if invalid_count:
        # Supplying a disk-backed output prevents scipy from allocating the two
        # full-size coordinate planes in RAM.  The transform is global, so a
        # nearest coast on the other side of a stripe is still selected.
        distance_transform_edt(
            invalid, return_distances=False, return_indices=True, indices=indices,
        )
    return scale, offset, invalid_count


def _month_stripes(values, indices, invalid_count, scale, offset):
    """Yield bounded, scaled stripes with CHELSA ocean cells coastal-filled."""
    for start, stop in _stripes(values.shape[0]):
        if invalid_count:
            rows = indices[0, start:stop]
            columns = indices[1, start:stop]
            stripe = values[rows, columns]
        else:
            stripe = np.array(values[start:stop], dtype=np.float32, copy=True)
        stripe *= scale
        stripe += offset
        yield start, stop, stripe


def _clear(mapping):
    for start, stop in _stripes(mapping.shape[0]):
        mapping[start:stop].fill(0)


def _write_reference_heights(build_dir, final_shape, sample_shape, sea_level,
                             destination, records):
    """Write mean terrain Y near every sample without loading a final PNG."""
    names = ("heightmap", "landmask", "lake_mask")
    sources = [rasterio.open(build_dir / f"{name}.png") for name in names]
    try:
        height, width = final_shape
        if any((src.height, src.width) != final_shape for src in sources):
            raise ValueError("Climate source PNG dimensions must match the final map")

        columns = np.arange(sample_shape[1]) * SPACING
        half = SPACING // 2
        left = np.clip(columns - half, 0, width)
        right = np.clip(columns + half, 0, width)
        cell_widths = right - left
        for z in range(sample_shape[0]):
            top = max(0, z * SPACING - half)
            bottom = min(height, z * SPACING + half)
            window = Window(0, top, width, bottom - top)
            terrain, land, lake = (src.read(1, window=window) for src in sources)
            surface = np.where((land > 0) | (lake > 0), terrain, sea_level)
            prefix = np.empty(width + 1, dtype=np.float64)
            prefix[0] = 0
            np.cumsum(surface.sum(axis=0, dtype=np.float64), out=prefix[1:])
            row = (prefix[right] - prefix[left]) / (cell_widths * (bottom - top))
            destination[z] = row
            records["terrain_y"][z] = np.rint(
                np.clip(row * 10, 0, 65535),
            ).astype("<u2")
    finally:
        for src in sources:
            src.close()


def _lapse_per_block(annual, reference):
    """Local regression of CHELSA temperature on terrain Y (°C per block)."""
    numerator = 0.0
    denominator = 0.0
    rows = annual.shape[0]
    halo = LAPSE_WINDOW_SAMPLES
    for start, stop in _stripes(rows):
        outer_start = max(0, start - halo)
        outer_stop = min(rows, stop + halo)
        temperatures = np.array(annual[outer_start:outer_stop], copy=True)
        heights = np.array(reference[outer_start:outer_stop], copy=True)
        mean_temperatures = uniform_filter(
            temperatures, size=LAPSE_WINDOW_SAMPLES, mode="nearest",
        )
        mean_heights = uniform_filter(
            heights, size=LAPSE_WINDOW_SAMPLES, mode="nearest",
        )
        core = slice(start - outer_start, stop - outer_start)
        temperatures = temperatures[core] - mean_temperatures[core]
        heights = heights[core] - mean_heights[core]
        numerator += float(np.einsum("ij,ij->", heights, temperatures,
                                     dtype=np.float64))
        denominator += float(np.einsum("ij,ij->", heights, heights,
                                       dtype=np.float64))
    if denominator < 1e-6:
        return 0.0  # flat map: no measurable altitude signal
    return float(np.clip(numerator / denominator, -0.5, 0.0))


def _close_memmap(mapping):
    if mapping is not None:
        mapping.flush()
        mapping._mmap.close()


def run(build_dir: Path, datasets_dir: Path, grid, cfg):
    build_dir = Path(build_dir)
    datasets_dir = Path(datasets_dir)
    width, height = cfg.FINAL_WIDTH, cfg.FINAL_LENGTH
    if width <= 0 or height <= 0 or width % 512 or height % 512:
        raise ValueError("Climate output dimensions must be positive multiples of 512")
    if not cfg.RESIZE_MAP and (width, height) != (grid.width, grid.height):
        raise ValueError("Climate dimensions must match the final PNG/region grid")

    shape = ((height - 1) // SPACING + 1, (width - 1) // SPACING + 1)
    final = grid.transform * Affine.scale(grid.width / width, grid.height / height)
    # Source grid -> final image pixels -> sample centers at x*SPACING,z*SPACING.
    transform = (final * Affine.translation((1 - SPACING) / 2,
                                            (1 - SPACING) / 2)
                 * Affine.scale(SPACING))
    build_dir.mkdir(parents=True, exist_ok=True)
    datasets_dir.mkdir(parents=True, exist_ok=True)
    destination = build_dir / "earthclimate.bin"
    temporary = destination.with_suffix(".bin.tmp")
    records = values = invalid = indices = annual = None
    lapse = 0.0
    try:
        with temporary.open("w+b") as output:
            output.write(HEADER.pack(b"EMCL", VERSION, width, height, SPACING, 0.0))
            output.truncate(HEADER.size + shape[0] * shape[1] * SAMPLE.itemsize)

        with tempfile.TemporaryDirectory(prefix="earthclimate-", dir=build_dir) as scratch:
            scratch = Path(scratch)
            try:
                # Allocate each disk-backed plane inside the cleanup scope so
                # even a failure partway through scratch creation releases the
                # already-open Windows mappings before TemporaryDirectory runs.
                records = np.memmap(temporary, mode="r+", dtype=SAMPLE,
                                    offset=HEADER.size, shape=shape)
                values = np.memmap(scratch / "values.f32", mode="w+",
                                   dtype=np.float32, shape=shape)
                invalid = np.memmap(scratch / "invalid.u8", mode="w+",
                                    dtype=np.uint8, shape=shape)
                indices = np.memmap(scratch / "nearest.i32", mode="w+",
                                    dtype=np.int32, shape=(2, *shape))
                annual = np.memmap(scratch / "annual.f32", mode="w+",
                                   dtype=np.float32, shape=shape)
                _clear(annual)
                for month in range(1, 13):
                    scale, offset, missing = _warp_month(
                        _raster(datasets_dir, "tas", month), shape, transform,
                        grid.crs, values, invalid, indices,
                    )
                    for start, stop, stripe in _month_stripes(
                            values, indices, missing, scale, offset):
                        stripe -= 273.15
                        records["temperature"][start:stop, :, month - 1] = np.rint(
                            np.clip(stripe * 10, -32768, 32767),
                        ).astype("<i2")
                        annual[start:stop] += stripe / 12
                    print(f"[earth climate] temperature month {month:02d}/12")

                _write_reference_heights(
                    build_dir, (height, width), shape, cfg.TERRAIN_SEA_LEVEL_Y,
                    values, records,
                )
                lapse = _lapse_per_block(annual, values)
                print(f"[earth climate] Lapse {lapse:.4f} °C per terrain block")

                for variable, field in (("pr", "precipitation"), ("pet", "pet")):
                    _clear(annual)
                    for month in range(1, 13):
                        scale, offset, missing = _warp_month(
                            _raster(datasets_dir, variable, month), shape, transform,
                            grid.crs, values, invalid, indices,
                        )
                        for start, stop, stripe in _month_stripes(
                                values, indices, missing, scale, offset):
                            np.maximum(stripe, 0, out=stripe)
                            annual[start:stop] += stripe
                        print(f"[earth climate] {variable} month {month:02d}/12")
                    for start, stop in _stripes(shape[0]):
                        records[field][start:stop] = np.rint(
                            np.clip(annual[start:stop], 0, 65535),
                        ).astype("<u2")
                records.flush()
            finally:
                _close_memmap(records)
                records = None
                _close_memmap(values)
                values = None
                _close_memmap(invalid)
                invalid = None
                _close_memmap(indices)
                indices = None
                _close_memmap(annual)
                annual = None

        with temporary.open("r+b") as output:
            output.seek(0)
            output.write(HEADER.pack(b"EMCL", VERSION, width, height, SPACING, lapse))
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print("[earth climate] Wrote", destination)
