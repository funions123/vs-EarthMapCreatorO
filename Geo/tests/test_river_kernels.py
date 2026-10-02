"""Native river propagation preserves FIFO and repeated-cell reach semantics."""
import unittest
from collections import deque

import numpy as np

from pipeline.river_profiles import _relax_surface, _lower_downstream_rises


class RiverKernelTests(unittest.TestCase):
    def test_fifo_relaxation_matches_at_edges_and_queue_growth(self):
        random = np.random.default_rng(20261001)
        for case in range(12):
            river = random.random((17, 19)) > 0.2
            surface = random.integers(1, 255, river.shape, dtype=np.uint8)
            expected = surface.copy()
            rows, columns = np.nonzero(river)
            queue = deque(zip(rows.tolist(), columns.tolist()))
            while queue:
                row, col = queue.popleft()
                ceiling = int(expected[row, col]) + 1
                for nr in range(max(0, row - 1), min(17, row + 2)):
                    for nc in range(max(0, col - 1), min(19, col + 2)):
                        if river[nr, nc] and expected[nr, nc] > ceiling:
                            expected[nr, nc] = ceiling
                            queue.append((nr, nc))
            with self.subTest(case=case):
                _relax_surface(surface, river, rows, columns)
                np.testing.assert_array_equal(surface, expected)

    def test_repeated_cells_and_shared_reaches_converge_downstream(self):
        surface = np.array([[100, 80, 90, 60, 110, 70]], dtype=np.uint8)
        first = np.array([0, 1, 2, 1, 3], dtype=np.int32)
        second = np.array([4, 2, 5], dtype=np.int32)
        ways = [(np.zeros_like(cols), cols, np.zeros_like(cols))
                for cols in (first, second)]
        self.assertTrue(_lower_downstream_rises(surface, ways))
        np.testing.assert_array_equal(surface, [[100, 80, 80, 60, 110, 70]])
        self.assertFalse(_lower_downstream_rises(surface, ways))
