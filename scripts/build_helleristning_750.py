"""End-to-end builder for the 750-piece Exclusive All-Figurative Helleristning Wooden Jigsaw.

Usage:
    .venv\\Scripts\\python.exe scripts/build_helleristning_750.py \\
        --catalog docs/helleristning-750/selected-750-catalog.json \\
        --out runs/helleristning-750
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from heller_jigsaw import __version__
from heller_jigsaw.geometry.export import export_puzzle_run
from heller_jigsaw.geometry.validate import ValidationConfig
from heller_jigsaw.scale750.audit_sparse import (
    fabrication_audit_sparse,
    fidelity_audit_fast,
)
from heller_jigsaw.scale750.lock_repair import (
    clean_micro_segments,
    lock_partition_sparse,
    rebalance_area_ratio,
    repair_necks_and_tips,
)
from heller_jigsaw.scale750.packer_gpu import (
    load_selected_catalog,
    pack_motifs_gpu,
    partition_gpu_750,
)
from heller_jigsaw.scale750.review import (
    build_standalone_review_tool,
    build_weakest_review_packet,
    render_assembly_and_crops,
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_puzzle(
    catalog_path: Path,
    out_dir: Path,
    width_mm: float = 860.0,
    height_mm: float = 570.0,
    seed: int = 750,
    restarts: int = 2,
    px: float = 0.45,
    bay: float = 18.0,
    border_pad_mm: float = 0.8,
    closing_mm: float = 2.2,
    min_neck_mm: float = 3.0,
    max_tip_mm: float = 6.0,
    min_seg_mm: float = 0.05,
    limit: int = 0,
    run_rigid: bool = False,
    weakest_count: int = 123,
) -> dict:
    t0 = time.perf_counter()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_lines: list[str] = []

    def log(msg: str) -> None:
        stamp = f"[{time.perf_counter() - t0:6.1f}s] {msg}"
        print(stamp, flush=True)
        log_lines.append(stamp)

    log(f"Loading catalog {catalog_path}")
    motifs, records, base_dir = load_selected_catalog(catalog_path, enforce_commercial=True)
    if limit > 0:
        motifs = motifs[:limit]
        records = records[:limit]
    n = len(motifs)
    log(f"Loaded {n} verified commercial_ok rock-art motifs (ZERO NC-SA, ZERO filler)")

    # Scale board linearly if testing smaller counts (< 750)
    if limit > 0 and limit < 750 and width_mm == 860.0 and height_mm == 570.0:
        scale_f = (limit / 750.0) ** 0.5
        width_mm = round(width_mm * scale_f, 1)
        height_mm = round(height_mm * scale_f, 1)
        log(f"Auto-scaled board for {limit} motifs -> {width_mm} x {height_mm} mm")

    # 1. GPU High-Density Packing
    layout = pack_motifs_gpu(
        motifs,
        board_width_mm=width_mm,
        board_height_mm=height_mm,
        seed=seed,
        restarts=restarts,
        log=log,
    )

    # 2. GPU Windowed Bay-Aware Eikonal Partition + Organic Edge-Extended Outline
    part, placed_targets = partition_gpu_750(
        layout,
        motifs,
        records,
        px=px,
        bay=bay,
        border_pad_mm=border_pad_mm,
        closing_mm=closing_mm,
        log=log,
    )

    # 3. Pre-lock neck & thin-tip repair
    part, pre_repairs = repair_necks_and_tips(
        part,
        placed_targets,
        min_neck_mm=min_neck_mm,
        max_tip_mm=max_tip_mm,
        preserve_lock_mm=0.0,
        max_passes=6,
        log=log,
    )

    # 4. Sparse 360-deg SCC interlocking (lock_margin_mm >= 3.0 mm)
    part, lock_tabs = lock_partition_sparse(
        part,
        placed_targets,
        threshold_mm=3.45,
        min_neck_mm=min_neck_mm,
        max_tip_mm=max_tip_mm,
        log=log,
    )

    # 5. Post-lock tip/neck repair (preserving >= 3.1 mm pairwise locks)
    part, post_repairs = repair_necks_and_tips(
        part,
        placed_targets,
        min_neck_mm=min_neck_mm,
        max_tip_mm=max_tip_mm,
        preserve_lock_mm=3.10,
        max_passes=5,
        log=log,
    )

    # 5b. Rebalance extreme piece-area outliers (keep max_area / min_area <= 3.85 < 4.0)
    part, area_repairs = rebalance_area_ratio(
        part,
        placed_targets,
        max_ratio=3.85,
        min_neck_mm=min_neck_mm,
        max_tip_mm=max_tip_mm,
        preserve_lock_mm=3.10,
        log=log,
    )

    # 6. Eliminate micro-segments < min_seg_mm while preserving topology
    part = clean_micro_segments(part, min_seg_mm=min_seg_mm, preserve_lock_mm=3.10, log=log)

    # 7. Export authoritative puzzle bundle (cut.svg, pieces.svg, partition.json, quality.json, etc.)
    catalog_sha = _sha256_file(catalog_path)
    bx0, by0, bx1, by1 = part.outline.bounds
    config_payload = {
        "engine": "heller_jigsaw.scale750",
        "version": __version__,
        "edition": "Helleristning 750 Exclusive Wooden Jigsaw",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "seed": seed,
        "num_pieces": n,
        "outline_type": "edge_extended_organic",
        "dimensions_mm": [round(bx1 - bx0, 2), round(by1 - by0, 2)],
        "kerf_mm": 0.15,
        "min_neck_mm": min_neck_mm,
        "max_tip_mm": max_tip_mm,
        "min_segment_mm_gate": min_seg_mm,
        "bay_penalty": bay,
        "eikonal_px_mm": px,
        "corpus_source": str(catalog_path),
        "catalog_sha256": catalog_sha,
        "packing_metrics": layout.metrics,
        "locking_tabs_added": len(lock_tabs),
        "pre_lock_repairs": len(pre_repairs),
        "post_lock_repairs": len(post_repairs),
        "area_rebalances": len(area_repairs),
        "zero_filler": True,
    }
    provenance_payload = {
        "engine": "heller_jigsaw.scale750",
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "corpus_source": str(catalog_path),
        "catalog_sha256": catalog_sha,
        "all_commercial_ok": all(m.commercial_ok for m in motifs),
        "zero_nc_sa_verified": all("non-commercial" not in m.license.lower() and "nc-sa" not in m.license.lower() for m in motifs),
        "commercial_release": "not cleared — requires physical laser test, safety confirmation, and legal rights sign-off",
        "assets": [
            {
                "piece_num": i + 1,
                "asset_id": m.asset_id,
                "family": m.family,
                "motif_class": m.motif_class,
                "site": m.site,
                "country": m.country,
                "source_archive": m.source_archive,
                "creator": m.creator,
                "license": m.license,
                "license_url": m.license_url,
                "commercial_ok": m.commercial_ok,
                "source_url": m.source_url,
                "sha256": m.sha256,
            }
            for i, m in enumerate(motifs)
        ],
    }

    log("Exporting common-line SVG bundle (cut.svg, partition.json, quality.json)...")
    vcfg = ValidationConfig(kerf_mm=0.15, min_neck_mm=min_neck_mm, min_segment_mm=min_seg_mm)
    quality = export_puzzle_run(
        partition=part,
        out_dir=out_dir,
        config_payload=config_payload,
        provenance_payload=provenance_payload,
        log_lines=log_lines,
        target_masks=None,  # Skip 750 slow contact-sheet renders in export_puzzle_run; we generate dedicated high-DPI visuals & review packet
        validation_config=vcfg,
    )
    log(f"Export hard gates passed: {quality['all_hard_gates_passed']}")

    # 8. Sparse fabrication, interlock, translation, thin-feature & fidelity audits
    log("Running full sparse fabrication, 360-deg lock, kerf/neck sweep, and finite-motion audits...")
    audit_report = fabrication_audit_sparse(
        out_dir,
        min_neck_mm=min_neck_mm,
        min_seg_mm=min_seg_mm,
        max_tip_mm=max_tip_mm,
        run_translation=True,
        run_rigid=run_rigid,
        write=True,
    )
    log(
        f"Audit summary: pass={audit_report['digital_screen_pass']} lock={audit_report['lock_margin_mm']}mm "
        f"tip={audit_report['worst_tip_loss_mm']}mm min_seg={audit_report['min_segment_mm']}mm "
        f"trans={audit_report['translation_unblocked_by_distance']}"
    )

    log("Running silhouette fidelity audit across all motifs...")
    fidelity_report = fidelity_audit_fast(out_dir, motifs=motifs, px=0.5, n_angles=48, write=True)
    aligned_targets = json.loads((out_dir / "aligned-targets.json").read_text(encoding="utf-8"))
    thin_report = json.loads((out_dir / "thin-feature-audit.json").read_text(encoding="utf-8"))
    log(
        f"Fidelity summary: mean_iou={fidelity_report['summary']['iou']['mean']:.4f} "
        f"min_iou={fidelity_report['summary']['iou']['minimum']:.4f} "
        f"mean_fid={fidelity_report['summary']['fidelity']['mean']:.4f} "
        f"min_fid={fidelity_report['summary']['fidelity']['minimum']:.4f}"
    )

    # 9. High-res renders, zoomed crops, weakest-pieces packet & standalone HTML review tool
    log("Rendering high-res assembly, zoomed detail crops, weakest review packet, and review_tool.html...")
    render_assembly_and_crops(out_dir, fidelity_report, aligned_targets, audit_report)
    build_weakest_review_packet(out_dir, fidelity_report, thin_report, aligned_targets, count=min(weakest_count, n))
    build_standalone_review_tool(out_dir, fidelity_report, thin_report, audit_report, aligned_targets, catalog_sha256=catalog_sha)

    summary = {
        "out_dir": str(out_dir),
        "elapsed_seconds": round(time.perf_counter() - t0, 1),
        "piece_count": n,
        "dimensions_mm": config_payload["dimensions_mm"],
        "digital_screen_pass": audit_report["digital_screen_pass"],
        "lock_margin_mm": audit_report["lock_margin_mm"],
        "worst_tip_loss_mm": audit_report["worst_tip_loss_mm"],
        "min_segment_mm": audit_report["min_segment_mm"],
        "mean_iou": round(fidelity_report["summary"]["iou"]["mean"], 4),
        "min_iou": round(fidelity_report["summary"]["iou"]["minimum"], 4),
        "mean_fidelity": round(fidelity_report["summary"]["fidelity"]["mean"], 4),
        "min_fidelity": round(fidelity_report["summary"]["fidelity"]["minimum"], 4),
        "zero_filler_verified": audit_report["zero_filler_verified"],
        "all_commercial_ok_flagged": audit_report["all_commercial_ok_flagged"],
    }
    (out_dir / "build-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(f"DONE: {json.dumps(summary)}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Build 750-piece all-figurative Helleristning wooden jigsaw")
    ap.add_argument("--catalog", type=Path, default=Path("docs/helleristning-750/selected-750-catalog.json"))
    ap.add_argument("--out", type=Path, default=Path("runs/helleristning-750"))
    ap.add_argument("--width-mm", type=float, default=860.0)
    ap.add_argument("--height-mm", type=float, default=570.0)
    ap.add_argument("--seed", type=int, default=750)
    ap.add_argument("--restarts", type=int, default=2)
    ap.add_argument("--px", type=float, default=0.45)
    ap.add_argument("--bay", type=float, default=18.0)
    ap.add_argument("--border-pad-mm", type=float, default=0.8)
    ap.add_argument("--closing-mm", type=float, default=2.2)
    ap.add_argument("--min-neck-mm", type=float, default=3.0)
    ap.add_argument("--max-tip-mm", type=float, default=6.0)
    ap.add_argument("--min-seg-mm", type=float, default=0.05)
    ap.add_argument("--limit", type=int, default=0, help="Optional piece limit for fast dev smoke runs")
    ap.add_argument("--rigid", action="store_true", help="Also run single-piece rigid-motion screen")
    ap.add_argument("--weakest-count", type=int, default=123)
    args = ap.parse_args()
    res = build_puzzle(
        catalog_path=args.catalog,
        out_dir=args.out,
        width_mm=args.width_mm,
        height_mm=args.height_mm,
        seed=args.seed,
        restarts=args.restarts,
        px=args.px,
        bay=args.bay,
        border_pad_mm=args.border_pad_mm,
        closing_mm=args.closing_mm,
        min_neck_mm=args.min_neck_mm,
        max_tip_mm=args.max_tip_mm,
        min_seg_mm=args.min_seg_mm,
        limit=args.limit,
        run_rigid=args.rigid,
        weakest_count=args.weakest_count,
    )
    return 0 if res["digital_screen_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
