"""Generate connected patches of the Smith–Myers–Kaplan–Goodman-Strauss Spectre.

The nine metatile labels, anchor correspondences and substitutions below are
adapted from Craig S. Kaplan's BSD-licensed spectreviz. See SPECTRE-LICENSE.txt
and https://cs.uwaterloo.ca/~csk/spectre/. The curved edge profile and print
palette here are independently generated. The straight variant is Tile(1,1).
"""

import argparse
import json
import math
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

from pathnet.lizards import path_data, verify_cut

RULES = {
    "Gamma": "Pi Delta - Theta Sigma Xi Phi Gamma",
    "Delta": "Xi Delta Xi Phi Sigma Pi Phi Gamma",
    "Theta": "Psi Delta Pi Phi Sigma Pi Phi Gamma",
    "Lambda": "Psi Delta Xi Phi Sigma Pi Phi Gamma",
    "Xi": "Psi Delta Pi Phi Sigma Psi Phi Gamma",
    "Pi": "Psi Delta Xi Phi Sigma Psi Phi Gamma",
    "Sigma": "Xi Delta Xi Phi Sigma Pi Lambda Gamma",
    "Phi": "Psi Delta Psi Phi Sigma Pi Phi Gamma",
    "Psi": "Psi Delta Psi Phi Sigma Psi Phi Gamma",
}
BASE_ANGLES = [0, -60, 30, 90, 0, 60, 150, 210, 120, 180, 180, 240, -30, -90]
PAPER = "https://doi.org/10.5070/C64264241"
PALETTE = (
    "#e8c69c",
    "#eeead1",
    "#83a49c",
    "#da986e",
    "#405c65",
    "#bc7461",
    "#acc6b1",
    "#dfd8ae",
    "#8bb6b2",
    "#c5a6a1",
)


def _rotation(degrees):
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def _point(transform, p):
    return transform[:2, :2] @ p + transform[:2, 2]


def _placements(root, depth, vertices):
    # Anchor points 3,5,7,11 track the supertile even after its outline
    # ceases to resemble the base. Each inflation reflects the entire cluster.
    anchors = vertices[[3, 5, 7, 11]]
    mirror = np.diag([-1.0, 1.0, 1.0])
    levels = []
    for _ in range(depth):
        placements = [np.eye(3)]
        angle = 0
        for turn, source, target in (
            (60, 3, 1),
            (0, 2, 0),
            (60, 3, 1),
            (60, 3, 1),
            (0, 2, 0),
            (60, 3, 1),
            (-120, 3, 3),
        ):
            angle += turn
            place = _rotation(angle)
            place[:2, 2] = _point(placements[-1], anchors[source]) - _point(
                place, anchors[target]
            )
            placements.append(place)
        placements = [mirror @ p for p in placements]
        anchors = np.array(
            [
                _point(placements[i], anchors[j])
                for i, j in ((6, 2), (5, 1), (3, 2), (0, 1))
            ]
        )
        levels.append(placements)

    second = _rotation(30)
    second[:2, 2] = vertices[8]

    def visit(label, level, transform):
        if level == 0:
            yield label, transform
            if label == "Gamma":
                yield "Mystic partner", transform @ second
            return
        for child, pos in zip(RULES[label].split(), levels[level - 1]):
            if child != "-":
                yield from visit(child, level - 1, transform @ pos)

    # Keep the same physical tile handedness across different output depths.
    yield from visit(root, depth, mirror if depth % 2 else np.eye(3))


def _build(depth, root, edge_mm, variant):
    angles = np.deg2rad(BASE_ANGLES)
    vectors = np.column_stack((np.cos(angles), np.sin(angles)))
    vertices = np.vstack(([0.0, 0.0], np.cumsum(vectors[:-1], axis=0)))
    t = np.linspace(0, 1, 17 if variant == "curved" else 2)
    # Every other equivalently directed unit edge bends inward. The next
    # bends outward. Reversing a shared edge produces exactly the same profile.
    bow = 0.18 * np.sin(np.pi * t) ** 2 if variant == "curved" else np.zeros_like(t)
    prototype = []
    for j, (x, y) in enumerate(vectors):
        edge = vertices[j] + t[:, None] * [x, y] + (-1) ** j * bow[:, None] * [-y, x]
        edge[[0, -1]] = vertices[[j, (j + 1) % 14]]
        prototype.append(edge)
    prototype = np.array(prototype)
    tile_placements = list(_placements(root, depth, vertices))
    cuts, edge_lookup, incident = [], {}, defaultdict(list)
    pieces, records = [], []
    for tile_id, (label, transform) in enumerate(tile_placements):
        oriented = np.linalg.det(transform[:2, :2])
        if not np.isclose(oriented, 1):
            raise ValueError("A substitution changed the handedness of a single tile")
        edges = (prototype @ transform[:2, :2].T + transform[:2, 2]) * edge_mm
        refs, parts = [], []
        for edge in edges:
            ticks = np.rint(edge * 10000).astype(np.int64)
            start, end = map(tuple, ticks[[0, -1]])
            reverse = start > end
            key = (end, start) if reverse else (start, end)
            canonical = ticks[::-1] if reverse else ticks
            if key not in edge_lookup:
                edge_lookup[key] = len(cuts)
                cuts.append(canonical)
            cut = edge_lookup[key]
            if np.max(np.abs(cuts[cut] - canonical)) > 1:
                raise ValueError(
                    "Substitution places incompatible profiles on a shared edge"
                )
            points = cuts[cut][::-1] if reverse else cuts[cut]
            parts.append(points[:-1])
            refs.append(-(cut + 1) if reverse else cut + 1)
            incident[cut].append(tile_id)
        pieces.append({"outline": (np.concatenate(parts) / 10000).tolist()})
        records.append(
            {
                "id": tile_id,
                "label": label,
                "signed_cut_ids": refs,
                "transform": transform[:2].round(12).tolist(),
            }
        )
    cuts = [points / 10000 for points in cuts]
    polygons = [Polygon(p["outline"]) for p in pieces]
    if any(not p.is_valid for p in polygons):
        raise ValueError("A substitution produced an invalid tile")
    patch = unary_union(polygons)
    overlap = sum(p.area for p in polygons) - patch.area
    if patch.geom_type != "Polygon" or patch.interiors or abs(overlap) > 0.0001:
        raise ValueError("Substitution failed the patch coverage check")
    neighbors = [set() for _ in pieces]
    for cut, owners in incident.items():
        if len(owners) == 2:
            a, b = owners
            neighbors[a].add(b)
            neighbors[b].add(a)
        elif len(owners) != 1:
            raise ValueError(f"Cut {cut} has {len(owners)} incident tiles")
    for rec, near in zip(records, neighbors):
        rec["neighbors"] = sorted(near)
    return pieces, records, cuts, patch, incident, max(0.0, overlap)


def generate(out_dir, depth=2, root="Delta", edge_mm=7.0, variant="curved", seed=42):
    """Write 1:1 cut and print files with tile-to-tile adjacency and analytic checks."""
    if root not in RULES or variant not in ("curved", "polygon"):
        raise ValueError("Unknown Spectre metatile or edge variant")
    if (
        not isinstance(depth, int)
        or depth < 0
        or not math.isfinite(edge_mm)
        or edge_mm <= 0
    ):
        raise ValueError("Use nonnegative integer depth and a positive edge length")
    pieces, records, cuts, patch, incident, overlap = _build(
        depth, root, edge_mm, variant
    )
    x, y, x2, y2 = patch.bounds
    header = (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{x2 - x:.4f}mm" height="{y2 - y:.4f}mm" '
        f'viewBox="{x:.4f} {y:.4f} {x2 - x:.4f} {y2 - y:.4f}">'
    )
    rng = np.random.default_rng(seed)
    palette = rng.permutation(PALETTE)
    # Both markings live in the unit tile frame. Moving a congruent tile
    # therefore moves its whole printed motif along with it.
    designs = rng.permutation(len(pieces)) % 2
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        stage = Path(tmp)
        cut_sheet = [
            header,
            '<g id="cut" fill="none" stroke="#c00000" stroke-width=".1">',
        ]
        cut_sheet.extend(
            f'<path id="c{i + 1}" d="{path_data(p)}"/>' for i, p in enumerate(cuts)
        )
        (stage / "cut.svg").write_text(
            "\n".join([*cut_sheet, "</g></svg>"]), encoding="utf-8"
        )
        n = verify_cut(stage / "cut.svg", pieces)
        print_defs = []
        pitch = 0.88 * edge_mm
        strokes = [
            '<circle cx=".5" cy=".5" r=".35"/><circle cx=".5" cy=".5" r=".21"/>',
            '<path d="M-.5,.5 Q-.25,-.02 0,.5 T.5,.5 T1,.5 T1.5,.5"/>',
        ]
        for design, mark in enumerate(strokes):
            ink = "#fff7db" if palette[design] == "#405c65" else "#334f52"
            print_defs.append(
                f'<pattern id="design-{design}" patternUnits="userSpaceOnUse" width="{pitch}" height="{pitch}" viewBox="0 0 1 1">'
                f'<rect width="1" height="1" fill="{palette[design]}"/>'
                f'<g fill="none" stroke="{ink}" stroke-width=".045">{mark}</g></pattern>'
            )
        print_sheet = []
        for p, r, design in zip(pieces, records, designs):
            (a, b, tx), (c, d, ty) = r["transform"]
            transform = f"matrix({a} {c} {b} {d} {tx * edge_mm} {ty * edge_mm})"
            print_defs.append(
                f'<pattern id="ink-{r["id"]}" href="#design-{design}" xlink:href="#design-{design}" patternTransform="{transform}"/>'
            )
            r["print_design"] = ("orbits", "ribbons")[design]
            print_sheet.append(
                f'<path id="tile-{r["id"]}" fill="url(#ink-{r["id"]})" d="{path_data(p["outline"], True)}"/>'
            )
        (stage / "artwork.svg").write_text(
            "\n".join(
                [header, "<defs>", *print_defs, "</defs>", *print_sheet, "</svg>"]
            ),
            encoding="utf-8",
        )
        report = {
            "family": "Spectre" if variant == "curved" else "Tile(1,1)",
            "source": PAPER,
            "rules_source": "https://cs.uwaterloo.ca/~csk/spectre/spectreviz.tar.gz",
            "root": root,
            "depth": depth,
            "edge_mm": edge_mm,
            "art_seed": seed,
            "print_treatment": "Two original motifs registered to the front of each tile. Leave the reverse unprinted for a plain-wood patch.",
            "pieces": n,
            "cut_paths": len(cuts),
            "shared_edges": sum(len(s) == 2 for s in incident.values()),
            "outer_edges": sum(len(s) == 1 for s in incident.values()),
            "overlap_mm2": overlap,
            "hole_count": len(patch.interiors),
            "area_mm2": patch.area,
            "verified_svg_pieces": n,
            "tile_chiralities": 1,
            "tiles": records,
            "edge_convention": "Signed 1-based cut.svg path IDs, in boundary order. Negative traverses the path backwards. Transform is in unit-edge coordinates before scaling by edge_mm.",
        }
        (stage / "assembly.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        for name in ("cut.svg", "artwork.svg", "assembly.json"):
            (stage / name).replace(out / name)
    return report


def add_arguments(parser):
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--root", choices=tuple(RULES), default="Delta")
    parser.add_argument("--edge-mm", type=float, default=7)
    parser.add_argument("--variant", choices=("curved", "polygon"), default="curved")
    parser.add_argument("--seed", type=int, default=42, help="Print palette seed")


def main_from_args(args):
    r = generate(args.out, args.depth, args.root, args.edge_mm, args.variant, args.seed)
    print(f"{r['pieces']} verified pieces and {r['shared_edges']} shared seams")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    raise SystemExit(main_from_args(parser.parse_args()))
