"""Tiled byte working layers and bounded integer windows."""
import numpy as np
import rasterio
from rasterio.windows import Window


TILE = 512


def windows(width, height, bounds=None, size=TILE):
    x0, y0, x1, y1 = bounds if bounds is not None else (0, 0, width, height)
    for y in range(y0, y1, size):
        for x in range(x0, x1, size):
            yield Window(x, y, min(size, x1 - x), min(size, y1 - y))


def expanded(window, width, height, halo):
    x, y = int(window.col_off), int(window.row_off)
    x0, y0 = max(0, x - halo), max(0, y - halo)
    x1 = min(width, x + int(window.width) + halo)
    y1 = min(height, y + int(window.height) + halo)
    outer = Window(x0, y0, x1 - x0, y1 - y0)
    core = (slice(y - y0, y - y0 + int(window.height)),
            slice(x - x0, x - x0 + int(window.width)))
    return outer, core


def create_layer(path, width, height, fill=0):
    result = rasterio.open(path, "w+", driver="GTiff", width=width, height=height,
                           count=1, dtype="uint8", tiled=True, blockxsize=TILE,
                           blockysize=TILE, compress="LZW", BIGTIFF="YES")
    if fill:
        for window in windows(width, height):
            result.write(np.full((int(window.height), int(window.width)), fill,
                                 dtype=np.uint8), 1, window=window)
    return result
