"""Independent fabrication screen of an exported cut network (works from the SVG alone).

    ..\\.venv\\Scripts\\python.exe audit_field.py <run-dir>            # reads <run-dir>/aphex-field-puzzle.svg

The SVG is re-read and polygonised without snapping, then every piece is screened:

* coverage of the board, overlaps, duplicate cut segments, pieces with holes;
* kerf: the piece shrunk by half of each kerf width (0.10-0.25 mm) must stay one polygon;
* necks: shrunk by 1.25 mm (a 2.5 mm neck) and 1.5 mm (3 mm) it must stay one polygon;
* thin tips: material that a 3 mm disc cannot reach (opening with radius 1.5 mm) is fragile; the screen reports
  how far such material reaches beyond the robust body of the piece, and flags reaches over 3 mm;
* size: area, and the smallest width of the piece's minimum rotated rectangle.

These are geometric screens of the cutting file, not tests of a material, a laser or an assembled puzzle.
"""
import json, re, sys
from pathlib import Path
from xml.etree import ElementTree as E

import cv2
import numpy as np
from shapely import union_all
from shapely.geometry import LineString, Point, box
from shapely.ops import polygonize

KERFS = (0.10, 0.15, 0.20, 0.25)


def read_cut(path):
    root = E.parse(path).getroot()
    W, H = (float(v) for v in root.attrib['viewBox'].split()[2:4])
    lines = []
    for q in root.findall('.//{*}path'):
        v = list(map(float, re.findall(r'[-+]?[0-9]+(?:\.[0-9]+)?(?:e[-+]?[0-9]+)?', q.attrib['d'])))
        lines.append(LineString(list(zip(v[::2], v[1::2]))))
    return lines, W, H


def parts(g):
    if g.is_empty: return 0
    return 1 if g.geom_type == 'Polygon' else len([p for p in g.geoms if p.area > 1e-6])


def tip_reach(p, r=1.5, step=0.25):
    """largest distance from the robust body (opening by r) to material only a thinner tool reaches"""
    body = p.buffer(-r).buffer(r)
    lost = p.difference(body)
    if lost.is_empty or lost.area < 1e-4: return 0.0, 0.0
    pts = []
    for g in (lost.geoms if hasattr(lost, 'geoms') else [lost]):
        if g.geom_type != 'Polygon' or g.area < 1e-4: continue
        ring = g.exterior
        pts.extend(ring.interpolate(d) for d in np.arange(0, ring.length, step))
    reach = max((body.distance(q) for q in pts), default=0.0) if not body.is_empty else float('inf')
    return float(reach), float(lost.area)


def audit(run_dir, svg_name='aphex-field-puzzle.svg'):
    run_dir = Path(run_dir); lines, W, H = read_cut(run_dir / svg_name); board = box(0, 0, W, H)
    pieces = sorted(polygonize(union_all(lines)), key=lambda p: (round(p.centroid.y / 20), p.centroid.x))
    segs = []
    for l in lines:
        xy = list(l.coords); segs.extend(tuple(sorted((a, b))) for a, b in zip(xy, xy[1:]))
    cover = union_all(pieces)
    rows = []
    for i, p in enumerate(pieces):
        reach, lost = tip_reach(p)
        mrr = p.minimum_rotated_rectangle; x, y = mrr.exterior.coords.xy
        sides = [Point(x[j], y[j]).distance(Point(x[j + 1], y[j + 1])) for j in range(2)]
        row = dict(piece=i, area_mm2=round(p.area, 2), min_width_mm=round(min(sides), 2), holes=len(p.interiors), valid=bool(p.is_valid),
                   edge_piece=bool(p.exterior.distance(board.exterior) < 1e-6),
                   kerf_parts={f'{k:.2f}': parts(p.buffer(-k / 2)) for k in KERFS},
                   neck_2_5mm_parts=parts(p.buffer(-1.25)), neck_3mm_parts=parts(p.buffer(-1.5)),
                   tip_reach_mm=round(reach, 2), thin_area_mm2=round(lost, 2))
        row['fails'] = [name for name, bad in (
            ('kerf', any(v != 1 for v in row['kerf_parts'].values())), ('neck_2_5mm', row['neck_2_5mm_parts'] != 1),
            ('neck_3mm', row['neck_3mm_parts'] != 1), ('tip_over_3mm', reach > 3.0), ('holes', row['holes'] > 0), ('invalid', not row['valid']),
            ('small', p.area < 130)) if bad]
        rows.append(row)
    summary = dict(
        cut_file=str(run_dir / svg_name), board_mm=[W, H], pieces=len(pieces), cut_length_mm=round(sum(l.length for l in lines), 1),
        coverage_error_mm2=round(board.symmetric_difference(cover).area, 6), overlap_mm2=round(sum(p.area for p in pieces) - cover.area, 6),
        duplicate_segments=len(segs) - len(set(segs)), pieces_with_holes=sum(r['holes'] > 0 for r in rows),
        area_mm2=dict(min=min(r['area_mm2'] for r in rows), median=float(np.median([r['area_mm2'] for r in rows])), max=max(r['area_mm2'] for r in rows)),
        min_width_mm=min(r['min_width_mm'] for r in rows), edge_pieces=sum(r['edge_piece'] for r in rows),
        kerf_failures={f'{k:.2f}': sum(r['kerf_parts'][f'{k:.2f}'] != 1 for r in rows) for k in KERFS},
        neck_2_5mm_failures=sum(r['neck_2_5mm_parts'] != 1 for r in rows), neck_3mm_failures=sum(r['neck_3mm_parts'] != 1 for r in rows),
        tips_over_3mm=sum(r['tip_reach_mm'] > 3 for r in rows), tips_over_2mm=sum(r['tip_reach_mm'] > 2 for r in rows),
        worst_tip_reach_mm=max(r['tip_reach_mm'] for r in rows), pieces_failing_any=sum(bool(r['fails']) for r in rows),
        scope='geometric screen of the exported cutting file; not a material, laser or assembly test')
    weak = interlock(pieces, summary)
    for r in rows: r['weak_lock'] = r['piece'] in weak
    (run_dir / 'fabrication-audit.json').write_text(json.dumps(dict(summary=summary, pieces=rows), indent=1))
    draw(run_dir / 'fabrication-audit.png', pieces, rows, W, H)
    return summary


def interlock(pieces, summary, ndir=360):
    """Planar translation lock of the re-read pieces (field.py's measure, recomputed here from the SVG): the assembly
    lock margin, and the groups that could slide out a little above it, which is where a repair has to act."""
    import field as fl
    pairs, block, _ = fl.blocking(pieces, ndir); Wm = fl.stops(pairs, block, len(pieces)); m = fl.lock_margin(pieces, Wm=Wm)
    weak, out = set(), {}
    for need in sorted({round(m + .25, 3), 1., 2., 3.}):
        groups = fl.loose_groups(Wm, need); small = [(t, g) for t, g in groups if g.sum() <= len(pieces) // 2]
        out[f'{need:g}'] = dict(directions=len({t for t, _ in groups}), groups=len(groups),
                                small_group_sizes=sorted(int(g.sum()) for _, g in small)[:20],
                                pieces_in_small_groups=sorted({int(i) for _, g in small for i in np.nonzero(g)[0]}))
        if need == round(m + .25, 3): weak = set(out[f'{need:g}']['pieces_in_small_groups'])
    summary['assembly_lock_margin_mm'] = m; summary['loose_groups_by_threshold_mm'] = out
    return weak


def draw(path, pieces, rows, W, H, scale=None):
    scale = scale or max(2., 3000 / max(W, H)); img = np.full((round(H * scale), round(W * scale), 3), 255, np.uint8)
    pts = lambda xy: np.round(np.asarray(xy) * scale * 16).astype(np.int32)
    for p, r in zip(pieces, rows):
        f = set(r['fails'])
        colour = (60, 60, 220) if f & {'kerf', 'neck_2_5mm', 'holes', 'invalid', 'small'} else (40, 140, 245) if f & {'neck_3mm', 'tip_over_3mm'} else (205, 222, 200)
        cv2.fillPoly(img, [pts(p.exterior.coords)], colour, lineType=cv2.LINE_AA, shift=4)
    cv2.polylines(img, [pts(p.exterior.coords) for p in pieces], True, (70, 70, 70), 1, cv2.LINE_AA, shift=4)
    weak = [p for p, r in zip(pieces, rows) if r.get('weak_lock')]
    if weak: cv2.polylines(img, [pts(p.exterior.coords) for p in weak], True, (200, 60, 20), max(2, int(scale * .6)), cv2.LINE_AA, shift=4)
    legend = ['green: passes every screen', 'orange: neck under 3 mm or thin tip over 3 mm', 'red: kerf, 2.5 mm neck, hole or size failure',
              'blue outline: weakest interlock (slides out just above the lock margin)']
    bar = np.full((28 * len(legend) + 12, img.shape[1], 3), 255, np.uint8)
    for i, text in enumerate(legend): cv2.putText(bar, text, (12, 28 * i + 30), cv2.FONT_HERSHEY_SIMPLEX, .7, (40, 40, 40), 2, cv2.LINE_AA)
    cv2.imwrite(str(path), np.vstack([bar, img]))


if __name__ == '__main__':
    print(json.dumps(audit(sys.argv[1], *(sys.argv[2:3])), indent=1))
