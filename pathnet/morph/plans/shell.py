"""Molluscs and other shelled animals for the FossilNet v3 corpus.

Coiled cephalopods (ammonite, heteromorph, nautiloid), straight ones (orthocone, belemnite), snails (high-spired
gastropod, spiny murex, low-spired snail), two-valved shells (brachiopod, scallop, clam), the chiton, and a free plan
that recombines their parts. Shells are drawn the way the owner's marker drawings look: one chunky white silhouette,
round-capped strokes and filled blobs with a little hand wobble, and a few engraved marks (whorl seams, sutures, septa,
apertures, growth lines, eyes) that are trimmed so they stay inside the 1 mm engraving clearance.

Orientation: the apex (umbo, beak, spire tip) is at the top; animals drawn with their soft parts (living nautilus,
belemnite, orthocone with arms) have the head at the top or, for the coiled nautilus, the aperture to one side.
"""
import math

import numpy as np

from morph.core import (Canvas, arc, bend, catmull, compose, densify, ellipse_pts, mirror, rotate, shear, stretch,
                        taper, wave, wiggle)

TAU = 2 * math.pi
MAXF = 6                       # engraving loops the corpus keeps


# ---------------------------------------------------------------- small helpers

def U(rng, a, b):
    return float(rng.uniform(a, b))


def I(rng, a, b):
    """integer in [a, b] inclusive"""
    return int(rng.integers(a, b + 1))


def pick(rng, opts, p=None):
    p = None if p is None else np.asarray(p, float) / np.sum(p)
    return opts[int(rng.choice(len(opts), p=p))]


def polar(r, th):
    r = np.asarray(r, float); th = np.asarray(th, float)
    return np.stack([r * np.cos(th), r * np.sin(th)], -1)


def arclen(p):
    p = np.asarray(p, float)
    return np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]


def normals(p):
    """unit left normals of an open polyline (y down: the normal points to the right of the direction of travel when
    the curve runs downward)"""
    d = np.gradient(np.asarray(p, float), axis=0); n = np.stack([-d[:, 1], d[:, 0]], 1)
    return n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-12)


def place(pts, at=(0., 0.), scale=1., ang=0., flip=False):
    p = np.array(pts, float).reshape(-1, 2) * scale
    if flip: p[:, 0] *= -1
    c, s = math.cos(ang), math.sin(ang)
    return np.stack([c * p[:, 0] - s * p[:, 1] + at[0], s * p[:, 0] + c * p[:, 1] + at[1]], 1)


def loop_wobble(rng, pts, amp=.005, fmax=6):
    """smooth periodic displacement along the normals of a closed polygon: a hand-drawn outline"""
    p = np.asarray(pts, float)
    if amp <= 0 or len(p) < 8: return p
    d = np.roll(p, -1, 0) - np.roll(p, 1, 0); n = np.stack([d[:, 1], -d[:, 0]], 1)
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    q = np.vstack([p, p[:1]]); s = arclen(q); s = s[:-1] / max(s[-1], 1e-9)
    w = sum(rng.normal() * np.sin(TAU * (f * s + rng.random())) for f in range(2, fmax + 1))
    return p + n * (w * amp / math.sqrt((fmax - 1) / 2))[:, None]


def resample_open(p, n):
    p = np.asarray(p, float); s = arclen(p)
    if s[-1] <= 1e-12: return np.repeat(p[:1], n, 0)
    t = np.linspace(0, s[-1], n); return np.stack([np.interp(t, s, p[:, i]) for i in range(2)], 1)


def bumps(phase, power=1.5):
    """rounded periodic bumps in [0, 1], one per unit of phase, peaks at integer phase"""
    return ((1 + np.cos(TAU * phase)) / 2) ** power


def ribbed_offset(pts, spacing, amp, power=1.5, phase0=0.):
    """bump profile along a polyline: rib i sits every `spacing` units of arc length (spacing and amp may be arrays)"""
    s = arclen(pts); sp = np.broadcast_to(np.asarray(spacing, float), s.shape)
    ph = phase0 + np.r_[0, np.cumsum(np.diff(s) / np.maximum(sp[1:], 1e-6))]
    return np.broadcast_to(np.asarray(amp, float), s.shape) * bumps(ph, power), ph


# ---------------------------------------------------------------- engraving that stays inside the clearance

def _seg_dist(P, A, B):
    AB = B - A; L2 = np.maximum((AB * AB).sum(1), 1e-18)
    APx = P[:, None, 0] - A[None, :, 0]; APy = P[:, None, 1] - A[None, :, 1]
    t = np.clip((APx * AB[None, :, 0] + APy * AB[None, :, 1]) / L2[None], 0., 1.)
    dx = APx - t * AB[None, :, 0]; dy = APy - t * AB[None, :, 1]
    return np.sqrt(dx * dx + dy * dy), t


def _inside(P, Q):
    x = P[:, :1]; y = P[:, 1:]; xa, ya = Q[None, :, 0], Q[None, :, 1]
    xb = np.roll(Q[:, 0], -1)[None]; yb = np.roll(Q[:, 1], -1)[None]
    cr = (ya > y) != (yb > y); dy = np.where(yb == ya, 1e-300, yb - ya)
    xi = xa + (y - ya) * (xb - xa) / dy
    return (cr & (x < xi)).sum(1) % 2 == 1


def _op_depth(o, P):
    """signed depth of points P inside one display-list op (positive inside)"""
    Q = o.pts
    if o.kind == 'blob':
        d, _ = _seg_dist(P, Q, np.roll(Q, -1, 0)); d = d.min(1)
        return np.where(_inside(P, Q), d, -d)
    if len(Q) == 1: return o.r[0] - np.hypot(P[:, 0] - Q[0, 0], P[:, 1] - Q[0, 1])
    d, t = _seg_dist(P, Q[:-1], Q[1:]); rr = o.r[:-1][None] + t * (o.r[1:] - o.r[:-1])[None]
    return (rr - d).max(1)


def depth(c, P):
    """lower bound of the depth of (warped) points P inside the body drawn so far (negative outside)"""
    P = np.asarray(P, float).reshape(-1, 2); add = np.full(len(P), -1.); cut = np.full(len(P), 1.)
    for o in c.ops:
        if o.layer != 'body': continue
        rm = float(o.r.max()) if o.r is not None else 0.; g = 0. if o.mode == 'add' else .2
        lo = o.pts.min(0) - rm - g; hi = o.pts.max(0) + rm + g
        m = (P[:, 0] > lo[0]) & (P[:, 0] < hi[0]) & (P[:, 1] > lo[1]) & (P[:, 1] < hi[1])
        if not m.any(): continue
        if o.mode == 'add': add[m] = np.maximum(add[m], _op_depth(o, P[m]))
        else: cut[m] = np.minimum(cut[m], -_op_depth(o, P[m]))
    return np.minimum(add, cut)


def extent(c):
    body = [o for o in c.ops if o.layer == 'body' and o.mode == 'add']
    lo = np.min([o.pts.min(0) - (o.r.max() if o.r is not None else 0) for o in body], 0)
    hi = np.max([o.pts.max(0) + (o.r.max() if o.r is not None else 0) for o in body], 0)
    return float((hi - lo).max())


def nfeat(c):
    return getattr(c, '_nf', 0)


def _bump_nf(c, k=1):
    c._nf = nfeat(c) + k


def _warp(c, P):
    return c.warp(P) if c.warp is not None else P


def feat_line(c, pts, r, min_len=.04, margin=.012, cap=MAXF):
    """engrave a line, keeping only the pieces that sit deep enough inside the body; returns pieces added"""
    if nfeat(c) >= cap: return 0
    P = densify(np.asarray(pts, float).reshape(-1, 2), .008)
    if len(P) < 2: return 0
    need = extent(c) / 30 + r + margin
    ok = depth(c, _warp(c, P)) >= need
    added = 0; i = 0; n = len(P)
    while i < n and nfeat(c) < cap:
        if not ok[i]: i += 1; continue
        j = i
        while j < n and ok[j]: j += 1
        q = P[i:j]
        if len(q) >= 2 and arclen(q)[-1] >= min_len:
            c.feature_stroke(q, r); _bump_nf(c); added += 1
        i = j
    return added


def feat_dot(c, p, r, margin=.012, cap=MAXF):
    if nfeat(c) >= cap: return 0
    p = np.asarray(p, float).reshape(1, 2)
    if depth(c, _warp(c, p))[0] < extent(c) / 30 + r + margin: return 0
    c.eye(p[0], r); _bump_nf(c); return 1


def feat_blob(c, poly, margin=.012, cap=MAXF):
    if nfeat(c) >= cap: return 0
    poly = np.asarray(poly, float)
    if depth(c, _warp(c, densify(poly, .015, closed=True))).min() < extent(c) / 30 + margin: return 0
    c.feature_blob(poly); _bump_nf(c); return 1


def flipx(p):
    return mirror(p)


def canvas(rng, k=(.012, .02), wob=(.003, .005), warp=None):
    c = Canvas(k=U(rng, *k), wobble=U(rng, *wob), warp=warp); c._nf = 0; return c


# ---------------------------------------------------------------- coiled shells (ammonite, nautiloid)

def coil_outline(rng, R, W, phi_a, ribs=None, face_curve=0., n=420):
    """planispiral shell from the side: the venter of the last whorl as a logarithmic spiral from the previous whorl
    (radius R / W) round to the aperture (radius R, direction phi_a, growth toward increasing angle), closed by the
    aperture face. ribs: dict(spacing, amp, power, alt, fade) puts rib bumps on the venter."""
    b = math.log(W) / TAU
    psi = np.linspace(phi_a - TAU, phi_a, n); rb = R * np.exp(b * (psi - phi_a))
    r = rb.copy()
    if ribs:
        base = polar(rb, psi); rel = rb / R
        amp = ribs['amp'] * rel ** ribs.get('grow', .6)
        fade = ribs.get('fade', 0.)
        if fade > 0: amp = amp * np.clip((psi - psi[0]) / (fade * TAU), 0, 1)
        prof, ph = ribbed_offset(base, ribs['spacing'] * rel ** ribs.get('sgrow', .3), amp, ribs.get('power', 1.5), ribs.get('phase', 0.))
        if ribs.get('alt', 1.) != 1.: prof = prof * np.where(np.round(ph) % 2 == 0, 1., ribs['alt'])
        r = rb + prof
    venter = polar(r, psi)
    v = np.linspace(1, 0, 16)[1:-1]; rho = R / W + (R - R / W) * v
    ang = phi_a - face_curve * np.sin(np.pi * v) / rho
    return np.vstack([venter, polar(rho, ang)]), b


def seam_pieces(R, u, b, phi_a, sig0, sig_max, max_len=.55, gap=.035, r_min=.065):
    """the umbilical seam spiral (radius u R exp(-b sigma) at direction phi_a - sigma) cut into pieces of at most
    max_len units with gaps of about `gap` units, so each piece stays a clean engraving loop"""
    ss = np.linspace(sig0, sig_max, 600); rr = u * R * np.exp(-b * ss); keep = rr >= r_min
    ss, rr = ss[keep], rr[keep]
    if len(ss) < 4: return []
    pts = polar(rr, phi_a - ss); s = arclen(pts); out = []; a = 0.
    while a < s[-1] - .1:
        e = min(a + max_len, s[-1]); m = (s >= a) & (s <= e)
        if m.sum() >= 4: out.append(pts[m])
        a = e + gap
    return out


def whorl_mark(rng, kind, psi, rin, rout, n=26):
    """an engraved mark across the whorl flank at direction psi, from radius rin (umbilical side) to rout (venter)"""
    t = np.linspace(0, 1, n); rho = rin + (rout - rin) * t; L = rout - rin
    if kind == 'goniatite':                      # chevron lobes pointing back
        k = pick(rng, [1, 1, 2]); z = 1 - np.abs(((t * k) % 1) * 2 - 1); lat = -U(rng, .03, .05) * z * min(1, L / .18)
    elif kind == 'ceratite':                     # rounded saddles
        lat = U(rng, .022, .034) * np.sin(TAU * U(rng, 1.2, 1.8) * t + U(rng, -.5, .5))
    elif kind == 'falcate':                      # sickle rib: back at mid-flank, swept forward at the venter
        lat = -U(rng, .02, .04) * np.sin(np.pi * t) + U(rng, .03, .06) * t ** 3
    elif kind == 'nautilus':                     # gentle sigmoid septum
        lat = U(rng, .025, .05) * np.sin(np.pi * t) * pick(rng, [-1, 1]) + U(rng, -.015, .015) * t
    else:                                        # rib: radial, leaning forward toward the venter
        lat = U(rng, .0, .035) * t + U(rng, -.012, .012) * np.sin(np.pi * t)
    return polar(rho, psi + lat / np.maximum(rho, .05))


def draw_coil_features(c, rng, R, W, u, b, phi_a, mark, n_mark, rf=None, span=(.45, 2.4), seam_cap=3, dot=True):
    """engraving of a coiled shell: seam spiral pieces, an umbilicus dot, marks across the outer whorl"""
    rf = rf or U(rng, .013, .017)
    seam = u * R > .09
    if seam:
        pcs = seam_pieces(R, u, b, phi_a, U(rng, .2, .4), U(rng, 3.5, 6.) * np.pi, max_len=U(rng, .42, .6))
        for q in pcs[:seam_cap]: feat_line(c, q, rf, min_len=.12)
    if dot:
        rd = U(rng, .022, .034) if seam else U(rng, .028, min(.06, max(.03, u * R * .8)))
        feat_dot(c, (0., 0.), rd)
    if n_mark > 0:
        rs = rf * U(rng, .85, 1.); lo, hi = span
        sig = np.linspace(lo, hi, n_mark) + rng.uniform(-.12, .12, n_mark)
        for sg in sig:
            rv = R * math.exp(-b * sg); rseam = u * R * math.exp(-b * sg)
            rin = rseam + rf + rs + .03 if seam else max(.06, rseam + rs + .03)
            if rv - rin < .1: continue
            feat_line(c, whorl_mark(rng, mark, phi_a - sg, rin, rv), rs, min_len=.07)


def ammonite(rng):
    """planispiral ammonoid from the side: ribbed, tuberculate, keeled, goniatite or ceratite"""
    style = pick(rng, ['ribbed', 'tuberculate', 'keeled', 'goniatite', 'ceratite'], [.36, .14, .18, .16, .16])
    R = .5; phi_a = U(rng, -np.pi, np.pi)
    if style == 'goniatite':
        W = U(rng, 1.9, 2.6); u = U(rng, .14, .32) / W
    elif style == 'keeled':
        W = U(rng, 1.7, 2.5); u = U(rng, .45, 1.) / W
    else:
        W = U(rng, 1.5, 2.3); u = U(rng, .55, 1.) / W
    ribs = None
    if style in ('ribbed', 'ceratite'):
        ribs = dict(spacing=U(rng, .13, .18), amp=U(rng, .025, .045), power=U(rng, 1., 1.8), grow=U(rng, .2, .9),
                    sgrow=U(rng, 0., .6), alt=U(rng, .5, 1.) if rng.random() < .3 else 1., fade=U(rng, 0, .3), phase=U(rng, 0, 1))
        if style == 'ceratite': ribs.update(spacing=U(rng, .17, .23), amp=U(rng, .035, .055), power=U(rng, 1.6, 2.6))
    elif style == 'tuberculate':
        ribs = dict(spacing=U(rng, .18, .26), amp=U(rng, .045, .07), power=U(rng, 1.8, 3.), grow=U(rng, .4, 1.), sgrow=U(rng, 0, .5), phase=U(rng, 0, 1))
    elif style == 'keeled' and rng.random() < .4:
        ribs = dict(spacing=U(rng, .11, .15), amp=U(rng, .01, .018), power=1., grow=.5, phase=U(rng, 0, 1))
    face_curve = U(rng, -.01, .06); flip = bool(rng.random() < .5); sx = U(rng, .9, 1.1)
    c = canvas(rng, wob=(.003, .0045), warp=compose(flipx if flip else None, stretch(sx, 1 / sx), shear(U(rng, -.12, .12)) if rng.random() < .4 else None))
    poly, b = coil_outline(rng, R, W, phi_a, ribs, face_curve)
    c.blob(loop_wobble(rng, poly, .003))
    tang = np.array([-math.sin(phi_a), math.cos(phi_a)])
    ap = pick(rng, ['plain', 'rostrum', 'lappet', 'collar'], [.5, .18, .17, .15]) if style != 'goniatite' else 'plain'
    if ap == 'rostrum':                          # the venter runs on as a short tongue
        rs = U(rng, .038, .046); ss = np.linspace(-.3, U(rng, .08, .16), 12)
        c.stroke(polar(R * np.exp(b * ss) - rs * (1 + 1.2 * np.clip(ss, 0, None) / .16), phi_a + ss), np.linspace(rs, rs * .92, 12))
    elif ap == 'lappet':                         # spoon-shaped ear from mid-flank
        rl = U(rng, .036, .044); rm = R / W + (R - R / W) * U(rng, .42, .58); dl = U(rng, .07, .12); p0 = polar(rm, phi_a - .12)
        c.stroke(catmull(np.array([p0, p0 + tang * dl * .55, p0 + tang * dl - polar(rm, phi_a) / rm * .01]), 10), [rl * .9, rl * .85, rl * 1.2])
    elif ap == 'collar':                         # thickened peristome
        rc = U(rng, .04, .05); c.stroke(polar(np.linspace(R / W + .04, R - rc * .6, 8), np.full(8, phi_a - .04)), rc)
    mark = {'goniatite': 'goniatite', 'ceratite': 'ceratite', 'keeled': 'falcate'}.get(style, 'rib')
    n_mark = I(rng, 2, 3)
    if style == 'keeled':                        # groove beside the keel
        rk = U(rng, .012, .015); dk = U(rng, .07, .09); ss = np.linspace(U(rng, .4, .7), U(rng, 2.6, 3.6), 40)
        feat_line(c, polar(R * np.exp(-b * ss) - dk, phi_a - ss), rk, min_len=.15)
    draw_coil_features(c, rng, R, W, u, b, phi_a, mark, n_mark, seam_cap=3 if style != 'keeled' else 2)
    c.genome = dict(style=style, W=W, umbilical=u * W, aperture_dir=phi_a, aperture=ap, ribs=ribs, flip=flip, stretch=sx)
    return c


PLANS = {
    'ammonite': (1.4, ammonite),
}
