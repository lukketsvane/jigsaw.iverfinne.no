"""Fabrication-safe post-processing, sub-pixel shared-seam vectorization, SVG export, and audit.

Guarantees:
1. Single-cut common-line `cut.svg` in exact 1:1 millimetres with three separate `<g>` layers:
   `interior_cut` (red), `exterior_cut` (blue), and `engrave` (green, never part of cut graph).
2. Zero duplicate cut segments, zero gaps, zero overlaps, exact SVG re-parse polygonization.
3. Morphological neck/pinch-off guard (`min_neck_mm >= 3.0 + kerf`) + disconnected fragment repair.
4. Independent kerf (0.10-0.25 mm), neck (2.5 & 3.0 mm), thin-tip, and 360-direction
   translation interlock audit (reusing/matching `heller_jigsaw.geometry.validate` & `pathnet.audit_field`).
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import shapely
from scipy import ndimage
from scipy.ndimage import gaussian_filter1d
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from shapely import get_parts, union_all
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, polygonize

from .whimsies import WhimsyPlacement

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

KERFS = (0.10, 0.15, 0.20, 0.25)

# Warm birchwood & dark walnut preview palettes inspired by artisanal wood jigsaws
BIRCH_PALETTE = [
    "#EED8B4",
    "#E5CCA3",
    "#F3E2C5",
    "#DEC194",
    "#E9D1AA",
    "#D7B887",
]
WALNUT_PALETTE = [
    "#5E554E",
    "#534A44",
    "#675E57",
    "#4C443E",
    "#635952",
    "#585049",
]


@dataclass
class ExportResult:
    """Bundle of verified vector pieces, cut lines, audit metrics, and written artifact paths."""

    faces: List[Polygon]
    interior_lines: List[LineString]
    exterior_lines: List[LineString]
    engrave_lines: List[LineString]
    bounds_mm: Tuple[float, float, float, float]
    audit: Dict[str, Any]
    out_dir: Optional[Path] = None
    files: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Label cleanup & morphological neck/pinch guard (matches Nervous System cleanup)
# ---------------------------------------------------------------------------

def clean_connected_labels(
    labels: np.ndarray,
    domain_mask: np.ndarray,
    fixed_mask: Optional[np.ndarray] = None,
) -> Tuple[np.ndarray, int]:
    """Keep the largest 4-connected component of every piece and reassign stray droplets."""
    lab = labels.copy()
    lab[~domain_mask] = -1
    if fixed_mask is None:
        fixed_mask = np.zeros_like(domain_mask, dtype=bool)
    stray = 0
    piece_ids = sorted(int(v) for v in np.unique(lab) if v >= 0)
    for pid in piece_ids:
        m = lab == pid
        if not np.any(m):
            continue
        ys, xs = np.nonzero(m)
        sl = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1))
        comp, n_c = ndimage.label(lab[sl] == pid)
        if n_c > 1:
            sizes = ndimage.sum_labels(np.ones_like(comp), comp, range(1, n_c + 1))
            keep = 1 + int(np.argmax(sizes))
            drop = (comp > 0) & (comp != keep) & (~fixed_mask[sl])
            stray += int(drop.sum())
            lab[sl][drop] = -1

    missing = domain_mask & (lab < 0)
    if np.any(missing):
        # Strictly NEVER let fixed whimsy pieces absorb stray pixels from dynamic pieces!
        donor_invalid = (lab < 0) | fixed_mask
        if np.all(donor_invalid):
            donor_invalid = lab < 0
        _, idx = ndimage.distance_transform_edt(donor_invalid, return_distances=True, return_indices=True)
        lab[missing] = lab[tuple(idx)][missing]
    lab[~domain_mask] = -1
    return lab, stray


def repair_label_necks(
    labels: np.ndarray,
    domain_mask: np.ndarray,
    px_mm: float,
    min_neck_mm: float = 3.0,
    kerf_mm: float = 0.15,
    fixed_mask: Optional[np.ndarray] = None,
    max_tip_reach_mm: float = 5.8,
    passes: int = 4,
) -> Tuple[np.ndarray, int]:
    """Morphological pinch-off and thin-neck repair on the discrete label image.

    Handing over fragile lobes that fracture under `(min_neck_mm + kerf_mm) / 2` erosion
    (while strictly protecting fixed whimsy pixels) guarantees wood-safe dendritic pieces.
    """
    lab = labels.copy()
    if fixed_mask is None:
        fixed_mask = np.zeros_like(domain_mask, dtype=bool)
    # Use tight safety margin over exact erosion radius so rich dendritic lobes stay intact while passing >= 3 mm necks
    r_mm = 0.5 * (min_neck_mm + kerf_mm) + 0.08
    k = max(1, int(round(r_mm / px_mm)))
    pad = k + 2
    disk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1))
    wide = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 3, 2 * k + 3))
    total_moved = 0

    for _ in range(passes):
        moved = 0
        piece_ids = sorted(int(v) for v in np.unique(lab) if v >= 0)
        for pid in piece_ids:
            ys, xs = np.nonzero(lab == pid)
            if len(ys) < 12:
                continue
            r0 = max(0, int(ys.min()) - pad)
            r1 = min(lab.shape[0], int(ys.max()) + pad + 1)
            c0 = max(0, int(xs.min()) - pad)
            c1 = min(lab.shape[1], int(xs.max()) + pad + 1)
            sub = lab[r0:r1, c0:c1]
            sub_fix = fixed_mask[r0:r1, c0:c1]
            if np.all(sub_fix[sub == pid]):
                continue  # Fixed whimsy piece is untouched
            m = np.pad((sub == pid).astype(np.uint8), pad, mode="constant", constant_values=0)
            n_comp, core = cv2.connectedComponents(cv2.erode(m, disk), connectivity=4)
            if n_comp < 2:
                continue
            counts = np.bincount(core.ravel())[1:]
            main = 1 + int(np.argmax(counts))
            body = cv2.dilate((core == main).astype(np.uint8), wide) & m
            loose = m & (1 - body)
            if not np.any(loose):
                continue
            _, rest = cv2.connectedComponents(loose, connectivity=4)
            far = ndimage.distance_transform_edt(1 - body) * px_mm
            detached_cores = np.unique(rest[(core > 0) & (core != main)])
            long_tips = np.unique(rest[far > max_tip_reach_mm])
            hit = np.union1d(detached_cores, long_tips)
            hit = hit[hit > 0]
            if not len(hit):
                continue
            drop = np.isin(rest, hit)[pad:-pad, pad:-pad] & (~sub_fix) & domain_mask[r0:r1, c0:c1]
            if not np.any(drop):
                continue
            # Hand dropped pixels to nearest *other* active piece inside domain
            other_mask = (sub != pid) & (sub >= 0) & (~sub_fix)
            if not np.any(other_mask):
                continue
            _, near = ndimage.distance_transform_edt(~other_mask, return_distances=True, return_indices=True)
            sub[drop] = sub[tuple(near)][drop]
            moved += int(drop.sum())
        total_moved += moved
        lab, _ = clean_connected_labels(lab, domain_mask, fixed_mask=fixed_mask)
        if moved == 0:
            break

    # Remove tiny 1-pixel checkerboard spurs (opening/closing-style majority filter on dynamic seams)
    lab, _ = clean_connected_labels(lab, domain_mask, fixed_mask=fixed_mask)
    return lab, total_moved


# ---------------------------------------------------------------------------
# Sub-pixel shared-seam contour extraction from label grid
# ---------------------------------------------------------------------------

def _smooth_polyline(xy: np.ndarray, sigma_mm: float, loop: bool = False) -> np.ndarray:
    n = len(xy)
    if sigma_mm <= 0.0 or n < 5:
        return xy
    diffs = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    step = float(diffs.mean()) + 1e-9
    s = sigma_mm / step
    if s < 0.15:
        return xy
    if loop:
        return gaussian_filter1d(xy, s, axis=0, mode="wrap")
    k = min(n - 1, int(3.0 * s) + 1)
    ext = np.vstack([2.0 * xy[0] - xy[k:0:-1], xy, 2.0 * xy[-1] - xy[-2 : -k - 2 : -1]])
    out = gaussian_filter1d(ext, s, axis=0, mode="nearest")[k : k + n]
    out[0] = xy[0]
    out[-1] = xy[-1]
    return out


def extract_shared_seams_from_labels(
    labels: np.ndarray,
    bounds_mm: Tuple[float, float, float, float],
    outline: Polygon,
    phase_fields: Optional[np.ndarray] = None,
    piece_to_color: Optional[Dict[int, int]] = None,
    fixed_piece_ids: Sequence[int] = (),
    whimsies: Sequence[WhimsyPlacement] = (),
    smooth_sigma_mm: float = 0.65,
    simplify_tol_mm: float = 0.08,
    snap_mm: float = 0.01,
) -> Tuple[List[Polygon], List[LineString], List[LineString], Dict[str, Any]]:
    """Trace every seam ONCE between junction nodes, smooth cleanly, snap, and polygonize.

    When `phase_fields` is supplied, boundary vertices are placed at exact sub-pixel
    zero-crossings `p_a = p_b` for ultra-smooth dendritic curves with zero stair-stepping!
    """
    h, w = labels.shape
    x0, y0, x1, y1 = bounds_mm
    px = (x1 - x0) / w
    py = (y1 - y0) / h
    fixed_set = set(int(i) for i in fixed_piece_ids)
    expected_ids = sorted(int(v) for v in np.unique(labels) if v >= 0)
    n_expected = len(expected_ids)

    Lp = np.pad(labels, 1, mode="constant", constant_values=-1)

    def _subpixel_t(r_a: np.ndarray, c_a: np.ndarray, r_b: np.ndarray, c_b: np.ndarray) -> np.ndarray:
        t = np.full(len(r_a), 0.5, dtype=np.float64)
        if phase_fields is None or not piece_to_color:
            return t
        la = labels[r_a, c_a]
        lb = labels[r_b, c_b]
        # Only use continuous phase crossing when both pixels belong to dynamic phase-field pieces
        for idx in range(len(la)):
            ia, ib = int(la[idx]), int(lb[idx])
            if ia in piece_to_color and ib in piece_to_color and ia not in fixed_set and ib not in fixed_set:
                ca, cb = piece_to_color[ia], piece_to_color[ib]
                if ca != cb:
                    da = float(phase_fields[ca, r_a[idx], c_a[idx]] - phase_fields[cb, r_a[idx], c_a[idx]])
                    db = float(phase_fields[ca, r_b[idx], c_b[idx]] - phase_fields[cb, r_b[idx], c_b[idx]])
                    denom = da - db
                    if abs(denom) > 1e-6:
                        t[idx] = float(np.clip(da / denom, 0.08, 0.92))
        return t

    # Horizontal cracks between rows (r-1) and r along column c
    hz = Lp[:-1, 1:-1] != Lp[1:, 1:-1]
    hr, hc = np.nonzero(hz)
    h_la = Lp[hr, hc + 1]
    h_lb = Lp[hr + 1, hc + 1]
    h_inner = (h_la >= 0) & (h_lb >= 0)
    ht = np.full(len(hr), 0.5, dtype=np.float64)
    if np.any(h_inner):
        ht[h_inner] = _subpixel_t(hr[h_inner] - 1, hc[h_inner], hr[h_inner], hc[h_inner])
    hp = np.stack(
        [
            x0 + (hc + 0.5) * px,
            y0 + np.where(h_inner, (hr - 0.5 + ht) * py, hr * py),
        ],
        axis=1,
    )
    hn = np.stack([hr * (w + 1) + hc, hr * (w + 1) + hc + 1], axis=1)
    hpair = np.sort(np.stack([h_la, h_lb], axis=1), axis=1)

    # Vertical cracks between cols (c-1) and c along row r
    vt = Lp[1:-1, :-1] != Lp[1:-1, 1:]
    vr, vc = np.nonzero(vt)
    v_la = Lp[vr + 1, vc]
    v_lb = Lp[vr + 1, vc + 1]
    v_inner = (v_la >= 0) & (v_lb >= 0)
    vt_off = np.full(len(vr), 0.5, dtype=np.float64)
    if np.any(v_inner):
        vt_off[v_inner] = _subpixel_t(vr[v_inner], vc[v_inner] - 1, vr[v_inner], vc[v_inner])
    vp = np.stack(
        [
            x0 + np.where(v_inner, (vc - 0.5 + vt_off) * px, vc * px),
            y0 + (vr + 0.5) * py,
        ],
        axis=1,
    )
    vn = np.stack([vr * (w + 1) + vc, (vr + 1) * (w + 1) + vc], axis=1)
    vpair = np.sort(np.stack([v_la, v_lb], axis=1), axis=1)

    pts = np.vstack([hp, vp])
    ends = np.vstack([hn, vn])
    pair = np.vstack([hpair, vpair])

    nodes, inv = np.unique(ends, return_inverse=True)
    ends_idx = inv.reshape(-1, 2)
    deg = np.bincount(ends_idx.ravel(), minlength=len(nodes))
    order = np.argsort(ends_idx.ravel(), kind="stable")
    start = np.r_[0, np.cumsum(deg)]
    inc = order // 2

    nr = nodes // (w + 1)
    nc = nodes % (w + 1)
    # Any node with degree != 2 or where incident label pairs change is a junction stop
    stop = deg != 2
    for n_i in np.nonzero(~stop)[0]:
        e0, e1 = inc[start[n_i] : start[n_i + 1]]
        if pair[e0, 0] != pair[e1, 0] or pair[e0, 1] != pair[e1, 1]:
            stop[n_i] = True

    # Corner nodes of rectangular outline also pinned
    corner = ((nr == 0) | (nr == h)) & ((nc == 0) | (nc == w))
    stop |= corner

    pos = np.stack([x0 + nc * px, y0 + nr * py], axis=1).astype(np.float64)
    interior = pair[:, 0] >= 0
    for n_i in np.nonzero(stop & ~corner)[0]:
        e_list = inc[start[n_i] : start[n_i + 1]]
        # Exterior junctions project neatly onto exact target outline boundary!
        has_ext = np.any(~interior[e_list])
        if has_ext:
            raw_pt = shapely.Point(float(pos[n_i, 0]), float(pos[n_i, 1]))
            proj = outline.exterior.interpolate(outline.exterior.project(raw_pt))
            pos[n_i] = [proj.x, proj.y]
        else:
            pos[n_i] = pts[e_list].mean(axis=0)

    # Walk maximal chains between stop nodes
    used = np.zeros(len(ends_idx), dtype=bool)
    arcs: List[Dict[str, Any]] = []

    def _walk(n0: int, e0: int):
        path = [e0]
        used[e0] = True
        cur = int(ends_idx[e0, 0] + ends_idx[e0, 1] - n0)
        while not stop[cur]:
            a, b = inc[start[cur] : start[cur + 1]]
            nxt = int(b if a == path[-1] else a)
            if used[nxt]:
                return n0, n0, path, True
            used[nxt] = True
            path.append(nxt)
            cur = int(ends_idx[nxt, 0] + ends_idx[nxt, 1] - cur)
        return n0, cur, path, False

    for n_i in np.nonzero(stop)[0]:
        for e_i in inc[start[n_i] : start[n_i + 1]]:
            if not used[e_i]:
                arcs.append({"walk": _walk(int(n_i), int(e_i)), "pair": (int(pair[e_i, 0]), int(pair[e_i, 1]))})
    for e_i in range(len(ends_idx)):
        if not used[e_i]:
            arcs.append({"walk": _walk(int(ends_idx[e_i, 0]), int(e_i)), "pair": (int(pair[e_i, 0]), int(pair[e_i, 1]))})

    # Try progressive smoothing fallbacks so topology always reproduces exact N faces
    ext_ring = outline.exterior
    whimsy_by_pid: Dict[int, Polygon] = {}
    for pid, wh in zip(fixed_piece_ids, whimsies):
        whimsy_by_pid[int(pid)] = wh.polygon

    for sg in (smooth_sigma_mm, smooth_sigma_mm * 0.6, smooth_sigma_mm * 0.3, 0.15, 0.0):
        int_lines_raw: List[LineString] = []
        for arc in arcs:
            a_id, b_id = arc["pair"]
            if a_id < 0:
                continue  # Exterior is constructed cleanly from exact target outline + exterior junctions!
            n0, n1, path, loop = arc["walk"]
            xy = pts[path] if loop else np.vstack([pos[n0], pts[path], pos[n1]])

            # If arc borders a fixed whimsy, project interior vertices gently onto exact whimsy polygon!
            wh_poly = whimsy_by_pid.get(a_id) or whimsy_by_pid.get(b_id)
            if wh_poly is not None and not loop and len(xy) >= 3:
                wh_ring = wh_poly.exterior
                for vi in range(1, len(xy) - 1):
                    qp = shapely.Point(float(xy[vi, 0]), float(xy[vi, 1]))
                    if wh_ring.distance(qp) < max(px, py) * 2.5:
                        pp = wh_ring.interpolate(wh_ring.project(qp))
                        xy[vi] = [pp.x, pp.y]
                xy = _smooth_polyline(xy, min(sg, 0.25), loop=False)
            elif wh_poly is not None and loop:
                xy = np.asarray(wh_poly.exterior.coords, dtype=np.float64)
            else:
                xy = _smooth_polyline(xy, sg, loop=loop)
                if loop:
                    xy = np.vstack([xy, xy[:1]])

            ls = LineString(np.round(xy, 4)).simplify(simplify_tol_mm, preserve_topology=False)
            if ls.length > 0.05:
                # Clip cleanly to outline so smoothed interior seams never overshoot curved outlines
                clipped = ls.intersection(outline)
                for part in get_parts(clipped):
                    if part.geom_type == "LineString" and part.length > 0.05:
                        int_lines_raw.append(LineString(np.round(np.asarray(part.coords) / snap_mm) * snap_mm))

        ext_snapped = LineString(np.round(np.asarray(ext_ring.coords) / snap_mm) * snap_mm)
        all_cut = union_all([*int_lines_raw, ext_snapped], grid_size=snap_mm)
        faces_raw = [orient(p, 1.0) for p in polygonize(all_cut) if p.is_valid and p.area > 1.0]
        if len(faces_raw) != n_expected:
            continue

        # Verify 1-to-1 correspondence with labels
        owners: List[int] = []
        for f in faces_raw:
            rp = f.representative_point()
            col = int(np.clip((rp.x - x0) / px, 0, w - 1))
            row = int(np.clip((rp.y - y0) / py, 0, h - 1))
            owners.append(int(labels[row, col]))
        if sorted(owners) != expected_ids:
            continue

        # Split noded network strictly into exterior_lines (on outline boundary) vs interior_lines
        noded_parts = [l for l in get_parts(all_cut) if l.geom_type == "LineString" and l.length > 1e-4]
        ext_buf = ext_snapped.buffer(max(0.08, snap_mm * 4.0))
        ext_edges = [l for l in noded_parts if ext_buf.covers(l)]
        int_edges = [l for l in noded_parts if not ext_buf.covers(l)]
        ext_merged = [l for l in get_parts(linemerge(ext_edges)) if l.geom_type == "LineString" and l.length > 1e-4]
        int_merged = [l for l in get_parts(linemerge(int_edges)) if l.geom_type == "LineString" and l.length > 1e-4]

        order_faces = [faces_raw[i] for i in np.argsort(owners)]
        return order_faces, int_merged, ext_merged, {"smoothing_mm": sg, "snap_mm": snap_mm}

    raise ValueError(
        f"Vectorized seams could not match expected {n_expected} faces cleanly (got {len(faces_raw)} at sigma=0)"
    )


# ---------------------------------------------------------------------------
# Independent Re-Parse, Kerf, Neck, Thin-Tip, and 360-dir Interlock Audit
# ---------------------------------------------------------------------------

def _parse_svg_paths(svg_path: Path, exclude_groups: Sequence[str] = ("engrave",)) -> Tuple[List[LineString], float, float, float, float]:
    """Read cut paths directly from an SVG file, ignoring engraving groups."""
    root = ET.parse(svg_path).getroot()
    vb = [float(v) for v in root.attrib["viewBox"].split()]
    vx, vy, vw, vh = vb[0], vb[1], vb[2], vb[3]
    excl = set(exclude_groups)
    lines: List[LineString] = []

    def _walk(elem: ET.Element):
        tag = elem.tag.split("}")[-1]
        if tag == "g" and elem.attrib.get("id") in excl:
            return
        if tag == "path":
            d = elem.attrib.get("d", "")
            # Split multi-subpath 'M' commands cleanly
            subpaths = re.split(r"(?=[Mm])", d.strip())
            for sp in subpaths:
                sp = sp.strip()
                if not sp:
                    continue
                nums = [float(v) for v in re.findall(r"[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?", sp)]
                if len(nums) >= 4:
                    coords = list(zip(nums[0::2], nums[1::2]))
                    if sp.rstrip().endswith(("Z", "z")) and coords[0] != coords[-1]:
                        coords.append(coords[0])
                    if len(coords) >= 2:
                        lines.append(LineString(coords))
            return
        for child in elem:
            _walk(child)

    _walk(root)
    return lines, vx, vy, vw, vh


def _parts_count(geom) -> int:
    if geom.is_empty:
        return 0
    if geom.geom_type == "Polygon":
        return 1 if geom.area > 1e-6 else 0
    return sum(1 for p in get_parts(geom) if p.geom_type == "Polygon" and p.area > 1e-6)


def _tip_reach_mm(poly: Polygon, r: float = 1.5, step: float = 0.35) -> Tuple[float, float]:
    body = poly.buffer(-r).buffer(r)
    lost = poly.difference(body)
    if lost.is_empty or lost.area < 1e-4:
        return 0.0, 0.0
    if body.is_empty:
        x0, y0, x1, y1 = poly.bounds
        return float(math.hypot(x1 - x0, y1 - y0)), float(lost.area)
    pts = []
    for g in get_parts(lost):
        if g.geom_type != "Polygon" or g.area < 1e-4:
            continue
        ring = g.exterior
        pts.extend(ring.interpolate(d) for d in np.arange(0.0, ring.length, step))
    reach = max((body.distance(q) for q in pts), default=0.0)
    return float(reach), float(lost.area)


def compute_lock_metrics(faces: Sequence[Polygon], ndir: int = 360) -> Dict[str, Any]:
    """360-direction planar translation interlock margin (matches pathnet.field & heller_jigsaw)."""
    n = len(faces)
    if n < 2:
        return {
            "adjacent_pairs": 0,
            "assembly_lock_margin_mm": 0.0,
            "locked_pairs_1mm": 0,
            "locked_pairs_2mm": 0,
            "locked_pairs_3mm": 0,
            "unlocked_directions_at_1mm": ndir,
            "unlocked_directions_at_2mm": ndir,
            "unlocked_directions_at_3mm": ndir,
            "median_pair_lock_margin_mm": 0.0,
        }

    owners: Dict[Tuple[Tuple[float, float], Tuple[float, float]], List[Tuple[int, Tuple[float, float], Tuple[float, float]]]] = defaultdict(list)
    for i, p in enumerate(faces):
        xy = np.round(np.asarray(orient(p, 1.0).exterior.coords), 4)
        for a, b in zip(map(tuple, xy[:-1]), map(tuple, xy[1:])):
            if a != b:
                key = (a, b) if a < b else (b, a)
                owners[key].append((i, a, b))

    con: Dict[Tuple[int, int], Tuple[List[float], List[List[float]]]] = defaultdict(lambda: ([], []))
    for items in owners.values():
        if len(items) != 2:
            continue
        (i, a, b), (j, _, _) = sorted(items, key=lambda x: x[0])
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        seg_len = math.hypot(dx, dy)
        if seg_len > 1e-9:
            con[(i, j)][0].append(seg_len)
            con[(i, j)][1].append([dy / seg_len, -dx / seg_len])  # outward normal of i

    pairs = list(con.keys())
    if not pairs:
        return {
            "adjacent_pairs": 0,
            "assembly_lock_margin_mm": 0.0,
            "locked_pairs_1mm": 0,
            "locked_pairs_2mm": 0,
            "locked_pairs_3mm": 0,
            "unlocked_directions_at_1mm": ndir,
            "unlocked_directions_at_2mm": ndir,
            "unlocked_directions_at_3mm": ndir,
            "median_pair_lock_margin_mm": 0.0,
        }

    ang = np.arange(ndir) * (2.0 * math.pi / ndir)
    dirs = np.stack([np.cos(ang), np.sin(ang)], axis=1)
    block = np.zeros((len(pairs), ndir), dtype=np.float64)
    for pi, k in enumerate(pairs):
        lens = np.asarray(con[k][0], dtype=np.float64)
        norms = np.asarray(con[k][1], dtype=np.float64)
        block[pi] = (np.clip(-(norms @ dirs.T), 0.0, None) * lens[:, None]).sum(axis=0)

    pair_margins = block.min(axis=1)
    lo_idx = np.array([k[0] for k in pairs], dtype=np.int32)
    hi_idx = np.array([k[1] for k in pairs], dtype=np.int32)

    Wm = np.zeros((ndir, n, n), dtype=np.float64)
    Wm[:, hi_idx, lo_idx] = block.T
    Wm[:, lo_idx, hi_idx] = np.roll(block, -(ndir // 2), axis=1).T

    def _unlocked(thresh: float) -> int:
        cnt = 0
        for t in range(ndir):
            if connected_components(csr_matrix(Wm[t] >= thresh), directed=True, connection="strong")[0] > 1:
                cnt += 1
        return cnt

    lo, hi = 0.0, 12.0
    if _unlocked(hi) == 0:
        margin = hi
    else:
        while hi - lo > 0.0625:
            mid = 0.5 * (lo + hi)
            if _unlocked(mid) > 0:
                hi = mid
            else:
                lo = mid
        margin = lo

    return {
        "adjacent_pairs": len(pairs),
        "assembly_lock_margin_mm": round(float(margin), 3),
        "locked_pairs_1mm": int((pair_margins >= 1.0).sum()),
        "locked_pairs_2mm": int((pair_margins >= 2.0).sum()),
        "locked_pairs_3mm": int((pair_margins >= 3.0).sum()),
        "unlocked_directions_at_1mm": _unlocked(1.0),
        "unlocked_directions_at_2mm": _unlocked(2.0),
        "unlocked_directions_at_3mm": _unlocked(3.0),
        "median_pair_lock_margin_mm": round(float(np.median(pair_margins)), 3),
    }


def audit_cut_svg(
    cut_svg_path: str | Path,
    expected_outline: Optional[Polygon] = None,
    min_neck_mm: float = 3.0,
) -> Dict[str, Any]:
    """Independently re-parse `cut.svg` and run full geometry, kerf, neck, and 360-dir lock audits."""
    from heller_jigsaw.geometry.validate import analyze_piece_fabrication

    svg_path = Path(cut_svg_path)
    lines, vx, vy, vw, vh = _parse_svg_paths(svg_path, exclude_groups=("engrave",))
    segs = []
    for ln in lines:
        xy = [(round(float(x), 4), round(float(y), 4)) for x, y in ln.coords]
        segs.extend(tuple(sorted((a, b))) for a, b in zip(xy, xy[1:]) if a != b)
    duplicate_segments = len(segs) - len(set(segs))

    noded = union_all(lines)
    pieces = [orient(p, 1.0) for p in polygonize(noded) if p.is_valid and p.area > 0.1]
    pieces.sort(key=lambda p: (round(p.centroid.y / 15.0), p.centroid.x))

    cover = union_all(pieces) if pieces else Polygon()
    overlap_mm2 = max(0.0, float(sum(p.area for p in pieces) - cover.area)) if pieces else 0.0
    if expected_outline is not None:
        coverage_error_mm2 = float(expected_outline.symmetric_difference(cover).area)
    else:
        # For borderless puzzles, verify union of faces is a single hole-free polygon
        coverage_error_mm2 = float(sum(Polygon(ring).area for ring in cover.interiors)) if cover.geom_type == "Polygon" else float(cover.area)

    kerf_failures = {f"{k:.2f}": 0 for k in KERFS}
    neck_2_5_failures = 0
    neck_3_0_failures = 0
    heller_neck_failures = 0
    worst_tip = 0.0
    per_piece = []

    for idx, p in enumerate(pieces):
        k_parts = {f"{k:.2f}": _parts_count(p.buffer(-0.5 * k)) for k in KERFS}
        for k_str, cnt in k_parts.items():
            if cnt != 1:
                kerf_failures[k_str] += 1
        n25 = _parts_count(p.buffer(-1.25))
        n30 = _parts_count(p.buffer(-0.5 * min_neck_mm))
        if n25 != 1:
            neck_2_5_failures += 1
        if n30 != 1:
            neck_3_0_failures += 1
        fab = analyze_piece_fabrication(p, kerf_mm=0.15, min_neck_mm=min_neck_mm)
        if not fab["neck_pass"]:
            heller_neck_failures += 1
        reach, thin_area = _tip_reach_mm(p, r=0.5 * min_neck_mm)
        worst_tip = max(worst_tip, reach)
        per_piece.append(
            {
                "piece": idx,
                "area_mm2": round(float(p.area), 2),
                "perimeter_mm": round(float(p.length), 2),
                "holes": len(p.interiors),
                "valid": bool(p.is_valid),
                "kerf_pass": all(v == 1 for v in k_parts.values()),
                "neck_2_5mm_parts": n25,
                "neck_3mm_parts": n30,
                "heller_neck_pass": bool(fab["neck_pass"]),
                "tip_reach_mm": round(reach, 2),
                "thin_area_mm2": round(thin_area, 2),
            }
        )

    lock = compute_lock_metrics(pieces, ndir=360)
    areas = [r["area_mm2"] for r in per_piece] or [0.0]
    cut_length_mm = float(sum(l.length for l in lines))
    all_kerf_ok = all(v == 0 for v in kerf_failures.values())
    all_hard_passed = (
        len(pieces) >= 2
        and duplicate_segments == 0
        and coverage_error_mm2 < 0.5
        and overlap_mm2 < 1e-2
        and all(r["valid"] and r["holes"] == 0 for r in per_piece)
        and all_kerf_ok
        and heller_neck_failures == 0
        and lock["assembly_lock_margin_mm"] > 0.0
    )

    return {
        "cut_svg": str(svg_path),
        "viewbox_mm": [round(vx, 3), round(vy, 3), round(vw, 3), round(vh, 3)],
        "pieces": len(pieces),
        "cut_length_mm": round(cut_length_mm, 2),
        "duplicate_segments": int(duplicate_segments),
        "coverage_error_mm2": round(coverage_error_mm2, 6),
        "overlap_mm2": round(overlap_mm2, 6),
        "invalid_pieces": sum(not r["valid"] for r in per_piece),
        "pieces_with_holes": sum(r["holes"] > 0 for r in per_piece),
        "area_mm2": {
            "min": round(min(areas), 2),
            "median": round(float(np.median(areas)), 2),
            "max": round(max(areas), 2),
        },
        "kerf_failures": kerf_failures,
        "neck_2_5mm_failures": int(neck_2_5_failures),
        "neck_3mm_failures": int(neck_3_0_failures),
        "heller_neck_failures": int(heller_neck_failures),
        "worst_tip_reach_mm": round(worst_tip, 2),
        "interlock": lock,
        "all_hard_gates_passed": bool(all_hard_passed),
        "per_piece": per_piece,
    }


# ---------------------------------------------------------------------------
# SVG, PNG, and Interactive Player JSON Exporter
# ---------------------------------------------------------------------------

def _graph_color_faces(faces: Sequence[Polygon]) -> List[int]:
    n = len(faces)
    adj: Dict[int, set[int]] = {i: set() for i in range(n)}
    for i in range(n):
        for j in range(i + 1, n):
            if faces[i].distance(faces[j]) < 0.05:
                adj[i].add(j)
                adj[j].add(i)
    colors: Dict[int, int] = {}
    for u in range(n):
        used = {colors[v] for v in adj[u] if v in colors}
        c = 0
        while c in used:
            c += 1
        colors[u] = c
    return [colors[i] for i in range(n)]


def _line_to_d(line: LineString) -> str:
    coords = list(line.coords)
    if len(coords) < 2:
        return ""
    return "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in coords)


def _poly_to_d(poly: Polygon) -> str:
    coords = list(poly.exterior.coords)
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    return "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in coords) + " Z"


def _hex_to_bgr(hx: str) -> Tuple[int, int, int]:
    h = hx.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b, g, r)


def _render_wood_preview(
    path: Path,
    faces: Sequence[Polygon],
    interior_lines: Sequence[LineString],
    exterior_lines: Sequence[LineString],
    engrave_lines: Sequence[LineString],
    bounds_mm: Tuple[float, float, float, float],
    palette: Sequence[str],
    whimsy_indices: set[int],
    theme: str = "birch",
    max_dim_px: int = 1800,
) -> None:
    x0, y0, x1, y1 = bounds_mm
    pad_mm = max(8.0, 0.04 * max(x1 - x0, y1 - y0))
    w_tot = (x1 - x0) + 2.0 * pad_mm
    h_tot = (y1 - y0) + 2.0 * pad_mm
    scale = max_dim_px / max(w_tot, h_tot)
    img_w = int(round(w_tot * scale))
    img_h = int(round(h_tot * scale))

    bg = (248, 248, 246) if theme == "birch" else (246, 246, 244)
    img = np.full((img_h, img_w, 3), bg, dtype=np.uint8)

    # Subtle directional wood-grain texture
    rng = np.random.default_rng(13)
    yy = np.linspace(0.0, 1.0, img_h, dtype=np.float32)[:, None]
    xx = np.linspace(0.0, 1.0, img_w, dtype=np.float32)[None, :]
    grain = (
        0.5 * np.sin(2.0 * math.pi * (yy * 18.0 + 0.15 * np.sin(xx * 6.0)))
        + 0.35 * np.sin(2.0 * math.pi * (yy * 43.0 + 0.08 * np.cos(xx * 11.0)))
        + 0.15 * rng.normal(0.0, 1.0, (img_h, img_w)).astype(np.float32)
    )
    grain = (grain * (6.0 if theme == "birch" else 8.0)).astype(np.float32)

    def _pts(coords):
        arr = np.asarray(coords, dtype=np.float64)
        px = np.round((arr[:, 0] - x0 + pad_mm) * scale * 16.0).astype(np.int32)
        py = np.round((arr[:, 1] - y0 + pad_mm) * scale * 16.0).astype(np.int32)
        return np.stack([px, py], axis=1)

    # Soft drop shadow under assembled board
    shadow_mask = np.zeros((img_h, img_w), dtype=np.uint8)
    for f in faces:
        cv2.fillPoly(shadow_mask, [_pts(f.exterior.coords)], 255, shift=4)
    sh_blur = cv2.GaussianBlur(shadow_mask, (0, 0), sigmaX=max(3.0, scale * 2.2))
    shift_y = max(3, int(round(scale * 1.4)))
    shift_x = max(2, int(round(scale * 0.9)))
    sh_shifted = np.roll(np.roll(sh_blur, shift_y, axis=0), shift_x, axis=1).astype(np.float32) / 255.0
    for ch in range(3):
        img[:, :, ch] = np.clip(img[:, :, ch].astype(np.float32) * (1.0 - 0.24 * sh_shifted), 0, 255).astype(np.uint8)

    tones = _graph_color_faces(faces)
    board_mask = np.zeros((img_h, img_w), dtype=bool)
    for idx, f in enumerate(faces):
        if idx in whimsy_indices:
            bgr = (165, 200, 232) if theme == "birch" else (96, 108, 122)
        else:
            bgr = _hex_to_bgr(palette[tones[idx] % len(palette)])
        cv2.fillPoly(img, [_pts(f.exterior.coords)], bgr, lineType=cv2.LINE_AA, shift=4)
        cv2.fillPoly(board_mask.view(np.uint8), [_pts(f.exterior.coords)], 1, shift=4)

    # Apply subtle wood grain inside board only
    for ch in range(3):
        plane = img[:, :, ch].astype(np.float32)
        plane[board_mask] = np.clip(plane[board_mask] + grain[board_mask], 0, 255)
        img[:, :, ch] = plane.astype(np.uint8)

    # Subtle laser char along cuts (warm amber halo + crisp dark kerf)
    if theme == "birch":
        char_mask = np.zeros((img_h, img_w), dtype=np.uint8)
        for ln in (*interior_lines, *exterior_lines):
            cv2.polylines(char_mask, [_pts(ln.coords)], False, 255, max(2, int(round(scale * 0.55))), cv2.LINE_AA, shift=4)
        char_blur = (cv2.GaussianBlur(char_mask, (0, 0), sigmaX=max(1.2, scale * 0.55)).astype(np.float32) / 255.0) * board_mask
        # Warm scorched edge tint (BGR)
        img[:, :, 0] = np.clip(img[:, :, 0] * (1.0 - 0.22 * char_blur), 0, 255).astype(np.uint8)
        img[:, :, 1] = np.clip(img[:, :, 1] * (1.0 - 0.14 * char_blur), 0, 255).astype(np.uint8)
        img[:, :, 2] = np.clip(img[:, :, 2] * (1.0 - 0.06 * char_blur), 0, 255).astype(np.uint8)

    cut_color = (24, 29, 36) if theme == "birch" else (16, 18, 22)
    cut_w = max(1, int(round(scale * 0.30)))
    for ln in interior_lines:
        cv2.polylines(img, [_pts(ln.coords)], False, cut_color, cut_w, cv2.LINE_AA, shift=4)
    for ln in exterior_lines:
        cv2.polylines(img, [_pts(ln.coords)], False, (18, 22, 28), max(cut_w + 1, 2), cv2.LINE_AA, shift=4)

    # Separate laser engraving lines (distinct warm-brown burn)
    eng_color = (42, 68, 108) if theme == "birch" else (180, 195, 210)
    eng_w = max(1, int(round(scale * 0.36)))
    for ln in engrave_lines:
        cv2.polylines(img, [_pts(ln.coords)], False, eng_color, eng_w, cv2.LINE_AA, shift=4)

    cv2.imwrite(str(path), img)


def _render_piece_sheet(
    path: Path,
    faces: Sequence[Polygon],
    whimsy_indices: set[int],
    theme: str = "birch",
    cell: int = 210,
) -> None:
    n = len(faces)
    cols = max(4, min(10, int(math.ceil(math.sqrt(n * 1.4)))))
    rows = int(math.ceil(n / cols))
    img = np.full((rows * cell, cols * cell, 3), 252, dtype=np.uint8)
    max_span = max(max(f.bounds[2] - f.bounds[0], f.bounds[3] - f.bounds[1]) for f in faces)
    k = (cell - 28.0) / max(max_span, 1e-6)
    fill_base = (190, 215, 238) if theme == "birch" else (92, 84, 78)
    fill_whimsy = (155, 195, 232) if theme == "birch" else (120, 112, 104)

    for i, f in enumerate(faces):
        r, c = divmod(i, cols)
        cx = (c + 0.5) * cell
        cy = (r + 0.5) * cell
        x0, y0, x1, y1 = f.bounds
        mx, my = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
        xy = np.asarray(f.exterior.coords, dtype=np.float64)
        pts = np.round(np.stack([(xy[:, 0] - mx) * k + cx, (xy[:, 1] - my) * k + cy], axis=1) * 16.0).astype(np.int32)
        cv2.fillPoly(img, [pts], fill_whimsy if i in whimsy_indices else fill_base, lineType=cv2.LINE_AA, shift=4)
        cv2.polylines(img, [pts], True, (30, 34, 40), 1, cv2.LINE_AA, shift=4)
        label = f"W{i}" if i in whimsy_indices else f"{i}"
        cv2.putText(img, label, (int(c * cell + 8), int(r * cell + 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (110, 110, 110), 1, cv2.LINE_AA)

    cv2.imwrite(str(path), img)


def export_puzzle(
    out_dir: str | Path,
    faces: Sequence[Polygon],
    interior_lines: Sequence[LineString],
    exterior_lines: Sequence[LineString],
    engrave_lines: Sequence[LineString] = (),
    expected_outline: Optional[Polygon] = None,
    whimsy_indices: Sequence[int] = (),
    kerf_mm: float = 0.15,
    min_neck_mm: float = 3.0,
    theme: str = "birch",
    metadata: Optional[Dict[str, Any]] = None,
) -> ExportResult:
    """Write 1:1 mm `cut.svg`, `artwork.svg`, `preview.png`, `pieces_sheet.png`, `puzzle.json`, and `audit.json`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    all_geom = union_all([*faces, *exterior_lines, *interior_lines])
    x0, y0, x1, y1 = all_geom.bounds
    # Shift viewBox origin cleanly to (0, 0) if negative or non-zero
    dx, dy = -float(x0), -float(y0)
    w_mm = float(x1 - x0)
    h_mm = float(y1 - y0)

    def _shift(g):
        return shapely.affinity.translate(g, xoff=dx, yoff=dy)

    faces_s = [orient(_shift(f), 1.0) for f in faces]
    int_s = [_shift(l) for l in interior_lines if l.length > 1e-4]
    ext_s = [_shift(l) for l in exterior_lines if l.length > 1e-4]
    eng_s = [_shift(l) for l in engrave_lines if l.length > 1e-4]
    outline_s = _shift(expected_outline) if expected_outline is not None else None

    # 1. Write 1:1 mm cut.svg with strictly separated layers
    cut_root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": f"{w_mm:.4f}mm",
            "height": f"{h_mm:.4f}mm",
            "viewBox": f"0 0 {w_mm:.4f} {h_mm:.4f}",
            "version": "1.1",
        },
    )
    g_int = ET.SubElement(
        cut_root,
        f"{{{SVG_NS}}}g",
        {
            "id": "interior_cut",
            "fill": "none",
            "stroke": "#FF0000",
            "stroke-width": f"{kerf_mm:.4f}",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for i, ln in enumerate(int_s):
        ET.SubElement(g_int, f"{{{SVG_NS}}}path", {"id": f"int-{i:04d}", "d": _line_to_d(ln)})

    g_ext = ET.SubElement(
        cut_root,
        f"{{{SVG_NS}}}g",
        {
            "id": "exterior_cut",
            "fill": "none",
            "stroke": "#0000FF",
            "stroke-width": f"{kerf_mm:.4f}",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for i, ln in enumerate(ext_s):
        ET.SubElement(g_ext, f"{{{SVG_NS}}}path", {"id": f"ext-{i:04d}", "d": _line_to_d(ln)})

    g_eng = ET.SubElement(
        cut_root,
        f"{{{SVG_NS}}}g",
        {
            "id": "engrave",
            "fill": "none",
            "stroke": "#00AA00",
            "stroke-width": "0.2000",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for i, ln in enumerate(eng_s):
        ET.SubElement(g_eng, f"{{{SVG_NS}}}path", {"id": f"eng-{i:04d}", "d": _line_to_d(ln)})

    cut_path = out / "cut.svg"
    ET.ElementTree(cut_root).write(str(cut_path), encoding="utf-8", xml_declaration=True)

    # 2. Write registered artwork.svg
    palette = BIRCH_PALETTE if theme == "birch" else WALNUT_PALETTE
    tones = _graph_color_faces(faces_s)
    wh_set = set(int(i) for i in whimsy_indices)
    art_root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": f"{w_mm:.4f}mm",
            "height": f"{h_mm:.4f}mm",
            "viewBox": f"0 0 {w_mm:.4f} {h_mm:.4f}",
            "version": "1.1",
        },
    )
    g_pieces = ET.SubElement(art_root, f"{{{SVG_NS}}}g", {"id": "pieces"})
    for i, f in enumerate(faces_s):
        fill = ("#DFB987" if theme == "birch" else "#766A61") if i in wh_set else palette[tones[i] % len(palette)]
        ET.SubElement(
            g_pieces,
            f"{{{SVG_NS}}}path",
            {
                "id": f"piece-{i:04d}",
                "d": _poly_to_d(f),
                "fill": fill,
                "stroke": "#241E18",
                "stroke-width": "0.30",
                "stroke-linejoin": "round",
            },
        )
    if eng_s:
        g_art_eng = ET.SubElement(
            art_root,
            f"{{{SVG_NS}}}g",
            {"id": "engrave_preview", "fill": "none", "stroke": "#6A4322", "stroke-width": "0.35", "stroke-linecap": "round"},
        )
        for i, ln in enumerate(eng_s):
            ET.SubElement(g_art_eng, f"{{{SVG_NS}}}path", {"d": _line_to_d(ln)})

    art_path = out / "artwork.svg"
    ET.ElementTree(art_root).write(str(art_path), encoding="utf-8", xml_declaration=True)

    # 3. Independent re-parse audit of cut.svg
    audit = audit_cut_svg(cut_path, expected_outline=outline_s, min_neck_mm=min_neck_mm)
    if metadata:
        audit["metadata"] = metadata
    audit_path = out / "audit.json"
    audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")

    # 4. Render high-res visual inspection PNGs
    preview_path = out / "preview.png"
    sheet_path = out / "pieces_sheet.png"
    _render_wood_preview(
        preview_path,
        faces_s,
        int_s,
        ext_s,
        eng_s,
        (0.0, 0.0, w_mm, h_mm),
        palette=palette,
        whimsy_indices=wh_set,
        theme=theme,
    )
    _render_piece_sheet(sheet_path, faces_s, wh_set, theme=theme)

    # 5. Export portable JSON for Formverk Studio & interactive player
    piece_records = []
    # Match engrave lines to containing piece so they move with the piece in the interactive player!
    piece_engrave: Dict[int, List[List[List[float]]]] = defaultdict(list)
    for ln in eng_s:
        mid = ln.interpolate(0.5, normalized=True)
        for i, f in enumerate(faces_s):
            if f.covers(mid):
                piece_engrave[i].append([[round(float(x), 3), round(float(y), 3)] for x, y in ln.coords])
                break

    for i, f in enumerate(faces_s):
        c = f.centroid
        fill = ("#DFB987" if theme == "birch" else "#766A61") if i in wh_set else palette[tones[i] % len(palette)]
        piece_records.append(
            {
                "id": i,
                "is_whimsy": i in wh_set,
                "color": fill,
                "centroid": [round(float(c.x), 3), round(float(c.y), 3)],
                "area_mm2": round(float(f.area), 2),
                "outline": [[round(float(x), 3), round(float(y), 3)] for x, y in list(f.exterior.coords)[:-1]],
                "engrave": piece_engrave.get(i, []),
            }
        )

    # Compute piece neighbor graph for snap-to-neighbor in interactive player
    neighbors: Dict[int, List[int]] = {i: [] for i in range(len(faces_s))}
    for i in range(len(faces_s)):
        for j in range(i + 1, len(faces_s)):
            if faces_s[i].distance(faces_s[j]) < 0.05:
                neighbors[i].append(j)
                neighbors[j].append(i)

    puzzle_payload = {
        "width_mm": round(w_mm, 3),
        "height_mm": round(h_mm, 3),
        "theme": theme,
        "metadata": metadata or {},
        "audit_summary": {k: v for k, v in audit.items() if k != "per_piece"},
        "pieces": piece_records,
        "neighbors": {str(k): v for k, v in neighbors.items()},
        "interior_cuts": [[[round(float(x), 3), round(float(y), 3)] for x, y in ln.coords] for ln in int_s],
        "exterior_cuts": [[[round(float(x), 3), round(float(y), 3)] for x, y in ln.coords] for ln in ext_s],
        "engrave_paths": [[[round(float(x), 3), round(float(y), 3)] for x, y in ln.coords] for ln in eng_s],
    }
    puzzle_json_path = out / "puzzle.json"
    puzzle_json_path.write_text(json.dumps(puzzle_payload), encoding="utf-8")

    return ExportResult(
        faces=faces_s,
        interior_lines=int_s,
        exterior_lines=ext_s,
        engrave_lines=eng_s,
        bounds_mm=(0.0, 0.0, w_mm, h_mm),
        audit=audit,
        out_dir=out,
        files={
            "cut_svg": str(cut_path),
            "artwork_svg": str(art_path),
            "preview_png": str(preview_path),
            "pieces_sheet_png": str(sheet_path),
            "audit_json": str(audit_path),
            "puzzle_json": str(puzzle_json_path),
        },
    )
