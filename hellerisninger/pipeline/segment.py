"""Rock-art panel cleaning and smart proximity-based individual motif segmentation.

Removes borders, page frames, scale bars, text captions, and isolated cupmark dots /
speckles while keeping anatomically connected strokes (antlers, crew lines, limbs,
skis, weapons) attached to their parent figure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np
from scipy import ndimage
from scipy.spatial import cKDTree


@dataclass
class MotifCandidate:
    """An isolated rock-art motif extracted from a source panel or vector drawing."""

    index: int
    mask: np.ndarray  # uint8 binary crop (255=fg, 0=bg)
    bbox: Tuple[int, int, int, int]  # (x, y, w, h) in source mask coords
    area_px: int
    raw_component_count: int
    source_shape: Tuple[int, int]  # (H, W) of full source mask
    debug: Dict[str, Any] = field(default_factory=dict)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _estimate_stroke_width(mask_u8: np.ndarray) -> float:
    """Estimate characteristic stroke width (pixels) from fast OpenCV distance transform."""
    bin_u8 = (mask_u8 > 0).astype(np.uint8)
    if not np.any(bin_u8):
        return 3.0
    dt = cv2.distanceTransform(bin_u8, cv2.DIST_L2, 3)
    vals = dt[bin_u8 > 0]
    if len(vals) == 0:
        return 3.0
    p75 = float(np.percentile(vals, 75))
    return max(1.5, min(28.0, 2.0 * p75))


def _remove_border_frames(
    labels: np.ndarray,
    stats: np.ndarray,
    h: int,
    w: int,
) -> Tuple[set[int], int]:
    """Identify component IDs that are outer rectangular page frames or border artifacts."""
    drop: set[int] = set()
    img_area = float(h * w)
    edge_pad = max(3, int(round(0.008 * min(h, w))))

    for cid in range(1, len(stats)):
        x, y, bw, bh, area = [int(v) for v in stats[cid]]
        bbox_area = max(bw * bh, 1)
        fill = area / float(bbox_area)

        touches_left = x <= edge_pad
        touches_right = (x + bw) >= (w - edge_pad)
        touches_top = y <= edge_pad
        touches_bot = (y + bh) >= (h - edge_pad)
        n_edges = int(touches_left) + int(touches_right) + int(touches_top) + int(touches_bot)

        # Classic rectangular outer border frame (thin hollow box around canvas)
        if bw >= 0.72 * w and bh >= 0.72 * h and fill < 0.14:
            drop.add(cid)
            continue

        # Long thin margin line touching edges (top/bottom rule or side rule)
        if n_edges >= 2 and (bw >= 0.80 * w or bh >= 0.80 * h) and fill < 0.25:
            drop.add(cid)
            continue

        # Edge-hugging dark scan border strip (e.g., photocopy shadow along page edge)
        if n_edges >= 1:
            if (touches_top or touches_bot) and bw >= 0.85 * w and bh <= 0.08 * h:
                drop.add(cid)
                continue
            if (touches_left or touches_right) and bh >= 0.85 * h and bw <= 0.08 * w:
                drop.add(cid)
                continue

        # Giant hollow frame anywhere inside page enclosing a big region
        if (bbox_area >= 0.55 * img_area) and fill < 0.06 and (bw >= 0.65 * w and bh >= 0.65 * h):
            drop.add(cid)
            continue

    return drop, len(drop)


def _remove_scale_bars(
    labels: np.ndarray,
    stats: np.ndarray,
    h: int,
    w: int,
    already_dropped: set[int],
) -> Tuple[set[int], int]:
    """Identify straight rectangular scale bars near top/bottom/side margins."""
    drop: set[int] = set()
    margin_y_top = int(0.20 * h)
    margin_y_bot = int(0.80 * h)

    for cid in range(1, len(stats)):
        if cid in already_dropped:
            continue
        x, y, bw, bh, area = [int(v) for v in stats[cid]]
        if area < 40:
            continue
        bbox_area = max(bw * bh, 1)
        rect_ratio = area / float(bbox_area)
        ar = max(bw / max(bh, 1), bh / max(bw, 1))
        cy = y + 0.5 * bh

        # Crisp straight horizontal/vertical ruler bar in margin
        in_margin = (cy <= margin_y_top) or (cy >= margin_y_bot)
        if ar >= 7.5 and rect_ratio >= 0.68 and (in_margin or ar >= 14.0):
            drop.add(cid)
            continue

        # Checkerboard/ticked scale bar in margin (long thin horizontal bar with tick marks)
        if in_margin and bw >= 0.08 * w and bw <= 0.65 * w and bh <= 0.045 * h and (bw / max(bh, 1)) >= 6.0:
            # Verify it is flat top/bottom (low vertical complexity)
            sub = (labels[y : y + bh, x : x + bw] == cid).astype(np.uint8)
            contours, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if contours:
                hull_area = cv2.contourArea(cv2.convexHull(contours[0]))
                solidity = area / max(hull_area, 1.0)
                if solidity >= 0.82 and rect_ratio >= 0.42:
                    drop.add(cid)

    return drop, len(drop)


def _remove_text_blocks(
    labels: np.ndarray,
    stats: np.ndarray,
    h: int,
    w: int,
    already_dropped: set[int],
) -> Tuple[set[int], int]:
    """Detect and remove printed text captions/labels while protecting rock-art crew strokes."""
    min_dim = min(h, w)
    max_glyph_h = max(36, int(round(0.048 * h)))
    min_glyph_h = max(5, int(round(0.005 * h)))

    glyph_cids: List[int] = []
    large_boxes: List[Tuple[int, int, int, int]] = []

    for cid in range(1, len(stats)):
        if cid in already_dropped:
            continue
        x, y, bw, bh, area = [int(v) for v in stats[cid]]
        if max(bw, bh) >= max(55, int(0.065 * min_dim)) or area >= max(500, int(0.0015 * h * w)):
            large_boxes.append((x, y, bw, bh))
        if min_glyph_h <= bh <= max_glyph_h and 2 <= bw <= max(48, int(0.055 * w)):
            ar = bw / max(bh, 1.0)
            if 0.12 <= ar <= 2.4 and area >= 8:
                glyph_cids.append(cid)

    if len(glyph_cids) < 3:
        return set(), 0

    # Group glyph candidates into horizontal text lines using Union-Find
    uf = _UnionFind(len(glyph_cids))
    # Sort by x to prune comparisons
    glyph_cids.sort(key=lambda c: int(stats[c, cv2.CC_STAT_LEFT]))

    for i in range(len(glyph_cids)):
        ci = glyph_cids[i]
        xi, yi, wi, hi = [int(stats[ci, k]) for k in range(4)]
        cyi = yi + 0.5 * hi
        for j in range(i + 1, len(glyph_cids)):
            cj = glyph_cids[j]
            xj, yj, wj, hj = [int(stats[cj, k]) for k in range(4)]
            max_h = max(hi, hj)
            # Horizontal gap
            dx = xj - (xi + wi)
            if dx > 2.2 * max_h:
                break
            cyj = yj + 0.5 * hj
            # Similar height and aligned horizontal baseline
            if abs(cyi - cyj) <= 0.48 * max_h and 0.42 <= (hi / max(hj, 1.0)) <= 2.4:
                uf.union(i, j)

    groups: Dict[int, List[int]] = {}
    for idx, cid in enumerate(glyph_cids):
        root = uf.find(idx)
        groups.setdefault(root, []).append(cid)

    drop: set[int] = set()
    for members in groups.values():
        if len(members) < 3:
            continue
        xs = [int(stats[c, cv2.CC_STAT_LEFT]) for c in members]
        ys = [int(stats[c, cv2.CC_STAT_TOP]) for c in members]
        x2s = [int(stats[c, cv2.CC_STAT_LEFT] + stats[c, cv2.CC_STAT_WIDTH]) for c in members]
        y2s = [int(stats[c, cv2.CC_STAT_TOP] + stats[c, cv2.CC_STAT_HEIGHT]) for c in members]
        hs = [int(stats[c, cv2.CC_STAT_HEIGHT]) for c in members]
        ws = [int(stats[c, cv2.CC_STAT_WIDTH]) for c in members]

        gx0, gy0, gx1, gy1 = min(xs), min(ys), max(x2s), max(y2s)
        med_h = float(np.median(hs))
        g_width = gx1 - gx0
        g_cy = 0.5 * (gy0 + gy1)

        in_margin = (g_cy <= 0.15 * h) or (g_cy >= 0.84 * h)

        # Protect Bronze Age ship crew strokes: crew strokes are mostly thin vertical bars right next to a large figure
        thin_bar_frac = float(np.mean([(ww / max(hh, 1.0)) < 0.34 for ww, hh in zip(ws, hs)]))
        near_large = False
        pad_check = 2.0 * med_h
        for lx, ly, lw, lh in large_boxes:
            if not (gx1 + pad_check < lx or gx0 - pad_check > lx + lw or gy1 + pad_check < ly or gy0 - pad_check > ly + lh):
                near_large = True
                break

        if near_large and thin_bar_frac >= 0.55:
            # Very likely crew strokes on a Bronze Age ship! Do NOT remove.
            continue

        if in_margin and len(members) >= 3 and g_width >= 2.2 * med_h:
            drop.update(members)
        elif (not near_large) and len(members) >= 5 and g_width >= 4.2 * med_h and thin_bar_frac < 0.65:
            drop.update(members)

    return drop, len(drop)


def _sample_contour_points(comp_mask: np.ndarray, x0: int, y0: int, max_pts: int = 120) -> np.ndarray:
    """Return subsampled boundary (x, y) coordinates in global image frame."""
    contours, _ = cv2.findContours(comp_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        ys, xs = np.nonzero(comp_mask)
        if len(xs) == 0:
            return np.empty((0, 2), dtype=np.float32)
        pts = np.stack([xs + x0, ys + y0], axis=1).astype(np.float32)
    else:
        pts_list = [c.reshape(-1, 2) for c in contours]
        pts = np.vstack(pts_list).astype(np.float32)
        pts[:, 0] += x0
        pts[:, 1] += y0

    if len(pts) > max_pts:
        step = len(pts) / float(max_pts)
        idx = np.floor(np.arange(max_pts) * step).astype(int)
        pts = pts[idx]
    return pts


def segment_motifs(
    mask_u8: np.ndarray,
    media_type: str = "bw_tracing",
    max_motifs_per_panel: int = 35,
) -> Tuple[List[MotifCandidate], Dict[str, Any]]:
    """Clean borders/text/scale bars and segment a rock-art mask into isolated motifs."""
    h, w = mask_u8.shape[:2]
    min_dim = min(h, w)
    img_area = float(h * w)
    telemetry: Dict[str, Any] = {
        "source_h": h,
        "source_w": w,
        "dropped_border_frames": 0,
        "dropped_scale_bars": 0,
        "dropped_text_glyphs": 0,
        "dropped_junk_clusters": 0,
        "rejected_reasons": {},
    }

    bin_mask = ((mask_u8 > 0).astype(np.uint8)) * 255
    num_lbl, labels, stats, _ = cv2.connectedComponentsWithStats(bin_mask, connectivity=8)
    if num_lbl <= 1:
        telemetry["rejected_reasons"]["empty_mask"] = 1
        return [], telemetry

    # 1. Strip border frames, scale bars, and printed captions
    drop_frames, n_frames = _remove_border_frames(labels, stats, h, w)
    drop_bars, n_bars = _remove_scale_bars(labels, stats, h, w, drop_frames)
    dropped_so_far = drop_frames | drop_bars
    drop_text, n_text = _remove_text_blocks(labels, stats, h, w, dropped_so_far)

    all_dropped = dropped_so_far | drop_text
    telemetry["dropped_border_frames"] = n_frames
    telemetry["dropped_scale_bars"] = n_bars
    telemetry["dropped_text_glyphs"] = n_text

    # Build cleaned mask
    if all_dropped:
        keep_lut = np.ones(num_lbl, dtype=np.uint8)
        keep_lut[0] = 0
        for cid in all_dropped:
            keep_lut[cid] = 0
        clean_mask = (keep_lut[labels] * 255).astype(np.uint8)
    else:
        clean_mask = bin_mask

    # Re-label cleaned mask
    num_lbl, labels, stats, _ = cv2.connectedComponentsWithStats(clean_mask, connectivity=8)
    if num_lbl <= 1:
        telemetry["rejected_reasons"]["empty_after_cleaning"] = 1
        return [], telemetry

    w_stroke = _estimate_stroke_width(clean_mask)
    telemetry["est_stroke_width_px"] = round(w_stroke, 2)

    # Filter extreme dust specks (< min_speck_area) BEFORE KD-tree grouping to keep it fast
    min_speck_area = max(8, int(round(0.000012 * img_area)))
    comp_ids: List[int] = []
    for cid in range(1, num_lbl):
        if int(stats[cid, cv2.CC_STAT_AREA]) >= min_speck_area:
            comp_ids.append(cid)

    # Cap pathological noisy scans with > 350 fragments by keeping the top 350 by area
    if len(comp_ids) > 350:
        comp_ids.sort(key=lambda c: int(stats[c, cv2.CC_STAT_AREA]), reverse=True)
        comp_ids = comp_ids[:350]

    # Sort left-to-right by x0 so inner pairwise sweep prunes in O(N) average time
    comp_ids.sort(key=lambda c: int(stats[c, cv2.CC_STAT_LEFT]))

    if not comp_ids:
        telemetry["rejected_reasons"]["only_dust_specks"] = 1
        return [], telemetry

    # 2. Extract bounding boxes & sampled boundary points for size-aware proximity grouping
    n_comp = len(comp_ids)
    boxes = np.zeros((n_comp, 4), dtype=np.int32)
    areas = np.zeros(n_comp, dtype=np.int32)
    diags = np.zeros(n_comp, dtype=np.float32)
    trees: List[cKDTree] = []
    pts_list: List[np.ndarray] = []

    for i, cid in enumerate(comp_ids):
        x, y, bw, bh, area = [int(v) for v in stats[cid]]
        boxes[i] = (x, y, bw, bh)
        areas[i] = area
        diags[i] = math.hypot(bw, bh)
        sub = (labels[y : y + bh, x : x + bw] == cid).astype(np.uint8)
        pts = _sample_contour_points(sub, x, y, max_pts=64)
        pts_list.append(pts)
        trees.append(cKDTree(pts) if len(pts) > 0 else cKDTree(np.array([[x, y]], dtype=np.float32)))

    # 3. Size-aware proximity & containment grouping via Union-Find
    uf = _UnionFind(n_comp)
    max_search_r = min(max(3.6 * w_stroke, 0.042 * min_dim), 75.0)
    # Slightly tighter for painted photos where separate painted strokes can be far apart or noisy
    if media_type == "painted_photo":
        max_search_r = min(max(3.0 * w_stroke, 0.032 * min_dim), 58.0)

    for i in range(n_comp):
        xi, yi, wi, hi = boxes[i]
        xi2, yi2 = xi + wi, yi + hi
        for j in range(i + 1, n_comp):
            xj, yj, wj, hj = boxes[j]
            # Sorted by x0: once xj is past xi2 + max_search_r, no subsequent j can be close!
            if xj > xi2 + max_search_r:
                break
            xj2, yj2 = xj + wj, yj + hj

            # Fast bbox gap pruning
            gap_y = max(0, max(yi - yj2, yj - yi2))
            if gap_y > max_search_r:
                continue

            # Exact contour-to-contour minimum distance
            dists, _ = trees[j].query(pts_list[i], k=1)
            d_min = float(np.min(dists))

            s_small = float(min(diags[i], diags[j]))
            s_big = float(max(diags[i], diags[j]))

            # Rule A: Containment (internal life-line, ribs, sun-wheel hub, boat crew inside hull bbox)
            ix0, iy0 = max(xi, xj), max(yi, yj)
            ix1, iy1 = min(xi2, xj2), min(yi2, yj2)
            inter_area = max(0, ix1 - ix0) * max(0, iy1 - iy0)
            min_box_area = max(1, min(wi * hi, wj * hj))
            containment = inter_area / float(min_box_area)
            if containment >= 0.65 and d_min <= min(0.16 * s_big, max_search_r):
                uf.union(i, j)
                continue

            # Rule B: Small appendage / crew stroke / detached antler near a larger figure
            if s_small <= 0.45 * s_big:
                r_attach = min(max(3.0 * w_stroke, 0.085 * s_big), max_search_r)
                if d_min <= r_attach:
                    uf.union(i, j)
                    continue

            # Rule C: Tight gap between two similar-sized parts of one broken contour / double-line hull
            r_tight = min(max(2.0 * w_stroke, 8.0), 0.014 * min_dim)
            if d_min <= r_tight:
                uf.union(i, j)

    # Assemble clusters
    clusters: Dict[int, List[int]] = {}
    for i in range(n_comp):
        clusters.setdefault(uf.find(i), []).append(i)

    # Compute cluster raw areas to calibrate relative cupmark rejection
    cluster_infos = []
    for root, members in clusters.items():
        xs0 = [int(boxes[m, 0]) for m in members]
        ys0 = [int(boxes[m, 1]) for m in members]
        xs1 = [int(boxes[m, 0] + boxes[m, 2]) for m in members]
        ys1 = [int(boxes[m, 1] + boxes[m, 3]) for m in members]
        x0, y0, x1, y1 = min(xs0), min(ys0), max(xs1), max(ys1)
        bw, bh = x1 - x0, y1 - y0
        total_area = int(sum(areas[m] for m in members))
        cluster_infos.append((total_area, x0, y0, bw, bh, members))

    cluster_infos.sort(key=lambda t: t[0], reverse=True)
    # Reference area of substantial figures on this panel
    large_areas = [c[0] for c in cluster_infos if max(c[3], c[4]) >= max(36, int(0.04 * min_dim))]
    ref_area = float(np.median(large_areas)) if large_areas else float(cluster_infos[0][0])

    candidates: List[MotifCandidate] = []
    reject_counts: Dict[str, int] = telemetry["rejected_reasons"]

    def _reject(reason: str) -> None:
        reject_counts[reason] = reject_counts.get(reason, 0) + 1
        telemetry["dropped_junk_clusters"] += 1

    min_motif_dim = max(44, int(round(0.052 * min_dim)))
    min_motif_area = max(280, int(round(0.00045 * img_area)))
    is_multi_panel = len(cluster_infos) >= 3

    for total_area, x0, y0, bw, bh, members in cluster_infos:
        if len(candidates) >= max_motifs_per_panel:
            _reject("panel_motif_cap")
            continue

        # Size gate (eliminates tiny scraps, stray letters, isolated ticks, specks)
        if max(bw, bh) < min_motif_dim or total_area < min_motif_area:
            _reject("too_small")
            continue

        # Drop small corner stamps / north arrows / page numbers on archaeological sheets
        in_corner_x = (x0 + bw <= 0.16 * w) or (x0 >= 0.84 * w)
        in_corner_y = (y0 + bh <= 0.16 * h) or (y0 >= 0.84 * h)
        if is_multi_panel and in_corner_x and in_corner_y and max(bw, bh) < 0.16 * min_dim:
            _reject("corner_annotation_or_arrow")
            continue

        # Drop figures brutally clipped/sliced by the raw image edge on multi-motif panels
        touches_edge = (x0 <= 2) or (y0 <= 2) or (x0 + bw >= w - 2) or (y0 + bh >= h - 2)
        if is_multi_panel and touches_edge and max(bw, bh) < 0.55 * max(h, w):
            _reject("clipped_at_image_edge")
            continue

        # Reject huge tangled whole-panel webs where overlapping cracks/carvings merged across the entire sheet
        if is_multi_panel and bw >= 0.78 * w and bh >= 0.78 * h and len(members) >= 10:
            _reject("tangled_whole_panel")
            continue

        # Build isolated cluster crop mask
        crop = np.zeros((bh, bw), dtype=np.uint8)
        for m in members:
            cid = comp_ids[m]
            mx, my, mw, mh = boxes[m]
            sub = labels[my : my + mh, mx : mx + mw] == cid
            crop[my - y0 : my - y0 + mh, mx - x0 : mx - x0 + mw][sub] = 255

        bbox_area = max(bw * bh, 1)
        rect_ratio = total_area / float(bbox_area)
        ar = max(bw / max(bh, 1.0), bh / max(bw, 1.0))

        # Shape sanity checks on raw cluster
        contours, _ = cv2.findContours(crop, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            _reject("no_contour")
            continue
        all_pts = np.vstack(contours)
        hull = cv2.convexHull(all_pts)
        hull_area = max(float(cv2.contourArea(hull)), 1.0)
        hull_perim = max(float(cv2.arcLength(hull, True)), 1.0)
        solidity = total_area / hull_area
        hull_circ = 4.0 * math.pi * hull_area / (hull_perim * hull_perim)

        # Reject thin straight lines / rock cracks / divider bars
        if ar >= 6.5 and solidity >= 0.72:
            _reject("straight_line_or_bar")
            continue

        # Reject solid rectangular boxes / blocks
        if rect_ratio >= 0.80 and solidity >= 0.90 and ar <= 6.5:
            _reject("solid_rectangle_box")
            continue

        # Ruthlessly reject cup-marks (skålgropar), pebbles, round dots, and featureless ovals!
        filled_crop = ndimage.binary_fill_holes(crop > 0)
        internal_hole_px = int(np.sum(filled_crop)) - int(np.sum(crop > 0))
        has_internal_structure = internal_hole_px >= max(40, int(0.10 * total_area))

        if (not has_internal_structure) and solidity >= 0.87 and hull_circ >= 0.78:
            # Allow elongated footprints (ar >= 1.8), reject round dots/ovals/cupmarks!
            if ar < 1.85:
                _reject("cupmark_or_featureless_oval")
                continue

        # Reject highly fragmented static noise cloud or dense crack web
        if len(members) >= 14 and (total_area / float(bbox_area)) < 0.06:
            _reject("fragmented_noise_cloud")
            continue

        candidates.append(
            MotifCandidate(
                index=len(candidates),
                mask=crop,
                bbox=(x0, y0, bw, bh),
                area_px=total_area,
                raw_component_count=len(members),
                source_shape=(h, w),
                debug={
                    "raw_solidity": round(solidity, 4),
                    "raw_rect_ratio": round(rect_ratio, 4),
                    "raw_aspect": round(ar, 3),
                    "has_internal_structure": has_internal_structure,
                },
            )
        )

    telemetry["candidates_extracted"] = len(candidates)
    return candidates, telemetry
