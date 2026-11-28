"""FossilNet v3: one continuous latent space over a broad fossil morphospace, drawn in the owner's style.

Data: the procedurally grown corpus (morph.corpus: trilobites, other arthropods, shells, echinoderms, corals,
dendrites, fronds, Cambrian oddities and crossbreeds between them), plus the owner's 13 drawings and their mirrors
as anchors, each augmented with fossil_net v2's smooth deformations. Every outline is a closed curve at extent 1,
centred on its box, with its start at the top of the vertical centre line, as 256 periodic cubic B-spline
controls.

Model: a variational autoencoder. The encoder (circular convolutions over the controls, an MLP over the engraving
loops) is used only in training and to place the anchors. The decoder maps a 32-d latent to the outline controls,
up to six engraving loops (with presence logits) and the 3 mm size factor. It is a plain residual MLP, so the
web page can run it.

    ..\\.venv\\Scripts\\python.exe fossil_net3.py anchors
    ..\\.venv\\Scripts\\python.exe fossil_net3.py train --hours 3 --follow
    ..\\.venv\\Scripts\\python.exe fossil_net3.py export
"""
from __future__ import annotations

import argparse, json, math, os, time
from pathlib import Path

import cv2
import numpy as np
import torch
from torch import nn
import torch.nn.functional as F

from morph.core import FEAT, FP, N, canonical, controls, flatten, measure, raster_iou, resample, signed_area

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'fossil_sources' / 'v3'
RUNS = ROOT.parent / 'runs' / 'fossil-v3'
Z = 32
CHIMERA = .12                  # share of crossbreeds in a training batch (before the anchors)
DEV = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
torch.backends.cuda.matmul.allow_tf32 = True; torch.backends.cudnn.allow_tf32 = True

# ---------------------------------------------------------------- anchors: the owner's drawings, upright

ORIENT = {   # how to stand each drawing up: 'sym' = mirror-symmetry axis vertical, 'pca' = long axis vertical, 'none' = as drawn, a number = turn by that many degrees
    'rib-skeleton': 'sym', 'lobster': 'sym', 'enrolled': 'none', 'many-legs': -16., 'antler': 'none', 'side-glance': 'pca',
    'crescent': 'sym', 'scalloped': 'pca', 'terraces': 'none', 'spined': 'sym', 'centipede': 'pca', 'fork': 'none', 'fan-tail': 'sym'}


def _rot(p, a):
    c, s = math.cos(a), math.sin(a); return p @ np.array([[c, s], [-s, c]])


def _upright(o, feats, mode):
    """rotate a unit outline (and its features) so its axis is vertical and the wider end (the head) is on top"""
    if mode == 'none': return o, feats, 0.
    c = o.mean(0)
    if not isinstance(mode, str):                                # a fixed turn in degrees
        a = math.radians(mode); return _rot(o - c, a) + c, [_rot(f - c, a) + c for f in feats], a
    if mode == 'sym':
        best = (-1, 0.)
        for th in np.radians(np.arange(0, 180, 1.)):
            u = np.array([math.cos(th), math.sin(th)]); d = o - c; r = c + 2 * (d @ u)[:, None] * u - d
            best = max(best, (raster_iou(o, r[::-1], 1 / 128), th))
        th = best[1]
    else:
        w, v = np.linalg.eigh(np.cov((o - c).T)); u = v[:, np.argmax(w)]; th = math.atan2(u[1], u[0])
    a = math.pi / 2 - th                                         # axis direction -> (0, 1)
    def turn(p, a): return _rot(p - c, a) + c
    q = turn(o, a); lo, hi = q.min(0), q.max(0); h = hi[1] - lo[1]
    top = np.ptp(q[q[:, 1] < lo[1] + .3 * h, 0]); bot = np.ptp(q[q[:, 1] > hi[1] - .3 * h, 0])
    if bot > top: a += math.pi
    return turn(o, a), [turn(f, a) for f in feats], a


def _order(feats):
    """engraving loops in a stable order: top to bottom in bands, then left to right (so eye pairs line up)"""
    key = lambda f: (round(float(f[:, 1].mean()) / .1), float(f[:, 0].mean()))
    return sorted(feats, key=key)


def _loop(f):
    """an engraving loop: positive orientation, start at its topmost point, FP points"""
    f = resample(f, 128)
    if signed_area(f) < 0: f = f[::-1]
    f = np.roll(f, -int(np.argmin(f[:, 1])), 0); return resample(f, FP)


def build_anchors(images=ROOT.parent / 'runs' / 'fossil-sources' / 'raw', out=OUT):
    """the 13 drawings -> anchors.npz: unit outline controls, engraving loops, size factor, upright rotation"""
    from scipy import ndimage
    from scipy.ndimage import gaussian_filter1d
    from fossil_net import FAMILIES
    out.mkdir(parents=True, exist_ok=True); rows = []
    for key, category, name in FAMILIES:
        path = next(images.glob(f'{key}*.png')); im = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE); white = im > 127
        lab, k = ndimage.label(white); body = lab == 1 + int(np.argmax(ndimage.sum(white, lab, range(1, k + 1))))
        filled = ndimage.binary_fill_holes(body)
        c = max(cv2.findContours(filled.astype(np.uint8) * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0], key=cv2.contourArea)[:, 0].astype(float)
        o = gaussian_filter1d(resample(c, 2048), 2., axis=0, mode='wrap')
        holes = filled & ~body; hl, hk = ndimage.label(holes); feats = []
        for j in range(1, hk + 1):
            m = (hl == j).astype(np.uint8) * 255
            if m.sum() / 255 < 60: continue
            hc = max(cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0], key=cv2.contourArea)[:, 0].astype(float)
            feats.append(gaussian_filter1d(resample(hc, 256), 1., axis=0, mode='wrap'))
        lo, hi = o.min(0), o.max(0); ctr = (lo + hi) / 2; ext = float((hi - lo).max()); o = (o - ctr) / ext; feats = [(f - ctr) / ext for f in feats]
        o, feats, ang = _upright(o, feats, ORIENT[name])
        lo, hi = o.min(0), o.max(0); ctr = (lo + hi) / 2; ext = float((hi - lo).max())
        unit = canonical((o - ctr) / ext); fl = [(f - ctr) / ext for f in feats]
        e_feat, e_notch, sf = measure(unit)
        for mirror in (False, True):
            u = unit.copy(); fs = [f.copy() for f in fl]
            if mirror: u = canonical(u[::-1] * [-1, 1]); fs = [f[::-1] * [-1, 1] for f in fs]
            fs = _order([_loop(f) for f in fs])[:FEAT]
            rows.append(dict(label=f'{category}/{name}{" (mirrored)" if mirror else ""}', key=key, category=category, name=name, mirror=mirror,
                             outline=u, feats=fs, sf=sf, e_feat=e_feat, e_notch=e_notch, angle=ang))
        print(f'{key} {category:9s} {name:13s} turned {math.degrees(ang):6.1f} deg  size factor {sf:.2f} (parts {e_feat:.0f} mm, notches {e_notch:.0f} mm)  features {len(feats)}', flush=True)
    q = np.stack([controls(r['outline']) for r in rows]).astype(np.float32)
    f = np.zeros((len(rows), FEAT, FP, 2), np.float32); fm = np.zeros((len(rows), FEAT), np.float32)
    for i, r in enumerate(rows):
        for j, lp in enumerate(r['feats']): f[i, j] = lp; fm[i, j] = 1
    np.savez(out / 'anchors.npz', q=q, f=f, fm=fm, sf=np.array([r['sf'] for r in rows], np.float32), outline=np.stack([r['outline'] for r in rows]).astype(np.float32),
             labels=np.array([r['label'] for r in rows]), angle=np.array([r['angle'] for r in rows], np.float32))
    sheet_anchors(out / 'anchors.npz', RUNS / 'anchors.png')
    return rows


def sheet_anchors(path, png, cell=170):
    d = np.load(path); n = len(d['labels']); cols = 6; img = np.zeros((-(-n // cols) * (cell + 16), cols * cell, 3), np.uint8)
    for i in range(n):
        r, c = divmod(i, cols); o = np.array([c * cell + cell / 2, r * (cell + 16) + 16 + cell / 2]); s = cell - 20
        cv2.fillPoly(img, [np.round((flatten(d['q'][i].astype(float)) * s + o) * 16).astype(np.int32)], (255, 255, 255), cv2.LINE_AA, shift=4)
        for j in range(FEAT):
            if d['fm'][i, j]: cv2.fillPoly(img, [np.round((d['f'][i, j] * s + o) * 16).astype(np.int32)], (0, 0, 0), cv2.LINE_AA, shift=4)
        cv2.circle(img, tuple(np.round(d['q'][i, 0] * s + o).astype(int)), 3, (60, 120, 255), -1)
        cv2.putText(img, f"{d['labels'][i]} {d['sf'][i]:.2f}", (c * cell + 3, r * (cell + 16) + 12), cv2.FONT_HERSHEY_SIMPLEX, .32, (130, 170, 230), 1, cv2.LINE_AA)
    png.parent.mkdir(parents=True, exist_ok=True); cv2.imwrite(str(png), img)


def deform(p, z, amp=1.):
    """fossil_net v2's smooth bounded warps (scale, shear, bends, waves, taper, arch) in unit coordinates"""
    x, y = p[..., 0], p[..., 1]; a, b, c, d, e, g, t, r = np.moveaxis(np.asarray(z), -1, 0)[..., None] * amp
    return np.stack([x * (1 + .12 * a + .14 * t * y) + .09 * b * y + .10 * c * np.sin(np.pi * y) + .05 * d * np.sin(2 * np.pi * y),
                     y * (1 + .12 * e) + .08 * g * x + .07 * c * np.sin(np.pi * x) - .035 * d * np.sin(2 * np.pi * x) + .10 * r * (x * x - .08)], -1)


def _anchor_variant(args):
    outline, feats, sf, seed, amp = args
    rng = np.random.default_rng(seed); z = rng.uniform(-1, 1, 8); o = deform(outline, z, amp); fs = [deform(f, z, amp) for f in feats]
    lo, hi = o.min(0), o.max(0); ctr = (lo + hi) / 2; ext = float((hi - lo).max())
    u = canonical((o - ctr) / ext); fs = _order([_loop((f - ctr) / ext) for f in fs])
    try: sf = measure(u)[2]
    except Exception: pass                                      # noqa: BLE001 (keep the drawing's factor)
    return controls(u).astype(np.float32), fs, float(sf)


def anchor_pool(per=320, amp_max=.6, workers=6):
    """deformed variants of every anchor (with their own 3 mm size factors), cached in anchor-pool.npz"""
    path = OUT / 'anchor-pool.npz'
    if path.exists(): return dict(np.load(path))
    d = np.load(OUT / 'anchors.npz'); jobs = []
    for i in range(len(d['labels'])):
        feats = [d['f'][i, j].astype(float) for j in range(FEAT) if d['fm'][i, j]]
        for s in range(per): jobs.append((d['outline'][i].astype(float), feats, float(d['sf'][i]), [i, s], amp_max * (s / per) ** .7))
    from multiprocessing import Pool
    with Pool(workers) as pool: res = pool.map(_anchor_variant, jobs, chunksize=64)
    q = np.stack([r[0] for r in res]); f = np.zeros((len(res), FEAT, FP, 2), np.float32); fm = np.zeros((len(res), FEAT), np.float32)
    for i, r in enumerate(res):
        for j, lp in enumerate(r[1][:FEAT]): f[i, j] = lp; fm[i, j] = 1
    out = dict(q=q, f=f, fm=fm, sf=np.array([r[2] for r in res], np.float32), anchor=np.repeat(np.arange(len(d['labels'])), per))
    np.savez(path, **out); return out

# ---------------------------------------------------------------- corpus

class Corpus:
    """all corpus shards in one set of tensors (float16 on the GPU); refresh() picks up shards written since"""

    def __init__(self, root, val_every=50, dev=DEV):
        self.root = Path(root); self.dev = dev; self.val_every = val_every; self.seen = set(); self.names = []
        self.parts = {'train': [], 'val': []}; self.q = {}; self.f = {}; self.fm = {}; self.sf = {}; self.plan = {}
        self.refresh()

    def refresh(self):
        new = 0
        for p in sorted(self.root.glob('shard-*.npz')):
            if p.name in self.seen: continue
            try: d = np.load(p)
            except Exception: continue                                # noqa: BLE001 (being written)
            names = [str(x) for x in d['names']]; plan = np.array([self._name(names[a]) if b < 0 else self._name('chimera') for a, b in zip(d['plan'], d['plan2'])], np.int16)
            f, fm = _sort_feats(d['f'].astype(np.float32), d['fm'].astype(np.float32))
            split = 'val' if int(p.stem.split('-')[1]) % self.val_every == 0 else 'train'
            self.parts[split].append((d['q'], f.astype(np.float16), fm.astype(np.uint8), d['sf'], plan)); self.seen.add(p.name); new += len(plan)
        if new:
            for split, parts in self.parts.items():
                if not parts: continue
                cat = lambda i: np.concatenate([x[i] for x in parts])
                self.q[split] = torch.from_numpy(cat(0)).to(self.dev); self.f[split] = torch.from_numpy(cat(1)).to(self.dev)
                self.fm[split] = torch.from_numpy(cat(2)).to(self.dev); self.sf[split] = torch.from_numpy(cat(3).astype(np.float32)).to(self.dev)
                self.plan[split] = torch.from_numpy(cat(4).astype(np.int64)).to(self.dev)
        return new

    def _name(self, n):
        if n not in self.names: self.names.append(n)
        return self.names.index(n)

    def size(self, split='train'):
        return 0 if split not in self.q else len(self.q[split])

    def weights(self, split='train'):
        """row weights that give every module group its corpus share (morph.plans.GROUPS, crossbreeds CHIMERA)"""
        if getattr(self, '_w', {}).get(split, (None, -1))[1] == self.size(split): return self._w[split][0]
        from morph.plans import GROUPS
        share = dict(GROUPS, chimera=CHIMERA); grp = [n.split('.')[0] for n in self.names]
        gid = torch.tensor([sorted(share).index(g) if g in share else len(share) for g in grp], device=self.dev)[self.plan[split]]
        cnt = torch.bincount(gid, minlength=len(share) + 1).float()
        tgt = torch.tensor([share[g] for g in sorted(share)] + [.02], device=self.dev); tgt = tgt * (cnt > 0); tgt = tgt / tgt.sum()
        w = (tgt / cnt.clamp_min(1))[gid]; self._w = getattr(self, '_w', {}); self._w[split] = (w, self.size(split)); return w

    def batch(self, n, split='train', gen=None, balanced=True):
        ix = torch.multinomial(self.weights(split), n, replacement=True, generator=gen) if balanced else torch.randint(0, self.size(split), (n,), device=self.dev, generator=gen)
        return self.q[split][ix].float(), self.f[split][ix].float(), self.fm[split][ix].float(), self.sf[split][ix], self.plan[split][ix]


def _sort_feats(f, fm):
    """numpy: engraving loops in band order (top to bottom, then left to right); absent loops last"""
    cy, cx = f[..., 1].mean(-1), f[..., 0].mean(-1); key = np.round(cy / .1) * 10 + cx; key = np.where(fm > 0, key, 1e9)
    ix = np.argsort(key, 1, kind='stable'); return np.take_along_axis(f, ix[..., None, None], 1), np.take_along_axis(fm, ix, 1)


def _sort_feats_t(f, fm):
    cy, cx = f[..., 1].mean(-1), f[..., 0].mean(-1); key = torch.round(cy / .1) * 10 + cx; key = torch.where(fm > 0, key, torch.full_like(key, 1e9))
    ix = torch.argsort(key, dim=1, stable=True); return torch.gather(f, 1, ix[..., None, None].expand_as(f)), torch.gather(fm, 1, ix)


def mirror_t(q, f, fm):
    """reflect across the vertical centre line keeping the canonical start (index 0) and orientation"""
    q = torch.flip(torch.roll(q, -1, 1), [1]); q = torch.roll(q, 1, 1) * torch.tensor([-1., 1.], device=q.device)
    f = torch.flip(torch.roll(f, -1, 2), [2]); f = torch.roll(f, 1, 2) * torch.tensor([-1., 1.], device=f.device)
    f, fm = _sort_feats_t(f, fm); return q, f, fm

# ---------------------------------------------------------------- model

class Encoder(nn.Module):
    def __init__(self, z=Z, w=1024):
        super().__init__()
        ch = [2, 64, 128, 256, 256, 256]
        self.conv = nn.ModuleList([nn.Conv1d(a, b, 5, stride=2, padding=2, padding_mode='circular') for a, b in zip(ch[:-1], ch[1:])])
        self.feat = nn.Sequential(nn.Linear(FEAT * (FP * 2 + 1) + 1, 256), nn.SiLU())
        self.mlp = nn.Sequential(nn.Linear(256 * (N // 32) + 256, w), nn.SiLU(), nn.Linear(w, w), nn.SiLU(), nn.Linear(w, 2 * z))

    def forward(self, q, f, fm, sf):
        h = q.transpose(1, 2)
        for c in self.conv: h = F.silu(c(h))
        g = self.feat(torch.cat([(f * fm[..., None, None]).flatten(1), fm, torch.log(sf)[:, None]], 1))
        mu, lv = self.mlp(torch.cat([h.flatten(1), g], 1)).chunk(2, 1); return mu, lv.clamp(-12, 6)


class Decoder(nn.Module):
    """z -> [256 x 2 controls | 6 x 24 x 2 engraving points | 6 presence logits | log size factor]; residual MLP"""
    OUT = N * 2 + FEAT * FP * 2 + FEAT + 1

    def __init__(self, z=Z, w=1024, blocks=4):
        super().__init__(); self.inp = nn.Linear(z, w); self.blocks = nn.ModuleList([nn.Linear(w, w) for _ in range(blocks)]); self.out = nn.Linear(w, self.OUT)
        for b in self.blocks: nn.init.normal_(b.weight, 0, .5 / math.sqrt(w)); nn.init.zeros_(b.bias)
        nn.init.normal_(self.out.weight, 0, .2 / math.sqrt(w)); nn.init.zeros_(self.out.bias)
        with torch.no_grad():                                        # start from a circle of radius 0.4
            t = torch.arange(N) / N * 2 * math.pi; circ = torch.stack([.4 * torch.sin(t), -.4 * torch.cos(t)], 1)
            self.out.bias[:2 * N] = circ.flatten()

    def forward(self, z):
        h = F.silu(self.inp(z))
        for b in self.blocks: h = h + F.silu(b(h))
        o = self.out(h); n0 = 2 * N; n1 = n0 + FEAT * FP * 2
        return o[:, :n0].reshape(-1, N, 2), o[:, n0:n1].reshape(-1, FEAT, FP, 2), o[:, n1:n1 + FEAT], o[:, -1]


class VAE(nn.Module):
    def __init__(self, z=Z, w=1024, blocks=4):
        super().__init__(); self.enc = Encoder(z); self.dec = Decoder(z, w, blocks)

# ---------------------------------------------------------------- training

def losses(model, q, f, fm, sf, beta, sigma=.01, free_bits=.02):
    mu, lv = model.enc(q, f, fm, sf); z = mu + torch.randn_like(mu) * torch.exp(.5 * lv)
    qh, fh, fl, ls = model.dec(z); e = qh - q
    rec = .5 * (e.square().sum((1, 2)) + (e - torch.roll(e, 1, 1)).square().sum((1, 2))) / sigma ** 2
    fe = .5 * ((fh - f).square().sum((2, 3)) * fm).sum(1) / (2 * sigma) ** 2
    pres = F.binary_cross_entropy_with_logits(fl, fm, reduction='none').sum(1)
    size = .5 * (ls - torch.log(sf)).square() / .03 ** 2
    kl_d = .5 * (mu.square() + lv.exp() - 1 - lv)                   # (B, Z)
    kl = torch.clamp(kl_d.mean(0), min=free_bits).sum()
    loss = rec.mean() + fe.mean() + pres.mean() + size.mean() + beta * kl
    with torch.no_grad():
        m = dict(rec=rec.mean(), feat=fe.mean(), pres=pres.mean(), size=size.mean(), kl=kl_d.sum(1).mean(), rms=e.square().mean().sqrt())
    return loss, m


def _save(obj, path):
    tmp = path.with_suffix('.tmp'); torch.save(obj, tmp)
    for i in range(30):
        try: os.replace(tmp, path); return
        except PermissionError: time.sleep(.2 * (i + 1))


def train(hours=3., follow=True, corpus=RUNS / 'corpus', out=RUNS / 'train', batch=1024, lr=1e-3, anchor_share=.08, w=1024, blocks=4,
          sheet_every=600, ckpt_every=300, resume=True, sigma=.01, warm_kl=8000, total_steps=None, log=print):
    out.mkdir(parents=True, exist_ok=True); torch.manual_seed(0)
    data = Corpus(corpus); pool = anchor_pool(); A = {k: torch.from_numpy(v).to(DEV) for k, v in pool.items()}
    anchors = {k: torch.from_numpy(v).to(DEV) if v.dtype != np.dtype('<U64') and v.dtype.kind != 'U' else v for k, v in np.load(OUT / 'anchors.npz').items()}
    model = VAE(Z, w, blocks).to(DEV); ema = Decoder(Z, w, blocks).to(DEV); ema.load_state_dict(model.dec.state_dict())
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-5); step = 0; t_spent = 0.
    ck = out / 'last.pt'
    if resume and ck.exists():
        s = torch.load(ck, map_location=DEV, weights_only=False); model.load_state_dict(s['model']); ema.load_state_dict(s['ema']); opt.load_state_dict(s['opt'])
        step = s['step']; t_spent = s.get('seconds', 0.); log(f'resumed at step {step} ({t_spent / 3600:.2f} h trained)')
    budget = hours * 3600; t0 = time.time() - t_spent; last_sheet = last_ck = last_refresh = time.time(); hist = []
    n_anchor = int(round(anchor_share * batch))
    log(f'corpus {data.size()} train / {data.size("val")} val rows, {len(data.names)} plans; anchor pool {len(pool["q"])}; '
        f'decoder {sum(p.numel() for p in model.dec.parameters()) / 1e6:.2f} M parameters, encoder {sum(p.numel() for p in model.enc.parameters()) / 1e6:.2f} M; device {DEV}')
    while time.time() - t0 < budget and (total_steps is None or step < total_steps):
        frac = (time.time() - t0) / budget
        for g in opt.param_groups: g['lr'] = lr * min(1, (step + 1) / 1000) * (.5 * (1 + math.cos(math.pi * min(frac, 1.))) * .97 + .03)
        q, f, fm, sf, _ = data.batch(batch - n_anchor)
        ia = torch.randint(0, len(A['q']), (n_anchor,), device=DEV)
        q = torch.cat([q, A['q'][ia]]); f = torch.cat([f, A['f'][ia]]); fm = torch.cat([fm, A['fm'][ia]]); sf = torch.cat([sf, A['sf'][ia]])
        flip = torch.rand(len(q), device=DEV) < .5
        if flip.any():
            mq, mf, mfm = mirror_t(q[flip], f[flip], fm[flip]); q = q.clone(); f = f.clone(); fm = fm.clone(); q[flip] = mq; f[flip] = mf; fm[flip] = mfm
        beta = min(1., step / warm_kl)
        loss, m = losses(model, q, f, fm, sf, beta, sigma)
        opt.zero_grad(set_to_none=True); loss.backward(); nn.utils.clip_grad_norm_(model.parameters(), 5.); opt.step(); step += 1
        with torch.no_grad():
            d = min(.999, (1 + step) / (10 + step)); pe = list(ema.parameters()); pm = [x.detach() for x in model.dec.parameters()]
            torch._foreach_mul_(pe, d); torch._foreach_add_(pe, pm, alpha=1 - d)
        if step % 200 == 0:
            m = {k: float(v) for k, v in m.items()}
            m.update(step=step, hours=round((time.time() - t0) / 3600, 3), lr=opt.param_groups[0]['lr'], beta=beta, rows=data.size()); hist.append(m)
            with open(out / 'log.jsonl', 'a') as fh: fh.write(json.dumps(m) + '\n')
            if step % 2000 == 0: log(f"step {step:7d}  {m['hours']:.2f} h  rms {m['rms']:.4f}  rec {m['rec']:8.1f}  kl {m['kl']:6.1f}  feat {m['feat']:7.1f}  size {m['size']:6.2f}  rows {m['rows']}")
        now = time.time()
        if follow and now - last_refresh > 240:
            last_refresh = now; k = data.refresh()
            if k: log(f'  + {k} new corpus rows ({data.size()} train, {len(data.names)} plans)')
        if now - last_ck > ckpt_every:
            last_ck = now; _save(dict(model=model.state_dict(), ema=ema.state_dict(), opt=opt.state_dict(), step=step, seconds=now - t0, w=w, blocks=blocks, names=data.names), ck)
        if now - last_sheet > sheet_every:
            last_sheet = now; ev = evaluate(model, ema, data, anchors, out / f'progress-{step:07d}.png', step, now - t0); log(f'  eval {json.dumps(ev)}')
            with open(out / 'eval.jsonl', 'a') as fh: fh.write(json.dumps(dict(ev, step=step)) + '\n')
    _save(dict(model=model.state_dict(), ema=ema.state_dict(), opt=opt.state_dict(), step=step, seconds=time.time() - t0, w=w, blocks=blocks, names=data.names), ck)
    ev = evaluate(model, ema, data, anchors, out / f'progress-{step:07d}.png', step, time.time() - t0); log(f'final eval {json.dumps(ev)}')
    return ev

# ---------------------------------------------------------------- evaluation and progress sheets

def _poly_ok(q):
    from shapely.geometry import Polygon
    p = Polygon(flatten(q, 3)); return p.is_valid and p.area > .05


@torch.no_grad()
def evaluate(model, dec, data, anchors, png, step, seconds, n=384, seed=0):
    model.eval(); dec.eval(); g = torch.Generator(device=DEV); g.manual_seed(seed)
    q, f, fm, sf, plan = data.batch(n, 'val' if data.size('val') else 'train', g)
    mu, _ = model.enc(q, f, fm, sf); qh, fh, fl, ls = dec(mu)
    qn, qhn = q.cpu().numpy().astype(float), qh.cpu().numpy().astype(float)
    ious = np.array([raster_iou(flatten(a, 3), flatten(b, 3)) for a, b in zip(qn, qhn)])
    zs = torch.randn(n, Z, generator=g, device=DEV) * .8; qs = dec(zs)[0].cpu().numpy().astype(float)
    valid = float(np.mean([_poly_ok(x) for x in qs]))
    amu, _ = model.enc(anchors['q'], anchors['f'], anchors['fm'], anchors['sf']); aq = dec(amu)[0].cpu().numpy().astype(float)
    a_iou = np.array([raster_iou(flatten(a, 3), flatten(b, 3)) for a, b in zip(anchors['q'].cpu().numpy().astype(float), aq)])
    size_err = float((ls.exp() - sf).abs().mean())
    ev = dict(val_iou=round(float(ious.mean()), 4), val_iou_p10=round(float(np.percentile(ious, 10)), 4), prior_valid=round(valid, 3),
              anchor_iou=round(float(a_iou.mean()), 4), anchor_iou_min=round(float(a_iou.min()), 4), size_abs_err=round(size_err, 4))
    # sheet: reconstructions, prior samples, walks between anchors
    cell = 120; cols = 12; rows = []
    rec = [(qn[i], qhn[i]) for i in range(cols * 2)]
    pri = [qs[i] for i in range(cols * 2)]
    ia = np.random.default_rng(step).choice(len(amu), 6, replace=False)
    walks = []
    for a, b in zip(ia[0::2], ia[1::2]):
        t = torch.linspace(0, 1, cols, device=DEV)[:, None]; zz = (1 - t) * amu[a] + t * amu[b]; walks.append(dec(zz)[0].cpu().numpy().astype(float))
    H = (2 + 2 + len(walks)) * cell + 40; img = np.full((H, cols * cell, 3), 18, np.uint8)
    def put(q, r, c, col=(255, 255, 255), fill=True):
        o = np.array([c * cell + cell / 2, 40 + r * cell + cell / 2]); p = np.round((flatten(q, 3) * (cell - 16) + o) * 16).astype(np.int32)
        if fill: cv2.fillPoly(img, [p], col, cv2.LINE_AA, shift=4)
        else: cv2.polylines(img, [p], True, col, 1, cv2.LINE_AA, shift=4)
    for i, (a, b) in enumerate(rec): put(a, i // cols, i % cols, (235, 235, 235)); put(b, i // cols, i % cols, (60, 90, 255), False)
    for i, x in enumerate(pri): put(x, 2 + i // cols, i % cols)
    for w_, wk in enumerate(walks):
        for i, x in enumerate(wk): put(x, 4 + w_, i, (210, 230, 255))
    txt = (f'FossilNet v3  step {step}  {seconds / 3600:.2f} h  |  held-out IoU {ev["val_iou"]:.3f}  drawings IoU {ev["anchor_iou"]:.3f}  '
           f'random samples valid {ev["prior_valid"]:.2f}  |  rows 1-2 shape (white) vs network (red), 3-4 new creatures, 5-7 walks between drawings')
    cv2.putText(img, txt, (8, 26), cv2.FONT_HERSHEY_SIMPLEX, .42, (200, 200, 200), 1, cv2.LINE_AA)
    cv2.imwrite(str(png), img); cv2.imwrite(str(png.parent / 'latest.png'), img)
    model.train(); dec.train(); return ev

# ---------------------------------------------------------------- export

@torch.no_grad()
def export(ck=RUNS / 'train' / 'last.pt', corpus=RUNS / 'corpus', out=OUT, landmarks=2048):
    """fossilnet3.pt: the EMA decoder, the anchors' latents and a set of corpus landmarks (latent means with plans)"""
    s = torch.load(ck, map_location=DEV, weights_only=False); w, blocks = s['w'], s['blocks']
    model = VAE(Z, w, blocks).to(DEV); model.load_state_dict(s['model']); model.eval(); dec = Decoder(Z, w, blocks).to(DEV); dec.load_state_dict(s['ema']); dec.eval()
    a = np.load(OUT / 'anchors.npz'); at = {k: torch.from_numpy(a[k]).to(DEV) for k in ('q', 'f', 'fm', 'sf')}
    amu, _ = model.enc(at['q'], at['f'], at['fm'], at['sf'])
    data = Corpus(corpus); g = torch.Generator(device=DEV); g.manual_seed(1)
    q, f, fm, sf, plan = data.batch(landmarks, 'train', g); lmu, _ = model.enc(q, f, fm, sf)
    plan = plan.clone()
    allmu = torch.cat([lmu, amu]); mean = allmu.mean(0); U, S, V = torch.pca_lowrank(allmu - mean, q=8, center=False)
    torch.save(dict(decoder=dec.state_dict(), Z=Z, N=N, FEAT=FEAT, FP=FP, w=w, blocks=blocks, anchors=amu.cpu(), anchor_labels=[str(x) for x in a['labels']],
                    anchor_sf=at['sf'].cpu(), landmarks=lmu.cpu(), landmark_plan=plan.cpu(), plan_names=data.names, pca_mean=mean.cpu(), pca=V.T.cpu(),
                    pca_sd=(S / math.sqrt(len(allmu))).cpu(), step=s['step'], hours=s.get('seconds', 0) / 3600), out / 'fossilnet3.pt')
    print(f'exported step {s["step"]} to {out / "fossilnet3.pt"}')


_cache = {}


def load(dev=DEV):
    """(decoder, info) from fossilnet3.pt"""
    if dev not in _cache:
        ck = torch.load(OUT / 'fossilnet3.pt', map_location='cpu', weights_only=False); dec = Decoder(ck['Z'], ck['w'], ck['blocks']); dec.load_state_dict(ck['decoder'])
        dec.eval().to(dev)
        for p in dec.parameters(): p.requires_grad_(False)
        _cache[dev] = (dec, ck)
    return _cache[dev]


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0]); s = a.add_subparsers(dest='cmd', required=True)
    s.add_parser('anchors')
    p = s.add_parser('train'); p.add_argument('--hours', type=float, default=3.); p.add_argument('--follow', action='store_true'); p.add_argument('--batch', type=int, default=1024)
    p.add_argument('--lr', type=float, default=1e-3); p.add_argument('--width', type=int, default=1024); p.add_argument('--blocks', type=int, default=4)
    p.add_argument('--out', type=Path, default=RUNS / 'train'); p.add_argument('--corpus', type=Path, default=RUNS / 'corpus'); p.add_argument('--sigma', type=float, default=.01)
    p.add_argument('--fresh', action='store_true'); p.add_argument('--sheet-every', type=int, default=600); p.add_argument('--steps', type=int)
    p = s.add_parser('export'); p.add_argument('--ckpt', type=Path, default=RUNS / 'train' / 'last.pt'); p.add_argument('--corpus', type=Path, default=RUNS / 'corpus')
    a = a.parse_args()
    if a.cmd == 'anchors': build_anchors(); anchor_pool()
    elif a.cmd == 'train': train(a.hours, a.follow, a.corpus, a.out, a.batch, a.lr, w=a.width, blocks=a.blocks, sigma=a.sigma, resume=not a.fresh, sheet_every=a.sheet_every, total_steps=a.steps)
    else: export(a.ckpt, a.corpus)
