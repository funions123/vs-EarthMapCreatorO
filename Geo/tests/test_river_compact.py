"""Compact global river fitting is byte-identical to the dense reference."""
import tempfile
from contextlib import ExitStack
from pathlib import Path
import unittest

import numpy as np
import rasterio
from affine import Affine
from scipy.ndimage import minimum_filter
from shapely.geometry import LineString

from pipeline.river_compact import fit_compact
from pipeline.river_profiles import fitted_river_surface
from pipeline.river_sampling import collect_inputs


def _write_tiff(path, values, transform):
    with rasterio.open(path, "w", driver="GTiff", width=values.shape[1],
                       height=values.shape[0], count=1, dtype=values.dtype,
                       transform=transform) as output:
        output.write(values, 1)


class CompactRiverSolverTests(unittest.TestCase):
    def _compare(self, lines, height, river, lake, land, fallback, ceiling):
        rows, cols = height.shape
        transform = Affine(1, 0, 0, 0, -1, rows)
        expected = fitted_river_surface(
            lines, transform, height, river, lake, land, fallback, ceiling)
        layers = {
            "height": height,
            "river": river.astype(np.uint8) * 255,
            "lake": lake.astype(np.uint8) * 255,
            "land": land.astype(np.uint8) * 255,
            "fallback": fallback,
            "ceiling": ceiling,
        }
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            for name, values in layers.items():
                _write_tiff(root / f"{name}.tif", values, transform)
            sources = {name: stack.enter_context(rasterio.open(root / f"{name}.tif"))
                       for name in layers}
            data = collect_inputs(lines, transform, sources["height"],
                                  sources["river"], sources["lake"],
                                  sources["land"], sources["fallback"],
                                  sources["ceiling"])
            actual = fit_compact(lines, transform, data)

        self.assertEqual(actual.dtype, np.uint8)
        np.testing.assert_array_equal(actual, expected.ravel()[data["water_pixels"]])

    def test_junction_repeated_cells_and_edge_reaches_match_dense_fit(self):
        rows, cols = 21, 23
        yy, xx = np.indices((rows, cols))
        height = (138 - yy - (xx // 7) + ((3 * yy + xx) % 5)).astype(np.uint8)
        river = np.zeros((rows, cols), dtype=bool)
        river[:, 10:13] = True
        river[7:11, 2:13] = True
        river[3:6, 10:23] = True
        river[:, :2] = True
        lake = np.zeros_like(river)
        lake[19:, 9:14] = True
        river &= ~lake
        land = np.ones_like(river)
        land[:, -1] = False

        # Source-to-outlet order includes an exact repeated coordinate at the
        # junction and a segment that revisits already sampled raster cells.
        lines = [
            LineString([(11.5, 20.5), (11.5, 12.5), (10.5, 12.5),
                        (11.5, 12.5), (11.2, 11.8), (11.5, 1.5)]),
            LineString([(2.5, 12.5), (8.5, 12.5), (11.5, 12.5)]),
            LineString([(22.8, 16.5), (16.5, 16.5), (11.5, 16.5)]),
            LineString([(0.2, 20.8), (0.2, 0.2)]),
        ]
        dry = land & ~river & ~lake
        ceiling = minimum_filter(np.where(dry, height, 255), size=3).astype(np.uint8)
        fit_height = height.copy()
        fit_height[river] = np.minimum(fit_height[river], ceiling[river])
        fallback = np.zeros_like(height)
        fallback[river] = minimum_filter(fit_height, size=5)[river]
        near_lake = minimum_filter(np.where(lake, height, 255), size=3)
        fallback[river & (near_lake < 255)] = near_lake[river & (near_lake < 255)]

        self._compare(lines, fit_height, river, lake, land, fallback, ceiling)

    def test_centerline_absent_from_nonempty_water_returns_fallback(self):
        rows, cols = 9, 11
        height = np.arange(rows * cols, dtype=np.uint8).reshape(rows, cols) + 80
        river = np.zeros((rows, cols), dtype=bool)
        river[3:6, 3:8] = True
        lake = np.zeros_like(river)
        land = np.ones_like(river)
        fallback = np.zeros_like(height)
        fallback[river] = height[river] - 4
        ceiling = np.full_like(height, 255)
        outside = LineString([(20.5, 20.5), (25.5, 25.5)])

        self._compare([outside], height, river, lake, land, fallback, ceiling)


if __name__ == "__main__":
    unittest.main()
