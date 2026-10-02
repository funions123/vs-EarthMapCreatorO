"""Windowed lake writer parity across tile seams and bounded world edges."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import fiona
import numpy as np
import rasterio
from affine import Affine
from pyproj import Transformer
from rasterio.crs import CRS
from shapely.geometry import box, mapping
from shapely.ops import transform as project

from pipeline.lakes import build_lake_maps, write_lake_maps
from util.projection import MasterGrid
from util.working import create_layer


class LakeWindowWriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.build = self.root / "build"
        self.work.mkdir()
        self.build.mkdir()

    def _write_polygons(self, grid, polygons):
        inverse = Transformer.from_crs(grid.crs, "EPSG:4326", always_xy=True)
        path = self.work / "crop_lakes.gpkg"
        with fiona.open(
            path, "w", driver="GPKG", crs="EPSG:4326",
            schema={"geometry": "Polygon", "properties": {
                "featurecla": "str:32", "name": "str:80"}},
        ) as destination:
            for index, polygon in enumerate(polygons):
                destination.write({
                    "geometry": mapping(project(inverse.transform, polygon)),
                    "properties": {"featurecla": "Lake", "name": f"Lake {index}"},
                })
        return path

    def _assert_writer_matches_array_path(self, shape, polygons, height, land):
        rows, columns = shape
        grid = MasterGrid(Affine(1000, 0, 0, 0, -1000, rows * 1000),
                          columns, rows, CRS.from_epsg(3857))
        gpkg = self._write_polygons(grid, polygons)
        expected_height = height.copy()
        expected_land = land.copy()
        expected_mask, expected_depth = build_lake_maps(
            gpkg, grid, expected_height, expected_land)

        with create_layer(self.build / "heightmap.tif", columns, rows) as output:
            output.write(height, 1)
        with create_layer(self.build / "landmask.tif", columns, rows) as output:
            output.write(land, 1)
        write_lake_maps(self.work, self.build, grid,
                        SimpleNamespace(SALINE_LAKE_NAMES=()))

        for name, expected in (
            ("heightmap.tif", expected_height),
            ("landmask.tif", expected_land),
            ("lake_mask.tif", expected_mask),
            ("lake_depth.tif", expected_depth),
        ):
            with rasterio.open(self.build / name) as source:
                np.testing.assert_array_equal(source.read(1), expected, err_msg=name)
        self.assertFalse(any(path.name.startswith("lakes-") for path in self.build.iterdir()))

    def test_large_lake_matches_array_path_across_both_tile_axes(self):
        shape = (700, 1100)
        rows, columns = np.indices(shape)
        height = ((rows * 17 + columns * 29) % 254 + 1).astype(np.uint8)
        land = np.zeros(shape, dtype=np.uint8)
        polygons = [
            box(430000, 70000, 970000, 650000),
            # A nearby lake makes bank ownership and polygon order observable.
            box(975000, 260000, 1040000, 430000),
        ]
        self._assert_writer_matches_array_path(shape, polygons, height, land)

    def test_world_filling_lake_keeps_minimum_depth_at_tile_boundaries(self):
        shape = (530, 530)
        height = np.arange(shape[0] * shape[1], dtype=np.uint32)
        height = (height % 200 + 20).astype(np.uint8).reshape(shape)
        land = np.zeros(shape, dtype=np.uint8)
        # Buffering in lake loading makes this cover every output cell.
        polygons = [box(0, 0, shape[1] * 1000, shape[0] * 1000)]
        self._assert_writer_matches_array_path(shape, polygons, height, land)
        with rasterio.open(self.build / "lake_depth.tif") as source:
            self.assertEqual(set(np.unique(source.read(1))), {1})


if __name__ == "__main__":
    unittest.main()
