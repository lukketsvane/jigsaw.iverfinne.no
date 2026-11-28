"""Command-line interface for `dendrite_engine` (`generate`, `train-guide`, `audit`, `serve`)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from shapely.geometry import LineString

from .artwork import read_artwork
from .design_art import ART_PATTERNS, photo_board, render_pattern
from .styles import CUT_FAMILIES
from .solvers import solve_cut
from .neural_guide import DEFAULT_CHECKPOINT, train_neural_guide
from .phase_field import build_outline_polygon
from .postprocess import (
    ExportResult,
    audit_cut_svg,
    export_puzzle,
)
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
    place_whimsies,
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
        name_part, sep, pos_part = item.rpartition(":")
        if sep and name_part.lower() != "glyph" and pos_part.split(",")[0].strip().replace(".", "", 1).lstrip("+-").isdigit():
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
    title: str = "",
    reverse_text: Optional[str] = None,
    back_artwork: Optional[np.ndarray] = None,
    artwork_pattern: str = "wood",
    auto_place_whimsies: bool = False,
    board_outline=None,
    regions=None,
) -> ExportResult:
    """Generate a puzzle using a shared cut, print and fabrication audit pipeline."""
    st = style.lower().strip()
    if st not in CUT_FAMILIES or artwork_pattern not in ART_PATTERNS:
        raise ValueError("Unknown cut style or artwork pattern.")
    if not np.isfinite([width_mm,height_mm,kerf_mm,min_neck_mm,px_mm]).all() or min(width_mm,height_mm,px_mm,min_neck_mm) <= 0 or kerf_mm < 0:
        raise ValueError("Dimensions and neck width must be positive, and kerf nonnegative.")
    if num_pieces < len(whimsies)+2:
        raise ValueError("Allow at least two ordinary pieces alongside the selected figures.")
    outline = board_outline if board_outline is not None else build_outline_polygon(outline_kind, width_mm, height_mm, seed=seed, svg_path=outline_svg)
    whimsies = place_whimsies(outline, whimsies, seed, max(kerf_mm,min_neck_mm*.5), auto_place_whimsies)
    if artwork_image is None and artwork_pattern != "wood":
        artwork_image = render_pattern(artwork_pattern, outline, seed)
    elif artwork_image is not None:
        artwork_pattern = "photo"
    x0, y0, x1, y1 = outline.bounds
    w_mm, h_mm = x1 - x0, y1 - y0
    chosen_theme = theme or ("walnut" if (st == "maze" and borderless) else "birch")
    design = {"title": title, "artwork_pattern": artwork_pattern, "requested_pieces": num_pieces,
              "style": st, "outline": outline_kind, "seed": seed, "num_whimsies": len(whimsies),
              "whimsy_names": [w.name for w in whimsies],
              "placements": [{"name": w.name, "x_frac": (w.center_mm[0]-x0)/w_mm,
                             "y_frac": (w.center_mm[1]-y0)/h_mm, "size_mm": w.size_mm,
                             "angle_deg": w.angle_deg} for w in whimsies]}
    surface_options = dict(artwork_image=artwork_image, reverse_text=reverse_text, back_artwork=back_artwork)

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

    solve_options = dict(whimsies=whimsies, px_mm=px_mm, kerf_mm=kerf_mm, min_neck_mm=min_neck_mm,
                         auto_tune=auto_tune, neural_checkpoint=neural_checkpoint, outline_kind=outline_kind,
                         artwork_image=artwork_image, overrides=overrides)
    if regions is None:
        geometry = solve_cut(outline, st, num_pieces, seed, borderless=borderless, **solve_options)
    else:
        from .regions import solve_regions
        if borderless:
            raise ValueError("Mixed cuts use the supplied board perimeter.")
        geometry = solve_regions(outline, regions, num_pieces, seed, **solve_options)
    return export_puzzle(out_dir, geometry.faces, geometry.interior_lines, geometry.exterior_lines,
                         engrave_lines=engrave_lines, expected_outline=geometry.expected_outline,
                         whimsy_indices=geometry.whimsy_indices, kerf_mm=kerf_mm, min_neck_mm=min_neck_mm,
                         theme=chosen_theme, metadata={**design, **geometry.metadata}, **surface_options)


EDITIONS = {
    "infinity": "Generate a torus, Klein bottle, projective plane or spherical puzzle",
    "spectre": "Generate a patch of the aperiodic Spectre monotile",
    "polyhex": "Generate or verify a four-colour polyhex challenge",
    "dual": "Cut a rectangular puzzle with a second disc assembly",
    "eclipse": "Rearrange reversible pieces into solar, lunar and eclipse configurations",
    "nested": "Generate connected groups of miniature puzzles",
    "chess": "Generate a chessboard with a fitted storage border and playing pieces",
    "parts": "Recut saved piece IDs or prepare individually rotated stock",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dendrite_engine", description="Formverk generative jigsaw workshop")
    sub = p.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="Generate a puzzle, registered artwork and fabrication audit")
    g.add_argument("--style", choices=CUT_FAMILIES, default="dendrite")
    g.add_argument("--outline", default="rect", help="rect, circle, organic, spiral, photo, or path to SVG")
    g.add_argument("--width-mm", type=float, default=260.0)
    g.add_argument("--height-mm", type=float, default=190.0)
    g.add_argument("--pieces", type=int, default=36)
    g.add_argument("--seed", type=int, default=42)
    g.add_argument("--borderless", action="store_true", help="Borderless organic perimeter (Maze style)")
    g.add_argument("--whimsy", action="append", default=[], help=f"Whimsy spec name[:x,y,size_mm,deg] ({', '.join(list_builtin_whimsies())}, pathnet, vae@ckpt, or file)")
    g.add_argument("--engrave-text", default=None)
    g.add_argument("--engrave-pos", default="0.5,0.90", help="Fractional x,y position for engraved text")
    g.add_argument("--engrave-height-mm", type=float, default=5.5)
    g.add_argument("--artwork", type=Path, default=None, help="Photograph to print on the board, centre-cropped to cover its outline; also guides phase-field cuts")
    g.add_argument("--auto-tune", action="store_true", help="Optimize PDE params with trained DendriteSurrogateNet")
    g.add_argument("--artwork-pattern", choices=ART_PATTERNS, default="wood")
    g.add_argument("--title", default="")
    g.add_argument("--regions", type=Path, help="JSON list of style, piece count and normalized polygon for each region")
    g.add_argument("--guide-curves", type=Path, help="Matrix guide polylines in board millimetres (JSON)")
    g.add_argument("--reverse-text", help="Inscription viewed from the reverse")
    g.add_argument("--back-artwork", type=Path, help="Print viewed from the reverse")
    g.add_argument("--auto-place-whimsies", action="store_true", help="Pack the selected figures with clearance")
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
    for command, description in EDITIONS.items():
        sub.add_parser(command, help=description)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in EDITIONS:
        from importlib import import_module
        command = argv[0]
        module = import_module(f".{command}", __package__)
        if hasattr(module, "add_arguments"):
            parser = argparse.ArgumentParser(prog=f"dendrite_engine {command}", description=EDITIONS[command])
            module.add_arguments(parser)
            return module.main_from_args(parser.parse_args(argv[1:])) or 0
        return module.main(argv[1:]) or 0
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        art = read_artwork(args.artwork) if args.artwork is not None else None
        if args.outline == "photo":
            if art is None:
                raise ValueError("--outline photo requires --artwork.")
            outline, art = photo_board(art, args.width_mm)
        else:
            outline = build_outline_polygon(args.outline, args.width_mm, args.height_mm, seed=args.seed)
        wh_list = parse_whimsy_arg(args.whimsy, outline) if args.whimsy else []
        ex, ey = (float(v) for v in args.engrave_pos.split(","))
        overrides: Dict[str, Any] = {}
        if args.steps is not None:
            overrides["steps"] = args.steps
            overrides["growth_steps"] = args.steps
        if args.gamma is not None:
            overrides["gamma"] = args.gamma
        if args.guide_curves:
            overrides["guide_curves"] = json.loads(args.guide_curves.read_text(encoding="utf-8-sig"))
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
            title=args.title, reverse_text=args.reverse_text,
            back_artwork=read_artwork(args.back_artwork) if args.back_artwork else None,
            artwork_pattern=args.artwork_pattern, auto_place_whimsies=args.auto_place_whimsies, board_outline=outline,
            regions=json.loads(args.regions.read_text(encoding="utf-8-sig")) if args.regions else None,
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
