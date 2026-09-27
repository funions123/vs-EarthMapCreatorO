"""Lake footprint, waterline, and bank invariants on a small projected grid."""
import tempfile
import unittest
from pathlib import Path

import fiona
import numpy as np
import rasterio
from affine import Affine
from PIL import Image
from rasterio.crs import CRS
from rasterio.transform import from_origin
from shapely.geometry import box, mapping
from shapely.ops import transform as project

from pipeline.land import _clip_lakes_to_gpkg
from pipeline.lakes import FRESH_LAKE, SALINE_LAKE, build_lake_maps, SEA_LEVEL
from pipeline.topography import _encode_terrain_y
from pipeline.translate import _write_heightmap
from util.projection import MasterGrid


class LakeMapsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gpkg = Path(self.temp.name) / "lakes.gpkg"
        self.grid = MasterGrid(Affine(1000, 0, 0, 0, -1000, 20000),
                               20, 20, CRS.from_epsg(3857))

    def maps(self, polygons, height, land=None, classes=None, names=None,
             saline_lake_names=()):
        with fiona.open(self.gpkg, "w", driver="GPKG", crs="EPSG:4326",
                        schema={"geometry": "Polygon",
                                "properties": {"featurecla": "str:32", "name": "str:80"}}) as dst:
            for index, polygon in enumerate(polygons):
                wgs84 = project(lambda x, y, z=None: (x * 1000 / 111319.49,
                                                      y * 1000 / 111319.49), polygon)
                dst.write({"geometry": mapping(wgs84),
                           "properties": {"featurecla": classes[index] if classes else "Lake",
                                          "name": names[index] if names else f"Lake {index}"}})
        if land is None:
            land = np.zeros_like(height)
        mask, depth = build_lake_maps(self.gpkg, self.grid, height, land,
                                      saline_lake_names)
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

    def test_signed_elevations_scale_peak_without_clipping_midrange(self):
        class Config:
            TERRAIN_SEA_LEVEL_Y = 92

        encoded = _encode_terrain_y(
            np.array([[-430, 0, 1580, 3000, 4500, -32768]], dtype=np.int16), Config)
        np.testing.assert_array_equal(encoded, [[76, 92, 149, 201, 255, 0]])

    def test_sea_datum_is_configurable_and_peak_follows_it(self):
        class Config:
            TERRAIN_SEA_LEVEL_Y = 70

        encoded = _encode_terrain_y(np.array([[0, 1000, 2000]], dtype=np.int16), Config)
        np.testing.assert_array_equal(encoded, [[70, 162, 255]])

    def test_final_grid_peak_is_preserved_across_output_bands(self):
        class Config:
            TERRAIN_SEA_LEVEL_Y = 92

        source = Path(self.temp.name) / "signed-dem.tif"
        output = Path(self.temp.name) / "heightmap.png"
        metres = np.zeros((520, 4), dtype=np.int16)
        metres[0, 0] = 2000
        metres[519, 3] = 4500
        metres[519, 0] = -430
        with rasterio.open(source, "w", driver="GTiff", width=4, height=520,
                           count=1, dtype="int16", nodata=-32768,
                           transform=from_origin(0, 520, 1, 1)) as dst:
            dst.write(metres, 1)

        _write_heightmap(source, output, None, None, Config)
        with Image.open(output) as image:
            heights = np.asarray(image)
        self.assertEqual(int(heights[519, 3]), 255)
        self.assertEqual(int(heights[0, 0]), 164)
        self.assertEqual(int(heights[519, 0]), 76)
        self.assertEqual(int(heights[250, 0]), 92)

    def test_named_salt_lake_override_keeps_other_lakes_fresh(self):
        height = np.full((20, 20), 100, dtype=np.uint8)
        mask, depth, _ = self.maps(
            [box(1, 1, 4, 4), box(8, 8, 11, 11), box(15, 15, 18, 18)],
            height,
            classes=["Lake", "Lake", "Alkaline Lake"],
            names=["Brackish Basin", "Clear Lake", "Great Salt Lake"],
            saline_lake_names=["Brackish Basin"])

        self.assertEqual(int(mask[17, 2]), SALINE_LAKE)
        self.assertEqual(int(mask[10, 9]), FRESH_LAKE)
        self.assertEqual(int(mask[3, 16]), SALINE_LAKE)
        self.assertTrue(np.all(depth[mask > 0] > 0))

    def test_reservoir_polygons_are_excluded_before_lake_rasterization(self):
        source = Path(self.temp.name) / "lakes.shp"
        polygons = [
            ("Lake", box(2, 2, 5, 5)),
            ("Reservoir", box(13, 13, 16, 16)),
            ("Alkaline Lake", box(24, 24, 27, 27)),
        ]
        with fiona.open(source, "w", driver="ESRI Shapefile", crs="EPSG:4326",
                        schema={"geometry": "Polygon", "properties": {"featurecla": "str:32"}}) as dst:
            for category, polygon in polygons:
                wgs84 = project(lambda x, y, z=None: (x * 1000 / 111319.49,
                                                      y * 1000 / 111319.49), polygon)
                dst.write({"geometry": mapping(wgs84), "properties": {"featurecla": category}})

        _clip_lakes_to_gpkg(str(source), str(self.gpkg), 0, 0, 0.3, 0.3,
                            box(0, 0, 0.3, 0.3))
        with fiona.open(self.gpkg) as cropped:
            self.assertEqual({feat["properties"]["featurecla"] for feat in cropped},
                             {"Lake", "Alkaline Lake"})
        grid = MasterGrid(Affine(1000, 0, 0, 0, -1000, 30000),
                          30, 30, CRS.from_epsg(3857))
        height = np.full((30, 30), 100, dtype=np.uint8)
        land = np.full_like(height, 255)
        mask, depth = build_lake_maps(self.gpkg, grid, height, land)
        self.assertEqual(int(mask[26, 3]), FRESH_LAKE)
        self.assertEqual(int(mask[4, 25]), SALINE_LAKE)
        self.assertEqual(int(mask[15, 14]), 0)
        self.assertEqual(int(depth[15, 14]), 0)

    def test_touching_polygons_share_one_waterline(self):
        height = np.full((20, 20), 80, dtype=np.uint8)
        height[5:15, 10:16] = 130
        mask, depth, _ = self.maps([box(4, 5, 10, 15), box(10, 5, 16, 15)], height)

        self.assertEqual(int(mask[10, 9]), 255)
        self.assertEqual(int(mask[10, 10]), 255)
        self.assertEqual(int(height[10, 9]), int(height[10, 10]))
        self.assertTrue(np.all(depth[mask > 0] > 0))


    def test_touching_saline_and_fresh_polygons_share_saline_waterline(self):
        height = np.full((20, 20), 80, dtype=np.uint8)
        height[5:15, 10:16] = 130
        mask, depth, _ = self.maps(
            [box(4, 5, 10, 15), box(10, 5, 16, 15)], height,
            classes=["Lake", "Alkaline Lake"])

        self.assertEqual(int(mask[10, 9]), SALINE_LAKE)
        self.assertEqual(int(mask[10, 10]), SALINE_LAKE)
        self.assertEqual(int(height[10, 9]), int(height[10, 10]))
        self.assertTrue(np.all(depth[mask > 0] > 0))

if __name__ == "__main__":
    unittest.main()
