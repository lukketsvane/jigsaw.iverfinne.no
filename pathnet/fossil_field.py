"""Fossil boards: every piece is a FossilNet creature (trilobite, coral or dendrite) drawn from the owner's sketches.

The composition is field_large's: lay the silhouettes out (field.pack), refine poses, sizes and latents together
through the decoder, split the board by first arrival with the bay rule, export and re-read the cut. FossilNet
replaces PathNet inside the refinement, each family keeps the size factor that holds its 3 mm features, and the
drawings' eyes and faces are exported as a separate engraving layer, kept at least 1 mm inside every cut piece.

    ..\\.venv\\Scripts\\python.exe fossil_field.py --mix trilobites --output ..\\runs\\fossil-boards\\trilobites-s1
    ..\\.venv\\Scripts\\python.exe fossil_field.py --mix reef --output ...
    ..\\.venv\\Scripts\\python.exe fossil_field.py --mix fossil-sea --output ...
"""
import argparse, json, math, time
from pathlib import Path

import cv2
import numpy as np
import torch
from shapely.geometry import Polygon

import field as fl
import field_large as fll
import fossil_net as fn
from field import DEV, BIG, T

fl.GL = 1.3                    # canonical distance grids must hold the larger families (size factors up to 1.9)
fl.GRES = 169                  # ... at the same sample spacing as field.py's 129 samples over [-1, 1]
MIXES = {'trilobites': ('trilobite',), 'reef': ('coral', 'dendrite'), 'fossil-sea': ('trilobite', 'coral', 'dendrite')}


def choose(fams, categories, n, rng):
    """n family indices: drawings of the chosen categories in turn (balanced), each mirrored half the time"""
    keys = sorted({f['key'] for f in fams if f['category'] in categories}, key=lambda k: [f['key'] for f in fams].index(k))
    picks = []
    for i in range(n):
        key = keys[i % len(keys)]; mirror = bool(rng.integers(2))
        picks.append(next(j for j, f in enumerate(fams) if f['key'] == key and f['mirror'] == mirror))
    rng.shuffle(picks); return picks


def auto_pieces(fams, categories, W, H, s_ref=34.):
    """piece count at which the mean motif size is about s_ref mm (field.pack's own size rule)"""
    sel = [f for f in fams if f['category'] in categories and not f['mirror']]
    unit = np.mean([Polygon(fn.flatten(f['q'])).area for f in sel])
    return int(round(.75 * W * H / (s_ref ** 2 * unit)))


def refine(net, k, z0, zr, c, th, size, W, H, s_lo, s_hi, steps=260, px=1.25, lam=(150., 3000.), chunk=12, free_latents=True, log=None):
    """field_large.refine with FossilNet as the decoder; free_latents=False moves only poses and sizes"""
    n = len(k); w = round(W / px); h = round(H / px); win = 2 * int(math.ceil((.58 * s_hi * 1.9 + 8) / px)); off = torch.arange(-win // 2, win // 2, device=DEV)
    oy, ox = torch.meshgrid(off, off, indexing='ij'); zlim = zr if torch.is_tensor(zr) else torch.full((n, 1), float(zr), device=DEV)   # per-piece safe radius
    c = c.clone().requires_grad_(True); th = th.clone().requires_grad_(True); a = torch.logit(((size - s_lo) / (s_hi - s_lo)).clamp(.02, .98)).requires_grad_(True)
    zeta = torch.atanh((z0 / zlim).clamp(-.97, .97)).requires_grad_(free_latents)
    groups = [{'params': [c], 'lr': .06}, {'params': [th], 'lr': .0015}, {'params': [a], 'lr': .006}] + ([{'params': [zeta], 'lr': .02}] if free_latents else [])
    opt = torch.optim.Adam(groups)
    from torch.utils.checkpoint import checkpoint
    for it in range(steps):
        f = it / max(1, steps - 1); weight = lam[0] * (lam[1] / lam[0]) ** f; sz = s_lo + (s_hi - s_lo) * torch.sigmoid(a); jit = (torch.rand(2, device=DEV) - .5) * px
        V = fl.flatten_t(fl.place_t(net(k, zlim * torch.tanh(zeta)), c, th, sz), 3)
        ci = torch.floor(c.detach() / px).long(); col = ci[:, 0, None, None] + ox; row = ci[:, 1, None, None] + oy
        X = torch.stack([(col + .5) * px + jit[0], (row + .5) * px + jit[1]], -1).reshape(n, -1, 2)
        phi = torch.cat([checkpoint(fl.sdf_polys, X[s:s + chunk], V[s:s + chunk], use_reentrant=False) for s in range(0, n, chunk)]).reshape(n, win, win)
        ok = (col >= 0) & (col < w) & (row >= 0) & (row < h)
        gap, ovl, m1 = fl.misfit(torch.where(ok, row * w + col, torch.zeros_like(col)).reshape(-1), torch.where(ok, phi, torch.full_like(phi, BIG)).reshape(-1), h * w, far=.5 * win * px)
        E = (gap + weight * (ovl + ((-phi).clamp_min(0) ** 2 * (~ok)).sum())) * px * px / (W * H)
        opt.zero_grad(); E.backward(); opt.step()
        if log and (it % 65 == 0 or it == steps - 1): log(f'  refine {it:4d} E {float(E.detach()):7.3f} covered {float((m1 <= 0).float().mean()):.3f} overlap energy {float(ovl.detach()) * px * px:8.2f}')
    return c.detach(), th.detach(), (s_lo + (s_hi - s_lo) * torch.sigmoid(a)).detach(), (zlim * torch.tanh(zeta)).detach()


def outlines(net, k, z, c, th, size, steps=6):
    with torch.no_grad(): return fl.flatten_t(fl.place_t(net(k, z), c, th, size), steps).double().cpu().numpy()


def engravings(net, fams, k, z, c, th, size):
    """feature loops (eyes, mouths, face lines) of every piece in board millimetres: list of lists of (m,2) arrays"""
    with torch.no_grad(): feats = net.full(k, z)[1]                                   # (n, FEAT, FP, 2) unit coordinates
    placed = fl.place_t(feats.reshape(len(k), -1, 2), c, th, size).reshape(feats.shape).cpu().numpy()
    out = []
    for i, kk in enumerate(k.tolist()):
        loops = []
        for j in range(len(fams[kk]['feats'])):
            p = placed[i, j]; p = np.vstack([p, p[:1]])
            for _ in range(2): p = np.vstack([.75 * p[:-1] + .25 * p[1:], .25 * p[:-1] + .75 * p[1:]]).reshape(2, -1, 2).transpose(1, 0, 2).reshape(-1, 2); p = np.vstack([p, p[:1]])   # Chaikin smoothing
            loops.append(p[:-1])
        out.append(loops)
    return out


def compose(mix='fossil-sea', seed=1, W=570., H=390., n=0, restarts=4, base_mm=fn.BASE_MM, px=.25, bay=32., layouts=3, margin=48., out=None, mean_size=34., log=print):
    from refine import flag
    t0 = time.time(); categories = MIXES[mix]; net, fams = fn.load(DEV); _, fams_cpu = fn.load(torch.device('cpu'))
    n = n or auto_pieces(fams, categories, W, H, mean_size); rng = np.random.default_rng(seed); torch.manual_seed(seed)
    ks = choose(fams, categories, n, rng); zs, qs, areas = [], [], []
    for kk in ks:
        q, p, z = fn.sample(rng, kk); zs.append(z); qs.append(q); areas.append(p.area)               # within the family's safe radius
    log(f'{n} FossilNet pieces ({mix}: {", ".join(categories)}) on {W:g} x {H:g} mm, {len(set(ks))} families, device {DEV}')
    G = torch.stack([fl.canon(q) for q in qs]); packed, (s_lo, s_hi) = fl.pack(G, areas, W, H, restarts, min_size=base_mm, log=log)
    k = T(ks, torch.long); z0 = T(np.array(zs)); best = None; tried = []; zr = T([fams[kk]['zsafe'] for kk in ks])[:, None]
    log(f'  size range {s_lo:.1f}-{s_hi:.1f} mm (times each family size factor); best packing score {packed[0][0]:.3f} ({time.time() - t0:.0f}s)')
    if out is not None: fll.snapshot(out, '1-layout', outlines(net, k, z0, packed[0][1], packed[0][2], packed[0][3]), W, H, f'Layout: {n} creatures placed ({time.time() - t0:.0f} s)')
    for li, (score, c0, th0, size0, _) in enumerate(packed[:max(1, layouts)]):
        c, th, size, z = refine(net, k, z0, zr, c0, th0, size0, W, H, s_lo, s_hi, log=log); rv = thin = 0
        mm = outlines(net, k, z, c, th, size)
        for i in range(n):                         # a refined latent must still decode to the drawing it came from ...
            p = Polygon(fn.flatten(net(k[i:i + 1], z[i:i + 1])[0].cpu().numpy().astype(float)))
            if not (p.is_valid and fn.iou(p, Polygon(fn.flatten(fams[int(k[i])]['q']))) >= .8): z[i] = z0[i]; rv += 1; continue
            e = Polygon(mm[i]).buffer(-1.5)         # ... and keep every part 3 mm wide at the size it is cut
            if e.is_empty or e.geom_type != 'Polygon': z[i] = z0[i]; thin += 1
        if rv + thin:                              # put-back shapes no longer fit their refined poses: settle poses and sizes only
            c, th, size, z = refine(net, k, z, zr, c, th, size, W, H, s_lo, s_hi, steps=120, lam=(3000., 3000.), free_latents=False)
            log(f'  layout {li}: {rv} latents put back for drawing fidelity, {thin} for thin parts; poses settled')
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            torch.save({'seed': seed, 'mix': mix, 'k': k.cpu(), 'z': z.cpu(), 'c': c.cpu(), 'th': th.cpu(), 'size': size.cpu(), 'W': W, 'H': H, 'bay': bay}, out / f'layout-{li}-state.pt')
        m = None
        if layouts > 1:
            try:
                faces = fll.partition(outlines(net, k, z, c, th, size), W, H, 2 * px, bay=bay, margin=margin)['faces']
                m = -1. if any(flag(f) for f in faces) else fl.lock_margin(faces)
            except ValueError as e: m = -1.; log(f'  layout {li}: {e}')
            tried.append({'packing_score': score, 'lock_margin_mm': m}); log(f'  layout {li} (packing score {score:.3f}): lock margin {m:.2f} mm ({time.time() - t0:.0f}s)')
        if best is None or (m is not None and m > best[0]): best = (m if m is not None else 0., c, th, size, z, (rv, thin))
    _, c, th, size, z, (reverted, reverted_thin) = best
    polys = outlines(net, k, z, c, th, size); lay = fl.measure(fl.distance_stack(polys, W, H, 1., 80.), W, H)
    thin_final = sum(1 for xy in polys if (lambda e: e.is_empty or e.geom_type != 'Polygon')(Polygon(xy).buffer(-1.5)))   # silhouettes still holding a part under 3 mm
    log(f'  silhouettes cover {lay["covered"]:.3f} of the board, rms gap {lay["rms_gap_mm"]:.2f} mm, deepest overlap {lay["max_overlap_depth_mm"]:.2f} mm ({time.time() - t0:.0f}s)')
    if out is not None: fll.snapshot(out, '2-refined', polys, W, H, f'Refined: poses, sizes and network latents optimised together ({time.time() - t0:.0f} s)')
    res = fll.partition(polys, W, H, px, bay=bay, margin=margin); bis = fll.partition(polys, W, H, px, bay=0., margin=margin)
    flags = sum(flag(f) for f in res['faces']); log(f'  cut {len(res["faces"])} pieces, {flags} erosion flags ({time.time() - t0:.0f}s)')
    if out is not None: fll.snapshot(out, '3-cut', polys, W, H, f'Cut: {len(res["faces"])} pieces, {flags} fragile ({time.time() - t0:.0f} s)', res['faces'])
    slides = lambda faces: {'assembly_lock_margin_mm': fl.lock_margin(faces), **{f'blocking_{need:g}mm': fl.lock_stats(faces, need)[0] for need in (1., 2., 3.)}}
    meta = []
    for i in range(n):
        f = fams[int(k[i])]
        meta.append({'class': f['label'], 'k': -1, 'family': int(k[i]), 'category': f['category'], 'drawing': f['key'], 'mirrored': f['mirror'], 'size_factor': f['size_factor'],
                     'size': float(size[i]), 'extent_mm': float(size[i]) * f['size_factor'], 'latent': z[i].tolist(), 'angle': float(th[i]), 'center_mm': c[i].tolist()})
    src_iou = []
    for i in range(n):                             # how far the network moved each creature from its drawing (both at unit pose)
        dec = Polygon(fn.flatten(net(k[i:i + 1], z[i:i + 1])[0].cpu().numpy().astype(float))); src_iou.append(fn.iou(dec, Polygon(fn.flatten(fams[int(k[i])]['q']))))
        meta[i]['drawing_iou'] = src_iou[-1]
    import hashlib
    report = {'model_sha256': hashlib.sha256((fn.DATA / 'fossilnet.pt').read_bytes()).hexdigest(), 'seed': seed, 'mix': mix, 'categories': list(categories), 'width_mm': W, 'height_mm': H, 'requested_pieces': n, 'device': str(DEV), 'model': 'FossilNet (pathnet/fossil_net.py)',
              'layout': lay, 'layouts_compared': tried, 'latents_reverted': reverted, 'latents_reverted_for_thin_parts': reverted_thin, 'silhouettes_with_parts_under_3mm': thin_final, 'size_range_mm': [float(size.min()), float(size.max())],
              'extent_range_mm': [min(m['extent_mm'] for m in meta), max(m['extent_mm'] for m in meta)],
              'drawing_iou': {'mean': float(np.mean(src_iou)), 'min': float(np.min(src_iou))},
              'families_used': {fams[j]['label']: int((k == j).sum()) for j in sorted(set(ks))},
              'partition': {'method': f'windowed first arrival, {margin:g} mm window margin', 'pixel_mm': px, 'bay_rule_strength': bay, 'stray_pixels_reassigned': res['stray_px'],
                            'spur_pixels_reassigned': res['lobe_px'], 'seam_smoothing_mm': res['smoothing_mm']},
              'field': {'trained': False, 'steps': 0, 'interlock_without_field': slides(res['faces']), 'interlock_plain_bisectors': slides(bis['faces'])}}
    state = {'seed': seed, 'mix': mix, 'k': k.cpu(), 'z': z.cpu(), 'c': c.cpu(), 'th': th.cpu(), 'size': size.cpu(), 'W': W, 'H': H, 'bay': bay, 'log_slowness': None, 'field': None}
    report['seconds'] = time.time() - t0
    return res, polys, meta, report, state, engravings(net, fams, k, z, c, th, size)


def export_engraving(out, faces, loops, W, H, polys=None, clearance=1.0, line_clearance=1.0, min_line=3.0, tag='fossil-field'):
    """engraving layer: every feature loop (eyes, faces) that stays `clearance` mm inside its own cut piece, plus the
    drawing's own outline wherever it runs at least `line_clearance` mm inside the piece (notches too narrow to cut)"""
    from generate import outsvg, points_svg
    from shapely.geometry import LineString
    kept, lines, dropped, paths = [], [], 0, []
    for i, (face, ls) in enumerate(zip(faces, loops)):
        inner = face.buffer(-clearance); mine = []
        for p in ls:
            poly = Polygon(p).buffer(0)
            if poly.is_empty or not inner.contains(poly): dropped += 1; continue
            mine.append(np.asarray(poly.exterior.coords)); paths.append(f'<path d="{points_svg(np.round(mine[-1], 3))} Z"/>')
        kept.append(mine); mine_l = []
        if polys is not None:
            ring = LineString(np.vstack([polys[i], polys[i][:1]])); seg = ring.intersection(face.buffer(-line_clearance))
            for g in getattr(seg, 'geoms', [seg]):
                if g.geom_type == 'LineString' and g.length >= min_line:
                    xy = np.asarray(g.coords); mine_l.append(xy); paths.append(f'<path d="{points_svg(np.round(xy, 3))}"/>')
        lines.append(mine_l)
    outsvg(out / f'{tag}-engrave.svg', '<g fill="none" stroke="#0000ff" stroke-width="0.1">' + ''.join(paths) + '</g>', W, H)
    cut = (out / f'{tag}-puzzle.svg').read_text(); body = cut[cut.index('>', cut.index('<svg')) + 1:cut.rindex('</svg>')]
    outsvg(out / f'{tag}-production.svg', '<g id="engrave" fill="none" stroke="#0000ff" stroke-width="0.1">' + ''.join(paths) + '</g><g id="cut">' + body + '</g>', W, H)
    return kept, lines, dropped


def render(out, faces, polys, kept, meta, W, H, lines=None, tag='fossil-field'):
    """high-resolution plywood render with engraved features, and an upright piece sheet"""
    tone = fl.colouring(faces); scale = max(4., 4600 / max(W, H)); img = np.full((round(H * scale), round(W * scale), 3), 255, np.uint8)
    pts = lambda xy, s=scale, o=(0, 0): np.round((np.asarray(xy) * s + o) * 16).astype(np.int32)
    wood = [(150 + 22 * (t % 3), 184 + 14 * (t % 2), 220 - 6 * (t % 4)) for t in tone]
    for f, col in zip(faces, wood): cv2.fillPoly(img, [pts(f.exterior.coords)], col, cv2.LINE_AA, shift=4)
    cv2.polylines(img, [pts(f.exterior.coords) for f in faces], True, (60, 60, 60), max(1, int(scale * .25)), cv2.LINE_AA, shift=4)
    for ls in kept:
        for p in ls: cv2.fillPoly(img, [pts(p)], (40, 52, 70), cv2.LINE_AA, shift=4)
    for ls in (lines or []):
        if ls: cv2.polylines(img, [pts(p) for p in ls], False, (70, 90, 120), max(1, int(scale * .3)), cv2.LINE_AA, shift=4)
    cv2.imwrite(str(out / f'{tag}-wood-hires.png'), img)
    cols = max(8, int(math.ceil(math.sqrt(len(faces) * 1.6)))); cell = 200; rows = -(-len(faces) // cols); sheet = np.full((rows * cell, cols * cell, 3), 255, np.uint8)
    for i, (f, m) in enumerate(zip(faces, meta)):
        a = -m['angle']; R = np.array([[math.cos(a), math.sin(a)], [-math.sin(a), math.cos(a)]]); ctr = np.array(m['center_mm'])
        shape = (np.asarray(f.exterior.coords) - ctr) @ R; k_ = (cell / 2 - 12) / max(np.abs(shape).max(), 1e-9); o = np.array([(i % cols + .5) * cell, (i // cols + .5) * cell])
        cv2.fillPoly(sheet, [np.round((shape * k_ + o) * 16).astype(np.int32)], wood[i], cv2.LINE_AA, shift=4)
        cv2.polylines(sheet, [np.round((shape * k_ + o) * 16).astype(np.int32)], True, (60, 60, 60), 1, cv2.LINE_AA, shift=4)
        sil = (np.asarray(polys[i]) - ctr) @ R; cv2.polylines(sheet, [np.round((sil * k_ + o) * 16).astype(np.int32)], True, (40, 40, 160), 1, cv2.LINE_AA, shift=4)
        for p in kept[i]: cv2.fillPoly(sheet, [np.round((((p - ctr) @ R) * k_ + o) * 16).astype(np.int32)], (40, 52, 70), cv2.LINE_AA, shift=4)
        for p in (lines[i] if lines else []): cv2.polylines(sheet, [np.round((((p - ctr) @ R) * k_ + o) * 16).astype(np.int32)], False, (70, 90, 120), 1, cv2.LINE_AA, shift=4)
    cv2.imwrite(str(out / f'{tag}-piece-sheet-wood.png'), sheet)


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    a.add_argument('--mix', choices=sorted(MIXES), default='fossil-sea'); a.add_argument('--seed', type=int, default=1)
    a.add_argument('--width', type=float, default=570); a.add_argument('--height', type=float, default=390); a.add_argument('--pieces', type=int, default=0, help='0 = size the count from --mean-size')
    a.add_argument('--mean-size', type=float, default=34., help='typical board size in mm before each family size factor (at least 30)')
    a.add_argument('--restarts', type=int, default=4); a.add_argument('--layouts', type=int, default=3); a.add_argument('--bay', type=float, default=32.)
    a.add_argument('--output', type=Path, required=True); a = a.parse_args(); log = lambda s: print(s, flush=True)
    res, polys, meta, report, state, loops = compose(a.mix, a.seed, a.width, a.height, a.pieces, a.restarts, bay=a.bay, layouts=a.layouts, out=a.output, mean_size=a.mean_size, log=log)
    faces = fl.export(a.output, res, polys, meta, report, state, tag='fossil-field')
    kept, lines, dropped = export_engraving(a.output, faces, loops, a.width, a.height, polys)
    report = json.loads((a.output / 'field-report.json').read_text())
    report['engraving'] = {'features_kept': sum(map(len, kept)), 'features_dropped_for_clearance': dropped, 'clearance_mm': 1.0,
                           'outline_lines': sum(map(len, lines)), 'outline_engraved_mm': round(sum(float(np.linalg.norm(np.diff(l, axis=0), axis=1).sum()) for ls in lines for l in ls), 1)}
    (a.output / 'field-report.json').write_text(json.dumps(report, indent=2))
    render(a.output, faces, polys, kept, meta, a.width, a.height, lines)
    fll.snapshot(a.output, '4-final', polys, a.width, a.height, f'Final: {report["pieces"]} pieces, lock margin {report["interlock"]["assembly_lock_margin_mm"]:.2f} mm, {report["erosion_flags_2_5mm"]} fragile', faces)
    print(json.dumps({k: v for k, v in report.items() if k not in ('motifs', 'field')}, indent=1))
