"""FossilNet: a conditional neural contour decoder trained on hand-drawn trilobites, corals and dendrites.

Each source drawing (white silhouette on black) becomes a shape family; mirrored drawings are families of their
own. Given a family and an 8-dimensional latent, the network predicts 192 periodic cubic B-spline controls of the
closed outline (the PathNet construction, twice the resolution so ribs and legs survive) plus the drawing's
interior features (eyes, mouth, face lines) as up to four 16-point loops for engraving. Training targets are
smooth, bounded deformations of every drawing, so the latent moves a creature through recognisable variants
rather than retrieving the stored drawing.

Every family carries a size factor: the outline is scaled so that, at a board size of 30 mm, every part of the
cut piece stays at least 3 mm wide (measured on the drawing: a 1.5 mm erosion keeps it in one piece and an
opening loses less than 3 % of its area). Fragile drawings therefore always come out larger.

    ..\\.venv\\Scripts\\python.exe fossil_net.py sources --images <folder of drawings>
    ..\\.venv\\Scripts\\python.exe fossil_net.py train
    ..\\.venv\\Scripts\\python.exe fossil_net.py sheet --out fossil-samples.png
"""
from __future__ import annotations

import argparse, json, math, time
from pathlib import Path

import cv2
import numpy as np
import torch
from scipy import ndimage
from scipy.ndimage import gaussian_filter1d
from shapely.affinity import scale as shp_scale
from shapely.geometry import Polygon
from torch import nn

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'fossil_sources'
N, Z, FEAT, FP = 192, 8, 4, 16
BASE_MM = 30.0                         # board size at which every family keeps 3 mm features after its size factor
NOTCH_KEEP = .8                        # share of a drawing's notches (bays) that must stay at least 3 mm wide, so a neighbour's tab can fill them
MAX_FACTOR = 1.8                       # cap on the size factor; notches still too narrow at the cap are engraved instead of cut

# source drawings (file prefix), category and a short name; duplicates of the upload were removed
FAMILIES = [
    ('42eb5982', 'trilobite', 'rib-skeleton'), ('b4f54f5a', 'trilobite', 'lobster'), ('76e4a972', 'trilobite', 'enrolled'),
    ('e4178fb5', 'trilobite', 'many-legs'), ('dd019b8d', 'dendrite', 'antler'), ('bfde43d5', 'trilobite', 'side-glance'),
    ('a9ef044b', 'trilobite', 'crescent'), ('6328bf8a', 'trilobite', 'scalloped'), ('2f4865e9', 'coral', 'terraces'),
    ('568f0d7e', 'trilobite', 'spined'), ('ccd026e0', 'trilobite', 'centipede'), ('6da3f0c8', 'coral', 'fork'),
    ('20f6c409', 'trilobite', 'fan-tail'),
]

# ---------------------------------------------------------------- B-spline outline helpers (as train.py, any N)

def resample(p, n):
    p = np.vstack([p, p[0]]); d = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]; d, ix = np.unique(d, return_index=True); p = p[ix]
    t = np.linspace(0, d[-1], n, endpoint=False); return np.stack([np.interp(t, d, p[:, i]) for i in range(2)], 1)


def controls(p, n=N):
    a = np.eye(n) * 4 / 6 + np.roll(np.eye(n), 1, axis=1) / 6 + np.roll(np.eye(n), -1, axis=1) / 6
    return np.linalg.solve(a, resample(p, n))


def flatten(q, steps=6):
    a, b, c, d = np.roll(q, 1, 0), q, np.roll(q, -1, 0), np.roll(q, -2, 0)
    seg = np.stack([(a + 4 * b + c) / 6, (2 * b + c) / 3, (b + 2 * c) / 3, (b + 4 * c + d) / 6], 1)
    t = np.arange(steps) / steps; w = np.stack([(1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3], 1)
    return np.einsum('tk,nkd->ntd', w, seg).reshape(-1, 2)


def bezier_path(q):
    """closed cubic Bezier SVG path of a control polygon"""
    a, b, c, d = np.roll(q, 1, 0), q, np.roll(q, -1, 0), np.roll(q, -2, 0)
    seg = np.stack([(a + 4 * b + c) / 6, (2 * b + c) / 3, (b + 2 * c) / 3, (b + 4 * c + d) / 6], 1)
    f = lambda p: ' '.join(f'{v:.4f}' for v in np.ravel(p))
    return 'M ' + f(seg[0, 0]) + ' ' + ' '.join('C ' + f(s[1:]) for s in seg) + ' Z'

# ---------------------------------------------------------------- sources

def min_extent(poly_unit, r=1.5, keep=.97):
    """smallest extent (mm) at which a 2r erosion keeps the outline in one piece and an r-opening keeps `keep` of its area"""
    for E in np.arange(16, 140, 1.):
        p = shp_scale(poly_unit, E, E, origin=(0, 0)); e = p.buffer(-r)
        if e.geom_type == 'Polygon' and not e.is_empty and e.buffer(r).area / p.area >= keep: return float(E)
    return 140.


def min_notch_extent(poly_unit, r=1.5, keep=NOTCH_KEEP):
    """smallest extent (mm) at which closing the outline with radius r fills at most 1 - keep of its bays (hull minus outline)"""
    for E in np.arange(16, 240, 2.):
        p = shp_scale(poly_unit, E, E, origin=(0, 0)); bays = p.convex_hull.difference(p).area
        if bays < 1e-9 or 1 - p.buffer(r).buffer(-r).difference(p).area / bays >= keep: return float(E)
    return 240.


def build_sources(images: Path, out: Path = DATA):
    """drawings -> fossil_sources/sources.json: outline (2048 points), interior features, size factor, provenance"""
    out.mkdir(parents=True, exist_ok=True); fams = []
    for key, category, name in FAMILIES:
        path = next(images.glob(f'{key}*.png')); im = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE); white = im > 127
        lab, k = ndimage.label(white); body = lab == 1 + int(np.argmax(ndimage.sum(white, lab, range(1, k + 1))))
        filled = ndimage.binary_fill_holes(body)
        c = max(cv2.findContours(filled.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0], key=cv2.contourArea)[:, 0].astype(float)
        outline = gaussian_filter1d(resample(c, 2048), 2., axis=0, mode='wrap')
        holes = filled & ~body; hl, hk = ndimage.label(holes); feats = []
        for j in range(1, hk + 1):
            m = (hl == j).astype(np.uint8) * 255
            if m.sum() / 255 < 60: continue                       # specks
            hc = max(cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0], key=cv2.contourArea)[:, 0].astype(float)
            feats.append(gaussian_filter1d(resample(hc, 256), 1., axis=0, mode='wrap'))
        feats = sorted(feats, key=lambda f: -Polygon(f).area)[:FEAT]
        lo, hi = outline.min(0), outline.max(0); centre = (lo + hi) / 2; ext = float((hi - lo).max())
        unit = (outline - centre) / ext; poly = Polygon(unit).buffer(0)
        need = min_extent(poly); notch = min_notch_extent(poly); factor = min(MAX_FACTOR, max(1., max(need, notch) / BASE_MM))
        fams.append(dict(key=key, category=category, name=name, source_file=path.name, image_px=list(im.shape),
                         size_factor=round(factor, 4), min_extent_3mm_features_mm=need, min_extent_3mm_notches_mm=notch, min_extent_2_5mm_features_mm=min_extent(poly, 1.25),
                         outline=np.round(unit * factor, 6).tolist(), features=[np.round((f - centre) / ext * factor, 6).tolist() for f in feats]))
        print(f'{key} {category:9s} {name:13s} factor {factor:.2f} (3 mm parts from {need:.0f} mm, 3 mm notches from {notch:.0f} mm) features {len(feats)}', flush=True)
    meta = dict(base_mm=BASE_MM, note='Hand-drawn white-on-black silhouettes supplied by the project owner. Interior black shapes are kept as engraving features.',
                families=fams)
    (out / 'sources.json').write_text(json.dumps(meta))
    return meta


def load_sources():
    meta = json.loads((DATA / 'sources.json').read_text()); fams = []
    for f in meta['families']:
        for mirror in (False, True):
            o = np.array(f['outline']); fe = [np.array(x) for x in f['features']]
            if mirror: o = o[::-1] * [-1, 1]; fe = [x[::-1] * [-1, 1] for x in fe]
            fams.append(dict(f, mirror=mirror, label=f"{f['category']}/{f['name']}{' (mirrored)' if mirror else ''}",
                             q=controls(o), feats=[resample(x, FP) for x in fe]))
    return fams

# ---------------------------------------------------------------- deformation family and network

def deform(p, z, amp=1.):
    """smooth bounded warps of an outline in unit coordinates: scale, shear, bends, waves, taper and arch"""
    x, y = p[..., 0], p[..., 1]; a, b, c, d, e, g, t, r = np.moveaxis(np.asarray(z), -1, 0)[..., None] * amp
    return np.stack([x * (1 + .12 * a + .14 * t * y) + .09 * b * y + .10 * c * np.sin(np.pi * y) + .05 * d * np.sin(2 * np.pi * y),
                     y * (1 + .12 * e) + .08 * g * x + .07 * c * np.sin(np.pi * x) - .035 * d * np.sin(2 * np.pi * x) + .10 * r * (x * x - .08)], -1)


class FossilNet(nn.Module):
    def __init__(self, k, width=320):
        super().__init__(); self.emb = nn.Embedding(k, 64)
        self.net = nn.Sequential(nn.Linear(64 + Z, width), nn.SiLU(), nn.Linear(width, width), nn.SiLU(), nn.Linear(width, width), nn.SiLU(),
                                 nn.Linear(width, (N + FEAT * FP) * 2))

    def full(self, k, z):
        out = self.net(torch.cat([self.emb(k), z], 1)); return out[:, :2 * N].reshape(-1, N, 2), out[:, 2 * N:].reshape(-1, FEAT, FP, 2)

    def forward(self, k, z):                                   # the field optimiser only needs the outline controls
        return self.full(k, z)[0]

# ---------------------------------------------------------------- training

def make_data(fams, per, rng, amp=1.):
    ks, zs, ys, fs, ms = [], [], [], [], []
    for k, f in enumerate(fams):
        z = rng.uniform(-1, 1, (per, Z)); ks.append(np.full(per, k)); zs.append(z); ys.append(deform(f['q'], z, amp))       # (per, N, 2)
        F_ = np.zeros((FEAT, FP, 2)); M = np.zeros(FEAT)
        for j, x in enumerate(f['feats']): F_[j] = x; M[j] = 1
        fs.append(deform(F_.reshape(-1, 2), z, amp).reshape(per, FEAT, FP, 2)); ms.append(np.repeat(M[None], per, 0))
    cat = lambda v, dt=np.float32: torch.from_numpy(np.concatenate(v).astype(dt))
    return cat(ks, np.int64), cat(zs), cat(ys), cat(fs), cat(ms)


def iou(a, b):
    a, b = a.buffer(0), b.buffer(0)
    return a.intersection(b).area / max(a.union(b).area, 1e-12)


def train(epochs=150, per=1600, val_per=120, batch=512, seed=7, out=DATA, log=print):
    torch.manual_seed(seed); rng = np.random.default_rng(seed); dev = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    fams = load_sources(); K = len(fams)
    k, z, y, f, m = (t.to(dev) for t in make_data(fams, per, rng)); kv, zv, yv, fv, mv = (t.to(dev) for t in make_data(fams, val_per, rng))
    model = FossilNet(K).to(dev); opt = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, 2e-3, total_steps=epochs * math.ceil(len(k) / batch)); trace = []; t0 = time.time()
    for ep in range(epochs):
        model.train(); perm = torch.randperm(len(k), device=dev); tot = 0.
        for ix in perm.split(batch):
            q, fe = model.full(k[ix], z[ix]); diff = q - y[ix]
            mse = diff.square().mean(); smooth = (diff - torch.roll(diff, 1, 1)).square().mean()
            fl = ((fe - f[ix]).square().mean((2, 3)) * m[ix]).sum() / m[ix].sum().clamp_min(1)
            loss = mse + 1.2 * smooth + .5 * fl; opt.zero_grad(); loss.backward(); opt.step(); sched.step(); tot += float(mse) * len(ix)
        if ep % 10 == 0 or ep == epochs - 1:
            model.eval()
            with torch.no_grad(): qv, fev = model.full(kv, zv); val = float((qv - yv).square().mean()); vf = float(((fev - fv).square().mean((2, 3)) * mv).sum() / mv.sum())
            trace.append(dict(epoch=ep + 1, train_mse=tot / len(k), val_mse=val, val_feature_mse=vf, seconds=round(time.time() - t0, 1))); log(trace[-1])
    model.eval(); zsafe = safe_radius(model.cpu(), fams); model.to(dev)
    torch.save(dict(state=model.state_dict(), K=K, N=N, Z=Z, FEAT=FEAT, FP=FP, labels=[x['label'] for x in fams], zsafe=zsafe), out / 'fossilnet.pt')
    # held-out evaluation in polygon terms: decoded outline against its deformed target and against the undeformed drawing
    with torch.no_grad(): qv = model(kv, zv).cpu().numpy()
    rows = {}
    for i, (kk, q) in enumerate(zip(kv.cpu().numpy(), qv)):
        p = Polygon(flatten(q)); t = Polygon(flatten(yv[i].cpu().numpy())); src = Polygon(flatten(fams[kk]['q']))
        r = rows.setdefault(fams[kk]['label'], dict(valid=0, n=0, target_iou=[], source_iou=[]))
        r['n'] += 1; ok = p.is_valid and p.area > 0; r['valid'] += ok
        if ok: r['target_iou'].append(iou(p, t)); r['source_iou'].append(iou(p, src))
    per_family = {lab: dict(valid_rate=r['valid'] / r['n'], target_iou=float(np.mean(r['target_iou'])), min_target_iou=float(np.min(r['target_iou'])),
                            source_iou=float(np.mean(r['source_iou']))) for lab, r in rows.items()}
    report = dict(architecture=f'family embedding (64) + {Z}-d latent -> MLP 3x320 SiLU -> {N} periodic cubic B-spline controls + {FEAT}x{FP} feature points',
                  parameters=sum(p.numel() for p in model.parameters()), families=K, train_contours=len(k), val_contours=len(kv), epochs=epochs, trace=trace,
                  device=str(dev), mean_val_target_iou=float(np.mean([v['target_iou'] for v in per_family.values()])),
                  min_val_target_iou=float(min(v['min_target_iou'] for v in per_family.values())),
                  valid_rate=float(np.mean([v['valid_rate'] for v in per_family.values()])), per_family=per_family,
                  safe_latent_radius={x['label']: r for x, r in zip(fams, zsafe)},
                  scope='held-out deformations of the supplied drawings; not new species, not a human recognition test')
    (out / 'fossilnet-report.json').write_text(json.dumps(report, indent=1)); return report

# ---------------------------------------------------------------- inference

_cache = {}


def safe_radius(model, fams, radii=(.6, .5, .4, .3, .25, .2, .15, .1), trials=48, min_iou=.8, share=.9, seed=3):
    """largest latent radius per family at which `share` of extreme latents (every coordinate at +-r) still decode
    to an outline with IoU >= min_iou against the drawing; the board optimiser keeps every latent inside it"""
    rng = np.random.default_rng(seed); out = []
    for k, f in enumerate(fams):
        src = Polygon(flatten(f['q'])); pick = radii[-1]
        for r in radii:
            z = torch.tensor(rng.choice([-r, r], (trials, Z)), dtype=torch.float32)
            with torch.no_grad(): qs = model(torch.full((trials,), k), z).numpy()
            if np.mean([iou(Polygon(flatten(q)), src) >= min_iou for q in qs]) >= share: pick = r; break
        out.append(float(pick))
    return out


def load(dev=None):
    dev = dev or torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    if dev not in _cache:
        ck = torch.load(DATA / 'fossilnet.pt', map_location='cpu', weights_only=True); m = FossilNet(ck['K']); m.load_state_dict(ck['state']); m.eval().to(dev)
        for p in m.parameters(): p.requires_grad_(False)
        fams = load_sources()
        for f, r in zip(fams, ck.get('zsafe') or [.6] * len(fams)): f['zsafe'] = r
        _cache[dev] = (m, fams)
    return _cache[dev]


def sample(rng, k, zr=None, min_iou=.8, dev=None):
    """draw a latent for family k (within its safe radius) whose decoded outline is valid and matches the drawing"""
    model, fams = load(torch.device('cpu')); src = Polygon(flatten(fams[k]['q'])); zr = fams[k]['zsafe'] if zr is None else zr
    for _ in range(80):
        z = rng.uniform(-zr, zr, Z)
        with torch.no_grad(): q = model(torch.tensor([k]), torch.tensor(z[None], dtype=torch.float32))[0].numpy()
        p = Polygon(flatten(q))
        if p.is_valid and iou(p, src) >= min_iou: return q, p, z
    raise ValueError(f'no valid sample for family {k}')


def sheet(out, seed=1, per=6, cell=150):
    """contact sheet: the drawing (left) and `per` network samples for every family"""
    model, fams = load(torch.device('cpu')); rng = np.random.default_rng(seed)
    img = np.full((cell * len(fams), cell * (per + 1), 3), 255, np.uint8)
    for k, f in enumerate(fams):
        shapes = [f['q']] + [sample(rng, k)[0] for _ in range(per)]
        for j, q in enumerate(shapes):
            xy = flatten(q); s = (cell - 24) / max(np.ptp(xy, 0)); o = np.array([(j + .5) * cell, (k + .5) * cell])
            cv2.fillPoly(img, [np.round(((xy - (xy.min(0) + xy.max(0)) / 2) * s + o) * 16).astype(np.int32)], (40, 40, 40) if j else (120, 60, 20), cv2.LINE_AA, shift=4)
        cv2.putText(img, f['label'], (4, k * cell + 14), cv2.FONT_HERSHEY_SIMPLEX, .4, (90, 90, 90), 1, cv2.LINE_AA)
    cv2.imwrite(str(out), img)


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0]); s = a.add_subparsers(dest='cmd', required=True)
    p = s.add_parser('sources'); p.add_argument('--images', type=Path, required=True)
    p = s.add_parser('train'); p.add_argument('--epochs', type=int, default=150); p.add_argument('--per', type=int, default=1600)
    p = s.add_parser('sheet'); p.add_argument('--out', type=Path, default=DATA / 'fossil-samples.png'); p.add_argument('--seed', type=int, default=1)
    a = a.parse_args()
    if a.cmd == 'sources': build_sources(a.images)
    elif a.cmd == 'train': r = train(a.epochs, a.per); print(json.dumps({k: v for k, v in r.items() if k not in ('per_family', 'trace')}, indent=1))
    else: sheet(a.out, a.seed)
