"""A jigsaw of connected round subpuzzles. Each bridge has its own shared profile.

Concept reference: https://n-e-r-v-o-u-s.com/shop/product.php?code=388
"""

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import shapely
from shapely.geometry import Polygon

from .infinity import _seam
from .regions import solve_regions
from .postprocess import KERFS, export_puzzle
from .styles import CUT_FAMILIES


DIRECTIONS = ((1, 0), (0, 1), (-1, 1), (-1, 0), (0, -1), (1, -1))
PALETTE = ("#cd795e", "#518d88", "#e1ae51", "#787aaf", "#8dba81", "#ca91ae", "#6c9ac1")


@dataclass
class Cluster:
    board: Polygon
    groups: list
    centers: list
    connectors: list


def _hex_positions(count):
    # Include the center, then consecutive neighbors along expanding hex rings.
    coords = [(0, 0)]
    ring = 1
    while len(coords) < count:
        q, r = ring, 0
        for dq, dr in (*DIRECTIONS[2:], *DIRECTIONS[:2]):
            for _ in range(ring):
                coords.append((q, r))
                if len(coords) == count:
                    return coords
                q, r = q + dq, r + dr
        ring += 1
    return coords


def _circle_point(center, radius, angle):
    return center + radius * np.array([math.cos(angle), math.sin(angle)])


def _sample_profile(path):
    """63 interior samples measured at equal fractions of arclength in chord coordinates."""
    chord = path[-1] - path[0]
    chord /= np.linalg.norm(chord)
    local = (path - path[0]) @ np.array([chord, [-chord[1], chord[0]]]).T
    length = np.r_[0, np.linalg.norm(np.diff(path, axis=0), axis=1).cumsum()]
    t = np.linspace(0, length[-1], 65)
    return np.stack([np.interp(t, length, local[:, axis]) for axis in (0, 1)], axis=1)


def _variants(profile):
    """Both traversal orders and both sides of the sheet."""
    reverse = profile[-1] - profile[::-1]
    for p in (profile, reverse):
        yield p
        yield p * [1, -1]


def _keyed_connector(a, b, rng, prior, kerf):
    for _ in range(180):
        candidate = _seam(a, b, rng).round(3)
        profile = _sample_profile(candidate)
        mismatch = min((float(np.linalg.norm(profile - alternate, axis=1).max())
                        for other in prior for alternate in _variants(other)), default=math.inf)
        if mismatch > max(0.35, 2 * kerf):
            prior.append(profile)
            return candidate, mismatch
    raise ValueError("The connector spacing and kerf leave too few distinguishable profiles")


def _make_cluster(count, radius, seed, kerf):
    if not isinstance(count, int) or count < 2 or not math.isfinite(radius) or radius <= 0:
        raise ValueError("Choose at least two subpuzzles and a positive, finite radius in millimetres")
    rng = np.random.default_rng(seed)
    coords = _hex_positions(count)
    ids = {coord: i for i, coord in enumerate(coords)}
    spacing = radius * 1.82
    centers = [spacing * np.array([q+r/2, r*math.sqrt(3)/2]) for q, r in coords]
    alpha = math.acos(spacing / (2 * radius))
    contacts = [[] for _ in coords]
    prior, connectors = [], []
    for i, (q, r) in enumerate(coords):
        for side, (dq, dr) in enumerate(DIRECTIONS):
            j = ids.get((q+dq, r+dr))
            if j is None or j <= i:
                continue
            theta = side * math.pi/3
            a = _circle_point(centers[i], radius, theta - alpha)
            b = _circle_point(centers[i], radius, theta + alpha)
            path, mismatch = _keyed_connector(a, b, rng, prior, kerf)
            contacts[i].append((theta, path))
            contacts[j].append(((theta+math.pi) % math.tau, path[::-1]))
            connectors.append({
                "groups": [i, j], "profile_mm": path.tolist(),
                "nearest_prior_profile_error_mm": None if math.isinf(mismatch) else round(mismatch, 4),
            })
    groups = []
    for center, contact in zip(centers, contacts):
        ordered = sorted(contact, key=lambda pair: pair[0])
        points = []
        for k, (theta, path) in enumerate(ordered):
            points.extend(path)
            after = theta + alpha
            before = ordered[(k+1) % len(ordered)][0] - alpha
            if before < after:
                before += math.tau
            segments = max(1, math.ceil((before-after)/0.07))
            for angle in np.linspace(after, before, segments+1)[1:-1]:
                points.append(_circle_point(center, radius, angle).round(3))
        poly = Polygon(points)
        if not poly.is_valid:
            raise ValueError("A connector crossed the exterior of its subpuzzle")
        groups.append(poly)
    board = shapely.coverage_union_all(groups)
    if board.geom_type != "Polygon" or not board.is_valid:
        raise ValueError("The round subpuzzles did not form one connected board")
    return Cluster(board, groups, centers, connectors)


def _artwork(cluster):
    x0, y0, x1, y1 = cluster.board.bounds
    scale = 1600 / max(x1-x0, y1-y0)
    w, h = max(1, round((x1-x0)*scale)), max(1, round((y1-y0)*scale))
    image = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(image)
    for i, poly in enumerate(cluster.groups):
        ring = [((x-x0)*w/(x1-x0), (y-y0)*h/(y1-y0)) for x, y in poly.exterior.coords]
        draw.polygon(ring, fill=PALETTE[i % len(PALETTE)])
    return np.asarray(image)


def generate_nested(out, *, seed=42, groups=7, pieces_per_group=4, radius_mm=40.,
                    styles=("matrix",), min_neck_mm=3., kerf_mm=.15):
    styles = (styles,) if isinstance(styles, str) else styles
    if pieces_per_group < 2 or not styles or any(s not in CUT_FAMILIES for s in styles):
        raise ValueError("Each subpuzzle requires at least two pieces and a supported cut style")
    if not all(math.isfinite(v) and v >= 0 for v in (min_neck_mm, kerf_mm)):
        raise ValueError("Kerf and neck width must be finite, nonnegative dimensions")
    cluster = _make_cluster(groups, radius_mm, seed, kerf_mm)
    x0, y0, x1, y1 = cluster.board.bounds
    specs = [
        {"points": ((np.asarray(poly.exterior.coords)[:-1] - [x0, y0]) / [x1-x0, y1-y0]).tolist(),
         "style": styles[i % len(styles)], "pieces": pieces_per_group}
        for i, poly in enumerate(cluster.groups)
    ]
    # Keep the parent connectors fixed while choosing subdivisions. A thin sliver
    # where a child seam meets a socket can otherwise splinter at nominal kerf.
    score, cut = math.inf, None
    for attempt in range(5):
        cut_seed = seed + 104729 * attempt
        try:
            candidate = solve_regions(cluster.board, specs, groups*pieces_per_group, cut_seed,
                                      min_neck_mm=min_neck_mm, kerf_mm=kerf_mm)
        except ValueError as error:
            if str(error).startswith("Region network produced") and (attempt < 4 or cut is not None):
                continue
            raise
        bad = 0
        for diameter in set((*KERFS, min_neck_mm, min_neck_mm + kerf_mm)):
            cores = shapely.buffer(candidate.faces, -diameter / 2, quad_segs=16)
            bad += sum(c.is_empty or c.geom_type != "Polygon" for c in cores)
        if bad < score:
            score, cut, selected_seed = bad, candidate, cut_seed
        if bad == 0:
            break
    members = [[] for _ in cluster.groups]
    for i, face in enumerate(cut.faces):
        owners = [j for j, p in enumerate(cluster.groups) if p.covers(face.representative_point())]
        if len(owners) != 1:
            raise ValueError("Ambiguous subpuzzle membership after cut export")
        members[owners[0]].append(i)
    group_info = []
    for j, member_ids in enumerate(members):
        joined = shapely.union_all([cut.faces[i] for i in member_ids])
        error = joined.symmetric_difference(cluster.groups[j]).area
        if len(member_ids) != pieces_per_group or error > .05:
            raise ValueError(f"Subpuzzle {j} failed membership verification ({error:.3f} mm²)")
        group_info.append({"group": j, "piece_ids": member_ids, "style": specs[j]["style"],
                           "color": PALETTE[j % len(PALETTE)], "coverage_error_mm2": round(error, 6)})
    # The reference mapping and connector coordinates use the exported board origin.
    for connector in cluster.connectors:
        connector["profile_mm"] = (np.asarray(connector["profile_mm"]) - [x0,y0]).round(3).tolist()
    meta = {"engine": "nested", "seed": seed, "subdivision_seed": selected_seed,
            "radius_mm": radius_mm, "groups": group_info,
            "connectors": cluster.connectors, "pieces_per_group": pieces_per_group}
    result = export_puzzle(out, cut.faces, cut.interior_lines, cut.exterior_lines,
                           expected_outline=cluster.board, min_neck_mm=min_neck_mm, kerf_mm=kerf_mm,
                           artwork_image=_artwork(cluster), metadata=meta)
    if result.audit["pieces"] != groups * pieces_per_group or result.audit["duplicate_segments"]:
        raise ValueError("The nested SVG did not recover the original piece count and cut paths")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate a set of colored, connected round mini-puzzles.")
    parser.add_argument("--out", type=Path, default=Path("runs/nested"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--groups", type=int, default=7)
    parser.add_argument("--pieces-per-group", type=int, default=4)
    parser.add_argument("--radius-mm", type=float, default=40.)
    parser.add_argument("--styles", nargs="+", choices=sorted(CUT_FAMILIES), default=["matrix"])
    parser.add_argument("--min-neck-mm", type=float, default=3.)
    parser.add_argument("--kerf-mm", type=float, default=.15)
    args = parser.parse_args(argv)
    result = generate_nested(**vars(args))
    print(json.dumps({k: result.audit[k] for k in ("pieces", "duplicate_segments", "coverage_error_mm2",
                                                    "all_hard_gates_passed")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
