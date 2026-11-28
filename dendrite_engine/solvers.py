"""Solve cut geometry in board millimetres independently of the common exporter."""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

import numpy as np
from shapely.geometry import Polygon, LineString
from .artwork import photo_image, sample_photo
from .neural_guide import (
    DEFAULT_CHECKPOINT,
    auto_tune_parameters,
    compute_artwork_style_fields,
)
from .phase_field import DendriteConfig, run_dendrite_simulation
from .rod_growth import RodConfig, run_rod_growth
from .postprocess import (
    clean_connected_labels,
    extract_shared_seams_from_labels,
    repair_label_necks,
)
from .styles import CUT_FAMILIES


@dataclass
class CutGeometry:
    faces: Sequence[Polygon]
    interior_lines: Sequence[LineString]
    exterior_lines: Sequence[LineString]
    expected_outline: Polygon
    whimsy_indices: Sequence[int]
    metadata: dict


def solve_cut(
    outline,
    st,
    num_pieces,
    seed,
    *,
    whimsies=(),
    px_mm=0.5,
    kerf_mm=0.15,
    min_neck_mm=3.0,
    borderless=False,
    auto_tune=False,
    neural_checkpoint=DEFAULT_CHECKPOINT,
    outline_kind="custom",
    artwork_image=None,
    overrides=None,
):
    if st not in CUT_FAMILIES:
        raise ValueError(f"Unknown cut style {st!r}")
    x0, y0, x1, y1 = outline.bounds
    w_mm, h_mm = x1 - x0, y1 - y0
    extra = dict(overrides or {})
    tune_info: Optional[Dict[str, Any]] = None
    if st in ("coral", "matrix"):
        if st == "coral":
            from .coral import generate_coral as solver
        else:
            from .matrix import generate_matrix as solver
        result = solver(
            outline,
            num_pieces=num_pieces,
            seed=seed,
            whimsies=whimsies,
            min_neck_mm=min_neck_mm,
            kerf_mm=kerf_mm,
            **(
                {"guide_curves": extra["guide_curves"]}
                if st == "matrix" and "guide_curves" in extra
                else {}
            ),
        )
        return CutGeometry(
            result.faces,
            result.interior_lines,
            result.exterior_lines,
            outline,
            result.whimsy_indices,
            {"engine": CUT_FAMILIES[st], **result.metrics},
        )
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
            **{
                k: v
                for k, v in extra.items()
                if hasattr(RodConfig, k) and v is not None
            },
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
            "clearance_stage": "snapped_rod_centerlines",
            "last_collision_sweep_min_mm": rres.last_collision_sweep_min_mm,
            "elapsed_sec": round(rres.elapsed_sec, 2),
        }
        return CutGeometry(
            rres.faces,
            rres.interior_lines,
            rres.exterior_lines,
            rres.expected_outline,
            rres.whimsy_indices,
            meta,
        )

    # Phase-field styles: dendrite, amoeba, linear, grid
    if auto_tune:
        tune_info = auto_tune_parameters(
            w_mm, h_mm, num_pieces, style=st, checkpoint=neural_checkpoint, seed=seed
        )
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
        **{
            k: v
            for k, v in extra.items()
            if hasattr(DendriteConfig, k) and v is not None
        },
    )

    gamma_map = amp_map = None
    if artwork_image is not None:
        h_px = max(32, int(round(h_mm / cfg.px_mm)))
        w_px = max(32, int(round(w_mm / cfg.px_mm)))
        field_image = sample_photo(
            photo_image(artwork_image), (w_mm, h_mm), (0, 0, w_mm, h_mm), (w_px, h_px)
        )
        gamma_map, amp_map = compute_artwork_style_fields(
            field_image, (h_px, w_px), base_gamma=cfg.gamma
        )

    dres = run_dendrite_simulation(
        outline,
        cfg,
        whimsies=whimsies,
        gamma_map=gamma_map,
        amplitude_map=amp_map,
    )
    lab, stray_px = clean_connected_labels(
        dres.labels, dres.domain_mask, fixed_mask=dres.whimsy_mask
    )
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
    return CutGeometry(faces, int_lines, ext_lines, outline, whimsy_indices, meta)
