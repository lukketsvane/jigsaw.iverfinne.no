"""Target puzzle outlines in millimetres: rectangle, circle, organic Nordic rock slab, or SVG."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Tuple

import numpy as np
import shapely
import shapely.affinity
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

SNAP = 1e-4


def snap_polygon(poly: Polygon, snap: float = SNAP) -> Polygon:
    """Return a valid, CCW, hole-free Polygon snapped to the `snap` mm grid."""
    if poly.is_empty:
        raise ValueError("outline polygon is empty")
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.geom_type != "Polygon":
        parts = [p for p in shapely.get_parts(poly) if p.geom_type == "Polygon" and p.area > 0]
        if not parts:
            raise ValueError("outline does not contain a valid Polygon")
        poly = max(parts, key=lambda p: p.area)
    poly = orient(Polygon(poly.exterior), 1.0)
    xy = np.round(np.asarray(poly.exterior.coords)[:-1] / snap) * snap
    keep = np.r_[True, np.any(np.diff(xy, axis=0) != 0, axis=1)]
    xy = xy[keep]
    if len(xy) > 1 and np.all(xy[0] == xy[-1]):
        xy = xy[:-1]
    out = Polygon(xy)
    if not out.is_valid or out.geom_type != "Polygon" or len(out.interiors):
        raise ValueError("outline must be one simple closed polygon without holes")
    return orient(out, 1.0)


def rectangle(width_mm: float, height_mm: float) -> Polygon:
    """Axis-aligned rectangular puzzle outline in mm."""
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("width_mm and height_mm must be positive")
    return snap_polygon(
        Polygon([(0.0, 0.0), (width_mm, 0.0), (width_mm, height_mm), (0.0, height_mm)])
    )


def circle(diameter_mm: float, max_sagitta_mm: float = 0.05) -> Polygon:
    """Regular polygon inscribed in a circle of diameter `diameter_mm`."""
    if diameter_mm <= 0:
        raise ValueError("diameter_mm must be positive")
    r = diameter_mm / 2.0
    ratio = max(1e-7, min(0.5, max_sagitta_mm / r))
    n = max(64, min(360, int(math.ceil(math.pi / math.acos(1.0 - ratio)))))
    n += (-n) % 4
    a = np.arange(n, dtype=np.float64) * (2.0 * math.pi / n)
    pts = np.stack([r + r * np.cos(a), r + r * np.sin(a)], axis=1)
    return snap_polygon(Polygon(pts))


def organic_stone(
    width_mm: float = 250.0,
    height_mm: float | None = None,
    seed: int = 42,
    roughness: float = 0.065,
    n_verts: int = 160,
) -> Polygon:
    """Smooth organic Nordic rock-slab / boulder outline suited to helleristninger puzzles."""
    if height_mm is None:
        height_mm = width_mm * 0.84
    rng = np.random.default_rng(seed)
    theta = np.linspace(0.0, 2.0 * math.pi, n_verts, endpoint=False)
    rx = width_mm * 0.5
    ry = height_mm * 0.5

    # Low-frequency harmonic modulation so the boundary is smooth, natural, and laser-friendly.
    mod = np.ones_like(theta)
    for k, amp in ((2, 0.55), (3, 0.45), (4, 0.28), (5, 0.16)):
        phase = float(rng.uniform(0.0, 2.0 * math.pi))
        mod += roughness * amp * np.cos(k * theta + phase)

    xs = rx * mod * np.cos(theta)
    ys = ry * mod * np.sin(theta)
    poly = Polygon(np.stack([xs, ys], axis=1))
    x0, y0, x1, y1 = poly.bounds
    sx = width_mm / (x1 - x0)
    sy = height_mm / (y1 - y0)
    poly = shapely.affinity.affine_transform(poly, [sx, 0.0, 0.0, sy, -x0 * sx, -y0 * sy])
    return snap_polygon(poly)


def _flatten_svg_path(d: str, steps: int = 20) -> list[np.ndarray]:
    """Parse SVG path data (M/L/H/V/C/S/Q/Z) into a list of closed vertex rings."""
    tok = re.findall(r"[MmLlHhVvCcSsQqZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", d)
    rings: list[list[np.ndarray]] = []
    cur: list[np.ndarray] = []
    pos = np.zeros(2, dtype=np.float64)
    start = np.zeros(2, dtype=np.float64)
    i = 0
    cmd: str | None = None
    prev_ctrl: np.ndarray | None = None

    def num(k: int) -> np.ndarray:
        nonlocal i
        if i + k > len(tok):
            raise ValueError("truncated SVG path data")
        vals = np.array([float(t) for t in tok[i : i + k]], dtype=np.float64)
        i += k
        return vals

    while i < len(tok):
        if re.match(r"^[A-Za-z]$", tok[i]):
            cmd = tok[i]
            i += 1
        if cmd is None:
            raise ValueError("SVG path data must start with a command")
        rel = cmd.islower()
        c = cmd.upper()
        base = pos if rel else np.zeros(2, dtype=np.float64)

        if c == "M":
            if cur:
                rings.append(cur)
            pos = base + num(2)
            start = pos.copy()
            cur = [pos.copy()]
            cmd = "l" if rel else "L"
            prev_ctrl = None
        elif c == "L":
            pos = base + num(2)
            cur.append(pos.copy())
            prev_ctrl = None
        elif c == "H":
            x = (pos[0] if rel else 0.0) + float(num(1)[0])
            pos = np.array([x, pos[1]], dtype=np.float64)
            cur.append(pos.copy())
            prev_ctrl = None
        elif c == "V":
            y = (pos[1] if rel else 0.0) + float(num(1)[0])
            pos = np.array([pos[0], y], dtype=np.float64)
            cur.append(pos.copy())
            prev_ctrl = None
        elif c in ("C", "S"):
            if c == "C":
                p1 = base + num(2)
            else:
                p1 = 2.0 * pos - prev_ctrl if prev_ctrl is not None else pos.copy()
            p2 = base + num(2)
            p3 = base + num(2)
            t = np.linspace(0.0, 1.0, steps + 1)[1:, None]
            curve = (
                (1.0 - t) ** 3 * pos
                + 3.0 * (1.0 - t) ** 2 * t * p1
                + 3.0 * (1.0 - t) * t**2 * p2
                + t**3 * p3
            )
            cur.extend(list(curve))
            prev_ctrl = p2
            pos = p3
        elif c == "Q":
            p1 = base + num(2)
            p2 = base + num(2)
            t = np.linspace(0.0, 1.0, steps + 1)[1:, None]
            curve = (1.0 - t) ** 2 * pos + 2.0 * (1.0 - t) * t * p1 + t**2 * p2
            cur.extend(list(curve))
            pos = p2
            prev_ctrl = None
        elif c == "Z":
            if cur:
                rings.append(cur)
            cur = []
            pos = start.copy()
            prev_ctrl = None
        else:
            raise ValueError(f"unsupported SVG path command {cmd!r}")

    if cur:
        rings.append(cur)
    return [np.asarray(r, dtype=np.float64) for r in rings if len(r) >= 3]


def from_svg(path: str | Path, width_mm: float | None = None) -> Polygon:
    """Load the largest closed shape from an SVG file as a mm-scale Polygon."""
    root = ET.parse(str(path)).getroot()
    polys: list[Polygon] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        try:
            if tag == "path":
                for ring in _flatten_svg_path(el.attrib.get("d", "")):
                    polys.append(Polygon(ring))
            elif tag == "polygon":
                nums = [
                    float(t)
                    for t in re.findall(
                        r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?",
                        el.attrib.get("points", ""),
                    )
                ]
                if len(nums) >= 6:
                    polys.append(Polygon(list(zip(nums[0::2], nums[1::2]))))
            elif tag == "rect":
                x = float(el.attrib.get("x", 0.0))
                y = float(el.attrib.get("y", 0.0))
                w = float(el.attrib.get("width", 0.0))
                h = float(el.attrib.get("height", 0.0))
                if w > 0 and h > 0:
                    polys.append(Polygon([(x, y), (x + w, y), (x + w, y + h), (x, y + h)]))
            elif tag in ("circle", "ellipse"):
                cx = float(el.attrib.get("cx", 0.0))
                cy = float(el.attrib.get("cy", 0.0))
                rx = float(el.attrib.get("r", el.attrib.get("rx", 0.0)))
                ry = float(el.attrib.get("r", el.attrib.get("ry", 0.0)))
                if rx > 0 and ry > 0:
                    a = np.arange(180, dtype=np.float64) * (2.0 * math.pi / 180.0)
                    polys.append(Polygon(np.stack([cx + rx * np.cos(a), cy + ry * np.sin(a)], 1)))
        except (ValueError, TypeError):
            continue

    cleaned: list[Polygon] = []
    for p in polys:
        if len(p.exterior.coords) < 4:
            continue
        p = p.buffer(0)
        for part in shapely.get_parts(p):
            if part.geom_type == "Polygon" and part.area > 0:
                cleaned.append(Polygon(part.exterior))
    if not cleaned:
        raise ValueError(f"no closed shape found in SVG: {path}")

    poly = max(cleaned, key=lambda p: p.area)
    x0, y0, x1, y1 = poly.bounds
    scale = (width_mm / (x1 - x0)) if width_mm else 1.0
    if width_mm is not None:
        poly = shapely.affinity.affine_transform(
            poly, [scale, 0.0, 0.0, scale, -x0 * scale, -y0 * scale]
        )
    return snap_polygon(poly)


def grid_shape(poly: Polygon, px: float) -> Tuple[int, int]:
    """Return `(H, W)` grid dimensions covering `poly` at resolution `px` mm/pixel."""
    x0, y0, x1, y1 = poly.bounds
    h = int(math.ceil((y1 - y0) / px - 1e-9))
    w = int(math.ceil((x1 - x0) / px - 1e-9))
    return max(1, h), max(1, w)


def inside_mask(poly: Polygon, h: int, w: int, px: float) -> np.ndarray:
    """Boolean `(H, W)` mask of pixel centres strictly inside `poly`."""
    x0, y0 = poly.bounds[:2]
    xx, yy = np.meshgrid(
        x0 + (np.arange(w, dtype=np.float64) + 0.5) * px,
        y0 + (np.arange(h, dtype=np.float64) + 0.5) * px,
    )
    return np.asarray(shapely.contains_xy(poly, xx, yy), dtype=bool)
