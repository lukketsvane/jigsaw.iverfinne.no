"""Build the six-coupon Formverk wood sampler using the production solver and exporter.

Run .venv/Scripts/python scripts/formverk_trial_styles.py [output-directory].
The cut file uses millimetres. The separate placement map contains the labels.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

from shapely import union_all
from shapely.geometry import box
from shapely.ops import polygonize

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from dendrite_engine.cli import generate_puzzle_run
from dendrite_engine.postprocess import _parse_svg_paths, audit_cut_svg


STYLES = ("dendrite", "amoeba", "maze", "wave", "coral", "matrix")
SEEDS = (42, 22, 7, 11, 101, 1, 2)
WIDTH, HEIGHT, COUNT = 90, 70, 10
SHEET_WIDTH, SHEET_HEIGHT = 297, 160
ORIGINS = {style: (7.5 + i % 3 * 96, 7 + i // 3 * 76) for i, style in enumerate(STYLES)}
LAYERS = ("interior_cut", "exterior_cut")
NS = "{http://www.w3.org/2000/svg}"
ET.register_namespace("", NS[1:-1])


def summary(audit):
    fields = ("pieces", "duplicate_segments", "coverage_error_mm2", "overlap_mm2",
              "invalid_pieces", "pieces_with_holes", "cut_length_mm", "area_mm2",
              "kerf_failures", "heller_neck_failures", "neck_2_5mm_failures",
              "neck_3mm_failures", "worst_tip_reach_mm", "all_hard_gates_passed")
    return {**{key: audit[key] for key in fields},
            "assembly_lock_margin_mm": audit["interlock"]["assembly_lock_margin_mm"]}


def passes(audit):
    return audit["pieces"] == COUNT and audit["all_hard_gates_passed"]


def select_coupons(staging, out):
    attempts = []
    for seed in SEEDS:
        row = {}
        for style in STYLES:
            folder = staging / str(seed) / style
            result = generate_puzzle_run(folder, style=style, width_mm=WIDTH, height_mm=HEIGHT,
                                         num_pieces=COUNT, seed=seed, px_mm=.5, kerf_mm=.15,
                                         min_neck_mm=3, artwork_pattern="wood")
            audited = summary(result.audit)
            row[style] = {key: audited[key] for key in ("pieces", "all_hard_gates_passed",
                          "assembly_lock_margin_mm", "heller_neck_failures",
                          "neck_2_5mm_failures", "neck_3mm_failures")}
            # The first two complete batches document the original wood-trial screen.
            if seed not in SEEDS[:2] and not passes(audited):
                break
        attempts.append({"seed": seed, "styles": row})
        if len(row) == len(STYLES) and all(passes(a) for a in row.values()):
            break
    else:
        raise RuntimeError(f"No common seed passed the wood-trial screen: {attempts}")
    for style in STYLES:
        dest = out / "coupons" / style
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staging / str(seed) / style / "cut.svg", dest / "cut.svg")
    return seed, attempts


def svg_page(width, height):
    return ET.Element(NS + "svg", {"width": f"{width}mm", "height": f"{height}mm",
                      "viewBox": f"0 0 {width} {height}", "version": "1.1"})


def coords_um(path):
    """Read the normal Formverk exporter's explicit absolute M/L polylines at 1 µm."""
    tokens = path.get("d", "").split()
    if not tokens or len(tokens) % 3 or tokens[0] != "M" or any(c != "L" for c in tokens[3::3]):
        raise ValueError("Unexpected cut-path commands")
    return [(round(float(x) * 1000), round(float(y) * 1000))
            for x, y in zip(tokens[1::3], tokens[2::3])]


def encode(points):
    return "M " + " L ".join(f"{x / 1000:.3f} {y / 1000:.3f}" for x, y in points)


def group(page, layer):
    return page.find(f"{NS}g[@id='{layer}']")


def write(page, file):
    ET.ElementTree(page).write(file, encoding="utf-8", xml_declaration=True)


def impose(out):
    sheet = svg_page(SHEET_WIDTH, SHEET_HEIGHT)
    for layer in LAYERS:
        for style in STYLES:
            x, y = ORIGINS[style]
            source = ET.parse(out / "coupons" / style / "cut.svg").getroot()
            if tuple(map(float, source.get("viewBox").split())) != (0, 0, WIDTH, HEIGHT):
                raise ValueError(f"Unexpected coupon dimensions for {style}")
            original = group(source, layer)
            if style == STYLES[0]:
                combined = ET.SubElement(sheet, NS + "g", original.attrib.copy())
            for path in original:
                shifted = [(px + round(x * 1000), py + round(y * 1000)) for px, py in coords_um(path)]
                ET.SubElement(combined, NS + "path", {"id": style + "-" + path.get("id"),
                                                      "d": encode(shifted)})
    write(sheet, out / "cut.svg")


def text_label(root, x, y, words, size, **attrs):
    ET.SubElement(root, NS + "text", {"x": str(x), "y": str(y), "font-size": str(size),
                  "font-family": "Barlow, Arial, sans-serif", "fill": "#241d16", **attrs}).text = words


def placement_map(out, seed, coupons):
    diagram = svg_page(297, 196)
    ET.SubElement(diagram, NS + "rect", {"width": "297", "height": "196", "fill": "#faf8f4"})
    text_label(diagram, 7.5, 8, "Formverk wood sampler", 6, **{"font-weight": "700"})
    text_label(diagram, 7.5, 14, f"Placement map    /    90 × 70 mm per coupon    /    10 pieces each    /    Seed {seed}", 3.0)
    cut = ET.parse(out / "cut.svg").getroot()
    for style, (x, y) in ORIGINS.items():
        text_label(diagram, x, y + 15.7, style.title(), 3.4, **{"font-weight": "700"})
        ET.SubElement(diagram, NS + "rect", {"x": str(x), "y": str(y + 18), "width": str(WIDTH),
                      "height": str(HEIGHT), "fill": "#eddfc6"})
    for layer in LAYERS:
        lines = ET.SubElement(diagram, NS + "g", {"transform": "translate(0 18)", "fill": "none",
                           "stroke": "#6e5843", "stroke-width": ".28" if layer == LAYERS[0] else ".52"})
        for path in group(cut, layer):
            ET.SubElement(lines, NS + "path", {"d": path.get("d")})
    for coupon in coupons.values():
        for flag in coupon["erosion_flags"]:
            x, y = flag["sheet_label_point_mm"]
            ET.SubElement(diagram, NS + "circle", {"cx": str(x), "cy": str(y + 18),
                          "r": "2.65", "fill": "#8a3624", "stroke": "#fff", "stroke-width": ".3"})
            text_label(diagram, x, y + 19.05, str(flag["audit_piece_id"]), 3.3,
                       **{"fill": "#fff", "font-weight": "700", "text-anchor": "middle"})
    text_label(diagram, 7.5, 179, "Numbered dots locate pieces flagged by a neck erosion check. Inspect the marked pieces after cutting.", 3.1)
    text_label(diagram, 7.5, 185, "Cut sheet    297 × 160 mm     ·     Keep the same grain direction for all six coupons.", 3.1)
    text_label(diagram, 7.5, 191, "Red paths cut the shared seams. Blue paths release each perimeter. Labels appear only on this map.", 3.0)
    write(diagram, out / "placement-map.svg")


def verify_sheet(out):
    cut_file = out / "cut.svg"
    page = ET.parse(cut_file).getroot()
    if (page.get("width"), page.get("height"), page.get("viewBox")) != ("297mm", "160mm", "0 0 297 160"):
        raise ValueError("Cut-sheet scale changed")
    if any(elem.tag not in {NS + "svg", NS + "g", NS + "path"} or "transform" in elem.attrib for elem in page.iter()):
        raise ValueError("Unexpected geometry or transform in the cut sheet")
    expected = {}
    boxes = {}
    for style, (x, y) in ORIGINS.items():
        boxes[style] = box(x, y, x + WIDTH, y + HEIGHT)
        source = ET.parse(out / "coupons" / style / "cut.svg").getroot()
        for layer in LAYERS:
            for path in group(source, layer):
                key = (layer, f"{style}-{path.get('id')}")
                expected[key] = [(px + round(x * 1000), py + round(y * 1000))
                                 for px, py in coords_um(path)]
    for layer in page:
        for path in layer:
            key = (layer.get("id"), path.get("id"))
            if coords_um(path) != expected.pop(key):
                raise ValueError(f"Coupon path changed on the combined sheet: {key}")
    if expected:
        raise ValueError("Missing source cut paths from sheet")
    lines, *_ = _parse_svg_paths(cut_file)
    segments = Counter(tuple(sorted((a, b))) for line in lines
                       for a, b in zip(line.coords, list(line.coords)[1:]) if a != b)
    polygons = list(polygonize(union_all(lines)))
    area_error = union_all(polygons).symmetric_difference(union_all(list(boxes.values()))).area
    duplicate_segments = sum(segments.values()) - len(segments)
    if duplicate_segments or area_error > 1e-6 or len(polygons) != COUNT * len(STYLES):
        raise ValueError("Sheet failed common-line, coverage or piece-count checks")
    step = min(a.distance(b) for i, a in enumerate(boxes.values()) for b in list(boxes.values())[i + 1:])
    coupon_audits = {}
    with TemporaryDirectory(dir=out) as tmp:
        for style, boundary in boxes.items():
            # Read paths from the final file, retaining absolute sheet coordinates.
            part = svg_page(SHEET_WIDTH, SHEET_HEIGHT)
            for layer in LAYERS:
                src = group(page, layer)
                dst = ET.SubElement(part, NS + "g", src.attrib.copy())
                for path in src:
                    if path.get("id").startswith(style + "-"):
                        ET.SubElement(dst, NS + "path", path.attrib.copy())
            file = Path(tmp) / f"{style}.svg"
            write(part, file)
            raw = audit_cut_svg(file, expected_outline=boundary, min_neck_mm=3)
            audit = summary(raw)
            inside = sorted((p for p in polygons if boundary.covers(p.representative_point())),
                            key=lambda p: (round(p.centroid.y / 15), p.centroid.x))
            if not passes(audit) or len(inside) != COUNT:
                raise ValueError(f"{style} failed its cut-sheet reimport: {audit}")
            flags = []
            for row, polygon in zip(raw["per_piece"], inside):
                if abs(row["area_mm2"] - polygon.area) > .00501:
                    raise ValueError(f"Coupon face ordering changed for {style}")
                if row["neck_2_5mm_parts"] == 1 and row["neck_3mm_parts"] == 1:
                    continue
                point = polygon.representative_point()
                ox, oy = ORIGINS[style]
                flags.append({"audit_piece_id": row["piece"], "neck_2_5mm_parts": row["neck_2_5mm_parts"],
                              "neck_3mm_parts": row["neck_3mm_parts"],
                              "local_label_point_mm": [round(point.x - ox, 2), round(point.y - oy, 2)],
                              "sheet_label_point_mm": [round(point.x, 2), round(point.y, 2)]})
            source_path = out / "coupons" / style / "cut.svg"
            coupon_audits[style] = {**audit, "origin_mm": ORIGINS[style],
                                    "erosion_flags": flags,
                                    "source_svg": source_path.relative_to(out).as_posix(),
                                    "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest()}
    return {"pieces": len(polygons), "duplicate_segments": duplicate_segments,
            "unique_segments": len(segments), "source_paths_match_at_0_001_mm": True,
            "coverage_error_mm2": round(area_error, 6), "minimum_coupon_spacing_mm": step,
            "cut_sha256": hashlib.sha256(cut_file.read_bytes()).hexdigest(), "coupons": coupon_audits}


def build_styles(out_folder: Path):
    out = Path(out_folder)
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out) as stage:
        seed, attempts = select_coupons(Path(stage), out)
    impose(out)
    checks = verify_sheet(out)
    placement_map(out, seed, checks["coupons"])
    manifest = {"sheet_mm": [SHEET_WIDTH, SHEET_HEIGHT], "coupon_mm": [WIDTH, HEIGHT],
              "pieces_requested_per_coupon": COUNT, "nominal_piece_pitch_mm": round((WIDTH * HEIGHT / COUNT) ** .5, 2),
              "phase_field_pixel_pitch_mm": .5, "nominal_kerf_mm": .15, "minimum_neck_setting_mm": 3,
              "art": "natural wood, no printing or engraving", "selected_seed": seed,
              "coordinate_system": "Millimetres from top left, x right and y down. Flag coordinates identify a point inside each affected piece. Piece IDs use the final imposition audit's zero-based ordering.",
              "selection": "First common seed with ten pieces and all engine hard gates for each coupon. Auxiliary erosion counts are reported separately.",
              "attempts": attempts, "sheet_reimport": checks}
    (out / "sampler-audit.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_folder", type=Path, nargs="?", default=REPO / "runs/formverk-wood-trial/style-sampler")
    result = build_styles(parser.parse_args().out_folder)
    print(f"Verified {result['sheet_reimport']['pieces']} pieces at seed {result['selected_seed']}.")
