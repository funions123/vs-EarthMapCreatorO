"""HydroRIVERS centerline and final-grid river invariants."""
import tempfile
from pathlib import Path
import unittest

import fiona
import numpy as np
from affine import Affine
from rasterio.crs import CRS
from scipy.ndimage import minimum_filter
from shapely.geometry import LineString

from pipeline.river_profiles import _carve_drop_transition, _flatten_cross_channel, _lower_downstream_rises, _lower_narrow_crests
from pipeline.rivers import _coastal_connectors, _grade_banks, build_river_maps, load_river_lines, river_width_metres
from util.projection import Bounds4326, MasterGrid


class Config:
    RIVER_MIN_WIDTH_PIXELS = 2.0
    RIVER_WIDTH_REFERENCE_FLOW_CMS = 100.0
    RIVER_MAX_WIDTH_PIXELS = 8.0
    RIVER_MIN_FLOW_CMS = 1.0
    RIVER_MIN_UPSTREAM_AREA_SQKM = 1000.0
    RIVER_AUTO_SCALE_UPSTREAM_AREA = True
    RIVER_SURFACE_WINDOW_BLOCKS = 5
    RIVER_MIN_DEPTH_BLOCKS = 1
    RIVER_MAX_DEPTH_BLOCKS = 3
    RIVER_BANK_WIDTH_BLOCKS = 3
    RIVER_BANK_SLOPE = 1
    TERRAIN_SEA_LEVEL_Y = 92
    RIVER_COASTAL_CONNECTION_MIN_UPSTREAM_AREA_SQKM = 10000
    RIVER_COASTAL_CONNECTION_MAX_DISTANCE_METRES = 3000


class RiverMapsTests(unittest.TestCase):

    def test_surface_never_rises_above_local_valley_and_lakes_win(self):
        grid = MasterGrid(Affine(30, 0, 0, 0, -30, 600), 20, 20, CRS.from_epsg(32636))
        height = np.full((20, 20), 90, dtype=np.uint8)
        height[:, 9:12] = 60
        land = np.full((20, 20), 255, dtype=np.uint8)
        lake = np.zeros((20, 20), dtype=np.uint8)
        lake[9:12, 9:12] = 255
        geometry = LineString([(300, 0), (300, 600)])

        mask, surface, depth = build_river_maps(
            [(geometry, 0)], grid, height, land, lake, Config)
        river = mask > 0

        self.assertEqual(set(np.unique(mask)), {0, 255})
        self.assertTrue(np.all(mask[lake > 0] == 0))
        self.assertTrue(np.all(surface[river] <= 60))
        self.assertTrue(np.all(surface[~river] == 0))
        self.assertTrue(np.all((depth[river] >= 1) & (depth[river] <= 3)))
        self.assertTrue(np.all(depth[~river] == 0))


    def test_lowland_terminal_reach_connects_to_coast(self):
        grid = MasterGrid(Affine(100, 0, 0, 0, -100, 6000), 60, 60, CRS.from_epsg(32636))
        height = np.full((60, 60), 92, dtype=np.uint8)
        land = np.full_like(height, 255)
        land[:, 50:] = 0
        lake = np.zeros_like(height)
        source = LineString([(1050, 2950), (2550, 2950)])
        outlets = [((2550, 2950), 100, 20000)]

        connectors = _coastal_connectors(outlets, grid, height, land, lake, Config)
        mask, surface, _ = build_river_maps([(source, 100), *connectors],
                                            grid, height, land, lake, Config)

        self.assertEqual(len(connectors), 1)
        self.assertTrue(np.all(mask[30, 25:50] > 0))
        self.assertTrue(np.all(surface[30, 25:50] == 92))
        self.assertEqual(int(mask[30, 50]), 0)

    def test_coastal_connector_does_not_cut_ridges_or_lakes(self):
        grid = MasterGrid(Affine(100, 0, 0, 0, -100, 6000), 60, 60, CRS.from_epsg(32636))
        height = np.full((60, 60), 92, dtype=np.uint8)
        land = np.full_like(height, 255)
        land[:, 50:] = 0
        lake = np.zeros_like(height)
        outlets = [((2550, 2950), 100, 20000)]

        height[:, 40] = 95
        self.assertEqual(_coastal_connectors(outlets, grid, height, land, lake, Config), [])
        height[:, 40] = 92
        lake[:, 35] = 255
        self.assertEqual(_coastal_connectors(outlets, grid, height, land, lake, Config), [])
        lake[:, 35] = 0
        self.assertEqual(_coastal_connectors([((2550, 2950), 100, 5000)],
                                              grid, height, land, lake, Config), [])

    def test_discharge_widens_channel_up_to_eight_blocks(self):
        grid = MasterGrid(Affine(100, 0, 0, 0, -100, 2000), 20, 20, CRS.from_epsg(32636))
        land = np.full((20, 20), 255, dtype=np.uint8)
        lake = np.zeros((20, 20), dtype=np.uint8)

        def render(flow, x=1050):
            line = LineString([(x, 2000), (x, 0)])
            mask, _, _ = build_river_maps(
                [(line, flow)], grid, np.full((20, 20), 90, dtype=np.uint8), land, lake, Config)
            return np.count_nonzero(mask[10])

        self.assertLess(render(0), render(400))
        self.assertLess(render(400), render(1600))
        self.assertLess(render(1600), render(3600))
        self.assertEqual(render(3600), 8)
        self.assertEqual(render(1000000), 8)
        self.assertEqual(render(1000000, 1000), 8)
        self.assertEqual(river_width_metres(None, 100, Config), 200)
        self.assertEqual(river_width_metres(1000000, 100, Config), 800)

    def test_minimum_mean_flow_excludes_dry_reaches_before_rasterization(self):
        with tempfile.TemporaryDirectory() as temporary:
            datasets = Path(temporary)
            source_dir = datasets / "HydroRIVERS_v10_na_shp"
            source_dir.mkdir()
            source = source_dir / "HydroRIVERS_v10_na.shp"
            schema = {"geometry": "LineString",
                      "properties": {"UPLAND_SKM": "float:10.1", "DIS_AV_CMS": "float:10.3",
                                     "NEXT_DOWN": "int"}}
            with fiona.open(source, "w", driver="ESRI Shapefile",
                            crs="EPSG:4326", schema=schema) as dst:
                for lon, flow, area in ((0.2, 0.0, 15000.0), (0.4, 0.999, 15000.0),
                                        (0.6, 1.0, 1500.0), (0.7, 4.0, 2000.0),
                                        (0.8, 5.0, 3000.0)):
                    dst.write({"geometry": LineString([(lon, 0.8), (lon, 0.2)]).__geo_interface__,
                               "properties": {"UPLAND_SKM": area, "DIS_AV_CMS": flow,
                                              "NEXT_DOWN": 0}})
            grid = MasterGrid(Affine(1000, 0, 0, 0, -1000, 112000),
                              112, 112, CRS.from_epsg(3857))
            bounds = Bounds4326(0, 0, 1, 1)
            reaches, _, outlets = load_river_lines(
                datasets, bounds, grid, (25600, 25600), Config)
            self.assertEqual([flow for _, flow in reaches], [1.0, 4.0, 5.0])
            self.assertEqual([flow for _, flow, _ in outlets], [1.0, 4.0, 5.0])
            for output_shape in ((12800, 12800), (25600, 12800)):
                reaches, _, outlets = load_river_lines(
                    datasets, bounds, grid, output_shape, Config)
                self.assertEqual([flow for _, flow in reaches], [4.0, 5.0])
                self.assertEqual([flow for _, flow, _ in outlets], [4.0, 5.0])

            class NoAutoScale(Config):
                RIVER_AUTO_SCALE_UPSTREAM_AREA = False

            reaches, _, _ = load_river_lines(
                datasets, bounds, grid, (12800, 12800), NoAutoScale)
            self.assertEqual([flow for _, flow in reaches], [1.0, 4.0, 5.0])

    def test_terrain_bump_does_not_raise_downstream_water(self):
        grid = MasterGrid(Affine(30, 0, 0, 0, -30, 900), 30, 30, CRS.from_epsg(32636))
        height = np.full((30, 30), 85, dtype=np.uint8)
        height[:, 13:18] = 50
        height[13:17, 13:18] = 65  # erroneous ridge across the channel
        height[10:21, 10:21] = 65  # DEM ridge wider than the local filter
        land = np.full_like(height, 255)
        lake = np.zeros_like(height)
        lake[25:, 13:18] = 255
        line = LineString([(450, 885), (450, 15)])
        _, surface, _ = build_river_maps([(line, 0)], grid, height, land, lake, Config)
        levels = surface[:25, 15]
        self.assertTrue(np.all(np.diff(levels.astype(np.int16)) <= 0), levels)
        self.assertEqual(levels[-1], 50)

    def test_tributary_joins_trunk_at_same_level(self):
        grid = MasterGrid(Affine(30, 0, 0, 0, -30, 1200), 40, 40, CRS.from_epsg(32636))
        height = np.full((40, 40), 90, np.uint8)
        height[4:37, 18:23] = np.linspace(72, 50, 33, dtype=np.uint8)[:, None]
        height[15:19, 4:21] = 72
        height[15:19, 16:23] = 64
        land = np.full_like(height, 255)
        lake = np.zeros_like(height)
        lake[36:, 18:23] = 255
        trunk = LineString([(600, 1050), (600, 90)])
        branch = LineString([(150, 690), (600, 690)])
        _, surface, _ = build_river_maps([(trunk, 0), (branch, 0)],
                                          grid, height, land, lake, Config)
        self.assertTrue(np.all(np.diff(surface[4:36, 20].astype(np.int16)) <= 0))
        self.assertTrue(np.all(np.diff(surface[17, 5:21].astype(np.int16)) <= 0))
        self.assertLessEqual(abs(int(surface[17, 20]) - int(surface[17, 19])), 1)

    def test_river_is_carved_below_low_bank_without_raising_shore(self):
        grid = MasterGrid(Affine(30, 0, 0, 0, -30, 900), 30, 30, CRS.from_epsg(32636))
        height = np.full((30, 30), 90, dtype=np.uint8)
        height[:, 14:17] = 105
        height[:, 13] = 70
        original = height.copy()
        land = np.full_like(height, 255)
        line = LineString([(450, 900), (450, 0)])
        mask, surface, depth = build_river_maps(
            [(line, 0)], grid, height, land, np.zeros_like(height), Config)
        river = mask > 0
        self.assertTrue(river.any())
        self.assertTrue(np.all(height[~river] <= original[~river]))
        self.assertTrue(np.all(surface[river] <= 71))
        shore = minimum_filter(np.where(~river, original, 255), size=3)
        edge = river & (shore < 255)
        self.assertTrue(np.all(surface[edge].astype(np.int16) <= shore[edge].astype(np.int16)))
        self.assertTrue(np.all(height[river] == surface[river]))
        self.assertTrue(np.all(depth[river] >= 1))

    def test_bank_touching_different_water_levels_keeps_high_side(self):
        height = np.full((9, 9), 120, dtype=np.uint8)
        river = np.zeros_like(height, dtype=bool)
        river[2:7, 3:6] = True
        surface = np.zeros_like(height)
        surface[river] = 116
        surface[3, 5] = 114
        height[river] = surface[river]
        height[5, 6] = 115  # Natural low ground must never be raised.
        original = height.copy()

        _grade_banks(height, river, surface, np.ones_like(river), 3, 1)

        self.assertEqual(int(height[2, 6]), 117)
        self.assertEqual(int(height[5, 6]), 115)
        self.assertTrue(np.all(height[~river] <= original[~river]))

    def test_connected_source_to_outlet_path_never_climbs(self):
        surface = np.array([[130, 129, 130, 129, 128, 130, 127]], dtype=np.uint8)
        # Downstream way processed first; upstream way then cuts the junction.
        downstream = (np.zeros(4, dtype=np.int32),
                      np.array([3, 4, 5, 6], dtype=np.int32), None)
        upstream = (np.zeros(4, dtype=np.int32),
                    np.array([0, 1, 2, 3], dtype=np.int32), None)

        self.assertTrue(_lower_downstream_rises(surface, [downstream, upstream]))
        self.assertEqual(surface.tolist(), [[130, 129, 129, 129, 128, 128, 127]])
        self.assertFalse(_lower_downstream_rises(surface, [downstream, upstream]))

    def test_cross_channel_carving_preserves_discrete_downstream_drop(self):
        river = np.zeros((5, 6), dtype=bool)
        river[:, 1:5] = True
        surface = np.full(river.shape, 90, dtype=np.uint8)
        surface[:3, 1:3] = 108
        surface[:3, 3:5] = 107
        surface[3:, 1:5] = 106
        water_y, water_x = np.nonzero(river)
        nearest = np.repeat(np.arange(5), 4)

        self.assertTrue(_flatten_cross_channel(surface, water_y, water_x, nearest, 5))
        self.assertTrue(np.all(surface[:3, 1:5] == 107))
        self.assertTrue(np.all(surface[3:, 1:5] == 106))
        self.assertTrue(np.all(surface[:, [0, 5]] == 90))
        self.assertFalse(_flatten_cross_channel(surface, water_y, water_x, nearest, 5))

    def test_rough_channel_has_no_one_block_surface_crests(self):
        grid = MasterGrid(Affine(30, 0, 0, 0, -30, 900), 30, 30, CRS.from_epsg(32636))
        height = np.full((30, 30), 70, dtype=np.uint8)
        height[:, 13:17] = 71
        land = np.full_like(height, 255)
        line = LineString([(450, 900), (450, 0)])

        mask, surface, _ = build_river_maps(
            [(line, 0)], grid, height, land, np.zeros_like(height), Config)

        self.assertTrue(np.any(mask > 0))
        self.assertTrue(np.all(surface[mask > 0] == 70))
        self.assertTrue(np.all(height[mask > 0] == surface[mask > 0]))

    def test_narrow_river_crest_is_cut_without_flattening_broad_high_reach(self):
        river = np.zeros((15, 25), dtype=bool)
        river[4:11, 3:22] = True
        surface = np.full(river.shape, 90, dtype=np.uint8)
        surface[river] = 70
        surface[6:8, 5:7] = 71
        surface[7, 8:14] = 71
        surface[5:10, 15:20] = 72

        _lower_narrow_crests(surface, river)

        self.assertTrue(np.all(surface[7, 5:14] == 70))
        self.assertTrue(np.all(surface[6:8, 5:7] == 70))
        self.assertEqual(int(surface[7, 17]), 72)
        self.assertTrue(np.all(surface[~river] == 90))

    def test_drop_transition_cuts_early_low_cell_across_channel(self):
        river = np.zeros((16, 12), dtype=bool)
        river[2:14, 3:9] = True
        surface = np.full(river.shape, 90, dtype=np.uint8)
        surface[river] = 128
        surface[7, 5] = 127
        surface[8:14, 3:9] = 127

        _carve_drop_transition(surface, river)

        self.assertTrue(np.all(surface[7, 3:9] == 127))
        self.assertEqual(int(surface[2, 5]), 128)
        self.assertTrue(np.all(surface[~river] == 90))

    def test_steep_channel_has_no_multi_block_water_edges(self):
        grid = MasterGrid(Affine(30, 0, 0, 0, -30, 900), 30, 30, CRS.from_epsg(32636))
        height = np.full((30, 30), 100, dtype=np.uint8)
        height[15:, :] = 70
        height[15:, 11] = 60
        land = np.full_like(height, 255)
        line = LineString([(450, 900), (450, 0)])
        mask, surface, _ = build_river_maps(
            [(line, 1000)], grid, height, land, np.zeros_like(height), Config)
        river = mask > 0
        for a, b in ((river[:, 1:] & river[:, :-1],
                      abs(np.diff(surface.astype(np.int16), axis=1))),
                     (river[1:, :] & river[:-1, :],
                      abs(np.diff(surface.astype(np.int16), axis=0)))):
            self.assertTrue(np.all(b[a] <= 1))
        self.assertTrue(np.all(height[river] == surface[river]))

    def test_lake_mouth_does_not_leave_perched_edge_water(self):
        grid = MasterGrid(Affine(30, 0, 0, 0, -30, 900), 30, 30, CRS.from_epsg(32636))
        height = np.full((30, 30), 95, dtype=np.uint8)
        height[:, 13:17] = 80
        height[20:, 16:] = 110
        lake = np.zeros_like(height)
        lake[20:, 17:] = 255
        height[lake > 0] = 110
        original = height.copy()
        land = np.full_like(height, 255)
        line = LineString([(450, 900), (450, 0)])
        mask, surface, _ = build_river_maps([(line, 100)], grid, height, land, lake, Config)
        river = mask > 0
        shore = minimum_filter(np.where(~river & (lake == 0), original, 255), size=3)
        edge = river & (shore < 255)
        self.assertTrue(edge.any())
        self.assertTrue(np.all(surface[edge].astype(np.int16) <= shore[edge].astype(np.int16)))
        self.assertTrue(np.all(abs(np.diff(surface.astype(np.int16), axis=1))
                               [river[:, 1:] & river[:, :-1]] <= 1))

if __name__ == "__main__":
    unittest.main()
