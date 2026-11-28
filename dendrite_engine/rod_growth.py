"""2D Planar Position-Based Cosserat Rod Puzzle Growth Engine (Maze & Wave styles).

Ported directly from the MIT-licensed PositionBasedDynamics Cosserat rod formulation:
  Kugelstadt & Schomer (2016), "Position and Orientation Based Cosserat Rods"
  (see `resources/nervous-system/upstream/PositionBasedDynamics/PositionBasedDynamics/PositionBasedElasticRods.cpp`,
   `solve_StretchShearConstraint` Eq. 37 and `solve_BendTwistConstraint` Eq. 40).

Supports:
- `maze` (borderless organic meandering branches OR bounded): elastic rods lengthen,
  buckle, adaptively subdivide, and repel non-adjacent rods to maintain uniform wood width
  >= `min_neck_mm + kerf_mm` with zero self-intersections.
- `wave`: directional anisotropic initial Voronoi cells + traveling/static vortex curl field
  that bends growing Cosserat rods into interlocking spiral tendrils.
- Reflective/pinned whimsy obstacles and custom outlines.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from scipy.spatial import Voronoi
from shapely import get_parts, union_all
from shapely.geometry import LineString, Point, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import linemerge, polygonize

from .whimsies import WhimsyPlacement

ROD_SNAP_MM = 0.01
MIN_ROD_LENGTH_MM = 0.05

try:
    from numba import njit
    _HAS_NUMBA = True
except Exception:  # pragma: no cover
    _HAS_NUMBA = False

    def njit(*args, **kwargs):  # type: ignore
        def _dec(fn):
            return fn
        return _dec


@dataclass
class RodConfig:
    """Configuration for 2D Position-Based Cosserat Rod growth (Maze / Wave)."""

    style: str = "maze"                 # "maze" or "wave"
    width_mm: float = 240.0
    height_mm: float = 200.0
    num_pieces: int = 24
    borderless: bool = True             # True = organic free-form perimeter (Maze), False = crisp outline
    seed: int = 42
    min_neck_mm: float = 3.0
    kerf_mm: float = 0.15
    collision_margin_mm: float = 1.05   # extra clearance above min_neck + kerf so bends stay >= 3 mm
    seg_init_mm: float = 2.6            # initial rod segment length
    seg_max_mm: float = 3.2             # split segment when L0 exceeds this
    growth_steps: int = 95              # outer growth + subdivision steps
    pbd_substeps: int = 10              # PBD constraint projection iterations per growth step
    growth_rate: float = 0.017          # fractional rest-length increase per step
    max_length_factor: float = 2.75     # cap total rod length growth multiplier
    k_stretch: float = 0.88             # Cosserat stretch stiffness
    k_shear: float = 0.72               # Cosserat shear stiffness
    k_bend: float = 0.42                # Cosserat bending stiffness
    vortex_count: int = 6               # Wave style: number of curl vortices
    vortex_strength: float = 0.65       # Wave style: tangential curl velocity scale
    junction_mobile: bool = True        # allow interior degree-3 junctions to move slightly


@dataclass
class RodResult:
    """Outputs of the 2D Cosserat rod puzzle solver.

    Clearance is measured on the retained, snapped centerlines. The sweep
    minimum records the distances seen before individual collision corrections
    in the retained step. Either value can be absent if no pair was measured.
    """

    faces: List[Polygon]
    interior_lines: List[LineString]
    exterior_lines: List[LineString]
    whimsy_indices: List[int]
    expected_outline: Optional[Polygon]
    num_nodes: int
    num_segments: int
    min_nonadjacent_clearance_mm: Optional[float]
    elapsed_sec: float
    config: RodConfig
    last_collision_sweep_min_mm: Optional[float] = None


# ---------------------------------------------------------------------------
# Numba-accelerated 2D Cosserat Rod Constraints & Spatial-Hash Collision
# (Exact 2D specialization of MIT PositionBasedCosseratRods Eqs. 37 & 40)
# ---------------------------------------------------------------------------

@njit(cache=True)
def _pbd_cosserat_step_numba(
    pos: np.ndarray,           # (N, 2) float64
    inv_mass: np.ndarray,      # (N,) float64
    seg_u: np.ndarray,         # (E,) int32
    seg_v: np.ndarray,         # (E,) int32
    seg_rod: np.ndarray,       # (E,) int32 rod ID
    seg_idx_in_rod: np.ndarray,# (E,) int32 local index along rod
    seg_L0: np.ndarray,        # (E,) float64 rest length
    theta: np.ndarray,         # (E,) float64 Cosserat edge orientation angle
    inv_mass_q: np.ndarray,    # (E,) float64 inverse orientation inertia
    bend_e0: np.ndarray,       # (B,) int32 adjacent segment pairs along same rod
    bend_e1: np.ndarray,       # (B,) int32
    bend_k0: np.ndarray,       # (B,) float64 rest Darboux curvature
    k_stretch: float,
    k_shear: float,
    k_bend: float,
    d_col: float,
    col_stiff: float,
    max_disp: float,
    substeps: int,
) -> float:
    """Run `substeps` iterations of Cosserat stretch/shear + bend/twist + spatial-hash repulsion."""
    n_nodes = len(pos)
    n_edges = len(seg_u)
    n_bends = len(bend_e0)
    cell_size = max(d_col * 1.15, 1.0)
    inv_cell = 1.0 / cell_size
    min_clear = math.inf

    for sub in range(substeps):
        # 1. Cosserat Stretch-Shear Constraint (Eq. 37, PositionBasedElasticRods.cpp lines 15-40)
        for e in range(n_edges):
            u = seg_u[e]
            v = seg_v[e]
            w0 = inv_mass[u]
            w1 = inv_mass[v]
            wq = inv_mass_q[e]
            if w0 + w1 + wq <= 1e-12:
                continue
            L0 = seg_L0[e]
            if L0 < 1e-6:
                continue
            ct = math.cos(theta[e])
            st = math.sin(theta[e])
            # d3 = tangent director (ct, st); d1 = normal director (-st, ct)
            gx = (pos[v, 0] - pos[u, 0]) / L0 - ct
            gy = (pos[v, 1] - pos[u, 1]) / L0 - st
            denom = (w0 + w1) / L0 + 4.0 * wq * L0 + 1e-6
            # Project strain onto stretch (d3) and shear (d1)
            g_par = (gx * ct + gy * st) * k_stretch / denom
            g_perp = (-gx * st + gy * ct) * k_shear / denom
            ex = g_par * ct - g_perp * st
            ey = g_par * st + g_perp * ct
            # Clamp single-step displacement for strict non-tunneling
            mag = math.hypot(ex, ey)
            if mag > max_disp:
                sc = max_disp / mag
                ex *= sc
                ey *= sc
            pos[u, 0] += w0 * ex
            pos[u, 1] += w0 * ey
            pos[v, 0] -= w1 * ex
            pos[v, 1] -= w1 * ey
            # Orientation update: rotate d3 toward (p1 - p0)
            dtheta = 2.0 * wq * L0 * g_perp
            if dtheta > 0.35:
                dtheta = 0.35
            elif dtheta < -0.35:
                dtheta = -0.35
            theta[e] += dtheta

        # 2. Cosserat Bend-Twist Constraint (Eq. 40, PositionBasedElasticRods.cpp lines 43-65)
        for b in range(n_bends):
            e0 = bend_e0[b]
            e1 = bend_e1[b]
            wq0 = inv_mass_q[e0]
            wq1 = inv_mass_q[e1]
            wsum = wq0 + wq1 + 1e-6
            diff = theta[e1] - theta[e0]
            # Wrap to [-pi, pi]
            diff = math.atan2(math.sin(diff), math.cos(diff)) - bend_k0[b]
            lam = k_bend * diff / wsum
            theta[e0] += wq0 * lam
            theta[e1] -= wq1 * lam

        # 3. Repel nearby nonadjacent segments toward the requested centerline spacing.
        # Build uniform grid hash over segment bounding boxes
        min_x = pos[0, 0]
        max_x = pos[0, 0]
        min_y = pos[0, 1]
        max_y = pos[0, 1]
        for i in range(1, n_nodes):
            x = pos[i, 0]
            y = pos[i, 1]
            if x < min_x:
                min_x = x
            if x > max_x:
                max_x = x
            if y < min_y:
                min_y = y
            if y > max_y:
                max_y = y
        gx_n = max(1, int(math.floor((max_x - min_x) * inv_cell)) + 3)
        gy_n = max(1, int(math.floor((max_y - min_y) * inv_cell)) + 3)
        n_cells = gx_n * gy_n

        # Assign each segment to its midpoint cell (segments are <= seg_max < cell_size, so 3x3 lookup covers all!)
        cell_head = np.full(n_cells, -1, dtype=np.int32)
        seg_next = np.full(n_edges, -1, dtype=np.int32)
        for e in range(n_edges):
            u = seg_u[e]
            v = seg_v[e]
            mx = 0.5 * (pos[u, 0] + pos[v, 0])
            my = 0.5 * (pos[u, 1] + pos[v, 1])
            cx = int(math.floor((mx - min_x) * inv_cell)) + 1
            cy = int(math.floor((my - min_y) * inv_cell)) + 1
            if cx < 0:
                cx = 0
            elif cx >= gx_n:
                cx = gx_n - 1
            if cy < 0:
                cy = 0
            elif cy >= gy_n:
                cy = gy_n - 1
            cid = cy * gx_n + cx
            seg_next[e] = cell_head[cid]
            cell_head[cid] = e

        # Precompute node -> (rod_id, local_idx) for fast topological neighbor skipping
        # (Junction nodes have rod_id = -1 and skip only directly incident segments.)
        # Here we check segment-segment proximity by visiting each segment endpoint against nearby segments.
        d_col2 = d_col * d_col
        for e_a in range(n_edges):
            ua = seg_u[e_a]
            va = seg_v[e_a]
            ra = seg_rod[e_a]
            ia = seg_idx_in_rod[e_a]
            # Test both endpoints of e_a? Test ua (and va if last in rod) via midpoint lookup around e_a
            mx = 0.5 * (pos[ua, 0] + pos[va, 0])
            my = 0.5 * (pos[ua, 1] + pos[va, 1])
            cx0 = int(math.floor((mx - min_x) * inv_cell)) + 1
            cy0 = int(math.floor((my - min_y) * inv_cell)) + 1

            for dy in (-1, 0, 1):
                cy = cy0 + dy
                if cy < 0 or cy >= gy_n:
                    continue
                for dx in (-1, 0, 1):
                    cx = cx0 + dx
                    if cx < 0 or cx >= gx_n:
                        continue
                    e_b = cell_head[cy * gx_n + cx]
                    while e_b != -1:
                        if e_b > e_a:
                            ub = seg_u[e_b]
                            vb = seg_v[e_b]
                            # Skip segments sharing a vertex, adjacent within 2 hops on the same rod,
                            # or a fixed wall segment interacting with a low-mobility collar segment anchored to a wall.
                            skip = (ua == ub or ua == vb or va == ub or va == vb)
                            if not skip and ra == seg_rod[e_b]:
                                if abs(ia - seg_idx_in_rod[e_b]) <= 2:
                                    skip = True
                            if not skip:
                                a_fix = (inv_mass[ua] == 0.0 and inv_mass[va] == 0.0)
                                b_fix = (inv_mass[ub] == 0.0 and inv_mass[vb] == 0.0)
                                if (a_fix and (inv_mass[ub] < 0.2 or inv_mass[vb] < 0.2)) or (
                                    b_fix and (inv_mass[ua] < 0.2 or inv_mass[va] < 0.2)
                                ):
                                    skip = True
                            if not skip:
                                # Closest points between segment (ua,va) and (ub,vb)
                                ax = pos[ua, 0]
                                ay = pos[ua, 1]
                                abx = pos[va, 0] - ax
                                aby = pos[va, 1] - ay
                                bx = pos[ub, 0]
                                by = pos[ub, 1]
                                cdx = pos[vb, 0] - bx
                                cdy = pos[vb, 1] - by

                                # Fast bounding-box reject
                                if (max(ax, ax + abx) + d_col >= min(bx, bx + cdx)
                                        and max(bx, bx + cdx) + d_col >= min(ax, ax + abx)
                                        and max(ay, ay + aby) + d_col >= min(by, by + cdy)
                                        and max(by, by + cdy) + d_col >= min(ay, ay + aby)):
                                    # Evaluate 4 endpoint-to-segment projections and take minimum
                                    best_d2 = 1e18
                                    best_s = 0.0
                                    best_t = 0.0

                                    ab_len2 = abx * abx + aby * aby + 1e-12
                                    cd_len2 = cdx * cdx + cdy * cdy + 1e-12

                                    # ua -> seg B
                                    t = ((ax - bx) * cdx + (ay - by) * cdy) / cd_len2
                                    t = min(1.0, max(0.0, t))
                                    px = ax - (bx + t * cdx)
                                    py = ay - (by + t * cdy)
                                    d2 = px * px + py * py
                                    if d2 < best_d2:
                                        best_d2 = d2
                                        best_s = 0.0
                                        best_t = t

                                    # va -> seg B
                                    t = ((ax + abx - bx) * cdx + (ay + aby - by) * cdy) / cd_len2
                                    t = min(1.0, max(0.0, t))
                                    px = (ax + abx) - (bx + t * cdx)
                                    py = (ay + aby) - (by + t * cdy)
                                    d2 = px * px + py * py
                                    if d2 < best_d2:
                                        best_d2 = d2
                                        best_s = 1.0
                                        best_t = t

                                    # ub -> seg A
                                    s = ((bx - ax) * abx + (by - ay) * aby) / ab_len2
                                    s = min(1.0, max(0.0, s))
                                    px = (ax + s * abx) - bx
                                    py = (ay + s * aby) - by
                                    d2 = px * px + py * py
                                    if d2 < best_d2:
                                        best_d2 = d2
                                        best_s = s
                                        best_t = 0.0

                                    # vb -> seg A
                                    s = ((bx + cdx - ax) * abx + (by + cdy - ay) * aby) / ab_len2
                                    s = min(1.0, max(0.0, s))
                                    px = (ax + s * abx) - (bx + cdx)
                                    py = (ay + s * aby) - (by + cdy)
                                    d2 = px * px + py * py
                                    if d2 < best_d2:
                                        best_d2 = d2
                                        best_s = s
                                        best_t = 1.0

                                    if sub == substeps - 1 and best_d2 < min_clear * min_clear:
                                        min_clear = math.sqrt(max(best_d2, 0.0))

                                    if best_d2 < d_col2 and best_d2 > 1e-12:
                                        dist = math.sqrt(best_d2)
                                        p_a_x = ax + best_s * abx
                                        p_a_y = ay + best_s * aby
                                        p_b_x = bx + best_t * cdx
                                        p_b_y = by + best_t * cdy
                                        nx = (p_a_x - p_b_x) / dist
                                        ny = (p_a_y - p_b_y) / dist
                                        pen = (d_col - dist) * col_stiff
                                        if pen > max_disp:
                                            pen = max_disp

                                        w_ua = inv_mass[ua] * (1.0 - best_s)
                                        w_va = inv_mass[va] * best_s
                                        w_ub = inv_mass[ub] * (1.0 - best_t)
                                        w_vb = inv_mass[vb] * best_t
                                        w_tot = (
                                            inv_mass[ua] * (1.0 - best_s) ** 2
                                            + inv_mass[va] * best_s**2
                                            + inv_mass[ub] * (1.0 - best_t) ** 2
                                            + inv_mass[vb] * best_t**2
                                        )
                                        if w_tot > 1e-12:
                                            sc = pen / w_tot
                                            pos[ua, 0] += w_ua * sc * nx
                                            pos[ua, 1] += w_ua * sc * ny
                                            pos[va, 0] += w_va * sc * nx
                                            pos[va, 1] += w_va * sc * ny
                                            pos[ub, 0] -= w_ub * sc * nx
                                            pos[ub, 1] -= w_ub * sc * ny
                                            pos[vb, 0] -= w_vb * sc * nx
                                            pos[vb, 1] -= w_vb * sc * ny
                        e_b = seg_next[e_b]

    return min_clear


# ---------------------------------------------------------------------------
# Planar Cell Graph Initialization (Voronoi / Directional Voronoi around Whimsies)
# ---------------------------------------------------------------------------

def _initial_voronoi_cells(
    outline: Polygon,
    num_pieces: int,
    whimsies: Sequence[WhimsyPlacement],
    style: str,
    rng: np.random.Generator,
) -> Tuple[List[Polygon], List[int]]:
    """Create initial balanced polygonal cells inside `outline` around fixed `whimsies`."""
    x0, y0, x1, y1 = outline.bounds
    w_mm = x1 - x0
    h_mm = y1 - y0
    fixed_polys = [orient(w.polygon.intersection(outline), 1.0) for w in whimsies if w.polygon.intersects(outline)]
    fixed_union = union_all(fixed_polys) if fixed_polys else Polygon()
    active_region = outline.difference(fixed_union) if not fixed_union.is_empty else outline
    n_dyn = max(2, num_pieces - len(fixed_polys))

    # Poisson dart throwing + Lloyd relaxation inside active_region
    area_act = float(active_region.area)
    rho = math.sqrt(area_act / max(1, n_dyn)) * 0.72
    seeds: List[Tuple[float, float]] = []
    # Slightly erode active region for seed safety
    inner = active_region.buffer(-min(rho * 0.25, 4.0))
    if inner.is_empty or inner.area < area_act * 0.15:
        inner = active_region

    for _attempt in range(10):
        for _ in range(4000):
            px = float(rng.uniform(x0, x1))
            py = float(rng.uniform(y0, y1))
            if not inner.contains(Point(px, py)):
                continue
            if not seeds or min((px - sx) ** 2 + (py - sy) ** 2 for sx, sy in seeds) >= rho * rho:
                seeds.append((px, py))
                if len(seeds) >= n_dyn:
                    break
        if len(seeds) >= n_dyn:
            break
        rho *= 0.82

    while len(seeds) < n_dyn:
        px = float(rng.uniform(x0, x1))
        py = float(rng.uniform(y0, y1))
        if active_region.contains(Point(px, py)):
            seeds.append((px, py))

    pts = np.asarray(seeds[:n_dyn], dtype=np.float64)

    # For Wave style, gently elongate Voronoi cells along the flow axis without creating sliver corners
    aniso = np.array([1.16, 0.86]) if style == "wave" else np.array([1.0, 1.0])
    margin = max(w_mm, h_mm) * 2.5

    def _clip_voronoi(seed_pts: np.ndarray) -> List[Polygon]:
        # Mirror distant bounding box points so all Voronoi regions are finite
        cx, cy = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
        scaled = (seed_pts - [cx, cy]) * aniso
        dummies = np.array(
            [
                [-margin, -margin],
                [margin, -margin],
                [margin, margin],
                [-margin, margin],
                [0.0, -margin],
                [0.0, margin],
                [-margin, 0.0],
                [margin, 0.0],
            ],
            dtype=np.float64,
        )
        vor = Voronoi(np.vstack([scaled, dummies]))
        cells: List[Polygon] = []
        for idx in range(len(seed_pts)):
            reg = vor.regions[vor.point_region[idx]]
            if not reg or -1 in reg:
                continue
            verts = vor.vertices[reg] / aniso + [cx, cy]
            poly = Polygon(verts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            clipped = poly.intersection(active_region)
            parts = [p for p in get_parts(clipped) if p.geom_type == "Polygon" and p.area > 5.0]
            if parts:
                cells.append(orient(max(parts, key=lambda p: p.area), 1.0))
        return cells

    for _ in range(5):
        cells = _clip_voronoi(pts)
        if len(cells) == len(pts):
            new_pts = []
            for c_poly, old in zip(cells, pts):
                cent = c_poly.centroid
                new_pts.append([cent.x, cent.y] if active_region.contains(cent) else old)
            pts = np.asarray(new_pts, dtype=np.float64)

    cells = _clip_voronoi(pts)
    # Re-node everything cleanly so shared vertices match identically
    all_polys = list(cells) + list(fixed_polys)
    whimsy_indices = list(range(len(cells), len(all_polys)))
    return all_polys, whimsy_indices


def _build_rod_graph_from_cells(
    cells: Sequence[Polygon],
    whimsy_indices: Sequence[int],
    outline: Polygon,
    borderless: bool,
    seg_init_mm: float,
) -> Dict[str, Any]:
    """Convert planar cell polygons into a topological shared-edge rod graph."""
    snap = 0.01
    boundaries = [LineString(np.round(np.asarray(p.exterior.coords) / snap) * snap) for p in cells]
    noded = union_all(boundaries, grid_size=snap)
    raw_lines = [l for l in get_parts(noded) if l.geom_type == "LineString" and l.length > 0.05]

    # Identify exact degree!=2 junctions
    vert_deg: Dict[Tuple[float, float], int] = defaultdict(int)
    for ln in raw_lines:
        a = (round(ln.coords[0][0], 3), round(ln.coords[0][1], 3))
        b = (round(ln.coords[-1][0], 3), round(ln.coords[-1][1], 3))
        vert_deg[a] += 1
        vert_deg[b] += 1

    # Merge chains between degree!=2 junctions (keep whimsy/exterior chains tagged)
    wh_set = set(whimsy_indices)
    ext_buf = outline.exterior.buffer(0.15)
    wh_bufs = [cells[i].exterior.buffer(0.15) for i in whimsy_indices]

    # Classify each raw segment by incident faces
    edge_groups: Dict[Tuple[int, int, bool, bool], List[LineString]] = defaultdict(list)
    for ln in raw_lines:
        mid = ln.interpolate(0.5, normalized=True)
        # Small perpendicular probe to identify left/right faces reliably
        p0 = ln.interpolate(0.49, normalized=True)
        p1 = ln.interpolate(0.51, normalized=True)
        dx, dy = p1.x - p0.x, p1.y - p0.y
        norm = math.hypot(dx, dy) + 1e-12
        nx, ny = -dy / norm * 0.08, dx / norm * 0.08
        pa = Point(mid.x + nx, mid.y + ny)
        pb = Point(mid.x - nx, mid.y - ny)
        fa = next((i for i, c in enumerate(cells) if c.covers(pa)), -1)
        fb = next((i for i, c in enumerate(cells) if c.covers(pb)), -1)
        pair = (min(fa, fb), max(fa, fb))
        is_ext = pair[0] < 0 or ext_buf.covers(ln)
        is_wh = (pair[0] in wh_set) or (pair[1] in wh_set) or any(wb.covers(ln) for wb in wh_bufs)
        edge_groups[(pair[0], pair[1], is_ext, is_wh)].append(ln)

    # Build rod chains (each rod is an ordered list of node indices)
    node_map: Dict[Tuple[float, float], int] = {}
    pos_list: List[List[float]] = []
    inv_mass_list: List[float] = []
    is_junction_list: List[bool] = []
    is_exterior_node: List[bool] = []

    def _get_node(xy: Tuple[float, float], fixed: bool, junction: bool, ext_flag: bool) -> int:
        key = (round(xy[0], 3), round(xy[1], 3))
        if key not in node_map:
            node_map[key] = len(pos_list)
            pos_list.append([float(xy[0]), float(xy[1])])
            inv_mass_list.append(0.0 if fixed else 1.0)
            is_junction_list.append(junction)
            is_exterior_node.append(ext_flag)
        else:
            idx = node_map[key]
            if fixed:
                inv_mass_list[idx] = 0.0
            if junction:
                is_junction_list[idx] = True
            if ext_flag:
                is_exterior_node[idx] = True
        return node_map[key]

    rods: List[Dict[str, Any]] = []
    for (fa, fb, is_ext, is_wh), lns in edge_groups.items():
        merged = linemerge(lns)
        for part in get_parts(merged):
            if part.geom_type != "LineString" or part.length < 0.1:
                continue
            length = float(part.length)
            # Determine if this rod is fixed (whimsy boundary, or exterior when bounded)
            rod_fixed = bool(is_wh or (is_ext and not borderless))
            if rod_fixed:
                # Preserve exact corner vertices of fixed exterior/whimsy boundaries (zero chamfer/coverage loss!)
                raw_coords = [(float(x), float(y)) for x, y in part.coords]
                pts_xy = [raw_coords[0]]
                max_fix_step = seg_init_mm * 1.5
                for pa, pb in zip(raw_coords[:-1], raw_coords[1:]):
                    d_seg = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
                    n_sub = max(1, int(math.ceil(d_seg / max_fix_step)))
                    for si in range(1, n_sub + 1):
                        frac = si / n_sub
                        pts_xy.append((pa[0] + frac * (pb[0] - pa[0]), pa[1] + frac * (pb[1] - pa[1])))
            else:
                n_segs = max(2, int(math.ceil(length / seg_init_mm)))
                ds = np.linspace(0.0, length, n_segs + 1)
                pts_xy = [(float(part.interpolate(d).x), float(part.interpolate(d).y)) for d in ds]
            closed_loop = math.hypot(pts_xy[0][0] - pts_xy[-1][0], pts_xy[0][1] - pts_xy[-1][1]) < 0.05

            rod_nodes: List[int] = []
            for k, xy in enumerate(pts_xy):
                is_end = (k == 0 or k == len(pts_xy) - 1) and not closed_loop
                # Junction endpoints are pinned if rod is fixed, or softly mobile (inv_mass=0.35) if interior
                nid = _get_node(xy, fixed=rod_fixed, junction=is_end, ext_flag=is_ext)
                if not is_end and not rod_fixed:
                    inv_mass_list[nid] = 1.0
                rod_nodes.append(nid)
            if closed_loop and len(rod_nodes) > 2:
                rod_nodes[-1] = rod_nodes[0]

            rods.append(
                {
                    "nodes": rod_nodes,
                    "pair": (fa, fb),
                    "is_exterior": is_ext,
                    "is_whimsy": is_wh,
                    "fixed": rod_fixed,
                    "init_length": length,
                    "closed": closed_loop,
                }
            )

    # Pin EVERY node lying on fixed whimsy boundaries, and on the outer boundary when bounded!
    outline_ring = outline.exterior
    whimsy_rings = [cells[i].exterior for i in whimsy_indices]
    for idx in range(len(pos_list)):
        pt = Point(pos_list[idx][0], pos_list[idx][1])
        if (not borderless and outline_ring.distance(pt) < 0.15) or any(wr.distance(pt) < 0.15 for wr in whimsy_rings):
            inv_mass_list[idx] = 0.0
        elif is_junction_list[idx] and inv_mass_list[idx] > 0.0:
            inv_mass_list[idx] = 0.45 if (borderless and is_exterior_node[idx]) else 0.35

    # For bounded rods attached to a rigid wall/whimsy, pin the 1st interior collar node slightly
    # (low mobility 0.15) so rods enter rigid walls cleanly without crossing neighboring wall segments!
    if not borderless:
        for rod in rods:
            if rod["fixed"] or len(rod["nodes"]) < 4:
                continue
            nds = rod["nodes"]
            if inv_mass_list[nds[0]] == 0.0 and inv_mass_list[nds[1]] > 0.0:
                inv_mass_list[nds[1]] = 0.08
            if inv_mass_list[nds[-1]] == 0.0 and inv_mass_list[nds[-2]] > 0.0:
                inv_mass_list[nds[-2]] = 0.08

    return {
        "pos": np.asarray(pos_list, dtype=np.float64),
        "inv_mass": np.asarray(inv_mass_list, dtype=np.float64),
        "is_junction": np.asarray(is_junction_list, dtype=bool),
        "is_exterior_node": np.asarray(is_exterior_node, dtype=bool),
        "rods": rods,
    }


def _subdivide_rods_and_build_arrays(
    pos: np.ndarray,
    inv_mass: np.ndarray,
    rods: List[Dict[str, Any]],
    seg_L0_by_rod: List[List[float]],
    theta_by_rod: List[List[float]],
    seg_max_mm: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """Adaptively split any active segment whose rest length exceeds `seg_max_mm`."""
    pos_list = pos.tolist()
    im_list = inv_mass.tolist()
    for r_i, rod in enumerate(rods):
        if rod["fixed"]:
            continue
        nodes = rod["nodes"]
        L0s = seg_L0_by_rod[r_i]
        ths = theta_by_rod[r_i]
        new_nodes = [nodes[0]]
        new_L0s: List[float] = []
        new_ths: List[float] = []
        for s_i in range(len(nodes) - 1):
            u, v = nodes[s_i], nodes[s_i + 1]
            L0 = L0s[s_i]
            th = ths[s_i]
            if L0 > seg_max_mm:
                mx = 0.5 * (pos_list[u][0] + pos_list[v][0])
                my = 0.5 * (pos_list[u][1] + pos_list[v][1])
                mid_id = len(pos_list)
                pos_list.append([mx, my])
                im_list.append(1.0)
                new_nodes.extend((mid_id, v))
                new_L0s.extend((0.5 * L0, 0.5 * L0))
                new_ths.extend((th, th))
            else:
                new_nodes.append(v)
                new_L0s.append(L0)
                new_ths.append(th)
        rod["nodes"] = new_nodes
        seg_L0_by_rod[r_i] = new_L0s
        theta_by_rod[r_i] = new_ths
    return np.asarray(pos_list, dtype=np.float64), np.asarray(im_list, dtype=np.float64)


def _pack_rod_arrays(
    rods: List[Dict[str, Any]],
    seg_L0_by_rod: List[List[float]],
    theta_by_rod: List[List[float]],
    bend_k0_by_rod: List[List[float]],
):
    seg_u, seg_v, seg_rod, seg_idx, seg_L0, theta, inv_mq = [], [], [], [], [], [], []
    bend_e0, bend_e1, bend_k0 = [], [], []
    for r_i, rod in enumerate(rods):
        nodes = rod["nodes"]
        L0s = seg_L0_by_rod[r_i]
        ths = theta_by_rod[r_i]
        k0s = bend_k0_by_rod[r_i]
        first_e = len(seg_u)
        n_s = len(nodes) - 1
        for s_i in range(n_s):
            seg_u.append(nodes[s_i])
            seg_v.append(nodes[s_i + 1])
            seg_rod.append(r_i)
            seg_idx.append(s_i)
            seg_L0.append(L0s[s_i])
            theta.append(ths[s_i])
            inv_mq.append(0.0 if rod["fixed"] else 1.0)
        for s_i in range(n_s - 1):
            bend_e0.append(first_e + s_i)
            bend_e1.append(first_e + s_i + 1)
            bend_k0.append(k0s[s_i] if s_i < len(k0s) else 0.0)
    return (
        np.asarray(seg_u, dtype=np.int32),
        np.asarray(seg_v, dtype=np.int32),
        np.asarray(seg_rod, dtype=np.int32),
        np.asarray(seg_idx, dtype=np.int32),
        np.asarray(seg_L0, dtype=np.float64),
        np.asarray(theta, dtype=np.float64),
        np.asarray(inv_mq, dtype=np.float64),
        np.asarray(bend_e0, dtype=np.int32),
        np.asarray(bend_e1, dtype=np.int32),
        np.asarray(bend_k0, dtype=np.float64),
    )


def _snapped_rod_clearance(
    pos: np.ndarray,
    rods: Sequence[Dict[str, Any]],
    inv_mass: np.ndarray,
    search_radius_mm: float,
) -> Optional[float]:
    """Minimum settled centerline spacing among collision-eligible segments, in mm.

    Measure before noding and merging, while the rod and junction identities still
    exist. Positive spacing and an unintended contact (zero) are both meaningful.
    A board with no eligible pair has no minimum.
    """
    snapped = np.round(pos / ROD_SNAP_MM) * ROD_SNAP_MM
    endpoints = []
    owners = []
    local_indices = []
    loop_sizes = np.zeros(len(rods), dtype=np.intp)
    for rod_id, rod in enumerate(rods):
        nodes = np.asarray(rod["nodes"], dtype=np.intp)
        if len(nodes) < 2 or LineString(snapped[nodes]).length < MIN_ROD_LENGTH_MM:
            continue  # Same short-chain filter as polygonization.
        edges = np.column_stack((nodes[:-1], nodes[1:]))
        edges = edges[np.any(snapped[edges[:, 0]] != snapped[edges[:, 1]], axis=1)]
        endpoints.extend(edges)
        owners.extend([rod_id] * len(edges))
        local_indices.extend(range(len(edges)))
        if nodes[0] == nodes[-1]:
            loop_sizes[rod_id] = len(edges)

    if len(endpoints) < 2:
        return None
    start, end = np.asarray(endpoints).T
    fixed = (inv_mass[start] == 0) & (inv_mass[end] == 0)
    if fixed.all():
        return None
    collar = (inv_mass[start] < 0.2) | (inv_mass[end] < 0.2)
    owner = np.asarray(owners)
    index = np.asarray(local_indices)
    segments = shapely.linestrings(snapped[np.asarray(endpoints)])
    tree = shapely.STRtree(segments)
    diameter = math.hypot(*np.ptp(snapped, axis=0))
    radius = min(max(ROD_SNAP_MM, search_radius_mm), diameter)

    # Once any eligible pair lies inside the radius, more distant pairs cannot
    # lower the minimum. Batching bounds the temporary pair arrays on large boards.
    while True:
        minimum = math.inf
        for first in range(0, len(segments), 256):
            a, b = tree.query(segments[first:first + 256], predicate="dwithin", distance=radius)
            a += first
            gap = np.abs(index[a] - index[b])
            loop = loop_sizes[owner[a]]
            near = (gap <= 2) | ((loop > 0) & (loop - gap <= 2))
            eligible = (
                (a < b) & ((owner[a] != owner[b]) | ~near)
                & (start[a] != start[b]) & (start[a] != end[b])
                & (end[a] != start[b]) & (end[a] != end[b])
                & ~(fixed[a] & collar[b]) & ~(fixed[b] & collar[a])
            )
            if eligible.any():
                distances = shapely.distance(segments[a[eligible]], segments[b[eligible]])
                minimum = min(minimum, float(np.min(distances)))
                if minimum == 0:
                    return 0.0
        if math.isfinite(minimum):
            return minimum
        if radius >= diameter:
            return None
        radius = min(radius * 2, diameter)


def _rods_to_geometry(
    pos: np.ndarray,
    rods: List[Dict[str, Any]],
    expected_pieces: int,
    snap_mm: float = ROD_SNAP_MM,
) -> Optional[Tuple[List[Polygon], List[LineString], List[LineString]]]:
    """Convert evolved rod chains into noded interior/exterior LineStrings and closed Polygons."""
    int_raw: List[LineString] = []
    ext_raw: List[LineString] = []
    for rod in rods:
        pts = np.round(pos[rod["nodes"]] / snap_mm) * snap_mm
        if len(pts) < 2:
            continue
        ls = LineString(pts)
        if ls.length < MIN_ROD_LENGTH_MM:
            continue
        if rod["is_exterior"]:
            ext_raw.append(ls)
        else:
            int_raw.append(ls)

    all_lines = [*int_raw, *ext_raw]
    noded = union_all(all_lines, grid_size=snap_mm)
    faces = [orient(p, 1.0) for p in polygonize(noded) if p.is_valid and p.area > 15.0]
    if len(faces) != expected_pieces:
        return None
    # Verify zero self-crossing created extra slivers or holes
    if any(len(f.interiors) > 0 for f in faces):
        return None

    # Keep exact single-pass interior and exterior line collections
    ext_merged = [l for l in get_parts(linemerge(union_all(ext_raw, grid_size=snap_mm))) if l.geom_type == "LineString" and l.length > 1e-4]
    int_merged = [l for l in get_parts(linemerge(union_all(int_raw, grid_size=snap_mm))) if l.geom_type == "LineString" and l.length > 1e-4]
    return faces, int_merged, ext_merged


def run_rod_growth(
    outline: Polygon,
    config: RodConfig,
    whimsies: Sequence[WhimsyPlacement] = (),
) -> RodResult:
    """Run 2D Position-Based Cosserat Rod growth for Maze (borderless/bounded) or Wave puzzles."""
    t0 = time.perf_counter()
    rng = np.random.default_rng(config.seed)
    style = config.style.lower().strip()
    borderless = bool(config.borderless and style == "maze")

    cells, whimsy_indices = _initial_voronoi_cells(outline, config.num_pieces, whimsies, style, rng)
    expected_pieces = len(cells)
    seg_init = max(config.seg_init_mm, 3.2) if not borderless else config.seg_init_mm
    seg_max = max(config.seg_max_mm, 3.8) if not borderless else config.seg_max_mm
    graph = _build_rod_graph_from_cells(cells, whimsy_indices, outline, borderless=borderless, seg_init_mm=seg_init)
    pos = graph["pos"]
    inv_mass = graph["inv_mass"]
    rods = graph["rods"]
    if not config.junction_mobile:
        inv_mass[graph["is_junction"]] = 0.0

    # Initialize per-segment rest lengths and Cosserat orientations
    seg_L0_by_rod: List[List[float]] = []
    theta_by_rod: List[List[float]] = []
    bend_k0_by_rod: List[List[float]] = []
    for rod in rods:
        nodes = rod["nodes"]
        L0s, ths = [], []
        for u, v in zip(nodes[:-1], nodes[1:]):
            dx, dy = pos[v, 0] - pos[u, 0], pos[v, 1] - pos[u, 1]
            L0s.append(max(0.2, math.hypot(dx, dy)))
            ths.append(math.atan2(dy, dx))
        seg_L0_by_rod.append(L0s)
        theta_by_rod.append(ths)
        bend_k0_by_rod.append([0.0] * max(0, len(L0s) - 1))

    # Minimum clearance between non-adjacent cuts guarantees wood width >= min_neck + kerf!
    d_col = config.min_neck_mm + config.kerf_mm + (0.75 if not borderless else config.collision_margin_mm)
    x0, y0, x1, y1 = outline.bounds
    cx_b, cy_b = 0.5 * (x0 + x1), 0.5 * (y0 + y1)

    # Seed vortex centers for Wave style (divergence-free curl field)
    vortices = []
    if style == "wave":
        for vi in range(config.vortex_count):
            vx = float(rng.uniform(x0 + 0.15 * (x1 - x0), x1 - 0.15 * (x1 - x0)))
            vy = float(rng.uniform(y0 + 0.15 * (y1 - y0), y1 - 0.15 * (y1 - y0)))
            sign = 1.0 if vi % 2 == 0 else -1.0
            sigma = 0.24 * min(x1 - x0, y1 - y0)
            vortices.append((vx, vy, sign * config.vortex_strength, sigma))

    # Initial multi-lobe sinusoidal lateral kick on active rods to trigger deep interlocking Euler buckling
    for r_i, rod in enumerate(rods):
        if rod["fixed"]:
            continue
        nodes = rod["nodes"]
        if len(nodes) <= 2:
            continue
        u0, u1 = nodes[0], nodes[-1]
        dx, dy = pos[u1, 0] - pos[u0, 0], pos[u1, 1] - pos[u0, 1]
        L = math.hypot(dx, dy) + 1e-9
        nx, ny = -dy / L, dx / L
        sgn = 1.0 if (r_i % 2 == 0) else -1.0
        if style == "wave":
            # S-wave antisymmetric curl seed so every interior edge winds into an interlocking spiral crest!
            amp = min(2.2, 0.12 * L) * sgn
            for k in range(1, len(nodes) - 1):
                nid = nodes[k]
                if inv_mass[nid] > 0.0:
                    s = k / (len(nodes) - 1)
                    # Squared sine envelope keeps rod entry angles wide and wood-strong at boundaries/junctions
                    bump = amp * math.sin(2.0 * math.pi * s) * (math.sin(math.pi * s) ** 2)
                    pos[nid, 0] += nx * bump
                    pos[nid, 1] += ny * bump
        else:
            freq = 2.0 if L >= 16.0 else 1.0
            amp = min(2.0, 0.12 * L) * (1.0 if rng.random() < 0.5 else -1.0)
            for k in range(1, len(nodes) - 1):
                nid = nodes[k]
                if inv_mass[nid] > 0.0:
                    s = k / (len(nodes) - 1)
                    bump = amp * math.sin(math.pi * s * freq)
                    pos[nid, 0] += nx * bump
                    pos[nid, 1] += ny * bump

    # Keep last verified crossing-free & fabrication-safe snapshot
    best_snapshot = (pos.copy(), [dict(r, nodes=list(r["nodes"])) for r in rods], math.inf)
    # Track which mobile nodes are directly adjacent to a pinned boundary node so bounded wall repulsion doesn't pinch corners
    near_pinned = set()
    for rod in rods:
        nds = rod["nodes"]
        if len(nds) >= 3:
            if inv_mass[nds[0]] == 0.0:
                near_pinned.add(nds[1])
            if inv_mass[nds[-1]] == 0.0:
                near_pinned.add(nds[-2])
    inner_outline = outline.buffer(-0.22 * d_col) if not borderless else None
    if inner_outline is not None and inner_outline.is_empty:
        inner_outline = outline
    whimsy_polys = [w.polygon for w in whimsies]

    for step in range(config.growth_steps):
        # 1. Grow rest lengths of active rods up to max_length_factor
        for r_i, rod in enumerate(rods):
            if rod["fixed"]:
                continue
            cur_len = sum(seg_L0_by_rod[r_i])
            cap_factor = (1.75 if style == "wave" else config.max_length_factor) * (0.85 if rod["is_exterior"] else 1.0)
            if cur_len < rod["init_length"] * cap_factor:
                rate = (0.011 if style == "wave" else config.growth_rate) * (0.80 if rod["is_exterior"] else 1.0)
                seg_L0_by_rod[r_i] = [L0 * (1.0 + rate) for L0 in seg_L0_by_rod[r_i]]

        # 2. Adaptive subdivision for smooth meandering curves
        pos, inv_mass = _subdivide_rods_and_build_arrays(
            pos, inv_mass, rods, seg_L0_by_rod, theta_by_rod, seg_max
        )
        # Update rest curvature arrays:
        # - Wave creates strong alternating S-spiral crests (like Nervous System Wave)
        # - Maze creates deep alternating undercut meanders (like Nervous System Maze)
        for r_i, rod in enumerate(rods):
            n_b = max(0, len(seg_L0_by_rod[r_i]) - 1)
            if not rod["fixed"] and n_b > 0:
                sgn = 1.0 if (r_i % 2 == 0) else -1.0
                if style == "wave":
                    bend_k0_by_rod[r_i] = [
                        sgn * 0.52 * math.sin(2.0 * math.pi * (bi + 0.5) / max(1, n_b)) * math.sin(math.pi * (bi + 0.5) / max(1, n_b))
                        for bi in range(n_b)
                    ]
                else:
                    bend_k0_by_rod[r_i] = [
                        sgn * 0.34 * math.sin(3.0 * math.pi * (bi + 0.5) / max(1, n_b))
                        for bi in range(n_b)
                    ]
            else:
                bend_k0_by_rod[r_i] = [0.0] * n_b

        # 3. External forcing: Wave local spiral curl + global vortices OR Maze outward tendrils
        if style == "wave":
            # Local S-curl swirling around each active rod's midpoint builds true spiral hooks
            for r_i, rod in enumerate(rods):
                if rod["fixed"] or len(rod["nodes"]) < 4:
                    continue
                nds = rod["nodes"]
                p_mid = pos[nds[len(nds) // 2]]
                sgn = 1.0 if (r_i % 2 == 0) else -1.0
                for k in range(1, len(nds) - 1):
                    nid = nds[k]
                    if inv_mass[nid] <= 0.5:
                        continue
                    rx = pos[nid, 0] - p_mid[0]
                    ry = pos[nid, 1] - p_mid[1]
                    r_dist = math.hypot(rx, ry) + 2.5
                    env = math.sin(math.pi * k / (len(nds) - 1)) ** 2.0
                    swirl = 0.32 * sgn * env * math.exp(-r_dist / 16.0)
                    pos[nid, 0] += max(-0.35, min(0.35, -ry * swirl / r_dist * 3.4))
                    pos[nid, 1] += max(-0.35, min(0.35, rx * swirl / r_dist * 3.4))
            for i in range(len(pos)):
                if inv_mass[i] <= 0.5:
                    continue
                vx_tot, vy_tot = 0.0, 0.0
                px, py = pos[i, 0], pos[i, 1]
                for cx_v, cy_v, strength, sig in vortices:
                    rx, ry = px - cx_v, py - cy_v
                    r2 = rx * rx + ry * ry
                    w = strength * math.exp(-r2 / (2.0 * sig * sig)) / (math.sqrt(r2) + 12.0)
                    vx_tot += -ry * w
                    vy_tot += rx * w
                pos[i, 0] += max(-0.28, min(0.28, vx_tot))
                pos[i, 1] += max(-0.28, min(0.28, vy_tot))
        elif borderless:
            # Organic outward radial meandering on borderless exterior rods (matches Maze reference photo!)
            for r_i, rod in enumerate(rods):
                if rod["is_exterior"] and not rod["fixed"]:
                    nds = rod["nodes"]
                    for k in range(1, len(nds) - 1):
                        nid = nds[k]
                        dx, dy = pos[nid, 0] - cx_b, pos[nid, 1] - cy_b
                        dist = math.hypot(dx, dy) + 1e-6
                        wave_f = 0.14 * (0.4 + 0.6 * math.sin(math.pi * k / max(1, len(nds) - 1)))
                        pos[nid, 0] += wave_f * dx / dist
                        pos[nid, 1] += wave_f * dy / dist

        # 4. Pack arrays & run Numba 2D Cosserat PBD + spatial-hash collision
        arrays = _pack_rod_arrays(rods, seg_L0_by_rod, theta_by_rod, bend_k0_by_rod)
        seg_u, seg_v, seg_rod, seg_idx, seg_L0, theta, inv_mq, b_e0, b_e1, b_k0 = arrays
        min_clear = _pbd_cosserat_step_numba(
            pos,
            inv_mass,
            seg_u,
            seg_v,
            seg_rod,
            seg_idx,
            seg_L0,
            theta,
            inv_mq,
            b_e0,
            b_e1,
            b_k0,
            config.k_stretch,
            config.k_shear,
            config.k_bend,
            d_col,
            0.55 if not borderless else 0.65,
            0.22 * min(seg_init, d_col) if not borderless else 0.35 * min(seg_init, d_col),
            config.pbd_substeps,
        )

        # Write updated Cosserat edge angles back
        cursor = 0
        for r_i, rod in enumerate(rods):
            n_s = len(rod["nodes"]) - 1
            theta_by_rod[r_i] = theta[cursor : cursor + n_s].tolist()
            cursor += n_s

        # 5. Enforce reflective boundary constraints (outline for bounded mode + whimsies always)
        if not borderless and inner_outline is not None:
            for i in range(len(pos)):
                if inv_mass[i] <= 0.0:
                    continue
                pt = Point(float(pos[i, 0]), float(pos[i, 1]))
                bound_poly = outline if i in near_pinned else inner_outline
                if not bound_poly.contains(pt):
                    proj = bound_poly.exterior.interpolate(bound_poly.exterior.project(pt))
                    pos[i, 0], pos[i, 1] = proj.x, proj.y

        for wp in whimsy_polys:
            buf_w = wp.buffer(0.65 * d_col)
            for i in range(len(pos)):
                if inv_mass[i] <= 0.0:
                    continue
                pt = Point(float(pos[i, 0]), float(pos[i, 1]))
                if buf_w.contains(pt):
                    proj = buf_w.exterior.interpolate(buf_w.exterior.project(pt))
                    pos[i, 0], pos[i, 1] = proj.x, proj.y

        # Periodically validate topological planarity & neck safety; keep latest valid state
        if (step + 1) % 4 == 0 or step == config.growth_steps - 1:
            geom_check = _rods_to_geometry(pos, rods, expected_pieces)
            if geom_check is not None:
                faces_chk, _, _ = geom_check
                ok_neck = True
                for f in faces_chk:
                    er = f.buffer(-0.5 * (config.min_neck_mm + config.kerf_mm))
                    if er.is_empty:
                        ok_neck = False
                        break
                    parts_er = [p for p in get_parts(er) if p.geom_type == "Polygon" and p.area >= 25.0]
                    if len(parts_er) > 1:
                        ok_neck = False
                        break
                    # Also ensure kerf erosion keeps 1 polygon
                    ek = f.buffer(-0.125)
                    if ek.is_empty or ek.geom_type != "Polygon":
                        ok_neck = False
                        break
                if ok_neck:
                    best_snapshot = (pos.copy(), [dict(r, nodes=list(r["nodes"])) for r in rods], min_clear)

    # Final smoothing + geometry extraction from best verified snapshot
    best_pos, best_rods, best_clear = best_snapshot
    # 1 gentle Chaikin/Laplacian polish pass on non-junction mobile nodes if topology & necks stay clean
    polished_pos = best_pos.copy()
    for rod in best_rods:
        if rod["fixed"] or len(rod["nodes"]) < 4:
            continue
        nds = rod["nodes"]
        for k in range(1, len(nds) - 1):
            u, mid, v = nds[k - 1], nds[k], nds[k + 1]
            polished_pos[mid] = 0.20 * best_pos[u] + 0.60 * best_pos[mid] + 0.20 * best_pos[v]

    geom = _rods_to_geometry(polished_pos, best_rods, expected_pieces)
    if geom is None:
        geom = _rods_to_geometry(best_pos, best_rods, expected_pieces)
        polished_pos = best_pos
    if geom is None:
        raise RuntimeError("Cosserat rod solver could not extract valid non-crossing polygons")

    faces, interior_lines, exterior_lines = geom
    clearance = _snapped_rod_clearance(polished_pos, best_rods, inv_mass, d_col)
    n_segs_total = sum(max(0, len(r["nodes"]) - 1) for r in best_rods)
    elapsed = time.perf_counter() - t0
    return RodResult(
        faces=faces,
        interior_lines=interior_lines,
        exterior_lines=exterior_lines,
        whimsy_indices=whimsy_indices,
        expected_outline=None if borderless else outline,
        num_nodes=len(polished_pos),
        num_segments=n_segs_total,
        min_nonadjacent_clearance_mm=round(clearance, 3) if clearance is not None else None,
        last_collision_sweep_min_mm=round(best_clear, 3) if math.isfinite(best_clear) else None,
        elapsed_sec=elapsed,
        config=config,
    )
