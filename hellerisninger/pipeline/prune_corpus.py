"""Surgical geometric & visual-artifact pruner for the processed helleristninger corpus.

Eliminates:
  - Triangular & straight-shafted north arrows (Alta/SHFA/Unimus arrowheads)
  - Rotated straight sticks, bars, planks, brackets, crosshairs, and rock-crack lines
  - Flat horizontal/vertical scale bars (|____|, |___, _|___)
  - Margin letters/captions (m, C, T, Exf, Z/N, numerals, handwritten margin text)
  - Fused cupmark clusters / 2-5 dot dumbbells, featureless circles, ovals, and blobs
  - Kerf/neck fabrication failures at 60 mm nominal piece scale (3.0 + 0.15 mm)
  - Noisy photo/rubbing extractions

Optionally re-classifies surviving motifs via the strict multilingual & archive-aware
taxonomy classifier, re-assigns leak-free grouped splits, preserves surviving asset IDs
when requested, and re-exports clean consistent SVG/mask/polygon/SDF/catalog/contact-sheet/Figma files.
"""

from __future__ import annotations

import json
import math
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage
from shapely.geometry import Polygon
from skimage import morphology

from .dedup_split import assign_grouped_splits, deduplicate_motifs
from .export import export_corpus
from .silhouette import EROSION_RADIUS_NORM
from .taxonomy import build_class_index, classify_motif, load_taxonomy


def _skeleton_topology(mask_256: np.ndarray) -> Tuple[int, int]:
    """Count topological branch junctions and endpoints on a 128x128 skeletonized silhouette.

    Real rock-art figures (animals with legs/head, humans with limbs, ships with crew strokes,
    fish/whales with fins/tail flukes) have >= 1 branch junction and >= 3 endpoints.
    Single unbranched sticks, hooks, C-arcs, clubs, brackets, and 2-dot dumbbells have 0 junctions!
    """
    small = cv2.resize(mask_256, (128, 128), interpolation=cv2.INTER_AREA) > 127
    if not np.any(small):
        return 0, 0
    skel = morphology.skeletonize(small)
    if not np.any(skel):
        return 0, 0
    u8 = skel.astype(np.uint8)
    # 8-neighbor count
    k = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.uint8)
    nbrs = cv2.filter2D(u8, -1, k, borderType=cv2.BORDER_CONSTANT)
    junction_mask = skel & (nbrs >= 3)
    endpoint_mask = skel & (nbrs == 1)
    _, n_junc = ndimage.label(junction_mask, structure=np.ones((3, 3), dtype=int))
    _, n_end = ndimage.label(endpoint_mask, structure=np.ones((3, 3), dtype=int))
    return int(n_junc), int(n_end)


def _is_printed_T_or_m(mask_256: np.ndarray, neck_mm: float = 0.0) -> bool:
    """Detect printed scale/label letters 'T' (Aspeberget) and 'm' ('1 m' scale bar)."""
    fg = mask_256 > 0
    ys, xs = np.nonzero(fg)
    if len(xs) < 100:
        return False
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    sub = fg[y0 : y1 + 1, x0 : x1 + 1]
    bh, bw = sub.shape
    if min(bh, bw) < 20:
        return False

    # --- Capital 'T' detector: flat wide top bar + narrow centered vertical stem ---
    top_row = sub[: max(4, bh // 7), :]
    bot_half = sub[bh // 2 :, :]
    top_span = float(np.mean(np.any(top_row, axis=0)))
    bot_cols = np.any(bot_half, axis=0)
    bot_span = float(np.mean(bot_cols))
    if top_span >= 0.80 and bot_span <= 0.34 and 0.65 <= (bw / float(bh)) <= 1.35:
        bot_xs = np.nonzero(bot_cols)[0]
        if len(bot_xs) > 0:
            center_rel = float(np.mean(bot_xs)) / float(bw)
            if 0.35 <= center_rel <= 0.65:
                return True

    # --- Lowercase 'm' detector: 3 thin vertical legs aligned on baseline + arched/flat solid top ---
    if 0.78 <= (bw / float(bh)) <= 1.65 and (neck_mm <= 0.0 or neck_mm <= 9.5):
        for y_lo, y_hi in ((0.48, 0.86), (0.55, 0.85)):
            mid_band = sub[int(y_lo * bh) : int(y_hi * bh), :]
            col_prof = np.mean(mid_band, axis=0) > 0.55
            runs: List[Tuple[int, int]] = []
            in_run = False
            start = 0
            for ci, v in enumerate(col_prof):
                if v and not in_run:
                    start = ci
                    in_run = True
                elif not v and in_run:
                    if ci - start >= max(3, bw // 30):
                        runs.append((start, ci))
                    in_run = False
            if in_run and len(col_prof) - start >= max(3, bw // 30):
                runs.append((start, len(col_prof)))
            if len(runs) == 3 and runs[0][0] <= 0.16 * bw and runs[2][1] >= 0.84 * bw:
                top_cov = float(np.mean(np.any(sub[: max(6, int(0.30 * bh)), :], axis=0)))
                if top_cov >= 0.80:
                    leg_bots = []
                    for rs, re_i in runs:
                        leg_ys = np.nonzero(sub[:, rs:re_i])[0]
                        if len(leg_ys) > 0:
                            leg_bots.append(int(leg_ys.max()))
                    if len(leg_bots) == 3 and (max(leg_bots) - min(leg_bots)) <= 0.12 * bh:
                        return True
    return False


def _is_cupmark_cluster(mask_256: np.ndarray) -> bool:
    """Detect 2..5 fused circular cupmarks (dumbbells / dot clusters)."""
    fg = (mask_256 > 0).astype(np.uint8)
    dist = cv2.distanceTransform(fg, cv2.DIST_L2, 5)
    max_r = float(dist.max())
    if max_r < 8.0:
        return False
    for frac in (0.35, 0.45, 0.55):
        core = (dist >= frac * max_r).astype(np.uint8)
        n_lbl, lbls, stats, _ = cv2.connectedComponentsWithStats(core, connectivity=8)
        circs: List[float] = []
        for i in range(1, n_lbl):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < 40:
                continue
            comp = (lbls == i).astype(np.uint8)
            cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            if not cnts:
                continue
            perim = max(float(cv2.arcLength(cnts[0], True)), 1e-6)
            circs.append(4.0 * math.pi * area / (perim * perim))
        if 2 <= len(circs) <= 5 and min(circs) >= 0.65 and (sum(circs) / len(circs)) >= 0.76:
            r_dil = max(1, int(round(frac * max_r * 1.15)))
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r_dil + 1, 2 * r_dil + 1))
            recon = cv2.dilate(core, k) > 0
            iou = float(np.sum(recon & (fg > 0))) / max(float(np.sum(recon | (fg > 0))), 1.0)
            if iou >= 0.74:
                return True
    return False


def _is_straight_north_arrow(mask_256: np.ndarray) -> bool:
    """Detect straight-shafted north arrows (long uniform straight shaft + flared triangular tip)."""
    fg = mask_256 > 0
    ys, xs = np.nonzero(fg)
    if len(xs) < 100:
        return False
    pts = np.column_stack([xs, ys]).astype(np.float32)
    centered = pts - pts.mean(axis=0)
    cov = np.cov(centered.T)
    _, eigvecs = np.linalg.eigh(cov)
    major = eigvecs[:, 1]
    minor = eigvecs[:, 0]
    proj_maj = centered @ major
    proj_min = centered @ minor
    span_l = float(proj_maj.max() - proj_maj.min())
    span_w = float(proj_min.max() - proj_min.min())
    if span_l < 40.0 or span_w < 10.0 or (span_l / max(span_w, 1.0)) < 1.85:
        return False
    edges = np.linspace(proj_maj.min(), proj_maj.max(), 21)
    widths = np.zeros(20, dtype=np.float64)
    centers = np.zeros(20, dtype=np.float64)
    for i in range(20):
        sel = (proj_maj >= edges[i]) & (proj_maj <= edges[i + 1])
        if np.sum(sel) >= 3:
            pmin = proj_min[sel]
            widths[i] = float(pmin.max() - pmin.min())
            centers[i] = float(np.mean(pmin))
    for w_arr, c_arr in ((widths, centers), (widths[::-1], centers[::-1])):
        shaft_w = w_arr[1:13]
        head_w = w_arr[13:20]
        med_shaft = float(np.median(shaft_w))
        if med_shaft <= 1.0:
            continue
        shaft_cv = float(np.std(shaft_w)) / med_shaft
        shaft_straight = float(np.std(c_arr[1:13])) / max(span_l, 1.0)
        head_max = float(np.max(head_w))
        flare_ratio = head_max / med_shaft
        tip_taper = float(w_arr[-1]) / max(head_max, 1.0)
        if shaft_cv < 0.25 and shaft_straight < 0.022 and flare_ratio >= 2.2 and tip_taper <= 0.65:
            score = flare_ratio * (1.0 - shaft_cv) * (1.0 - min(1.0, shaft_straight * 20.0))
            if score >= 2.2:
                return True
    return False


def _strict_neck_ok(poly: Polygon) -> bool:
    """Verify conservative 3.0 + 0.15 mm neck erosion survival at 60 mm nominal piece scale."""
    if not poly.is_valid or poly.is_empty or poly.area <= 0:
        return False
    minx, miny, maxx, maxy = poly.bounds
    max_dim = max(maxx - minx, maxy - miny, 1e-6)
    er = poly.buffer(-EROSION_RADIUS_NORM * max_dim, quad_segs=12)
    return (not er.is_empty) and (er.geom_type == "Polygon")


def _edge_flatness(mask_256: np.ndarray) -> Tuple[float, float, float, float]:
    """Measure fraction of foreground width/height lying dead-flat along the 4 bbox edges.

    Scale bars (|____|, L-bars, brackets `]`, capital `T`, rectangles) have long dead-flat
    horizontal or vertical runs spanning >= 55% of their width or height!
    """
    fg = mask_256 > 0
    ys, xs = np.nonzero(fg)
    if len(xs) == 0:
        return 0.0, 0.0, 0.0, 0.0
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    band = 4

    top_cov = float(np.mean(np.any(fg[y0 : min(y0 + band, y1 + 1), x0 : x1 + 1], axis=0)))
    bot_cov = float(np.mean(np.any(fg[max(y0, y1 - band + 1) : y1 + 1, x0 : x1 + 1], axis=0)))
    left_cov = float(np.mean(np.any(fg[y0 : y1 + 1, x0 : min(x0 + band, x1 + 1)], axis=1)))
    right_cov = float(np.mean(np.any(fg[y0 : y1 + 1, max(x0, x1 - band + 1) : x1 + 1], axis=1)))
    return top_cov, bot_cov, left_cov, right_cov


def _longest_straight_run_ratio(poly: Polygon) -> float:
    """Ratio of longest near-straight boundary chain to total perimeter."""
    simp = poly.simplify(1.2, preserve_topology=True)
    if simp.geom_type != "Polygon" or simp.is_empty:
        return 0.0
    coords = list(simp.exterior.coords)
    if len(coords) < 3:
        return 1.0
    perim = max(float(simp.length), 1e-6)
    max_seg = 0.0
    for i in range(len(coords) - 1):
        d = math.hypot(coords[i + 1][0] - coords[i][0], coords[i + 1][1] - coords[i][1])
        if d > max_seg:
            max_seg = d
    return max_seg / perim


def _count_convexity_bays(poly: Polygon) -> Tuple[int, float]:
    """Count meaningful concave bays (separating legs, head, antlers, crew strokes, prows)."""
    diff = poly.convex_hull.difference(poly)
    if diff.is_empty:
        return 0, 0.0
    geoms = list(diff.geoms) if hasattr(diff, "geoms") else [diff]
    poly_area = max(float(poly.area), 1e-6)
    sig_bays = [g for g in geoms if g.area >= 0.025 * poly_area]
    max_bay = max((g.area / poly_area for g in geoms), default=0.0)
    return len(sig_bays), float(max_bay)


def evaluate_motif_quality(rec: Dict[str, Any], poly: Polygon, mask_256: np.ndarray) -> Tuple[bool, str]:
    """Return (keep, reject_reason) using strict visual & geometric artifact detectors."""
    media = str(rec.get("media_type", ""))
    rstyle = str(rec.get("render_style", ""))
    mclass = str(rec.get("motif_class", ""))
    tier = str(rec.get("jigsaw_tier", ""))

    # 0. Drop Tier C, photo_other, rubbing_frottage, and noisy painted_photo
    if tier not in ("A_ready", "B_thickened"):
        return False, "tier_c"
    if media in ("photo_other", "rubbing_frottage"):
        return False, f"media_{media}"
    if media == "painted_photo":
        if rec.get("solidity", 0) > 0.78 or rec.get("solidity", 0) < 0.36 or rec.get("min_neck_mm_at_60mm", 0) > 20.0:
            return False, "noisy_painted_photo"

    if not _strict_neck_ok(poly):
        return False, "strict_kerf_neck_fail"

    sol = float(rec.get("solidity", 0.0))
    cmp = float(rec.get("compactness", 0.0))
    ar = float(rec.get("aspect_ratio", 1.0))
    neck = float(rec.get("min_neck_mm_at_60mm", 0.0))
    esr = float(rec.get("erosion_survival_ratio", 0.0))
    vcount = int(rec.get("vertex_count", 0))

    # 1. Minimum-area rotated rectangle (catches rotated bars, planks, sticks, diagonal cracks!)
    mrr = poly.minimum_rotated_rectangle
    mc = list(mrr.exterior.coords)
    e1 = math.hypot(mc[1][0] - mc[0][0], mc[1][1] - mc[0][1])
    e2 = math.hypot(mc[2][0] - mc[1][0], mc[2][1] - mc[1][1])
    rot_ar = max(e1, e2) / max(min(e1, e2), 1e-4)
    rot_fill = float(poly.area) / max(float(mrr.area), 1e-4)

    # 2. Minimum enclosing triangle (catches Alta/SHFA triangular north arrows!)
    pts = np.array(poly.exterior.coords, dtype=np.float32).reshape(-1, 1, 2)
    tri_area, _ = cv2.minEnclosingTriangle(pts)
    tri_fill = float(poly.area) / max(float(tri_area), 1e-4)

    # 3. Straightness / flat-edge / bay-structure / skeleton-topology signatures
    top_c, bot_c, left_c, right_c = _edge_flatness(mask_256)
    max_flat_edge = max(top_c, bot_c, left_c, right_c)
    longest_seg = _longest_straight_run_ratio(poly)
    n_bays, max_bay = _count_convexity_bays(poly)
    n_junc, n_end = _skeleton_topology(mask_256)

    # --- RULE 0: Printed 'T'/'m' glyphs, cupmark clusters, & straight-shafted north arrows ---
    if _is_printed_T_or_m(mask_256, neck_mm=neck):
        return False, "printed_T_or_m_glyph"
    if mclass != "footprint" and _is_cupmark_cluster(mask_256):
        return False, "cupmark_cluster_or_dumbbell"
    if _is_straight_north_arrow(mask_256):
        return False, "straight_north_arrow"

    # --- RULE A: Triangular north arrows & wedges (Alta ▲, arrowheads) ---
    if tri_fill >= 0.72 and sol >= 0.85:
        return False, "triangular_north_arrow"
    if longest_seg >= 0.28 and sol >= 0.84:
        return False, "straight_sided_geometric_arrow_or_box"

    # --- RULE A2: Unbranched 1D sticks, vertical clubs, hooks, C-arcs, and 2-dot dumbbells ---
    if n_junc == 0 and n_end <= 2:
        is_curved_ship = (ar >= 1.45 and n_bays >= 1 and max_bay >= 0.045 and rot_fill <= 0.74 and longest_seg < 0.22)
        is_allowed_footprint = (mclass == "footprint" and 1.70 <= rot_ar <= 3.3 and longest_seg < 0.16)
        is_allowed_spiral = (mclass == "geometric_spiral" and rot_ar <= 1.55 and sol <= 0.72)
        is_dumbbell = (n_bays == 2 and sol >= 0.74 and neck <= 9.0 and rot_ar <= 2.4)
        if is_dumbbell or not (is_curved_ship or is_allowed_footprint or is_allowed_spiral):
            return False, "unbranched_stick_hook_or_dumbbell"

    # --- RULE B: Straight/diagonal bars, sticks, cracks, planks at ANY angle ---
    if rot_ar >= 2.7 and rot_fill >= 0.56 and max_bay < 0.14:
        if not (mclass == "footprint" and longest_seg < 0.14 and rot_ar <= 3.1):
            return False, "rotated_bar_or_stick"
    if rot_ar >= 4.2:
        if max_flat_edge >= 0.62 or n_bays < 2 or longest_seg >= 0.22:
            return False, "skinny_stick_or_scale_bar"
    if rot_fill >= 0.73 and sol >= 0.86:
        if not (mclass == "footprint" and 1.9 <= rot_ar <= 3.0 and longest_seg < 0.14):
            return False, "rectangular_block_or_plank"

    # --- RULE C: Flat-edged scale bars (|_____|, |___, _|___), brackets (`]`), capital `T` ---
    if max_flat_edge >= 0.70 and longest_seg >= 0.20:
        return False, "flat_scale_bar_or_bracket"
    if (bot_c >= 0.78 or top_c >= 0.78) and ar >= 1.8 and longest_seg >= 0.17:
        return False, "horizontal_scale_bar"
    if (left_c >= 0.78 or right_c >= 0.78) and ar <= 0.62 and longest_seg >= 0.17:
        return False, "vertical_bracket_or_bar"
    if top_c >= 0.72 and ar <= 1.25 and sol <= 0.55 and longest_seg >= 0.16 and n_bays <= 2:
        return False, "printed_T_or_L_glyph"
    if n_bays <= 1 and sol < 0.58 and rot_ar <= 2.6 and cmp < 0.30:
        return False, "single_C_or_hook_curve"

    # --- RULE D: Plain circles, filled sun-wheel blobs, giant cupmarks, and fat lumps ---
    if neck >= 29.0:
        return False, "fat_featureless_lump"
    if cmp >= 0.62 and sol >= 0.86 and rot_ar < 1.95:
        return False, "round_oval_cupmark_blob"
    if sol >= 0.89 and n_bays == 0:
        if not (mclass == "footprint" and 1.9 <= rot_ar <= 3.1):
            return False, "convex_featureless_blob"

    # --- RULE E: Over-thickened amorphous linear_motif blobs & cursive words ---
    if rstyle == "linear_motif":
        if neck > 19.0:
            return False, "overthickened_linear_blob"
        if sol > 0.76:
            return False, "blobby_linear_motif"
        if cmp > 0.46:
            return False, "compact_linear_blob"
        if n_bays == 0 and rot_fill >= 0.56:
            return False, "simple_bar_linear_fragment"
        if vcount < 24:
            return False, "underspecified_linear_fragment"

    # --- RULE F: General complexity & recognizability floor ---
    if vcount < 18 or vcount > 165:
        return False, "bad_vertex_count"
    if cmp < 0.10 or sol < 0.28:
        return False, "spindly_crack_web"
    if esr < 0.26:
        return False, "weak_erosion_survival"

    # --- RULE G: Handwritten cursive word filter ---
    if ar >= 2.2 and sol >= 0.68 and rot_fill >= 0.58 and mclass not in ("bronze_age_ship", "stone_age_boat", "footprint"):
        return False, "handwritten_text_strip"

    return True, "keep"


def prune_processed_corpus(
    base_dir: Path,
    drop_ids: set[str] | None = None,
    reclassify: bool = False,
    preserve_asset_ids: bool = True,
) -> Dict[str, Any]:
    proc_dir = base_dir / "processed"
    cat_path = proc_dir / "catalog.json"
    old_stats_path = proc_dir / "stats.json"

    catalog = json.loads(cat_path.read_text(encoding="utf-8"))
    old_stats = json.loads(old_stats_path.read_text(encoding="utf-8")) if old_stats_path.exists() else {}
    old_ingest = old_stats.get("pipeline_ingest_summary", {})
    drop_ids = drop_ids or set()
    class_index = build_class_index(load_taxonomy(base_dir / "taxonomy.json")) if reclassify else None

    kept_items: List[Dict[str, Any]] = []
    reject_counts: Counter = Counter()
    reclass_transitions: Counter = Counter()

    for rec in catalog:
        if rec.get("asset_id") in drop_ids:
            reject_counts["visual_qa_pruned"] += 1
            continue
        poly_path = base_dir / rec["polygon_path"]
        mask_path = base_dir / rec["mask_path"]
        if not poly_path.is_file() or not mask_path.is_file():
            reject_counts["missing_artifact"] += 1
            continue

        pdat = json.loads(poly_path.read_text(encoding="utf-8"))
        coords = [(float(x), float(y)) for x, y in pdat["exterior"]]
        poly = Polygon(coords)
        mask_256 = np.array(Image.open(mask_path).convert("L"), dtype=np.uint8)

        ok, reason = evaluate_motif_quality(rec, poly, mask_256)
        if not ok:
            reject_counts[reason] += 1
            continue

        family = rec["family"]
        motif_class = rec["motif_class"]
        conf = float(rec.get("classification_confidence", 0.0))
        if reclassify and class_index is not None:
            shape_m = {
                "aspect_ratio": rec.get("aspect_ratio", 1.0),
                "solidity": rec.get("solidity", 0.5),
                "compactness": rec.get("compactness", 0.3),
                "circularity": rec.get("compactness", 0.3),
                "render_style": rec.get("render_style", ""),
            }
            new_fam, new_cls, new_conf = classify_motif(
                rec,
                class_index=class_index,
                num_extracted_from_source=int(rec.get("panel_total_motifs", 1)),
                shape_metrics=shape_m,
            )
            if new_cls != motif_class:
                reclass_transitions[f"{motif_class}->{new_cls}"] += 1
            family, motif_class, conf = new_fam, new_cls, new_conf

        item = {
            "asset_id": rec["asset_id"],
            "source_id": rec["source_id"],
            "source_archive": rec["source_archive"],
            "source_group": rec["source_group"],
            "panel_motif_index": rec.get("panel_motif_index", 0),
            "panel_total_motifs": rec.get("panel_total_motifs", 1),
            "title": rec.get("title", ""),
            "description": rec.get("description", ""),
            "site": rec.get("site", ""),
            "region": rec.get("region", ""),
            "country": rec.get("country", "NORDIC"),
            "period": rec.get("period", "unknown"),
            "media_type": rec.get("media_type", ""),
            "motif_hints": rec.get("motif_hints", []),
            "raw_path": rec["raw_path"],
            "source_url": rec.get("source_url", ""),
            "download_url": rec.get("download_url", ""),
            "creator": rec.get("creator", ""),
            "license": rec.get("license", ""),
            "license_url": rec.get("license_url", ""),
            "commercial_ok": bool(rec["commercial_ok"]),
            "sha256": rec["sha256"],
            "family": family,
            "motif_class": motif_class,
            "classification_confidence": conf,
            "render_style": rec["render_style"],
            "was_thickened": bool(rec["was_thickened"]),
            "jigsaw_tier": rec["jigsaw_tier"],
            "coords": coords,
            "mask_256": mask_256,
            "metrics": pdat["metrics"],
            "raw_bbox_px": rec.get("raw_bbox_px", [0, 0, 0, 0]),
            "raw_bbox_area_px": int(rec["raw_bbox_px"][2] * rec["raw_bbox_px"][3]) if len(rec.get("raw_bbox_px", [])) == 4 else 0,
        }
        kept_items.append(item)

    deduped, dedup_dropped = deduplicate_motifs(kept_items, iou_threshold=0.92, phash_max_hamming=8)
    split_summary = assign_grouped_splits(deduped)

    # Clean stale processed subdirs completely before re-exporting pristine corpus
    for sub in ("svg", "masks", "polygons", "sdf", "contact_sheets", "figma_sheets"):
        p = proc_dir / sub
        if p.exists():
            shutil.rmtree(p)

    prune_summary = dict(old_ingest)
    prune_summary["pruner_input_motifs"] = len(catalog)
    prune_summary["pruner_kept_pre_dedup"] = len(kept_items)
    prune_summary["pruner_kept_final"] = len(deduped)
    prune_summary["pruner_reject_reasons"] = dict(reject_counts)
    prune_summary["pruner_reclassified"] = bool(reclassify)
    if reclassify:
        prune_summary["pruner_reclass_transitions"] = dict(reclass_transitions.most_common(40))

    stats = export_corpus(
        deduped,
        output_dir=proc_dir,
        pipeline_summary=prune_summary,
        split_summary=split_summary,
        dedup_dropped=dedup_dropped,
        preserve_asset_ids=preserve_asset_ids,
    )
    return stats


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--drop-ids", type=str, default="")
    ap.add_argument("--drop-ids-file", type=Path, default=None)
    ap.add_argument("--reclassify", action="store_true", help="Re-run strict taxonomy.classify_motif on surviving motifs")
    ap.add_argument("--reindex", action="store_true", help="Re-assign sequential asset IDs instead of preserving surviving IDs")
    args = ap.parse_args()
    drop_set = {x.strip() for x in args.drop_ids.split(",") if x.strip()}
    if args.drop_ids_file and args.drop_ids_file.is_file():
        for line in args.drop_ids_file.read_text(encoding="utf-8").splitlines():
            tok = line.split("#", 1)[0].strip()
            if tok:
                for piece in tok.split(","):
                    if piece.strip():
                        drop_set.add(piece.strip())
    base = Path(__file__).resolve().parent.parent
    st = prune_processed_corpus(
        base,
        drop_ids=drop_set,
        reclassify=args.reclassify,
        preserve_asset_ids=not args.reindex,
    )
    print(
        json.dumps(
            {
                "total_motifs": st["total_motifs"],
                "tier_counts": st["tier_counts"],
                "by_country": st["by_country"],
                "by_archive": st["by_archive"],
                "by_family": st["by_family"],
                "by_motif_class": st["by_motif_class"],
                "by_render_style": st["by_render_style"],
                "by_split": st["by_split"],
                "commercial_ok_counts": st["commercial_ok_counts"],
                "pruner_reject_reasons": st["pipeline_ingest_summary"]["pruner_reject_reasons"],
                "contact_sheets_count": len(st["contact_sheets"]),
                "figma_sheets": st["figma_sheets"],
            },
            indent=2,
        )
    )
