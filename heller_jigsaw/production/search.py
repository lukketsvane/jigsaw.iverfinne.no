import json, math, sys, time
from pathlib import Path
import cv2, numpy as np, shapely
from scipy import ndimage as ndi
from shapely.geometry import Polygon
from shapely.affinity import translate
from shapely.ops import unary_union
from heller_jigsaw.data.corpus import load_corpus_catalog, load_motif_asset
from heller_jigsaw.geometry.partition import extract_partition_from_labels, partition_from_polygons
from heller_jigsaw.geometry.validate import ValidationConfig, validate_partition, analyze_piece_fabrication
from heller_jigsaw.geometry.export import export_puzzle_run
from heller_jigsaw.solver.geodesic import _aligned_iou_and_chamfer
from .geometry import rotated, disk
from .audit import measure
cv2.setNumThreads(1)
CONFIGS = [(1, 2, 0), (2, 2, 0), (2, 3, 0), (2, 3, 4), (3, 3, 0), (3, 5, 0)]

def dock(motifs, seed, mode):
    rng = np.random.default_rng(seed)
    px = 0.65
    n = len(motifs)
    N = 960
    cores = np.zeros((n, N, N), bool)
    occ = np.zeros((N, N), bool)
    radial, holes, noise, step, perimeter = [(0.85, 4.5, 0.025, 15, 0), (0.5, 9, 0.07, 15, 0.25), (1.1, 8, 0.045, 15, 0.15), (0.65, 13, 0.1, 10, 0.1)][mode]
    compact = []
    for m in motifs:
        cs, _ = cv2.findContours((m.mask > 0.5).astype('uint8'), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        c = max(cs, key=cv2.contourArea)
        a = cv2.contourArea(c)
        h = cv2.contourArea(cv2.convexHull(c))
        l = cv2.arcLength(c, True)
        compact.append(0.5 * a / max(h, 1) + 0.5 * 4 * math.pi * a / max(l * l, 1) + rng.uniform(-noise, noise))
    order = np.argsort(-np.asarray(compact))
    center = np.array([N / 2, N / 2])
    for i, idx in enumerate(order):
        variants = [rotated(motifs[idx].mask, float(a), 1850 / px ** 2) for a in np.arange(0, 360, step)]
        if i == 0:
            m = variants[int(rng.integers(len(variants)))]
            h, w = m.shape
            y, x = ((N - h) // 2, (N - w) // 2)
        else:
            yy, xx = np.where(occ)
            margin = max((max(m.shape) for m in variants)) + 24
            y0 = max(28, int(yy.min()) - margin)
            y1 = min(N - 28, int(yy.max()) + margin + 1)
            x0 = max(28, int(xx.min()) - margin)
            x1 = min(N - 28, int(xx.max()) + margin + 1)
            region = occ[y0:y1, x0:x1]
            near = np.exp(-ndi.distance_transform_edt(~region) / 4.0).astype('float32')
            of = region.astype('float32')
            options = []
            for m in variants:
                h, w = m.shape
                if h > region.shape[0] or w > region.shape[1]:
                    continue
                mf = m.astype('float32')
                overlap = cv2.matchTemplate(of, mf, cv2.TM_CCORR)
                contact = cv2.matchTemplate(near, mf, cv2.TM_CCORR)
                yy, xx = np.ogrid[:contact.shape[0], :contact.shape[1]]
                rad = np.hypot(yy + y0 + h * 0.5 - center[0], xx + x0 + w * 0.5 - center[1])
                score = contact - radial * rad
                score[(overlap > 0.25) | (contact < 6)] = -np.inf
                for j in range(2):
                    cy, cx = np.unravel_index(np.argmax(score), score.shape)
                    v = score[cy, cx]
                    if not np.isfinite(v):
                        break
                    options.append((float(v), m, cy, cx))
                    score[max(0, cy - 16):cy + 17, max(0, cx - 16):cx + 17] = -np.inf
            best = None
            for v, m, cy, cx in sorted(options, key=lambda q: -q[0])[:32]:
                h, w = m.shape
                test = region.copy()
                test[cy:cy + h, cx:cx + w] |= m
                sm = cv2.resize(test.astype(np.float32), (max(1, test.shape[1] // 3), max(1, test.shape[0] // 3)), interpolation=cv2.INTER_AREA) > 0.25
                grown = ndi.binary_dilation(sm, iterations=2)
                filled = ndi.binary_fill_holes(grown)
                v -= holes * (filled & ~grown).sum() + perimeter * (grown & ~sm).sum()
                if best is None or v > best[0]:
                    best = (v, m, cy + y0, cx + x0)
            if best is None:
                raise ValueError('no valid placement')
            _, m, y, x = best
        cores[idx, y:y + m.shape[0], x:x + m.shape[1]] = m
        assert not np.any(occ & cores[idx])
        occ |= cores[idx]
    yy, xx = np.where(occ)
    return (cores[:, yy.min() - 20:yy.max() + 21, xx.min() - 20:xx.max() + 21], px)

def partition(cores, px, radius, closing, bay):
    occ = cores.any(0)
    grown = ndi.binary_dilation(occ, structure=disk(radius))
    solid = ndi.binary_fill_holes(grown | ndi.binary_closing(grown, structure=disk(closing)))
    if ndi.label(solid)[1] != 1:
        raise ValueError('outline disconnected')
    cs, _ = cv2.findContours(solid.astype('uint8'), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    poly = Polygon((max(cs, key=cv2.contourArea).reshape(-1, 2) + 0.5) * px).buffer(0).simplify(0.25, preserve_topology=True)
    if poly.geom_type != 'Polygon' or poly.interiors:
        raise ValueError('invalid outline')
    x0, y0, x1, y1 = poly.bounds
    w = math.ceil((x1 - x0) / px) + 1
    h = math.ceil((y1 - y0) / px) + 1
    xs = np.arange(w) + x0 / px
    ys = np.arange(h) + y0 / px
    offsets = np.stack(np.meshgrid(ys, xs, indexing='ij'), axis=0)
    poly = translate(poly, -x0, -y0)
    lab = np.full((h, w), -1, np.int32)
    best = np.full((h, w), np.inf, np.float32)
    for i, c in enumerate(cores):
        cc = ndi.map_coordinates(c.astype(np.uint8), offsets, order=0, mode='constant') > 0
        if not cc.any():
            raise ValueError('lost core')
        dist = ndi.distance_transform_edt(~cc).astype(np.float32)
        if bay:
            contours, _ = cv2.findContours(cc.astype('uint8'), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            hull = np.zeros(cc.shape, np.uint8)
            cv2.fillConvexPoly(hull, cv2.convexHull(max(contours, key=cv2.contourArea)), 1)
            dist += bay * (hull.astype(bool) & ~cc)
        improve = dist < best
        lab[improve] = i
        best[improve] = dist[improve]
    X = (np.arange(w) + 0.5) * px
    Y = (np.arange(h) + 0.5) * px
    lab[~shapely.contains_xy(poly, X[None, :], Y[:, None])] = -1
    return extract_partition_from_labels(lab, poly, px, n_pieces=len(cores), smooth_iters=3, simplify_tol_mm=0.25)

def evaluate(polys, motifs, angles=36, px=0.65):
    out = []
    for p, m in zip(polys, motifs):
        a, b, c, d = p.bounds
        x0 = math.floor(a / px) * px - 8 * px
        y0 = math.floor(b / px) * px - 8 * px
        x = x0 + (np.arange(math.ceil((c - x0) / px) + 9) + 0.5) * px
        y = y0 + (np.arange(math.ceil((d - y0) / px) + 9) + 0.5) * px
        out.append(_aligned_iou_and_chamfer(shapely.contains_xy(p, x[None, :], y[:, None]), m, n_angles=angles))
    return out

def stat(fits):
    a = np.array([v[0] for v in fits])
    return dict(mean=float(a.mean()), minimum=float(a.min()), p10=float(np.percentile(a, 10)), score=float(a.mean() + 0.35 * np.percentile(a, 10) + 0.25 * a.min()))

def all_kerf(part):
    for f in part.faces:
        for k in (0.1, 0.15, 0.2, 0.25):
            q = analyze_piece_fabrication(f.polygon, kerf_mm=k, min_neck_mm=3)
            if not q['kerf_pass'] or not q['neck_pass']:
                return False
    return True

def run(catalog, out, seed=42, mode=0, reference_catalog=None):
    start = time.perf_counter()
    out = Path(out)
    theme = Path(catalog).parent.name
    if (out / 'partition.json').exists():
        raise FileExistsError(out)
    out.mkdir(exist_ok=True, parents=True)
    cat = Path(catalog)
    rows, base = load_corpus_catalog(cat)
    motifs = [load_motif_asset(r, base) for r in rows]
    if not 2 <= len(motifs) <= 32:
        raise ValueError('Supply 2 to 32 catalog records')
    references = None
    if reference_catalog is not None:
        old, oldbase = load_corpus_catalog(reference_catalog)
        original = {r['asset_id']: load_motif_asset(r, oldbase) for r in old}
        references = [original[r.get('parent_asset_id', r['asset_id'])] for r in rows]
    results = []
    best = None
    try:
        cores, px = dock(motifs, seed, mode)
    except Exception as e:
        (out / 'trial.json').write_text(json.dumps(dict(theme=theme, seed=seed, mode=mode, error=str(e), seconds=time.perf_counter() - start, results=[])))
        raise RuntimeError('layout failed') from e
    vcfg = ValidationConfig()
    for radius, closing, bay in CONFIGS:
        result = dict(radius=radius, closing=closing, bay=bay)
        try:
            part = partition(cores, px, radius, closing, bay)
            qa = validate_partition(part, expected_n=len(motifs), config=vcfg)
            if not qa['all_hard_gates_passed']:
                for tol in (0.15, 0.35, 0.6):
                    pp = list(shapely.coverage_simplify([f.polygon for f in part.faces], tol))
                    try:
                        temp = partition_from_polygons(pp, unary_union(pp), auto_node=False)
                    except ValueError:
                        continue
                    check = validate_partition(temp, expected_n=len(motifs), config=vcfg)
                    if check['all_hard_gates_passed']:
                        part = temp
                        qa = check
                        break
            if not qa['all_hard_gates_passed']:
                raise ValueError(','.join((k for k, v in qa['gates'].items() if not v)))
            if not all_kerf(part):
                raise ValueError('kerf sweep')
            ff = evaluate([f.polygon for f in part.faces], motifs, angles=36)
            result.update(stat(ff))
            result['pass'] = True
            if references is not None:
                orig = evaluate([f.polygon for f in part.faces], references, angles=36)
                result['original'] = stat(orig)
                tips = [measure(f.polygon)['max_tip_loss_mm'] for f in part.faces]
                result['max_tip_mm'] = float(max(tips))
                result['over6'] = sum((v > 6 for v in tips))
                result['score'] = result['original']['mean'] + 0.35 * result['original']['p10'] + 0.4 * result['original']['minimum'] - 0.012 * result['over6'] - 0.004 * max(tips)
            if best is None or result['score'] > best[0]:
                best = (result['score'], part, ff, result.copy())
        except (ValueError, RuntimeError, shapely.GEOSException) as e:
            result['pass'] = False
            result['error'] = str(e)[:180]
        results.append(result)
        print(theme, seed, mode, result, flush=True)
    report = dict(theme=theme, seed=seed, mode=mode, seconds=time.perf_counter() - start, results=results, best=None)
    if best:
        _, part, ff, result = best
        for f, m, v in zip(part.faces, motifs, ff):
            f.metadata.update(asset_id=m.asset_id, family=m.family, motif_class=m.motif_class, aligned_iou=round(float(v[0]), 4), sdf_chamfer=round(float(v[1]), 4))
        cfg = dict(seed=seed, mode=mode, outline_type='campaign_v10', num_pieces=len(motifs), kerf_mm=0.15, min_neck_mm=3, corpus_source=str(cat), motifs=[dict(asset_id=m.asset_id, motif_class=m.motif_class, family=m.family) for m in motifs], docking=result)
        prov = dict(corpus_source=str(cat), all_commercial_ok=all((m.commercial_ok for m in motifs)), assets=rows)
        q = export_puzzle_run(part, out, cfg, prov, ['V10 multi-seed layout search candidate; not locked'], [m.mask for m in motifs], vcfg)
        report['best'] = dict(result, export_pass=q['all_hard_gates_passed'])
    report['seconds'] = time.perf_counter() - start
    (out / 'trial.json').write_text(json.dumps(report, indent=2))
    print('TRIAL COMPLETE', theme, seed, mode, report['seconds'], report['best'], flush=True)
    if not report['best'] or not report['best']['export_pass']:
        raise RuntimeError('no validated layout')
    return report
