"""Package and re-audit the two original, matched Formverk wood-trial cuts."""

import argparse
import base64
from collections import Counter, defaultdict
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw
from shapely import union_all
from shapely.geometry import MultiPoint, Point, box
from shapely.geometry.polygon import orient
from shapely.ops import polygonize_full

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pathnet.audit_field import read_cut


RUNS = ROOT / "runs" / "aphex-logo-398-v2"
ARMS = {
    "A": ("v1-seed22-L0-bay64r", "1790fcda8a56cd657a65bbac0c5c94b7392591965ecf8eac953c5fc0ff5e290a"),
    "B": ("seed22-m-L0r-bay64", "44f696c5adc590903f7c05445a8aad62ab6d70c042b4ee9cbb72fb2273e8ab36"),
}
SITE_SPECS = (
    ("S1", (0.25, 0.25), "spatial_sample"),
    ("S2", (0.50, 0.50), "spatial_sample"),
    ("S3", (0.75, 0.75), "spatial_sample"),
    ("S4", (0.25, 0.75), "digital_positive_control"),
)
MIN_JUNCTION_MM = 4.0


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_cut(path, stored_audit):
    lines, width, height = read_cut(path)
    view_box = ET.parse(path).getroot().attrib
    if view_box["viewBox"].split()[:2] != ["0", "0"] or any(
        view_box[key] != f"{number}mm" for key, number in (("width", width), ("height", height))
    ):
        raise ValueError(f"Unexpected SVG physical units or origin in {path}")

    network = union_all(lines)
    polygons, cuts, dangles, invalid = polygonize_full(network)
    # Same numbering as the original fabrication audit. The area check below
    # confirms the stable correspondence on these two identified exports.
    faces = sorted(polygons.geoms, key=lambda face: (round(face.centroid.y / 20), face.centroid.x))
    board = box(0, 0, width, height)
    cover = union_all(faces)
    segment_count = Counter()
    for line in lines:
        coords = list(line.coords)
        segment_count.update(tuple(sorted((a, b))) for a, b in zip(coords, coords[1:]))
    raw_length = sum(line.length for line in lines)
    result = {
        "size_mm": [width, height], "bounded_faces": len(faces), "board_area_mm2": board.area,
        "face_union_area_mm2": round(cover.area, 6),
        "coverage_error_mm2": round(cover.symmetric_difference(board).area, 6),
        "overlap_mm2": round(max(0, sum(face.area for face in faces) - cover.area), 6),
        "duplicate_segments": sum(segment_count.values()) - len(segment_count),
        "repeated_cut_length_mm": round(max(0, raw_length - network.length), 6),
        "cuts_without_a_face": len(cuts.geoms), "dangling_paths": len(dangles.geoms),
        "invalid_rings": len(invalid.geoms), "invalid_faces": sum(not f.is_valid for f in faces),
        "faces_with_holes": sum(bool(f.interiors) for f in faces),
        "total_cut_length_mm": round(raw_length, 3),
    }
    if len(faces) != 183 or [width, height] != [368.0, 368.0] or any(
        result[key] for key in ("coverage_error_mm2", "overlap_mm2", "duplicate_segments", "repeated_cut_length_mm",
                                "cuts_without_a_face", "dangling_paths", "invalid_rings", "invalid_faces", "faces_with_holes")
    ):
        raise ValueError(f"Delivered geometry failed inspection: {result}")
    rows = stored_audit["pieces"]
    if len(rows) != len(faces) or any(
        row["piece"] != index or abs(row["area_mm2"] - face.area) > 0.005001
        for index, (row, face) in enumerate(zip(rows, faces))
    ):
        raise ValueError("Face IDs differ from the fabrication audit")
    return result, faces, board


def inspect_seams(faces, board):
    owners = defaultdict(list)
    for piece, face in enumerate(faces):
        ring = list(orient(face, 1).exterior.coords)
        for start, end in zip(ring, ring[1:]):
            owners[tuple(sorted((start, end)))].append((piece, start, end))

    degree = Counter(point for edge in owners for point in edge)
    junctions = MultiPoint([point for point, count in degree.items() if count >= 3])
    border_ids = {i for i, f in enumerate(faces) if f.intersects(board.boundary)}
    segments, piece_pairs = [], []
    vectors = defaultdict(list)
    for records in owners.values():
        if len(records) != 2:
            continue
        (first, start, end), (second, _, _) = records
        pair = first, second
        vectors[pair].append((end[1] - start[1], start[0] - end[0]))
        if first not in border_ids and second not in border_ids:
            segments.append((start, end))
            piece_pairs.append(pair)

    # Each vector is the outward normal of one edge multiplied by its length.
    # The minimum over a full sweep is independent of which neighbor is first.
    angles = np.deg2rad(np.arange(360))
    directions = np.array([np.cos(angles), np.sin(angles)])
    margins = {pair: float(np.maximum(0, np.asarray(normals) @ directions).sum(axis=0).min())
               for pair, normals in vectors.items()}
    stats = {"adjacent_pairs": len(margins), "locked_pairs_at_1mm": sum(m >= 1.0 for m in margins.values()),
             "directions_tested": len(angles)}

    start_points, end_points = np.transpose(np.array(segments), (1, 0, 2))
    delta = end_points - start_points
    length_sq = np.sum(delta ** 2, axis=1)
    sites = []
    for site_id, target, site_class in SITE_SPECS:
        target_mm = np.asarray(target) * np.asarray(board.bounds[2:])
        t = np.clip(np.sum((target_mm - start_points) * delta, axis=1) / length_sq, 0, 1)
        candidates = start_points + delta * t[:, None]
        distances = np.linalg.norm(candidates - target_mm, axis=1)
        for i in np.argsort(distances, kind="stable"):
            pair = piece_pairs[i]
            if site_class == "digital_positive_control" and margins[pair] < 1.0:
                continue
            point = Point(candidates[i])
            clearance = point.distance(junctions)
            if clearance < MIN_JUNCTION_MM:
                continue
            if any(faces[p].boundary.distance(point) > 1e-7 for p in pair):
                raise ValueError("Site does not lie on both adjoining faces")
            sites.append({"id": site_id, "site_class": site_class, "target_uv": list(target),
                          "point_mm": [round(point.x, 7), round(point.y, 7)], "piece_ids": list(pair),
                          "junction_clearance_mm": round(clearance, 3),
                          "distance_from_target_mm": round(float(distances[i]), 3),
                          "pair_projected_lock_margin_mm": round(margins[pair], 5)})
            break
        else:
            raise ValueError(f"No interior seam available near {target}")
    if len({tuple(site["piece_ids"]) for site in sites}) != len(sites):
        raise ValueError("Locator targets selected a repeated pair")
    return stats, sites


def write_map(path, arm, board, faces, sites):
    # A raster seam drawing inside the SVG keeps the physical cut vectors
    # out of the reference file. Text and callouts live outside the cut set.
    width, height = board.bounds[2:]
    scale = 5
    raster = Image.new("RGB", (round(width * scale) + 1, round(height * scale) + 1), "#fbfaf6")
    draw = ImageDraw.Draw(raster)
    selected = {p for site in sites for p in site["piece_ids"]}
    for i, face in enumerate(faces):
        points = [(round(x * scale), round(y * scale)) for x, y in face.exterior.coords]
        if i in selected:
            draw.polygon(points, fill="#c8dfdc")
    for face in faces:
        points = [(round(x * scale), round(y * scale)) for x, y in face.exterior.coords]
        draw.line(points, fill="#596460", width=2)
    pixels = io.BytesIO()
    raster.save(pixels, format="PNG", optimize=True)
    data = base64.b64encode(pixels.getvalue()).decode()
    # Board coordinates use the original top-left origin, with y increasing down.
    labels = []
    for site in sites:
        x, y = site["point_mm"]
        labels.append(f'<g transform="translate({x} {y})"><path d="M3 -3 L8 -8 H11" stroke="#174948" '
                      f'stroke-width="0.8" fill="none"/><circle r="2.1" stroke="#174948" '
                      f'stroke-width="0.9" fill="#f8d878"/><circle r=".55" fill="#203c3c"/>'
                      f'<rect x="10" y="-12" width="12" height="7" rx="1.3" fill="#174948"/>'
                      f'<text x="16" y="-7" text-anchor="middle" fill="white" font-size="4.4">{site["id"]}</text></g>')
    foot = []
    for i, site in enumerate(sites):
        x, y = site["point_mm"]
        first, second = site["piece_ids"]
        foot.append(f'<text x="{8+i*93}" y="404" font-size="4.6" font-weight="bold">{site["id"]}  ·  '
                    f'P{first:03} / P{second:03}</text><text x="{8+i*93}" y="411" font-size="4">'
                    f'x {x:.1f} mm   y {y:.1f} mm</text>')
    path.write_text(f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
 width="384mm" height="417mm" viewBox="0 0 384 417">
 <title>Board {arm} seam locator, reference only</title>
 <desc>Four interior seam sites. Yellow dots mark measurement points on the shared edge of the two tinted pieces.</desc>
 <rect width="384" height="417" fill="#ffffff"/>
 <g font-family="Arial, sans-serif" fill="#1a302e">
 <text x="8" y="10" font-size="7.2" font-weight="bold">Board {arm} · Seam locator</text>
 <text x="376" y="9.5" text-anchor="end" font-size="4.3" font-weight="bold">REFERENCE ONLY · DO NOT CUT</text>
 <text x="8" y="18" font-size="4.3">Measure at each yellow dot. Coordinates in mm from the top-left corner, y increasing downward.</text>
 <g transform="translate(8 24)">
 <image x="0" y="0" width="{width}" height="{height}" xlink:href="data:image/png;base64,{data}"/>
 {''.join(labels)}</g>{''.join(foot)}</g></svg>''', encoding="utf-8")


def build_boards(out_folder: Path) -> dict:
    """Write both original cuts, non-cut site maps and verification.json to out_folder."""
    out_folder = Path(out_folder)
    out_folder.mkdir(parents=True, exist_ok=True)
    report = {"units": "mm", "piece_ids": "Zero-based piece IDs from each run's fabrication-audit.json.",
              "site_selection": "Same normalized targets on each board. Closest eligible segment projection on an "
                                "interior pair at least 4 mm from a junction, in top-left SVG coordinates. S1–S3 "
                                "are spatial samples. S4 additionally requires the pair's projected seam resistance "
                                "to reach 1 mm in every sampled direction; this is a digital positive control.",
              "boards": {}}
    # Stage both arms before publishing any new files, so a bad input is caught first.
    with tempfile.TemporaryDirectory(dir=out_folder) as temp:
        stage = Path(temp)
        for arm, (name, expected_hash) in ARMS.items():
            source = RUNS / name
            cut = stage / f"{arm}-cut.svg"
            cut.write_bytes((source / "aphex-field-puzzle.svg").read_bytes())
            if sha256(cut) != expected_hash:
                raise ValueError(f"Board {arm} source differs from the identified trial cut")
            fields = json.loads((source / "field-report.json").read_text())
            audit = json.loads((source / "fabrication-audit.json").read_text())
            frame = json.loads((source / "frame-lock.json").read_text())
            geom, faces, board = inspect_cut(cut, audit)
            pair_metrics, sites = inspect_seams(faces, board)
            repro = fields["interlock"]["blocking_1mm"]
            if (pair_metrics["adjacent_pairs"], pair_metrics["locked_pairs_at_1mm"]) != (
                    repro["adjacent_pairs"], repro["locked_pairs"]):
                raise ValueError(f"Board {arm} pair metrics differ from its stored report")
            geom.update(pair_metrics)
            locator = stage / f"{arm}-map.svg"
            write_map(locator, arm, board, faces, sites)
            report["boards"][arm] = {
                "source_run": source.relative_to(ROOT).as_posix(), "cut_file": cut.name, "cut_sha256": expected_hash,
                "map_file": locator.name, "map_sha256": sha256(locator), "geometry": geom, "sites": sites,
                "source_report_sha256": {fn: sha256(source / fn) for fn in (
                    "field-report.json", "fabrication-audit.json", "frame-lock.json")},
                "reported_metrics": {
                    "seed": fields["seed"], "bay_rule_strength": fields["partition"]["bay_rule_strength"],
                    "pixel_mm": fields["partition"]["pixel_mm"],
                    "board_inside_silhouettes_percent": fields["board_inside_silhouettes"] * 100,
                    "adjacent_pairs": repro["adjacent_pairs"], "locked_pairs_at_1mm": repro["locked_pairs"],
                    "assembly_lock_margin_unframed_mm": frame["lock_margin_no_frame_mm"],
                    "assembly_lock_margin_framed_mm": frame["lock_margin_with_frame_mm"],
                    "pieces_failing_fabrication_screen": audit["summary"]["pieces_failing_any"],
                },
            }
        (stage / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        for artifact in sorted(stage.iterdir()):
            artifact.replace(out_folder / artifact.name)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "runs/formverk-wood-trial/boards")
    output = parser.parse_args().out
    result = build_boards(output)
    for arm, row in result["boards"].items():
        print(f"{arm}: {row['geometry']['bounded_faces']} pieces, {len(row['sites'])} measured seam locations")
    print(output / "verification.json")
