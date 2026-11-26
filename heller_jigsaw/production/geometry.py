import math

import cv2
import numpy as np
from scipy.interpolate import CubicSpline
from shapely.geometry import Polygon


def disk(r):
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r


def rotated(mask, angle, area):
    yy, xx = np.where(mask > 0.5)
    crop = mask[yy.min():yy.max() + 1, xx.min():xx.max() + 1]
    scale = math.sqrt(area / (crop > 0.5).sum())
    w, h = (max(2, round(crop.shape[1] * scale)), max(2, round(crop.shape[0] * scale)))
    c = cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)
    side = math.ceil(math.hypot(w, h)) + 6
    padded = np.zeros((side, side), np.float32)
    y = (side - h) // 2
    x = (side - w) // 2
    padded[y:y + h, x:x + w] = c
    rot = cv2.warpAffine(padded, cv2.getRotationMatrix2D(((side - 1) / 2, (side - 1) / 2), angle, 1), (side, side))
    m = rot > 0.5
    yy, xx = np.where(m)
    return m[yy.min():yy.max() + 1, xx.min():xx.max() + 1]


def face_from_edges(face, edges):
    ring = []
    for eid, rev in face['edge_refs']:
        pts = edges[eid]['coords']
        if rev:
            pts = pts[::-1]
        if ring and ring[-1] != pts[0]:
            raise ValueError('edge refs not joined')
        ring.extend(pts if not ring else pts[1:])
    return Polygon(ring)


def tab_curve(line, frac, sign, size):
    xy = np.asarray(line.coords)
    cumul = np.r_[0, np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
    mid = line.length * frac
    lo, hi = (mid - 6 * size, mid + 6 * size)
    if lo < 2 or hi > line.length - 2:
        return None
    p = np.array(line.interpolate(lo).coords[0])
    q = np.array(line.interpolate(hi).coords[0])
    L = np.linalg.norm(q - p)
    if L < 10 * size:
        return None
    t = (q - p) / L
    n = np.array([-t[1], t[0]]) * sign
    pts = np.array([[-6, 0], [-4.2, 0.2], [-3.2, 1.6], [-3.1, 3.4], [-5.6, 4.7], [-7, 7.1], [-6.02, 10.2], [0, 12.5], [6.02, 10.2], [7, 7.1], [5.6, 4.7], [3.1, 3.4], [3.2, 1.6], [4.2, 0.2], [6, 0]])
    pts[:, 0] *= L / 12
    pts[:, 1] *= size
    u = np.r_[0, np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))]
    curve = CubicSpline(u, pts, axis=0)(np.linspace(0, u[-1], 45))
    curve = (p + q) / 2 + curve[:, 0, None] * t + curve[:, 1, None] * n
    curve[0] = p
    curve[-1] = q
    out = np.round(np.concatenate([xy[cumul < lo], curve, xy[cumul > hi]]), 4)
    keep = np.r_[True, np.linalg.norm(np.diff(out, axis=0), axis=1) > 0.03]
    out = out[keep]
    out[-1] = xy[-1]
    return out.tolist()


def pair_margin(edges, n, a, b):
    th = np.arange(360) * math.pi / 180
    dirs = np.stack([np.cos(th), np.sin(th)], axis=1)
    proj = np.zeros(360)
    for e in edges:
        if {e['left_face'], e['right_face']} != {a, b}:
            continue
        delta = np.diff(np.array(e['coords']), axis=0)
        nr = delta[:, [1, 0]] * np.array([1, -1])
        if e['left_face'] != a:
            nr = -nr
        proj += np.maximum(nr @ dirs.T, 0).sum(axis=0)
    return float(proj.min())


def target_polygon(m, pose, x0, y0, px):
    mask = (m.mask > 0.5).astype('uint8')
    if pose.flip_x:
        mask = mask[:, ::-1].copy()
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    xy = max(cs, key=cv2.contourArea).reshape(-1, 2).astype(float) - 127.5
    a = math.radians(pose.angle_deg)
    xy = xy @ np.array([[math.cos(a), math.sin(a)], [-math.sin(a), math.cos(a)]]) * (pose.scale / 256)
    xy = (xy + np.array([pose.cx + 0.5, pose.cy + 0.5])) * px + np.array([x0, y0])
    return Polygon(xy).buffer(0).simplify(0.18, preserve_topology=True)
