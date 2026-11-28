"""Dorsal-view trilobites for the FossilNet v3 corpus (10 of the owner's 13 drawings are trilobites).

Most plans draw with one kit of marker parts (build): heads (a cap whose rim runs down as genal spines, a bell with
hanging genal lobes, a crescent with sweeping genal spines, a phacopid bulb, a harpetid horseshoe brim), thoraxes
(stroke ribs on a thin axis, round-ended lobes on a fat axis, a fused body with a scalloped margin, swept spiny
pleurae), tails (stub, knob, plate, fan, spine, fork, lobster foot, spiny plate) and extras (Dicranurus horns, a
Walliserops trident, a nose spine), with engraved eyes (dots, dashes, crescents, compound kidneys, a sleepy face), a
glabella and axial furrows. The named plans sample those parts within one fossil type; `grammar` recombines them
freely. Shield, agnostid and enrolled creatures have bodies of their own.

Parts are laid out in a design frame and replayed into the Canvas through the plan's warps and a uniform scale that
gives the body box extent 1 (Sketch.canvas), so radii and gaps are final units: cut strokes keep radius R0 or more,
and free ends keep GAP from their neighbours (pleurae, spines and horns are cut short where they would come closer),
which keeps the 3 mm size factor low.
"""
import math

import numpy as np

from morph.core import Canvas, arc, bend, catmull, compose, densify, ellipse_pts, mirror, rotate, shear, stretch, taper, wiggle

R0 = .042          # smallest radius of a cut stroke (final units: the creature's box has extent 1)
GAP = .084         # free gap kept between neighbouring parts
FR = .014          # engraved line radius
TOP = -.5          # front of the head


def _radii(pts, r):
    n = len(np.asarray(pts, float).reshape(-1, 2)); r = np.atleast_1d(np.asarray(r, float))
    if len(r) == n: return r
    if len(r) == 1: return np.full(n, float(r[0]))
    return np.interp(np.linspace(0, 1, n), np.linspace(0, 1, len(r)), r)


class Sketch:
    """records Canvas calls in a design frame; canvas() replays them through the warp and a uniform scale that gives
    the body box extent 1 (radii are not scaled: they are final units). obst holds parts that later free ends must
    keep clear of: (points, radii, tag, closed)."""

    def __init__(self):
        self.calls, self.obst, self.nfeat = [], [], 0

    def _rec(self, kind, layer, mode, pts, r=None, k=None):
        self.calls.append((kind, layer, mode, np.asarray(pts, float).reshape(-1, 2), r, k)); return self

    def obs(self, pts, r, tag, closed=False):
        p = np.asarray(pts, float).reshape(-1, 2); self.obst.append((p, _radii(p, r), tag, closed)); return self

    def stroke(self, pts, r, k=None, tag=None):
        if tag: self.obs(pts, r, tag)
        return self._rec('stroke', 'body', 'add', pts, r, k)

    def disc(self, c, r, k=None): return self._rec('stroke', 'body', 'add', [c], r, k)
    def blob(self, pts, k=None): return self._rec('blob', 'body', 'add', pts, None, k)
    def cut(self, pts, r, k=None): return self._rec('stroke', 'body', 'cut', pts, r, k)

    def eye(self, c, r):
        self.nfeat += 1; return self._rec('stroke', 'feat', 'add', [c], r)

    def line(self, pts, r=FR):
        self.nfeat += 1; return self._rec('stroke', 'feat', 'add', pts, r)

    def mark(self, pts):
        self.nfeat += 1; return self._rec('blob', 'feat', 'add', pts)

    def room(self, n=1): return self.nfeat + n <= 6

    def canvas(self, warp=None, k=.02, wobble=.004):
        P, R = [], []
        for kind, layer, mode, pts, r, _ in self.calls:
            if layer != 'body' or mode != 'add': continue
            if kind == 'stroke': p, rr = densify(pts, values=_radii(pts, r))
            else: p = densify(pts, closed=True); rr = np.zeros(len(p))
            P.append(p if warp is None else warp(p)); R.append(rr)
        P = np.vstack(P); R = np.concatenate(R)[:, None]; s = 1.
        for _ in range(5):
            s /= float(((s * P + R).max(0) - (s * P - R).min(0)).max())
        ctr = ((s * P + R).max(0) + (s * P - R).min(0)) / 2
        c = Canvas(k=k, wobble=wobble, warp=compose(warp, lambda p: s * np.asarray(p, float) - ctr))
        for kind, layer, mode, pts, r, kk in self.calls:
            if layer == 'feat': c.feature_stroke(pts, r) if kind == 'stroke' else c.feature_blob(pts)
            elif mode == 'add': c.stroke(pts, r, kk) if kind == 'stroke' else c.blob(pts, kk)
            else: c.cut_stroke(pts, r, kk) if kind == 'stroke' else c.cut_blob(pts, kk)
        return c, s

# ---------------------------------------------------------------- geometry


def sweep(p0, ang, length, curv=0., curl=0., n=14):
    """centre line from p0 heading ang (radians from straight down, positive toward +x), turning at curv + curl * s per unit"""
    s = np.linspace(0, length, n); th = ang + curv * s + curl * s * s; m = (th[:-1] + th[1:]) / 2
    d = np.stack([np.sin(m), np.cos(m)], 1) * (length / (n - 1))
    return np.vstack([np.asarray(p0, float)[None], np.asarray(p0, float) + np.cumsum(d, 0)])


def heading(p):
    d = p[-1] - p[-2]; return math.atan2(d[0], d[1])


def dome(hw, hh, yb, e=2., n=40, t0=0., t1=math.pi):
    """superellipse arc from (hw, yb) over the top (yb - hh) to (-hw, yb)"""
    t = np.linspace(t0, t1, n); c, s = np.cos(t), np.sin(t)
    return np.stack([hw * np.sign(c) * np.abs(c) ** (2 / e), yb - hh * np.abs(s) ** (2 / e)], 1)


def post(hw, yb, cc, n=11):
    """posterior border of a head shield, rising cc in the middle, left to right"""
    x = np.linspace(-hw, hw, n); return np.stack([x, yb - cc * (1 - (x / hw) ** 2)], 1)


def shield(hw, hh, yb, e, cc):
    return np.vstack([dome(hw, hh, yb, e, 44), post(hw, yb, cc)[1:-1]])


def rounded(poly, rc):
    """round the corners of a polygon by an opening of radius rc"""
    from shapely.geometry import Polygon
    P = Polygon(poly).buffer(0)
    if rc > 0: P = P.buffer(-rc, 6).buffer(rc, 6)
    if P.is_empty: return np.asarray(poly, float)
    if P.geom_type == 'MultiPolygon': P = max(P.geoms, key=lambda q: q.area)
    return np.asarray(P.exterior.coords)[:-1]


def arclen(p):
    return np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]


def cut_at(p, r, L):
    """the polyline p (radii r) up to arc length L"""
    s = arclen(p)
    if L >= s[-1]: return p, r
    i = max(1, int(np.searchsorted(s, L))); t = (L - s[i - 1]) / max(s[i] - s[i - 1], 1e-12)
    return np.vstack([p[:i], p[i - 1] + t * (p[i] - p[i - 1])]), np.r_[r[:i], r[i - 1] + t * (r[i] - r[i - 1])]


def _inside(P, poly):
    a = poly; b = np.roll(poly, -1, 0); x, y = P[:, :1], P[:, 1:]
    cond = (a[None, :, 1] > y) != (b[None, :, 1] > y)
    xi = a[None, :, 0] + (y - a[None, :, 1]) * (b[None, :, 0] - a[None, :, 0]) / (b[None, :, 1] - a[None, :, 1] + 1e-12)
    return ((cond & (x < xi)).sum(1) % 2) == 1


def edge_gap(P, obst):
    """free distance from points P to the edges of obstacle strokes (negative inside a closed obstacle)"""
    out = np.full(len(P), np.inf)
    for Q, rq, _tag, closed in obst:
        if closed: Q = np.vstack([Q, Q[:1]]); rq = np.r_[rq, rq[:1]]
        if len(Q) == 1: d = np.linalg.norm(P - Q[0], axis=1) - rq[0]
        else:
            A, AB = Q[:-1], np.diff(Q, axis=0); L2 = (AB ** 2).sum(1) + 1e-12
            t = np.clip(np.einsum('nmk,mk->nm', P[:, None] - A[None], AB) / L2, 0, 1)
            D = np.linalg.norm(P[:, None] - (A[None] + t[..., None] * AB[None]), axis=2) - (rq[:-1][None] + t * np.diff(rq)[None])
            d = D.min(1)
            if closed: d = np.where(_inside(P, Q[:-1]), -np.abs(d) - 1., d)
        out = np.minimum(out, d)
    return out


def clip(p, r, obst, gap=GAP, start=0., after_free=False, keep=0.):
    """the stroke p (radii r) cut short where its edge comes within gap of an obstacle. Points nearer than `start`
    to its root, or (after_free) before it first gets clear, are not checked. None if `keep` or less would remain."""
    p = np.asarray(p, float).reshape(-1, 2); r = _radii(p, r)
    if not obst or len(p) < 2: return p, r
    q, rq = densify(p, .01, values=r); s = arclen(q); free = edge_gap(q, obst) - rq
    chk = s >= start
    if after_free: chk &= np.maximum.accumulate(free >= gap)
    bad = chk & (free < gap)
    if not bad.any(): return p, r
    L = s[int(np.argmax(bad))] - .006
    if L <= max(keep, .012): return None
    return cut_at(p, r, L)


def U(rng, a, b): return float(rng.uniform(a, b))


def pick(rng, opts):
    """weighted choice from {option: weight}"""
    k = list(opts); w = np.array([opts[o] for o in k], float); return k[int(rng.choice(len(k), p=w / w.sum()))]

# ---------------------------------------------------------------- engraving


def eyes(sk, rng, style, x, y, scale=1.):
    """engraved eyes at (+-x, y): dots, dashes, crescents, compound kidneys or a sleepy face (dashes and a mouth)"""
    if style == 'none' or not sk.room(2): return
    j = lambda: rng.normal(0, .004)
    if style == 'dots':
        r = U(rng, .022, .032) * scale
        for s in (1, -1): sk.eye((s * x + j(), y + j()), r)
    elif style in ('dashes', 'sleepy'):
        L = U(rng, .028, .048) * scale; tilt = U(rng, -.25, .3); w = U(rng, .012, .016)
        for s in (1, -1):
            c = np.array([s * x + j(), y + j()]); d = np.array([s * math.cos(tilt), math.sin(tilt)]) * L
            sk.line([c - d, c + d], w)
        if style == 'sleepy' and sk.room():
            m = U(rng, .022, .034) * scale; dy = U(rng, .055, .08) * scale; h = U(rng, .008, .016)
            sk.line(catmull([(-m, y + dy + h), (-m * .45, y + dy - h * .2), (m * .45, y + dy - h * .2), (m, y + dy + h)], 12), w * .85)
    elif style == 'crescents':
        R = U(rng, .028, .042) * scale; w = U(rng, .011, .015); span = U(rng, .9, 1.4); rot = U(rng, -.6, .9)
        for s in (1, -1):
            a = arc((x + R * .4, y + j()), R, rot - span, rot + span, 12)[::-1]
            sk.line(a if s > 0 else mirror(a), w)
    elif style == 'compound':
        R = U(rng, .045, .065) * scale; w = U(rng, .02, .028); span = U(rng, .55, .9); rot = U(rng, -.25, .35)
        for s in (1, -1):
            a = arc((x - R * .55, y + j()), R, rot - span, rot + span, 12)
            sk.line(a if s > 0 else mirror(a), w)


def glabella(sk, rng, front, back, gx):
    """the glabella as an engraved U, open toward the neck"""
    if not sk.room() or back - front < .1: return
    h = back - front; b = gx * U(rng, .75, 1.)
    sk.line(catmull([(-b, back), (-gx, front + h * .45), (-gx * .65, front + h * .08), (0, front), (gx * .65, front + h * .08),
                     (gx, front + h * .45), (b, back)], 22), U(rng, .011, .015))

# ---------------------------------------------------------------- the trilobite kit


def build(rng, g):
    """draw a dorsal trilobite from genome g (the samplers below fill it) and return the Canvas"""
    sk = Sketch(); hw, hh, e, cc = g['hw'], g['hh'], g['e'], g['cc']; yb = TOP + hh; head = g['head']
    # ---- head shield
    if head in ('rim', 'brim'):
        rr = g['rim_r']; ins = rr * (.55 if head == 'rim' else .15); ybc = yb + g['cap_drop']
        cap = shield(hw - ins, hh - ins, ybc, e, cc); sk.blob(cap); sk.obs(cap, 0., 'cap', True)
        neck = ybc - cc; hb = ybc
    else:
        cap = rounded(shield(hw, hh, yb, e, cc), g.get('rc', .03)); sk.blob(cap); sk.obs(cap, 0., 'cap', True)
        neck = yb - cc; hb = yb
        if head == 'bulb':
            gl = ellipse_pts((0, TOP + g['gry'] - g['bulge']), g['grx'], g['gry'], 0, 40); sk.blob(gl)
    up = g['bulge'] if head == 'bulb' else 0.
    # ---- extras on the front of the head (their height comes off the body budget)
    ex = g.get('extra', 'none')
    if ex == 'trident': up = max(up, g['tr_len'] + g['tr_rise'] + .04)
    elif ex == 'nose': up = max(up, g['nose_len'] * .9)
    elif ex == 'horns': up = max(up, g['horn_up'])
    # ---- layout of thorax and tail along the axis
    th = g['thorax']; r = g['r_seg']; tail = g['tail']
    if th == 'fused': p = g['p_fused']; y0 = neck + p * .55
    else:
        p = 2 * r + g['g_seg']
        L0 = min(g['tw'], hw * .95); y0 = hb - cc * (1 - (L0 / hw) ** 2) + r + g['g_head']
    tl = _tail_len(g, r)
    room = g.get('H', 1.) - up - (y0 - TOP) - tl
    n = int(np.clip(g['n'], g.get('n_min', 1), max(g.get('n_min', 1), 1 + math.floor(room / p))))
    g['n'] = n; ys = y0 + p * np.arange(n); f = np.arange(n) / max(n - 1, 1)
    Ws = g['tw'] * (1 - g['t_taper'] * f ** g.get('t_pow', 1.))
    yl = ys[-1]
    # ---- genal parts, rims and brims (obstacles for everything that follows)
    if head in ('rim', 'brim'):
        rr = g['rim_r']
        for s in (1, -1):
            q = dome(hw, hh, yb, e, 22, 0, math.pi / 2)[::-1]
            sp = sweep(q[-1], g['gs_ang'] + rng.normal(0, .03), g['gs_len'] * U(rng, .92, 1.08), g['gs_curv'] * U(rng, .85, 1.15), g['gs_curl'], 16)
            pts = wiggle(rng, np.vstack([q, sp[1:]]), .003); rad = np.r_[np.full(len(q), rr), np.linspace(rr, g['gs_r1'], len(sp) - 1)]
            sk.stroke(pts if s > 0 else mirror(pts), rad, tag='genal')
    elif g.get('gs_len', 0) > 0:
        r0, r1 = g['gs_r0'], g['gs_r1']
        for s in (1, -1):
            p0 = (hw - r0 * g['gs_in'], yb - r0 * .55)
            pts = sweep(p0, g['gs_ang'] + rng.normal(0, .04), g['gs_len'] * U(rng, .88, 1.12), g['gs_curv'] * U(rng, .85, 1.15),
                        g['gs_curl'] * U(rng, .8, 1.2), 16)
            pts = wiggle(rng, pts, .003); pts = pts if s > 0 else mirror(pts)
            res = clip(pts, np.linspace(r0, r1, len(pts)), [o for o in sk.obst if o[2] == 'genal'], GAP, start=.08)
            if res is not None: sk.stroke(res[0], res[1], tag='genal')
    # ---- extras
    if ex == 'horns': _horns(sk, rng, g)
    elif ex == 'trident': _trident(sk, rng, g)
    elif ex == 'nose':
        pts = sweep((0, TOP + .05), math.pi + rng.normal(0, .05), g['nose_len'], g['nose_curv'], 0, 10)
        sk.stroke(wiggle(rng, pts, .002), np.linspace(g['nose_r'], R0, len(pts)))
    # ---- axis, tail and thorax
    _thorax_tail(sk, rng, g, th, tail, neck, ys, Ws, p, r)
    # ---- engraving
    eye_y = TOP + hh * g['eye_y']; eyes(sk, rng, g['eyes'], g['eye_x'] * hw, eye_y, g.get('eye_scale', 1.))
    if g.get('glab', False):
        gx = g.get('glab_w', .08)
        glabella(sk, rng, TOP + hh * .16 + (g['bulge'] if head == 'bulb' else 0) * 0, neck - .05, gx)
    if th == 'fused' and g.get('furrows', False) and sk.room(2):
        aw = g['ax_w']
        for s in (1, -1):
            pts = np.stack([s * np.linspace(aw, aw * .7, 6), np.linspace(neck + .03, yl + p * .2, 6)], 1)
            sk.line(wiggle(rng, pts, .002), FR)
    # ---- warps
    warp = compose(taper(g['taper']), bend(g['bend']), shear(g['shear']), rotate(g['rot']), stretch(-1., 1.) if g['flip'] else None)
    c, s = sk.canvas(warp, k=g['k'], wobble=g['wobble'])
    g['scale'] = round(s, 3)
    c.genome = {a: (round(v, 4) if isinstance(v, float) else v) for a, v in g.items()}
    return c


def _tail_len(g, r):
    t = g['tail']
    if t == 'stub': return r + g.get('stub', .04)
    if t == 'knob': return r + g['g_tail'] + 2 * g['knob_r']
    if t in ('plate', 'spiny'): return r + g['g_tail'] + 2 * g['pl_ry'] + (g['ps_len'] * .8 if t == 'spiny' else 0)
    if t == 'fan': return r + g['g_tail'] + g['fan_R'] * (1 + math.sin(min(g['fan_ext'], .3))) * .9
    if t in ('spine', 'fork'): return r + g['sp_len'] * math.cos(g['sp_ang']) * .95
    if t == 'lobster': return r + g['g_tail'] + 2 * g['lob_r'] + max(g['lob_droop'], 0)
    return r


def _horns(sk, rng, g):
    """Dicranurus: thick horns rising from the glabella and curling out and down beside the head"""
    hw, rh = g['hw'], g['horn_r']
    for s in (1, -1):
        L = g['horn_len'] * U(rng, .93, 1.07)
        pts = sweep((g['horn_x'] * hw, TOP + .07), math.pi - g['horn_ang'], L, -g['horn_turn'] / L * U(rng, .9, 1.1), 0, 22)
        pts = wiggle(rng, pts, .003); pts = pts if s > 0 else mirror(pts)
        res = clip(pts, np.linspace(rh, R0, len(pts)), [o for o in sk.obst if o[2] in ('cap', 'genal', 'horn')], GAP, after_free=True)
        if res is not None: sk.stroke(res[0], res[1], tag='horn')


def _trident(sk, rng, g):
    """Walliserops: a shaft from the front of the head ending in three prongs"""
    rt = g['tr_r']; top = np.array([rng.normal(0, .01), TOP + .04 - g['tr_len']])
    sk.stroke(wiggle(rng, np.stack([np.linspace(0, top[0], 8), np.linspace(TOP + .05, top[1], 8)], 1), .002), rt)
    sk.stroke([top, top + [rng.normal(0, .01), -g['tr_mid']]], np.linspace(rt, R0, 2))
    for s in (1, -1):
        a = sweep(top, math.pi / 2 + g['tr_lift'], g['tr_arm'], 0, 0, 5)
        b = sweep(a[-1], math.pi / 2 + g['tr_lift'], g['tr_rise'] * 1.4, (math.pi / 2 - g['tr_lift']) / (g['tr_rise'] * 1.4) * 1.15, 0, 10)
        pts = np.vstack([a, b[1:]]); pts = pts if s > 0 else mirror(pts)
        sk.stroke(wiggle(rng, pts, .002), np.linspace(rt, R0, len(pts)))


def _pleura_path(rng, g, y, L, r, last):
    """one pleura from the axis at height y out to x = L (right side), with its tip shape"""
    dr = g['_droop']; arch = g['arch']
    pts = catmull([(0, y), (L * .55, y + arch * L + dr * .3), (L, y + dr)], 10)
    tip = g['tip']
    if tip == 'point': rad = np.linspace(r * 1.25, max(R0, r * .78), len(pts))
    else: rad = np.full(len(pts), r)
    if tip == 'hook' and (g.get('hook_all', False) or last):
        hl = g['hook_len']; hk = sweep(pts[-1], heading(pts), hl, -g['hook_turn'] / hl, 0, 8)
        pts = np.vstack([pts, hk[1:]]); rad = np.r_[rad, np.full(len(hk) - 1, rad[-1])]
    return pts, rad


def _thorax_tail(sk, rng, g, th, tail, neck, ys, Ws, p, r):
    n = len(ys); yl = ys[-1]
    # ---- axis (ribs, lobes, spiny) or fused body
    if th == 'fused':
        rs = g['sc_r']; prot = g['sc_prot']
        right = [(Ws[0] * 1.02, neck - .05)] + [(w, y) for w, y in zip(Ws, ys)]
        yend = yl + p * .45
        right += [(Ws[-1] * .55, yend + .01)]
        body = catmull(right + [(0, yend + .03)] + [(-x, y) for x, y in right[::-1]], 60, closed=True)
        sk.blob(body); sk.obs(body, 0., 'body', True)
        for i, (w, y) in enumerate(zip(Ws, ys)):
            for s in (1, -1):
                ri = rs * U(rng, .92, 1.08)
                sk.disc((s * (w + ri * (prot - 1) + rng.normal(0, .004)), y + rng.normal(0, .004)), ri)
        ax_end = yend; rax = g['ax_w']
    else:
        rax0 = g['ax_r']; rax1 = g.get('ax_r1', rax0); ax_end = yl + p * .5 + (_axis_ext(g) if tail != 'stub' else g.get('stub', .04))
        pts = np.stack([rng.normal(0, .006, 10) * np.linspace(0, 1, 10), np.linspace(neck - .06, ax_end, 10)], 1)
        sk.stroke(wiggle(rng, pts, .003), np.linspace(rax0, rax1, 10))
        rax = rax0
    # ---- tail
    y_t = yl + r + g.get('g_tail', GAP)
    _tail(sk, rng, g, tail, y_t, ax_end, Ws[-1], r, rax)
    # ---- pleurae
    if th in ('ribs', 'lobes', 'spiny'):
        obst = [o for o in sk.obst if o[2] in ('cap', 'genal', 'tail', 'horn')]
        gpp = min(GAP, (p - 2 * r) * .9); prev = {1: None, -1: None}
        last_short = g.get('last_short', 1.)
        for i, (y, W) in enumerate(zip(ys, Ws)):
            fi = i / max(n - 1, 1); g['_droop'] = g['droop0'] + (g['droop1'] - g['droop0']) * fi
            Wi = W * (last_short if (i == n - 1 and n > 2) else 1.)
            for s in (1, -1):
                pts, rad = _pleura_path(rng, g, y + rng.normal(0, .004), Wi * U(rng, .94, 1.06), r, i >= n - 2)
                pts = wiggle(rng, pts, .003); pts = pts if s > 0 else mirror(pts)
                start = rax * 1.05 + .01
                res = clip(pts, rad, obst, GAP, start=start, keep=start + r * .5)
                if res is not None and prev[s] is not None: res = clip(res[0], res[1], [prev[s]], gpp, start=start, keep=start + r * .5)
                if res is None: continue
                sk.stroke(res[0], res[1]); prev[s] = (res[0], res[1], 'pleura', False); sk.obs(res[0], res[1], 'pleura')
        g.pop('_droop', None)
    # ---- macropleural spine (olenellids): one long pleural spine swept back
    if g.get('macro', 0) and n > 2:
        i = min(n - 1, int(g['macro_i'])); y = ys[i]
        for s in (1, -1):
            pts = sweep((Ws[i] * .5, y), math.pi / 2 - g['macro_ang'], g['macro_len'] * U(rng, .92, 1.08), -g['macro_curv'], 0, 14)
            pts = wiggle(rng, pts, .003); pts = pts if s > 0 else mirror(pts)
            obst = [o for o in sk.obst if o[2] in ('genal', 'tail', 'tspine')]
            res = clip(pts, np.linspace(g['macro_r'], R0, len(pts)), obst, GAP, start=Ws[i] * .6, keep=Ws[i] * .6 + .05)
            if res is not None: sk.stroke(res[0], res[1], tag='tspine')
    # ---- tail spines last: they keep clear of everything drawn
    if tail in ('spine', 'fork'):
        obst = [o for o in sk.obst if o[2] in ('genal', 'pleura', 'horn', 'tspine')]
        angs = [g['sp_ang']] if tail == 'spine' else [g['sp_ang'], -g['sp_ang']]
        for a in angs:
            a = a + rng.normal(0, .04); L = g['sp_len'] * U(rng, .92, 1.08)
            cv = g['sp_curv'] * (1 if tail == 'spine' else np.sign(a))
            pts = sweep((0, ax_end - .02), a, L, cv, 0, 16)
            res = clip(wiggle(rng, pts, .003), np.linspace(g['sp_r'], R0, 16), obst, GAP, start=.1, keep=.12)
            if res is not None: sk.stroke(res[0], res[1], tag='tspine')


def _axis_ext(g):
    t = g['tail']
    if t in ('plate', 'spiny'): return g['g_tail'] + g['pl_ry']
    if t == 'fan': return g['g_tail'] + g['fan_R'] * .3
    if t == 'knob': return g['g_tail'] + g['knob_r']
    if t == 'lobster': return g['g_tail'] + g['lob_r']
    return .02


def _tail(sk, rng, g, tail, y_t, ax_end, w_last, r, rax):
    if tail == 'knob':
        sk.disc((rng.normal(0, .005), y_t + g['knob_r']), g['knob_r'])
    elif tail in ('plate', 'spiny'):
        rx, ry = g['pl_rx'], g['pl_ry']; c = np.array([0., y_t + ry])
        t = np.linspace(0, 2 * np.pi, 48, endpoint=False); pw = g.get('pl_point', 0.)
        yy = ry * np.sin(t) * (1 + pw * np.clip(np.sin(t), 0, 1) ** 3)          # optionally pointed at the back
        poly = np.stack([rx * np.cos(t), yy], 1) + c
        sk.blob(poly); sk.obs(poly, 0., 'tail', True)
        nsc = g.get('pl_sc', 0)
        for i in range(nsc):                                                   # scalloped margin
            a = math.pi * (i + .5) / nsc; rs = g['pl_sc_r']
            q = c + [(rx - rs * .5) * math.cos(a), (ry - rs * .5) * math.sin(a)]
            sk.disc(q, rs)
        if tail == 'spiny':
            ns = g['ps_n']
            for i in range(ns):
                a = math.pi / 2 + (i - (ns - 1) / 2) * g['ps_gap']; u = np.array([math.cos(a), math.sin(a)])
                p0 = c + u * [rx * .6, ry * .6]; ang = math.atan2(u[0], u[1])
                pts = sweep(p0, ang, g['ps_len'] + max(rx, ry) * .45, 0, 0, 8)
                sk.stroke(pts, np.linspace(g['ps_r'], R0, len(pts)), tag='tail')
        if g.get('pl_border', False) and sk.room():
            a = arc(c, 1., math.pi * .12, math.pi * .88, 16) - c
            sk.line(c + a * [rx - .055, ry - .05], FR)
    elif tail == 'fan':
        R = g['fan_R']; ext = g['fan_ext']; c = np.array([0., y_t + R * .18]); nf = g['fan_n']; rf = g['fan_r']
        core = np.vstack([arc(c, R * .78, -ext, math.pi + ext, 30), [c + [-R * .5, -R * .18], c + [R * .5, -R * .18]]])
        sk.blob(core); sk.obs(core, 0., 'tail', True)
        for i in range(nf):
            a = -ext + (math.pi + 2 * ext) * (i + .5) / nf + rng.normal(0, .03); u = np.array([math.cos(a), math.sin(a)])
            L = R * U(rng, .95, 1.05); pts = [c + u * R * .45, c + u * L]
            sk.stroke(pts, [rf * 1.1, rf], tag='tail')
        if g.get('fan_furrows', 0):
            for i in range(1, nf):
                if not sk.room() or i % 2 == 0: continue
                a = -ext + (math.pi + 2 * ext) * i / nf; u = np.array([math.cos(a), math.sin(a)])
                sk.line([c + u * R * .32, c + u * R * .62], FR)
    elif tail == 'lobster':
        fw, lr, dr = g['lob_w'], g['lob_r'], g['lob_droop']; y = y_t + lr
        pts = catmull([(-fw, y + dr), (-fw * .5, y + dr * .15), (0, y), (fw * .5, y + dr * .15), (fw, y + dr)], 16)
        sk.stroke(wiggle(rng, pts, .003), lr, tag='tail')
    elif tail == 'stub':
        pass

# ---------------------------------------------------------------- genome samplers


def _common(rng, g, bend_sd=.3, rot_sd=.08):
    g.setdefault('taper', U(rng, -.18, .18)); g.setdefault('bend', float(np.clip(rng.normal(0, bend_sd), -.8, .8)))
    g.setdefault('shear', float(rng.normal(0, .05))); g.setdefault('rot', float(rng.normal(0, rot_sd)))
    g.setdefault('flip', bool(rng.random() < .5)); g.setdefault('k', U(rng, .014, .024)); g.setdefault('wobble', U(rng, .003, .006))
    g.setdefault('cc', 0.); g.setdefault('extra', 'none'); g.setdefault('g_head', U(rng, GAP, GAP + .02))
    g.setdefault('g_tail', U(rng, GAP, GAP + .02)); g.setdefault('arch', U(rng, -.1, .06)); g.setdefault('t_pow', U(rng, .7, 1.6))
    return g


def _bell(rng, g, long=False):
    """bell head: dome with hanging genal lobes (long=True: crescent with sweeping genal spines)"""
    g.update(head='bell', e=U(rng, 1.8, 3.), cc=U(rng, 0, .07), rc=U(rng, .02, .045))
    if long:
        g.update(gs_r0=U(rng, .052, .072), gs_r1=U(rng, R0, .05), gs_len=U(rng, .22, .5), gs_ang=U(rng, -.08, .35),
                 gs_curv=U(rng, -1.6, .5), gs_curl=U(rng, -2., 1.), gs_in=U(rng, .7, 1.))
    else:
        g.update(gs_r0=U(rng, .052, .085), gs_len=U(rng, .02, .2), gs_ang=U(rng, -.15, .25),
                 gs_curv=U(rng, -4., 1.), gs_curl=U(rng, -8., 0.), gs_in=U(rng, .7, 1.))
        g['gs_r1'] = U(rng, max(R0, g['gs_r0'] * .7), g['gs_r0'])
    return g


def _tail_params(rng, g, tail, small=False):
    g['tail'] = tail
    if tail == 'stub': g['stub'] = U(rng, .0, .06)
    elif tail == 'knob': g['knob_r'] = U(rng, .05, .075)
    elif tail in ('plate', 'spiny'):
        g.update(pl_rx=U(rng, .1, .2) if small else U(rng, .14, .3), pl_ry=U(rng, .07, .13) if small else U(rng, .09, .18),
                 pl_point=U(rng, 0, .5) if rng.random() < .4 else 0., pl_sc=int(rng.integers(0, 5)) if rng.random() < .4 else 0)
        g['pl_sc_r'] = U(rng, .045, .06)
        if tail == 'spiny':
            g.update(ps_n=int(rng.choice([2, 2, 3, 4, 5, 6])), ps_len=U(rng, .05, .16), ps_r=U(rng, .05, .06))
            g['ps_gap'] = max((2 * g['ps_r'] + GAP + .02) / (max(g['pl_rx'], g['pl_ry']) * .6 + g['ps_len']), U(rng, .35, .9))
            g['ps_n'] = int(min(g['ps_n'], 1 + math.floor(2.6 / g['ps_gap'])))
    elif tail == 'fan':
        g.update(fan_R=U(rng, .24, .38), fan_ext=U(rng, -.15, .3), fan_r=U(rng, .045, .06), fan_furrows=bool(rng.random() < .5))
        nmax = int((math.pi + 2 * g['fan_ext']) * g['fan_R'] / (2 * g['fan_r'] + GAP * .95))
        g['fan_n'] = int(rng.integers(max(3, nmax - 3), max(4, nmax + 1)))
    elif tail in ('spine', 'fork'):
        g.update(sp_len=U(rng, .15, .3) if small else U(rng, .2, .45), sp_r=U(rng, .055, .075),
                 sp_ang=U(rng, -.35, .35) if tail == 'spine' else U(rng, .3, .7), sp_curv=U(rng, -1.2, 1.2))
    elif tail == 'lobster':
        g.update(lob_w=U(rng, .08, .2), lob_r=U(rng, .05, .07), lob_droop=U(rng, -.04, .08))
    return g


def _finish_genome(rng, g, eyes_opts, eye_y=(.45, .68), eye_x=(.28, .5)):
    g.setdefault('eyes', pick(rng, eyes_opts)); g.setdefault('eye_y', U(rng, *eye_y)); g.setdefault('eye_x', U(rng, *eye_x))
    return _common(rng, g)


def rib_skeleton(rng):
    """drawing 42eb5982: domed cap whose rim runs down as genal spines, a thin axis and paired stroke ribs"""
    g = dict(head='rim', hw=U(rng, .34, .46), hh=U(rng, .24, .34), e=U(rng, 1.8, 3.), cc=U(rng, 0, .04), rim_r=U(rng, .044, .056),
             cap_drop=U(rng, -.03, .03), gs_len=U(rng, .3, .62), gs_ang=U(rng, -.12, .12), gs_curv=U(rng, -.9, .5),
             gs_curl=U(rng, -2.5, 1.), thorax='ribs', n=int(rng.choice([2, 3, 4, 4, 5])), r_seg=U(rng, R0, .05),
             g_seg=U(rng, GAP, GAP + .03), tw=U(rng, .2, .38), t_taper=U(rng, 0, .5), droop0=U(rng, -.02, .04),
             droop1=U(rng, 0, .08), tip=pick(rng, {'round': .55, 'point': .25, 'hook': .2}), hook_len=U(rng, .05, .1),
             hook_turn=U(rng, 1., 2.2), ax_r=U(rng, R0, .052), last_short=U(rng, .35, .65) if rng.random() < .4 else 1.)
    g['gs_r1'] = U(rng, R0, g['rim_r']); g['ax_r1'] = g['ax_r'] * U(rng, .85, 1.)
    _tail_params(rng, g, pick(rng, {'stub': .5, 'knob': .2, 'fork': .15, 'spine': .15}), small=True)
    _finish_genome(rng, g, {'sleepy': .45, 'dashes': .25, 'dots': .3}, eye_y=(.5, .68), eye_x=(.3, .48))
    return build(rng, g)


def lobed(rng):
    """crescent and fan-tail drawings: bell head, fat axis with round-ended pleural lobes"""
    g = dict(hw=U(rng, .36, .48), hh=U(rng, .26, .38), thorax='lobes', n=int(rng.integers(3, 7)), r_seg=U(rng, .045, .062),
             g_seg=U(rng, .07, .095), ax_r=U(rng, .07, .11), tw=U(rng, .24, .4), t_taper=U(rng, .1, .5),
             droop0=U(rng, -.02, .05), droop1=U(rng, 0, .1), tip=pick(rng, {'round': .6, 'hook': .25, 'point': .15}),
             hook_len=U(rng, .06, .12), hook_turn=U(rng, 1.2, 2.6))
    g['ax_r1'] = g['ax_r'] * U(rng, .6, .9); _bell(rng, g)
    _tail_params(rng, g, pick(rng, {'lobster': .35, 'plate': .2, 'stub': .25, 'knob': .2}), small=True)
    _finish_genome(rng, g, {'dots': .45, 'dashes': .3, 'sleepy': .1, 'none': .15})
    return build(rng, g)


PLANS = {
    'rib-skeleton': (1.2, rib_skeleton),
    'lobed': (1.2, lobed),
}
