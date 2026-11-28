"""Independent >=5% stratified dual-trace QA with exact polygon IoU, boundary distances, overlays, and disagreement logs."""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
from scipy.spatial import cKDTree
from scipy.spatial.distance import directed_hausdorff

from .extract_layers import TracedFigure, retrace_figure_method_b_otsu_marching_squares


def _load_font(size: int = 12) -> ImageFont.ImageFont:
    for p in (
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(p, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _sample_geom_boundary(geom: Any, max_pts: int = 600) -> np.ndarray:
    """Sample boundary coordinates (x, y) from a Shapely Polygon or MultiPolygon."""
    pts_list: List[np.ndarray] = []
    geoms = list(geom.geoms) if geom.geom_type == "MultiPolygon" else ([geom] if geom.geom_type == "Polygon" else [])
    for g in geoms:
        if g.exterior is not None:
            pts_list.append(np.asarray(g.exterior.coords, dtype=np.float64)[:, :2])
        for hole in g.interiors:
            pts_list.append(np.asarray(hole.coords, dtype=np.float64)[:, :2])
    if not pts_list:
        return np.empty((0, 2), dtype=np.float64)
    arr = np.vstack(pts_list)
    if len(arr) > max_pts:
        idx = np.linspace(0, len(arr) - 1, max_pts).astype(int)
        arr = arr[idx]
    return arr


def _rasterize_geom_for_overlay(geom: Any, crop_w: int, crop_h: int, out_w: int, out_h: int) -> np.ndarray:
    """Rasterize a Shapely Polygon/MultiPolygon in raw crop pixel coordinates to (out_h, out_w) uint8 mask."""
    canvas = np.zeros((out_h, out_w), dtype=np.uint8)
    if geom is None or geom.is_empty:
        return canvas
    sx = out_w / float(max(crop_w, 1))
    sy = out_h / float(max(crop_h, 1))
    geoms = list(geom.geoms) if geom.geom_type == "MultiPolygon" else ([geom] if geom.geom_type == "Polygon" else [])
    for g in geoms:
        ext = np.asarray(g.exterior.coords, dtype=np.float64)
        ext[:, 0] *= sx
        ext[:, 1] *= sy
        pts_list = [np.round(ext).astype(np.int32)]
        for hole in g.interiors:
            hi = np.asarray(hole.coords, dtype=np.float64)
            hi[:, 0] *= sx
            hi[:, 1] *= sy
            pts_list.append(np.round(hi).astype(np.int32))
        cv2.fillPoly(canvas, pts_list, 255)
    return canvas


def _render_overlay_png(
    raw_path: Path,
    crop_px: List[int],
    media_type: str,
    geom_a: Any,
    geom_b: Any,
    qa_item: Dict[str, Any],
    out_path: Path,
) -> None:
    """Render visual QA overlay PNG comparing Method A (cyan) vs Method B (orange) & XOR error (magenta)."""
    rx, ry, rw, rh = [int(v) for v in crop_px]
    max_disp = 540
    scale = min(1.0, max_disp / float(max(rw, rh, 1)))
    disp_w = max(80, int(round(rw * scale)))
    disp_h = max(80, int(round(rh * scale)))

    if raw_path.suffix.lower() == ".svg" or media_type == "svg_vector":
        bg = np.full((disp_h, disp_w, 3), 248, dtype=np.uint8)
    else:
        try:
            with Image.open(raw_path) as im:
                im = ImageOps.exif_transpose(im)
                iw, ih = im.size
                x0 = max(0, min(iw - 1, rx))
                y0 = max(0, min(ih - 1, ry))
                x1 = max(x0 + 1, min(iw, rx + rw))
                y1 = max(y0 + 1, min(ih, ry + rh))
                crop_im = im.crop((x0, y0, x1, y1)).convert("RGB")
                crop_im = crop_im.resize((disp_w, disp_h), Image.Resampling.BILINEAR)
                bg = np.array(crop_im, dtype=np.uint8)
        except Exception:
            bg = np.full((disp_h, disp_w, 3), 245, dtype=np.uint8)

    mask_a = _rasterize_geom_for_overlay(geom_a, rw, rh, disp_w, disp_h) > 0
    mask_b = _rasterize_geom_for_overlay(geom_b, rw, rh, disp_w, disp_h) > 0
    xor_diff = mask_a ^ mask_b

    overlay = bg.astype(np.float32)
    # Tint disagreement XOR pixels magenta
    overlay[xor_diff] = overlay[xor_diff] * 0.45 + np.array([220, 38, 127], dtype=np.float32) * 0.55
    vis = np.clip(overlay, 0, 255).astype(np.uint8)

    # Draw crisp contours: Method A in Cyan/Blue, Method B in Amber/Orange
    cnt_a, _ = cv2.findContours((mask_a.astype(np.uint8) * 255), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cnt_b, _ = cv2.findContours((mask_b.astype(np.uint8) * 255), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(vis, cnt_a, -1, (0, 140, 255), 2)
    cv2.drawContours(vis, cnt_b, -1, (255, 140, 0), 2)

    header_h = 62
    canvas_w = max(disp_w + 24, 520)
    canvas_h = header_h + disp_h + 36
    canvas = Image.new("RGB", (canvas_w, canvas_h), (245, 246, 248))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, canvas_w, header_h], fill=(20, 24, 31))

    font_b = _load_font(13)
    font_s = _load_font(11)
    fid = qa_item["figure_id"]
    reg = qa_item["core_region"]
    iou = qa_item["polygon_iou"]
    hd = qa_item["hausdorff_px"]
    md = qa_item["mean_boundary_dist_px"]
    ad = qa_item["area_delta_pct"]
    topo = "OK" if qa_item["topology_agreement"] else "MISMATCH"

    draw.text((12, 10), f"{fid}  |  region: {reg}  |  {qa_item['media_type']}", fill=(255, 255, 255), font=font_b)
    draw.text(
        (12, 34),
        f"IoU: {iou:.4f}   Hausdorff: {hd:.2f}px   MeanDist: {md:.2f}px   AreaΔ: {ad:+.1f}%   Topo: {topo}",
        fill=(209, 213, 219),
        font=font_s,
    )

    paste_x = (canvas_w - disp_w) // 2
    canvas.paste(Image.fromarray(vis, mode="RGB"), (paste_x, header_h + 8))
    draw.text(
        (12, canvas_h - 22),
        "Blue: Method A (Sauvola+Grad)   Orange: Method B (Otsu+Morph+MarchingSq)   Magenta: XOR Disagreement",
        fill=(75, 85, 99),
        font=font_s,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG", optimize=True)


def select_stratified_qa_sample(
    records: List[Dict[str, Any]],
    min_fraction: float = 0.05,
    seed: int = 20261128,
) -> List[Dict[str, Any]]:
    """Select >= 5% stratified sample across core regions (alta, ostfold, bohuslan_tanum, namforsen, others)."""
    if not records:
        return []
    n_total = len(records)
    target_n = max(1, int(math.ceil(min_fraction * n_total)))

    by_region: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_region[str(r.get("core_region") or "other")].append(r)

    rng = np.random.default_rng(seed)
    # Deterministic ordering per region before sampling
    for reg in by_region:
        by_region[reg].sort(key=lambda x: str(x["figure_id"]))

    selected: List[Dict[str, Any]] = []
    selected_ids: set[str] = set()

    # Step 1: Guarantee at least 1 from every non-empty region (while <= target_n or expand target_n if needed)
    regions_sorted = sorted(by_region.keys())
    if target_n < len(regions_sorted) and n_total >= len(regions_sorted):
        target_n = len(regions_sorted)

    # Proportional allocation with floor >= 1 per non-empty region
    alloc: Dict[str, int] = {}
    for reg in regions_sorted:
        prop = int(round(target_n * (len(by_region[reg]) / float(n_total))))
        alloc[reg] = min(len(by_region[reg]), max(1, prop))

    # Adjust sum to >= target_n
    while sum(alloc.values()) < min(target_n, n_total):
        # Add to region with largest unallocated pool
        best_reg = max(regions_sorted, key=lambda rg: len(by_region[rg]) - alloc[rg])
        if alloc[best_reg] < len(by_region[best_reg]):
            alloc[best_reg] += 1
        else:
            break

    for reg in regions_sorted:
        pool = by_region[reg]
        k = alloc[reg]
        if k >= len(pool):
            chosen = list(pool)
        else:
            # Spread across archive/media types by picking evenly spaced indices after deterministic shuffle
            idxs = rng.permutation(len(pool))[:k]
            chosen = [pool[int(i)] for i in sorted(idxs)]
        for rec in chosen:
            if rec["figure_id"] not in selected_ids:
                selected.append(rec)
                selected_ids.add(rec["figure_id"])

    return selected


def run_dual_trace_qa(
    records: List[Dict[str, Any]],
    traced_by_id: Dict[str, TracedFigure],
    base_dir: Path,
    qa_dir: Path,
    min_fraction: float = 0.05,
) -> Dict[str, Any]:
    """Run independent Method B dual-trace QA on >=5% stratified sample and write reports, overlays, and logs."""
    qa_dir.mkdir(parents=True, exist_ok=True)
    overlays_dir = qa_dir / "overlays"
    overlays_dir.mkdir(parents=True, exist_ok=True)

    sample = select_stratified_qa_sample(records, min_fraction=min_fraction)
    results: List[Dict[str, Any]] = []
    disagreements: List[Dict[str, Any]] = []

    for qa_idx, rec in enumerate(sample, start=1):
        fid = rec["figure_id"]
        tf = traced_by_id[fid]
        raw_rel = rec["source_image"]["raw_path"]
        raw_abs = (base_dir / raw_rel).resolve()
        if not raw_abs.is_file():
            raw_abs = (base_dir.parent / raw_rel).resolve()

        geom_a = tf.geometry
        area_a = float(geom_a.area)
        comp_a = int(tf.metrics["component_count"])
        hole_a = int(tf.metrics["hole_count"])

        geom_b, _, info_b = retrace_figure_method_b_otsu_marching_squares(
            raw_path=raw_abs,
            crop_px=rec["crop_px"],
            media_type=rec["source_image"]["media_type"],
            polarity_hint=tf.polarity,
            ink_mode_hint=tf.ink_mode,
            work_scale_hint=tf.work_scale,
        )

        px_per_mm = rec["size_mm"].get("px_per_mm")
        calibrated = bool(rec["size_mm"].get("scale_calibrated")) and isinstance(px_per_mm, (int, float))

        if geom_b is None or not info_b.get("ok"):
            iou = 0.0
            hausdorff_px = float(max(rec["crop_px"][2], rec["crop_px"][3]))
            mean_dist_px = hausdorff_px * 0.5
            area_b = 0.0
            area_delta_pct = -100.0
            comp_b = 0
            hole_b = 0
            topo_agree = False
            b_error = str(info_b.get("error") or "method_b_failed")
        else:
            b_error = ""
            try:
                inter = geom_a.intersection(geom_b).area
                union = geom_a.union(geom_b).area
                iou = float(inter / max(union, 1e-6))
            except Exception:
                # Fallback via rasterization in case of numerical GEOS edge case
                rw, rh = int(rec["crop_px"][2]), int(rec["crop_px"][3])
                ma = _rasterize_geom_for_overlay(geom_a, rw, rh, min(400, rw), min(400, rh)) > 0
                mb = _rasterize_geom_for_overlay(geom_b, rw, rh, min(400, rw), min(400, rh)) > 0
                iou = float(np.sum(ma & mb) / max(int(np.sum(ma | mb)), 1))

            area_b = float(geom_b.area)
            area_delta_pct = 100.0 * (area_b - area_a) / max(area_a, 1e-6)
            comp_b = int(info_b["component_count"])
            hole_b = int(info_b["hole_count"])
            topo_agree = (comp_a == comp_b) and (hole_a == hole_b)

            pts_a = _sample_geom_boundary(geom_a, max_pts=500)
            pts_b = _sample_geom_boundary(geom_b, max_pts=500)
            if len(pts_a) > 0 and len(pts_b) > 0:
                d_ab = directed_hausdorff(pts_a, pts_b)[0]
                d_ba = directed_hausdorff(pts_b, pts_a)[0]
                hausdorff_px = float(max(d_ab, d_ba))
                t_a = cKDTree(pts_a)
                t_b = cKDTree(pts_b)
                mean_dist_px = float(0.5 * (np.mean(t_b.query(pts_a, k=1)[0]) + np.mean(t_a.query(pts_b, k=1)[0])))
            else:
                hausdorff_px = 0.0
                mean_dist_px = 0.0

        overlay_rel = f"qa/overlays/{fid}_overlay.png"
        item = {
            "figure_id": fid,
            "core_region": rec["core_region"],
            "source_archive": rec["source_image"]["source_archive"],
            "media_type": rec["source_image"]["media_type"],
            "motif_family": rec["motif"]["family"],
            "motif_class": rec["motif"]["motif_class"],
            "period": rec["period"],
            "scale_calibrated": calibrated,
            "method_a": tf.method,
            "method_b": "otsu_multiscale_morph_marching_squares",
            "polygon_iou": round(iou, 5),
            "hausdorff_px": round(hausdorff_px, 3),
            "hausdorff_mm": round(hausdorff_px / float(px_per_mm), 3) if calibrated else None,
            "mean_boundary_dist_px": round(mean_dist_px, 3),
            "mean_boundary_dist_mm": round(mean_dist_px / float(px_per_mm), 3) if calibrated else None,
            "area_a_px2": round(area_a, 2),
            "area_b_px2": round(area_b, 2),
            "area_delta_pct": round(area_delta_pct, 3),
            "abs_area_delta_pct": round(abs(area_delta_pct), 3),
            "components_a": comp_a,
            "components_b": comp_b,
            "holes_a": hole_a,
            "holes_b": hole_b,
            "topology_agreement": bool(topo_agree),
            "overlay_path": overlay_rel,
            "error": b_error,
        }

        _render_overlay_png(
            raw_path=raw_abs,
            crop_px=rec["crop_px"],
            media_type=rec["source_image"]["media_type"],
            geom_a=geom_a,
            geom_b=geom_b,
            qa_item=item,
            out_path=qa_dir / "overlays" / f"{fid}_overlay.png",
        )

        # Log EVERY disagreement honestly (any non-trivial geometric or topological discrepancy)
        reasons: List[str] = []
        if b_error:
            reasons.append(f"method_b_error:{b_error}")
        if iou < 0.85:
            reasons.append(f"iou_below_0.85:{iou:.4f}")
        if abs(area_delta_pct) > 10.0:
            reasons.append(f"area_delta_gt_10pct:{area_delta_pct:+.2f}%")
        if not topo_agree:
            reasons.append(f"topology_mismatch:A({comp_a}c,{hole_a}h)_vs_B({comp_b}c,{hole_b}h)")
        if mean_dist_px > 2.5:
            reasons.append(f"mean_boundary_dist_gt_2.5px:{mean_dist_px:.2f}px")

        if reasons:
            if b_error or iou < 0.65:
                severity = "major"
            elif iou < 0.85 or (not topo_agree):
                severity = "moderate"
            else:
                severity = "minor"

            if b_error:
                cause = "extraction_failure"
            elif not topo_agree and abs(comp_a - comp_b) > 0:
                cause = "component_split_or_crop_neighbor_fragment"
            elif not topo_agree and abs(hole_a - hole_b) > 0:
                cause = "internal_hole_threshold_sensitivity"
            elif abs(area_delta_pct) > 10.0:
                cause = "stroke_thickness_or_threshold_shift"
            else:
                cause = "boundary_quantization_or_weathered_edge"

            disagreement = {
                "figure_id": fid,
                "core_region": rec["core_region"],
                "source_archive": rec["source_image"]["source_archive"],
                "media_type": rec["source_image"]["media_type"],
                "motif_family": rec["motif"]["family"],
                "severity": severity,
                "cause_category": cause,
                "reasons": reasons,
                "polygon_iou": item["polygon_iou"],
                "hausdorff_px": item["hausdorff_px"],
                "mean_boundary_dist_px": item["mean_boundary_dist_px"],
                "area_delta_pct": item["area_delta_pct"],
                "topology_agreement": item["topology_agreement"],
                "overlay_path": overlay_rel,
            }
            disagreements.append(disagreement)
            item["disagreement_logged"] = True
            item["disagreement_severity"] = severity
        else:
            item["disagreement_logged"] = False
            item["disagreement_severity"] = "none"

        results.append(item)
        if qa_idx % 50 == 0 or qa_idx == len(sample):
            print(f"[qa] {qa_idx}/{len(sample)} dual-traced ({len(disagreements)} disagreements)", flush=True)

    # Write dual_trace_disagreements.jsonl
    disagree_path = qa_dir / "dual_trace_disagreements.jsonl"
    with open(disagree_path, "w", encoding="utf-8") as f:
        for d in disagreements:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    ious = [r["polygon_iou"] for r in results]
    hds = [r["hausdorff_px"] for r in results]
    mds = [r["mean_boundary_dist_px"] for r in results]
    ads = [r["abs_area_delta_pct"] for r in results]

    def _agg(sub: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not sub:
            return {"count": 0}
        s_ious = [x["polygon_iou"] for x in sub]
        return {
            "count": len(sub),
            "mean_iou": round(float(statistics.mean(s_ious)), 4),
            "median_iou": round(float(statistics.median(s_ious)), 4),
            "min_iou": round(float(min(s_ious)), 4),
            "topology_agreement_rate": round(sum(1 for x in sub if x["topology_agreement"]) / float(len(sub)), 4),
            "disagreements_logged": sum(1 for x in sub if x["disagreement_logged"]),
        }

    by_region = {}
    for reg in sorted({r["core_region"] for r in results}):
        by_region[reg] = _agg([r for r in results if r["core_region"] == reg])

    by_archive = {}
    for arch in sorted({r["source_archive"] for r in results}):
        by_archive[arch] = _agg([r for r in results if r["source_archive"] == arch])

    by_media = {}
    for mt in sorted({r["media_type"] for r in results}):
        by_media[mt] = _agg([r for r in results if r["media_type"] == mt])

    by_family = {}
    for fam in sorted({r["motif_family"] for r in results}):
        by_family[fam] = _agg([r for r in results if r["motif_family"] == fam])

    by_period = {}
    for per in sorted({r["period"] for r in results}):
        by_period[per] = _agg([r for r in results if r["period"] == per])

    n_total = len(records)
    n_qa = len(results)
    report = {
        "schema_version": "1.0.0",
        "method_a": "sauvola_local_gradient_contour",
        "method_b": "otsu_multiscale_morph_marching_squares",
        "algorithmic_independence": "Zero shared intermediate masks; Method B reads raw source crops independently using multi-scale morphological black-hat/top-hat + Otsu + sub-pixel marching squares.",
        "total_corpus_figures": n_total,
        "dual_traced_count": n_qa,
        "dual_traced_pct": round(100.0 * n_qa / max(n_total, 1), 2),
        "meets_5pct_gate": (n_qa / float(max(n_total, 1))) >= 0.05 if n_total > 0 else False,
        "mean_iou": round(float(statistics.mean(ious)), 4) if ious else 0.0,
        "median_iou": round(float(statistics.median(ious)), 4) if ious else 0.0,
        "p10_iou": round(float(np.percentile(ious, 10)), 4) if ious else 0.0,
        "min_iou": round(float(min(ious)), 4) if ious else 0.0,
        "max_iou": round(float(max(ious)), 4) if ious else 0.0,
        "mean_hausdorff_px": round(float(statistics.mean(hds)), 3) if hds else 0.0,
        "median_hausdorff_px": round(float(statistics.median(hds)), 3) if hds else 0.0,
        "mean_boundary_dist_px": round(float(statistics.mean(mds)), 3) if mds else 0.0,
        "median_boundary_dist_px": round(float(statistics.median(mds)), 3) if mds else 0.0,
        "mean_abs_area_delta_pct": round(float(statistics.mean(ads)), 3) if ads else 0.0,
        "median_abs_area_delta_pct": round(float(statistics.median(ads)), 3) if ads else 0.0,
        "topology_agreement_count": sum(1 for r in results if r["topology_agreement"]),
        "topology_agreement_rate": round(sum(1 for r in results if r["topology_agreement"]) / max(n_qa, 1), 4),
        "disagreement_count": len(disagreements),
        "disagreement_severity_counts": dict(Counter(d["severity"] for d in disagreements)),
        "disagreement_cause_counts": dict(Counter(d["cause_category"] for d in disagreements)),
        "by_core_region": by_region,
        "by_archive": by_archive,
        "by_media_type": by_media,
        "by_motif_family": by_family,
        "by_period": by_period,
        "figures": results,
    }

    with open(qa_dir / "dual_trace_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Write clean Markdown report
    lines = [
        "# Independent Dual-Trace QA Report",
        "",
        f"- **Corpus figures**: {n_total}",
        f"- **Dual-traced sample**: {n_qa} ({report['dual_traced_pct']:.2f}%, >=5% gate: {'PASS' if report['meets_5pct_gate'] else 'FAIL'})",
        f"- **Method A**: `{report['method_a']}`",
        f"- **Method B**: `{report['method_b']}` (independent raw-crop re-trace, zero shared masks)",
        f"- **Mean Polygon IoU**: {report['mean_iou']:.4f} | **Median IoU**: {report['median_iou']:.4f} | **Min IoU**: {report['min_iou']:.4f}",
        f"- **Mean Boundary Distance**: {report['mean_boundary_dist_px']:.3f} px | **Mean Hausdorff**: {report['mean_hausdorff_px']:.3f} px",
        f"- **Mean |Area Δ|**: {report['mean_abs_area_delta_pct']:.2f}% | **Topology Agreement**: {report['topology_agreement_count']}/{n_qa} ({report['topology_agreement_rate']*100:.1f}%)",
        f"- **Disagreements Logged**: {report['disagreement_count']} (see `dual_trace_disagreements.jsonl`)",
        "",
        "## Stratified Breakdown by Core Region",
        "",
        "| Core Region | Dual-Traced | Mean IoU | Median IoU | Min IoU | Topo Agree | Disagreements |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for reg, st in by_region.items():
        lines.append(
            f"| {reg} | {st['count']} | {st['mean_iou']:.4f} | {st['median_iou']:.4f} | {st['min_iou']:.4f} | {st['topology_agreement_rate']*100:.1f}% | {st['disagreements_logged']} |"
        )
    lines.extend([
        "",
        "## Disagreement Breakdown",
        "",
        f"- Severity counts: `{report['disagreement_severity_counts']}`",
        f"- Cause categories: `{report['disagreement_cause_counts']}`",
        "",
    ])
    (qa_dir / "dual_trace_report.md").write_text("\n".join(lines), encoding="utf-8")
    return report
