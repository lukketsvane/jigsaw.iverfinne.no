"""Coral cut by seeded search over tangent circular arcs on a shared seam graph.

The studio describes its stochastic arc search at https://n-e-r-v-o-u-s.com/blog/?p=9636.
The arc construction, objective and clearance tests here are independent implementations.
Coordinates are in millimetres. Faces are reconstructed from the same paths sent to the laser.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

import numpy as np
import shapely
from shapely.geometry import LineString, MultiLineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import polygonize

from .rod_growth import _initial_voronoi_cells
from .whimsies import WhimsyPlacement


@dataclass
class CoralResult:
    faces: list[Polygon]
    interior_lines: list[LineString]
    exterior_lines: list[LineString]
    whimsy_indices: list[int]
    metrics: dict


def _arc_lobe(span: float, sweep: float, radius_share: float) -> np.ndarray:
    """Three circular arcs with common tangents, ending on the chord with zero slope.

    For sweep > pi/2 the tangent locally travels backwards along the chord,
    producing an undercut. The signed turns sum to zero. Their chord projections
    sum to 2 (outer_radius + middle_radius) sin(sweep).
    """
    radii_sum = span / (2.0 * math.sin(sweep))
    shoulder, crown = radii_sum * radius_share, radii_sum * (1.0 - radius_share)
    points = [np.zeros((1, 2))]
    heading = 0.0
    for turn, radius in ((sweep, shoulder), (-2 * sweep, crown), (sweep, shoulder)):
        # Keep the chord approximation within 0.02 mm of each true circular arc.
        step = 2 * math.acos(max(0.0, 1.0 - 0.02 / radius))
        headings = heading + np.linspace(0, turn, max(3, math.ceil(abs(turn) / step)) + 1)[1:]
        offsets = np.column_stack((np.sin(headings) - math.sin(heading),
                                   math.cos(heading) - np.cos(headings)))
        points.append(points[-1][-1] + math.copysign(radius, turn) * offsets)
        heading += turn
    curve = np.concatenate(points)
    curve[-1] = (span, 0.0)
    return curve


def _arc_candidates(line: LineString, gap: float, rng: np.random.Generator):
    start, end = np.asarray(line.coords)[[0, -1]]
    length = float(np.linalg.norm(end - start))
    if length < max(3 * gap, 1.0) or line.length > length + 0.003:
        return
    tangent = (end - start) / length
    basis = np.array([tangent, [-tangent[1], tangent[0]]])
    for attempt in range(16):
        collar = max(gap, length * rng.uniform(0.12, 0.25))
        span = length - 2 * collar
        max_lobes = min(3, int(span / max(2.7 * gap, 2.0)))
        if max_lobes < 1:
            continue
        lobes = rng.integers(1, max_lobes + 1)
        widths = rng.uniform(0.85, 1.15, size=lobes)
        widths *= span / widths.sum()
        sign = 1 if attempt % 2 else -1
        parts = []
        offset = collar
        for width in widths:
            sweep = math.radians(rng.uniform(93, 122) if attempt < 12 else rng.uniform(48, 88))
            radius_share = rng.uniform(0.32, 0.58)
            # Once the shoulders turn beyond vertical, their closest opposing
            # horizontal extrema are at x = radius and x = width - radius.
            # This is the throat of the lobe itself, distinct from clearance to
            # the other cuts on the board.
            shoulder_radius = width * radius_share / (2 * math.sin(sweep))
            if sweep > math.pi / 2 and width - 2 * shoulder_radius < gap:
                break
            curve = _arc_lobe(width, sweep, radius_share)
            curve[:, 0] += offset
            curve[:, 1] *= sign
            parts.append(curve if not parts else curve[1:])
            sign *= -1
            offset += width
        if len(parts) != lobes:
            continue
        local = np.concatenate(parts)
        body = LineString(np.round(start + local @ basis, 3))
        if not body.is_simple:
            continue
        # Extra cut length encourages sinuous edges. Backwards travel rewards
        # undercuts that resist sliding along the local normal.
        backtrack = np.maximum(-np.diff(local[:, 0]), 0).sum()
        score = (body.length - span + 3.0 * backtrack) / length
        yield score, body, 3 * int(lobes), float(backtrack)


def _seam_graph(outline, num_pieces, whimsies, rng):
    """Build junction-to-junction paths, including stationary outlines and fixed figures."""
    fixed = [w.polygon for w in whimsies]
    fixed_boundary = shapely.union_all([p.boundary for p in fixed])
    for _ in range(12):
        cells, _ = _initial_voronoi_cells(outline, num_pieces, whimsies, "coral", rng)
        noded = shapely.union_all([c.boundary for c in cells], grid_size=1e-8)
        chains, exterior, mutable = [], [], []
        for path in shapely.get_parts(shapely.line_merge(noded)):
            sample = path.interpolate(0.5, normalized=True)
            is_exterior = outline.boundary.distance(sample) < 1e-6
            is_fixed = bool(fixed) and fixed_boundary.distance(sample) < 1e-6
            # Snap *after* noding so both incident paths share their junctions
            # even when the SVG exporter rounds coordinates to three decimals.
            rounded = shapely.remove_repeated_points(LineString(np.round(path.coords, 3)))
            if rounded.length < 0.001:
                continue
            chains.append(rounded)
            exterior.append(is_exterior)
            mutable.append(not (is_fixed or is_exterior))
        faces = _faces(chains, outline)
        if (len(faces) == num_pieces and all(not p.interiors for p in faces)
                and outline.symmetric_difference(shapely.union_all(faces)).area <= outline.length * 0.001):
            return chains, exterior, mutable
    raise ValueError("Could not place connected Coral cells around these figures at this piece count")


def _faces(paths, outline):
    return [orient(face, 1.0) for face in polygonize(paths)
            if face.area > 1e-8 and outline.covers(face.representative_point())]


def generate_coral(
    outline: Polygon,
    num_pieces: int,
    seed: int = 42,
    whimsies: Sequence[WhimsyPlacement] = (),
    min_neck_mm: float = 3.0,
    kerf_mm: float = 0.15,
) -> CoralResult:
    """Generate a shared, bounded vector cut. The requested total includes fixed figures."""
    if outline.is_empty or not outline.is_valid or outline.area <= 0:
        raise ValueError("Coral requires a valid board with positive area")
    if num_pieces < 2 or num_pieces - len(whimsies) < 2:
        raise ValueError("Coral requires room for at least two generated pieces")
    if (not math.isfinite(min_neck_mm) or not math.isfinite(kerf_mm)
            or min_neck_mm < 0 or kerf_mm < 0):
        raise ValueError("Coral neck width and kerf must be finite and nonnegative")
    for i, whimsy in enumerate(whimsies):
        p = whimsy.polygon
        if p.is_empty or not p.is_valid or not outline.contains_properly(p):
            raise ValueError("Place each Coral whimsy strictly inside the board")
        if any(p.intersects(other.polygon) for other in whimsies[:i]):
            raise ValueError("Coral whimsy outlines must be disjoint")

    rng = np.random.default_rng(seed)
    gap = min_neck_mm + kerf_mm
    paths, exterior, mutable = _seam_graph(outline, num_pieces, whimsies, rng)
    editable = np.flatnonzero(mutable)
    bounds = np.array([path.bounds for path in paths])
    stats = {"editable_seams": len(editable), "arc_seams": 0, "circular_arcs": 0,
             "tested_arc_candidates": 0, "undercut_seams": 0}
    for index in rng.permutation(editable):
        path = paths[index]
        # Only nearby paths can collide. Use current paths, as an earlier seam
        # may have curved away from its original Voronoi edge.
        x0, y0, x1, y1 = bounds[index]
        margin = path.length + gap
        nearby = ((bounds[:, 0] < x1 + margin) & (bounds[:, 2] > x0 - margin)
                  & (bounds[:, 1] < y1 + margin) & (bounds[:, 3] > y0 - margin))
        nearby[index] = False
        obstacles = MultiLineString([paths[j] for j in np.flatnonzero(nearby)])
        best_score, best = 0.0, None
        for score, body, arcs, backtrack in _arc_candidates(path, gap, rng):
            stats["tested_arc_candidates"] += 1
            if score <= best_score or body.distance(obstacles) <= max(gap, 0.002):
                continue
            candidate = LineString([path.coords[0], *body.coords, path.coords[-1]])
            if candidate.is_simple:
                best_score, best = score, (candidate, arcs, backtrack)
        if best is not None:
            paths[index], arcs, backtrack = best
            bounds[index] = paths[index].bounds
            stats["arc_seams"] += 1
            stats["circular_arcs"] += arcs
            stats["undercut_seams"] += int(backtrack > 0.001)

    faces = _faces(paths, outline)
    if len(faces) != num_pieces or any(not p.is_valid or p.interiors for p in faces):
        raise RuntimeError("Coral arc search failed to preserve the starting subdivision")
    faces.sort(key=lambda p: (p.centroid.y, p.centroid.x))
    wh_indices = []
    for w in whimsies:
        center = w.polygon.representative_point()
        index = next((i for i, p in enumerate(faces) if p.covers(center)), None)
        if index is None or faces[index].symmetric_difference(w.polygon).area > 0.001 * w.polygon.length:
            raise RuntimeError("Coral arc search changed a fixed figure outline")
        wh_indices.append(index)
    return CoralResult(faces, [p for p, ext in zip(paths, exterior) if not ext],
                       [p for p, ext in zip(paths, exterior) if ext], wh_indices, stats)
