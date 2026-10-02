"""Fit globally connected river levels using only river-cell storage."""
import heapq

import numpy as np
from numba import njit
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from pipeline.river_profiles import _isotonic_downhill, _limit_profile_steps
from pipeline.river_sampling import morphology


@njit(cache=True)
def _sorted_index(pixels, pixel):
    """Return a compact index for a sorted global pixel, or -1."""
    low = 0
    high = len(pixels)
    while low < high:
        middle = (low + high) // 2
        if pixels[middle] < pixel:
            low = middle + 1
        else:
            high = middle
    if low < len(pixels) and pixels[low] == pixel:
        return low
    return -1


@njit(cache=True)
def _relax_compact(surface, water_pixels, rows, cols):
    """FIFO eight-neighbor relaxation over sorted compact water cells."""
    capacity = max(1, len(water_pixels))
    queue = np.empty(capacity, dtype=np.int64)
    for i in range(len(water_pixels)):
        queue[i] = i
    head = 0
    count = len(water_pixels)
    while count:
        compact = queue[head]
        head = (head + 1) % capacity
        count -= 1
        pixel = water_pixels[compact]
        row, col = pixel // cols, pixel % cols
        ceiling = int(surface[compact]) + 1
        for nr in range(max(0, row - 1), min(rows, row + 2)):
            for nc in range(max(0, col - 1), min(cols, col + 2)):
                adjacent = _sorted_index(water_pixels, nr * cols + nc)
                if adjacent >= 0 and surface[adjacent] > ceiling:
                    surface[adjacent] = ceiling
                    if count == capacity:
                        grown = np.empty(capacity * 2, dtype=np.int64)
                        for i in range(count):
                            grown[i] = queue[(head + i) % capacity]
                        queue = grown
                        capacity *= 2
                        head = 0
                    queue[(head + count) % capacity] = adjacent
                    count += 1


@njit(cache=True)
def _downstream_compact(surface, cells, offsets):
    """Lower ordered reaches with the dense solver's snapshot semantics."""
    changed = False
    scratch = np.empty(len(cells), dtype=np.uint8)
    while True:
        pass_changed = False
        for reach in range(len(offsets) - 1):
            start, stop = offsets[reach], offsets[reach + 1]
            level = 255
            for i in range(start, stop):
                level = min(level, int(surface[cells[i]]))
                scratch[i] = level
            for i in range(start, stop):
                if scratch[i] < surface[cells[i]]:
                    surface[cells[i]] = scratch[i]
                    pass_changed = True
                    changed = True
        if not pass_changed:
            return changed




def _center_neighbors(pixels, rows, cols):
    """Build the dense solver's ordered eight-connected centerline graph."""
    rr, cc = np.divmod(pixels, cols)
    neighbors = [[] for _ in pixels]
    for i, (row, col) in enumerate(zip(rr, cc)):
        for dr, dc in ((-1, -1), (-1, 0), (-1, 1), (0, -1)):
            nr, nc = int(row) + dr, int(col) + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                pixel = nr * cols + nc
                j = int(np.searchsorted(pixels, pixel))
                if j < len(pixels) and pixels[j] == pixel:
                    cost = 1.41421356 if dr and dc else 1.0
                    neighbors[i].append((j, cost))
                    neighbors[j].append((i, cost))
    return rr, cc, neighbors


def _components(neighbors):
    component = np.full(len(neighbors), -1, np.int32)
    components = []
    for start in range(len(neighbors)):
        if component[start] >= 0:
            continue
        group = []
        stack = [start]
        component[start] = len(components)
        while stack:
            i = stack.pop()
            group.append(i)
            for j, _ in neighbors[i]:
                if component[j] < 0:
                    component[j] = component[start]
                    stack.append(j)
        components.append(np.array(group, dtype=np.int32))
    return components


def _ordered_way_cells(lines, transform, water_pixels, rows, cols, tree):
    """Sample source-to-downstream traces into compact water-cell IDs."""
    inverse = ~transform
    edges_from = []
    edges_to = []
    way_cells = []
    for line in lines:
        x, y = line.xy
        col, row = inverse * (np.asarray(x), np.asarray(y))
        cells = []
        for r0, c0, r1, c1 in zip(row[:-1], col[:-1], row[1:], col[1:]):
            steps = max(1, int(np.ceil(max(abs(r1 - r0), abs(c1 - c0)))))
            for step in range(steps):
                r = int(r0 + (r1 - r0) * step / steps)
                c = int(c0 + (c1 - c0) * step / steps)
                if 0 <= r < rows and 0 <= c < cols:
                    compact = int(np.searchsorted(water_pixels, r * cols + c))
                    if (compact < len(water_pixels)
                            and water_pixels[compact] == r * cols + c
                            and (not cells or cells[-1] != compact)):
                        cells.append(compact)
        r, c = int(row[-1]), int(col[-1])
        if 0 <= r < rows and 0 <= c < cols:
            compact = int(np.searchsorted(water_pixels, r * cols + c))
            if compact < len(water_pixels) and water_pixels[compact] == r * cols + c:
                # Deliberately do not suppress a repeated terminal cell. The
                # original solver includes it in the reach snapshot.
                cells.append(compact)
        if len(cells) < 2:
            continue
        compact_cells = np.asarray(cells, dtype=np.int64)
        cell_rows, cell_cols = np.divmod(water_pixels[compact_cells], cols)
        _, indices = tree.query(np.column_stack((cell_rows, cell_cols)))
        indices = np.asarray(indices, dtype=np.int32)
        way_cells.append((compact_cells, indices))
        distinct = indices[np.r_[True, np.diff(indices) != 0]]
        if len(distinct) >= 2:
            edges_from.extend(distinct[:-1])
            edges_to.extend(distinct[1:])
    return edges_from, edges_to, way_cells


def _direct_centerlines(fitted, edges_from, edges_to):
    if not edges_from:
        return fitted
    arcs = csr_matrix((np.ones(len(edges_from), dtype=np.uint8),
                       (edges_from, edges_to)), shape=(len(fitted), len(fitted)))
    count, labels = connected_components(arcs, directed=True, connection="strong")
    del arcs
    levels = np.full(count, 255, dtype=np.uint8)
    np.minimum.at(levels, labels, fitted)
    outgoing = [set() for _ in range(count)]
    indegree = np.zeros(count, dtype=np.int32)
    for source, target in zip(edges_from, edges_to):
        a, b = labels[source], labels[target]
        if a != b and b not in outgoing[a]:
            outgoing[a].add(b)
            indegree[b] += 1
    queue = list(np.flatnonzero(indegree == 0))
    for a in queue:
        for b in outgoing[a]:
            levels[b] = min(levels[b], levels[a])
            indegree[b] -= 1
            if indegree[b] == 0:
                queue.append(b)
    fitted = levels[labels]
    return _limit_profile_steps(fitted, edges_from, edges_to)


def _flatten_cross_channel(surface, nearest, center_count):
    cross_levels = np.full(center_count, 255, dtype=np.uint8)
    np.minimum.at(cross_levels, nearest, surface)
    lowered = cross_levels[nearest]
    changed = np.any(lowered < surface)
    if changed:
        surface[:] = lowered
    return bool(changed)


def _pack_way_cells(way_cells):
    offsets = np.zeros(len(way_cells) + 1, dtype=np.int64)
    for i, (cells, _) in enumerate(way_cells):
        offsets[i + 1] = offsets[i] + len(cells)
    cells = np.empty(offsets[-1], dtype=np.int64)
    for i, (reach, _) in enumerate(way_cells):
        cells[offsets[i]:offsets[i + 1]] = reach
    return cells, offsets


def _settle_surface(surface, water_pixels, rows, cols, nearest, center_count,
                    way_cells, way_offsets):
    while True:
        flattened = _flatten_cross_channel(surface, nearest, center_count)
        _relax_compact(surface, water_pixels, rows, cols)
        if not _downstream_compact(surface, way_cells, way_offsets) and not flattened:
            return


def fit_compact(lines, transform, data):
    """Return fitted river levels aligned with sorted ``water_pixels``.

    All topology and fitting are global. Only values outside the river mask are
    omitted; no tile is solved independently.
    """
    rows = int(data["rows"])
    cols = int(data["cols"])
    water_pixels = np.asarray(data["water_pixels"], dtype=np.int64)
    pixels = np.asarray(data["pixels"], dtype=np.int64)
    surface = np.asarray(data["fallback"], dtype=np.uint8).copy()
    if not lines or not len(water_pixels) or not len(pixels):
        return surface

    rr, cc, neighbors = _center_neighbors(pixels, rows, cols)
    components = _components(neighbors)
    lake_levels = np.asarray(data["center_lake_levels"], dtype=np.uint8)
    lake_near = lake_levels < 255
    ocean_near = np.asarray(data["center_ocean_near"], dtype=bool)
    samples = np.asarray(data["samples"], dtype=np.float64)

    parent = np.full(len(pixels), -1, np.int32)
    distance = np.full(len(pixels), np.inf)
    anchor = np.full(len(pixels), -1, np.int16)
    for group in components:
        shore = group[lake_near[group]]
        if len(shore):
            seeds = shore
            anchor[shore] = lake_levels[shore]
        else:
            coast = group[ocean_near[group]]
            edge = group[(rr[group] == 0) | (cc[group] == 0)
                         | (rr[group] == rows - 1) | (cc[group] == cols - 1)]
            candidates = coast if len(coast) else edge if len(edge) else group
            seeds = [candidates[np.argmin(samples[candidates])]]
            anchor[seeds] = samples[seeds].astype(np.int16)
        queue = [(0.0, int(i)) for i in seeds]
        heapq.heapify(queue)
        distance[seeds] = 0.0
        while queue:
            d, i = heapq.heappop(queue)
            if d != distance[i]:
                continue
            for j, cost in neighbors[i]:
                nd = d + cost
                if nd < distance[j]:
                    distance[j] = nd
                    parent[j] = i
                    heapq.heappush(queue, (nd, j))

    fitted = np.full(len(pixels), -1.0)
    for i in np.argsort(distance)[::-1]:
        if fitted[i] >= 0:
            continue
        path = []
        node = int(i)
        while node >= 0 and fitted[node] < 0:
            path.append(node)
            node = int(parent[node])
        if node >= 0:
            outlet = fitted[node]
        else:
            outlet = float(anchor[path[-1]])
            fitted[path[-1]] = outlet
            path.pop()
        if path:
            fitted[path] = _isotonic_downhill(samples[path], outlet)
            downstream = outlet
            for point in reversed(path):
                fitted[point] = min(fitted[point], downstream + 1)
                downstream = fitted[point]
    fitted = np.clip(np.rint(fitted), 1, 255).astype(np.uint8)
    del components, lake_levels, lake_near, ocean_near, anchor, parent, distance, samples

    tree = cKDTree(np.column_stack((rr, cc)))
    edges_from, edges_to, ways = _ordered_way_cells(
        lines, transform, water_pixels, rows, cols, tree)
    fitted = _direct_centerlines(fitted, edges_from, edges_to)
    del edges_from, edges_to

    link_a = []
    link_b = []
    for i, adjacent in enumerate(neighbors):
        for j, _ in adjacent:
            if j > i:
                link_a.append(i)
                link_b.append(j)
    if link_a:
        fitted = _limit_profile_steps(fitted, link_a + link_b, link_b + link_a)
    del neighbors, link_a, link_b

    water_rows, water_cols = np.divmod(water_pixels, cols)
    _, nearest = tree.query(np.column_stack((water_rows, water_cols)), workers=-1)
    nearest = np.asarray(nearest, dtype=np.int64)
    del tree, rr, cc, water_rows, water_cols
    center_count = len(pixels)
    surface[:] = fitted[nearest]
    for compact_cells, indices in ways:
        surface[compact_cells] = fitted[indices]

    water_lake_levels = np.asarray(data["water_lake_levels"], dtype=np.uint8)
    joining = water_lake_levels < 255
    surface[joining] = water_lake_levels[joining]

    np.minimum(surface, np.asarray(data["ceiling"], dtype=np.uint8), out=surface)
    floor = morphology(surface, water_pixels, rows, cols, "floor3")
    np.minimum(surface, floor, out=surface)
    way_cells, way_offsets = _pack_way_cells(ways)
    del ways, fitted
    _downstream_compact(surface, way_cells, way_offsets)
    np.minimum(surface, np.asarray(data["ceiling"], dtype=np.uint8), out=surface)

    opened = morphology(surface, water_pixels, rows, cols, "crest5")
    np.minimum(surface, opened, out=surface)
    _settle_surface(surface, water_pixels, rows, cols, nearest, center_count,
                    way_cells, way_offsets)
    floor = morphology(surface, water_pixels, rows, cols, "drop5")
    np.minimum(surface, floor, out=surface)
    _settle_surface(surface, water_pixels, rows, cols, nearest, center_count,
                    way_cells, way_offsets)
    return surface
