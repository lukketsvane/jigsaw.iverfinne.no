"""Target domains: rectangle, circle, or a closed path read from an SVG file. Always a shapely Polygon in mm."""
import math
import re
import xml.etree.ElementTree as ET

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

from . import SNAP


def snap_polygon(poly):
    """exterior ring on the SNAP grid, counter-clockwise, no repeated points"""
    xy = np.round(np.asarray(orient(poly, 1.0).exterior.coords)[:-1] / SNAP) * SNAP
    keep = np.r_[True, np.any(np.diff(xy, axis=0) != 0, axis=1)]
    xy = xy[keep]
    if len(xy) > 1 and np.all(xy[0] == xy[-1]): xy = xy[:-1]
    out = Polygon(xy)
    if not out.is_valid or out.geom_type != 'Polygon' or len(out.interiors):
        raise ValueError('outline must be one simple closed polygon without holes')
    return orient(out, 1.0)


def rectangle(width_mm, height_mm):
    return snap_polygon(Polygon([(0, 0), (width_mm, 0), (width_mm, height_mm), (0, height_mm)]))


def circle(diameter_mm, max_sagitta_mm=0.005):
    """regular polygon inscribed in the circle; the chord error (sagitta) is at most max_sagitta_mm"""
    r = diameter_mm / 2
    n = max(64, int(math.ceil(math.pi / math.acos(1 - max_sagitta_mm / r))))
    n += (-n) % 4
    a = np.arange(n) * 2 * math.pi / n
    return snap_polygon(Polygon(np.stack([r + r * np.cos(a), r + r * np.sin(a)], 1)))


def _flatten_path(d, steps=24):
    """absolute/relative M L H V C S Q Z path data -> list of closed rings (curves flattened with fixed steps)"""
    tok = re.findall(r'[MmLlHhVvCcSsQqZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', d)
    rings, cur, pos, start, i, cmd, prev_ctrl = [], [], np.zeros(2), np.zeros(2), 0, None, None

    def num(k):
        nonlocal i
        v = [float(t) for t in tok[i:i + k]]
        if len(v) != k: raise ValueError('truncated path data')
        i += k
        return v
    while i < len(tok):
        if re.match(r'[A-Za-z]', tok[i]): cmd = tok[i]; i += 1
        if cmd is None: raise ValueError('path data must start with a command')
        rel = cmd.islower(); c = cmd.upper(); base = pos if rel else np.zeros(2)
        if c == 'M':
            if cur: rings.append(cur)
            pos = base + num(2); start = pos.copy(); cur = [pos.copy()]; cmd = 'l' if rel else 'L'; prev_ctrl = None
        elif c == 'L': pos = base + num(2); cur.append(pos.copy()); prev_ctrl = None
        elif c == 'H': pos = np.array([(pos[0] if rel else 0) + num(1)[0], pos[1]]); cur.append(pos.copy()); prev_ctrl = None
        elif c == 'V': pos = np.array([pos[0], (pos[1] if rel else 0) + num(1)[0]]); cur.append(pos.copy()); prev_ctrl = None
        elif c in 'CS':
            if c == 'C': p1 = base + num(2)
            else: p1 = 2 * pos - prev_ctrl if prev_ctrl is not None else pos.copy()
            p2 = base + num(2); p3 = base + num(2); t = np.linspace(0, 1, steps + 1)[1:, None]
            cur += list((1 - t) ** 3 * pos + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t * t * p2 + t ** 3 * p3)
            prev_ctrl = p2; pos = p3
        elif c == 'Q':
            p1 = base + num(2); p2 = base + num(2); t = np.linspace(0, 1, steps + 1)[1:, None]
            cur += list((1 - t) ** 2 * pos + 2 * (1 - t) * t * p1 + t * t * p2); pos = p2; prev_ctrl = None
        elif c == 'Z':
            if cur: rings.append(cur)
            cur = []; pos = start.copy(); prev_ctrl = None
        else: raise ValueError(f'unsupported path command {cmd!r}')
    if cur: rings.append(cur)
    return [np.array(r) for r in rings if len(r) >= 3]


def from_svg(path, width_mm=None):
    """Largest closed shape of an SVG file (path / polygon / rect / circle / ellipse), scaled to width_mm if given.
    Element transforms are not applied; an outline file should contain plain geometry."""
    root = ET.parse(path).getroot()
    polys = []
    for el in root.iter():
        tag = el.tag.split('}')[-1]
        try:
            if tag == 'path': polys += [Polygon(r) for r in _flatten_path(el.attrib.get('d', ''))]
            elif tag == 'polygon':
                v = [float(t) for t in re.findall(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?', el.attrib.get('points', ''))]
                polys.append(Polygon(list(zip(v[::2], v[1::2]))))
            elif tag == 'rect':
                x, y, w, h = (float(el.attrib.get(k, 0)) for k in ('x', 'y', 'width', 'height'))
                polys.append(Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)]))
            elif tag in ('circle', 'ellipse'):
                cx, cy = float(el.attrib.get('cx', 0)), float(el.attrib.get('cy', 0))
                rx = float(el.attrib.get('r', el.attrib.get('rx', 0))); ry = float(el.attrib.get('r', el.attrib.get('ry', 0)))
                a = np.arange(720) * 2 * math.pi / 720
                polys.append(Polygon(np.stack([cx + rx * np.cos(a), cy + ry * np.sin(a)], 1)))
        except (ValueError, TypeError):
            continue
    polys = [p.buffer(0) for p in polys if len(p.exterior.coords) >= 4]
    polys = [max(shapely.get_parts(p), key=lambda q: q.area) for p in polys if not p.is_empty]
    if not polys: raise ValueError(f'no closed shape found in {path}')
    poly = max(polys, key=lambda p: p.area)
    poly = Polygon(poly.exterior)
    x0, y0, x1, y1 = poly.bounds
    s = (width_mm / (x1 - x0)) if width_mm else 1.0
    return snap_polygon(shapely.affinity.affine_transform(poly, [s, 0, 0, s, -x0 * s, -y0 * s]))


def inside_mask(poly, h, w, px):
    """pixels whose centre lies inside the outline (grid origin at the outline's bounding-box corner)"""
    x0, y0 = poly.bounds[:2]
    xx, yy = np.meshgrid(x0 + (np.arange(w) + .5) * px, y0 + (np.arange(h) + .5) * px)
    return shapely.contains_xy(poly, xx, yy)


def grid_shape(poly, px):
    x0, y0, x1, y1 = poly.bounds
    return int(math.ceil((y1 - y0) / px - 1e-9)), int(math.ceil((x1 - x0) / px - 1e-9))
