"""Field composition: every piece is a PathNet silhouette and the board is divided between them.

Each piece i has a target silhouette (PathNet family, latent, pose) with signed distance phi_i (mm, negative
inside). The board is split by first arrival: T_i solves the Eikonal equation |grad T_i| = s(x) outward from
silhouette i and a point belongs to the piece whose front reaches it first. Neighbouring pieces therefore grow
at the same time and every seam is shared. s(x) is a neural slowness field trained per puzzle; with s = 1 the
seams are the plain bisectors between silhouettes. There are no filler pieces.
"""
import argparse, copy, json, math, os, time
from collections import defaultdict
from pathlib import Path
import numpy as np, torch, cv2
import torch.nn.functional as F
from scipy import ndimage
from scipy.fft import next_fast_len
from scipy.ndimage import gaussian_filter1d
from shapely import union_all, get_parts
from shapely.geometry import Polygon, LineString, Point, box
from shapely.ops import polygonize
from train import flatten, Z
from generate import ROOT, names, model, ref, sample, outsvg, points_svg, poly_svg

torch.set_num_threads(max(1, min(8, (os.cpu_count() or 2) - 2)))   # generate.py pins torch to one thread
DEV = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
net = copy.deepcopy(model).to(DEV).eval()                           # PathNet decoder used inside the optimisation
for p in net.parameters(): p.requires_grad_(False)
BIG = 1e4

def T(x, dtype=torch.float32): return torch.as_tensor(np.asarray(x), dtype=dtype, device=DEV)

# ---------------------------------------------------------------- shapes and distance fields

def flatten_t(q, steps):
    """torch twin of train.flatten: periodic cubic B-spline controls (...,96,2) -> polyline (...,96*steps,2)"""
    a, b, c, d = torch.roll(q, 1, -2), q, torch.roll(q, -1, -2), torch.roll(q, -2, -2)
    seg = torch.stack([(a + 4 * b + c) / 6, (2 * b + c) / 3, (b + 2 * c) / 3, (b + 4 * c + d) / 6], -2)
    t = torch.arange(steps, dtype=q.dtype, device=q.device) / steps
    w = torch.stack([(1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3], 1)
    return torch.einsum('tk,...nkd->...ntd', w, seg).reshape(*q.shape[:-2], -1, 2)

def place_t(q, c, th, size):
    """controls (B,96,2) in unit coordinates -> board millimetres (same convention as puzzle.py)"""
    co, si = th.cos(), th.sin()
    rot = torch.stack([torch.stack([co, -si], -1), torch.stack([si, co], -1)], -2)          # (B,2,2)
    return torch.einsum('bnd,bed->bne', q, rot) * size[:, None, None] + c[:, None, :]

def sdf_polys(P, V, chunk=1024):
    """signed distance from points P (n,M,2) to closed polygons V (n,S,2), one polygon per row; differentiable in V"""
    A, B = V, torch.roll(V, -1, 1); E = B - A; EE = (E * E).sum(-1).clamp_min(1e-12)
    Ey = torch.where(E[..., 1] == 0, torch.ones_like(E[..., 1]), E[..., 1]); out = []
    for p in P.split(chunk, 1):
        Wv = p[:, :, None, :] - A[:, None]
        t = ((Wv * E[:, None]).sum(-1) / EE[:, None]).clamp(0, 1)
        D = Wv - t[..., None] * E[:, None]
        d = (D * D).sum(-1).min(2).values.clamp_min(1e-12).sqrt()
        cross = (A[:, None, :, 1] > p[:, :, None, 1]) != (B[:, None, :, 1] > p[:, :, None, 1])
        xi = A[:, None, :, 0] + (p[:, :, None, 1] - A[:, None, :, 1]) * E[:, None, :, 0] / Ey[:, None]
        inside = (cross & (p[:, :, None, 0] < xi)).sum(2) % 2 == 1
        out.append(torch.where(inside, -d, d))
    return torch.cat(out, 1)

def sdf_poly(P, V, chunk=16384):
    """signed distance from points P (M,2) to one closed polygon V (S,2)"""
    return sdf_polys(P[None], V[None], chunk)[0]

def centres(W, H, px):
    """pixel-centre coordinates (h,w,2) of a board grid"""
    w = round(W / px); h = round(H / px)
    yy, xx = torch.meshgrid((torch.arange(h, device=DEV) + .5) * (H / h), (torch.arange(w, device=DEV) + .5) * (W / w), indexing='ij')
    return torch.stack([xx, yy], -1)

# ---------------------------------------------------------------- layout: where every silhouette sits

GL, GRES = 1.0, 129          # canonical distance grids cover [-GL,GL]^2 in unit shape coordinates
EDGES = (0., 1.5, 3., 5., 7.5, 10.)          # distance bands (mm) outside a silhouette used when scoring a pose

def canon(q):
    """exact signed distance grid of a unit-size shape (controls q) for fast rigid sampling"""
    u = torch.linspace(-GL, GL, GRES, device=DEV); yy, xx = torch.meshgrid(u, u, indexing='ij')
    return sdf_poly(torch.stack([xx.reshape(-1), yy.reshape(-1)], 1), T(flatten(q, 4))).reshape(GRES, GRES)

def grid_phi(G, c, th, size, X):
    """sample canonical grids G (B,res,res) under pose (c, th, size) at points X (B,h,w,2) mm -> distance in mm"""
    sig = size[:, None, None]; co = th.cos()[:, None, None]; si = th.sin()[:, None, None]
    dx = X[..., 0] - c[:, None, None, 0]; dy = X[..., 1] - c[:, None, None, 1]
    u = (co * dx + si * dy) / sig; v = (-si * dx + co * dy) / sig; uc = u.clamp(-GL, GL); vc = v.clamp(-GL, GL)
    g = F.grid_sample(G[:, None], torch.stack([uc, vc], -1) / GL, mode='bilinear', padding_mode='border', align_corners=True)[:, 0]
    return sig * (g + ((u - uc) ** 2 + (v - vc) ** 2 + 1e-9).sqrt())

def misfit(flat, vals, hw, far):
    """Two smallest distances per board pixel from scattered samples -> (gap, overlap, m1).
    gap = sum max(m1,0)^2: uncovered board, squared distance to the nearest silhouette.
    overlap = sum max(-m2,0)^2: squared penetration depth where two silhouettes overlap.
    For a fixed set of silhouettes, the partition label = argmin_i phi_i minimises exactly this pair."""
    m1 = torch.full((hw,), float(far), device=vals.device).scatter_reduce(0, flat, vals, 'amin', include_self=True)
    rest = torch.where(vals.detach() <= m1.detach()[flat], torch.full_like(vals, BIG), vals)
    m2 = torch.full((hw,), BIG, device=vals.device).scatter_reduce(0, flat, rest, 'amin', include_self=True)
    return (m1.clamp_min(0) ** 2).sum(), ((-m2).clamp_min(0) ** 2).sum(), m1

def kernels(Gi, sizes, angles, px, kh):
    """signed distance of one piece on a (2kh+1)^2 pixel patch for every size x angle candidate"""
    off = torch.arange(-kh, kh + 1, device=DEV, dtype=torch.float32) * px; yy, xx = torch.meshgrid(off, off, indexing='ij')
    S, A = torch.meshgrid(sizes, angles, indexing='ij'); S = S.reshape(-1); A = A.reshape(-1); C = len(S)
    return grid_phi(Gi.expand(C, -1, -1), torch.zeros(C, 2, device=DEV), A, S, torch.stack([xx, yy], -1).expand(C, -1, -1, -1)), S, A

def board_maps(others, kh, px, W, H, cap):
    """padded maps around the board: gap distance to the other pieces, depth inside pieces/outside the board, occupancy"""
    h, w = others.shape; o = F.pad(others, (kh, kh, kh, kh), value=BIG)
    ax = (torch.arange(w + 2 * kh, device=DEV) - kh + .5) * px; ay = (torch.arange(h + 2 * kh, device=DEV) - kh + .5) * px
    wall = torch.minimum(torch.minimum(ax, W - ax)[None, :], torch.minimum(ay, H - ay)[:, None])       # < 0 outside the board
    obst = torch.minimum(o, wall)
    return torch.where(wall > 0, o.clamp(0, cap), torch.zeros_like(o)), (-obst).clamp_min(0), (obst < 0).float()

def dock_scores(phi_k, g, depth, occ):
    """Every pose of one piece at once (all board positions x all size/angle candidates) by FFT correlation:
    gap energy it would remove, overlap energy it would add, overlapped pixels, touching pixels, own area."""
    mid = [(a + b) / 2 for a, b in zip(EDGES[:-1], EDGES[1:])]
    bands = torch.stack([phi_k < 0] + [(phi_k >= a) & (phi_k < b) for a, b in zip(EDGES[:-1], EDGES[1:])], 1).float()   # (C,J,k,k)
    R = torch.stack([g * g] + [(g * g - m * m).clamp_min(0) for m in mid])
    Hp, Wp = g.shape; k = phi_k.shape[-1]; s = (next_fast_len(Hp), next_fast_len(Wp))
    Kf = torch.fft.rfft2(bands, s=s).conj(); Rf = torch.fft.rfft2(R, s=s); Of = torch.fft.rfft2(torch.stack([depth * depth, occ]), s=s)
    crop = lambda x: torch.fft.irfft2(x, s=s)[..., :Hp - k + 1, :Wp - k + 1]
    return crop((Kf * Rf[None]).sum(1)), crop(Kf[:, 0] * Of[0]), crop(Kf[:, 0] * Of[1]), crop((Kf[:, 1] + Kf[:, 2]) * Of[1]), bands[:, 0].sum((1, 2))

def take(score, S, A, px):
    """best (candidate, row, col) of a score volume -> centre, angle, size"""
    C, h, w = score.shape; i = int(torch.argmax(score)); k, r, col = i // (h * w), (i // w) % h, i % w
    return torch.tensor([(col + .5) * px, (r + .5) * px], device=DEV), A[k], S[k], float(score[k, r, col])

def construct(G, want, W, H, px=1.5, K=48, tol=.012, cap=12., log=None):
    """Greedy start: largest first, each piece goes to the non-overlapping pose that touches the most of what is
    already there (other pieces or the board edge). Returns poses and the number that had to be forced in."""
    n = len(G); h = round(H / px); w = round(W / px); X = centres(W, H, px); kh = int(math.ceil((.58 * float(want.max()) + EDGES[-1]) / px))
    Phi = torch.full((n, h, w), BIG, device=DEV); c = torch.zeros(n, 2, device=DEV); th = torch.zeros(n, device=DEV); size = want.clone(); forced = 0
    for i in torch.argsort(want, descending=True).tolist():
        g, depth, occ = board_maps(Phi.min(0).values, kh, px, W, H, cap); ang = (torch.arange(K, device=DEV) + torch.rand(1, device=DEV)) * (2 * math.pi / K); done = False
        for mult in (1., .92, .84):
            phi_k, S, A = kernels(G[i], want[i:i + 1] * mult, ang, px, kh); gain, ovl, cnt, touch, area = dock_scores(phi_k, g, depth, occ)
            ok = cnt <= tol * area[:, None, None]
            if bool(ok.any()):
                c[i], th[i], size[i], _ = take(torch.where(ok, touch + 2e-3 * gain / cap ** 2 + .5 * torch.rand_like(touch), torch.full_like(touch, -BIG)), S, A, px); done = True; break
        if not done: forced += 1; c[i], th[i], size[i], _ = take(gain - 40 * ovl, S, A, px)
        Phi[i] = grid_phi(G[i:i + 1], c[i:i + 1], th[i:i + 1], size[i:i + 1], X[None])[0]
    return c, th, size, forced

def reinsert(G, c, th, size, W, H, s_lo, s_hi, px=1.5, K=72, lam=40., cap=12.):
    """One sweep of exact single-piece moves: lift each piece out and put it back at the best pose anywhere on the
    board (all positions, K angles, three sizes) for the gap/overlap energy, the others held still."""
    n = len(G); X = centres(W, H, px); kh = int(math.ceil((.58 * s_hi + EDGES[-1]) / px)); c, th, size = c.clone(), th.clone(), size.clone()
    Phi = grid_phi(G, c, th, size, X[None].expand(n, -1, -1, -1)); moved = 0; mid = [(a + b) / 2 for a, b in zip(EDGES[:-1], EDGES[1:])]
    for i in torch.randperm(n).tolist():
        v, l = torch.topk(Phi, 2, dim=0, largest=False); others = torch.where(l[0] == i, v[1], v[0])
        g, depth, occ = board_maps(others, kh, px, W, H, cap); ang = (torch.arange(K, device=DEV) + torch.rand(1, device=DEV)) * (2 * math.pi / K)
        sizes = (size[i] * torch.tensor([.9, 1., 1.1], device=DEV)).clamp(s_lo, s_hi)
        phi_k, S, A = kernels(G[i], sizes, ang, px, kh); gain, ovl, cnt, touch, area = dock_scores(phi_k, g, depth, occ)
        ci, ti, si, best = take(gain - lam * ovl, S, A, px)
        # score of the pose it has now, measured the same way
        gi = g[kh:-kh, kh:-kh]; di = depth[kh:-kh, kh:-kh]; p = Phi[i]
        now = (gi * gi * (p < 0)).sum() + sum(((gi * gi - m * m).clamp_min(0) * ((p >= a) & (p < b))).sum() for m, a, b in zip(mid, EDGES[:-1], EDGES[1:])) - lam * (di * di * (p < 0)).sum()
        if best > float(now) * 1.01 + 1.:
            c[i], th[i], size[i] = ci, ti, si; Phi[i] = grid_phi(G[i:i + 1], c[i:i + 1], th[i:i + 1], size[i:i + 1], X[None])[0]; moved += 1
    return c, th, size, moved

def relax(G, c, th, size, W, H, s_lo, s_hi, steps=250, px=2.0, lam=(30., 400.), log=None):
    """All poses and sizes descend the gap/overlap energy together (rigid silhouettes, sampled distance grids)."""
    n = len(G); w = round(W / px); h = round(H / px); win = 2 * int(math.ceil((.58 * s_hi + 10) / px)); off = torch.arange(-win // 2, win // 2, device=DEV)
    oy, ox = torch.meshgrid(off, off, indexing='ij'); c = c.clone().requires_grad_(True); th = th.clone().requires_grad_(True)
    a = torch.logit(((size - s_lo) / (s_hi - s_lo)).clamp(.02, .98)).requires_grad_(True)
    opt = torch.optim.Adam([{'params': [c], 'lr': .2}, {'params': [th], 'lr': .005}, {'params': [a], 'lr': .02}])
    for it in range(steps):
        f = it / max(1, steps - 1); weight = lam[0] * (lam[1] / lam[0]) ** f; sz = s_lo + (s_hi - s_lo) * torch.sigmoid(a); jit = (torch.rand(2, device=DEV) - .5) * px
        ci = torch.floor(c.detach() / px).long(); col = ci[:, 0, None, None] + ox; row = ci[:, 1, None, None] + oy
        phi = grid_phi(G, c, th, sz, torch.stack([(col + .5) * px + jit[0], (row + .5) * px + jit[1]], -1)); ok = (col >= 0) & (col < w) & (row >= 0) & (row < h)
        gap, ovl, m1 = misfit(torch.where(ok, row * w + col, torch.zeros_like(col)).reshape(-1), torch.where(ok, phi, torch.full_like(phi, BIG)).reshape(-1), h * w, far=.5 * win * px)
        E = (gap + weight * (ovl + ((-phi).clamp_min(0) ** 2 * (~ok)).sum())) * px * px / (W * H)
        opt.zero_grad(); E.backward(); opt.step()
        if log and (it % 100 == 0 or it == steps - 1): log(f'    relax {it:4d} E {float(E):7.3f} covered {float((m1 <= 0).float().mean()):.3f} overlap {float(ovl) * px * px:8.1f}')
    return c.detach(), th.detach(), (s_lo + (s_hi - s_lo) * torch.sigmoid(a)).detach()

def measure(Phi, W, H):
    """layout quality from a stack of distance maps (n,h,w) covering the board"""
    v = torch.topk(Phi, 2, dim=0, largest=False).values; m1, m2 = v[0], v[1]; px2 = W * H / m1.numel()
    return {'covered': float((m1 <= 0).float().mean()), 'rms_gap_mm': float((m1.clamp_min(0) ** 2).mean().sqrt()), 'max_gap_mm': float(m1.max()),
            'gap_energy': float((m1.clamp_min(0) ** 2).sum() * px2 / (W * H)), 'overlap_mm2': float((m2 < 0).float().sum() * px2), 'max_overlap_depth_mm': float((-m2).clamp_min(0).max())}

def pack(G, unit_area, W, H, restarts=6, lo=.75, hi=1.3, min_size=42., log=None):
    """Layout search. Each restart: greedy construction at the highest density that fits, then alternate exact
    single-piece re-insertion sweeps (FFT over every pose) with joint gradient relaxation. Returns every restart
    as (score, centres, angles, sizes, measurements), lowest gap/overlap energy first."""
    n = len(G); s_ref = math.sqrt(.75 * W * H / float(np.sum(unit_area))); s_lo = max(min_size, lo * s_ref); s_hi = max(s_lo * 1.05, hi * s_ref); X1 = centres(W, H, 1.0); found = []
    for r in range(restarts):
        t0 = time.time(); u = .86 + .28 * torch.rand(n, device=DEV)
        for fill in (.88, .84, .80, .76, .72, .68, .64):
            want = (u * math.sqrt(fill * W * H / float(np.sum(unit_area)))).clamp(s_lo, s_hi); c, th, size, forced = construct(G, want, W, H)
            if forced == 0: break
        for sweep in range(3):
            c, th, size = relax(G, c, th, size, W, H, s_lo, s_hi, steps=220)
            if sweep < 2: c, th, size, moved = reinsert(G, c, th, size, W, H, s_lo, s_hi)
        m = measure(grid_phi(G, c, th, size, X1[None].expand(n, -1, -1, -1)), W, H); score = m['gap_energy'] + 40 * m['overlap_mm2'] / (W * H)
        if log: log(f'  layout {r}: fill {fill:.2f} forced {forced} covered {m["covered"]:.3f} rms gap {m["rms_gap_mm"]:.2f} max gap {m["max_gap_mm"]:.1f} overlap {m["overlap_mm2"]:.0f} mm2 score {score:.3f} ({time.time() - t0:.1f}s)')
        found.append((score, c, th, size, m))
    return sorted(found, key=lambda f: f[0]), (s_lo, s_hi)

# ---------------------------------------------------------------- partition -> shared vector seams

def distance_stack(polys, W, H, px, margin):
    """exact signed distance of every silhouette on the board grid near itself, BIG elsewhere -> (n,h,w)"""
    w = round(W / px); h = round(H / px); sx = W / w; sy = H / h; Phi = torch.full((len(polys), h, w), BIG, device=DEV)
    for i, xy in enumerate(polys):
        lo = xy.min(0) - margin; hi = xy.max(0) + margin
        r0, r1, c0, c1 = max(0, int(lo[1] // sy)), min(h, int(hi[1] // sy) + 1), max(0, int(lo[0] // sx)), min(w, int(hi[0] // sx) + 1)
        yy, xx = torch.meshgrid((torch.arange(r0, r1, device=DEV) + .5) * sy, (torch.arange(c0, c1, device=DEV) + .5) * sx, indexing='ij')
        Phi[i, r0:r1, c0:c1] = sdf_poly(torch.stack([xx.reshape(-1), yy.reshape(-1)], 1), T(xy)).reshape(r1 - r0, c1 - c0)
    return Phi

def connect(l1, n):
    """keep the largest 4-connected component of every label and hand stray pixels to the nearest kept label"""
    lab = l1.copy(); stray = 0
    for i in range(n):
        ys, xs = np.nonzero(lab == i)
        if not len(ys): raise ValueError(f'piece {i} owns no area')
        s = (slice(ys.min(), ys.max() + 1), slice(xs.min(), xs.max() + 1)); comp, k = ndimage.label(lab[s] == i)
        if k > 1:
            keep = 1 + int(np.argmax(ndimage.sum_labels(np.ones_like(comp), comp, range(1, k + 1)))); drop = (comp > 0) & (comp != keep)
            stray += int(drop.sum()); lab[s][drop] = -1
    if stray:
        idx = ndimage.distance_transform_edt(lab < 0, return_distances=False, return_indices=True); lab = lab[tuple(idx)]
    return lab, stray

def trace(lab, v1, l1, v2, l2, W, H, slow=None):
    """label image -> arcs between junctions. Each seam vertex sits where the two competing fields cross
    between neighbouring pixel centres, so the polylines are sub-pixel and identical for both neighbours."""
    h, w = lab.shape; px = W / w; py = H / h
    Lp = np.pad(lab, 1, constant_values=-1); own = lab == l1; slow = np.ones((h, w), np.float32) if slow is None else slow
    def cross(a, b):                                   # a, b: index tuples of the two pixels of each crack
        la, lb = lab[a], lab[b]; ka = own[a] & (l2[a] == lb); kb = own[b] & (l2[b] == la)
        da = np.where(ka, v1[a] - v2[a], np.nan)        # field of a's piece minus field of b's piece at a (<=0)
        db = np.where(kb, v2[b] - v1[b], np.nan)        # same difference at b (>=0)
        # a seam lying on a silhouette edge: one pixel inside its own silhouette, the other in the gap, and the gap
        # piece's field unknown inside the silhouette. Continue the silhouette's own distance and borrow the other.
        ea = own[a] & own[b] & ~ka & kb & (v1[a] <= 0) & (v1[b] > 0); da = np.where(ea, slow[a] * v1[a] - v1[b], da)
        eb = own[a] & own[b] & ~kb & ka & (v1[b] <= 0) & (v1[a] > 0); db = np.where(eb, v1[a] - slow[b] * v1[b], db)
        with np.errstate(invalid='ignore', divide='ignore'): t = da / (da - db)
        return np.where(np.isfinite(t), np.clip(t, .02, .98), .5)
    hz = Lp[:-1, 1:-1] != Lp[1:, 1:-1]; vt = Lp[1:-1, :-1] != Lp[1:-1, 1:]      # (h+1,w), (h,w+1)
    r, c = np.nonzero(hz); inner = (r > 0) & (r < h); t = np.full(len(r), .5); ri, ci = r[inner], c[inner]
    t[inner] = cross((ri - 1, ci), (ri, ci))
    hp = np.stack([(c + .5) * px, np.where(inner, (r - .5 + t) * py, r * py)], 1)
    hn = np.stack([r * (w + 1) + c, r * (w + 1) + c + 1], 1); hl = np.stack([Lp[r, c + 1], Lp[r + 1, c + 1]], 1)
    r, c = np.nonzero(vt); inner = (c > 0) & (c < w); t = np.full(len(r), .5); ri, ci = r[inner], c[inner]
    t[inner] = cross((ri, ci - 1), (ri, ci))
    vp = np.stack([np.where(inner, (c - .5 + t) * px, c * px), (r + .5) * py], 1)
    vn = np.stack([r * (w + 1) + c, (r + 1) * (w + 1) + c], 1); vl = np.stack([Lp[r + 1, c], Lp[r + 1, c + 1]], 1)
    pts = np.vstack([hp, vp]); ends = np.vstack([hn, vn]); pair = np.sort(np.vstack([hl, vl]), 1)
    nodes, inv = np.unique(ends, return_inverse=True); ends = inv.reshape(-1, 2); deg = np.bincount(ends.ravel(), minlength=len(nodes))
    order = np.argsort(ends.ravel(), kind='stable'); start = np.r_[0, np.cumsum(deg)]; inc = order // 2      # incident cracks per node
    nr, nc = nodes // (w + 1), nodes % (w + 1); corner = ((nr == 0) | (nr == h)) & ((nc == 0) | (nc == w)); stop = (deg != 2) | corner
    interior = pair[:, 0] >= 0; pos = np.stack([nc * px, nr * py], 1).astype(float)
    for n in np.nonzero(stop & ~corner)[0]:            # junction: mean of the incident seam crossings, kept on the border
        e = inc[start[n]:start[n + 1]]; e = e[interior[e]]
        if not len(e): continue
        p = pts[e].mean(0)
        if nr[n] in (0, h): p[1] = nr[n] * py
        if nc[n] in (0, w): p[0] = nc[n] * px
        pos[n] = p
    used = np.zeros(len(ends), bool); arcs = []
    def walk(n0, e):
        path = [e]; used[e] = True; n = ends[e, 0] + ends[e, 1] - n0
        while not stop[n]:
            a, b = inc[start[n]:start[n + 1]]; e = b if a == path[-1] else a
            if used[e]: return n0, n0, path, True
            used[e] = True; path.append(e); n = ends[e, 0] + ends[e, 1] - n
        return n0, n, path, False
    for n in np.nonzero(stop)[0]:
        for e in inc[start[n]:start[n + 1]]:
            if not used[e]: arcs.append(walk(n, e))
    for e in range(len(ends)):
        if not used[e]: arcs.append(walk(ends[e, 0], e))
    out = []
    for n0, n1, path, loop in arcs:
        xy = pts[path] if loop else np.vstack([pos[n0], pts[path], pos[n1]])
        out.append({'pair': tuple(int(v) for v in pair[path[0]]), 'xy': xy, 'loop': loop})
    return out

def smooth_arc(xy, sigma, loop=False):
    """Gaussian smoothing along an arc (sigma in mm) with both ends pinned"""
    n = len(xy)
    if sigma <= 0 or n < 4: return xy
    step = np.linalg.norm(np.diff(xy, axis=0), axis=1).mean() + 1e-9; s = sigma / step
    if loop: return gaussian_filter1d(xy, s, axis=0, mode='wrap')
    k = min(n - 1, int(3 * s) + 1); ext = np.vstack([2 * xy[0] - xy[k:0:-1], xy, 2 * xy[-1] - xy[-2:-k - 2:-1]])
    out = gaussian_filter1d(ext, s, axis=0, mode='nearest')[k:k + n]; out[0] = xy[0]; out[-1] = xy[-1]
    return out

def seams(arcs, sigma=.35, tol=.02):
    """arcs -> smoothed, simplified, 0.01 mm snapped cut lines (board edge arcs become straight)"""
    lines = []
    for a in arcs:
        xy = a['xy']
        if a['pair'][0] < 0: xy = xy[[0, -1]] if not a['loop'] else xy
        else: xy = smooth_arc(xy, sigma, a['loop'])
        if a['loop']: xy = np.vstack([xy, xy[:1]])
        l = LineString(np.round(xy, 4)).simplify(tol, preserve_topology=False)
        if l.length > 0: lines.append((a['pair'], LineString(np.round(np.array(l.coords), 2))))
    return lines

# ---------------------------------------------------------------- interlock: measurement

def contacts(faces):
    """shared boundary of every adjacent pair as (lengths, unit normals pointing from the lower index to the higher)"""
    from shapely.geometry.polygon import orient
    owners = defaultdict(list)
    for i, p in enumerate(faces):
        xy = np.array(orient(p, 1).exterior.coords)
        for a, b in zip(map(tuple, xy[:-1]), map(tuple, xy[1:])):
            if a != b: owners[(a, b) if a < b else (b, a)].append((i, a, b))
    con = defaultdict(lambda: ([], []))
    for items in owners.values():
        if len(items) != 2: continue
        (i, a, b), (j, _, _) = sorted(items)
        d = np.subtract(b, a); n = np.linalg.norm(d)
        con[(i, j)][0].append(n); con[(i, j)][1].append([d[1] / n, -d[0] / n])        # outward normal of i
    return {k: (np.array(v[0]), np.array(v[1])) for k, v in con.items()}

def lock_stats(faces, need=1.0, ndir=360):
    """In-plane translation locking with a margin, from the final polygons.
    blocked(pair, d) = projected seam length that collides when the higher-index piece slides along d.
    A pair is locked when every direction is blocked by at least `need` mm (an undercut, not just a wave).
    The assembly is locked along d when no group of pieces can slide out together: the graph "x sliding along d is
    stopped by y" must be strongly connected. Translations only; rotations and lifting are not modelled."""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components
    ang = np.arange(ndir) * 2 * np.pi / ndir; dirs = np.stack([np.cos(ang), np.sin(ang)], 1); con = contacts(faces); n = len(faces); pairs = list(con)
    block = np.array([(np.clip(-(con[k][1] @ dirs.T), 0, None) * con[k][0][:, None]).sum(0) for k in pairs]); margin = block.min(1)      # (pairs, ndir)
    lo = np.array([k[0] for k in pairs]); hi = np.array([k[1] for k in pairs]); locked = margin >= need
    comps = connected_components(csr_matrix((np.ones(int(locked.sum())), (lo[locked], hi[locked])), shape=(n, n)), directed=False)[0]
    degree = np.bincount(np.r_[lo[locked], hi[locked]], minlength=n); stop = block >= need; back = np.roll(stop, -(ndir // 2), 1); separable = 0
    for t in range(ndir):                              # hi stopped by lo along d_t; lo stopped by hi along d_t (= hi moving along -d_t)
        rows = np.r_[hi[stop[:, t]], lo[back[:, t]]]; cols = np.r_[lo[stop[:, t]], hi[back[:, t]]]
        separable += connected_components(csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n)), directed=True, connection='strong')[0] > 1
    return {'adjacent_pairs': len(pairs), 'lock_margin_required_mm': need, 'locked_pairs': int(locked.sum()), 'lock_graph_components': int(comps),
            'min_locked_neighbors': int(degree.min()), 'pieces_without_locked_neighbor': int((degree == 0).sum()),
            'directions_tested': ndir, 'directions_where_a_group_can_slide_out': int(separable), 'median_pair_lock_margin_mm': float(np.median(margin))}, dict(zip(pairs, margin.tolist()))

# ---------------------------------------------------------------- joint refinement through the PathNet decoder

def refine(k, z0, c, th, size, W, H, s_lo, s_hi, steps=260, px=1.25, lam=(150., 3000.), log=None):
    """Joint stage with the PathNet decoder inside the loop: poses, sizes and latents of every piece descend the
    gap/overlap energy together, on exact distance fields of the decoded silhouettes."""
    n = len(k); w = round(W / px); h = round(H / px); win = 2 * int(math.ceil((.58 * s_hi + 8) / px)); off = torch.arange(-win // 2, win // 2, device=DEV)
    oy, ox = torch.meshgrid(off, off, indexing='ij'); zlim = torch.where(k == 0, .28, 1.)[:, None]      # latent ranges generate.sample draws from
    c = c.clone().requires_grad_(True); th = th.clone().requires_grad_(True); a = torch.logit(((size - s_lo) / (s_hi - s_lo)).clamp(.02, .98)).requires_grad_(True)
    zeta = torch.atanh((z0 / zlim).clamp(-.97, .97)).requires_grad_(True)
    opt = torch.optim.Adam([{'params': [c], 'lr': .06}, {'params': [th], 'lr': .0015}, {'params': [a], 'lr': .006}, {'params': [zeta], 'lr': .02}])
    for it in range(steps):
        f = it / max(1, steps - 1); weight = lam[0] * (lam[1] / lam[0]) ** f; sz = s_lo + (s_hi - s_lo) * torch.sigmoid(a); jit = (torch.rand(2, device=DEV) - .5) * px
        V = flatten_t(place_t(net(k, zlim * torch.tanh(zeta)), c, th, sz), 3)
        ci = torch.floor(c.detach() / px).long(); col = ci[:, 0, None, None] + ox; row = ci[:, 1, None, None] + oy
        X = torch.stack([(col + .5) * px + jit[0], (row + .5) * px + jit[1]], -1); phi = sdf_polys(X.reshape(n, -1, 2), V).reshape(n, win, win)
        ok = (col >= 0) & (col < w) & (row >= 0) & (row < h)
        gap, ovl, m1 = misfit(torch.where(ok, row * w + col, torch.zeros_like(col)).reshape(-1), torch.where(ok, phi, torch.full_like(phi, BIG)).reshape(-1), h * w, far=.5 * win * px)
        E = (gap + weight * (ovl + ((-phi).clamp_min(0) ** 2 * (~ok)).sum())) * px * px / (W * H)
        opt.zero_grad(); E.backward(); opt.step()
        if log and (it % 65 == 0 or it == steps - 1): log(f'  refine {it:4d} E {float(E):7.3f} covered {float((m1 <= 0).float().mean()):.3f} overlap energy {float(ovl) * px * px:8.2f}')
    return c.detach(), th.detach(), (s_lo + (s_hi - s_lo) * torch.sigmoid(a)).detach(), (zlim * torch.tanh(zeta)).detach()

def outlines(k, z, c, th, size, steps=6):
    """final silhouettes in board millimetres, one (96*steps, 2) polyline per piece"""
    with torch.no_grad(): return flatten_t(place_t(net(k, z), c, th, size), steps).double().cpu().numpy()

# ---------------------------------------------------------------- first-arrival partition

def eikonal(phi, slow, px, sweeps, band, tol=None, block=0):
    """Travel time T_i of every piece's front: |grad T_i| = slow, starting on silhouette i (T = slow*phi in a thin band
    around it, which keeps the start sub-pixel exact and differentiable). Godunov upwind update, Jacobi sweeps,
    all pieces at once. Other silhouettes are obstacles. phi: (n,h,w) signed distance; slow: (h,w) shared by all
    pieces, or (n,h,w) when each piece has its own (the bay rule). With tol, sweeping stops once nothing moves.
    block > 0 (training) checkpoints the unrolled sweeps in blocks so their graph fits in GPU memory."""
    sl = slow if slow.dim() == 3 else slow[None]; solid = (phi.min(0).values <= 0)[None] & (phi > 0)
    hold = (phi <= band) | solid; Tm = torch.where((phi <= band) & ~solid, sl * phi.clamp_min(0), torch.full_like(phi, BIG)); f = sl * px; del solid
    def run(Tm, f, count):
        for _ in range(count):
            Tp = F.pad(Tm, (1, 1, 1, 1), value=BIG)
            a = torch.minimum(Tp[:, 1:-1, :-2], Tp[:, 1:-1, 2:]); b = torch.minimum(Tp[:, :-2, 1:-1], Tp[:, 2:, 1:-1]); d = a - b; del Tp
            Tn = torch.where(d.abs() < f, .5 * (a + b + torch.sqrt((2 * f * f - d * d).clamp_min(1e-8))), torch.minimum(a, b) + f); del a, b, d
            Tm = torch.where(hold, Tm, torch.minimum(Tm, Tn)); del Tn
        return Tm
    if block:
        from torch.utils.checkpoint import checkpoint
        for s in range(0, sweeps, block): Tm = checkpoint(run, Tm, f, min(block, sweeps - s), use_reentrant=False)
        return Tm
    for s in range(0, sweeps, 16):
        Tn = run(Tm, f, min(16, sweeps - s)); done = tol is not None and float((Tm - Tn).max()) < tol; Tm = Tn
        if done: break
    return Tm

def bays(polys, Phi, W, H):
    """(n,h,w) mask of every silhouette's own bays: board pixels inside its convex hull but outside the silhouette.
    These are the negative spaces that make the outline readable; the bay rule keeps a piece from filling them."""
    n, h, w = Phi.shape; sx = W / w; sy = H / h; out = torch.zeros((n, h, w), dtype=torch.bool, device=DEV)
    for i, xy in enumerate(polys):
        hull = np.array(Polygon(xy).convex_hull.exterior.coords)[:-1]; lo = hull.min(0); hi = hull.max(0)
        r0, r1, c0, c1 = max(0, int(lo[1] // sy)), min(h, int(hi[1] // sy) + 1), max(0, int(lo[0] // sx)), min(w, int(hi[0] // sx) + 1)
        yy, xx = torch.meshgrid((torch.arange(r0, r1, device=DEV) + .5) * sy, (torch.arange(c0, c1, device=DEV) + .5) * sx, indexing='ij')
        inside = sdf_poly(torch.stack([xx.reshape(-1), yy.reshape(-1)], 1), T(hull)).reshape(r1 - r0, c1 - c0) < 0
        out[i, r0:r1, c0:c1] = inside & (Phi[i, r0:r1, c0:c1] > 0)
    return out

def despike(lab, solid, n, px, r=1.5, reach=2.):
    """Fabrication repair on the label image. Gap material that a disc of radius r mm cannot enter is fragile: a lobe
    hanging on a thin neck would snap off and a thin spur would char or break. Whatever sticks out more than
    `reach` mm from the part of a piece the disc can sweep is handed to the neighbouring piece it lies against.
    A piece never loses any of its own silhouette (solid). Returns the labels and the number of pixels moved."""
    k = max(1, int(round(r / px))); p = k + 1; lab = lab.copy(); moved = 0
    disk = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 1, 2 * k + 1)); wide = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * k + 3, 2 * k + 3))
    for i in range(n):
        ys, xs = np.nonzero(lab == i)
        if not len(ys): continue
        r0, r1, c0, c1 = max(0, ys.min() - p), min(lab.shape[0], ys.max() + p + 1), max(0, xs.min() - p), min(lab.shape[1], xs.max() + p + 1)
        sub = lab[r0:r1, c0:c1]; m = np.pad((sub == i).astype(np.uint8), p)          # zero padding: the board edge erodes too
        count, core = cv2.connectedComponents(cv2.erode(m, disk), connectivity=4)
        if count < 2: continue
        main = 1 + int(np.argmax(np.bincount(core.ravel())[1:])); body = cv2.dilate((core == main).astype(np.uint8), wide) & m
        loose = m & (1 - body)
        if not loose.any(): continue
        _, rest = cv2.connectedComponents(loose, connectivity=4); far = ndimage.distance_transform_edt(1 - body) * px
        hit = np.union1d(np.unique(rest[(core > 0) & (core != main)]), np.unique(rest[far > reach])); hit = hit[hit > 0]
        if not len(hit): continue
        drop = np.isin(rest, hit)[p:-p, p:-p] & ~solid[r0:r1, c0:c1]
        near = ndimage.distance_transform_edt(sub == i, return_distances=False, return_indices=True); sub[drop] = sub[tuple(near)][drop]; moved += int(drop.sum())
    return lab, moved

def partition(polys, W, H, px=.25, logslow=None, bay=0., margin=20., sigma=.35, reach=16.):
    """Silhouettes -> pieces. Without a slowness field the label is argmin_i phi_i (unit slowness, closed form);
    with one, gap points go to the front that arrives first. bay > 0 is the bay rule: a piece's own front is
    (1 + bay) times slower inside its own bays, so a neighbour that reaches the bay mouth claims the bay as a tab
    and the piece keeps its negative space. Returns faces (one per piece, in piece order) and the shared cut
    network, vectorised once for the whole board."""
    n = len(polys); w = round(W / px); h = round(H / px); moved = 0
    with torch.no_grad():
        Phi = distance_stack(polys, W, H, px, margin); sl = None
        if logslow is None and not bay: Fm = Phi
        else:
            sl = torch.ones((h, w), device=DEV) if logslow is None else F.interpolate(logslow[None, None], size=(h, w), mode='bicubic', align_corners=False)[0, 0].exp()
            own = sl if not bay else sl[None] * (1 + bay * bays(polys, Phi, W, H))
            Fm = torch.where(Phi <= 0, Phi, eikonal(Phi, own, W / w, int(2.4 * (reach * (3 if bay else 1)) / px) + 8, 1.2 * W / w, tol=1e-3)); del own
        v, l = torch.topk(Fm, 2, dim=0, largest=False); del Fm, Phi
    v1, v2 = v[0].cpu().numpy(), v[1].cpu().numpy(); l1 = l[0].cpu().numpy().astype(np.int32); l2 = l[1].cpu().numpy().astype(np.int32)
    if (v1 >= BIG / 2).any(): raise ValueError(f'a gap is wider than the {margin} mm search margin')
    l2[v2 >= BIG / 2] = -1; lab, stray = connect(l1, n)
    if bay:                                            # tabs can hang on thin necks or end in spurs: hand those over, then re-check connectivity
        for _ in range(3):
            lab, m = despike(lab, v1 <= 0, n, px); moved += m
            if not m: break
        lab, more = connect(lab, n); stray += more
    arcs = trace(lab, v1, l1, v2, l2, W, H, None if sl is None else sl.cpu().numpy())
    for sg in (sigma, sigma / 2, .1, 0.):
        cut = union_all([ln for _, ln in seams(arcs, sg)], grid_size=.01); faces = list(polygonize(cut))
        owner = [int(lab[min(h - 1, int(p.representative_point().y * h / H)), min(w - 1, int(p.representative_point().x * w / W))]) for p in faces]
        if len(faces) == n and sorted(owner) == list(range(n)): break
    else: raise ValueError(f'vectorised seams give {len(faces)} faces for {n} pieces')
    return {'faces': [faces[i] for i in np.argsort(owner)], 'cut': cut, 'lab': lab, 'stray_px': stray, 'lobe_px': moved, 'smoothing_mm': sg, 'px': px}

# ---------------------------------------------------------------- neural slowness field

class Slowness(torch.nn.Module):
    """log-slowness u(x): Gaussian Fourier features -> MLP -> bounded scalar. It is evaluated on a coarse lattice
    and interpolated, so the field (and the seams it steers) cannot carry detail finer than the lattice."""
    def __init__(self, freqs=256, sigma=.05, width=256, umax=1.2):
        super().__init__(); nn = torch.nn; self.register_buffer('B', torch.randn(freqs, 2) * sigma * 2 * math.pi); self.umax = umax
        self.net = nn.Sequential(nn.Linear(2 * freqs, width), nn.SiLU(), nn.Linear(width, width), nn.SiLU(), nn.Linear(width, width), nn.SiLU(), nn.Linear(width, 1))
        nn.init.normal_(self.net[-1].weight, std=1e-2); nn.init.zeros_(self.net[-1].bias)
    def forward(self, X):
        p = X @ self.B.T; return self.umax * torch.tanh(self.net(torch.cat([p.sin(), p.cos()], -1))[..., 0])

def blocking(faces, ndir=360):
    """adjacent pairs and block[p, t]: projected seam length (mm) that stops the higher-index piece of pair p sliding
    along direction t, plus the length of every shared seam. The same quantity lock_stats thresholds."""
    ang = np.arange(ndir) * 2 * np.pi / ndir; dirs = np.stack([np.cos(ang), np.sin(ang)], 1); con = contacts(faces); pairs = list(con)
    return pairs, np.array([(np.clip(-(con[k][1] @ dirs.T), 0, None) * con[k][0][:, None]).sum(0) for k in pairs]), np.array([con[k][0].sum() for k in pairs])

def stops(pairs, block, n):
    """Wm[t, x, y]: seam length (mm) that stops piece x sliding along direction t against piece y"""
    ndir = block.shape[1]; Wm = np.zeros((ndir, n, n)); lo = np.array([k[0] for k in pairs]); hi = np.array([k[1] for k in pairs])
    Wm[:, hi, lo] = block.T; Wm[:, lo, hi] = np.roll(block, -(ndir // 2), 1).T
    return Wm

def loose_groups(Wm, need):
    """Groups of pieces that can still slide out. Along direction t, draw an edge x -> y where Wm[t, x, y] reaches
    `need`; a set of pieces with no edge leaving it is free to move, and those sets are the sink components of the
    graph. Returns [(t, mask of the group)]; lock_stats counts the directions that have at least one."""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components
    A = Wm >= need; out = []
    for t in range(len(A)):
        k, comp = connected_components(csr_matrix(A[t]), directed=True, connection='strong')
        if k == 1: continue
        leaves = np.zeros(k, bool); r, c = np.nonzero(A[t] & (comp[:, None] != comp[None, :])); leaves[comp[r]] = True
        out.extend((t, comp == s) for s in np.nonzero(~leaves)[0])
    return out

def lock_margin(faces, top=12., step=.125, Wm=None):
    """Assembly lock margin: the largest blocking threshold (mm, up to `top`) at which no group of pieces can slide
    out in any of 360 directions. lock_stats answers the same question for one fixed threshold."""
    if Wm is None: pairs, block, _ = blocking(faces); Wm = stops(pairs, block, len(faces))
    if not loose_groups(Wm, top): return top
    lo, hi = 0., top
    while hi - lo > step:
        mid = (lo + hi) / 2; lo, hi = (lo, mid) if loose_groups(Wm, mid) else (mid, hi)
    return lo

def seam_blocks(Fm, valid, px, dirs, tau=.1, eps=.75, cap=60.):
    """Differentiable twin of blocking(), measured on the first-arrival fields Fm (n,h,w) instead of on polygons.
    At a pixel the two earliest pieces a < b meet where D = F_a - F_b = 0. D / |grad D| is the signed distance to
    that seam and grad D its normal (from a to b), so a unit bump of width eps px around D = 0 turns the sum over
    pixels into a line integral along the seam: millimetres, whatever the resolution or the steepness of the
    fields. Differences never reach across a silhouette edge (valid marks gap and overlap pixels), where the
    other piece's field is an obstacle value. blocked[t, a * n + b] = seam length that stops b sliding along t."""
    n, h, w = Fm.shape; Fc = Fm.clamp(max=cap); idx = torch.topk(Fm.detach(), 2, dim=0, largest=False).indices
    a = torch.minimum(idx[0], idx[1])[None]; b = torch.maximum(idx[0], idx[1])[None]; diff = lambda S: S.gather(0, a)[0] - S.gather(0, b)[0]
    Fp = F.pad(Fc[None], (1, 1, 1, 1), mode='replicate')[0]; Vp = F.pad(valid[None, None].float(), (1, 1, 1, 1))[0, 0] > 0; D = diff(Fc)
    def slope(hi, lo, vh, vl):                         # central where both neighbours are usable, one-sided next to a silhouette
        return torch.where(vh & vl, (hi - lo) / 2, torch.where(vh, hi - D, torch.where(vl, D - lo, torch.zeros_like(D)))) / px
    Dx = slope(diff(Fp[:, 1:-1, 2:]), diff(Fp[:, 1:-1, :-2]), Vp[1:-1, 2:], Vp[1:-1, :-2]); Dy = slope(diff(Fp[:, 2:, 1:-1]), diff(Fp[:, :-2, 1:-1]), Vp[2:, 1:-1], Vp[:-2, 1:-1])
    g = torch.sqrt(Dx * Dx + Dy * Dy + 1e-8); e = eps * px
    rho = torch.exp(-.5 * (D / (g * e)) ** 2) / (e * math.sqrt(2 * math.pi)) * px * px * (valid & (g > 1e-3))     # seam length carried by the pixel
    t = -(Dx[None] * dirs[:, 0, None, None] + Dy[None] * dirs[:, 1, None, None]) / g[None]; pid = (a[0] * n + b[0]).reshape(-1)
    length = torch.zeros(n * n, device=Fm.device).scatter_add(0, pid, rho.reshape(-1))
    blocked = torch.zeros(len(dirs), n * n, device=Fm.device).scatter_add(1, pid[None].expand(len(dirs), -1), (F.softplus(t / tau) * tau * rho[None]).reshape(len(dirs), -1))
    return blocked, length

def train_field(polys, W, H, steps=300, px=.5, cell=2.0, headroom=1.5, bay=0., init=None, lr=2e-3, w_lock=1., w_keep=10., w_smooth=1., beta=3., reach=14., ndir=72, refresh=10, cut_px=.5, tau=(.5, .1), log=None):
    """Train the slowness network for one puzzle (init = weights of an earlier run, to continue it).
    Every `refresh` steps the current field is really cut into pieces and measured as lock_stats does. The target
    is the assembly lock margin: with the pieces holding at m mm, every group that could slide out at
    m + headroom must gain a seam that stops it. The loss takes the measured stop lengths of the seams leaving
    such a group and pushes the most promising ones up, with gradients from seam_blocks through the unrolled
    Eikonal sweeps (tau softens that gradient, wide at first so straight seams start to bend). A second term keeps
    the soft partition close to the untrained one, so the field only moves seams where that buys a lock;
    smoothness bounds the field. The field whose real cut scored best (no fabrication flags, then the largest
    lock margin) is returned."""
    from refine import flag
    n = len(polys); w = round(W / px); h = round(H / px); Phi = distance_stack(polys, W, H, px, 3 * reach).detach(); Xc = centres(W, H, cell)
    own = 1 + bay * bays(polys, Phi, W, H) if bay else None; sweeps = int(2.4 * reach * (2 if bay else 1) / px); half = ndir // 2
    solid = (Phi <= 0).sum(0); valid = (solid == 0) | (solid >= 2); weights = (1., .5, .25)
    def arrive(u): return torch.where(Phi <= 0, Phi, eikonal(Phi, u.exp() if own is None else u.exp()[None] * own, W / w, sweeps, 1.2 * W / w, block=8 if u.requires_grad else 0))
    def cut(u):                                        # the pieces this field really gives: score, summary, stop lengths, seam lengths
        try: faces = partition(polys, W, H, cut_px, logslow=u, bay=bay)['faces']
        except ValueError: return None
        pairs, block, seam = blocking(faces); Wm = stops(pairs, block, n); m = lock_margin(faces, Wm=Wm); flags = sum(flag(f) for f in faces)
        L = np.zeros((n, n)); lo = [k[0] for k in pairs]; hi = [k[1] for k in pairs]; L[lo, hi] = L[hi, lo] = seam
        return (flags, -m), f'flags {flags}, lock margin {m:.2f} mm', Wm[::len(Wm) // ndir], L, m
    field = Slowness().to(DEV)
    if init is not None: field.load_state_dict(init)
    opt = torch.optim.Adam(field.parameters(), lr=lr); sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, steps)
    ang = np.arange(ndir) * 2 * np.pi / ndir; dirs = T(np.stack([np.cos(ang), np.sin(ang)], 1)); hist = []; best = None; groups = []; Wh = shared = None; need = headroom; t0 = time.time()
    with torch.no_grad(): plain = torch.softmax(-beta * arrive(torch.zeros(h, w, device=DEV)), 0)
    for it in range(steps + 1):
        uc = field(Xc)
        if it % refresh == 0 or it == steps:
            got = cut(uc.detach())
            if got is not None:
                score, info, Wm, L, m = got; need = m + headroom; groups = loose_groups(Wm, need); Wh = T(Wm); shared = T(L); new = best is None or score < best[0]
                if new: best = (score, uc.detach().clone(), copy.deepcopy(field.state_dict()), it, info)
                if log and (new or it % 100 == 0 or it == steps): log(f'  cut {it:4d} {info}{"  <- kept" if new else ""} ({time.time() - t0:.0f}s)')
        if it == steps: break
        Fm = arrive(F.interpolate(uc[None, None], size=(h, w), mode='bicubic', align_corners=False)[0, 0]); lock = uc.new_zeros(())
        if groups:
            B = seam_blocks(Fm, valid, W / w, dirs, tau[0] * (tau[1] / tau[0]) ** (it / max(1, steps - 1)))[0].view(ndir, n, n); Ws = B.transpose(1, 2) + torch.roll(B, -half, 0)       # [t, x, y]: x sliding along t is stopped by y
            Wst = Wh + Ws - Ws.detach()                # value: measured on the real pieces; gradient: from the fields
            for t, member in groups:                   # push the most promising seams leaving the group (ties go to the longer seam)
                rows, cols = T(member, torch.bool), T(~member, torch.bool); out = Wst[t][rows][:, cols].reshape(-1)
                top = out[torch.topk(Wh[t][rows][:, cols].reshape(-1) + 1e-4 * shared[rows][:, cols].reshape(-1), min(len(weights), len(out))).indices]
                lock = lock + sum(wt * (need - v).clamp_min(0) ** 2 for wt, v in zip(weights, top)) / ndir
        keep = ((torch.softmax(-beta * Fm, 0) - plain) ** 2).sum(0).mean(); smooth = ((uc[1:] - uc[:-1]) ** 2).mean() + ((uc[:, 1:] - uc[:, :-1]) ** 2).mean()
        loss = w_lock * lock + w_keep * keep + w_smooth * smooth + 1e-3 * (uc * uc).mean()
        opt.zero_grad(); loss.backward(); opt.step(); sched.step()
        if it % 50 == 0 or it == steps - 1:
            hist.append({'step': it, 'loss': float(loss), 'lock': float(lock), 'loose_groups': len(groups), 'open_directions': len({t for t, _ in groups}), 'directions': ndir, 'need_mm': need,
                         'moved_share': float(keep), 'smooth': float(smooth), 'mean_abs_log_slowness': float(uc.abs().mean())})
            if log: log(f'  field {it:4d} loss {float(loss):.4f} lock {float(lock):.4f} loose groups {len(groups)} in {hist[-1]["open_directions"]}/{ndir} directions at {need:g} mm moved {float(keep):.4f} smooth {float(smooth):.4f} |u| {float(uc.abs().mean()):.3f} ({time.time() - t0:.0f}s)')
    if best is None: raise ValueError('no trained field gave a valid cut')
    field.load_state_dict(best[2]); return field, best[1], hist, {'step': best[3], 'info': best[4], 'lock_margin_mm': -best[0][1]}

# ---------------------------------------------------------------- measurement, export, command line

LIGHT = ['#efeee8', '#d9d8d0', '#e5e4dc', '#c9cbc3', '#f9f8f4', '#bcbeb5']

def describe(faces, polys, meta, W, H):
    """every cut piece against its own PathNet silhouette (and, for logos, against the traced reference logo)"""
    from shapely.affinity import affine_transform
    from refine import flag
    for i, (f, xy, m) in enumerate(zip(faces, polys, meta)):
        g = Polygon(xy).buffer(0); pts = np.array([f.exterior.interpolate(d).coords[0] for d in np.arange(0, f.exterior.length, .5)])
        seam = (pts[:, 0] > .05) & (pts[:, 0] < W - .05) & (pts[:, 1] > .05) & (pts[:, 1] < H - .05); dev = sdf_poly(T(pts), T(xy)).abs().cpu().numpy()
        # how much of the silhouette's own outline is still an edge of the piece, and how much of its bays the piece filled in
        near = sdf_poly(T(xy), T(np.array(f.exterior.coords)[:-1])).abs().cpu().numpy(); own_bays = g.convex_hull.difference(g).intersection(box(0, 0, W, H))
        m.update({'area_mm2': f.area, 'silhouette_area_mm2': g.area, 'silhouette_iou': f.intersection(g).area / f.union(g).area, 'silhouette_kept': f.intersection(g).area / g.area,
                  'outline_kept_1mm': float((near <= 1.).mean()), 'outline_kept_2mm': float((near <= 2.).mean()), 'own_bays_filled': f.intersection(own_bays).area / max(own_bays.area, 1e-9),
                  'edge_piece': bool((~seam).any()), 'seam_offset_mean_mm': float(dev[seam].mean()), 'seam_offset_p95_mm': float(np.percentile(dev[seam], 95)), 'seam_offset_max_mm': float(dev.max()),
                  'erosion_flag_2_5mm': bool(flag(f)), 'holes': len(f.interiors)})
        if m['k'] == 0:
            a, s, (cx, cy) = m['angle'], m['size'], m['center_mm']; r = affine_transform(ref, [math.cos(a) * s, -math.sin(a) * s, math.sin(a) * s, math.cos(a) * s, cx, cy])
            m['reference_iou'] = f.intersection(r).area / f.union(r).area; m['silhouette_reference_iou'] = g.intersection(r).area / g.union(r).area
    return meta

def colouring(faces):
    """greedy colouring of the adjacency graph in smallest-last order: neighbours always differ and a planar
    graph never needs more than six tones"""
    adj = defaultdict(set)
    for a, b in contacts(faces): adj[a].add(b); adj[b].add(a)
    left = {i: set(adj[i]) for i in range(len(faces))}; order = []
    while left:
        i = min(left, key=lambda v: len(left[v])); order.append(i)
        for j in left.pop(i): left[j].discard(i)
    col = {}
    for i in reversed(order): col[i] = next(c for c in range(len(faces)) if c not in {col.get(j) for j in adj[i]})
    return [col[i] for i in range(len(faces))]

def raster(path, W, H, layers, scale=4, edge=None):
    """anti-aliased PNG of filled polygons: layers = [(list of (S,2) arrays, list of grey levels or BGR tuples)];
    edge = a grey level draws the outlines of the first layer on top, so neighbours of similar tone stay apart"""
    img = np.full((round(H * scale), round(W * scale), 3), 255, np.uint8); pts = lambda xy: np.round(np.asarray(xy) * scale * 16).astype(np.int32)
    for shapes, tones in layers:
        for xy, t in zip(shapes, tones): cv2.fillPoly(img, [pts(xy)], (t,) * 3 if isinstance(t, int) else t, lineType=cv2.LINE_AA, shift=4)
    if edge is not None: cv2.polylines(img, [pts(xy) for xy in layers[0][0]], True, (edge,) * 3, 1, cv2.LINE_AA, shift=4)
    cv2.imwrite(str(path), img)

def sheet(path, faces, polys, meta, outline, cols=8, cell=260):
    """Contact sheet for judging pieces on their own: every cut piece alone, turned upright (the pose of its
    silhouette undone), all at one scale. outline=True draws the PathNet silhouette inside each piece."""
    def upright(xy, m):
        c, s = math.cos(-m['angle']), math.sin(-m['angle']); d = np.asarray(xy, float) - m['center_mm']; return d @ np.array([[c, s], [-s, c]])
    shapes = [upright(f.exterior.coords, m) for f, m in zip(faces, meta)]; logos = [upright(xy, m) for xy, m in zip(polys, meta)]
    k = (cell / 2 - 10) / max(np.abs(a).max() for a in shapes); rows = -(-len(faces) // cols); img = np.full((rows * cell, cols * cell, 3), 255, np.uint8)
    for i, (a, b) in enumerate(zip(shapes, logos)):
        o = np.array([(i % cols + .5) * cell, (i // cols + .5) * cell]); pts = lambda xy: np.round((xy * k + o) * 16).astype(np.int32)
        cv2.fillPoly(img, [pts(a)], (46, 46, 46), lineType=cv2.LINE_AA, shift=4)
        if outline:
            cv2.polylines(img, [pts(b)], True, (255, 255, 255), 1, cv2.LINE_AA, shift=4)
            cv2.putText(img, str(i), (int(o[0] - cell / 2 + 8), int(o[1] - cell / 2 + 20)), cv2.FONT_HERSHEY_SIMPLEX, .5, (140, 140, 140), 1, cv2.LINE_AA)
    cv2.imwrite(str(path), img)

def export(out, res, polys, meta, report, state, tag='aphex-field'):
    """write the cut network, re-read it and validate what was actually written, then previews, report and state"""
    from shapely.ops import linemerge
    from refine import readlines, flag, translation_lock_stats
    W, H = report['width_mm'], report['height_mm']; board = box(0, 0, W, H); faces = res['faces']; out.mkdir(parents=True, exist_ok=True)
    arcs = [l for l in get_parts(linemerge(res['cut'])) if l.geom_type == 'LineString']
    outsvg(out / f'{tag}-puzzle.svg', '<g fill="none" stroke="#ff0000" stroke-width="0.1" stroke-linecap="round" stroke-linejoin="round">' + ''.join(f'<path d="{points_svg(l.coords)}"/>' for l in arcs) + '</g>', W, H)
    exported = list(readlines(out / f'{tag}-puzzle.svg')); actual = list(polygonize(union_all(exported))); segments = []
    for l in exported:
        xy = list(l.coords); segments.extend(tuple(sorted((a, b))) for a, b in zip(xy, xy[1:]))
    cover = union_all(actual); gap = board.symmetric_difference(cover).area; overlap = sum(p.area for p in actual) - cover.area; dups = len(segments) - len(set(segments))
    match = [max(range(len(actual)), key=lambda j: actual[j].intersection(f).area) for f in faces]
    same = len(actual) == len(faces) and sorted(match) == list(range(len(actual))) and all(actual[j].symmetric_difference(f).area < 1e-6 for j, f in zip(match, faces))
    if not (same and gap < 1e-5 and abs(overlap) < 1e-5 and dups == 0): raise ValueError(f'exported cut network does not reproduce the pieces: faces {len(actual)}/{len(faces)} gap {gap} overlap {overlap} duplicates {dups}')
    actual = [actual[j] for j in match]; describe(actual, polys, meta, W, H); logos = [m for m in meta if m['k'] == 0]; iou = [m['silhouette_iou'] for m in meta]; off = [m['seam_offset_mean_mm'] for m in meta]
    report.update({'pieces': len(actual), 'filler_pieces': 0, 'logo_pieces': len(logos), 'holes': sum(len(p.interiors) for p in actual), 'coverage_error_mm2': gap, 'overlap_mm2': max(0, overlap),
                   'invalid_pieces': sum(not p.is_valid for p in actual), 'erosion_flags_2_5mm': sum(flag(p) for p in actual), 'min_area_mm2': min(p.area for p in actual), 'max_area_mm2': max(p.area for p in actual),
                   'duplicate_segments': dups, 'cut_length_mm': sum(l.length for l in exported), 'snap_precision_mm': .01, 'verification': 'reparsed exported SVG, noded and polygonized without tolerance snapping, compared face by face',
                   'board_inside_silhouettes': sum(m['silhouette_area_mm2'] * m['silhouette_kept'] for m in meta) / (W * H), 'mean_silhouette_iou': float(np.mean(iou)), 'min_silhouette_iou': float(np.min(iou)),
                   'mean_seam_offset_mm': float(np.mean(off)), 'max_seam_offset_mm': max(m['seam_offset_max_mm'] for m in meta), 'min_silhouette_kept': min(m['silhouette_kept'] for m in meta),
                   'mean_outline_kept_1mm': float(np.mean([m['outline_kept_1mm'] for m in meta])), 'mean_outline_kept_2mm': float(np.mean([m['outline_kept_2mm'] for m in meta])),
                   'min_outline_kept_2mm': min(m['outline_kept_2mm'] for m in meta), 'mean_own_bays_filled': float(np.mean([m['own_bays_filled'] for m in meta]))})
    if logos: report.update({'mean_logo_reference_iou': float(np.mean([m['reference_iou'] for m in logos])), 'min_final_logo_reference_iou': min(m['reference_iou'] for m in logos), 'min_logo_silhouette_reference_iou': min(m['silhouette_reference_iou'] for m in logos)})
    report['interlock'] = {'assembly_lock_margin_mm': lock_margin(actual), **{f'blocking_{need:g}mm': lock_stats(actual, need)[0] for need in (1., 2., 3.)}}
    report['interlock']['zero_clearance'] = translation_lock_stats(actual, {i for i, m in enumerate(meta) if m['k'] == 0})
    report['motifs'] = meta; (out / 'field-report.json').write_text(json.dumps(report, indent=2))
    tone = colouring(actual); dark = ['#252622' if m['k'] == 0 else '#65675e' for m in meta]
    body = ''.join(f'<path d="{poly_svg(p)}" fill="{LIGHT[tone[i] % 6]}" stroke="#161715" stroke-width="0.25" stroke-linejoin="round"/>' for i, p in enumerate(actual))
    outsvg(out / f'{tag}-puzzle-preview.svg', body + ''.join(f'<path d="{points_svg(xy)} Z" fill="{d}"/>' for xy, d in zip(polys, dark)), W, H)
    outsvg(out / f'{tag}-piece-outlines.svg', '<g fill="none" stroke="#111" stroke-width=".2">' + ''.join(f'<path id="p{i}" d="{poly_svg(p)}"/>' for i, p in enumerate(actual)) + '</g>', W, H)
    outsvg(out / f'{tag}-puzzle-score.svg', '<g fill="none" stroke-linecap="round" stroke-linejoin="round"><g stroke="#0000ff" stroke-width="0.1">' + ''.join(f'<path d="{points_svg(np.round(xy, 2))} Z"/>' for xy in polys)
           + '</g><g stroke="#ff0000" stroke-width="0.1">' + ''.join(f'<path d="{points_svg(l.coords)}"/>' for l in arcs) + '</g></g>', W, H)
    grey = [239, 217, 228, 201, 248, 188]; ext = [np.array(p.exterior.coords) for p in actual]
    raster(out / f'{tag}-puzzle.png', W, H, [(ext, [grey[t % 6] for t in tone]), (polys, [37 if m['k'] == 0 else 96 for m in meta])])
    raster(out / f'{tag}-pieces.png', W, H, [(ext, [[38, 84, 128, 168, 204, 232][t % 6] for t in tone])], edge=255)
    sheet(out / f'{tag}-piece-sheet.png', actual, polys, meta, True); sheet(out / f'{tag}-piece-sheet-plain.png', actual, polys, meta, False)
    torch.save(state, out / f'{tag}-state.pt')
    return actual

def compose(seed=22, W=300., H=220., n=32, logo_share=1., restarts=6, field_steps=100, min_size=42., px=.25, bay=None, resume=None, layouts=1, discard=False, log=print):
    """seed -> pieces: sample PathNet shapes, lay them out, refine all poses and latents together through the decoder,
    train the slowness network, split the board by first arrival, vectorise. Returns everything export() needs.
    layouts > 1 (with the bay rule): refine that many of the best-packed layouts and keep the one whose untrained
    pieces hold with the largest lock margin, since how well a board locks depends mostly on its layout.
    resume = a saved *-state.pt: keep its silhouettes and poses and continue training its slowness network
    (or, with field_steps 0, cut its stored field again); discard = True drops that stored network and field first.
    bay = None: the strength saved with a resumed board, else 32."""
    from refine import flag
    st = torch.load(resume, map_location='cpu') if resume is not None else None
    if bay is None: bay = 32. if st is None or st.get('bay') is None else float(st['bay'])
    if not (W > 0 and H > 0 and n >= 2 and 0 <= logo_share <= 1 and bay >= 0): raise ValueError('positive board dimensions, at least two pieces, logo share in [0,1] and bay >= 0 required')
    t0 = time.time(); init = prior = earlier = None; reverted = 0; tried = []
    if resume is None:
        rng = np.random.default_rng(seed); torch.manual_seed(seed); ks, zs, qs, areas = [], [], [], []
        for i in range(n):
            k = 0 if i < round(n * logo_share) else int(rng.integers(1, len(names))); q, p, z = sample(rng, k); ks.append(k); zs.append(z); qs.append(q); areas.append(p.area)
        log(f'{n} PathNet silhouettes ({sum(k == 0 for k in ks)} logos) on {W:g} x {H:g} mm, device {DEV}')
        G = torch.stack([canon(q) for q in qs]); packed, (s_lo, s_hi) = pack(G, areas, W, H, restarts, min_size=min_size, log=log); k = T(ks, torch.long); z0 = T(np.array(zs)); best = None
        for score, c0, th0, size0, _ in packed[:max(1, layouts if bay else 1)]:
            c, th, size, z = refine(k, z0, c0, th0, size0, W, H, s_lo, s_hi, log=log); reverted = 0; m = None
            for i in range(n):      # a refined latent must still decode to a valid shape that passes the sampler's own test
                p = Polygon(flatten(net(k[i:i + 1], z[i:i + 1])[0].cpu().numpy().astype(float), 6))
                if not (p.is_valid and p.area > .035 and (ks[i] != 0 or p.intersection(ref).area / p.union(ref).area >= .85)): z[i] = z0[i]; reverted += 1
            if bay and layouts > 1:                    # a quick, coarser cut of this layout: unusable (-1) or its lock margin
                try: faces = partition(outlines(k, z, c, th, size), W, H, 2 * px, bay=bay)['faces']; m = -1. if any(flag(f) for f in faces) else lock_margin(faces)
                except ValueError: m = -1.
                tried.append({'packing_score': score, 'lock_margin_mm': m}); log(f'  layout with packing score {score:.3f}: lock margin {m:.2f} mm')
            if best is None or m > best[0]: best = (m, c, th, size, z, reverted)
        _, c, th, size, z, reverted = best
    else:
        seed, W, H = st['seed'], st['W'], st['H']; torch.manual_seed(seed)
        k, z, c, th, size = (st[q].to(DEV) for q in ('k', 'z', 'c', 'th', 'size')); n = len(k); ks = k.tolist(); zs = [None] * n; init, prior = st.get('field'), st.get('log_slowness')
        record = Path(resume).with_name('field-report.json'); known = json.loads(record.read_text()) if record.is_file() else {}; earlier = known.get('field'); tried = known.get('layouts_compared', [])
        if earlier and not earlier.get('trained'): earlier = earlier.get('training_of_the_stored_field')      # a re-cut of a re-cut: keep the original record
        if discard: init = prior = earlier = None
        log(f'resumed {n} silhouettes ({sum(v == 0 for v in ks)} logos) on {W:g} x {H:g} mm from {resume}, slowness network {"continued" if init and field_steps else "started afresh" if field_steps else "discarded" if discard else "not trained"}, device {DEV}')
    polys = outlines(k, z, c, th, size); lay = measure(distance_stack(polys, W, H, 1., 60.), W, H); log(f'  silhouettes cover {lay["covered"]:.3f} of the board, rms gap {lay["rms_gap_mm"]:.2f} mm, deepest overlap {lay["max_overlap_depth_mm"]:.2f} mm')
    bisector = partition(polys, W, H, px); plain = partition(polys, W, H, px, bay=bay) if bay else bisector; res, field, uc, hist, scale, best = plain, None, None, [], 0., None
    slides = lambda faces: {'assembly_lock_margin_mm': lock_margin(faces), **{f'blocking_{need:g}mm': lock_stats(faces, need)[0] for need in (1., 2., 3.)}}; base = slides(plain['faces'])
    if field_steps: field, uc, hist, best = train_field(polys, W, H, steps=field_steps, bay=bay, init=init, lr=1e-3 if init else 2e-3, cut_px=2 * px, log=log)
    elif prior is not None: uc = prior.to(DEV)
    if uc is not None:
        for scale in (1., .7, .5, .35, .2):          # fabrication guard: weaken the field until every piece passes the erosion test
            try:
                res = partition(polys, W, H, px, logslow=uc * scale, bay=bay)
                if not any(flag(f) for f in res['faces']): break
            except ValueError as e: log(f'  field at strength {scale}: {e}')
        else: res, scale = plain, 0.; log('  field rejected by the fabrication guard, using the untrained seams')
        rank = lambda r: (sum(flag(f) for f in r['faces']), -lock_margin(r['faces']))
        if scale and rank(plain) <= rank(res): res, scale = plain, 0.; log('  the field does not beat the untrained seams on lock margin, keeping those')
    meta = [{'class': names[ks[i]], 'k': ks[i], 'size': float(size[i]), 'latent': z[i].tolist(), 'angle': float(th[i]), 'center_mm': c[i].tolist(), 'sampled_latent': None if zs[i] is None else np.asarray(zs[i]).tolist()} for i in range(n)]
    report = {'seed': seed, 'width_mm': W, 'height_mm': H, 'requested_pieces': n, 'requested_logo_pieces': sum(v == 0 for v in ks), 'device': str(DEV), 'resumed_from': None if resume is None else str(resume), 'layout': lay, 'layouts_compared': tried, 'latents_reverted': reverted,
              'size_range_mm': [float(size.min()), float(size.max())],
              'partition': {'pixel_mm': px, 'bay_rule_strength': bay, 'stray_pixels_reassigned': res['stray_px'], 'spur_pixels_reassigned': res['lobe_px'], 'seam_smoothing_mm': res['smoothing_mm']},
              'field': {'trained': bool(field_steps), 'steps': field_steps, 'continued_from_saved_weights': bool(init and field_steps), 'strength_used': scale, 'kept_step': best and best['step'], 'kept_cut': best and best['info'],
                        'parameters': sum(p.numel() for p in field.parameters()) if field else 0, 'history': hist, 'interlock_without_field': base, 'interlock_plain_bisectors': slides(bisector['faces'])}}
    state = {'seed': seed, 'k': k.cpu(), 'z': z.cpu(), 'c': c.cpu(), 'th': th.cpu(), 'size': size.cpu(), 'W': W, 'H': H, 'bay': bay, 'log_slowness': None if uc is None else (uc * scale).cpu(), 'field': init if field is None else field.state_dict()}
    if earlier and not field_steps: report['field']['training_of_the_stored_field'] = {k: earlier.get(k) for k in ('trained', 'steps', 'continued_from_saved_weights', 'kept_step', 'kept_cut', 'parameters', 'history')}
    report['seconds'] = time.time() - t0
    return res, polys, meta, report, state

if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0]); a.add_argument('--seed', type=int, default=22); a.add_argument('--width', type=float, default=300); a.add_argument('--height', type=float, default=220)
    a.add_argument('--pieces', type=int, default=32); a.add_argument('--logo-share', type=float, default=1.); a.add_argument('--restarts', type=int, default=6, help='layout attempts; --layouts of the best-packed ones are compared')
    a.add_argument('--field-steps', type=int, default=100, help='training steps for the slowness network (about 7 s each on a GTX 1070); 0 = untrained seams'); a.add_argument('--min-size', type=float, default=42., help='smallest silhouette extent in mm')
    a.add_argument('--bay', type=float, help='bay rule strength: a piece is (1 + bay) times slower inside its own bays, so neighbours fill them as tabs; 0 = plain bisector seams (default: the strength saved with a resumed board, else 32)')
    a.add_argument('--layouts', type=int, default=3, help='how many of the best-packed layouts to refine and compare on lock margin (bay rule only); 1 = take the best-packed one')
    a.add_argument('--resume', type=Path, help='a saved *-state.pt: keep its layout and continue training its slowness network (seed, size and piece count come from the file)')
    a.add_argument('--discard-field', action='store_true', help='with --resume: drop the stored slowness network and field, keep only the layout')
    a.add_argument('--output', type=Path, default=ROOT); a = a.parse_args()
    res, polys, meta, report, state = compose(a.seed, a.width, a.height, a.pieces, a.logo_share, a.restarts, a.field_steps, a.min_size, bay=a.bay, resume=a.resume, layouts=a.layouts, discard=a.discard_field, log=lambda s: print(s, flush=True))
    export(a.output, res, polys, meta, report, state); print(json.dumps({k: v for k, v in report.items() if k not in ('motifs', 'field')}, indent=1)); print(json.dumps(report['field']['interlock_without_field']))
