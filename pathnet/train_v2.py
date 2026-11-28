"""PathNet v2: a larger Aphex decoder with a richer latent space and learned cutting rules, trained on an endless
stream of fresh shapes on the GPU.

    ..\\.venv\\Scripts\\python.exe train_v2.py calibrate                        # shape-space statistics and a sample sheet
    ..\\.venv\\Scripts\\python.exe train_v2.py train --name m --width 1024 --depth 6 --minutes 15
    ..\\.venv\\Scripts\\python.exe train_v2.py overview                         # one picture of every run's latest progress
    ..\\.venv\\Scripts\\python.exe train_v2.py evaluate <checkpoint.pt>         # exact polygon metrics, also for v1

v1 (train.py) decodes a class and 6 latents into 96 periodic B-spline controls; it learned 2600 fixed deformations of
the logo, and the board optimiser may only use logo latents within +-0.28 of them. v2 keeps the interface (class index
and latent vector in, (n, 96, 2) controls out) and the 59 classes, and changes the rest:

* 18 latents: v1's 6 global modes (stretch, shear, bends) plus 2 at each of 6 handles, the farthest-point features of
  the outline (for the logo: both ends of the top bar, the right tower, both feet and the notch). A handle moves its
  part of the outline smoothly (Gaussian falloff, 0.2 of the extent), so an arm can lean into a gap on its own.
* the whole latent cube is usable: amplitudes are calibrated (`calibrate`) so that random latents in [-1, 1]^18 stay
  within IoU 0.85 of the traced logo, the threshold field.py checks; checkpoints carry zlim = 1 for every class.
* cutting rules learned on the decoder's own output: no neck under 2.8 mm and no two parts of the outline closer than
  0.7 mm, at the smallest piece size (34 mm). Where a target breaks a rule, the decoder learns the nearest shape that
  keeps it, so the board optimiser can roam the whole cube and still get cuttable logos.
* the network predicts a displacement from the class's base outline (zero at the start) through residual blocks with
  class-conditioned feature modulation (FiLM). New targets are drawn on the GPU at every step, so no shape is seen
  twice. AdamW with warm-up and a cosine decay over the time budget; the saved weights are an exponential moving average.

Runs write to runs/pathnet-v2/<name>: progress-*.png every few minutes, log.jsonl, pathnet-v2.pt and report.json.
generate.py loads a v2 checkpoint when PATHNET_CKPT points at one; without it everything still uses v1.
"""
import argparse, copy, json, math, time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

from train import N, ROOT, flatten, load

HANDLES = 6; ZG = 6; Z = ZG + 2 * HANDLES
SIZE_MM = 34.                     # smallest logo extent on the boards
NECK = 2.8 / SIZE_MM              # neck limit, normalised (logo extent = 1); the traced logo itself is 2.92 mm
GAP = 0.7 / SIZE_MM               # self-contact limit, normalised
REACH = 1.6                       # pairs closer than REACH x NECK along the outline are neighbours, not necks
SIGMA = .2                        # handle falloff
OUT_SCALE = .1                    # displacements are ~0.05 of the extent: a raw output of ~0.5 keeps the last layer well scaled
DEV = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
RUNS = ROOT.parent / 'runs' / 'pathnet-v2'


# ---------------------------------------------------------------- shape space

def handle_points(p, h=HANDLES):
    """farthest-point samples of the outline, starting from the point furthest from its centroid"""
    q = flatten(p, 4); c = q.mean(0); idx = [int(np.argmax(((q - c) ** 2).sum(1)))]; d = ((q - q[idx[0]]) ** 2).sum(1)
    for _ in range(h - 1):
        i = int(np.argmax(d)); idx.append(i); d = np.minimum(d, ((q - q[i]) ** 2).sum(1))
    return q[idx]


class Family:
    """targets of the v2 shape space for (class, latent) batches: handle moves, then v1's global modes"""

    def __init__(self, ag_logo=.174, ah_logo=.045, ag_other=1., ah_other=.045, handles=HANDLES, sigma=SIGMA, device=DEV):
        base = load(); self.names = [n for n, _ in base]; P = np.stack([p for _, p in base]); K = len(base); self.h = handles; self.z = ZG + 2 * handles
        H = np.stack([handle_points(p, handles) for _, p in base])
        W = np.exp(-((P[:, :, None] - H[:, None]) ** 2).sum(-1) / (2 * sigma ** 2))
        self.P = torch.tensor(P, dtype=torch.float32, device=device); self.W = torch.tensor(W, dtype=torch.float32, device=device)
        self.ag = torch.full((K,), float(ag_other), device=device); self.ag[0] = ag_logo
        self.ah = torch.full((K,), float(ah_other), device=device); self.ah[0] = ah_logo
        self.config = dict(ag_logo=ag_logo, ah_logo=ah_logo, ag_other=ag_other, ah_other=ah_other, sigma=sigma, handle_count=handles, handles=H.round(5).tolist())

    def __call__(self, k, z):
        p = self.P[k] + self.ah[k, None, None] * torch.einsum('bnh,bhd->bnd', self.W[k], z[:, ZG:].reshape(-1, self.h, 2))
        x, y = p[..., 0], p[..., 1]; a, b, c, d, e, g = (z[:, :ZG] * self.ag[k, None]).T[..., None]
        return torch.stack([x * (1 + .12 * a) + .09 * b * y + .10 * c * torch.sin(math.pi * y) + .055 * d * torch.sin(2 * math.pi * y),
                            y * (1 + .12 * e) + .08 * g * x + .07 * c * torch.sin(math.pi * x) - .035 * d * torch.sin(2 * math.pi * x)], -1)


def draw(n, K, gen, logo=.5, corners=.15, device=DEV, zdim=Z):
    """classes (logo with probability `logo`) and latents: uniform in the cube, a share pushed to its faces"""
    k = torch.where(torch.rand(n, generator=gen, device=device) < logo, 0, torch.randint(1, K, (n,), generator=gen, device=device))
    z = torch.rand(n, zdim, generator=gen, device=device) * 2 - 1
    edge = (torch.rand(n, 1, generator=gen, device=device) < corners).float()
    z = edge * z.sign() * (1 - .1 * torch.rand(n, zdim, generator=gen, device=device)) + (1 - edge) * z
    return k, z


# ---------------------------------------------------------------- outline geometry (torch)

def outline(q, steps=2):
    """points along the closed cubic B-spline of controls q (B, n, 2), as train.flatten"""
    a, b, c, d = q.roll(1, 1), q, q.roll(-1, 1), q.roll(-2, 1)
    B = torch.stack([(a + 4 * b + c) / 6, (2 * b + c) / 3, (b + 2 * c) / 3, (b + 4 * c + d) / 6], 2)
    t = torch.arange(steps, device=q.device, dtype=q.dtype) / steps
    w = torch.stack([(1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3], 1)
    return torch.einsum('tk,bnkd->bntd', w, B).reshape(q.shape[0], -1, 2)


def cut_rules(q, steps=2):
    """necks and self-contacts of decoded outlines. For pairs of outline points further apart along the outline than
    REACH x NECK: a neck pair sees each other through the inside (each lies behind the other's outward normal), a
    contact pair through the outside. Returns per shape: neck penalty, contact penalty, narrowest neck, closest contact."""
    p = outline(q, steps); m = p.shape[1]
    sl = (p.roll(-1, 1) - p).norm(dim=-1); s = sl.cumsum(1) - sl; L = sl.sum(1)[:, None, None]
    arc = (s[:, :, None] - s[:, None, :]).abs(); arc = torch.minimum(arc, L - arc)
    t = p.roll(-1, 1) - p.roll(1, 1); area = (p[..., 0] * p.roll(-1, 1)[..., 1] - p.roll(-1, 1)[..., 0] * p[..., 1]).sum(1)
    nrm = torch.stack([t[..., 1], -t[..., 0]], -1) * torch.sign(area)[:, None, None]
    v = p[:, None, :, :] - p[:, :, None, :]
    D = (v.square().sum(-1) + 1e-12).sqrt()
    inside = (nrm[:, :, None, :] * v).sum(-1) < 0
    far = arc > REACH * NECK
    neck = far & inside & inside.transpose(1, 2); contact = far & ~inside & ~inside.transpose(1, 2)
    big = torch.full_like(D, 9.)
    return ((F.relu(NECK - D) * neck).square().sum((1, 2)) / m, (F.relu(GAP - D) * contact).square().sum((1, 2)) / m,
            torch.where(neck, D, big).amin((1, 2)), torch.where(contact, D, big).amin((1, 2)))


def rules_chunked(q, chunk=256):
    out = [cut_rules(q[i:i + chunk]) for i in range(0, len(q), chunk)]
    return [torch.cat([o[j] for o in out]) for j in range(4)]


# ---------------------------------------------------------------- network

class Block(nn.Module):
    def __init__(self, w, c):
        super().__init__(); self.norm = nn.LayerNorm(w, elementwise_affine=False); self.film = nn.Linear(c, 2 * w)
        self.fc1 = nn.Linear(w, w); self.fc2 = nn.Linear(w, w)
        for m in (self.film, self.fc2): nn.init.zeros_(m.weight); nn.init.zeros_(m.bias)

    def forward(self, h, e):
        s, t = self.film(e).chunk(2, -1)
        return h + self.fc2(F.silu(self.fc1(self.norm(h) * (1 + s) + t)))


class DecoderV2(nn.Module):
    """class embedding + latent -> displacement of the class's base controls through FiLM residual blocks"""

    def __init__(self, k, base, width=1024, depth=6, emb=64, latent=Z):
        super().__init__()
        self.register_buffer('base', torch.as_tensor(np.asarray(base), dtype=torch.float32))
        self.emb = nn.Embedding(k, emb); self.inp = nn.Linear(emb + latent, width)
        self.blocks = nn.ModuleList(Block(width, emb) for _ in range(depth))
        self.norm = nn.LayerNorm(width); self.out = nn.Linear(width, self.base.shape[1] * 2)
        nn.init.zeros_(self.out.weight); nn.init.zeros_(self.out.bias)

    def forward(self, k, z):
        e = self.emb(k); h = self.inp(torch.cat([e, z], -1))
        for b in self.blocks: h = b(h, e)
        return self.base[k] + OUT_SCALE * self.out(self.norm(h)).view(-1, self.base.shape[1], 2)


def build(ck):
    """the eval-mode decoder of a v2 checkpoint dict"""
    m = DecoderV2(len(ck['names']), np.zeros((len(ck['names']), ck['points'], 2)), latent=ck['latent'], **ck['model'])
    m.load_state_dict(ck['state']); return m.eval()


# ---------------------------------------------------------------- pictures

def probes(K, device=DEV, zdim=Z):
    g = torch.Generator(device='cpu'); g.manual_seed(7)
    z = torch.rand(32, zdim, generator=g) * 2 - 1; z[0] = 0; z[18:26] = z[18:26].sign()          # zero, random, cube corners
    k = torch.zeros(32, dtype=torch.long); k[26:] = torch.tensor([1, 5, 12, 23, 34, 45]) % K
    return k.to(device), z.to(device)


def sheet(path, q, t, lines, curve=None, cell=210, cols=8):
    """decoded shapes (dark) over their targets (red) with a caption and an optional validation curve"""
    q = q.detach().float().cpu().numpy(); t = t.detach().float().cpu().numpy(); rows = math.ceil(len(q) / cols)
    head = 34 * len(lines) + 16; foot = 230 if curve else 0
    img = np.full((head + rows * cell + foot, cols * cell, 3), 255, np.uint8)
    for i, (a, b) in enumerate(zip(q, t)):
        r, c = divmod(i, cols); o = np.array([c * cell + cell / 2, head + r * cell + cell / 2]); s = cell * .8
        P = lambda xy: np.round((xy * s + o) * 16).astype(np.int32)
        cv2.fillPoly(img, [P(flatten(a, 6))], (52, 50, 46), cv2.LINE_AA, shift=4)
        cv2.polylines(img, [P(flatten(b, 6))], True, (40, 40, 230), 2, cv2.LINE_AA, shift=4)
    for j, line in enumerate(lines):
        cv2.putText(img, line, (14, 32 + 34 * j), cv2.FONT_HERSHEY_SIMPLEX, .8, (25, 25, 25), 2, cv2.LINE_AA)
    if curve:
        x0, y0, w, h = 70, head + rows * cell + 20, cols * cell - 110, foot - 60
        cv2.rectangle(img, (x0, y0), (x0 + w, y0 + h), (200, 200, 200), 1)
        tt = np.array([c[0] for c in curve]); vv = np.log10(np.maximum([c[1] for c in curve], 1e-4)); lo, hi = math.floor(vv.min()), math.ceil(vv.max() + 1e-9)
        hi = max(hi, lo + 1); pts = np.stack([x0 + w * tt / max(tt.max(), 1e-9), y0 + h - h * (vv - lo) / (hi - lo)], 1)
        cv2.polylines(img, [np.round(pts).astype(np.int32)], False, (180, 90, 20), 2, cv2.LINE_AA)
        for e in range(lo, hi + 1):
            y = int(y0 + h - h * (e - lo) / (hi - lo)); cv2.putText(img, f'{10.0 ** e:g}', (6, y + 5), cv2.FONT_HERSHEY_SIMPLEX, .45, (90, 90, 90), 1, cv2.LINE_AA)
        cv2.putText(img, 'logo rms error, mm at 34 mm (log scale) over minutes', (x0 + 10, y0 + h + 30), cv2.FONT_HERSHEY_SIMPLEX, .55, (90, 90, 90), 1, cv2.LINE_AA)
    cv2.imwrite(str(path), img)


# ---------------------------------------------------------------- metrics

def validation(K, n_logo=2048, per_class=16, device=DEV, zdim=Z):
    g = torch.Generator(device=device); g.manual_seed(1234)
    k = torch.cat([torch.zeros(n_logo, dtype=torch.long, device=device), torch.arange(1, K, device=device).repeat_interleave(per_class)])
    z = torch.rand(len(k), zdim, generator=g, device=device) * 2 - 1; z[n_logo - 348:n_logo] = z[n_logo - 348:n_logo].sign()
    return k, z


@torch.no_grad()
def measure(model, fam, kv, zv, chunk=1024):
    q = torch.cat([model(kv[i:i + chunk], zv[i:i + chunk]) for i in range(0, len(kv), chunk)]).float(); t = fam(kv, zv)
    err = (q - t).norm(dim=-1); logo = kv == 0
    pn, pc, dn, dc = rules_chunked(q[logo]); tn, tc, tdn, tdc = rules_chunked(t[logo])
    clean = err[logo][(tdn >= NECK) & (tdc >= GAP)]            # targets that keep the rules: pure fidelity
    return dict(logo_rms_mm=float(err[logo].square().mean().sqrt() * SIZE_MM), logo_max_mm=float(err[logo].max() * SIZE_MM),
                logo_clean_rms_mm=float(clean.square().mean().sqrt() * SIZE_MM), logo_clean_max_mm=float(clean.max() * SIZE_MM),
                peak_gpu_mib=round(torch.cuda.max_memory_allocated() / 2 ** 20) if DEV.type == 'cuda' else 0,
                logo_p99_max_mm=float(torch.quantile(err[logo].amax(1), .99) * SIZE_MM),
                other_rms_mm=float(err[~logo].square().mean().sqrt() * SIZE_MM), other_max_mm=float(err[~logo].max() * SIZE_MM),
                logo_neck_min_mm=float(dn.min() * SIZE_MM), logo_neck_violations=float((dn < NECK - .05 / SIZE_MM).float().mean()),
                logo_contact_violations=float((dc < GAP - .05 / SIZE_MM).float().mean()),
                target_neck_violations=float((tdn < NECK - .05 / SIZE_MM).float().mean()),
                target_contact_violations=float((tdc < GAP - .05 / SIZE_MM).float().mean()))


def polygon_metrics(q, t, ref, k):
    """exact (shapely) IoU against targets and the traced logo, validity, for numpy control arrays"""
    from shapely.geometry import Polygon
    out = dict(valid=0, iou_target=[], iou_ref=[])
    for a, b, kk in zip(q, t, k):
        A = Polygon(flatten(a, 8)); B = Polygon(flatten(b, 8))
        if not (A.is_valid and A.area > 0): continue
        out['valid'] += 1
        if B.is_valid: out['iou_target'].append(A.intersection(B).area / A.union(B).area)
        if kk == 0: out['iou_ref'].append(A.intersection(ref).area / A.union(ref).area)
    n = len(q); it = np.array(out['iou_target']); ir = np.array(out['iou_ref'])
    return dict(shapes=n, valid=out['valid'], iou_target_mean=float(it.mean()), iou_target_min=float(it.min()), iou_target_p1=float(np.percentile(it, 1)),
                iou_ref_mean=float(ir.mean()) if len(ir) else None, ref_iou_at_least_0_85=float((ir >= .85).mean()) if len(ir) else None)


# ---------------------------------------------------------------- training

def logo_rule_penalty(controls, classes, limit):
    """Penalize logo cuts only. Glyph strokes can be thinner by design."""
    logos = controls[classes == 0][:limit]
    if len(logos) == 0:
        return controls.new_zeros(())
    neck, contact, _, _ = cut_rules(logos)
    penalty = neck + contact
    # Bound the loss on a tangled outline so its fidelity gradient can still untangle it.
    return (penalty / (1 + penalty / 1e-4)).mean()


def train(a):
    torch.backends.cuda.matmul.allow_tf32 = True; torch.backends.cudnn.allow_tf32 = True; torch.manual_seed(a.seed)
    out = RUNS / a.name; out.mkdir(parents=True, exist_ok=True)
    fam = Family(a.ag_logo, a.ah_logo, handles=a.handles, sigma=a.sigma); K = len(fam.names); zd = fam.z; log = (out / 'log.jsonl').open('a')
    model = DecoderV2(K, fam.P.cpu().numpy(), a.width, a.depth, a.emb, zd).to(DEV); ema = copy.deepcopy(model).requires_grad_(False)
    params = sum(p.numel() for p in model.parameters())
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, betas=(.9, .99), weight_decay=a.wd, fused=DEV.type == 'cuda')
    gen = torch.Generator(device=DEV); gen.manual_seed(a.seed); kv, zv = validation(K, zdim=zd); pk, pz = probes(K, zdim=zd)
    budget = a.minutes * 60; t0 = time.time(); step = 0; seen = 0; next_eval = 0.; next_img = 0.; curve = []; last = {}
    print(f'{a.name}: {params / 1e6:.2f} M parameters, batch {a.batch}, rules on {a.rule_batch}, {a.minutes} min', flush=True)
    while True:
        el = time.time() - t0; frac = min(1., el / budget)
        lr = a.lr * min(1., (step + 1) / a.warmup) * (a.final + (1 - a.final) * .5 * (1 + math.cos(math.pi * frac)))
        for gp in opt.param_groups: gp['lr'] = lr
        k, z = draw(a.batch, K, gen, a.logo_share, a.corners, zdim=zd); t = fam(k, z); q = model(k, z); diff = q - t
        mse = diff.square().mean(); loss = mse + 1.2 * (diff - diff.roll(1, 1)).square().mean()
        if a.rules > 0:
            rule_weight = a.rules * min(1., max(0., frac - .05) / .1)
            loss = loss + rule_weight * logo_rule_penalty(q, k, a.rule_batch)
        opt.zero_grad(set_to_none=True); loss.backward()
        if a.clip: torch.nn.utils.clip_grad_norm_(model.parameters(), a.clip)
        opt.step(); step += 1; seen += a.batch
        with torch.no_grad():
            d = min(a.ema, (1 + step) / (10 + step))
            for pe, pm in zip(ema.parameters(), model.parameters()): pe.lerp_(pm, 1 - d)
        el = time.time() - t0; done = el >= budget
        if el >= next_eval or done:
            last = measure(ema, fam, kv, zv); last.update(step=step, minutes=round(el / 60, 2), shapes_seen=seen, lr=lr, train_mse=float(mse.detach()))
            curve.append((el / 60, last['logo_clean_rms_mm'])); log.write(json.dumps(last) + '\n'); log.flush(); next_eval = el + a.eval_every
            print(json.dumps({q_: (round(v, 5) if isinstance(v, float) else v) for q_, v in last.items()}), flush=True)
        if el >= next_img or done:
            with torch.no_grad(): qq = ema(pk, pz); tt = fam(pk, pz)
            lines = [f'PathNet v2 / {a.name}: {params / 1e6:.1f} M parameters, step {step:,}, {seen / 1e6:.1f} M fresh shapes, {el / 60:.0f} of {a.minutes:.0f} min',
                     f'logo error rms {last["logo_clean_rms_mm"]:.3f} mm, worst {last["logo_clean_max_mm"]:.2f} mm at 34 mm; necks < 2.8 mm: {last["logo_neck_violations"]:.1%} '
                     f'(targets {last["target_neck_violations"]:.1%}); narrowest {last["logo_neck_min_mm"]:.2f} mm',
                     'dark: network output   red: target shape   row 1-2 logo, random latents; row 3 logo at cube corners; last 6: glyphs']
            name = f'progress-{int(el // 60):04d}min.png'; sheet(out / name, qq, tt, lines, curve)
            (out / 'progress-latest.png').write_bytes((out / name).read_bytes()); next_img = el + a.image_every
        if done: break
    ck = dict(arch='pathnet-v2', state={k_: v.detach().cpu() for k_, v in ema.state_dict().items()}, names=fam.names, points=N, latent=zd,
              zlim=[1.] * K, ref_iou_min=.45, model=dict(width=a.width, depth=a.depth, emb=a.emb), family=fam.config,
              rules=dict(size_mm=SIZE_MM, neck_mm=NECK * SIZE_MM, contact_mm=GAP * SIZE_MM), train=vars(a) | dict(steps=step, shapes_seen=seen, parameters=params))
    torch.save(ck, out / 'pathnet-v2.pt')
    report = dict(name=a.name, parameters=params, steps=step, shapes_seen=seen, minutes=round((time.time() - t0) / 60, 1), final=last)
    (out / 'report.json').write_text(json.dumps(report, indent=1)); print('saved', out / 'pathnet-v2.pt', flush=True)


# ---------------------------------------------------------------- calibration, evaluation, overview

def calibrate(a):
    from shapely.geometry import Polygon
    out = RUNS; out.mkdir(parents=True, exist_ok=True)
    ref = Polygon(flatten(load()[0][1], 8)); gen = torch.Generator(device=DEV); gen.manual_seed(3)
    for ag in a.ag:
        for ah in a.ah:
            fam = Family(ag, ah, handles=a.handles, sigma=a.sigma); k, z = draw(a.samples, len(fam.names), gen, logo=1., corners=.15, zdim=fam.z); t = fam(k, z)
            _, _, dn, dc = rules_chunked(t); ious = []
            for q in t.cpu().numpy():
                A = Polygon(flatten(q, 6)); ious.append(A.intersection(ref).area / A.union(ref).area if A.is_valid else 0.)
            ious = np.array(ious)
            print(f'ag {ag:.3f} ah {ah:.3f}: IoU vs logo mean {ious.mean():.3f} p1 {np.percentile(ious, 1):.3f} min {ious.min():.3f} >=0.85 {np.mean(ious >= .85):.3%} | '
                  f'necks < 2.8 mm {(dn < NECK).float().mean():.1%} (narrowest {dn.min() * SIZE_MM:.2f} mm) contacts < 0.7 mm {(dc < GAP).float().mean():.1%}', flush=True)
    fam = Family(a.ag[-1], a.ah[-1], handles=a.handles, sigma=a.sigma); pk, pz = probes(len(fam.names), zdim=fam.z); t = fam(pk, pz); b = fam(pk, torch.zeros_like(pz))
    base = fam.P[:1]; pn, pc, dn, dc = cut_rules(base)
    sheet(out / f'calibration-h{a.handles}.png', t, b, [f'v2 shape space: {a.handles} handles, sigma {a.sigma}, ag {a.ag[-1]}, ah {a.ah[-1]}; dark = target at the probe latent, red = base outline',
                                          f'logo base: narrowest neck {float(dn[0]) * SIZE_MM:.2f} mm, closest contact {float(dc[0]) * SIZE_MM:.2f} mm at 34 mm'])
    print('base logo neck %.2f mm, contact %.2f mm; sheet %s' % (float(dn[0]) * SIZE_MM, float(dc[0]) * SIZE_MM, out / f'calibration-h{a.handles}.png'))


def evaluate(a):
    """exact polygon metrics of a checkpoint (v1 or v2) on its own shape space"""
    from shapely.geometry import Polygon
    import train as v1
    ck = torch.load(a.checkpoint, weights_only=True, map_location='cpu'); ref = Polygon(flatten(load()[0][1], 8)); rng = np.random.default_rng(99)
    if ck.get('arch') == 'pathnet-v2':
        f = ck['family']; fam = Family(f['ag_logo'], f['ah_logo'], f['ag_other'], f['ah_other'], f.get('handle_count', 6), f['sigma'], device='cpu'); model = build(ck)
        k = torch.zeros(a.samples, dtype=torch.long); z = torch.tensor(rng.uniform(-1, 1, (a.samples, ck['latent'])), dtype=torch.float32)
        with torch.no_grad(): q = model(k, z).numpy(); t = fam(k, z).numpy()
        _, _, dn, dc = rules_chunked(torch.tensor(q)); _, _, tdn, _ = rules_chunked(torch.tensor(t)); scope = f'logo, latents uniform in [-1, 1]^{ck["latent"]} (the range the board optimiser may use)'
    else:
        model = v1.Model(len(ck['names'])); model.load_state_dict(ck['state']); model.eval()
        z = rng.uniform(-.28, .28, (a.samples, v1.Z)).astype(np.float32)
        with torch.no_grad(): q = model(torch.zeros(a.samples, dtype=torch.long), torch.tensor(z)).numpy()
        t = v1.deform(load()[0][1], z, True); _, _, dn, dc = rules_chunked(torch.tensor(q)); _, _, tdn, _ = rules_chunked(torch.tensor(t, dtype=torch.float32))
        scope = 'logo, latents uniform in [-0.28, 0.28]^6 (the range the board optimiser may use)'
    with torch.no_grad():
        z0 = torch.zeros(1, ck['latent']); q0 = (build(ck) if ck.get('arch') == 'pathnet-v2' else model)(torch.zeros(1, dtype=torch.long), z0).numpy()[0]
    A0 = Polygon(flatten(q0, 8))
    r = dict(checkpoint=str(a.checkpoint), scope=scope, latent_dims=ck['latent'], parameters=sum(p.numel() for p in model.parameters()),
             zero_latent_iou_vs_traced_logo=A0.intersection(ref).area / A0.union(ref).area, zero_latent_hausdorff_mm=ref.exterior.hausdorff_distance(A0.exterior) * SIZE_MM,
             **polygon_metrics(q, t, ref, np.zeros(len(q), int)), rms_error_mm=float(np.sqrt(((q - t) ** 2).sum(-1).mean()) * SIZE_MM),
             worst_error_mm=float(np.sqrt(((q - t) ** 2).sum(-1)).max() * SIZE_MM),
             necks_under_2_8mm=float((dn < NECK - .05 / SIZE_MM).float().mean()), target_necks_under_2_8mm=float((tdn < NECK - .05 / SIZE_MM).float().mean()),
             narrowest_neck_mm=float(dn.min() * SIZE_MM), contacts_under_0_7mm=float((dc < GAP - .05 / SIZE_MM).float().mean()))
    print(json.dumps(r, indent=1))
    if a.out: Path(a.out).write_text(json.dumps(r, indent=1))


def overview(a):
    """one picture with the latest progress sheet of every run, side by side"""
    runs = sorted(p for p in Path(a.dir).iterdir() if (p / 'progress-latest.png').exists())
    ims = [cv2.imread(str(p / 'progress-latest.png')) for p in runs]
    if not ims: return print('no runs')
    w = max(i.shape[1] for i in ims); ims = [cv2.copyMakeBorder(i, 0, 0, 0, w - i.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255)) for i in ims]
    cols = 2 if len(ims) > 1 else 1; rows = [np.hstack(ims[i:i + cols] + [np.full_like(ims[0], 255)] * (cols - len(ims[i:i + cols]))) for i in range(0, len(ims), cols)]
    hh = max(r.shape[0] for r in rows); grid = np.vstack([cv2.copyMakeBorder(r, 0, hh - r.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=(255, 255, 255)) for r in rows])
    scale = min(1., 3200 / grid.shape[1]); grid = cv2.resize(grid, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(Path(a.dir) / 'overview.png'), grid); print(Path(a.dir) / 'overview.png')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0]); sub = p.add_subparsers(dest='cmd', required=True)
    t = sub.add_parser('train'); t.add_argument('--name', required=True); t.add_argument('--width', type=int, default=1024); t.add_argument('--depth', type=int, default=6)
    t.add_argument('--emb', type=int, default=64); t.add_argument('--lr', type=float, default=1e-3); t.add_argument('--final', type=float, default=.01)
    t.add_argument('--wd', type=float, default=0.); t.add_argument('--warmup', type=int, default=500); t.add_argument('--clip', type=float, default=1.)
    t.add_argument('--batch', type=int, default=4096); t.add_argument('--rule-batch', type=int, default=512); t.add_argument('--rules', type=float, default=10.)
    t.add_argument('--ema', type=float, default=.999); t.add_argument('--minutes', type=float, default=15.); t.add_argument('--seed', type=int, default=22)
    t.add_argument('--logo-share', type=float, default=.5); t.add_argument('--corners', type=float, default=.15)
    t.add_argument('--handles', type=int, default=HANDLES); t.add_argument('--sigma', type=float, default=SIGMA)
    t.add_argument('--ag-logo', type=float, default=.174); t.add_argument("--ah-logo", type=float, default=.045)
    t.add_argument('--eval-every', type=float, default=60.); t.add_argument('--image-every', type=float, default=300.)
    c = sub.add_parser('calibrate'); c.add_argument('--ag', type=float, nargs='+', default=[.174]); c.add_argument('--ah', type=float, nargs='+', default=[.02, .03, .04, .05, .06])
    c.add_argument('--samples', type=int, default=2000); c.add_argument('--handles', type=int, default=HANDLES); c.add_argument('--sigma', type=float, default=SIGMA)
    e = sub.add_parser('evaluate'); e.add_argument('checkpoint', type=Path); e.add_argument('--samples', type=int, default=1000); e.add_argument('--out')
    o = sub.add_parser('overview'); o.add_argument('dir', nargs='?', default=str(RUNS))
    a = p.parse_args(); {'train': train, 'calibrate': calibrate, 'evaluate': evaluate, 'overview': overview}[a.cmd](a)
