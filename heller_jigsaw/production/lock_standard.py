import json, math
from pathlib import Path
import numpy as np
import shapely
from shapely.geometry import LineString
from shapely.ops import unary_union
from ..data.corpus import load_corpus_catalog, load_motif_asset
from ..geometry.partition import partition_from_polygons
from ..geometry.validate import ValidationConfig, validate_partition, analyze_piece_fabrication
from ..geometry.export import export_puzzle_run
from ..solver.geodesic import _aligned_iou_and_chamfer
from .geometry import face_from_edges, tab_curve, pair_margin, target_polygon
from .audit import lock_check, fabrication_audit
from .search import all_kerf

def run(src, out):
    src = Path(src)
    out = Path(out)
    if src.resolve() == out.resolve() or (out / 'partition.json').exists():
        raise FileExistsError(out)
    out.mkdir(exist_ok=True, parents=True)
    d = json.loads((src / 'partition.json').read_text())
    cfg = json.loads((src / 'config.json').read_text())
    prov = json.loads((src / 'provenance.json').read_text())
    edges = d['edges']
    n = len(d['faces'])
    polys = [face_from_edges(f, edges) for f in d['faces']]
    outline = unary_union(polys)
    assert all((p.is_valid and (not p.interiors) for p in polys))
    vcfg = ValidationConfig()
    rows, base = load_corpus_catalog(cfg['corpus_source'])
    by_id = {r['asset_id']: r for r in rows}
    motifs = [load_motif_asset(by_id[r['asset_id']], base) for r in cfg['motifs']]
    x0, y0, x1, y1 = outline.bounds
    px = 0.65
    x = x0 + (np.arange(math.ceil((x1 - x0) / px)) + 0.5) * px
    y = y0 + (np.arange(math.ceil((y1 - y0) / px)) + 0.5) * px

    def fits(pp):
        return [_aligned_iou_and_chamfer(shapely.contains_xy(p, x[None, :], y[:, None]), m, n_angles=24) for p, m in zip(pp, motifs)]
    before = fits(polys)
    targets = [target_polygon(m, f[2], x0, y0, px) for m, f in zip(motifs, before)]

    def iou(p, t):
        inter = p.intersection(t).area
        return inter / (p.area + t.area - inter)
    parents = list(range(n))

    def root(i):
        while parents[i] != i:
            i = parents[i]
        return i

    def link(a, b):
        parents[root(a)] = root(b)
    threshold = 3.1
    for e in edges:
        if not e['is_exterior'] and pair_margin(edges, n, e['left_face'], e['right_face']) >= threshold:
            link(e['left_face'], e['right_face'])
    print('initial groups', len({root(i) for i in range(n)}), flush=True)
    added = []
    for edge in sorted([e for e in edges if not e['is_exterior']], key=lambda e: -e['length_mm']):
        a, b = (edge['left_face'], edge['right_face'])
        if root(a) == root(b):
            continue
        line = LineString(edge['coords'])
        union = polys[a].union(polys[b])
        best = None
        baseline = iou(polys[a], targets[a]) + iou(polys[b], targets[b])
        for size in [0.55, 0.6, 0.7, 0.85, 1.0]:
            for frac in [0.5, 0.35, 0.65, 0.22, 0.78, 0.14, 0.86]:
                for sign in [-1, 1]:
                    coords = tab_curve(line, frac, sign, size)
                    if coords is None:
                        continue
                    ls = LineString(coords)
                    if not ls.is_simple or ls.difference(union.buffer(0.0001)).length > 0.005:
                        continue
                    old = edge['coords']
                    edge['coords'] = coords
                    try:
                        na, nb = (face_from_edges(d['faces'][a], edges), face_from_edges(d['faces'][b], edges))
                        if any((not p.is_valid or p.interiors for p in [na, nb])):
                            continue
                        if na.intersection(nb).area > 0.001 or na.union(nb).symmetric_difference(union).area > 0.001:
                            continue
                        fab = [analyze_piece_fabrication(p, kerf_mm=k, min_neck_mm=3) for p in [na, nb] for k in (0.1, 0.15, 0.2, 0.25)]
                        if any((not f['neck_pass'] or not f['kerf_pass'] for f in fab)):
                            continue
                        pm = pair_margin(edges, n, a, b)
                        if pm < threshold:
                            continue
                        area = na.symmetric_difference(polys[a]).area
                        loss = baseline - iou(na, targets[a]) - iou(nb, targets[b])
                        cost = loss + 0.0003 * area
                        if best is None or cost < best[0]:
                            best = (cost, coords, na, nb, pm, size, area)
                    finally:
                        edge['coords'] = old
            if best is not None:
                break
        if best is None:
            print('failed seam', a, b, flush=True)
            continue
        cost, coords, polys[a], polys[b], pm, size, area = best
        edge['coords'] = coords
        link(a, b)
        added.append(dict(a=a, b=b, margin=pm, scale=size, area=area))
        if len(added) % 4 == 0:
            print('locked seams', len(added), 'groups', len({root(i) for i in range(n)}), flush=True)
        if len({root(i) for i in range(n)}) == 1:
            break
    report = lock_check(edges, n)
    print('lock', report, flush=True)
    if report['lock_margin_mm'] < 3.0:
        raise RuntimeError('lock margin below 3mm')
    for tol in [0, 0.12, 0.25, 0.5]:
        try:
            pp = polys if tol == 0 else list(shapely.coverage_simplify(polys, tol))
            po = unary_union(pp)
            part = partition_from_polygons(pp, po, face_metadata=[f['metadata'].copy() for f in d['faces']], auto_node=False)
            qa = validate_partition(part, expected_n=n, config=vcfg)
            lock = lock_check([e.to_dict() for e in part.edges], n)
            if not qa['all_hard_gates_passed'] or lock['lock_margin_mm'] < 3 or (not all_kerf(part)):
                print('pre-export fail', tol, [k for k, v in qa['gates'].items() if not v], lock, flush=True)
                continue
            ff = fits([f.polygon for f in part.faces])
            for f, fit in zip(part.faces, ff):
                f.metadata.update(aligned_iou=round(float(fit[0]), 4), sdf_chamfer=round(float(fit[1]), 4))
            cfg['locking_tabs'] = added
            cfg['simplification_mm'] = tol
            prov['parent_run'] = str(src)
            dest = out
            quality = export_puzzle_run(part, dest, cfg, prov, ['32-piece minimum-area canonical shared-seam undercuts'], [m.mask for m in motifs], vcfg)
            print('FINAL', str(dest), quality['all_hard_gates_passed'], lock, 'mean', np.mean([f[0] for f in ff]), 'min', min((f[0] for f in ff)), flush=True)
            if quality['all_hard_gates_passed']:
                fabrication_audit(dest)
                return dest
        except Exception as e:
            print('export exception', tol, str(e)[:180], flush=True)
    raise RuntimeError('No export passed all requirements')
