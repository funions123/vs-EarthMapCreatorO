"""Shallow-ocean replacement for defective final-grid coastline pixels."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from util.working import create_layer

from pipeline.coast import repair_coast, write_coastal_maps


class Config:
    TERRAIN_SEA_LEVEL_Y = 92
    BATHY_SCALE_MAXDEPTH = 50


class CoastTests(unittest.TestCase):
    def test_invalid_coast_pixels_become_shallow_ocean_without_grading_neighbors(self):
        height = np.full((80, 80), 95, dtype=np.uint8)
        land = np.full_like(height, 255)
        land[:, :40] = 0
        lake = np.zeros_like(height)
        bathy = np.zeros_like(height)
        bathy[:, :40] = 50
        bathy[:, 36] = 10  # Interpolation with no-data creates an invalid depth.
        bathy[:, 37:40] = 0  # Missing source data beside the land polygon.
        height[:, 40:43] = 89
        height[40, 40] = 0  # Invalid land elevation used to cut to the mantle.
        height[:, 70] = 89  # Inland lowlands must not be raised.
        lake[40, 41] = 255
        height[40, 41] = 85

        repair_coast(height, land, lake, bathy, 92, 50)

        self.assertEqual(bathy[40, 36:40].tolist(), [92, 92, 92, 92])
        self.assertEqual(int(bathy[40, 35]), 50)
        self.assertEqual(height[40, 40:43].tolist(), [92, 85, 92])
        self.assertEqual(land[40, 40:43].tolist(), [0, 255, 0])
        self.assertEqual(bathy[40, 40:43].tolist(), [92, 0, 92])
        self.assertEqual(int(height[40, 70]), 89)
        self.assertEqual(int(land[40, 70]), 255)

    def test_correction_is_continuous_across_output_row_bands(self):
        shape = (1040, 64)
        height = np.full(shape, 89, dtype=np.uint8)
        land = np.full(shape, 255, dtype=np.uint8)
        land[:512, :] = 0
        lake = np.zeros(shape, dtype=np.uint8)
        bathy = np.zeros(shape, dtype=np.uint8)
        bathy[:512, :] = 50
        bathy[509:512, :] = 0
        lake[512, 33] = 255
        with tempfile.TemporaryDirectory() as temporary:
            build = Path(temporary)
            for name, pixels in (("heightmap", height), ("landmask", land),
                                 ("lake_mask", lake), ("bathymetry_heightmap", bathy)):
                with create_layer(build / f"{name}.tif", pixels.shape[1], pixels.shape[0]) as output:
                    output.write(pixels, 1)
            write_coastal_maps(build, Config)
            with rasterio.open(build / "heightmap.tif") as image:
                actual_height = image.read(1)
            with rasterio.open(build / "landmask.tif") as image:
                actual_land = image.read(1)
            with rasterio.open(build / "bathymetry_heightmap.tif") as image:
                actual_bathy = image.read(1)
        for z in (512, 513, 514):
            self.assertEqual(int(actual_height[z, 32]), 92)
        self.assertEqual(actual_bathy[509:512, 32].tolist(), [92, 92, 92])
        self.assertEqual(int(actual_bathy[0, 32]), 50)
        self.assertEqual(int(actual_land[512, 32]), 0)
        self.assertEqual(int(actual_bathy[512, 32]), 92)
        self.assertEqual(int(actual_height[512, 33]), 89)
        self.assertEqual(int(actual_land[512, 33]), 255)
        self.assertEqual(int(actual_height[529, 32]), 89)
        self.assertEqual(int(actual_land[529, 32]), 255)
