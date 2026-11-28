"""Branching growth for the FossilNet v3 corpus, after the owner's antler drawing (dd019b8d): a trunk that splits into
chunky marker branches with round tips.

Every plan grows a skeleton (Skel): polylines with a radius per point, added one branch at a time. A branch is kept
only as far as it stays a clear gap (GAP) away from everything grown before, except in the V of its own junction,
where strokes that meet may sit closer as long as they diverge by at least alpha. So tips stay round and at least
R_TIP thick, neighbouring branches keep gaps about as wide as themselves, and the 3 mm rule holds near a 1.3 size
factor. The skeleton lives in a unit box (y down, apex or crown at the top) and is drawn as round-capped strokes with
a gentle wobble; engraving marks (scars, slits, grooves, shaft openings) go where the parts are wide enough to hold
them 1 mm inside the outline.
"""
import math

import numpy as np

from morph.core import Canvas, wiggle

TAU = 2 * math.pi
UP, DOWN, LEFT, RIGHT = -math.pi / 2, math.pi / 2, math.pi, 0.
R_TIP = .046                   # thinnest stroke radius (unit box): the 3 mm rule near a 1.25 size factor
GAP = .09                      # clear gap between neighbouring strokes
STEP = .022                    # skeleton point spacing


# ---------------------------------------------------------------- small helpers

def wrap(a):
    return (a + math.pi) % TAU - math.pi


def unit(h):
    return np.array([math.cos(h), math.sin(h)])


def arclen(p):
    return float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum()) if len(p) > 1 else 0.


def head(p, i=-1):
    """heading of polyline p at point i"""
    n = len(p); i = i % n; i = min(max(i, 1), n - 1); d = p[i] - p[i - 1]
    return math.atan2(d[1], d[0])


def path(p, h, L, curv=0., drift=0., target=UP, rng=None, jitter=0., step=STEP):
    """polyline of length L from p, initial heading h, constant curvature curv (1/units) plus a turn toward the
    heading target at rate drift (per unit length) and a random walk of the heading (jitter)"""
    n = max(2, int(math.ceil(L / step)) + 1); ds = L / (n - 1); out = np.empty((n, 2)); x, y = float(p[0]), float(p[1])
    out[0] = x, y
    for i in range(1, n):
        h += curv * ds + drift * wrap(target - h) * ds + (jitter * rng.normal() * math.sqrt(ds) if jitter else 0.)
        x += ds * math.cos(h); y += ds * math.sin(h); out[i] = x, y
    return out


def prof(n, r0, r1, pw=1.):
    """radius profile from r0 to r1 over n points"""
    return r0 + (r1 - r0) * np.linspace(0, 1, n) ** pw


def beads(n, L, amp, wl, rng):
    """radius modulation for beaded (botryoidal) branches: 1 + amp * sin along the arc length"""
    if amp <= 0 or n < 3: return np.ones(n)
    s = np.linspace(0, L, n); return 1 + amp * np.sin(TAU * s / wl + rng.uniform(0, TAU))


def box(hw=.5, top=-.5, bot=.5):
    return lambda p, r: (np.abs(p[:, 0]) + r <= hw) & (p[:, 1] - r >= top) & (p[:, 1] + r <= bot)


def disc_dom(R, c=(0., 0.)):
    return lambda p, r: np.hypot(p[:, 0] - c[0], p[:, 1] - c[1]) + r <= R


# ---------------------------------------------------------------- the skeleton

class Skel:
    """branches as polylines with radii; every point added is kept in one array for clearance tests"""

    def __init__(self, gap=GAP, alpha=30., domain=None):
        self.br = []                                           # dict(p, r, J, rj, parent, knob, **tags)
        self.parts = []                                        # extra drawing: ('ellipse', c, rx, ry, ang)
        self.feats = []                                        # engraving: ('eye', c, r) | ('stroke', pts, r)
        self.Q = np.zeros((0, 2)); self.QR = np.zeros(0); self.QO = np.zeros(0, int)
        self.gap = gap; self.cosa = math.cos(math.radians(alpha)); self.s2 = 2 * math.sin(math.radians(alpha) / 2)
        self.domain = domain

    def _store(self, p, r, owner):
        self.Q = np.vstack([self.Q, p]); self.QR = np.r_[self.QR, r]; self.QO = np.r_[self.QO, np.full(len(p), owner, int)]

    def related(self, J, rj, parent=-1):
        """branches that meet at junction J: the parent, branches starting near J, strokes covering J"""
        out = {parent} if parent >= 0 else set()
        out |= {i for i, b in enumerate(self.br) if b['J'] is not None and np.hypot(*(b['J'] - J)) < .6 * max(rj, b['rj']) + .01}
        if len(self.Q): out |= set(np.unique(self.QO[np.hypot(*(self.Q - J).T) < self.QR + .012]).tolist())
        return np.array(sorted(out), int)

    def first_bad(self, P, R, J=None, rj=0., parent=-1):
        """index of the first candidate point that leaves the domain or comes within the gap of the skeleton
        (len(P) if none). Points inside the stroke at the junction J (radius rj) are free; strokes that meet at J may
        come closer inside the junction's V while they diverge by alpha; a close pair whose midpoint lies inside a
        third stroke sits on opposite sides of that stroke and is no gap."""
        n = len(P); lim = n
        if self.domain is not None:
            ok = self.domain(P, R)
            if not ok.all(): lim = int(np.argmin(ok))
        if lim == 0 or not len(self.Q): return lim
        P = P[:lim]; R = R[:lim]
        B = np.sqrt(((P[:, None, :] - self.Q[None]) ** 2).sum(-1)) - R[:, None] - self.QR[None] < self.gap
        if not B.any(): return lim
        if J is not None:
            vp = P - J; dp = np.sqrt((vp ** 2).sum(1)); B[dp < rj] = False
            vq = self.Q - J; dq = np.sqrt((vq ** 2).sum(1))
            meet = self.related(J, rj, parent)
            if len(meet):
                rel = np.nonzero(np.isin(self.QO, meet))[0]
                rv = (self.gap + R.max() + self.QR[rel].max()) / self.s2 + rj
                cs = (vp @ vq[rel].T) / (dp[:, None] * dq[rel][None] + 1e-12)
                v = (dp[:, None] < rv) & (dq[rel][None] < rv) & ((cs < self.cosa) | (dq[rel][None] < rj))
                B[:, rel] &= ~v
        for i in np.nonzero(B.any(1))[0]:
            js = np.nonzero(B[i])[0]; M = .5 * (P[i] + self.Q[js])
            dm = np.sqrt(((M[:, None, :] - self.Q[None]) ** 2).sum(-1))
            cov = ((dm < .9 * self.QR[None]) & (self.QO[None] != self.QO[js][:, None])).any(1)
            if not cov.all(): return int(i)
        return lim

    def fits(self, P, R, J=None, rj=0., parent=-1):
        P = np.atleast_2d(np.asarray(P, float)); R = np.broadcast_to(np.asarray(R, float), (len(P),)).astype(float)
        return self.first_bad(P, R, J, rj, parent) == len(P)

    def grow(self, p, r, J=None, rj=0., parent=-1, knob=0., min_len=0., **tags):
        """add the longest clear prefix of the candidate polyline p (radii r, optional knob at the tip) as a branch
        starting at junction J of a stroke of radius rj; returns its index or None when too short"""
        p = np.asarray(p, float); r = np.broadcast_to(np.asarray(r, float), (len(p),)).astype(float)
        f = self.first_bad(p, r, J, rj, parent)
        if knob > 0:
            kr = np.array([knob])
            while f >= 2 and self.first_bad(p[f - 1:f], kr, J, rj, parent) == 0: f -= 1
        if f < 2: return None
        p, r = p[:f], r[:f]
        if arclen(p) < min_len: return None
        i = len(self.br)
        keep = np.ones(f, bool) if J is None else np.hypot(*(p - J).T) >= rj
        self._store(p[keep], r[keep], i)
        if knob > 0: self._store(p[-1:], np.array([knob]), i)
        self.br.append(dict(p=p, r=r, J=J, rj=rj, parent=parent, knob=knob, **tags))
        return i

    def sprout(self, b, i, h, L, r0, r1, knob=0., min_len=None, pw=1., **kw):
        """lateral or terminal branch from point i of branch b with heading h; kw goes to path()"""
        B = self.br[b]; i = i % len(B['p']); J = B['p'][i]; rj = float(B['r'][i])
        p = path(J, h, L, **kw); r = prof(len(p), r0, r1, pw)
        if min_len is None: min_len = rj + 1.3 * r1
        return self.grow(p, r, J, rj, parent=b, knob=knob, min_len=min_len)

    def ellipse(self, c, rx, ry, ang, J=None, rj=0., owner=-1):
        """an elliptic part (sporangium, bladder): added when its proxy capsule clears the skeleton"""
        a, b = (rx, ry) if rx >= ry else (ry, rx); u = unit(ang if rx >= ry else ang + math.pi / 2)
        q = np.asarray(c, float) + np.linspace(-(a - b), a - b, max(2, int(math.ceil(2 * (a - b) / STEP)) + 1))[:, None] * u
        if not self.fits(q, b, J, rj, owner): return False
        self._store(q, np.full(len(q), b), owner if owner >= 0 else len(self.br) - 1)
        self.parts.append(('ellipse', np.asarray(c, float), rx, ry, ang)); return True

    def tips(self):
        """indices of branches with no children"""
        par = {b['parent'] for b in self.br}; return [i for i in range(len(self.br)) if i not in par]

    def kids(self, b):
        return [i for i, x in enumerate(self.br) if x['parent'] == b]

    def radius_at(self, x):
        """largest inscribed radius of the skeleton around point x (approximate, from stroke discs)"""
        d = self.QR - np.hypot(*(self.Q - x).T); return float(d.max()) if len(d) else 0.

    # engraving marks (kept only where the part around them is wide enough)
    def mark_dot(self, c, r, need=None):
        need = (1 / 30 + r + .006) if need is None else need
        if self.radius_at(c) >= need: self.feats.append(('eye', np.asarray(c, float), r)); return True
        return False

    def mark_line(self, pts, r):
        pts = np.asarray(pts, float)
        if all(self.radius_at(q) >= 1 / 30 + r + .006 for q in pts[::max(1, len(pts) // 6)]) and self.radius_at(pts[-1]) >= 1 / 30 + r + .006:
            self.feats.append(('stroke', pts, r)); return True
        return False

    def draw(self, rng, k=.022, wobble=.004, wig=.004, genome=None):
        c = Canvas(k=k, wobble=wobble)
        for b in self.br:
            p = b['p']; L = arclen(p)
            if wig > 0 and len(p) >= 4 and L > .1: p = wiggle(rng, p, min(wig, .03 * L), 1. + 2.5 * L)
            c.stroke(p, b['r'])
            if b['knob'] > 0: c.disc(p[-1], b['knob'])
        for kind, *a in self.parts:
            if kind == 'ellipse': c.ellipse(a[0], a[1], a[2], a[3])
        for kind, *a in self.feats:
            if kind == 'eye': c.eye(a[0], a[1])
            else: c.feature_stroke(a[0], a[1])
        c.genome = dict(genome or {}); c.genome.setdefault('branches', len(self.br)); c.genome.setdefault('tips', len(self.tips()))
        return c


# ---------------------------------------------------------------- shared branching moves

def attempt(sk, rng, tries, make):
    """call make(rng) -> branch index or None up to tries times; the first success wins"""
    for _ in range(tries):
        b = make(rng)
        if b is not None: return b
    return None


def tines(sk, rng, b, n, rt, kr, lo=.3, hi=.92, pat='up', th=(.55, 1.2), L=(.1, .28), drift=(0., 1.5), tries=3, fork=0.):
    """n lateral tines along branch b at arc fractions in [lo, hi]; pat: 'up' (the side facing up), 'alt', 'out', 'in'"""
    B = sk.br[b]; p = B['p']; m = len(p); out = []
    fr = np.sort(rng.uniform(lo, hi, n)) if n > 1 else rng.uniform(lo, hi, 1)
    for q, f in enumerate(fr):
        def make(g, f=f, q=q):
            i = min(m - 2, max(1, int((f + g.normal(0, .04)) * (m - 1)))); hb = head(p, i)
            if pat == 'alt': s = (-1) ** q
            elif pat == 'out': s = 1 if wrap(hb - UP) > 0 else -1
            elif pat == 'in': s = -1 if wrap(hb - UP) > 0 else 1
            elif pat == 'rand': s = g.choice([-1, 1])
            else: s = 1 if abs(wrap(hb + .9 - UP)) < abs(wrap(hb - .9 - UP)) else -1
            h = hb + s * g.uniform(*th); Lt = g.uniform(*L) * (1.15 - .35 * f)
            dr = g.uniform(*drift) if abs(wrap(hb - UP)) > .6 else 0.
            return sk.sprout(b, i, h, Lt + B['r'][i], B['r'][i] * .82, rt, knob=kr, drift=dr, target=UP + g.normal(0, .2), curv=g.normal(0, .6))
        c = attempt(sk, rng, tries, make)
        if c is not None:
            out.append(c)
            if fork and rng.random() < fork: forks(sk, rng, c, 1, rt, kr, rng.uniform(.4, .65), rng.uniform(.4, .7))
    return out


def forks(sk, rng, b, depth, rt, kr, ang, ratio, drift=0., n=2, target=UP, lead=0., curv=.8, tries=2):
    """dichotomous (n-way) forks at the end of branch b for depth levels; lead > 0 makes one daughter the leader
    (sympodial: longer and straighter)"""
    B = sk.br[b]; p = B['p']; h = head(p); L0 = max(arclen(p), .12); r0 = float(B['r'][-1]); out = []
    lead_j = int(rng.integers(n)) if lead > 0 else -1
    for j in range(n):
        def make(g, j=j):
            hj = h + ((j / (n - 1) - .5) * 2 * ang * g.uniform(.85, 1.2) if n > 1 else 0.)
            if j == lead_j: hj = h + (hj - h) * (1 - lead)
            L = max(.07, L0 * ratio * g.uniform(.75, 1.25) * (1 + lead if j == lead_j else 1)) + r0
            r1 = rt if depth <= 1 else max(rt, r0 * .82)
            return sk.sprout(b, -1, hj, L, max(r0 * .92, rt), r1, knob=kr if depth <= 1 else 0., drift=drift, target=target,
                             curv=g.normal(0, curv))
        c = attempt(sk, rng, tries, make)
        if c is not None:
            out.append(c)
            if depth > 1: out += forks(sk, rng, c, depth - 1, rt, kr, ang * rng.uniform(.8, 1.05), ratio, drift, n, target, lead, curv, tries)
    if not out and B['knob'] == 0 and kr > 0:                  # nothing fitted: give the end its knob back
        if sk.fits(p[-1:], kr, p[-1], r0, b): B['knob'] = kr; sk._store(p[-1:], np.array([kr]), b)
    return out


# ---------------------------------------------------------------- antler

def antler(rng):
    """a short trunk that splits into 1-3 beams curving up, with tines on their upper side (sympodial), forked ends
    (dichotomous) or a palm with fingers; round, often bulbous tips; a flared foot and sometimes a stub on the trunk,
    like the owner's drawing"""
    alpha = rng.uniform(28, 34); sk = Skel(GAP, alpha, box(.5))
    rt = rng.uniform(.046, .056); kr = rt * rng.uniform(1.1, 1.4) if rng.random() < .8 else 0.
    R0 = rng.uniform(.07, .105); lean = rng.normal(0, .1); x0 = rng.normal(0, .05); y0 = .5 - R0 * 1.4
    Hs = rng.uniform(.14, .36)
    trunk = path((x0, y0), UP + lean, Hs, curv=rng.normal(0, .8))
    t = sk.grow(trunk, prof(len(trunk), R0 * rng.uniform(1.05, 1.3), R0, .5))
    T = sk.br[t]
    foot = rng.random() < .6
    if foot:                                                   # flared foot
        fw = R0 * rng.uniform(.7, 1.5); fr = R0 * rng.uniform(.55, .75)
        for s in (-1, 1): sk.sprout(t, 0, (RIGHT if s > 0 else LEFT) + rng.normal(0, .12), fw * rng.uniform(.8, 1.2), fr, fr * .9, min_len=0.)
    nb = int(rng.choice([1, 2, 3], p=[.22, .6, .18])); mode = str(rng.choice(['tines', 'fork', 'palm', 'mixed'], p=[.42, .25, .1, .23]))
    room = T['p'][-1][1] + .5; h0 = head(T['p'])
    if nb == 1: ths = [rng.normal(0, .25)]
    elif nb == 2: ths = [-rng.uniform(.4, 1.15), rng.uniform(.4, 1.15)]
    else: ths = [-rng.uniform(.6, 1.2), rng.normal(0, .15), rng.uniform(.6, 1.2)]
    beams = []
    for th in ths:
        m = mode if mode != 'mixed' else str(rng.choice(['tines', 'fork', 'palm'], p=[.5, .35, .15]))
        def make(g, th=th, m=m):
            th2 = th + g.normal(0, .08); L = room * g.uniform(.65, 1.) / max(.55, math.cos(th2 * .6))
            r1 = rt * (g.uniform(1., 1.2) if m != 'palm' else g.uniform(1.5, 2.1))
            return sk.sprout(t, -1, h0 + th2, L, R0 * g.uniform(.7, .88), r1, knob=kr if m == 'tines' else 0., pw=.8 if m != 'palm' else 1.5,
                             drift=g.uniform(.8, 3.), target=UP + .3 * th2, curv=g.normal(0, .5))
        b = attempt(sk, rng, 3, make)
        if b is not None: beams.append((b, m))
    for b, m in beams:
        if m == 'tines': tines(sk, rng, b, int(rng.integers(1, 4)), rt, kr, pat=str(rng.choice(['up', 'up', 'alt', 'out'])), fork=.25)
        elif m == 'fork':
            forks(sk, rng, b, int(rng.integers(1, 3)), rt, kr, rng.uniform(.35, .6), rng.uniform(.45, .8), lead=rng.uniform(0, .5))
            if rng.random() < .5: tines(sk, rng, b, 1, rt, kr, .35, .75)
        else:
            tines(sk, rng, b, int(rng.integers(3, 6)), rt, kr, .5, 1., pat=str(rng.choice(['up', 'out'])), th=(.3, 1.1), L=(.08, .2))
            if kr: sk.br[b]['knob'] = 0.
    if rng.random() < .45:                                      # a short stub on the trunk, like the drawing
        def make(g):
            i = int(len(T['p']) * g.uniform(.2, .75)); s = g.choice([-1, 1])
            return sk.sprout(t, i, head(T['p'], i) + s * g.uniform(1.1, 1.7), g.uniform(.08, .16) + R0, R0 * .75, rt * 1.1, knob=kr * 1.05)
        attempt(sk, rng, 3, make)
    if R0 > .08 and rng.random() < .5:                          # longitudinal grooves of the antler surface
        p = T['p']; a = p[int(len(p) * .15)]; e = p[int(len(p) * .8)]
        if np.hypot(*(e - a)) > .07: sk.mark_line(np.stack([np.linspace(a[0], e[0], 6), np.linspace(a[1], e[1], 6)], 1), .011)
    G = dict(trunk_r=R0, tip_r=rt, knob=kr, beams=len(beams), mode=mode, alpha=alpha, foot=foot)
    return sk.draw(rng, k=rng.uniform(.015, .03), wobble=rng.uniform(.003, .0045), genome=G)


PLANS = {
    'antler': (1.4, antler),
}
