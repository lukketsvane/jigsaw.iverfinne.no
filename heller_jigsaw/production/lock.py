import copy, json, sys, math
from pathlib import Path
import numpy as np, shapely
from shapely.geometry import LineString
from shapely.ops import unary_union
from heller_jigsaw.data.corpus import load_corpus_catalog, load_motif_asset
from heller_jigsaw.geometry.partition import partition_from_polygons
from heller_jigsaw.geometry.validate import ValidationConfig, validate_partition, analyze_piece_fabrication
from heller_jigsaw.geometry.export import export_puzzle_run
from .geometry import face_from_edges, tab_curve, pair_margin, target_polygon
from .audit import lock_check, fabrication_audit
from .search import evaluate, stat, all_kerf
from heller_jigsaw.solver.geodesic import _aligned_iou_and_chamfer

def iou(p, t):
    a = p.intersection(t).area
    return a / (p.area + t.area - a)

def run(src, out):
    src, out = (Path(src), Path(out))
    if src.resolve() == out.resolve() or (out / 'partition.json').exists():
        raise FileExistsError(out)
    out.mkdir(exist_ok=True, parents=True)
    d = json.loads((src / 'partition.json').read_text())
    cfg = json.loads((src / 'config.json').read_text())
    prov = json.loads((src / 'provenance.json').read_text())
    edges = d['edges']
    n = len(d['faces'])
    polys = [face_from_edges(f, edges) for f in d['faces']]
    rows, base = load_corpus_catalog(cfg['corpus_source'])
    by = {r['asset_id']: r for r in rows}
    motifs = [load_motif_asset(by[r['asset_id']], base) for r in cfg['motifs']]
    x0, y0, x1, y1 = unary_union(polys).bounds
    px = 0.65
    x0 = math.floor(x0 / px) * px - 8 * px
    y0 = math.floor(y0 / px) * px - 8 * px
    x = x0 + (np.arange(math.ceil((x1 - x0) / px) + 9) + 0.5) * px
    y = y0 + (np.arange(math.ceil((y1 - y0) / px) + 9) + 0.5) * px
    ff = [_aligned_iou_and_chamfer(shapely.contains_xy(p, x[None, :], y[:, None]), m, n_angles=36) for p, m in zip(polys, motifs)]
    targets = [target_polygon(m, f[2], x0, y0, px) for m, f in zip(motifs, ff)]
    assert all((t.geom_type == 'Polygon' for t in targets))
    parent = list(range(n))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    def link(a, b):
        parent[root(a)] = root(b)
    threshold = 3.3
    for e in edges:
        a, b = (e['left_face'], e['right_face'])
        if b >= 0 and a >= 0 and (pair_margin(edges, n, a, b) >= threshold):
            link(a, b)

    def test(e, coords, all_widths=False):
        a, b = (e['left_face'], e['right_face'])
        union = polys[a].union(polys[b])
        ls = LineString(coords)
        if not ls.is_simple or ls.difference(union.buffer(0.0001)).length > 0.005:
            return None
        old = e['coords']
        e['coords'] = coords
        try:
            na, nb = (face_from_edges(d['faces'][a], edges), face_from_edges(d['faces'][b], edges))
            if any((not p.is_valid or p.interiors for p in (na, nb))):
                return None
            if na.intersection(nb).area > 0.001 or na.union(nb).symmetric_difference(union).area > 0.001:
                return None
            pm = pair_margin(edges, n, a, b)
            if pm < threshold:
                return None
            if not all((q['kerf_pass'] and q['neck_pass'] for p in (na, nb) for k in ((0.1, 0.15, 0.2, 0.25) if all_widths else (0.25,)) for q in [analyze_piece_fabrication(p, kerf_mm=k, min_neck_mm=3)])):
                return None
            before = [iou(polys[a], targets[a]), iou(polys[b], targets[b])]
            after = [iou(na, targets[a]), iou(nb, targets[b])]
            weights = [1 + 4 * max(0, 0.85 - ff[k][0]) for k in (a, b)]
            loss = sum((w * (o - v) for w, o, v in zip(weights, before, after)))
            cost = loss + 0.00025 * na.symmetric_difference(polys[a]).area + 0.0008 / max(pm, 1)
            return (cost, na, nb, pm)
        except (ValueError, shapely.GEOSException):
            return None
        finally:
            e['coords'] = old
    proposals = {}
    for e in edges:
        a, b = (e['left_face'], e['right_face'])
        if min(a, b) < 0 or root(a) == root(b):
            continue
        line = LineString(e['coords'])
        if line.length < 13:
            continue
        choices = []
        for size in (0.55, 0.65, 0.8):
            for frac in (0.18, 0.34, 0.5, 0.66, 0.82):
                for sign in (-1, 1):
                    coords = tab_curve(line, frac, sign, size)
                    if coords is None:
                        continue
                    r = test(e, coords)
                    if r is not None:
                        choices.append((r[0], coords, size))
        if choices:
            proposals[e['edge_id']] = sorted(choices, key=lambda z: z[0])[:5]
    print('initial groups', len({root(i) for i in range(n)}), 'candidate seams', len(proposals), flush=True)
    selected = []
    while len({root(i) for i in range(n)}) > 1:
        best = None
        for i, options in proposals.items():
            e = edges[i]
            a, b = (e['left_face'], e['right_face'])
            if root(a) == root(b):
                continue
            for oldcost, coords, size in options:
                r = test(e, coords, all_widths=True)
                if r is None:
                    continue
                cost, na, nb, pm = r
                if best is None or cost < best[0]:
                    best = (cost, i, coords, size, na, nb, pm)
                break
        if best is None:
            break
        cost, i, coords, size, na, nb, pm = best
        e = edges[i]
        a, b = (e['left_face'], e['right_face'])
        area = na.symmetric_difference(polys[a]).area
        polys[a], polys[b] = (na, nb)
        e['coords'] = coords
        link(a, b)
        selected.append(dict(a=a, b=b, margin=pm, scale=size, area=area, cost=cost))
        proposals.pop(i, None)
        if len(selected) % 8 == 0:
            print('selected', len(selected), 'groups', len({root(i) for i in range(n)}), flush=True)
    if len({root(i) for i in range(n)}) != 1:
        raise RuntimeError('no complete spanning lock tree')
    for tol in (0.0, 0.12, 0.25, 0.5):
        pp = polys if tol == 0 else list(shapely.coverage_simplify(polys, tol))
        part = partition_from_polygons(pp, unary_union(pp), face_metadata=[f['metadata'].copy() for f in d['faces']], auto_node=False)
        qa = validate_partition(part, expected_n=n)
        lock = lock_check([e.to_dict() for e in part.edges], n)
        if not qa['all_hard_gates_passed'] or lock['lock_margin_mm'] < 3 or (not all_kerf(part)):
            continue
        fits = evaluate([f.polygon for f in part.faces], motifs)
        for f, v in zip(part.faces, fits):
            f.metadata.update(aligned_iou=round(float(v[0]), 4), sdf_chamfer=round(float(v[1]), 4))
        cfg['locking_tabs'] = selected
        cfg['locking_method'] = 'min-cost spanning tree, fixed-target contour loss'
        prov['parent_run'] = str(src)
        q = export_puzzle_run(part, out, cfg, prov, ['Cost-minimised shared seam locks'], [m.mask for m in motifs], ValidationConfig())
        print('FINAL', q['all_hard_gates_passed'], stat(fits), lock['lock_margin_mm'], flush=True)
        if q['all_hard_gates_passed']:
            fabrication_audit(out)
            return out
    raise RuntimeError('no validated locked export')
