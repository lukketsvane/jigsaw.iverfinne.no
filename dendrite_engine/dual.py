"""Two assemblies from one cut. Numbered rectangle pieces make a second disc.

Example: python -m dendrite_engine.dual --seed 12 --out runs/two-assemblies
Recheck the exported cut and guide: python -m dendrite_engine.dual --verify runs/two-assemblies
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import shapely
from shapely.affinity import affine_transform
from shapely.geometry import LineString, Point, box
from shapely.ops import polygonize, polylabel, substring

from .coral import _arc_candidates, generate_coral
from .postprocess import SVG_NS, _line_to_d, _parse_svg_paths, export_puzzle
from .whimsies import WhimsyPlacement, make_engrave_text_paths


def _disc(diameter):
    radius = diameter / 2
    return Point(radius, radius).buffer(radius, quad_segs=90)


def _place_sectors(pieces, width, height, rng):
    """Assign pieces to separate cells with rigid quarter turns at SVG precision."""
    best = None
    for cols in range(1, len(pieces) + 1):
        rows = math.ceil(len(pieces) / cols)
        cell_w, cell_h = width / cols, height / rows
        options = []
        for piece in pieces:
            x0, y0, x1, y1 = piece.bounds
            w, h = x1 - x0, y1 - y0
            options.append(
                max(min(cell_w - w, cell_h - h), min(cell_w - h, cell_h - w))
            )
        gap = min(options)
        if best is None or gap > best[0]:
            best = (gap, cols, rows)
    gap, cols, rows = best
    if gap < 8:
        raise ValueError(
            "Increase the rectangle size or reduce the disc diameter to leave space between pieces."
        )
    cell_w, cell_h = width / cols, height / rows
    slots = rng.permutation(cols * rows)
    placements, marks = [], []
    # Signed integer rotation coefficients make quarter turns exact on the cut-file grid.
    rotations = ((1, 0, 0, 1), (0, -1, 1, 0), (-1, 0, 0, -1), (0, 1, -1, 0))
    for number, (piece, slot) in enumerate(zip(pieces, slots), start=1):
        row, col = divmod(int(slot), cols)
        for turn in rng.permutation(4):
            a, b, d, e = rotations[turn]
            turned = affine_transform(piece, [a, b, d, e, 0, 0])
            x0, y0, x1, y1 = turned.bounds
            if x1 - x0 <= cell_w - 8 and y1 - y0 <= cell_h - 8:
                break
        tx = round((col + 0.5) * cell_w - (x0 + x1) / 2, 3)
        ty = round((row + 0.5) * cell_h - (y0 + y1) / 2, 3)
        forward = [a, b, d, e, tx, ty]
        inverse = [a, d, b, e, round(-a * tx - d * ty, 3), round(-b * tx - e * ty, 3)]
        placed = affine_transform(piece, forward)
        target = polylabel(piece, tolerance=0.2)
        clearance = piece.boundary.distance(target)
        text_size = min(5.0, clearance / len(str(number)))
        paths = make_engrave_text_paths(str(number), (target.x, target.y), text_size)
        ink = [list(affine_transform(LineString(p), forward).coords) for p in paths]
        pin = affine_transform(target, forward)
        placements.append(
            WhimsyPlacement(
                name=f"disc-{number}",
                center_mm=(pin.x, pin.y),
                size_mm=max(x1 - x0, y1 - y0),
                angle_deg=90 * int(turn),
                polygon=placed,
                engrave_paths=ink,
                category="alternate assembly",
                source="disc",
            )
        )
        marks.append(
            {
                "number": number,
                "rectangle_pin_mm": [pin.x, pin.y],
                "disc_pin_mm": [target.x, target.y],
                "rectangle_to_disc": inverse,
                "disc_number_paths": paths,
            }
        )
    return cols, rows, placements, marks


def _rectangle_cut(width, height, rows, cols, marked, total, rng):
    """Node a single common-line graph around the inset pieces, then curve its free edges."""
    board = box(0, 0, width, height)
    cells = [
        box(
            c * width / cols,
            r * height / rows,
            (c + 1) * width / cols,
            (r + 1) * height / rows,
        )
        for r in range(rows)
        for c in range(cols)
    ]
    contents = [
        next((m for m in marked if cell.contains(m.polygon)), None) for cell in cells
    ]
    ordinary = np.array([2 if m else 1 for m in contents])
    spare = total - len(marked) - ordinary.sum()
    if spare < 0:
        raise ValueError(
            f"This arrangement needs at least {total - spare} pieces to surround its numbered set."
        )
    for slot in np.resize(rng.permutation(len(cells)), spare):
        ordinary[slot] += 1
    original = [cell.boundary for cell in cells]
    for cell, item, count in zip(cells, contents, ordinary):
        if item is None:
            left, top, right, bottom = cell.bounds
            for i in range(1, count):
                x = left + i * (right - left) / count
                original.append(LineString([(x, top), (x, bottom)]))
            continue
        original.append(item.polygon.boundary)
        # Distinct rays approach the same interior pin, and stop at the FIRST
        # boundary crossing. This works for a deeply concave numbered piece.
        offset = rng.uniform(0, 1)
        for i in range(count):
            candidates = []
            for delta in (0, -0.12, 0.12, -0.24, 0.24):
                start = cell.exterior.interpolate(
                    ((i + offset + delta) / count) % 1, normalized=True
                )
                ray = LineString([start.coords[0], item.center_mm])
                visible = next(
                    part
                    for part in shapely.get_parts(ray.difference(item.polygon))
                    if part.distance(start) < 1e-7
                )
                # Choose a spoke meeting the inset transversely. Nearly tangent
                # approaches can leave a fragile splinter along a curved tip.
                collar = min(4.0, visible.length / 2)
                approach = substring(visible, 0, visible.length - collar)
                clearance = approach.distance(item.polygon)
                candidates.append((clearance, visible))
            original.append(max(candidates, key=lambda pair: pair[0])[1])

    network = shapely.union_all(original, grid_size=0.001)
    chains = list(shapely.get_parts(shapely.line_merge(network)))
    fixed = shapely.union_all([m.polygon.boundary for m in marked])
    perimeter = []
    modified = 0
    for i, seam in enumerate(chains):
        mid = seam.interpolate(0.5, normalized=True)
        outer = mid.distance(board.boundary) < 0.002
        perimeter.append(outer)
        if outer or mid.distance(fixed) < 0.002:
            continue
        neighbours = shapely.union_all([s for j, s in enumerate(chains) if i != j])
        for _, body, _, backtrack in _arc_candidates(seam, 3.15, rng):
            if backtrack > 0 and body.distance(neighbours) > 3.15:
                curved = LineString([seam.coords[0], *body.coords, seam.coords[-1]])
                if curved.is_simple:
                    chains[i] = curved
                    modified += 1
                    break
    faces = sorted(
        polygonize(shapely.union_all(chains)),
        key=lambda p: (p.centroid.y, p.centroid.x),
    )
    if len(faces) != total or any(not p.is_valid or p.interiors for p in faces):
        raise ValueError(
            "Primary subdivision did not preserve the requested connected pieces."
        )
    indices = [
        next(i for i, f in enumerate(faces) if f.contains(Point(m.center_mm)))
        for m in marked
    ]
    return (
        faces,
        [s for s, ext in zip(chains, perimeter) if not ext],
        [s for s, ext in zip(chains, perimeter) if ext],
        indices,
        modified,
    )


def _write_assembly(path, diameter, secondary, marks):
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": f"{diameter}mm",
            "height": f"{diameter}mm",
            "viewBox": f"0 0 {diameter} {diameter}",
        },
    )
    ET.SubElement(
        root, "title"
    ).text = "Disc assembly for the numbered rectangle pieces"
    for layer_id, seams, color in (
        ("interior_cut", secondary.interior_lines, "#423b34"),
        ("exterior_cut", secondary.exterior_lines, "#201e1b"),
        (
            "engrave",
            [LineString(p) for mark in marks for p in mark["disc_number_paths"]],
            "#754818",
        ),
    ):
        layer = ET.SubElement(
            root,
            "g",
            {
                "id": layer_id,
                "stroke": color,
                "fill": "none",
                "stroke-width": "0.35",
                "stroke-linejoin": "round",
            },
        )
        for seam in seams:
            ET.SubElement(layer, "path", {"d": _line_to_d(seam)})
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _read_faces(file):
    lines, *_ = _parse_svg_paths(file)
    segments = [
        tuple(sorted((a, b)))
        for line in lines
        for a, b in zip(line.coords, list(line.coords)[1:])
        if a != b
    ]
    faces = list(polygonize(shapely.union_all(lines)))
    return faces, len(segments) - len(set(segments))


def verify_dual(out_dir):
    """Read both serialized common-line networks and apply the saved rigid placements."""
    out = Path(out_dir)
    layout = json.loads((out / "assemblies.json").read_text(encoding="utf-8"))
    primary, rectangle_dupes = _read_faces(out / "cut.svg")
    alternate, disc_dupes = _read_faces(out / "disc-assembly.svg")
    selected = []
    disc_faces = set()
    primary_faces = set()
    errors = []
    for item in layout["numbered_pieces"]:
        source_pin = Point(item["rectangle_pin_mm"])
        target_pin = Point(item["disc_pin_mm"])
        source = next(
            (i for i, face in enumerate(primary) if face.contains(source_pin)), None
        )
        target = next(
            (i for i, face in enumerate(alternate) if face.contains(target_pin)), None
        )
        if (
            source is None
            or target is None
            or source in primary_faces
            or target in disc_faces
        ):
            raise ValueError(
                "Each engraved number must select one distinct face in both assemblies."
            )
        primary_faces.add(source)
        disc_faces.add(target)
        transform = item["rectangle_to_disc"]
        a, b, d, e, tx, ty = transform
        rotation = np.array([[a, b], [d, e]])
        if (
            not np.isfinite([a, b, d, e, tx, ty]).all()
            or not np.allclose(rotation.T @ rotation, np.eye(2))
            or not math.isclose(np.linalg.det(rotation), 1)
        ):
            raise ValueError(
                "The saved placement must be a rigid rotation and translation."
            )
        moved = affine_transform(primary[source], transform)
        selected.append(moved)
        errors.append(moved.symmetric_difference(alternate[target]).area)
    cover = shapely.union_all(selected)
    expected = _disc(layout["disc_diameter_mm"])
    board = box(0, 0, *layout["rectangle_size_mm"])
    rectangular_cover = shapely.union_all(primary)
    # Both SVGs round to .001 mm. Bound the resulting area discrepancies by
    # perimeter times that precision, and separately measure overlaps and gaps.
    tolerance = 0.001 * expected.length
    rectangle_error = board.symmetric_difference(rectangular_cover).area
    disc_error = expected.symmetric_difference(cover).area
    overlap = max(0.0, sum(p.area for p in selected) - cover.area)
    valid = (
        len(primary) == layout["rectangle_piece_count"]
        and len(alternate) == len(selected)
        and rectangle_dupes == disc_dupes == 0
        and max(errors) <= tolerance
        and disc_error <= tolerance
        and overlap <= tolerance
        and rectangle_error <= 0.001 * board.length
        and all(p.is_valid and not p.interiors for p in primary + alternate)
    )
    if not valid:
        raise ValueError(
            f"Alternate assembly failed its cut-file geometry check: "
            f"rectangle {len(primary)}, disc {len(alternate)}, "
            f"coverage {disc_error:.4f}, overlap {overlap:.4f}, largest piece error {max(errors):.4f} mm²."
        )
    return {
        "geometry_passed": True,
        "rectangle_faces": len(primary),
        "disc_faces": len(alternate),
        "rectangle_duplicate_segments": rectangle_dupes,
        "disc_duplicate_segments": disc_dupes,
        "rectangle_coverage_error_mm2": rectangle_error,
        "disc_coverage_error_mm2": disc_error,
        "disc_overlap_mm2": overlap,
        "max_piece_difference_mm2": max(errors),
        "disc_area_tolerance_mm2": tolerance,
    }


def generate_dual(
    out_dir,
    *,
    width_mm=240.0,
    height_mm=160.0,
    diameter_mm=80.0,
    pieces=24,
    disc_pieces=5,
    seed=12,
):
    """Build an independently generated rectangular cut and its numbered disc solution."""
    if not all(math.isfinite(v) and v > 0 for v in (width_mm, height_mm, diameter_mm)):
        raise ValueError("Board dimensions must be finite and positive.")
    if not 2 <= disc_pieces <= pieces - 2:
        raise ValueError(
            "Allow at least two disc pieces and two ordinary rectangle pieces."
        )
    out = Path(out_dir)
    expected = _disc(diameter_mm)
    alternate = generate_coral(expected, disc_pieces, seed=seed)
    rng = np.random.default_rng(seed)
    cols, rows, placements, marks = _place_sectors(
        alternate.faces, width_mm, height_mm, rng
    )
    faces, inside, perimeter, fixed, tabs = _rectangle_cut(
        width_mm, height_mm, rows, cols, placements, pieces, rng
    )
    primary = export_puzzle(
        out,
        faces,
        inside,
        perimeter,
        expected_outline=box(0, 0, width_mm, height_mm),
        whimsy_indices=fixed,
        engrave_lines=[LineString(p) for m in placements for p in m.engrave_paths],
        metadata={
            "title": "The second disc",
            "style": "dual",
            "seed": seed,
            "rectangle_arc_seams": tabs,
            "disc_arc_seams": alternate.metrics["arc_seams"],
            "disc_piece_count": disc_pieces,
        },
    )
    puzzle = json.loads((out / "puzzle.json").read_text(encoding="utf-8"))
    for mark in marks:
        face_id = next(
            i
            for i, poly in enumerate(primary.faces)
            if poly.contains(Point(mark["rectangle_pin_mm"]))
        )
        if not puzzle["pieces"][face_id]["is_whimsy"]:
            raise RuntimeError(
                "The numbered piece is missing from the player's fixed-piece set."
            )
        mark["rectangle_piece_id"] = face_id
    _write_assembly(out / "disc-assembly.svg", diameter_mm, alternate, marks)
    layout = {
        "schema_version": 1,
        "seed": seed,
        "rectangle_size_mm": [width_mm, height_mm],
        "disc_diameter_mm": diameter_mm,
        "rectangle_piece_count": pieces,
        "coordinate_system": "millimetres, origin at upper left, positive y down",
        "transform_format": "[a,b,d,e,tx,ty] maps rectangle (x,y) to disc (a*x+b*y+tx, d*x+e*y+ty)",
        "numbered_pieces": [
            {k: v for k, v in mark.items() if k != "disc_number_paths"}
            for mark in marks
        ],
    }
    (out / "assemblies.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")
    result = verify_dual(out)
    (out / "assembly-audit.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Make a rectangle with a removable numbered disc assembly."
    )
    parser.add_argument("--out", type=Path, default=Path("runs/two-assemblies"))
    parser.add_argument(
        "--verify", type=Path, help="Recheck the exported files in an existing run"
    )
    parser.add_argument("--width-mm", type=float, default=240)
    parser.add_argument("--height-mm", type=float, default=160)
    parser.add_argument("--diameter-mm", type=float, default=80)
    parser.add_argument("--pieces", type=int, default=24)
    parser.add_argument("--disc-pieces", type=int, default=5)
    parser.add_argument("--seed", type=int, default=12)
    args = parser.parse_args(argv)
    result = (
        verify_dual(args.verify)
        if args.verify
        else generate_dual(
            args.out,
            width_mm=args.width_mm,
            height_mm=args.height_mm,
            diameter_mm=args.diameter_mm,
            pieces=args.pieces,
            disc_pieces=args.disc_pieces,
            seed=args.seed,
        )
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
