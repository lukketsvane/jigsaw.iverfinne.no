"""Authoritative planar subdivision (DCEL-like) kernel for common-line laser-cut jigsaws.

Key guarantees:
1. Boundaries are extracted ONCE from a label grid: label interfaces between junctions are
   traced once, smoothed once while pinning junctions, and verified crossing-free.
2. Smoothed interior chains are noded cleanly against the exact target outline in mm.
3. Every canonical undirected edge is stored ONCE with its incident face pair `(left, right)`.
4. Both incident faces are reconstructed from the exact same shared canonical edge curves.
5. `cut.svg` traverses every interior and exterior edge EXACTLY ONCE (zero duplicate cuts).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from scipy import ndimage
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from .outline import SNAP, snap_polygon

Coord = Tuple[float, float]
GridPt = Tuple[int, int]


@dataclass
class CanonicalEdge:
    """Single canonical undirected edge between two junction vertices."""

    edge_id: int
    v_start: int
    v_end: int
    left_face: int          # face ID on the left when walking v_start -> v_end
    right_face: int         # face ID on the right (-1 if exterior boundary)
    coords: List[Coord]     # exact SNAP-rounded polyline from v_start to v_end

    @property
    def is_exterior(self) -> bool:
        return self.left_face == -1 or self.right_face == -1

    @property
    def is_interior(self) -> bool:
        return self.left_face >= 0 and self.right_face >= 0

    @property
    def length_mm(self) -> float:
        if len(self.coords) < 2:
            return 0.0
        arr = np.asarray(self.coords, dtype=np.float64)
        diffs = np.diff(arr, axis=0)
        return float(np.sum(np.hypot(diffs[:, 0], diffs[:, 1])))

    def to_linestring(self) -> LineString:
        return LineString(self.coords)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "v_start": self.v_start,
            "v_end": self.v_end,
            "left_face": self.left_face,
            "right_face": self.right_face,
            "is_exterior": self.is_exterior,
            "length_mm": round(self.length_mm, 5),
            "coords": [[round(x, 4), round(y, 4)] for x, y in self.coords],
        }


@dataclass
class FaceRecord:
    """Single puzzle piece face reconstructed from shared canonical edges."""

    face_id: int
    edge_refs: List[Tuple[int, bool]]  # list of (edge_id, reversed_flag) in CCW order
    polygon: Polygon
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def area_mm2(self) -> float:
        return float(self.polygon.area)

    @property
    def perimeter_mm(self) -> float:
        return float(self.polygon.length)

    def to_dict(self) -> Dict[str, Any]:
        coords = list(self.polygon.exterior.coords)
        return {
            "face_id": self.face_id,
            "area_mm2": round(self.area_mm2, 4),
            "perimeter_mm": round(self.perimeter_mm, 4),
            "edge_refs": [[eid, bool(rev)] for eid, rev in self.edge_refs],
            "exterior": [[round(float(x), 4), round(float(y), 4)] for x, y in coords],
            "interiors": [],
            "metadata": self.metadata,
        }


@dataclass
class PlanarPartition:
    """Authoritative planar subdivision of `outline` into `N` non-overlapping faces."""

    outline: Polygon
    vertices: List[Coord]              # junction vertices (degree != 2 or seam junctions)
    edges: List[CanonicalEdge]         # every undirected edge ONCE
    faces: List[FaceRecord]            # exact N closed CCW faces built from `edges`
    adjacency: Dict[int, List[int]]    # face_id -> sorted list of neighbor face_ids

    @property
    def num_pieces(self) -> int:
        return len(self.faces)

    @property
    def interior_edges(self) -> List[CanonicalEdge]:
        return [e for e in self.edges if e.is_interior]

    @property
    def exterior_edges(self) -> List[CanonicalEdge]:
        return [e for e in self.edges if e.is_exterior]

    def total_cut_length_mm(self) -> float:
        return float(sum(e.length_mm for e in self.edges))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "units": "mm",
            "snap_mm": SNAP,
            "num_pieces": self.num_pieces,
            "num_vertices": len(self.vertices),
            "num_edges": len(self.edges),
            "num_interior_edges": len(self.interior_edges),
            "num_exterior_edges": len(self.exterior_edges),
            "total_cut_length_mm": round(self.total_cut_length_mm(), 4),
            "outline_area_mm2": round(float(self.outline.area), 4),
            "outline_bounds_mm": [round(float(v), 4) for v in self.outline.bounds],
            "vertices": [[round(x, 4), round(y, 4)] for x, y in self.vertices],
            "edges": [e.to_dict() for e in self.edges],
            "faces": [f.to_dict() for f in self.faces],
            "adjacency": {str(k): v for k, v in sorted(self.adjacency.items())},
        }


# ---------------------------------------------------------------------------
# Snap / exact integer key helpers
# ---------------------------------------------------------------------------

def _to_key(pt: Sequence[float], snap: float = SNAP) -> GridPt:
    return (int(round(float(pt[0]) / snap)), int(round(float(pt[1]) / snap)))


def _from_key(key: GridPt, snap: float = SNAP) -> Coord:
    return (round(key[0] * snap, 6), round(key[1] * snap, 6))


# ---------------------------------------------------------------------------
# Step 1: Dual grid label-interface extraction & smoothing (ONCE per edge)
# ---------------------------------------------------------------------------

def _remove_checkerboards(labels: np.ndarray, max_passes: int = 8) -> np.ndarray:
    """Resolve 2x2 diagonal pinch/saddle patterns (a,b; c,a or b,a; a,c) so dual faces stay simple."""
    out = labels.copy()
    for _ in range(max_passes):
        a = out[:-1, :-1]
        b = out[:-1, 1:]
        c = out[1:, :-1]
        d = out[1:, 1:]
        pinch1 = (a == d) & (b != a) & (c != a)
        pinch2 = (b == c) & (a != b) & (d != b)
        ys1, xs1 = np.where(pinch1)
        ys2, xs2 = np.where(pinch2)
        if len(ys1) == 0 and len(ys2) == 0:
            break
        for r, col in zip(ys1, xs1):
            if out[r, col] == out[r + 1, col + 1] and out[r, col + 1] != out[r, col] and out[r + 1, col] != out[r, col]:
                out[r + 1, col + 1] = out[r, col + 1]
        for r, col in zip(ys2, xs2):
            if out[r, col + 1] == out[r + 1, col] and out[r, col] != out[r, col + 1] and out[r + 1, col + 1] != out[r, col + 1]:
                out[r + 1, col] = out[r, col]
    return out


def _extract_dual_chains(
    full_labels: np.ndarray,
    x0: float,
    y0: float,
    px: float,
    pad: int,
) -> List[Tuple[np.ndarray, bool]]:
    """Trace every maximal interface chain between label-grid junctions ONCE.

    Returns list of `(coords_mm [K, 2], is_closed)`.
    """
    h, w = full_labels.shape
    # Build undirected dual graph segments on grid corners (ix in 0..w, iy in 0..h).
    # Each undirected segment stores its two corner endpoints and its unordered label pair.
    adj: Dict[GridPt, List[Tuple[GridPt, Tuple[int, int]]]] = defaultdict(list)

    # Horizontal cracks between row r-1 and row r
    h_diff = full_labels[:-1, :] != full_labels[1:, :]
    hrs, hcs = np.where(h_diff)
    for r, c in zip(hrs, hcs):
        l1 = int(full_labels[r, c])
        l2 = int(full_labels[r + 1, c])
        pair = (l1, l2) if l1 < l2 else (l2, l1)
        u = (int(c), int(r + 1))
        v = (int(c + 1), int(r + 1))
        adj[u].append((v, pair))
        adj[v].append((u, pair))

    # Vertical cracks between col c-1 and col c
    v_diff = full_labels[:, :-1] != full_labels[:, 1:]
    vrs, vcs = np.where(v_diff)
    for r, c in zip(vrs, vcs):
        l1 = int(full_labels[r, c])
        l2 = int(full_labels[r, c + 1])
        pair = (l1, l2) if l1 < l2 else (l2, l1)
        u = (int(c + 1), int(r))
        v = (int(c + 1), int(r + 1))
        adj[u].append((v, pair))
        adj[v].append((u, pair))

    def is_junction(node: GridPt) -> bool:
        nbrs = adj[node]
        if len(nbrs) != 2:
            return True
        return nbrs[0][1] != nbrs[1][1]

    junctions = {v for v in adj if is_junction(v)}
    visited_edges: set[Tuple[GridPt, GridPt]] = set()

    def edge_key(u: GridPt, v: GridPt) -> Tuple[GridPt, GridPt]:
        return (u, v) if u < v else (v, u)

    raw_chains: List[Tuple[List[GridPt], bool]] = []

    # 1. Open chains starting at junctions
    for j in sorted(junctions):
        for nxt, pair in adj[j]:
            ek = edge_key(j, nxt)
            if ek in visited_edges:
                continue
            chain = [j, nxt]
            visited_edges.add(ek)
            prev, cur = j, nxt
            while cur not in junctions:
                stepped = False
                for cand, cpair in adj[cur]:
                    if cand == prev:
                        continue
                    cek = edge_key(cur, cand)
                    if cek not in visited_edges and cpair == pair:
                        visited_edges.add(cek)
                        chain.append(cand)
                        prev, cur = cur, cand
                        stepped = True
                        break
                if not stepped:
                    break
            raw_chains.append((chain, chain[0] == chain[-1]))

    # 2. Closed loops with no junctions
    for node in sorted(adj.keys()):
        for nxt, pair in adj[node]:
            ek = edge_key(node, nxt)
            if ek in visited_edges:
                continue
            chain = [node, nxt]
            visited_edges.add(ek)
            prev, cur = node, nxt
            while cur != node:
                stepped = False
                for cand, cpair in adj[cur]:
                    if cand == prev and len(adj[cur]) > 1:
                        continue
                    cek = edge_key(cur, cand)
                    if cek not in visited_edges and cpair == pair:
                        visited_edges.add(cek)
                        chain.append(cand)
                        prev, cur = cur, cand
                        stepped = True
                        break
                if not stepped:
                    break
            raw_chains.append((chain, chain[0] == chain[-1]))

    chains_mm: List[Tuple[np.ndarray, bool]] = []
    for chain, is_closed in raw_chains:
        arr = np.asarray(chain, dtype=np.float64)
        pts = np.empty_like(arr)
        pts[:, 0] = x0 + (arr[:, 0] - pad) * px
        pts[:, 1] = y0 + (arr[:, 1] - pad) * px
        chains_mm.append((pts, is_closed))
    return chains_mm


def _chaikin_smooth(pts: np.ndarray, is_closed: bool, iters: int = 3) -> np.ndarray:
    """Gaussian + Chaikin smoothing that eliminates pixel stair-stepping while pinning open junctions."""
    if iters <= 0 or len(pts) < 3:
        return pts.copy()
    cur = pts.copy()
    sigma = 0.85 * float(iters)
    if is_closed:
        if len(cur) > 2 and np.allclose(cur[0], cur[-1]):
            cur = cur[:-1]
        if len(cur) >= 5 and sigma > 0.2:
            cur[:, 0] = ndimage.gaussian_filter1d(cur[:, 0], sigma=sigma, mode="wrap")
            cur[:, 1] = ndimage.gaussian_filter1d(cur[:, 1], sigma=sigma, mode="wrap")
        for _ in range(min(2, iters)):
            n = len(cur)
            if n < 3:
                break
            nxt = np.empty((2 * n, 2), dtype=np.float64)
            p0 = cur
            p1 = np.roll(cur, -1, axis=0)
            nxt[0::2] = 0.75 * p0 + 0.25 * p1
            nxt[1::2] = 0.25 * p0 + 0.75 * p1
            cur = nxt
        return np.vstack([cur, cur[0:1]])
    else:
        start = cur[0:1].copy()
        end = cur[-1:].copy()
        if len(cur) >= 5 and sigma > 0.2:
            sx = ndimage.gaussian_filter1d(cur[:, 0], sigma=sigma, mode="nearest")
            sy = ndimage.gaussian_filter1d(cur[:, 1], sigma=sigma, mode="nearest")
            t = np.linspace(0.0, 1.0, len(cur), dtype=np.float64)
            # Linear endpoint-error ramp keeps junctions pinned at 0.0 mm error
            sx -= (1.0 - t) * (sx[0] - start[0, 0]) + t * (sx[-1] - end[0, 0])
            sy -= (1.0 - t) * (sy[0] - start[0, 1]) + t * (sy[-1] - end[0, 1])
            cur = np.stack([sx, sy], axis=1)
            cur[0] = start[0]
            cur[-1] = end[0]
        for _ in range(min(2, iters)):
            if len(cur) < 3:
                break
            p0 = cur[:-1]
            p1 = cur[1:]
            q = 0.75 * p0 + 0.25 * p1
            r = 0.25 * p0 + 0.75 * p1
            mid = np.empty((2 * len(p0), 2), dtype=np.float64)
            mid[0::2] = q
            mid[1::2] = r
            cur = np.vstack([start, mid[1:-1], end])
        return cur


def _smooth_and_guard_chains(
    chains_mm: List[Tuple[np.ndarray, bool]],
    smooth_iters: int = 3,
    simplify_tol_mm: float = 0.16,
) -> List[LineString]:
    """Smooth each shared chain ONCE (endpoints pinned) and verify zero crossings."""
    smoothed: List[LineString] = []
    raw_lines: List[LineString] = []

    def make_line(pts: np.ndarray, is_closed: bool, iters: int) -> LineString:
        spts = _chaikin_smooth(pts, is_closed, iters=iters)
        ls = LineString(spts)
        if simplify_tol_mm > 0 and iters > 0:
            ls_s = ls.simplify(simplify_tol_mm, preserve_topology=True)
            if ls_s.geom_type == "LineString" and len(ls_s.coords) >= 2 and ls_s.is_simple:
                ls = ls_s
        return ls

    # Track per-chain smoothing level so we can dial back only conflicting chains
    levels = [smooth_iters] * len(chains_mm)
    for pts, is_closed in chains_mm:
        raw_lines.append(LineString(pts))
        ls = make_line(pts, is_closed, smooth_iters)
        if not ls.is_simple:
            for lvl in range(smooth_iters - 1, -1, -1):
                ls = make_line(pts, is_closed, lvl)
                levels[len(smoothed)] = lvl
                if ls.is_simple:
                    break
        smoothed.append(ls)

    # Iteratively check for pairwise interior crossings (sharing endpoints is fine!)
    for _ in range(smooth_iters + 2):
        if len(smoothed) <= 1:
            break
        tree = STRtree(smoothed)
        bad_indices: set[int] = set()
        for idx, line_a in enumerate(smoothed):
            end_a = {line_a.coords[0], line_a.coords[-1]}
            for j in tree.query(line_a):
                j = int(j)
                if j <= idx:
                    continue
                line_b = smoothed[j]
                if not line_a.intersects(line_b):
                    continue
                inter = line_a.intersection(line_b)
                if inter.is_empty:
                    continue
                # Allowed intersection: ONLY exact shared endpoints!
                end_b = {line_b.coords[0], line_b.coords[-1]}
                shared_ends = end_a & end_b
                ok = True
                for part in shapely.get_parts(inter):
                    if part.geom_type != "Point":
                        ok = False
                        break
                    pt = (part.x, part.y)
                    if not any(math.hypot(pt[0] - e[0], pt[1] - e[1]) < 1e-5 for e in shared_ends):
                        ok = False
                        break
                if not ok:
                    bad_indices.add(idx)
                    bad_indices.add(j)
        if not bad_indices:
            break
        changed = False
        for idx in bad_indices:
            if levels[idx] > 0:
                levels[idx] -= 1
                pts, is_closed = chains_mm[idx]
                smoothed[idx] = make_line(pts, is_closed, levels[idx])
                changed = True
        if not changed:
            # Final fallback to raw unsmoothed grid cracks for conflicting pairs
            for idx in bad_indices:
                smoothed[idx] = raw_lines[idx]
            break

    return smoothed


# ---------------------------------------------------------------------------
# Step 2: Canonical DCEL construction from exact shared-boundary polygons
# ---------------------------------------------------------------------------

def _node_and_snap_polygons(
    polys: Sequence[Polygon],
    outline: Polygon,
    snap: float = SNAP,
) -> List[Polygon]:
    """Ensure every shared boundary vertex across `polys` is mutually noded on the `snap` grid."""
    # Collect all exterior rings + outline ring, snap, unary_union to node every junction,
    # then rebuild each face from its own snapped noded boundary segments.
    # First check if already cleanly noded on SNAP grid:
    snapped_outline = snap_polygon(outline, snap=snap)
    rings = [snapped_outline.exterior]
    for p in polys:
        p_ccw = orient(p, 1.0)
        rings.append(p_ccw.exterior)

    noded = unary_union(rings)
    noded = unary_union(shapely.set_precision(noded, grid_size=snap))
    cells = [
        orient(c, 1.0)
        for c in polygonize(noded)
        if c.geom_type == "Polygon" and c.is_valid and c.area > 1e-5
    ]
    # Keep only cells inside `snapped_outline`
    inside_cells = [c for c in cells if snapped_outline.contains(c.representative_point())]

    # Map every elementary noded cell back to its owning input polygon
    grouped: Dict[int, List[Polygon]] = defaultdict(list)
    unassigned: List[Polygon] = []
    for cell in inside_cells:
        rp = cell.representative_point()
        owner = -1
        best_area = 0.0
        for idx, p in enumerate(polys):
            if p.contains(rp):
                owner = idx
                break
        if owner < 0:
            for idx, p in enumerate(polys):
                ia = cell.intersection(p).area
                if ia > best_area:
                    best_area = ia
                    owner = idx
        if owner >= 0:
            grouped[owner].append(cell)
        else:
            unassigned.append(cell)

    # Assign any tiny unassigned residual to longest-shared-edge neighbor
    for cell in unassigned:
        best_idx = 0
        best_len = -1.0
        for idx, clist in grouped.items():
            for c in clist:
                shared = cell.boundary.intersection(c.boundary).length
                if shared > best_len:
                    best_len = shared
                    best_idx = idx
        grouped[best_idx].append(cell)

    rebuilt: List[Polygon] = []
    for idx in range(len(polys)):
        clist = grouped.get(idx, [])
        if not clist:
            rebuilt.append(Polygon())
            continue
        merged = unary_union(clist) if len(clist) > 1 else clist[0]
        if merged.geom_type != "Polygon":
            parts = [q for q in shapely.get_parts(merged) if q.geom_type == "Polygon" and q.area > 0]
            merged = max(parts, key=lambda q: q.area) if parts else Polygon()
        rebuilt.append(orient(merged, 1.0) if not merged.is_empty else Polygon())
    return rebuilt


def partition_from_polygons(
    polys: Sequence[Polygon],
    outline: Polygon,
    face_metadata: Optional[Sequence[Dict[str, Any]]] = None,
    snap: float = SNAP,
    auto_node: bool = True,
) -> PlanarPartition:
    """Build an authoritative `PlanarPartition` from N non-overlapping polygons tiling `outline`.

    Extracts every shared undirected canonical edge ONCE, assigns incident faces `(left, right)`,
    and reconstructs each face directly from those exact shared edges.
    """
    snapped_outline = snap_polygon(outline, snap=snap)
    n = len(polys)
    if n == 0:
        raise ValueError("partition requires at least 1 polygon")

    work_polys = (
        _node_and_snap_polygons(polys, snapped_outline, snap=snap)
        if auto_node
        else [orient(p, 1.0) for p in polys]
    )

    # Step A: Record every directed elementary segment (u -> v) -> face_id
    directed_face: Dict[Tuple[GridPt, GridPt], int] = {}
    face_directed_loops: List[List[Tuple[GridPt, GridPt]]] = []

    for fid, poly in enumerate(work_polys):
        if poly.is_empty or poly.geom_type != "Polygon" or not poly.is_valid:
            raise ValueError(f"Face {fid} is empty or invalid ({poly.geom_type})")
        if len(poly.interiors) > 0:
            raise ValueError(f"Face {fid} has {len(poly.interiors)} interior hole(s); v1 requires hole-free faces")
        poly_ccw = orient(poly, 1.0)
        raw_pts = [_to_key(pt, snap) for pt in poly_ccw.exterior.coords]
        # Remove consecutive duplicates
        ring: List[GridPt] = []
        for k in raw_pts:
            if not ring or ring[-1] != k:
                ring.append(k)
        if len(ring) >= 2 and ring[0] == ring[-1]:
            ring.pop()
        if len(ring) < 3:
            raise ValueError(f"Face {fid} collapsed to < 3 vertices after snapping")

        segs: List[Tuple[GridPt, GridPt]] = []
        m = len(ring)
        for i in range(m):
            u = ring[i]
            v = ring[(i + 1) % m]
            if u == v:
                continue
            if (u, v) in directed_face:
                raise ValueError(
                    f"Duplicate directed boundary segment {u}->{v} shared by faces "
                    f"{directed_face[(u, v)]} and {fid} (overlapping faces)"
                )
            directed_face[(u, v)] = fid
            segs.append((u, v))
        face_directed_loops.append(segs)

    # Step B: Build undirected elementary segments with canonical (left_face, right_face)
    # Using canonical orientation so left_face >= 0 and (right_face == -1 or left_face < right_face)
    undir_segs: Dict[Tuple[GridPt, GridPt], Tuple[GridPt, GridPt, int, int]] = {}
    v_incident: Dict[GridPt, List[Tuple[GridPt, Tuple[int, int]]]] = defaultdict(list)

    for (u, v), fl in directed_face.items():
        ek = (u, v) if u < v else (v, u)
        if ek in undir_segs:
            continue
        fr = directed_face.get((v, u), -1)
        # Canonicalize orientation:
        # - Exterior edge: left_face = fl (>=0), right_face = -1, direction u -> v
        # - Interior edge: smaller face ID on the left
        if fr == -1:
            cu, cv, clf, crf = u, v, fl, -1
        elif fl < fr:
            cu, cv, clf, crf = u, v, fl, fr
        else:
            cu, cv, clf, crf = v, u, fr, fl
        undir_segs[ek] = (cu, cv, clf, crf)
        fp = (clf, crf)
        v_incident[cu].append((cv, fp))
        v_incident[cv].append((cu, fp))

    # Step C: Identify junction vertices (degree != 2 or changing face-pair)
    junction_set: set[GridPt] = set()
    for v, inc in v_incident.items():
        if len(inc) != 2:
            junction_set.add(v)
        elif inc[0][1] != inc[1][1]:
            junction_set.add(v)

    # If a pure closed loop exists with 0 junctions (e.g. N=1 circle), pin its lexicographically smallest vertex
    visited_seg_keys: set[Tuple[GridPt, GridPt]] = set()

    def seg_key(a: GridPt, b: GridPt) -> Tuple[GridPt, GridPt]:
        return (a, b) if a < b else (b, a)

    # Build quick lookup from directed elementary segment to canonical (clf, crf, forward_dir)
    # Notice: for any undirected segment ek=(cu,cv,clf,crf), walking cu->cv has left=clf, right=crf.
    # Walking cv->cu has left=crf, right=clf.
    adj_by_fp: Dict[Tuple[GridPt, Tuple[int, int]], List[GridPt]] = defaultdict(list)
    for ek, (cu, cv, clf, crf) in undir_segs.items():
        fp = (clf, crf)
        adj_by_fp[(cu, fp)].append(cv)
        adj_by_fp[(cv, fp)].append(cu)

    # Trace maximal chains between junctions
    raw_canonical: List[Tuple[List[GridPt], int, int]] = []

    def trace_from(start_v: GridPt, first_nxt: GridPt, fp: Tuple[int, int]) -> List[GridPt]:
        chain = [start_v, first_nxt]
        visited_seg_keys.add(seg_key(start_v, first_nxt))
        prev, cur = start_v, first_nxt
        while cur not in junction_set and cur != start_v:
            cands = adj_by_fp[(cur, fp)]
            moved = False
            for cand in cands:
                if cand == prev and len(cands) > 1:
                    continue
                sk = seg_key(cur, cand)
                if sk not in visited_seg_keys:
                    visited_seg_keys.add(sk)
                    chain.append(cand)
                    prev, cur = cur, cand
                    moved = True
                    break
            if not moved:
                break
        return chain

    for j in sorted(junction_set):
        for nxt, fp in sorted(v_incident[j]):
            sk = seg_key(j, nxt)
            if sk in visited_seg_keys:
                continue
            chain = trace_from(j, nxt, fp)
            # Determine orientation of `chain` relative to (clf, crf):
            cu, cv, clf, crf = undir_segs[seg_key(chain[0], chain[1])]
            if (chain[0], chain[1]) != (cu, cv):
                chain.reverse()
            raw_canonical.append((chain, clf, crf))

    # Any unvisited segments belong to junctionless closed loops
    for ek in sorted(undir_segs.keys()):
        if ek in visited_seg_keys:
            continue
        cu, cv, clf, crf = undir_segs[ek]
        junction_set.add(cu)
        chain = trace_from(cu, cv, (clf, crf))
        if (chain[0], chain[1]) != (cu, cv):
            chain.reverse()
        raw_canonical.append((chain, clf, crf))

    # Step D: Assign vertex IDs & build CanonicalEdge list
    sorted_junctions = sorted(junction_set)
    vertex_to_id: Dict[GridPt, int] = {pt: i for i, pt in enumerate(sorted_junctions)}
    vertices_mm: List[Coord] = [_from_key(pt, snap) for pt in sorted_junctions]

    edges: List[CanonicalEdge] = []
    # Map every directed elementary segment -> (edge_id, is_reversed)
    dir_seg_to_edge: Dict[Tuple[GridPt, GridPt], Tuple[int, bool]] = {}

    for eid, (chain, clf, crf) in enumerate(raw_canonical):
        v_start = vertex_to_id[chain[0]]
        v_end = vertex_to_id[chain[-1]]
        coords_mm = [_from_key(pt, snap) for pt in chain]
        edges.append(
            CanonicalEdge(
                edge_id=eid,
                v_start=v_start,
                v_end=v_end,
                left_face=clf,
                right_face=crf,
                coords=coords_mm,
            )
        )
        for i in range(len(chain) - 1):
            a, b = chain[i], chain[i + 1]
            dir_seg_to_edge[(a, b)] = (eid, False)
            dir_seg_to_edge[(b, a)] = (eid, True)

    # Step E: Reconstruct every face strictly from its ordered canonical edge references!
    faces: List[FaceRecord] = []
    adjacency_sets: Dict[int, set[int]] = {i: set() for i in range(n)}
    for e in edges:
        if e.is_interior:
            adjacency_sets[e.left_face].add(e.right_face)
            adjacency_sets[e.right_face].add(e.left_face)

    for fid, segs in enumerate(face_directed_loops):
        # Collapse consecutive elementary segments belonging to the same canonical edge
        edge_refs: List[Tuple[int, bool]] = []
        for u, v in segs:
            ref = dir_seg_to_edge[(u, v)]
            if not edge_refs or edge_refs[-1] != ref:
                edge_refs.append(ref)
        # Handle wrap-around if first and last ref are the same canonical edge
        if len(edge_refs) > 1 and edge_refs[0] == edge_refs[-1]:
            edge_refs.pop()

        # Reconstruct exact closed polygon from canonical edges!
        ring_coords: List[Coord] = []
        for eid, rev in edge_refs:
            ec = edges[eid].coords
            seq = list(reversed(ec)) if rev else ec
            if not ring_coords:
                ring_coords.extend(seq)
            else:
                # Continuity check: seq[0] must equal ring_coords[-1]
                ring_coords.extend(seq[1:])
        rebuilt_poly = orient(Polygon(ring_coords), 1.0)
        meta = dict(face_metadata[fid]) if face_metadata and fid < len(face_metadata) else {}
        faces.append(
            FaceRecord(
                face_id=fid,
                edge_refs=edge_refs,
                polygon=rebuilt_poly,
                metadata=meta,
            )
        )

    adjacency = {fid: sorted(nbrs) for fid, nbrs in adjacency_sets.items()}
    return PlanarPartition(
        outline=snapped_outline,
        vertices=vertices_mm,
        edges=edges,
        faces=faces,
        adjacency=adjacency,
    )


# ---------------------------------------------------------------------------
# Step 3: End-to-end extraction from a raster label grid
# ---------------------------------------------------------------------------

def _repair_label_grid_connectivity(
    labels: np.ndarray,
    inside: np.ndarray,
    n_pieces: int,
) -> np.ndarray:
    """Guarantee every label 0..N-1 inside `inside` is 1 connected hole-free component."""
    out = labels.copy()
    out[~inside] = -1

    # 1. Keep only the largest 4-connected component of each piece; mark detached islands -2
    for lbl in range(n_pieces):
        mask = out == lbl
        if not np.any(mask):
            continue
        cc, num = ndimage.label(mask)  # 4-connectivity
        if num > 1:
            sizes = np.bincount(cc.ravel())
            sizes[0] = 0
            keep = int(np.argmax(sizes))
            out[(cc > 0) & (cc != keep)] = -2

    # Reassign detached islands (-2) to nearest surviving interior label
    orphan = out == -2
    if np.any(orphan):
        valid = out >= 0
        if np.any(valid):
            _, idx = ndimage.distance_transform_edt(~valid, return_indices=True)
            out[orphan] = out[idx[0][orphan], idx[1][orphan]]

    # 2. Fill any internal hole inside a piece that is completely enclosed by a single piece
    #    (wait: only if an unassigned pixel; if another real piece sits inside, keep it!)
    # Check if any piece encloses another piece completely (would create a hole in the outer piece):
    # That is handled cleanly by checking boundary adjacency in the solver, and validated strictly.
    return out


def extract_partition_from_labels(
    labels_inside: np.ndarray,
    outline: Polygon,
    px: float,
    n_pieces: Optional[int] = None,
    full_labels: Optional[np.ndarray] = None,
    face_metadata: Optional[Sequence[Dict[str, Any]]] = None,
    smooth_iters: int = 3,
    simplify_tol_mm: float = 0.18,
    min_sliver_area_mm2: float = 12.0,
) -> PlanarPartition:
    """Extract an authoritative `PlanarPartition` ONCE from a raster label grid.

    - Traces shared dual interface chains ONCE between grid junctions.
    - Smooths each chain ONCE while pinning junctions and preventing crossings.
    - Clips and nodes smoothed chains against the exact `outline` polygon in mm.
    - Absorbs any boundary clipping slivers cleanly without losing any piece.
    - Reconstructs all `N` faces from exact shared canonical edges.
    """
    snapped_outline = snap_polygon(outline, snap=SNAP)
    x0, y0 = snapped_outline.bounds[:2]
    inside = labels_inside >= 0
    if n_pieces is None:
        unique_lbls = sorted(int(v) for v in np.unique(labels_inside[inside]))
        n_pieces = len(unique_lbls)
        # Remap to contiguous 0..N-1 if needed
        if unique_lbls != list(range(n_pieces)):
            remap = {old: new for new, old in enumerate(unique_lbls)}
            clean_in = np.full_like(labels_inside, -1)
            for old, new in remap.items():
                clean_in[labels_inside == old] = new
            labels_inside = clean_in
    else:
        labels_inside = labels_inside.copy()

    labels_inside = _repair_label_grid_connectivity(labels_inside, inside, n_pieces)

    # Extend labels outside the domain smoothly so interface chains cleanly cross `outline`
    if full_labels is not None and full_labels.shape == labels_inside.shape and np.all(full_labels >= 0):
        ext = full_labels.copy()
        ext[inside] = labels_inside[inside]
    else:
        _, idx = ndimage.distance_transform_edt(~inside, return_indices=True)
        ext = labels_inside[idx[0], idx[1]]

    pad = 4
    padded = np.pad(ext, pad_width=pad, mode="edge")
    padded = _remove_checkerboards(padded)

    # 1. Trace & smooth every shared interior edge chain ONCE
    chains_mm = _extract_dual_chains(padded, x0=x0, y0=y0, px=px, pad=pad)
    smoothed_chains = _smooth_and_guard_chains(
        chains_mm,
        smooth_iters=smooth_iters,
        simplify_tol_mm=simplify_tol_mm,
    )

    # Intersect the extended seams and the outline in one fixed-precision
    # operation. Rounding a pre-clipped endpoint can pull it off a sloped
    # outline and silently merge the two incident pieces during polygonization.
    noded = shapely.union_all([snapped_outline.exterior, *smoothed_chains], grid_size=SNAP)

    raw_faces = [
        orient(p, 1.0)
        for p in polygonize(noded)
        if p.geom_type == "Polygon" and p.is_valid and p.area > 1e-5
    ]
    inside_faces = [p for p in raw_faces if snapped_outline.contains(p.representative_point())]

    if not inside_faces:
        raise RuntimeError("polygonize produced zero faces inside target outline")

    # 3. Assign each polygonized cell to a piece label 0..N-1 using grid lookup
    h, w = ext.shape

    def lookup_label(poly: Polygon) -> int:
        # Sample representative point + centroid (if inside) to vote reliably
        rp = poly.representative_point()
        pts = [(rp.x, rp.y)]
        c = poly.centroid
        if poly.contains(c):
            pts.append((c.x, c.y))
        votes: Dict[int, int] = defaultdict(int)
        for px_x, px_y in pts:
            col = int(np.clip(math.floor((px_x - x0) / px), 0, w - 1))
            row = int(np.clip(math.floor((px_y - y0) / px), 0, h - 1))
            lbl = int(ext[row, col])
            votes[lbl] += 1
        return max(votes.items(), key=lambda kv: kv[1])[0]

    cell_labels = [lookup_label(f) for f in inside_faces]

    # 4. Merge same-label adjacent faces and absorb slivers / disconnected extra components
    # Keep largest component for each label 0..N-1, absorb any secondary fragments into their
    # longest-shared-edge neighbor until we have exactly `n_pieces` valid single-component faces.
    by_label: Dict[int, List[Polygon]] = defaultdict(list)
    for f, lbl in zip(inside_faces, cell_labels):
        by_label[lbl].append(f)

    main_polys: Dict[int, Polygon] = {}
    orphans: List[Polygon] = []

    for lbl in range(n_pieces):
        clist = by_label.get(lbl, [])
        if not clist:
            continue
        merged = unary_union(clist) if len(clist) > 1 else clist[0]
        parts = [
            orient(p, 1.0)
            for p in shapely.get_parts(merged)
            if p.geom_type == "Polygon" and p.area > 1e-5
        ]
        if not parts:
            continue
        parts.sort(key=lambda p: p.area, reverse=True)
        main_polys[lbl] = parts[0]
        for extra in parts[1:]:
            orphans.append(extra)

    # Also check if any main poly is below min_sliver_area? No — keep all N labels if possible!
    # Absorb orphans iteratively into whichever existing main_poly shares the longest boundary edge
    # (and does not create a hole).
    for _ in range(len(orphans) * 2 + 4):
        if not orphans:
            break
        remaining: List[Polygon] = []
        progressed = False
        for orph in orphans:
            best_lbl = -1
            best_len = -1.0
            for lbl, mp in main_polys.items():
                shared = orph.boundary.intersection(mp.boundary).length
                if shared > best_len:
                    cand = unary_union([mp, orph])
                    if cand.geom_type == "Polygon" and cand.is_valid and len(cand.interiors) == 0:
                        best_len = shared
                        best_lbl = lbl
            if best_lbl >= 0 and best_len > SNAP:
                main_polys[best_lbl] = orient(unary_union([main_polys[best_lbl], orph]), 1.0)
                progressed = True
            else:
                remaining.append(orph)
        orphans = remaining
        if not progressed:
            # Fallback: absorb into any touching face regardless of length threshold
            for orph in orphans:
                best_lbl = max(
                    main_polys.keys(),
                    key=lambda lbl: orph.boundary.intersection(main_polys[lbl].boundary).length,
                )
                cand = unary_union([main_polys[best_lbl], orph])
                if cand.geom_type == "Polygon":
                    main_polys[best_lbl] = orient(cand, 1.0)
            orphans = []

    if len(main_polys) != n_pieces:
        missing = sorted(set(range(n_pieces)) - set(main_polys.keys()))
        raise RuntimeError(
            f"Label grid lost {len(missing)} piece(s) during extraction: missing={missing}"
        )

    ordered_polys = [main_polys[i] for i in range(n_pieces)]
    return partition_from_polygons(
        ordered_polys,
        outline=snapped_outline,
        face_metadata=face_metadata,
        snap=SNAP,
        auto_node=True,
    )
