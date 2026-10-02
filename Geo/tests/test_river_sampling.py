"""Dense-reference checks for compact river sampling."""

import tempfile
from contextlib import ExitStack
from pathlib import Path
import unittest

import numpy as np
import rasterio
from affine import Affine
from rasterio.features import rasterize
from scipy.ndimage import maximum_filter, minimum_filter
from shapely.geometry import LineString

from pipeline.river_sampling import collect_inputs, morphology, write_values


class RiverSamplingTests(unittest.TestCase):

    @staticmethod
    def _dataset(stack, path, values, transform):
        with rasterio.open(
                path, "w", driver="GTiff", width=values.shape[1], height=values.shape[0],
                count=1, dtype=values.dtype, transform=transform) as dataset:
            dataset.write(values, 1)
        return stack.enter_context(rasterio.open(path, "r+"))

    def test_collect_inputs_matches_dense_filters_and_global_rasterize_at_boundaries(self):
        rows, cols = 520, 17
        transform = Affine(1, 0, 0, 0, -1, rows)
        flat = np.arange(rows * cols, dtype=np.int64).reshape(rows, cols)
        height = ((flat * 37 + 19) % 254 + 1).astype(np.uint8)
        river = np.full((rows, cols), 255, dtype=np.uint8)
        lake = ((flat % 41 == 0) | (flat == 0) | (flat == rows * cols - 1)).astype(np.uint8) * 255
        land = np.full((rows, cols), 255, dtype=np.uint8)
        land[0, 0] = 0
        land[-1, -1] = 0
        fallback = ((flat * 11) % 255).astype(np.uint8)
        shore = ((flat * 13 + 3) % 255).astype(np.uint8)
        # Includes world edges, a horizontal segment on an internal band edge,
        # and diagonals passing exactly through pixel corners across that edge.
        lines = [
            LineString([(0, rows), (cols, 0)]),
            LineString([(0, rows - 256), (cols, rows - 256)]),
            LineString([(0, rows - 255), (17, rows - 272)]),
            LineString([(0, 0), (cols, rows)]),
        ]

        arrays = (height, river, lake, land, fallback, shore)
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            datasets = [self._dataset(stack, Path(temporary) / f"{index}.tif", array, transform)
                        for index, array in enumerate(arrays)]
            data = collect_inputs(lines, transform, *datasets)

        water = river > 0
        center = rasterize(
            ((line, 1) for line in lines), out_shape=(rows, cols),
            transform=transform, all_touched=True, dtype=np.uint8,
        ).astype(bool) & water
        water_pixels = np.flatnonzero(water)
        pixels = np.flatnonzero(center)
        rr, cc = np.divmod(pixels, cols)
        water_rr, water_cc = np.divmod(water_pixels, cols)
        lake_levels = minimum_filter(np.where(lake > 0, height, 255), size=3)

        np.testing.assert_array_equal(data["water_pixels"], water_pixels)
        np.testing.assert_array_equal(data["pixels"], pixels)
        np.testing.assert_array_equal(
            data["samples"], minimum_filter(height, size=5)[rr, cc].astype(np.float64))
        np.testing.assert_array_equal(data["center_lake_levels"], lake_levels[rr, cc])
        np.testing.assert_array_equal(
            data["center_ocean_near"], (~minimum_filter(land > 0, size=3))[rr, cc])
        np.testing.assert_array_equal(
            data["water_lake_levels"], lake_levels[water_rr, water_cc])
        np.testing.assert_array_equal(data["ceiling"], shore[water_rr, water_cc])
        np.testing.assert_array_equal(data["fallback"], fallback[water_rr, water_cc])
        self.assertEqual((data["rows"], data["cols"]), (rows, cols))

    def test_morphology_matches_dense_masked_reference_at_world_and_band_edges(self):
        rows, cols = 520, 13
        rr, cc = np.indices((rows, cols))
        water = ((rr + 2 * cc) % 5 != 0)
        water[0, :4] = True
        water[-1, -4:] = True
        water[254:259, :] = True
        pixels = np.flatnonzero(water)
        values = ((pixels * 31 + 97) % 255).astype(np.uint8)
        dense = np.zeros((rows, cols), dtype=np.uint8)
        dense.ravel()[pixels] = values

        floor3 = minimum_filter(np.where(water, dense, 255), size=3)
        floor5 = minimum_filter(np.where(water, dense, 255), size=5)
        opened5 = maximum_filter(np.where(water, floor5, 0), size=5)
        references = {
            "floor3": np.minimum(dense[water], floor3[water]),
            "drop5": np.minimum(dense[water], floor5[water]),
            "crest5": np.minimum(dense[water], opened5[water]),
        }
        for operation, expected in references.items():
            actual = morphology(values, pixels, rows, cols, operation)
            np.testing.assert_array_equal(actual, expected, err_msg=operation)
        np.testing.assert_array_equal(values, ((pixels * 31 + 97) % 255).astype(np.uint8))

    def test_empty_river_returns_typed_empty_arrays_and_write_preserves_raster(self):
        rows, cols = 4, 5
        transform = Affine(1, 0, 0, 0, -1, rows)
        height = np.arange(rows * cols, dtype=np.uint8).reshape(rows, cols)
        zeros = np.zeros_like(height)
        land = np.full_like(height, 255)
        fallback = np.full_like(height, 73)
        shore = np.full_like(height, 91)
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            arrays = (height, zeros, zeros, land, fallback, shore)
            datasets = [self._dataset(stack, Path(temporary) / f"{index}.tif", array, transform)
                        for index, array in enumerate(arrays)]
            data = collect_inputs([LineString([(0, rows), (cols, 0)])],
                                  transform, *datasets)
            write_values(datasets[4], data["water_pixels"], data["fallback"])
            np.testing.assert_array_equal(datasets[4].read(1), fallback)

        self.assertEqual(data["water_pixels"].dtype, np.dtype(np.int64))
        self.assertEqual(data["pixels"].dtype, np.dtype(np.int64))
        self.assertEqual(data["samples"].dtype, np.dtype(np.float64))
        self.assertEqual(data["center_lake_levels"].dtype, np.dtype(np.uint8))
        self.assertEqual(data["center_ocean_near"].dtype, np.dtype(bool))
        self.assertEqual(data["water_lake_levels"].dtype, np.dtype(np.uint8))
        self.assertEqual(data["ceiling"].dtype, np.dtype(np.uint8))
        self.assertEqual(data["fallback"].dtype, np.dtype(np.uint8))
        for key in ("water_pixels", "pixels", "samples", "center_lake_levels",
                    "center_ocean_near", "water_lake_levels", "ceiling", "fallback"):
            self.assertEqual(data[key].size, 0)
        np.testing.assert_array_equal(
            morphology(np.empty(0, np.uint8), np.empty(0, np.int64), rows, cols, "crest5"),
            np.empty(0, np.uint8),
        )


if __name__ == "__main__":
    unittest.main()
