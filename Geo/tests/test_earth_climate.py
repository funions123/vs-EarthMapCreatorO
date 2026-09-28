"""EMCL v2 writer contract, CHELSA scale/nodata handling, and terrain lapse."""
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

from pipeline.earth_climate import run
from util.projection import MasterGrid


class EarthClimateTests(unittest.TestCase):
    def test_monthly_values_and_z_major_samples(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            datasets = root / "datasets"
            datasets.mkdir()
            transform = from_origin(-1, 1, 1 / 64, 1 / 64)
            # Final-grid terrain: west half Y=60 (sea level), east half Y=160;
            # ocean in the top-left corner cell must count as sea level.
            terrain = np.full((512, 512), 60, dtype=np.uint8)
            terrain[:, 256:] = 160
            land = np.full((512, 512), 255, dtype=np.uint8)
            land[:16, :16] = 0
            terrain[:16, :16] = 200
            for name, pixels in (("heightmap", terrain), ("landmask", land),
                                 ("lake_mask", np.zeros_like(land))):
                Image.fromarray(pixels).save(root / f"{name}.png")
            profile = dict(driver="GTiff", height=64, width=64, count=1,
                           dtype="uint16", crs="EPSG:4326", transform=transform,
                           nodata=65535)
            for variable, scale in (("tas", 0.1), ("pr", 0.1), ("pet", 0.01)):
                for month in range(1, 13):
                    if variable == "tas":
                        pixels = np.full((64, 64), 2732 + 20 * month, dtype=np.uint16)
                        pixels[:, 32:] -= 100  # east (high) half is 10 °C colder
                        pixels[32:, :] += 60
                    else:
                        pixels = np.full((64, 64), month * (100 if variable == "pr" else 250),
                                         dtype=np.uint16)
                        pixels[:, 32:] += 200 if variable == "pr" else 500
                        pixels[32:, :] += 300 if variable == "pr" else 750
                    pixels[:2, :2] = 65535  # ocean must use the nearest coastal value
                    path = datasets / f"CHELSA_{variable}_{month:02d}_1981-2010_V.2.1.tif"
                    with rasterio.open(path, "w", **profile) as tif:
                        tif.scales = (scale,)
                        tif.write(pixels, 1)
            grid = MasterGrid(from_origin(-1, 1, 1 / 512, 1 / 512), 512, 512, CRS.from_epsg(4326))
            run(root, datasets, grid, SimpleNamespace(FINAL_WIDTH=512, FINAL_LENGTH=512,
                                                      RESIZE_MAP=True, TERRAIN_SEA_LEVEL_Y=60))
            content = (root / "earthclimate.bin").read_bytes()
            magic, version, width, height, spacing, lapse = struct.unpack_from("<4siiiif", content)
            self.assertEqual((magic, version, width, height, spacing), (b"EMCL", 2, 512, 512, 32))
            # 10 °C colder over 100 blocks higher terrain.
            self.assertAlmostEqual(lapse, -0.1, places=2)
            self.assertEqual(len(content), 24 + 16 * 16 * 30)
            for x, z, extra_temp, extra_rain, extra_pet, terrain_y in ((0, 0, 0, 0, 0, 60),
                                                                        (15, 0, -10, 20, 5, 160),
                                                                        (0, 15, 6, 30, 7.5, 60),
                                                                        (15, 15, -4, 50, 12.5, 160)):
                values = struct.unpack_from("<12hHHH", content, 24 + (z * 16 + x) * 30)
                self.assertEqual(values[:12], tuple(20 * month + extra_temp * 10 + 1
                                                     for month in range(1, 13)))
                self.assertEqual(values[12], 780 + extra_rain * 12)
                self.assertEqual(values[13], round(2.5 * 78 + extra_pet * 12))
                self.assertEqual(values[14], terrain_y * 10)


if __name__ == "__main__":
    unittest.main()
