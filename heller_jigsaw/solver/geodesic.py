"""Independent PyTorch + Exact Adjoint Dijkstra shared-metric geodesic Voronoi solver.

Implements Dossier Sections 2.2, 4.5, and 4.6:
  D = physical target outline (mm)
  s(x) > 0 = single shared positive scalar slowness field
  q_i = seed inside D for piece i
  |grad T_i(x)| = s(x), T_i(q_i) = 0  (8-connected metric weighted-grid Dijkstra)
  R_i = { x in D : T_i(x) <= T_j(x) for all j }

Key capabilities:
1. Exact analytical adjoint sensitivities dL / ds(x) along each seed's Dijkstra shortest-path
   tree, accelerated via Numba JIT (with automatic pure-NumPy/Python fallback).
2. Multi-restart, aspect- and exterior-boundary-aware layout assignment followed by fast
   simulated-annealing Escher-style SDF-tiling blueprint packing inside D.
3. Log-space multi-scale slowness pyramid with scale-invariant softmax temperature annealing,
   contour/limb/concavity saliency weighting (Dossier 4.5 protected-feature loss), and
   optional differentiable Model A silhouette recognizer/embedding guidance.
4. High-resolution topology- and neck-safe extraction through the authoritative planar
   subdivision kernel with smooth SDF-guided seam polish.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import shapely
import torch
import torch.nn.functional as F
from scipy import ndimage
from scipy.optimize import linear_sum_assignment
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import Polygon

from ..data.corpus import MotifAsset
from ..geometry.outline import grid_shape, inside_mask
from ..geometry.partition import PlanarPartition, extract_partition_from_labels
from ..geometry.validate import ValidationConfig, validate_partition

try:
    from numba import njit

    @njit(cache=True)
    def _adjoint_numba(
        dist: np.ndarray,
        pred: np.ndarray,
        grad_dist: np.ndarray,
        ys: np.ndarray,
        xs: np.ndarray,
    ) -> np.ndarray:
        n_seeds, num_nodes = dist.shape
        grad_s = np.zeros(num_nodes, dtype=np.float64)
        sqrt2 = math.sqrt(2.0)
        for i in range(n_seeds):
            order = np.argsort(dist[i])
            t_bar = grad_dist[i].copy()
            for k in range(num_nodes - 1, -1, -1):
                v = order[k]
                p = pred[i, v]
                if p < 0:
                    continue
                g = t_bar[v]
                if g == 0.0:
                    continue
                step_len = sqrt2 if (ys[v] != ys[p] and xs[v] != xs[p]) else 1.0
                hw = 0.5 * step_len * g
                grad_s[v] += hw
                grad_s[p] += hw
                t_bar[p] += g
        return grad_s

    _HAS_NUMBA = True
except Exception:  # pragma: no cover - optional acceleration fallback
    _adjoint_numba = None
    _HAS_NUMBA = False


@dataclass
class GeodesicSolverConfig:
    """Configuration for shared-metric geodesic Voronoi puzzle generation."""

    opt_grid_max_dim: int = 112      # optimization grid resolution on longest side
    final_grid_max_dim: int = 240    # final extraction grid resolution (~1.0 mm/px at 240 mm)
    n_steps: int = 110               # Adam steps for slowness optimization
    lr: float = 0.18                 # Adam learning rate
    s_min: float = 0.04              # minimum slowness (fast waveguide inside motif features)
    s_max: float = 24.0              # maximum slowness (slow barrier along silhouette contours)
    tau_start: float = 0.35          # initial softmax temperature as fraction of eq_radius_px (upgraded mode)
    tau_end: float = 0.022           # final softmax temperature as fraction of eq_radius_px (upgraded mode)
    w_iou: float = 4.6               # aligned soft IoU weight
    w_sdf: float = 4.0               # aligned SDF contour matching weight
    w_area: float = 0.85             # relative area balance weight
    w_neck: float = 0.35             # soft erosion neck fragility weight
    w_tv: float = 0.010              # slowness smoothness regularization
    w_interlock: float = 0.12        # boundary curvature / interlocking bonus
    w_sem: float = 0.0               # Model A semantic/embedding guidance weight (auto-enabled if recognizer given)
    sem_interval: int = 4            # evaluate differentiable Model A every k steps (after warmup)
    recognizer_checkpoint: Optional[str] = None  # optional path to Model A model_a_recognizer.pt
    device: str = "cpu"              # "cpu" (default, avoids GPU contention), "cuda", or "auto"
    solver_mode: str = "upgraded"    # "upgraded" (Escher packing + log-space + saliency) or "baseline"
    n_restarts: int = 8              # multi-restart Lloyd candidates for blueprint initialization
    pack_iters: int = -1             # Escher SDF packing SA iterations (-1 = auto by piece count, 0 = off)
    allow_flip: bool = True          # allow horizontal reflection when aligning silhouettes
    kerf_mm: float = 0.15
    min_neck_mm: float = 3.0
    smooth_iters: int = 2
    simplify_tol_mm: float = 0.18
    seed: int = 42


@dataclass
class PiecePose:
    """2D similarity + reflection pose placing a 256x256 motif into grid pixel coords."""

    cx: float
    cy: float
    angle_deg: float
    scale: float
    flip_x: bool = False


@dataclass
class GeodesicSolution:
    """Output of the shared-metric geodesic Voronoi solver."""

    partition: PlanarPartition
    slowness_grid: np.ndarray        # (H_final, W_final) positive shared slowness field
    labels_inside: np.ndarray        # (H_final, W_final) int32 piece IDs (-1 outside)
    seeds_mm: List[Tuple[float, float]]
    poses: List[PiecePose]
    per_piece_iou: List[float]
    per_piece_chamfer: List[float]
    loss_history: List[float]
    elapsed_sec: float
    logs: List[str] = field(default_factory=list)
    sem_loss_history: List[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exact 8-connected metric grid graph & exact shortest-path tree adjoint
# ---------------------------------------------------------------------------

class GridGraph:
    """8-connected weighted grid graph over active pixels of a domain."""

    def __init__(self, active_mask: np.ndarray):
        self.mask = np.asarray(active_mask, dtype=bool)
        self.h, self.w = self.mask.shape
        ys, xs = np.where(self.mask)
        self.num_nodes = len(ys)
        self.ys = ys.astype(np.int32)
        self.xs = xs.astype(np.int32)
        self.coord_to_node = np.full((self.h, self.w), -1, dtype=np.int32)
        self.coord_to_node[ys, xs] = np.arange(self.num_nodes, dtype=np.int32)

        u_list: List[np.ndarray] = []
        v_list: List[np.ndarray] = []
        len_list: List[np.ndarray] = []

        offsets = [
            (0, 1, 1.0),
            (1, 0, 1.0),
            (1, 1, math.sqrt(2.0)),
            (1, -1, math.sqrt(2.0)),
        ]
        for dr, dc, step_len in offsets:
            r0 = max(0, -dr)
            r1 = min(self.h, self.h - dr)
            c0 = max(0, -dc)
            c1 = min(self.w, self.w - dc)
            a = self.coord_to_node[r0:r1, c0:c1]
            b = self.coord_to_node[r0 + dr : r1 + dr, c0 + dc : c1 + dc]
            valid = (a >= 0) & (b >= 0)
            ua = a[valid]
            vb = b[valid]
            if len(ua) == 0:
                continue
            lens = np.full(len(ua), step_len, dtype=np.float64)
            u_list.append(ua)
            v_list.append(vb)
            len_list.append(lens)
            u_list.append(vb)
            v_list.append(ua)
            len_list.append(lens)

        self.edge_u = np.concatenate(u_list).astype(np.int32)
        self.edge_v = np.concatenate(v_list).astype(np.int32)
        self.edge_len = np.concatenate(len_list).astype(np.float64)

        dummy = csr_matrix(
            (np.ones(len(self.edge_u), dtype=np.float64), (self.edge_u, self.edge_v)),
            shape=(self.num_nodes, self.num_nodes),
        )
        dummy.sum_duplicates()
        coo = dummy.tocoo()
        self.csr_indptr = dummy.indptr
        self.csr_indices = dummy.indices
        self.csr_u = coo.row.astype(np.int32)
        self.csr_v = coo.col.astype(np.int32)
        dy = np.abs(self.ys[self.csr_u] - self.ys[self.csr_v])
        dx = np.abs(self.xs[self.csr_u] - self.xs[self.csr_v])
        self.csr_len = np.where((dx + dy) > 1, math.sqrt(2.0), 1.0).astype(np.float64)
        self._ys_list = self.ys.tolist()
        self._xs_list = self.xs.tolist()

    def build_csr(self, slowness_vec: np.ndarray) -> csr_matrix:
        s = np.asarray(slowness_vec, dtype=np.float64)
        weights = 0.5 * (s[self.csr_u] + s[self.csr_v]) * self.csr_len
        return csr_matrix(
            (weights, self.csr_indices, self.csr_indptr),
            shape=(self.num_nodes, self.num_nodes),
            copy=False,
        )

    def shortest_paths(
        self, slowness_vec: np.ndarray, seed_nodes: Sequence[int]
    ) -> Tuple[np.ndarray, np.ndarray]:
        csr = self.build_csr(slowness_vec)
        dist, pred = dijkstra(
            csr,
            directed=False,
            indices=np.asarray(seed_nodes, dtype=np.int32),
            return_predecessors=True,
        )
        return dist, pred

    def adjoint_slowness_grad(
        self,
        dist: np.ndarray,
        pred: np.ndarray,
        grad_dist: np.ndarray,
    ) -> np.ndarray:
        """Exact analytical adjoint gradient dL / ds_vec along Dijkstra shortest-path trees."""
        if _HAS_NUMBA and _adjoint_numba is not None:
            return _adjoint_numba(
                np.ascontiguousarray(dist, dtype=np.float64),
                np.ascontiguousarray(pred, dtype=np.int32),
                np.ascontiguousarray(grad_dist, dtype=np.float64),
                self.ys,
                self.xs,
            )

        n_seeds, num_nodes = dist.shape
        grad_s = [0.0] * num_nodes
        ys = self._ys_list
        xs = self._xs_list
        sqrt2 = math.sqrt(2.0)

        for i in range(n_seeds):
            d_i = dist[i]
            order = np.argsort(d_i)[::-1].tolist()
            p_i = pred[i].tolist()
            t_bar = grad_dist[i].astype(np.float64).tolist()
            for v in order:
                p = p_i[v]
                if p < 0:
                    continue
                g = t_bar[v]
                if g == 0.0:
                    continue
                step_len = sqrt2 if (ys[v] != ys[p] and xs[v] != xs[p]) else 1.0
                hw = 0.5 * step_len * g
                grad_s[v] += hw
                grad_s[p] += hw
                t_bar[p] += g

        return np.asarray(grad_s, dtype=np.float64)


class _GeodesicDijkstraFn(torch.autograd.Function):
    """Differentiable PyTorch wrapper around exact 8-connected weighted-grid Dijkstra."""

    @staticmethod
    def forward(ctx, s_vec: torch.Tensor, graph: GridGraph, seed_nodes: Tuple[int, ...]) -> torch.Tensor:
        s_np = s_vec.detach().cpu().numpy()
        dist, pred = graph.shortest_paths(s_np, seed_nodes)
        finite_max = float(np.max(dist[np.isfinite(dist)])) if np.any(np.isfinite(dist)) else 1e3
        dist = np.where(np.isfinite(dist), dist, finite_max * 2.0)
        ctx.graph = graph
        ctx.dist = dist
        ctx.pred = pred
        return torch.from_numpy(dist).to(s_vec)

    @staticmethod
    def backward(ctx, grad_output: torch.Tensor):
        grad_np = grad_output.detach().cpu().numpy()
        grad_s_np = ctx.graph.adjoint_slowness_grad(ctx.dist, ctx.pred, grad_np)
        grad_s = torch.from_numpy(grad_s_np).to(device=grad_output.device, dtype=grad_output.dtype)
        return grad_s, None, None


def geodesic_distances(
    s_vec: torch.Tensor, graph: GridGraph, seed_nodes: Sequence[int]
) -> torch.Tensor:
    """Differentiable geodesic distances `[N, M]` from `seed_nodes` under shared slowness `s_vec`."""
    return _GeodesicDijkstraFn.apply(s_vec, graph, tuple(int(q) for q in seed_nodes))


# ---------------------------------------------------------------------------
# Silhouette warping, contour saliency, & fast pose alignment
# ---------------------------------------------------------------------------

def warp_motif_to_grid(
    motif: MotifAsset,
    pose: PiecePose,
    h: int,
    w: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Warp a 256x256 motif mask & SDF into `(h, w)` domain coordinates using `pose`.

    Returns `(mask_f32 [h, w] in [0, 1], sdf_f32 [h, w] in grid pixels)`.
    """
    src_mask = motif.mask[:, ::-1].copy() if pose.flip_x else motif.mask
    src_sdf = motif.sdf[:, ::-1].copy() if pose.flip_x else motif.sdf

    s = pose.scale / 256.0
    ang = math.radians(pose.angle_deg)
    ca = math.cos(ang) * s
    sa = math.sin(ang) * s
    mc = 127.5
    tx = pose.cx - (ca * mc - sa * mc)
    ty = pose.cy - (sa * mc + ca * mc)
    M = np.array([[ca, -sa, tx], [sa, ca, ty]], dtype=np.float32)

    warped_mask = cv2.warpAffine(
        src_mask,
        M,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0.0,
    )
    warped_sdf = cv2.warpAffine(
        src_sdf * s,
        M,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=float(max(h, w)),
    )
    return np.clip(warped_mask, 0.0, 1.0), warped_sdf


def _warp_scalar_map(
    arr: np.ndarray,
    pose: PiecePose,
    h: int,
    w: int,
    border: float = 1.0,
) -> np.ndarray:
    src = arr[:, ::-1].copy() if pose.flip_x else arr
    s = pose.scale / 256.0
    ang = math.radians(pose.angle_deg)
    ca = math.cos(ang) * s
    sa = math.sin(ang) * s
    mc = 127.5
    tx = pose.cx - (ca * mc - sa * mc)
    ty = pose.cy - (sa * mc + ca * mc)
    M = np.array([[ca, -sa, tx], [sa, ca, ty]], dtype=np.float32)
    return cv2.warpAffine(
        src,
        M,
        (w, h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=float(border),
    )


def compute_motif_saliency(mask: np.ndarray) -> np.ndarray:
    """Automatic protected-feature saliency map (Dossier 4.5): boosts limbs, tips & concave bays."""
    fg = mask > 0.5
    if not np.any(fg):
        return np.ones_like(mask, dtype=np.float32)
    d_in = ndimage.distance_transform_edt(fg)
    d_out = ndimage.distance_transform_edt(~fg)
    d_bnd = np.minimum(d_in, d_out)
    bnd_band = np.exp(-0.5 * (d_bnd / 7.0) ** 2)
    max_r = max(1.0, float(d_in.max()))
    limb_score = fg.astype(np.float32) * np.clip(1.0 - d_in / (0.60 * max_r), 0.0, 1.0)
    closed = ndimage.binary_closing(fg, iterations=18)
    bay_score = ((closed & (~fg)).astype(np.float32)) * np.exp(-0.5 * (d_out / 14.0) ** 2)
    return (1.0 + 1.25 * bnd_band + 1.50 * limb_score + 2.00 * bay_score).astype(np.float32)


def _motif_solidity(mask: np.ndarray) -> float:
    fg = (mask > 0.5).astype(np.uint8)
    cnts, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return 0.7
    cnt = max(cnts, key=cv2.contourArea)
    hull_area = max(1.0, float(cv2.contourArea(cv2.convexHull(cnt))))
    return float(np.clip(cv2.contourArea(cnt) / hull_area, 0.15, 1.0))


def _aligned_iou_and_chamfer(
    cell_mask: np.ndarray,
    motif: MotifAsset,
    allow_flip: bool = True,
    n_angles: int = 36,
) -> Tuple[float, float, PiecePose]:
    """Find best similarity+flip alignment of `motif` to `cell_mask`, returning `(iou, chamfer_norm, pose)`."""
    h, w = cell_mask.shape
    cell_bin = cell_mask > 0.5
    cell_area = float(np.sum(cell_bin))
    if cell_area < 4.0:
        return 0.0, 1.0, PiecePose(w * 0.5, h * 0.5, 0.0, float(min(h, w)) * 0.4, False)

    ys, xs = np.where(cell_bin)
    cx = float(np.mean(xs))
    cy = float(np.mean(ys))
    motif_area_px = max(100.0, float(np.sum(motif.mask > 0.5)))
    base_scale = 256.0 * math.sqrt(cell_area / motif_area_px)

    my, mx = np.where(motif.mask > 0.5)
    if not len(mx):
        return 0.0, 1.0, PiecePose(cx, cy, 0.0, base_scale, False)
    mx_mean, my_mean = mx.mean(), my.mean()
    radius = np.hypot(mx - mx_mean, my - my_mean).max()
    reach = math.ceil((radius + 2) * base_scale * 1.14 / 256.0) + 4
    x0 = min(int(xs.min()), math.floor(cx) - reach)
    x1 = max(int(xs.max()), math.ceil(cx) + reach)
    y0 = min(int(ys.min()), math.floor(cy) - reach)
    y1 = max(int(ys.max()), math.ceil(cy) + reach)
    crop = np.zeros((y1 - y0 + 1, x1 - x0 + 1), dtype=bool)
    crop[ys - y0, xs - x0] = True
    ch, cw = crop.shape
    lcx, lcy = cx - x0, cy - y0
    best_iou = -1.0
    best_pose = PiecePose(lcx, lcy, 0.0, base_scale, False)

    flips = (False, True) if allow_flip else (False,)
    angles = np.linspace(0.0, 360.0, n_angles, endpoint=False)

    for flip in flips:
        src = motif.mask[:, ::-1].copy() if flip else motif.mask
        mx_offset = (127.5 - mx_mean) if flip else (mx_mean - 127.5)
        my_offset = my_mean - 127.5
        for scale_mult in (0.94, 1.04, 1.14):
            sc = base_scale * scale_mult
            s = sc / 256.0
            for ang_deg in angles:
                ang = math.radians(float(ang_deg))
                ca = math.cos(ang) * s
                sa = math.sin(ang) * s
                pcx = lcx - (ca * mx_offset - sa * my_offset)
                pcy = lcy - (sa * mx_offset + ca * my_offset)
                tx = pcx - (ca * 127.5 - sa * 127.5)
                ty = pcy - (sa * 127.5 + ca * 127.5)
                M = np.array([[ca, -sa, tx], [sa, ca, ty]], dtype=np.float32)
                wm = cv2.warpAffine(src, M, (cw, ch), flags=cv2.INTER_LINEAR, borderValue=0.0) > 0.5
                inter = float(np.sum(wm & crop))
                union = float(np.sum(wm | crop))
                iou = inter / union if union > 0 else 0.0
                if iou > best_iou:
                    best_iou = iou
                    best_pose = PiecePose(pcx, pcy, float(ang_deg), sc, flip)

    coarse_pose = best_pose
    for dx, dy in ((0, 0), (-3, 0), (3, 0), (0, -3), (0, 3), (-2, -2), (2, 2), (-2, 2), (2, -2)):
        for da in (-5.0, 0.0, 5.0):
            cand = PiecePose(
                coarse_pose.cx + dx,
                coarse_pose.cy + dy,
                (coarse_pose.angle_deg + da) % 360.0,
                coarse_pose.scale,
                coarse_pose.flip_x,
            )
            wm, _ = warp_motif_to_grid(motif, cand, ch, cw)
            wb = wm > 0.5
            inter = float(np.sum(wb & crop))
            union = float(np.sum(wb | crop))
            iou = inter / union if union > 0 else 0.0
            if iou > best_iou:
                best_iou = iou
                best_pose = cand

    wm, _ = warp_motif_to_grid(motif, best_pose, ch, cw)
    wb = wm > 0.5
    b_cell = crop ^ ndimage.binary_erosion(crop)
    b_motif = wb ^ ndimage.binary_erosion(wb)
    eq_r = max(1.0, math.sqrt(cell_area / math.pi))
    if b_motif.any():
        d1 = ndimage.distance_transform_edt(~b_motif)[b_cell].mean()
        d2 = ndimage.distance_transform_edt(~b_cell)[b_motif].mean()
        chamfer_norm = float((d1 + d2) / (2.0 * eq_r))
    else:
        chamfer_norm = 1.0
    grid_pose = PiecePose(
        best_pose.cx + x0,
        best_pose.cy + y0,
        best_pose.angle_deg,
        best_pose.scale,
        best_pose.flip_x,
    )
    return best_iou, chamfer_norm, grid_pose


# ---------------------------------------------------------------------------
# Phase 1: Lloyd seeding, exterior-aware assignment & Escher SDF blueprint packing
# ---------------------------------------------------------------------------

def _lloyd_seeds(inside: np.ndarray, n: int, seed: int = 42, iters: int = 18) -> np.ndarray:
    """Deterministic centroidal Voronoi seeds `(N, 2)` in `(x, y)` grid coordinates inside `inside`."""
    rng = np.random.default_rng(seed)
    ys, xs = np.where(inside)
    pts = np.stack([xs.astype(np.float64), ys.astype(np.float64)], axis=1)
    if len(pts) < n:
        raise ValueError(f"Domain grid has only {len(pts)} interior pixels for {n} pieces")

    centers = np.empty((n, 2), dtype=np.float64)
    idx0 = int(rng.integers(0, len(pts)))
    centers[0] = pts[idx0]
    min_d2 = np.sum((pts - centers[0]) ** 2, axis=1)
    for k in range(1, n):
        probs = min_d2 / max(float(np.sum(min_d2)), 1e-9)
        idx = int(rng.choice(len(pts), p=probs))
        centers[k] = pts[idx]
        d2 = np.sum((pts - centers[k]) ** 2, axis=1)
        min_d2 = np.minimum(min_d2, d2)

    for _ in range(iters):
        d2 = np.sum((pts[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        assign = np.argmin(d2, axis=1)
        for k in range(n):
            sel = pts[assign == k]
            if len(sel) > 0:
                centers[k] = np.mean(sel, axis=0)
        for k in range(n):
            sel = pts[assign == k]
            if len(sel) > 0:
                d_to_c = np.sum((sel - centers[k]) ** 2, axis=1)
                centers[k] = sel[int(np.argmin(d_to_c))]
    return centers


def _pole_of_inaccessibility(mask: np.ndarray) -> Tuple[float, float]:
    """Deepest interior pixel `(x, y)` of a binary mask."""
    if not np.any(mask):
        return (mask.shape[1] * 0.5, mask.shape[0] * 0.5)
    dist = ndimage.distance_transform_edt(mask)
    idx = int(np.argmax(dist))
    ry, rx = np.unravel_index(idx, mask.shape)
    return (float(rx), float(ry))


def _initialize_packed_layout_baseline(
    inside: np.ndarray,
    motifs: Sequence[MotifAsset],
    seed: int = 42,
    allow_flip: bool = True,
) -> Tuple[List[int], List[PiecePose], np.ndarray]:
    """Legacy single-restart Lloyd + aspect-ratio layout initializer (retained for ablations)."""
    h, w = inside.shape
    n = len(motifs)
    centers = _lloyd_seeds(inside, n, seed=seed)

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float64), np.arange(w, dtype=np.float64), indexing="ij")
    d2 = (xx[None, :, :] - centers[:, 0, None, None]) ** 2 + (yy[None, :, :] - centers[:, 1, None, None]) ** 2
    vor = np.argmin(d2, axis=0)
    vor[~inside] = -1

    slot_aspects = []
    for s_idx in range(n):
        sm = vor == s_idx
        ys, xs = np.where(sm)
        if len(xs) < 4:
            slot_aspects.append(1.0)
            continue
        pts = np.stack([xs - xs.mean(), ys - ys.mean()], axis=1).astype(np.float64)
        cov = np.cov(pts.T)
        evals = np.maximum(np.linalg.eigvalsh(cov), 1e-4)
        slot_aspects.append(float(math.sqrt(evals[1] / evals[0])))

    cost = np.zeros((n, n), dtype=np.float64)
    for m_idx, m in enumerate(motifs):
        ma = m.aspect_ratio
        for s_idx in range(n):
            cost[m_idx, s_idx] = abs(math.log(max(ma, 1.0) / max(slot_aspects[s_idx], 1.0)))

    row_ind, col_ind = linear_sum_assignment(cost)
    ordered_centers = np.zeros_like(centers)
    slot_masks: List[np.ndarray] = []
    for m_idx, s_idx in zip(row_ind, col_ind):
        ordered_centers[m_idx] = centers[s_idx]
        slot_masks.append(vor == s_idx)

    poses: List[PiecePose] = []
    for i, m in enumerate(motifs):
        _, _, pose = _aligned_iou_and_chamfer(
            slot_masks[i].astype(np.float32), m, allow_flip=allow_flip, n_angles=24
        )
        poses.append(pose)

    target_area = float(np.sum(inside)) / float(n)
    for _ in range(4):
        warped = [warp_motif_to_grid(motifs[i], poses[i], h, w)[0] for i in range(n)]
        occ = np.sum(np.stack(warped, axis=0), axis=0)
        for i in range(n):
            mi = warped[i]
            others = occ - mi
            overlap = mi * np.clip(others, 0.0, 2.0)
            if np.sum(overlap) > 1.0:
                oy, ox = np.where(overlap > 0.25)
                if len(ox) > 0:
                    dx = poses[i].cx - float(np.mean(ox))
                    dy = poses[i].cy - float(np.mean(oy))
                    norm = max(math.hypot(dx, dy), 1e-3)
                    poses[i].cx += 1.6 * dx / norm
                    poses[i].cy += 1.6 * dy / norm
            outside_leak = mi * (~inside).astype(np.float32)
            if np.sum(outside_leak) > 2.0:
                ly, lx = np.where(outside_leak > 0.25)
                if len(lx) > 0:
                    dx = poses[i].cx - float(np.mean(lx))
                    dy = poses[i].cy - float(np.mean(ly))
                    norm = max(math.hypot(dx, dy), 1e-3)
                    poses[i].cx += 1.5 * dx / norm
                    poses[i].cy += 1.5 * dy / norm

            unique_core = (mi > 0.5) & (others < 0.3) & inside
            if not np.any(unique_core):
                unique_core = (mi > 0.5) & inside
            if not np.any(unique_core):
                unique_core = slot_masks[i] & inside
            sx, sy = _pole_of_inaccessibility(unique_core)
            ordered_centers[i] = [sx, sy]

            m_area = max(100.0, float(np.sum(motifs[i].mask > 0.5)))
            ideal_scale = 256.0 * math.sqrt(target_area / m_area)
            poses[i].scale = float(np.clip(poses[i].scale, 0.85 * ideal_scale, 1.25 * ideal_scale))

    return list(range(n)), poses, ordered_centers


def _initialize_escher_packed_layout(
    outline: Polygon,
    inside_opt: np.ndarray,
    motifs: Sequence[MotifAsset],
    seed: int = 42,
    allow_flip: bool = True,
    n_restarts: int = 8,
    pack_iters: int = -1,
) -> Tuple[List[PiecePose], np.ndarray, np.ndarray, Dict[str, float]]:
    """Multi-restart exterior-aware assignment + Escher-style SDF-tiling SA blueprint packer.

    Directly optimizes the nearest-SDF tiling partition `R_sdf(x) = argmin_i target_sdf_i(x)`
    inside `outline` so motifs nestle with minimal voids, minimal overlaps, and smooth convex
    backs aligned against the outer outline while intricate limbs face interior carvable seams.
    """
    n = len(motifs)
    h_opt, w_opt = inside_opt.shape
    x0, y0, x1, y1 = outline.bounds
    span_mm = max(x1 - x0, y1 - y0)

    # Compact packing grid: 72px up to 12 pieces, gently growing up to 96px for 24+ pieces
    pack_dim = min(96, max(72, int(round(26.0 * math.sqrt(n)))))
    px_pack = span_mm / float(pack_dim)
    hp, wp = grid_shape(outline, px_pack)
    in_p = inside_mask(outline, hp, wp, px_pack)
    in_idx = np.where(in_p.ravel())[0]
    n_in = len(in_idx)
    if n_in < n:
        raise ValueError(f"Packing grid too coarse ({n_in} interior px for {n} pieces)")

    target_area_p = float(n_in) / float(n)
    eq_r_p = max(3.0, math.sqrt(target_area_p / math.pi))
    m_areas = [max(100.0, float(np.sum(m.mask > 0.5))) for m in motifs]
    ideal_scales_p = [256.0 * math.sqrt(target_area_p / a) for a in m_areas]
    m_solidities = [_motif_solidity(m.mask) for m in motifs]
    bnd_p = in_p ^ ndimage.binary_erosion(in_p)

    def eval_full(poses_list: Sequence[PiecePose]) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        masks_in = np.empty((n, n_in), dtype=bool)
        sdfs_in = np.empty((n, n_in), dtype=np.float32)
        leaks = np.empty(n, dtype=np.float64)
        epxs = np.empty(n, dtype=np.float64)
        for i, (m, p) in enumerate(zip(motifs, poses_list)):
            wm, ws = warp_motif_to_grid(m, p, hp, wp)
            mb_in = (wm > 0.5).ravel()[in_idx]
            masks_in[i] = mb_in
            sdfs_in[i] = ws.ravel()[in_idx]
            epx = m_areas[i] * ((p.scale / 256.0) ** 2)
            epxs[i] = epx
            leaks[i] = max(0.0, epx - float(mb_in.sum())) / max(epx, 1.0)
        return masks_in, sdfs_in, leaks, epxs

    arange_in = np.arange(n_in, dtype=np.int32)

    def score_arrays(
        masks_in: np.ndarray,
        sdfs_in: np.ndarray,
        leaks: np.ndarray,
        epxs: np.ndarray,
    ) -> Tuple[float, float, float, np.ndarray, np.ndarray]:
        lbl = np.argmin(sdfs_in, axis=0)
        ca = np.bincount(lbl, minlength=n).astype(np.float64)
        hit = masks_in[lbl, arange_in]
        inter = np.bincount(lbl, weights=hit, minlength=n)
        in_px = masks_in.sum(axis=1).astype(np.float64)
        leaked_px = np.maximum(0.0, epxs - in_px)
        union = ca + in_px - inter + leaked_px
        ious = inter / np.maximum(union, 1.0)
        area_err = float(np.mean(((ca - target_area_p) / target_area_p) ** 2))
        sc = float(ious.mean()) + 0.65 * float(ious.min()) - 0.30 * area_err - 0.22 * float(leaks.mean())
        return sc, float(ious.mean()), float(ious.min()), ious, lbl

    # Step 1: Multi-restart Lloyd + aspect-ratio & exterior-boundary-fraction Hungarian matching
    yy_p, xx_p = np.meshgrid(np.arange(hp, dtype=np.float64), np.arange(wp, dtype=np.float64), indexing="ij")
    restarts = max(1, int(n_restarts))
    best_init_sc = -1e9
    best_poses_p: Optional[List[PiecePose]] = None

    for r in range(restarts):
        rseed = seed + r * 131
        centers = _lloyd_seeds(in_p, n, seed=rseed, iters=16)
        d2 = (xx_p[None] - centers[:, 0, None, None]) ** 2 + (yy_p[None] - centers[:, 1, None, None]) ** 2
        vor = np.argmin(d2, axis=0)
        vor[~in_p] = -1

        slot_aspects: List[float] = []
        slot_ext_frac: List[float] = []
        for s_idx in range(n):
            sm = vor == s_idx
            ys_s, xs_s = np.where(sm)
            if len(xs_s) < 4:
                slot_aspects.append(1.0)
                slot_ext_frac.append(0.5)
                continue
            pts = np.stack([xs_s - xs_s.mean(), ys_s - ys_s.mean()], axis=1)
            evals = np.maximum(np.linalg.eigvalsh(np.cov(pts.T)), 1e-4)
            slot_aspects.append(float(math.sqrt(evals[1] / evals[0])))
            perim_s = float((sm ^ ndimage.binary_erosion(sm)).sum())
            ext_s = float((sm & bnd_p).sum())
            slot_ext_frac.append(ext_s / max(perim_s, 1.0))

        cost = np.zeros((n, n), dtype=np.float64)
        for m_idx, m in enumerate(motifs):
            for s_idx in range(n):
                asp_c = abs(math.log(max(m.aspect_ratio, 1.0) / max(slot_aspects[s_idx], 1.0)))
                # Intricate low-solidity motifs strongly prefer interior / low-exterior slots
                ext_c = (1.0 - m_solidities[m_idx]) * slot_ext_frac[s_idx] * 2.8
                cost[m_idx, s_idx] = asp_c + ext_c

        r_ind, c_ind = linear_sum_assignment(cost)
        cand_poses: List[PiecePose] = []
        n_ang = 24 if n <= 12 else 18
        for m_idx, s_idx in zip(r_ind, c_ind):
            sm = (vor == s_idx).astype(np.float32)
            _, _, pose = _aligned_iou_and_chamfer(sm, motifs[m_idx], allow_flip=allow_flip, n_angles=n_ang)
            pose.scale = float(np.clip(pose.scale, 0.80 * ideal_scales_p[m_idx], 1.25 * ideal_scales_p[m_idx]))
            cand_poses.append(pose)

        ms, ss, ls, es = eval_full(cand_poses)
        sc, _, _, _, _ = score_arrays(ms, ss, ls, es)
        if sc > best_init_sc:
            best_init_sc = sc
            best_poses_p = cand_poses

    assert best_poses_p is not None

    # Step 2: Fast simulated-annealing Escher SDF-tiling polish
    n_sa = min(1400, max(650, 80 * n)) if pack_iters < 0 else int(pack_iters)
    cur_poses = [PiecePose(p.cx, p.cy, p.angle_deg, p.scale, p.flip_x) for p in best_poses_p]
    masks_in, sdfs_in, leaks_p, epxs_p = eval_full(cur_poses)
    cur_sc, cur_mi, cur_mn, cur_ious, _ = score_arrays(masks_in, sdfs_in, leaks_p, epxs_p)
    best_sc = cur_sc
    best_poses_p = [PiecePose(p.cx, p.cy, p.angle_deg, p.scale, p.flip_x) for p in cur_poses]
    rng = np.random.default_rng(seed + 17)

    def upd_one(i: int, p: PiecePose) -> None:
        wm, ws = warp_motif_to_grid(motifs[i], p, hp, wp)
        mb_in = (wm > 0.5).ravel()[in_idx]
        masks_in[i] = mb_in
        sdfs_in[i] = ws.ravel()[in_idx]
        epx = m_areas[i] * ((p.scale / 256.0) ** 2)
        epxs_p[i] = epx
        leaks_p[i] = max(0.0, epx - float(mb_in.sum())) / max(epx, 1.0)

    for it in range(n_sa):
        temp = 0.017 * (0.02 ** (it / max(1, n_sa - 1)))
        i = int(np.argmin(cur_ious)) if rng.random() < 0.45 else int(rng.integers(0, n))
        old_p = cur_poses[i]
        old_m, old_s, old_l, old_e = masks_in[i].copy(), sdfs_in[i].copy(), leaks_p[i], epxs_p[i]
        mv = rng.random()

        if mv < 0.14:
            cand = PiecePose(
                old_p.cx,
                old_p.cy,
                (old_p.angle_deg + float(rng.uniform(-180.0, 180.0))) % 360.0,
                old_p.scale,
                (not old_p.flip_x) if (allow_flip and rng.random() < 0.5) else old_p.flip_x,
            )
            cur_poses[i] = cand
            upd_one(i, cand)
            sc, mi, mn, ious, _ = score_arrays(masks_in, sdfs_in, leaks_p, epxs_p)
            if sc > cur_sc or rng.random() < math.exp((sc - cur_sc) / max(temp, 1e-6)):
                cur_sc, cur_mi, cur_mn, cur_ious = sc, mi, mn, ious
                if sc > best_sc:
                    best_sc = sc
                    best_poses_p = [PiecePose(p.cx, p.cy, p.angle_deg, p.scale, p.flip_x) for p in cur_poses]
            else:
                cur_poses[i] = old_p
                masks_in[i], sdfs_in[i], leaks_p[i], epxs_p[i] = old_m, old_s, old_l, old_e
        elif mv < 0.21 and n >= 2:
            # Slot-swap move preserving orientation alignment (with 0/90/180/270 offset)
            j = int(rng.integers(0, n - 1))
            if j >= i:
                j += 1
            old_pj = cur_poses[j]
            old_mj, old_sj, old_lj, old_ej = masks_in[j].copy(), sdfs_in[j].copy(), leaks_p[j], epxs_p[j]
            da_i = float(rng.choice([0.0, 90.0, 180.0, 270.0]))
            da_j = float(rng.choice([0.0, 90.0, 180.0, 270.0]))
            ci = PiecePose(
                old_pj.cx,
                old_pj.cy,
                (old_pj.angle_deg + da_i) % 360.0,
                ideal_scales_p[i],
                bool(rng.integers(0, 2)) if allow_flip else False,
            )
            cj = PiecePose(
                old_p.cx,
                old_p.cy,
                (old_p.angle_deg + da_j) % 360.0,
                ideal_scales_p[j],
                bool(rng.integers(0, 2)) if allow_flip else False,
            )
            cur_poses[i], cur_poses[j] = ci, cj
            upd_one(i, ci)
            upd_one(j, cj)
            sc, mi, mn, ious, _ = score_arrays(masks_in, sdfs_in, leaks_p, epxs_p)
            if sc > cur_sc or rng.random() < math.exp((sc - cur_sc) / max(temp, 1e-6)):
                cur_sc, cur_mi, cur_mn, cur_ious = sc, mi, mn, ious
                if sc > best_sc:
                    best_sc = sc
                    best_poses_p = [PiecePose(p.cx, p.cy, p.angle_deg, p.scale, p.flip_x) for p in cur_poses]
            else:
                cur_poses[i], cur_poses[j] = old_p, old_pj
                masks_in[i], sdfs_in[i], leaks_p[i], epxs_p[i] = old_m, old_s, old_l, old_e
                masks_in[j], sdfs_in[j], leaks_p[j], epxs_p[j] = old_mj, old_sj, old_lj, old_ej
        else:
            decay = 1.0 - 0.70 * (it / max(1, n_sa))
            cand = PiecePose(
                old_p.cx + float(rng.normal(0.0, 0.30 * eq_r_p * decay)),
                old_p.cy + float(rng.normal(0.0, 0.30 * eq_r_p * decay)),
                (old_p.angle_deg + float(rng.normal(0.0, 30.0 * decay))) % 360.0,
                float(
                    np.clip(
                        old_p.scale * math.exp(float(rng.normal(0.0, 0.08 * decay))),
                        0.76 * ideal_scales_p[i],
                        1.28 * ideal_scales_p[i],
                    )
                ),
                (not old_p.flip_x) if (allow_flip and rng.random() < 0.12) else old_p.flip_x,
            )
            cur_poses[i] = cand
            upd_one(i, cand)
            sc, mi, mn, ious, _ = score_arrays(masks_in, sdfs_in, leaks_p, epxs_p)
            if sc > cur_sc or rng.random() < math.exp((sc - cur_sc) / max(temp, 1e-6)):
                cur_sc, cur_mi, cur_mn, cur_ious = sc, mi, mn, ious
                if sc > best_sc:
                    best_sc = sc
                    best_poses_p = [PiecePose(p.cx, p.cy, p.angle_deg, p.scale, p.flip_x) for p in cur_poses]
            else:
                cur_poses[i] = old_p
                masks_in[i], sdfs_in[i], leaks_p[i], epxs_p[i] = old_m, old_s, old_l, old_e

    ms_b, ss_b, ls_b, es_b = eval_full(best_poses_p)
    _, mi_b, mn_b, _, lbl_flat = score_arrays(ms_b, ss_b, ls_b, es_b)
    lbl_pack = np.full((hp, wp), -1, dtype=np.int32)
    lbl_pack.ravel()[in_idx] = lbl_flat

    # Scale poses and blueprint labels onto optimization grid
    sx_opt = w_opt / float(wp)
    sy_opt = h_opt / float(hp)
    s_avg = 0.5 * (sx_opt + sy_opt)
    poses_opt = [
        PiecePose(p.cx * sx_opt, p.cy * sy_opt, p.angle_deg, p.scale * s_avg, p.flip_x)
        for p in best_poses_p
    ]
    lbl_pack_opt = cv2.resize(lbl_pack.astype(np.int32), (w_opt, h_opt), interpolation=cv2.INTER_NEAREST)
    lbl_pack_opt[~inside_opt] = -1

    seed_xy = np.zeros((n, 2), dtype=np.float64)
    for i, m in enumerate(motifs):
        wm, _ = warp_motif_to_grid(m, poses_opt[i], h_opt, w_opt)
        core = (wm > 0.5) & (lbl_pack_opt == i) & inside_opt
        if not np.any(core):
            core = (lbl_pack_opt == i) & inside_opt
        if not np.any(core):
            core = (wm > 0.5) & inside_opt
        seed_xy[i] = _pole_of_inaccessibility(core)

    return poses_opt, seed_xy, lbl_pack_opt, {"pack_mean_iou": mi_b, "pack_min_iou": mn_b}


# ---------------------------------------------------------------------------
# Phase 2: Multi-scale positive slowness fields & differentiable Model A guidance
# ---------------------------------------------------------------------------

class MultiScaleSlownessField(torch.nn.Module):
    """Coarse-to-fine bounded positive slowness field s(x) in [s_min, s_max].

    Supports `log_space=True` (recommended: centered geometric mean with balanced gradients across
    fast waveguides and slow barriers) and `log_space=False` (legacy baseline mode).
    """

    def __init__(
        self,
        h: int,
        w: int,
        s_min: float = 0.04,
        s_max: float = 24.0,
        init_raw: Optional[np.ndarray] = None,
        log_space: bool = True,
    ):
        super().__init__()
        self.h = h
        self.w = w
        self.s_min = float(s_min)
        self.s_max = float(s_max)
        self.log_space = bool(log_space)
        self.log_min = math.log(max(self.s_min, 1e-6))
        self.log_max = math.log(max(self.s_max, self.s_min + 1e-6))

        if self.log_space:
            c1 = max(10, min(18, max(h, w) // 8))
            c2 = max(24, min(40, max(h, w) // 4))
            c3 = max(48, min(72, max(h, w) // 2))
            self.p_coarse = torch.nn.Parameter(torch.zeros(1, 1, c1, c1))
            self.p_mid = torch.nn.Parameter(torch.zeros(1, 1, c2, c2))
            self.p_fine = torch.nn.Parameter(torch.zeros(1, 1, c3, c3))
            self.p_detail = torch.nn.Parameter(torch.zeros(1, 1, h, w))
        else:
            self.p_coarse = torch.nn.Parameter(torch.zeros(1, 1, 10, 10))
            self.p_mid = torch.nn.Parameter(torch.zeros(1, 1, 22, 22))
            self.p_fine = torch.nn.Parameter(torch.zeros(1, 1, 44, 44))
            self.p_detail = torch.nn.Parameter(torch.zeros(1, 1, 64, 64))

        if init_raw is not None:
            t = torch.from_numpy(init_raw.astype(np.float32))[None, None, :, :]
            with torch.no_grad():
                c = F.interpolate(t, size=self.p_coarse.shape[-2:], mode="bilinear", align_corners=False)
                self.p_coarse.copy_(c)
                rem1 = t - F.interpolate(c, size=(h, w), mode="bilinear", align_corners=False)
                m = F.interpolate(rem1, size=self.p_mid.shape[-2:], mode="bilinear", align_corners=False)
                self.p_mid.copy_(m)
                rem2 = rem1 - F.interpolate(m, size=(h, w), mode="bilinear", align_corners=False)
                f = F.interpolate(rem2, size=self.p_fine.shape[-2:], mode="bilinear", align_corners=False)
                self.p_fine.copy_(f * 0.85)
                rem3 = rem2 - F.interpolate(f * 0.85, size=(h, w), mode="bilinear", align_corners=False)
                d = F.interpolate(rem3, size=self.p_detail.shape[-2:], mode="bilinear", align_corners=False)
                self.p_detail.copy_(d * 0.65)

    def raw_field(self) -> torch.Tensor:
        u1 = F.interpolate(self.p_coarse, size=(self.h, self.w), mode="bilinear", align_corners=False)
        u2 = F.interpolate(self.p_mid, size=(self.h, self.w), mode="bilinear", align_corners=False)
        u3 = F.interpolate(self.p_fine, size=(self.h, self.w), mode="bilinear", align_corners=False)
        if self.p_detail.shape[-2:] == (self.h, self.w):
            u4 = self.p_detail
        else:
            u4 = F.interpolate(self.p_detail, size=(self.h, self.w), mode="bilinear", align_corners=False)

        if self.log_space:
            raw = u1 + 0.85 * u2 + 0.70 * u3 + 0.60 * u4
            return raw[0, 0]

        raw = u1 + 0.80 * u2 + 0.60 * u3 + 0.45 * u4
        raw = F.avg_pool2d(F.pad(raw, (1, 1, 1, 1), mode="replicate"), kernel_size=3, stride=1)
        return raw[0, 0]

    def forward(self) -> Tuple[torch.Tensor, torch.Tensor]:
        raw = self.raw_field()
        if self.log_space:
            log_s = self.log_min + (self.log_max - self.log_min) * torch.sigmoid(raw)
            return torch.exp(log_s), raw
        s = self.s_min + (self.s_max - self.s_min) * torch.sigmoid(raw)
        return s, raw


class _DifferentiableRecognizerGuidance:
    """Frozen Model A silhouette encoder providing smooth semantic + embedding gradients."""

    def __init__(
        self,
        checkpoint_path: str | Path,
        motifs: Sequence[MotifAsset],
        device: torch.device,
    ):
        from ..models.recognizer import (
            fast_mask_to_model_input,
            load_recognizer_checkpoint,
            normalize_mask_frame,
        )

        self._fast_input = fast_mask_to_model_input
        self.model, self.meta = load_recognizer_checkpoint(checkpoint_path, device=device)
        for p in self.model.parameters():
            p.requires_grad_(False)
        self.model.eval()
        self.device = device
        self.img_size = int(self.meta["img_size"])
        self.families: List[str] = list(self.meta["families"])
        self.classes: List[str] = list(self.meta["classes"])
        self.temp_fam = max(0.1, float(self.meta.get("temperature_family", 1.0)))
        self.temp_cls = max(0.1, float(self.meta.get("temperature_class", 1.0)))

        # Precompute canonical target motif embeddings and valid class/family target IDs
        target_inputs = [
            fast_mask_to_model_input(normalize_mask_frame(m.mask, size=self.img_size), size=self.img_size)
            for m in motifs
        ]
        with torch.no_grad():
            bx = torch.from_numpy(np.stack(target_inputs, axis=0)).to(device)
            self.target_embs = self.model(bx)["embedding"].detach()  # [N, D], L2-normalized

        self.cls_targets = [
            self.classes.index(m.motif_class) if m.motif_class in self.classes else -1
            for m in motifs
        ]
        self.fam_targets = [
            self.families.index(m.family) if m.family in self.families else -1
            for m in motifs
        ]

    def _canonical_crop_batch(
        self,
        P_grid: torch.Tensor,
        poses: Sequence[PiecePose],
    ) -> torch.Tensor:
        """Differentiably resample each soft cell `P_grid[i]` into canonical `(img_size, img_size)` frame."""
        n, h, w = P_grid.shape
        sz = self.img_size
        thetas = []
        # Map canonical output coordinates (u, v) in [-1, 1] -> source grid coords in [-1, 1]
        # via piece pose (cx, cy, angle, scale, flip_x) with slight 1.05x safety margin.
        for pose in poses:
            ang = math.radians(pose.angle_deg)
            half_span = 0.52 * pose.scale
            ca = math.cos(ang) * half_span
            sa = math.sin(ang) * half_span
            fx = -1.0 if pose.flip_x else 1.0
            # x_px = cx + (ca * (fx * u) - sa * v), y_px = cy + (sa * (fx * u) + ca * v)
            # Normalized grid_sample coord: x_n = 2 * x_px / (w - 1) - 1
            wm1 = max(1.0, float(w - 1))
            hm1 = max(1.0, float(h - 1))
            t00 = 2.0 * ca * fx / wm1
            t01 = -2.0 * sa / wm1
            t02 = 2.0 * pose.cx / wm1 - 1.0
            t10 = 2.0 * sa * fx / hm1
            t11 = 2.0 * ca / hm1
            t12 = 2.0 * pose.cy / hm1 - 1.0
            thetas.append([[t00, t01, t02], [t10, t11, t12]])

        theta_t = torch.tensor(thetas, device=P_grid.device, dtype=P_grid.dtype)  # [N, 2, 3]
        grid = F.affine_grid(theta_t, size=(n, 1, sz, sz), align_corners=True)
        soft_masks = F.grid_sample(
            P_grid.unsqueeze(1),
            grid,
            mode="bilinear",
            padding_mode="zeros",
            align_corners=True,
        )  # [N, 1, sz, sz]
        return soft_masks.clamp(0.0, 1.0)

    def loss(self, P_grid: torch.Tensor, poses: Sequence[PiecePose]) -> torch.Tensor:
        """Compute embedding consistency + calibrated class/family semantic guidance loss."""
        soft_masks = self._canonical_crop_batch(P_grid, poses)  # [N, 1, sz, sz]
        n = soft_masks.shape[0]

        # Exact forward SDF via OpenCV EDT + smooth multi-scale straight-through surrogate for gradients
        masks_np = soft_masks.detach().cpu().numpy()[:, 0]
        exact_sdfs = np.stack(
            [self._fast_input(masks_np[i], size=self.img_size)[1] for i in range(n)],
            axis=0,
        )
        sdf_exact_t = torch.from_numpy(exact_sdfs).to(device=soft_masks.device, dtype=soft_masks.dtype).unsqueeze(1)

        occ_signed = 1.0 - 2.0 * soft_masks  # negative inside, positive outside
        s_lo = F.avg_pool2d(F.pad(occ_signed, (2, 2, 2, 2), mode="replicate"), kernel_size=5, stride=1)
        s_hi = F.avg_pool2d(F.pad(occ_signed, (6, 6, 6, 6), mode="replicate"), kernel_size=13, stride=1)
        sdf_proxy = 0.50 * occ_signed + 0.30 * s_lo + 0.20 * s_hi
        sdf_chan = (sdf_exact_t - sdf_proxy).detach() + sdf_proxy

        x = torch.cat([soft_masks, sdf_chan], dim=1)  # [N, 2, sz, sz]
        out = self.model(x)

        # 1. Target-motif embedding cosine alignment (prevents adversarial class-only artifacts)
        cos_sim = (out["embedding"] * self.target_embs).sum(dim=1)
        loss_emb = (1.0 - cos_sim).mean()

        total = loss_emb

        # 2. Class & family supervision where labels are known to this checkpoint
        valid_c = [(i, cid) for i, cid in enumerate(self.cls_targets) if cid >= 0]
        if valid_c and len(self.classes) > 1:
            idx_t = torch.tensor([i for i, _ in valid_c], device=self.device, dtype=torch.long)
            lbl_t = torch.tensor([cid for _, cid in valid_c], device=self.device, dtype=torch.long)
            loss_cls = F.cross_entropy(out["class_logits"][idx_t] / self.temp_cls, lbl_t)
            total = total + 0.35 * loss_cls

        valid_f = [(i, fid) for i, fid in enumerate(self.fam_targets) if fid >= 0]
        if valid_f and len(self.families) > 1:
            idx_t = torch.tensor([i for i, _ in valid_f], device=self.device, dtype=torch.long)
            lbl_t = torch.tensor([fid for _, fid in valid_f], device=self.device, dtype=torch.long)
            loss_fam = F.cross_entropy(out["family_logits"][idx_t] / self.temp_fam, lbl_t)
            total = total + 0.20 * loss_fam

        return total


def _physics_warm_start_slowness(
    target_masks: np.ndarray,
    target_sdfs: np.ndarray,
    inside: np.ndarray,
    seed_xy: np.ndarray,
    lbl_pack: Optional[np.ndarray] = None,
    log_space: bool = True,
) -> np.ndarray:
    """Physics-informed initial raw slowness field: fast waveguide inside limbs, slow ridge on seams."""
    n, h, w = target_masks.shape
    raw = np.zeros((h, w), dtype=np.float32)

    min_abs_sdf = np.min(np.abs(target_sdfs), axis=0)
    if log_space:
        if n >= 2:
            sorted_sdf = np.sort(target_sdfs, axis=0)
            seam_gap = np.abs(sorted_sdf[1] - sorted_sdf[0])
            raw += 2.30 * np.exp(-0.5 * (seam_gap / 1.7) ** 2)
        raw += 1.00 * np.exp(-0.5 * (min_abs_sdf / 1.5) ** 2)

        yy, xx = np.meshgrid(np.arange(h, dtype=np.float32), np.arange(w, dtype=np.float32), indexing="ij")
        for i in range(n):
            owner_mask = (lbl_pack == i).astype(np.float32) if lbl_pack is not None else target_masks[i]
            interior = (target_sdfs[i] < 0).astype(np.float32) * owner_mask
            dist_from_seed = np.hypot(xx - seed_xy[i, 0], yy - seed_xy[i, 1]) / max(h, w)
            raw -= (0.60 + 2.35 * dist_from_seed) * interior

        raw = ndimage.gaussian_filter(raw, sigma=0.85)
        raw[~inside] = 0.0
        return np.clip(raw, -2.5, 2.5).astype(np.float32)

    contour_band = np.exp(-0.5 * (min_abs_sdf / 1.8) ** 2)
    raw += 1.35 * contour_band
    if n >= 2:
        sorted_sdf = np.sort(target_sdfs, axis=0)
        seam_gap = np.abs(sorted_sdf[1] - sorted_sdf[0])
        seam_ridge = np.exp(-0.5 * (seam_gap / 2.2) ** 2)
        raw += 0.90 * seam_ridge

    yy, xx = np.meshgrid(np.arange(h, dtype=np.float32), np.arange(w, dtype=np.float32), indexing="ij")
    for i in range(n):
        interior = np.clip(-target_sdfs[i] / 4.0, 0.0, 1.0)
        dist_from_seed = np.hypot(xx - seed_xy[i, 0], yy - seed_xy[i, 1]) / max(h, w)
        raw -= (0.65 + 1.15 * dist_from_seed) * interior * target_masks[i]

    raw = ndimage.gaussian_filter(raw, sigma=1.1)
    raw[~inside] = 0.0
    return np.clip(raw, -2.2, 2.2).astype(np.float32)


def _snap_seed_to_node(x: float, y: float, graph: GridGraph, used: set[int]) -> int:
    """Map continuous `(x, y)` to a unique interior graph node ID."""
    r = int(np.clip(round(y), 0, graph.h - 1))
    c = int(np.clip(round(x), 0, graph.w - 1))
    nid = int(graph.coord_to_node[r, c])
    if nid >= 0 and nid not in used:
        return nid
    d2 = (graph.xs - x) ** 2 + (graph.ys - y) ** 2
    for idx in np.argsort(d2):
        cand = int(idx)
        if cand not in used:
            return cand
    return 0


# ---------------------------------------------------------------------------
# Phase 3: Discrete neck safeguard & smooth SDF-guided seam polish
# ---------------------------------------------------------------------------

def _repair_fragile_necks_on_grid(
    labels_inside: np.ndarray,
    inside: np.ndarray,
    n_pieces: int,
    px_mm: float,
    min_neck_mm: float,
    kerf_mm: float,
    max_rounds: int = 6,
) -> np.ndarray:
    """Thicken any load-bearing neck narrower than `(min_neck_mm + kerf_mm)` on the fine label grid."""
    out = labels_inside.copy()
    neck_r_px = ((min_neck_mm + kerf_mm) * 0.5) / px_mm
    bridge_r_px = max(2, int(math.ceil(neck_r_px * 1.25)))
    min_lobe_px = max(12, int(round(20.0 / (px_mm * px_mm))))

    for _ in range(max_rounds):
        any_repaired = False
        for lbl in range(n_pieces):
            mask = out == lbl
            if not np.any(mask):
                continue
            dist = ndimage.distance_transform_edt(mask)
            core = dist >= (neck_r_px * 1.08)
            cc, num = ndimage.label(core)
            if num <= 1:
                continue
            sizes = np.bincount(cc.ravel())
            sizes[0] = 0
            big_lobes = [int(cid) for cid in range(1, num + 1) if sizes[cid] >= min_lobe_px]
            if len(big_lobes) <= 1:
                continue

            big_lobes.sort(key=lambda cid: sizes[cid], reverse=True)
            main_lobe = cc == big_lobes[0]
            sec_lobe = cc == big_lobes[1]

            d_main, idx_main = ndimage.distance_transform_edt(~main_lobe, return_indices=True)
            sec_ys, sec_xs = np.where(sec_lobe)
            dists_sec = d_main[sec_ys, sec_xs]
            best_k = int(np.argmin(dists_sec))
            y2, x2 = int(sec_ys[best_k]), int(sec_xs[best_k])
            y1, x1 = int(idx_main[0, y2, x2]), int(idx_main[1, y2, x2])

            steps = max(2, int(math.ceil(math.hypot(x2 - x1, y2 - y1) * 2.0)))
            bridge = np.zeros_like(inside, dtype=np.uint8)
            for t in np.linspace(0.0, 1.0, steps):
                bx = int(round(x1 + t * (x2 - x1)))
                by = int(round(y1 + t * (y2 - y1)))
                cv2.circle(bridge, (bx, by), bridge_r_px, 1, thickness=-1)
            add_mask = (bridge > 0) & inside & (out != lbl)
            if not np.any(add_mask):
                continue

            cand = out.copy()
            affected = sorted(set(int(v) for v in np.unique(out[add_mask]) if v >= 0))
            cand[add_mask] = lbl
            safe = True
            for nb in affected:
                nb_mask = cand == nb
                if int(np.sum(nb_mask)) < min_lobe_px * 3:
                    safe = False
                    break
                _, ncc = ndimage.label(nb_mask)
                if ncc != 1:
                    safe = False
                    break
            if safe:
                out = cand
                any_repaired = True
        if not any_repaired:
            break
    return out


def _apply_gentle_interlock_waves(
    slowness: np.ndarray,
    inside: np.ndarray,
    seed: int = 42,
    amp: float = 0.25,
) -> np.ndarray:
    """Add mild harmonic slowness modulation to guarantee curved interlocking shared boundaries."""
    h, w = slowness.shape
    rng = np.random.default_rng(seed + 777)
    yy, xx = np.meshgrid(np.linspace(0.0, 1.0, h), np.linspace(0.0, 1.0, w), indexing="ij")
    wave = np.zeros((h, w), dtype=np.float32)
    for freq in (4.0, 7.0):
        ph1 = float(rng.uniform(0.0, 2.0 * math.pi))
        ph2 = float(rng.uniform(0.0, 2.0 * math.pi))
        wave += np.sin(2.0 * math.pi * freq * xx + ph1) * np.cos(2.0 * math.pi * freq * yy + ph2)
    wave = wave / max(float(np.std(wave)), 1e-3)
    mod = slowness * np.exp(amp * 0.35 * wave)
    return np.clip(mod, 0.04, 28.0).astype(np.float32)


def _extend_motif_seams(
    labels: np.ndarray,
    motifs: Sequence[MotifAsset],
    poses: Sequence[PiecePose],
    smooth_sigma: float = 1.1,
) -> np.ndarray:
    """Smoothly offer misplaced pixels to a contiguous piece whose fitted motif contains them."""
    inside = labels >= 0
    depths = []
    for motif, pose in zip(motifs, poses):
        mask, _ = warp_motif_to_grid(motif, pose, *labels.shape)
        if smooth_sigma > 0.0:
            mask = ndimage.gaussian_filter(mask, sigma=smooth_sigma)
        depths.append(ndimage.distance_transform_edt(mask > 0.5))
    depth = np.stack(depths)
    winner = depth.argmax(axis=0)
    current_depth = np.take_along_axis(depth, labels.clip(0)[None], axis=0)[0]
    available = inside & (current_depth == 0) & (depth.max(axis=0) > 0)
    proposal = labels.copy()
    for i in range(len(motifs)):
        eligible = available & (winner == i)
        if eligible.any():
            # Smooth candidate transfer slightly so raw jagged target noise isn't copied into cuts
            eligible_s = ndimage.binary_opening(eligible, iterations=1)
            if not eligible_s.any():
                continue
            existing = labels == i
            attached = ndimage.binary_propagation(existing, mask=existing | eligible_s)
            proposal[attached & eligible_s] = i
    return proposal


def _resolve_device(device_pref: str) -> torch.device:
    pref = (device_pref or "cpu").lower()
    if pref == "cuda":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if pref == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device("cpu")


# ---------------------------------------------------------------------------
# Main end-to-end solver
# ---------------------------------------------------------------------------

def solve_geodesic_puzzle(
    outline: Polygon,
    motifs: Sequence[MotifAsset],
    config: Optional[GeodesicSolverConfig] = None,
) -> GeodesicSolution:
    """Optimize a shared-metric geodesic Voronoi partition of `outline` matching `motifs`."""
    t0 = time.perf_counter()
    cfg = config or GeodesicSolverConfig()
    n = len(motifs)
    if n < 2:
        raise ValueError("Puzzle solver requires at least 2 motifs")

    upgraded = (cfg.solver_mode or "upgraded").lower() != "baseline"
    logs: List[str] = []
    torch.manual_seed(cfg.seed)
    np.random.seed(cfg.seed)

    x0, y0, x1, y1 = outline.bounds
    span_mm = max(x1 - x0, y1 - y0)

    # 1. Build optimization grid & 8-connected graph
    px_opt = span_mm / float(cfg.opt_grid_max_dim)
    h_opt, w_opt = grid_shape(outline, px_opt)
    inside_opt = inside_mask(outline, h_opt, w_opt, px_opt)
    graph_opt = GridGraph(inside_opt)
    logs.append(
        f"Solver mode={cfg.solver_mode} | Optimization grid: {w_opt}x{h_opt} px "
        f"({px_opt:.2f} mm/px), {graph_opt.num_nodes} active nodes, numba={_HAS_NUMBA}"
    )

    # 2. Phase 1: Layout initialization & blueprint packing
    lbl_pack_opt: Optional[np.ndarray] = None
    if upgraded:
        t_p0 = time.perf_counter()
        poses, seed_xy, lbl_pack_opt, pack_stats = _initialize_escher_packed_layout(
            outline=outline,
            inside_opt=inside_opt,
            motifs=motifs,
            seed=cfg.seed,
            allow_flip=cfg.allow_flip,
            n_restarts=cfg.n_restarts,
            pack_iters=cfg.pack_iters,
        )
        logs.append(
            f"Escher SDF blueprint pack ({time.perf_counter() - t_p0:.2f}s, restarts={cfg.n_restarts}): "
            f"mean_iou={pack_stats['pack_mean_iou']:.4f}, min_iou={pack_stats['pack_min_iou']:.4f}"
        )
    else:
        _, poses, seed_xy = _initialize_packed_layout_baseline(
            inside_opt, motifs, seed=cfg.seed, allow_flip=cfg.allow_flip
        )

    def render_targets(cur_poses: Sequence[PiecePose]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        ms = np.zeros((n, h_opt, w_opt), dtype=np.float32)
        sdfs = np.zeros((n, h_opt, w_opt), dtype=np.float32)
        sals = np.ones((n, h_opt, w_opt), dtype=np.float32)
        for i, m in enumerate(motifs):
            ms[i], sdfs[i] = warp_motif_to_grid(m, cur_poses[i], h_opt, w_opt)
            if upgraded:
                sals[i] = _warp_scalar_map(compute_motif_saliency(m.mask), cur_poses[i], h_opt, w_opt, border=1.0)
        return ms, sdfs, sals

    target_masks_np, target_sdfs_np, target_sal_np = render_targets(poses)
    init_raw = _physics_warm_start_slowness(
        target_masks_np,
        target_sdfs_np,
        inside_opt,
        seed_xy,
        lbl_pack=lbl_pack_opt,
        log_space=upgraded,
    )

    # 3. Phase 2: Differentiable PyTorch Adam optimization over shared multi-scale slowness field
    device = _resolve_device(cfg.device)
    s_min = cfg.s_min if upgraded else max(cfg.s_min, 0.08)
    s_max = cfg.s_max if upgraded else min(cfg.s_max, 12.0)
    model = MultiScaleSlownessField(
        h_opt,
        w_opt,
        s_min=s_min,
        s_max=s_max,
        init_raw=init_raw,
        log_space=upgraded,
    ).to(device)
    lr = cfg.lr if upgraded else min(cfg.lr, 0.14)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    # Optional Model A semantic / embedding guidance (Dossier Section 4.5)
    sem_guide: Optional[_DifferentiableRecognizerGuidance] = None
    eff_w_sem = float(cfg.w_sem)
    if cfg.recognizer_checkpoint and eff_w_sem > 0.0:
        sem_guide = _DifferentiableRecognizerGuidance(cfg.recognizer_checkpoint, motifs, device=device)
        logs.append(
            f"Loaded Model A guidance: {cfg.recognizer_checkpoint} "
            f"(arch_v{sem_guide.meta['arch_version']}, img={sem_guide.img_size}, w_sem={eff_w_sem:.3f})"
        )

    ys_t = torch.from_numpy(graph_opt.ys.astype(np.int64)).to(device)
    xs_t = torch.from_numpy(graph_opt.xs.astype(np.int64)).to(device)
    total_area_px = float(np.sum(inside_opt))
    target_area_px = total_area_px / float(n)
    eq_radius_px = max(2.0, math.sqrt(target_area_px / math.pi))

    # In upgraded mode, let each piece target its Escher-packed natural area share (clamped
    # strictly within [0.58, 1.55] of mean so max/min <= 2.67 << 4.0 hard gate) so slender
    # motifs (fish, ships, whales) are not forced to bloat into voids.
    if upgraded and lbl_pack_opt is not None:
        packed_counts = np.array(
            [float(np.sum(lbl_pack_opt == i)) for i in range(n)],
            dtype=np.float32,
        )
        clamped = np.clip(packed_counts, 0.58 * target_area_px, 1.55 * target_area_px)
        clamped = clamped * (total_area_px / max(float(clamped.sum()), 1.0))
        target_areas_t = torch.from_numpy(clamped).to(device)
    else:
        target_areas_t = torch.full((n,), target_area_px, dtype=torch.float32, device=device)

    neck_k = max(3, int(round((cfg.min_neck_mm + cfg.kerf_mm) / px_opt)) | 1)
    neck_pad = neck_k // 2

    def make_seed_nodes(sxy: np.ndarray) -> List[int]:
        used: set[int] = set()
        nodes: List[int] = []
        for i in range(n):
            nid = _snap_seed_to_node(sxy[i, 0], sxy[i, 1], graph_opt, used)
            used.add(nid)
            nodes.append(nid)
        return nodes

    seed_nodes = make_seed_nodes(seed_xy)
    t_masks_t = torch.from_numpy(target_masks_np).to(device)
    t_sdfs_t = torch.from_numpy(target_sdfs_np / eq_radius_px).to(device)
    t_sal_t = torch.from_numpy(target_sal_np).to(device)

    loss_history: List[float] = []
    sem_loss_history: List[float] = []
    best_loss = float("inf")
    best_s_np: Optional[np.ndarray] = None
    best_seed_xy = seed_xy.copy()
    best_poses = [PiecePose(p.cx, p.cy, p.angle_deg, p.scale, p.flip_x) for p in poses]

    for step in range(cfg.n_steps):
        frac = step / float(max(1, cfg.n_steps - 1))
        optimizer.zero_grad()
        s_grid, raw_grid = model()
        s_vec = s_grid[ys_t, xs_t]

        T = geodesic_distances(s_vec, graph_opt, seed_nodes)  # [N, M]
        T_rel = T - T.min(dim=0, keepdim=True).values
        if upgraded:
            # Neutral log-space slowness is ~1.0, so eq_radius_px is the exact scale-invariant
            # geodesic distance unit across piece counts without neutralizing barrier growth.
            tau_frac = cfg.tau_start * ((cfg.tau_end / max(cfg.tau_start, 1e-4)) ** frac)
            tau = max(0.08, eq_radius_px * tau_frac)
        else:
            tau_s = 2.4
            tau_e = 0.16
            tau = tau_s * ((tau_e / tau_s) ** frac)

        P_vec = F.softmax(-T_rel / tau, dim=0)  # [N, M]
        P_grid = torch.zeros((n, h_opt, w_opt), device=device, dtype=s_grid.dtype)
        P_grid[:, ys_t, xs_t] = P_vec

        # (a) Soft IoU loss (+ worst-piece floor in upgraded mode)
        inter = (P_grid * t_masks_t).sum(dim=(1, 2))
        union = (P_grid + t_masks_t - P_grid * t_masks_t).sum(dim=(1, 2)) + 1e-4
        iou_per = inter / union
        loss_iou = (1.0 - iou_per).mean() + (0.35 * (1.0 - iou_per.min()) if upgraded else 0.0)

        # (b) Saliency-weighted SDF contour matching loss
        loss_sdf_out = (P_grid * F.relu(t_sdfs_t) * t_sal_t).sum(dim=(1, 2)) / (
            P_grid.sum(dim=(1, 2)) + 1.0
        )
        loss_sdf_in = ((1.0 - P_grid) * t_masks_t * F.relu(-t_sdfs_t) * t_sal_t).sum(dim=(1, 2)) / (
            t_masks_t.sum(dim=(1, 2)) + 1.0
        )
        in_w = 1.35 if upgraded else 1.20
        loss_sdf = (loss_sdf_out + in_w * loss_sdf_in).mean()

        # (c) Area balance loss (against blueprint-matched clamped piece target areas)
        areas = P_grid.sum(dim=(1, 2))
        loss_area = (((areas - target_areas_t) / target_areas_t) ** 2).mean()

        # (d) Differentiable soft-erosion neck robustness penalty
        eroded = -F.max_pool2d(-P_grid.unsqueeze(1), kernel_size=neck_k, stride=1, padding=neck_pad).squeeze(1)
        core_ratio = eroded.sum(dim=(1, 2)) / (areas + 1.0)
        loss_neck = F.relu((0.36 if upgraded else 0.42) - core_ratio).mean()

        # (e) Smoothness / TV regularization
        tv_raw = ((raw_grid[1:, :] - raw_grid[:-1, :]) ** 2).mean() + (
            (raw_grid[:, 1:] - raw_grid[:, :-1]) ** 2
        ).mean()
        if upgraded:
            loss_tv = tv_raw
        else:
            p_grad = (
                torch.abs(P_grid[:, 1:, :] - P_grid[:, :-1, :]).mean()
                + torch.abs(P_grid[:, :, 1:] - P_grid[:, :, :-1]).mean()
            )
            loss_tv = tv_raw + 0.35 * p_grad

        # (f) Interlocking slowness variance floor
        raw_std = torch.sqrt(torch.var(raw_grid[ys_t, xs_t]) + 1e-5)
        loss_inter = F.relu(0.55 - raw_std)

        geom_loss = (
            cfg.w_iou * loss_iou
            + cfg.w_sdf * loss_sdf
            + cfg.w_area * loss_area
            + cfg.w_neck * loss_neck
            + cfg.w_tv * loss_tv
            + cfg.w_interlock * loss_inter
        )

        # (g) Optional Model A semantic / embedding guidance (applied periodically after warmup)
        loss = geom_loss
        if sem_guide is not None and frac >= 0.20 and (step % max(1, cfg.sem_interval) == 0 or step == cfg.n_steps - 1):
            l_sem = sem_guide.loss(P_grid, poses)
            sem_loss_history.append(float(l_sem.item()))
            loss = loss + eff_w_sem * l_sem

        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 3.0 if upgraded else 2.0)
        optimizer.step()

        lval = float(geom_loss.item())
        loss_history.append(lval)
        min_best_step = min(12, max(2, cfg.n_steps // 6))
        if lval < best_loss and step >= min_best_step:
            best_loss = lval
            best_s_np = s_grid.detach().cpu().numpy().copy()
            best_seed_xy = seed_xy.copy()
            best_poses = [PiecePose(p.cx, p.cy, p.angle_deg, p.scale, p.flip_x) for p in poses]

        # Mid-optimization seed centering (baseline also re-aligns poses to Voronoi cells)
        if not upgraded and step in (25, 55) and step < cfg.n_steps - 15:
            with torch.no_grad():
                hard_lbl = np.full((h_opt, w_opt), -1, dtype=np.int32)
                hard_lbl[inside_opt] = np.argmax(P_vec.detach().cpu().numpy(), axis=0)
                for i in range(n):
                    cell_i = (hard_lbl == i).astype(np.float32)
                    if np.sum(cell_i) < 12:
                        continue
                    _, _, new_pose = _aligned_iou_and_chamfer(
                        cell_i, motifs[i], allow_flip=cfg.allow_flip, n_angles=24
                    )
                    m_area = max(100.0, float(np.sum(motifs[i].mask > 0.5)))
                    ideal_scale = 256.0 * math.sqrt(target_area_px / m_area)
                    new_pose.scale = float(np.clip(new_pose.scale, 0.85 * ideal_scale, 1.25 * ideal_scale))
                    poses[i] = new_pose

                    wm, _ = warp_motif_to_grid(motifs[i], poses[i], h_opt, w_opt)
                    core_m = (cell_i > 0.5) & (wm > 0.5)
                    if not np.any(core_m):
                        core_m = cell_i > 0.5
                    sx, sy = _pole_of_inaccessibility(core_m)
                    seed_xy[i] = [sx, sy]

                seed_nodes = make_seed_nodes(seed_xy)
                target_masks_np, target_sdfs_np, target_sal_np = render_targets(poses)
                t_masks_t = torch.from_numpy(target_masks_np).to(device)
                t_sdfs_t = torch.from_numpy(target_sdfs_np / eq_radius_px).to(device)
                t_sal_t = torch.from_numpy(target_sal_np).to(device)
        elif upgraded and step == max(20, cfg.n_steps // 3) and step < cfg.n_steps - 20:
            # Gently re-center seeds inside cell ∩ motif without disturbing packed poses
            with torch.no_grad():
                hard_lbl = np.full((h_opt, w_opt), -1, dtype=np.int32)
                hard_lbl[inside_opt] = np.argmax(P_vec.detach().cpu().numpy(), axis=0)
                for i in range(n):
                    core_m = (hard_lbl == i) & (target_masks_np[i] > 0.5) & inside_opt
                    if int(np.sum(core_m)) >= 12:
                        sx, sy = _pole_of_inaccessibility(core_m)
                        seed_xy[i] = [0.5 * seed_xy[i, 0] + 0.5 * sx, 0.5 * seed_xy[i, 1] + 0.5 * sy]
                seed_nodes = make_seed_nodes(seed_xy)

    if best_s_np is None:
        with torch.no_grad():
            best_s_np = model()[0].detach().cpu().numpy()

    sem_note = (
        f", sem_loss {sem_loss_history[0]:.3f} -> {sem_loss_history[-1]:.3f}"
        if sem_loss_history
        else ""
    )
    logs.append(
        f"Geodesic optimization ({cfg.n_steps} steps on {device.type}): "
        f"loss {loss_history[0]:.3f} -> {best_loss:.3f}{sem_note}"
    )

    # 4. Phase 3: Upsample shared slowness field to crisp high-res grid & extract exact partition
    px_hi = span_mm / float(cfg.final_grid_max_dim)
    h_hi, w_hi = grid_shape(outline, px_hi)
    inside_hi = inside_mask(outline, h_hi, w_hi, px_hi)
    s_hi_base = cv2.resize(best_s_np, (w_hi, h_hi), interpolation=cv2.INTER_CUBIC)
    s_hi_base = np.clip(s_hi_base, s_min * 0.75, s_max * 1.25).astype(np.float32)

    scale_x = w_hi / float(w_opt)
    scale_y = h_hi / float(h_opt)
    seeds_hi_xy = np.stack([best_seed_xy[:, 0] * scale_x, best_seed_xy[:, 1] * scale_y], axis=1)

    graph_hi = GridGraph(inside_hi)
    vcfg = ValidationConfig(kerf_mm=cfg.kerf_mm, min_neck_mm=cfg.min_neck_mm)

    final_partition: Optional[PlanarPartition] = None
    final_labels: Optional[np.ndarray] = None
    final_s_hi = s_hi_base
    seeds_hi_nodes: List[int] = []
    best_gate_score = -1

    attempts = (
        (0.0, cfg.smooth_iters),
        (0.35, cfg.smooth_iters),
        (0.70, cfg.smooth_iters),
        (0.15, max(1, cfg.smooth_iters - 1)),
        (0.50, 1),
    )
    for attempt, (wave_amp, sm_iters) in enumerate(attempts):
        s_try = (
            s_hi_base
            if wave_amp == 0.0
            else _apply_gentle_interlock_waves(s_hi_base, inside_hi, seed=cfg.seed + attempt, amp=wave_amp)
        )
        used_hi: set[int] = set()
        s_nodes: List[int] = []
        for i in range(n):
            nid = _snap_seed_to_node(seeds_hi_xy[i, 0], seeds_hi_xy[i, 1], graph_hi, used_hi)
            used_hi.add(nid)
            s_nodes.append(nid)

        s_vec_hi = s_try[graph_hi.ys, graph_hi.xs]
        dist_hi, _ = graph_hi.shortest_paths(s_vec_hi, s_nodes)
        lbl_hi = np.full((h_hi, w_hi), -1, dtype=np.int32)
        lbl_hi[graph_hi.ys, graph_hi.xs] = np.argmin(dist_hi, axis=0).astype(np.int32)

        lbl_hi = _repair_fragile_necks_on_grid(
            lbl_hi,
            inside_hi,
            n_pieces=n,
            px_mm=px_hi,
            min_neck_mm=cfg.min_neck_mm,
            kerf_mm=cfg.kerf_mm,
        )

        try:
            cand_part = extract_partition_from_labels(
                lbl_hi,
                outline=outline,
                px=px_hi,
                n_pieces=n,
                smooth_iters=sm_iters,
                simplify_tol_mm=cfg.simplify_tol_mm,
            )
            rep = validate_partition(cand_part, expected_n=n, config=vcfg)
            gate_score = sum(1 for v in rep["gates"].values() if v)
            if gate_score > best_gate_score:
                best_gate_score = gate_score
                final_partition = cand_part
                final_labels = lbl_hi
                final_s_hi = s_try
                seeds_hi_nodes = s_nodes
            if rep["all_hard_gates_passed"]:
                logs.append(
                    f"High-res extraction attempt {attempt + 1} (wave={wave_amp:.2f}, smooth={sm_iters}): "
                    f"ALL HARD GATES PASSED"
                )
                break
            failed = [k for k, v in rep["gates"].items() if not v]
            logs.append(
                f"High-res extraction attempt {attempt + 1} (wave={wave_amp:.2f}, smooth={sm_iters}): "
                f"failed gates={failed}"
            )
        except Exception as exc:
            logs.append(f"High-res extraction attempt {attempt + 1} error: {exc}")

    if final_partition is None or final_labels is None:
        raise RuntimeError("Failed to extract valid planar partition from geodesic Voronoi grid")

    seeds_mm = [
        (round(x0 + (graph_hi.xs[nid] + 0.5) * px_hi, 3), round(y0 + (graph_hi.ys[nid] + 0.5) * px_hi, 3))
        for nid in seeds_hi_nodes
    ]

    xs_mm = x0 + (np.arange(w_hi) + 0.5) * px_hi
    ys_mm = y0 + (np.arange(h_hi) + 0.5) * px_hi

    def fit_vector_faces(partition: PlanarPartition, n_ang: int = 36) -> List[Tuple[float, float, PiecePose]]:
        fits = []
        for face, motif in zip(partition.faces, motifs):
            mask = shapely.contains_xy(face.polygon, xs_mm[None, :], ys_mm[:, None])
            fits.append(_aligned_iou_and_chamfer(mask, motif, allow_flip=cfg.allow_flip, n_angles=n_ang))
        return fits

    eval_angles = 36 if n <= 12 else 24
    fits = fit_vector_faces(final_partition, n_ang=eval_angles)
    max_refine_iters = 3 if n <= 12 else 2
    for iteration in range(max_refine_iters):
        proposed = _extend_motif_seams(
            final_labels,
            motifs,
            [pose for _, _, pose in fits],
            smooth_sigma=1.1 if upgraded else 0.0,
        )
        if np.array_equal(proposed, final_labels):
            break
        proposed = _repair_fragile_necks_on_grid(
            proposed, inside_hi, n, px_hi, cfg.min_neck_mm, cfg.kerf_mm
        )
        try:
            candidate = extract_partition_from_labels(
                proposed,
                outline,
                px_hi,
                n_pieces=n,
                smooth_iters=cfg.smooth_iters,
                simplify_tol_mm=cfg.simplify_tol_mm,
            )
            report = validate_partition(candidate, expected_n=n, config=vcfg)
        except (ValueError, RuntimeError, shapely.errors.GEOSException) as exc:
            logs.append(f"Silhouette refinement stopped during extraction: {exc}")
            break
        if not report["all_hard_gates_passed"]:
            failed = [key for key, passed in report["gates"].items() if not passed]
            logs.append(f"Silhouette refinement rejected by geometry checks: {failed}")
            break
        if not all(face.polygon.covers(shapely.Point(xy)) for face, xy in zip(candidate.faces, seeds_mm)):
            logs.append("Silhouette refinement would move a seed outside its piece")
            break
        candidate_fits = fit_vector_faces(candidate, n_ang=eval_angles)
        before = [fit[0] for fit in fits]
        after = [fit[0] for fit in candidate_fits]
        improved = np.mean(after) > np.mean(before) and min(after) >= min(before) - 1e-4
        logs.append(
            f"Silhouette refinement {iteration + 1}: mean IoU {np.mean(before):.4f} -> "
            f"{np.mean(after):.4f}, minimum {min(before):.4f} -> {min(after):.4f}; "
            f"{'accepted' if improved else 'rejected'}"
        )
        if not improved:
            break
        final_partition, final_labels, fits = candidate, proposed, candidate_fits

    per_piece_iou = [round(float(iou), 4) for iou, _, _ in fits]
    per_piece_chamfer = [round(float(chamfer), 4) for _, chamfer, _ in fits]
    final_poses_hi = [pose for _, _, pose in fits]
    edge_faces = {
        e.left_face if e.left_face >= 0 else e.right_face
        for e in final_partition.exterior_edges
    }
    for i, (face, m) in enumerate(zip(final_partition.faces, motifs)):
        face.metadata.update({
            "asset_id": m.asset_id,
            "family": m.family,
            "motif_class": m.motif_class,
            "site": m.site,
            "region": m.region,
            "country": m.country,
            "period": m.period,
            "jigsaw_tier": m.jigsaw_tier,
            "license": m.license,
            "creator": m.creator,
            "source_url": m.source_url,
            "aligned_iou": per_piece_iou[i],
            "sdf_chamfer": per_piece_chamfer[i],
            "is_edge_piece": face.face_id in edge_faces,
            "seed_mm": list(seeds_mm[i]),
        })

    elapsed = time.perf_counter() - t0
    logs.append(
        f"Mean aligned IoU={np.mean(per_piece_iou):.3f} (min={np.min(per_piece_iou):.3f}), "
        f"mean Chamfer={np.mean(per_piece_chamfer):.3f}, elapsed={elapsed:.2f}s"
    )

    return GeodesicSolution(
        partition=final_partition,
        slowness_grid=final_s_hi,
        labels_inside=final_labels,
        seeds_mm=seeds_mm,
        poses=final_poses_hi,
        per_piece_iou=per_piece_iou,
        per_piece_chamfer=per_piece_chamfer,
        loss_history=loss_history,
        elapsed_sec=elapsed,
        logs=logs,
        sem_loss_history=sem_loss_history,
    )
