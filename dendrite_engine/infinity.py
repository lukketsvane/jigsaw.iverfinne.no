"""Borderless jigsaws on a torus, Klein bottle, projective plane or icosahedral sphere.

Run python -m dendrite_engine.infinity --help. Concept references:
https://n-e-r-v-o-u-s.com/blog/?p=7613 and https://n-e-r-v-o-u-s.com/blog/?p=8293.
"""

from __future__ import annotations

import argparse
import json
import math
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicSpline
from shapely.geometry import Polygon
from shapely.ops import unary_union

from pathnet.lizards import path_data, verify_cut

from .infinity_print import print_artwork


@dataclass
class Cell:
    corners: np.ndarray
    keys: list
    chart: int


def _rectangular(cols, rows, scale, topology):
    cells, flips = [], set()
    flip_x = topology in ("klein", "projective")
    flip_y = topology == "projective"
    for j in range(rows):
        for i in range(cols):
            x = i + 1
            right_row = rows - j - 1 if flip_x and x == cols else j
            bottom_col = cols - i - 1 if flip_y and j == rows - 1 else i
            keys = [
                ("h", i, j),
                ("v", x % cols, right_row),
                ("h", bottom_col, (j + 1) % rows),
                ("v", i, j),
            ]
            if flip_x and i == 0:
                flips.add(keys[-1])
            if flip_y and j == 0:
                flips.add(keys[0])
            corners = (
                np.array([[i, j], [x, j], [x, j + 1], [i, j + 1]], dtype=float) * scale
            )
            cells.append(Cell(corners, keys, 0))
    return cells, flips


def _icosahedron(frequency, scale):
    """Unfold a north cap, interleaved equatorial belt and south cap into one net."""
    h = math.sqrt(3) / 2
    side = frequency * scale
    pole_n, pole_s = 10, 11
    xyz = []
    for offset, z in ((0, 1 / math.sqrt(5)), (0.5, -1 / math.sqrt(5))):
        for i in range(5):
            lon = 2 * math.pi * (i + offset) / 5
            radius = math.sqrt(1 - z * z)
            xyz.append([radius * math.cos(lon), radius * math.sin(lon), z])
    xyz.extend([[0, 0, 1], [0, 0, -1]])
    cells, charts = [], []
    for i in range(5):
        u, v = i, (i + 1) % 5
        l, m = 5 + i, 5 + v
        top, lo = (i, 0), (i + 0.5, h)
        top2, lo2 = (i + 1, 0), (i + 1.5, h)
        for ids, coords in (
            ((u, pole_n, v), (top, (i + 0.5, -h), top2)),
            ((u, v, l), (top, top2, lo)),
            ((v, m, l), (top2, lo2, lo)),
            ((l, m, pole_s), (lo, lo2, (i + 1, 2 * h))),
        ):
            points = np.array(coords) * side
            chart = len(charts)
            charts.append((ids, points))
            grid = {}
            for a in range(frequency + 1):
                for b in range(frequency - a + 1):
                    weights = (frequency - a - b, a, b)
                    # This integer identity is shared across charts at edges
                    # and vertices even though the net may draw two copies.
                    key = tuple(sorted((v, w) for v, w in zip(ids, weights) if w))
                    grid[a, b] = (np.array(weights) @ points / frequency, key)
            for a in range(frequency):
                for b in range(frequency - a):
                    triangles = [[(a, b), (a + 1, b), (a, b + 1)]]
                    if a + b + 2 <= frequency:
                        triangles.append([(a + 1, b), (a + 1, b + 1), (a, b + 1)])
                    for tri in triangles:
                        verts, keys = zip(*(grid[t] for t in tri))
                        edges = [
                            tuple(sorted((k, keys[(j + 1) % 3])))
                            for j, k in enumerate(keys)
                        ]
                        cells.append(Cell(np.asarray(verts), edges, chart))
    return cells, charts, np.asarray(xyz)


def _isometry(a, b, c, d, flip):
    # Return a matrix taking column coordinates a,b to c,d respectively.
    t = (b - a) / np.linalg.norm(b - a)
    u = (d - c) / np.linalg.norm(d - c)
    src = np.column_stack((t, [-t[1], t[0]]))
    dst = np.column_stack((u, [-u[1], u[0]]))
    if flip:
        dst[:, 1] *= -1
    rotation = dst @ src.T
    return np.column_stack((rotation, c - rotation @ a))


def _seam(a, b, rng):
    # A smooth rounded bulb narrows below its head. It stays away from both
    # ends so neighbouring sides of an equilateral piece cannot intersect.
    knots = np.array(
        [
            [0, 0],
            [0.22, 0],
            [0.39, 0.008],
            [0.44, 0.04],
            [0.421, 0.10],
            [0.448, 0.152],
            [0.515, 0.17],
            [0.57, 0.14],
            [0.58, 0.093],
            [0.553, 0.042],
            [0.59, 0.008],
            [0.75, 0],
            [1, 0],
        ]
    )
    knots[1:-1, 0] += rng.uniform(-0.033, 0.033) * np.sin(np.pi * knots[1:-1, 0])
    knots[:, 1] *= rng.choice([-1, 1]) * rng.uniform(0.77, 1.06)
    knots[1:-1, 1] += rng.uniform(-0.018, 0.018) * np.sin(2 * np.pi * knots[1:-1, 0])
    arc = np.r_[0, np.cumsum(np.linalg.norm(np.diff(knots, axis=0), axis=1))]
    profile = CubicSpline(arc, knots, bc_type=((1, [1, 0]), (1, [1, 0])))(
        np.linspace(0, arc[-1], 53)
    )
    chord = b - a
    path = a + profile @ np.array([chord, [-chord[1], chord[0]]])
    path[[0, -1]] = (a, b)
    return path


def _topological_vertices(cells, gluings):
    # Identify the endpoints across each join. This checks the topology of
    # the exported pairing itself, rather than assuming the requested surface.
    parent = {(i, j): (i, j) for i, c in enumerate(cells) for j in range(len(c.keys))}

    def root(k):
        while k != parent[k]:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for pair in gluings:
        (i, e), (j, f) = pair["sides"]
        a = [(i, e), (i, (e + 1) % len(cells[i].keys))]
        b = [(j, f), (j, (f + 1) % len(cells[j].keys))]
        if not pair["flip"]:
            b.reverse()
        for p, q in zip(a, b):
            parent[root(q)] = root(p)
    return len({root(k) for k in parent})


def _assemble(cells, flips, rng):
    incidences = defaultdict(list)
    profiles = [[None] * len(c.keys) for c in cells]
    for i, c in enumerate(cells):
        for e, key in enumerate(c.keys):
            incidences[key].append((i, e))
    physical, gluings = [], []
    max_error = 0.0
    for key, sides in incidences.items():
        if len(sides) != 2:
            raise ValueError("An abstract seam requires two incident pieces")
        (i, e), (j, f) = sides
        a, b = cells[i].corners[[e, (e + 1) % len(cells[i].keys)]]
        c, d = cells[j].corners[[f, (f + 1) % len(cells[j].keys)]]
        flip = key in flips
        matrix = _isometry(a, b, *((c, d) if flip else (d, c)), flip)
        planar = np.allclose(matrix, [[1, 0, 0], [0, 1, 0]], atol=1e-8)
        source = _seam(a, b, rng)
        transported = source @ matrix[:, :2].T + matrix[:, 2]
        target = transported if flip else transported[::-1].copy()
        target[[0, -1]] = (c, d)
        first, second = np.round(source, 4), np.round(target, 4)
        if planar:
            # Use identical rounded vertices on both uses of a physical cut.
            second = first[::-1].copy()
        profiles[i][e], profiles[j][f] = first, second
        left = len(physical)
        physical.append(first)
        right = left
        if not planar:
            right = len(physical)
            physical.append(second)
        error = np.linalg.norm(
            first @ matrix[:, :2].T + matrix[:, 2] - (second if flip else second[::-1]),
            axis=1,
        ).max()
        max_error = max(max_error, float(error))
        gluings.append(
            {
                "sides": sides,
                "cut_ids": [left, right],
                "flip": flip,
                "same_cut": bool(planar),
                "map_first_to_second": matrix.round(10).tolist(),
            }
        )
    pieces = [
        {
            "id": i,
            "chart": c.chart,
            "outline": np.vstack([ln[:-1] for ln in prof]).tolist(),
        }
        for i, (c, prof) in enumerate(zip(cells, profiles))
    ]
    polygons = [Polygon(p["outline"]) for p in pieces]
    if any(not p.is_valid for p in polygons) or max_error > 0.0002:
        raise ValueError("Invalid piece or matching seam after SVG quantization")
    assembled = unary_union(polygons)
    if (
        assembled.geom_type != "Polygon"
        or sum(p.area for p in polygons) - assembled.area > 0.001
    ):
        raise ValueError("Physical pieces must make one non-overlapping net")
    return pieces, physical, gluings, max_error, assembled.bounds


def _generate(
    out_dir,
    topology="torus",
    seed=42,
    cell_mm=28.0,
    cols=6,
    rows=4,
    frequency=2,
    artwork=None,
    pixels_per_mm=2.5,
    land_mask=None,
):
    """Export common cuts, registered print(s), explicit mates and measured geometry checks."""
    if topology not in ("torus", "klein", "projective", "sphere"):
        raise ValueError("Choose torus, klein, projective or sphere")
    if (
        not math.isfinite(cell_mm)
        or cell_mm <= 0
        or not math.isfinite(pixels_per_mm)
        or pixels_per_mm <= 0
    ):
        raise ValueError("cell_mm and pixels_per_mm must be positive finite values")
    charts, xyz = [], None
    orientation = None
    if land_mask is not None and topology != "sphere":
        raise ValueError("A land mask applies to the spherical map edition.")
    if topology == "sphere":
        if frequency < 1:
            raise ValueError("Sphere subdivision frequency must be a positive integer")
        cells, charts, xyz = _icosahedron(frequency, cell_mm)
        if land_mask is not None:
            from .infinity_print import orient_map_vertices

            xyz, orientation = orient_map_vertices(xyz, land_mask, seed)
        flips = set()
    else:
        if cols < 2 or rows < 2:
            raise ValueError("Use at least two rows and columns")
        cells, flips = _rectangular(cols, rows, cell_mm, topology)
    pieces, cuts, gluings, residual, bounds = _assemble(
        cells, flips, np.random.default_rng(seed)
    )
    vertices = _topological_vertices(cells, gluings)
    chi = vertices - len(gluings) + len(pieces)
    if chi != {"torus": 0, "klein": 0, "projective": 1, "sphere": 2}[topology]:
        raise ValueError("Incorrect Euler characteristic for the assembled surface")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    x, y, mx, my = bounds
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{mx - x:.4f}mm" height="{my - y:.4f}mm" '
        f'viewBox="{x:.4f} {y:.4f} {mx - x:.4f} {my - y:.4f}">'
    )
    cut = [svg, '<g id="cut" fill="none" stroke="#c00000" stroke-width=".1">']
    cut.extend(
        f'<path id="cut-{i}" d="{path_data(line)}"/>' for i, line in enumerate(cuts)
    )
    (out / "cut.svg").write_text("\n".join([*cut, "</g></svg>"]), encoding="utf-8")
    verified = verify_cut(out / "cut.svg", pieces)
    print_artwork(
        out,
        svg,
        pieces,
        bounds,
        topology,
        charts,
        xyz,
        (cols * cell_mm, rows * cell_mm),
        artwork,
        seed,
        pixels_per_mm,
    )
    result = {
        "topology": topology,
        "map_orientation": orientation,
        "seed": seed,
        "units": "mm",
        "gluing_convention": "sides are [piece ID, zero-based edge in ring order]. The 2x3 matrix maps the first side into the second side's print coordinates; flip requires turning the piece over.",
        "euler_characteristic": chi,
        "topological_vertices": vertices,
        "surface_edges": len(gluings),
        "cut_paths": len(cuts),
        "verified_svg_pieces": verified,
        "max_mate_error_mm": round(residual, 8),
        "bounds_mm": bounds,
        "pieces": pieces,
        "joins": gluings,
        "print_files": ["artwork.svg"]
        + (["artwork-back.svg"] if topology in ("klein", "projective") else []),
    }
    if topology in ("klein", "projective"):
        result["duplex_front_to_back"] = [[-1, 0, x + mx], [0, 1, 0]]
    if topology == "sphere":
        result["sphere_charts"] = [
            {"vertices": ids, "net_mm": coords.tolist()} for ids, coords in charts
        ]
        result["sphere_unit_vertices"] = xyz.tolist()
    (out / "assembly.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def generate(
    out_dir,
    topology="torus",
    seed=42,
    cell_mm=28.0,
    cols=6,
    rows=4,
    frequency=2,
    artwork=None,
    pixels_per_mm=2.5,
    land_mask=None,
):
    """Verify all exports in a staging directory before replacing a previous run."""
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as scratch:
        stage = Path(scratch)
        result = _generate(
            stage,
            topology,
            seed,
            cell_mm,
            cols,
            rows,
            frequency,
            artwork,
            pixels_per_mm,
            land_mask,
        )
        lock = output / ".infinity-publishing"
        lock.mkdir()
        try:
            files = ["cut.svg", *result["print_files"], "assembly.json"]
            old_manifest = output / "assembly.json"
            old_prints = (
                json.loads(old_manifest.read_text(encoding="utf-8")).get(
                    "print_files", []
                )
                if old_manifest.is_file()
                else []
            )
            stale_back = (
                "artwork-back.svg" in old_prints and "artwork-back.svg" not in files
            )
            moved, published = [], []
            try:
                for name in files + (["artwork-back.svg"] if stale_back else []):
                    prior = output / name
                    if prior.is_file():
                        prior.replace(stage / (name + ".old"))
                        moved.append(name)
                    if name in files:
                        (stage / name).replace(prior)
                        published.append(name)
            except OSError:
                for name in published:
                    (output / name).unlink()
                for name in moved:
                    (stage / (name + ".old")).replace(output / name)
                raise
        finally:
            lock.rmdir()
    return result


def add_arguments(parser):
    parser.add_argument(
        "--topology",
        choices=["torus", "klein", "projective", "sphere"],
        default="torus",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--cell-mm", type=float, default=28)
    parser.add_argument("--cols", type=int, default=6)
    parser.add_argument("--rows", type=int, default=4)
    parser.add_argument(
        "--frequency",
        type=int,
        default=2,
        help="Sphere subdivision: 20 × frequency² pieces",
    )
    parser.add_argument(
        "--artwork",
        type=Path,
        help="Equirectangular spherical texture or periodic planar art",
    )
    parser.add_argument("--pixels-per-mm", type=float, default=2.5)
    parser.add_argument(
        "--land-mask",
        type=Path,
        help="Optional equirectangular land mask (white land) to orient a spherical map",
    )


def main_from_args(args):
    r = generate(
        args.out,
        args.topology,
        args.seed,
        args.cell_mm,
        args.cols,
        args.rows,
        args.frequency,
        args.artwork,
        args.pixels_per_mm,
        args.land_mask,
    )
    print(
        f"{r['verified_svg_pieces']} pieces, {r['surface_edges']} paired seams, "
        f"maximum matching error {r['max_mate_error_mm']:.6f} mm"
    )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    raise SystemExit(main_from_args(parser.parse_args()))
