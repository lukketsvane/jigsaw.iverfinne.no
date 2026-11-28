"""Generate a common rectangular board with each Formverk cut and compare the SVGs."""

import argparse
import csv
import hashlib
import json
import sys
import tempfile
from pathlib import Path

import torch

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dendrite_engine.cli import generate_puzzle_run
from dendrite_engine.phase_field import build_outline_polygon
from dendrite_engine.postprocess import audit_cut_svg
from dendrite_engine.styles import CUT_FAMILIES
from local.technical.cut_plate import write_plate


def compare(folder: Path) -> list[dict]:
    width, height, requested, seed = 200, 160, 14, 42
    neck_mm, kerf_mm = 3.0, 0.15
    outline = build_outline_polygon("rect", width, height)
    rows = []
    for style, family in CUT_FAMILIES.items():
        print(f"Generating {style} in {folder / style}", file=sys.stderr, flush=True)
        settings = {}
        if family == "phase_field":
            settings = {"device": "cpu", "steps": 240}
        elif family == "elastic_rods":
            settings = {"growth_steps": 45}
        result = generate_puzzle_run(
            out_dir=folder / style, style=style, width_mm=width, height_mm=height,
            num_pieces=requested, seed=seed, px_mm=0.8, overrides=settings,
            borderless=False, min_neck_mm=neck_mm, kerf_mm=kerf_mm,
        )
        cut = Path(result.files["cut_svg"])
        audit = audit_cut_svg(cut, expected_outline=outline, min_neck_mm=neck_mm)
        saved = json.loads(Path(result.files["audit_json"]).read_text(encoding="utf-8"))
        if any(saved.get(key) != value for key, value in audit.items()):
            raise ValueError(f"{style}: independent SVG measurements differ from {result.files['audit_json']}")
        row = {
            "style": style, "family": family, "seed": seed, "width_mm": width,
            "height_mm": height, "requested_pieces": requested, "pieces": audit["pieces"],
            "kerf_mm": kerf_mm, "min_neck_mm": neck_mm,
            "phase_steps": settings.get("steps"), "growth_steps": settings.get("growth_steps"),
            "phase_grid_mm": 0.8 if family == "phase_field" else None,
            "cut_length_mm": audit["cut_length_mm"], "coverage_error_mm2": audit["coverage_error_mm2"],
            "duplicate_segments": audit["duplicate_segments"],
            "heller_neck_failures": audit["heller_neck_failures"],
            "assembly_lock_margin_mm": audit["interlock"]["assembly_lock_margin_mm"],
            "all_hard_gates_passed": audit["all_hard_gates_passed"],
            "cut_sha256": hashlib.sha256(cut.read_bytes()).hexdigest(),
        }
        rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-parent", type=Path, help="Parent of the new comparison run directory")
    args = parser.parse_args()
    if args.out_parent:
        args.out_parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="formverk-cuts-", dir=args.out_parent)).resolve()
    torch.set_num_threads(min(4, torch.get_num_threads()))
    rows = compare(folder)
    with (folder / "cut-comparison.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys(), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    write_plate(folder, rows)
    print(folder)


if __name__ == "__main__":
    main()
