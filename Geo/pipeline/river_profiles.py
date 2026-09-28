"""Fit downstream-monotone water levels on rasterized HydroRIVERS lines."""
from collections import deque
import heapq

import numpy as np
from rasterio.features import rasterize
from scipy.ndimage import maximum_filter, minimum_filter
from scipy.spatial import cKDTree
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components

def _isotonic_downhill(values, outlet):
    """Least-squares nonincreasing profile from headwater to a fixed outlet."""
    # Pool adjacent violators on the reversed (downstream-to-upstream) sequence.
    blocks = []
    for value in np.r_[float(outlet), values[::-1]]:
        blocks.append([float(value), 1, 1])  # sum, weight, number of samples
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            tail = blocks.pop()
            blocks[-1][0] += tail[0]
            blocks[-1][1] += tail[1]
            blocks[-1][2] += tail[2]
    fitted = np.concatenate([np.full(count, total / weight) for total, weight, count in blocks])
    return np.maximum(fitted[1:][::-1], outlet)


def _limit_profile_steps(levels, sources, targets, max_step=1):
    """Lower upstream levels until every directed arc descends by at most max_step."""
    incoming = [[] for _ in levels]
    for source, target in zip(sources, targets):
        if source != target:
            incoming[target].append(source)
    queue = list(range(len(levels)))
    head = 0
    while head < len(queue):
        target = queue[head]
        head += 1
        ceiling = int(levels[target]) + max_step
        for source in incoming[target]:
            if levels[source] > ceiling:
                levels[source] = ceiling
                queue.append(source)
    return levels


def _lower_narrow_crests(surface, river):
    """Cut narrow water ridges across the full channel width, never raising Y."""
    water_floor = minimum_filter(np.where(river, surface, 255), size=5)
    opened = maximum_filter(np.where(river, water_floor, 0), size=5)
    np.minimum(surface, opened, out=surface, where=river)


def _lower_downstream_rises(surface, way_cells):
    """Honor HydroRIVERS source-to-NEXT_DOWN order, including shared junctions."""
    changed = False
    while True:
        pass_changed = False
        for row, col, _ in way_cells:
            levels = surface[row, col]
            downhill = np.minimum.accumulate(levels)
            if np.any(downhill < levels):
                np.minimum.at(surface, (row, col), downhill)
                pass_changed = changed = True
        if not pass_changed:
            return changed


def _flatten_cross_channel(surface, water_y, water_x, nearest, center_count):
    """Carve each centerline cross-section to its lowest water column."""
    levels = surface[water_y, water_x]
    cross_levels = np.full(center_count, 255, dtype=np.uint8)
    np.minimum.at(cross_levels, nearest, levels)
    lowered = cross_levels[nearest]
    changed = np.any(lowered < levels)
    if changed:
        surface[water_y, water_x] = lowered
    return bool(changed)


def _carve_drop_transition(surface, river):
    """Widen isolated low water to a full channel step without raising water."""
    floor = minimum_filter(np.where(river, surface, 255), size=5)
    np.minimum(surface, floor, out=surface, where=river)


def _settle_surface(surface, river, water_y, water_x, nearest, center_count, way_cells):
    cols = surface.shape[1]
    while True:
        flattened = _flatten_cross_channel(surface, water_y, water_x, nearest, center_count)
        queue = deque(map(int, water_y * cols + water_x))
        while queue:
            index = queue.popleft()
            row, col = divmod(index, cols)
            ceiling = int(surface[row, col]) + 1
            for nr in range(max(0, row - 1), min(surface.shape[0], row + 2)):
                for nc in range(max(0, col - 1), min(cols, col + 2)):
                    if river[nr, nc] and surface[nr, nc] > ceiling:
                        surface[nr, nc] = ceiling
                        queue.append(nr * cols + nc)
        if not _lower_downstream_rises(surface, way_cells) and not flattened:
            return


def fitted_river_surface(lines, transform, height, river, lake, land, fallback, shore_ceiling=None):
    """Fit tributaries into shared junction levels on the generalized river lines."""
    rows, cols = height.shape
    if not lines or not river.any():
        return fallback
    way_points = [line.xy for line in lines]
    center = rasterize(((line, 1) for line in lines), out_shape=height.shape,
                       transform=transform, all_touched=True, dtype=np.uint8).astype(bool) & river
    pixels = np.flatnonzero(center)
    if not len(pixels):
        return fallback
    rr, cc = np.divmod(pixels, cols)
    positions = {int(pixel): index for index, pixel in enumerate(pixels)}
    neighbors = [[] for _ in pixels]
    # Eight-connected centerline graph; Euclidean cost discourages diagonal shortcuts.
    for i, (r, c) in enumerate(zip(rr, cc)):
        for dr, dc in ((-1, -1), (-1, 0), (-1, 1), (0, -1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols:
                j = positions.get(int(nr * cols + nc))
                if j is not None:
                    cost = 1.41421356 if dr and dc else 1.0
                    neighbors[i].append((j, cost))
                    neighbors[j].append((i, cost))

    # Find connected components before choosing an outlet. Lakes, then ocean,
    # then map edge win; an inland truncated reach drains to its lowest endpoint.
    component = np.full(len(pixels), -1, np.int32)
    components = []
    for start in range(len(pixels)):
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

    lake_near = minimum_filter(np.where(lake, height, 255).astype(np.uint8), size=3) < 255
    lake_levels = minimum_filter(np.where(lake, height, 255).astype(np.uint8), size=3)
    ocean_near = ~minimum_filter(land, size=3)
    parent = np.full(len(pixels), -1, np.int32)
    distance = np.full(len(pixels), np.inf)
    anchor = np.full(len(pixels), -1, np.int16)
    samples = minimum_filter(height, size=5)[rr, cc].astype(np.float64)
    for group in components:
        shore = group[lake_near[rr[group], cc[group]]]
        if len(shore):
            seeds = shore
            anchor[shore] = lake_levels[rr[shore], cc[shore]]
        else:
            coast = group[ocean_near[rr[group], cc[group]]]
            edge = group[(rr[group] == 0) | (cc[group] == 0) |
                         (rr[group] == rows - 1) | (cc[group] == cols - 1)]
            candidates = coast if len(coast) else edge if len(edge) else group
            # One outlet per non-lake component avoids split profiles at the map edge.
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

    # Fit the longest trunk first; shorter tributaries share its confluence level.
    # Assign the chosen path atomically so every centerline pixel has one water Y.
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
            # A junction cannot jump several blocks between adjacent cells.
            downstream = outlet
            for point in reversed(path):
                fitted[point] = min(fitted[point], downstream + 1)
                downstream = fitted[point]
    fitted = np.clip(np.rint(fitted), 1, 255).astype(np.uint8)
    tree = cKDTree(np.column_stack((rr, cc)))
    inverse = ~transform
    edges_from, edges_to = [], []
    way_cells = []
    for x, y in way_points:
        col, row = inverse * (np.asarray(x), np.asarray(y))
        cells = []
        for r0, c0, r1, c1 in zip(row[:-1], col[:-1], row[1:], col[1:]):
            steps = max(1, int(np.ceil(max(abs(r1 - r0), abs(c1 - c0)))))
            for step in range(steps):
                r = int(r0 + (r1 - r0) * step / steps)
                c = int(c0 + (c1 - c0) * step / steps)
                if 0 <= r < rows and 0 <= c < cols and river[r, c] and (not cells or cells[-1] != (r, c)):
                    cells.append((r, c))
        r, c = int(row[-1]), int(col[-1])
        if 0 <= r < rows and 0 <= c < cols and river[r, c]:
            cells.append((r, c))
        if len(cells) < 2:
            continue
        row, col = np.array(cells, dtype=np.int32).T
        _, indices = tree.query(np.column_stack((row, col)))
        indices = np.asarray(indices, dtype=np.int32)
        way_cells.append((row, col, indices))
        indices = indices[np.r_[True, np.diff(indices) != 0]]
        if len(indices) < 2:
            continue
        # HydroRIVERS coordinates follow source -> NEXT_DOWN. Heights inferred
        # from a rough DEM must not reverse that measured network direction.
        edges_from.extend(indices[:-1])
        edges_to.extend(indices[1:])
    if edges_from:
        arcs = csr_matrix((np.ones(len(edges_from), dtype=np.uint8),
                           (edges_from, edges_to)), shape=(len(pixels), len(pixels)))
        count, labels = connected_components(arcs, directed=True, connection='strong')
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
        fitted = _limit_profile_steps(fitted, edges_from, edges_to)

    # A rasterized centerline can also have side-by-side cells omitted by the
    # OSM trace. Avoid high ledges there without flattening the whole reach.
    link_a, link_b = [], []
    for i, adjacent in enumerate(neighbors):
        for j, _ in adjacent:
            if j > i:
                link_a.append(i)
                link_b.append(j)
    if link_a:
        fitted = _limit_profile_steps(fitted, link_a + link_b, link_b + link_a)

    # Nearest centerline transfers one longitudinal level across each river width.
    water_y, water_x = np.nonzero(river)
    _, nearest = tree.query(np.column_stack((water_y, water_x)), workers=-1)
    surface = fallback.copy()
    surface[water_y, water_x] = fitted[nearest]
    # Keep centerline sample cells in lockstep with the graph used for fitting.
    for row, col, indices in way_cells:
        surface[row, col] = fitted[indices]
    # Lake-mouth targets can be lowered if surrounding ground is below lake Y.
    joining = river & lake_near
    surface[joining] = lake_levels[joining]
    if shore_ceiling is not None:
        # Isotonic fitting can raise a low edge sample again. Lower the water
        # without moving the neighboring dry bank, then carry any new cuts
        # upstream along the oriented source reaches.
        np.minimum(surface, shore_ceiling, out=surface, where=river)
        # The nearest-centerline transfer and byte rounding can leave isolated
        # one-block high columns in a rough reach. Lower them to touching water
        # before carrying cuts along the directed reaches.
        water_floor = minimum_filter(np.where(river, surface, 255), size=3)
        np.minimum(surface, water_floor, out=surface, where=river)
        _lower_downstream_rises(surface, way_cells)
        np.minimum(surface, shore_ceiling, out=surface, where=river)
    # The directed centerline pass can restore narrow crests across the
    # channel. Cut them without raising either water or adjacent dry terrain.
    _lower_narrow_crests(surface, river)
    # Cross-sections and directed reaches can lower one another; settle both.
    _settle_surface(surface, river, water_y, water_x, nearest, len(pixels), way_cells)
    # A single low cell at a bend creates an early one-block pit followed by
    # a rise on the opposite bank. Extend the drop across the channel once.
    _carve_drop_transition(surface, river)
    _settle_surface(surface, river, water_y, water_x, nearest, len(pixels), way_cells)
    return surface
