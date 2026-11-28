"""Build and audit the 32-piece Formverk photo trial. Run from the repository root."""

import argparse
import base64
import hashlib
import io
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dendrite_engine.cli import generate_puzzle_run, parse_whimsy_arg
from dendrite_engine.artwork import read_artwork
from dendrite_engine.phase_field import build_outline_polygon
from dendrite_engine.postprocess import _parse_svg_paths


WIDTH_MM, HEIGHT_MM = 240, 180
PIECES = 32
FIGURES = ("trilobite:0.32,0.43,44,-12", "ammonite:0.69,0.58,40,18")
SEEDS = range(42, 52)
PITCH_MM = 0.75
NECK_MM = 3.0
KERF_MM = 0.15
# Try the same seed on finer/coarser growth grids. The final two attempts
# reduce the procedural displacement, preserving the photo and fossil poses.
GROWTH_SETTINGS = (
    (PITCH_MM, {}),
    (.65, {}),
    (.85, {}),
    (.70, {"U1_mm": 3.4, "U2_mm": 1.5}),
    (.80, {"U1_mm": 3.0, "U2_mm": 1.3}),
)


def check_trial_board(folder, figures, audit, source_pixels):
    """Measure the physical cut paths and engraving against the intended fossils."""
    lines, *_ = _parse_svg_paths(folder / "cut.svg")
    faces = list(polygonize(unary_union(lines)))
    engraving, *_ = _parse_svg_paths(folder / "cut.svg", exclude_groups=("interior_cut", "exterior_cut"))
    matches = {}
    for figure in figures:
        face = max(faces, key=lambda p: p.intersection(figure.polygon).area)
        iou = face.intersection(figure.polygon).area / face.union(figure.polygon).area
        error = face.boundary.hausdorff_distance(figure.polygon.boundary)
        # Half a 0.001 mm SVG unit on both axes amounts to less than 0.00071 mm.
        marks = [LineString(points) for points in figure.engrave_paths]
        retained = sum(any(mark.equals_exact(line, .00071) and face.covers(line)
                           for line in engraving) for mark in marks)
        matches[figure.name] = {
            "outline_iou": iou, "boundary_hausdorff_mm": error,
            "engraving_paths_requested": len(marks), "engraving_paths_retained": retained,
            "passed": iou >= .99 and error <= .5 and retained == len(marks),
        }
    cut = ET.parse(folder / "cut.svg").getroot()
    art = ET.parse(folder / "artwork.svg").getroot()
    same_page = all(cut.get(k) == art.get(k) for k in ("viewBox", "width", "height"))
    photos = art.findall(".//{http://www.w3.org/2000/svg}image")
    photo_in_print = False
    registered_photo = False
    if len(photos) == 1:
        image = photos[0]
        data = image.get("href") or image.get("{http://www.w3.org/1999/xlink}href", "")
        if data.startswith("data:image/png;base64,"):
            with Image.open(io.BytesIO(base64.b64decode(data.partition(",")[2], validate=True))) as embedded:
                photo_in_print = np.array_equal(np.asarray(embedded), source_pixels)
        registered_photo = (
            tuple(float(image.get(k)) for k in ("x", "y", "width", "height")) == (0, 0, WIDTH_MM, HEIGHT_MM)
            and image.get("preserveAspectRatio") == "xMidYMid slice"
        )
    # Unlike the engine's fabrication screen, this trial requires the specified piece count.
    metrics = {k: audit[k] for k in (
        "pieces", "duplicate_segments", "coverage_error_mm2", "overlap_mm2",
        "invalid_pieces", "pieces_with_holes", "kerf_failures", "neck_2_5mm_failures", "neck_3mm_failures",
        "heller_neck_failures", "all_hard_gates_passed",
    )}
    return {
        **metrics, "all_reimported_faces": len(faces), "figures": matches,
        "lock_margin_mm": audit["interlock"]["assembly_lock_margin_mm"],
        "artwork_and_cut_share_page": same_page, "embedded_photo_matches_source_pixels": photo_in_print,
        "centered_full_board_photo": registered_photo,
        "trial_passed": len(faces) == PIECES and audit["all_hard_gates_passed"]
                        and audit["neck_3mm_failures"] == 0
                        and all(m["passed"] for m in matches.values())
                        and same_page and photo_in_print and registered_photo,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artwork", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "runs/formverk-photo-trial")
    args = parser.parse_args()
    photo = read_artwork(args.artwork)
    board = build_outline_polygon("rect", WIDTH_MM, HEIGHT_MM)
    figures = parse_whimsy_arg(FIGURES, board)
    args.output.mkdir(parents=True, exist_ok=True)
    report = {
        "source_name": args.artwork.name, "source_sha256": hashlib.sha256(args.artwork.read_bytes()).hexdigest(),
        "requested_pieces_including_figures": PIECES, "figures": FIGURES, "style": "dendrite",
        "dimensions_mm": [WIDTH_MM, HEIGHT_MM], "pitch_mm": PITCH_MM, "neck_mm": NECK_MM, "kerf_mm": KERF_MM,
        "planned_seeds": list(SEEDS), "required_figure_iou": .99, "allowed_figure_boundary_error_mm": .5,
        "physical_test": "not performed", "results": [], "retry_results": [],
        "planned_growth_settings": [{"pitch_mm": pitch, "overrides": overrides} for pitch, overrides in GROWTH_SETTINGS],
        "passing_folders": {}, "passed_seeds": [],
    }
    for attempt, (pitch, overrides) in enumerate(GROWTH_SETTINGS):
        for seed in SEEDS:
            if seed in report["passed_seeds"]:
                continue
            name = f"seed-{seed}" if attempt == 0 else f"seed-{seed}-retry-{attempt}"
            folder = args.output / name
            try:
                result = generate_puzzle_run(
                    out_dir=folder, style="dendrite", width_mm=WIDTH_MM, height_mm=HEIGHT_MM,
                    num_pieces=PIECES, seed=seed, whimsies=figures, artwork_image=photo,
                    px_mm=pitch, min_neck_mm=NECK_MM, kerf_mm=KERF_MM, overrides=overrides,
                )
                row = check_trial_board(folder, figures, result.audit, photo)
            except Exception as exc:
                # Preserve unsuccessful seeds in the report and finish the predetermined cohort.
                row = {"trial_passed": False, "error": f"{type(exc).__name__}: {exc}"}
            if row["trial_passed"]:
                report["passed_seeds"].append(seed)
                report["passing_folders"][seed] = name
            rows = report["retry_results"] if attempt else report["results"]
            rows.append({"seed": seed, "attempt": attempt, "folder": name, **row})
            tmp = args.output / "trial-report.json.tmp"
            tmp.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
            tmp.replace(args.output / "trial-report.json")
            print(name, "PASS" if row["trial_passed"] else "FAIL", row.get("error", ""), flush=True)
    return 0 if len(report["passed_seeds"]) == len(SEEDS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
