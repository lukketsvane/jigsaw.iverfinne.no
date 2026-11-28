"""Morphogenesis core for FossilNet v3: an SDF canvas that draws creatures the way the owner's marker drawings look.

A body plan draws into a Canvas with strokes (round-capped, tapered capsules along polylines: the marker), filled
blobs, cut-outs, and engraving features (eyes, faces, sutures) in a separate layer. Geometry is in local units
(about one unit across, y pointing down as in the drawings, the head at the top) and passes through the canvas warp
when it is added, so a bend or a wave keeps every stroke's thickness.

Canvas.finish() renders the display list on a grid fitted to the creature, adds a slight hand-drawn wobble, keeps the
largest component, fills holes, and returns a Creature:

* outline: 2048 points, centred on its box, extent 1, canonical orientation and start (the top of the vertical
  centre line), so outlines of different creatures correspond as well as possible;
* features: engraving loops inside the outline, at least 1 mm from it at the 30 mm base size;
* size factor (fossil_net v2's 3 mm rule): at a board size of BASE_MM x factor, a 1.5 mm erosion keeps the creature in
  one piece and an opening keeps 97 % of its area, and closing fills at most 20 % of its bays.

Creatures that would need more than MAX_FACTOR to keep 3 mm parts, that are too sparse, too elongated, or that 256
B-spline controls cannot follow are rejected with Reject(reason).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy import ndimage
from scipy.ndimage import gaussian_filter1d

SEG = .02                      # longest segment when strokes and blobs are densified (local units), so warps bend them smoothly
RES = 256                      # render grid along the longer side of the creature's box
NPTS = 2048                    # outline points
N = 256                        # B-spline controls the network predicts
FEAT, FP = 6, 24               # engraving loops per creature, points per loop
BASE_MM, R_MM, KEEP, NOTCH_KEEP, MAX_FACTOR = 30., 1.5, .97, .8, 1.8
CLEAR = 1. / BASE_MM           # engraving clearance: 1 mm inside the outline at the base size (unit coordinates)
BIG = 1e3

# acceptance limits for a training creature
MAX_ASPECT = 3.2               # box long side / short side
MIN_SOLIDITY = .30             # area / convex hull area
MAX_LOST = .03                 # area share of components other than the largest
MIN_SPLINE_IOU = .97           # 256 controls must follow the outline this well


class Reject(Exception):
    """a creature that cannot be a training target; str(e) is the reason"""


# ---------------------------------------------------------------- geometry helpers for body plans

def densify(pts, seg=SEG, closed=False, values=None):
    """insert points so no segment is longer than seg; values (per point) are interpolated along"""
    p = np.asarray(pts, float).reshape(-1, 2)
    if closed: p = np.vstack([p, p[:1]])
    v = None if values is None else np.broadcast_to(np.asarray(values, float), (len(p) - closed,)).copy()
    if v is not None and closed: v = np.r_[v, v[:1]]
    if len(p) < 2: return (p, v) if values is not None else p
    out, vo = [p[:1]], [v[:1]] if v is not None else None
    for i in range(len(p) - 1):
        L = float(np.hypot(*(p[i + 1] - p[i]))); k = max(1, int(math.ceil(L / seg))); t = (np.arange(1, k + 1) / k)[:, None]
        out.append(p[i] + t * (p[i + 1] - p[i]))
        if v is not None: vo.append(v[i] + t[:, 0] * (v[i + 1] - v[i]))
    p = np.vstack(out); v = np.concatenate(vo) if v is not None else None
    if closed: p = p[:-1]; v = v[:-1] if v is not None else None
    return (p, v) if values is not None else p


def arc(center, radius, a0, a1, n=24):
    """points on a circular arc (angles in radians, y down: angle 0 points right, pi/2 points down)"""
    t = np.linspace(a0, a1, n); return np.stack([center[0] + radius * np.cos(t), center[1] + radius * np.sin(t)], 1)


def ellipse_pts(center, rx, ry, ang=0., n=64):
    t = np.linspace(0, 2 * np.pi, n, endpoint=False); x, y = rx * np.cos(t), ry * np.sin(t); c, s = math.cos(ang), math.sin(ang)
    return np.stack([center[0] + c * x - s * y, center[1] + s * x + c * y], 1)


def catmull(ctrl, n=48, closed=False):
    """smooth curve through control points (centripetal-ish Catmull-Rom, uniform parameter)"""
    P = np.asarray(ctrl, float)
    if len(P) < 3 and not closed: return densify(P, seg=1e9) if len(P) > 1 else P
    if closed: P = np.vstack([P[-1:], P, P[:2]])
    else: P = np.vstack([2 * P[0] - P[1], P, 2 * P[-1] - P[-2]])
    segs = len(P) - 3; per = max(2, int(math.ceil(n / segs))); out = []
    for i in range(segs):
        p0, p1, p2, p3 = P[i:i + 4]; t = np.linspace(0, 1, per, endpoint=False)[:, None]
        out.append(.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t ** 2 + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3))
    if not closed: out.append(P[-2][None])
    return np.vstack(out)


def bezier(p0, p1, p2, p3, n=24):
    t = np.linspace(0, 1, n)[:, None]; p0, p1, p2, p3 = (np.asarray(v, float) for v in (p0, p1, p2, p3))
    return (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t * t * p2 + t ** 3 * p3


def mirror(pts):
    """reflect across the vertical centre line (x -> -x)"""
    p = np.array(pts, float); p[..., 0] *= -1; return p


def wiggle(rng, pts, amp=.006, freq=3.):
    """smooth random sideways displacement along a polyline: a hand-drawn line instead of a ruled one"""
    p = np.asarray(pts, float)
    if len(p) < 3 or amp <= 0: return p
    d = np.gradient(p, axis=0); n = np.stack([-d[:, 1], d[:, 0]], 1); n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-12
    s = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]; s = s / max(s[-1], 1e-9)
    w = sum(rng.normal() * np.sin(2 * np.pi * (f * s + rng.random())) for f in (freq, 2.1 * freq)) * amp / 1.4
    return p + n * w[:, None]

# ---------------------------------------------------------------- warps (applied to geometry when it is added)

def compose(*fs):
    fs = [f for f in fs if f is not None]
    def w(p):
        for f in fs: p = f(p)
        return p
    return w


def bend(curv, y0=0.):
    """bend the vertical body axis into an arc of curvature curv (1/units); thickness is kept"""
    if abs(curv) < 1e-6: return None
    R = 1. / curv
    def w(p):
        p = np.asarray(p, float); th = (p[:, 1] - y0) * curv; r = R - p[:, 0]
        return np.stack([R - r * np.cos(th), y0 + r * np.sin(th)], 1)
    return w


def wave(amp, freq, phase=0.):
    def w(p):
        p = np.array(p, float); p[:, 0] += amp * np.sin(2 * np.pi * freq * p[:, 1] + phase); return p
    return w


def shear(s):
    def w(p):
        p = np.array(p, float); p[:, 0] += s * p[:, 1]; return p
    return w


def taper(t):
    """widen the body toward the head (t > 0) or the tail (t < 0)"""
    def w(p):
        p = np.array(p, float); p[:, 0] *= 1 - t * p[:, 1]; return p
    return w


def stretch(sx, sy):
    def w(p):
        p = np.array(p, float); p[:, 0] *= sx; p[:, 1] *= sy; return p
    return w


def rotate(ang):
    c, s = math.cos(ang), math.sin(ang)
    def w(p):
        p = np.asarray(p, float); return np.stack([c * p[:, 0] - s * p[:, 1], s * p[:, 0] + c * p[:, 1]], 1)
    return w

# ---------------------------------------------------------------- the canvas

@dataclass
class Op:
    layer: str                 # 'body' or 'feat'
    mode: str                  # 'add' or 'cut'
    kind: str                  # 'stroke' or 'blob'
    pts: np.ndarray            # warped geometry
    r: np.ndarray | None       # per-point radius (strokes)
    k: float                   # smoothing radius of the union / subtraction


@dataclass
class Creature:
    outline: np.ndarray                         # (NPTS, 2) unit extent, centred, canonical start and orientation
    features: list                              # (FP, 2) loops in the same frame
    size_factor: float
    e_feat: float                               # board size (mm) at which every part is 3 mm wide
    e_notch: float                              # board size (mm) at which 80 % of the bays are 3 mm wide
    area: float
    solidity: float
    aspect: float
    spline_iou: float
    plan: str = ''
    genome: dict = field(default_factory=dict)
    mask: np.ndarray | None = None              # rendered silhouette (for previews)


class Canvas:
    """display list of strokes, blobs and cut-outs in local units; see the module docstring"""

    def __init__(self, k=.025, wobble=.004, warp=None, seg=SEG):
        self.ops: list[Op] = []; self.k = k; self.wobble = wobble; self.warp = warp; self.seg = seg; self.genome = {}

    # every primitive is a stroke (polyline with radii) or a blob (closed polygon)
    def _add(self, layer, mode, kind, pts, r=None, k=None):
        if kind == 'stroke':
            pts = np.asarray(pts, float).reshape(-1, 2); r = np.atleast_1d(np.asarray(r, float))
            if len(r) not in (1, len(pts)):                 # radii given at any resolution: spread them along the stroke
                r = np.interp(np.linspace(0, 1, len(pts)), np.linspace(0, 1, len(r)), r)
            if (r <= 0).any(): raise Reject('non-positive stroke radius')
            p, rr = densify(pts, self.seg, values=np.broadcast_to(r, (len(pts),)))
        else:
            p, rr = densify(pts, self.seg, closed=True), None
        if self.warp is not None: p = self.warp(p)
        if not np.all(np.isfinite(p)): raise Reject('non-finite geometry')
        self.ops.append(Op(layer, mode, kind, np.asarray(p, float), rr, self.k if k is None else float(k)))
        return self

    def stroke(self, pts, r, k=None):
        """round-capped marker line along pts, radius r (scalar or one per point), smoothly joined to the body"""
        return self._add('body', 'add', 'stroke', pts, r, k)

    def disc(self, c, r, k=None):
        return self._add('body', 'add', 'stroke', np.asarray(c, float)[None], r, k)

    def blob(self, pts, k=None):
        """filled closed polygon"""
        return self._add('body', 'add', 'blob', pts, None, k)

    def ellipse(self, c, rx, ry, ang=0., k=None):
        return self.blob(ellipse_pts(c, rx, ry, ang), k)

    def cut_stroke(self, pts, r, k=None):
        return self._add('body', 'cut', 'stroke', pts, r, 0. if k is None else k)

    def cut_blob(self, pts, k=None):
        return self._add('body', 'cut', 'blob', pts, None, 0. if k is None else k)

    def cut_disc(self, c, r, k=None):
        return self._add('body', 'cut', 'stroke', np.asarray(c, float)[None], r, 0. if k is None else k)

    def eye(self, c, r):
        """engraving: a dot"""
        return self._add('feat', 'add', 'stroke', np.asarray(c, float)[None], r, 0.)

    def feature_stroke(self, pts, r):
        """engraving: a line (eyebrow, mouth, suture, glabella rim); thin lines are thickened to one loop"""
        return self._add('feat', 'add', 'stroke', pts, r, 0.)

    def feature_blob(self, pts):
        return self._add('feat', 'add', 'blob', pts, None, 0.)

    def finish(self, rng=None, plan='', res=RES, keep_mask=False) -> Creature:
        return finish(self, rng if rng is not None else np.random.default_rng(), plan, res, keep_mask)


def _smin(a, b, k):
    if k <= 0: return np.minimum(a, b)
    h = np.clip(.5 + .5 * (b - a) / k, 0., 1.)
    return b * (1 - h) + a * h - k * h * (1 - h)


def _capsules(xs, ys, pts, r, reach):
    """distance to the union of tapered capsules along a polyline (the uneven capsule of I. Quilez) on the grid
    xs (columns) x ys (rows); each segment is evaluated only within `reach` of itself"""
    out = np.full((len(ys), len(xs)), BIG)
    x0, y0, px = xs[0], ys[0], (xs[1] - xs[0]) if len(xs) > 1 else 1.
    for i in range(max(1, len(pts) - 1)):
        ax, ay = pts[i]; bx, by = pts[min(i + 1, len(pts) - 1)]; ra, rb = r[i], r[min(i + 1, len(pts) - 1)]
        R = max(ra, rb) + reach
        c0 = max(0, int((min(ax, bx) - R - x0) / px)); c1 = min(len(xs), int(math.ceil((max(ax, bx) + R - x0) / px)) + 1)
        r0 = max(0, int((min(ay, by) - R - y0) / px)); r1 = min(len(ys), int(math.ceil((max(ay, by) + R - y0) / px)) + 1)
        if c1 <= c0 or r1 <= r0: continue
        X = xs[None, c0:c1]; Y = ys[r0:r1, None]
        px_, py_ = X - ax, Y - ay; ex, ey = bx - ax, by - ay; h = ex * ex + ey * ey
        if h < 1e-14: d = np.sqrt(px_ * px_ + py_ * py_) - max(ra, rb)
        else:
            b = ra - rb
            if b * b >= h:                                  # one end disc swallows the other
                d = np.sqrt(px_ * px_ + py_ * py_) - ra if ra >= rb else np.sqrt((X - bx) ** 2 + (Y - by) ** 2) - rb
            else:
                qx = np.abs((px_ * ey - py_ * ex) / h); qy = (px_ * ex + py_ * ey) / h; cc = math.sqrt(h - b * b)
                k = cc * qy - b * qx; m = cc * qx + b * qy; n = qx * qx + qy * qy
                d = np.where(k < 0, np.sqrt(h * n) - ra, np.where(k > cc, np.sqrt(np.maximum(h * (n + 1 - 2 * qy), 0)) - rb, m - ra))
        sub = out[r0:r1, c0:c1]; np.minimum(sub, d, out=sub)
    return out


def edt(mask):
    """exact Euclidean distance (pixels) from every True pixel to the nearest False pixel"""
    return cv2.distanceTransform(np.ascontiguousarray(mask, np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)


def _blob_sdf(X0, Y0, px, shape, poly):
    """signed distance (local units) to a filled polygon on a window, from its raster (pixel accurate)"""
    h, w = shape; pp = np.round(((poly - [X0, Y0]) / px - .5) * 16).astype(np.int32)
    m = np.zeros((h, w), np.uint8); cv2.fillPoly(m, [pp], 1, cv2.LINE_8, shift=4)
    if not m.any(): return np.full((h, w), BIG)
    inside = m.astype(bool); din = edt(inside); dout = edt(~inside)
    return np.where(inside, -(din - .5), dout - .5) * px


def _render(ops, x0, y0, px, nx, ny, layer):
    F = np.full((ny, nx), BIG, np.float64)
    for op in ops:
        if op.layer != layer: continue
        rmax = float(op.r.max()) if op.r is not None else 0.
        pad = rmax + op.k + 3 * px
        lo = op.pts.min(0) - pad; hi = op.pts.max(0) + pad
        c0 = max(0, int((lo[0] - x0) / px)); c1 = min(nx, int(math.ceil((hi[0] - x0) / px)) + 1)
        r0 = max(0, int((lo[1] - y0) / px)); r1 = min(ny, int(math.ceil((hi[1] - y0) / px)) + 1)
        if c1 <= c0 or r1 <= r0: continue
        if op.kind == 'stroke':
            xs = x0 + (np.arange(c0, c1) + .5) * px; ys = y0 + (np.arange(r0, r1) + .5) * px
            D = _capsules(xs, ys, op.pts, op.r, op.k + 3 * px)
        else:
            D = _blob_sdf(x0 + c0 * px, y0 + r0 * px, px, (r1 - r0, c1 - c0), op.pts)
        W = F[r0:r1, c0:c1]
        F[r0:r1, c0:c1] = _smin(W, D, op.k) if op.mode == 'add' else -_smin(-W, D, op.k)
    return F


def _noise(X, Y, rng, amp, size):
    if amp <= 0: return 0.
    out = np.zeros_like(X)
    for _ in range(7):
        f = rng.uniform(2.5, 11) / size; th = rng.uniform(0, 2 * np.pi)
        out += rng.normal(1, .3) * np.sin(2 * np.pi * f * (X * math.cos(th) + Y * math.sin(th)) + rng.uniform(0, 2 * np.pi))
    return out * amp / math.sqrt(7 / 2)


def resample(p, n):
    p = np.vstack([p, p[0]]); d = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]; d, ix = np.unique(d, return_index=True); p = p[ix]
    t = np.linspace(0, d[-1], n, endpoint=False); return np.stack([np.interp(t, d, p[:, i]) for i in range(2)], 1)


def signed_area(p):
    x, y = p[:, 0], p[:, 1]; return .5 * float(x @ np.roll(y, -1) - y @ np.roll(x, -1))


def canonical(p, strip=.06):
    """positive orientation (y down) and the start at the top of the vertical centre line"""
    p = np.asarray(p, float)
    if signed_area(p) < 0: p = p[::-1]
    lo, hi = p.min(0), p.max(0); cx = (lo[0] + hi[0]) / 2; near = np.abs(p[:, 0] - cx) < strip * (hi - lo).max()
    i = int(np.argmin(np.where(near, p[:, 1], np.inf))) if near.any() else int(np.argmin(p[:, 1]))
    return np.roll(p, -i, axis=0)


_CINV = {}


def controls(p, n=N):
    """closed outline -> n periodic cubic B-spline controls that interpolate its arc-length samples"""
    if n not in _CINV:
        a = np.eye(n) * 4 / 6 + np.roll(np.eye(n), 1, axis=1) / 6 + np.roll(np.eye(n), -1, axis=1) / 6; _CINV[n] = np.linalg.inv(a)
    return _CINV[n] @ resample(p, n)


def flatten(q, steps=6):
    a, b, c, d = np.roll(q, 1, 0), q, np.roll(q, -1, 0), np.roll(q, -2, 0)
    seg = np.stack([(a + 4 * b + c) / 6, (2 * b + c) / 3, (b + 2 * c) / 3, (b + 4 * c + d) / 6], 1)
    t = np.arange(steps) / steps; w = np.stack([(1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3], 1)
    return np.einsum('tk,nkd->ntd', w, seg).reshape(-1, 2)

# ---------------------------------------------------------------- 3 mm rule on a raster

def raster(p, px, pad):
    """fill polygon p (unit coordinates) on a grid of pixel px with pad pixels around its box"""
    lo = p.min(0); hi = p.max(0); w = int(math.ceil((hi[0] - lo[0]) / px)) + 2 * pad; h = int(math.ceil((hi[1] - lo[1]) / px)) + 2 * pad
    m = np.zeros((h, w), np.uint8); cv2.fillPoly(m, [np.round(((p - lo) / px + pad - .5) * 16).astype(np.int32)], 1, cv2.LINE_8, shift=4)
    return m.astype(bool)


def _bisect(ok, lo, hi, it=9):
    """largest t in [lo, hi] with ok(t), for ok true at lo and monotone (true below, false above)"""
    if ok(hi): return hi
    for _ in range(it):
        mid = .5 * (lo + hi)
        if ok(mid): lo = mid
        else: hi = mid
    return lo


def measure(outline, base=BASE_MM, r=R_MM, keep=KEEP, notch_keep=NOTCH_KEEP, px=1 / 224):
    """fossil_net v2's 3 mm rule on a raster: (e_feat, e_notch, size_factor); the outline has extent 1.
    e_feat: board size (mm) from which a 1.5 mm erosion keeps the outline in one piece and a 1.5 mm opening keeps
    `keep` of its area. e_notch: board size from which a 1.5 mm closing fills at most 1 - notch_keep of its bays."""
    tmax = r / 16. / px                                 # erosion radius (px) at the smallest board size considered, 16 mm
    pad = int(math.ceil(tmax)) + 4; M = raster(outline, px, pad); area = M.sum()
    if area < 50: raise Reject('tiny')
    din = edt(M); dout = edt(~M)

    def kept(t):                                        # opening by a disc of radius t keeps `keep` of the area
        er = din > t
        if not er.any(): return False
        return (edt(~er) <= t).sum() / area >= keep

    t = _bisect(kept, .5, min(tmax, float(din.max()) - .5), 8) if kept(.5) else .5
    for _ in range(40):                                 # a 1.5 mm erosion must stay one piece (specks under 4 px ignored)
        n, lab, st, _c = cv2.connectedComponentsWithStats((din > t).astype(np.uint8), connectivity=8)
        if n <= 2 or (st[1:, cv2.CC_STAT_AREA] >= 4).sum() <= 1: break
        t *= .96
    e_feat = min(140., r / (t * px))
    hull = np.zeros_like(M, np.uint8); ys, xs = np.nonzero(M)
    cv2.fillPoly(hull, [cv2.convexHull(np.stack([xs, ys], 1).astype(np.int32))], 1); bays = hull.astype(bool) & ~M; nb = bays.sum()
    if nb < 1e-3 * area: e_notch = 16.
    else:
        def open_ok(t):                                 # closing by t fills at most 1 - notch_keep of the bays
            closed = ~(edt(~(dout > t)) <= t)
            return (closed & bays).sum() / nb <= 1 - notch_keep
        t_n = _bisect(open_ok, .5, tmax, 7) if open_ok(.5) else .5
        e_notch = min(240., r / (t_n * px))
    factor = min(MAX_FACTOR, max(1., max(e_feat, e_notch) / base))
    return float(e_feat), float(e_notch), float(factor)


def raster_iou(a, b, px=1 / 200):
    lo = np.minimum(a.min(0), b.min(0)); w = int(math.ceil((np.maximum(a.max(0), b.max(0)) - lo).max() / px)) + 4
    ma = np.zeros((w, w), np.uint8); mb = np.zeros((w, w), np.uint8)
    cv2.fillPoly(ma, [np.round(((a - lo) / px + 2) * 16).astype(np.int32)], 1, cv2.LINE_8, shift=4)
    cv2.fillPoly(mb, [np.round(((b - lo) / px + 2) * 16).astype(np.int32)], 1, cv2.LINE_8, shift=4)
    u = np.count_nonzero(ma | mb); return np.count_nonzero(ma & mb) / max(u, 1)

# ---------------------------------------------------------------- finishing a creature

def _silhouette(F, x0, y0, px):
    """largest component of F < 0 with its holes filled, and its outer contour at sub-pixel accuracy (local units)"""
    from skimage.measure import find_contours
    m = F < 0
    n, lab, st, _c = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=8)
    if n <= 1: raise Reject('empty')
    sizes = st[1:, cv2.CC_STAT_AREA]; main = 1 + int(np.argmax(sizes))
    if 1 - sizes.max() / sizes.sum() > MAX_LOST: raise Reject('disconnected')
    m = ndimage.binary_fill_holes(lab == main)
    if m[0].any() or m[-1].any() or m[:, 0].any() or m[:, -1].any(): raise Reject('clipped')
    near = cv2.dilate(m.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    G = np.where(m & (F >= 0), -.5 * px, F)                 # holes count as inside
    G = np.where(near, G, np.maximum(G, .5 * px))           # other components count as outside
    cs = find_contours(G, 0.)
    if not cs: raise Reject('empty')
    ct = max(cs, key=len)
    if np.hypot(*(ct[0] - ct[-1])) > 2: raise Reject('open contour')
    return m, ct[:-1, ::-1] * px + [x0 + .5 * px, y0 + .5 * px]


def _loops(mask, x0, y0, px, minpix=6):
    cs = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)[0]
    return [c[:, 0].astype(float) * px + [x0 + .5 * px, y0 + .5 * px] for c in cs if len(c) >= minpix]


def finish(c: Canvas, rng, plan='', res=RES, keep_mask=False) -> Creature:
    body = [o for o in c.ops if o.layer == 'body' and o.mode == 'add']
    if not body: raise Reject('empty')
    lo = np.min([o.pts.min(0) - (o.r.max() if o.r is not None else 0) for o in body], 0)
    hi = np.max([o.pts.max(0) + (o.r.max() if o.r is not None else 0) for o in body], 0)
    size = float((hi - lo).max())
    if not np.isfinite(size) or size <= 1e-6: raise Reject('empty')
    margin = .06 * size + 2 * max(o.k for o in body); px = (size + 2 * margin) / res
    x0, y0 = lo - margin; nx = int(math.ceil((hi[0] - lo[0] + 2 * margin) / px)); ny = int(math.ceil((hi[1] - lo[1] + 2 * margin) / px))
    F = _render(c.ops, x0, y0, px, nx, ny, 'body')
    X, Y = np.meshgrid(x0 + (np.arange(nx) + .5) * px, y0 + (np.arange(ny) + .5) * px)
    F = F + _noise(X, Y, rng, c.wobble * size, size)
    m, ct = _silhouette(F, x0, y0, px)
    o = gaussian_filter1d(resample(ct, NPTS), 1., axis=0, mode='wrap')
    olo, ohi = o.min(0), o.max(0); centre = (olo + ohi) / 2; ext = float((ohi - olo).max())
    unit = canonical((o - centre) / ext)
    w, h = (ohi - olo) / ext; aspect = float(max(w, h) / max(min(w, h), 1e-9))
    if aspect > MAX_ASPECT: raise Reject('elongated')
    from shapely.geometry import Polygon
    P = Polygon(unit)
    if not P.is_valid: raise Reject('self-intersecting')
    solidity = P.area / P.convex_hull.area
    if solidity < MIN_SOLIDITY: raise Reject('sparse')
    s_iou = raster_iou(unit, flatten(controls(unit)))
    if s_iou < MIN_SPLINE_IOU: raise Reject('too detailed for 256 controls')
    e_feat, e_notch, factor = measure(unit)
    if e_feat > BASE_MM * MAX_FACTOR + 1e-6: raise Reject('thin parts')
    feats = []
    if any(o.layer == 'feat' for o in c.ops):
        G = _render(c.ops, x0, y0, px, nx, ny, 'feat')
        clear = CLEAR * ext / px                     # clearance in render pixels (unit -> local via ext)
        inner = ndimage.distance_transform_edt(m) > clear
        fm = (G < 0) & inner
        lab2, k2 = ndimage.label(fm)
        for j in range(1, k2 + 1):                    # only features wholly inside the clearance zone
            comp = lab2 == j
            if (comp & ~inner).any() or ((G < 0) & ~inner & ndimage.binary_dilation(comp, iterations=2)).any(): continue
            for lp in _loops(comp, x0, y0, px):
                q = (lp - centre) / ext
                if abs(signed_area(q)) < (1.5 / BASE_MM) ** 2 * .25: continue    # under about 0.75 mm across at base size
                q = gaussian_filter1d(resample(q, 128), 1., axis=0, mode='wrap')
                feats.append(resample(q, FP))
        feats = sorted(feats, key=lambda f: -abs(signed_area(f)))[:FEAT]
    cr = Creature(unit, feats, factor, e_feat, e_notch, float(P.area), float(solidity), aspect, float(s_iou), plan, dict(c.genome))
    if keep_mask: cr.mask = m
    return cr

# ---------------------------------------------------------------- crossbreeds

def blend(a: Canvas, b: Canvas, t: float, rng, plan='chimera', res=RES) -> Creature:
    """crossbreed: interpolate the exact signed distance fields of two finished creatures, aligned on their
    centroids and scaled to equal area; the nearer parent lends its engraving"""
    ca, cb = a.finish(rng), b.finish(rng)

    def frame(cr):
        p = cr.outline; x, y = p[:, 0], p[:, 1]; cross = x * np.roll(y, -1) - np.roll(x, -1) * y; A = cross.sum() / 2
        cen = np.array([((x + np.roll(x, -1)) * cross).sum(), ((y + np.roll(y, -1)) * cross).sum()]) / (6 * A)
        s = math.sqrt(.3 / abs(A)); return lambda q: (q - cen) * s
    fa, fb = frame(ca), frame(cb); pa, pb = fa(ca.outline), fb(cb.outline)
    lo = np.minimum(pa.min(0), pb.min(0)); hi = np.maximum(pa.max(0), pb.max(0)); size = float((hi - lo).max())
    margin = .08 * size; px = (size + 2 * margin) / res; x0, y0 = lo - margin
    nx = int(math.ceil((hi[0] - lo[0] + 2 * margin) / px)); ny = int(math.ceil((hi[1] - lo[1] + 2 * margin) / px))

    def sdf(p):
        m = np.zeros((ny, nx), np.uint8); cv2.fillPoly(m, [np.round(((p - [x0, y0]) / px - .5) * 16).astype(np.int32)], 1, cv2.LINE_8, shift=4)
        m = m.astype(bool); return np.where(m, -(edt(m) - .5), edt(~m) - .5) * px
    _, ct = _silhouette(ndimage.gaussian_filter((1 - t) * sdf(pa) + t * sdf(pb), 1.2), x0, y0, px)
    c = Canvas(wobble=0.); c.blob(ct, k=0.)
    src, f = (ca, fa) if t < .5 else (cb, fb)
    for lp in src.features: c.feature_blob(f(lp))
    cr = c.finish(rng, plan, res)
    cr.genome = {'t': float(t), 'parents': [ca.plan, cb.plan]}
    return cr

# ---------------------------------------------------------------- previews

def sheet(rows, cell=150, label_h=16, path=None):
    """rows: list of (label, [Creature, ...]); white silhouettes on black with black engraving, like the drawings"""
    cols = max(len(r[1]) for r in rows); img = np.zeros((len(rows) * (cell + label_h), cols * cell, 3), np.uint8)
    for i, (label, crs) in enumerate(rows):
        y = i * (cell + label_h)
        cv2.putText(img, label, (4, y + 12), cv2.FONT_HERSHEY_SIMPLEX, .4, (130, 170, 230), 1, cv2.LINE_AA)
        for j, cr in enumerate(crs):
            if cr is None: continue
            s = cell - 14; o = np.array([j * cell + cell / 2, y + label_h + cell / 2])
            cv2.fillPoly(img, [np.round((cr.outline * s + o) * 16).astype(np.int32)], (255, 255, 255), cv2.LINE_AA, shift=4)
            for f in cr.features: cv2.fillPoly(img, [np.round((f * s + o) * 16).astype(np.int32)], (0, 0, 0), cv2.LINE_AA, shift=4)
            if cr.size_factor > 1.01:
                cv2.putText(img, f'{cr.size_factor:.2f}', (j * cell + 3, y + label_h + cell - 4), cv2.FONT_HERSHEY_SIMPLEX, .33, (90, 90, 90), 1, cv2.LINE_AA)
    if path is not None: cv2.imwrite(str(path), img)
    return img
