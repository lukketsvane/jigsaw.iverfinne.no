"""Scalable 750-piece neck/tip repair, spanning-tree 360-deg seam locking, and micro-segment cleanup.

Every edit operates strictly on local shared edges or exterior boundary chains of 1–2 pieces at a time,
using thread-safe parallel proposal generation and fast morphological pre-screening followed by exact
4-kerf (0.10–0.25 mm) fabrication, 3.0 mm neck, <= 6.0 mm tip, and 360-deg lock confirmation.
"""

from __future__ import annotations

import math
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from scipy.interpolate import CubicSpline
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import nearest_points, substring, unary_union
from shapely.strtree import STRtree

from ..geometry.outline import SNAP, snap_polygon
from ..geometry.partition import PlanarPartition, partition_from_polygons
from ..geometry.validate import analyze_piece_fabrication
from ..production.audit import measure
from ..production.geometry import face_from_edges, tab_curve
from .audit_sparse import _DIRS_360, lock_check_sparse, min_segment_length_mm, pair_margin_fast


def _iou(p: Polygon, t: Polygon) -> float:
    if p.is_empty or t.is_empty:
        return 0.0
    inter = p.intersection(t).area
    denom = p.area + t.area - inter
    return float(inter / denom) if denom > 1e-9 else 0.0


def _face_from_edges_override(
    face: Dict[str, Any],
    edges: Sequence[Dict[str, Any]],
    override_eid: int,
    override_coords: List[List[float]],
) -> Polygon:
    """Thread-safe face reconstruction substituting ``override_coords`` for ``override_eid`` without mutating ``edges``."""
    ring: List[Any] = []
    for eid, rev in face["edge_refs"]:
        pts = override_coords if int(eid) == override_eid else edges[int(eid)]["coords"]
        if rev:
            pts = pts[::-1]
        if ring and ring[-1] != pts[0]:
            raise ValueError("edge refs not joined")
        ring.extend(pts if not ring else pts[1:])
    return Polygon(ring)


def _fast_neck_ok(poly: Polygon, min_neck_mm: float = 3.0, kerf_mm: float = 0.25) -> bool:
    """Fast morphological kerf + neck screen (skips 2D raster EDT during candidate proposal search)."""
    area = float(poly.area)
    if area <= 10.0 or not poly.is_valid:
        return False
    ek = poly.buffer(-kerf_mm * 0.5, quad_segs=6, join_style="round", mitre_limit=2.0)
    kparts = [p for p in shapely.get_parts(ek) if p.geom_type == "Polygon" and p.area > 1e-3]
    if len(kparts) != 1 or not kparts[0].is_valid or (kparts[0].area / area) <= 0.5:
        return False
    ec = poly.buffer(-(min_neck_mm + kerf_mm) * 0.5, quad_segs=6, join_style="round", mitre_limit=2.0)
    cparts = [p for p in shapely.get_parts(ec) if p.geom_type == "Polygon" and p.area > 0.5]
    if not cparts:
        return False
    core_area = sum(p.area for p in cparts)
    if (core_area / area) < 0.20:
        return False
    load_bearing = sum(1 for p in cparts if p.area >= 25.0)
    return load_bearing <= 1


def _fast_tip_loss(poly: Polygon, kerf: float = 0.25, r: float = 1.5) -> float:
    """Fast thin-tip reach screen (~5x faster than exact measure; exact measure still verifies acceptance)."""
    p = poly.buffer(-kerf * 0.5, quad_segs=6)
    if p.geom_type != "Polygon" or not p.is_valid:
        return 999.0
    core = p.buffer(-r, quad_segs=6)
    if core.is_empty:
        return float(math.hypot(p.bounds[2] - p.bounds[0], p.bounds[3] - p.bounds[1]))
    opened = core.buffer(r, quad_segs=6).intersection(p)
    coords = shapely.get_coordinates(shapely.segmentize(p.boundary, 0.85))
    return float(shapely.distance(shapely.points(coords), opened).max())


def _safe_tip(poly: Polygon) -> float:
    """Wrapper around ``production.audit.measure`` returning 999.0 if kerf erosion disconnects."""
    try:
        return float(measure(poly)["max_tip_loss_mm"])
    except (ValueError, shapely.GEOSException):
        return 999.0


def _fab_ok(poly: Polygon, min_neck_mm: float = 3.0, all_widths: bool = True) -> bool:
    # Check strictest kerf (0.25 mm) first for instant early rejection
    widths = (0.25, 0.20, 0.15, 0.10) if all_widths else (0.25,)
    for k in widths:
        q = analyze_piece_fabrication(poly, kerf_mm=k, min_neck_mm=min_neck_mm)
        if not q["kerf_pass"] or not q["neck_pass"]:
            return False
    # Also ensure production.audit.measure's strict quad_segs=12 kerf check stays single-component
    return _safe_tip(poly) < 900.0


def _sleek_tab_curve(line: LineString, frac: float, sign: float, size: float) -> Optional[List[List[float]]]:
    """Low-profile wide-neck undercut tab minimizing motif silhouette distortion while clearing >= 3.25 mm lock."""
    xy = np.asarray(line.coords, dtype=np.float64)
    cumul = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
    mid = line.length * frac
    half_span = 5.4 * size
    lo, hi = mid - half_span, mid + half_span
    if lo < 1.2 or hi > line.length - 1.2:
        return None
    p = np.array(line.interpolate(lo).coords[0])
    q = np.array(line.interpolate(hi).coords[0])
    L = float(np.linalg.norm(q - p))
    if L < 7.8 * size:
        return None
    t = (q - p) / L
    n = np.array([-t[1], t[0]]) * sign
    pts = np.array(
        [
            [-5.4, 0.0],
            [-4.1, 0.25],
            [-3.5, 1.3],
            [-3.45, 2.5],
            [-5.6, 3.8],
            [-6.7, 5.6],
            [-5.5, 7.6],
            [0.0, 8.7],
            [5.5, 7.6],
            [6.7, 5.6],
            [5.6, 3.8],
            [3.45, 2.5],
            [3.5, 1.3],
            [4.1, 0.25],
            [5.4, 0.0],
        ],
        dtype=np.float64,
    )
    pts[:, 0] *= L / 10.8
    pts[:, 1] *= size
    u = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]
    curve = CubicSpline(u, pts, axis=0)(np.linspace(0.0, u[-1], 35))
    curve = (p + q) * 0.5 + curve[:, 0, None] * t + curve[:, 1, None] * n
    curve[0] = p
    curve[-1] = q
    out = np.round(np.concatenate([xy[cumul < lo], curve, xy[cumul > hi]]), 4)
    keep = np.r_[True, np.linalg.norm(np.diff(out, axis=0), axis=1) > 0.08]
    out = out[keep]
    if len(out) < 4:
        return None
    out[0] = xy[0]
    out[-1] = xy[-1]
    return out.tolist()


def _compact_tab_curve(line: LineString, frac: float, sign: float, size: float) -> Optional[List[List[float]]]:
    """Compact undercut tab with a >= 3.5 mm wide neck for shorter seams (9–14 mm)."""
    xy = np.asarray(line.coords, dtype=np.float64)
    cumul = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
    mid = line.length * frac
    half_span = 5.2 * size
    lo, hi = mid - half_span, mid + half_span
    if lo < 1.2 or hi > line.length - 1.2:
        return None
    p = np.array(line.interpolate(lo).coords[0])
    q = np.array(line.interpolate(hi).coords[0])
    L = float(np.linalg.norm(q - p))
    if L < 7.5 * size:
        return None
    t = (q - p) / L
    n = np.array([-t[1], t[0]]) * sign
    pts = np.array(
        [
            [-5.2, 0.0],
            [-3.9, 0.2],
            [-3.3, 1.5],
            [-3.2, 3.2],
            [-5.3, 4.6],
            [-6.4, 6.8],
            [-5.4, 9.6],
            [0.0, 11.4],
            [5.4, 9.6],
            [6.4, 6.8],
            [5.3, 4.6],
            [3.2, 3.2],
            [3.3, 1.5],
            [3.9, 0.2],
            [5.2, 0.0],
        ],
        dtype=np.float64,
    )
    pts[:, 0] *= L / 10.4
    pts[:, 1] *= size
    u = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]
    curve = CubicSpline(u, pts, axis=0)(np.linspace(0.0, u[-1], 37))
    curve = (p + q) * 0.5 + curve[:, 0, None] * t + curve[:, 1, None] * n
    curve[0] = p
    curve[-1] = q
    out = np.round(np.concatenate([xy[cumul < lo], curve, xy[cumul > hi]]), 4)
    keep = np.r_[True, np.linalg.norm(np.diff(out, axis=0), axis=1) > 0.08]
    out = out[keep]
    if len(out) < 4:
        return None
    out[0] = xy[0]
    out[-1] = xy[-1]
    return out.tolist()


def _clean_edge_coords(coords: Sequence[Sequence[float]], min_seg_mm: float = 0.06) -> List[List[float]]:
    """Drop interior vertices spaced closer than ``min_seg_mm`` while pinning exact junction endpoints."""
    arr = np.asarray(coords, dtype=np.float64)
    if len(arr) <= 2:
        return [[round(float(x), 4), round(float(y), 4)] for x, y in arr]
    kept = [arr[0]]
    end = arr[-1]
    for pt in arr[1:-1]:
        if float(np.hypot(pt[0] - kept[-1][0], pt[1] - kept[-1][1])) >= min_seg_mm:
            kept.append(pt)
    while len(kept) > 1 and float(np.hypot(end[0] - kept[-1][0], end[1] - kept[-1][1])) < min_seg_mm:
        kept.pop()
    kept.append(end)
    return [[round(float(p[0]), 4), round(float(p[1]), 4)] for p in kept]


def _safe_union(geoms: Sequence[Any]) -> Any:
    valid = [g.buffer(0) if not g.is_valid else g for g in geoms if g is not None and not g.is_empty]
    if not valid:
        return Polygon()
    if len(valid) == 1:
        return valid[0]
    try:
        return shapely.union_all(valid, grid_size=SNAP)
    except shapely.GEOSException:
        return unary_union([g.buffer(0) for g in valid])


def _rebuild_partition(
    polys: Sequence[Polygon],
    face_metadata: Sequence[Dict[str, Any]],
    auto_node: bool = False,
) -> PlanarPartition:
    clean_polys = [orient(p.buffer(0) if not p.is_valid else p, 1.0) for p in polys]
    # Use exact unary_union (without coarse grid shift) first so exterior edges lie at 0.0 mm from outline!
    try:
        outline = unary_union(clean_polys)
    except shapely.GEOSException:
        outline = _safe_union(clean_polys)
    if outline.geom_type != "Polygon":
        outline = max(getattr(outline, "geoms", [outline]), key=lambda g: g.area)
    if outline.interiors:
        # Absorb any microscopic interior gap into its longest-shared-edge neighbor so zero interior holes exist
        tree_h = STRtree(clean_polys)
        for hole_ring in list(outline.interiors):
            hole_poly = Polygon(hole_ring).buffer(0)
            if hole_poly.is_empty or hole_poly.area <= 0:
                continue
            cands = [int(k) for k in tree_h.query(hole_poly.buffer(0.2))]
            if not cands:
                cands = list(range(len(clean_polys)))
            best_j = max(cands, key=lambda j: hole_poly.boundary.intersection(clean_polys[j].boundary).length)
            merged = unary_union([clean_polys[best_j], hole_poly]).buffer(0)
            if merged.geom_type == "Polygon" and merged.is_valid and not merged.interiors:
                clean_polys[best_j] = orient(merged, 1.0)
        outline = unary_union(clean_polys)
        if outline.geom_type != "Polygon":
            raise ValueError(f"Union of polygons is disconnected ({outline.geom_type})")
        if outline.interiors:
            raise ValueError(f"Union of polygons still has {len(outline.interiors)} enclosed interior hole(s)")
        auto_node = True
    outline = snap_polygon(orient(outline, 1.0), snap=SNAP)
    return partition_from_polygons(
        clean_polys,
        outline=outline,
        face_metadata=[dict(m) for m in face_metadata],
        snap=SNAP,
        auto_node=auto_node,
    )


def _pair_edges_map(edges: Sequence[Dict[str, Any]]) -> Dict[Tuple[int, int], List[Dict[str, Any]]]:
    m: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)
    for e in edges:
        a, b = int(e["left_face"]), int(e["right_face"])
        if min(a, b) >= 0:
            key = (a, b) if a < b else (b, a)
            m[key].append(e)
    return m


# ---------------------------------------------------------------------------
# Phase A: Spanning-tree 360-deg interlocking (lock_margin_mm >= 3.0 mm)
# ---------------------------------------------------------------------------

def lock_partition_sparse(
    part: PlanarPartition,
    placed_targets: Sequence[Polygon],
    threshold_mm: float = 3.45,
    min_neck_mm: float = 3.0,
    max_tip_mm: float = 6.0,
    workers: int = 0,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[PlanarPartition, List[Dict[str, Any]]]:
    """Construct a minimum-silhouette-loss 360-deg strongly-connected lock graph (`lock_margin >= threshold_mm`).

    Instead of blindly placing N-1 pairwise tabs, exploits natural multi-neighbor blocking in the
    exact 360-degree directed blocking graph and adds sleek undercut tabs ONLY on the lowest-IoU-loss
    seams needed to achieve strong connectivity across all 360 directions.
    """
    t0 = time.perf_counter()
    d = part.to_dict()
    edges = d["edges"]
    faces = d["faces"]
    n = len(faces)
    polys = [face_from_edges(f, edges) for f in faces]
    meta_list = [f["metadata"].copy() for f in faces]
    pair_map = _pair_edges_map(edges)
    max_workers = workers if workers > 0 else max(1, min(8, os.cpu_count() or 1))

    # Build exact 360-direction sparse directed blocking weights W[dir, directed_pair_idx]
    pair_to_widx: Dict[Tuple[int, int], int] = {}
    rows_l: List[int] = []
    cols_l: List[int] = []
    weights_l: List[np.ndarray] = []

    def _pair_directed_weights(a: int, b: int) -> Tuple[np.ndarray, np.ndarray]:
        key = (a, b) if a < b else (b, a)
        w_ab = np.zeros(360, dtype=np.float64)
        w_ba = np.zeros(360, dtype=np.float64)
        for ex in pair_map[key]:
            coords = np.asarray(ex["coords"], dtype=np.float64)
            if len(coords) < 2:
                continue
            delta = np.diff(coords, axis=0)
            nr = delta[:, [1, 0]] * np.array([1.0, -1.0])
            if int(ex["left_face"]) != a:
                nr = -nr
            dot = nr @ _DIRS_360.T
            w_ab += np.maximum(dot, 0.0).sum(axis=0)
            w_ba += np.maximum(-dot, 0.0).sum(axis=0)
        return w_ab, w_ba

    for a, b in sorted(pair_map.keys()):
        w_ab, w_ba = _pair_directed_weights(a, b)
        pair_to_widx[(a, b)] = len(weights_l)
        rows_l.append(a)
        cols_l.append(b)
        weights_l.append(w_ab)
        pair_to_widx[(b, a)] = len(weights_l)
        rows_l.append(b)
        cols_l.append(a)
        weights_l.append(w_ba)

    rows_arr = np.asarray(rows_l, dtype=np.int32)
    cols_arr = np.asarray(cols_l, dtype=np.int32)
    W = np.stack(weights_l, axis=1) if weights_l else np.zeros((360, 0), dtype=np.float64)
    ones_buf = np.ones(len(rows_arr), dtype=np.int8)

    def _dir_scc(di: int, thr: float) -> Tuple[int, np.ndarray]:
        mask = W[di] >= thr
        r = rows_arr[mask]
        c = cols_arr[mask]
        if len(r) < n:
            return n, np.arange(n, dtype=np.int32)
        mat = csr_matrix((ones_buf[: len(r)], (r, c)), shape=(n, n))
        nc, labels = connected_components(mat, directed=True, connection="strong", return_labels=True)
        return int(nc), labels

    def _refresh_unlocked(dir_indices: Sequence[int], thr: float) -> Dict[int, np.ndarray]:
        out: Dict[int, np.ndarray] = {}
        for di in dir_indices:
            nc, labels = _dir_scc(di, thr)
            if nc > 1:
                out[di] = labels
        return out

    coarse_dirs = list(range(0, 360, 5))
    all_dirs = list(range(360))
    unlocked_sccs = _refresh_unlocked(coarse_dirs, threshold_mm)
    if log:
        log(
            f"Locking {n} pieces via 360-deg SCC graph: {len(unlocked_sccs)}/{len(coarse_dirs)} "
            f"coarse directions unlocked at {threshold_mm:.2f}mm"
        )

    base_iou = [_iou(polys[i], placed_targets[i]) for i in range(n)]
    if max_workers > 1 and n > 32:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            base_tip = list(ex.map(_safe_tip, polys))
    else:
        base_tip = [_safe_tip(p) for p in polys]

    def eval_seam_fast(
        e: Dict[str, Any],
        coords: List[List[float]],
        req_margin: float,
        allow_tip_relax: bool,
    ) -> Optional[Tuple[float, float]]:
        """Thread-safe fast candidate evaluator (no mutation of shared ``edges``)."""
        eid = int(e["edge_id"])
        a, b = int(e["left_face"]), int(e["right_face"])
        key = (a, b) if a < b else (b, a)
        elist_override = [
            {**x, "coords": coords} if int(x["edge_id"]) == eid else x
            for x in pair_map[key]
        ]
        pm = pair_margin_fast(elist_override, a, b)
        if pm < req_margin:
            return None

        ls = LineString(coords)
        if not ls.is_simple:
            return None
        union_ab = polys[a].union(polys[b])
        if ls.difference(union_ab.buffer(1e-4)).length > 0.008:
            return None

        try:
            na = _face_from_edges_override(faces[a], edges, eid, coords)
            nb = _face_from_edges_override(faces[b], edges, eid, coords)
            if any(not p.is_valid or p.interiors or p.geom_type != "Polygon" for p in (na, nb)):
                return None
            if na.intersection(nb).area > 0.002 or na.union(nb).symmetric_difference(union_ab).area > 0.002:
                return None
            if not _fast_neck_ok(na, min_neck_mm=min_neck_mm, kerf_mm=0.25):
                return None
            if not _fast_neck_ok(nb, min_neck_mm=min_neck_mm, kerf_mm=0.25):
                return None
            ta_after = _fast_tip_loss(na)
            tb_after = _fast_tip_loss(nb)
            if not allow_tip_relax:
                if ta_after > max(max_tip_mm + 0.25, base_tip[a] + 0.5) or tb_after > max(max_tip_mm + 0.25, base_tip[b] + 0.5):
                    return None
            loss = (base_iou[a] + base_iou[b]) - (_iou(na, placed_targets[a]) + _iou(nb, placed_targets[b]))
            tip_pen = 0.005 * max(0.0, ta_after - max_tip_mm) + 0.005 * max(0.0, tb_after - max_tip_mm)
            cost = loss + 0.0002 * na.symmetric_difference(polys[a]).area + tip_pen + 0.001 / max(pm, 1.0)
            return cost, pm
        except (ValueError, shapely.GEOSException):
            return None

    def eval_seam_exact(
        e: Dict[str, Any],
        coords: List[List[float]],
        req_margin: float,
        allow_tip_relax: bool,
    ) -> Optional[Tuple[float, Polygon, Polygon, float]]:
        """Full exact 4-kerf + exact tip-loss confirmation before accepting a seam edit."""
        eid = int(e["edge_id"])
        a, b = int(e["left_face"]), int(e["right_face"])
        key = (a, b) if a < b else (b, a)
        elist_override = [
            {**x, "coords": coords} if int(x["edge_id"]) == eid else x
            for x in pair_map[key]
        ]
        pm = pair_margin_fast(elist_override, a, b)
        if pm < req_margin:
            return None
        ls = LineString(coords)
        if not ls.is_simple:
            return None
        union_ab = polys[a].union(polys[b])
        if ls.difference(union_ab.buffer(1e-4)).length > 0.008:
            return None
        try:
            na = _face_from_edges_override(faces[a], edges, eid, coords)
            nb = _face_from_edges_override(faces[b], edges, eid, coords)
            if any(not p.is_valid or p.interiors or p.geom_type != "Polygon" for p in (na, nb)):
                return None
            if na.intersection(nb).area > 0.002 or na.union(nb).symmetric_difference(union_ab).area > 0.002:
                return None
            if not _fab_ok(na, min_neck_mm=min_neck_mm, all_widths=True):
                return None
            if not _fab_ok(nb, min_neck_mm=min_neck_mm, all_widths=True):
                return None
            ta_after = _safe_tip(na)
            tb_after = _safe_tip(nb)
            if not allow_tip_relax:
                if ta_after > max(max_tip_mm, base_tip[a] + 0.35) or tb_after > max(max_tip_mm, base_tip[b] + 0.35):
                    return None
            loss = (base_iou[a] + base_iou[b]) - (_iou(na, placed_targets[a]) + _iou(nb, placed_targets[b]))
            tip_pen = 0.005 * max(0.0, ta_after - max_tip_mm) + 0.005 * max(0.0, tb_after - max_tip_mm)
            cost = loss + 0.0002 * na.symmetric_difference(polys[a]).area + tip_pen + 0.001 / max(pm, 1.0)
            return cost, na, nb, pm
        except (ValueError, shapely.GEOSException):
            return None

    def propose_for_edge(e: Dict[str, Any], relaxed: bool = False) -> Tuple[int, List[Tuple[float, List[List[float]], float]]]:
        eid = int(e["edge_id"])
        line = LineString(e["coords"])
        min_len = 6.5 if relaxed else 8.5
        if line.length < min_len:
            return eid, []
        choices: List[Tuple[float, List[List[float]], float]] = []
        sizes = (0.50, 0.56, 0.64) if not relaxed else (0.46, 0.52, 0.58, 0.68, 0.80, 0.92)
        fracs = (0.50, 0.30, 0.70, 0.18, 0.82) if not relaxed else (0.50, 0.35, 0.65, 0.22, 0.78)
        req_m = threshold_mm if not relaxed else 3.15
        fns = (_sleek_tab_curve, _compact_tab_curve, tab_curve)
        for fn in fns:
            for sz in sizes:
                for fr in fracs:
                    for sign in (-1.0, 1.0):
                        coords = fn(line, fr, sign, sz)
                        if coords is None:
                            continue
                        coords = _clean_edge_coords(coords, min_seg_mm=0.06)
                        if len(coords) < 4:
                            continue
                        res = eval_seam_fast(e, coords, req_margin=req_m, allow_tip_relax=relaxed)
                        if res is not None:
                            choices.append((res[0], coords, sz))
                if choices and not relaxed:
                    break
            if choices and not relaxed:
                break
        choices.sort(key=lambda z: z[0])
        return eid, choices[:3]

    # Parallel proposal generation across all interior seams that aren't already pairwise locked
    cand_edges = [
        e for e in edges
        if min(int(e["left_face"]), int(e["right_face"])) >= 0
        and pair_margin_fast(pair_map[(min(int(e["left_face"]), int(e["right_face"])), max(int(e["left_face"]), int(e["right_face"])))], int(e["left_face"]), int(e["right_face"])) < threshold_mm
    ]
    proposals: Dict[int, List[Tuple[float, List[List[float]], float]]] = {}
    if max_workers > 1 and len(cand_edges) > 16:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for eid, ch in ex.map(lambda e: propose_for_edge(e, False), cand_edges):
                if ch:
                    proposals[eid] = ch
    else:
        for e in cand_edges:
            eid, ch = propose_for_edge(e, False)
            if ch:
                proposals[eid] = ch

    if log:
        log(f"  proposal pool built: {len(proposals)} candidate seams from {len(cand_edges)} edges ({time.perf_counter() - t0:.1f}s)")

    selected: List[Dict[str, Any]] = []
    face_ver = [0] * n
    exact_cache: Dict[int, Tuple[int, int, float, bool, Optional[Tuple[Any, ...]]]] = {}

    def _run_scc_merge(dir_indices: Sequence[int], req_m: float, allow_tip_relax: bool) -> Dict[int, np.ndarray]:
        unl = _refresh_unlocked(dir_indices, req_m)
        while unl:
            dir_keys = list(unl.keys())
            lbl_mat = np.stack([unl[di] for di in dir_keys], axis=0)  # (U, n)
            ranked: List[Tuple[float, int, int]] = []
            for eid, opts in proposals.items():
                if not opts:
                    continue
                e = edges[eid]
                a, b = int(e["left_face"]), int(e["right_face"])
                gain = int(np.count_nonzero(lbl_mat[:, a] != lbl_mat[:, b]))
                if gain <= 0:
                    continue
                # Prefer lowest IoU loss, rewarding seams that fuse more unlocked directions at once
                rank_sc = float(opts[0][0]) - 0.015 * math.log1p(gain)
                ranked.append((rank_sc, gain, eid))
            if not ranked:
                break
            ranked.sort(key=lambda x: x[0])

            chosen = None
            for _, _, eid in ranked:
                e = edges[eid]
                a, b = int(e["left_face"]), int(e["right_face"])
                cached = exact_cache.get(eid)
                if (
                    cached is not None
                    and cached[0] == face_ver[a]
                    and cached[1] == face_ver[b]
                    and cached[2] == req_m
                    and cached[3] == allow_tip_relax
                ):
                    valid_opt = cached[4]
                else:
                    valid_opt = None
                    for _, coords, sz in proposals[eid]:
                        r = eval_seam_exact(e, coords, req_margin=req_m, allow_tip_relax=allow_tip_relax)
                        if r is not None:
                            valid_opt = (r[0], eid, coords, sz, r[1], r[2], r[3])
                            break
                    exact_cache[eid] = (face_ver[a], face_ver[b], req_m, allow_tip_relax, valid_opt)
                if valid_opt is None:
                    proposals.pop(eid, None)
                    continue
                chosen = valid_opt
                break
            if chosen is None:
                break

            cost, eid, coords, sz, na, nb, pm = chosen
            e = edges[eid]
            a, b = int(e["left_face"]), int(e["right_face"])
            polys[a], polys[b] = na, nb
            face_ver[a] += 1
            face_ver[b] += 1
            e["coords"] = coords
            base_iou[a] = _iou(na, placed_targets[a])
            base_iou[b] = _iou(nb, placed_targets[b])
            base_tip[a] = _safe_tip(na)
            base_tip[b] = _safe_tip(nb)
            w_ab, w_ba = _pair_directed_weights(a, b)
            W[:, pair_to_widx[(a, b)]] = w_ab
            W[:, pair_to_widx[(b, a)]] = w_ba
            selected.append(dict(edge_id=eid, a=a, b=b, margin=round(pm, 3), scale=sz, cost=round(cost, 4)))
            proposals.pop(eid, None)
            exact_cache.pop(eid, None)

            # Incrementally update SCCs only for currently unlocked directions where a and b were separated
            for di in dir_keys:
                if unl[di][a] != unl[di][b]:
                    nc, labels = _dir_scc(di, req_m)
                    if nc <= 1:
                        del unl[di]
                    else:
                        unl[di] = labels
            if log and len(selected) % 50 == 0:
                log(f"  locked {len(selected)} tabs -> {len(unl)}/{len(dir_indices)} unlocked dirs ({time.perf_counter() - t0:.1f}s)")
        return unl

    # Stage 1: Coarse 72 directions (fast) -> Stage 2: Exact all 360 directions
    _run_scc_merge(coarse_dirs, req_m=threshold_mm, allow_tip_relax=False)
    rem_unl = _run_scc_merge(all_dirs, req_m=threshold_mm, allow_tip_relax=False)

    # Fallback relaxed pass if any directions remain unlocked
    if rem_unl:
        if log:
            log(f"  running relaxed bridge pass for {len(rem_unl)}/360 remaining unlocked directions...")
        lbl_mat = np.stack(list(rem_unl.values()), axis=0)
        rem_edges = [
            e for e in edges
            if min(int(e["left_face"]), int(e["right_face"])) >= 0
            and int(e["edge_id"]) not in proposals
            and bool(np.any(lbl_mat[:, int(e["left_face"])] != lbl_mat[:, int(e["right_face"])]))
        ]
        if max_workers > 1 and len(rem_edges) > 8:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                for eid, ch in ex.map(lambda e: propose_for_edge(e, True), rem_edges):
                    if ch:
                        proposals[eid] = ch
        else:
            for e in rem_edges:
                eid, ch = propose_for_edge(e, True)
                if ch:
                    proposals[eid] = ch
        _run_scc_merge(all_dirs, req_m=3.15, allow_tip_relax=True)

    lc = lock_check_sparse(edges, n)
    if log:
        log(
            f"Locking finished: {len(selected)} tabs added, "
            f"lock_margin={lc['lock_margin_mm']:.3f}mm, unlocked@3mm={lc['unlocked_directions_at_3mm']} "
            f"({time.perf_counter() - t0:.1f}s)"
        )

    rebuilt = _rebuild_partition(polys, meta_list, auto_node=False)
    return rebuilt, selected


# ---------------------------------------------------------------------------
# Phase B: Neck & Thin-Tip Repair (both exterior and interior seams)
# ---------------------------------------------------------------------------

def _arc(line: LineString, s: float, t: float) -> List[Tuple[float, float]]:
    if t >= s:
        return list(substring(line, s, t).coords)
    return list(substring(line, s, line.length).coords) + list(substring(line, 0.0, t).coords)[1:]


def _taper(a: np.ndarray, p: np.ndarray, q: np.ndarray) -> Optional[np.ndarray]:
    u = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(a, axis=0), axis=1))]
    if u[-1] < 1.5:
        return None
    s_len = min(10.0, u[-1] * 0.35)
    start = np.clip(1.0 - u / s_len, 0.0, 1.0) ** 2
    end = np.clip(1.0 - (u[-1] - u) / s_len, 0.0, 1.0) ** 2
    return a + start[:, None] * (p - a[0]) + end[:, None] * (q - a[-1])


def _exterior_reroute_paths(edge: Dict[str, Any], target: Polygon) -> List[List[List[float]]]:
    old = LineString(edge["coords"])
    p = np.array(old.coords[0], dtype=np.float64)
    q = np.array(old.coords[-1], dtype=np.float64)
    if target.geom_type != "Polygon" or target.interiors:
        return []
    ring = LineString(target.exterior.coords)
    a, b = ring.project(Point(p)), ring.project(Point(q))
    out: List[List[List[float]]] = []
    for xy in (_arc(ring, a, b), _arc(ring, b, a)[::-1]):
        if len(xy) < 3:
            continue
        line = LineString(xy)
        if line.length < old.length * 0.5 or line.length > old.length * 1.65:
            continue
        if line.distance(Point(p)) > 1.5 or line.distance(Point(q)) > 1.5:
            continue
        if float(np.max(shapely.distance(shapely.points(np.asarray(line.coords)), old))) > 11.0:
            continue
        u = np.linspace(0.0, 1.0, max(10, int(line.length / 0.7) + 1))
        pts = _taper(shapely.get_coordinates(shapely.line_interpolate_point(line, u, normalized=True)), p, q)
        if pts is None:
            continue
        cc = _clean_edge_coords(pts, min_seg_mm=0.08)
        if len(cc) >= 3 and LineString(cc).is_simple:
            out.append(cc)
    return out


def _split_clean_transfer(
    p: Polygon,
    pj: Polygon,
    union_ij: Polygon,
    grab: Any,
    max_orphan_frac: float = 0.22,
) -> Optional[Tuple[Polygon, Polygon]]:
    """Transfer ``grab`` from ``pj`` to ``p`` on a single consistent ``SNAP`` grid with zero sliver mismatch."""
    if grab.is_empty or grab.area <= 0.1:
        return None
    try:
        u_sn = shapely.set_precision(union_ij, SNAP)
        pj_sn = shapely.set_precision(pj, SNAP)
        gr_sn = shapely.set_precision(grab, SNAP)
        qj_raw = shapely.difference(pj_sn, gr_sn, grid_size=SNAP).buffer(0)
        parts = sorted(
            [g for g in shapely.get_parts(qj_raw) if g.geom_type == "Polygon" and g.area > 1e-4],
            key=lambda g: g.area,
            reverse=True,
        )
        if not parts:
            return None
        qj_main = parts[0]
        if qj_main.interiors:
            # Fill tiny accidental interior pinholes caused by grab cutting inside pj
            qj_main = Polygon(qj_main.exterior.coords).buffer(0)
        orphan_area = sum(g.area for g in parts[1:])
        if orphan_area > pj.area * max_orphan_frac:
            return None
        qi_raw = shapely.difference(u_sn, qj_main, grid_size=SNAP).buffer(0)
        qi_parts = sorted(
            [g for g in shapely.get_parts(qi_raw) if g.geom_type == "Polygon" and g.area > 1e-4],
            key=lambda g: g.area,
            reverse=True,
        )
        if not qi_parts:
            return None
        qi_main = qi_parts[0]
        if len(qi_parts) > 1 or qi_main.interiors:
            if sum(g.area for g in qi_parts[1:]) > p.area * 0.05 or qi_main.interiors:
                return None
            qj_main = shapely.difference(u_sn, qi_main, grid_size=SNAP).buffer(0)
        if qi_main.geom_type != "Polygon" or qj_main.geom_type != "Polygon":
            return None
        if qi_main.interiors or qj_main.interiors:
            return None
        return orient(qi_main, 1.0), orient(qj_main, 1.0)
    except shapely.GEOSException:
        return None


def _kerf_all_pass_and_lobe(poly: Polygon, min_neck_mm: float = 3.0) -> Tuple[bool, float]:
    """Check whether all 4 kerf widths stay single-piece and return max secondary lobe area."""
    if _safe_tip(poly) >= 900.0:
        return False, 1e9
    max_sec = 0.0
    for k in (0.25, 0.20, 0.15, 0.10):
        q = analyze_piece_fabrication(poly, kerf_mm=k, min_neck_mm=min_neck_mm)
        if not q["kerf_pass"] or q["core_survival_ratio"] < 0.20:
            return False, 1e9
        if q["secondary_lobe_max_mm2"] > max_sec:
            max_sec = float(q["secondary_lobe_max_mm2"])
    return True, max_sec


def _oriented_pair_margin(poly_a: Polygon, poly_b: Polygon, a: int, b: int) -> float:
    """Compute exact 360-deg blocking margin between two polygons with verified per-component orientation."""
    shared_b = poly_a.boundary.intersection(poly_b.boundary)
    lines = [g for g in shapely.get_parts(shared_b) if g.geom_type == "LineString" and g.length > 0.05]
    if not lines:
        return 0.0
    oriented_edges: List[Dict[str, Any]] = []
    for ln in lines:
        arr = np.asarray(ln.coords, dtype=np.float64)
        if len(arr) < 2:
            continue
        diffs = np.diff(arr, axis=0)
        seg_lens = np.hypot(diffs[:, 0], diffs[:, 1])
        k_max = int(np.argmax(seg_lens))
        slen = float(seg_lens[k_max])
        if slen < 1e-6:
            continue
        mid = 0.5 * (arr[k_max] + arr[k_max + 1])
        n_left = np.array([-diffs[k_max, 1] / slen, diffs[k_max, 0] / slen], dtype=np.float64)
        lf, rf = a, b
        for eps in (1e-3, 1e-4, 5e-3):
            in_a = poly_a.contains(Point(mid[0] + eps * n_left[0], mid[1] + eps * n_left[1]))
            in_b = poly_b.contains(Point(mid[0] + eps * n_left[0], mid[1] + eps * n_left[1]))
            if in_a and not in_b:
                lf, rf = a, b
                break
            if in_b and not in_a:
                lf, rf = b, a
                break
        oriented_edges.append({"left_face": lf, "right_face": rf, "coords": arr.tolist()})
    return pair_margin_fast(oriented_edges, a, b)


def repair_necks_and_tips(
    part: PlanarPartition,
    placed_targets: Sequence[Polygon],
    min_neck_mm: float = 3.0,
    max_tip_mm: float = 6.0,
    preserve_lock_mm: float = 0.0,
    max_passes: int = 3,
    workers: int = 0,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[PlanarPartition, List[Dict[str, Any]]]:
    """Repair all pieces failing 3.0 mm neck erosion OR having thin tips > ``max_tip_mm``."""
    t0 = time.perf_counter()
    d = part.to_dict()
    edges = d["edges"]
    faces = d["faces"]
    n = len(faces)
    polys = [face_from_edges(f, edges) for f in faces]
    meta_list = [f["metadata"].copy() for f in faces]
    max_workers = workers if workers > 0 else max(1, min(8, os.cpu_count() or 1))
    history: List[Dict[str, Any]] = []

    def _scan_piece(i: int) -> Tuple[int, bool, float]:
        p = polys[i]
        ok = _fab_ok(p, min_neck_mm=min_neck_mm, all_widths=True)
        tip = _safe_tip(p)
        return i, (not ok), tip

    for pass_idx in range(max_passes):
        if max_workers > 1 and n > 24:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                scans = list(ex.map(_scan_piece, range(n)))
        else:
            scans = [_scan_piece(i) for i in range(n)]

        bad = [(i, nf, tp) for i, nf, tp in scans if nf or tp > max_tip_mm]
        if not bad:
            break
        bad.sort(key=lambda item: (not item[1], -item[2]))
        if log:
            n_neck = sum(1 for _, nf, _ in bad if nf)
            n_tip = sum(1 for _, _, tp in bad if tp > max_tip_mm)
            log(f"Repair pass {pass_idx + 1}: {len(bad)} pieces to fix (neck_fails={n_neck}, tips>{max_tip_mm}mm={n_tip})")

        tree = STRtree(polys)
        polys_pass_start = list(polys)
        pass_edits: List[Tuple[Dict[int, Polygon], Dict[str, Any]]] = []
        fixed_this_pass = 0
        touched_faces: set[int] = set()

        for i, neck_fail, old_tip in bad:
            if i in touched_faces:
                continue
            p = polys[i]
            cur_fab_ok = _fab_ok(p, min_neck_mm=min_neck_mm, all_widths=True)
            cur_tip = _safe_tip(p)
            if cur_fab_ok and cur_tip <= max_tip_mm:
                continue
            _, cur_sec_lobe = _kerf_all_pass_and_lobe(p, min_neck_mm=min_neck_mm) if not cur_fab_ok else (True, 0.0)

            kerf_p = p.buffer(-0.125, quad_segs=10)
            core = kerf_p.buffer(-1.5, quad_segs=10)
            opened = core.buffer(1.5, quad_segs=10).intersection(kerf_p) if not core.is_empty else Polygon()
            lost = kerf_p.difference(opened) if not opened.is_empty else kerf_p
            weak_geoms = [
                g for g in shapely.get_parts(lost)
                if g.geom_type == "Polygon" and g.area > 0.05
            ]
            bridges: List[LineString] = []
            for k_check in (0.25, 0.10):
                c_k = p.buffer(-(min_neck_mm + k_check) * 0.5, quad_segs=10)
                cp_k = sorted(
                    [g for g in shapely.get_parts(c_k) if g.geom_type == "Polygon" and g.area > 0.5],
                    key=lambda g: g.area,
                    reverse=True,
                )
                if len(cp_k) >= 2:
                    pt1, pt2 = nearest_points(cp_k[0], cp_k[1])
                    br_k = LineString([pt1, pt2])
                    bridges.append(br_k)
                    weak_geoms.insert(0, br_k.buffer(1.2, quad_segs=8).intersection(p))
                    break

            if not weak_geoms:
                continue
            weak_union = unary_union(weak_geoms)

            cand_nbrs = [int(j) for j in tree.query(p.buffer(0.2)) if int(j) != i]
            touching = sorted(
                ((p.boundary.intersection(polys[j].boundary).length, j) for j in cand_nbrs),
                reverse=True,
            )
            touching = [(l, j) for l, j in touching if l > 0.2]

            repaired = False

            # Strategy 1: Exterior boundary thickening / smoothing
            ext_edges = [
                e for e in edges
                if e["is_exterior"] and i in (int(e["left_face"]), int(e["right_face"])) and LineString(e["coords"]).length >= 4.0
            ]
            if ext_edges and any(LineString(e["coords"]).distance(weak_union) < 3.5 for e in ext_edges):
                # Include ALL pieces within 6 mm (not only touching neighbors) so exterior bulges never bridge coves & trap holes
                nearby_ext = [int(k) for k in tree.query(p.buffer(6.0)) if int(k) != i]
                near_others = _safe_union([polys[k] for k in nearby_ext]) if nearby_ext else Polygon()
                int_seams = _safe_union(
                    [LineString(e["coords"]) for e in edges if not e["is_exterior"] and i in (int(e["left_face"]), int(e["right_face"]))]
                )
                block = near_others.buffer(0.25).union(int_seams.buffer(0.6))
                best_ext = None
                for mode in ("add", "mix", "trim"):
                    for w_buf in (1.2, 1.8, 2.4, 3.0):
                        try:
                            support = weak_union.buffer(w_buf, quad_segs=6).difference(block)
                            if mode == "add":
                                tgt = _safe_union([p, support])
                            elif mode == "mix":
                                tgt = _safe_union([opened, support, p.intersection(block)])
                            else:
                                tgt = _safe_union([opened, p.intersection(block)])
                            tgt = tgt.buffer(0).simplify(0.15, preserve_topology=True)
                        except shapely.GEOSException:
                            continue
                        if tgt.geom_type != "Polygon" or tgt.interiors:
                            continue
                        for e in ext_edges:
                            if LineString(e["coords"]).distance(weak_union) > w_buf + 2.5:
                                continue
                            eid = int(e["edge_id"])
                            for cc in _exterior_reroute_paths(e, tgt):
                                try:
                                    q = _face_from_edges_override(faces[i], edges, eid, cc)
                                    if not q.is_valid or q.interiors or q.geom_type != "Polygon":
                                        continue
                                    q_sn = snap_polygon(q, snap=SNAP)
                                    if not q_sn.is_valid or q_sn.interiors or q_sn.geom_type != "Polygon":
                                        continue
                                    if not near_others.is_empty and q.intersection(near_others).area > 1e-5:
                                        continue
                                    if not near_others.is_empty:
                                        loc_u = _safe_union([q, near_others])
                                        if getattr(loc_u, "interiors", None) or any(
                                            len(g.interiors) > 0 for g in shapely.get_parts(loc_u) if g.geom_type == "Polygon"
                                        ):
                                            continue
                                    if not _fast_neck_ok(q, min_neck_mm=min_neck_mm, kerf_mm=0.25):
                                        continue
                                    if not _fab_ok(q, min_neck_mm=min_neck_mm, all_widths=True):
                                        continue
                                    qt = _safe_tip(q)
                                    if qt > max_tip_mm and qt > cur_tip - 0.8:
                                        continue
                                    sc = qt + 0.05 * q.symmetric_difference(p).area - 2.0 * _iou(q, placed_targets[i])
                                    if best_ext is None or sc < best_ext[0]:
                                        best_ext = (sc, e, cc, q, qt, mode, w_buf)
                                except (ValueError, shapely.GEOSException):
                                    continue
                        if mode == "trim":
                            break
                if best_ext is not None:
                    _, e_win, cc_win, q_win, qt_win, mode_win, w_win = best_ext
                    e_win["coords"] = cc_win
                    polys[i] = q_win
                    touched_faces.add(i)
                    for _, j_nb in touching:
                        touched_faces.add(j_nb)
                    pass_edits.append(
                        (
                            {i: q_win},
                            dict(piece=i + 1, mode=f"exterior-{mode_win}", width=w_win, tip_before=round(cur_tip, 2), tip_after=round(qt_win, 2)),
                        )
                    )
                    repaired = True
                    fixed_this_pass += 1
                else:
                    # Direct exterior polygon bulge/trim fallback when weak tip spans multiple exterior edges.
                    # Never simplify the whole polygon so shared interior seam vertices remain bit-for-bit intact.
                    def _ext_poly_topology_ok(cand_q: Polygon) -> bool:
                        if cand_q.geom_type != "Polygon" or cand_q.interiors or not cand_q.is_valid:
                            return False
                        try:
                            sn = snap_polygon(cand_q, snap=SNAP)
                        except (ValueError, shapely.GEOSException):
                            return False
                        if sn.geom_type != "Polygon" or sn.interiors or not sn.is_valid:
                            return False
                        if not near_others.is_empty and cand_q.intersection(near_others).area > 1e-5:
                            return False
                        if not int_seams.is_empty and int_seams.difference(cand_q.boundary.buffer(1e-5)).length > 1e-4:
                            return False
                        if not near_others.is_empty:
                            loc_u = _safe_union([cand_q, near_others])
                            if getattr(loc_u, "interiors", None) or any(
                                len(g.interiors) > 0 for g in shapely.get_parts(loc_u) if g.geom_type == "Polygon"
                            ):
                                return False
                        return True

                    best_poly_ext = None
                    for mode in ("add", "mix", "trim"):
                        for w_buf in (1.5, 2.2, 3.0):
                            try:
                                support = weak_union.buffer(w_buf, quad_segs=6).difference(block)
                                if mode == "add":
                                    cand_q = _safe_union([p, support])
                                elif mode == "mix":
                                    cand_q = _safe_union([opened, support, p.intersection(block)])
                                else:
                                    cand_q = _safe_union([opened, p.intersection(block)])
                                cand_q = cand_q.buffer(0)
                            except shapely.GEOSException:
                                continue
                            if not _ext_poly_topology_ok(cand_q):
                                continue
                            if not _fast_neck_ok(cand_q, min_neck_mm=min_neck_mm, kerf_mm=0.25):
                                continue
                            if not _fab_ok(cand_q, min_neck_mm=min_neck_mm, all_widths=True):
                                continue
                            qt = _safe_tip(cand_q)
                            if qt > max_tip_mm and qt > cur_tip - 0.8:
                                continue
                            sc = qt + 0.05 * cand_q.symmetric_difference(p).area - 2.0 * _iou(cand_q, placed_targets[i])
                            if best_poly_ext is None or sc < best_poly_ext[0]:
                                best_poly_ext = (sc, cand_q, qt, mode, w_buf)
                        if mode == "trim":
                            break
                    if best_poly_ext is None and cur_tip > max_tip_mm and not opened.is_empty:
                        # Precision tip-cap blunt: clip ONLY the extreme exterior tip while keeping interior seams intact
                        for cap_r in (max(2.0, max_tip_mm - 1.0), max(2.0, max_tip_mm - 1.6), max(2.0, max_tip_mm - 2.3)):
                            try:
                                cap_zone = opened.buffer(cap_r, quad_segs=6)
                                cand_q = p.intersection(_safe_union([cap_zone, block])).buffer(0)
                            except shapely.GEOSException:
                                continue
                            if (
                                _ext_poly_topology_ok(cand_q)
                                and _fast_neck_ok(cand_q, min_neck_mm=min_neck_mm, kerf_mm=0.25)
                                and _fab_ok(cand_q, min_neck_mm=min_neck_mm, all_widths=True)
                            ):
                                qt = _safe_tip(cand_q)
                                if qt <= max_tip_mm or qt < cur_tip - 0.5:
                                    best_poly_ext = (qt, cand_q, qt, "cap-blunt", round(cap_r, 2))
                                    if qt <= max_tip_mm:
                                        break
                    if best_poly_ext is not None:
                        _, q_win, qt_win, mode_win, w_win = best_poly_ext
                        polys[i] = q_win
                        touched_faces.add(i)
                        for _, j_nb in touching:
                            touched_faces.add(j_nb)
                        pass_edits.append(
                            (
                                {i: q_win},
                                dict(piece=i + 1, mode=f"exterior-poly-{mode_win}", width=w_win, tip_before=round(cur_tip, 2), tip_after=round(qt_win, 2)),
                            )
                        )
                        repaired = True
                        fixed_this_pass += 1

            if repaired:
                continue

            # Strategy 2: Interior neighbor widening or excess-spike trim
            best_int = None
            wt_pt_cur: Optional[Point] = None
            if cur_tip > max_tip_mm:
                try:
                    wt_pt_cur = Point(measure(p)["worst_tip"])
                except (ValueError, KeyError, shapely.GEOSException):
                    wt_pt_cur = None
            if wt_pt_cur is not None:
                touching_ordered = sorted(touching, key=lambda item: (polys[item[1]].distance(wt_pt_cur), -item[0]))
            else:
                touching_ordered = sorted(touching, key=lambda item: (polys[item[1]].distance(weak_union), -item[0]))
            for _, j in touching_ordered[:4]:
                if j in touched_faces:
                    continue
                pj = polys[j]
                if pj.distance(weak_union) > 2.5 and (wt_pt_cur is None or pj.distance(wt_pt_cur) > 2.5):
                    continue
                union_ij = _safe_union([p, pj])
                if union_ij.geom_type != "Polygon" or not union_ij.is_valid:
                    continue
                pj_tip0 = _safe_tip(pj)
                orig_margin_ij = _oriented_pair_margin(p, pj, i, j) if preserve_lock_mm > 0.0 else 0.0
                # Collect any third neighbors touching i or j to guard their locked seams in post-lock repair
                third_orig_i: Dict[int, float] = {}
                third_orig_j: Dict[int, float] = {}
                if preserve_lock_mm > 0.0:
                    third_nbrs = [
                        int(k)
                        for k in tree.query(union_ij.buffer(0.2))
                        if int(k) not in (i, j)
                    ]
                    for k in third_nbrs:
                        if p.boundary.intersection(polys[k].boundary).length > 0.2:
                            m_ik = _oriented_pair_margin(p, polys[k], i, k)
                            if m_ik >= preserve_lock_mm:
                                third_orig_i[k] = m_ik
                        if pj.boundary.intersection(polys[k].boundary).length > 0.2:
                            m_jk = _oriented_pair_margin(pj, polys[k], j, k)
                            if m_jk >= preserve_lock_mm:
                                third_orig_j[k] = m_jk
                opts: List[Tuple[str, float, Polygon, Polygon]] = []
                # Dedicated neck-bridge widening (absorbs any small severed bay peninsula cleanly)
                for br in bridges:
                    if pj.distance(br) <= 3.5:
                        for w_buf in (2.0, 2.6, 3.2, 4.0, 4.8):
                            try:
                                grab = br.buffer(w_buf, quad_segs=6).intersection(pj)
                            except shapely.GEOSException:
                                continue
                            res_t = _split_clean_transfer(p, pj, union_ij, grab)
                            if res_t is not None:
                                opts.append(("bridge-widen", w_buf, res_t[0], res_t[1]))
                for w_buf in (1.2, 1.8, 2.5, 3.2, 4.0):
                    try:
                        grab = weak_union.buffer(w_buf, quad_segs=6).intersection(pj)
                    except shapely.GEOSException:
                        continue
                    res_t = _split_clean_transfer(p, pj, union_ij, grab)
                    if res_t is not None:
                        opts.append(("widen", w_buf, res_t[0], res_t[1]))
                for wg in weak_geoms[:3]:
                    if wg.area < p.area * 0.14 and pj.distance(wg) < 0.15:
                        try:
                            opts.append(("trim", 0.0, p.difference(wg).buffer(0), _safe_union([pj, wg]).buffer(0)))
                        except shapely.GEOSException:
                            pass
                        for w_buf in (1.2, 2.0):
                            try:
                                grab = wg.buffer(w_buf, quad_segs=6).intersection(pj)
                            except shapely.GEOSException:
                                continue
                            res_t = _split_clean_transfer(p, pj, union_ij, grab)
                            if res_t is not None:
                                opts.append(("spike-widen", w_buf, res_t[0], res_t[1]))
                # Precision interior tip-cap transfer: hand ONLY the narrow tip end to neighbor j
                if cur_tip > max_tip_mm and not opened.is_empty:
                    for cap_r in (max(2.0, max_tip_mm - 1.0), max(2.0, max_tip_mm - 1.7), max(2.0, max_tip_mm - 2.4)):
                        try:
                            cap_cut = p.difference(opened.buffer(cap_r, quad_segs=6))
                        except shapely.GEOSException:
                            continue
                        for cg in shapely.get_parts(cap_cut):
                            if cg.geom_type == "Polygon" and 0.05 < cg.area < p.area * 0.14 and pj.distance(cg) < 0.15:
                                res_ct = _split_clean_transfer(pj, p, union_ij, cg)
                                if res_ct is not None:
                                    opts.append(("cap-trim", round(cap_r, 2), res_ct[1], res_ct[0]))
                                try:
                                    grab_c = cg.buffer(1.8, quad_segs=6).intersection(pj)
                                except shapely.GEOSException:
                                    continue
                                res_t = _split_clean_transfer(p, pj, union_ij, grab_c)
                                if res_t is not None:
                                    opts.append(("cap-widen", round(cap_r, 2), res_t[0], res_t[1]))
                # Worst-tip-targeted disk give/take for stubborn tips > max_tip_mm (checked lazily below if standard opts fail)
                wt_opts: List[Tuple[str, float, Polygon, Polygon]] = []

                def _eval_int_opts(cand_list: Sequence[Tuple[str, float, Polygon, Polygon]]) -> None:
                    nonlocal best_int
                    for mode, w_buf, qi, qj in cand_list:
                        try:
                            if any(q.geom_type != "Polygon" or q.interiors or not q.is_valid or q.area < 180.0 for q in (qi, qj)):
                                continue
                            qi_sn = snap_polygon(qi, snap=SNAP)
                            qj_sn = snap_polygon(qj, snap=SNAP)
                            if any(q.geom_type != "Polygon" or q.interiors or not q.is_valid for q in (qi_sn, qj_sn)):
                                continue
                            if qi.intersection(qj).area > 0.002 or _safe_union([qi, qj]).symmetric_difference(union_ij).area > 0.01:
                                continue
                        except (ValueError, shapely.GEOSException):
                            continue
                        if not _fast_neck_ok(qj, min_neck_mm=min_neck_mm):
                            continue
                        if _fast_tip_loss(qj) > max(max_tip_mm + 0.35, pj_tip0 + 0.55):
                            continue
                        if cur_fab_ok:
                            if not _fast_neck_ok(qi, min_neck_mm=min_neck_mm):
                                continue
                            if _fast_tip_loss(qi) > max(max_tip_mm + 0.35, cur_tip - 0.55):
                                continue
                        if not _fab_ok(qj, min_neck_mm=min_neck_mm, all_widths=True):
                            continue
                        qi_fab_ok = _fast_neck_ok(qi, min_neck_mm=min_neck_mm) and _fab_ok(qi, min_neck_mm=min_neck_mm, all_widths=True)
                        ti = _safe_tip(qi)
                        tj = _safe_tip(qj)
                        if not qi_fab_ok:
                            if cur_fab_ok:
                                continue
                            # Allow staged progress when piece has BOTH a neck fail and a >6mm tip: fix tip without worsening neck
                            if not (cur_tip > max_tip_mm and (ti <= max_tip_mm or ti < cur_tip - 1.5)):
                                continue
                            k_ok, sec_lobe = _kerf_all_pass_and_lobe(qi, min_neck_mm=min_neck_mm)
                            if not k_ok or sec_lobe > cur_sec_lobe + 0.5:
                                continue
                        if (not cur_fab_ok and qi_fab_ok) or ti <= max_tip_mm or ti < cur_tip - 0.8:
                            if tj > max(max_tip_mm, pj_tip0 + 0.3):
                                continue
                            if preserve_lock_mm > 0.0:
                                if orig_margin_ij >= preserve_lock_mm and _oriented_pair_margin(qi, qj, i, j) < preserve_lock_mm:
                                    continue
                                delta_zone = qi.symmetric_difference(p).buffer(0.05)
                                third_broken = False
                                for k in third_orig_i:
                                    if polys[k].intersects(delta_zone) and _oriented_pair_margin(qi, polys[k], i, k) < preserve_lock_mm:
                                        third_broken = True
                                        break
                                if third_broken:
                                    continue
                                for k in third_orig_j:
                                    if polys[k].intersects(delta_zone) and _oriented_pair_margin(qj, polys[k], j, k) < preserve_lock_mm:
                                        third_broken = True
                                        break
                                if third_broken:
                                    continue
                            loss = (_iou(p, placed_targets[i]) + _iou(pj, placed_targets[j])) - (
                                _iou(qi, placed_targets[i]) + _iou(qj, placed_targets[j])
                            )
                            fab_pen = 0.0 if qi_fab_ok else 50.0
                            sc = fab_pen + ti + 0.4 * tj + 3.0 * max(0.0, loss) + 0.02 * qi.symmetric_difference(p).area
                            if best_int is None or sc < best_int[0]:
                                best_int = (sc, j, qi, qj, ti, mode, w_buf)

                _eval_int_opts(opts)
                if best_int is None and wt_pt_cur is not None and pj.distance(wt_pt_cur) <= 2.5:
                    try:
                        for r_cap in (1.8, 2.8, 3.8, 5.0, 6.2):
                            cap_d = p.intersection(wt_pt_cur.buffer(r_cap, quad_segs=6))
                            if not cap_d.is_empty and cap_d.area < p.area * 0.16:
                                res_g = _split_clean_transfer(pj, p, union_ij, cap_d)
                                if res_g is not None:
                                    wt_opts.append(("wt-give", r_cap, res_g[1], res_g[0]))
                            grab_d = pj.intersection(wt_pt_cur.buffer(r_cap, quad_segs=6))
                            if not grab_d.is_empty and grab_d.area < pj.area * 0.16:
                                res_w = _split_clean_transfer(p, pj, union_ij, grab_d)
                                if res_w is not None:
                                    wt_opts.append(("wt-take", r_cap, res_w[0], res_w[1]))
                            if wt_opts:
                                _eval_int_opts(wt_opts)
                                wt_opts.clear()
                                if best_int is not None and best_int[4] <= max_tip_mm:
                                    break
                    except (ValueError, KeyError, shapely.GEOSException):
                        pass

            if best_int is not None:
                _, j_win, qi_win, qj_win, ti_win, mode_win, w_win = best_int
                polys[i] = qi_win
                polys[j_win] = qj_win
                touched_faces.add(i)
                touched_faces.add(j_win)
                for k_nb in tree.query(_safe_union([qi_win, qj_win]).buffer(0.2)):
                    touched_faces.add(int(k_nb))
                pass_edits.append(
                    (
                        {i: qi_win, j_win: qj_win},
                        dict(piece=i + 1, neighbor=j_win + 1, mode=f"interior-{mode_win}", width=w_win, tip_before=round(cur_tip, 2), tip_after=round(ti_win, 2)),
                    )
                )
                fixed_this_pass += 1
            elif len(touching) >= 2:
                # Strategy 3: Simultaneous multi-neighbor waist widening / tip-cap rounding for stubborn pieces
                nbr_ids = [j for _, j in touching[:4]]
                base_zone = _safe_union(bridges) if bridges else weak_union
                for mode_m, w_m in (("multi-widen", 2.0), ("multi-widen", 3.0), ("multi-widen", 4.0), ("multi-cap-trim", 0.0)):
                    try:
                        if mode_m == "multi-widen":
                            patch = base_zone.buffer(w_m, quad_segs=6)
                            cand_i = p
                            cand_nbrs: Dict[int, Polygon] = {}
                            ok_m = True
                            for j in nbr_ids:
                                grab = patch.intersection(polys[j])
                                if grab.is_empty or grab.area < 0.15:
                                    continue
                                res_t = _split_clean_transfer(cand_i, polys[j], _safe_union([cand_i, polys[j]]), grab)
                                if res_t is None:
                                    ok_m = False
                                    break
                                cand_i, qj = res_t
                                if qj.geom_type != "Polygon" or qj.interiors or not qj.is_valid or qj.area < 180.0:
                                    ok_m = False
                                    break
                                if not _fast_neck_ok(qj, min_neck_mm=min_neck_mm):
                                    ok_m = False
                                    break
                                cand_nbrs[j] = qj
                            if not ok_m or not cand_nbrs:
                                continue
                            qi = cand_i.buffer(0)
                        else:
                            if opened.is_empty or cur_tip <= max_tip_mm:
                                continue
                            cap_cut = p.difference(opened.buffer(max(2.0, max_tip_mm - 1.5), quad_segs=6))
                            cap_parts = [g for g in shapely.get_parts(cap_cut) if g.geom_type == "Polygon" and 0.05 < g.area < p.area * 0.15]
                            if not cap_parts:
                                continue
                            qi = p
                            cand_nbrs = {}
                            ok_m = True
                            for cg in cap_parts:
                                best_j = max(nbr_ids, key=lambda j: cg.boundary.intersection(polys[j].boundary).length)
                                if cg.boundary.intersection(polys[best_j].boundary).length < 0.05:
                                    continue
                                base_j = cand_nbrs.get(best_j, polys[best_j])
                                qj = _safe_union([base_j, cg]).buffer(0)
                                qi_next = qi.difference(cg).buffer(0)
                                if any(q.geom_type != "Polygon" or q.interiors or not q.is_valid for q in (qi_next, qj)):
                                    ok_m = False
                                    break
                                cand_nbrs[best_j] = qj
                                qi = qi_next
                            if not ok_m or not cand_nbrs:
                                continue

                        if qi.geom_type != "Polygon" or qi.interiors or not qi.is_valid:
                            continue
                        qi_sn = snap_polygon(qi, snap=SNAP)
                        if qi_sn.geom_type != "Polygon" or qi_sn.interiors or not qi_sn.is_valid:
                            continue
                        if any(
                            snap_polygon(qj, snap=SNAP).interiors or not snap_polygon(qj, snap=SNAP).is_valid
                            for qj in cand_nbrs.values()
                        ):
                            continue
                        orig_group_u = _safe_union([p, *(polys[j] for j in cand_nbrs)])
                        new_group_u = _safe_union([qi, *cand_nbrs.values()])
                        if new_group_u.symmetric_difference(orig_group_u).area > 0.01:
                            continue
                        if not _fab_ok(qi, min_neck_mm=min_neck_mm, all_widths=True):
                            continue
                        ti = _safe_tip(qi)
                        if cur_fab_ok and ti > max_tip_mm and ti > cur_tip - 0.6:
                            continue
                        if not all(_fab_ok(qj, min_neck_mm=min_neck_mm, all_widths=True) for qj in cand_nbrs.values()):
                            continue
                        if any(_safe_tip(qj) > max(max_tip_mm, _safe_tip(polys[j]) + 0.4) for j, qj in cand_nbrs.items()):
                            continue
                        if preserve_lock_mm > 0.0:
                            lock_broken = False
                            for j, qj in cand_nbrs.items():
                                if _oriented_pair_margin(p, polys[j], i, j) >= preserve_lock_mm and _oriented_pair_margin(qi, qj, i, j) < preserve_lock_mm:
                                    lock_broken = True
                                    break
                            if not lock_broken:
                                delta_m = qi.symmetric_difference(p).buffer(0.05)
                                group_set = {i, *cand_nbrs.keys()}
                                for k_raw in tree.query(orig_group_u.buffer(0.2)):
                                    k = int(k_raw)
                                    if k in group_set or not polys[k].intersects(delta_m):
                                        continue
                                    if p.boundary.intersection(polys[k].boundary).length > 0.2:
                                        if _oriented_pair_margin(p, polys[k], i, k) >= preserve_lock_mm and _oriented_pair_margin(qi, polys[k], i, k) < preserve_lock_mm:
                                            lock_broken = True
                                            break
                                    for j, qj in cand_nbrs.items():
                                        if polys[j].boundary.intersection(polys[k].boundary).length > 0.2:
                                            if _oriented_pair_margin(polys[j], polys[k], j, k) >= preserve_lock_mm and _oriented_pair_margin(qj, polys[k], j, k) < preserve_lock_mm:
                                                lock_broken = True
                                                break
                                    if lock_broken:
                                        break
                            if lock_broken:
                                continue
                        polys[i] = qi
                        for j, qj in cand_nbrs.items():
                            polys[j] = qj
                        touched_faces.add(i)
                        for j in cand_nbrs:
                            touched_faces.add(j)
                        pass_edits.append(
                            (
                                {i: qi, **cand_nbrs},
                                dict(piece=i + 1, neighbors=[j + 1 for j in cand_nbrs], mode=mode_m, width=w_m, tip_before=round(cur_tip, 2), tip_after=round(ti, 2)),
                            )
                        )
                        fixed_this_pass += 1
                        break
                    except (ValueError, shapely.GEOSException):
                        continue

        if fixed_this_pass > 0:
            def _try_commit(trial_polys: Sequence[Polygon]) -> Optional[PlanarPartition]:
                for auto_n in (False, True):
                    try:
                        cand_part = _rebuild_partition(trial_polys, meta_list, auto_node=auto_n)
                        if not all(f.polygon.is_valid and not f.polygon.interiors for f in cand_part.faces):
                            continue
                        # Verify exact planar Euler characteristic V - E + F == 1 & clean exterior outline incidence
                        if (len(cand_part.vertices) - len(cand_part.edges) + len(cand_part.faces)) != 1:
                            continue
                        oring = cand_part.outline.exterior
                        if any(
                            oring.distance(Point(c)) > 0.04
                            for ex in cand_part.exterior_edges
                            for c in (ex.coords[0], ex.coords[len(ex.coords) // 2], ex.coords[-1])
                        ):
                            continue
                        if any(not ex.to_linestring().is_simple for ex in cand_part.edges):
                            continue
                        if preserve_lock_mm > 0.0:
                            lc_chk = lock_check_sparse([ex.to_dict() for ex in cand_part.edges], n)
                            if lc_chk["lock_margin_mm"] < preserve_lock_mm or lc_chk["unlocked_directions_at_3mm"] > 0:
                                continue
                        return cand_part
                    except (ValueError, shapely.GEOSException):
                        continue
                return None

            rebuilt = _try_commit(polys)
            if rebuilt is not None:
                part = rebuilt
                history.extend(rec for _, rec in pass_edits)
            else:
                # Incremental commit fallback: salvage every valid edit in this pass instead of dropping all
                cur_polys = list(polys_pass_start)
                cur_part = part
                committed = 0
                for edit_map, rec in pass_edits:
                    trial = list(cur_polys)
                    for idx_k, poly_k in edit_map.items():
                        trial[idx_k] = poly_k
                    cand = _try_commit(trial)
                    if cand is not None:
                        cur_part = cand
                        cur_polys = [f.polygon for f in cand.faces]
                        history.append(rec)
                        committed += 1
                if committed == 0:
                    polys = [f.polygon for f in part.faces]
                    break
                part = cur_part
            d = part.to_dict()
            edges = d["edges"]
            faces = d["faces"]
            polys = [face_from_edges(f, edges) for f in faces]
            meta_list = [f["metadata"].copy() for f in faces]
        else:
            break

    # Always synchronize polys to the exact returned partition before reporting final metrics
    polys = [f.polygon for f in part.faces]
    if log:
        if max_workers > 1 and n > 24:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                final_scans = list(ex.map(_scan_piece, range(n)))
        else:
            final_scans = [_scan_piece(i) for i in range(n)]
        rem_neck = sum(1 for _, nf, _ in final_scans if nf)
        rem_tip = sum(1 for _, _, tp in final_scans if tp > max_tip_mm)
        worst = max(tp for _, _, tp in final_scans)
        log(
            f"Repair complete: {len(history)} edits, remaining neck_fails={rem_neck}, "
            f"tips>{max_tip_mm}mm={rem_tip}, worst_tip={worst:.2f}mm ({time.perf_counter() - t0:.1f}s)"
        )
    return part, history


# ---------------------------------------------------------------------------
# Phase C: Micro-segment cleanup (min_segment_mm >= 0.05 mm)
# ---------------------------------------------------------------------------

def clean_micro_segments(
    part: PlanarPartition,
    min_seg_mm: float = 0.05,
    log: Optional[Callable[[str], None]] = None,
) -> PlanarPartition:
    """Eliminate all polyline segments shorter than ``min_seg_mm`` while preserving exact planar topology."""
    d = part.to_dict()
    edges = d["edges"]
    faces = d["faces"]
    meta_list = [f["metadata"].copy() for f in faces]

    for e in edges:
        cleaned = _clean_edge_coords(e["coords"], min_seg_mm=max(min_seg_mm * 1.15, 0.06))
        if len(cleaned) >= 2 and LineString(cleaned).is_simple:
            eid = int(e["edge_id"])
            ok_inc = True
            for fid in (int(e["left_face"]), int(e["right_face"])):
                if fid >= 0:
                    try:
                        pf = _face_from_edges_override(faces[fid], edges, eid, cleaned)
                        if not pf.is_valid or pf.interiors or pf.geom_type != "Polygon":
                            ok_inc = False
                            break
                    except (ValueError, shapely.GEOSException):
                        ok_inc = False
                        break
            if ok_inc:
                e["coords"] = cleaned

    polys = [face_from_edges(f, edges) for f in faces]
    cur = _rebuild_partition(polys, meta_list, auto_node=False)
    ms = min_segment_length_mm([e.to_dict() for e in cur.edges])
    if ms >= min_seg_mm:
        if log:
            log(f"Micro-segment cleanup: min_segment_mm={ms:.4f}mm (>= {min_seg_mm}mm)")
        return cur

    for tol in (0.08, 0.12, 0.18, 0.25):
        simp = list(shapely.coverage_simplify([f.polygon for f in cur.faces], tol))
        try:
            cand = _rebuild_partition(simp, meta_list, auto_node=False)
        except ValueError:
            continue
        cd = cand.to_dict()
        for e in cd["edges"]:
            cl = _clean_edge_coords(e["coords"], min_seg_mm=max(min_seg_mm * 1.15, 0.06))
            if len(cl) >= 2 and LineString(cl).is_simple:
                eid = int(e["edge_id"])
                ok_inc = True
                for fid in (int(e["left_face"]), int(e["right_face"])):
                    if fid >= 0:
                        try:
                            pf = _face_from_edges_override(cd["faces"][fid], cd["edges"], eid, cl)
                            if not pf.is_valid or pf.interiors or pf.geom_type != "Polygon":
                                ok_inc = False
                                break
                        except (ValueError, shapely.GEOSException):
                            ok_inc = False
                            break
                if ok_inc:
                    e["coords"] = cl
        c_polys = [face_from_edges(f, cd["edges"]) for f in cd["faces"]]
        try:
            cand2 = _rebuild_partition(c_polys, meta_list, auto_node=False)
        except ValueError:
            cand2 = cand
        ms2 = min_segment_length_mm([e.to_dict() for e in cand2.edges])
        if ms2 > ms:
            cur, ms = cand2, ms2
        if ms >= min_seg_mm:
            break

    # Final exact-outline re-sync: ensure every exterior edge lies at 0.0 mm from cur.outline.boundary
    cur = _rebuild_partition([f.polygon for f in cur.faces], [f.metadata for f in cur.faces], auto_node=False)
    ms = min_segment_length_mm([e.to_dict() for e in cur.edges])
    if log:
        log(f"Micro-segment cleanup final: min_segment_mm={ms:.4f}mm (target >= {min_seg_mm}mm)")
    return cur
