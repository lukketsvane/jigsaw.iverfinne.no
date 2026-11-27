"""Extract the 183 piece outlines (mm, original assembled position) into a compact JSON for the live field + atlas."""
import json, re

import os
B = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(B, '..', 'public')
D = os.environ.get('SIP_SRC', os.path.join(B, '..', '..', '..', 'runs', 'sound-in-pieces'))  # private sources: renders, cut field, GLB
SVG = os.path.join(D, 'scrollzip', '05-cut-field.svg')
TOL = 0.08  # mm, Douglas-Peucker tolerance; display is ~0.25 mm/px at most


def rdp(pts, eps):
    if len(pts) < 3:
        return pts
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    n = (dx * dx + dy * dy) ** 0.5 or 1e-9
    i, dmax = 0, 0.0
    for k in range(1, len(pts) - 1):
        x, y = pts[k]
        d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / n
        if d > dmax:
            i, dmax = k, d
    if dmax > eps:
        return rdp(pts[:i + 1], eps)[:-1] + rdp(pts[i:], eps)
    return [pts[0], pts[-1]]


def simplify_ring(pts):
    # split the closed ring at the point farthest from the start so both halves keep their shape
    far = max(range(len(pts)), key=lambda k: (pts[k][0] - pts[0][0]) ** 2 + (pts[k][1] - pts[0][1]) ** 2)
    a = rdp(pts[:far + 1], TOL)
    b = rdp(pts[far:] + [pts[0]], TOL)
    return a[:-1] + b[:-1]


def area_centroid(p):
    A = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(p, p[1:] + p[:1]):
        c = x0 * y1 - x1 * y0
        A += c; cx += (x0 + x1) * c; cy += (y0 + y1) * c
    A /= 2
    return abs(A), cx / (6 * A), cy / (6 * A)


def fmt(v):
    s = f'{v:.1f}'.rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


svg = open(SVG).read()
out, raw_pts, kept_pts = [], 0, 0
for pid, d in re.findall(r'<path id="piece-(\d+)" d="([^"]+)"', svg):
    nums = [float(v) for v in re.findall(r'-?\d+\.?\d*', d)]
    pts = list(zip(nums[0::2], nums[1::2]))
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    raw_pts += len(pts)
    area, cx, cy = area_centroid(pts)
    s = simplify_ring(pts)
    kept_pts += len(s)
    xs, ys = [p[0] for p in s], [p[1] for p in s]
    # relative path: M x y l dx dy ... z, in 0.01 mm steps
    q = [(round(x * 10), round(y * 10)) for x, y in s]
    parts = [f'M{fmt(q[0][0] / 10)} {fmt(q[0][1] / 10)}l']
    rel = []
    for (ax, ay), (bx, by) in zip(q, q[1:]):
        rel.append(f'{fmt((bx - ax) / 10)} {fmt((by - ay) / 10)}')
    path = parts[0] + ' '.join(rel).replace(' -', '-') + 'z'
    out.append({'n': pid, 'd': path, 'a': round(area), 'c': [round(cx, 1), round(cy, 1)],
                'b': [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]})

json.dump(out, open(os.path.join(SITE, 'data', 'pieces.json'), 'w'), separators=(',', ':'))
areas = sorted(p['a'] for p in out)
print(len(out), 'pieces; points', raw_pts, '->', kept_pts, '; area', areas[0], '-', areas[-1], 'sum', sum(areas))
print({p['n']: p['a'] for p in out if p['n'] in ('093', '046', '131')})
print('max bbox', max(max(p['b'][2] - p['b'][0], p['b'][3] - p['b'][1]) for p in out))
