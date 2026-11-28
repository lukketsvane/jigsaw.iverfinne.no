"""Flagship 600x400 mm Aphex Twin Typeface + Oversized Logo Jigsaw Composer.

Combines:
- 1 distinctly oversized centerpiece Aphex Twin logo pinned upright at board center;
- All 26 A-Z glyphs + all 32 labeled glyph blends (+ labeled repeats, zero anonymous filler)
  from ``pathnet/aphex-glyph-dataset.json`` and ``pathnet/reference.json``;
- Area-balanced GPU SDF packing and differentiable relaxation on a clean rectangular board;
- Windowed first-arrival bay-rule Eikonal partition + strict 3.0 mm fabrication cleanup;
- Strict-3mm-safe cross-fault seam locking + single-piece rigid-motion pinning.

Non-commercial / rights notice:
  Aphex Twin logo and supplied typeface glyphs are NOT commercially cleared
  (``commercial_ok: false``). Personal / research use only.
"""

from __future__ import annotations

import json
import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import shapely
import torch
import torch.nn.functional as F
from PIL import Image, ImageDraw, ImageFont
from scipy.fft import next_fast_len
from shapely import union_all
from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parents[1]
PATHNET_DIR = Path(__file__).resolve().parent
if str(PATHNET_DIR) not in sys.path:
    sys.path.insert(0, str(PATHNET_DIR))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import audit_field as af  # noqa: E402
import field as fl  # noqa: E402
import field_large as fll  # noqa: E402
from field import BIG, DEV, EDGES, GL, GRES, T  # noqa: E402
from train import flatten  # noqa: E402

from heller_jigsaw.geometry.partition import partition_from_polygons  # noqa: E402
from heller_jigsaw.production.audit import measure as hj_measure  # noqa: E402
from heller_jigsaw.production.audit import motion_grid  # noqa: E402

DATASET = ROOT / "pathnet" / "aphex-glyph-dataset.json"
REFERENCE = ROOT / "pathnet" / "reference.json"
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
VARIANTS = ("AE-50", "CR-50", "FN-30", "KM-50", "XZ-70")


# ---------------------------------------------------------------------------
# Shape loading & normalization
# ---------------------------------------------------------------------------

def _resample_ring(points: np.ndarray, n_pts: int = 256) -> np.ndarray:
    """Resample a closed polygon exterior uniformly along arc length."""
    poly = Polygon(points).buffer(0)
    if poly.geom_type != "Polygon":
        poly = max(getattr(poly, "geoms", [poly]), key=lambda g: g.area)
    ring = orient(poly, 1.0).exterior
    L = float(ring.length)
    d = np.linspace(0.0, L, n_pts, endpoint=False)
    return np.array([ring.interpolate(float(s)).coords[0] for s in d], dtype=np.float64)


def _normalize_poly(points: np.ndarray, n_pts: int = 256) -> Tuple[np.ndarray, float]:
    """Center polygon on bbox midpoint, scale max bounding span to 1.0, return (pts, unit_area)."""
    pts = _resample_ring(np.asarray(points, dtype=np.float64), n_pts=n_pts)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    span = float(max(hi - lo))
    if span <= 0 or not np.isfinite(span):
        raise ValueError("Invalid silhouette polygon span")
    unit = (pts - 0.5 * (lo + hi)) / span
    ua = float(Polygon(unit).buffer(0).area)
    return unit, ua


def load_typeface_shapes(
    pieces: int = 100,
    logo_scale: float = 2.9,
    n_pts: int = 256,
) -> Tuple[List[Dict[str, Any]], List[np.ndarray], np.ndarray]:
    """Load logo + all 26 A-Z glyphs + 32 blends (+ labeled repeats) matching build_aphex_typeface_catalog."""
    if pieces < 27:
        raise ValueError("Need at least 27 pieces for centerpiece logo + all 26 A-Z glyphs")
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    reference = json.loads(REFERENCE.read_text(encoding="utf-8"))
    glyphs = {r["glyph"]: r for r in dataset["glyphs"]}
    variants = {r["id"]: r for r in dataset["variants"]}

    base_items: List[Tuple[Dict[str, Any], np.ndarray, float]] = []

    def add(asset_id, family, motif_class, glyph_label, short_label, glyph_kind, source_asset, pts, **extra):
        unit, ua = _normalize_poly(pts, n_pts=n_pts)
        rec = dict(
            asset_id=asset_id,
            family=family,
            motif_class=motif_class,
            glyph_label=glyph_label,
            short_label=short_label,
            glyph_kind=glyph_kind,
            source_archive="pathnet",
            source_asset=source_asset,
            commercial_ok=False,
            area_scale=1.0,
        )
        rec.update(extra)
        base_items.append((rec, unit, ua))

    logo_pts = flatten(np.asarray(reference["logo_control_points"], dtype=np.float64), steps=8)
    add(
        "aphex-logo", "logo", "aphex_logo", "Aphex Twin logo", "LOGO", "logo",
        "pathnet/reference.json", logo_pts,
        area_scale=float(logo_scale),
        source_record="logo_control_points",
        license="Logo rights not cleared",
    )
    for letter in LETTERS:
        row = glyphs[letter]
        add(
            f"aphex-glyph-{letter.lower()}", "typeface", f"aphex_glyph_{letter.lower()}",
            letter, letter, "letter", "pathnet/aphex-glyph-dataset.json", row["piece"],
            source_record=letter, typeface_source=dataset["source"],
            adaptation=row["adaptation"], license=dataset["license"],
        )
    variant_order = VARIANTS if pieces == 32 else (*VARIANTS, *sorted(variants.keys() - set(VARIANTS)))
    for vid in variant_order:
        if len(base_items) == pieces:
            break
        row = variants[vid]
        add(
            f"aphex-variant-{vid.lower()}", "typeface", f"aphex_variant_{vid.lower()}",
            vid, vid, "blend", "pathnet/aphex-glyph-dataset.json", row["piece"],
            source_record=vid, typeface_source=dataset["source"],
            parents=row["parents"], interpolation=row["interpolation"],
            mirrored_second=row["mirrored_second"], license=dataset["license"],
        )

    records: List[Dict[str, Any]] = [r for r, _, _ in base_items]
    unit_polys: List[np.ndarray] = [u for _, u, _ in base_items]
    unit_areas: List[float] = [a for _, _, a in base_items]

    # Exhaust distinct outlines before repeating (logo stays unique)
    orig_glyphs = list(zip(records[1:], unit_polys[1:], unit_areas[1:]))
    repeats = Counter()
    for idx in range(pieces - len(records)):
        rec0, u0, a0 = orig_glyphs[idx % len(orig_glyphs)]
        aid = rec0["asset_id"]
        repeats[aid] += 1
        records.append(dict(rec0, asset_id=f"{aid}-repeat-{repeats[aid]}", repeat_of=aid))
        unit_polys.append(u0.copy())
        unit_areas.append(a0)

    return records, unit_polys, np.asarray(unit_areas, dtype=np.float64)


def canonical_grids(records: Sequence[Dict[str, Any]], unit_polys: Sequence[np.ndarray]) -> torch.Tensor:
    """Compute exact (N, GRES, GRES) canonical signed-distance grids on [-GL, GL]^2 (deduplicated)."""
    u = torch.linspace(-GL, GL, GRES, device=DEV)
    yy, xx = torch.meshgrid(u, u, indexing="ij")
    pts_grid = torch.stack([xx.reshape(-1), yy.reshape(-1)], 1)
    cache: Dict[str, torch.Tensor] = {}
    grids = []
    for rec, poly in zip(records, unit_polys):
        key = str(rec.get("source_record", rec["asset_id"]))
        if key not in cache:
            cache[key] = fl.sdf_poly(pts_grid, T(poly)).reshape(GRES, GRES)
        grids.append(cache[key])
    return torch.stack(grids, dim=0)


def place_polygons(
    unit_polys: Sequence[np.ndarray],
    c: np.ndarray,
    th: np.ndarray,
    size: np.ndarray,
) -> List[np.ndarray]:
    """Place unit polygons into board mm coordinates matching field.place_t convention."""
    out = []
    for xy, ci, angle, sz in zip(unit_polys, c, th, size):
        co, si = math.cos(float(angle)), math.sin(float(angle))
        # Match field.place_t: einsum('bnd,bed->bne', q, [[co,-si],[si,co]]) -> x'=co*x - si*y? Wait:
        # field.place_t: rot = [[co, -si], [si, co]]; einsum('bnd,bed->bne', q, rot)
        # => out[..., e] = sum_d q[..., d] * rot[e, d] => x' = co*x - si*y, y' = si*x + co*y
        # That is standard CCW rotation by +angle.
        rot_T = np.array([[co, si], [-si, co]], dtype=np.float64)
        out.append(xy @ rot_T * float(sz) + np.asarray(ci, dtype=np.float64))
    return out


# ---------------------------------------------------------------------------
# Centerpiece-pinned GPU packing & area-balanced differentiable relaxation
# ---------------------------------------------------------------------------

def _construct_around_logo(
    G: torch.Tensor,
    want: torch.Tensor,
    W: float,
    H: float,
    px: float = 2.0,
    K: int = 48,
    tol: float = 0.015,
    cap: float = 12.0,
    rng_seed: int = 0,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, int]:
    """Pin Piece 0 (Aphex logo) upright at board center, then greedily dock pieces 1..N-1 around it."""
    n = len(G)
    h = round(H / px)
    w = round(W / px)
    X = fl.centres(W, H, px)
    kh = int(math.ceil((0.58 * float(want[1:].max()) + EDGES[-1]) / px))
    Phi = torch.full((n, h, w), BIG, device=DEV)
    c = torch.zeros((n, 2), device=DEV)
    th = torch.zeros(n, device=DEV)
    size = want.clone()

    # Piece 0: centerpiece Aphex Twin logo, pinned dead-center upright
    c[0] = torch.tensor([W * 0.5, H * 0.5], device=DEV)
    th[0] = 0.0
    size[0] = want[0]
    Phi[0] = fl.grid_phi(G[0:1], c[0:1], th[0:1], size[0:1], X[None])[0]

    # Order remaining pieces largest-first with gentle seed jitter for restart diversity
    gen = torch.Generator(device=DEV).manual_seed(int(rng_seed) + 17)
    jitter = 1.0 + 0.08 * (torch.rand(n - 1, generator=gen, device=DEV) - 0.5)
    order = (1 + torch.argsort(want[1:] * jitter, descending=True)).tolist()
    forced = 0
    for i in order:
        g, depth, occ = fl.board_maps(Phi.min(0).values, kh, px, W, H, cap)
        ang = (torch.arange(K, device=DEV) + torch.rand(1, device=DEV)) * (2.0 * math.pi / K)
        done = False
        for mult in (1.0, 0.94, 0.88):
            phi_k, S, A = fl.kernels(G[i], want[i:i + 1] * mult, ang, px, kh)
            gain, ovl, cnt, touch, area = fl.dock_scores(phi_k, g, depth, occ)
            ok = cnt <= tol * area[:, None, None]
            if bool(ok.any()):
                score = touch + 3e-3 * gain / (cap ** 2) + 0.4 * torch.rand_like(touch)
                c[i], th[i], size[i], _ = fl.take(torch.where(ok, score, torch.full_like(touch, -BIG)), S, A, px)
                done = True
                break
        if not done:
            forced += 1
            c[i], th[i], size[i], _ = fl.take(gain - 40.0 * ovl, S, A, px)
        Phi[i] = fl.grid_phi(G[i:i + 1], c[i:i + 1], th[i:i + 1], size[i:i + 1], X[None])[0]
    return c, th, size, forced


def _reinsert_glyphs(
    G: torch.Tensor,
    c: torch.Tensor,
    th: torch.Tensor,
    size: torch.Tensor,
    s_lo: torch.Tensor,
    s_hi: torch.Tensor,
    W: float,
    H: float,
    px: float = 2.0,
    K: int = 60,
    lam: float = 40.0,
    cap: float = 12.0,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, int]:
    """Single-piece exact FFT re-insertion for pieces 1..N-1 (keeping centerpiece logo pinned)."""
    n = len(G)
    X = fl.centres(W, H, px)
    kh = int(math.ceil((0.58 * float(s_hi[1:].max()) + EDGES[-1]) / px))
    c, th, size = c.clone(), th.clone(), size.clone()
    Phi = fl.grid_phi(G, c, th, size, X[None].expand(n, -1, -1, -1))
    moved = 0
    mid = [(a + b) / 2 for a, b in zip(EDGES[:-1], EDGES[1:])]
    perm = (1 + torch.randperm(n - 1)).tolist()
    for i in perm:
        v, l = torch.topk(Phi, 2, dim=0, largest=False)
        others = torch.where(l[0] == i, v[1], v[0])
        g, depth, occ = fl.board_maps(others, kh, px, W, H, cap)
        ang = (torch.arange(K, device=DEV) + torch.rand(1, device=DEV)) * (2.0 * math.pi / K)
        sizes = (size[i] * torch.tensor([0.92, 1.0, 1.08], device=DEV)).clamp(float(s_lo[i]), float(s_hi[i]))
        phi_k, S, A = fl.kernels(G[i], sizes, ang, px, kh)
        gain, ovl, cnt, touch, area = fl.dock_scores(phi_k, g, depth, occ)
        ci, ti, si, best = fl.take(gain - lam * ovl, S, A, px)
        gi = g[kh:-kh, kh:-kh]
        di = depth[kh:-kh, kh:-kh]
        p = Phi[i]
        now = (
            (gi * gi * (p < 0)).sum()
            + sum(((gi * gi - m * m).clamp_min(0) * ((p >= a) & (p < b))).sum() for m, a, b in zip(mid, EDGES[:-1], EDGES[1:]))
            - lam * (di * di * (p < 0)).sum()
        )
        if best > float(now) * 1.01 + 1.0:
            c[i], th[i], size[i] = ci, ti, si
            Phi[i] = fl.grid_phi(G[i:i + 1], c[i:i + 1], th[i:i + 1], size[i:i + 1], X[None])[0]
            moved += 1
    return c, th, size, moved


def _relax_area_balanced(
    G: torch.Tensor,
    c: torch.Tensor,
    th: torch.Tensor,
    size: torch.Tensor,
    s_lo: torch.Tensor,
    s_hi: torch.Tensor,
    target_area: torch.Tensor,
    W: float,
    H: float,
    steps: int = 240,
    px: float = 2.0,
    lam: Tuple[float, float] = (30.0, 450.0),
    w_area: float = 35.0,
    log=None,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Differentiable joint relaxation with full-board soft-Voronoi area equalization and centerpiece pinning."""
    n = len(G)
    pad_cells = int(math.ceil(14.0 / px))
    w_in = round(W / px)
    h_in = round(H / px)
    sx = W / w_in
    sy = H / h_in
    xs = (torch.arange(-pad_cells, w_in + pad_cells, device=DEV) + 0.5) * sx
    ys = (torch.arange(-pad_cells, h_in + pad_cells, device=DEV) + 0.5) * sy
    yy, xx = torch.meshgrid(ys, xs, indexing="ij")
    X_full = torch.stack([xx, yy], -1)  # (h_pad, w_pad, 2)
    in_sl = (slice(pad_cells, pad_cells + h_in), slice(pad_cells, pad_cells + w_in))
    out_mask = torch.ones((h_in + 2 * pad_cells, w_in + 2 * pad_cells), dtype=torch.bool, device=DEV)
    out_mask[in_sl] = False

    c = c.clone().requires_grad_(True)
    th = th.clone().requires_grad_(True)
    a_init = ((size - s_lo) / (s_hi - s_lo).clamp_min(1e-3)).clamp(0.03, 0.97)
    a = torch.logit(a_init).requires_grad_(True)
    opt = torch.optim.Adam([
        {"params": [c], "lr": 0.25},
        {"params": [th], "lr": 0.006},
        {"params": [a], "lr": 0.025},
    ])
    center = torch.tensor([W * 0.5, H * 0.5], device=DEV)
    cell_area = sx * sy
    glyph_lo = float(target_area[1:].mean()) * 0.85
    glyph_hi = float(target_area[1:].mean()) * 1.13
    logo_lo = float(target_area[0]) * 0.96

    for it in range(steps):
        f = it / max(1, steps - 1)
        weight = lam[0] * (lam[1] / lam[0]) ** f
        sz = s_lo + (s_hi - s_lo) * torch.sigmoid(a)
        jit = (torch.rand(2, device=DEV) - 0.5) * px * 0.6
        Xj = X_full + jit
        # Evaluate Phi on padded board in chunks to keep peak VRAM < 0.8 GB
        Phi = fl.grid_phi(G, c, th, sz, Xj[None].expand(n, -1, -1, -1))  # (n, H_p, W_p)
        Phi_in = Phi[:, in_sl[0], in_sl[1]]
        top2 = torch.topk(Phi_in, 2, dim=0, largest=False).values
        m1, m2 = top2[0], top2[1]
        gap = (m1.clamp_min(0) ** 2).mean()
        ovl = ((-m2).clamp_min(0) ** 2).mean()
        # Penalize silhouettes poking outside [0, W] x [0, H]
        out_pen = ((-Phi[:, out_mask]).clamp_min(0) ** 2).sum() * cell_area / (W * H)

        # Differentiable soft-Voronoi area balance
        probs = torch.softmax(-1.1 * Phi_in, dim=0)
        est_area = probs.sum(dim=(1, 2)) * cell_area
        rel_err = ((est_area - target_area) / target_area) ** 2
        area_hinge = (
            ((est_area[1:] - glyph_hi).clamp_min(0) / 300.0).pow(2).sum()
            + ((glyph_lo - est_area[1:]).clamp_min(0) / 300.0).pow(2).sum()
            + ((logo_lo - est_area[0]).clamp_min(0) / 300.0).pow(2)
        )
        area_loss = rel_err.mean() + 0.15 * area_hinge

        # Pin centerpiece logo dead-center and upright
        logo_anchor = 4.0 * ((c[0] - center) / 8.0).pow(2).sum() + 40.0 * (th[0] ** 2)

        E = gap + weight * (ovl + out_pen) + w_area * area_loss + logo_anchor
        opt.zero_grad()
        E.backward()
        opt.step()
        if log and (it % 80 == 0 or it == steps - 1):
            log(
                f"    relax {it:3d} E={float(E.detach()):.3f} cov={float((m1.detach() <= 0).float().mean()):.3f} "
                f"ovl_mm2={float((m2.detach() < 0).float().sum() * cell_area):.0f} "
                f"logo={float(est_area[0].detach()):.0f} glyph=[{float(est_area[1:].detach().min()):.0f},{float(est_area[1:].detach().max()):.0f}]"
            )
    return c.detach(), th.detach(), (s_lo + (s_hi - s_lo) * torch.sigmoid(a)).detach()


def pack_typeface_board(
    records: Sequence[Dict[str, Any]],
    unit_polys: Sequence[np.ndarray],
    unit_areas: np.ndarray,
    W: float = 600.0,
    H: float = 400.0,
    logo_area_target: float = 7100.0,
    seed: int = 42,
    restarts: int = 3,
    bay: float = 32.0,
    log=print,
) -> Dict[str, Any]:
    """Run multi-restart centerpiece-pinned packing + bay-calibrated area-balanced relaxation on GPU."""
    n = len(records)
    G = canonical_grids(records, unit_polys)
    glyph_area_target = (W * H - logo_area_target) / float(n - 1)
    desired_area = np.array([logo_area_target] + [glyph_area_target] * (n - 1), dtype=np.float64)
    target_area = T(desired_area)

    # Target silhouette sizes scaled to equal glyph area (logo scaled to ~84% of logo_area_target)
    s_raw = np.zeros(n, dtype=np.float64)
    s_raw[0] = math.sqrt(0.84 * logo_area_target / unit_areas[0])
    s_raw[1:] = np.sqrt(0.76 * glyph_area_target / unit_areas[1:])
    med_s = float(np.median(s_raw[1:]))
    s_raw[1:] = np.clip(s_raw[1:], 0.72 * med_s, 1.32 * med_s)

    want = T(s_raw)
    s_lo = want * T([0.95] + [0.80] * (n - 1))
    s_hi = want * T([1.10] + [1.22] * (n - 1))

    if log:
        log(
            f"Packing {n} Aphex typeface pieces on {W:g}x{H:g} mm ({DEV}): "
            f"logo_target={logo_area_target:.0f}mm2 (s={s_raw[0]:.1f}mm), "
            f"glyph_target={glyph_area_target:.0f}mm2 (s={s_raw[1:].min():.1f}-{s_raw[1:].max():.1f}mm)"
        )

    best = None
    for r in range(restarts):
        t0 = time.time()
        torch.manual_seed(seed + r * 101)
        np.random.seed(seed + r * 101)
        c, th, size, forced = _construct_around_logo(G, want, W, H, px=2.0, K=48, rng_seed=seed + r * 101)
        for sw in range(2):
            c, th, size = _relax_area_balanced(
                G, c, th, size, s_lo, s_hi, target_area, W, H, steps=180, px=2.0, w_area=36.0
            )
            c, th, size, moved = _reinsert_glyphs(G, c, th, size, s_lo, s_hi, W, H, px=2.0, K=54)
        c, th, size = _relax_area_balanced(
            G, c, th, size, s_lo, s_hi, target_area, W, H, steps=240, px=1.6, lam=(40.0, 600.0), w_area=52.0, log=log
        )

        # Post-bay-rule area feedback loop: compensate for bay-rule area transfers so final
        # post-bay pieces hit logo/max_glyph >= 2.4x AND max_area/min_area <= 3.85 (<= 4.0 gate)
        eff_target = desired_area.copy()
        polys = place_polygons(unit_polys, c.cpu().numpy(), th.cpu().numpy(), size.cpu().numpy())
        eval_res = None
        for fb in range(3):
            try:
                res_q = fll.partition(polys, W, H, px=0.5, bay=bay, margin=82.0)
                faces_q = res_q["faces"]
                areas = np.array([f.area for f in faces_q], dtype=np.float64)
                logo_a = float(areas[0])
                max_g = float(areas[1:].max())
                min_a = float(areas.min())
                logo_ratio = logo_a / max(max_g, 1.0)
                area_ratio = float(areas.max()) / max(min_a, 1.0)
                sil_ious = [
                    f.intersection(Polygon(p).buffer(0)).area / max(f.union(Polygon(p).buffer(0)).area, 1e-6)
                    for f, p in zip(faces_q, polys)
                ]
                mean_iou = float(np.mean(sil_ious))
                min_iou = float(np.min(sil_ious))
                lm = fl.lock_margin(faces_q)
                score = (
                    mean_iou * 2.0
                    + min_iou * 0.8
                    + min(lm, 4.0) * 0.15
                    - max(0.0, 2.40 - logo_ratio) * 6.0
                    - max(0.0, area_ratio - 3.85) * 14.0
                )
                if log:
                    log(
                        f"  restart {r} fb={fb}: forced={forced} logo={logo_a:.0f}mm2 max_g={max_g:.0f}mm2 min={min_a:.0f}mm2 "
                        f"logo/max_g={logo_ratio:.2f}x max/min={area_ratio:.2f}x "
                        f"sil_iou={mean_iou:.3f}/{min_iou:.3f} lock={lm:.2f}mm score={score:.3f} ({time.time() - t0:.1f}s)"
                    )
                cand = dict(
                    score=score, c=c.cpu().numpy(), th=th.cpu().numpy(), size=size.cpu().numpy(),
                    polys=polys, quick_logo_ratio=logo_ratio, quick_area_ratio=area_ratio,
                    quick_mean_iou=mean_iou, quick_lock=lm,
                )
                if eval_res is None or cand["score"] > eval_res["score"]:
                    eval_res = cand
                if area_ratio <= 3.78 and logo_ratio >= 2.45:
                    break
                if fb < 2:
                    ratio_corr = np.clip(desired_area / np.maximum(areas, 500.0), 0.72, 1.38)
                    eff_target = eff_target * (ratio_corr ** 0.72)
                    eff_target = np.clip(eff_target, 1450.0, 9200.0)
                    eff_target *= (W * H) / eff_target.sum()
                    c, th, size = _relax_area_balanced(
                        G, c, th, size, s_lo, s_hi, T(eff_target), W, H,
                        steps=160, px=1.6, lam=(60.0, 650.0), w_area=72.0, log=None,
                    )
                    polys = place_polygons(unit_polys, c.cpu().numpy(), th.cpu().numpy(), size.cpu().numpy())
            except ValueError as exc:
                if log:
                    log(f"  restart {r} fb={fb} quick cut failed: {exc}")
                break

        if eval_res is not None and (best is None or eval_res["score"] > best["score"]):
            best = eval_res

    if best is None:
        raise RuntimeError("All layout restarts failed quick partition")
    return best


# ---------------------------------------------------------------------------
# Strict-3mm fabrication cleanup + rigid-motion pinning
# ---------------------------------------------------------------------------

def _piece_fab_score(poly: Polygon, min_area: float = 1500.0, max_tip: float = 2.85) -> Tuple[int, float, float]:
    """Return (fail_count, max_tip_both, af_tip_reach); (0, <=max_tip, <=max_tip) is fully passing."""
    if poly.is_empty or not poly.is_valid or poly.geom_type != "Polygon" or len(poly.interiors) > 0:
        return (10, 99.0, 99.0)
    fails = 0
    if poly.area < min_area:
        fails += 2
    for k in af.KERFS:
        if af.parts(poly.buffer(-k * 0.5)) != 1:
            fails += 2
    if af.parts(poly.buffer(-1.25)) != 1:
        fails += 2
    if af.parts(poly.buffer(-1.50)) != 1:
        fails += 2
    af_tip, _ = af.tip_reach(poly, r=1.5)
    if af_tip > max_tip:
        fails += 1
    try:
        hj_d = hj_measure(poly, kerf=0.25, r=1.5)
        hj_tip = float(hj_d["max_tip_loss_mm"])
    except ValueError:
        return (fails + 4, 99.0, af_tip)
    if hj_tip > max_tip:
        fails += 1
    return (fails, max(af_tip, hj_tip), af_tip)


def clean_strict_fabrication(
    faces: Sequence[Polygon],
    polys: Sequence[np.ndarray],
    W: float,
    H: float,
    max_tip_mm: float = 2.85,
    min_area: float = 1550.0,
    max_passes: int = 4,
    log=print,
) -> List[Polygon]:
    """Repair any residual strict 2.5/3.0mm neck splits or >2.85mm thin tips across shared seams."""
    n = len(faces)
    board = box(0.0, 0.0, W, H)
    cur = [orient(p.buffer(0), 1.0) for p in faces]
    sil_polys = [Polygon(xy).buffer(0) for xy in polys]

    for p_idx in range(max_passes):
        bad = []
        for i, p in enumerate(cur):
            fc, mtip, _ = _piece_fab_score(p, min_area=min_area, max_tip=max_tip_mm)
            if fc > 0:
                bad.append((fc, mtip, i))
        if not bad:
            if log:
                log(f"  clean_strict_fabrication: 0/{n} failures after pass {p_idx}")
            break
        bad.sort(reverse=True)
        if log:
            log(f"  clean_strict_fabrication pass {p_idx + 1}: {len(bad)} pieces to refine (worst tip {bad[0][1]:.2f}mm)")

        changed_any = False
        for _, _, i in bad:
            p = cur[i]
            sc_i = _piece_fab_score(p, min_area=min_area, max_tip=max_tip_mm)
            if sc_i[0] == 0:
                continue

            # Identify offending detached lobes and thin spurs under r=1.65 mm opening
            eroded = p.buffer(-1.65, quad_segs=10)
            parts_e = sorted(
                [g for g in shapely.get_parts(eroded) if g.geom_type == "Polygon" and g.area > 1e-4],
                key=lambda g: g.area,
                reverse=True,
            )
            if not parts_e:
                continue
            main_body = parts_e[0].buffer(1.65, quad_segs=10).intersection(p)
            lost = p.difference(main_body)
            comps = sorted(
                [g for g in shapely.get_parts(lost) if g.geom_type == "Polygon" and g.area > 0.05],
                key=lambda g: g.area,
                reverse=True,
            )
            if not comps:
                continue

            best_opt = None
            for comp in comps[:4]:
                # Find adjacent neighbors sharing boundary with this thin lobe/spur
                nbrs = []
                for j in range(n):
                    if j == i or cur[j].distance(comp) > 0.08:
                        continue
                    sh = cur[j].boundary.intersection(comp.buffer(0.05)).length
                    if sh > 0.2:
                        nbrs.append((sh, j))
                nbrs.sort(reverse=True)
                for _, j in nbrs[:3]:
                    pj = cur[j]
                    sc_j = _piece_fab_score(pj, min_area=min_area, max_tip=max_tip_mm)
                    union_ij = p.union(pj)
                    cands = []
                    # Option A: trim thin spur/lobe and hand to neighbor j
                    xfer = p.intersection(comp.buffer(0.05)).buffer(0)
                    if not xfer.is_empty:
                        cands.append(("trim", p.difference(xfer), pj.union(xfer)))
                    # Option B: widen the neck/spur using a small fillet from neighbor j
                    for w in (1.1, 1.8, 2.5):
                        grab = comp.buffer(w, quad_segs=10).intersection(pj).buffer(0)
                        if not grab.is_empty:
                            cands.append((f"widen-{w}", p.union(grab), pj.difference(grab)))

                    for mode, qi, qj in cands:
                        qi = qi.intersection(board).buffer(0)
                        qj = qj.intersection(board).buffer(0)
                        if any(q.is_empty or q.geom_type != "Polygon" or not q.is_valid or len(q.interiors) > 0 for q in (qi, qj)):
                            continue
                        if qi.intersection(qj).area > 1e-3:
                            continue
                        if qi.union(qj).symmetric_difference(union_ij).area > 0.05:
                            continue
                        # Preserve centerpiece logo dominance (piece 0 stays >= 6400, glyphs stay <= 2850)
                        if (i == 0 and qi.area < 6400.0) or (j == 0 and qj.area < 6400.0):
                            continue
                        if (i > 0 and qi.area > 2900.0) or (j > 0 and qj.area > 2900.0):
                            continue
                        nsc_i = _piece_fab_score(qi, min_area=min_area, max_tip=max_tip_mm)
                        nsc_j = _piece_fab_score(qj, min_area=min_area, max_tip=max_tip_mm)
                        old_f = sc_i[0] + sc_j[0]
                        new_f = nsc_i[0] + nsc_j[0]
                        old_t = max(sc_i[1], sc_j[1])
                        new_t = max(nsc_i[1], nsc_j[1])
                        if new_f > old_f or (new_f == old_f and new_t >= old_t - 0.15):
                            continue
                        if nsc_j[0] > sc_j[0]:
                            continue
                        # Prefer minimal silhouette perturbation
                        iou_drop = (
                            (p.intersection(sil_polys[i]).area / max(p.union(sil_polys[i]).area, 1e-6))
                            - (qi.intersection(sil_polys[i]).area / max(qi.union(sil_polys[i]).area, 1e-6))
                        )
                        rank = (new_f, round(new_t, 2), iou_drop)
                        if best_opt is None or rank < best_opt[0]:
                            best_opt = (rank, j, qi, qj, mode, nsc_i, nsc_j)

            if best_opt is not None:
                _, j, qi, qj, mode, nsc_i, nsc_j = best_opt
                cur[i], cur[j] = orient(qi, 1.0), orient(qj, 1.0)
                changed_any = True
                if log:
                    log(f"    repaired piece {i} via {mode} with {j}: fails {sc_i[0]}->{nsc_i[0]}, tip {sc_i[1]:.2f}->{nsc_i[1]:.2f}mm")

        if not changed_any:
            break

    # Re-node cleanly onto exact 0.01 mm planar grid via PlanarPartition
    part = partition_from_polygons(cur, board, snap=0.01, auto_node=True)
    return [f.polygon for f in part.faces]


def _rigid_escapes_for_piece(idx: int, kerf_polys: Sequence[Polygon], candidates: np.ndarray) -> Tuple[int, int]:
    """Exact single-piece rigid motion screen (matches heller_jigsaw.production.audit.rigid_motion_audit)."""
    shape = kerf_polys[idx]
    cx, cy = shape.centroid.coords[0]
    a, b, c, e = shape.bounds
    reach = 3.0 + max(math.hypot(x - cx, y - cy) for x, y in shape.exterior.coords) * 0.11
    near = box(a - reach, b - reach, c + reach, e + reach)
    others = unary_union([v for j, v in enumerate(kerf_polys) if j != idx and v.intersects(near)])
    active = np.arange(len(candidates))
    for step in (0.25, 0.5, 0.75, 1.0):
        if not len(active):
            break
        params = candidates[active] * step
        objs = np.full(len(params), shape, dtype=object)
        xy, p_idx = shapely.get_coordinates(objs, return_index=True)
        uv = xy - np.array([cx, cy])
        alpha = params[p_idx, 0]
        cs = np.cos(alpha)
        sn = np.sin(alpha)
        new = np.column_stack([cs * uv[:, 0] - sn * uv[:, 1], sn * uv[:, 0] + cs * uv[:, 1]]) + np.array([cx, cy]) + params[p_idx, 1:]
        objs = shapely.set_coordinates(objs, new)
        collisions = shapely.intersects(objs, others)
        blocked = np.zeros(len(objs), bool)
        blocked[collisions] = shapely.area(shapely.intersection(objs[collisions], others)) > 0.01
        active = active[~blocked]
    safe = candidates[active]
    if not len(safe):
        return 0, 0
    trans = np.linalg.norm(safe[:, 1:], axis=1)
    deg = np.abs(np.rad2deg(safe[:, 0]))
    return int((deg >= 3.99).sum()), int((trans >= 2.99).sum())


def pin_rigid_motion_escapes(
    faces: Sequence[Polygon],
    polys: Sequence[np.ndarray],
    W: float,
    H: float,
    min_area: float = 1550.0,
    max_tip_mm: float = 2.85,
    min_board_lock: float = 3.5,
    log=print,
) -> List[Polygon]:
    """Ensure every piece passes rigid_motion_audit (0 rotation>=4deg, 0 translation>=3mm)."""
    n = len(faces)
    board = box(0.0, 0.0, W, H)
    arcs, face_refs = fll.build_arc_graph(faces)
    cur = [fll._rebuild_face(face_refs[i], arcs) for i in range(n)]
    sil_polys = [Polygon(xy).buffer(0) for xy in polys]
    candidates = motion_grid()
    kerf_polys = [p.buffer(-0.125, quad_segs=8) for p in cur]

    escaping = []
    for i in range(n):
        r4, t3 = _rigid_escapes_for_piece(i, kerf_polys, candidates)
        if r4 > 0 or t3 > 0:
            escaping.append((i, r4, t3))
    if not escaping:
        if log:
            log("  pin_rigid_motion_escapes: already 0/0 escaping pieces")
        return cur
    if log:
        log(f"  pin_rigid_motion_escapes: pinning {len(escaping)} pieces: {[(i, r4, t3) for i, r4, t3 in escaping]}")

    tab_specs = [(4.4, 8.2, 7.4, 14.5), (4.6, 8.8, 7.8, 15.5), (4.2, 7.8, 7.0, 13.5)]
    for i, r4_0, t3_0 in escaping:
        r4_now, t3_now = _rigid_escapes_for_piece(i, kerf_polys, candidates)
        if r4_now == 0 and t3_now == 0:
            continue
        arc_lines = [LineString(e["coords"]) for e in arcs]
        arc_tree = STRtree(arc_lines)
        inc_arcs = [e["id"] for e in arcs if i in e["pair"] and min(e["pair"]) >= 0]
        inc_arcs.sort(key=lambda eid: -arc_lines[eid].length)
        best = None
        for eid in inc_arcs:
            e = arcs[eid]
            a, b = e["pair"]
            j = b if a == i else a
            line = arc_lines[eid]
            if line.length < 14.0:
                continue
            old_coords = e["coords"]
            ep0, ep1 = Point(old_coords[0]), Point(old_coords[-1])
            union_ij = cur[i].union(cur[j])
            for nw, hw, dep, sp in tab_specs:
                if line.length < sp + 3.0:
                    continue
                for frac in (0.5, 0.35, 0.65, 0.25, 0.75):
                    for sign in (1, -1):
                        new_coords = fll._strict_tab_coords(old_coords, frac, sign, nw, hw, dep, sp)
                        if new_coords is None:
                            continue
                        ls = LineString(new_coords)
                        if not ls.is_simple or not board.covers(ls) or ls.difference(union_ij.buffer(0.02)).length > 0.05:
                            continue
                        hit = False
                        for k in arc_tree.query(ls):
                            k = int(k)
                            if k == eid:
                                continue
                            inter = ls.intersection(arc_lines[k])
                            if inter.is_empty:
                                continue
                            if inter.geom_type == "Point" and (inter.distance(ep0) <= 0.02 or inter.distance(ep1) <= 0.02):
                                continue
                            hit = True
                            break
                        if hit:
                            continue
                        e["coords"] = new_coords
                        try:
                            ni = fll._rebuild_face(face_refs[i], arcs)
                            nj = fll._rebuild_face(face_refs[j], arcs)
                        except ValueError:
                            e["coords"] = old_coords
                            continue
                        e["coords"] = old_coords
                        if not (
                            fll._strict_piece_ok(ni, min_area=min_area, max_tip_mm=max_tip_mm, check_hj_tip=True)
                            and fll._strict_piece_ok(nj, min_area=min_area, max_tip_mm=max_tip_mm, check_hj_tip=True)
                        ):
                            continue
                        if abs(ni.area + nj.area - cur[i].area - cur[j].area) > 0.05 or ni.intersection(nj).area > 1e-4:
                            continue
                        ki, kj = ni.buffer(-0.125, quad_segs=8), nj.buffer(-0.125, quad_segs=8)
                        old_ki, old_kj = kerf_polys[i], kerf_polys[j]
                        kerf_polys[i], kerf_polys[j] = ki, kj
                        nr4_i, nt3_i = _rigid_escapes_for_piece(i, kerf_polys, candidates)
                        nr4_j, nt3_j = _rigid_escapes_for_piece(j, kerf_polys, candidates)
                        kerf_polys[i], kerf_polys[j] = old_ki, old_kj
                        if nr4_j > 0 or nt3_j > 0:
                            continue
                        if (nr4_i + nt3_i) >= (r4_now + t3_now):
                            continue
                        iou_loss = (
                            (cur[i].intersection(sil_polys[i]).area / max(cur[i].union(sil_polys[i]).area, 1e-6))
                            - (ni.intersection(sil_polys[i]).area / max(ni.union(sil_polys[i]).area, 1e-6))
                        )
                        rank = (nr4_i + nt3_i, iou_loss)
                        if best is None or rank < best[0]:
                            best = (rank, eid, j, new_coords, ni, nj, ki, kj, nr4_i, nt3_i)
                        if nr4_i == 0 and nt3_i == 0:
                            break
                if best is not None and best[0][0] == 0:
                    break
            if best is not None and best[0][0] == 0:
                break
        if best is not None:
            _, eid, j, new_coords, ni, nj, ki, kj, nr4_i, nt3_i = best
            arcs[eid]["coords"] = new_coords
            cur[i], cur[j] = ni, nj
            kerf_polys[i], kerf_polys[j] = ki, kj
            if log:
                log(f"    pinned piece {i} via seam ({i},{j}): rigid ({r4_now},{t3_now}) -> ({nr4_i},{nt3_i})")

    cut = union_all([LineString(e["coords"]) for e in arcs], grid_size=0.01)
    repoly = list(polygonize(cut))
    if len(repoly) != n:
        raise ValueError(f"pin_rigid_motion_escapes cut gave {len(repoly)} faces for {n}")
    match = [max(range(n), key=lambda k: repoly[k].intersection(f).area) for f in cur]
    return [orient(repoly[k], 1.0) for k in match]


# ---------------------------------------------------------------------------
# Full composition pipeline
# ---------------------------------------------------------------------------

def compose_typeface_board(
    pieces: int = 100,
    W: float = 600.0,
    H: float = 400.0,
    logo_area_target: float = 7100.0,
    logo_scale: float = 2.9,
    seed: int = 42,
    restarts: int = 3,
    bay: float = 32.0,
    target_lock: float = 3.8,
    log=print,
) -> Dict[str, Any]:
    """End-to-end flagship 600x400 mm Aphex Twin typeface + oversized logo puzzle composition."""
    t0 = time.time()
    records, unit_polys, unit_areas = load_typeface_shapes(pieces=pieces, logo_scale=logo_scale)
    packed = pack_typeface_board(
        records, unit_polys, unit_areas,
        W=W, H=H, logo_area_target=logo_area_target, seed=seed, restarts=restarts, bay=bay, log=log,
    )
    polys = packed["polys"]
    if log:
        log(f"Cutting high-resolution 0.25mm bay-rule Eikonal partition (bay={bay})...")
    res = fll.partition(polys, W, H, px=0.25, bay=bay, margin=82.0)
    faces = res["faces"]
    if log:
        log(f"  initial cut: {len(faces)} faces, lock={fl.lock_margin(faces):.3f}mm ({time.time() - t0:.1f}s)")

    # 1. Strict 3.0 mm fabrication cleanup
    faces = clean_strict_fabrication(faces, polys, W, H, max_tip_mm=2.85, min_area=1550.0, log=log)

    # 2. Strict-3mm-safe cross-fault seam locking (>= target_lock, checking BOTH audit_field & heller_jigsaw tips)
    locked = fll.lock_fault_lines(
        faces, polys, W, H, target_margin=target_lock,
        min_area=1550.0, max_tip_mm=2.85, check_hj_tip=True, max_hooks=90, log=log,
    )
    faces = locked["faces"]

    # 3. Ensure 0/0 single-piece rigid motion escapes
    faces = pin_rigid_motion_escapes(faces, polys, W, H, min_area=1550.0, max_tip_mm=2.85, log=log)

    # 4. Re-verify board lock margin (top up if rigid pinning nudged any seam)
    if fl.lock_margin(faces) < target_lock - 0.15:
        locked2 = fll.lock_fault_lines(
            faces, polys, W, H, target_margin=target_lock,
            min_area=1550.0, max_tip_mm=2.85, check_hj_tip=True, max_hooks=30, log=log,
        )
        faces = locked2["faces"]

    final_lm = fl.lock_margin(faces)
    if log:
        areas = [f.area for f in faces]
        log(
            f"Done in {time.time() - t0:.1f}s: {len(faces)} faces, "
            f"logo={areas[0]:.1f}mm2 ({areas[0] / max(areas[1:]):.2f}x max glyph, "
            f"{areas[0] / float(np.median(areas[1:])):.2f}x median), "
            f"max/min={max(areas) / min(areas):.2f}x, lock={final_lm:.3f}mm"
        )

    return {
        "records": records,
        "unit_polys": unit_polys,
        "polys": polys,
        "faces": faces,
        "c": packed["c"],
        "th": packed["th"],
        "size": packed["size"],
        "W": W,
        "H": H,
        "bay": bay,
        "seed": seed,
        "hooks": locked["hooks"],
        "lock_margin_mm": final_lm,
        "seconds": time.time() - t0,
    }


# ---------------------------------------------------------------------------
# High-craft artwork SVG & visual renders
# ---------------------------------------------------------------------------

def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _poly_d(coords: Sequence[Tuple[float, float]]) -> str:
    pts = list(coords)
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in pts) + " Z"


def write_artwork_svg(
    out_path: Path,
    faces: Sequence[Polygon],
    polys: Sequence[np.ndarray],
    records: Sequence[Dict[str, Any]],
    W: float,
    H: float,
) -> None:
    """Export high-craft presentation & laser-engrave artwork.svg (600x400 mm)."""
    tone = fl.colouring(list(faces))
    # Warm architectural birch / stone palette + commanding dark obsidian centerpiece logo
    birch_fills = ["#EFE6D5", "#E4D7C1", "#D8C8AE", "#EAE0CC", "#DFD2BA", "#F4ECE0"]
    glyph_inlay = "#24221F"
    logo_piece_fill = "#C99B4B"  # warm anodized gold centerpiece tile
    logo_inlay = "#141312"

    parts = [
        f'<?xml version="1.0" encoding="utf-8"?>',
        f'<!-- NON-COMMERCIAL RESEARCH PROTOTYPE: commercial_ok=false; Aphex Twin logo and supplied typeface rights are NOT cleared. -->',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.4f}mm" height="{H:.4f}mm" viewBox="0 0 {W:.4f} {H:.4f}" version="1.1">',
        f'  <rect x="0" y="0" width="{W:.4f}" height="{H:.4f}" fill="#F6F1E7"/>',
        '  <g id="piece-fills" stroke="#2D261F" stroke-width="0.35" stroke-linejoin="round">',
    ]
    for i, (f, rec) in enumerate(zip(faces, records)):
        fill = logo_piece_fill if rec["glyph_kind"] == "logo" else birch_fills[tone[i] % len(birch_fills)]
        parts.append(
            f'    <path id="tile-{i + 1:04d}" data-asset="{rec["asset_id"]}" data-label="{rec["glyph_label"]}" '
            f'd="{_poly_d(list(f.exterior.coords))}" fill="{fill}"/>'
        )
    parts.append("  </g>")
    parts.append('  <g id="glyph-artwork" stroke="none">')
    for i, (poly, rec) in enumerate(zip(polys, records)):
        # Clip silhouette cleanly inside its own cut piece so artwork never crosses a cut seam
        clipped = faces[i].intersection(Polygon(poly).buffer(0))
        fill = logo_inlay if rec["glyph_kind"] == "logo" else glyph_inlay
        opacity = "0.96" if rec["glyph_kind"] == "logo" else "0.84"
        for g in shapely.get_parts(clipped):
            if g.geom_type == "Polygon" and g.area > 1.0:
                parts.append(
                    f'    <path id="glyph-{i + 1:04d}" d="{_poly_d(list(g.exterior.coords))}" fill="{fill}" fill-opacity="{opacity}"/>'
                )
    parts.append("  </g>")
    parts.append(f'  <rect x="0" y="0" width="{W:.4f}" height="{H:.4f}" fill="none" stroke="#181512" stroke-width="0.6"/>')
    parts.append("</svg>")
    out_path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def render_edition_visuals(
    out_dir: Path,
    faces: Sequence[Polygon],
    polys: Sequence[np.ndarray],
    records: Sequence[Dict[str, Any]],
    c: np.ndarray,
    th: np.ndarray,
    W: float,
    H: float,
    ious: Sequence[float],
) -> None:
    """Render clean assembly.png, assembly_labeled.png, and pieces_sheet.png."""
    tone = fl.colouring(list(faces))
    # BGR birch/architectural palette for cv2
    birch_bgr = [
        (213, 230, 239),
        (193, 215, 228),
        (174, 200, 216),
        (204, 224, 234),
        (186, 210, 223),
        (224, 236, 244),
    ]
    logo_tile_bgr = (75, 155, 201)  # rich warm gold-ochre centerpiece tile
    scale = 4.0  # 2400 x 1600 px for 600 x 400 mm
    img_w, img_h = round(W * scale), round(H * scale)
    img = np.full((img_h, img_w, 3), (242, 246, 249), np.uint8)

    def to_pts(xy):
        return np.round(np.asarray(xy, dtype=np.float64) * scale * 16).astype(np.int32)

    # 1. Fill piece tiles
    for i, (f, rec) in enumerate(zip(faces, records)):
        col = logo_tile_bgr if rec["glyph_kind"] == "logo" else birch_bgr[tone[i] % len(birch_bgr)]
        cv2.fillPoly(img, [to_pts(f.exterior.coords)], col, lineType=cv2.LINE_AA, shift=4)

    # 2. Fill crisp glyph/logo silhouettes clipped to their pieces
    for i, (f, poly, rec) in enumerate(zip(faces, polys, records)):
        clipped = f.intersection(Polygon(poly).buffer(0))
        col = (20, 20, 22) if rec["glyph_kind"] == "logo" else (48, 45, 42)
        for g in shapely.get_parts(clipped):
            if g.geom_type == "Polygon" and g.area > 1.0:
                cv2.fillPoly(img, [to_pts(g.exterior.coords)], col, lineType=cv2.LINE_AA, shift=4)

    # 3. Draw crisp laser-cut seam lines
    for f in faces:
        cv2.polylines(img, [to_pts(f.exterior.coords)], True, (36, 30, 26), 2, cv2.LINE_AA, shift=4)
    cv2.rectangle(img, (0, 0), (img_w - 1, img_h - 1), (20, 18, 16), 3)

    # Save clean assembly.png (unlabeled museum view)
    cv2.imwrite(str(out_dir / "assembly.png"), img)

    # 4. Save assembly_labeled.png with crisp, compact non-overlapping pill badges
    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    f_logo = _font(32)
    f_let = _font(22)
    f_bld = _font(17)
    for i, (f, rec) in enumerate(zip(faces, records)):
        # Pole of inaccessibility approximation via deepest point of eroded core
        er = f.buffer(-4.0)
        anchor = (er if not er.is_empty else f).representative_point()
        px_x, px_y = anchor.x * scale, anchor.y * scale
        kind = rec["glyph_kind"]
        if kind == "logo":
            txt = "APHEX LOGO"
            fnt = f_logo
            bg_col = (20, 18, 16)
            fg_col = (248, 218, 130)
            pad_x, pad_y = 10, 5
        elif kind == "letter":
            txt = f"{rec['short_label']}" + ("*" if "repeat_of" in rec else "")
            fnt = f_let
            bg_col = (255, 252, 244)
            fg_col = (18, 18, 20)
            pad_x, pad_y = 6, 3
        else:
            txt = f"{rec['short_label']}" + ("*" if "repeat_of" in rec else "")
            fnt = f_bld
            bg_col = (242, 246, 250)
            fg_col = (35, 45, 65)
            pad_x, pad_y = 5, 2
        bb = draw.textbbox((px_x, px_y), txt, font=fnt, anchor="mm")
        draw.rounded_rectangle(
            [bb[0] - pad_x, bb[1] - pad_y, bb[2] + pad_x, bb[3] + pad_y],
            radius=5, fill=bg_col, outline=(70, 65, 60), width=1,
        )
        draw.text((px_x, px_y), txt, fill=fg_col, font=fnt, anchor="mm")
    pil.save(str(out_dir / "assembly_labeled.png"), optimize=True)

    # 5. Save pieces_sheet.png (10x10 clean upright contact sheet)
    n = len(faces)
    cols = 10
    rows = int(math.ceil(n / cols))
    cell = 260
    hdr_h = 56
    sheet = Image.new("RGB", (cols * cell, hdr_h + rows * cell), (246, 243, 236))
    sd = ImageDraw.Draw(sheet)
    sd.text(
        (20, 16),
        f"Aphex Twin Typeface + Oversized Logo Jigsaw — {n} Pieces ({W:.0f}x{H:.0f} mm) | Non-Commercial (commercial_ok=false)",
        fill=(24, 22, 20),
        font=_font(22),
    )

    def upright(xy, ci, angle):
        co, si = math.cos(-float(angle)), math.sin(-float(angle))
        d = np.asarray(xy, dtype=np.float64) - np.asarray(ci, dtype=np.float64)
        return d @ np.array([[co, si], [-si, co]])

    f_cap = _font(15)
    f_sub = _font(13)
    for i, (f, poly, rec, iou) in enumerate(zip(faces, polys, records, ious)):
        r_idx, c_idx = i // cols, i % cols
        ox, oy = c_idx * cell, hdr_h + r_idx * cell
        sd.rectangle([ox + 4, oy + 4, ox + cell - 4, oy + cell - 4], fill=(255, 255, 255), outline=(210, 204, 196), width=1)
        uf = upright(f.exterior.coords, c[i], th[i])
        up = upright(poly, c[i], th[i])
        both = np.vstack([uf, up])
        lo, hi = both.min(axis=0), both.max(axis=0)
        mid = 0.5 * (lo + hi)
        span = max(float(max(hi - lo)), 1.0)
        sc = (cell - 66) / span
        cx_px, cy_px = ox + cell * 0.5, oy + (cell - 34) * 0.5 + 4
        pts_f = [(cx_px + (x - mid[0]) * sc, cy_px + (y - mid[1]) * sc) for x, y in uf]
        pts_p = [(cx_px + (x - mid[0]) * sc, cy_px + (y - mid[1]) * sc) for x, y in up]
        tile_fill = (201, 155, 75) if rec["glyph_kind"] == "logo" else (58, 54, 50)
        sd.polygon(pts_f, fill=tile_fill, outline=(18, 16, 14))
        sd.line(pts_p + [pts_p[0]], fill=(255, 244, 214) if rec["glyph_kind"] == "logo" else (235, 232, 224), width=2)
        tag = rec["short_label"] + (" (rep)" if "repeat_of" in rec else "")
        sd.text((ox + 12, oy + cell - 42), f"#{i + 1:03d} {tag}", fill=(24, 22, 20), font=f_cap)
        sd.text((ox + 12, oy + cell - 23), f"{f.area:.0f} mm² | IoU {iou:.2f}", fill=(95, 90, 84), font=f_sub)
    sheet.save(str(out_dir / "pieces_sheet.png"), optimize=True)
