"""PNV NoData interpolation retains categorical classes and water masks."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from util.working import create_layer

from pipeline.translate import _feather_vegetation, _feather_pnv_band
from pipeline.vegetation import _fill_nodata_land


class VegetationTests(unittest.TestCase):
    def test_holes_copy_nearest_class_without_changing_water_or_valid_cells(self):
        vegetation = np.array([
            [1, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 32],
        ], dtype=np.uint8)
        land = np.full(vegetation.shape, 255, dtype=np.uint8)
        land[1, 1] = 0
        land[2, 5] = 0
        land[4, 6] = 0

        self.assertEqual(_fill_nodata_land(vegetation, land), 43)
        self.assertEqual(vegetation[0, 0], 1)
        self.assertEqual(vegetation[5, 7], 32)
        self.assertEqual(vegetation[1, 2], 1)
        self.assertEqual(vegetation[4, 5], 32)
        self.assertTrue(np.all(vegetation[land == 0] == 0))
        self.assertTrue(np.all(vegetation[land != 0] != 0))
        self.assertEqual(_fill_nodata_land(vegetation, land), 0)

    def test_unclassified_land_without_any_source_cannot_be_filled(self):
        with self.assertRaisesRegex(ValueError, "without a classified PNV cell"):
            _fill_nodata_land(np.zeros((3, 3), dtype=np.uint8),
                              np.full((3, 3), 255, dtype=np.uint8))

    def test_feather_blends_classes_across_band_seam_without_changing_water(self):
        vegetation = np.full((520, 30), 15, dtype=np.uint8)
        vegetation[:, 15:] = 27
        land = np.full_like(vegetation, 255)
        land[1, 3] = 0
        land[259:263, 13] = 0
        land[4, 25:] = 0
        with tempfile.TemporaryDirectory() as directory:
            build = Path(directory)
            for name, pixels in (("vegetation", vegetation), ("landmask", land)):
                with create_layer(build / f"{name}.tif", 30, 520) as output:
                    output.write(pixels, 1)
            _feather_vegetation(build)
            with rasterio.open(build / "vegetation.tif") as result:
                actual = result.read(1)
        self.assertTrue(np.all(actual[land == 0] == 0))
        self.assertTrue(np.all(np.isin(actual[land != 0], [15, 27])))
        self.assertTrue(np.all(actual[:, :9][land[:, :9] != 0] == 15))
        self.assertTrue(np.all(actual[5:, 21:][land[5:, 21:] != 0] == 27))
        self.assertTrue(np.any(actual[240:280, 12:15] == 27))
        self.assertTrue(np.any(actual[240:280, 15:18] == 15))

    def test_feather_is_independent_of_processing_band_edges(self):
        source = np.full((40, 32), 15, dtype=np.uint8)
        source[:, 16:] = 27
        valid = np.ones_like(source, dtype=bool)
        full = _feather_pnv_band(source, valid, 0, 40, 100)
        top = _feather_pnv_band(source[:24], valid[:24], 0, 20, 100)
        bottom = _feather_pnv_band(source[16:], valid[16:], 4, 20, 120)
        np.testing.assert_array_equal(full, np.vstack([top, bottom]))
