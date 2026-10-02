"""Regression coverage for the bounded TIFF river pipeline."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import LineString

from pipeline.rivers import _write_depth, build_river_maps, write_river_maps
from util.working import create_layer, windows
from util.projection import MasterGrid


class Config:
    RIVER_MIN_WIDTH_PIXELS = 2.0
    RIVER_WIDTH_REFERENCE_FLOW_CMS = 100.0
    RIVER_MAX_WIDTH_PIXELS = 8.0
    RIVER_SURFACE_WINDOW_BLOCKS = 5
    RIVER_MIN_DEPTH_BLOCKS = 1
    RIVER_MAX_DEPTH_BLOCKS = 3
    RIVER_BANK_WIDTH_BLOCKS = 3
    RIVER_BANK_SLOPE = 1
    TERRAIN_SEA_LEVEL_Y = 92
    RIVER_COASTAL_CONNECTION_MIN_UPSTREAM_AREA_SQKM = 10000
    RIVER_COASTAL_CONNECTION_MAX_DISTANCE_METRES = 3000


def _write_layer(path, values):
    rows, cols = values.shape
    with create_layer(path, cols, rows) as output:
        for window in windows(cols, rows):
            y, x = int(window.row_off), int(window.col_off)
            output.write(values[y:y + int(window.height), x:x + int(window.width)],
                         1, window=window)


class TiledRiverMapsTests(unittest.TestCase):
    def _compare_writer(self, height, land, lake, reaches):
        rows, cols = height.shape
        grid = MasterGrid(Affine(30, 0, 0, 0, -30, rows * 30),
                          cols, rows, CRS.from_epsg(32636))
        expected_height = height.copy()
        expected_mask, expected_surface, expected_depth = build_river_maps(
            reaches, grid, expected_height, land, lake, Config)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            build = root / "build"
            work.mkdir()
            build.mkdir()
            _write_layer(build / "heightmap.tif", height)
            _write_layer(build / "landmask.tif", land)
            _write_layer(build / "lake_mask.tif", lake)

            with patch("pipeline.rivers.load_river_lines",
                       return_value=(list(reaches), 30, [])):
                write_river_maps(work, build, grid, None, Config)

            expected = {
                "heightmap.tif": expected_height,
                "river.tif": expected_mask,
                "river_surface.tif": expected_surface,
                "river_depth.tif": expected_depth,
            }
            for name, values in expected.items():
                with self.subTest(layer=name), rasterio.open(build / name) as actual:
                    np.testing.assert_array_equal(actual.read(1), values)
            self.assertFalse(list(build.glob("_river_*.tif")))

    def test_junction_filters_depth_and_banks_are_identical_across_tile_edges(self):
        rows, cols = 519, 521
        height = np.full((rows, cols), 110, dtype=np.uint8)
        height[:, 505:517] = np.linspace(95, 70, rows, dtype=np.uint8)[:, None]
        height[503:516, :] = 83
        land = np.full_like(height, 255)
        land[:, :2] = 0
        lake = np.zeros_like(height)
        lake[514:, 505:519] = 255
        height[lake > 0] = 68
        top = rows * 30
        trunk = LineString([(511.5 * 30, top), (511.5 * 30, 0)])
        branch = LineString([(cols * 30, top - 510.5 * 30),
                             (511.5 * 30, top - 510.5 * 30)])
        edge = LineString([(2.5 * 30, top), (2.5 * 30, 0)])

        self._compare_writer(height, land, lake,
                             [(trunk, 400), (branch, 100), (edge, 0)])

    def test_empty_river_preserves_world_and_writes_zero_layers(self):
        rows, cols = 517, 515
        yy, xx = np.indices((rows, cols))
        height = ((yy + 2 * xx) % 180 + 40).astype(np.uint8)
        land = np.full_like(height, 255)
        land[0, :] = 0
        land[:, -1] = 0
        lake = np.zeros_like(height)
        lake[255:260, 510:] = 255

        self._compare_writer(height, land, lake, [])

    def test_capped_depth_does_not_treat_an_all_water_interior_tile_as_distance_minus_one(self):
        size = 1031
        river = np.full((size, size), 255, dtype=np.uint8)
        river[[0, -1], :] = 0
        river[:, [0, -1]] = 0
        expected = np.minimum.reduce((
            np.indices((size, size))[0],
            np.indices((size, size))[1],
            size - 1 - np.indices((size, size))[0],
            size - 1 - np.indices((size, size))[1],
        ))
        expected = np.clip(expected, 1, Config.RIVER_MAX_DEPTH_BLOCKS).astype(np.uint8)
        expected[river == 0] = 0

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_layer(root / "river.tif", river)
            with rasterio.open(root / "river.tif") as source, \
                    create_layer(root / "depth.tif", size, size) as output:
                _write_depth(source, output, Config.RIVER_MAX_DEPTH_BLOCKS,
                             Config.RIVER_MIN_DEPTH_BLOCKS)
            with rasterio.open(root / "depth.tif") as actual:
                np.testing.assert_array_equal(actual.read(1), expected)

    def test_world_filling_river_retains_global_distance_transform_semantics(self):
        size = 519
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_layer(root / "river.tif", np.full((size, size), 255, dtype=np.uint8))
            with rasterio.open(root / "river.tif") as source, create_layer(root / "depth.tif", size, size) as output:
                _write_depth(source, output, 3, 1)
            with rasterio.open(root / "depth.tif") as actual:
                np.testing.assert_array_equal(actual.read(1), np.ones((size, size), dtype=np.uint8))


if __name__ == "__main__":
    unittest.main()
