"""Command-line interface for `heller_jigsaw`: generate, validate, evaluate, train-encoder, train-shapes."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
from PIL import Image

from . import __version__
from .data.corpus import MotifAsset, make_synthetic_nordic_motifs, select_diverse_motifs
from .geometry.export import export_puzzle_run, rasterize_normalized_piece_mask
from .geometry.outline import circle, from_svg, organic_stone, rectangle
from .geometry.validate import ValidationConfig, validate_run_dir
from .models.recognizer import load_recognizer_checkpoint, predict_silhouette
from .models.shape_prior import sample_motifs
from .models.train_recognizer import train_and_evaluate_recognizer
from .models.train_shapes import train_shapes
from .solver.geodesic import GeodesicSolverConfig, solve_geodesic_puzzle


def _build_outline(args: argparse.Namespace):
    kind = args.outline.lower()
    if kind == "circle":
        return circle(args.width_mm)
    if kind == "rectangle":
        h = args.height_mm if args.height_mm else args.width_mm * 0.80
        return rectangle(args.width_mm, h)
    if kind in ("organic", "stone", "slab"):
        h = args.height_mm if args.height_mm else args.width_mm * 0.84
        return organic_stone(args.width_mm, h, seed=args.seed)
    p = Path(args.outline)
    if p.exists():
        return from_svg(p, width_mm=args.width_mm)
    raise ValueError(f"Unknown outline '{args.outline}' (expected circle, rectangle, organic, or SVG path)")


def score_pieces_with_recognizer(
    piece_masks: Sequence[np.ndarray],
    piece_metadata: Sequence[Dict[str, Any]],
    checkpoint_path: str | Path,
    target_masks: Optional[Sequence[Optional[np.ndarray]]] = None,
    device: str = "cpu",
    tta_rotations: int = 8,
) -> Dict[str, Any]:
    """Score isolated piece masks using a Model A checkpoint (orientation-invariant TTA)."""
    model, meta = load_recognizer_checkpoint(checkpoint_path, device=device)
    families: List[str] = meta["families"]
    classes: List[str] = meta["classes"]
    img_size = int(meta["img_size"])
    temp_f = float(meta.get("temperature_family", 1.0))
    temp_c = float(meta.get("temperature_class", 1.0))
    class_protos = meta.get("class_prototypes")

    pieces_out: List[Dict[str, Any]] = []
    fam_eval = 0
    fam_hits = 0
    cls_eval = 0
    cls_top1_hits = 0
    cls_top3_hits = 0
    proto_top1_hits = 0
    embed_sims: List[float] = []

    for idx, (mask, pmeta) in enumerate(zip(piece_masks, piece_metadata), start=1):
        pred = predict_silhouette(
            model,
            mask,
            families=families,
            classes=classes,
            img_size=img_size,
            tta_rotations=tta_rotations,
            include_flips=True,
            temperature_family=temp_f,
            temperature_class=temp_c,
            class_prototypes=class_protos,
            device=device,
        )
        t_fam = str(pmeta.get("family", ""))
        t_cls = str(pmeta.get("motif_class", ""))
        if t_fam in families:
            fam_eval += 1
            if pred["pred_family"] == t_fam:
                fam_hits += 1
        if t_cls in classes:
            cls_eval += 1
            if pred["pred_class_top1"] == t_cls:
                cls_top1_hits += 1
            if t_cls in pred["pred_class_top3"]:
                cls_top3_hits += 1
            if pred.get("proto_class_top1") == t_cls:
                proto_top1_hits += 1

        rec: Dict[str, Any] = {
            "piece_num": idx,
            "target_family": t_fam,
            "target_class": t_cls,
            "pred_family": pred["pred_family"],
            "pred_family_conf": round(float(pred["pred_family_conf"]), 4),
            "pred_class_top1": pred["pred_class_top1"],
            "pred_class_conf": round(float(pred["pred_class_conf"]), 4),
            "pred_class_top3": pred["pred_class_top3"],
        }
        if "proto_class_top1" in pred:
            rec["proto_class_top1"] = pred["proto_class_top1"]
            rec["max_prototype_sim"] = round(float(pred["max_prototype_sim"]), 4)

        if target_masks is not None and idx - 1 < len(target_masks) and target_masks[idx - 1] is not None:
            t_pred = predict_silhouette(
                model,
                target_masks[idx - 1],
                families=families,
                classes=classes,
                img_size=img_size,
                tta_rotations=tta_rotations,
                include_flips=True,
                temperature_family=temp_f,
                temperature_class=temp_c,
                class_prototypes=class_protos,
                device=device,
            )
            cos_sim = float(np.dot(pred["embedding"], t_pred["embedding"]))
            embed_sims.append(cos_sim)
            rec["embed_target_cos"] = round(cos_sim, 4)
            rec["target_motif_pred_top1"] = t_pred["pred_class_top1"]
            rec["target_motif_pred_top3"] = t_pred["pred_class_top3"]

        pieces_out.append(rec)

    return {
        "checkpoint": str(checkpoint_path),
        "arch_version": int(meta["arch_version"]),
        "tta_rotations": int(tta_rotations),
        "num_pieces": len(pieces_out),
        "family_evaluated": fam_eval,
        "family_top1_match_rate": round(fam_hits / float(max(1, fam_eval)), 4) if fam_eval else None,
        "class_evaluated": cls_eval,
        "class_top1_match_rate": round(cls_top1_hits / float(max(1, cls_eval)), 4) if cls_eval else None,
        "class_top3_match_rate": round(cls_top3_hits / float(max(1, cls_eval)), 4) if cls_eval else None,
        "proto_top1_match_rate": (
            round(proto_top1_hits / float(max(1, cls_eval)), 4)
            if (cls_eval and class_protos is not None)
            else None
        ),
        "mean_embed_target_cos": round(float(np.mean(embed_sims)), 4) if embed_sims else None,
        "pieces": pieces_out,
    }


def cmd_generate(args: argparse.Namespace) -> int:
    outline = _build_outline(args)

    req_classes = [c.strip() for c in args.classes.split(",") if c.strip()] if args.classes else None
    motifs: List[MotifAsset]
    if args.shape_model:
        if args.commercial_only:
            raise ValueError("This shape model supplies motifs with unreviewed commercial status")
        motifs = sample_motifs(args.shape_model, args.pieces, req_classes, args.seed)
        if args.family is not None and any(m.family != args.family for m in motifs):
            raise ValueError(
                f"Shape model produced families {sorted({m.family for m in motifs})}, not requested {args.family!r}"
            )
        corpus_source = str(Path(args.shape_model))
    elif args.synthetic:
        motifs = make_synthetic_nordic_motifs(args.pieces, family=args.family)
        corpus_source = "synthetic"
    else:
        motifs = select_diverse_motifs(
            catalog_path=args.corpus,
            n_pieces=args.pieces,
            seed=args.seed,
            prefer_norway=not args.no_prefer_norway,
            commercial_only=args.commercial_only,
            requested_classes=req_classes,
            family=args.family,
        )
        corpus_source = str(Path(args.corpus))

    sem_weight = float(args.sem_weight) if args.recognizer else 0.0
    scfg = GeodesicSolverConfig(
        opt_grid_max_dim=args.opt_grid,
        final_grid_max_dim=args.final_grid,
        n_steps=args.steps,
        kerf_mm=args.kerf_mm,
        min_neck_mm=args.min_neck_mm,
        recognizer_checkpoint=args.recognizer,
        w_sem=sem_weight,
        device=args.device,
        solver_mode=args.solver_mode,
        n_restarts=args.restarts,
        pack_iters=args.pack_iters,
        smooth_iters=args.smooth_iters,
        seed=args.seed,
    )
    sol = solve_geodesic_puzzle(outline, motifs, config=scfg)

    recognition_report: Optional[Dict[str, Any]] = None
    if args.recognizer:
        piece_masks = [
            (rasterize_normalized_piece_mask(face.polygon, size=256) >= 127.5).astype(np.float32)
            for face in sol.partition.faces
        ]
        recognition_report = score_pieces_with_recognizer(
            piece_masks=piece_masks,
            piece_metadata=[face.metadata for face in sol.partition.faces],
            checkpoint_path=args.recognizer,
            target_masks=[m.mask for m in motifs],
            device=args.device,
        )

    config_payload = {
        "engine": "heller_jigsaw",
        "version": __version__,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "outline_type": args.outline,
        "width_mm": args.width_mm,
        "height_mm": args.height_mm,
        "num_pieces": args.pieces,
        "kerf_mm": args.kerf_mm,
        "min_neck_mm": args.min_neck_mm,
        "opt_grid_max_dim": args.opt_grid,
        "final_grid_max_dim": args.final_grid,
        "steps": args.steps,
        "solver_mode": args.solver_mode,
        "restarts": args.restarts,
        "pack_iters": args.pack_iters,
        "smooth_iters": args.smooth_iters,
        "device": args.device,
        "recognizer_checkpoint": args.recognizer,
        "sem_weight": sem_weight,
        "corpus_source": corpus_source,
        "family": args.family,
        "motifs": [
            {
                "piece_num": i + 1,
                "asset_id": m.asset_id,
                "family": m.family,
                "motif_class": m.motif_class,
                "site": m.site,
                "country": m.country,
                "jigsaw_tier": m.jigsaw_tier,
            }
            for i, m in enumerate(motifs)
        ],
    }

    provenance_payload: Dict[str, Any] = {
        "engine": "heller_jigsaw",
        "version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "corpus_source": corpus_source,
        "all_commercial_ok": all(m.commercial_ok for m in motifs),
        "assets": [
            {
                "piece_num": i + 1,
                "asset_id": m.asset_id,
                "source_archive": m.source_archive,
                "source_group": m.source_group,
                "site": m.site,
                "region": m.region,
                "country": m.country,
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
    if args.shape_model:
        provenance_payload["model_sha256"] = motifs[0].source_group
        manifest = Path(args.shape_model).with_name("training-sources.json")
        if manifest.is_file():
            provenance_payload["dataset_records"] = json.loads(manifest.read_text(encoding="utf-8"))
    if args.recognizer:
        provenance_payload["recognizer_checkpoint"] = str(Path(args.recognizer))

    vcfg = ValidationConfig(kerf_mm=args.kerf_mm, min_neck_mm=args.min_neck_mm)
    quality = export_puzzle_run(
        partition=sol.partition,
        out_dir=args.out,
        config_payload=config_payload,
        provenance_payload=provenance_payload,
        log_lines=sol.logs,
        target_masks=[m.mask for m in motifs],
        validation_config=vcfg,
        recognition_report=recognition_report,
    )

    out_summary: Dict[str, Any] = {
        "out_dir": str(args.out),
        "all_hard_gates_passed": quality["all_hard_gates_passed"],
        "gates": quality["gates"],
        "mean_aligned_iou": round(sum(sol.per_piece_iou) / len(sol.per_piece_iou), 4),
        "min_aligned_iou": min(sol.per_piece_iou),
        "elapsed_sec": round(sol.elapsed_sec, 2),
        "summary": quality["summary"],
    }
    if recognition_report is not None:
        out_summary["recognition"] = {
            k: v for k, v in recognition_report.items() if k != "pieces"
        }
    print(json.dumps(out_summary, indent=2))
    return 0 if quality["all_hard_gates_passed"] else 2


def cmd_validate(args: argparse.Namespace) -> int:
    vcfg = ValidationConfig(kerf_mm=args.kerf_mm, min_neck_mm=args.min_neck_mm)
    report = validate_run_dir(args.run_dir, config=vcfg)
    print(json.dumps(report, indent=2))
    return 0 if report["all_hard_gates_passed"] else 2


def cmd_evaluate(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir)
    vcfg = ValidationConfig(kerf_mm=args.kerf_mm, min_neck_mm=args.min_neck_mm)
    val_report = validate_run_dir(run_dir, config=vcfg)

    part_json = json.loads((run_dir / "partition.json").read_text(encoding="utf-8"))
    faces = part_json.get("faces", [])
    ext_faces = {
        int(e["left_face"]) if int(e["left_face"]) >= 0 else int(e["right_face"])
        for e in part_json.get("edges", [])
        if e.get("is_exterior")
    }
    all_ious, edge_ious, int_ious, chamfers = [], [], [], []
    for f in faces:
        meta = f.get("metadata", {})
        iou = meta.get("aligned_iou")
        ch = meta.get("sdf_chamfer")
        if isinstance(iou, (int, float)):
            all_ious.append(float(iou))
            (edge_ious if int(f["face_id"]) in ext_faces else int_ious).append(float(iou))
        if isinstance(ch, (int, float)):
            chamfers.append(float(ch))

    result: Dict[str, Any] = {
        "run_dir": str(run_dir),
        "all_hard_gates_passed": val_report["all_hard_gates_passed"],
        "failed_gates": [k for k, v in val_report["gates"].items() if not v],
        "num_pieces": len(faces),
        "num_edge_pieces": sum(1 for f in faces if int(f["face_id"]) in ext_faces),
        "num_interior_pieces": sum(1 for f in faces if int(f["face_id"]) not in ext_faces),
        "mean_aligned_iou": round(float(np.mean(all_ious)), 4) if all_ious else None,
        "min_aligned_iou": round(float(np.min(all_ious)), 4) if all_ious else None,
        "edge_mean_aligned_iou": round(float(np.mean(edge_ious)), 4) if edge_ious else None,
        "interior_mean_aligned_iou": round(float(np.mean(int_ious)), 4) if int_ious else None,
        "mean_chamfer": round(float(np.mean(chamfers)), 4) if chamfers else None,
    }

    if args.recognizer:
        piece_masks: List[np.ndarray] = []
        piece_metas: List[Dict[str, Any]] = []
        for idx, f in enumerate(faces, start=1):
            png = run_dir / "pieces" / f"{idx:04d}.png"
            mask = (np.asarray(Image.open(png).convert("L"), dtype=np.float32) >= 127.5).astype(np.float32)
            piece_masks.append(mask)
            piece_metas.append(f.get("metadata", {}))
        result["recognition"] = score_pieces_with_recognizer(
            piece_masks=piece_masks,
            piece_metadata=piece_metas,
            checkpoint_path=args.recognizer,
            device=args.device,
            tta_rotations=args.tta_rotations,
        )

    if args.out:
        out_p = Path(args.out)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(json.dumps(result, indent=2))
    return 0 if val_report["all_hard_gates_passed"] else 2


def cmd_train_encoder(args: argparse.Namespace) -> int:
    metrics = train_and_evaluate_recognizer(
        catalog_path=args.corpus,
        out_dir=args.out,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        img_size=args.img_size,
        seed=args.seed,
        init_from=args.init_from,
        resume=args.resume,
        sampling=args.sampling,
    )
    print(json.dumps(metrics, indent=2))
    return 0


def cmd_compose(args: argparse.Namespace) -> int:
    from .production.search import run

    run(args.catalog, args.out, args.seed, args.mode, args.reference_catalog,
        piece_area=args.piece_area, objective=args.objective, config_set=args.configs,
        bounds_mm=args.bounds_mm)
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    if args.method == "mincost":
        from .production.lock import run
    else:
        from .production.lock_standard import run

    run(args.run_dir, args.out)
    return 0


def cmd_fabrication_audit(args: argparse.Namespace) -> int:
    from .production.audit import fabrication_audit, translation_audit, thin_feature_audit, rigid_motion_audit

    geometry = fabrication_audit(args.run_dir)
    translation = translation_audit(args.run_dir)
    thin = thin_feature_audit(args.run_dir)
    rigid = rigid_motion_audit(args.run_dir) if args.rigid else None
    passed = geometry["geometry_pass"] and geometry["lock_margin_mm"] >= 3
    passed = passed and all(
        geometry[f"kerf_{k:.2f}"]["kerf_pass"] == geometry["n"]
        and geometry[f"kerf_{k:.2f}"]["neck_pass"] == geometry["n"]
        for k in (0.10, 0.15, 0.20, 0.25)
    )
    passed = passed and translation["unblocked_directions_by_distance"]["1.0"] == 0
    passed = passed and thin["worst_tip_loss_mm"] <= args.max_tip_mm
    if rigid is not None:
        passed = passed and rigid["pieces_with_rotation_4deg_or_more"] == 0 and rigid["pieces_with_translation_3mm"] == 0
    print(json.dumps({"digital_screen_pass": bool(passed), "physical_test": "not performed",
                      "commercial_release": "not cleared"}, indent=2))
    return 0 if passed else 2


def cmd_fidelity_audit(args: argparse.Namespace) -> int:
    from .production.fidelity import fidelity_audit

    report = fidelity_audit(args.run_dir, reference_catalog=args.reference_catalog)
    print(json.dumps({"summary": report["summary"], "weakest": [
        {k: r[k] for k in ("piece", "motif_class", "fidelity", "boundary_f", "bay_fill", "extremity_recall")}
        for r in report["weakest"]]}, indent=2))
    return 0


def cmd_benchmark_shapes(args: argparse.Namespace) -> int:
    from .models.benchmark_shapes import benchmark

    report = benchmark(args.corpus, args.models, args.out, args.device)
    print(json.dumps({"selected_model": report["selected_model"], "ranking": report["ranking"]}, indent=2))
    return 0


def cmd_render_previews(args: argparse.Namespace) -> int:
    from .production.preview import render_previews

    print(render_previews(args.run_dir, args.out, args.dpi, args.piece_dpi, args.reference_catalog))
    return 0


def cmd_repair_tips(args: argparse.Namespace) -> int:
    from .production.tips import repair_tips

    repair_tips(args.run_dir, args.out, args.max_tip_mm)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="heller_jigsaw",
        description="Nordic Helleristninger figurative laser-cut jigsaw generator & validator",
    )
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="Generate and validate a figurative helleristninger jigsaw puzzle")
    g.add_argument("--pieces", "-n", type=int, default=8, help="Number of puzzle pieces")
    g.add_argument("--width-mm", type=float, default=250.0, help="Target puzzle width/diameter in mm")
    g.add_argument("--height-mm", type=float, default=None, help="Optional target height in mm")
    g.add_argument("--outline", type=str, default="organic", help="circle | rectangle | organic | <path.svg>")
    g.add_argument("--seed", type=int, default=42, help="Random seed")
    g.add_argument("--corpus", type=str, default="hellerisninger/processed/catalog.json", help="Path to processed catalog.json")
    source = g.add_mutually_exclusive_group()
    source.add_argument("--synthetic", action="store_true", help="Use built-in synthetic Nordic test motifs")
    source.add_argument("--shape-model", help="Sample motifs from a trained conditional SDF VAE (.pt)")
    g.add_argument("--classes", type=str, default=None, help="Optional comma-separated motif class hints")
    g.add_argument("--family", help="Use only motifs from this catalog family, e.g. animal")
    g.add_argument("--commercial-only", action="store_true", help="Restrict to commercial_ok=true motifs")
    g.add_argument("--no-prefer-norway", action="store_true", help="Do not prioritize Norwegian sites")
    g.add_argument("--kerf-mm", type=float, default=0.15, help="Laser kerf simulation width in mm")
    g.add_argument("--min-neck-mm", type=float, default=3.0, help="Minimum finished load-bearing neck in mm")
    g.add_argument("--opt-grid", type=int, default=112, help="Optimization grid resolution (px)")
    g.add_argument("--final-grid", type=int, default=240, help="Final extraction grid resolution (px)")
    g.add_argument("--steps", type=int, default=110, help="Geodesic slowness optimization steps")
    g.add_argument("--recognizer", type=str, default=None, help="Optional Model A recognizer checkpoint (.pt) for guidance & evaluation")
    g.add_argument("--sem-weight", type=float, default=0.35, help="Model A guidance loss weight when --recognizer is set (0.0 = eval only)")
    g.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu", help="Compute device for solver optimization")
    g.add_argument("--solver-mode", choices=["upgraded", "baseline"], default="upgraded", help="Solver variant for generation or ablation")
    g.add_argument("--restarts", type=int, default=8, help="Multi-restart layout candidates (upgraded mode)")
    g.add_argument("--pack-iters", type=int, default=-1, help="Escher SDF blueprint packing steps (-1=auto, 0=off)")
    g.add_argument("--smooth-iters", type=int, default=2, help="Shared-edge smoothing iterations")
    g.add_argument("--out", "-o", type=str, required=True, help="Output run directory")
    g.set_defaults(func=cmd_generate)

    v = sub.add_parser("validate", help="Independently validate an exported puzzle run directory")
    v.add_argument("--run-dir", "-r", type=str, required=True, help="Path to puzzle run directory")
    v.add_argument("--kerf-mm", type=float, default=0.15)
    v.add_argument("--min-neck-mm", type=float, default=3.0)
    v.set_defaults(func=cmd_validate)

    e = sub.add_parser("evaluate", help="Evaluate a puzzle run directory (geometry gates, edge/interior IoU, optional Model A)")
    e.add_argument("--run-dir", "-r", type=str, required=True, help="Path to puzzle run directory")
    e.add_argument("--recognizer", type=str, default=None, help="Optional Model A checkpoint (.pt)")
    e.add_argument("--device", choices=["cpu", "cuda", "auto"], default="cpu")
    e.add_argument("--tta-rotations", type=int, default=8, help="Orientation-invariant TTA rotations for Model A")
    e.add_argument("--kerf-mm", type=float, default=0.15)
    e.add_argument("--min-neck-mm", type=float, default=3.0)
    e.add_argument("--out", "-o", type=str, default=None, help="Optional JSON output report path")
    e.set_defaults(func=cmd_evaluate)

    t = sub.add_parser("train-encoder", help="Train & evaluate Model A silhouette recognizer on leak-free splits")
    t.add_argument("--corpus", type=str, default="hellerisninger/processed/catalog.json")
    t.add_argument("--out", "-o", type=str, default="runs/encoder_baseline")
    t.add_argument("--epochs", type=int, default=24)
    t.add_argument("--batch-size", type=int, default=32)
    t.add_argument("--lr", type=float, default=1.5e-3)
    t.add_argument("--img-size", type=int, default=128)
    t.add_argument("--seed", type=int, default=42)
    start = t.add_mutually_exclusive_group()
    start.add_argument("--init-from", help="Initialize from a recognizer weights file with a fresh optimizer")
    start.add_argument("--resume", action="store_true", help="Restore the last complete epoch in --out")
    t.add_argument("--sampling", choices=["shuffle", "sqrt-balanced"], default="shuffle")
    t.set_defaults(func=cmd_train_encoder)

    s = sub.add_parser("train-shapes", help="Train a conditional generator on animal silhouettes")
    s.add_argument("--corpus", default="hellerisninger/processed/catalog.json")
    s.add_argument("--out", "-o", default="runs/animal-shapes-42")
    s.add_argument("--epochs", type=int, default=120)
    s.add_argument("--batch-size", type=int, default=32)
    s.add_argument("--lr", type=float, default=0.001)
    s.add_argument("--img-size", type=int, default=64)
    s.add_argument("--latent-dim", type=int, default=32)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--resume", action="store_true", help="Restore the last complete epoch in --out")
    s.add_argument("--balanced", action="store_true", help="Train with uniform class sampling and affine augmentation")
    s.add_argument("--checkpoint-seconds", type=float, default=0,
                   help="Minimum seconds between checkpoint writes, with first and final epoch always saved")
    s.add_argument("--width", type=int, default=1, help="Channel multiplier (1 = original architecture)")
    s.add_argument("--stages", type=int, default=3, choices=(3, 4, 5), help="Stride-2 encoder/decoder levels")
    s.add_argument("--kl-weight", type=float, default=0.045)
    s.add_argument("--aug-rotation", type=float, default=0.35, help="Rotation range in radians (--balanced only)")
    s.add_argument("--aug-scale", type=float, default=0.15, help="Scale jitter range (--balanced only)")
    s.add_argument("--all-families", action="store_true", help="Train on all catalog families")
    s.add_argument("--min-train-count", type=int, default=1)
    s.add_argument("--eikonal-weight", type=float, default=0.0)
    s.set_defaults(func=cmd_train_shapes)

    b = sub.add_parser("benchmark-shapes", help="Compare shape models on held-out masks at a common 256 px resolution")
    b.add_argument("--corpus", required=True)
    b.add_argument("--models", nargs="+", required=True, help="Training output folders")
    b.add_argument("--out", required=True)
    b.add_argument("--device", choices=["cpu", "cuda"], default=None)
    b.set_defaults(func=cmd_benchmark_shapes)

    c = sub.add_parser("compose", help="Search free-form layouts from an explicit ordered catalog")
    c.add_argument("--catalog", required=True)
    c.add_argument("--reference-catalog")
    c.add_argument("--seed", type=int, default=42)
    c.add_argument("--mode", type=int, choices=range(4), default=0)
    c.add_argument("--piece-area", type=float, default=1850.0, help="Core motif area in mm^2 before gaps are shared")
    c.add_argument("--objective", choices=["iou", "fidelity"], default="iou",
                   help="Rank partition configurations by aligned IoU (V10) or silhouette fidelity")
    c.add_argument("--configs", choices=["standard", "extended", "bounded"], default="standard",
                   help="Partition search presets. Bounded runs a short search for large catalogs.")
    c.add_argument("--bounds-mm", nargs=2, type=float, metavar=("WIDTH", "HEIGHT"),
                   help="Bounds for target placement before growing the shared outline")
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_compose)

    k = sub.add_parser("lock", help="Construct validated shared-seam undercuts")
    k.add_argument("--run-dir", required=True)
    k.add_argument("--out", required=True)
    k.add_argument("--method", choices=["standard", "mincost"], default="standard")
    k.set_defaults(func=cmd_lock)

    a = sub.add_parser("audit-fabrication", help="Run independent digital fabrication and motion screens")
    a.add_argument("--run-dir", required=True)
    a.add_argument("--max-tip-mm", type=float, default=6.0)
    a.add_argument("--rigid", action="store_true", help="Also sample individual planar rotations and translations")
    a.set_defaults(func=cmd_fabrication_audit)

    rt = sub.add_parser("repair-tips", help="Thicken or trim thin exterior tips of a locked run without moving seams")
    rt.add_argument("--run-dir", required=True)
    rt.add_argument("--out", required=True)
    rt.add_argument("--max-tip-mm", type=float, default=6.0)
    rt.set_defaults(func=cmd_repair_tips)

    fa = sub.add_parser("audit-fidelity", help="Measure silhouette fidelity of each piece against its source mask")
    fa.add_argument("--run-dir", required=True)
    fa.add_argument("--reference-catalog", help="Judge modified targets against these original silhouettes")
    fa.set_defaults(func=cmd_fidelity_audit)

    rp = sub.add_parser("render-previews", help="Render true-scale assembly, exploded and per-piece preview images")
    rp.add_argument("--run-dir", required=True)
    rp.add_argument("--out", help="Output folder (default: <run-dir>/previews)")
    rp.add_argument("--dpi", type=int, default=300)
    rp.add_argument("--piece-dpi", type=int, default=600)
    rp.add_argument("--reference-catalog")
    rp.set_defaults(func=cmd_render_previews)

    return p


def cmd_train_shapes(args: argparse.Namespace) -> int:
    metrics = train_shapes(
        args.corpus,
        args.out,
        epochs=args.epochs,
        batch_size=args.batch_size,
        lr=args.lr,
        size=args.img_size,
        latent_dim=args.latent_dim,
        seed=args.seed,
        resume=args.resume,
        balanced=args.balanced,
        width=args.width,
        stages=args.stages,
        kl_weight=args.kl_weight,
        aug_rotation=args.aug_rotation,
        aug_scale=args.aug_scale,
        families=None if args.all_families else ("animal",),
        min_train_count=args.min_train_count,
        eikonal_weight=args.eikonal_weight,
        checkpoint_seconds=args.checkpoint_seconds,
    )
    print(json.dumps({k: v for k, v in metrics.items() if k != "history"}, indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
