"""Vector Matrix reconstruction: conforming quad cells, connected regions and seam optimization."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math
from typing import Sequence

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components, dijkstra
from shapely import affinity
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import polygonize

from .whimsies import WhimsyPlacement


@dataclass
class MatrixResult:
    faces: list[Polygon]
    interior_lines: list[LineString]
    exterior_lines: list[LineString]
    whimsy_indices: list[int]
    metrics: dict


def _guide_segments(active, curves):
    segments = []
    for curve in curves:
        xy = np.asarray(curve, dtype=float)
        if xy.ndim != 2 or xy.shape[1] != 2 or len(xy) < 2 or not np.isfinite(xy).all():
            raise ValueError("A Matrix guide curve needs at least two finite x,y points in millimetres")
        clipped = LineString(xy).intersection(active)
        pieces = [seg for seg in shapely.get_parts(clipped)
                  if seg.geom_type == "LineString" and seg.length > 1e-8]
        if not pieces:
            raise ValueError("Each Matrix guide curve needs a segment inside the moving board area")
        segments.extend(pieces)
    return segments


def _steer_lattice(gx, gy, vx, vy, points, guides, pitch):
    """Locally turn the nearer of the two lattice directions along the guide."""
    tree = shapely.STRtree(guides)
    nearest = tree.geometries[tree.nearest(points)]
    distance_along = shapely.line_locate_point(nearest, points)
    projected = shapely.line_interpolate_point(nearest, distance_along)
    before = shapely.line_interpolate_point(nearest, np.maximum(0., distance_along - 0.15*pitch))
    after = shapely.line_interpolate_point(nearest, distance_along + 0.15*pitch)
    tangent = shapely.get_coordinates(after) - shapely.get_coordinates(before)
    horizontal = np.abs(tangent[:, 0]) >= np.abs(tangent[:, 1])
    offset = shapely.get_coordinates(projected) - shapely.get_coordinates(points)
    # A horizontal-family edge follows a slope by moving its vertices chiefly
    # in y. A vertical-family edge analogously moves chiefly in x.
    strength = np.where(horizontal[:, None], [0.12, 0.40], [0.40, 0.12])
    guided = pitch * strength * np.tanh(1.7 * offset / pitch)
    weight = 0.86 * np.exp(-(np.linalg.norm(offset, axis=1) / (1.45*pitch))**2)
    weight = weight.reshape(gx.shape)
    gx_target = gx + guided[:, 0].reshape(gx.shape)
    gy_target = gy + guided[:, 1].reshape(gy.shape)
    return vx + weight*(gx_target-vx), vy + weight*(gy_target-vy)


def _quad_cells(outline, active, fixed, pitch, rng, guides):
    corners = np.asarray(outline.minimum_rotated_rectangle.exterior.coords)
    sides = np.diff(corners, axis=0)
    axis = sides[np.linalg.norm(sides, axis=1).argmax()]
    angle = math.atan2(axis[1], axis[0]) % (math.pi / 2)
    local = affinity.rotate(active, -angle, origin=(0, 0), use_radians=True)
    x0, y0, x1, y1 = local.bounds
    x = np.arange(x0 - (1 + rng.random()) * pitch, x1 + 2 * pitch, pitch)
    y = np.arange(y0 - (1 + rng.random()) * pitch, y1 + 2 * pitch, pitch)
    gx, gy = np.meshgrid(x, y)

    # Deform a contiguous lattice towards the closest outline or fixed figure.
    # Limiting each displacement preserves the ordering of the grid vertices.
    points = shapely.points(gx.ravel(), gy.ravel())
    nearest = shapely.get_coordinates(shapely.shortest_line(points, local.boundary))[1::2]
    offset = nearest - np.column_stack((gx.ravel(), gy.ravel()))
    distance = np.linalg.norm(offset, axis=1)
    weight = 0.32 * np.exp(-distance / (2 * pitch)) / np.maximum(distance / pitch, 1)
    phase_x, phase_y = rng.uniform(0, 2 * math.pi, 2)
    vx = gx + (offset[:, 0] * weight).reshape(gx.shape) + 0.12 * pitch * np.sin(gy / (3 * pitch) + phase_x)
    vy = gy + (offset[:, 1] * weight).reshape(gy.shape) + 0.12 * pitch * np.sin(gx / (3 * pitch) + phase_y)
    if guides:
        local_guides = [affinity.rotate(g, -angle, origin=(0, 0), use_radians=True) for g in guides]
        vx, vy = _steer_lattice(gx, gy, vx, vy, points, local_guides, pitch)
    co, si = math.cos(angle), math.sin(angle)
    vertices = np.stack((co * vx - si * vy, si * vx + co * vy), axis=-1)
    linework = [LineString(row) for row in vertices]
    linework.extend(LineString(col) for col in vertices.transpose(1, 0, 2))
    linework.extend([outline.boundary, *(w.boundary for w in fixed)])
    # Node shared edges once; neighboring clipped polygons then use the same vertices.
    all_cells = polygonize(shapely.union_all(linework))
    return [orient(c, sign=1) for c in all_cells if active.covers(c.representative_point())]


def _cell_graph(cells):
    """Index each interior half-edge once, with its direction around the first cell."""
    pending = {}
    edges = []
    incident = [[] for _ in cells]
    neighbors = [set() for _ in cells]
    for cell, poly in enumerate(cells):
        for ring in (poly.exterior, *poly.interiors):
            coords = list(ring.coords)
            for a, b in zip(coords, coords[1:]):
                other = pending.pop((b, a), None)
                if other is None:
                    pending[a, b] = cell
                    continue
                eid = len(edges)
                direction = math.atan2(a[1] - b[1], a[0] - b[0])
                edges.append((other, cell, b, a, direction))
                incident[cell].append(eid)
                incident[other].append(eid)
                neighbors[cell].add(other)
                neighbors[other].add(cell)
    return edges, incident, neighbors, list(pending)


def _seed_regions(cells, neighbors, count, rng):
    """Seed every component, then grow contiguous pieces by mesh geodesic distance."""
    centers = np.asarray([p.representative_point().coords[0] for p in cells])
    links = [(i, j) for i, row in enumerate(neighbors) for j in sorted(row)]
    if links:
        source, target = np.asarray(links).T
        length = np.linalg.norm(centers[source] - centers[target], axis=1)
        graph = csr_matrix((length, (source, target)), shape=(len(cells), len(cells)))
    else:
        graph = csr_matrix((len(cells), len(cells)))
    components, connected = connected_components(graph, directed=False)
    if components > count:
        raise ValueError(f"Matrix needs at least {components} moving pieces to fill the separate board regions")
    seeds = [int(rng.choice(np.flatnonzero(connected == component))) for component in range(components)]
    dist = dijkstra(graph, indices=seeds, min_only=True)
    while len(seeds) < count:
        next_seed = int(np.argmax(dist))
        seeds.append(next_seed)
        dist = np.minimum(dist, dijkstra(graph, indices=next_seed))
    _, _, nearest = dijkstra(graph, indices=seeds, min_only=True, return_predecessors=True)
    seed_to_piece = {s: i for i, s in enumerate(seeds)}
    return np.array([seed_to_piece[int(s)] for s in nearest], dtype=np.int32)


def _tangent_span(seam, edges):
    """Largest angular coverage along a connected shared interface (radians)."""
    if not seam:
        return 0.
    endpoints = defaultdict(list)
    for eid in seam:
        _, _, a, b, _ = edges[eid]
        endpoints[a].append(eid)
        endpoints[b].append(eid)
    remaining = set(seam)
    best = 0.
    while remaining:
        stack = [remaining.pop()]
        angles = []
        while stack:
            eid = stack.pop()
            angles.append(seam[eid] % math.tau)
            for point in edges[eid][2:4]:
                for other in endpoints[point]:
                    if other in remaining:
                        remaining.remove(other)
                        stack.append(other)
        angles.sort()
        gaps = [angles[0] + math.tau - angles[-1]]
        gaps.extend(b - a for a, b in zip(angles, angles[1:]))
        best = max(best, math.tau - max(gaps))
    return best


def _owner_pair(edge, owners):
    a, b, _, _, theta = edge
    first, second = int(owners[a]), int(owners[b])
    if first == second:
        return None, 0.
    if first < second:
        return (first, second), theta
    return (second, first), theta + math.pi


def _connected_after_removal(cell, owner, neighbors, owners):
    adjacent = {i for i in neighbors[cell] if owners[i] == owner}
    if len(adjacent) < 2:
        return bool(adjacent)
    stack = [adjacent.pop()]
    seen = {cell, stack[0]}
    while adjacent and stack:
        for other in neighbors[stack.pop()]:
            if other not in seen and owners[other] == owner:
                seen.add(other)
                adjacent.discard(other)
                stack.append(other)
    return not adjacent


def _survives_cut(cells, ids, thickness):
    face = shapely.coverage_union_all([cells[i] for i in ids])
    if face.geom_type != "Polygon" or not face.is_valid:
        return False
    core = face.buffer(-0.5 * thickness)
    return core.geom_type == "Polygon" and not core.is_empty


def _optimize(cells, owners, edges, incident, neighbors, rng, pitch, thickness):
    areas = np.array([c.area for c in cells])
    piece_area = np.bincount(owners, weights=areas)
    members = [set() for _ in piece_area]
    for cell, piece in enumerate(owners):
        members[piece].add(cell)
    target = piece_area.mean()
    seams = defaultdict(dict)
    for eid, edge in enumerate(edges):
        pair, theta = _owner_pair(edge, owners)
        if pair:
            seams[pair][eid] = theta
    spans = {pair: _tangent_span(seam, edges) for pair, seam in seams.items()}
    initial_hooks = sum(value > math.pi for value in spans.values())
    exchanges = 0
    for _ in range(3):
        for cell in rng.permutation(len(cells)):
            donor = int(owners[cell])
            recipients = sorted({int(owners[n]) for n in neighbors[cell]} - {donor})
            if not recipients or areas[cell] < max(0.35 * pitch ** 2, 0.55 * thickness ** 2):
                continue
            if piece_area[donor] - areas[cell] < target * 0.55:
                continue
            if not _connected_after_removal(cell, donor, neighbors, owners):
                continue
            old = [(eid, _owner_pair(edges[eid], owners)[0]) for eid in incident[cell]]
            best_gain = 0.025
            winner = None
            for receiver in recipients:
                if piece_area[receiver] + areas[cell] > target * 1.65:
                    continue
                owners[cell] = receiver
                moved = [(eid, *_owner_pair(edges[eid], owners)) for eid in incident[cell]]
                owners[cell] = donor
                affected = {p for _, p in old if p} | {p for _, p, _ in moved if p}
                proposal = {p: seams.get(p, {}).copy() for p in affected}
                for eid, pair in old:
                    if pair:
                        del proposal[pair][eid]
                for eid, pair, theta in moved:
                    if pair:
                        proposal[pair][eid] = theta
                scores = {p: _tangent_span(s, edges) for p, s in proposal.items()}
                gain = sum(scores[p] - spans.get(p, 0.) for p in affected) / math.pi
                # Discourage thin tendrils, excessive perimeter and highly unequal sizes.
                gain -= 0.10 * sum(len(proposal[p]) - len(seams.get(p, {})) for p in affected)
                gain += 2.5 * areas[cell] * (piece_area[donor] - piece_area[receiver] - areas[cell]) / target**2
                if gain > best_gain:
                    winner = receiver, proposal, scores
                    best_gain = gain
            if winner:
                receiver, proposal, scores = winner
                if not (_survives_cut(cells, members[donor] - {cell}, thickness)
                        and _survives_cut(cells, members[receiver] | {cell}, thickness)):
                    continue
                owners[cell] = receiver
                members[donor].remove(cell)
                members[receiver].add(cell)
                piece_area[donor] -= areas[cell]
                piece_area[receiver] += areas[cell]
                seams.update(proposal)
                spans.update(scores)
                exchanges += 1
    return seams, {
        "mesh_cells": len(cells),
        "cell_exchanges": exchanges,
        "initial_interfaces_over_180_deg": initial_hooks,
        "interfaces_over_180_deg": sum(s > math.pi for s in spans.values()),
        "moving_interfaces": sum(bool(s) for s in seams.values()),
        "mesh_pitch_mm": round(pitch, 4),
    }


def generate_matrix(outline: Polygon, num_pieces: int, seed: int = 42,
                    whimsies: Sequence[WhimsyPlacement] = (), min_neck_mm: float = 3.,
                    kerf_mm: float = 0.15, guide_curves=()) -> MatrixResult:
    """Partition the outline, steering the lattice near optional physical-mm polylines."""
    if not isinstance(outline, Polygon) or outline.is_empty or not outline.is_valid or outline.area <= 0:
        raise ValueError("Matrix needs a valid polygonal board")
    if (not isinstance(num_pieces, (int, np.integer)) or num_pieces < 1
            or not all(math.isfinite(v) and v >= 0 for v in (min_neck_mm, kerf_mm))):
        raise ValueError("Matrix piece count must be positive and cutting dimensions nonnegative")
    fixed = [w.polygon for w in whimsies]
    for i, poly in enumerate(fixed):
        if not isinstance(poly, Polygon) or poly.is_empty or not poly.is_valid or not outline.contains_properly(poly):
            raise ValueError("Each Matrix fixed piece must be valid and strictly inside the board")
        if any(poly.intersects(prior) for prior in fixed[:i]):
            raise ValueError("Matrix fixed pieces must be separated")
    count = num_pieces - len(fixed)
    if count < 1:
        raise ValueError("Matrix needs at least one moving piece in addition to the fixed figures")
    active = outline.difference(shapely.union_all(fixed))
    curves = tuple(guide_curves) if guide_curves is not None else ()
    guides = _guide_segments(active, curves)
    rng = np.random.default_rng(seed)
    width = math.sqrt(active.area / count)
    pitch = min(0.55 * width, max(width / 4.5, 1.4 * (min_neck_mm + kerf_mm)))
    cells = _quad_cells(outline, active, fixed, pitch, rng, guides)
    if len(cells) < count:
        raise ValueError("Too few mesh cells for the requested number of pieces")
    edges, incident, neighbors, exposed_edges = _cell_graph(cells)
    thickness = min_neck_mm + kerf_mm
    best_bad = count + 1
    for _ in range(4):
        candidate = _seed_regions(cells, neighbors, count, rng)
        seed_groups = [[] for _ in range(count)]
        for i, piece in enumerate(candidate):
            seed_groups[piece].append(i)
        bad = sum(not _survives_cut(cells, group, thickness) for group in seed_groups)
        if bad < best_bad:
            owners, best_bad = candidate, bad
        if not bad:
            break
    seams, metrics = _optimize(cells, owners, edges, incident, neighbors, rng, pitch, thickness)
    metrics["guide_curve_count"] = len(curves)
    metrics["guide_length_inside_mm"] = round(sum(g.length for g in guides), 4)
    groups = [[] for _ in range(count)]
    for cell, owner in zip(cells, owners):
        groups[owner].append(cell)
    moving = [shapely.coverage_union_all(group) for group in groups]
    if any(face.geom_type != "Polygon" or not face.is_valid for face in moving):
        raise RuntimeError("Matrix ownership did not produce connected polygonal pieces")
    cores = shapely.buffer(moving, -0.5 * thickness, quad_segs=16)
    metrics["moving_pieces_with_split_or_empty_core"] = sum(
        core.is_empty or core.geom_type != "Polygon" for core in cores
    )
    interior = []
    for pair in sorted(seams):
        if seams[pair]:
            segments = [LineString(edges[eid][2:4]) for eid in sorted(seams[pair])]
            interior.extend(shapely.get_parts(shapely.line_merge(shapely.MultiLineString(segments))))
    # Preserve mesh junctions on curved rings so rounding the SVG cannot open a seam.
    exposed = [LineString(segment) for segment in exposed_edges]
    if fixed:
        rings = shapely.STRtree([outline.boundary, *(p.boundary for p in fixed)])
        boundary_ids = rings.nearest(shapely.line_interpolate_point(exposed, 0.5, normalized=True))
        fixed_edges = [e for e, boundary in zip(exposed, boundary_ids) if boundary != 0]
        interior.extend(shapely.get_parts(shapely.line_merge(shapely.MultiLineString(fixed_edges))))
        exposed = [e for e, boundary in zip(exposed, boundary_ids) if boundary == 0]
    exterior = list(shapely.get_parts(shapely.line_merge(shapely.MultiLineString(exposed))))
    return MatrixResult(moving + fixed, interior, exterior, list(range(count, num_pieces)), metrics)
