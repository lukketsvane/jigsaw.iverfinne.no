"""Arthropods beyond the dorsal trilobite for the FossilNet v3 corpus.

Plans: enrolled and side-view trilobites, legged trilobites, myriapods, lobsters and crayfish, eurypterids, horseshoe
crabs, anomalocaridids, marrellomorphs, shrimps, ostracods, and a free grammar that recombines their heads, trunks,
limbs and tails.

Conventions as in plans/_example.py: local units, the creature about one unit across, y down, head at the top. Every cut
part is at least 2 * R_MIN wide and neighbouring appendages keep gaps about as wide as themselves (checked with _gap
where they could crowd), so the 3 mm size factor stays low. Segment lines, sutures and eyes are engraving features, at
most six per creature, chosen by priority (eyes first) in _Marks.
"""
import math

import numpy as np

from morph.core import Canvas, bend, catmull, compose, densify, rotate, shear, stretch, wave, wiggle

TAU = 2 * math.pi
R_MIN = .042          # smallest radius of a cut part (0.084 units wide)
GAP = .09             # smallest gap kept between neighbouring appendages


# ---------------------------------------------------------------- small helpers

def _u(rng, a, b): return float(rng.uniform(a, b))
def _i(rng, a, b): return int(rng.integers(a, b + 1))
def _sgn(rng): return 1. if rng.random() < .5 else -1.
def _dir(a): return np.array([math.cos(a), math.sin(a)])
def _ang(v): return math.atan2(v[1], v[0])


def _smooth(t):
    t = np.clip(t, 0., 1.); return t * t * (3 - 2 * t)


def _polar(r, a):
    r, a = np.asarray(r, float), np.asarray(a, float); return np.stack([r * np.cos(a), r * np.sin(a)], -1)


def _shift(dx, dy):
    def w(p): return np.asarray(p, float) + [dx, dy]
    return w


def _flip(s):
    """mirror the creature left-right when s < 0"""
    if s > 0: return None
    def w(p):
        p = np.array(p, float); p[:, 0] *= -1; return p
    return w


def _path(base, ang, L, turn=0., curl=0., n=None):
    """centre line from base, heading ang (radians, y down: 0 right, pi/2 down), length L; `turn` bends it evenly and
    `curl` hooks its outer 45 % (radians, positive turns clockwise on screen)"""
    n = n or max(4, int(math.ceil(L / .03)) + 1)
    s = (np.arange(n - 1) + .5) / (n - 1)
    th = ang + turn * s + curl * np.clip((s - .55) / .45, 0, 1) ** 2
    d = np.stack([np.cos(th), np.sin(th)], 1) * (L / (n - 1))
    return np.vstack([[0., 0.], np.cumsum(d, 0)]) + np.asarray(base, float)


def _along(pts, t):
    """points and unit tangents at arc-length fractions t of a polyline"""
    p = np.asarray(pts, float); s = np.r_[0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))]; s = s / s[-1]
    t = np.atleast_1d(np.asarray(t, float))
    q = np.stack([np.interp(t, s, p[:, 0]), np.interp(t, s, p[:, 1])], 1)
    g = np.gradient(p, axis=0); d = np.stack([np.interp(t, s, g[:, 0]), np.interp(t, s, g[:, 1])], 1)
    return q, d / np.linalg.norm(d, axis=1, keepdims=True)


def _gap(a, ra, b, rb):
    """smallest gap between two strokes with centre lines a, b and (largest) radii ra, rb"""
    a = densify(np.asarray(a, float).reshape(-1, 2), .015); b = densify(np.asarray(b, float).reshape(-1, 2), .015)
    d = np.hypot(a[:, None, 0] - b[None, :, 0], a[:, None, 1] - b[None, :, 1])
    return float(d.min()) - ra - rb


def _gaps(a, ra, parts):
    """smallest gap from stroke (a, ra) to a list of (centre line, radius) parts"""
    return min((_gap(a, ra, p, r) for p, r in parts), default=1.)


class _Marks:
    """engraving requests with priorities: finish() keeps the six largest features, so the plan picks its six (eyes
    first) instead; each request remembers the canvas warp it was made under"""

    def __init__(self, c): self.c, self.q = c, []
    def _add(self, pri, kind, *a): self.q.append((pri, len(self.q), kind, a, self.c.warp))
    def eye(self, p, r, pri=0): self._add(pri, 'eye', p, r)
    def line(self, pts, r=.013, pri=2): self._add(pri, 'line', np.asarray(pts, float), r)
    def blob(self, pts, pri=2): self._add(pri, 'blob', np.asarray(pts, float))

    def flush(self, cap=6):
        keep = self.c.warp
        for pri, _, kind, a, w in sorted(self.q, key=lambda t: t[:2])[:cap]:
            self.c.warp = w
            if kind == 'eye': self.c.eye(*a)
            elif kind == 'line': self.c.feature_stroke(*a)
            else: self.c.feature_blob(*a)
        self.c.warp = keep


def _limb(c, rng, pts, r0, r1=None, wig=.0035):
    """a marker limb along pts, radius r0 at the base and r1 at the tip (never under R_MIN)"""
    pts = np.asarray(pts, float)
    if len(pts) > 3 and wig > 0: pts = wiggle(rng, pts, wig, _u(rng, 1.5, 3.))
    r1 = r0 if r1 is None else r1
    c.stroke(pts, np.linspace(max(r0, R_MIN), max(r1, R_MIN), len(pts)))
    return pts


def _dome(cx, yb, w, h, droop=0., n=72):
    """cephalon / carapace outline: an elliptic dome of half width w and height h over the base line y = yb, with
    genal corners drooping by `droop` below it"""
    return catmull([(cx - w, yb + droop), (cx - w * 1.02, yb - h * .45), (cx - w * .72, yb - h * .9), (cx, yb - h),
                    (cx + w * .72, yb - h * .9), (cx + w * 1.02, yb - h * .45), (cx + w, yb + droop),
                    (cx + w * .62, yb + .03), (cx, yb + .02), (cx - w * .62, yb + .03)], n, closed=True)


def _superellipse(c, rx, ry, e, ang=0., n=72):
    t = np.linspace(0, TAU, n, endpoint=False); x = np.sign(np.cos(t)) * np.abs(np.cos(t)) ** e * rx
    y = np.sign(np.sin(t)) * np.abs(np.sin(t)) ** e * ry; ca, sa = math.cos(ang), math.sin(ang)
    return np.stack([c[0] + ca * x - sa * y, c[1] + sa * x + ca * y], 1)


def _fan(c, rng, base, ang, n, spread, L, r):
    """tail fan: n lobes radiating from base around heading ang over `spread` radians; returns their centre lines"""
    out = []
    for j in range(n):
        a = ang + (spread * (j / (n - 1) - .5) if n > 1 else 0.)
        Lj = L * (1 - .12 * abs(j / max(n - 1, 1) - .5) * 2) * _u(rng, .92, 1.05)
        out.append(_limb(c, rng, _path(base, a, Lj), r * 1.08, r * _u(rng, .95, 1.1), wig=.002))
    return out


def _claw(c, rng, mk, base, ang, arm, palm, r, side, kind):
    """cheliped: an arm from base heading ang, a palm, and two fingers (open), a mitten (closed) or a C (crab)"""
    p = _limb(c, rng, _path(base, ang, arm, turn=side * _u(rng, -.5, .15)), r * 1.12, r)
    a = _ang(p[-1] - p[-2]); ctr = p[-1] + _dir(a) * palm * .42; pw = palm * _u(rng, .3, .38)
    c.ellipse(ctr, palm * .55, max(pw, R_MIN + .01), a)
    tip = ctr + _dir(a) * palm * .38; fl = palm * _u(rng, .7, 1.)
    if kind == 'open':
        op = _u(rng, .75, 1.15)
        for s in (-1., 1.):
            q = _path(tip + _dir(a + s * TAU / 4) * pw * .55, a + s * op / 2, fl, curl=-s * _u(rng, .3, .7))
            _limb(c, rng, q, R_MIN + .005, R_MIN, wig=0)
    elif kind == 'crab':
        rc = _u(rng, .05, .07); cc = tip + _dir(a) * (rc + R_MIN * .6); mouth = _u(rng, 1.5, 2.1)
        t = a + math.pi + np.linspace(mouth / 2, TAU - mouth / 2, 18)
        c.stroke(_polar(rc + R_MIN, t) + cc, R_MIN + .003)
    else:
        q = _path(tip - _dir(a) * .02, a, fl * .9, curl=side * _u(rng, -.3, .3))
        _limb(c, rng, q, pw * .75, R_MIN + .004, wig=0)
        mk.line(_path(tip - _dir(a) * pw * .4, a, fl * .55), .012, pri=3)


# ---------------------------------------------------------------- enrolled trilobite

def enrolled(rng):
    """trilobite rolled into a ball or kidney (drawing 76e4a972): the cephalon with an eye, thoracic segments as rim
    bumps between round-bottomed notches whose slits run on inward as engraved lines, and the pygidium curled round
    under the cephalon across an open slot"""
    c = Canvas(k=.02, wobble=_u(rng, .003, .005)); mk = _Marks(c)
    R = _u(rng, .43, .49); g = _u(rng, .09, .12); rn = _u(rng, .043, .05)          # radius, slot width, notch radius
    head, tail = _u(rng, .95, 1.4), _u(rng, .9, 1.55)                               # angular spans (radians)
    ths = _u(rng, 2.45, 2.9)                                                         # slot direction (lower left)
    ga = (g + .04) / R
    a0, a3 = ths + ga / 2, ths + TAU - ga / 2; a1, a2 = a0 + head, a3 - tail
    n = min(_i(rng, 4, 8), max(2, int((a2 - a1) * R / _u(rng, .19, .24))))
    edges = np.r_[a0, a1 + (a2 - a1) * np.arange(n + 1) / n, a3]
    radii = np.r_[R * _u(rng, .96, 1.03), R * rng.uniform(.99, 1.07, n), R * _u(rng, .92, 1.)]
    th = np.linspace(ths, ths + TAU, 360, endpoint=False); rr = np.full_like(th, radii[0])
    for j in range(1, len(radii)): rr = rr + (radii[j] - radii[j - 1]) * _smooth((th - edges[j]) / .14 + .5)
    rr = rr * (1 + _u(rng, -.05, .05) * np.cos(2 * (th - _u(rng, 0, TAU))))
    sq = _u(rng, .84, 1.); beta = _u(rng, -.1, 1.25); flip = _sgn(rng)
    c.warp = compose(stretch(1., sq), rotate(beta), _flip(flip))
    c.blob(_polar(rr, th))

    def r_at(a): return float(np.interp((a - ths) % TAU, th - ths, rr))
    depth = rng.uniform(.05, .11, n + 1); lines = []
    for j in range(1, n + 2):                                     # round-bottomed notches: head | segments | tail
        a = edges[j]; rb = r_at(a) - depth[j - 1]
        c.cut_stroke(_polar(np.array([rb, R + .15]), np.array([a, a])), rn, k=.025)
        r_in = max(.12, rb - rn - .05 - _u(rng, .08, .17))
        if rb - rn - .05 - r_in > .05: lines.append(_polar(np.array([rb - rn - .05, r_in]), np.array([a, a + _u(rng, -.05, .05)])))
    tw = _u(rng, -.25, .35); r0 = R * _u(rng, .12, .32)
    c.cut_stroke(catmull(_polar(np.array([r0, .55 * R, R * 1.2]), np.array([ths + tw, ths + tw * .35, ths])), 16), g / 2, k=.02)
    mk.eye(_polar(R * _u(rng, .5, .66), a0 + head * _u(rng, .35, .6)), _u(rng, .026, .036))
    for j in rng.permutation(len(lines))[:4]: mk.line(lines[j], .013, pri=1)
    if rng.random() < .5:                                         # glabella rim on the cephalon
        mk.line(_polar(np.full(10, R * .78), np.linspace(a0 + .25, a1 - .2, 10)), .013, pri=2)
    if rng.random() < .5:                                         # pygidial axis
        mk.line(_polar(np.full(10, R * .55), np.linspace(a2 + .2, a3 - .35, 10)), .013, pri=2)
    mk.flush()
    c.genome = dict(radius=R, segments=n, slot=g, head_span=head, tail_span=tail, squash=sq, turn=beta, flip=flip)
    return c


# ---------------------------------------------------------------- side-view trilobite

def side_view(rng):
    """trilobite seen from the side or three quarters (drawing bfde43d5): a hooded cephalon with a beak and two eyes,
    a leaning trunk with a fringe of walking legs on both sides (near legs longer) and a lobed tail"""
    c = Canvas(k=.022, wobble=_u(rng, .003, .006)); mk = _Marks(c)
    hw, hh, beak = _u(rng, .28, .4), _u(rng, .15, .23), _u(rng, .02, .13)
    yh = -.5 + hh
    hood = catmull([(-hw, yh + hh * .5 + beak), (-hw * 1.03, yh - hh * .15), (-hw * .7, yh - hh * .82), (0, yh - hh),
                    (hw * .55, yh - hh * .88), (hw * .93, yh - hh * .3), (hw * .85, yh + hh * .4), (hw * .3, yh + hh * .45),
                    (-hw * .5, yh + hh * .55)], 80, closed=True)
    rb0 = _u(rng, .09, .125); rb1 = rb0 * _u(rng, .62, .85); lean = _u(rng, 0., .26); x0 = _u(rng, -.06, .1)
    yb0, yb1 = yh + hh * .2, _u(rng, .3, .42)
    axis = catmull([(x0, yb0), (x0 + lean * .45 + _u(rng, -.04, .04), (yb0 + yb1) / 2), (x0 + lean, yb1)], 24)
    flip = _sgn(rng); c.warp = compose(rotate(_u(rng, -.2, .15)), _flip(flip))
    c.blob(hood); axis = _limb(c, rng, axis, rb0, rb1, wig=.004)
    ell = float(np.hypot(*np.diff(axis, axis=0).T).sum()); t0, t1 = (hh * .35) / ell, .88
    legs = {}
    for side, d0, d1, Ls, off in ((-1, _u(rng, -.25, .1), _u(rng, .25, .6), _u(rng, .13, .24), 0.),
                                  (1, _u(rng, -.2, .1), _u(rng, .2, .55), _u(rng, .16, .3), .5)):
        r = _u(rng, R_MIN, .052); pitch = _u(rng, .155, .19)
        nl = int(np.clip((t1 - t0) * ell / pitch + 1, 2, 5)); placed = []
        for i in range(nl):
            f = i / max(nl - 1, 1); t = t0 + (t1 - t0) * min(1., f + off / max(nl - 1, 1) * (i < nl - 1))
            q, d = _along(axis, t); q = q[0]
            a = (math.pi - (d0 + (d1 - d0) * f) if side < 0 else d0 + (d1 - d0) * f) + _u(rng, -.08, .08)
            L = rb0 + (rb1 - rb0) * t + Ls * _u(rng, .85, 1.12)
            p = _path(q, a, L, turn=side * _u(rng, 0., .35))
            if _gaps(p[2:], r, placed) < GAP: continue
            placed.append((p, r)); _limb(c, rng, p, r * 1.08, r)
        legs[side] = len(placed)
    q, d = _along(axis, 1.); a = _ang(d[0]); end = q[0] + d[0] * .03
    c.ellipse(end, _u(rng, .09, .14), _u(rng, .06, .09), a + math.pi / 2)
    if rng.random() < .6: c.disc(end + _dir(a) * .05 + _dir(a + math.pi / 2) * _u(rng, -.07, .07), _u(rng, .05, .065))
    re = _u(rng, .025, .034)
    mk.eye((-hw * _u(rng, .4, .6), yh - hh * _u(rng, -.05, .25)), re)
    if rng.random() < .8: mk.eye((hw * _u(rng, .2, .45), yh - hh * _u(rng, .25, .5)), re * _u(rng, .8, 1.))
    mk.flush()
    c.genome = dict(hood_width=hw, hood_height=hh, beak=beak, trunk=rb0, lean=lean, legs_near=legs[-1], legs_far=legs[1], flip=flip)
    return c


# ---------------------------------------------------------------- legged trilobite

def legged(rng):
    """trilobite with stubby curled walking legs and antennae round its body (drawing e4178fb5): a dome cephalon,
    an oval trunk, three or four thick legs a side with hooked tips"""
    c = Canvas(k=.022, wobble=_u(rng, .003, .006)); mk = _Marks(c)
    cw, ch, gd = _u(rng, .28, .4), _u(rng, .17, .26), _u(rng, .02, .09)
    tw, th = _u(rng, .14, .2), _u(rng, .22, .3)
    yc = -.5 + ch + _u(rng, .0, .05); dx = _u(rng, -.07, .07)
    flip = _sgn(rng); c.warp = compose(shear(_u(rng, -.15, .15)), rotate(_u(rng, -.3, .3)), _flip(flip))
    c.blob(_dome(dx, yc, cw, ch, gd)); yt = yc + th * .85
    c.ellipse((0, yt), tw, th)
    if rng.random() < .6: c.ellipse((0, yt + th * .9), tw * _u(rng, .55, .8), _u(rng, .06, .09))
    obst = [(np.array([[dx - cw * .95, yc + gd - .01], [dx + cw * .95, yc + gd - .01]]), .035)]
    nlegs = {}
    for side in (-1., 1.):
        nl = _i(rng, 3, 4); d0, d1 = _u(rng, -.4, -.05), _u(rng, .4, .85); hook = _sgn(rng); placed = []
        r = _u(rng, .045, .058)
        for i in range(nl):
            f = i / (nl - 1); y = yc + .05 + (th * 1.55 - .05) * f
            xb = side * tw * .55 * math.sqrt(max(.1, 1 - ((y - yt) / th) ** 2))
            for _try in range(4):
                a = (math.pi - (d0 + (d1 - d0) * f) if side < 0 else d0 + (d1 - d0) * f) + _u(rng, -.12, .12)
                L = _u(rng, .2, .33); cu = side * hook * _u(rng, .7, 1.7) * (rng.random() < .8)
                p = _path((xb, y), a, L, turn=side * _u(rng, -.1, .4), curl=cu)
                if _gaps(p[3:], r, placed) >= GAP and _gaps(p[2:], r, obst) >= GAP * .8: break
            else: continue
            placed.append((p, r)); _limb(c, rng, p, r * 1.08, r * _u(rng, .9, 1.))
        nlegs[side] = len(placed)
    ant = rng.choice(['none', 'front', 'under'], p=[.25, .45, .3])
    if ant == 'front':
        La, cu = _u(rng, .14, .22), _u(rng, .8, 1.8)
        for side in (-1., 1.):
            a = -math.pi / 2 + side * _u(rng, .35, .7)
            _limb(c, rng, _path((dx + side * cw * .3, yc - ch * .8), a, La, curl=side * cu), R_MIN + .003)
    elif ant == 'under':
        La, cu = _u(rng, .14, .2), _u(rng, 1., 2.)
        for side in (-1., 1.):
            a = math.pi / 2 - side * _u(rng, .9, 1.3)
            p = _path((dx + side * cw * .7, yc), a, La, curl=-side * cu)
            _limb(c, rng, p, R_MIN + .004)
    re = _u(rng, .024, .033)
    for s in (-1, 1): mk.eye((dx + s * cw * _u(rng, .3, .42), yc - ch * _u(rng, .35, .55)), re)
    if rng.random() < .7:
        for s in (-1, 1): mk.line([(s * tw * .4, yt - th * .6), (s * tw * .35, yt + th * .5)], .013, pri=2)
    mk.flush()
    c.genome = dict(head_width=cw, head_height=ch, trunk_width=tw, trunk_height=th, legs_left=nlegs[-1], legs_right=nlegs[1], antennae=str(ant), flip=flip)
    return c


# ---------------------------------------------------------------- centipede / myriapod

def centipede(rng):
    """myriapod (drawing ccd026e0): a mushroom-cap head and a long body with a leg nub on each side of every
    segment, bent and waved into a curve that fits the board"""
    for _ in range(60):
        ns, p = _i(rng, 6, 11), _u(rng, .15, .19)
        br, ll, lr, hw = _u(rng, .065, .095), _u(rng, .05, .11), _u(rng, R_MIN, .048), _u(rng, .15, .22)
        back = _u(rng, -.1, .45); L = ns * p + hw * 1.3 + .1
        kap = _u(rng, -1.7, 1.7) if rng.random() < .85 else 0.
        amp = _u(rng, .0, .07) if rng.random() < .6 else 0.; fr = _u(rng, .5, 1.1) / L; ph = _u(rng, 0, TAU)
        rot = _u(rng, -.95, .95); reach = br + ll + lr
        kmax = abs(kap) + amp * (TAU * fr) ** 2
        if p * (1 - kmax * (br + ll * .5)) - 2 * lr < GAP * .85: continue                # inner legs too crowded
        warp = compose(wave(amp, fr, ph), bend(kap, 0.), rotate(rot))
        ax = warp(np.stack([np.zeros(40), np.linspace(-L / 2, L / 2, 40)], 1))
        lo, hi = ax.min(0) - reach, ax.max(0) + reach; ext = hi - lo
        if ext.max() <= 1.06 and ext.max() / ext.min() <= 3. and ax[0, 1] <= ax[:, 1].min() + .25: break
    c = Canvas(k=.02, wobble=_u(rng, .003, .006), warp=compose(warp, _shift(*(-(lo + hi) / 2)))); mk = _Marks(c)
    yh = -L / 2; ear = _u(rng, .0, .06)
    cap = catmull([(-hw, yh + hw * .75 + ear), (-hw * 1.03, yh + hw * .32), (-hw * .68, yh + hw * .04), (0, yh),
                   (hw * .68, yh + hw * .04), (hw * 1.03, yh + hw * .32), (hw, yh + hw * .75 + ear),
                   (hw * .55, yh + hw * .68), (0, yh + hw * .62), (-hw * .55, yh + hw * .68)], 64, closed=True)
    c.blob(cap)
    tailmode = rng.choice(['blob', 'cerci', 'plain'], p=[.45, .35, .2]); yt = L / 2 - br
    body = np.stack([wiggle(rng, np.zeros(30), 1.) * 0 + rng.normal(0, .004, 30), np.linspace(yh + hw * .4, yt, 30)], 1)
    _limb(c, rng, body, br, br * _u(rng, .7, .9), wig=.004)
    y0 = yh + hw * .75 + ear + .045 + GAP + lr * .5; ys = np.arange(y0, yt - br * .3, p)
    bulge = rng.random() < .45
    for y in ys:
        if bulge: c.disc((0, y - p * .15), br * 1.1)
        for s in (-1., 1.):
            a = (math.pi / 2 - s * (math.pi / 2 - back)) if s > 0 else (math.pi / 2 + (math.pi / 2 - back))
            _limb(c, rng, _path((0, y), a + _u(rng, -.06, .06), br + ll, turn=s * _u(rng, -.1, .25)), lr, lr, wig=.002)
    if tailmode == 'blob': c.disc((0, yt), br * _u(rng, 1.05, 1.3))
    elif tailmode == 'cerci':
        for s in (-1., 1.): _limb(c, rng, _path((0, yt), math.pi / 2 - s * _u(rng, .4, .7), _u(rng, .1, .16)), R_MIN + .002)
    re = _u(rng, .022, .03)
    for s in (-1, 1): mk.eye((s * hw * _u(rng, .32, .45), yh + hw * _u(rng, .3, .42)), re)
    if br > .078:
        for y in ys[1::2][:3]: mk.line([(-br + .05, y + p * .5), (br - .05, y + p * .5)], .012, pri=2)
    mk.flush()
    c.genome = dict(segments=len(ys), pitch=p, body=br, leg=ll, head=hw, bend=kap, wave=amp, turn=rot, tail=str(tailmode))
    return c


# ---------------------------------------------------------------- lobster / crayfish

def lobster(rng):
    """lobster or crayfish (drawing b4f54f5a): a dome head shield, legs along a segmented trunk (the last pair often
    curling in), a tail fan that may curl to one side, and claws in front"""
    c = Canvas(k=.022, wobble=_u(rng, .003, .006)); mk = _Marks(c)
    claws = rng.random() < .75; kind = rng.choice(['open', 'closed', 'crab'], p=[.45, .35, .2])
    cw, ch = _u(rng, .22, .4), _u(rng, .2, .3) * (.85 if claws else 1.)
    nl = _i(rng, 2, 4); lp = _u(rng, .16, .2); ap = _u(rng, .064, .078)
    tl = _u(rng, .1, .15); clh = _u(rng, .18, .26) if claws else 0.
    tr = max(nl * lp + .02, _i(rng, 4, 6) * ap); na = max(3, int(round(tr / ap)))
    H = clh + ch + na * ap + tl + .04; yc = -H / 2 + clh + ch
    flip = _sgn(rng); base = compose(rotate(_u(rng, -.15, .15)), _flip(flip)); c.warp = base
    c.blob(_dome(0, yc, cw, ch, _u(rng, .0, .07)))
    if rng.random() < .45: _limb(c, rng, _path((0, yc - ch + .05), -math.pi / 2 + _u(rng, -.1, .1), _u(rng, .08, .13)), .048, R_MIN)
    aw0 = min(cw * .62, _u(rng, .1, .17)); aw1 = _u(rng, .04, .08); dr = _u(rng, -.01, .03)
    kap = _u(rng, -1.4, 1.4) if rng.random() < .55 else 0.
    c.warp = compose(bend(kap, yc), base)
    ys = yc + ap * (np.arange(na) + .5); trunk = []
    for k, y in enumerate(ys):
        w = aw0 + (aw1 - aw0) * k / max(na - 1, 1); rho = max(R_MIN + .004, ap * _u(rng, .62, .72))
        seg = catmull([(-w, y + dr), (0, y), (w, y + dr)], 8); c.stroke(seg, rho); trunk.append((seg, rho))
    c.stroke([(0, yc), (0, ys[-1])], max(R_MIN + .01, aw1 + .02))
    yf = ys[-1] + ap * .3
    _fan(c, rng, (0, yf), math.pi / 2, _i(rng, 3, 5), _u(rng, 1.3, 2.1), tl, _u(rng, .045, .056))
    legs = {}
    for side in (-1., 1.):
        a0, a1 = _u(rng, -.15, .2), _u(rng, .55, .95); r = _u(rng, .044, .052); placed = []; curl_last = rng.random() < .5
        for i in range(nl):
            f = i / max(nl - 1, 1); y = yc + .03 + lp * i; w = aw0 + (aw1 - aw0) * (y - yc) / (na * ap)
            a = a0 + (a1 - a0) * f + _u(rng, -.08, .08); L = w + _u(rng, .17, .27)
            cu = _u(rng, 1.2, 2.2) if (curl_last and i == nl - 1) else _u(rng, -.3, .4)
            for _try in range(4):
                p = _path((side * w * .3, y), math.pi - a if side < 0 else a, L, turn=-side * _u(rng, -.25, .1) if side < 0 else _u(rng, -.1, .25), curl=side * cu)
                if _gaps(p[3:], r, placed) >= GAP and _gaps(p[int(len(p) * .5):], r, trunk) >= GAP * .9: break
                cu *= .6; L *= .9
            else: continue
            placed.append((p, r)); _limb(c, rng, p, r * 1.1, r)
        legs[side] = len(placed)
    c.warp = base
    if claws:
        arm, palm, rcl = _u(rng, .12, .2), _u(rng, .15, .22), _u(rng, .044, .05)
        for side in (-1., 1.):
            a = -math.pi / 2 + side * _u(rng, .35, .75)
            _claw(c, rng, mk, (side * cw * .55, yc - ch * .55), a, arm, palm, rcl, side, kind)
    re = _u(rng, .024, .032)
    for s in (-1, 1):
        if rng.random() < .5: mk.eye((s * cw * _u(rng, .28, .4), yc - ch * _u(rng, .45, .6)), re)
        else: mk.line([(s * cw * .22, yc - ch * .55), (s * cw * .42, yc - ch * .52)], .016, pri=0)
    c.warp = compose(bend(kap, yc), base)
    for y in ys[1:4]: mk.line([(-aw0 * .45, y - ap * .5), (aw0 * .45, y - ap * .5)], .012, pri=3)
    mk.flush()
    c.genome = dict(head_width=cw, head_height=ch, claws=str(kind) if claws else 'none', legs=nl, segments=na, curl=kap, flip=flip)
    return c


# ---------------------------------------------------------------- eurypterid

def eurypterid(rng):
    """sea scorpion: a rounded prosoma with eyes, a pair of broad swimming paddles, short walking legs, a broad
    segmented preabdomen, a narrow postabdomen and a telson spine (or paddle, or fork)"""
    c = Canvas(k=.022, wobble=_u(rng, .003, .006)); mk = _Marks(c)
    pw, ph, pe = _u(rng, .14, .2), _u(rng, .15, .22), _u(rng, .55, .9)
    m1, p1, W = _i(rng, 5, 7), _u(rng, .056, .07), _u(rng, .13, .21)
    m2, p2, W2 = _i(rng, 3, 5), _u(rng, .05, .065), _u(rng, .0, .04)
    tel = rng.choice(['spine', 'spine', 'paddle', 'fork'])
    tl = _u(rng, .14, .26) if tel == 'spine' else _u(rng, .1, .15)
    L = ph + m1 * p1 + m2 * p2 + tl; y0 = -L / 2
    flip = _sgn(rng); base = compose(rotate(_u(rng, -.18, .18)), _flip(flip)); c.warp = base
    c.blob(_superellipse((0, y0 + ph / 2), pw, ph / 2, pe))
    kap = _u(rng, -.9, .9) if rng.random() < .5 else 0.; body = compose(bend(kap, y0 + ph), base)
    c.warp = body; segs = []
    for k in range(m1):
        y = y0 + ph + p1 * (k + .35); w = W * (.75 + .35 * math.sin(math.pi * (k + .5) / m1)) * (1 - .25 * k / m1)
        rho = max(R_MIN + .004, p1 * _u(rng, .62, .74)); c.stroke([(-w, y), (w, y)], rho); segs.append((np.array([[-w, y], [w, y]]), rho))
    yp = y0 + ph + p1 * m1
    for k in range(m2):
        y = yp + p2 * (k + .5); rho = max(R_MIN + .008, _u(rng, .048, .06)) * (1 - .08 * k / m2)
        c.stroke([(-W2, y), (W2, y)], rho)
    yt = yp + p2 * m2
    if tel == 'spine': _limb(c, rng, _path((0, yt - .02), math.pi / 2 + _u(rng, -.15, .15), tl, turn=_u(rng, -.3, .3)), .052, R_MIN)
    elif tel == 'paddle':
        c.ellipse((0, yt + tl * .45), _u(rng, .06, .09), tl * .55 + .02); mk.line([(0, yt + .02), (0, yt + tl * .7)], .012, pri=3)
    else:
        for s in (-1., 1.): _limb(c, rng, _path((0, yt - .02), math.pi / 2 - s * _u(rng, .3, .5), tl), .048, R_MIN)
    c.warp = base
    # swimming paddles from the back corners of the prosoma, clear of the preabdomen
    Lp, prx, pry, rp = _u(rng, .16, .28), _u(rng, .075, .1), _u(rng, .048, .065), _u(rng, R_MIN, .05)
    droop = _u(rng, -.1, .5)
    for _try in range(6):
        p = _path((-pw * .7, y0 + ph * .72), math.pi - droop, Lp, turn=_u(rng, -.3, .25))
        a = _ang(p[-1] - p[-2]); pc = p[-1] + _dir(a) * prx * .7
        pad = np.vstack([p, pc + _dir(a) * prx * .5])
        if _gaps(pad[2:], max(rp, pry), [(np.array(s_) * [1, 1], r_) for s_, r_ in segs]) >= GAP: break
        droop -= .15
    for s in (-1., 1.):
        q = p * [s * -1, 1]; ac = _ang(q[-1] - q[-2]); qc = q[-1] + _dir(ac) * prx * .7
        _limb(c, rng, q, rp * 1.1, rp); c.ellipse(qc, prx, pry, ac)
    # short walking legs on the front half of the prosoma
    nw = _i(rng, 0, 2); lw = _u(rng, .08, .14)
    for s in (-1., 1.):
        placed = [(p * [s * -1, 1], rp)]
        for j in range(nw):
            a = -math.pi / 2 - _u(rng, .5, .9) + j * _u(rng, .45, .65); a = a if s < 0 else math.pi - a
            a = math.pi + a if False else a
            q = _path((s * pw * .55, y0 + ph * (.25 + .22 * j)), -math.pi / 2 + s * (math.pi / 2 - .9 + j * .55) if False else (math.pi + (.6 - .5 * j) if s < 0 else -(.6 - .5 * j)), pw * .45 + lw)
            if _gaps(q[3:], R_MIN, placed) < GAP: continue
            placed.append((q, R_MIN)); _limb(c, rng, q, R_MIN + .004, R_MIN)
    re = _u(rng, .022, .03)
    for s in (-1, 1): mk.eye((s * pw * _u(rng, .38, .55), y0 + ph * _u(rng, .3, .45)), re)
    c.warp = body
    for k in rng.permutation(np.arange(1, m1))[:3]:
        y = y0 + ph + p1 * k - .005; w = W * .5; mk.line([(-w, y), (w, y)], .012, pri=2)
    mk.flush()
    c.genome = dict(prosoma=pw, segments=m1 + m2, width=W, telson=str(tel), paddle=Lp, bend=kap, flip=flip)
    return c


# ---------------------------------------------------------------- horseshoe crab

def horseshoe(rng):
    """horseshoe crab: a horseshoe dome with compound eyes and genal spines, a hinged opisthosoma with marginal
    spines, and a long telson"""
    c = Canvas(k=.02, wobble=_u(rng, .003, .005)); mk = _Marks(c)
    Wd, Hd = _u(rng, .36, .46), _u(rng, .24, .34)
    Wo = _u(rng, .16, max(.17, .88 * Wd - .16)); Ho, Wb = _u(rng, .17, .25), _u(rng, .06, .11)
    gl, Lt = _u(rng, .05, .16), _u(rng, .28, .48)
    L = Hd + Ho + Lt; yd = -L / 2 + Hd
    flip = _sgn(rng); c.warp = compose(rotate(_u(rng, -.15, .15)), _flip(flip))
    t = np.linspace(math.pi, TAU, 48)
    dome = np.vstack([np.stack([Wd * np.cos(t), yd + Hd * np.sin(t)], 1), [(Wd * .8, yd + .02), (0, yd + .035), (-Wd * .8, yd + .02)]])
    c.blob(dome)
    gen = []
    for s in (-1., 1.):
        g = np.array([(s * Wd * .93, yd - .07), (s * Wd * .9, yd + gl * .5), (s * (Wd * .88 - .02), yd + gl)])
        c.stroke(catmull(g, 10), np.linspace(.055, .046, 10)); gen.append(g)
    ob = catmull([(-Wo, yd - .01), (0, yd - .02), (Wo, yd - .01), (Wo * .85, yd + Ho * .5), (Wb + .03, yd + Ho),
                  (0, yd + Ho + .01), (-Wb - .03, yd + Ho), (-Wo * .85, yd + Ho * .5)], 48, closed=True)
    c.blob(ob)
    ns = _i(rng, 0, 3)
    for s in (-1., 1.):
        for j in range(ns):
            f = (j + .7) / (ns + .4); e = np.array([s * (Wo + (Wb + .03 - Wo) * f), yd + Ho * f])
            nrm = _ang(np.array([s * Ho, -(Wb + .03 - Wo) * s * s])) + s * .4
            _limb(c, rng, _path(e - _dir(nrm) * .03, nrm, _u(rng, .07, .1)), R_MIN, R_MIN, wig=0)
    a = math.pi / 2 + _u(rng, -.25, .25)
    _limb(c, rng, _path((0, yd + Ho - .05), a, Lt, turn=_u(rng, -.3, .3)), .05, R_MIN, wig=.003)
    for s in (-1, 1):
        ex, ey = s * Wd * _u(rng, .42, .56), yd - Hd * _u(rng, .35, .55)
        mk.line(catmull([(ex - .025, ey + .012), (ex, ey - .008), (ex + .025, ey + .012)], 8), .016, pri=0)
    mk.line([(0, yd - Hd * .72), (0, yd - .06)], .013, pri=1)
    if rng.random() < .6:
        for s in (-1, 1): mk.line(catmull([(s * Wd * .25, yd - Hd * .62), (s * Wd * .33, yd - Hd * .3), (s * Wd * .3, yd - .06)], 10), .012, pri=2)
    mk.line([(0, yd + .05), (0, yd + Ho - .07)], .012, pri=3)
    mk.flush()
    c.genome = dict(width=Wd, height=Hd, abdomen=Wo, spines=ns, telson=Lt, flip=flip)
    return c


# ---------------------------------------------------------------- anomalocaris

def anomalocaris(rng):
    """anomalocaridid: an oval body with a row of overlapping swim flaps down each side, two curled frontal grasping
    appendages, eyes (often stalked) and a tail fan"""
    c = Canvas(k=.02, wobble=_u(rng, .003, .006)); mk = _Marks(c)
    bw, bl = _u(rng, .11, .16), _u(rng, .26, .34); yb = _u(rng, .04, .1)
    flip = _sgn(rng); c.warp = compose(bend(_u(rng, -.5, .5), yb), rotate(_u(rng, -.15, .15)), _flip(flip))
    c.ellipse((0, yb), bw, bl); yh = yb - bl * .85
    c.ellipse((0, yh), bw * _u(rng, .8, 1.), _u(rng, .08, .11))
    F, sw = _u(rng, .09, .17), _u(rng, .2, .55)
    span = bl * 1.55; nf = int(np.clip(span / _u(rng, .062, .08), 5, 10))
    for i in range(nf):
        f = (i + .5) / nf; y = yb - bl * .7 + span * f
        fl = F * (.45 + .55 * math.sin(math.pi * f) ** .6)
        xb = bw * .85 * math.sqrt(max(.05, 1 - ((y - yb) / bl) ** 2))
        for s in (-1., 1.):
            a = math.pi - sw if s < 0 else sw
            ctr = np.array([s * xb, y]) + _dir(a) * fl * .55
            c.ellipse(ctr, fl * .55 + .03, _u(rng, .045, .056), a)
    La, cu = _u(rng, .24, .36), _u(rng, 1.4, 2.6) * _sgn(rng)
    arms = []
    for s in (-1., 1.):
        a = -math.pi / 2 + s * _u(rng, .3, .65)
        for _try in range(5):
            p = _path((s * bw * .35, yh - .06), a, La, turn=s * _u(rng, -.2, .5), curl=-s * cu)
            if _gaps(p[2:], .05, arms) >= GAP: break
            cu *= .7
        arms.append((p, .05)); _limb(c, rng, p, _u(rng, .055, .065), R_MIN)
        if rng.random() < .5:
            for j in range(_i(rng, 2, 3)):
                q, d = _along(p, .35 + .2 * j); nrm = _ang(d[0]) - s * math.pi / 2 * np.sign(cu)
                _limb(c, rng, _path(q[0], nrm, _u(rng, .07, .09)), R_MIN, R_MIN, wig=0)
    if rng.random() < .6:
        for s in (-1., 1.):
            p = _path((s * bw * .6, yh + .01), (math.pi + .25 if s < 0 else -.25) + _u(rng, -.15, .15), _u(rng, .1, .15))
            _limb(c, rng, p, R_MIN, R_MIN); er = _u(rng, .055, .07); c.disc(p[-1], er); mk.eye(p[-1], er * .42)
    else:
        for s in (-1, 1): mk.eye((s * bw * .45, yh), _u(rng, .022, .03))
    nt = _i(rng, 1, 3); ty = yb + bl * .92
    for j in range(nt):
        for s in (-1., 1.):
            a = math.pi / 2 - s * (.35 + .55 * j) + _u(rng, -.08, .08)
            _limb(c, rng, _path((0, ty), a, _u(rng, .12, .18) * (1 - .15 * j)), R_MIN + .006, R_MIN)
    mk.line([(0, yb - bl * .5), (0, yb + bl * .6)], .013, pri=2)
    mk.flush()
    c.genome = dict(body_width=bw, body_length=bl, flaps=nf, flap=F, sweep=sw, arm=La, curl=cu, tail=nt, flip=flip)
    return c


# ---------------------------------------------------------------- marrella

def marrella(rng):
    """marrellomorph: a small head shield with two pairs of long swept-back horns, antennae, and a short body with a
    scalloped fringe of legs"""
    c = Canvas(k=.02, wobble=_u(rng, .003, .005)); mk = _Marks(c)
    hs, yh = _u(rng, .08, .12), _u(rng, -.3, -.22)
    flip = _sgn(rng); c.warp = compose(rotate(_u(rng, -.2, .2)), _flip(flip))
    c.blob(catmull([(0, yh - hs * 1.3), (hs, yh - hs * .2), (hs * .7, yh + hs * .6), (0, yh + hs * .8),
                    (-hs * .7, yh + hs * .6), (-hs, yh - hs * .2)], 36, closed=True))
    BL = _u(rng, .45, .62); br0, br1 = _u(rng, .06, .08), _u(rng, .045, .055)
    body = np.stack([np.zeros(20), np.linspace(yh, yh + BL, 20)], 1)
    _limb(c, rng, body, br0, br1); parts = [(body, br0 + .03)]
    nb = int(BL / _u(rng, .075, .09)); fringe = rng.random() < .6
    if fringe:
        for j in range(nb):
            y = yh + hs + (BL - hs) * (j + .5) / nb; c.stroke([(-br0 * .5, y), (br0 * .5, y)], br0 + _u(rng, .015, .03))
    LA, LB = _u(rng, .3, .44), _u(rng, .42, .62)
    tA, aB = _u(rng, .8, 1.6), _u(rng, .25, .6)
    for _try in range(6):
        hornB = _path((hs * .4, yh + hs * .2), math.pi / 2 - aB, LB, turn=_u(rng, -.25, .15))
        if _gaps(hornB[4:], .048, parts) >= GAP: break
        aB += .1
    hornA = _path((hs * .6, yh - hs * .3), _u(rng, -.45, -.1), LA, turn=tA)
    for _try in range(5):
        if _gaps(hornA[5:], .05, [(hornB, .048)]) >= GAP: break
        tA *= .8; hornA = _path((hs * .6, yh - hs * .3), _u(rng, -.45, -.1), LA, turn=tA)
    ant_a, La = -math.pi / 2 + _u(rng, .3, .75), _u(rng, .18, .3)
    ant = _path((hs * .3, yh - hs * 1.05), ant_a, La, turn=_u(rng, .4, 1.1))
    for s in (-1., 1.):
        m = np.array([s, 1.])
        _limb(c, rng, hornA * m, .052, R_MIN); _limb(c, rng, hornB * m, .05, R_MIN); _limb(c, rng, ant * m, R_MIN + .002, R_MIN)
    c.disc((0, yh + BL + .01), _u(rng, .045, .06))
    for s in (-1, 1): mk.eye((s * hs * .42, yh - hs * .2), _u(rng, .02, .026))
    mk.line([(0, yh + hs * 1.2), (0, yh + BL - .05)], .012, pri=2)
    mk.flush()
    c.genome = dict(shield=hs, body=BL, hornA=LA, hornB=LB, splayA=tA, splayB=aB, fringe=bool(fringe), flip=flip)
    return c


# ---------------------------------------------------------------- shrimp / prawn

def shrimp(rng):
    """shrimp or prawn in side view: a carapace with a rostrum, a stalked eye and long antennae, a six-segment
    abdomen curling under, a fan tail, walking legs and swimmerets"""
    c = Canvas(k=.02, wobble=_u(rng, .003, .006)); mk = _Marks(c)
    Lc, dc = _u(rng, .28, .38), _u(rng, .12, .16); ap = _u(rng, .07, .085); na = 6; tl = _u(rng, .1, .14)
    kap = _u(rng, .6, 2.)
    flip = _sgn(rng); rot = _u(rng, -.5, .25)
    # straight frame: head at y = 0, dorsal side x < 0, ventral side x > 0; centred afterwards
    yc = Lc; L = Lc + na * ap + tl
    warp0 = compose(bend(kap, yc))
    ax = warp0(np.stack([np.zeros(30), np.linspace(-.25, L, 30)], 1)); lo, hi = ax.min(0) - .18, ax.max(0) + .18
    base = compose(_shift(*(-(lo + hi) / 2)), rotate(rot), _flip(flip))
    c.warp = base
    c.ellipse((0, Lc / 2), dc, Lc / 2 + .01)
    _limb(c, rng, _path((-dc * .25, .03), -math.pi / 2 + _u(rng, -.05, .35), _u(rng, .1, .18)), .046, R_MIN)
    ep = _path((dc * .45, .07), -math.pi / 4 + _u(rng, -.2, .2), _u(rng, .05, .08)); _limb(c, rng, ep, R_MIN, R_MIN)
    er = _u(rng, .05, .062); c.disc(ep[-1], er); mk.eye(ep[-1], er * .42)
    nant = _i(rng, 1, 2); parts = []
    for j in range(nant):
        La = _u(rng, .28, .42); tt = -_u(rng, 1.1, 2.)
        for _try in range(5):
            p = _path((dc * .6, .05 + .03 * j), -math.pi / 2 + _u(rng, .25, .6), La, turn=tt)
            if _gaps(p[4:], R_MIN, parts + [(np.array([[0, .1], [0, Lc]]), dc)]) >= GAP: break
            tt *= .75; La *= .9
        parts.append((p, R_MIN)); _limb(c, rng, p, R_MIN + .002, R_MIN)
    nleg = _i(rng, 2, 4); placed = []
    for j in range(nleg):
        a = _u(rng, -.1, .25) + .32 * j
        p = _path((dc * .55, Lc * (.4 + .5 * j / max(nleg, 1))), a, _u(rng, .14, .22), turn=_u(rng, 0, .4))
        if _gaps(p[3:], R_MIN, placed) < GAP: continue
        placed.append((p, R_MIN)); _limb(c, rng, p, R_MIN + .003, R_MIN)
    c.warp = compose(bend(kap, yc), base)
    for k in range(na):
        y = yc + ap * (k + .5); d = dc * (.95 - .45 * k / na); rho = max(R_MIN + .004, ap * _u(rng, .6, .7))
        c.stroke([(-d + rho, y), (d - rho * .6, y + .01)], rho)
        if k % 2 == 1 and rng.random() < .6:
            _limb(c, rng, _path((d * .5, y), _u(rng, .1, .5), d * .5 + _u(rng, .05, .08)), R_MIN, R_MIN, wig=0)
    c.stroke([(0, yc - .02), (0, yc + na * ap)], max(R_MIN + .01, dc * .5))
    _fan(c, rng, (0, yc + na * ap), math.pi / 2, 3, _u(rng, 1., 1.5), tl, _u(rng, .044, .052))
    c.warp = base
    mk.line(catmull([(-dc * .45, Lc * .2), (-dc * .3, Lc * .55), (-dc * .45, Lc * .85)], 8), .012, pri=2)
    mk.flush()
    c.genome = dict(carapace=Lc, depth=dc, curl=kap, antennae=nant, legs=len(placed), turn=rot, flip=flip)
    return c


# ---------------------------------------------------------------- ostracod

def ostracod(rng):
    """ostracod: a bean-shaped bivalved shell in side view with an eye spot, a muscle scar and ribs or pits, and a few
    small appendages poking out of the ventral gape"""
    c = Canvas(k=.02, wobble=_u(rng, .003, .005)); mk = _Marks(c)
    a, b = _u(rng, .38, .46), _u(rng, .2, .29); et, hp, vn = _u(rng, .55, 1.), _u(rng, -.15, .2), _u(rng, .0, .045)
    t = np.linspace(0, TAU, 96, endpoint=False); x = a * np.cos(t); s = np.sin(t)
    y = np.where(s < 0, -b * np.abs(s) ** et, b * s) * (1 + hp * x / a) - vn * np.exp(-((x + a * .1) / (a * .35)) ** 2) * (s > 0)
    shell = np.stack([x, y], 1)
    flip = _sgn(rng); c.warp = compose(rotate(_u(rng, -.25, .25)), _flip(flip))
    c.blob(shell)

    def bottom(xq): return float(np.interp(xq, x[s > 0][::-1], y[s > 0][::-1]))
    extra = rng.choice(['none', 'spine', 'alae'], p=[.6, .22, .18])
    if extra == 'spine': _limb(c, rng, _path((a * .8, -b * .2 * (1 + hp)), _u(rng, -.6, .3), _u(rng, .12, .18)), .05, R_MIN)
    elif extra == 'alae': c.stroke(catmull([(-a * .3, bottom(-a * .3) - .03), (a * .3, bottom(a * .3) + .02), (a * .85, bottom(a * .7) + .07)], 12), np.linspace(.045, R_MIN, 12))
    napp = _i(rng, 0, 3); placed = []
    if rng.random() < .65:                                          # antenna with a setal brush from the front
        p = _path((-a * .8, bottom(-a * .8) - .06), math.pi - _u(rng, -.6, -.1), _u(rng, .12, .2), turn=-_u(rng, 0, .6))
        _limb(c, rng, p, R_MIN + .003, R_MIN); c.disc(p[-1], _u(rng, .05, .062)); placed.append((p, .06))
    xs = np.sort(rng.uniform(-a * .45, a * .55, napp))
    for xq in xs:
        p = _path((xq, bottom(xq) - .05), math.pi / 2 + _u(rng, -.5, .4), _u(rng, .1, .17), turn=_u(rng, -.5, .5))
        if _gaps(p[3:], R_MIN, placed) < GAP: continue
        placed.append((p, R_MIN)); _limb(c, rng, p, R_MIN + .004, R_MIN)
    mk.eye((-a * _u(rng, .45, .6), -b * _u(rng, .3, .5)), _u(rng, .022, .032))
    mk.blob(ellipse_pts_((a * _u(rng, -.1, .1), b * _u(rng, -.1, .15)), _u(rng, .03, .045), _u(rng, .022, .03)), pri=1)
    orn = rng.choice(['ribs', 'pits', 'hinge'])
    if orn == 'ribs':
        for f in (.55, .75)[:_i(rng, 1, 2)]:
            tt = np.linspace(math.pi * 1.1, math.pi * 1.9, 12); mk.line(np.stack([a * f * np.cos(tt), b * f * np.sin(tt) * 1.1], 1), .013, pri=2)
    elif orn == 'pits':
        for _ in range(4): mk.eye((rng.uniform(-a * .5, a * .55), rng.uniform(-b * .5, b * .45)), _u(rng, .018, .024), pri=2)
    else: mk.line(catmull([(-a * .6, -b * .62), (0, -b * .78), (a * .6, -b * .62)], 12), .013, pri=2)
    mk.flush()
    c.genome = dict(length=a, height=b, top=et, posterior=hp, notch=vn, extra=str(extra), appendages=len(placed), ornament=str(orn), flip=flip)
    return c


def ellipse_pts_(ctr, rx, ry, ang=0., n=24):
    t = np.linspace(0, TAU, n, endpoint=False); ca, sa = math.cos(ang), math.sin(ang); x, y = rx * np.cos(t), ry * np.sin(t)
    return np.stack([ctr[0] + ca * x - sa * y, ctr[1] + sa * x + ca * y], 1)


# ---------------------------------------------------------------- free grammar

def free(rng):
    """a free recombination of this module's parts: a head (dome, hood, horseshoe, wedge with horns, small shield),
    a trunk (segment chain, oval, long stroke), limbs (legs, curled legs, paddles, flaps, nubs, claws) and a tail
    (fan, spine, paddle, cerci, knob)"""
    c = Canvas(k=.021, wobble=_u(rng, .003, .006)); mk = _Marks(c)
    head = rng.choice(['dome', 'hood', 'horseshoe', 'wedge', 'shield'])
    trunk = rng.choice(['chain', 'oval', 'stroke'])
    limbs = rng.choice(['legs', 'curled', 'paddles', 'flaps', 'nubs', 'claws'])
    tail = rng.choice(['fan', 'spine', 'paddle', 'cerci', 'knob'])
    hw, hh = _u(rng, .16, .38), _u(rng, .14, .28)
    TL = _u(rng, .4, .62); yh = -.5 + hh
    flip = _sgn(rng); base = compose(rotate(_u(rng, -.25, .25)), _flip(flip)); c.warp = base
    if head in ('dome', 'horseshoe'): c.blob(_dome(0, yh, hw, hh, _u(rng, 0, .09) if head == 'dome' else _u(rng, .06, .14)))
    elif head == 'hood':
        c.blob(catmull([(-hw, yh + hh * .4), (-hw * 1.03, yh - hh * .2), (-hw * .6, yh - hh * .85), (hw * .2, yh - hh),
                        (hw * .9, yh - hh * .4), (hw * .8, yh + hh * .3), (0, yh + hh * .4)], 60, closed=True))
    elif head == 'wedge':
        c.blob(catmull([(0, yh - hh), (hw * .6, yh - hh * .2), (hw * .45, yh + hh * .3), (0, yh + hh * .35),
                        (-hw * .45, yh + hh * .3), (-hw * .6, yh - hh * .2)], 40, closed=True))
        for s in (-1., 1.): _limb(c, rng, _path((s * hw * .4, yh - hh * .1), (math.pi * .5 + s * _u(rng, .5, 1.)), _u(rng, .25, .45), turn=-s * _u(rng, -.3, .3)), .05, R_MIN)
    else: c.ellipse((0, yh), hw * .75, hh * .75)
    kap = _u(rng, -1.2, 1.2) if rng.random() < .5 else 0.; c.warp = compose(bend(kap, yh), base)
    y0 = yh + hh * .2; y1 = y0 + TL; tw = _u(rng, .08, .2)
    if trunk == 'chain':
        n = int(TL / _u(rng, .06, .075)); pitch = TL / n; rho = max(R_MIN + .004, pitch * .68)
        for k in range(n):
            w = tw * (1 - .55 * k / n); c.stroke([(-w, y0 + pitch * (k + .5)), (w, y0 + pitch * (k + .5))], rho)
        side_w = lambda y: tw * (1 - .55 * (y - y0) / TL) + rho
    elif trunk == 'oval':
        c.ellipse((0, (y0 + y1) / 2), tw + .04, TL / 2 + .02); side_w = lambda y: (tw + .04) * math.sqrt(max(.05, 1 - ((y - (y0 + y1) / 2) / (TL / 2 + .02)) ** 2))
    else:
        r0 = max(R_MIN + .03, tw * .7); _limb(c, rng, [(0, y0), (0, y1)], r0, r0 * .7); side_w = lambda y: r0 * (1 - .3 * (y - y0) / TL)
    placed = {-1: [], 1: []}
    if limbs in ('legs', 'curled', 'nubs'):
        n = int(np.clip(TL / _u(rng, .16, .2), 2, 5)); r = _u(rng, R_MIN, .052)
        L = _u(rng, .06, .1) if limbs == 'nubs' else _u(rng, .16, .3)
        a0, a1 = _u(rng, -.3, .2), _u(rng, .3, .8)
        for i in range(n):
            f = i / max(n - 1, 1); y = y0 + TL * (.12 + .8 * f); w = side_w(y)
            for s in (-1., 1.):
                a = a0 + (a1 - a0) * f; a = math.pi - a if s < 0 else a
                cu = s * _u(rng, .8, 1.8) if limbs == 'curled' else 0.
                p = _path((s * w * .5, y), a, w * .5 + L, turn=s * _u(rng, 0, .3), curl=cu)
                if _gaps(p[2:], r, placed[s]) < GAP: continue
                placed[s].append((p, r)); _limb(c, rng, p, r * 1.06, r)
    elif limbs == 'flaps':
        n = int(np.clip(TL / _u(rng, .065, .085), 4, 9)); F = _u(rng, .08, .15); sw = _u(rng, .2, .5)
        for i in range(n):
            f = (i + .5) / n; y = y0 + TL * f; w = side_w(y); fl = F * (.5 + .5 * math.sin(math.pi * f))
            for s in (-1., 1.):
                a = math.pi - sw if s < 0 else sw; c.ellipse(np.array([s * w * .8, y]) + _dir(a) * fl * .55, fl * .55 + .03, .05, a)
    elif limbs == 'paddles':
        Lp = _u(rng, .16, .26)
        for s in (-1., 1.):
            a = math.pi - _u(rng, -.1, .4) if s < 0 else _u(rng, -.1, .4)
            p = _path((s * side_w(y0 + .05) * .6, y0 + .03), a, Lp, turn=-s * _u(rng, -.2, .3)); _limb(c, rng, p, .05, R_MIN + .004)
            ap_ = _ang(p[-1] - p[-2]); c.ellipse(p[-1] + _dir(ap_) * .06, _u(rng, .075, .1), _u(rng, .048, .062), ap_)
    else:
        arm, palm = _u(rng, .1, .18), _u(rng, .14, .2); kind = rng.choice(['open', 'closed', 'crab'])
        c.warp = base
        for s in (-1., 1.): _claw(c, rng, mk, (s * hw * .55, yh - hh * .4), -math.pi / 2 + s * _u(rng, .4, .8), arm, palm, .046, s, kind)
        c.warp = compose(bend(kap, yh), base)
    if tail == 'fan': _fan(c, rng, (0, y1), math.pi / 2, _i(rng, 3, 5), _u(rng, 1.2, 2.), _u(rng, .1, .14), _u(rng, .045, .055))
    elif tail == 'spine': _limb(c, rng, _path((0, y1 - .02), math.pi / 2 + _u(rng, -.2, .2), _u(rng, .15, .3), turn=_u(rng, -.3, .3)), .052, R_MIN)
    elif tail == 'paddle': c.ellipse((0, y1 + .06), _u(rng, .06, .09), _u(rng, .08, .12))
    elif tail == 'cerci':
        for s in (-1., 1.): _limb(c, rng, _path((0, y1 - .02), math.pi / 2 - s * _u(rng, .35, .6), _u(rng, .1, .16)), R_MIN + .004, R_MIN)
    else: c.disc((0, y1), _u(rng, .06, .09))
    c.warp = base
    re = _u(rng, .022, .032)
    for s in (-1, 1): mk.eye((s * hw * _u(rng, .3, .45), yh - hh * _u(rng, .25, .5)), re)
    mk.flush()
    c.genome = dict(head=str(head), trunk=str(trunk), limbs=str(limbs), tail=str(tail), head_width=hw, trunk_length=TL, bend=kap, flip=flip)
    return c


PLANS = {
    'enrolled': (1., enrolled),
    'side-view': (1., side_view),
    'legged': (1., legged),
    'centipede': (1., centipede),
    'lobster': (1., lobster),
    'eurypterid': (1., eurypterid),
    'horseshoe-crab': (.9, horseshoe),
    'anomalocaris': (1., anomalocaris),
    'marrella': (.8, marrella),
    'shrimp': (.9, shrimp),
    'ostracod': (.8, ostracod),
    'free': (1.6, free),
}
