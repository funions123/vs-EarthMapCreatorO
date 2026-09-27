"""Bake final PNGs into fixed-size, region-addressable game values.

Format: <8sIIII header (magic, version, width, height, region size), followed
by X-major/Z-minor regions; each region holds ten 512x512 byte planes in
LAYERS order, row-major within each plane. A temporary file is published only
when every plane has been written successfully.
"""
from contextlib import ExitStack
from pathlib import Path
import os
import struct

import numpy as np
import rasterio
from rasterio.windows import Window

MAGIC = b"EMREGION"
VERSION = 1
REGION = 512
HEADER = struct.Struct("<8sIIII")
LAYERS = ("heightmap", "lake_depth", "bathymetry_heightmap", "climate",
          "tree", "river", "river_surface", "river_depth", "lake_mask", "landmask")
COLORS = {
    (0, 0, 255): 8, (0, 120, 255): 8, (70, 170, 250): 7,
    (255, 0, 0): 2, (255, 150, 150): 10, (245, 165, 0): 11,
    (255, 220, 100): 3, (255, 255, 0): 9, (200, 200, 0): 9,
    (150, 150, 0): 9, (150, 255, 150): 5, (100, 200, 100): 5,
    (50, 150, 50): 5, (200, 255, 80): 5, (100, 255, 80): 5,
    (50, 200, 0): 5, (0, 255, 255): 6, (55, 200, 255): 6,
    (255, 0, 255): 6, (200, 0, 200): 6, (170, 175, 255): 6,
    (90, 120, 220): 6, (150, 50, 150): 4, (150, 100, 150): 4,
    (75, 80, 180): 4, (50, 0, 135): 4, (0, 125, 125): 4,
    (0, 70, 95): 4, (178, 178, 178): 1, (102, 102, 102): 0,
}


def bake(build_dir: Path, width: int, height: int):
    if width % REGION or height % REGION or width <= 0 or height <= 0:
        raise ValueError("Map dimensions must be positive multiples of 512")
    destination = build_dir / "earthmap.regions"
    temporary = build_dir / "earthmap.regions.tmp"
    zregions = height // REGION
    region_bytes = REGION * REGION * len(LAYERS)
    expected = HEADER.size + width // REGION * zregions * region_bytes
    try:
        with temporary.open("w+b") as output, ExitStack() as sources:
            images = {name: sources.enter_context(rasterio.open(build_dir / (name + ".png")))
                      for name in LAYERS}
            for name, image in images.items():
                if (image.width, image.height) != (width, height):
                    raise ValueError(f"{name}.png dimensions {(image.width, image.height)} != {(width, height)}")
            output.write(HEADER.pack(MAGIC, VERSION, width, height, REGION))
            output.truncate(expected)
            for rz in range(zregions):
                for rx in range(width // REGION):
                    window = Window(rx * REGION, rz * REGION, REGION, REGION)
                    land = images["landmask"].read(1, window=window)
                    for layer_index, name in enumerate(LAYERS):
                        image = images[name]
                        if name == "climate":
                            pixels = image.read((1, 2, 3), window=window).transpose(1, 2, 0)
                            values = np.full((REGION, REGION), 9, dtype=np.uint8)
                            for color, zone in COLORS.items():
                                values[np.all(pixels == color, axis=2)] = zone
                        else:
                            values = image.read(1, window=window)
                        if name == "heightmap":
                            values = np.where(land > 0, np.maximum(values, 1), 0).astype(np.uint8)
                        elif name == "bathymetry_heightmap":
                            values = np.where((land == 0) & (values > 0),
                                              np.clip(values, 50, 92), 92).astype(np.uint8)
                        offset = HEADER.size + (rx * zregions + rz) * region_bytes
                        output.seek(offset + layer_index * REGION * REGION)
                        output.write(values.tobytes())
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print("[regions] Wrote", destination)
