"""Generate one seeded Wave puzzle, re-audit its SVG and inspect the exported files."""

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dendrite_engine.cli import generate_puzzle_run
from dendrite_engine.phase_field import build_outline_polygon
from dendrite_engine.postprocess import audit_cut_svg


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-parent", type=Path,
        help="Create a fresh run below this directory (defaults to the system temporary directory)",
    )
    args = parser.parse_args()
    if args.out_parent is not None:
        args.out_parent.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="wave-geometry-", dir=args.out_parent)).resolve()
    print(f"Generating in {run_dir}", file=sys.stderr, flush=True)

    width, height, count = 200.0, 160.0, 14
    result = generate_puzzle_run(
        out_dir=run_dir,
        style="wave",
        outline_kind="rect",
        width_mm=width,
        height_mm=height,
        num_pieces=count,
        seed=42,
        overrides={"growth_steps": 45},
        min_neck_mm=3.0,
        kerf_mm=0.15,
        engrave_text="WAVE",
    )
    files = {key: Path(name) for key, name in result.files.items()}
    cut = ET.parse(files["cut_svg"]).getroot()
    art = ET.parse(files["artwork_svg"]).getroot()
    if any(cut.get(key) != art.get(key) for key in ("viewBox", "width", "height")):
        raise ValueError("Artwork and cut paths use different coordinate systems")
    if list(map(float, cut.attrib["viewBox"].split())) != [0, 0, width, height]:
        raise ValueError("Unexpected board dimensions in the cut SVG")
    for attr, expected in (("width", width), ("height", height)):
        value = cut.attrib[attr]
        if not value.endswith("mm") or float(value[:-2]) != expected:
            raise ValueError(f"Unexpected physical {attr} in the cut SVG: {value}")
    layers = {g.get("id"): len(g.findall("{*}path")) for g in cut.findall("{*}g")}
    if any(layers.get(key, 0) == 0 for key in ("interior_cut", "exterior_cut", "engrave")):
        raise ValueError(f"Missing cut or engraving paths: {layers}")

    outline = build_outline_polygon("rect", width, height)
    audit = audit_cut_svg(files["cut_svg"], expected_outline=outline, min_neck_mm=3.0)
    saved = json.loads(files["audit_json"].read_text(encoding="utf-8"))
    if any(saved.get(key) != value for key, value in audit.items()):
        raise ValueError("The independently parsed SVG and the saved audit disagree")
    player = json.loads(files["puzzle_json"].read_text(encoding="utf-8"))
    if not audit["all_hard_gates_passed"] or (audit["pieces"], len(player["pieces"])) != (count, count):
        raise ValueError(f"Expected {count} audited and playable pieces; inspect {files['audit_json']}")

    images = {}
    for key in ("preview_png", "pieces_sheet_png"):
        with Image.open(files[key]) as image:
            image.load()
            low, high = image.convert("L").getextrema()
            if image.format != "PNG" or low == high:
                raise ValueError(f"Expected a nonuniform PNG in {files[key]}")
            images[files[key].name] = list(image.size)

    metadata = saved["metadata"]
    summary = {
        "run_dir": str(run_dir),
        "pieces": audit["pieces"],
        "paths_by_layer": layers,
        "cut_length_mm": audit["cut_length_mm"],
        "coverage_error_mm2": audit["coverage_error_mm2"],
        "assembly_lock_margin_mm": audit["interlock"]["assembly_lock_margin_mm"],
        "clearance_stage": metadata.get("clearance_stage", "legacy_unlabelled"),
        "min_nonadjacent_clearance_mm": metadata["min_nonadjacent_clearance_mm"],
        "all_hard_gates_passed": audit["all_hard_gates_passed"],
        "image_sizes_px": images,
        "cut_sha256": hashlib.sha256(files["cut_svg"].read_bytes()).hexdigest(),
    }
    if "last_collision_sweep_min_mm" in metadata:
        summary["last_collision_sweep_min_mm"] = metadata["last_collision_sweep_min_mm"]
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
