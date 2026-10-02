"""Windowed raster sampling and compact river-only morphology."""

import numpy as np
from rasterio.features import rasterize
from rasterio.windows import Window, transform as window_transform
from scipy.ndimage import maximum_filter, minimum_filter


_BAND_ROWS = 256


def _validate_datasets(datasets):
    first = datasets[0]
    rows, cols = int(first.height), int(first.width)
    for dataset in datasets[1:]:
        if (int(dataset.height), int(dataset.width)) != (rows, cols):
            raise ValueError("river sampling TIFF dimensions must match")
    return rows, cols


def _band_window(start, stop, cols):
    return Window(0, start, cols, stop - start)


def _rasterize_center(lines, transform, rows, cols, start, stop):
    """Rasterize a row band with an overlap so band edges cannot add endpoints."""
    if not lines:
        return np.zeros((stop - start, cols), dtype=bool)
    outer_start = max(0, start - 1)
    outer_stop = min(rows, stop + 1)
    outer = _band_window(outer_start, outer_stop, cols)
    rendered = rasterize(
        ((line, 1) for line in lines),
        out_shape=(outer_stop - outer_start, cols),
        transform=window_transform(outer, transform),
        all_touched=True,
        dtype=np.uint8,
    )
    return rendered[start - outer_start:stop - outer_start].astype(bool)


def collect_inputs(lines, transform, height, river, lake, land, fallback, shore):
    """Collect fitter inputs without constructing a world-sized array.

    All index arrays are global row-major flat indices. Raster inputs remain open
    and are read in bounded, full-width row bands so scipy's horizontal and world
    edge reflection exactly matches filtering the complete image.
    """
    rows, cols = _validate_datasets((height, river, lake, land, fallback, shore))
    lines = tuple(lines)

    water_parts = []
    pixel_parts = []
    sample_parts = []
    center_lake_parts = []
    center_ocean_parts = []
    water_lake_parts = []
    ceiling_parts = []
    fallback_parts = []

    for start in range(0, rows, _BAND_ROWS):
        stop = min(rows, start + _BAND_ROWS)
        core_window = _band_window(start, stop, cols)
        water = river.read(1, window=core_window) > 0
        local_water = np.flatnonzero(water)
        if local_water.size:
            water_parts.append(local_water.astype(np.int64) + np.int64(start * cols))

        center = _rasterize_center(lines, transform, rows, cols, start, stop) & water
        local_center = np.flatnonzero(center)
        if local_center.size:
            pixel_parts.append(local_center.astype(np.int64) + np.int64(start * cols))

        if not local_water.size:
            continue

        outer_start = max(0, start - 2)
        outer_stop = min(rows, stop + 2)
        outer_window = _band_window(outer_start, outer_stop, cols)
        heights = height.read(1, window=outer_window)
        lakes = lake.read(1, window=outer_window) > 0
        lands = land.read(1, window=outer_window) > 0
        core = slice(start - outer_start, stop - outer_start)

        lake_levels = minimum_filter(
            np.where(lakes, heights, 255).astype(np.uint8), size=3, mode="reflect"
        )[core]
        if local_water.size:
            water_lake_parts.append(lake_levels.ravel()[local_water])
            ceiling_parts.append(shore.read(1, window=core_window).ravel()[local_water])
            fallback_parts.append(fallback.read(1, window=core_window).ravel()[local_water])
        if local_center.size:
            samples = minimum_filter(heights, size=5, mode="reflect")[core]
            ocean_near = ~minimum_filter(lands, size=3, mode="reflect")[core]
            sample_parts.append(samples.ravel()[local_center].astype(np.float64))
            center_lake_parts.append(lake_levels.ravel()[local_center])
            center_ocean_parts.append(ocean_near.ravel()[local_center])

    def joined(parts, dtype):
        return np.concatenate(parts).astype(dtype, copy=False) if parts else np.empty(0, dtype=dtype)

    return {
        "rows": rows,
        "cols": cols,
        "water_pixels": joined(water_parts, np.int64),
        "pixels": joined(pixel_parts, np.int64),
        "samples": joined(sample_parts, np.float64),
        "center_lake_levels": joined(center_lake_parts, np.uint8),
        "center_ocean_near": joined(center_ocean_parts, bool),
        "water_lake_levels": joined(water_lake_parts, np.uint8),
        "ceiling": joined(ceiling_parts, np.uint8),
        "fallback": joined(fallback_parts, np.uint8),
    }


def _validate_compact(values, water_pixels, rows, cols):
    values = np.asarray(values)
    pixels = np.asarray(water_pixels, dtype=np.int64)
    if values.ndim != 1 or pixels.ndim != 1 or len(values) != len(pixels):
        raise ValueError("values and water_pixels must be equal-length vectors")
    if len(pixels):
        if pixels[0] < 0 or pixels[-1] >= rows * cols:
            raise ValueError("water_pixels contains an out-of-bounds index")
        if np.any(pixels[1:] <= pixels[:-1]):
            raise ValueError("water_pixels must be sorted and unique")
    return values.astype(np.uint8, copy=False), pixels


def morphology(values, water_pixels, rows, cols, operation):
    """Apply dense-compatible masked morphology to compact water values."""
    rows, cols = int(rows), int(cols)
    if rows < 0 or cols < 0:
        raise ValueError("rows and cols must be nonnegative")
    values, pixels = _validate_compact(values, water_pixels, rows, cols)
    if operation == "floor3":
        size, halo = 3, 1
    elif operation == "drop5":
        size, halo = 5, 2
    elif operation == "crest5":
        size, halo = 5, 4
    else:
        raise ValueError(f"unknown river morphology operation: {operation}")
    if not len(pixels):
        return values.copy()

    result = np.empty_like(values)
    for start in range(0, rows, _BAND_ROWS):
        stop = min(rows, start + _BAND_ROWS)
        core_lo = int(np.searchsorted(pixels, start * cols, side="left"))
        core_hi = int(np.searchsorted(pixels, stop * cols, side="left"))
        if core_lo == core_hi:
            continue

        outer_start = max(0, start - halo)
        outer_stop = min(rows, stop + halo)
        outer_lo = int(np.searchsorted(pixels, outer_start * cols, side="left"))
        outer_hi = int(np.searchsorted(pixels, outer_stop * cols, side="left"))
        outer_pixels = pixels[outer_lo:outer_hi]
        relative_rows = outer_pixels // cols - outer_start
        relative_cols = outer_pixels % cols
        water = np.zeros((outer_stop - outer_start, cols), dtype=bool)
        water[relative_rows, relative_cols] = True
        source = np.full(water.shape, 255, dtype=np.uint8)
        source[relative_rows, relative_cols] = values[outer_lo:outer_hi]
        floor = minimum_filter(source, size=size, mode="reflect")
        if operation == "crest5":
            filtered = maximum_filter(
                np.where(water, floor, 0).astype(np.uint8), size=5, mode="reflect"
            )
        else:
            filtered = floor

        core_pixels = pixels[core_lo:core_hi]
        rr = core_pixels // cols - outer_start
        cc = core_pixels % cols
        result[core_lo:core_hi] = np.minimum(values[core_lo:core_hi], filtered[rr, cc])
    return result


def write_values(dataset, water_pixels, values):
    """Patch compact water values into a raster while preserving every dry cell."""
    rows, cols = int(dataset.height), int(dataset.width)
    values, pixels = _validate_compact(values, water_pixels, rows, cols)
    for start in range(0, rows, _BAND_ROWS):
        stop = min(rows, start + _BAND_ROWS)
        lo = int(np.searchsorted(pixels, start * cols, side="left"))
        hi = int(np.searchsorted(pixels, stop * cols, side="left"))
        if lo == hi:
            continue
        window = _band_window(start, stop, cols)
        raster = dataset.read(1, window=window)
        band_pixels = pixels[lo:hi]
        raster[band_pixels // cols - start, band_pixels % cols] = values[lo:hi]
        dataset.write(raster, 1, window=window)
