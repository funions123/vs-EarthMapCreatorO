"""OSM geometry and final-grid river invariants."""
import unittest

import numpy as np
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import LineString

from pipeline.rivers import build_river_maps, prepare_river_geometries
from util.projection import MasterGrid


class Config:
    RIVER_DEFAULT_WIDTH_METRES = 45.0
    RIVER_MAX_LINE_WIDTH_METRES = 250.0
    RIVER_SURFACE_WINDOW_BLOCKS = 5
    RIVER_MIN_DEPTH_BLOCKS = 1
    RIVER_MAX_DEPTH_BLOCKS = 3
    RIVER_BANK_WIDTH_BLOCKS = 3
    RIVER_BANK_SLOPE = 1


class RiverMapsTests(unittest.TestCase):
    def test_line_width_is_buffered_in_projected_metres(self):
        elements = [{
            "type": "way", "id": 1,
            "tags": {"waterway": "river", "width": "60"},
            "geometry": [
                {"lon": 35.0, "lat": 31.0},
                {"lon": 35.0, "lat": 31.01},
            ],
        }]
        geometry = prepare_river_geometries(elements, CRS.from_epsg(32636), Config)[0]
        rectangle = geometry.minimum_rotated_rectangle
        coordinates = list(rectangle.exterior.coords)
        sides = [LineString(coordinates[i:i + 2]).length for i in range(4)]
        self.assertAlmostEqual(min(sides), 60, delta=1)

    def test_surface_never_rises_above_local_valley_and_lakes_win(self):
        grid = MasterGrid(Affine(30, 0, 0, 0, -30, 600), 20, 20, CRS.from_epsg(32636))
        height = np.full((20, 20), 90, dtype=np.uint8)
        height[:, 9:12] = 60
        land = np.full((20, 20), 255, dtype=np.uint8)
        lake = np.zeros((20, 20), dtype=np.uint8)
        lake[9:12, 9:12] = 255
        geometry = LineString([(300, 0), (300, 600)]).buffer(30)

        mask, surface, depth = build_river_maps(
            [geometry], grid, height, land, lake, Config)
        river = mask > 0

        self.assertEqual(set(np.unique(mask)), {0, 255})
        self.assertTrue(np.all(mask[lake > 0] == 0))
        self.assertTrue(np.all(surface[river] <= 60))
        self.assertTrue(np.all(surface[~river] == 0))
        self.assertTrue(np.all((depth[river] >= 1) & (depth[river] <= 3)))
        self.assertTrue(np.all(depth[~river] == 0))


if __name__ == "__main__":
    unittest.main()
