"""Lake footprint, waterline, and bank invariants on a small projected grid."""
import tempfile
import unittest
from pathlib import Path

import fiona
import numpy as np
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import box, mapping
from shapely.ops import transform as project

from pipeline.lakes import build_lake_maps, SEA_LEVEL
from pipeline.topography import _encode_terrain_y
from util.projection import MasterGrid


class LakeMapsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gpkg = Path(self.temp.name) / "lakes.gpkg"
        self.grid = MasterGrid(Affine(1000, 0, 0, 0, -1000, 20000),
                               20, 20, CRS.from_epsg(3857))

    def maps(self, polygons, height, land=None):
        with fiona.open(self.gpkg, "w", driver="GPKG", crs="EPSG:4326",
                        schema={"geometry": "Polygon", "properties": {}}) as dst:
            for polygon in polygons:
                wgs84 = project(lambda x, y, z=None: (x * 1000 / 111319.49,
                                                      y * 1000 / 111319.49), polygon)
                dst.write({"geometry": mapping(wgs84), "properties": {}})
        if land is None:
            land = np.zeros_like(height)
        mask, depth = build_lake_maps(self.gpkg, self.grid, height, land)
        return mask, depth, land

    def test_lake_has_one_surface_and_depth_tapers_from_exact_shore(self):
        height = np.full((20, 20), 190, dtype=np.uint8)
        height[6:14, 6:14] = 80
        height[8, 8] = 140
        mask, depth, land = self.maps([box(6, 6, 14, 14)], height)

        self.assertEqual(set(np.unique(mask)), {0, 255})
        self.assertTrue(np.all(land[mask > 0] == 255))
        self.assertEqual(set(np.unique(height[mask > 0])), {80})
        self.assertTrue(np.all(depth[mask == 0] == 0))
        self.assertEqual(int(depth[6, 10]), 1)
        self.assertGreater(int(depth[9, 9]), int(depth[6, 10]))
        self.assertTrue(np.all(depth[mask > 0] >= 1))
        self.assertLessEqual(int(depth.max()), 12)
        self.assertEqual(int(height[5, 10]), 80)
        self.assertEqual(int(height[0, 0]), 190)

    def test_low_adjacent_ground_is_joined_to_waterline_without_cliff(self):
        height = np.full((20, 20), 20, dtype=np.uint8)
        height[6:14, 6:14] = 80
        mask, _, _ = self.maps([box(6, 6, 14, 14)], height)

        self.assertEqual(int(height[5, 10]), int(height[6, 10]))
        self.assertGreater(int(height[4, 10]), int(height[0, 10]))
        self.assertEqual(int(mask[5, 10]), 0)
        self.assertEqual(int(height[0, 10]), 20)
        self.assertLess(int(height[6, 10]), SEA_LEVEL)

    def test_signed_elevations_encode_below_sea_level(self):
        class Config:
            TERRAIN_SEA_LEVEL_Y = 92
            TERRAIN_METRES_PER_BLOCK = 10.0
            TERRAIN_MAX_Y = 250

        encoded = _encode_terrain_y(
            np.array([[-430, 0, 1580, -32768]], dtype=np.int16), Config)
        np.testing.assert_array_equal(encoded, [[49, 92, 250, 0]])

    def test_touching_polygons_share_one_waterline(self):
        height = np.full((20, 20), 80, dtype=np.uint8)
        height[5:15, 10:16] = 130
        mask, depth, _ = self.maps([box(4, 5, 10, 15), box(10, 5, 16, 15)], height)

        self.assertEqual(int(mask[10, 9]), 255)
        self.assertEqual(int(mask[10, 10]), 255)
        self.assertEqual(int(height[10, 9]), int(height[10, 10]))
        self.assertTrue(np.all(depth[mask > 0] > 0))


if __name__ == "__main__":
    unittest.main()
