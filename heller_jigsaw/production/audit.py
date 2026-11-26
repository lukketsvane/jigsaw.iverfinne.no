import json
import math
import time
from pathlib import Path

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from ..geometry.validate import analyze_piece_fabrication, validate_run_dir


def lock_check(edges, n):
    theta = np.arange(360) * 2 * np.pi / 360
    dirs = np.stack([np.cos(theta), np.sin(theta)], axis=1)
    blocks = np.zeros((len(theta), n, n))
    for e in edges:
        a, b = (e['left_face'], e['right_face'])
        if min(a, b) < 0:
            continue
        delta = np.diff(np.array(e['coords']), axis=0)
        nr = delta[:, [1, 0]] * np.array([1, -1])
        dot = nr @ dirs.T
        blocks[:, a, b] += np.maximum(dot, 0).sum(0)
        blocks[:, b, a] += np.maximum(-dot, 0).sum(0)

    def unlocked(threshold):
        return int(sum((connected_components(csr_matrix(m >= threshold), directed=True, connection='strong')[0] > 1 for m in blocks)))
    lo, hi = (0.0, 12.0)
    while hi - lo > 0.0625:
        mid = (lo + hi) / 2
        if unlocked(mid):
            hi = mid
        else:
            lo = mid
    return dict(lock_margin_mm=lo, unlocked_directions_at_1mm=unlocked(1.0), unlocked_directions_at_2mm=unlocked(2.0), unlocked_directions_at_3mm=unlocked(3.0))


def fabrication_audit(p):
    p = Path(p)
    d = json.loads((p / 'partition.json').read_text())
    q = validate_run_dir(p)
    polys = [Polygon(f['exterior']) for f in d['faces']]
    report = dict(geometry_pass=q['all_hard_gates_passed'], n=len(polys), **lock_check(d['edges'], len(polys)))
    for kerf in [0.1, 0.15, 0.2, 0.25]:
        vals = [analyze_piece_fabrication(v, kerf_mm=kerf, min_neck_mm=3.0) for v in polys]
        report[f'kerf_{kerf:.2f}'] = {'kerf_pass': sum((x['kerf_pass'] for x in vals)), 'neck_pass': sum((x['neck_pass'] for x in vals))}
    (p / 'production-audit.json').write_text(json.dumps(report, indent=2))
    print(p.name, report, flush=True)
    return report


def translation_audit(path, kerf=0.25, ndir=360):
    if ndir < 4 or ndir % 2:
        raise ValueError('Translation sampling requires an even direction count >= 4')
    p = Path(path)
    d = json.loads((p / 'partition.json').read_text())
    polys = [Polygon(f['exterior']).buffer(-kerf / 2, quad_segs=8) for f in d['faces']]
    n = len(polys)
    if any((q.geom_type != 'Polygon' or not q.is_valid for q in polys)):
        raise ValueError('Kerf breaks a piece')
    pairs = sorted({tuple(sorted([e['left_face'], e['right_face']])) for e in d['edges'] if not e['is_exterior']})
    theta = np.arange(ndir) * 2 * np.pi / ndir
    dirs = np.stack([np.cos(theta), np.sin(theta)], axis=1)
    distances = np.array([0.5, 1.0, 2.0, 3.0])
    offsets = (dirs[:, None, :] * distances[None, :, None]).reshape(-1, 2)
    hit = np.zeros((ndir, len(distances), n, n), bool)
    for a, b in pairs:
        moving = np.full(len(offsets), polys[a], dtype=object)
        xy, idx = shapely.get_coordinates(moving, return_index=True)
        moving = shapely.set_coordinates(moving, xy + offsets[idx])
        intersects = shapely.intersects(moving, polys[b])
        result = np.zeros(len(offsets), bool)
        result[intersects] = shapely.area(shapely.intersection(moving[intersects], polys[b])) > 0.01
        result = result.reshape(ndir, len(distances))
        hit[:, :, a, b] = result
        hit[:, :, b, a] = np.roll(result, ndir // 2, axis=0)
    cumulative = np.maximum.accumulate(hit, axis=1)
    unlocked = {}
    for k, r in enumerate(distances):
        unlocked[str(r)] = int(sum((connected_components(csr_matrix(m), directed=True, connection='strong')[0] > 1 for m in cumulative[:, k])))
    report = dict(kerf_mm=kerf, directions=ndir, displacements_mm=distances.tolist(), intersection_area_threshold_mm2=0.01, unblocked_directions_by_distance=unlocked, all_proper_groups_blocked_within_3mm=unlocked['3.0'] == 0)
    (p / 'finite-motion-audit.json').write_text(json.dumps(report, indent=2))
    print(str(p), report, flush=True)
    return report


def measure(poly, kerf=0.25, r=1.5):
    p = poly.buffer(-kerf * 0.5, quad_segs=12)
    if p.geom_type != 'Polygon' or not p.is_valid:
        raise ValueError('kerf disconnected')
    core = p.buffer(-r, quad_segs=12)
    if core.is_empty:
        return dict(core_survival=0.0, max_tip_loss_mm=float(math.hypot(p.bounds[2] - p.bounds[0], p.bounds[3] - p.bounds[1])), lost_area_fraction=1.0)
    opened = core.buffer(r, quad_segs=12).intersection(p)
    coords = shapely.get_coordinates(shapely.segmentize(p.boundary, 0.25))
    distances = shapely.distance(shapely.points(coords), opened)
    j = int(np.argmax(distances))
    return dict(core_survival=float(core.area / p.area), max_tip_loss_mm=float(distances[j]), lost_area_fraction=float(p.difference(opened).area / p.area), worst_tip=coords[j].tolist())


def thin_feature_audit(path):
    p = Path(path)
    d = json.loads((p / 'partition.json').read_text())
    report = []
    for i, f in enumerate(d['faces']):
        poly = Polygon(f['exterior'])
        data = measure(poly)
        report.append(dict(piece=i + 1, asset_id=f['metadata']['asset_id'], **data))
    report.sort(key=lambda r: -r['max_tip_loss_mm'])
    result = dict(kerf_mm=0.25, opening_radius_mm=1.5, scope='Geometric thin-feature screen, not a structural-strength test or physical material certification.', pieces=len(report), worst_tip_loss_mm=report[0]['max_tip_loss_mm'], pieces_tip_loss_over_3mm=sum((r['max_tip_loss_mm'] > 3 for r in report)), pieces_tip_loss_over_6mm=sum((r['max_tip_loss_mm'] > 6 for r in report)), per_piece=report)
    (p / 'thin-feature-audit.json').write_text(json.dumps(result, indent=2))
    print(p, [(r['piece'], round(r['max_tip_loss_mm'], 2), round(r['lost_area_fraction'], 3)) for r in report[:5]], flush=True)
    return result


def motion_grid():
    angles = np.deg2rad(np.array([-6.0, -4.0, -2.0, 0.0, 2.0, 4.0, 6.0]))
    offsets = [(0.0, 0.0)]
    for r in (0.5, 1.0, 2.0, 3.0):
        offsets.extend(((r * math.cos(a), r * math.sin(a)) for a in np.arange(24) * 2 * math.pi / 24))
    return np.array([(a, dx, dy) for a in angles for dx, dy in offsets if a or dx or dy])


def rigid_motion_audit(path, kerf=0.25):
    t = time.perf_counter()
    p = Path(path)
    d = json.loads((p / 'partition.json').read_text())
    pieces = [Polygon(f['exterior']).buffer(-kerf / 2, quad_segs=8) for f in d['faces']]
    if any((not v.is_valid or v.geom_type != 'Polygon' for v in pieces)):
        raise ValueError('invalid kerf-eroded shape')
    candidates = motion_grid()
    reports = []
    for i, shape in enumerate(pieces):
        cx, cy = shape.centroid.coords[0]
        a, b, c, e = shape.bounds
        reach = 3 + max((np.hypot(x - cx, y - cy) for x, y in shape.exterior.coords)) * 0.11
        near = box(a - reach, b - reach, c + reach, e + reach)
        others = unary_union([v for j, v in enumerate(pieces) if i != j and v.intersects(near)])
        active = np.arange(len(candidates))
        for step in (0.25, 0.5, 0.75, 1.0):
            if not len(active):
                break
            params = candidates[active] * step
            objs = np.full(len(params), shape, dtype=object)
            xy, idx = shapely.get_coordinates(objs, return_index=True)
            uv = xy - np.array([cx, cy])
            alpha = params[idx, 0]
            cs = np.cos(alpha)
            sn = np.sin(alpha)
            new = np.column_stack([cs * uv[:, 0] - sn * uv[:, 1], sn * uv[:, 0] + cs * uv[:, 1]]) + np.array([cx, cy]) + params[idx, 1:]
            objs = shapely.set_coordinates(objs, new)
            collisions = shapely.intersects(objs, others)
            blocked = np.zeros(len(objs), bool)
            blocked[collisions] = shapely.area(shapely.intersection(objs[collisions], others)) > 0.01
            active = active[~blocked]
        safe = candidates[active]
        trans = np.linalg.norm(safe[:, 1:], axis=1)
        deg = np.abs(np.rad2deg(safe[:, 0]))
        report = dict(piece=i + 1, asset_id=d['faces'][i]['metadata']['asset_id'], sampled_paths=len(candidates), safe_paths=len(safe), max_tested_rotation_deg=float(deg.max()) if len(deg) else 0.0, max_tested_translation_mm=float(trans.max()) if len(trans) else 0.0, rotation_4deg_or_more_paths=int((deg >= 3.99).sum()), translation_3mm_paths=int((trans >= 2.99).sum()))
        reports.append(report)
    result = dict(kerf_mm=kerf, rotation_center='individual piece centroid', path_fractions=[0.25, 0.5, 0.75, 1.0], collision_area_threshold_mm2=0.01, piece_count=len(pieces), paths_per_piece=len(candidates), pieces_with_rotation_4deg_or_more=sum((r['rotation_4deg_or_more_paths'] > 0 for r in reports)), pieces_with_translation_3mm=sum((r['translation_3mm_paths'] > 0 for r in reports)), per_piece=reports, seconds=time.perf_counter() - t, scope='Single-piece sampled planar rigid motions, not proof of global locking or physical fit.')
    (p / 'rigid-motion-screen.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(p, result['pieces_with_rotation_4deg_or_more'], result['pieces_with_translation_3mm'], result['seconds'], flush=True)
    return result
