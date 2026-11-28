"""Export the original or shared-edge lizards at their intended assembled dimensions.

    python -m pathnet.lizard_edge_board --seed 387 --mode baseline

The nominal footprint excludes any SVG margin. Each shared vertex and its
period-translated copies round to the same 0.0001 mm grid.
"""

import argparse
from collections import defaultdict
import hashlib
from pathlib import Path
import json
import math
import os
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET

import numpy as np
import shapely
from shapely.geometry import LineString, MultiLineString, Point, Polygon

from . import lizards
from .lizard_tile import LizardTile, make_tiles


PRODUCT_MM = np.array([298.45, 236.22])
ATLAS_CELLS = (9, 4)
TICKS = lizards.TICKS
ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = ROOT / "runs/lizard-shared-edge"


def _fit_footprint(tiles, lengths, target_mm):
    """Scale each axis once, constraining the extrema and repeat vectors at SVG precision."""
    lengths = np.asarray(lengths)
    target_mm = np.asarray(target_mm)
    pts = np.concatenate([tile.outline for tile in tiles])
    low, high = pts.min(axis=0), pts.max(axis=0)
    scale = target_mm / (high - low)
    period_ticks = np.rint(TICKS * lengths * scale).astype(np.int64)
    if (period_ticks <= 0).any():
        raise ValueError("Tile periods must be strictly positive")
    modulo = 10**10

    def split(x):
        return np.divmod(np.rint(np.asarray(x) / lengths * modulo).astype(np.int64), modulo)

    def raw_ticks(x):
        lifts, local = split(x)
        return lifts * period_ticks + np.rint(local / modulo * period_ticks).astype(np.int64)

    bottom, top = raw_ticks(low), raw_ticks(low) + np.rint(TICKS * target_mm).astype(np.int64)
    lo_lift, lo_local = split(low)
    hi_lift, hi_local = split(high)

    def transform(x):
        lifts, local = split(x)
        arr = raw_ticks(x)
        for axis in range(2):
            for source, lift, target in ((lo_local, lo_lift, bottom), (hi_local, hi_lift, top)):
                mask = local[:, axis] == source[axis]
                arr[mask, axis] = target[axis] + (lifts[mask, axis] - lift[axis]) * period_ticks[axis]
        return arr / TICKS

    result = []
    for tile in tiles:
        edges = tuple(transform(e) for e in tile.edges)
        result.append(LizardTile(np.concatenate([e[:-1] for e in edges]),
                                 edges, transform(tile.spine), transform(tile.eyes)))
    actual = np.ptp(np.concatenate([t.outline for t in result]), axis=0)
    if not np.allclose(actual, target_mm, atol=0.00011, rtol=0):
        raise ValueError(f"Unexpected assembled footprint {actual}")
    return result, period_ticks / TICKS, scale


def _raw_baseline(seed):
    data, _, _ = lizards.build(seed=seed, cols=ATLAS_CELLS[0], rows=ATLAS_CELLS[1],
                              width=lizards.DEFAULT_WIDTH)
    _, templates = make_tiles(*ATLAS_CELLS)
    tiles = []
    for piece, template in zip(data["pieces"], templates, strict=True):
        pts = np.asarray(piece["outline"])
        closed = np.vstack((pts, pts[0]))
        idx = np.cumsum([0, *[len(e) - 1 for e in template.edges]])
        if idx[-1] != len(pts):
            raise ValueError("Incompatible lizard sample layout")
        curves = tuple(closed[a:b+1] for a, b in zip(idx, idx[1:]))
        tiles.append(LizardTile(pts, curves, np.array(piece["spine"]), np.array(piece["eyes"])))
    return tiles, np.diag(data["periods"]), data["pieces"]


def load_baseline(seed):
    """Return existing seeded LizardTiles in product-size mm and [repeat_x, repeat_y]."""
    tiles, lengths, _ = _raw_baseline(seed)
    fitted, periods, _ = _fit_footprint(tiles, lengths, PRODUCT_MM)
    return fitted, periods


def _interior_weights(outline, points):
    """Mean-value coordinates carry engraving through an edit to its surrounding face."""
    vectors = outline[None, :, :] - points[:, None, :]
    radii = np.linalg.norm(vectors, axis=2)
    unit = vectors / radii[..., None]
    along = np.roll(unit, -1, axis=1)
    cross = unit[..., 0] * along[..., 1] - unit[..., 1] * along[..., 0]
    dot = (unit * along).sum(axis=2)
    half_tangent = np.tan(0.5 * np.arctan2(cross, dot))
    weights = (half_tangent + np.roll(half_tangent, 1, axis=1)) / radii
    return weights / weights.sum(axis=1, keepdims=True)


def _project_face_edits(original, proposed, periods, eye_radius):
    """Apply a local validity line search to shared edges, keeping each seam paired.

    Only self-crossings, winding and engraving containment can trigger attenuation.
    The neck score and recognition score play no part in this decision.
    """
    p_ticks = np.rint(periods * TICKS).astype(np.int64)
    ownership = {}
    for i, tile in enumerate(original):
        for j, edge in enumerate(tile.edges):
            ticks = np.rint(edge * TICKS).astype(np.int64)
            ownership.setdefault(lizards.path_key(ticks % p_ticks), []).append((i, j))
    index = np.empty((len(original), 6), dtype=int)
    for n, pair in enumerate(ownership.values()):
        if len(pair) != 2:
            raise ValueError("Unexpected number of input seam owners")
        for i, j in pair:
            index[i, j] = n

    shapes = []
    for old, new in zip(original, proposed, strict=True):
        marks = np.vstack((old.spine, old.eyes))
        basis = _interior_weights(old.outline, marks)
        delta = [b - a for a, b in zip(old.edges, new.edges)]
        shapes.append((marks, basis, delta, Polygon(old.outline).exterior.is_ccw))

    weights = np.ones(len(ownership))
    for attempt in range(18):
        bad = []
        finished = []
        for i, (old, (marks, basis, delta, winding)) in enumerate(zip(original, shapes)):
            edges = tuple(e + weights[idx] * d for e, d, idx in zip(old.edges, delta, index[i]))
            pts = np.concatenate([e[:-1] for e in edges])
            moved = marks + basis @ (pts - old.outline)
            spine, eyes = moved[:-2], moved[-2:]
            poly = Polygon(pts)
            # Judge at a 0.25 mm kerf with 0.02 mm spare for the final footprint fit.
            valid = poly.is_valid and poly.area > 0 and poly.exterior.is_ccw == winding
            if valid:
                inside = poly.buffer(-.145)
                valid = (inside.covers(LineString(spine)) and
                         all(inside.covers(Point(eye).buffer(eye_radius)) for eye in eyes))
            if not valid:
                bad.append(i)
            finished.append(LizardTile(pts, edges, spine, eyes))
        if not bad:
            return finished, dict(iterations=attempt,
                                  attenuated_seams=int((weights < 1).sum()),
                                  minimum_edge_gain=float(weights.min()))
        affected = np.unique(index[bad])
        weights[affected] *= .75
        if attempt == 15:
            weights[affected] = 0.
    raise ValueError("Shared-edge projection did not recover a fabrication-valid board")


def _collect(tiles, periods, data_at_500, seed, eye_radius):
    periods_ticks = np.rint(periods * TICKS).astype(np.int64)
    seams, cuts, pieces, polygons = defaultdict(list), {}, [], []
    for pid, tile in enumerate(tiles):
        edges = [np.rint(e * TICKS).astype(np.int64) for e in tile.edges]
        if any(not np.array_equal(e[-1], f[0]) for e, f in zip(edges, edges[1:]+edges[:1])):
            raise ValueError(f"Open outline in piece {pid}")
        for e in edges:
            key = lizards.path_key(e % periods_ticks)
            seams[key].append((pid, e))
            cuts.setdefault(lizards.path_key(e), e)
        outline = np.concatenate([e[:-1] for e in edges])/TICKS
        polygon = Polygon(outline)
        if not polygon.is_valid or polygon.area <= 0:
            raise ValueError(f"Intersecting or empty piece {pid}")
        polygons.append(polygon)
        pieces.append(dict(id=pid, center=np.round(polygon.centroid.coords[0], 4).tolist(),
                           outline=outline.tolist(), eyes=tile.eyes.tolist(),
                           spine=tile.spine.tolist(), eyeRadius=eye_radius,
                           color=data_at_500[pid]["color"], neighbors=[]))
    wraps = np.zeros(2, dtype=int)
    lengths = []
    for pair in seams.values():
        if len(pair) != 2:
            raise ValueError(f"Found {len(pair)} owners for a periodic seam")
        (a, e), (b, f) = pair
        offset = e[0] - f[-1]
        if np.any(offset % periods_ticks) or not np.array_equal(e, f[::-1] + offset):
            raise ValueError(f"Wrapped cut mismatch: pieces {a}, {b}")
        wraps += offset != 0
        pieces[a]["neighbors"].append(dict(id=b, offset=(offset / TICKS).tolist()))
        pieces[b]["neighbors"].append(dict(id=a, offset=(-offset / TICKS).tolist()))
        lengths.append(float(np.linalg.norm(np.diff(e,axis=0),axis=1).sum()/TICKS))
    area_error = float(abs(sum(p.area for p in polygons) - np.prod(periods)))
    if len({p.exterior.is_ccw for p in polygons}) != 1 or area_error > 1e-6 or not all(wraps):
        raise ValueError(f"Invalid single cover, area error {area_error}, wrapped seams {wraps}")
    return (dict(seed=seed, periods=np.diag(periods).tolist(), pieces=pieces),
            [c / TICKS for c in cuts.values()],
            dict(pieces=len(pieces), periodic_seams=len(seams), cut_paths=len(cuts),
                 distinct_seam_lengths=len(set(np.round(lengths, 8))),
                 distinct_piece_areas=len(set(round(p.area, 8) for p in polygons)),
                 seam_crossings_x=int(wraps[0]), seam_crossings_y=int(wraps[1]),
                 torus_area_error_mm2=area_error))


def _tight_page(directory, data):
    # Existing standalone exporters add 1 mm of page margin on every side.
    # Retain their content, with the page frame exactly at the measured footprint.
    low, high = lizards.bounds(data["pieces"])
    span = high - low
    attrs = (f'<svg xmlns="{lizards.SVG_NS}" width="{span[0]:.4f}mm" '
             f'height="{span[1]:.4f}mm" viewBox="{low[0]:.4f} {low[1]:.4f} {span[0]:.4f} {span[1]:.4f}">')
    for file in [directory / name for name in ("cut.svg", "artwork.svg", "engrave.svg", "production.svg")]:
        _, body = file.read_text(encoding="utf8").split("\n", 1)
        file.write_text(attrs + "\n" + body, encoding="utf8")


def _verify_segments(lines):
    """Check the exported centerlines for duplicated segments and interior crossings."""
    segments = []
    for line in lines:
        xy = np.rint(np.asarray(line.coords) * TICKS).astype(np.int64)
        a, b = xy[:-1], xy[1:]
        if (a == b).all(axis=1).any():
            raise ValueError("Zero-length segment in exported cut")
        forward = (a[:, 0] < b[:, 0]) | ((a[:, 0] == b[:, 0]) & (a[:, 1] < b[:, 1]))
        segments.append(np.column_stack((np.where(forward[:, None], a, b),
                                         np.where(forward[:, None], b, a))))
    all_edges = np.concatenate(segments)
    if len(np.unique(all_edges, axis=0)) != len(all_edges):
        raise ValueError("Repeated exported cut segment")
    noded = shapely.node(MultiLineString(lines))
    noded_faces = shapely.get_parts(shapely.polygonize(shapely.get_parts(noded)))
    return len(all_edges), noded_faces


def _write_verified_board(seed, mode, directory, checkpoint):
    raw, lengths, source_pieces = _raw_baseline(seed)
    original, periods, scale = _fit_footprint(raw, lengths, PRODUCT_MM)
    radius = source_pieces[0]["eyeRadius"] * math.sqrt(np.prod(scale))
    tiles = original
    checkpoint_sha256 = None
    projection = None
    if mode != "baseline":
        from .lizard_edge_model import CHECKPOINT, edit_tile_edges
        model_path = Path(checkpoint or CHECKPOINT)
        checkpoint_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
        edited = edit_tile_edges(original, periods, checkpoint=model_path, mode=mode)
        edited, projection = _project_face_edits(original, edited, periods, radius)
        tiles, periods, fit = _fit_footprint(edited, periods, PRODUCT_MM)
        radius *= math.sqrt(np.prod(fit))
    data, cuts, audit = _collect(tiles, periods, source_pieces, seed, round(radius, 4))
    directory.mkdir(parents=True, exist_ok=True)
    lizards.svg_files(data, cuts, directory, color_mode="solid")
    _tight_page(directory, data)
    audit["reimported_faces"] = lizards.verify_cut(directory / "cut.svg", data["pieces"])
    audit["production_reimport"] = lizards.verify_production_svg(directory / "production.svg", data["pieces"])
    audit["engraving_reimport"] = lizards.verify_engrave(directory / "engrave.svg", data["pieces"])
    paths = ET.parse(directory / "cut.svg").getroot().findall(f".//{{{lizards.SVG_NS}}}path")
    lines = lizards._parse_paths(paths)
    audit["unique_cut_segments"], faces = _verify_segments(lines)
    if len(faces) != len(tiles):
        raise ValueError("Noding the cut reveals crossing or extra faces")
    by_key = {p.normalize().wkb: p for p in faces}
    exported = [by_key[Polygon(p["outline"]).normalize().wkb] for p in data["pieces"]]
    def failed(erosion):
        return [i for i, poly in enumerate(exported)
                if (s := poly.buffer(-erosion)).is_empty or s.geom_type != "Polygon"]
    baseline_polys = [Polygon(t.outline) for t in original]
    iou = np.array([p.intersection(q).area / p.union(q).area
                    for p, q in zip(baseline_polys, exported, strict=True)])
    audit.update(seed=seed, mode=mode, checkpoint_sha256=checkpoint_sha256,
                 validity_projection=projection,
                 engraving_transport="mean-value coordinates" if projection is not None else "original",
                 assembled_outline_mm=PRODUCT_MM.tolist(),
                 periods_mm=data["periods"], strict_3mm_neck_piece_ids=failed(1.5),
                 strict_2_5mm_neck_piece_ids=failed(1.25), strict_kerf_0_25mm_piece_ids=failed(.125),
                 min_iou_to_original=float(iou.min()), median_iou_to_original=float(np.median(iou)),
                 strict_neck_rule="single connected nonempty core after a 1.5 mm inward offset")
    for name, value in (("audit.json", audit), ("board.json", data)):
        (directory / name).write_text(json.dumps(value, separators=(",", ":")), encoding="utf8")
    return audit


def write_board(seed, mode, directory, checkpoint=None):
    """Keep the previous valid files intact when a candidate fails reimport."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".verify-", dir=directory) as stage:
        stage = Path(stage)
        audit = _write_verified_board(seed, mode, stage, checkpoint)
        for name in ("cut.svg", "engrave.svg", "artwork.svg", "production.svg", "board.json", "audit.json"):
            os.replace(stage / name, directory / name)
    return audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--mode", choices=("baseline", "fixed", "model"), required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    args = parser.parse_args()
    audit = write_board(args.seed, args.mode, args.output or OUT_ROOT / str(args.seed) / args.mode,
                        args.checkpoint)
    print(f'{args.mode} seed {args.seed}: {audit["reimported_faces"]} faces. '
          f'{len(audit["strict_3mm_neck_piece_ids"])} fail 3 mm. '
          f'Median paired IoU {audit["median_iou_to_original"]:.4f}.')

if __name__ == "__main__":
    main()

