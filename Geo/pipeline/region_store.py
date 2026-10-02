"""Bake tiled final rasters into fixed-size, region-addressable game values.

Format: <8sIIIII header (magic, version, width, height, region size, sea Y),
followed by X-major/Z-minor regions; each region holds nine 512x512 byte
planes in LAYERS order, row-major within each plane. A temporary file is
published only when every plane has been written successfully.
"""
from contextlib import ExitStack
from pathlib import Path
import os
import struct

import numpy as np
import rasterio
from rasterio.windows import Window

MAGIC = b"EMREGION"
VERSION = 4
REGION = 512
HEADER = struct.Struct("<8sIIIII")
LAYERS = ("heightmap", "lake_depth", "bathymetry_heightmap", "vegetation",
          "river", "river_surface", "river_depth", "lake_mask", "landmask")


def bake(build_dir: Path, width: int, height: int, sea_level: int, minimum_depth: int):
    if width % REGION or height % REGION or width <= 0 or height <= 0:
        raise ValueError("Map dimensions must be positive multiples of 512")
    if not 1 <= minimum_depth < sea_level < 255:
        raise ValueError("Ocean depth must be below sea level, with both in byte range")
    destination = build_dir / "earthmap.regions"
    temporary = build_dir / "earthmap.regions.tmp"
    zregions = height // REGION
    region_bytes = REGION * REGION * len(LAYERS)
    expected = HEADER.size + width // REGION * zregions * region_bytes
    try:
        with temporary.open("w+b") as output, ExitStack() as sources:
            images = {name: sources.enter_context(rasterio.open(build_dir / (name + ".tif")))
                      for name in LAYERS}
            for name, image in images.items():
                if (image.width, image.height) != (width, height):
                    raise ValueError(f"{name}.tif dimensions {(image.width, image.height)} != {(width, height)}")
            output.write(HEADER.pack(MAGIC, VERSION, width, height, REGION, sea_level))
            output.truncate(expected)
            for rz in range(zregions):
                for rx in range(width // REGION):
                    window = Window(rx * REGION, rz * REGION, REGION, REGION)
                    land = images["landmask"].read(1, window=window)
                    for layer_index, name in enumerate(LAYERS):
                        values = images[name].read(1, window=window)
                        if name == "heightmap":
                            values = np.where(land > 0, np.maximum(values, 1), 0).astype(np.uint8)
                        elif name == "bathymetry_heightmap":
                            values = np.where((land == 0) & (values > 0),
                                              np.clip(values, minimum_depth, sea_level), sea_level).astype(np.uint8)
                        offset = HEADER.size + (rx * zregions + rz) * region_bytes
                        output.seek(offset + layer_index * REGION * REGION)
                        output.write(values.tobytes())
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print("[regions] Wrote", destination)
