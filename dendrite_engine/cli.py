"""Command-line interface for `dendrite_engine` (`generate`, `train-guide`, `audit`, `serve`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from shapely.geometry import LineString

from .neural_guide import DEFAULT_CHECKPOINT, auto_tune_parameters, compute_artwork_style_fields, train_neural_guide
from .phase_field import DendriteConfig, STYLE_PRESETS, build_outline_polygon, run_dendrite_simulation
from .postprocess import (
    ExportResult,
    audit_cut_svg,
    clean_connected_labels,
    export_puzzle,
    extract_shared_seams_from_labels,
    repair_label_necks,
)
from .rod_growth import RodConfig, run_rod_growth
from .whimsies import (
    WhimsyPlacement,
    build_whimsy_geometry,
    get_builtin_whimsy,
    list_builtin_whimsies,
    load_pathnet_whimsies,
    load_vae_whimsies,
    load_whimsy_from_image,
    load_whimsy_from_svg,
    make_engrave_text_paths,
)


def parse_whimsy_arg(
    raw_items: Sequence[str],
    outline,
) -> List[WhimsyPlacement]:
    """Parse `--whimsy name[:cx_frac,cy_frac,size_mm[,angle_deg]]` specs into placements."""
    x0, y0, x1, y1 = outline.bounds
    w_mm, h_mm = x1 - x0, y1 - y0
    default_size = min(w_mm, h_mm) * 0.24
    placements: List[WhimsyPlacement] = []

    for idx, item in enumerate(raw_items):
        if ":" in item:
            name_part, pos_part = item.split(":", 1)
            vals = [float(v.strip()) for v in pos_part.split(",") if v.strip()]
            cx = x0 + vals[0] * w_mm if len(vals) >= 1 else x0 + (0.32 + 0.36 * (idx % 2)) * w_mm
            cy = y0 + vals[1] * h_mm if len(vals) >= 2 else y0 + (0.35 + 0.30 * (idx // 2)) * h_mm
            sz = vals[2] if len(vals) >= 3 else default_size
            ang = vals[3] if len(vals) >= 4 else 0.0
        else:
            name_part = item.strip()
            cx = x0 + (0.33 + 0.34 * (idx % 2)) * w_mm
            cy = y0 + (0.38 + 0.26 * (idx // 2)) * h_mm
            sz = default_size
            ang = 0.0

        p_check = Path(name_part)
        if name_part.lower().startswith("vae@"):
            ckpt = name_part[4:]
            spec = load_vae_whimsies(ckpt, count=1, seed=42 + idx)[0]
        elif name_part.lower() == "pathnet":
            spec = load_pathnet_whimsies(count=1, seed=22 + idx, logo_only=True)[0]
        elif p_check.is_file() and p_check.suffix.lower() == ".svg":
            spec = load_whimsy_from_svg(p_check)
        elif p_check.is_file():
            spec = load_whimsy_from_image(p_check)
        else:
            spec = get_builtin_whimsy(name_part)

        placements.append(build_whimsy_geometry(spec, (cx, cy), sz, angle_deg=ang))
    return placements


def generate_puzzle_run(
    out_dir: str | Path,
    style: str = "dendrite",
    outline_kind: str = "rect",
    width_mm: float = 260.0,
    height_mm: float = 190.0,
    num_pieces: int = 36,
    seed: int = 42,
    borderless: bool = False,
    whimsies: Sequence[WhimsyPlacement] = (),
    engrave_text: Optional[str] = None,
    engrave_pos_frac: Tuple[float, float] = (0.5, 0.90),
    engrave_height_mm: float = 5.5,
    artwork_image: Optional[np.ndarray] = None,
    auto_tune: bool = False,
    neural_checkpoint: str | Path = DEFAULT_CHECKPOINT,
    theme: Optional[str] = None,
    px_mm: float = 0.50,
    min_neck_mm: float = 3.0,
    kerf_mm: float = 0.15,
    outline_svg: Optional[str | Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> ExportResult:
    """Unified generation entry point supporting Dendrite/Amoeba/Linear/Grid and Maze/Wave."""
    st = style.lower().strip()
    outline = build_outline_polygon(outline_kind, width_mm, height_mm, seed=seed, svg_path=outline_svg)
    x0, y0, x1, y1 = outline.bounds
    w_mm, h_mm = x1 - x0, y1 - y0
    extra = dict(overrides or {})
    chosen_theme = theme or ("walnut" if (st == "maze" and borderless) else "birch")

    # Gather separate engraving lines (whimsies + optional engraved text)
    engrave_lines: List[LineString] = []
    for wh in whimsies:
        for pts in wh.engrave_paths:
            if len(pts) >= 2:
                engrave_lines.append(LineString(pts))
    if engrave_text:
        tx = x0 + engrave_pos_frac[0] * w_mm
        ty = y0 + engrave_pos_frac[1] * h_mm
        for pts in make_engrave_text_paths(engrave_text, (tx, ty), height_mm=engrave_height_mm):
            engrave_lines.append(LineString(pts))

    tune_info: Optional[Dict[str, Any]] = None
    if st in ("maze", "wave"):
        rod_cfg = RodConfig(
            style=st,
            width_mm=w_mm,
            height_mm=h_mm,
            num_pieces=num_pieces,
            borderless=bool(borderless and st == "maze"),
            seed=seed,
            min_neck_mm=min_neck_mm,
            kerf_mm=kerf_mm,
            **{k: v for k, v in extra.items() if hasattr(RodConfig, k) and v is not None},
        )
        rres = run_rod_growth(outline, rod_cfg, whimsies=whimsies)
        meta = {
            "engine": "cosserat_rod_growth",
            "style": st,
            "borderless": rod_cfg.borderless,
            "outline": outline_kind,
            "seed": seed,
            "num_whimsies": len(whimsies),
            "whimsy_names": [w.name for w in whimsies],
            "min_nonadjacent_clearance_mm": rres.min_nonadjacent_clearance_mm,
            "elapsed_sec": round(rres.elapsed_sec, 2),
        }
        return export_puzzle(
            out_dir=out_dir,
            faces=rres.faces,
            interior_lines=rres.interior_lines,
            exterior_lines=rres.exterior_lines,
            engrave_lines=engrave_lines,
            expected_outline=rres.expected_outline,
            whimsy_indices=rres.whimsy_indices,
            kerf_mm=kerf_mm,
            min_neck_mm=min_neck_mm,
            theme=chosen_theme,
            metadata=meta,
        )

    # Phase-field styles: dendrite, amoeba, linear, grid
    if auto_tune:
        tune_info = auto_tune_parameters(w_mm, h_mm, num_pieces, style=st, checkpoint=neural_checkpoint, seed=seed)
        for k, v in tune_info["recommended"].items():
            extra.setdefault(k, v)

    cfg = DendriteConfig.from_style(
        st,
        width_mm=w_mm,
        height_mm=h_mm,
        num_pieces=num_pieces,
        seed=seed,
        px_mm=px_mm,
        min_neck_mm=min_neck_mm,
        kerf_mm=kerf_mm,
        **{k: v for k, v in extra.items() if hasattr(DendriteConfig, k) and v is not None},
    )

    gamma_map = amp_map = None
    if artwork_image is not None:
        h_px = max(32, int(round(h_mm / cfg.px_mm)))
        w_px = max(32, int(round(w_mm / cfg.px_mm)))
        gamma_map, amp_map = compute_artwork_style_fields(artwork_image, (h_px, w_px), base_gamma=cfg.gamma)

    dres = run_dendrite_simulation(
        outline,
        cfg,
        whimsies=whimsies,
        gamma_map=gamma_map,
        amplitude_map=amp_map,
    )
    lab, stray_px = clean_connected_labels(dres.labels, dres.domain_mask, fixed_mask=dres.whimsy_mask)
    lab, neck_moved_px = repair_label_necks(
        lab,
        dres.domain_mask,
        dres.px_mm,
        min_neck_mm=min_neck_mm,
        kerf_mm=kerf_mm,
        fixed_mask=dres.whimsy_mask,
    )

    faces, int_lines, ext_lines, vec_info = extract_shared_seams_from_labels(
        lab,
        dres.bounds_mm,
        outline,
        phase_fields=dres.phase_fields,
        piece_to_color=dres.piece_to_color,
        fixed_piece_ids=dres.fixed_piece_ids,
        whimsies=whimsies,
        smooth_sigma_mm=0.65,
        simplify_tol_mm=0.07,
    )

    # Identify whimsy indices in final ordered faces (exact pid order matches fixed_piece_ids)
    unique_pids = sorted(int(v) for v in np.unique(lab) if v >= 0)
    fixed_set = set(dres.fixed_piece_ids)
    whimsy_indices = [i for i, pid in enumerate(unique_pids) if pid in fixed_set]

    meta = {
        "engine": "multiphase_allen_cahn_gpu",
        "style": st,
        "outline": outline_kind,
        "seed": seed,
        "device": dres.device,
        "num_phase_colors": dres.num_colors,
        "coloring_distance": cfg.coloring_distance,
        "conservation_error_max": round(dres.conservation_error_max, 6),
        "stray_px_reassigned": stray_px,
        "neck_px_reassigned": neck_moved_px,
        "smoothing_mm": vec_info["smoothing_mm"],
        "num_whimsies": len(whimsies),
        "whimsy_names": [w.name for w in whimsies],
        "auto_tune": tune_info,
        "elapsed_sec": round(dres.elapsed_sec, 2),
    }
    return export_puzzle(
        out_dir=out_dir,
        faces=faces,
        interior_lines=int_lines,
        exterior_lines=ext_lines,
        engrave_lines=engrave_lines,
        expected_outline=outline,
        whimsy_indices=whimsy_indices,
        kerf_mm=kerf_mm,
        min_neck_mm=min_neck_mm,
        theme=chosen_theme,
        metadata=meta,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dendrite_engine", description="GPU Dendritic & Cosserat Rod Puzzle Generator")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="Generate a laser-ready dendritic, amoeba, maze, or wave puzzle")
    g.add_argument("--style", choices=["dendrite", "amoeba", "linear", "grid", "maze", "wave"], default="dendrite")
    g.add_argument("--outline", default="rect", help="rect, circle, organic, or path to SVG outline")
    g.add_argument("--width-mm", type=float, default=260.0)
    g.add_argument("--height-mm", type=float, default=190.0)
    g.add_argument("--pieces", type=int, default=36)
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--borderless", action="store_true", help="Borderless organic perimeter (Maze style)")
    g.add_argument("--whimsy", action="append", default=[], help=f"Whimsy spec name[:x,y,size_mm,deg] ({', '.join(list_builtin_whimsies())}, pathnet, vae@ckpt, or file)")
    g.add_argument("--engrave-text", default=None)
    g.add_argument("--engrave-pos", default="0.5,0.90", help="Fractional x,y position for engraved text")
    g.add_argument("--engrave-height-mm", type=float, default=5.5)
    g.add_argument("--artwork", type=Path, default=None, help="Optional artwork image for saliency-aware cut modulation")
    g.add_argument("--auto-tune", action="store_true", help="Optimize PDE params with trained DendriteSurrogateNet")
    g.add_argument("--px-mm", type=float, default=0.50)
    g.add_argument("--steps", type=int, default=None)
    g.add_argument("--gamma", type=float, default=None)
    g.add_argument("--min-neck-mm", type=float, default=3.0)
    g.add_argument("--kerf-mm", type=float, default=0.15)
    g.add_argument("--theme", choices=["birch", "walnut"], default=None)
    g.add_argument("--out", "-o", type=Path, required=True)

    t = sub.add_parser("train-guide", help="Train GPU DendriteSurrogateNet on simulated puzzle patches")
    t.add_argument("--samples", type=int, default=120)
    t.add_argument("--epochs", type=int, default=260)
    t.add_argument("--seed", type=int, default=42)
    t.add_argument("--out", "-o", type=Path, default=Path("runs/dendrite-engine"))

    a = sub.add_parser("audit", help="Independently audit a cut.svg file")
    a.add_argument("cut_svg", type=Path)
    a.add_argument("--min-neck-mm", type=float, default=3.0)

    s = sub.add_parser("serve", help="Launch local Formverk Studio web server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        outline = build_outline_polygon(args.outline, args.width_mm, args.height_mm, seed=args.seed)
        wh_list = parse_whimsy_arg(args.whimsy, outline) if args.whimsy else []
        art = None
        if args.artwork is not None:
            bgr = cv2.imread(str(args.artwork), cv2.IMREAD_COLOR)
            if bgr is not None:
                art = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        ex, ey = (float(v) for v in args.engrave_pos.split(","))
        overrides: Dict[str, Any] = {}
        if args.steps is not None:
            overrides["steps"] = args.steps
            overrides["growth_steps"] = args.steps
        if args.gamma is not None:
            overrides["gamma"] = args.gamma
        res = generate_puzzle_run(
            out_dir=args.out,
            style=args.style,
            outline_kind=args.outline,
            width_mm=args.width_mm,
            height_mm=args.height_mm,
            num_pieces=args.pieces,
            seed=args.seed,
            borderless=args.borderless,
            whimsies=wh_list,
            engrave_text=args.engrave_text,
            engrave_pos_frac=(ex, ey),
            engrave_height_mm=args.engrave_height_mm,
            artwork_image=art,
            auto_tune=args.auto_tune,
            theme=args.theme,
            px_mm=args.px_mm,
            min_neck_mm=args.min_neck_mm,
            kerf_mm=args.kerf_mm,
            overrides=overrides,
        )
        summary = {k: v for k, v in res.audit.items() if k != "per_piece"}
        print(json.dumps(summary, indent=2))
        return 0 if res.audit["all_hard_gates_passed"] else 2

    if args.command == "train-guide":
        rep = train_neural_guide(out_dir=args.out, num_samples=args.samples, epochs=args.epochs, seed=args.seed)
        print(json.dumps({k: v for k, v in rep.items() if k != "history"}, indent=2))
        return 0

    if args.command == "audit":
        rep = audit_cut_svg(args.cut_svg, min_neck_mm=args.min_neck_mm)
        print(json.dumps({k: v for k, v in rep.items() if k != "per_piece"}, indent=2))
        return 0 if rep["all_hard_gates_passed"] else 2

    if args.command == "serve":
        from .server import serve_studio

        serve_studio(host=args.host, port=args.port)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
