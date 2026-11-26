"""Generate a playable whole-piece lizard torus, laser files, and full fabrication audits."""

import argparse
import base64
from collections import defaultdict
import io
import json
import math
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
import shapely
from shapely import get_parts, polygonize
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from .lizard_net import ElasticWarp, make_warp, training_report
from .lizard_tile import EYE_RADIUS, make_tiles


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
# Use integer ticks so wrapped paths, game outlines and the exported cuts share vertices.
TICKS = 10000
DEFAULT_WIDTH = 500.0
MIN_OUTLINE_IOU = .75
# Smooth rainbow palette matching the birch-wood gecko reference photos (teal -> green -> gold -> orange -> coral).
PALETTE = np.array([
    [0, 196, 154], [28, 189, 92], [116, 186, 40], [186, 192, 34],
    [242, 200, 34], [254, 158, 28], [248, 106, 30], [234, 62, 42],
], dtype=float)
SVG_NS = "http://www.w3.org/2000/svg"
KERF_WIDTHS = (0.10, 0.15, 0.20, 0.25)


def _import_audit_helpers():
    """Import read-only geometry/lock/fabrication helpers from pathnet and heller_jigsaw."""
    for folder in (str(ROOT), str(HERE)):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    import audit_field as af
    import field as fl
    from heller_jigsaw.geometry.validate import analyze_piece_fabrication
    from heller_jigsaw.production.audit import measure, motion_grid
    return af, fl, analyze_piece_fabrication, measure, motion_grid


def _rainbow_phase(seed):
    return np.random.default_rng(seed).uniform(0, 2 * np.pi, 2)


def rainbow_field_t(points, periods, seed):
    """Evaluate a smooth, periodic 2D rainbow coordinate in [0, 1] across the torus."""
    pts = np.asarray(points, dtype=float)
    shape = pts.shape[:-1]
    uv = pts.reshape(-1, 2) @ np.linalg.inv(periods)
    phase = _rainbow_phase(seed)
    x = 2 * np.pi * uv[:, 0] + phase[0]
    y = 2 * np.pi * uv[:, 1] + phase[1]
    field = .61 * np.sin(x + .85 * np.sin(y)) + .33 * np.cos(y + .55 * np.sin(x)) + .09 * np.sin(2 * x - y)
    return np.clip((field + 1.0) * 0.5, 0.0, 1.0).reshape(shape)


def palette_rgb(t):
    """Smoothly interpolate RGB colours (0..255 float) for t in [0, 1]."""
    scaled = np.clip(np.asarray(t, dtype=float), 0.0, 1.0) * (len(PALETTE) - 1)
    lo = np.floor(scaled).astype(int)
    hi = np.minimum(lo + 1, len(PALETTE) - 1)
    frac = (scaled - lo)[..., None]
    return PALETTE[lo] + (PALETTE[hi] - PALETTE[lo]) * frac


def _hex(rgb):
    r, g, b = np.rint(np.clip(rgb, 0, 255)).astype(int)
    return f"#{r:02x}{g:02x}{b:02x}"


def color_field(centers, periods, seed):
    """Assign each piece a distinct solid hex ink sampled from the periodic rainbow field."""
    rng = np.random.default_rng(seed)
    # Advance rng by the two phase draws so the perturbation stream stays deterministic.
    rng.uniform(0, 2 * np.pi, 2)
    rgbs = np.rint(palette_rgb(rainbow_field_t(centers, periods, seed))).astype(int)
    used, colors = set(), []
    for rgb in rgbs:
        rgb = rgb.copy()
        # Give each solid ink its own RGB value, even in the flattest colour bands.
        while tuple(rgb) in used:
            rgb = np.clip(rgb + rng.integers(-3, 4, 3), 0, 255)
        used.add(tuple(rgb))
        colors.append("#%02x%02x%02x" % tuple(rgb))
    return colors


def piece_gradient(piece, periods, seed):
    """Fit a local linear SVG gradient matching the continuous board rainbow across one piece."""
    pts = np.asarray(piece["outline"], dtype=float)
    center = np.asarray(piece["center"], dtype=float)
    eps = max(0.25, float(np.linalg.norm(pts.max(0) - pts.min(0))) * 0.02)
    t_vals = rainbow_field_t(
        np.array([center + [eps, 0], center - [eps, 0], center + [0, eps], center - [0, eps]]),
        periods, seed,
    )
    grad = np.array([(t_vals[0] - t_vals[1]) / (2 * eps), (t_vals[2] - t_vals[3]) / (2 * eps)])
    norm = float(np.linalg.norm(grad))
    axis = grad / norm if norm > 1e-8 else np.array([1.0, 0.0])
    proj = (pts - center) @ axis
    p0 = center + axis * float(proj.min())
    p1 = center + axis * float(proj.max())
    samples = np.array([p0, 0.5 * (p0 + p1), p1])
    stops = [_hex(rgb) for rgb in palette_rgb(rainbow_field_t(samples, periods, seed))]
    return dict(
        x1=round(float(p0[0]), 4), y1=round(float(p0[1]), 4),
        x2=round(float(p1[0]), 4), y2=round(float(p1[1]), 4),
        stops=stops,
    )


def path_key(line):
    raw, reverse = line.tobytes(), line[::-1].tobytes()
    return min(raw, reverse)


def outline_iou(template, contour):
    """Compare corresponding outlines allowing rotation and at most 1.5:1 stretch."""
    a = template - template.mean(axis=0)
    b = contour - contour.mean(axis=0)
    transform = np.linalg.lstsq(a, b, rcond=None)[0]
    left, scales, right = np.linalg.svd(transform)
    if np.linalg.det(transform) <= 0:
        return 0.
    ratio = math.sqrt(min(scales[0] / scales[1], 1.5))
    scales = math.sqrt(np.prod(scales)) * np.array([ratio, 1/ratio])
    ref, actual = Polygon(a @ ((left * scales) @ right)), Polygon(b)
    intersection = ref.intersection(actual).area
    return intersection / (ref.area + actual.area - intersection)


def build(seed=387, cols=9, rows=4, width=DEFAULT_WIDTH):
    if not math.isfinite(width) or width < 1 or cols < 1 or rows < 1 or seed < 0:
        raise ValueError("Use positive cell counts, a width of at least 1 mm and a nonnegative seed.")
    units, tiles = make_tiles(cols, rows)
    warp = make_warp(seed, *units, piece_count=len(tiles))
    # A few extreme elastic fields need less deformation to keep every lizard legible.
    # Use the same latent draw and taper the whole field so the neighbours still agree.
    for attempt in range(8):
        data, cuts, report = assemble(seed, width, units, tiles, warp)
        if report["min_outline_iou"] >= MIN_OUTLINE_IOU:
            report["deformation_scale"] = .8 ** attempt
            return data, cuts, report
        warp = ElasticWarp(units, warp.controls * .8)
    raise ValueError("No deformation met the outline fidelity threshold")


def assemble(seed, width, units, tiles, warp):
    # The rectangular repeat is rounded first, so period lifts are exact in SVG precision.
    lengths = np.diag(units)
    period_ticks = np.rint(lengths * (width / lengths[0]) * TICKS).astype(np.int64)
    periods = np.diag(period_ticks / TICKS)

    def ticks(points):
        # Evaluate translated and rotated copies at identical inputs to the model,
        # even when a coordinate falls exactly on a rounding boundary in the SVG.
        normalized = np.rint(np.asarray(points) / lengths * 1.e10).astype(np.int64)
        lift, local = np.divmod(normalized, 10**10)
        warped = warp(local * (lengths / 1.e10))
        return lift * period_ticks + np.rint(warped / lengths * period_ticks).astype(np.int64)

    joins, cuts, pieces, polygons = defaultdict(list), {}, [], []
    outline_scores = []
    eye_clearances, spine_clearances = [], []
    # Index both lifts of every torus seam. Keep each physical cut once in the plane.
    for index, tile in enumerate(tiles):
        curves = [*tile.edges, tile.eyes, tile.spine]
        transformed = np.split(ticks(np.concatenate(curves)), np.cumsum([len(c) for c in curves[:-1]]))
        edges, eyes, spine = transformed[:-2], transformed[-2] / TICKS, transformed[-1] / TICKS
        if any(not np.array_equal(a[-1], b[0]) for a, b in zip(edges, edges[1:] + edges[:1])):
            raise ValueError(f"Open outline at piece {index}")
        outline = np.concatenate([edge[:-1] for edge in edges]) / TICKS
        poly = Polygon(outline)
        if not poly.is_valid or poly.is_empty or poly.area == 0:
            raise ValueError(f"Invalid outline at piece {index}")
        outline_scores.append(outline_iou(tile.outline, outline))
        polygons.append(poly)
        for edge in edges:
            key = path_key(edge % period_ticks)
            joins[key].append((index, edge))
            cuts.setdefault(path_key(edge), edge)
        eye_radius = round(EYE_RADIUS * width / lengths[0], 4)
        if not all(poly.covers(Point(p).buffer(eye_radius)) for p in eyes):
            raise ValueError(f"Eye leaves piece {index}")
        spine_line = LineString(spine)
        if not poly.covers(spine_line.buffer(eye_radius * .3)):
            raise ValueError(f"Centreline leaves piece {index}")
        eye_clearances.extend(float(poly.exterior.distance(Point(p)) - eye_radius) for p in eyes)
        spine_clearances.append(float(poly.exterior.distance(spine_line)))
        # The centre of mass is also the anchor for rotating or moving a game piece.
        pieces.append(dict(id=index, center=np.round(poly.centroid.coords[0], 4).tolist(),
                           outline=outline.tolist(), spine=spine.tolist(), eyes=eyes.tolist(),
                           eyeRadius=eye_radius, neighbors=[]))

    wrapping = np.zeros(2, dtype=int)
    seam_lengths = []
    for pair in joins.values():
        if len(pair) != 2:
            raise ValueError("A periodic seam does not have exactly two owners")
        (a, left), (b, right) = pair
        offset = left[0] - right[-1]
        if np.any(offset % period_ticks) or not np.array_equal(left, right[::-1] + offset):
            raise ValueError(f"Seam mismatch between pieces {a} and {b}")
        wrapping += offset != 0
        seam_lengths.append(np.linalg.norm(np.diff(left / TICKS, axis=0), axis=1).sum())
        pieces[a]["neighbors"].append(dict(id=b, offset=(offset / TICKS).tolist()))
        pieces[b]["neighbors"].append(dict(id=a, offset=(-offset / TICKS).tolist()))

    # Simple faces with opposing paired boundaries have constant coverage on the torus.
    # Total area equal to one fundamental domain then fixes that coverage at exactly one.
    total_area = sum(p.area for p in polygons)
    area_error = abs(total_area - float(np.prod(period_ticks / TICKS)))
    consistent_winding = len({p.exterior.is_ccw for p in polygons}) == 1
    if not consistent_winding or area_error > 1.e-6 * max(1., width) or not all(wrapping):
        raise ValueError(f"Invalid periodic cover (area error {area_error:.8f} mm²)")
    if len({round(p.area, 8) for p in polygons}) != len(pieces):
        raise ValueError("Two pieces have indistinguishable areas at export precision")
    # Different arc lengths preclude exact rigid matches between unrelated seams.
    if np.min(np.diff(np.sort(seam_lengths))) < 1.e-10:
        raise ValueError("Two unrelated seams have indistinguishable lengths")
    for p, color in zip(pieces, color_field([p["center"] for p in pieces], periods, seed)):
        p["color"] = color
        p["gradient"] = piece_gradient(p, periods, seed)
    low, high = bounds(pieces)
    board_w, board_h = (high - low + 2.0).tolist()
    data = dict(seed=seed, periods=periods.tolist(), pieces=pieces)
    report = dict(seed=seed, pieces=len(pieces), periods_mm=periods.tolist(),
                  cut_svg_size_mm=[round(board_w, 4), round(board_h, 4)],
                  different_piece_areas=len(pieces), different_solid_colors=len(pieces),
                  torus_seams=len(joins), different_seam_lengths=len(seam_lengths),
                  seams_crossing_x=int(wrapping[0]),
                  seams_crossing_y=int(wrapping[1]), physical_cut_paths=len(cuts),
                  torus_area_error_mm2=area_error, min_area_mm2=min(p.area for p in polygons),
                  median_area_mm2=float(np.median([p.area for p in polygons])),
                  max_area_mm2=max(p.area for p in polygons),
                  min_outline_iou=min(outline_scores), median_outline_iou=float(np.median(outline_scores)),
                  min_eye_clearance_mm=round(min(eye_clearances), 4),
                  min_spine_clearance_mm=round(min(spine_clearances), 4),
                  warp_jacobian_lower_bound=float(warp.jacobian_lower_bound))
    return data, [line / TICKS for line in cuts.values()], report


def path_data(points, closed=False):
    return "M" + " ".join(f"{x:.4f},{y:.4f}" for x, y in points) + ("Z" if closed else "")


def bounds(pieces):
    points = np.concatenate([p["outline"] for p in pieces])
    return points.min(axis=0), points.max(axis=0)


def _engrave_elements(pieces):
    lines = []
    for p in pieces:
        lines.append(
            f'<path id="spine-{p["id"]}" class="spine" fill="none" stroke="#3a2812" '
            f'stroke-width="{p["eyeRadius"] * .58:.4f}" stroke-linecap="round" '
            f'stroke-linejoin="round" d="{path_data(p["spine"])}"/>'
        )
        for j, (ex, ey) in enumerate(p["eyes"]):
            lines.append(
                f'<circle id="eye-{p["id"]}-{j}" class="eye" cx="{ex:.4f}" cy="{ey:.4f}" '
                f'r="{p["eyeRadius"]:.4f}" fill="#2b1e0e"/>'
            )
    return lines


def _rainbow_png_data_uri(x, y, w, h, periods, seed, max_dim=1400):
    scale = max_dim / max(w, h)
    pw, ph = max(32, round(w * scale)), max(32, round(h * scale))
    xs = x + (np.arange(pw) + 0.5) * (w / pw)
    ys = y + (np.arange(ph) + 0.5) * (h / ph)
    gx, gy = np.meshgrid(xs, ys)
    grid = np.stack((gx, gy), axis=-1)
    rgb = np.rint(palette_rgb(rainbow_field_t(grid, np.asarray(periods), seed))).clip(0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def svg_files(data, cuts, directory, color_mode="gradient"):
    if color_mode not in ("gradient", "solid"):
        raise ValueError("color_mode must be 'gradient' or 'solid'")
    low, high = bounds(data["pieces"])
    x, y = low - 1
    w, h = high - low + 2
    header = (f'<svg xmlns="{SVG_NS}" width="{w:.4f}mm" height="{h:.4f}mm" '
              f'viewBox="{x:.4f} {y:.4f} {w:.4f} {h:.4f}">')
    cut_paths = [f'<path d="{path_data(edge)}"/>' for edge in cuts]
    engrave_elems = _engrave_elements(data["pieces"])

    # 1. Pure cut SVG (red hairline paths only; first child <g> directly holds <path>s).
    (directory / "cut.svg").write_text("\n".join([
        header,
        '<g id="cut" fill="none" stroke="red" stroke-width=".01">',
        *cut_paths,
        '</g>',
        '</svg>',
    ]), encoding="utf-8")

    # 2. Pure engraving SVG (eyes + dorsal spines only, registered 1:1 mm with cut.svg).
    (directory / "engrave.svg").write_text("\n".join([
        header,
        '<g id="engrave">',
        *engrave_elems,
        '</g>',
        '</svg>',
    ]), encoding="utf-8")

    # 3. Combined dual-layer laser production SVG (#engrave + #cut).
    (directory / "production.svg").write_text("\n".join([
        header,
        '<g id="engrave">',
        *engrave_elems,
        '</g>',
        '<g id="cut" fill="none" stroke="red" stroke-width=".01">',
        *cut_paths,
        '</g>',
        '</svg>',
    ]), encoding="utf-8")

    # 4. Print / visual artwork SVG (smooth continuous rainbow gradient or solid per-piece inks).
    if color_mode == "gradient":
        data_uri = _rainbow_png_data_uri(x, y, w, h, data["periods"], data["seed"])
        eye_r = data["pieces"][0]["eyeRadius"]
        clip_paths = [f'<path d="{path_data(p["outline"], True)}"/>' for p in data["pieces"]]
        art = [
            header,
            '<defs>',
            '<clipPath id="board-silhouette">',
            *clip_paths,
            '</clipPath>',
            '</defs>',
            f'<g id="rainbow-artwork" clip-path="url(#board-silhouette)">',
            f'<image x="{x:.4f}" y="{y:.4f}" width="{w:.4f}" height="{h:.4f}" preserveAspectRatio="none" href="{data_uri}"/>',
            '</g>',
            '<g id="engrave-overlay">',
            *engrave_elems,
            '</g>',
            f'<g id="cut-overlay" fill="none" stroke="#4f3618" stroke-opacity="0.82" stroke-width="{eye_r * .42:.4f}" stroke-linecap="round" stroke-linejoin="round">',
            *cut_paths,
            '</g>',
            '</svg>',
        ]
    else:
        art = [header]
        for p in data["pieces"]:
            art.append(f'<g id="lizard-{p["id"]}"><path fill="{p["color"]}" d="{path_data(p["outline"], True)}"/>')
            art.append(f'<path fill="none" stroke="#554020" stroke-width="{p["eyeRadius"]*.6:.4f}" '
                       f'stroke-linecap="round" stroke-linejoin="round" d="{path_data(p["spine"])}"/>')
            for ex, ey in p["eyes"]:
                art.append(f'<circle cx="{ex:.4f}" cy="{ey:.4f}" r="{p["eyeRadius"]:.4f}" fill="#43341e"/>')
            art.append('</g>')
        art.append('</svg>')
    (directory / "artwork.svg").write_text("\n".join(art), encoding="utf-8")


def _parse_paths(elements):
    lines = []
    for elem in elements:
        d = elem.attrib["d"].strip()
        if not d.startswith("M"):
            raise ValueError("Cut path must start with M")
        pts = np.fromstring(d[1:].replace("Z", "").replace(",", " "), sep=" ").reshape(-1, 2)
        lines.append(LineString(pts))
    return lines


def verify_cut(file, pieces):
    # Re-import the actual file. A duplicated, displaced, contaminated or missing seam must fail here.
    root = ET.parse(file).getroot()
    if root.findall(f".//{{{SVG_NS}}}text") or root.findall(f".//{{{SVG_NS}}}circle"):
        raise ValueError("Cut SVG must not contain text or engraving circles")
    paths = root.findall(f".//{{{SVG_NS}}}path")
    lines = _parse_paths(paths)
    if len({path_key(np.asarray(line.coords)) for line in lines}) != len(lines):
        raise ValueError("Duplicate path in the cut SVG")
    faces = list(get_parts(polygonize(lines)))
    # GEOS normalize removes differences in starting vertex and winding.
    expected = {Polygon(p["outline"]).normalize().wkb for p in pieces}
    if {face.normalize().wkb for face in faces} != expected or len(faces) != len(pieces):
        raise ValueError(f"Cut SVG polygonizes to {len(faces)} faces for {len(pieces)} pieces")
    return len(faces)


def verify_engrave(file, pieces, kerf_mm=0.25):
    """Re-import engrave.svg and confirm every spine and eye stays strictly inside its kerf-eroded lizard."""
    root = ET.parse(file).getroot()
    spines = root.findall(f".//{{{SVG_NS}}}path")
    eyes = root.findall(f".//{{{SVG_NS}}}circle")
    if len(spines) != len(pieces) or len(eyes) != 2 * len(pieces):
        raise ValueError(f"Expected {len(pieces)} spines and {2 * len(pieces)} eyes, got {len(spines)} and {len(eyes)}")
    eroded = {p["id"]: Polygon(p["outline"]).buffer(-kerf_mm * 0.5) for p in pieces}
    raw = {p["id"]: Polygon(p["outline"]) for p in pieces}
    min_spine_clear = float("inf")
    min_eye_clear = float("inf")
    for elem in spines:
        pid = int(elem.attrib["id"].split("-")[1])
        line = _parse_paths([elem])[0]
        if not eroded[pid].covers(line):
            raise ValueError(f"Engraved spine leaves kerf-eroded piece {pid}")
        min_spine_clear = min(min_spine_clear, float(raw[pid].exterior.distance(line)))
    for elem in eyes:
        pid = int(elem.attrib["id"].split("-")[1])
        cx, cy, r = float(elem.attrib["cx"]), float(elem.attrib["cy"]), float(elem.attrib["r"])
        pt = Point(cx, cy)
        if not eroded[pid].covers(pt.buffer(r)):
            raise ValueError(f"Engraved eye leaves kerf-eroded piece {pid}")
        min_eye_clear = min(min_eye_clear, float(raw[pid].exterior.distance(pt) - r))
    return dict(
        verified_spines=len(spines),
        verified_eyes=len(eyes),
        kerf_checked_mm=kerf_mm,
        min_spine_clearance_mm=round(min_spine_clear, 4),
        min_eye_clearance_mm=round(min_eye_clear, 4),
    )


def verify_production_svg(file, pieces, kerf_mm=0.25):
    """Re-import dual-layer production.svg and validate both #cut and #engrave layers."""
    root = ET.parse(file).getroot()
    groups = {g.attrib.get("id"): g for g in root.findall(f"./{{{SVG_NS}}}g")}
    if "cut" not in groups or "engrave" not in groups:
        raise ValueError("production.svg must contain top-level #engrave and #cut layers")
    cut_lines = _parse_paths(groups["cut"].findall(f".//{{{SVG_NS}}}path"))
    if len({path_key(np.asarray(line.coords)) for line in cut_lines}) != len(cut_lines):
        raise ValueError("Duplicate path in production.svg #cut layer")
    faces = list(get_parts(polygonize(cut_lines)))
    expected = {Polygon(p["outline"]).normalize().wkb for p in pieces}
    if {face.normalize().wkb for face in faces} != expected or len(faces) != len(pieces):
        raise ValueError(f"production.svg #cut polygonizes to {len(faces)} faces for {len(pieces)} pieces")
    spines = groups["engrave"].findall(f".//{{{SVG_NS}}}path")
    eyes = groups["engrave"].findall(f".//{{{SVG_NS}}}circle")
    if len(spines) != len(pieces) or len(eyes) != 2 * len(pieces):
        raise ValueError("production.svg #engrave layer element count mismatch")
    return dict(verified_cut_faces=len(faces), verified_spines=len(spines), verified_eyes=len(eyes))


def _min_wood_width(poly):
    """Exact minimum feature width (mm) before inward erosion splits a piece into >1 part."""
    lo, hi = 0.0, 8.0
    for _ in range(16):
        mid = 0.5 * (lo + hi)
        eroded = poly.buffer(-mid)
        if not eroded.is_empty and eroded.geom_type == "Polygon":
            lo = mid
        else:
            hi = mid
    return lo * 2.0


def _finite_translation_audit(data, raw_polys, kerf=0.25, ndir=360, workers=None):
    """Exact finite-motion translation screen (matches heller_jigsaw.production.audit.translation_audit)."""
    t0 = time.perf_counter()
    polys = [p.buffer(-kerf * 0.5, quad_segs=8) for p in raw_polys]
    if any(q.geom_type != "Polygon" or not q.is_valid for q in polys):
        raise ValueError("Kerf breaks a piece")
    for p in polys:
        shapely.prepare(p)
    n = len(polys)
    pairs = sorted({
        tuple(sorted([p["id"], nb["id"]]))
        for p in data["pieces"]
        for nb in p["neighbors"]
        if nb["offset"] == [0.0, 0.0]
    })
    theta = np.arange(ndir) * 2 * np.pi / ndir
    dirs = np.stack([np.cos(theta), np.sin(theta)], axis=1)
    distances = np.array([0.5, 1.0, 2.0, 3.0])

    def eval_pair(pair):
        a, b = pair
        pa, pb = polys[a], polys[b]
        alive = np.ones(ndir, dtype=bool)
        acc = np.zeros(ndir, dtype=bool)
        out = np.zeros((ndir, len(distances)), dtype=bool)
        for k, dist in enumerate(distances):
            idxs = np.flatnonzero(alive)
            if len(idxs):
                offs = dirs[idxs] * dist
                moving = np.full(len(idxs), pa, dtype=object)
                xy, gi = shapely.get_coordinates(moving, return_index=True)
                moving = shapely.set_coordinates(moving, xy + offs[gi])
                inter = shapely.intersects(moving, pb)
                if inter.any():
                    hit_local = np.zeros(len(idxs), dtype=bool)
                    hit_local[inter] = shapely.area(shapely.intersection(moving[inter], pb)) > 0.01
                    acc[idxs[hit_local]] = True
                    alive[idxs[hit_local]] = False
            out[:, k] = acc
        return a, b, out

    cum_hit = np.zeros((ndir, len(distances), n, n), bool)
    for pair in pairs:
        a, b, out = eval_pair(pair)
        cum_hit[:, :, a, b] = out
        cum_hit[:, :, b, a] = np.roll(out, ndir // 2, axis=0)

    unlocked = {
        str(float(r)): int(sum(
            connected_components(csr_matrix(m), directed=True, connection="strong")[0] > 1
            for m in cum_hit[:, k]
        ))
        for k, r in enumerate(distances)
    }
    return dict(
        kerf_mm=kerf,
        directions=ndir,
        displacements_mm=distances.tolist(),
        intersection_area_threshold_mm2=0.01,
        unblocked_directions_by_distance=unlocked,
        all_proper_groups_blocked_within_3mm=unlocked["3.0"] == 0,
        seconds=round(time.perf_counter() - t0, 2),
    )


def _rigid_motion_audit(data, raw_polys, motion_grid_fn, kerf=0.25, workers=None):
    """Exact single-piece rigid-motion screen (matches heller_jigsaw.production.audit.rigid_motion_audit)."""
    t0 = time.perf_counter()
    pieces = [p.buffer(-kerf * 0.5, quad_segs=8) for p in raw_polys]
    if any(not v.is_valid or v.geom_type != "Polygon" for v in pieces):
        raise ValueError("Invalid kerf-eroded shape")
    for p in pieces:
        shapely.prepare(p)
    candidates = motion_grid_fn()

    def eval_piece(i):
        shape = pieces[i]
        cx, cy = shape.centroid.coords[0]
        a, b, c, e = shape.bounds
        reach = 3 + max(np.hypot(px - cx, py - cy) for px, py in shape.exterior.coords) * 0.11
        near = box(a - reach, b - reach, c + reach, e + reach)
        nbrs = [v for j, v in enumerate(pieces) if i != j and v.intersects(near)]
        others_union = None
        active = np.arange(len(candidates))
        for step in (0.25, 0.5, 0.75, 1.0):
            if not len(active):
                break
            params = candidates[active] * step
            objs = np.full(len(params), shape, dtype=object)
            xy, idx = shapely.get_coordinates(objs, return_index=True)
            uv = xy - np.array([cx, cy])
            alpha = params[idx, 0]
            cs, sn = np.cos(alpha), np.sin(alpha)
            new = np.column_stack([cs * uv[:, 0] - sn * uv[:, 1], sn * uv[:, 0] + cs * uv[:, 1]]) + np.array([cx, cy]) + params[idx, 1:]
            objs = shapely.set_coordinates(objs, new)
            blocked = np.zeros(len(objs), bool)
            for nb in nbrs:
                rem = ~blocked
                if not rem.any():
                    break
                coll = shapely.intersects(objs[rem], nb)
                if coll.any():
                    rem_idx = np.flatnonzero(rem)[coll]
                    hit = shapely.area(shapely.intersection(objs[rem_idx], nb)) > 0.01
                    blocked[rem_idx[hit]] = True
            rem = ~blocked
            if rem.any() and len(nbrs) > 1:
                if others_union is None:
                    others_union = unary_union(nbrs)
                    shapely.prepare(others_union)
                rem_idx = np.flatnonzero(rem)
                coll = shapely.intersects(objs[rem_idx], others_union)
                if coll.any():
                    c_idx = rem_idx[coll]
                    hit = shapely.area(shapely.intersection(objs[c_idx], others_union)) > 0.01
                    blocked[c_idx[hit]] = True
            active = active[~blocked]
        safe = candidates[active]
        trans = np.linalg.norm(safe[:, 1:], axis=1) if len(safe) else np.array([])
        deg = np.abs(np.rad2deg(safe[:, 0])) if len(safe) else np.array([])
        return dict(
            piece=i + 1,
            asset_id=f"lizard-{i}",
            sampled_paths=len(candidates),
            safe_paths=len(safe),
            max_tested_rotation_deg=float(deg.max()) if len(deg) else 0.0,
            max_tested_translation_mm=float(trans.max()) if len(trans) else 0.0,
            rotation_4deg_or_more_paths=int((deg >= 3.99).sum()),
            translation_3mm_paths=int((trans >= 2.99).sum()),
        )

    reports = [eval_piece(i) for i in range(len(pieces))]
    return dict(
        kerf_mm=kerf,
        rotation_center="individual piece centroid",
        path_fractions=[0.25, 0.5, 0.75, 1.0],
        collision_area_threshold_mm2=0.01,
        piece_count=len(pieces),
        paths_per_piece=len(candidates),
        pieces_with_rotation_4deg_or_more=sum(r["rotation_4deg_or_more_paths"] > 0 for r in reports),
        pieces_with_translation_3mm=sum(r["translation_3mm_paths"] > 0 for r in reports),
        per_piece=reports,
        seconds=round(time.perf_counter() - t0, 2),
        scope="Single-piece sampled planar rigid motions, not proof of global locking or physical fit.",
    )


def audit_board(data, directory, report, *, run_motion=True, workers=None):
    """Run independent full-board fabrication, thin-feature, lock, and motion screens."""
    af, fl, analyze_piece_fabrication, measure, motion_grid = _import_audit_helpers()
    raw_polys = [Polygon(p["outline"]) for p in data["pieces"]]
    n = len(raw_polys)

    # 1. Re-verify cut.svg, engrave.svg, production.svg
    cut_faces = verify_cut(directory / "cut.svg", data["pieces"])
    engrave_info = verify_engrave(directory / "engrave.svg", data["pieces"], kerf_mm=0.25)
    prod_svg_info = verify_production_svg(directory / "production.svg", data["pieces"], kerf_mm=0.25)

    # 2. Kerf + neck screens (both heller_jigsaw load-bearing neck & strict audit_field single-part erosion)
    kerf_table = {}
    strict_kerf_fails = {}
    for kerf in KERF_WIDTHS:
        vals = [analyze_piece_fabrication(p, kerf_mm=kerf, min_neck_mm=3.0) for p in raw_polys]
        kerf_table[f"kerf_{kerf:.2f}"] = {
            "kerf_pass": sum(int(v["kerf_pass"]) for v in vals),
            "neck_pass": sum(int(v["neck_pass"]) for v in vals),
        }
        strict_kerf_fails[f"{kerf:.2f}"] = sum(af.parts(p.buffer(-kerf * 0.5)) != 1 for p in raw_polys)

    strict_neck_25 = sum(af.parts(p.buffer(-1.25)) != 1 for p in raw_polys)
    strict_neck_30 = sum(af.parts(p.buffer(-1.50)) != 1 for p in raw_polys)
    wood_widths = [_min_wood_width(p) for p in raw_polys]

    # 3. Thin-feature audit (both heller_jigsaw.measure at kerf=0.25, r=1.5 and audit_field.tip_reach r=1.5)
    thin_rows = []
    for i, poly in enumerate(raw_polys):
        m = measure(poly, kerf=0.25, r=1.5)
        reach, lost = af.tip_reach(poly, r=1.5)
        thin_rows.append(dict(
            piece=i + 1,
            asset_id=f"lizard-{i}",
            tip_reach_mm=round(reach, 4),
            tip_reach_thin_area_mm2=round(lost, 4),
            min_wood_width_mm=round(wood_widths[i], 4),
            **m,
        ))
    thin_rows.sort(key=lambda r: -r["max_tip_loss_mm"])
    thin_report = dict(
        kerf_mm=0.25,
        opening_radius_mm=1.5,
        scope="Geometric thin-feature screen, not a structural-strength test or physical material certification.",
        pieces=n,
        worst_tip_loss_mm=round(thin_rows[0]["max_tip_loss_mm"], 4),
        pieces_tip_loss_over_3mm=sum(r["max_tip_loss_mm"] > 3.0 for r in thin_rows),
        pieces_tip_loss_over_6mm=sum(r["max_tip_loss_mm"] > 6.0 for r in thin_rows),
        worst_tip_reach_mm=round(max(r["tip_reach_mm"] for r in thin_rows), 4),
        pieces_tip_reach_over_3mm=sum(r["tip_reach_mm"] > 3.0 for r in thin_rows),
        pieces_tip_reach_over_6mm=sum(r["tip_reach_mm"] > 6.0 for r in thin_rows),
        min_wood_width_mm=round(min(wood_widths), 4),
        median_wood_width_mm=round(float(np.median(wood_widths)), 4),
        per_piece=thin_rows,
    )

    # 4. Full-board planar unwrapped translation interlock margin (pathnet.field)
    pairs_fl, block, _ = fl.blocking(raw_polys, 360)
    Wm = fl.stops(pairs_fl, block, n)
    lock_margin = float(fl.lock_margin(raw_polys, Wm=Wm))
    unlocked_1 = len({t for t, _ in fl.loose_groups(Wm, 1.0)})
    unlocked_2 = len({t for t, _ in fl.loose_groups(Wm, 2.0)})
    unlocked_3 = len({t for t, _ in fl.loose_groups(Wm, 3.0)})

    geometry_pass = bool(
        cut_faces == n
        and prod_svg_info["verified_cut_faces"] == n
        and report["torus_area_error_mm2"] < 1e-6 * max(1.0, data["periods"][0][0])
        and all(v["kerf_pass"] == n and v["neck_pass"] == n for v in kerf_table.values())
        and all(v == 0 for v in strict_kerf_fails.values())
    )

    prod_audit = dict(
        geometry_pass=geometry_pass,
        n=n,
        board_cut_mm=report["cut_svg_size_mm"],
        periods_mm=report["periods_mm"],
        torus_area_error_mm2=report["torus_area_error_mm2"],
        lock_margin_mm=lock_margin,
        unlocked_directions_at_1mm=unlocked_1,
        unlocked_directions_at_2mm=unlocked_2,
        unlocked_directions_at_3mm=unlocked_3,
        **kerf_table,
        strict_kerf_failures=strict_kerf_fails,
        strict_neck_2_5mm_failures=strict_neck_25,
        strict_neck_3_0mm_failures=strict_neck_30,
        min_wood_width_mm=round(min(wood_widths), 4),
        median_wood_width_mm=round(float(np.median(wood_widths)), 4),
        worst_tip_reach_mm=thin_report["worst_tip_reach_mm"],
        worst_tip_loss_mm=thin_report["worst_tip_loss_mm"],
        engrave_verification=engrave_info,
        production_svg_verification=prod_svg_info,
        physical_test="not performed",
        commercial_release="not cleared",
        scope="Full-board digital fabrication and interlock screen; not a physical laser or assembly test.",
    )

    finite_report = None
    rigid_report = None
    if run_motion:
        finite_report = _finite_translation_audit(data, raw_polys, kerf=0.25, ndir=360, workers=workers)
        rigid_report = _rigid_motion_audit(data, raw_polys, motion_grid, kerf=0.25, workers=workers)

    digital_pass = (
        geometry_pass
        and lock_margin >= 3.0
        and unlocked_1 == 0
        and unlocked_2 == 0
        and unlocked_3 == 0
        and strict_neck_25 == 0
        and thin_report["worst_tip_loss_mm"] <= 6.0
    )
    if finite_report is not None:
        digital_pass = digital_pass and finite_report["unblocked_directions_by_distance"]["1.0"] == 0 and finite_report["all_proper_groups_blocked_within_3mm"]
    if rigid_report is not None:
        digital_pass = digital_pass and rigid_report["pieces_with_rotation_4deg_or_more"] == 0 and rigid_report["pieces_with_translation_3mm"] == 0
    prod_audit["digital_screen_pass"] = bool(digital_pass)

    (directory / "production-audit.json").write_text(json.dumps(prod_audit, indent=2) + "\n", encoding="utf-8")
    (directory / "thin-feature-audit.json").write_text(json.dumps(thin_report, indent=2) + "\n", encoding="utf-8")
    if finite_report is not None:
        (directory / "finite-motion-audit.json").write_text(json.dumps(finite_report, indent=2) + "\n", encoding="utf-8")
    if rigid_report is not None:
        (directory / "rigid-motion-screen.json").write_text(json.dumps(rigid_report, indent=2) + "\n", encoding="utf-8")

    return dict(
        production_audit=prod_audit,
        thin_feature_audit=thin_report,
        finite_motion_audit=finite_report,
        rigid_motion_screen=rigid_report,
    )


def preview(pieces, file, detail=False, *, periods=None, seed=387, color_mode="gradient", closeup=False):
    lo, hi = bounds(pieces)
    ext = hi - lo
    moved = {}
    if detail or closeup:
        pitch = math.sqrt(float(np.prod(ext)) / len(pieces))
        width = pitch * (7.2 if detail else 5.8)
        center = np.array([lo[0] + ext[0] * .53, hi[1] - pitch * (.42 if detail else 1.45)])
        window = width * np.array([1., .82 if detail else .68])
        lo, hi = center - window / 2, center + window / 2
        if detail:
            # Lift three real edge lizards into the foreground with clear separation.
            rim = [p for p in pieces if any(n["offset"][1] > 0 for n in p["neighbors"])]
            for side, angle, y_shift in [(-0.30, -0.22, 1.05), (0.00, 0.14, 1.48), (0.31, 0.25, 0.90)]:
                slot = center[0] + side * width
                candidates = [p for p in rim if p["id"] not in moved]
                if not candidates:
                    break
                p = min(candidates, key=lambda p: abs(p["center"][0] - slot) - .4 * p["center"][1])
                rot = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
                moved[p["id"]] = (rot, np.array([slot, hi[1] - pitch * y_shift]))
    else:
        lo, hi = lo - .055 * max(ext), hi + .055 * max(ext)
    scale = 2000 / (hi[0] - lo[0])
    w, h = np.rint((hi - lo) * scale).astype(int)
    image = Image.new("RGB", (w, h), "#faf9f5")
    face_rgb = np.zeros((h, w, 3), dtype=np.float32)
    mask = Image.new("L", (w, h), 0)
    masks = ImageDraw.Draw(mask)
    line_overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ldraw = ImageDraw.Draw(line_overlay)

    use_gradient = color_mode == "gradient" and periods is not None
    periods_arr = np.asarray(periods, dtype=float) if periods is not None else None
    if use_gradient:
        # Evaluate the continuous rainbow field once for all stationary pieces.
        xs = lo[0] + (np.arange(w) + 0.5) / scale
        ys = lo[1] + (np.arange(h) + 0.5) / scale
        gx, gy = np.meshgrid(xs, ys)
        board_rgb = palette_rgb(rainbow_field_t(np.stack((gx, gy), axis=-1), periods_arr, seed)).astype(np.float32)
    else:
        board_rgb = None

    # Draw stationary pieces first, then lifted pieces on top so lifted shadows/edges are crisp.
    ordered = [p for p in pieces if p["id"] not in moved] + [p for p in pieces if p["id"] in moved]
    stationary_mask = Image.new("L", (w, h), 0)
    smasks = ImageDraw.Draw(stationary_mask)
    solid_layer = Image.new("RGB", (w, h), "#faf9f5")
    sdraw = ImageDraw.Draw(solid_layer)

    for p in ordered:
        def position(points):
            xy = np.asarray(points, dtype=float)
            if p["id"] in moved:
                rot, location = moved[p["id"]]
                xy = (xy - p["center"]) @ rot + location
            return (xy - lo) * scale

        pts_arr = position(p["outline"])
        if (pts_arr.max(axis=0) < 0).any() or (pts_arr.min(axis=0) > [w, h]).any():
            continue
        pts = [tuple(x) for x in pts_arr]
        masks.polygon(pts, fill=255)
        if p["id"] not in moved:
            smasks.polygon(pts, fill=255)
            if not use_gradient:
                sdraw.polygon(pts, fill=p["color"])
        else:
            # Render the exact gradient patch carried by the lifted lizard in its original coordinates.
            pmask = Image.new("L", (w, h), 0)
            ImageDraw.Draw(pmask).polygon(pts, fill=255)
            pbox = pmask.getbbox()
            if pbox:
                x0, y0, x1, y1 = pbox
                sub = np.asarray(pmask.crop(pbox)) > 0
                if use_gradient:
                    rot, location = moved[p["id"]]
                    sxs = lo[0] + (np.arange(x0, x1) + 0.5) / scale
                    sys_ = lo[1] + (np.arange(y0, y1) + 0.5) / scale
                    sgx, sgy = np.meshgrid(sxs, sys_)
                    screen_xy = np.stack((sgx[sub], sgy[sub]), axis=-1)
                    orig_xy = (screen_xy - location) @ rot.T + np.asarray(p["center"])
                    prgb = palette_rgb(rainbow_field_t(orig_xy, periods_arr, seed)).astype(np.float32)
                    patch = face_rgb[y0:y1, x0:x1]
                    patch[sub] = prgb
                else:
                    sdraw.polygon(pts, fill=p["color"])
        r = p["eyeRadius"] * scale
        ldraw.line(pts + pts[:1], fill=(64, 42, 18, 220), width=max(1, round(r * .38)), joint="curve")
        ldraw.line([tuple(x) for x in position(p["spine"])], fill=(56, 36, 15, 235),
                   width=max(1, round(r * .56)), joint="curve")
        for ex, ey in position(p["eyes"]):
            ldraw.ellipse([ex - r, ey - r, ex + r, ey + r], fill=(42, 28, 12, 245))

    if use_gradient:
        stat_bool = np.asarray(stationary_mask) > 0
        face_rgb[stat_bool] = board_rgb[stat_bool]
        colored = face_rgb
    else:
        colored = np.asarray(solid_layer, dtype=np.float32)

    # A shallow charred-birch edge and soft drop shadow distinguish exposed contours from interior seams.
    depth = max(2, round(pieces[0]["eyeRadius"] * scale * 2.4))
    shadow = Image.new("L", (w, h), 0)
    shadow.paste(mask, (depth, 2 * depth))
    image.paste("#b7ad94", mask=shadow.filter(ImageFilter.GaussianBlur(depth * 1.4)))
    for d in range(depth, 0, -1):
        image.paste("#4e341c", (d // 3, d, w + d // 3, h + d), mask)

    # Subtle birch-wood grain in the preview only; cut/engrave SVGs remain strictly vector.
    rng = np.random.default_rng(24)
    grain = Image.fromarray(rng.integers(0, 256, (h, max(2, w // 60)), dtype=np.uint8))
    noise = (np.asarray(grain.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32) - 128) * .022
    noise += rng.normal(0, .85, (h, w)).astype(np.float32)
    textured = (colored + noise[..., None]).clip(0, 255).astype(np.uint8)
    face_img = Image.fromarray(textured, mode="RGB")
    face_img.paste(line_overlay, (0, 0), line_overlay)
    image.paste(face_img, (0, 0), mask)
    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    image.save(file, optimize=True)


def generate(output, seed=387, cols=9, rows=4, width=DEFAULT_WIDTH, *,
             color_mode="gradient", run_motion=True, workers=None):
    data, cuts, report = build(seed, cols, rows, width)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as scratch:
        target = Path(scratch)
        svg_files(data, cuts, target, color_mode=color_mode)
        report["verified_svg_faces"] = verify_cut(target / "cut.svg", data["pieces"])
        report["color_mode"] = color_mode
        report["model_training"] = training_report()
        audits = audit_board(data, target, report, run_motion=run_motion, workers=workers)
        pa = audits["production_audit"]
        th = audits["thin_feature_audit"]
        fm = audits["finite_motion_audit"]
        rm = audits["rigid_motion_screen"]
        report["all_hard_gates_passed"] = pa["digital_screen_pass"]
        report["fabrication_summary"] = dict(
            lock_margin_mm=pa["lock_margin_mm"],
            unlocked_directions_at_1mm=pa["unlocked_directions_at_1mm"],
            unlocked_directions_at_2mm=pa["unlocked_directions_at_2mm"],
            unlocked_directions_at_3mm=pa["unlocked_directions_at_3mm"],
            strict_neck_2_5mm_failures=pa["strict_neck_2_5mm_failures"],
            strict_neck_3_0mm_failures=pa["strict_neck_3_0mm_failures"],
            min_wood_width_mm=pa["min_wood_width_mm"],
            median_wood_width_mm=pa["median_wood_width_mm"],
            worst_tip_reach_mm=th["worst_tip_reach_mm"],
            worst_tip_loss_mm=th["worst_tip_loss_mm"],
            finite_motion_unblocked_3mm=fm["unblocked_directions_by_distance"]["3.0"] if fm else None,
            rigid_rot_4deg_pieces=rm["pieces_with_rotation_4deg_or_more"] if rm else None,
            rigid_trans_3mm_pieces=rm["pieces_with_translation_3mm"] if rm else None,
        )
        (target / "payload.json").write_text(json.dumps(data, separators=(",", ":"), allow_nan=False), encoding="utf-8")
        html = (HERE / "lizard_game.html").read_text(encoding="utf-8")
        if html.count("__PUZZLE_JSON__") != 1:
            raise ValueError("Game template needs one puzzle payload")
        (target / "index.html").write_text(html.replace("__PUZZLE_JSON__",
            json.dumps(data, separators=(",", ":"), allow_nan=False)), encoding="utf-8")
        preview(data["pieces"], target / "assembly.png", periods=data["periods"], seed=seed, color_mode=color_mode)
        preview(data["pieces"], target / "detail.png", detail=True, periods=data["periods"], seed=seed, color_mode=color_mode)
        preview(data["pieces"], target / "detail2.png", closeup=True, periods=data["periods"], seed=seed, color_mode=color_mode)
        (target / "quality.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        for file in target.iterdir():
            file.replace(output / file.name)
    return report


def audit_seeds(first_seed, count, cols, rows, width):
    if count < 1:
        raise ValueError("The audit needs at least one seed.")
    checked = []
    with tempfile.TemporaryDirectory() as scratch:
        directory = Path(scratch)
        for seed in range(first_seed, first_seed + count):
            data, cuts, report = build(seed, cols, rows, width)
            svg_files(data, cuts, directory, color_mode="solid")
            report["verified_svg_faces"] = verify_cut(directory / "cut.svg", data["pieces"])
            report["engrave_verification"] = verify_engrave(directory / "engrave.svg", data["pieces"])
            report["production_svg_verification"] = verify_production_svg(directory / "production.svg", data["pieces"])
            checked.append(report)
    return dict(seeds=count, first_seed=first_seed, last_seed=first_seed + count - 1,
                width_mm=width,
                verified_svg_faces=sum(r["verified_svg_faces"] for r in checked),
                largest_torus_area_error_mm2=max(r["torus_area_error_mm2"] for r in checked),
                min_outline_iou=min(r["min_outline_iou"] for r in checked),
                boards=checked)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=387)
    parser.add_argument("--cols", type=int, default=9, help="six pieces per cell")
    parser.add_argument("--rows", type=int, default=4)
    parser.add_argument("--width", type=float, default=DEFAULT_WIDTH, help="width of one repeat in mm")
    parser.add_argument("--color-mode", choices=("gradient", "solid"), default="gradient")
    parser.add_argument("--skip-motion", action="store_true", help="skip finite/rigid motion audits for fast drafts")
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--output", type=Path, default=HERE / "generated" / "lizards")
    parser.add_argument("--check-seeds", type=int, help="audit consecutive seeds through SVG re-import")
    args = parser.parse_args()
    if args.check_seeds is None:
        result = generate(args.output, args.seed, args.cols, args.rows, args.width,
                          color_mode=args.color_mode, run_motion=not args.skip_motion, workers=args.workers)
    else:
        result = audit_seeds(args.seed, args.check_seeds, args.cols, args.rows, args.width)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "seed-audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        result = {key: value for key, value in result.items() if key != "boards"}
    print(json.dumps(result, indent=2))
