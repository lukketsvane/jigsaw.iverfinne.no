"""End-to-end builder for the 750-Piece Dendrite + Trilobites + Helleristninger Wooden Jigsaw.

Usage:
    .venv\\Scripts\\python.exe scripts/build_dendrite_trilobite_750.py
    .venv\\Scripts\\python.exe scripts/build_dendrite_trilobite_750.py --pieces 750 --trilobites 150 --output runs/dendrite-trilobite-750
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from heller_jigsaw.geometry.partition import extract_partition_from_labels
from heller_jigsaw.trilobite_dendrite.audit_and_lock import (
    build_and_audit_trilobite_engraving,
    clean_partition_micro_segments,
    lock_dendritic_board,
    repair_weak_pieces_750,
    run_full_750_audit,
)
from heller_jigsaw.trilobite_dendrite.catalog import curate_750_motifs
from heller_jigsaw.trilobite_dendrite.compose import compose_750_board
from heller_jigsaw.trilobite_dendrite.dendrite_seams import DendriteConfig, evolve_dendritic_seams_gpu
from heller_jigsaw.trilobite_dendrite.export_and_render import export_dendrite_trilobite_bundle


def build_puzzle(
    out_dir: Path,
    n_pieces: int = 750,
    n_trilobites: int = 150,
    board_width_mm: float = 890.0,
    board_height_mm: float = 650.0,
    base_area_mm2: float = 720.0,
    px: float = 0.30,
    seed: int = 2026,
    pack_steps: int = 280,
    dendrite_steps: int = 180,
    allow_share_alike: bool = False,
) -> dict:
    """Run the full 7-stage GPU build, dendritic evolution, locking, audit, and export pipeline."""
    t_start = time.perf_counter()
    log = lambda msg: print(msg, flush=True)
    log(f"=== Building {n_pieces}-Piece Dendrite + Trilobites ({n_trilobites}) + Helleristninger ({n_pieces - n_trilobites}) ===")

    # Stage 1: Curate commercial-clean all-figurative motifs (0 Alta NC-SA, 0 filler!)
    motifs, rights_summary = curate_750_motifs(
        catalog_path=ROOT / "hellerisninger" / "processed" / "catalog.json",
        n_total=n_pieces,
        n_trilobites=n_trilobites,
        seed=seed,
        base_area_mm2=base_area_mm2,
        allow_share_alike=allow_share_alike,
    )
    log(
        f"[stage 1] Curated {len(motifs)} motifs: {rights_summary['trilobite_count']} trilobites + "
        f"{rights_summary['helleristning_count']} helleristninger | alta_nc_sa={rights_summary['alta_nc_sa_count']} | "
        f"share_alike={rights_summary['share_alike_count']}"
    )

    # Stage 2: GPU rigid-pose packing + organic rock-slab outline + high-res windowed Eikonal partition
    composed = compose_750_board(
        motifs=motifs,
        board_width_mm=board_width_mm,
        board_height_mm=board_height_mm,
        px=px,
        seed=seed,
        pack_steps=pack_steps,
        use_motif_hugging_outline=True,
        log=log,
    )

    # Stage 3: Corridor-constrained GPU Multiphase Allen-Cahn Dendritic Seam Evolution
    dcfg = DendriteConfig(steps=dendrite_steps, seed=seed)
    labels_den, dendrite_metrics = evolve_dendritic_seams_gpu(
        labels_init=composed.labels,
        inside=composed.inside,
        phi_own=composed.phi_own,
        dist_second=composed.dist_second,
        piece_kinds=[m.kind for m in motifs],
        px=px,
        cfg=dcfg,
        log=log,
    )

    # Stage 4: Extract canonical single-pass common-line PlanarPartition
    t_ext = time.perf_counter()
    face_meta = [dict(m.metadata) for m in motifs]
    partition = extract_partition_from_labels(
        labels_inside=labels_den,
        outline=composed.outline,
        px=px,
        n_pieces=n_pieces,
        face_metadata=face_meta,
        smooth_iters=3,
        simplify_tol_mm=0.16,
    )
    partition = clean_partition_micro_segments(partition, min_seg_mm=0.065)
    log(
        f"[stage 4] Extracted exact PlanarPartition: {partition.num_pieces} faces, "
        f"{len(partition.interior_edges)} interior cuts, {len(partition.exterior_edges)} exterior cuts "
        f"({time.perf_counter() - t_ext:.1f}s)"
    )

    # Stage 5: Thin-tip / neck pre-repair -> Organic Dendritic 360-deg Seam Locking -> final tip check
    partition, pre_repair = repair_weak_pieces_750(partition, composed.placed_silhouettes, max_tip_mm=5.75, log=log)
    if pre_repair["remaining_weak_count"] > 0:
        partition, pre_repair2 = repair_weak_pieces_750(partition, composed.placed_silhouettes, max_tip_mm=5.85, log=log)
        pre_repair["pass2"] = pre_repair2
    partition, lock_report = lock_dendritic_board(
        partition=partition,
        placed_silhouettes=composed.placed_silhouettes,
        piece_kinds=[m.kind for m in motifs],
        target_lock_margin_mm=3.25,
        max_tip_mm=5.75,
        seed=seed,
        log=log,
    )
    partition, post_repair = repair_weak_pieces_750(partition, composed.placed_silhouettes, max_tip_mm=5.85, log=log)
    if post_repair["repaired_count"] > 0:
        partition, lock_report2 = lock_dendritic_board(
            partition=partition,
            placed_silhouettes=composed.placed_silhouettes,
            piece_kinds=[m.kind for m in motifs],
            target_lock_margin_mm=3.15,
            max_tip_mm=5.85,
            seed=seed + 11,
            log=log,
        )
        lock_report["post_repair_relock"] = lock_report2
    partition = clean_partition_micro_segments(partition, min_seg_mm=0.06)

    # Stage 6: Map & verify strictly inset (>= 1.50 mm) trilobite anatomical engraving + Full 750-pc Audit
    engrave_by_face, engrave_audit = build_and_audit_trilobite_engraving(
        partition=partition,
        motifs=motifs,
        poses=composed.poses,
        min_clearance_mm=1.50,
    )
    log(
        f"[stage 6] Trilobite engraving: {engrave_audit['passed_count']}/{engrave_audit['trilobite_count']} passed | "
        f"global_min_clearance={engrave_audit['global_min_clearance_mm']} mm | strokes={engrave_audit['total_engrave_strokes']}"
    )

    prod_audit, val_report = run_full_750_audit(
        partition=partition,
        motifs=motifs,
        placed_silhouettes=composed.placed_silhouettes,
        poses=composed.poses,
        engrave_audit=engrave_audit,
        log=log,
    )

    # Stage 7: Dual-layer SVG export, independent SVG re-import check, and high-res visual renders
    config_payload = {
        "edition": "dendrite-trilobite-750",
        "num_pieces": n_pieces,
        "num_trilobites": n_trilobites,
        "num_helleristninger": n_pieces - n_trilobites,
        "seed": seed,
        "px_mm": px,
        "kerf_mm": 0.15,
        "min_neck_mm": 3.0,
        "min_engrave_clearance_mm": 1.50,
        "compose_stats": composed.stats,
        "dendrite_metrics": dendrite_metrics,
        "pre_repair": pre_repair,
        "lock_report": lock_report,
        "post_repair": post_repair,
        "total_build_seconds": round(time.perf_counter() - t_start, 2),
    }

    final_audit = export_dendrite_trilobite_bundle(
        out_dir=out_dir,
        partition=partition,
        motifs=motifs,
        placed_silhouettes=composed.placed_silhouettes,
        engrave_by_face=engrave_by_face,
        engrave_audit=engrave_audit,
        rights_summary=rights_summary,
        prod_audit=prod_audit,
        val_report=val_report,
        config_payload=config_payload,
        log=log,
    )

    log(f"=== FINISHED in {time.perf_counter() - t_start:.1f}s | ALL PRODUCTION GATES PASSED: {final_audit['all_production_gates_passed']} ===")
    print(json.dumps(final_audit["gates"], indent=2), flush=True)
    return final_audit


def main() -> None:
    parser = argparse.ArgumentParser(description="Build 750-Piece Dendrite + Trilobites + Helleristninger Jigsaw")
    parser.add_argument("--pieces", type=int, default=750)
    parser.add_argument("--trilobites", type=int, default=150)
    parser.add_argument("--width", type=float, default=880.0)
    parser.add_argument("--height", type=float, default=640.0)
    parser.add_argument("--area", type=float, default=600.0)
    parser.add_argument("--px", type=float, default=0.30)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--pack-steps", type=int, default=300)
    parser.add_argument("--dendrite-steps", type=int, default=220)
    parser.add_argument("--allow-share-alike", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "runs" / "dendrite-trilobite-750")
    args = parser.parse_args()

    build_puzzle(
        out_dir=args.output,
        n_pieces=args.pieces,
        n_trilobites=args.trilobites,
        board_width_mm=args.width,
        board_height_mm=args.height,
        base_area_mm2=args.area,
        px=args.px,
        seed=args.seed,
        pack_steps=args.pack_steps,
        dendrite_steps=args.dendrite_steps,
        allow_share_alike=args.allow_share_alike,
    )


if __name__ == "__main__":
    main()
