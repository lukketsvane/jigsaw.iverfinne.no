"""A reversible solar/lunar puzzle with reflection-matched pairs of physical pieces.

Generate: python -m dendrite_engine.eclipse --out runs/eclipse --seed 42
Verify:   python -m dendrite_engine.eclipse --verify runs/eclipse
"""

from __future__ import annotations

import argparse
import base64
from io import BytesIO
import json
import math
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import shapely
from shapely.affinity import scale
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize

from .coral import _arc_lobe
from .postprocess import _parse_svg_paths, export_puzzle


def _polar_mesh(diameter, sectors, bands, seed):
    """Build each physical seam once. All arc choices are paired under x reflection."""
    if not math.isfinite(diameter) or diameter <= 0:
        raise ValueError("Choose a positive disc diameter.")
    if sectors < 4 or sectors % 4 or not 1 <= bands <= 5:
        raise ValueError(
            "Choose a multiple of four sectors and one to five outer bands."
        )
    radius = diameter / 2
    radii = np.linspace(radius * 0.23, radius, bands + 1)
    if np.diff(radii).min() < 7 or 2 * math.pi * radii[0] / sectors < 6:
        raise ValueError(
            "Increase the diameter or reduce the subdivision for these small pieces."
        )
    step = math.tau / sectors
    rng = np.random.default_rng(seed)
    # The pair of shoulders forms a real undercut before the ring is bent into polar space.
    arc = _arc_lobe(0.54, math.radians(108), 0.48)
    arc[:, 0] += 0.23
    collar = np.linspace(0, 0.23, 6, endpoint=False)
    samples = np.concatenate(
        (
            np.column_stack((collar, np.zeros_like(collar))),
            arc,
            np.column_stack((1 - collar[::-1], np.zeros_like(collar))),
        )
    )
    along, perpendicular = samples.T
    normal = perpendicular / perpendicular.max()

    def xy(theta, r):
        return np.round(
            np.stack(
                np.broadcast_arrays(
                    radius + r * np.cos(theta), radius + r * np.sin(theta)
                ),
                axis=-1,
            ),
            3,
        )

    def reflected(line, reverse=False):
        coords = np.array(line.coords)
        coords[:, 0] = np.round(diameter - coords[:, 0], 3)
        return LineString(coords[::-1] if reverse else coords)

    # Construct only one side of each mirrored pair on the actual .001 mm
    # SVG grid. Independently sampling both sides can round a half-step in
    # different directions, leaving a visible gap under high magnification.
    inside, outer, target_points = [], [], [(radius, radius)]
    for k, r in enumerate(radii):
        edge_signs = rng.choice([-1, 1], size=sectors)
        ring = []
        for j in range(sectors):
            mirrored_edge = (sectors // 2 - j - 1) % sectors
            if mirrored_edge < j:
                edge = reflected(ring[mirrored_edge], reverse=True)
            elif k == bands:
                coords = xy(
                    step * (j + np.linspace(0, 1, max(8, 360 // sectors) + 1)), r
                )
                edge = LineString(coords)
            else:
                direction = edge_signs[j]
                coords = xy(
                    step * (j + along),
                    r + direction * 0.19 * (radii[k + 1] - r) * normal,
                )
                edge = LineString(coords)
            ring.append(edge)
        (outer if k == bands else inside).extend(ring)
        if k == bands:
            break
        ray_signs = rng.choice([-1, 1], size=sectors)
        rays = []
        for j in range(sectors):
            mirrored_ray = (sectors // 2 - j) % sectors
            # A ray on the symmetry axis stays straight; the mirrored pair meets there.
            if mirrored_ray < j:
                ray = reflected(rays[mirrored_ray])
            elif mirrored_ray == j:
                ray = LineString(xy(step * j, np.array([r, radii[k + 1]])))
            else:
                angle = step * (j + ray_signs[j] * 0.16 * normal)
                ray = LineString(xy(angle, r + along * (radii[k + 1] - r)))
            rays.append(ray)
            target_points.append(tuple(xy(step * (j + 0.5), (r + radii[k + 1]) / 2)))
        inside.extend(rays)
    board = Polygon(np.vstack([edge.coords for edge in outer]))
    faces = list(polygonize(shapely.union_all([*inside, *outer])))
    ordered = [
        next((p for p in faces if p.contains(Point(pt))), None) for pt in target_points
    ]
    if (
        len(faces) != len(ordered)
        or any(p is None for p in ordered)
        or len(set(ordered)) != len(ordered)
    ):
        raise ValueError("The circular subdivision did not preserve all pieces.")
    return board, ordered, inside, outer, target_points


def _prints(seed, side=840):
    """Produce new solar filaments and a cratered lunar surface in board coordinates."""
    rng = np.random.default_rng(seed)
    axis = (np.arange(side) + 0.5) / side * 2 - 1
    x, y = np.meshgrid(axis, axis)
    r, theta = np.hypot(x, y), np.arctan2(y, x)
    phase = rng.uniform(0, math.tau, 5)
    field = (
        np.sin(8 * x + 2 * np.sin(5 * y + phase[0])) + np.sin(13 * y - 3 * x + phase[1])
    ) / 2
    strands = (
        0.5 + 0.5 * np.cos(46 * r + 9 * field + 3 * np.sin(8 * theta + phase[2]))
    ) ** 5
    streamers = (
        0.5 + 0.5 * np.cos(22 * theta + 4 * np.sin(5 * theta + r * 4 + phase[3]))
    ) ** 3
    core = 1 / (1 + np.exp(np.clip((r - 0.59) * 100, -60, 60)))
    glow = np.exp(-abs(r - 0.605) * 8) * (0.55 + 0.45 * streamers)
    fine = rng.normal(0, 0.012, size=r.shape)
    solar = np.clip(
        np.stack(
            (
                0.085 + 0.65 * core + 0.65 * glow + 0.27 * strands * core + fine,
                0.065 + 0.32 * core + 0.57 * glow + 0.36 * strands * core + fine,
                0.095 + 0.08 * core + 0.29 * glow + 0.17 * strands * core + fine,
            ),
            axis=-1,
        ),
        0,
        1,
    )
    light = np.maximum(
        0, -0.43 * x - 0.51 * y + 0.62 * np.sqrt(np.maximum(0, 1 - r * r))
    )
    texture = 0.17 + 0.39 * light + 0.035 * field + fine
    for _ in range(56):
        cx, cy = rng.uniform(-1, 1, 2)
        if cx * cx + cy * cy > 0.94:
            continue
        rad = rng.uniform(0.018, 0.115)
        q = np.hypot(x - cx, y - cy) / rad
        shade = np.clip((cx - x + cy - y) / (1.4 * rad), -1, 1)
        texture += 0.13 * np.exp(-(((q - 1) / 0.16) ** 2)) * (
            1 + shade
        ) - 0.095 * np.exp(-((q / 0.82) ** 4))
    lunar = np.clip(
        np.stack(
            (0.025 + texture * 0.81, 0.035 + texture * 0.89, 0.055 + texture), axis=-1
        ),
        0,
        1,
    )
    return (solar * 255).astype(np.uint8), (lunar * 255).astype(np.uint8)


def _state_table(sectors, bands):
    states = {}
    for name in ("sun", "ingress", "partial", "late", "corona", "moon"):
        rows = []
        for source in range(1 + sectors * bands):
            k, j = divmod(source - 1, sectors) if source else (-1, 0)
            partner = (
                1 + k * sectors + (sectors // 2 - j - 1) % sectors if source else 0
            )
            # Use whole reflection pairs at the north/south edges of the outside ring.
            # The coarsest allowed five-piece board has just one pair on each side.
            cap = 2 * max(1, sectors // 12) - 1
            north = k == bands - 1 and abs(2 * j + 1 - 3 * sectors // 2) <= cap
            south = k == bands - 1 and abs(2 * j + 1 - sectors // 2) <= cap
            flip = (
                name == "moon"
                or name == "ingress"
                and (south if sectors == 4 and bands == 1 else north)
                or name == "late"
                and not south
                or name == "corona"
                and k < max(0, bands - 1)
                or name == "partial"
                and source > 0
                and j >= sectors // 2
            )
            rows.append(
                {
                    "piece": source,
                    "slot": partner if flip else source,
                    "face": "back" if flip else "front",
                }
            )
        states[name] = rows
    return states


def _read_print(path, diameter=None):
    img = ET.parse(path).find(".//{*}image")
    if img is None or not img.get("href", "").startswith("data:image/png;base64,"):
        raise ValueError(f"Missing registered print in {path.name}")
    if diameter is not None:
        placement = [float(img.get(k)) for k in ("x", "y", "width", "height")]
        if not np.allclose(placement, [0, 0, diameter, diameter], atol=0.001, rtol=0):
            raise ValueError(
                f"The print in {path.name} is offset or scaled relative to the cut."
            )
    return Image.open(
        BytesIO(base64.b64decode(img.get("href").split(",", 1)[1]))
    ).convert("RGB")


def _contact_sheet(path, slots, table, diameter, sun, moon):
    side = 380
    columns = len(table) // 2
    canvas = Image.new("RGB", (columns * side, 2 * (side + 37)), "#ede9de")
    font = ImageFont.load_default(size=20)
    for i, (name, mappings) in enumerate(table.items()):
        assembled = Image.new("RGB", (side, side), "#ede9de")
        moon_mask = Image.new("L", (side, side))
        disc_mask = Image.new("L", (side, side))
        moon_draw, board_draw = ImageDraw.Draw(moon_mask), ImageDraw.Draw(disc_mask)
        seams = []
        for move in mappings:
            p = slots[move["slot"]]
            xy = [
                (round(x / diameter * (side - 1)), round(y / diameter * (side - 1)))
                for x, y in p.exterior.coords
            ]
            board_draw.polygon(xy, fill=255)
            if move["face"] == "back":
                moon_draw.polygon(xy, fill=255)
            seams.append(xy)
        assembled.paste(
            sun.resize((side, side), Image.Resampling.LANCZOS), mask=disc_mask
        )
        assembled.paste(
            moon.resize((side, side), Image.Resampling.LANCZOS), mask=moon_mask
        )
        ink = ImageDraw.Draw(assembled)
        for seam in seams:
            ink.line(seam, fill="#403630", width=1)
        x, y = (i % columns) * side, (i // columns) * (side + 37)
        canvas.paste(assembled, (x, y))
        ImageDraw.Draw(canvas).text(
            (x + 12, y + side + 5),
            {
                "ingress": "Early partial eclipse",
                "partial": "Half eclipse",
                "late": "Late partial eclipse",
            }.get(name, name.capitalize()),
            font=font,
            fill="#262825",
        )
    canvas.save(path)


def verify_eclipse(out_dir):
    """Reconstruct physical pieces from the SVG and verify every face swap against its slot."""
    out = Path(out_dir)
    plan = json.loads((out / "states.json").read_text(encoding="utf-8"))
    lines, _, _, width, height = _parse_svg_paths(out / "cut.svg")
    segments = [
        tuple(sorted((a, b)))
        for ln in lines
        for a, b in zip(ln.coords, list(ln.coords)[1:])
        if a != b
    ]
    faces = list(polygonize(shapely.union_all(lines)))
    slots = [
        next((p for p in faces if p.contains(Point(pt))), None)
        for pt in plan["slot_pins_mm"]
    ]
    count = 1 + plan["bands"] * plan["sectors"]
    if (
        len(faces) != count
        or len(slots) != count
        or any(p is None for p in slots)
        or len(set(slots)) != count
    ):
        raise ValueError("The exported cut no longer contains the specified pieces.")
    if len(segments) != len(set(segments)):
        raise ValueError("Duplicate cut segments found in the exported board.")
    if not math.isclose(width, plan["diameter_mm"], abs_tol=0.002) or not math.isclose(
        height, width, abs_tol=0.002
    ):
        raise ValueError("The exported board no longer matches the print registration.")
    board = shapely.union_all(slots)
    sun, moon = (
        _read_print(out / "artwork.svg", width),
        _read_print(out / "back.svg", width),
    )
    if sun.size != moon.size or sun.tobytes() == moon.tobytes():
        raise ValueError(
            "The two registered prints must be distinct and equal in size."
        )
    expected = _state_table(plan["sectors"], plan["bands"])
    if plan["schema_version"] == 1:
        expected = {key: expected[key] for key in ("sun", "moon", "corona", "partial")}
    patterns = {tuple(r["face"] for r in table) for table in expected.values()}
    if len(patterns) != len(expected):
        raise ValueError(
            "Each requested state needs a different arrangement of visible faces."
        )
    if plan["states"] != expected:
        raise ValueError(
            "The stored face and slot instructions do not match the symmetric states."
        )
    stats = {}
    for name, rows in plan["states"].items():
        if sorted(r["slot"] for r in rows) != list(range(count)) or sorted(
            r["piece"] for r in rows
        ) != list(range(count)):
            raise ValueError(
                "Each state must use all physical pieces once and fill every slot."
            )
        moved = []
        max_piece_error = 0.0
        for move in rows:
            p = slots[move["piece"]]
            flipped = move["face"] == "back"
            if flipped:
                p = scale(p, xfact=-1, yfact=1, origin=(width / 2, 0))
            moved.append(p)
            max_piece_error = max(
                max_piece_error, p.symmetric_difference(slots[move["slot"]]).area
            )
        cover = shapely.union_all(moved)
        gap = board.symmetric_difference(cover).area
        overlap = max(0.0, sum(p.area for p in moved) - cover.area)
        if max(max_piece_error, gap, overlap) > 0.001 * board.length:
            raise ValueError(f"The physical pieces do not tile the {name} state.")
        stats[name] = {
            "front": sum(r["face"] == "front" for r in rows),
            "back": sum(r["face"] == "back" for r in rows),
            "coverage_error_mm2": gap,
            "overlap_mm2": overlap,
            "max_piece_difference_mm2": max_piece_error,
        }
    return {
        "geometry_passed": True,
        "pieces": count,
        "duplicate_cut_segments": 0,
        "diameter_mm": width,
        "registered_print_pixels": list(sun.size),
        "states": stats,
    }


def generate_eclipse(out_dir, *, diameter_mm=180, sectors=12, bands=2, seed=42):
    out = Path(out_dir)
    board, slots, inner, outer, pins = _polar_mesh(diameter_mm, sectors, bands, seed)
    sun, moon = _prints(seed)
    export_puzzle(
        out,
        slots,
        inner,
        outer,
        expected_outline=board,
        artwork_image=sun,
        back_artwork=moon,
        metadata={
            "title": "Eclipse",
            "style": "eclipse",
            "seed": seed,
            "sectors": sectors,
            "bands": bands,
        },
    )
    table = _state_table(sectors, bands)
    plan = {
        "schema_version": 2,
        "diameter_mm": diameter_mm,
        "sectors": sectors,
        "bands": bands,
        "instructions": "Keep front pieces in their slots. Turn each back piece face-down about the vertical axis and put it in the indicated mirror slot.",
        "state_descriptions": {
            "sun": "All pieces show their solar print.",
            "ingress": "A pair or group of outer pieces shows the approaching Moon.",
            "partial": "One half of the surrounding rings shows the lunar print.",
            "late": "Only a small cap at the opposite rim still shows its solar print.",
            "corona": "The central and inner pieces show the Moon against a solar rim.",
            "moon": "Turn the complete puzzle to its lunar print.",
        },
        "slot_pins_mm": pins,
        "states": table,
    }
    (out / "states.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    verified = verify_eclipse(out)
    sun_print, moon_print = (
        _read_print(out / "artwork.svg"),
        _read_print(out / "back.svg"),
    )
    _contact_sheet(
        out / "six-states.png", slots, table, diameter_mm, sun_print, moon_print
    )
    original = {key: table[key] for key in ("sun", "moon", "corona", "partial")}
    _contact_sheet(
        out / "four-states.png", slots, original, diameter_mm, sun_print, moon_print
    )
    (out / "state-audit.json").write_text(
        json.dumps(verified, indent=2), encoding="utf-8"
    )
    return verified


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Make a reversible puzzle with six solar and lunar arrangements."
    )
    parser.add_argument("--out", type=Path, default=Path("runs/eclipse"))
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--diameter-mm", type=float, default=180)
    parser.add_argument("--sectors", type=int, default=12)
    parser.add_argument("--bands", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)
    report = (
        verify_eclipse(args.verify)
        if args.verify
        else generate_eclipse(
            args.out,
            diameter_mm=args.diameter_mm,
            sectors=args.sectors,
            bands=args.bands,
            seed=args.seed,
        )
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
