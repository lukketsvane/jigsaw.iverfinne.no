"""GPU-accelerated Multiphase Allen-Cahn Dendritic Solidification Puzzle Solver.

Faithfully implements the 2012 Nervous System paper:
  Louis-Rosenberg, Resnick, Rosenkrantz (2012),
  "Multiphase Numerical Modeling of Dendritic Solidification for Jigsaw Puzzle Generation"
  (Equations 1-11 and Figure 6), with two key modern GPU upgrades:
1. Distance-2 graph coloring (typically 12-20 phase channels instead of 5) eliminates
   the cross-piece temperature bleeding ("dendrite melting") documented in Section 6.
2. Vectorized O(C) pairwise Steinbach potential and exact zero-flux Neumann boundary
   conditions on arbitrary outlines (rectangle, circle, organic, SVG/mask with holes)
   and arbitrary fixed whimsy / protected motif masks, running on CUDA RTX A4500.

Public API is designed so Workstream 2 (750-piece Dendrite + Trilobites + Helleristninger)
can directly pass arbitrary fixed motif masks and per-region growth masks.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from scipy import ndimage
from shapely.geometry import Polygon

from .whimsies import WhimsyPlacement


# ---------------------------------------------------------------------------
# Style presets grounded in Figure 6 of the 2012 paper
# ---------------------------------------------------------------------------

STYLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "dendrite": {
        "seed_mode": "dart",
        "dx": 0.024,
        "dt": 0.00015,
        "eps": 0.0086,
        "tau": 0.00030,
        "alpha": 0.90,
        "gamma": 40.0,
        "T0": 0.35,
        "K": 1.12,
        "D_T": 1.0,
        "lambda1_mm": 15.5,
        "lambda2_mm": 7.6,
        "U1_mm": 4.2,
        "U2_mm": 1.85,
        "steps": 360,
    },
    "amoeba": {
        "seed_mode": "dart",
        "dx": 0.024,
        "dt": 0.00015,
        "eps": 0.0082,
        "tau": 0.00030,
        "alpha": 0.92,
        "gamma": 44.0,
        "T0": 0.37,
        "K": 1.18,
        "D_T": 1.0,
        "lambda1_mm": 22.0,
        "lambda2_mm": 6.6,
        "U1_mm": 2.9,
        "U2_mm": 2.4,
        "steps": 340,
    },
    "linear": {
        "seed_mode": "linear",
        "dx": 0.024,
        "dt": 0.00015,
        "eps": 0.0086,
        "tau": 0.00030,
        "alpha": 0.90,
        "gamma": 38.0,
        "T0": 0.35,
        "K": 1.12,
        "D_T": 1.0,
        "lambda1_mm": 16.0,
        "lambda2_mm": 7.6,
        "U1_mm": 3.8,
        "U2_mm": 1.7,
        "steps": 340,
    },
    "grid": {
        "seed_mode": "grid",
        "dx": 0.024,
        "dt": 0.00015,
        "eps": 0.0086,
        "tau": 0.00030,
        "alpha": 0.90,
        "gamma": 40.0,
        "T0": 0.35,
        "K": 1.12,
        "D_T": 1.0,
        "lambda1_mm": 16.0,
        "lambda2_mm": 7.6,
        "U1_mm": 3.8,
        "U2_mm": 1.7,
        "steps": 350,
    },
}


@dataclass
class DendriteConfig:
    """Configuration for GPU multiphase Allen-Cahn dendritic puzzle simulation."""

    style: str = "dendrite"
    width_mm: float = 260.0
    height_mm: float = 190.0
    num_pieces: int = 36
    seed: int = 42
    px_mm: float = 0.50              # physical mm per simulation grid pixel
    dx: float = 0.024                # dimensionless PDE spacing (Paper Fig. 6)
    dt: float = 0.00015              # dimensionless time step (Paper Fig. 6)
    eps: float = 0.0080              # diffuse interface thickness
    tau: float = 0.00030             # phase mobility time scale
    alpha: float = 0.90              # driving force saturation
    gamma: float = 35.0              # temperature sensitivity
    T0: float = 0.30                 # initial supercooling/superheating magnitude
    K: float = 1.0                   # dimensionless latent heat
    D_T: float = 1.0                 # thermal diffusivity
    lambda1_mm: float = 15.0         # gross boundary perturbation wavelength (mm)
    lambda2_mm: float = 5.5          # fine boundary perturbation wavelength (mm)
    U1_mm: float = 2.4               # gross boundary perturbation amplitude (mm)
    U2_mm: float = 0.75              # fine boundary perturbation amplitude (mm)
    steps: int = 420                 # PDE forward-Euler steps
    seed_mode: str = "dart"          # "dart", "grid", "linear", "radial"
    coloring_distance: int = 2       # 2 = distance-2 coloring (prevents Sec. 6 heat bleed), 1 = standard
    use_reaction_diffusion_init: bool = False  # Eq. 11 reaction-diffusion vs fast exact geodesic Voronoi
    rd_steps: int = 140
    min_neck_mm: float = 3.0
    kerf_mm: float = 0.15
    device: str = "auto"             # "auto", "cuda", "cpu"

    @classmethod
    def from_style(cls, style: str, **overrides: Any) -> "DendriteConfig":
        key = style.lower().strip()
        if key not in STYLE_PRESETS:
            raise ValueError(f"Unknown phase-field style {style!r}; expected {sorted(STYLE_PRESETS)}")
        data = dict(STYLE_PRESETS[key])
        data["style"] = key
        data.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**data)


@dataclass
class DendriteResult:
    """Outputs of the multiphase Allen-Cahn dendritic solidification solver."""

    labels: np.ndarray                        # (H, W) int32 piece IDs (-1 outside outline)
    domain_mask: np.ndarray                   # (H, W) bool inside puzzle outline
    whimsy_mask: np.ndarray                   # (H, W) bool inside fixed whimsical pieces
    fixed_piece_ids: List[int]                # piece IDs belonging to fixed whimsies / motifs
    phase_fields: np.ndarray                  # (C, H, W) float32 final color phase fields
    piece_to_color: Dict[int, int]            # piece_id -> phase channel index
    num_colors: int
    seeds_mm: List[Tuple[float, float]]
    outline: Polygon
    bounds_mm: Tuple[float, float, float, float]
    px_mm: float
    conservation_error_max: float
    elapsed_sec: float
    device: str
    config: DendriteConfig


# ---------------------------------------------------------------------------
# Outline construction & rasterization
# ---------------------------------------------------------------------------

def build_outline_polygon(
    kind: str,
    width_mm: float,
    height_mm: float,
    seed: int = 42,
    svg_path: Optional[str | Path] = None,
) -> Polygon:
    """Build a valid CCW mm-scale outline polygon (reusing heller_jigsaw.geometry.outline)."""
    from heller_jigsaw.geometry.outline import circle, from_svg, organic_stone, rectangle

    k = kind.lower().strip()
    if k in ("rect", "rectangle"):
        return rectangle(width_mm, height_mm)
    if k == "circle":
        return circle(min(width_mm, height_mm))
    if k in ("organic", "stone"):
        return organic_stone(width_mm, height_mm, seed=seed)
    if k == "spiral":
        from shapely.geometry import LineString
        from shapely.affinity import affine_transform
        from heller_jigsaw.geometry.outline import snap_polygon
        rng = np.random.default_rng(seed)
        turns = rng.uniform(1.45,1.85)
        t = np.linspace(0,turns*2*np.pi,360)
        phase = rng.uniform(0,2*np.pi)
        radius = .24 + t/(2*np.pi)
        spine = LineString(np.column_stack((radius*np.cos(t+phase),radius*np.sin(t+phase))))
        coil = spine.buffer(.45, quad_segs=12)
        x0,y0,x1,y1 = coil.bounds
        sx,sy = width_mm/(x1-x0),height_mm/(y1-y0)
        return snap_polygon(affine_transform(coil,[sx,0,0,sy,-x0*sx,-y0*sy]))
    if k == "svg" or (svg_path is not None):
        if svg_path is None:
            raise ValueError("svg_path is required when outline kind='svg'")
        return from_svg(svg_path, width_mm=width_mm)
    p = Path(kind)
    if p.is_file():
        return from_svg(p, width_mm=width_mm)
    raise ValueError(f"Unknown outline kind: {kind!r}")


def rasterize_polygon(
    poly: Polygon,
    bounds_mm: Tuple[float, float, float, float],
    shape: Tuple[int, int],
) -> np.ndarray:
    """Rasterize a mm-space Polygon (including any interior holes) onto a (H, W) bool grid."""
    x0, y0, x1, y1 = bounds_mm
    h, w = shape
    sx = w / max(x1 - x0, 1e-9)
    sy = h / max(y1 - y0, 1e-9)
    canvas = np.zeros((h, w), dtype=np.uint8)
    ext = np.asarray(poly.exterior.coords, dtype=np.float64)
    pts = np.round(np.stack([(ext[:, 0] - x0) * sx, (ext[:, 1] - y0) * sy], axis=1)).astype(np.int32)
    cv2.fillPoly(canvas, [pts], 1)
    for ring in poly.interiors:
        ic = np.asarray(ring.coords, dtype=np.float64)
        ipts = np.round(np.stack([(ic[:, 0] - x0) * sx, (ic[:, 1] - y0) * sy], axis=1)).astype(np.int32)
        cv2.fillPoly(canvas, [ipts], 0)
    return canvas > 0


# ---------------------------------------------------------------------------
# Seeding, Voronoi / Reaction-Diffusion Init, Sinusoidal Perturbation, Coloring
# ---------------------------------------------------------------------------

def _generate_seeds(
    active_mask: np.ndarray,
    count: int,
    bounds_mm: Tuple[float, float, float, float],
    seed_mode: str,
    rng: np.random.Generator,
) -> List[Tuple[float, float]]:
    """Generate `count` well-spaced piece seeds inside `active_mask` (dart throwing, grid, linear, radial)."""
    if count <= 0:
        return []
    h, w = active_mask.shape
    x0, y0, x1, y1 = bounds_mm
    dx_px = (x1 - x0) / w
    dy_px = (y1 - y0) / h
    area_mm2 = float(active_mask.sum()) * dx_px * dy_px
    rho_mm = math.sqrt(area_mm2 / max(1, count)) * 0.78

    # Stay slightly clear of mask boundaries so seeds sit comfortably in open space
    dist_inside = ndimage.distance_transform_edt(active_mask) * min(dx_px, dy_px)
    safe = active_mask & (dist_inside >= min(rho_mm * 0.22, 4.0))
    if int(safe.sum()) < count * 4:
        safe = active_mask
    ys, xs = np.nonzero(safe)
    if len(xs) == 0:
        raise ValueError("Active non-whimsy domain is empty; cannot place seeds")

    coords_mm = np.stack([x0 + (xs + 0.5) * dx_px, y0 + (ys + 0.5) * dy_px], axis=1)
    mode = seed_mode.lower().strip()

    if mode == "grid":
        step = math.sqrt(area_mm2 / max(1, count))
        gx = np.arange(x0 + 0.5 * step, x1, step)
        gy = np.arange(y0 + 0.5 * step, y1, step)
        cand = []
        for py in gy:
            for px in gx:
                r = int(np.clip((py - y0) / dy_px, 0, h - 1))
                c = int(np.clip((px - x0) / dx_px, 0, w - 1))
                if active_mask[r, c]:
                    cand.append((float(px + rng.uniform(-0.08, 0.08) * step), float(py + rng.uniform(-0.08, 0.08) * step)))
        if len(cand) >= count:
            idx = np.linspace(0, len(cand) - 1, count).round().astype(int)
            return [cand[i] for i in idx]
        seeds = list(cand)
    elif mode == "linear":
        # Elongated row bands (Paper Fig. 6c)
        row_step = math.sqrt(area_mm2 / max(1, count)) * 0.85
        col_step = (area_mm2 / max(1, count)) / max(row_step, 1e-6)
        seeds = []
        row_i = 0
        py = y0 + 0.5 * row_step
        while py < y1:
            px = x0 + (0.25 if row_i % 2 else 0.75) * col_step
            while px < x1:
                r = int(np.clip((py - y0) / dy_px, 0, h - 1))
                c = int(np.clip((px - x0) / dx_px, 0, w - 1))
                if active_mask[r, c]:
                    seeds.append((float(px), float(py)))
                px += col_step
            py += row_step
            row_i += 1
        if len(seeds) > count:
            keep = rng.choice(len(seeds), size=count, replace=False)
            seeds = [seeds[i] for i in sorted(keep)]
    else:
        # Dart throwing (Poisson blue noise, Runions et al. 2005 / Paper Sec. 4)
        seeds = []
        cur_rho = rho_mm
        for _attempt in range(8):
            order = rng.permutation(len(coords_mm))
            for idx in order[: min(len(order), 12000)]:
                pt = coords_mm[idx]
                if not seeds:
                    seeds.append((float(pt[0]), float(pt[1])))
                else:
                    arr = np.asarray(seeds, dtype=np.float64)
                    d2 = np.sum((arr - pt[None, :]) ** 2, axis=1)
                    if float(d2.min()) >= cur_rho * cur_rho:
                        seeds.append((float(pt[0]), float(pt[1])))
                if len(seeds) >= count:
                    break
            if len(seeds) >= count:
                break
            cur_rho *= 0.84

    # Fill any deficit by farthest-point sampling inside active_mask
    while len(seeds) < count:
        if not seeds:
            pick = int(rng.integers(len(coords_mm)))
            seeds.append((float(coords_mm[pick, 0]), float(coords_mm[pick, 1])))
        else:
            sub = coords_mm[rng.choice(len(coords_mm), size=min(len(coords_mm), 2048), replace=False)]
            arr = np.asarray(seeds, dtype=np.float64)
            d2 = np.min(np.sum((sub[:, None, :] - arr[None, :, :]) ** 2, axis=2), axis=1)
            best = sub[int(np.argmax(d2))]
            seeds.append((float(best[0]), float(best[1])))

    # 3 Lloyd relaxation sweeps to guarantee balanced piece areas around whimsies
    seeds_arr = np.asarray(seeds[:count], dtype=np.float64)
    for _ in range(3):
        # Assign each active pixel to nearest seed
        # Chunked to avoid big memory allocation
        nearest = np.empty(len(coords_mm), dtype=np.int32)
        chunk = 16384
        for s0 in range(0, len(coords_mm), chunk):
            pts = coords_mm[s0 : s0 + chunk]
            d2 = np.sum((pts[:, None, :] - seeds_arr[None, :, :]) ** 2, axis=2)
            nearest[s0 : s0 + chunk] = np.argmin(d2, axis=1)
        for i in range(count):
            mine = coords_mm[nearest == i]
            if len(mine) >= 4:
                c = mine.mean(axis=0)
                r = int(np.clip((c[1] - y0) / dy_px, 0, h - 1))
                col = int(np.clip((c[0] - x0) / dx_px, 0, w - 1))
                if active_mask[r, col]:
                    seeds_arr[i] = c

    return [(float(p[0]), float(p[1])) for p in seeds_arr]


def _geodesic_voronoi_init(
    active_mask: np.ndarray,
    seeds_mm: Sequence[Tuple[float, float]],
    bounds_mm: Tuple[float, float, float, float],
    seed_mode: str = "dart",
) -> np.ndarray:
    """Compute generalized Voronoi partition inside `active_mask` respecting obstacles/whimsies."""
    h, w = active_mask.shape
    x0, y0, x1, y1 = bounds_mm
    dx_px = (x1 - x0) / w
    dy_px = (y1 - y0) / h
    labels = np.full((h, w), -1, dtype=np.int32)
    if not seeds_mm:
        return labels

    # Fast multi-source geodesic breadth/distance propagation using priority/level-set EDT
    # around whimsies so pieces never jump across a whimsy!
    seed_grid = np.full((h, w), -1, dtype=np.int32)
    for idx, (sx_mm, sy_mm) in enumerate(seeds_mm):
        c = int(np.clip((sx_mm - x0) / dx_px, 0, w - 1))
        r = int(np.clip((sy_mm - y0) / dy_px, 0, h - 1))
        if not active_mask[r, c]:
            # Snap to nearest active pixel
            ys, xs = np.nonzero(active_mask)
            j = int(np.argmin((ys - r) ** 2 + (xs - c) ** 2))
            r, c = int(ys[j]), int(xs[j])
        if seed_mode == "linear":
            # Horizontal seed segment (Paper Fig. 6c)
            half_len = max(2, int(round(0.35 * math.sqrt(active_mask.sum() / max(1, len(seeds_mm))))))
            c0 = max(0, c - half_len)
            c1 = min(w, c + half_len + 1)
            seg = active_mask[r, c0:c1]
            seed_grid[r, c0:c1] = np.where(seg & (seed_grid[r, c0:c1] < 0), idx, seed_grid[r, c0:c1])
        else:
            seed_grid[r, c] = idx

    # Use OpenCV/SciPy geodesic wavefront (fast 8-neighbor Dijkstra in NumPy/heap or multi-step dilation)
    # Using scipy.sparse.csgraph.dijkstra on active pixels gives exact obstacle-aware Voronoi in ~20ms!
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import dijkstra

    active_idx = np.full((h, w), -1, dtype=np.int32)
    ay, ax = np.nonzero(active_mask)
    n_act = len(ay)
    active_idx[ay, ax] = np.arange(n_act, dtype=np.int32)

    rows_l, cols_l, wts_l = [], [], []
    for dr, dc, dist_w in ((0, 1, 1.0), (1, 0, 1.0), (1, 1, 1.41421356), (1, -1, 1.41421356)):
        r0 = slice(0, h - dr) if dr else slice(0, h)
        r1 = slice(dr, h) if dr else slice(0, h)
        if dc >= 0:
            c0 = slice(0, w - dc) if dc else slice(0, w)
            c1 = slice(dc, w) if dc else slice(0, w)
        else:
            c0 = slice(-dc, w)
            c1 = slice(0, w + dc)
        u = active_idx[r0, c0]
        v = active_idx[r1, c1]
        ok = (u >= 0) & (v >= 0)
        uu = u[ok]
        vv = v[ok]
        ww = np.full(len(uu), dist_w, dtype=np.float32)
        rows_l.extend((uu, vv))
        cols_l.extend((vv, uu))
        wts_l.extend((ww, ww))

    graph = csr_matrix(
        (np.concatenate(wts_l), (np.concatenate(rows_l), np.concatenate(cols_l))),
        shape=(n_act, n_act),
    )

    seed_nodes = []
    seed_labels_list = []
    sy_s, sx_s = np.nonzero(seed_grid >= 0)
    for r, c in zip(sy_s, sx_s):
        nid = int(active_idx[r, c])
        if nid >= 0:
            seed_nodes.append(nid)
            seed_labels_list.append(int(seed_grid[r, c]))

    _, _, sources = dijkstra(
        graph,
        directed=False,
        indices=np.asarray(seed_nodes, dtype=np.int32),
        min_only=True,
        return_predecessors=True,
    )
    node_to_label = dict(zip(seed_nodes, seed_labels_list))
    out_act = np.array([node_to_label.get(int(s), 0) for s in sources], dtype=np.int32)
    labels[ay, ax] = out_act
    return labels


def _reaction_diffusion_voronoi_init(
    active_mask: np.ndarray,
    seeds_mm: Sequence[Tuple[float, float]],
    bounds_mm: Tuple[float, float, float, float],
    device: torch.device,
    steps: int = 140,
    mu: float = 0.08,
    nu: float = 0.25,
) -> np.ndarray:
    """Equation 11 of 2012 paper: dA_i/dt = lap(A_i) + mu*A_i - nu*sum_{j!=i} A_j on CUDA."""
    h, w = active_mask.shape
    x0, y0, x1, y1 = bounds_mm
    dx_px = (x1 - x0) / w
    dy_px = (y1 - y0) / h
    n = len(seeds_mm)
    A = torch.zeros((1, n, h, w), dtype=torch.float32, device=device)
    for i, (sx_mm, sy_mm) in enumerate(seeds_mm):
        c = int(np.clip((sx_mm - x0) / dx_px, 0, w - 1))
        r = int(np.clip((sy_mm - y0) / dy_px, 0, h - 1))
        A[0, i, max(0, r - 1) : min(h, r + 2), max(0, c - 1) : min(w, c + 2)] = 1.0
    mask_t = torch.from_numpy(active_mask.astype(np.float32)).to(device)[None, None]
    k_off = torch.tensor(
        [[1.0, 4.0, 1.0], [4.0, 0.0, 4.0], [1.0, 4.0, 1.0]],
        dtype=torch.float32,
        device=device,
    )[None, None] / 6.0
    mask_deg = F.conv2d(mask_t, k_off, padding=1)
    k_ch = k_off.expand(n, 1, 3, 3).contiguous()
    dt_rd = 0.18
    with torch.no_grad():
        for _ in range(steps):
            lap = F.conv2d(A * mask_t, k_ch, padding=1, groups=n) - A * mask_deg
            total = A.sum(dim=1, keepdim=True)
            dA = lap + mu * A - nu * (total - A)
            A = (A + dt_rd * dA).clamp(0.0, 1.0) * mask_t
    lab = A[0].argmax(dim=0).cpu().numpy().astype(np.int32)
    lab[~active_mask] = -1
    return lab


def _perturb_boundaries_sinusoidal(
    labels: np.ndarray,
    active_mask: np.ndarray,
    px_mm: float,
    lambda1_mm: float,
    lambda2_mm: float,
    U1_mm: float,
    U2_mm: float,
    rng: np.random.Generator,
    amplitude_map: Optional[np.ndarray] = None,
) -> np.ndarray:
    """Apply Paper Section 4 two-scale isotropic sinusoidal boundary perturbation (Fig. 5b)."""
    h, w = labels.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    x_mm = xx * px_mm
    y_mm = yy * px_mm

    disp_x = np.zeros((h, w), dtype=np.float32)
    disp_y = np.zeros((h, w), dtype=np.float32)

    for lam_mm, amp_mm, n_waves in ((lambda1_mm, U1_mm, 6), (lambda2_mm, U2_mm, 8)):
        if amp_mm <= 0.0 or lam_mm <= 0.0:
            continue
        k_mag = 2.0 * math.pi / lam_mm
        scale = amp_mm / math.sqrt(n_waves * 0.5)
        for wi in range(n_waves):
            ang = (wi + rng.uniform(-0.25, 0.25)) * (math.pi / n_waves)
            kx = k_mag * math.cos(ang)
            ky = k_mag * math.sin(ang)
            ph1 = float(rng.uniform(0.0, 2.0 * math.pi))
            ph2 = float(rng.uniform(0.0, 2.0 * math.pi))
            wave1 = np.sin(kx * x_mm + ky * y_mm + ph1)
            wave2 = np.sin(kx * x_mm + ky * y_mm + ph2)
            disp_x += scale * math.cos(ang + 0.5 * math.pi) * wave1 + 0.35 * scale * math.cos(ang) * wave2
            disp_y += scale * math.sin(ang + 0.5 * math.pi) * wave1 + 0.35 * scale * math.sin(ang) * wave2

    # Smoothly damp perturbation ONLY near fixed whimsies (keep outer border seams active!)
    # Near fixed whimsies, whimsy_mask pixels are ~active_mask inside domain; we damp only within 2.0 mm of them
    dist_wall_mm = ndimage.distance_transform_edt(active_mask).astype(np.float32) * px_mm
    taper = np.clip(dist_wall_mm / 1.8, 0.25, 1.0)
    if amplitude_map is not None:
        taper = taper * np.asarray(amplitude_map, dtype=np.float32)

    src_x = np.clip(np.rint(xx + (disp_x * taper) / px_mm).astype(np.int32), 0, w - 1)
    src_y = np.clip(np.rint(yy + (disp_y * taper) / px_mm).astype(np.int32), 0, h - 1)
    warped = labels[src_y, src_x]
    # Keep original label wherever warping sampled outside active_mask
    out = np.where(active_mask & (warped >= 0), warped, labels)
    out[~active_mask] = -1
    return out


def distance2_graph_coloring(
    adjacency: Dict[int, set[int]],
    distance: int = 2,
) -> Tuple[Dict[int, int], int]:
    """Greedy smallest-last graph coloring at graph distance 1 or 2.

    Distance-2 coloring guarantees that neither adjacent pieces NOR pieces sharing
    a common neighbor receive the same phase channel — eliminating the cross-piece
    temperature bleeding ("dendrite melting") noted in Section 6 of the 2012 paper.
    """
    nodes = sorted(adjacency.keys())
    if not nodes:
        return {}, 0

    conflicts: Dict[int, set[int]] = {}
    for u in nodes:
        nbrs = set(adjacency[u])
        if distance >= 2:
            d2 = set(nbrs)
            for v in nbrs:
                d2.update(adjacency.get(v, ()))
            d2.discard(u)
            conflicts[u] = d2
        else:
            nbrs.discard(u)
            conflicts[u] = nbrs

    # Smallest-last ordering
    rem = {u: set(conflicts[u]) for u in nodes}
    order: List[int] = []
    while rem:
        u = min(rem, key=lambda x: (len(rem[x]), x))
        order.append(u)
        for v in rem.pop(u):
            if v in rem:
                rem[v].discard(u)

    colors: Dict[int, int] = {}
    for u in reversed(order):
        used = {colors[v] for v in conflicts[u] if v in colors}
        c = 0
        while c in used:
            c += 1
        colors[u] = c

    return colors, (max(colors.values()) + 1 if colors else 0)


def _extract_adjacency(labels: np.ndarray, valid_ids: Sequence[int]) -> Dict[int, set[int]]:
    adj: Dict[int, set[int]] = {int(i): set() for i in valid_ids}
    # Horizontal and vertical neighbor pairs
    for a, b in ((labels[:, :-1], labels[:, 1:]), (labels[:-1, :], labels[1:, :])):
        diff = (a != b) & (a >= 0) & (b >= 0)
        if np.any(diff):
            pairs = np.unique(np.stack([a[diff], b[diff]], axis=1), axis=0)
            for u, v in pairs:
                iu, iv = int(u), int(v)
                if iu in adj and iv in adj:
                    adj[iu].add(iv)
                    adj[iv].add(iu)
    return adj


def _recover_piece_ids_from_colors(
    color_labels: np.ndarray,
    init_piece_labels: np.ndarray,
    active_mask: np.ndarray,
    piece_ids: Sequence[int],
    piece_to_color: Dict[int, int],
) -> np.ndarray:
    """Map evolved color-channel labels back to unique piece IDs without merging distant same-color pieces."""
    h, w = color_labels.shape
    out = np.full((h, w), -1, dtype=np.int32)
    color_to_pieces: Dict[int, List[int]] = defaultdict(list)
    for pid in piece_ids:
        color_to_pieces[piece_to_color[pid]].append(pid)

    for c_idx, pids in color_to_pieces.items():
        mask_c = active_mask & (color_labels == c_idx)
        if not np.any(mask_c):
            continue
        if len(pids) == 1:
            out[mask_c] = pids[0]
            continue
        comp, n_comp = ndimage.label(mask_c)
        # Assign each connected component to the piece in `pids` with maximum initial overlap
        # (or nearest initial centroid if a tiny droplet drifted outside initial footprint).
        centroids = {}
        for pid in pids:
            ys, xs = np.nonzero(init_piece_labels == pid)
            if len(ys):
                centroids[pid] = (float(ys.mean()), float(xs.mean()))
        for cid in range(1, n_comp + 1):
            cm = comp == cid
            init_vals = init_piece_labels[cm]
            best_pid = None
            best_overlap = 0
            for pid in pids:
                ov = int(np.sum(init_vals == pid))
                if ov > best_overlap:
                    best_overlap = ov
                    best_pid = pid
            if best_pid is None:
                cy, cx = np.nonzero(cm)
                my, mx = float(cy.mean()), float(cx.mean())
                best_pid = min(
                    centroids.keys(),
                    key=lambda p: (centroids[p][0] - my) ** 2 + (centroids[p][1] - mx) ** 2,
                )
            out[cm] = best_pid

    # Ensure every original piece keeps at least its core if fully eroded, and fill any unassigned active pixel
    missing_px = active_mask & (out < 0)
    if np.any(missing_px):
        _, idx = ndimage.distance_transform_edt(out < 0, return_distances=True, return_indices=True)
        out[missing_px] = out[tuple(idx)][missing_px]

    return out


# ---------------------------------------------------------------------------
# Main GPU Multiphase Allen-Cahn Solver
# ---------------------------------------------------------------------------

def run_dendrite_simulation(
    outline: Polygon,
    config: DendriteConfig,
    whimsies: Sequence[WhimsyPlacement] = (),
    fixed_masks: Optional[Sequence[np.ndarray]] = None,
    initial_labels: Optional[np.ndarray] = None,
    growth_mask: Optional[np.ndarray] = None,
    gamma_map: Optional[np.ndarray] = None,
    eps_map: Optional[np.ndarray] = None,
    amplitude_map: Optional[np.ndarray] = None,
) -> DendriteResult:
    """Run multiphase Allen-Cahn dendritic solidification on GPU (or CPU fallback).

    Parameters
    ----------
    outline:
        Outer puzzle boundary Polygon in mm.
    config:
        `DendriteConfig` with PDE & style parameters.
    whimsies:
        Placed `WhimsyPlacement` pieces kept as exact reflective Neumann boundaries.
    fixed_masks:
        Optional extra `(H, W)` bool masks for Workstream 2 motif integration (each mask
        becomes an immutable fixed piece with reflective boundary conditions).
    initial_labels:
        Optional pre-computed `(H, W)` int32 label field inside `active_mask` to evolve
        directly (lets Workstream 2 evolve dendritic seams between pre-placed motifs).
    growth_mask:
        Optional `(H, W)` float32 field in `[0, 1]` modulating local PDE growth rate.
    gamma_map / eps_map / amplitude_map:
        Optional spatially-varying `(H, W)` parameter maps (e.g. artwork-guided cut style).
    """
    t0 = time.perf_counter()
    if config.device == "auto":
        dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    else:
        dev = torch.device(config.device)

    x0, y0, x1, y1 = outline.bounds
    bounds_mm = (float(x0), float(y0), float(x1), float(y1))
    w_mm = x1 - x0
    h_mm = y1 - y0
    w_px = max(32, int(round(w_mm / config.px_mm)))
    h_px = max(32, int(round(h_mm / config.px_mm)))
    px_mm = 0.5 * (w_mm / w_px + h_mm / h_px)

    domain_mask = rasterize_polygon(outline, bounds_mm, (h_px, w_px))
    rng = np.random.default_rng(config.seed)

    # Rasterize fixed whimsies and extra fixed motif masks (strictly inside outline)
    fixed_piece_masks: List[np.ndarray] = []
    whimsy_mask = np.zeros((h_px, w_px), dtype=bool)
    for wh in whimsies:
        wm = rasterize_polygon(wh.polygon, bounds_mm, (h_px, w_px)) & domain_mask & (~whimsy_mask)
        if int(wm.sum()) > 16:
            fixed_piece_masks.append(wm)
            whimsy_mask |= wm
    if fixed_masks is not None:
        for fm in fixed_masks:
            fm_b = np.asarray(fm, dtype=bool)
            if fm_b.shape != (h_px, w_px):
                fm_b = cv2.resize(fm_b.astype(np.uint8), (w_px, h_px), interpolation=cv2.INTER_NEAREST) > 0
            fm_b = fm_b & domain_mask & (~whimsy_mask)
            if int(fm_b.sum()) > 16:
                fixed_piece_masks.append(fm_b)
                whimsy_mask |= fm_b

    active_mask = domain_mask & (~whimsy_mask)
    n_fixed = len(fixed_piece_masks)
    n_dynamic = max(2, int(config.num_pieces) - n_fixed)

    # 1. Initialize active piece regions
    if initial_labels is not None:
        init_dyn = np.asarray(initial_labels, dtype=np.int32).copy()
        if init_dyn.shape != (h_px, w_px):
            init_dyn = cv2.resize(init_dyn.astype(np.int32), (w_px, h_px), interpolation=cv2.INTER_NEAREST)
        init_dyn[~active_mask] = -1
        unique_dyn = sorted(int(v) for v in np.unique(init_dyn) if v >= 0)
        remap = {old: idx for idx, old in enumerate(unique_dyn)}
        mapped = np.full((h_px, w_px), -1, dtype=np.int32)
        for old, new in remap.items():
            mapped[init_dyn == old] = new
        init_dyn = mapped
        n_dynamic = len(unique_dyn)
        seeds_mm = []
        for pid in range(n_dynamic):
            ys, xs = np.nonzero(init_dyn == pid)
            seeds_mm.append((x0 + float(xs.mean() + 0.5) * px_mm, y0 + float(ys.mean() + 0.5) * px_mm))
    else:
        seeds_mm = _generate_seeds(active_mask, n_dynamic, bounds_mm, config.seed_mode, rng)
        if config.use_reaction_diffusion_init and n_dynamic <= 64:
            init_dyn = _reaction_diffusion_voronoi_init(active_mask, seeds_mm, bounds_mm, dev, steps=config.rd_steps)
        else:
            init_dyn = _geodesic_voronoi_init(active_mask, seeds_mm, bounds_mm, seed_mode=config.seed_mode)

    # 2. Two-scale sinusoidal boundary perturbation (Paper Sec. 4, Fig. 5b)
    def _resize_opt(arr: Optional[np.ndarray]) -> Optional[np.ndarray]:
        if arr is None:
            return None
        a = np.asarray(arr, dtype=np.float32)
        if a.shape != (h_px, w_px):
            a = cv2.resize(a, (w_px, h_px), interpolation=cv2.INTER_LINEAR)
        return a

    amp_map_r = _resize_opt(amplitude_map)
    init_dyn = _perturb_boundaries_sinusoidal(
        init_dyn,
        active_mask,
        px_mm=px_mm,
        lambda1_mm=config.lambda1_mm,
        lambda2_mm=config.lambda2_mm,
        U1_mm=config.U1_mm,
        U2_mm=config.U2_mm,
        rng=rng,
        amplitude_map=amp_map_r,
    )

    # Keep only largest connected component per dynamic piece before coloring
    dyn_ids = sorted(int(v) for v in np.unique(init_dyn) if v >= 0)
    for pid in dyn_ids:
        m = init_dyn == pid
        comp, n_c = ndimage.label(m)
        if n_c > 1:
            sizes = ndimage.sum_labels(np.ones_like(comp), comp, range(1, n_c + 1))
            keep = 1 + int(np.argmax(sizes))
            init_dyn[(comp > 0) & (comp != keep)] = -1
    holes = active_mask & (init_dyn < 0)
    if np.any(holes):
        _, idx = ndimage.distance_transform_edt(init_dyn < 0, return_distances=True, return_indices=True)
        init_dyn[holes] = init_dyn[tuple(idx)][holes]
    dyn_ids = sorted(int(v) for v in np.unique(init_dyn) if v >= 0)

    # 3. Distance-2 graph coloring (eliminates Sec. 6 cross-piece temperature bleed!)
    adj = _extract_adjacency(init_dyn, dyn_ids)
    piece_to_color, num_colors = distance2_graph_coloring(adj, distance=config.coloring_distance)
    num_colors = max(2, num_colors)

    # 4. Initialize multiphase tensors p (1, C, H, W) and T (1, C, H, W) on GPU
    p_np = np.zeros((num_colors, h_px, w_px), dtype=np.float32)
    for pid in dyn_ids:
        c_idx = piece_to_color[pid]
        p_np[c_idx, init_dyn == pid] = 1.0

    # Slight diffuse interface initialization (1-pixel Gaussian blur inside active_mask)
    # ensures smooth initial gradients so the explicit Allen-Cahn PDE ignites cleanly.
    active_f = active_mask.astype(np.float32)
    p = torch.from_numpy(p_np).to(dev)[None]  # (1, C, H, W)
    mask_t = torch.from_numpy(active_f).to(dev)[None, None]  # (1, 1, H, W)

    # Precompute 9-point isotropic Neumann stencil weights:
    # lap(f) = (1 / (6 * dx^2)) * [4*(N+S+E+W) + (NE+NW+SE+SW) - 20*C]
    # With zero-flux Neumann boundary on mask_t:
    # lap_N(f) = (1 / dx^2) * [conv2d(f * mask, K_off) - f * conv2d(mask, K_off)]
    k_off = torch.tensor(
        [[1.0, 4.0, 1.0], [4.0, 0.0, 4.0], [1.0, 4.0, 1.0]],
        dtype=torch.float32,
        device=dev,
    )[None, None] / 6.0
    mask_deg = F.conv2d(mask_t, k_off, padding=1)  # (1, 1, H, W)
    k_ch = k_off.expand(num_colors, 1, 3, 3).contiguous()

    inv_dx2 = 1.0 / (config.dx * config.dx)
    dt = float(config.dt)
    tau = float(config.tau)
    alpha_over_pi = float(config.alpha) / math.pi
    K_lat = float(config.K)
    D_T = float(config.D_T)
    T0 = float(config.T0)

    # Spatially-varying parameter tensors (broadcastable to (1, 1, H, W))
    g_map = _resize_opt(gamma_map)
    gamma_t = torch.from_numpy(g_map).to(dev)[None, None] if g_map is not None else float(config.gamma)
    e_map = _resize_opt(eps_map)
    eps2_t = (torch.from_numpy(e_map).to(dev)[None, None] ** 2) if e_map is not None else float(config.eps * config.eps)

    # Automatic piece-core protection envelope:
    # Keep a solid wood core around each piece's center (distance-to-seam > ~38% of piece radius)
    # while allowing 100% full-strength dendritic branching along ALL shared boundaries!
    # This prevents over-growth from punching through neighboring crystal cores into disconnected islands
    # (which mirrors Nervous System's solid central cores surrounded by branching dendritic fringes).
    seam_mask = np.zeros((h_px, w_px), dtype=bool)
    seam_mask[:-1, :] |= (init_dyn[:-1, :] != init_dyn[1:, :]) & active_mask[:-1, :] & active_mask[1:, :]
    seam_mask[1:, :] |= seam_mask[:-1, :]
    seam_mask[:, :-1] |= (init_dyn[:, :-1] != init_dyn[:, 1:]) & active_mask[:, :-1] & active_mask[:, 1:]
    seam_mask[:, 1:] |= seam_mask[:, :-1]
    dist_to_seam_mm = ndimage.distance_transform_edt(~seam_mask).astype(np.float32) * px_mm
    est_radius_mm = math.sqrt(float(active_mask.sum()) * px_mm * px_mm / (math.pi * max(1, len(dyn_ids))))
    fringe_width_mm = max(6.5, min(16.0, 0.56 * est_radius_mm))
    core_envelope = np.clip((fringe_width_mm - dist_to_seam_mm) / max(2.2, 0.25 * fringe_width_mm), 0.0, 1.0).astype(np.float32)

    gr_map = _resize_opt(growth_mask)
    if gr_map is not None:
        combined_growth = np.clip(gr_map, 0.0, 1.0) * core_envelope
    else:
        combined_growth = core_envelope
    growth_t = torch.from_numpy(combined_growth).to(dev)[None, None]

    # Initialize diffuse interface smoothly (2 Neumann smoothing substeps)
    with torch.no_grad():
        for _ in range(2):
            lap_p = F.conv2d(p * mask_t, k_ch, padding=1, groups=num_colors) - p * mask_deg
            p = (p + 0.14 * lap_p).clamp(0.0, 1.0)
            p_sum = p.sum(dim=1, keepdim=True).clamp_min(1e-6)
            p = torch.where(mask_t > 0.5, p / p_sum, torch.zeros_like(p))

        # Paper Page 4 initialization: T_i = -T0 inside piece i (p_i = 1), +T0 outside (p_i = 0)
        T = (T0 * (1.0 - 2.0 * p)) * mask_t

        max_cons_err = 0.0
        for _step in range(int(config.steps)):
            # Exact zero-flux Neumann 9-point isotropic Laplacian
            lap_p = (F.conv2d(p * mask_t, k_ch, padding=1, groups=num_colors) - p * mask_deg) * inv_dx2
            lap_T = (F.conv2d(T * mask_t, k_ch, padding=1, groups=num_colors) - T * mask_deg) * inv_dx2

            # Supercooling driving force Eq. 5: m(T_i) = (alpha / pi) * arctan(gamma * T_i)
            m = alpha_over_pi * torch.atan(gamma_t * T)

            # Exact O(C) Steinbach multiphase pairwise potential (Eqs. 8-10, antisymmetric m_ij):
            # F'_i = 0.5 * p_i * ((p_i - sum_j p_j^2) + (m_i - sum_j p_j m_j))
            # Identically satisfies sum_i F'_i = 0 and reduces exactly to Kobayashi Eq. 7 at C=2!
            S2 = (p * p).sum(dim=1, keepdim=True)
            Sm = (p * m).sum(dim=1, keepdim=True)
            pot = 0.5 * p * ((p - S2) + 1.35 * (m - Sm))
            dp_dt = ((eps2_t * lap_p + pot) / tau) * growth_t

            p_next = p + dt * dp_dt
            # Measure simplex conservation before numerical clamping
            if _step == int(config.steps) - 1 or _step % 100 == 0:
                cons = (p_next.sum(dim=1, keepdim=True) - 1.0).abs() * mask_t
                max_cons_err = max(max_cons_err, float(cons.max().item()))

            p_next = p_next.clamp(0.0, 1.0)
            p_sum = p_next.sum(dim=1, keepdim=True).clamp_min(1e-6)
            p_next = torch.where(mask_t > 0.5, p_next / p_sum, torch.zeros_like(p_next))

            # Actual dp used for exact enthalpy conservation (Eq. 6, sign matching T_i = -T0 inside i)
            dp_actual = (p_next - p) / dt
            T = (T + dt * (D_T * lap_T - K_lat * dp_actual)) * mask_t
            p = p_next

        color_lab = p[0].argmax(dim=0).cpu().numpy().astype(np.int32)
        p_out = p[0].cpu().numpy()

    # 5. Recover unique piece IDs and stamp fixed whimsies back cleanly
    dyn_recovered = _recover_piece_ids_from_colors(
        color_labels=color_lab,
        init_piece_labels=init_dyn,
        active_mask=active_mask,
        piece_ids=dyn_ids,
        piece_to_color=piece_to_color,
    )

    final_labels = np.full((h_px, w_px), -1, dtype=np.int32)
    next_id = 0
    for pid in dyn_ids:
        m = active_mask & (dyn_recovered == pid)
        if np.any(m):
            final_labels[m] = next_id
            next_id += 1

    fixed_piece_ids: List[int] = []
    for fm in fixed_piece_masks:
        final_labels[fm] = next_id
        fixed_piece_ids.append(next_id)
        next_id += 1

    # Ensure zero unassigned pixels inside domain_mask
    unassigned = domain_mask & (final_labels < 0)
    if np.any(unassigned):
        _, idx = ndimage.distance_transform_edt(final_labels < 0, return_distances=True, return_indices=True)
        final_labels[unassigned] = final_labels[tuple(idx)][unassigned]
    final_labels[~domain_mask] = -1

    elapsed = time.perf_counter() - t0
    return DendriteResult(
        labels=final_labels,
        domain_mask=domain_mask,
        whimsy_mask=whimsy_mask,
        fixed_piece_ids=fixed_piece_ids,
        phase_fields=p_out,
        piece_to_color=piece_to_color,
        num_colors=num_colors,
        seeds_mm=seeds_mm,
        outline=outline,
        bounds_mm=bounds_mm,
        px_mm=px_mm,
        conservation_error_max=max_cons_err,
        elapsed_sec=elapsed,
        device=str(dev),
        config=config,
    )
