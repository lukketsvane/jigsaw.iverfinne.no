"""GPU-accelerated zero-overlap hole-aware docking & windowed bay-aware Eikonal partitioning for 750+ motifs.

Every piece is placed from an actual Nordic rock-art silhouette (zero filler cells):
1. Center-outward GPU FFT multi-angle docking enforces ZERO core overlap, scores top spatially
   distinct candidates with explicit enclosed-hole & perimeter-compactness penalties (matching
   search.py::dock's high-density interlocking geometry at 100x GPU speed), and maintains the
   target landscape aspect ratio without flat walls clipping border silhouettes.
2. Joint GPU SDF compaction pulls motifs deeper into neighbor bays while preserving zero core
   chopping (`overlap ~ 0`).
3. Tight hole-free organic outer boundary hugs border rock-art figures cleanly (zero gaps).
4. GPU windowed bay-aware Eikonal (`bay=18`) hands concavities between legs/horns/prows to
   protruding neighbors so ALL 750 pieces remain crisp, unmistakable helleristninger.
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import shapely
import torch
import torch.nn.functional as F
from scipy import ndimage as ndi
from scipy.fft import next_fast_len
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from ..data.corpus import MotifAsset, load_corpus_catalog, load_motif_asset
from ..geometry.outline import SNAP, snap_polygon
from ..geometry.partition import (
    PlanarPartition,
    _extract_dual_chains,
    _remove_checkerboards,
    _repair_label_grid_connectivity,
    _smooth_and_guard_chains,
    partition_from_polygons,
)

GL = 0.85          # canonical SDF half-extent in unit motif-span coordinates (motif spans [-0.5, 0.5])
GRES = 129         # canonical SDF grid resolution per motif
BIG = 1e4


@dataclass
class PackedLayout:
    """Result of GPU motif packing prior to fine Eikonal partitioning."""

    centres_mm: np.ndarray        # (N, 2) float64
    angles_rad: np.ndarray        # (N,) float64
    sizes_mm: np.ndarray          # (N,) float64 (max span of each motif in mm)
    board_width_mm: float
    board_height_mm: float
    unit_areas: np.ndarray        # (N,) float64
    metrics: Dict[str, Any]
    canonical_sdfs: np.ndarray    # (N, GRES, GRES) float32
    canonical_polys: List[np.ndarray]  # list of (K_i, 2) unit-coord exterior polygons


def get_device(prefer_cuda: bool = True) -> torch.device:
    if prefer_cuda and torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def load_selected_catalog(
    catalog_path: str | Path,
    base_catalog_path: str | Path = "hellerisninger/processed/catalog.json",
    enforce_commercial: bool = True,
) -> Tuple[List[MotifAsset], List[Dict[str, Any]], Path]:
    """Load an ordered motif catalog (either standard catalog.json or curated selected-750-catalog.json)."""
    cpath = Path(catalog_path)
    raw = json.loads(cpath.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        records = (
            raw.get("selected_motifs")
            or raw.get("pieces")
            or raw.get("motifs")
            or raw.get("items")
            or raw.get("records")
            or raw.get("catalog")
            or []
        )
    else:
        raise ValueError(f"Unsupported catalog structure in {cpath}")

    base_rows, base_dir = load_corpus_catalog(base_catalog_path)
    base_by_id = {str(r["asset_id"]): r for r in base_rows}

    motifs: List[MotifAsset] = []
    merged_records: List[Dict[str, Any]] = []
    for idx, rec in enumerate(records):
        aid = str(rec.get("asset_id") or rec.get("motif_id") or rec.get("source_asset_id") or "")
        if not aid:
            raise ValueError(f"Catalog entry {idx} missing asset_id/motif_id")
        merged = dict(base_by_id.get(aid, {}))
        merged.update(rec)
        merged["asset_id"] = aid
        if "archive" in rec and not merged.get("source_archive"):
            merged["source_archive"] = rec["archive"]
        if enforce_commercial:
            lic = str(merged.get("license", "")).lower()
            if not merged.get("commercial_ok", False) or "non-commercial" in lic or "nc-sa" in lic or "by-nc" in lic:
                raise ValueError(f"Entry {idx} ({aid}) fails commercial_ok check (license={merged.get('license')!r})")
        use_dir = cpath.parent
        mask_rel = str(merged.get("mask_path", ""))
        if not (use_dir / mask_rel).exists():
            if Path(mask_rel).exists():
                use_dir = Path(".")
            else:
                use_dir = base_dir
        asset = load_motif_asset(merged, use_dir)
        motifs.append(asset)
        merged_records.append(merged)

    return motifs, merged_records, base_dir


def build_canonical_sdf_bank(
    motifs: Sequence[MotifAsset],
    gres: int = GRES,
    gl: float = GL,
) -> Tuple[np.ndarray, np.ndarray, List[np.ndarray], np.ndarray]:
    """Convert each MotifAsset binary mask into a centered, max-span=1 canonical SDF grid and unit polygon."""
    n = len(motifs)
    bank = np.empty((n, gres, gres), dtype=np.float32)
    unit_areas = np.empty(n, dtype=np.float64)
    compactness = np.empty(n, dtype=np.float64)
    polys: List[np.ndarray] = []

    u = np.linspace(-gl, gl, gres, dtype=np.float32)
    du = (2.0 * gl) / float(gres - 1)

    for i, m in enumerate(motifs):
        fg = (m.mask > 0.5).astype(np.uint8)
        cc, num = ndi.label(fg)
        if num > 1:
            sizes = np.bincount(cc.ravel())
            sizes[0] = 0
            fg = (cc == int(np.argmax(sizes))).astype(np.uint8)
        fg = ndi.binary_fill_holes(fg > 0).astype(np.uint8)

        ys, xs = np.where(fg > 0)
        if len(xs) == 0:
            raise ValueError(f"Empty mask for motif {m.asset_id}")
        x0, x1 = int(xs.min()), int(xs.max())
        y0, y1 = int(ys.min()), int(ys.max())
        bw = float(x1 - x0 + 1)
        bh = float(y1 - y0 + 1)
        span = max(bw, bh)
        cx = 0.5 * (x0 + x1)
        cy = 0.5 * (y0 + y1)

        map_x = np.broadcast_to((cx + u[None, :] * span).astype(np.float32), (gres, gres)).copy()
        map_y = np.broadcast_to((cy + u[:, None] * span).astype(np.float32), (gres, gres)).copy()
        sampled = cv2.remap(
            fg.astype(np.float32),
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0.0,
        )
        bm = sampled >= 0.5
        if not bm.any():
            bm[gres // 2, gres // 2] = True
        d_out = ndi.distance_transform_edt(~bm) * du
        d_in = ndi.distance_transform_edt(bm) * du
        bank[i] = (d_out - d_in).astype(np.float32)
        unit_areas[i] = float(bm.sum()) * (du * du)

        cs, _ = cv2.findContours(bm.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        c_max = max(cs, key=cv2.contourArea)
        area_c = max(float(cv2.contourArea(c_max)), 1.0)
        hull_a = max(float(cv2.contourArea(cv2.convexHull(c_max))), 1.0)
        perim_c = max(float(cv2.arcLength(c_max, True)), 1.0)
        compactness[i] = 0.5 * (area_c / hull_a) + 0.5 * (4.0 * math.pi * area_c / (perim_c * perim_c))

        xy = -gl + c_max.reshape(-1, 2).astype(np.float64) * du
        poly = Polygon(xy).buffer(0).simplify(0.008, preserve_topology=True)
        if poly.geom_type != "Polygon":
            poly = max(getattr(poly, "geoms", [poly]), key=lambda g: g.area)
        polys.append(np.asarray(poly.exterior.coords, dtype=np.float64)[:-1])

    return bank, unit_areas, polys, compactness


def placed_motif_polygons(
    canonical_polys: Sequence[np.ndarray],
    centres_mm: np.ndarray,
    angles_rad: np.ndarray,
    sizes_mm: np.ndarray,
) -> List[Polygon]:
    out: List[Polygon] = []
    for xy, c, th, sz in zip(canonical_polys, centres_mm, angles_rad, sizes_mm):
        co, si = math.cos(float(th)), math.sin(float(th))
        rot = np.array([[co, -si], [si, co]], dtype=np.float64)
        pts = (xy @ rot.T) * float(sz) + np.asarray(c, dtype=np.float64)
        p = Polygon(pts).buffer(0)
        if p.geom_type != "Polygon":
            p = max(getattr(p, "geoms", [p]), key=lambda g: g.area)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# GPU SDF sampling, hole-penalized zero-overlap center-outward FFT docking
# ---------------------------------------------------------------------------

def _grid_phi(
    G: torch.Tensor,
    c: torch.Tensor,
    th: torch.Tensor,
    size: torch.Tensor,
    X: torch.Tensor,
    gl: float = GL,
) -> torch.Tensor:
    sig = size[:, None, None]
    co = th.cos()[:, None, None]
    si = th.sin()[:, None, None]
    dx = X[..., 0] - c[:, None, None, 0]
    dy = X[..., 1] - c[:, None, None, 1]
    u = (co * dx + si * dy) / sig
    v = (-si * dx + co * dy) / sig
    uc = u.clamp(-gl, gl)
    vc = v.clamp(-gl, gl)
    grid = torch.stack([uc, vc], -1) / gl
    g = F.grid_sample(G[:, None], grid, mode="bilinear", padding_mode="border", align_corners=True)[:, 0]
    return sig * (g + ((u - uc) ** 2 + (v - vc) ** 2 + 1e-9).sqrt())


def _dock_center_outward_gpu(
    G: torch.Tensor,
    want: torch.Tensor,
    compactness: np.ndarray,
    aspect_wh: float,
    seed: int = 42,
    px: float = 1.25,
    n_angles: int = 36,
    radial_weight: float = 1.45,
    hole_weight: float = 14.0,
    perim_weight: float = 0.35,
    top_candidates: int = 28,
    order_mode: str = "interleaved",
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, Tuple[float, float]]:
    """Center-outward ZERO-OVERLAP GPU FFT docking with per-angle enclosed-hole & perimeter verification.

    Combines CUDA FFT global multi-angle contact scoring with per-angle top candidate extraction
    (matching ``production/search.py::dock``'s angle diversity at GPU speed) and local hole-fill /
    perimeter screening so pieces nest flush without forming empty lakes or dangling peninsulas.
    """
    t0 = time.perf_counter()
    dev = G.device
    if dev.type == "cuda":
        torch.backends.cuda.cufft_plan_cache.max_size = 16
    n = len(G)
    rng = np.random.default_rng(seed)

    # Generous open canvas (no hard rectangular walls clipping border pieces)
    est_area = float((want.cpu().numpy() ** 2).sum()) * 0.45
    est_h = max(160.0, math.sqrt(est_area / max(aspect_wh, 0.5)) * 1.9)
    est_w = max(200.0, est_h * aspect_wh)
    w_grid = int(math.ceil(est_w / px)) + 80
    h_grid = int(math.ceil(est_h / px)) + 80
    cy_grid = h_grid // 2
    cx_grid = w_grid // 2

    occ_np = np.zeros((h_grid, w_grid), dtype=bool)
    c_out = np.zeros((n, 2), dtype=np.float64)
    th_out = np.zeros(n, dtype=np.float64)
    sz_out = want.cpu().numpy().astype(np.float64).copy()

    jittered_comp = compactness + rng.uniform(-0.05, 0.05, size=n)
    if order_mode == "interleaved":
        # Interleave compact and concave motifs so deep bays open early for nesting AND outer rim stays crisp
        s_idx = np.argsort(-jittered_comp)
        hi = s_idx[: n // 2].tolist()
        lo = s_idx[n // 2 :][::-1].tolist()
        order: List[int] = []
        for a_i, b_i in zip(hi, lo):
            order.append(a_i)
            order.append(b_i)
        if len(lo) > len(hi):
            order.extend(lo[len(hi) :])
    else:
        order = np.argsort(-jittered_comp).tolist()

    max_span = float(want.max())
    kh = int(math.ceil((0.55 * max_span + 3.0) / px))
    off = torch.arange(-kh, kh + 1, device=dev, dtype=torch.float32) * px
    yy_k, xx_k = torch.meshgrid(off, off, indexing="ij")
    X_k = torch.stack([xx_k, yy_k], -1)
    k_side = 2 * kh + 1

    r_min = r_max = cy_grid
    c_min = c_max = cx_grid
    margin_px = kh + 8
    dil_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    for step_idx, idx in enumerate(order):
        sz_i = float(sz_out[idx])
        if step_idx == 0:
            ang0 = float(rng.uniform(0.0, 2.0 * math.pi))
            c_out[idx] = [(cx_grid + 0.5) * px, (cy_grid + 0.5) * px]
            th_out[idx] = ang0
            phi0 = _grid_phi(
                G[idx : idx + 1],
                torch.zeros(1, 2, device=dev),
                torch.tensor([ang0], device=dev, dtype=torch.float32),
                torch.tensor([sz_i], device=dev, dtype=torch.float32),
                X_k[None],
            )[0]
            m0 = (phi0 <= 0.0).cpu().numpy()
            occ_np[cy_grid - kh : cy_grid + kh + 1, cx_grid - kh : cx_grid + kh + 1] |= m0
            ys, xs = np.where(occ_np)
            r_min, r_max = int(ys.min()), int(ys.max())
            c_min, c_max = int(xs.min()), int(xs.max())
            continue

        y0 = max(kh + 2, r_min - margin_px)
        y1 = min(h_grid - kh - 2, r_max + margin_px + 1)
        x0 = max(kh + 2, c_min - margin_px)
        x1 = min(w_grid - kh - 2, c_max + margin_px + 1)
        reg = occ_np[y0 - kh : y1 + kh, x0 - kh : x1 + kh]

        edt = ndi.distance_transform_edt(~reg).astype(np.float32)
        near = np.where(reg, 0.0, np.exp(-edt / (2.6 / px))).astype(np.float32)
        if step_idx >= 4:
            grown_reg = ndi.binary_dilation(reg, iterations=max(2, int(round(3.5 / px))))
            pockets = ndi.binary_fill_holes(grown_reg) & (~reg)
            near = near + 2.6 * pockets.astype(np.float32)

        occ_t = torch.as_tensor(reg.astype(np.float32), device=dev)
        near_t = torch.as_tensor(near, device=dev)
        Hp, Wp = reg.shape
        out_h = Hp - k_side + 1
        out_w = Wp - k_side + 1
        # Quantize FFT dimensions to multiples of 64 so cuFFT reuses ~6 cached plans instead of 100+
        fft_s = (next_fast_len(((Hp + 63) // 64) * 64), next_fast_len(((Wp + 63) // 64) * 64))

        angles = (torch.arange(n_angles, device=dev, dtype=torch.float32) + float(rng.random())) * (2.0 * math.pi / n_angles)
        phi_k = _grid_phi(
            G[idx : idx + 1].expand(n_angles, -1, -1),
            torch.zeros(n_angles, 2, device=dev),
            angles,
            torch.full((n_angles,), sz_i, device=dev, dtype=torch.float32),
            X_k[None].expand(n_angles, -1, -1, -1),
        )
        # Strict core mask (forbids >0.25mm overlap while closing grid quantization gaps) + tight skin band
        mask_k = (phi_k <= 0.0).float()
        core_k = (phi_k <= -0.25).float()
        skin_k = ((phi_k > 0.0) & (phi_k <= 2.6)).float()

        Cf = torch.fft.rfft2(core_k, s=fft_s).conj()
        Mf = torch.fft.rfft2(mask_k, s=fft_s).conj()
        Sf = torch.fft.rfft2(skin_k, s=fft_s).conj()
        Of = torch.fft.rfft2(occ_t, s=fft_s)
        Nf = torch.fft.rfft2(near_t, s=fft_s)

        ovl = torch.fft.irfft2(Cf * Of[None], s=fft_s)[:, :out_h, :out_w]
        contact = torch.fft.irfft2(Mf * Nf[None] + 0.85 * Sf * Of[None], s=fft_s)[:, :out_h, :out_w]

        yy_g = (torch.arange(out_h, device=dev, dtype=torch.float32) + y0 - cy_grid) * aspect_wh
        xx_g = torch.arange(out_w, device=dev, dtype=torch.float32) + x0 - cx_grid
        rad = torch.hypot(yy_g[:, None], xx_g[None, :])

        # Normalize radial penalty by pixel scale so it matches contact scale cleanly
        score = contact - (radial_weight * rad[None])
        valid = (ovl < 0.25) & (contact > 4.0)
        if not bool(valid.any()):
            valid = ovl < 0.25
        score_w = torch.where(valid, score, torch.full_like(score, -1e9))

        # Vectorized per-angle top candidate extraction so hole/perimeter screening tests full angle diversity
        flat_s = score_w.reshape(n_angles, -1)
        best_vals, best_pos = flat_s.max(dim=1)
        bv_cpu = best_vals.cpu().numpy()
        bp_cpu = best_pos.cpu().numpy()
        cands: List[Tuple[float, int, int, int]] = []
        for ak in range(n_angles):
            v_best = float(bv_cpu[ak])
            if v_best > -1e8:
                pos = int(bp_cpu[ak])
                cands.append((v_best, ak, pos // out_w, pos % out_w))
        cands.sort(key=lambda z: -z[0])
        cands = cands[:top_candidates]

        # Evaluate enclosed-hole + perimeter compactness on a fast local crop around each candidate!
        masks_cpu = (phi_k <= 0.0).cpu().numpy()
        best_choice = None
        patch_r = kh + int(round(24.0 / px))
        for v_base, ak, br, bc in cands:
            gy = y0 + br
            gx = x0 + bc
            ly0, ly1 = max(0, gy - patch_r), min(h_grid, gy + patch_r + 1)
            lx0, lx1 = max(0, gx - patch_r), min(w_grid, gx + patch_r + 1)
            loc = occ_np[ly0:ly1, lx0:lx1].copy()
            # Stamp candidate into local patch
            sy0 = gy - kh - ly0
            sx0 = gx - kh - lx0
            loc[sy0 : sy0 + k_side, sx0 : sx0 + k_side] |= masks_cpu[ak]
            # Downsample 2x for ultra-fast hole/perimeter evaluation (~0.15ms)
            ds_w = max(4, loc.shape[1] // 2)
            ds_h = max(4, loc.shape[0] // 2)
            sm = (cv2.resize(loc.astype(np.float32), (ds_w, ds_h), interpolation=cv2.INTER_AREA) > 0.25).astype(np.uint8)
            grown = cv2.dilate(sm, dil_k, iterations=1) > 0
            filled = ndi.binary_fill_holes(grown)
            hole_px = int((filled & (~grown)).sum())
            perim_px = int((grown & (sm == 0)).sum())
            v_adj = v_base - hole_weight * hole_px - perim_weight * perim_px
            if best_choice is None or v_adj > best_choice[0]:
                best_choice = (v_adj, ak, gy, gx)

        _, ak_win, gy_win, gx_win = best_choice
        ang_best = float(angles[ak_win])
        c_out[idx] = [(gx_win + 0.5) * px, (gy_win + 0.5) * px]
        th_out[idx] = ang_best
        occ_np[gy_win - kh : gy_win + kh + 1, gx_win - kh : gx_win + kh + 1] |= masks_cpu[ak_win]
        r_min = min(r_min, gy_win - kh)
        r_max = max(r_max, gy_win + kh)
        c_min = min(c_min, gx_win - kh)
        c_max = max(c_max, gx_win + kh)

        if log and (step_idx + 1) % 150 == 0:
            log(f"  docked {step_idx + 1}/{n} motifs ({time.perf_counter() - t0:.1f}s)")

    ys, xs = np.where(occ_np)
    ox = float(xs.min()) * px - 10.0
    oy = float(ys.min()) * px - 10.0
    bw = float(xs.max() - xs.min() + 1) * px + 20.0
    bh = float(ys.max() - ys.min() + 1) * px + 20.0
    c_out[:, 0] -= ox
    c_out[:, 1] -= oy

    if log:
        crop_occ = occ_np[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        hull_occ = ndi.binary_fill_holes(ndi.binary_closing(ndi.binary_dilation(crop_occ, iterations=1), iterations=3))
        org_fill = float(crop_occ.sum()) / max(1.0, float(hull_occ.sum()))
        log(
            f"  zero-overlap hole-aware docking done: {n} motifs, bounds={bw:.1f}x{bh:.1f}mm, "
            f"organic_fill={org_fill:.3f} ({time.perf_counter() - t0:.1f}s)"
        )

    if dev.type == "cuda":
        torch.backends.cuda.cufft_plan_cache.clear()
        torch.cuda.empty_cache()

    return (
        torch.as_tensor(c_out, dtype=torch.float32, device=dev),
        torch.as_tensor(th_out, dtype=torch.float32, device=dev),
        torch.as_tensor(sz_out, dtype=torch.float32, device=dev),
        (bw, bh),
    )


def _compact_zero_overlap_gpu(
    G: torch.Tensor,
    c: torch.Tensor,
    th: torch.Tensor,
    size: torch.Tensor,
    s_lo: torch.Tensor,
    s_hi: torch.Tensor,
    W: float,
    H: float,
    aspect_wh: float,
    steps: int = 200,
    px: float = 1.25,
    ovl_lo: float = 14.0,
    ovl_hi: float = 52.0,
    rad_weight: float = 4.0,
    kiss_mm: float = 0.30,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, Tuple[float, float]]:
    """Monotonic shrinking-hull GPU SDF compaction squeezing dead interstitial moats without core chopping."""
    dev = G.device
    w = int(math.ceil(W / px)) + 8
    h = int(math.ceil(H / px)) + 8
    win = 2 * int(math.ceil((0.55 * float(s_hi.max()) + 8.0) / px))
    off = torch.arange(-win // 2, win // 2, device=dev, dtype=torch.long)
    oy, ox = torch.meshgrid(off, off, indexing="ij")

    want_ref = size.detach().clone()
    c_opt = c.clone().requires_grad_(True)
    th_opt = th.clone().requires_grad_(True)
    frac0 = ((size - s_lo) / (s_hi - s_lo).clamp_min(1e-3)).clamp(0.05, 0.95)
    a_opt = torch.logit(frac0).requires_grad_(True)
    opt = torch.optim.Adam(
        [
            {"params": [c_opt], "lr": 0.50},
            {"params": [th_opt], "lr": 0.014},
            {"params": [a_opt], "lr": 0.075},
        ]
    )
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, steps), eta_min=0.05)
    center = torch.tensor([W * 0.5, H * 0.5], device=dev, dtype=torch.float32)
    hull_w: Optional[torch.Tensor] = None

    for it in range(steps):
        f = it / max(1, steps - 1)
        ovl_weight = ovl_lo * ((ovl_hi / max(ovl_lo, 1e-3)) ** f)
        sz = s_lo + (s_hi - s_lo) * torch.sigmoid(a_opt)
        jit = (torch.rand(2, device=dev) - 0.5) * px
        ci = torch.floor(c_opt.detach() / px).long()
        col = ci[:, 0, None, None] + ox[None]
        row = ci[:, 1, None, None] + oy[None]
        X = torch.stack([(col.float() + 0.5) * px + jit[0], (row.float() + 0.5) * px + jit[1]], -1)
        phi = _grid_phi(G, c_opt, th_opt, sz, X)
        ok = (col >= 0) & (col < w) & (row >= 0) & (row < h)
        flat = torch.where(ok, row * w + col, torch.zeros_like(col)).reshape(-1)
        vals = torch.where(ok, phi, torch.full_like(phi, BIG)).reshape(-1)
        m1 = torch.full((h * w,), 12.0, device=dev).scatter_reduce(0, flat, vals, "amin", include_self=True)
        rest = torch.where(vals.detach() <= m1.detach()[flat], torch.full_like(vals, BIG), vals)
        m2 = torch.full((h * w,), BIG, device=dev).scatter_reduce(0, flat, rest, "amin", include_self=True)

        if hull_w is None or it % 40 == 0:
            occ_cur = (m1.detach().reshape(h, w) <= 0.0).cpu().numpy()
            hull_cur = ndi.binary_fill_holes(ndi.binary_closing(occ_cur, iterations=3))
            hull_w = torch.as_tensor(hull_cur.astype(np.float32), device=dev).reshape(-1)

        # Monotonic gap loss over all current-hull pixels (moving closer strictly lowers loss)
        gap_in = ((m1.clamp(0.0, 12.0) ** 2) * hull_w).sum()
        ovl = ((-m2 - kiss_mm).clamp_min(0.0) ** 2).sum()
        dc = c_opt - center[None]
        rad_pen = ((dc[:, 0] / max(W, 1.0)) ** 2 + (dc[:, 1] / max(H, 1.0)) ** 2).mean()
        sz_reg = ((sz / want_ref.clamp_min(1.0) - 1.0) ** 2).mean()

        hull_area = max(float(hull_w.sum()) * px * px, 1.0)
        E = (gap_in + ovl_weight * ovl) * (px * px) / hull_area + rad_weight * rad_pen + 0.15 * sz_reg
        opt.zero_grad()
        E.backward()
        opt.step()
        sched.step()
        if log and (it % 50 == 0 or it == steps - 1):
            log(
                f"  compact {it:4d} E={float(E.detach()):.3f} "
                f"overlap_mm2={float((m2.detach() < -kiss_mm).float().sum()) * px * px:.1f}"
            )

    final_c = c_opt.detach()
    final_th = th_opt.detach()
    final_sz = (s_lo + (s_hi - s_lo) * torch.sigmoid(a_opt)).detach()

    # Re-normalize origin & tight bounding box around compacted motifs
    c_np = final_c.cpu().numpy()
    s_np = final_sz.cpu().numpy()
    min_x = float((c_np[:, 0] - 0.55 * s_np).min()) - 10.0
    min_y = float((c_np[:, 1] - 0.55 * s_np).min()) - 10.0
    max_x = float((c_np[:, 0] + 0.55 * s_np).max()) + 10.0
    max_y = float((c_np[:, 1] + 0.55 * s_np).max()) + 10.0
    final_c = final_c - torch.tensor([min_x, min_y], device=dev, dtype=torch.float32)
    if dev.type == "cuda":
        torch.cuda.empty_cache()
    return final_c, final_th, final_sz, (max_x - min_x, max_y - min_y)


def _measure_layout_gpu(
    G: torch.Tensor,
    c: torch.Tensor,
    th: torch.Tensor,
    size: torch.Tensor,
    W: float,
    H: float,
    px: float = 1.0,
) -> Dict[str, float]:
    dev = G.device
    h = int(math.ceil(H / px)) + 4
    w = int(math.ceil(W / px)) + 4
    v1 = torch.full((h, w), BIG, device=dev)
    v2 = torch.full((h, w), BIG, device=dev)
    for i in range(len(G)):
        rad_px = int(math.ceil((0.58 * float(size[i]) + 14.0) / px))
        cr = int(round(float(c[i, 1]) / px))
        cc = int(round(float(c[i, 0]) / px))
        r0, r1 = max(0, cr - rad_px), min(h, cr + rad_px + 1)
        c0, c1 = max(0, cc - rad_px), min(w, cc + rad_px + 1)
        if r1 <= r0 or c1 <= c0:
            continue
        yy, xx = torch.meshgrid(
            (torch.arange(r0, r1, device=dev, dtype=torch.float32) + 0.5) * px,
            (torch.arange(c0, c1, device=dev, dtype=torch.float32) + 0.5) * px,
            indexing="ij",
        )
        lp = _grid_phi(G[i : i + 1], c[i : i + 1], th[i : i + 1], size[i : i + 1], torch.stack([xx, yy], -1)[None])[0]
        sl = (slice(r0, r1), slice(c0, c1))
        V1, V2 = v1[sl], v2[sl]
        better = lp < V1
        second = (~better) & (lp < V2)
        v2[sl] = torch.where(better, V1, torch.where(second, lp, V2))
        v1[sl] = torch.where(better, lp, V1)

    occ = (v1 <= 0.0).cpu().numpy()
    hull = ndi.binary_fill_holes(ndi.binary_closing(ndi.binary_dilation(occ, iterations=1), iterations=4))
    in_hull = torch.as_tensor(hull, device=dev)
    hull_gaps = v1[in_hull].clamp_min(0.0)
    return {
        "organic_covered": float(occ.sum()) / max(1.0, float(hull.sum())),
        "rms_gap_mm": float((hull_gaps ** 2).mean().sqrt()) if hull_gaps.numel() else 0.0,
        "max_gap_mm": float(hull_gaps.max()) if hull_gaps.numel() else 0.0,
        "overlap_mm2": float((v2 < 0.0).float().sum() * (px * px)),
        "max_overlap_depth_mm": float((-v2).clamp_min(0.0).max()),
    }


def pack_motifs_gpu(
    motifs: Sequence[MotifAsset],
    board_width_mm: float = 855.0,
    board_height_mm: float = 600.0,
    seed: int = 42,
    restarts: int = 1,
    target_core_area_mm2: float = 420.0,
    relax_steps: int = 200,
    prefer_cuda: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> PackedLayout:
    """Pack all N rock-art motifs via per-angle hole-aware GPU FFT docking + shrinking-hull SDF compaction."""
    t0 = time.perf_counter()
    dev = get_device(prefer_cuda)
    n = len(motifs)
    bank_np, unit_areas, canon_polys, compactness = build_canonical_sdf_bank(motifs)
    G = torch.as_tensor(bank_np, dtype=torch.float32, device=dev)

    aspect_wh = board_width_mm / max(board_height_mm, 1.0)
    base_spans = np.sqrt(target_core_area_mm2 / np.clip(unit_areas, 0.10, 0.95))
    min_spans = np.maximum(24.0, base_spans * 0.90)
    max_spans = np.minimum(90.0, base_spans * 1.24)
    for i, m in enumerate(motifs):
        raw_neck = m.metrics.get("min_neck_mm_at_60mm")
        if raw_neck is not None and float(raw_neck) > 0:
            need = 60.0 * (2.60 / float(raw_neck))
            min_spans[i] = max(min_spans[i], min(need, max_spans[i] * 0.98))

    s_lo = torch.as_tensor(min_spans, dtype=torch.float32, device=dev)
    s_hi = torch.as_tensor(max_spans, dtype=torch.float32, device=dev)
    want_base = torch.as_tensor(base_spans, dtype=torch.float32, device=dev).clamp(s_lo, s_hi)

    def _layout_score(met: Dict[str, float]) -> float:
        ovl_per_piece = met["overlap_mm2"] / max(1, n)
        depth_pen = max(0.0, met["max_overlap_depth_mm"] - 1.8) * 0.8
        return (1.0 - met["organic_covered"]) * 20.0 + met["rms_gap_mm"] + 0.02 * ovl_per_piece + depth_pen

    best_pack = None
    for r in range(max(1, restarts)):
        tr = time.perf_counter()
        torch.manual_seed(seed + 101 * r)
        np.random.seed(seed + 101 * r)
        c, th, sz, (bw, bh) = _dock_center_outward_gpu(
            G,
            want_base,
            compactness=compactness,
            aspect_wh=aspect_wh,
            seed=seed + 101 * r,
            px=1.25,
            n_angles=36,
            radial_weight=1.45 + 0.25 * (r % 2),
            hole_weight=14.0 + 3.0 * (r % 2),
            top_candidates=28,
            order_mode="interleaved" if (r % 2 == 0) else "desc",
            log=log,
        )
        m_pre = _measure_layout_gpu(G, c, th, sz, bw, bh, px=1.0)
        sc_pre = _layout_score(m_pre)
        m, score = m_pre, sc_pre
        if relax_steps > 0:
            c2, th2, sz2, (bw2, bh2) = _compact_zero_overlap_gpu(
                G,
                c,
                th,
                sz,
                s_lo,
                s_hi,
                bw,
                bh,
                aspect_wh,
                steps=relax_steps,
                log=log if r == 0 else None,
            )
            m_post = _measure_layout_gpu(G, c2, th2, sz2, bw2, bh2, px=1.0)
            sc_post = _layout_score(m_post)
            if sc_post < sc_pre:
                c, th, sz, bw, bh, m, score = c2, th2, sz2, bw2, bh2, m_post, sc_post
        if log:
            log(
                f"  restart {r}: organic_covered={m['organic_covered']:.3f} rms_gap={m['rms_gap_mm']:.2f}mm "
                f"overlap={m['overlap_mm2']:.1f}mm2 max_ovl_depth={m['max_overlap_depth_mm']:.2f}mm "
                f"score={score:.3f} ({time.perf_counter() - tr:.1f}s)"
            )
        if best_pack is None or score < best_pack[0]:
            best_pack = (
                score,
                c.cpu().numpy().astype(np.float64),
                th.cpu().numpy().astype(np.float64),
                sz.cpu().numpy().astype(np.float64),
                bw,
                bh,
                m,
            )

    score, c_np, th_np, sz_np, bw, bh, metrics = best_pack
    metrics["packing_score"] = float(score)
    metrics["packing_seconds"] = round(time.perf_counter() - t0, 2)
    return PackedLayout(
        centres_mm=c_np,
        angles_rad=th_np,
        sizes_mm=sz_np,
        board_width_mm=bw,
        board_height_mm=bh,
        unit_areas=unit_areas,
        metrics=metrics,
        canonical_sdfs=bank_np,
        canonical_polys=canon_polys,
    )


# ---------------------------------------------------------------------------
# GPU windowed bay-aware Eikonal partitioning & organic edge-extended outline
# ---------------------------------------------------------------------------

def _eikonal_solid(
    phi: torch.Tensor,
    slow: torch.Tensor,
    solid: torch.Tensor,
    px: float,
    sweeps: int,
    band: float,
    tol: float = 2e-3,
) -> torch.Tensor:
    hold = (phi <= band) | solid
    Tm = torch.where((phi <= band) & (~solid), slow * phi.clamp_min(0.0), torch.full_like(phi, BIG))
    f = slow * px
    for s in range(0, sweeps, 16):
        Tn = Tm
        for _ in range(min(16, sweeps - s)):
            Tp = F.pad(Tn, (1, 1, 1, 1), value=BIG)
            a = torch.minimum(Tp[:, 1:-1, :-2], Tp[:, 1:-1, 2:])
            b = torch.minimum(Tp[:, :-2, 1:-1], Tp[:, 2:, 1:-1])
            d = a - b
            del Tp
            Tc = torch.where(
                d.abs() < f,
                0.5 * (a + b + torch.sqrt((2.0 * f * f - d * d).clamp_min(1e-8))),
                torch.minimum(a, b) + f,
            )
            del a, b, d
            Tn = torch.where(hold, Tn, torch.minimum(Tn, Tc))
            del Tc
        done = float((Tm - Tn).max()) < tol
        Tm = Tn
        if done:
            break
    return Tm


def _rasterize_convex_hulls_window(
    placed_polys: Sequence[Polygon],
    org: np.ndarray,
    x0: float,
    y0: float,
    sh: int,
    sw: int,
    px: float,
    dev: torch.device,
) -> torch.Tensor:
    b = len(placed_polys)
    out = np.zeros((b, sh, sw), dtype=np.uint8)
    for k, poly in enumerate(placed_polys):
        hull = poly.convex_hull
        coords = np.asarray(hull.exterior.coords, dtype=np.float64)[:-1]
        r0, c0 = int(org[k, 0]), int(org[k, 1])
        pts = np.empty((len(coords), 2), dtype=np.int32)
        pts[:, 0] = np.round((coords[:, 0] - x0) / px - c0 - 0.5).astype(np.int32)
        pts[:, 1] = np.round((coords[:, 1] - y0) / px - r0 - 0.5).astype(np.int32)
        cv2.fillConvexPoly(out[k], pts, 1)
    return torch.as_tensor(out, dtype=torch.bool, device=dev)


def _disk(r: int) -> np.ndarray:
    y, x = np.ogrid[-r : r + 1, -r : r + 1]
    return (x * x + y * y) <= (r * r)


def build_organic_outline(
    occ_mask: np.ndarray,
    x0: float,
    y0: float,
    px: float,
    border_pad_mm: float = 1.2,
    closing_mm: float = 4.0,
    simplify_mm: float = 0.22,
) -> Polygon:
    """Build a tight, hole-free edge-extended organic outer boundary hugging border rock-art motifs."""
    r_pad = max(1, int(round(border_pad_mm / px)))
    r_close = max(1, int(round(closing_mm / px)))
    grown = ndi.binary_dilation(occ_mask, structure=_disk(r_pad))
    solid = ndi.binary_fill_holes(grown | ndi.binary_closing(grown, structure=_disk(r_close)))
    cc, num = ndi.label(solid)
    if num > 1:
        for mult in (1.5, 2.5, 4.0, 6.5, 10.0):
            rc = max(r_close + 2, int(round(closing_mm * mult / px)))
            solid = ndi.binary_fill_holes(grown | ndi.binary_closing(grown, structure=_disk(rc)))
            cc, num = ndi.label(solid)
            if num == 1:
                break
        if num > 1:
            # Never drop any motif island: bridge every minor component to the main body
            sizes = np.bincount(cc.ravel())
            sizes[0] = 0
            main_id = int(np.argmax(sizes))
            main_mask = cc == main_id
            for cid in range(1, num + 1):
                if cid == main_id:
                    continue
                ys_m, xs_m = np.where(main_mask)
                ys_c, xs_c = np.where(cc == cid)
                # Connect centroids/nearest pixels cleanly
                cy_c, cx_c = int(round(ys_c.mean())), int(round(xs_c.mean()))
                dists = (ys_m - cy_c) ** 2 + (xs_m - cx_c) ** 2
                j_min = int(np.argmin(dists))
                cv2.line(solid := solid.astype(np.uint8), (cx_c, cy_c), (int(xs_m[j_min]), int(ys_m[j_min])), 1, thickness=max(3, r_pad * 2 + 1))
                solid = ndi.binary_fill_holes(solid > 0)
                main_mask = solid

    solid_u8 = solid.astype(np.uint8) * 255
    blur = cv2.GaussianBlur(solid_u8, (0, 0), sigmaX=max(1.0, 0.65 / px))
    solid_sm = ndi.binary_fill_holes(blur >= 127)
    cs, _ = cv2.findContours(solid_sm.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    cnt = max(cs, key=cv2.contourArea).reshape(-1, 2).astype(np.float64)
    coords = np.column_stack([x0 + (cnt[:, 0] + 0.5) * px, y0 + (cnt[:, 1] + 0.5) * px])
    poly = Polygon(coords).buffer(0).simplify(simplify_mm, preserve_topology=True)
    if poly.geom_type != "Polygon":
        poly = max(getattr(poly, "geoms", [poly]), key=lambda g: g.area)
    if poly.interiors:
        poly = Polygon(poly.exterior.coords).buffer(0)
    return snap_polygon(orient(poly, 1.0), snap=SNAP)


def partition_gpu_750(
    layout: PackedLayout,
    motifs: Sequence[MotifAsset],
    metadata_records: Sequence[Dict[str, Any]],
    px: float = 0.45,
    bay: float = 18.0,
    margin_mm: float = 28.0,
    border_pad_mm: float = 1.2,
    closing_mm: float = 4.0,
    smooth_iters: int = 3,
    simplify_tol_mm: float = 0.22,
    batch_size: int = 48,
    prefer_cuda: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[PlanarPartition, List[Polygon]]:
    """Run GPU windowed bay-aware Eikonal partitioning and extract an exact common-line PlanarPartition."""
    t0 = time.perf_counter()
    dev = get_device(prefer_cuda)
    n = len(motifs)

    placed_polys = placed_motif_polygons(
        layout.canonical_polys, layout.centres_mm, layout.angles_rad, layout.sizes_mm
    )
    all_bounds = np.array([p.bounds for p in placed_polys], dtype=np.float64)
    pad_box = max(border_pad_mm + closing_mm + 6.0, 14.0)
    x0 = math.floor((float(all_bounds[:, 0].min()) - pad_box) / px) * px
    y0 = math.floor((float(all_bounds[:, 1].min()) - pad_box) / px) * px
    x1 = math.ceil((float(all_bounds[:, 2].max()) + pad_box) / px) * px
    y1 = math.ceil((float(all_bounds[:, 3].max()) + pad_box) / px) * px

    w = int(round((x1 - x0) / px))
    h = int(round((y1 - y0) / px))
    max_span = float(layout.sizes_mm.max())
    sh = min(h, int(math.ceil((max_span + 2.0 * margin_mm) / px)) + 4)
    sw = min(w, int(math.ceil((max_span + 2.0 * margin_mm) / px)) + 4)
    if log:
        log(f"Partitioning {n} motifs on {w}x{h} grid (px={px}mm, window={sw}x{sh}, bay={bay})")

    G_dev = torch.as_tensor(layout.canonical_sdfs, dtype=torch.float32, device=dev)
    c_dev = torch.as_tensor(layout.centres_mm, dtype=torch.float32, device=dev)
    th_dev = torch.as_tensor(layout.angles_rad, dtype=torch.float32, device=dev)
    sz_dev = torch.as_tensor(layout.sizes_mm, dtype=torch.float32, device=dev)

    org = np.zeros((n, 2), dtype=np.int32)
    gmin = torch.full((h, w), BIG, dtype=torch.float32, device=dev)
    for i in range(n):
        cx, cy = float(layout.centres_mm[i, 0]), float(layout.centres_mm[i, 1])
        r0 = int(np.clip(round((cy - y0) / px - sh * 0.5), 0, h - sh))
        c0 = int(np.clip(round((cx - x0) / px - sw * 0.5), 0, w - sw))
        org[i] = (r0, c0)

    with torch.no_grad():
        for b0 in range(0, n, batch_size):
            b1 = min(n, b0 + batch_size)
            bs = b1 - b0
            rows_b = torch.arange(sh, device=dev, dtype=torch.float32)[None, :, None] + torch.as_tensor(org[b0:b1, 0], device=dev, dtype=torch.float32)[:, None, None]
            cols_b = torch.arange(sw, device=dev, dtype=torch.float32)[None, None, :] + torch.as_tensor(org[b0:b1, 1], device=dev, dtype=torch.float32)[:, None, None]
            X = torch.stack([x0 + (cols_b.expand(bs, sh, sw) + 0.5) * px, y0 + (rows_b.expand(bs, sh, sw) + 0.5) * px], -1)
            phi_b = _grid_phi(G_dev[b0:b1], c_dev[b0:b1], th_dev[b0:b1], sz_dev[b0:b1], X)
            for j in range(bs):
                r0, c0 = int(org[b0 + j, 0]), int(org[b0 + j, 1])
                gmin[r0 : r0 + sh, c0 : c0 + sw] = torch.minimum(gmin[r0 : r0 + sh, c0 : c0 + sw], phi_b[j])
            del phi_b, X

        occ_np = (gmin <= 0.0).cpu().numpy()

        v1 = torch.full((h, w), BIG, dtype=torch.float32, device=dev)
        l1 = torch.full((h, w), -1, dtype=torch.int32, device=dev)
        sweeps = int(2.2 * (margin_mm * (2.5 if bay > 0 else 1.0)) / px) + 8
        for b0 in range(0, n, batch_size):
            b1 = min(n, b0 + batch_size)
            bs = b1 - b0
            rows_b = torch.arange(sh, device=dev, dtype=torch.float32)[None, :, None] + torch.as_tensor(org[b0:b1, 0], device=dev, dtype=torch.float32)[:, None, None]
            cols_b = torch.arange(sw, device=dev, dtype=torch.float32)[None, None, :] + torch.as_tensor(org[b0:b1, 1], device=dev, dtype=torch.float32)[:, None, None]
            X = torch.stack([x0 + (cols_b.expand(bs, sh, sw) + 0.5) * px, y0 + (rows_b.expand(bs, sh, sw) + 0.5) * px], -1)
            phi_b = _grid_phi(G_dev[b0:b1], c_dev[b0:b1], th_dev[b0:b1], sz_dev[b0:b1], X)
            if bay > 0.0:
                hull_mask = _rasterize_convex_hulls_window(placed_polys[b0:b1], org[b0:b1], x0, y0, sh, sw, px, dev)
                bay_mask = hull_mask & (phi_b > 0.0)
                slow = 1.0 + bay * bay_mask.float()
                solid = torch.stack(
                    [gmin[int(r) : int(r) + sh, int(c) : int(c) + sw] <= 0.0 for r, c in org[b0:b1]]
                ) & (phi_b > 0.0)
                Fm = torch.where(phi_b <= 0.0, phi_b, _eikonal_solid(phi_b, slow, solid, px, sweeps, 1.2 * px))
                del hull_mask, bay_mask, slow, solid
            else:
                Fm = phi_b

            for j in range(bs):
                idx = b0 + j
                r0, c0 = int(org[idx, 0]), int(org[idx, 1])
                sl = (slice(r0, r0 + sh), slice(c0, c0 + sw))
                f_j = Fm[j]
                better = f_j < v1[sl]
                v1[sl] = torch.where(better, f_j, v1[sl])
                l1[sl] = torch.where(better, torch.full_like(l1[sl], idx), l1[sl])
            del phi_b, Fm, X

        labels_full = l1.cpu().numpy()

    outline = build_organic_outline(
        occ_np, x0=x0, y0=y0, px=px, border_pad_mm=border_pad_mm, closing_mm=closing_mm, simplify_mm=simplify_tol_mm
    )
    ox0, oy0, ox1, oy1 = outline.bounds
    outline = snap_polygon(shapely.affinity.translate(outline, -ox0, -oy0), snap=SNAP)
    placed_polys_shifted = [shapely.affinity.translate(p, -ox0, -oy0) for p in placed_polys]

    c_start = max(0, int(math.floor((ox0 - x0) / px)) - 2)
    r_start = max(0, int(math.floor((oy0 - y0) / px)) - 2)
    c_end = min(w, int(math.ceil((ox1 - x0) / px)) + 3)
    r_end = min(h, int(math.ceil((oy1 - y0) / px)) + 3)
    sub_full = labels_full[r_start:r_end, c_start:c_end].copy()
    sub_x0 = x0 + c_start * px - ox0
    sub_y0 = y0 + r_start * px - oy0
    sh_sub, sw_sub = sub_full.shape

    xs_sub = sub_x0 + (np.arange(sw_sub, dtype=np.float64) + 0.5) * px
    ys_sub = sub_y0 + (np.arange(sh_sub, dtype=np.float64) + 0.5) * px
    inside_mask = np.asarray(shapely.contains_xy(outline, xs_sub[None, :], ys_sub[:, None]), dtype=bool)
    labels_inside = np.where(inside_mask, sub_full, -1)

    missing_in = inside_mask & (labels_inside < 0)
    if np.any(missing_in):
        valid = labels_inside >= 0
        _, nearest_idx = ndi.distance_transform_edt(~valid, return_indices=True)
        labels_inside[missing_in] = labels_inside[nearest_idx[0][missing_in], nearest_idx[1][missing_in]]

    labels_inside = _repair_label_grid_connectivity(labels_inside, inside_mask, n)

    _, ext_idx = ndi.distance_transform_edt(~inside_mask, return_indices=True)
    ext = labels_inside[ext_idx[0], ext_idx[1]]
    pad = 4
    padded = np.pad(ext, pad_width=pad, mode="edge")
    padded = _remove_checkerboards(padded)

    chains_mm = _extract_dual_chains(padded, x0=sub_x0, y0=sub_y0, px=px, pad=pad)
    main_polys: Dict[int, Polygon] = {}
    for sm_it, simp_tol in ((smooth_iters, simplify_tol_mm), (2, 0.16), (1, 0.12), (0, 0.0)):
        smoothed_chains = _smooth_and_guard_chains(chains_mm, smooth_iters=sm_it, simplify_tol_mm=simp_tol)
        noded = shapely.union_all([outline.exterior, *smoothed_chains], grid_size=SNAP)
        raw_faces = [
            orient(p, 1.0)
            for p in polygonize(noded)
            if p.geom_type == "Polygon" and p.is_valid and p.area > 1e-4
        ]
        inside_faces = [p for p in raw_faces if outline.contains(p.representative_point())]

        by_label: Dict[int, List[Polygon]] = defaultdict(list)
        unmatched_faces: List[Polygon] = []
        for f in inside_faces:
            rp = f.representative_point()
            col = int(np.clip(math.floor((rp.x - sub_x0) / px), 0, sw_sub - 1))
            row = int(np.clip(math.floor((rp.y - sub_y0) / px), 0, sh_sub - 1))
            lbl = int(ext[row, col])
            if 0 <= lbl < n:
                by_label[lbl].append(f)
            else:
                unmatched_faces.append(f)

        # If any label was missed by rep-point voting, rescue via silhouette overlap with placed_polys_shifted
        missing_lbls = [lbl for lbl in range(n) if lbl not in by_label]
        if missing_lbls and inside_faces:
            ftree = STRtree(inside_faces)
            for lbl in list(missing_lbls):
                tp = placed_polys_shifted[lbl]
                cands = ftree.query(tp)
                if len(cands):
                    best_fi = int(max(cands, key=lambda j: inside_faces[int(j)].intersection(tp).area))
                    cand_f = inside_faces[best_fi]
                    if cand_f.intersection(tp).area > 0.25 * tp.area:
                        # Reassign this face exclusively to lbl if its previous label has other faces
                        for k_old, flist in by_label.items():
                            if cand_f in flist and len(flist) > 1:
                                flist.remove(cand_f)
                                by_label[lbl].append(cand_f)
                                break

        cand_main: Dict[int, Polygon] = {}
        orphans: List[Polygon] = list(unmatched_faces)
        for lbl in range(n):
            clist = by_label.get(lbl, [])
            if not clist:
                continue
            merged = unary_union(clist) if len(clist) > 1 else clist[0]
            parts = [
                orient(p, 1.0)
                for p in shapely.get_parts(merged)
                if p.geom_type == "Polygon" and p.area > 1e-5
            ]
            parts.sort(key=lambda p: p.area, reverse=True)
            if parts:
                cand_main[lbl] = parts[0]
                orphans.extend(parts[1:])

        if len(cand_main) == n:
            main_polys = cand_main
            break
        if log:
            log(f"  smooth_iters={sm_it} yielded {len(cand_main)}/{n} faces; retrying finer smoothing...")

    if orphans:
        for _ in range(len(orphans) * 2 + 4):
            if not orphans:
                break
            keys = sorted(main_polys.keys())
            vals = [main_polys[k] for k in keys]
            tree = STRtree(vals)
            remaining = []
            progressed = False
            for orph in orphans:
                cands = tree.query(orph.buffer(0.05))
                best_lbl = -1
                best_len = -1.0
                for ci in cands:
                    lbl = keys[int(ci)]
                    mp = main_polys[lbl]
                    shared = orph.boundary.intersection(mp.boundary).length
                    if shared > best_len:
                        cand = unary_union([mp, orph])
                        if cand.geom_type == "Polygon" and cand.is_valid and len(cand.interiors) == 0:
                            best_len = shared
                            best_lbl = lbl
                if best_lbl >= 0:
                    main_polys[best_lbl] = orient(unary_union([main_polys[best_lbl], orph]), 1.0)
                    progressed = True
                else:
                    remaining.append(orph)
            orphans = remaining
            if not progressed and orphans:
                for orph in orphans:
                    best_lbl = max(
                        main_polys.keys(),
                        key=lambda lbl: orph.boundary.intersection(main_polys[lbl].boundary).length,
                    )
                    cand = unary_union([main_polys[best_lbl], orph])
                    if cand.geom_type == "Polygon":
                        main_polys[best_lbl] = orient(cand, 1.0)
                orphans = []

    if len(main_polys) != n:
        missing = sorted(set(range(n)) - set(main_polys.keys()))
        raise RuntimeError(f"Partition lost {len(missing)} motifs: {missing[:10]}")

    ordered_polys = [main_polys[i] for i in range(n)]
    face_meta: List[Dict[str, Any]] = []
    for i, (m, rec) in enumerate(zip(motifs, metadata_records)):
        md = {
            "piece_index": i + 1,
            "asset_id": m.asset_id,
            "family": m.family,
            "motif_class": m.motif_class,
            "site": m.site,
            "region": m.region,
            "country": m.country,
            "source_archive": m.source_archive,
            "source_group": m.source_group,
            "source_url": m.source_url,
            "creator": m.creator,
            "license": m.license,
            "license_url": m.license_url,
            "commercial_ok": bool(m.commercial_ok),
            "jigsaw_tier": m.jigsaw_tier,
            "sha256": m.sha256,
            "is_filler": False,
            "figure": True,
            "placement_center_mm": [round(float(layout.centres_mm[i, 0] - ox0), 3), round(float(layout.centres_mm[i, 1] - oy0), 3)],
            "placement_angle_deg": round(float(math.degrees(layout.angles_rad[i])), 2),
            "placement_span_mm": round(float(layout.sizes_mm[i]), 3),
        }
        if "share_alike_required" in rec:
            md["share_alike_required"] = bool(rec["share_alike_required"])
        face_meta.append(md)

    part = partition_from_polygons(
        ordered_polys,
        outline=outline,
        face_metadata=face_meta,
        snap=SNAP,
        auto_node=False,
    )
    if log:
        bx0, by0, bx1, by1 = outline.bounds
        log(
            f"Extracted PlanarPartition: {part.num_pieces} faces, {len(part.edges)} edges, "
            f"bounds={bx1 - bx0:.1f}x{by1 - by0:.1f}mm ({time.perf_counter() - t0:.1f}s)"
        )
    return part, placed_polys_shifted
