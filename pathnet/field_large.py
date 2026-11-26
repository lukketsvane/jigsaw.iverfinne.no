"""Field composition for boards with hundreds of pieces (every piece an Aphex logo).

`field.py` holds a dense (pieces x rows x columns) distance stack for the whole board. At 300 pieces on
570 x 390 mm and 0.25 mm pixels that stack alone is 4 GB, and the bay-rule Eikonal step needs several more,
which no longer fits a 20 GB card. Here every piece is solved on a fixed-size window around its silhouette:

* signed distance and bays are computed on the window only;
* each piece's front is propagated on its own window with every silhouette as an obstacle (the fronts never
  interact, so this is the same first-arrival solve as field.eikonal as long as the window margin exceeds the
  distance at which a front can still arrive first or second, which `--margin` sets, 48 mm by default);
* the first and second arrival of all windows are merged into board-sized buffers.

Everything after that (stray repair, spur repair, sub-pixel seam tracing, vectorising, export and the interlock
measures) is field.py's own code. The decoder refinement keeps field.refine's maths but recomputes the distance
evaluations in chunks during the backward pass, so its memory no longer grows with the number of pieces.

    ..\\.venv\\Scripts\\python.exe field_large.py --pieces 300 --width 570 --height 390 --min-size 34 --output <dir>
"""
import argparse, json, math, time
from pathlib import Path
import numpy as np, torch, cv2
import torch.nn.functional as F
from torch.utils.checkpoint import checkpoint
from shapely import union_all
from shapely.geometry import Polygon
from shapely.ops import polygonize
import field as fl
from field import DEV, BIG, T
from train import flatten
from generate import names, ref, sample

# ---------------------------------------------------------------- windowed first-arrival partition

def windows(polys, W, H, px, margin):
    """Exact signed distance of every silhouette on one fixed-size window around it.
    Returns phi (n,sh,sw), window origins (n,2) as (row, col), the board-wide minimum over all silhouettes (h,w)
    and the pixel pitch (sx, sy)."""
    n = len(polys); w = round(W / px); h = round(H / px); sx = W / w; sy = H / h
    ext = max(float(np.ptp(np.asarray(xy), 0).max()) for xy in polys)
    sh = min(h, int(math.ceil((ext + 2 * margin) / sy)) + 2); sw = min(w, int(math.ceil((ext + 2 * margin) / sx)) + 2)
    org = np.zeros((n, 2), int); phi = torch.empty((n, sh, sw), device=DEV); gmin = torch.full((h, w), BIG, device=DEV)
    for i, xy in enumerate(polys):
        cx, cy = (np.min(xy, 0) + np.max(xy, 0)) / 2
        r0 = int(np.clip(round(cy / sy - sh / 2), 0, h - sh)); c0 = int(np.clip(round(cx / sx - sw / 2), 0, w - sw)); org[i] = r0, c0
        yy, xx = torch.meshgrid((torch.arange(r0, r0 + sh, device=DEV) + .5) * sy, (torch.arange(c0, c0 + sw, device=DEV) + .5) * sx, indexing='ij')
        phi[i] = fl.sdf_poly(torch.stack([xx.reshape(-1), yy.reshape(-1)], 1), T(xy)).reshape(sh, sw)
        gmin[r0:r0 + sh, c0:c0 + sw] = torch.minimum(gmin[r0:r0 + sh, c0:c0 + sw], phi[i])
    return phi, org, gmin, (sx, sy)


def window_bays(polys, phi, org, sx, sy):
    """field.bays on the windows: pixels inside a silhouette's convex hull but outside the silhouette"""
    n, sh, sw = phi.shape; out = torch.zeros((n, sh, sw), dtype=torch.bool, device=DEV)
    for i, xy in enumerate(polys):
        hull = np.array(Polygon(xy).convex_hull.exterior.coords)[:-1]; r0, c0 = org[i]
        yy, xx = torch.meshgrid((torch.arange(r0, r0 + sh, device=DEV) + .5) * sy, (torch.arange(c0, c0 + sw, device=DEV) + .5) * sx, indexing='ij')
        out[i] = (fl.sdf_poly(torch.stack([xx.reshape(-1), yy.reshape(-1)], 1), T(hull)).reshape(sh, sw) < 0) & (phi[i] > 0)
    return out


def eikonal_solid(phi, slow, solid, px, sweeps, band, tol=1e-3):
    """field.eikonal with the obstacle mask given explicitly (on windows the other silhouettes are not in phi)"""
    hold = (phi <= band) | solid; Tm = torch.where((phi <= band) & ~solid, slow * phi.clamp_min(0), torch.full_like(phi, BIG)); f = slow * px
    for s in range(0, sweeps, 16):
        Tn = Tm
        for _ in range(min(16, sweeps - s)):
            Tp = F.pad(Tn, (1, 1, 1, 1), value=BIG)
            a = torch.minimum(Tp[:, 1:-1, :-2], Tp[:, 1:-1, 2:]); b = torch.minimum(Tp[:, :-2, 1:-1], Tp[:, 2:, 1:-1]); d = a - b; del Tp
            Tc = torch.where(d.abs() < f, .5 * (a + b + torch.sqrt((2 * f * f - d * d).clamp_min(1e-8))), torch.minimum(a, b) + f); del a, b, d
            Tn = torch.where(hold, Tn, torch.minimum(Tn, Tc)); del Tc
        done = float((Tm - Tn).max()) < tol; Tm = Tn
        if done: break
    return Tm


def arrival_fields(polys, W, H, px, bay, margin, reach, batch=64):
    """first and second arrival over the whole board: values v1, v2 and labels l1, l2 (h,w), as field.partition's topk"""
    phi, org, gmin, (sx, sy) = windows(polys, W, H, px, margin); n, sh, sw = phi.shape; h, w = gmin.shape
    v1 = torch.full((h, w), BIG, device=DEV); v2 = v1.clone(); l1 = torch.full((h, w), -1, dtype=torch.long, device=DEV); l2 = l1.clone()
    sweeps = int(2.4 * (reach * (3 if bay else 1)) / px) + 8
    for b0 in range(0, n, batch):
        p = phi[b0:b0 + batch]
        if bay:
            own = 1 + bay * window_bays(polys[b0:b0 + batch], p, org[b0:b0 + batch], sx, sy).float()
            solid = torch.stack([gmin[r:r + sh, c:c + sw] <= 0 for r, c in org[b0:b0 + batch]]) & (p > 0)
            Fm = torch.where(p <= 0, p, eikonal_solid(p, own, solid, sx, sweeps, 1.2 * sx)); del own, solid
        else: Fm = p
        for j in range(len(p)):
            i = b0 + j; r, c = org[i]; sl = (slice(r, r + sh), slice(c, c + sw)); f = Fm[j]
            V1, V2, L1, L2 = v1[sl], v2[sl], l1[sl], l2[sl]; better = f < V1; second = ~better & (f < V2)
            nv2 = torch.where(better, V1, torch.where(second, f, V2)); nl2 = torch.where(better, L1, torch.where(second, torch.full_like(L2, i), L2))
            v1[sl] = torch.where(better, f, V1); l1[sl] = torch.where(better, torch.full_like(L1, i), L1); v2[sl] = nv2; l2[sl] = nl2
        del Fm
    return v1, l1, v2, l2


def assign_by_silhouette(faces, polys):
    """face -> piece by the silhouette each face overlaps most; None unless that is one-to-one"""
    from shapely.strtree import STRtree
    tree = STRtree(faces); owner = [None] * len(faces)
    for i, xy in enumerate(polys):
        s = Polygon(xy).buffer(0); cand = tree.query(s)
        if not len(cand): return None
        j = int(max(cand, key=lambda j: faces[j].intersection(s).area))
        if owner[j] is not None: return None
        owner[j] = i
    return owner if all(o is not None for o in owner) else None


def partition(polys, W, H, px=.25, bay=0., margin=48., sigma=.35, reach=16.):
    """field.partition (untrained slowness only) with the arrival fields computed on windows"""
    n = len(polys); h = round(H / px); w = round(W / px); moved = 0
    with torch.no_grad(): v1, l1, v2, l2 = arrival_fields(polys, W, H, px, bay, margin, reach)
    v1, v2 = v1.cpu().numpy(), v2.cpu().numpy(); l1 = l1.cpu().numpy().astype(np.int32); l2 = l2.cpu().numpy().astype(np.int32)
    if (v1 >= BIG / 2).any() or (l1 < 0).any(): raise ValueError(f'part of the board is further than the {margin} mm window margin from every silhouette')
    l2[v2 >= BIG / 2] = -1; lab, stray = fl.connect(l1, n)
    if bay:
        for _ in range(3):
            lab, m = fl.despike(lab, v1 <= 0, n, px); moved += m
            if not m: break
        lab, more = fl.connect(lab, n); stray += more
    arcs = fl.trace(lab, v1, l1, v2, l2, W, H, np.ones((h, w), np.float32) if bay else None)
    for sg in (sigma, sigma / 2, .1, 0.):
        cut = union_all([ln for _, ln in fl.seams(arcs, sg)], grid_size=.01); faces = list(polygonize(cut))
        if len(faces) != n: continue
        owner = [int(lab[min(h - 1, int(p.representative_point().y * h / H)), min(w - 1, int(p.representative_point().x * w / W))]) for p in faces]
        if sorted(owner) != list(range(n)): owner = assign_by_silhouette(faces, polys)       # one sample point can land across a seam
        if owner is not None and sorted(owner) == list(range(n)): break
    else: raise ValueError(f'vectorised seams give {len(faces)} faces for {n} pieces, or no one-to-one match with the silhouettes')
    return {'faces': [faces[i] for i in np.argsort(owner)], 'cut': cut, 'lab': lab, 'stray_px': stray, 'lobe_px': moved, 'smoothing_mm': sg, 'px': px}

# ---------------------------------------------------------------- decoder refinement with bounded memory

def refine(k, z0, c, th, size, W, H, s_lo, s_hi, steps=260, px=1.25, lam=(150., 3000.), chunk=40, log=None):
    """field.refine, with the distance evaluations of `chunk` pieces at a time recomputed in the backward pass"""
    n = len(k); w = round(W / px); h = round(H / px); win = 2 * int(math.ceil((.58 * s_hi + 8) / px)); off = torch.arange(-win // 2, win // 2, device=DEV)
    oy, ox = torch.meshgrid(off, off, indexing='ij'); zlim = torch.where(k == 0, .28, 1.)[:, None]
    c = c.clone().requires_grad_(True); th = th.clone().requires_grad_(True); a = torch.logit(((size - s_lo) / (s_hi - s_lo)).clamp(.02, .98)).requires_grad_(True)
    zeta = torch.atanh((z0 / zlim).clamp(-.97, .97)).requires_grad_(True)
    opt = torch.optim.Adam([{'params': [c], 'lr': .06}, {'params': [th], 'lr': .0015}, {'params': [a], 'lr': .006}, {'params': [zeta], 'lr': .02}])
    for it in range(steps):
        f = it / max(1, steps - 1); weight = lam[0] * (lam[1] / lam[0]) ** f; sz = s_lo + (s_hi - s_lo) * torch.sigmoid(a); jit = (torch.rand(2, device=DEV) - .5) * px
        V = fl.flatten_t(fl.place_t(fl.net(k, zlim * torch.tanh(zeta)), c, th, sz), 3)
        ci = torch.floor(c.detach() / px).long(); col = ci[:, 0, None, None] + ox; row = ci[:, 1, None, None] + oy
        X = torch.stack([(col + .5) * px + jit[0], (row + .5) * px + jit[1]], -1).reshape(n, -1, 2)
        phi = torch.cat([checkpoint(fl.sdf_polys, X[s:s + chunk], V[s:s + chunk], use_reentrant=False) for s in range(0, n, chunk)]).reshape(n, win, win)
        ok = (col >= 0) & (col < w) & (row >= 0) & (row < h)
        gap, ovl, m1 = fl.misfit(torch.where(ok, row * w + col, torch.zeros_like(col)).reshape(-1), torch.where(ok, phi, torch.full_like(phi, BIG)).reshape(-1), h * w, far=.5 * win * px)
        E = (gap + weight * (ovl + ((-phi).clamp_min(0) ** 2 * (~ok)).sum())) * px * px / (W * H)
        opt.zero_grad(); E.backward(); opt.step()
        if log and (it % 65 == 0 or it == steps - 1): log(f'  refine {it:4d} E {float(E):7.3f} covered {float((m1 <= 0).float().mean()):.3f} overlap energy {float(ovl) * px * px:8.2f}')
    return c.detach(), th.detach(), (s_lo + (s_hi - s_lo) * torch.sigmoid(a)).detach(), (zlim * torch.tanh(zeta)).detach()

# ---------------------------------------------------------------- progress snapshots

def snapshot(out, name, polys, W, H, caption, faces=None):
    """quick PNG of the silhouettes (and, once cut, the pieces) for progress reports"""
    out.mkdir(parents=True, exist_ok=True); scale = max(2., 2400 / max(W, H))
    layers = []
    if faces is not None:
        tone = fl.colouring(faces); grey = [239, 217, 228, 201, 248, 188]
        layers.append(([np.array(p.exterior.coords) for p in faces], [grey[t % 6] for t in tone]))
    layers.append((list(polys), [37] * len(polys)))
    path = out / f'progress-{name}.png'; fl.raster(path, W, H, layers, scale=scale, edge=150 if faces is not None else None)
    img = cv2.imread(str(path)); bar = np.full((44, img.shape[1], 3), 255, np.uint8)
    cv2.putText(bar, caption, (12, 30), cv2.FONT_HERSHEY_SIMPLEX, .8, (30, 30, 30), 2, cv2.LINE_AA); cv2.imwrite(str(path), np.vstack([bar, img]))
    status = out / 'progress.json'; rows = json.loads(status.read_text()) if status.exists() else []
    rows.append({'time': time.strftime('%H:%M:%S'), 'stage': name, 'caption': caption, 'image': str(path)}); status.write_text(json.dumps(rows, indent=1))

# ---------------------------------------------------------------- composition

def compose(seed=22, W=570., H=390., n=300, restarts=4, min_size=34., px=.25, bay=32., layouts=3, margin=48., resume=None, out=None, log=print):
    """field.compose for large boards: logos only, untrained seams (the bay rule), windowed partition"""
    from refine import flag
    t0 = time.time(); tried = []; reverted = 0
    if resume is None:
        rng = np.random.default_rng(seed); torch.manual_seed(seed); ks, zs, qs, areas = [], [], [], []
        for i in range(n):
            q, p, z = sample(rng, 0); ks.append(0); zs.append(z); qs.append(q); areas.append(p.area)
        log(f'{n} PathNet logos on {W:g} x {H:g} mm, device {DEV}')
        G = torch.stack([fl.canon(q) for q in qs]); packed, (s_lo, s_hi) = fl.pack(G, areas, W, H, restarts, min_size=min_size, log=log)
        k = T(ks, torch.long); z0 = T(np.array(zs)); best = None
        log(f'  size range {s_lo:.1f}-{s_hi:.1f} mm; best packing score {packed[0][0]:.3f} ({time.time() - t0:.0f}s)')
        if out is not None:
            _, c0, th0, size0, m0 = packed[0]
            snapshot(out, '1-layout', fl.outlines(k, z0, c0, th0, size0), W, H, f'Layout search: {n} logos placed, {m0["covered"]:.0%} of the board covered ({time.time() - t0:.0f} s)')
        for li, (score, c0, th0, size0, _) in enumerate(packed[:max(1, layouts)]):
            c, th, size, z = refine(k, z0, c0, th0, size0, W, H, s_lo, s_hi, log=log); rv = 0
            for i in range(n):          # a refined latent must still decode to a logo that passes the sampler's own test
                p = Polygon(flatten(fl.net(k[i:i + 1], z[i:i + 1])[0].cpu().numpy().astype(float), 6))
                if not (p.is_valid and p.area > .035 and p.intersection(ref).area / p.union(ref).area >= .85): z[i] = z0[i]; rv += 1
            if out is not None:         # keep every refined layout, so it can be cut again with --resume
                out.mkdir(parents=True, exist_ok=True)
                torch.save({'seed': seed, 'k': k.cpu(), 'z': z.cpu(), 'c': c.cpu(), 'th': th.cpu(), 'size': size.cpu(), 'W': W, 'H': H, 'bay': bay,
                            'log_slowness': None, 'field': None}, out / f'layout-{li}-state.pt')
            m = None
            if layouts > 1:
                try:
                    faces = partition(fl.outlines(k, z, c, th, size), W, H, 2 * px, bay=bay, margin=margin)['faces']
                    flags = sum(flag(f) for f in faces); m = -1. if flags else fl.lock_margin(faces)
                except ValueError as e: m = -1.; log(f'  layout {li}: {e}')
                tried.append({'packing_score': score, 'lock_margin_mm': m}); log(f'  layout {li} (packing score {score:.3f}): lock margin {m:.2f} mm ({time.time() - t0:.0f}s)')
            if best is None or (m is not None and m > best[0]): best = (m if m is not None else 0., c, th, size, z, rv)
        _, c, th, size, z, reverted = best
    else:
        st = torch.load(resume, map_location='cpu'); seed, W, H = st['seed'], st['W'], st['H']; bay = st.get('bay', bay)
        k, z, c, th, size = (st[q].to(DEV) for q in ('k', 'z', 'c', 'th', 'size')); n = len(k); log(f'resumed {n} silhouettes on {W:g} x {H:g} mm from {resume}')
    polys = fl.outlines(k, z, c, th, size); lay = fl.measure(fl.distance_stack(polys, W, H, 1., 60.), W, H)
    log(f'  silhouettes cover {lay["covered"]:.3f} of the board, rms gap {lay["rms_gap_mm"]:.2f} mm, deepest overlap {lay["max_overlap_depth_mm"]:.2f} mm ({time.time() - t0:.0f}s)')
    if out is not None: snapshot(out, '2-refined', polys, W, H, f'Refined: logo poses, sizes and shapes optimised together, {lay["covered"]:.0%} covered ({time.time() - t0:.0f} s)')
    res = partition(polys, W, H, px, bay=bay, margin=margin); bis = partition(polys, W, H, px, bay=0., margin=margin)
    flags = sum(flag(f) for f in res['faces']); log(f'  cut {len(res["faces"])} pieces, {flags} erosion flags ({time.time() - t0:.0f}s)')
    if out is not None: snapshot(out, '3-cut', polys, W, H, f'Cut: {len(res["faces"])} pieces with shared seams, {flags} fragile ({time.time() - t0:.0f} s)', res['faces'])
    slides = lambda faces: {'assembly_lock_margin_mm': fl.lock_margin(faces), **{f'blocking_{need:g}mm': fl.lock_stats(faces, need)[0] for need in (1., 2., 3.)}}
    base = slides(res['faces'])
    meta = [{'class': names[int(k[i])], 'k': int(k[i]), 'size': float(size[i]), 'latent': z[i].tolist(), 'angle': float(th[i]), 'center_mm': c[i].tolist()} for i in range(n)]
    report = {'seed': seed, 'width_mm': W, 'height_mm': H, 'requested_pieces': n, 'requested_logo_pieces': n, 'device': str(DEV), 'resumed_from': None if resume is None else str(resume),
              'layout': lay, 'layouts_compared': tried, 'latents_reverted': reverted, 'size_range_mm': [float(size.min()), float(size.max())],
              'partition': {'method': f'windowed first arrival, {margin:g} mm window margin', 'pixel_mm': px, 'bay_rule_strength': bay, 'stray_pixels_reassigned': res['stray_px'],
                            'spur_pixels_reassigned': res['lobe_px'], 'seam_smoothing_mm': res['smoothing_mm']},
              'field': {'trained': False, 'steps': 0, 'interlock_without_field': base, 'interlock_plain_bisectors': slides(bis['faces'])}}
    state = {'seed': seed, 'k': k.cpu(), 'z': z.cpu(), 'c': c.cpu(), 'th': th.cpu(), 'size': size.cpu(), 'W': W, 'H': H, 'bay': bay, 'log_slowness': None, 'field': None}
    report['seconds'] = time.time() - t0
    return res, polys, meta, report, state


def previews(out, faces, polys, meta, W, H, tag='aphex-field'):
    """high-resolution views on top of field.export's: the assembled board at about 8 px/mm and a wide piece sheet"""
    tone = fl.colouring(faces); grey = [239, 217, 228, 201, 248, 188]; ext = [np.array(p.exterior.coords) for p in faces]; scale = max(4., 4600 / max(W, H))
    fl.raster(out / f'{tag}-puzzle-hires.png', W, H, [(ext, [grey[t % 6] for t in tone]), (polys, [37] * len(polys))], scale=scale, edge=120)
    wood = [(int(150 + 25 * (t % 3)), int(185 + 15 * (t % 2)), int(222 - 6 * (t % 4))) for t in tone]                     # BGR plywood tones
    fl.raster(out / f'{tag}-pieces-hires.png', W, H, [(ext, wood)], scale=scale, edge=60)
    cols = max(8, int(math.ceil(math.sqrt(len(faces) * 1.6))))
    fl.sheet(out / f'{tag}-piece-sheet-wide.png', faces, polys, meta, True, cols=cols, cell=200)


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    a.add_argument('--seed', type=int, default=22); a.add_argument('--width', type=float, default=570); a.add_argument('--height', type=float, default=390)
    a.add_argument('--pieces', type=int, default=300); a.add_argument('--restarts', type=int, default=4); a.add_argument('--layouts', type=int, default=3)
    a.add_argument('--min-size', type=float, default=34., help='smallest logo extent in mm; 34 keeps every 3 mm feature of the logo')
    a.add_argument('--bay', type=float, default=32.); a.add_argument('--margin', type=float, default=48., help='window margin in mm around every silhouette')
    a.add_argument('--resume', type=Path, help='a saved *-state.pt: keep its layout and cut it again')
    a.add_argument('--output', type=Path, required=True); a = a.parse_args()
    log = lambda s: print(s, flush=True)
    res, polys, meta, report, state = compose(a.seed, a.width, a.height, a.pieces, a.restarts, a.min_size, bay=a.bay, layouts=a.layouts, margin=a.margin, resume=a.resume, out=a.output, log=log)
    faces = fl.export(a.output, res, polys, meta, report, state)
    previews(a.output, faces, polys, meta, report['width_mm'], report['height_mm'])
    snapshot(a.output, '4-final', polys, report['width_mm'], report['height_mm'], f'Final: {report["pieces"]} pieces, lock margin {report["interlock"]["assembly_lock_margin_mm"]:.2f} mm, {report["erosion_flags_2_5mm"]} fragile', faces)
    print(json.dumps({k: v for k, v in report.items() if k not in ('motifs', 'field')}, indent=1)); print(json.dumps(report['field']['interlock_without_field']))
