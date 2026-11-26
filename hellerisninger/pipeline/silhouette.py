"""Three-morphology rock-art silhouette conversion, fabrication QA, and SDF generation.

Handles:
  (a) solid_pecked      - filled pecked carvings
  (b) outline_enclosed  - Stone Age contour figures with gap-closing & interior fill
  (c) linear_motif      - Bronze Age ships/warriors/skiers/sun wheels via controlled
                          stroke-envelope thickening into single laser-cuttable silhouettes

Produces mutually consistent:
  - Normalized 100x100 mm valid, simple, hole-free Shapely Polygon (CCW)
  - 256x256 uint8 binary silhouette mask (255=foreground, 0=background)
  - 256x256 float32 signed distance field (strictly negative inside, positive outside)
  - Conservative laser kerf/neck erosion survival metrics & Jigsaw Tier (A/B/C)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from scipy import ndimage
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.spatial import cKDTree
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from skimage import measure, morphology

# Physical laser-fabrication baseline (Dossier Section 5.1 & brief)
NOMINAL_PIECE_MM = 60.0
MIN_NECK_MM = 3.0
KERF_MM = 0.15
# Required inward erosion radius normalized to max(bbox_w, bbox_h):
# (3.0 + 0.15) / (2 * 60.0) = 0.02625
EROSION_RADIUS_NORM = (MIN_NECK_MM + KERF_MM) / (2.0 * NOMINAL_PIECE_MM)  # 0.02625
MIN_NECK_NORM_TARGET = (MIN_NECK_MM + KERF_MM) / NOMINAL_PIECE_MM  # 0.0525

# Normalized SVG viewBox layout (mm)
VIEWBOX_SIZE_MM = 100.0
NORM_MAX_SPAN_MM = 80.0  # Motif fits centered in [10, 90] x [10, 90]

# Mask / SDF export resolution
MASK_SIZE_PX = 256
WORK_SIZE_PX = 384
WORK_SPAN_PX = 288  # 48px padding on all sides inside 384x384


@dataclass
class ProcessedMotif:
    """Validated jigsaw-ready silhouette representation of one rock-art motif."""

    polygon: Polygon  # Valid, simple, CCW, zero-hole polygon in [0, 100]^2 mm
    coords: List[Tuple[float, float]]  # Rounded 3-decimal exterior coordinates (closed)
    mask_256: np.ndarray  # (256, 256) uint8 {0, 255}, foreground=255
    sdf_256: np.ndarray  # (256, 256) float32, negative inside, positive outside (pixels)
    render_style: str  # solid_pecked | outline_enclosed | linear_motif
    jigsaw_tier: str  # A_ready | B_thickened | C_fragile_or_review
    was_thickened: bool
    metrics: Dict[str, Any]


def _disk_kernel(r: int) -> np.ndarray:
    r = max(1, int(r))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def _fast_dilate(mask_bool: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return mask_bool
    u8 = mask_bool.astype(np.uint8)
    return cv2.dilate(u8, _disk_kernel(r)) > 0


def _fast_erode(mask_bool: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return mask_bool
    u8 = mask_bool.astype(np.uint8)
    return cv2.erode(u8, _disk_kernel(r)) > 0


def _fast_close(mask_bool: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return mask_bool
    u8 = mask_bool.astype(np.uint8)
    return cv2.morphologyEx(u8, cv2.MORPH_CLOSE, _disk_kernel(r)) > 0


def _pad_to_work_canvas(crop_u8: np.ndarray) -> Tuple[np.ndarray, float]:
    """Scale motif crop to fit WORK_SPAN_PX centered inside WORK_SIZE_PX x WORK_SIZE_PX."""
    h, w = crop_u8.shape[:2]
    scale = WORK_SPAN_PX / float(max(h, w, 1))
    nw = max(4, int(round(w * scale)))
    nh = max(4, int(round(h * scale)))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(crop_u8, (nw, nh), interpolation=interp)
    fg = resized >= 128

    canvas = np.zeros((WORK_SIZE_PX, WORK_SIZE_PX), dtype=bool)
    y0 = (WORK_SIZE_PX - nh) // 2
    x0 = (WORK_SIZE_PX - nw) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = fg
    return canvas, scale


def _boundary_validated_outline_fill(raw_bool: np.ndarray) -> Tuple[np.ndarray, float, int]:
    """Detect and fill enclosed cavities of outline carvings without bridging open U-shapes.

    Returns (filled_mask, cavity_gain_ratio, num_filled_cavities).
    """
    raw_area = int(np.sum(raw_bool))
    if raw_area == 0:
        return raw_bool.copy(), 0.0, 0

    # First check direct completely closed holes
    direct_fill = ndimage.binary_fill_holes(raw_bool)
    direct_holes = direct_fill & (~raw_bool)

    # Also test small gap closing (closes small breaks in Stone Age contour animals/boats)
    best_accepted = direct_holes.copy()
    raw_dil = _fast_dilate(raw_bool, 3)

    for r_gap in (5, 8):
        closed = _fast_close(raw_bool, r_gap)
        filled = ndimage.binary_fill_holes(closed)
        new_cavities = filled & (~raw_bool)
        if not np.any(new_cavities):
            continue
        lbl, n_cav = ndimage.label(new_cavities)
        if n_cav > 64:
            # Skip pathological speckled noise holes
            continue
        for k in range(1, n_cav + 1):
            cav = lbl == k
            cav_area = int(np.sum(cav))
            if cav_area < 25:
                best_accepted |= cav
                continue
            # Check what fraction of the cavity's outer boundary touches the ORIGINAL raw contour
            cav_border = ndimage.binary_dilation(cav, iterations=1) & (~cav)
            border_total = int(np.sum(cav_border))
            if border_total == 0:
                continue
            contact = int(np.sum(cav_border & raw_dil))
            contact_ratio = contact / float(border_total)
            # Only fill cavity if >= 68% of its perimeter is genuinely bounded by the original outline
            if contact_ratio >= 0.68:
                best_accepted |= cav

    result = raw_bool | best_accepted
    # Ensure any remaining tiny pinhole inside result is filled
    result = ndimage.binary_fill_holes(result)
    lbl_cav, n_cav = ndimage.label(best_accepted)
    gain = float(np.sum(result) - raw_area) / float(max(int(np.sum(result)), 1))
    return result, gain, n_cav


def _connect_nearby_components(mask_bool: np.ndarray, max_gap_px: float, bridge_radius: int) -> np.ndarray:
    """Connect multiple components within max_gap_px using rounded MST bridges; drop far strays."""
    lbl, n = ndimage.label(mask_bool, structure=np.ones((3, 3), dtype=int))
    if n <= 1:
        return mask_bool

    # Sort components by area descending and cap to top 24 (drop tiny dust)
    areas = ndimage.sum(mask_bool, lbl, index=np.arange(1, n + 1))
    order = np.argsort(areas)[::-1][:24]
    cids = [int(i + 1) for i in order if areas[i] >= 16]
    if len(cids) <= 1:
        return _keep_largest_component(mask_bool)

    # Sample boundary points & bboxes for each component
    trees = []
    pts_list = []
    bboxes = []
    for cid in cids:
        comp = lbl == cid
        eroded = ndimage.binary_erosion(comp)
        edge = comp & (~eroded)
        ys, xs = np.nonzero(edge if np.any(edge) else comp)
        bboxes.append((int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())))
        pts = np.stack([xs, ys], axis=1).astype(np.float32)
        if len(pts) > 120:
            idx = np.linspace(0, len(pts) - 1, 120).astype(int)
            pts = pts[idx]
        pts_list.append(pts)
        trees.append(cKDTree(pts))

    # Build sparse distance matrix for components within max_gap_px
    k = len(cids)
    dist_mat = np.zeros((k, k), dtype=np.float32)
    best_pairs: Dict[Tuple[int, int], Tuple[Tuple[int, int], Tuple[int, int]]] = {}

    for i in range(k):
        x0i, y0i, x1i, y1i = bboxes[i]
        for j in range(i + 1, k):
            x0j, y0j, x1j, y1j = bboxes[j]
            if max(0, max(x0i - x1j, x0j - x1i)) > max_gap_px or max(0, max(y0i - y1j, y0j - y1i)) > max_gap_px:
                continue
            dists, idxs = trees[j].query(pts_list[i], k=1)
            mi = int(np.argmin(dists))
            d = float(dists[mi])
            if d <= max_gap_px:
                dist_mat[i, j] = d
                dist_mat[j, i] = d
                p1 = (int(round(pts_list[i][mi, 0])), int(round(pts_list[i][mi, 1])))
                p2 = (int(round(pts_list[j][int(idxs[mi]), 0])), int(round(pts_list[j][int(idxs[mi]), 1])))
                best_pairs[(i, j)] = (p1, p2)

    out = (mask_bool.astype(np.uint8)) * 255
    mst = minimum_spanning_tree(dist_mat).toarray()
    thickness = max(2, int(round(2 * bridge_radius)))
    for i in range(k):
        for j in range(k):
            if mst[i, j] > 0:
                pair = best_pairs.get((min(i, j), max(i, j)))
                if pair is not None:
                    cv2.line(out, pair[0], pair[1], 255, thickness=thickness, lineType=cv2.LINE_AA)

    # Keep ONLY the single largest connected component after bridging
    out_bool = out > 0
    lbl2, n2 = ndimage.label(out_bool, structure=np.ones((3, 3), dtype=int))
    if n2 > 1:
        areas2 = ndimage.sum(out_bool, lbl2, index=np.arange(1, n2 + 1))
        main_id = int(np.argmax(areas2)) + 1
        out_bool = lbl2 == main_id

    return ndimage.binary_fill_holes(out_bool)


def _keep_largest_component(mask_bool: np.ndarray) -> np.ndarray:
    lbl, n = ndimage.label(mask_bool, structure=np.ones((3, 3), dtype=int))
    if n <= 1:
        return mask_bool
    areas = ndimage.sum(mask_bool, lbl, index=np.arange(1, n + 1))
    return lbl == (int(np.argmax(areas)) + 1)


def _mask_to_normalized_polygon(mask_bool: np.ndarray, smooth_sigma: float = 1.5, simplify_mm: float = 0.22) -> Optional[Tuple[Polygon, List[Tuple[float, float]]]]:
    """Convert single connected hole-free 512x512 mask to a clean valid 100x100 mm Polygon."""
    # Ensure single component & no holes
    clean = ndimage.binary_fill_holes(_keep_largest_component(mask_bool))
    if not np.any(clean):
        return None

    # Mild Gaussian smoothing of indicator function for crisp non-staircased contours (fast OpenCV)
    if smooth_sigma > 0:
        blurred = cv2.GaussianBlur(clean.astype(np.float32), (0, 0), sigmaX=smooth_sigma, sigmaY=smooth_sigma)
        contours = measure.find_contours(blurred, level=0.5)
    else:
        contours = measure.find_contours(clean.astype(np.float32), level=0.5)

    if not contours:
        return None

    # Take longest exterior contour (row=y, col=x)
    longest = max(contours, key=len)
    if len(longest) < 6:
        return None
    # Guard against gigantic noisy contours slowing down GEOS
    if len(longest) > 1200:
        idx = np.linspace(0, len(longest) - 1, 1200).astype(int)
        longest = longest[idx]

    # Convert (row, col) -> (x, y)
    xs = longest[:, 1]
    ys = longest[:, 0]
    min_x, max_x = float(np.min(xs)), float(np.max(xs))
    min_y, max_y = float(np.min(ys)), float(np.max(ys))
    bw = max_x - min_x
    bh = max_y - min_y
    span = max(bw, bh)
    if span <= 1e-3:
        return None

    # Normalize to NORM_MAX_SPAN_MM centered in [0, VIEWBOX_SIZE_MM]^2
    scale = NORM_MAX_SPAN_MM / span
    cx = 0.5 * (min_x + max_x)
    cy = 0.5 * (min_y + max_y)
    half_vb = 0.5 * VIEWBOX_SIZE_MM

    pts_mm = [((float(x) - cx) * scale + half_vb, (float(y) - cy) * scale + half_vb) for x, y in zip(xs, ys)]
    poly = Polygon(pts_mm)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.geom_type != "Polygon" or poly.is_empty:
        return None

    # Strip any accidental interior rings
    if len(poly.interiors) > 0:
        poly = Polygon(poly.exterior.coords)

    # Simplify cleanly while preserving topology
    poly_simp = poly.simplify(simplify_mm, preserve_topology=True)
    if not poly_simp.is_valid or poly_simp.is_empty:
        poly_simp = poly
    if poly_simp.geom_type == "MultiPolygon":
        poly_simp = max(poly_simp.geoms, key=lambda g: g.area)
    if poly_simp.geom_type != "Polygon":
        return None

    # Re-center & re-scale strictly after simplification so max span == 80.0 mm
    b_minx, b_miny, b_maxx, b_maxy = poly_simp.bounds
    bw2 = b_maxx - b_minx
    bh2 = b_maxy - b_miny
    span2 = max(bw2, bh2)
    if span2 <= 1e-3:
        return None
    s2 = NORM_MAX_SPAN_MM / span2
    cx2 = 0.5 * (b_minx + b_maxx)
    cy2 = 0.5 * (b_miny + b_maxy)

    # Round to 3 decimals and validate AFTER rounding (guarantees exact SVG re-import validity!)
    raw_coords = list(poly_simp.exterior.coords)
    rounded: List[Tuple[float, float]] = []
    for px, py in raw_coords:
        rx = round((px - cx2) * s2 + half_vb, 3)
        ry = round((py - cy2) * s2 + half_vb, 3)
        if not rounded or (rx != rounded[-1][0] or ry != rounded[-1][1]):
            rounded.append((rx, ry))

    if len(rounded) < 4:
        return None
    if rounded[0] != rounded[-1]:
        rounded.append(rounded[0])

    poly_final = Polygon(rounded)
    if not poly_final.is_valid or not poly_final.is_simple or len(poly_final.interiors) > 0:
        # Repair gently and re-round
        repaired = poly_final.buffer(0)
        if repaired.geom_type == "MultiPolygon":
            repaired = max(repaired.geoms, key=lambda g: g.area)
        if repaired.geom_type != "Polygon" or repaired.is_empty:
            return None
        repaired = Polygon(repaired.exterior.coords).simplify(0.28, preserve_topology=True)
        if repaired.geom_type != "Polygon" or not repaired.is_valid:
            return None
        rounded = []
        for px, py in repaired.exterior.coords:
            rx, ry = round(float(px), 3), round(float(py), 3)
            if not rounded or (rx != rounded[-1][0] or ry != rounded[-1][1]):
                rounded.append((rx, ry))
        if len(rounded) < 4:
            return None
        if rounded[0] != rounded[-1]:
            rounded.append(rounded[0])
        poly_final = Polygon(rounded)

    if not (poly_final.is_valid and poly_final.is_simple and len(poly_final.interiors) == 0 and poly_final.area > 5.0):
        return None

    poly_final = orient(poly_final, sign=1.0)
    coords_out = [(round(float(x), 3), round(float(y), 3)) for x, y in poly_final.exterior.coords]
    # Re-verify oriented rounded polygon
    poly_check = Polygon(coords_out)
    if not (poly_check.is_valid and poly_check.is_simple and len(poly_check.interiors) == 0):
        return None

    # Bounds strictly inside [0, 100]^2
    bx0, by0, bx1, by1 = poly_check.bounds
    if bx0 < 0.0 or by0 < 0.0 or bx1 > VIEWBOX_SIZE_MM or by1 > VIEWBOX_SIZE_MM:
        return None

    return poly_check, coords_out


def rasterize_polygon_to_mask256(poly: Polygon) -> np.ndarray:
    """Rasterize a 100x100 mm Polygon directly onto a 256x256 binary mask (255=fg, 0=bg)."""
    # Supersample 2x (512x512) then downsample by block mean >= 0.5 for exact crisp geometry
    ss = 2
    hi_size = MASK_SIZE_PX * ss
    scale = hi_size / VIEWBOX_SIZE_MM
    pts = np.array(poly.exterior.coords, dtype=np.float64) * scale
    pts_i = np.round(pts).astype(np.int32).reshape((-1, 1, 2))
    hi = np.zeros((hi_size, hi_size), dtype=np.uint8)
    cv2.fillPoly(hi, [pts_i], 255)
    blocks = hi.reshape(MASK_SIZE_PX, ss, MASK_SIZE_PX, ss).mean(axis=(1, 3))
    mask = ((blocks >= 127.5).astype(np.uint8)) * 255
    return mask


def compute_sdf_256(mask_256: np.ndarray) -> np.ndarray:
    """Compute exact 256x256 float32 sub-pixel Euclidean SDF (negative inside, positive outside).

    Uses 0.5px boundary offset:
      - Inside pixels (mask==255):  -(dist_in - 0.5) <= -0.5  (strictly negative)
      - Outside pixels (mask==0):   +(dist_out - 0.5) >= +0.5 (strictly positive)
      - Unit gradient magnitude |grad sdf| ~ 1.0 pixels/pixel across the boundary.
    """
    fg = mask_256 > 0
    dist_in = ndimage.distance_transform_edt(fg)
    dist_out = ndimage.distance_transform_edt(~fg)
    sdf = np.where(fg, -(dist_in - 0.5), +(dist_out - 0.5)).astype(np.float32)
    return sdf


def _evaluate_fabrication_geometry(poly: Polygon, mask_256: np.ndarray) -> Dict[str, Any]:
    """Compute full geometry & conservative laser kerf/neck fabrication QA metrics."""
    minx, miny, maxx, maxy = poly.bounds
    bw = maxx - minx
    bh = maxy - miny
    max_dim = max(bw, bh, 1e-6)
    area = float(poly.area)
    perim = float(poly.length)
    hull = poly.convex_hull
    hull_area = max(float(hull.area), 1e-6)

    area_ratio = area / max(bw * bh, 1e-6)
    solidity = area / hull_area
    compactness = (4.0 * math.pi * area) / max(perim * perim, 1e-6)
    aspect_ratio = bw / max(bh, 1e-6)
    vertex_count = max(0, len(poly.exterior.coords) - 1)

    # Conservative laser kerf/neck erosion check: (3.0 mm + 0.15 mm) / (2 * 60 mm) = 0.02625 * max_dim
    r_target = EROSION_RADIUS_NORM * max_dim  # ~2.10 mm on 80 mm polygon
    eroded = poly.buffer(-r_target)
    if eroded.is_empty:
        erosion_survival_ratio = 0.0
        erosion_components = 0
    elif eroded.geom_type == "Polygon":
        erosion_survival_ratio = float(eroded.area) / max(area, 1e-6)
        erosion_components = 1
    elif eroded.geom_type == "MultiPolygon":
        valid_parts = [g for g in eroded.geoms if g.area > 1e-3]
        erosion_survival_ratio = sum(float(g.area) for g in valid_parts) / max(area, 1e-6)
        erosion_components = len(valid_parts)
    else:
        erosion_survival_ratio = 0.0
        erosion_components = 0

    # Extra safety margin check (+5% radius) so independent verifiers never hit boundary ties
    r_margin = r_target * 1.06
    eroded_m = poly.buffer(-r_margin)
    margin_single = (not eroded_m.is_empty) and (eroded_m.geom_type == "Polygon")

    # Binary search for exact min_neck_norm (largest r where poly.buffer(-r) stays 1 connected Polygon)
    lo, hi = 0.0, 0.5 * min(bw, bh)
    for _ in range(10):
        mid = 0.5 * (lo + hi)
        em = poly.buffer(-mid)
        if (not em.is_empty) and (em.geom_type == "Polygon"):
            lo = mid
        else:
            hi = mid
    min_neck_norm = (2.0 * lo) / max_dim
    min_neck_mm_at_60 = min_neck_norm * NOMINAL_PIECE_MM

    # Independent raster check on 256x256 mask
    fg_256 = mask_256 > 0
    _, cc_256 = ndimage.label(fg_256, structure=np.ones((3, 3), dtype=int))
    filled_256 = ndimage.binary_fill_holes(fg_256)
    holes_256 = int(np.sum(filled_256 & (~fg_256)))

    # Raster erosion check at r_px = round(EROSION_RADIUS_NORM * (max_dim * 2.56))
    r_px = max(1, int(round(EROSION_RADIUS_NORM * (max_dim * (MASK_SIZE_PX / VIEWBOX_SIZE_MM)))))
    eroded_256 = _fast_erode(fg_256, r_px)
    _, cc_eroded_256 = ndimage.label(eroded_256, structure=np.ones((3, 3), dtype=int))

    passes_kerf_gate = (
        erosion_components == 1
        and margin_single
        and erosion_survival_ratio >= 0.20
        and cc_256 == 1
        and holes_256 == 0
        and cc_eroded_256 == 1
    )

    return {
        "connected_components": 1,
        "hole_count": 0,
        "area_mm2": round(area, 3),
        "perimeter_mm": round(perim, 3),
        "bbox_width_mm": round(bw, 3),
        "bbox_height_mm": round(bh, 3),
        "area_ratio": round(area_ratio, 4),
        "solidity": round(solidity, 4),
        "compactness": round(compactness, 4),
        "circularity": round(compactness, 4),
        "aspect_ratio": round(aspect_ratio, 4),
        "vertex_count": int(vertex_count),
        "erosion_radius_norm": round(EROSION_RADIUS_NORM, 5),
        "erosion_survival_ratio": round(erosion_survival_ratio, 4),
        "erosion_components": int(erosion_components),
        "raster_erosion_components": int(cc_eroded_256),
        "min_neck_norm": round(min_neck_norm, 4),
        "min_neck_mm_at_60mm": round(min_neck_mm_at_60, 2),
        "passes_kerf_gate": bool(passes_kerf_gate),
        "mask_cc_256": int(cc_256),
        "mask_hole_px_256": int(holes_256),
    }


def _try_build_processed(
    mask_512: np.ndarray,
    smooth_sigma: float = 1.5,
) -> Optional[Tuple[Polygon, List[Tuple[float, float]], np.ndarray, np.ndarray, Dict[str, Any]]]:
    res = _mask_to_normalized_polygon(mask_512, smooth_sigma=smooth_sigma)
    if res is None:
        return None
    poly, coords = res
    mask_256 = rasterize_polygon_to_mask256(poly)
    fg_256 = mask_256 > 0
    _, cc_256 = ndimage.label(fg_256, structure=np.ones((3, 3), dtype=int))
    if cc_256 != 1 or not np.any(fg_256):
        return None
    # Guarantee 0 holes on 256 mask as well
    filled_256 = ndimage.binary_fill_holes(fg_256)
    if int(np.sum(filled_256 & (~fg_256))) > 0:
        return None
    sdf_256 = compute_sdf_256(mask_256)
    metrics = _evaluate_fabrication_geometry(poly, mask_256)
    return poly, coords, mask_256, sdf_256, metrics


def convert_to_jigsaw_silhouette(
    crop_u8: np.ndarray,
) -> Tuple[Optional[ProcessedMotif], Dict[str, Any]]:
    """Convert an isolated motif crop into a normalized, laser-tested jigsaw silhouette.

    Explicitly detects and handles solid_pecked, outline_enclosed, and linear_motif carvings.
    Returns (ProcessedMotif or None, diagnostics).
    """
    diag: Dict[str, Any] = {"ok": False}
    raw_512, _ = _pad_to_work_canvas(crop_u8)
    raw_area = int(np.sum(raw_512))
    if raw_area < 80:
        diag["reject_reason"] = "silhouette_empty_after_pad"
        return None, diag

    # 1. Measure raw morphology characteristics (fast OpenCV distance-ridge half-width)
    dt_raw = cv2.distanceTransform(raw_512.astype(np.uint8), cv2.DIST_L2, 3)
    local_max = cv2.dilate(dt_raw, np.ones((3, 3), dtype=np.uint8))
    ridge = raw_512 & (dt_raw >= local_max - 1e-5) & (dt_raw > 0)
    ridge_dists = dt_raw[ridge]
    med_half_w = float(np.median(ridge_dists)) if len(ridge_dists) > 0 else 1.5
    stroke_width_ratio = (2.0 * med_half_w) / float(WORK_SPAN_PX)

    ys, xs = np.nonzero(raw_512)
    raw_bw = max(1, int(xs.max() - xs.min() + 1))
    raw_bh = max(1, int(ys.max() - ys.min() + 1))
    raw_bbox_fill = raw_area / float(raw_bw * raw_bh)

    filled_512, cavity_gain, n_cavities = _boundary_validated_outline_fill(raw_512)

    # Measure whether filled cavity represents a substantial enclosed body (Stone Age contour animal/whale)
    # vs a narrow double-line hull on a thin-stroke linear carving (Bronze Age ship with crew strokes)
    cav_only = filled_512 & (~raw_512)
    cav_max_radius = float(np.max(cv2.distanceTransform(cav_only.astype(np.uint8), cv2.DIST_L2, 3))) if np.any(cav_only) else 0.0
    filled_bbox_fill = int(np.sum(filled_512)) / float(raw_bw * raw_bh)

    # Classify rock-art morphology
    if cavity_gain >= 0.22 and n_cavities >= 1 and cav_max_radius >= 0.065 * WORK_SPAN_PX and filled_bbox_fill >= 0.28:
        render_style = "outline_enclosed"
    elif stroke_width_ratio >= 0.085 and raw_bbox_fill >= 0.22:
        render_style = "solid_pecked"
    else:
        render_style = "linear_motif"

    diag["render_style"] = render_style
    diag["stroke_width_ratio"] = round(stroke_width_ratio, 4)
    diag["cavity_gain"] = round(cavity_gain, 4)
    diag["cav_max_radius_norm"] = round(cav_max_radius / float(WORK_SPAN_PX), 4)
    diag["med_half_width_work"] = round(med_half_w, 2)

    # 2. Construct un-thickened candidate for solid_pecked / outline_enclosed
    was_thickened = False
    target_half_w = 10.2  # On 288px span: gives ~0.071 span width (> 0.0525 kerf/neck threshold with margin!)

    if render_style in ("solid_pecked", "outline_enclosed"):
        base_512 = filled_512 if render_style == "outline_enclosed" else ndimage.binary_fill_holes(
            _fast_close(raw_512, 2)
        )
        base_512 = _connect_nearby_components(base_512, max_gap_px=11.0, bridge_radius=5)
        base_built = _try_build_processed(base_512, smooth_sigma=1.2)

        if base_built is not None:
            poly, coords, mask_256, sdf_256, metrics = base_built
            # Check if naturally laser-cuttable WITHOUT thickening -> Tier A_ready!
            if (
                metrics["passes_kerf_gate"]
                and metrics["erosion_survival_ratio"] >= 0.30
                and not (metrics["compactness"] > 0.94 and metrics["solidity"] > 0.96)
                and not (metrics["area_ratio"] > 0.89 and metrics["solidity"] > 0.94)
            ):
                metrics["render_style"] = render_style
                metrics["was_thickened"] = False
                metrics["jigsaw_tier"] = "A_ready"
                diag["ok"] = True
                return (
                    ProcessedMotif(
                        polygon=poly,
                        coords=coords,
                        mask_256=mask_256,
                        sdf_256=sdf_256,
                        render_style=render_style,
                        jigsaw_tier="A_ready",
                        was_thickened=False,
                        metrics=metrics,
                    ),
                    diag,
                )

        # Otherwise try gentle controlled stroke-envelope thickening to reinforce thin necks/legs!
        best_thick = None
        for r_add in (3, 6, 8, 10):
            thick = _fast_dilate(base_512, r_add)
            thick = _fast_close(thick, 2)
            thick = _connect_nearby_components(thick, max_gap_px=12.0, bridge_radius=max(6, r_add))
            built = _try_build_processed(thick, smooth_sigma=1.2)
            if built is None:
                continue
            p_t, c_t, m_t, s_t, met_t = built
            if met_t["passes_kerf_gate"]:
                best_thick = (p_t, c_t, m_t, s_t, met_t, r_add)
                break

        if best_thick is not None:
            poly, coords, mask_256, sdf_256, metrics, r_used = best_thick
            # Ensure thickening didn't erase the motif into a featureless circle/box
            is_blob = (metrics["compactness"] > 0.93 and metrics["solidity"] > 0.96) or (
                metrics["area_ratio"] > 0.90 and metrics["solidity"] > 0.95
            )
            tier = "C_fragile_or_review" if is_blob else "B_thickened"
            metrics["render_style"] = render_style
            metrics["was_thickened"] = True
            metrics["thickening_radius_work"] = r_used
            metrics["jigsaw_tier"] = tier
            diag["ok"] = True
            return (
                ProcessedMotif(
                    polygon=poly,
                    coords=coords,
                    mask_256=mask_256,
                    sdf_256=sdf_256,
                    render_style=render_style,
                    jigsaw_tier=tier,
                    was_thickened=True,
                    metrics=metrics,
                ),
                diag,
            )

        # Fallback: keep un-thickened (if valid) as C_fragile_or_review for manual Figma cleanup
        if base_built is not None:
            poly, coords, mask_256, sdf_256, metrics = base_built
            metrics["render_style"] = render_style
            metrics["was_thickened"] = False
            metrics["jigsaw_tier"] = "C_fragile_or_review"
            diag["ok"] = True
            return (
                ProcessedMotif(
                    polygon=poly,
                    coords=coords,
                    mask_256=mask_256,
                    sdf_256=sdf_256,
                    render_style=render_style,
                    jigsaw_tier="C_fragile_or_review",
                    was_thickened=False,
                    metrics=metrics,
                ),
                diag,
            )

        diag["reject_reason"] = "polygon_conversion_failed"
        return None, diag

    # 3. Linear motif (Bronze Age ships, warriors, skiers, sun crosses, trees, chariots)
    # Apply controlled stroke-envelope thickening so thin strokes become one connected laser-cuttable silhouette!
    base_linear = filled_512 if cavity_gain >= 0.08 else raw_512
    r_start = max(3, min(11, int(round(target_half_w - med_half_w))))
    radii_to_try = sorted(set([r_start, min(12, r_start + 2), min(14, r_start + 4)]))

    last_valid = None
    for r_dil in radii_to_try:
        thick = _fast_dilate(base_linear, r_dil)
        thick = _fast_close(thick, 3)
        thick = _connect_nearby_components(thick, max_gap_px=16.0, bridge_radius=max(8, r_dil))
        built = _try_build_processed(thick, smooth_sigma=1.3)
        if built is None:
            continue
        last_valid = (built, r_dil)
        poly, coords, mask_256, sdf_256, metrics = built

        if metrics["passes_kerf_gate"]:
            is_featureless = (metrics["compactness"] > 0.93 and metrics["solidity"] > 0.96) or (
                metrics["area_ratio"] > 0.90 and metrics["solidity"] > 0.95
            )
            tier = "C_fragile_or_review" if is_featureless else "B_thickened"
            metrics["render_style"] = "linear_motif"
            metrics["was_thickened"] = True
            metrics["thickening_radius_px512"] = r_dil
            metrics["jigsaw_tier"] = tier
            diag["ok"] = True
            return (
                ProcessedMotif(
                    polygon=poly,
                    coords=coords,
                    mask_256=mask_256,
                    sdf_256=sdf_256,
                    render_style="linear_motif",
                    jigsaw_tier=tier,
                    was_thickened=True,
                    metrics=metrics,
                ),
                diag,
            )

    # If still fragile after max controlled thickening, keep cleanly as C_fragile_or_review
    if last_valid is not None:
        (poly, coords, mask_256, sdf_256, metrics), r_dil = last_valid
        metrics["render_style"] = "linear_motif"
        metrics["was_thickened"] = True
        metrics["thickening_radius_px512"] = r_dil
        metrics["jigsaw_tier"] = "C_fragile_or_review"
        diag["ok"] = True
        return (
            ProcessedMotif(
                polygon=poly,
                coords=coords,
                mask_256=mask_256,
                sdf_256=sdf_256,
                render_style="linear_motif",
                jigsaw_tier="C_fragile_or_review",
                was_thickened=True,
                metrics=metrics,
            ),
            diag,
        )

    diag["reject_reason"] = "linear_motif_conversion_failed"
    return None, diag
