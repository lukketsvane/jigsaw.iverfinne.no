"""FossilNet v3: one continuous morphological latent space for fossil pieces.

A procedural morphogenesis engine grows creatures in the manner of the owner's drawings (thick strokes, rounded lobes):
arthropods with a shield, ribs or legs, spines and a tail on a straight, bent or enrolled axis; branching corals and
antlers; terraced corals; and crossbreeds that graft one plan's parts onto another. Every shape gets the 3 mm size
factor of fossil_net.build_sources (parts and notches, capped at 1.8; shapes whose parts need more are dropped). A
variational decoder learns them together with the drawings and their mirrors, so every point of its latent space is a
creature and the drawings sit in it as anchors. The decoder keeps FossilNet's output: 192 periodic cubic B-spline
controls and four 16-point engraving loops, plus one presence logit per loop.

    ..\\.venv\\Scripts\\python.exe fossil_morph.py data --count 200000
    ..\\.venv\\Scripts\\python.exe fossil_morph.py train --epochs 200
    ..\\.venv\\Scripts\\python.exe fossil_morph.py sheet
"""
import argparse, json, math, time
from multiprocessing import Pool
from pathlib import Path

import cv2
import numpy as np
import torch
from scipy import ndimage
from scipy.ndimage import gaussian_filter1d
from torch import nn

import fossil_net as fn

DATA = fn.DATA
N, FEAT, FP = fn.N, fn.FEAT, fn.FP
R = 288                                   # drawing canvas, pixels
TAU = 2 * math.pi

# ---------------------------------------------------------------- drawing tools: everything is a thick round-capped stroke

_T = [.36, 0., .5, .5]                                   # scale, rotation, centre x, centre y (canvas fractions)


def to_px(p):
    p = np.asarray(p, float); c, s = math.cos(_T[1]), math.sin(_T[1])
    q = np.stack([c * p[..., 0] - s * p[..., 1], s * p[..., 0] + c * p[..., 1]], -1) * _T[0] + _T[2:]
    return np.round(q * R * 16).astype(np.int32)


def disc(m, p, r, v=255): cv2.circle(m, tuple(int(v_) for v_ in to_px(p)), max(1, int(r * _T[0] * R * 16)), v, -1, cv2.LINE_AA, shift=4)


def stroke(m, pts, r0, r1=None, v=255):
    """stamp discs along a polyline, radius from r0 to r1: a tapering limb with round ends"""
    pts = np.asarray(pts, float); r1 = r0 if r1 is None else r1
    d = np.r_[0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]
    for s in np.arange(0, d[-1] + 1e-9, max(.004, .3 * min(r0, r1))):
        t = s / max(d[-1], 1e-9); disc(m, [np.interp(s, d, pts[:, 0]), np.interp(s, d, pts[:, 1])], r0 + (r1 - r0) * t, v)


def blob(m, p, rx, ry, ang, v=255):
    cv2.ellipse(m, tuple(int(v_) for v_ in to_px(p)), (max(1, int(rx * _T[0] * R * 16)), max(1, int(ry * _T[0] * R * 16))), math.degrees(ang + _T[1]), 0, 360, v, -1, cv2.LINE_AA, 4)


def arc(p, th, length, curv, n=24):
    """constant-curvature curve from p, heading th"""
    s = np.linspace(0, length, n); a = th + curv * s
    if abs(curv) < 1e-6: return p + np.stack([s * math.cos(th), s * math.sin(th)], 1)
    return p + np.stack([(np.sin(a) - math.sin(th)) / curv, (math.cos(th) - np.cos(a)) / curv], 1)


def along(pts, t):
    """point and heading at fraction t of a polyline"""
    d = np.r_[0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]; s = t * d[-1]; i = min(len(pts) - 2, int(np.searchsorted(d, s, 'right') - 1))
    f = (s - d[i]) / max(d[i + 1] - d[i], 1e-9); q = pts[i] + f * (pts[i + 1] - pts[i]); g = pts[i + 1] - pts[i]
    return q, math.atan2(g[1], g[0])

# ---------------------------------------------------------------- body plans

def tree(m, rng, p, th, length, r, depth, spread, ratio, bulb, tines=0.):
    """branching coral / antler limb"""
    pts = arc(p, th, length, rng.normal(0, .8)); stroke(m, pts, r, r * rng.uniform(.8, 1.)); end = pts[-1]
    if tines and depth:
        for t in np.linspace(.35, .9, rng.integers(1, 4)):
            q, h = along(pts, t); side = 1 if rng.random() < .5 else -1
            stroke(m, arc(q, h + side * rng.uniform(.5, 1.1), length * rng.uniform(.3, .6), -side * rng.uniform(0, 2)), r * .8, r * .7)
    if depth == 0:
        if bulb: disc(m, end, r * bulb)
        return
    k = rng.choice([2, 2, 2, 3]); base = th - spread * (k - 1) / 2
    for j in range(k):
        tree(m, rng, end, base + j * spread + rng.normal(0, .15), length * ratio * rng.uniform(.8, 1.2), r * rng.uniform(.8, .95), depth - 1, spread * rng.uniform(.8, 1.1), ratio, bulb, tines)


def coral(m, rng):
    r = rng.uniform(.08, .13)
    if rng.random() < .6: blob(m, [0, .62], rng.uniform(.15, .32), rng.uniform(.07, .12), rng.normal(0, .1))
    tree(m, rng, np.array([rng.normal(0, .05), .62]), -math.pi / 2 + rng.normal(0, .25), rng.uniform(.35, .6), r, rng.integers(1, 4),
         rng.uniform(.5, 1.2), rng.uniform(.55, .85), rng.choice([0, 0, 1.2, 1.4]), tines=float(rng.random() < .35))


def terraces(m, rng):
    x = rng.normal(0, .08); top = rng.uniform(-.7, -.35); stroke(m, [[x, .7], [x + rng.normal(0, .12), top]], rng.uniform(.1, .16), rng.uniform(.08, .12))
    for y in np.linspace(top, .5, rng.integers(2, 5)):
        w = rng.uniform(.18, .55); h = rng.uniform(.08, .14); cx = x + rng.normal(0, .2); tilt = rng.normal(0, .12)
        stroke(m, [[cx - w, y - w * tilt], [cx + w, y + w * tilt]], h * rng.uniform(.8, 1.2), h * rng.uniform(.8, 1.2))
        for _ in range(rng.integers(0, 4)): disc(m, [cx + rng.uniform(-w, w), y - h * .7], h * rng.uniform(.6, 1.))


def arthropod(m, rng, eyes):
    L = rng.uniform(.9, 1.6); curl = rng.choice([0., rng.normal(0, .7), rng.choice([-1, 1]) * rng.uniform(2.6, 4.4)], p=[.35, .45, .2])
    axis = arc(np.array([0., -L / 2]), math.pi / 2, L, curl, 48)
    if curl: axis -= axis.mean(0)
    W = rng.uniform(.17, .36); prof = np.interp(np.linspace(0, 1, 48), [0, .2, .55, .85, 1], [W * rng.uniform(.8, 1.3), W, W * rng.uniform(.7, 1.1), W * rng.uniform(.45, .9), W * rng.uniform(.25, .6)])
    for p, r in zip(axis, prof): disc(m, p, r)                                     # body: a fat tapering stroke
    hp, hh = along(axis, .06); nrm = np.array([-math.sin(hh), math.cos(hh)]); fwd = np.array([math.cos(hh), math.sin(hh)])
    if rng.random() < .8:                                                          # cephalon shield
        blob(m, hp + .05 * fwd, W * rng.uniform(1.1, 1.7), W * rng.uniform(.6, 1.), hh + math.pi / 2)
        if rng.random() < .3:                                                      # genal spines
            for s in (-1, 1): stroke(m, arc(hp + s * W * 1.3 * nrm, hh + s * rng.uniform(-.3, .3), L * rng.uniform(.3, .65), -s * rng.uniform(0, 1.5)), W * .3, W * .2)
    n = int(rng.choice([rng.integers(3, 8), rng.integers(6, 13)])); both = rng.random() < .82; t0, t1 = rng.uniform(.14, .3), rng.uniform(.7, .95)
    gap = (t1 - t0) * L / max(1, n - 1); rr = max(.045, min(W * .55, gap * rng.uniform(.32, .48))); ln = rng.uniform(.8, 2.4) * W
    lp = rng.choice(['flat', 'bulge', 'taper', 'grow']); fan0, fan1 = rng.uniform(-.2, .5), rng.uniform(.3, 1.2); bend = rng.uniform(-1, 2.5); tip = rng.choice([.8, 1., 1., 1.25])
    graft = rng.random() < .1
    for i, t in enumerate(np.linspace(t0, t1, n)):                                 # ribs, legs or pleural lobes
        f = i / max(1, n - 1); l = ln * {'flat': 1, 'bulge': .6 + .8 * math.sin(math.pi * f), 'taper': 1.1 - .6 * f, 'grow': .6 + .6 * f}[lp] * rng.uniform(.9, 1.1)
        q, h = along(axis, t)
        for s in ((-1, 1) if both else (1,)):
            a = h + s * (math.pi / 2 - (fan0 + (fan1 - fan0) * f))
            if graft and i % 3 == 0: tree(m, rng, q, a, l * .6, rr * 1.2, 1, .8, .7, 1.2)
            else: stroke(m, arc(q, a, l + prof[int(t * 47)], -s * bend), rr * 1.05, rr * tip)
    tq, th_ = along(axis, 1.)                                                       # tail
    kind = rng.choice(['none', 'plate', 'fan', 'spine', 'fork', 'tree'], p=[.2, .25, .2, .15, .12, .08])
    if kind == 'plate': disc(m, tq, W * rng.uniform(.6, 1.2))
    elif kind == 'fan':
        for a in np.linspace(-1, 1, rng.integers(3, 7)): stroke(m, arc(tq, th_ + a * rng.uniform(.5, 1.1), W * rng.uniform(.6, 1.2), 0), W * .32, W * .28)
    elif kind == 'spine': stroke(m, arc(tq, th_, L * rng.uniform(.25, .6), rng.normal(0, 1.5)), W * .32, W * .18)
    elif kind == 'fork':
        for s in (-1, 1): stroke(m, arc(tq, th_ + s * rng.uniform(.2, .7), L * rng.uniform(.2, .45), -s * rng.uniform(0, 2)), W * .28, W * .18)
    elif kind == 'tree': tree(m, rng, tq, th_, L * .25, W * .3, 2, .9, .7, 1.2)
    if rng.random() < .2:                                                          # antennae
        for s in (-1, 1): stroke(m, arc(hp, hh + math.pi + s * rng.uniform(.3, .8), L * rng.uniform(.2, .4), s * rng.uniform(0, 2)), W * .18, W * .15)
    if eyes is not None and rng.random() < .55:                                    # eyes: holes kept as engraving features
        d = W * rng.uniform(.4, .8); r = W * rng.uniform(.13, .22)
        for s in (-1, 1): disc(eyes, hp + s * d * nrm + .05 * fwd, r)


def _draw(rng, plan, graft):
    m = np.zeros((R, R), np.uint8); eyes = np.zeros_like(m)
    if plan == 'arthropod': arthropod(m, rng, eyes)
    elif plan == 'coral': coral(m, rng)
    else: terraces(m, rng)
    if graft:                                                                      # crossbreed: a second plan grafted on, smaller and turned
        keep = list(_T); _T[0] *= rng.uniform(.4, .65); _T[1] += rng.uniform(-math.pi, math.pi); _T[2] += rng.normal(0, .1); _T[3] += rng.normal(0, .1)
        (coral if plan != 'coral' else terraces)(m, rng); _T[:] = keep
    return m, eyes


def grow(rng):
    """one creature as (silhouette mask, eye mask), drawn twice so it fills the canvas, or None"""
    plan = rng.choice(['arthropod', 'coral', 'terraces'], p=[.68, .22, .10]); graft = rng.random() < .1
    _T[:] = [.3, 0., .5, .5]; state = rng.bit_generator.state; m, _ = _draw(rng, plan, graft)
    ys, xs = np.nonzero(m > 127)
    if not len(ys): return None
    k = .82 * R / (max(np.ptp(xs), np.ptp(ys)) + 1); cx, cy = (xs.min() + xs.max()) / 2 / R, (ys.min() + ys.max()) / 2 / R
    _T[:] = [.3 * k, 0., .5 - (cx - .5) * k, .5 - (cy - .5) * k]; rng.bit_generator.state = state; m, eyes = _draw(rng, plan, graft); _T[:] = [.3, 0., .5, .5]
    m = m > 127; e = (eyes > 127) & m
    if m[:3].any() or m[-3:].any() or m[:, :3].any() or m[:, -3:].any(): return None            # off the canvas
    lab, k = ndimage.label(m)
    if k > 1:
        sizes = ndimage.sum(m, lab, range(1, k + 1))
        if sizes.max() < .985 * sizes.sum(): return None                         # a part came loose
        m = lab == 1 + int(np.argmax(sizes))
    m = ndimage.binary_fill_holes(m)
    if e.any() and not (e <= ndimage.binary_erosion(m, iterations=3)).all(): e[:] = False      # an eye on the edge would open the outline
    return m, e

# ---------------------------------------------------------------- outline, features and the 3 mm size factor (raster twin of fossil_net)

_A = np.eye(N) * 4 / 6 + np.roll(np.eye(N), 1, axis=1) / 6 + np.roll(np.eye(N), -1, axis=1) / 6; _AI = np.linalg.inv(_A)


def _smallest(ok, lo, hi):
    """smallest integer extent in [lo, hi] mm at which the monotone test ok(extent) holds, or None"""
    if not ok(hi): return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid): hi = mid
        else: lo = mid
    return float(hi)


def factor_of(m, px=176):
    """fossil_net's size factor on a raster: 3 mm parts (2r erosion one piece, r opening keeps 97 %) and 3 mm notches"""
    ys, xs = np.nonzero(m); s = px / (max(np.ptp(xs), np.ptp(ys)) + 1)
    m = np.pad(cv2.resize(m[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8), None, fx=s, fy=s, interpolation=cv2.INTER_AREA) > 0, 8)
    ext = max(m.shape) - 16; din = ndimage.distance_transform_edt(m); dout = ndimage.distance_transform_edt(~m); area = m.sum()
    hull = cv2.fillConvexPoly(np.zeros(m.shape, np.uint8), cv2.convexHull(np.argwhere(m)[:, ::-1].astype(np.int32)), 1).astype(bool); bays = hull & ~m; nb = bays.sum()

    def parts(E):
        r = 1.5 * ext / E; e = din > r
        return e.any() and ndimage.label(e)[1] == 1 and (ndimage.distance_transform_edt(~e) <= r).sum() >= .97 * area

    def notches(E):
        r = 1.5 * ext / E
        return nb < 1 or (((ndimage.distance_transform_edt(dout <= r) > r) & bays).sum() <= (1 - fn.NOTCH_KEEP) * nb)
    need = _smallest(parts, 16, int(fn.BASE_MM * fn.MAX_FACTOR))
    if need is None: return None
    return min(fn.MAX_FACTOR, max(1., max(need, _smallest(notches, 16, 240) or 240.) / fn.BASE_MM))


def describe(m, e):
    """mask -> (controls (N,2), features (FEAT,FP,2), presence (FEAT,), size factor) in fossil_net's unit coordinates, or None"""
    factor = factor_of(m)
    if factor is None: return None                                                # parts too thin even at the largest size factor
    c = max(cv2.findContours(m.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0], key=cv2.contourArea)[:, 0].astype(float)
    outline = gaussian_filter1d(fn.resample(c, 2048), 2., axis=0, mode='wrap')
    lo, hi = outline.min(0), outline.max(0); centre = (lo + hi) / 2; ext = float((hi - lo).max())
    q = _AI @ fn.resample((outline - centre) / ext * factor, N)
    F = np.zeros((FEAT, FP, 2), np.float32); P = np.zeros(FEAT, np.float32); j = 0
    lab, k = ndimage.label(e)
    for i in range(1, k + 1):
        if j == FEAT: break
        hc = cv2.findContours((lab == i).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
        if not hc or len(hc[0]) < 8: continue
        loop = gaussian_filter1d(fn.resample(hc[0][:, 0].astype(float), 256), 1., axis=0, mode='wrap')
        F[j] = fn.resample((loop - centre) / ext * factor, FP); P[j] = 1; j += 1
    return q.astype(np.float32), F, P, factor


def _one(seed):
    rng = np.random.default_rng(seed)
    for _ in range(20):
        g = grow(rng)
        if g is None: continue
        d = describe(*g)
        if d is not None and fn.Polygon(fn.flatten(d[0])).is_valid: return d
    return None


def drawings():
    """the owner's drawings and their mirrors, exactly as FossilNet sees them"""
    out = []
    for f in fn.load_sources():
        F = np.zeros((FEAT, FP, 2), np.float32); P = np.zeros(FEAT, np.float32)
        for j, x in enumerate(f['feats'][:FEAT]): F[j] = x; P[j] = 1
        out.append((f['q'].astype(np.float32), F, P, f['size_factor'], f['label']))
    return out


def build(count, out=DATA / 'morph-data.npz', workers=None, seed=11):
    t0 = time.time(); rows = []
    with Pool(workers) as pool:
        for i, d in enumerate(pool.imap_unordered(_one, range(seed * 10 ** 7, seed * 10 ** 7 + count), chunksize=64)):
            if d is not None: rows.append(d)
            if i % 5000 == 4999: print(f'{i + 1} grown, {len(rows)} kept, {time.time() - t0:.0f}s', flush=True)
    q, F, P, s = (np.stack(v) for v in zip(*rows))
    np.savez_compressed(out, q=q, F=F, P=P, factor=np.array(s, np.float32))
    print(f'{len(q)} creatures -> {out} ({time.time() - t0:.0f}s)')

# ---------------------------------------------------------------- the variational decoder

def mlp(sizes):
    layers = []
    for a, b in zip(sizes, sizes[1:]): layers += [nn.Linear(a, b), nn.SiLU()]
    return nn.Sequential(*layers[:-1])


class Morph(nn.Module):
    """encoder (outline, loops, presence) -> latent; decoder latent -> 2N controls, FEAT*FP*2 loop points, FEAT presence logits"""
    def __init__(self, Z=32, width=512):
        super().__init__(); D = 2 * N + 2 * FEAT * FP + FEAT; self.Z = Z
        self.enc = mlp([D, width, width, 2 * Z]); self.dec = mlp([Z, width, width, width, width, D])

    def decode(self, z):
        o = self.dec(z); B = len(z)
        return o[:, :2 * N].reshape(B, N, 2), o[:, 2 * N:2 * N + 2 * FEAT * FP].reshape(B, FEAT, FP, 2), o[:, -FEAT:]

    def forward(self, x):
        mu, lv = self.enc(x).chunk(2, 1); z = mu + torch.randn_like(mu) * (.5 * lv).exp()
        return self.decode(z), mu, lv


def pack(q, F, P): return torch.cat([q.flatten(1), F.flatten(1), P], 1)


def canon(q):
    """same orientation and start for every outline: counter-clockwise on screen, starting at the topmost control"""
    x, y = q[..., 0], q[..., 1]; flip = (x * np.roll(y, -1, -1) - np.roll(x, -1, -1) * y).sum(-1) > 0
    q = np.where(flip[:, None, None], q[:, ::-1], q); i = np.argmin(q[..., 1] + 1e-3 * q[..., 0], -1)
    return q[np.arange(len(q))[:, None], (np.arange(q.shape[1])[None] + i[:, None]) % q.shape[1]]


def train(epochs=60, Z=32, width=512, batch=512, beta=(1e-7, 3e-6), warps=200, seed=5, out=DATA / 'fossilnet-v3.pt', log=print):
    torch.manual_seed(seed); rng = np.random.default_rng(seed); dev = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    d = np.load(DATA / 'morph-data.npz'); draw = drawings()
    q = [d['q']]; F = [d['F']]; P = [d['P']]
    for dq, dF, dP, _, _ in draw:                                                  # the drawings, each with `warps` gentle FossilNet deformations
        z = rng.uniform(-.6, .6, (warps, fn.Z)); z[0] = 0
        q.append(fn.deform(dq, z).astype(np.float32)); F.append(fn.deform(dF.reshape(-1, 2), z).reshape(warps, FEAT, FP, 2).astype(np.float32)); P.append(np.repeat(dP[None], warps, 0))
    q = [canon(np.concatenate(q)).astype(np.float32)]; draw = [(canon(x[0][None])[0], *x[1:]) for x in draw]
    q, F, P = (torch.from_numpy(np.concatenate(v)).to(dev) for v in (q, F, P))
    n = len(q); val = torch.from_numpy(rng.permutation(n)[:min(4000, n // 20)]).to(dev); tr = torch.ones(n, dtype=torch.bool, device=dev); tr[val] = False; tri = tr.nonzero()[:, 0]
    model = Morph(Z, width).to(dev); opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-5)
    steps = epochs * math.ceil(len(tri) / batch); sched = torch.optim.lr_scheduler.OneCycleLR(opt, 1e-3, total_steps=steps); step = 0; t0 = time.time(); trace = []

    def loss_of(ix, b):
        (oq, oF, oP), mu, lv = model(pack(q[ix], F[ix], P[ix])); diff = oq - q[ix]
        rec = diff.square().mean(); smooth = (diff - torch.roll(diff, 1, 1)).square().mean()
        fl = ((oF - F[ix]).square().mean((2, 3)) * P[ix]).sum() / P[ix].sum().clamp_min(1); pl = nn.functional.binary_cross_entropy_with_logits(oP, P[ix])
        kl = (-.5 * (1 + lv - mu.square() - lv.exp())).sum(1).mean()
        return rec + 1.2 * smooth + .5 * fl + .02 * pl + b * kl, rec, kl
    for ep in range(epochs):
        model.train(); perm = tri[torch.randperm(len(tri), device=dev)]
        for ix in perm.split(batch):
            b = beta[0] + (beta[1] - beta[0]) * min(1, step / (.4 * steps)); L, rec, kl = loss_of(ix, b)
            opt.zero_grad(); L.backward(); nn.utils.clip_grad_norm_(model.parameters(), 1.); opt.step(); sched.step(); step += 1
        if ep % 5 == 0 or ep == epochs - 1:
            model.eval()
            with torch.no_grad(): _, vr, vk = loss_of(val, beta[1])
            trace.append(dict(epoch=ep + 1, val_mse=float(vr), val_kl=float(vk), seconds=round(time.time() - t0))); log(trace[-1])
    model.eval().cpu()
    with torch.no_grad():
        mu = torch.cat([model.enc(pack(*(t.cpu() for t in (q[i:i + 4096], F[i:i + 4096], P[i:i + 4096])))).chunk(2, 1)[0] for i in range(0, n, 4096)])
        anchors = model.enc(pack(*(torch.from_numpy(np.stack(v)) for v in list(zip(*draw))[:3]))).chunk(2, 1)[0]
    bank = mu[torch.from_numpy(rng.choice(n, min(2048, n), replace=False))]        # codes of real creatures: sampling near them stays on the creatures
    torch.save(dict(state=model.state_dict(), Z=Z, width=width, N=N, FEAT=FEAT, FP=FP, mean=mu.mean(0), std=mu.std(0), bank=bank, anchors=anchors,
                    labels=[x[4] for x in draw], factors=[float(x[3]) for x in draw], trace=trace, creatures=n), out)
    log(f'saved {out}')


def sheet(out=DATA / 'fossil-morph-samples.png', seed=1, cols=12, rows=8, cell=120):
    """creatures near the latent bank (top rows) and walks between drawings (bottom rows)"""
    ck = torch.load(DATA / 'fossilnet-v3.pt', weights_only=False); model = Morph(ck['Z'], ck['width']); model.load_state_dict(ck['state']); model.eval()
    g = torch.Generator().manual_seed(seed); b = ck['bank']; z = b[torch.randint(len(b), (cols * rows // 2,), generator=g)] + .15 * ck['std'] * torch.randn(cols * rows // 2, ck['Z'], generator=g)
    a = ck['anchors']; walks = torch.cat([a[i] + (a[(i * 7 + 3) % len(a)] - a[i]) * torch.linspace(0, 1, cols)[:, None] for i in range(0, 2 * (rows // 2), 2)])
    with torch.no_grad(): qs = model.decode(torch.cat([z, walks]))[0].numpy()
    img = np.full((rows * cell, cols * cell), 255, np.uint8)
    for i, q in enumerate(qs):
        xy = fn.flatten(q); s = (cell - 16) / max(np.ptp(xy, 0)); o = np.array([(i % cols + .5) * cell, (i // cols + .5) * cell])
        cv2.fillPoly(img, [np.round(((xy - (xy.min(0) + xy.max(0)) / 2) * s + o) * 16).astype(np.int32)], 40, cv2.LINE_AA, shift=4)
    cv2.imwrite(str(out), img); print(out)


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0]); s = a.add_subparsers(dest='cmd', required=True)
    p = s.add_parser('data'); p.add_argument('--count', type=int, default=200000); p.add_argument('--workers', type=int, default=None)
    p = s.add_parser('train'); p.add_argument('--epochs', type=int, default=200); p.add_argument('--latent', type=int, default=32); p.add_argument('--width', type=int, default=512)
    p = s.add_parser('sheet'); p.add_argument('--seed', type=int, default=1)
    a = a.parse_args()
    if a.cmd == 'data': build(a.count, workers=a.workers)
    elif a.cmd == 'train': train(a.epochs, a.latent, a.width)
    else: sheet(seed=a.seed)
