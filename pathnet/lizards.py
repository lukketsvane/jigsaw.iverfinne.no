"""Generate a playable whole-piece lizard torus and its registered print and cut files."""

import argparse
import json
import math
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from shapely import get_parts, polygonize
from shapely.geometry import LineString, Point, Polygon

from .lizard_net import ElasticWarp, make_warp, training_report
from .lizard_tile import EYE_RADIUS, make_tiles


HERE = Path(__file__).resolve().parent
# Use integer ticks so wrapped paths, game outlines and the exported cuts share vertices.
TICKS = 10000
MIN_OUTLINE_IOU = .75
PALETTE = np.array([
    [0, 187, 151], [30, 183, 87], [114, 177, 42], [179, 184, 39],
    [237, 198, 42], [252, 162, 36], [248, 110, 36], [237, 69, 49],
])


def color_field(centers, periods, seed):
    rng = np.random.default_rng(seed)
    uv = np.asarray(centers) @ np.linalg.inv(periods)
    x, y = (2 * np.pi * uv + rng.uniform(0, 2 * np.pi, 2)).T
    field = .61 * np.sin(x + .85 * np.sin(y)) + .33 * np.cos(y + .55 * np.sin(x))
    field += .09 * np.sin(2 * x - y)
    t = np.clip((field + 1) / 2, 0, 1) * (len(PALETTE) - 1)
    used, colors = set(), []
    for value in t:
        lo, hi = math.floor(value), math.ceil(value)
        rgb = np.rint(PALETTE[lo] + (PALETTE[hi] - PALETTE[lo]) * (value - lo)).astype(int)
        # Give each solid ink its own RGB value, even in the flattest colour bands.
        while tuple(rgb) in used:
            rgb = np.clip(rgb + rng.integers(-3, 4, 3), 0, 255)
        used.add(tuple(rgb))
        colors.append("#%02x%02x%02x" % tuple(rgb))
    return colors


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


def build(seed=387, cols=9, rows=4, width=298.45):
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
        if not poly.covers(LineString(spine).buffer(eye_radius * .3)):
            raise ValueError(f"Centreline leaves piece {index}")
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
    data = dict(seed=seed, periods=periods.tolist(), pieces=pieces)
    report = dict(seed=seed, pieces=len(pieces), periods_mm=periods.tolist(),
                  different_piece_areas=len(pieces), different_solid_colors=len(pieces),
                  torus_seams=len(joins), different_seam_lengths=len(seam_lengths),
                  seams_crossing_x=int(wrapping[0]),
                  seams_crossing_y=int(wrapping[1]), physical_cut_paths=len(cuts),
                  torus_area_error_mm2=area_error, min_area_mm2=min(p.area for p in polygons),
                  max_area_mm2=max(p.area for p in polygons),
                  min_outline_iou=min(outline_scores), median_outline_iou=float(np.median(outline_scores)),
                  warp_jacobian_lower_bound=float(warp.jacobian_lower_bound))
    return data, [line / TICKS for line in cuts.values()], report


def path_data(points, closed=False):
    return "M" + " ".join(f"{x:.4f},{y:.4f}" for x, y in points) + ("Z" if closed else "")


def bounds(pieces):
    points = np.concatenate([p["outline"] for p in pieces])
    return points.min(axis=0), points.max(axis=0)


def svg_files(data, cuts, directory):
    low, high = bounds(data["pieces"])
    x, y = low - 1
    w, h = high - low + 2
    header = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.4f}mm" height="{h:.4f}mm" '
              f'viewBox="{x:.4f} {y:.4f} {w:.4f} {h:.4f}">')
    cut_paths = [f'<path d="{path_data(edge)}"/>' for edge in cuts]
    (directory / "cut.svg").write_text("\n".join([header, '<g fill="none" stroke="red" stroke-width=".01">',
                                                  *cut_paths, '</g></svg>']), encoding="utf-8")
    art = [header]
    for p in data["pieces"]:
        art.append(f'<g id="lizard-{p["id"]}"><path fill="{p["color"]}" d="{path_data(p["outline"], True)}"/>')
        art.append(f'<path fill="none" stroke="#554020" stroke-width="{p["eyeRadius"]*.6:.4f}" '
                   f'stroke-linecap="round" stroke-linejoin="round" d="{path_data(p["spine"])}"/>')
        for x, y in p["eyes"]:
            art.append(f'<circle cx="{x}" cy="{y}" r="{p["eyeRadius"]}" fill="#43341e"/>')
        art.append('</g>')
    (directory / "artwork.svg").write_text("\n".join(art + ["</svg>"]), encoding="utf-8")


def verify_cut(file, pieces):
    # Re-import the actual file. A duplicated, displaced or missing seam must fail here.
    paths = ET.parse(file).getroot().findall(".//{http://www.w3.org/2000/svg}path")
    lines = [LineString(np.fromstring(p.attrib["d"][1:].replace(",", " "), sep=" ").reshape(-1, 2))
             for p in paths]
    if len({path_key(np.asarray(line.coords)) for line in lines}) != len(lines):
        raise ValueError("Duplicate path in the cut SVG")
    faces = list(get_parts(polygonize(lines)))
    # GEOS normalize removes differences in starting vertex and winding.
    expected = {Polygon(p["outline"]).normalize().wkb for p in pieces}
    if {face.normalize().wkb for face in faces} != expected or len(faces) != len(pieces):
        raise ValueError(f"Cut SVG polygonizes to {len(faces)} faces for {len(pieces)} pieces")
    return len(faces)


def preview(pieces, file, detail=False):
    lo, hi = bounds(pieces)
    ext = hi - lo
    moved = {}
    if detail:
        pitch = math.sqrt(float(np.prod(ext)) / len(pieces))
        width = pitch * 6.6
        center = np.array([lo[0] + ext[0] * .55, hi[1] - pitch * .48])
        window = width * np.array([1., .80])
        lo, hi = center - window/2, center + window/2
        # Lift two real pieces out of visible sockets to expose the isolated silhouettes.
        rim = [p for p in pieces if any(n["offset"][1] > 0 for n in p["neighbors"])]
        for side in [-1, 1]:
            slot = center[0] + side * .19 * width
            candidates = [p for p in rim if p["id"] not in moved]
            if not candidates:
                break
            p = min(candidates, key=lambda p: abs(p["center"][0] - slot) - .4*p["center"][1])
            a = side * .21
            rot = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]])
            moved[p["id"]] = (rot, np.array([slot, hi[1] - pitch*1.12]))
    else:
        lo, hi = lo - .055 * max(ext), hi + .055 * max(ext)
    scale = 2000 / (hi[0] - lo[0])
    w, h = np.rint((hi - lo) * scale).astype(int)
    image = Image.new("RGB", (w, h), "#faf9f5")
    face = Image.new("RGBA", image.size)
    mask = Image.new("L", image.size)
    draw, masks = ImageDraw.Draw(face), ImageDraw.Draw(mask)
    for p in pieces:
        def position(points):
            xy = np.asarray(points)
            if p["id"] in moved:
                rot, location = moved[p["id"]]
                xy = (xy - p["center"]) @ rot + location
            return (xy - lo) * scale

        pts = position(p["outline"])
        if (pts.max(axis=0) < 0).any() or (pts.min(axis=0) > [w, h]).any():
            continue
        pts = [tuple(x) for x in pts]
        masks.polygon(pts, fill=255)
        draw.polygon(pts, fill=p["color"])
        r = p["eyeRadius"] * scale
        draw.line(pts + pts[:1], fill="#715021", width=max(1, round(r*.42)), joint="curve")
        draw.line([tuple(x) for x in position(p["spine"])], fill="#69461e",
                  width=max(1, round(r*.60)), joint="curve")
        for x, y in position(p["eyes"]):
            draw.ellipse([x-r, y-r, x+r, y+r], fill="#49391c")
    # A shallow edge and drop shadow distinguish exposed contours from interior seams.
    depth = max(2, round(pieces[0]["eyeRadius"] * scale * 2.3))
    shadow = Image.new("L", image.size)
    shadow.paste(mask, (depth, 2*depth))
    image.paste("#b7ad94", mask=shadow.filter(ImageFilter.GaussianBlur(depth*1.4)))
    for d in range(depth, 0, -1):
        image.paste("#654527", (d//3, d, w+d//3, h+d), mask)
    # Fine surface variation in the preview only; the registered artwork uses solid inks.
    rng = np.random.default_rng(24)
    grain = Image.fromarray(rng.integers(0, 256, (h, max(2, w//60)), dtype=np.uint8))
    noise = (np.asarray(grain.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32)-128) * .019
    noise += rng.normal(0, .9, (h, w)).astype(np.float32)
    colored = np.asarray(face.convert("RGB"), dtype=np.float32) + noise[..., None]
    image.paste(Image.fromarray(colored.clip(0, 255).astype(np.uint8)), mask=face.getchannel("A"))
    image.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
    image.save(file, optimize=True)


def generate(output, seed=387, cols=9, rows=4, width=298.45):
    data, cuts, report = build(seed, cols, rows, width)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as scratch:
        target = Path(scratch)
        svg_files(data, cuts, target)
        report["verified_svg_faces"] = verify_cut(target / "cut.svg", data["pieces"])
        report["model_training"] = training_report()
        html = (HERE / "lizard_game.html").read_text(encoding="utf-8")
        if html.count("__PUZZLE_JSON__") != 1:
            raise ValueError("Game template needs one puzzle payload")
        (target / "index.html").write_text(html.replace("__PUZZLE_JSON__",
            json.dumps(data, separators=(",", ":"), allow_nan=False)), encoding="utf-8")
        preview(data["pieces"], target / "assembly.png")
        preview(data["pieces"], target / "detail.png", detail=True)
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
            svg_files(data, cuts, directory)
            report["verified_svg_faces"] = verify_cut(directory / "cut.svg", data["pieces"])
            checked.append(report)
    return dict(seeds=count, first_seed=first_seed, last_seed=first_seed+count-1,
                verified_svg_faces=sum(r["verified_svg_faces"] for r in checked),
                largest_torus_area_error_mm2=max(r["torus_area_error_mm2"] for r in checked),
                boards=checked)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=387)
    parser.add_argument("--cols", type=int, default=9, help="six pieces per cell")
    parser.add_argument("--rows", type=int, default=4)
    parser.add_argument("--width", type=float, default=298.45, help="width of one repeat in mm")
    parser.add_argument("--output", type=Path, default=HERE / "generated" / "lizards")
    parser.add_argument("--check-seeds", type=int, help="audit consecutive seeds through SVG re-import")
    args = parser.parse_args()
    if args.check_seeds is None:
        result = generate(args.output, args.seed, args.cols, args.rows, args.width)
    else:
        result = audit_seeds(args.seed, args.check_seeds, args.cols, args.rows, args.width)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "seed-audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        result = {key: value for key, value in result.items() if key != "boards"}
    print(json.dumps(result, indent=2))
