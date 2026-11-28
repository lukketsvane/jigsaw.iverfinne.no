"""A chessboard jigsaw, pocketed frame, backing and 32 flat chessmen in one cut layout.

Independent design inspired by the published storage concept at
https://n-e-r-v-o-u-s.com/blog/?p=9122 . All coordinates and distances are in mm.
"""

import argparse
import itertools
import json
import math
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import polygonize, unary_union

from pathnet.lizards import path_data

from .infinity import _seam

BACK_RANK = ("rook", "knight", "bishop", "queen", "king", "bishop", "knight", "rook")
LIGHT, DARK = "#e8d4a8", "#4d766d"
WHITE, BLACK = "#faf0d2", "#202e39"


def _token(kind, square):
    """Six original silhouettes in square-width units, all with a flat base."""
    foot = [
        (0.25, 0.27),
        (0.32, 0.33),
        (0.32, 0.39),
        (-0.32, 0.39),
        (-0.32, 0.33),
        (-0.25, 0.27),
    ]
    body = [
        (-0.21, 0.27),
        (-0.13, 0.18),
        (-0.085, 0.02),
        (-0.14, -0.01),
        (-0.14, -0.06),
    ]
    right = [(0.14, -0.06), (0.14, -0.01), (0.085, 0.02), (0.13, 0.18), (0.21, 0.27)]
    head = {
        "rook": [
            (-0.17, -0.08),
            (-0.25, -0.12),
            (-0.25, -0.35),
            (-0.15, -0.35),
            (-0.15, -0.23),
            (-0.055, -0.23),
            (-0.055, -0.35),
            (0.055, -0.35),
            (0.055, -0.23),
            (0.15, -0.23),
            (0.15, -0.35),
            (0.25, -0.35),
            (0.25, -0.12),
            (0.17, -0.08),
        ],
        "bishop": [
            (-0.19, -0.11),
            (-0.20, -0.21),
            (-0.14, -0.30),
            (0.013, -0.41),
            (0.08, -0.34),
            (-0.03, -0.19),
            (0.04, -0.19),
            (0.13, -0.29),
            (0.20, -0.20),
            (0.18, -0.10),
        ],
        "queen": [
            (-0.18, -0.09),
            (-0.27, -0.32),
            (-0.16, -0.29),
            (-0.10, -0.18),
            (-0.035, -0.38),
            (0.035, -0.38),
            (0.10, -0.18),
            (0.16, -0.29),
            (0.27, -0.32),
            (0.18, -0.09),
        ],
        "king": [
            (-0.17, -0.10),
            (-0.19, -0.18),
            (-0.10, -0.24),
            (-0.055, -0.24),
            (-0.055, -0.29),
            (-0.15, -0.29),
            (-0.15, -0.36),
            (-0.055, -0.36),
            (-0.055, -0.43),
            (0.055, -0.43),
            (0.055, -0.36),
            (0.15, -0.36),
            (0.15, -0.29),
            (0.055, -0.29),
            (0.055, -0.24),
            (0.10, -0.24),
            (0.19, -0.18),
            (0.17, -0.10),
        ],
        "knight": [
            (-0.20, -0.02),
            (-0.08, -0.13),
            (-0.16, -0.10),
            (-0.28, -0.12),
            (-0.30, -0.19),
            (-0.19, -0.29),
            (-0.09, -0.33),
            (-0.07, -0.42),
            (0.012, -0.35),
            (0.11, -0.38),
            (0.13, -0.30),
            (0.22, -0.18),
            (0.24, -0.02),
            (0.18, 0.10),
        ],
    }
    if kind == "pawn":
        neck = Polygon([*body, (-0.08, -0.15), (0.08, -0.15), *right, *foot])
        shape = neck.union(Point(0, -0.22).buffer(0.13, quad_segs=20))
    elif kind == "knight":
        shape = Polygon(
            [
                (-0.23, 0.27),
                (-0.18, 0.16),
                (-0.15, 0.07),
                *head[kind],
                (0.19, 0.20),
                (0.23, 0.27),
                *foot,
            ]
        )
    else:
        shape = Polygon([*body, *head[kind], *right, *foot])
    # A small tool radius removes needle-like tips and inside corners.
    shape = shape.buffer(-0.012).buffer(0.024).buffer(-0.012)
    if shape.geom_type != "Polygon" or shape.interiors:
        raise ValueError(f"Invalid chess silhouette {kind}")
    return affinity.scale(shape, square, square, origin=(0, 0))


def _header(width, height):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.4f}mm" '
        f'height="{height:.4f}mm" viewBox="0 0 {width:.4f} {height:.4f}">'
    )


def _path(identifier, geom, style=""):
    points = geom.exterior.coords if isinstance(geom, Polygon) else geom.coords
    return f'<path id="{identifier}" d="{path_data(points)}" {style}/>'


def _grid(board, pieces, seed):
    x, y, x2, y2 = board.bounds
    spacing = (x2 - x) / pieces
    nodes = np.stack(
        np.meshgrid(np.linspace(x, x2, pieces + 1), np.linspace(y, y2, pieces + 1)),
        axis=-1,
    )
    rng = np.random.default_rng(seed)
    nodes[1:-1, 1:-1] += rng.uniform(-0.12, 0.12, (pieces - 1, pieces - 1, 2)) * spacing
    lines = [LineString(board.exterior.coords)]
    for j in range(1, pieces):
        for i in range(pieces):
            lines.append(LineString(_seam(nodes[j, i], nodes[j, i + 1], rng)))
            lines.append(LineString(_seam(nodes[i, j], nodes[i + 1, j], rng)))
    return lines


def _translate(poly, pos):
    return affinity.translate(poly, *pos)


def _load_paths(svg):
    doc = ET.parse(svg).getroot()
    return {
        e.attrib["id"]: LineString(
            np.fromstring(e.attrib["d"][1:].replace(",", " "), sep=" ").reshape(-1, 2)
        )
        for e in doc.findall(".//{*}path")
    }


def _audit(stage, board, square, tokens, kerf, clearance, min_web, jigsaw_n):
    """Check the actual exported centerlines before estimating the removed material."""
    paths = _load_paths(stage / "cut.svg")
    edges = [v for k, v in paths.items() if k.startswith("board-")]
    faces = list(polygonize(unary_union(edges)))
    if (
        len(faces) != jigsaw_n**2
        or unary_union(faces).symmetric_difference(board).area > 0.06
    ):
        raise ValueError("Checkerboard cut does not partition the entire playable area")
    segments = set()
    for path in paths.values():
        xy = list(path.coords)
        for a, b in itertools.pairwise(xy):
            if a == b:
                continue
            key = tuple(sorted((a, b)))
            if key in segments:
                raise ValueError("Duplicate cut segment on the sheet")
            segments.add(key)
    outer = Polygon(paths["frame-outer"])
    backing = Polygon(paths["backing"])
    backing_shift = np.subtract(outer.bounds[:2], backing.bounds[:2])
    backing = affinity.translate(backing, *backing_shift)
    if outer.symmetric_difference(backing).area > 0.01:
        raise ValueError("Backing registration differs from frame")
    holes, spare = [], []
    gaps = []
    for token in tokens:
        tag = token["id"]
        pocket = Polygon(paths["pocket-" + tag]).buffer(kerf / 2)
        blank = Polygon(paths["token-" + tag]).buffer(-kerf / 2)
        spare.append(Polygon(paths["token-" + tag]))
        if (
            blank.is_empty
            or blank.geom_type != "Polygon"
            or blank.buffer(-min_web / 2).geom_type != "Polygon"
            or blank.buffer(-min_web / 2).is_empty
        ):
            raise ValueError(f"Token {tag} fails connected-width screen")
        dx, dy = np.subtract(token["storage_center_mm"], token["cut_center_mm"])
        disk = affinity.translate(blank, dx, dy)
        ejector = _translate(
            Polygon(paths["lift-" + tag]).buffer(kerf / 2), backing_shift
        )
        if not disk.contains(ejector):
            raise ValueError(f"Backing lift hole for {tag} misses its token")
        gap = pocket.boundary.distance(disk.boundary)
        if not pocket.contains(disk) or gap < clearance - 0.025:
            raise ValueError(
                f"Token {tag} cannot reach the requested clearance in its pocket ({gap:.3f} mm)"
            )
        holes.append(pocket)
        gaps.append(gap)
        play = affinity.translate(
            blank, *np.subtract(token["play_center_mm"], token["cut_center_mm"])
        )
        x, y = token["play_center_mm"]
        if not box(
            x - square / 2, y - square / 2, x + square / 2, y + square / 2
        ).contains(play):
            raise ValueError(f"Token {tag} overhangs its starting square")
    if any(a.intersects(b) for i, a in enumerate(spare) for b in spare[i + 1 :]):
        raise ValueError("Token blanks overlap on the production sheet")
    web = min(
        [h.distance(outer.exterior) - kerf / 2 for h in holes]
        + [h.distance(board) - kerf / 2 for h in holes]
        + [a.distance(b) for i, a in enumerate(holes) for b in holes[i + 1 :]]
    )
    if web < min_web:
        raise ValueError(f"Remaining frame web {web:.2f} mm is below {min_web:.2f} mm")
    squares = (
        ET.parse(stage / "artwork.svg").getroot().findall(".//{*}rect[@class='square']")
    )
    expected = {f"{file}{rank}" for file in "abcdefgh" for rank in range(1, 9)}
    if {r.attrib["id"] for r in squares} != expected or len(squares) != 64:
        raise ValueError("Printed checkerboard is incomplete")
    for r in squares:
        f, rank = r.attrib["id"]
        ix, iy = ord(f) - ord("a"), 8 - int(rank)
        target = [
            board.bounds[0] + ix * square,
            board.bounds[1] + iy * square,
            square,
            square,
        ]
        if not np.allclose(
            [float(r.get(k)) for k in ("x", "y", "width", "height")],
            target,
            atol=0.0001,
        ):
            raise ValueError("Printed square registration error")
    return {
        "board_pieces": len(faces),
        "printed_squares": 64,
        "token_count": len(tokens),
        "frame_pockets": len(holes),
        "verified_lift_holes": len(holes),
        "duplicate_cut_segments": 0,
        "min_radial_clearance_mm": round(min(gaps), 4),
        "min_remaining_frame_web_mm": round(web, 4),
        "board_coverage_error_mm2": unary_union(faces).symmetric_difference(board).area,
    }


def generate(
    out_dir,
    square_mm=28.0,
    pieces_per_side=7,
    seed=42,
    kerf_mm=0.15,
    clearance_mm=0.30,
    min_web_mm=2.0,
):
    s = square_mm
    if (
        not all(
            math.isfinite(v) and v > 0 for v in (s, kerf_mm, clearance_mm, min_web_mm)
        )
        or not isinstance(pieces_per_side, int)
        or pieces_per_side < 1
    ):
        raise ValueError(
            "Use positive finite dimensions and at least one jigsaw cell per side"
        )
    border = 1.1 * s
    size = 2 * border + 8 * s
    board = box(border, border, border + 8 * s, border + 8 * s)
    outer = box(0, 0, size, size)
    gutter = 0.6 * s
    blanks_x = size + gutter
    # The strip of 32 blanks sits above the backing, leaving all outlines clear.
    backing_y = 4 * s + gutter
    frame_cut = _grid(board, pieces_per_side, seed)
    cut = [
        _path("frame-outer", outer),
        *[_path(f"board-{i}", line) for i, line in enumerate(frame_cut)],
        _path("backing", _translate(outer, (blanks_x, backing_y))),
    ]
    shapes = {name: _token(name, s) for name in set(BACK_RANK) | {"pawn"}}
    squares = []
    for j in range(8):
        for i, file in enumerate("abcdefgh"):
            fill = LIGHT if (i + j) % 2 == 0 else DARK
            squares.append(
                f'<rect class="square" id="{file}{8 - j}" x="{border + i * s:.4f}" y="{border + j * s:.4f}" width="{s:.4f}" height="{s:.4f}" fill="{fill}"/>'
            )
    tokens, slot_art, token_art, stored, played = [], [], [], [], []
    for group, (side, pawns) in enumerate(
        [("black", False), ("black", True), ("white", True), ("white", False)]
    ):
        for i, kind in enumerate(["pawn"] * 8 if pawns else BACK_RANK):
            rank = (7 if pawns else 8) if side == "black" else (2 if pawns else 1)
            position = f"{'abcdefgh'[i]}{rank}"
            tag = f"{side}-{position}"
            along = border + (i + 0.5) * s
            slots = [
                (along, border / 2),
                (size - border / 2, along),
                (border / 2, along),
                (along, size - border / 2),
            ]
            pocket_pos = slots[group]
            cut_pos = (blanks_x + (i + 0.5) * s, (group + 0.5) * s)
            play_pos = (along, border + (8 - rank + 0.5) * s)
            shape = shapes[kind]
            slot_shape = _translate(
                shape.buffer(clearance_mm - kerf_mm / 2), pocket_pos
            )
            cut.append(_path("pocket-" + tag, slot_shape))
            release = Point(
                blanks_x + pocket_pos[0], backing_y + pocket_pos[1] + 0.28 * s
            ).buffer(0.08 * s)
            cut.append(_path("lift-" + tag, release))
            cut.append(
                _path("token-" + tag, _translate(shape.buffer(kerf_mm / 2), cut_pos))
            )
            ink = WHITE if side == "white" else BLACK
            style = f'fill="{ink}" stroke="#795e46" stroke-width=".3"'
            slot_art.append(_path("opening-" + tag, slot_shape, 'fill="#8a7158"'))
            slot_art.append(
                _path(
                    "lift-opening-" + tag,
                    _translate(release, (-blanks_x, -backing_y)),
                    'fill="#594636"',
                )
            )
            token_art.append(_path("print-" + tag, _translate(shape, cut_pos), style))
            stored.append(_path(tag, _translate(shape, pocket_pos), style))
            played.append(_path(tag, _translate(shape, play_pos), style))
            tokens.append(
                {
                    "id": tag,
                    "side": side,
                    "kind": kind,
                    "square": position,
                    "storage_center_mm": pocket_pos,
                    "cut_center_mm": cut_pos,
                    "play_center_mm": play_pos,
                }
            )
    sheet = _header(blanks_x + size, backing_y + size)
    frame_ink = _path("frame", outer, 'fill="#c59d72"')
    seams = [
        '<g fill="none" stroke="#4c4035" stroke-width=".35" opacity=".48">',
        *[_path(f"seam-{i}", edge) for i, edge in enumerate(frame_cut)],
        "</g>",
    ]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        stage = Path(tmp)
        files = {
            "cut.svg": [
                sheet,
                '<g id="cut" stroke="#c00000" stroke-width=".1" fill="none">',
                *cut,
                "</g>",
            ],
            "artwork.svg": [sheet, frame_ink, *squares, *slot_art, *token_art],
            "stored.svg": [
                _header(size, size),
                frame_ink,
                *squares,
                *seams,
                *slot_art,
                *stored,
            ],
            "playing.svg": [
                _header(size, size),
                frame_ink,
                *squares,
                *seams,
                *slot_art,
                *played,
            ],
        }
        for name, lines in files.items():
            (stage / name).write_text("\n".join([*lines, "</svg>"]), encoding="utf-8")
        audit = _audit(
            stage, board, s, tokens, kerf_mm, clearance_mm, min_web_mm, pieces_per_side
        )
        report = {
            "edition": "Chess storage jigsaw",
            "units": "mm",
            "board_width_mm": 8 * s,
            "frame_width_mm": size,
            "square_mm": s,
            "seed": seed,
            "kerf_mm": kerf_mm,
            "target_radial_clearance_mm": clearance_mm,
            "requested_min_web_mm": min_web_mm,
            "chessmen": tokens,
            "measurements": audit,
            "assembly": "Fix the frame alone to the matching backing. Leave the playing field and all 32 pockets free of adhesive. Insert the jigsaw board. Press a stored token up through its backing hole with a blunt peg, then place it on its assigned starting square.",
            "cut_layout": "Left panel is the jigsaw field and surrounding frame. Upper-right strip holds 32 finished chessmen. Lower-right square is the backing with 32 lift holes. The material removed from pocket interiors is scrap.",
        }
        (stage / "assembly.json").write_text(
            json.dumps(report, indent=2), encoding="utf-8"
        )
        for name in [*files, "assembly.json"]:
            (stage / name).replace(out / name)
    return report


def add_arguments(parser):
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--square-mm", type=float, default=28)
    parser.add_argument("--pieces-per-side", type=int, default=7)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--kerf-mm", type=float, default=0.15)
    parser.add_argument("--clearance-mm", type=float, default=0.30)
    parser.add_argument("--min-web-mm", type=float, default=2)


def main_from_args(args):
    r = generate(
        args.out,
        args.square_mm,
        args.pieces_per_side,
        args.seed,
        args.kerf_mm,
        args.clearance_mm,
        args.min_web_mm,
    )
    print(json.dumps(r["measurements"], indent=2))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    raise SystemExit(main_from_args(parser.parse_args()))
