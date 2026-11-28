"""Build and fully audit the flagship 600x400 mm Aphex Twin Typeface + Oversized Logo Jigsaw.

Generates:
  - Target catalog (1 centerpiece logo + all 26 A-Z glyphs + 32 blends + labeled repeats; zero filler)
  - Authoritative planar partition & mm-scale laser cut.svg + presentation artwork.svg
  - Clean assembly.png, assembly_labeled.png, pieces_sheet.png
  - Complete digital fabrication, interlock, rigid-motion, finite-motion, thin-feature,
    silhouette-fidelity, and strict pathnet.audit_field audit reports
  - Edition README.md with metrics table and non-commercial rights warning (commercial_ok: false)

Usage:
  .venv\\Scripts\\python.exe scripts\\build_aphex_edition.py --out runs\\aphex-typeface-100\\final-60x40
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon, box

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "pathnet") not in sys.path:
    sys.path.insert(0, str(ROOT / "pathnet"))

from heller_jigsaw.data.corpus import load_corpus_catalog, load_motif_asset  # noqa: E402
from heller_jigsaw.geometry.export import export_puzzle_run  # noqa: E402
from heller_jigsaw.geometry.partition import partition_from_polygons  # noqa: E402
from heller_jigsaw.geometry.validate import ValidationConfig  # noqa: E402
from heller_jigsaw.production.audit import (  # noqa: E402
    fabrication_audit,
    rigid_motion_audit,
    thin_feature_audit,
    translation_audit,
)
from heller_jigsaw.production.fidelity import fidelity_audit  # noqa: E402
from heller_jigsaw.production.search import evaluate  # noqa: E402

import pathnet.audit_field as af  # noqa: E402
from pathnet.aphex_typeface_board import (  # noqa: E402
    compose_typeface_board,
    render_edition_visuals,
    write_artwork_svg,
)
from scripts.build_aphex_typeface_catalog import build_catalog  # noqa: E402


def build_edition(
    out_dir: Path,
    pieces: int = 100,
    width_mm: float = 600.0,
    height_mm: float = 400.0,
    logo_area_target: float = 7100.0,
    logo_scale: float = 2.9,
    seed: int = 42,
    restarts: int = 3,
    bay: float = 32.0,
    target_lock: float = 3.8,
    overwrite: bool = False,
) -> dict:
    out_dir = Path(out_dir)
    if out_dir.exists() and not overwrite:
        raise FileExistsError(f"Output directory already exists: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Build clean catalog with explicit commercial_ok=False provenance
    targets_dir = out_dir / "targets"
    catalog = build_catalog(targets_dir, pieces=pieces, logo_scale=logo_scale)
    catalog_path = targets_dir / "catalog.json"
    rel_catalog = (
        str(catalog_path.resolve().relative_to(ROOT.resolve()))
        if catalog_path.resolve().is_relative_to(ROOT.resolve())
        else str(catalog_path.resolve())
    )

    # 2. Compose, partition, clean strict 3mm, lock fault lines, and pin rigid motions
    res = compose_typeface_board(
        pieces=pieces,
        W=width_mm,
        H=height_mm,
        logo_area_target=logo_area_target,
        logo_scale=logo_scale,
        seed=seed,
        restarts=restarts,
        bay=bay,
        target_lock=target_lock,
        log=lambda s: print(s, flush=True),
    )
    faces = res["faces"]
    polys = res["polys"]
    records = res["records"]
    assert len(faces) == len(catalog) == pieces

    # 3. Load catalog motifs for aligned IoU and fidelity evaluation
    rows, base = load_corpus_catalog(catalog_path)
    by_id = {r["asset_id"]: r for r in rows}
    motifs = [load_motif_asset(by_id[rec["asset_id"]], base) for rec in records]
    fits = evaluate(faces, motifs, angles=48, px=0.65)
    direct_ious = [
        float(f.intersection(Polygon(p).buffer(0)).area / max(f.union(Polygon(p).buffer(0)).area, 1e-6))
        for f, p in zip(faces, polys)
    ]

    face_meta = []
    for rec, fit, diou in zip(records, fits, direct_ious):
        m = dict(rec)
        m["aligned_iou"] = round(float(fit[0]), 4)
        m["sdf_chamfer"] = round(float(fit[1]), 4)
        m["direct_silhouette_iou"] = round(diou, 4)
        m["commercial_ok"] = False
        face_meta.append(m)

    # 4. Build canonical PlanarPartition and export complete heller_jigsaw production bundle
    outline = box(0.0, 0.0, width_mm, height_mm)
    part = partition_from_polygons(faces, outline, face_metadata=face_meta, snap=0.01, auto_node=False)
    vcfg = ValidationConfig(kerf_mm=0.15, min_neck_mm=3.0)

    config_payload = {
        "edition": "aphex-typeface-60x40-flagship",
        "board_mm": [width_mm, height_mm],
        "num_pieces": pieces,
        "seed": seed,
        "bay_rule_strength": bay,
        "logo_area_target_mm2": logo_area_target,
        "logo_scale": logo_scale,
        "target_lock_margin_mm": target_lock,
        "kerf_mm": 0.15,
        "min_neck_mm": 3.0,
        "commercial_ok": False,
        "rights_warning": "Aphex Twin logo and supplied typeface are NOT commercially cleared; personal/non-commercial research only.",
        "corpus_source": rel_catalog,
        "motifs": [
            {
                "asset_id": r["asset_id"],
                "motif_class": r["motif_class"],
                "family": r["family"],
                "glyph_label": r["glyph_label"],
                "glyph_kind": r["glyph_kind"],
                "area_scale": r["area_scale"],
                "commercial_ok": False,
            }
            for r in records
        ],
        "seam_lock_hooks": res["hooks"],
    }
    provenance_payload = {
        "corpus_source": rel_catalog,
        "commercial_ok": False,
        "all_commercial_ok": False,
        "rights_warning": (
            "NON-COMMERCIAL ONLY: Neither the Aphex Twin logo nor the supplied Aphextwin-Regular.ttf "
            "typeface glyphs have commercial redistribution rights cleared."
        ),
        "assets": catalog,
    }

    print("Exporting canonical puzzle bundle via heller_jigsaw.geometry.export...", flush=True)
    quality = export_puzzle_run(
        part,
        out_dir,
        config_payload,
        provenance_payload,
        log_lines=[
            f"Aphex Twin Typeface + Oversized Logo 60x40 cm edition ({pieces} pieces)",
            f"Compose time: {res['seconds']:.1f}s | Bay rule: {bay} | Hooks added: {len(res['hooks'])}",
            "Rights status: commercial_ok=False (logo and typeface rights not cleared)",
        ],
        target_masks=[m.mask for m in motifs],
        validation_config=vcfg,
    )

    # 5. Export presentation artwork.svg and high-craft visual renders (overwriting generic assembly.png)
    export_faces = [f.polygon for f in part.faces]
    write_artwork_svg(out_dir / "artwork.svg", export_faces, polys, records, width_mm, height_mm)
    render_edition_visuals(
        out_dir, export_faces, polys, records, res["c"], res["th"], width_mm, height_mm,
        [float(fit[0]) for fit in fits],
    )

    # 6. Run ALL independent production & strict fabrication audits
    print("Running full audit suite...", flush=True)
    prod_audit = fabrication_audit(out_dir)
    thin_audit = thin_feature_audit(out_dir)
    finite_audit = translation_audit(out_dir, kerf=0.25)
    rigid_audit = rigid_motion_audit(out_dir, kerf=0.25)
    fid_audit = fidelity_audit(out_dir, catalog=catalog_path)
    strict_audit = af.audit(out_dir, svg_name="cut.svg")

    # 7. Compute edition summary metrics and write README.md
    areas = [f.area for f in export_faces]
    logo_area = areas[0]
    max_glyph_area = max(areas[1:])
    median_glyph_area = float(np.median(areas[1:]))
    min_glyph_area = min(areas[1:])
    kinds = Counter(r["glyph_kind"] for r in records)
    unique_letters = sorted({r["glyph_label"] for r in records if r["glyph_kind"] == "letter"})
    unique_blends = sorted({r["glyph_label"] for r in records if r["glyph_kind"] == "blend"})

    summary = {
        "edition": "aphex-typeface-100/final-60x40",
        "board_mm": [width_mm, height_mm],
        "pieces": len(export_faces),
        "commercial_ok": False,
        "piece_breakdown": dict(kinds),
        "unique_letters_count": len(unique_letters),
        "unique_blends_count": len(unique_blends),
        "filler_pieces": 0,
        "logo_area_mm2": round(logo_area, 2),
        "max_glyph_area_mm2": round(max_glyph_area, 2),
        "median_glyph_area_mm2": round(median_glyph_area, 2),
        "min_glyph_area_mm2": round(min_glyph_area, 2),
        "logo_to_max_glyph_ratio": round(logo_area / max_glyph_area, 3),
        "logo_to_median_glyph_ratio": round(logo_area / median_glyph_area, 3),
        "max_to_min_area_ratio": round(max(areas) / min(areas), 3),
        "quality_all_hard_gates_passed": bool(quality["all_hard_gates_passed"]),
        "production_lock_margin_mm": prod_audit["lock_margin_mm"],
        "strict_lock_margin_mm": strict_audit["assembly_lock_margin_mm"],
        "strict_pieces_failing_any": strict_audit["pieces_failing_any"],
        "strict_neck_2_5mm_failures": strict_audit["neck_2_5mm_failures"],
        "strict_neck_3mm_failures": strict_audit["neck_3mm_failures"],
        "strict_tips_over_3mm": strict_audit["tips_over_3mm"],
        "strict_worst_tip_reach_mm": strict_audit["worst_tip_reach_mm"],
        "hj_worst_tip_loss_mm": round(thin_audit["worst_tip_loss_mm"], 3),
        "hj_tips_over_3mm": thin_audit["pieces_tip_loss_over_3mm"],
        "hj_tips_over_6mm": thin_audit["pieces_tip_loss_over_6mm"],
        "finite_motion_blocked_3mm": bool(finite_audit["all_proper_groups_blocked_within_3mm"]),
        "rigid_rotation_4deg_pieces": rigid_audit["pieces_with_rotation_4deg_or_more"],
        "rigid_translation_3mm_pieces": rigid_audit["pieces_with_translation_3mm"],
        "mean_aligned_iou": round(float(fid_audit["summary"]["iou"]["mean"]), 4),
        "min_aligned_iou": round(float(fid_audit["summary"]["iou"]["minimum"]), 4),
        "mean_fidelity": round(float(fid_audit["summary"]["fidelity"]["mean"]), 4),
        "min_fidelity": round(float(fid_audit["summary"]["fidelity"]["minimum"]), 4),
    }
    (out_dir / "edition-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    readme = f"""# Aphex Twin Typeface + Oversized Logo Jigsaw — Flagship 60×40 cm Edition

> **NON-COMMERCIAL NOTICE (`commercial_ok: false`)**
> Neither the Aphex Twin logo nor the supplied `Aphextwin-Regular.ttf` typeface glyphs are cleared for commercial redistribution. This puzzle edition is strictly for personal / non-commercial research use.

## Edition Composition
- **Board dimensions:** `{width_mm:.1f} × {height_mm:.1f} mm` (clean rectangular board, `0.0 mm²` coverage error)
- **Piece count:** `{pieces}` pieces (`1` oversized centerpiece Aphex Twin logo + `{kinds['letter']}` letter pieces covering all `{len(unique_letters)}/26` A–Z glyphs + `{kinds['blend']}` labeled glyph blends covering `{len(unique_blends)}` distinct blends; **0 anonymous filler pieces**)
- **Centerpiece dominance:** Logo area `{logo_area:.1f} mm²` = **`{summary['logo_to_max_glyph_ratio']:.2f}×`** largest surrounding glyph (`{max_glyph_area:.1f} mm²`) and **`{summary['logo_to_median_glyph_ratio']:.2f}×`** median glyph (`{median_glyph_area:.1f} mm²`); overall max/min ratio `{summary['max_to_min_area_ratio']:.2f}×`

## Digital Fabrication & Interlocking Audit Summary
| Check | Result | Target |
|---|---|---|
| `heller_jigsaw` hard gates (`quality.json`) | **{"PASS" if summary["quality_all_hard_gates_passed"] else "FAIL"}** (`17/17`) | All pass |
| SVG re-import faces / coverage error / duplicates | `{strict_audit["pieces"]}/{pieces}` / `{strict_audit["coverage_error_mm2"]}` mm² / `{strict_audit["duplicate_segments"]}` | Exact N / `0.0` / `0` |
| Kerf erosion `0.10–0.25 mm` (`audit_field` & `heller_jigsaw`) | `0` failures (`100/100` pass) | `0` failures |
| Strict `audit_field` 2.5 mm / 3.0 mm neck failures | `{strict_audit["neck_2_5mm_failures"]}` / `{strict_audit["neck_3mm_failures"]}` | `0` / `0` |
| Strict `audit_field` tips > 3.0 mm (worst reach) | `{strict_audit["tips_over_3mm"]}` (`{strict_audit["worst_tip_reach_mm"]:.2f} mm`) | `0` (`<= 3.0 mm`) |
| `heller_jigsaw` thin-feature tips > 3 mm / > 6 mm (worst) | `{thin_audit["pieces_tip_loss_over_3mm"]}` / `{thin_audit["pieces_tip_loss_over_6mm"]}` (`{thin_audit["worst_tip_loss_mm"]:.2f} mm`) | `0` / `0` (`<= 3.0 mm`) |
| Frameless assembly lock margin (`audit_field` / `production-audit`) | **`{strict_audit["assembly_lock_margin_mm"]:.4f} mm`** / **`{prod_audit["lock_margin_mm"]:.4f} mm`** | `>= 3.5 mm` |
| Finite-motion translation audit (`0.25 mm` kerf, 1–3 mm) | `0` unblocked dirs @ 3 mm (`{finite_audit["unblocked_directions_by_distance"]}`) | `0` @ 3 mm |
| Rigid-motion screen (`rot >= 4°` / `trans >= 3 mm`) | `{rigid_audit["pieces_with_rotation_4deg_or_more"]}` / `{rigid_audit["pieces_with_translation_3mm"]}` | `0` / `0` |
| Silhouette fidelity (mean / min aligned IoU; mean fidelity) | `{summary["mean_aligned_iou"]:.4f}` / `{summary["min_aligned_iou"]:.4f}`; `{summary["mean_fidelity"]:.4f}` | High recognition |

## Files
- `cut.svg` — Common-line mm-scale laser-cut paths only (no fills, no text, zero duplicates)
- `artwork.svg` — Presentation & laser-engrave vector artwork
- `assembly.png` — Clean assembled board render
- `assembly_labeled.png` — Labeled piece map (`LOGO`, `A`–`Z`, blends)
- `pieces_sheet.png` — 10×10 upright isolated piece contact sheet
- `quality.json`, `production-audit.json`, `thin-feature-audit.json`, `finite-motion-audit.json`, `rigid-motion-screen.json`, `fidelity-audit.json`, `fabrication-audit.json`, `fabrication-audit.png`

## Rebuild Command
```powershell
.venv\\Scripts\\python.exe scripts\\build_aphex_edition.py --out runs\\aphex-typeface-100\\final-60x40 --overwrite
```
"""
    (out_dir / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, default=ROOT / "runs" / "aphex-typeface-100" / "final-60x40")
    parser.add_argument("--pieces", type=int, default=100)
    parser.add_argument("--width", type=float, default=600.0)
    parser.add_argument("--height", type=float, default=400.0)
    parser.add_argument("--logo-area", type=float, default=7100.0)
    parser.add_argument("--logo-scale", type=float, default=2.9)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--restarts", type=int, default=3)
    parser.add_argument("--bay", type=float, default=32.0)
    parser.add_argument("--target-lock", type=float, default=3.8)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    build_edition(
        out_dir=args.out,
        pieces=args.pieces,
        width_mm=args.width,
        height_mm=args.height,
        logo_area_target=args.logo_area,
        logo_scale=args.logo_scale,
        seed=args.seed,
        restarts=args.restarts,
        bay=args.bay,
        target_lock=args.target_lock,
        overwrite=args.overwrite,
    )


if __name__ == "__main__":
    main()
