"""Recut identified pieces or rotate them individually for a patchwork-grain edition."""

import argparse
import hashlib
import json
import math
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import LineString, Polygon

from .postprocess import SVG_NS, _line_to_d, _poly_to_d


def export_parts(
    run,
    out,
    ids=None,
    seed=0,
    rotate=False,
    sheet_width=400.0,
    spacing=4.0,
    stripe=False,
):
    """Shelf-pack rigid copies of saved outlines and carry their original print coordinates."""
    run, out = Path(run), Path(out)
    source = run / "puzzle.json"
    data = json.loads(source.read_text(encoding="utf-8"))
    pieces = {int(p["id"]): p for p in data["pieces"]}
    ids = list(pieces) if ids is None else list(ids)
    if not ids or len(set(ids)) != len(ids) or not set(ids) <= pieces.keys():
        raise ValueError("Select distinct piece IDs present in puzzle.json.")
    if (
        not math.isfinite(spacing)
        or spacing <= 0
        or not math.isfinite(sheet_width)
        or sheet_width <= 2 * spacing
    ):
        raise ValueError("Use positive stock width and spacing.")
    rng = np.random.default_rng(seed)
    placed = []
    x = y = spacing
    row_height = 0.0
    for pid in ids:
        shape = Polygon(pieces[pid]["outline"])
        angle = float(rng.uniform(0, 360)) if rotate else 0.0
        turned = affinity.rotate(shape, angle, origin=(0, 0))
        l, t, r, b = turned.bounds
        w, h = r - l, b - t
        if w + 2 * spacing > sheet_width:
            raise ValueError(f"Piece {pid} needs a wider stock sheet.")
        if x + w + spacing > sheet_width:
            x, y, row_height = spacing, y + row_height + spacing, 0.0
        dx, dy = x - l, y - t
        placed.append((pid, angle, dx, dy, affinity.translate(turned, dx, dy)))
        x += w + spacing
        row_height = max(row_height, h)
    height = y + row_height + spacing
    attributes = {
        "width": f"{sheet_width:.4f}mm",
        "height": f"{height:.4f}mm",
        "viewBox": f"0 0 {sheet_width:.4f} {height:.4f}",
    }
    cut = ET.Element(f"{{{SVG_NS}}}svg", attributes)
    art = ET.Element(f"{{{SVG_NS}}}svg", attributes)
    cuts = ET.SubElement(
        cut,
        f"{{{SVG_NS}}}g",
        {"id": "cut", "fill": "none", "stroke": "#f00", "stroke-width": "0.1"},
    )
    engrave = ET.SubElement(
        cut,
        f"{{{SVG_NS}}}g",
        {"id": "engrave", "fill": "none", "stroke": "#00aa00", "stroke-width": "0.15"},
    )
    defs = ET.SubElement(art, f"{{{SVG_NS}}}defs")
    if data.get("artwork"):
        ET.SubElement(
            defs,
            f"{{{SVG_NS}}}image",
            {"id": "source-print", **{k: str(v) for k, v in data["artwork"].items()}},
        )
    original_strokes = [
        LineString(line) for line in data.get("engrave_paths", []) if len(line) > 1
    ]
    records = []
    for pid, angle, dx, dy, poly in placed:
        src_poly = Polygon(pieces[pid]["outline"])
        ET.SubElement(
            cuts, f"{{{SVG_NS}}}path", {"id": f"piece-{pid}", "d": _poly_to_d(poly)}
        )
        strokes = list(original_strokes)
        if stripe:
            cx, cy = src_poly.centroid.coords[0]
            for theta in np.linspace(0, 2 * np.pi, 24, endpoint=False):
                length = src_poly.length
                strokes.append(
                    LineString(
                        [
                            (cx, cy),
                            (cx + length * np.cos(theta), cy + length * np.sin(theta)),
                        ]
                    )
                )
        for line in strokes:
            for part in shapely.get_parts(line.intersection(src_poly)):
                if part.geom_type == "LineString" and part.length > 0.05:
                    moved = affinity.translate(
                        affinity.rotate(part, angle, origin=(0, 0)), dx, dy
                    )
                    ET.SubElement(
                        engrave, f"{{{SVG_NS}}}path", {"d": _line_to_d(moved)}
                    )
        clip = ET.SubElement(defs, f"{{{SVG_NS}}}clipPath", {"id": f"mask-{pid}"})
        ET.SubElement(clip, f"{{{SVG_NS}}}path", {"d": _poly_to_d(src_poly)})
        group = ET.SubElement(
            art,
            f"{{{SVG_NS}}}g",
            {
                "transform": f"translate({dx} {dy}) rotate({angle})",
                "clip-path": f"url(#mask-{pid})",
            },
        )
        if data.get("artwork"):
            ET.SubElement(group, f"{{{SVG_NS}}}use", {"href": "#source-print"})
        else:
            ET.SubElement(
                group,
                f"{{{SVG_NS}}}path",
                {"d": _poly_to_d(src_poly), "fill": pieces[pid]["color"]},
            )
        records.append({"piece": pid, "angle_deg": angle, "translate_mm": [dx, dy]})
    out.mkdir(parents=True, exist_ok=True)
    for root, name in ((cut, "parts-cut.svg"), (art, "parts-artwork.svg")):
        ET.ElementTree(root).write(out / name, encoding="utf-8", xml_declaration=True)
    manifest = {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source": str(source),
        "stock_mm": [sheet_width, height],
        "spacing_mm": spacing,
        "pieces": records,
    }
    (out / "parts.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run", type=Path)
    p.add_argument(
        "--ids",
        type=int,
        nargs="+",
        help="IDs from puzzle.json; default is every piece",
    )
    p.add_argument(
        "--rotate",
        action="store_true",
        help="Turn each piece independently on the stock",
    )
    p.add_argument(
        "--stripe", action="store_true", help="Engrave radial lines on each piece"
    )
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--sheet-width", type=float, default=400)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)
    export_parts(a.run, a.out, a.ids, a.seed, a.rotate, a.sheet_width, stripe=a.stripe)


if __name__ == "__main__":
    main()
