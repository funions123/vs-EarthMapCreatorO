"""EMCL v2 spacing, striped coastal fill, terrain, and lapse contracts."""
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

from pipeline.earth_climate import run
from util.projection import MasterGrid
from util.working import create_layer


class EarthClimateTests(unittest.TestCase):
    def test_spacing_eight_values_edges_and_z_major_samples(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            datasets = root / "datasets"
            datasets.mkdir()
            transform = from_origin(-1, 1, 1 / 64, 1 / 64)

            # Final-grid terrain: west half Y=60 (sea level), east half Y=160.
            # Ocean terrain must count as sea level, including at the clipped
            # top/left sample footprint.
            terrain = np.full((512, 512), 60, dtype=np.uint8)
            terrain[:, 256:] = 160
            land = np.full((512, 512), 255, dtype=np.uint8)
            land[:16, :16] = 0
            terrain[:16, :16] = 200
            for name, pixels in (("heightmap", terrain), ("landmask", land),
                                 ("lake_mask", np.zeros_like(land))):
                with create_layer(root / f"{name}.tif", 512, 512) as output:
                    output.write(pixels, 1)

            profile = dict(driver="GTiff", height=64, width=64, count=1,
                           dtype="uint16", crs="EPSG:4326", transform=transform,
                           nodata=65535)
            for variable, scale in (("tas", 0.1), ("pr", 0.1), ("pet", 0.01)):
                for month in range(1, 13):
                    if variable == "tas":
                        pixels = np.full((64, 64), 2732 + 20 * month,
                                         dtype=np.uint16)
                        pixels[:, 32:] -= 100  # high terrain is 10 °C colder
                        pixels[19:, :] += 60
                    else:
                        monthly = 100 if variable == "pr" else 250
                        east = 200 if variable == "pr" else 500
                        south = 300 if variable == "pr" else 750
                        pixels = np.full((64, 64), month * monthly,
                                         dtype=np.uint16)
                        pixels[:, 32:] += east
                        pixels[19:, :] += south
                    # The corner and a full-width band are CHELSA ocean/nodata.
                    # A deliberately tiny writer stripe makes the band cross a
                    # stripe boundary and proves nearest-coast fill has no seam.
                    pixels[:2, :2] = 65535
                    pixels[16:19, :] = 65535
                    path = datasets / f"CHELSA_{variable}_{month:02d}_1981-2010_V.2.1.tif"
                    with rasterio.open(path, "w", **profile) as tif:
                        tif.scales = (scale,)
                        tif.write(pixels, 1)

            grid = MasterGrid(from_origin(-1, 1, 1 / 512, 1 / 512),
                              512, 512, CRS.from_epsg(4326))
            config = SimpleNamespace(FINAL_WIDTH=512, FINAL_LENGTH=512,
                                     RESIZE_MAP=True, TERRAIN_SEA_LEVEL_Y=60)
            with patch("pipeline.earth_climate.STRIPE_ROWS", 17):
                run(root, datasets, grid, config)
            self.assertEqual(list(root.glob("earthclimate-*")), [])

            content = (root / "earthclimate.bin").read_bytes()
            magic, version, width, height, spacing, lapse = struct.unpack_from(
                "<4siiiif", content,
            )
            self.assertEqual((magic, version, width, height, spacing),
                             (b"EMCL", 2, 512, 512, 8))
            self.assertEqual(len(content), 24 + 64 * 64 * 30)
            # The east-west climate/terrain relation is -10 °C per 100 blocks;
            # the north-south climate step is independent of terrain.
            self.assertAlmostEqual(lapse, -0.1, places=2)

            def sample(x, z):
                return struct.unpack_from("<12hHHH", content,
                                          24 + (z * 64 + x) * 30)

            for x, z, extra_temp, extra_rain, extra_pet, terrain_y in (
                    # At the top/left edge the centered terrain footprint is
                    # clipped to 4x4, not diluted as though it were 8x8.
                    (0, 0, 0, 0, 0, 60),
                    (63, 0, -10, 20, 5, 160),
                    (0, 63, 6, 30, 7.5, 60),
                    (63, 63, -4, 50, 12.5, 160)):
                values = sample(x, z)
                self.assertEqual(values[:12], tuple(
                    20 * month + extra_temp * 10 + 1
                    for month in range(1, 13)
                ))
                self.assertEqual(values[12], 780 + extra_rain * 12)
                self.assertEqual(values[13], round(2.5 * 78 + extra_pet * 12))
                self.assertEqual(values[14], terrain_y * 10)

            # z=17 and z=18 are in source nodata on opposite sides of the
            # writer's row-17 stripe boundary.  Both must use the globally
            # nearest coast rather than acquiring stripe-edge artifacts.
            north_filled = sample(0, 17)
            south_filled = sample(0, 18)
            self.assertEqual(north_filled[:12], tuple(
                20 * month + 1 for month in range(1, 13)
            ))
            self.assertEqual(north_filled[12:14],
                             (780, round(2.5 * 78)))
            self.assertEqual(south_filled[:12], tuple(
                20 * month + 61 for month in range(1, 13)
            ))
            self.assertEqual(south_filled[12:14],
                             (780 + 30 * 12, round(2.5 * 78 + 7.5 * 12)))

            # Adjacent records verify x changes fastest (z-major rows), and
            # the sample centered on the terrain boundary averages both sides.
            self.assertEqual(sample(31, 0)[14], 600)
            self.assertEqual(sample(32, 0)[14], 1100)
            self.assertEqual(sample(33, 0)[14], 1600)
            self.assertEqual(sample(0, 1)[14], 600)

    def test_failure_removes_scratch_and_partial_output(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            datasets = root / "datasets"
            datasets.mkdir()
            terrain = np.full((512, 512), 60, dtype=np.uint8)
            for name in ("heightmap", "landmask", "lake_mask"):
                with create_layer(root / f"{name}.tif", 512, 512) as output:
                    output.write(terrain, 1)
            grid = MasterGrid(from_origin(-1, 1, 1 / 512, 1 / 512),
                              512, 512, CRS.from_epsg(4326))
            config = SimpleNamespace(FINAL_WIDTH=512, FINAL_LENGTH=512,
                                     RESIZE_MAP=True, TERRAIN_SEA_LEVEL_Y=60)
            with patch("pipeline.earth_climate._raster",
                       side_effect=RuntimeError("fixture failure")):
                with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                    run(root, datasets, grid, config)
            self.assertFalse((root / "earthclimate.bin").exists())
            self.assertFalse((root / "earthclimate.bin.tmp").exists())
            self.assertEqual(list(root.glob("earthclimate-*")), [])


if __name__ == "__main__":
    unittest.main()
