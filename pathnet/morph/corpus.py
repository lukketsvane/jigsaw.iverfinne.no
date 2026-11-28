"""Corpus of procedurally grown fossils for FossilNet v3: shards of outline controls, engraving loops and size factors.

    ..\\.venv\\Scripts\\python.exe -m morph.corpus --out ..\\runs\\fossil-v3\\corpus --shards 400 --per 2000 --workers 7

Resumable: finished shards are skipped, so new body-plan modules join later shards. Each shard draws plans from the
registry mixture (morph.plans) with seed = shard index, and a --chimera share of crossbreeds: SDF blends of two
creatures of different plans. Every row holds the 256 B-spline controls of the unit outline (extent 1, canonical
start), up to FEAT engraving loops, the 3 mm size factor and the plan names; stats-*.json records rejections.
"""
import argparse, json, os, sys, time, traceback
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import numpy as np

from morph.core import FEAT, FP, N, Reject, blend, controls


def _save(path, **arrays):
    tmp = path.with_suffix('.tmp.npz'); np.savez_compressed(tmp, **arrays)
    for i in range(20):
        try: os.replace(tmp, path); return
        except PermissionError: time.sleep(.2 * (i + 1))          # Windows scanners hold fresh files for a moment
    raise PermissionError(path)


def shard(args):
    idx, per, out, chimera, only = args
    from morph.plans import load
    plans = load(only); names = sorted(plans); prob = np.array([plans[n][0] for n in names]); prob /= prob.sum()
    rng = np.random.default_rng([idx, 9173]); t0 = time.time()
    q = np.zeros((per, N, 2), np.float16); f = np.zeros((per, FEAT, FP, 2), np.float16); fm = np.zeros((per, FEAT), np.uint8)
    sf = np.zeros(per, np.float32); ef = np.zeros(per, np.float32); en = np.zeros(per, np.float32)
    p1 = np.zeros(per, np.int16); p2 = np.full(per, -1, np.int16); tt = np.zeros(per, np.float16)
    why, tries, errs, n, att = Counter(), Counter(), Counter(), 0, 0
    while n < per and att < 25 * per:
        att += 1; a = int(rng.choice(len(names), p=prob)); b = -1
        try:
            if rng.random() < chimera:
                b = int(rng.choice(len(names), p=prob))
                if names[b].split('.')[0] == names[a].split('.')[0] and rng.random() < .7: continue   # mostly across modules
                t = float(rng.uniform(.25, .75))
                cr = blend(plans[names[a]][1](rng), plans[names[b]][1](rng), t, rng, 'chimera')
            else:
                t = 0.; cr = plans[names[a]][1](rng).finish(rng, names[a])
        except Reject as e:
            why[f'{names[a] if b < 0 else "chimera"}: {e}'] += 1; tries[names[a]] += 1; continue
        except Exception as e:                                   # noqa: BLE001 (a bug in one plan must not stop the shard)
            errs[f'{names[a]}: {type(e).__name__}: {e}'[:200]] += 1
            if sum(errs.values()) <= 3: traceback.print_exc(file=sys.stderr)
            continue
        tries[names[a]] += 1
        q[n] = controls(cr.outline); sf[n] = cr.size_factor; ef[n] = cr.e_feat; en[n] = cr.e_notch; p1[n] = a; p2[n] = b; tt[n] = t
        for j, lp in enumerate(cr.features[:FEAT]): f[n, j] = lp; fm[n, j] = 1
        n += 1
    path = Path(out) / f'shard-{idx:05d}.npz'
    _save(path, q=q[:n], f=f[:n], fm=fm[:n], sf=sf[:n], e_feat=ef[:n], e_notch=en[:n], plan=p1[:n], plan2=p2[:n], t=tt[:n], names=np.array(names))
    stats = dict(shard=idx, rows=n, attempts=att, seconds=round(time.time() - t0, 1), rejected=dict(why.most_common()), errors=dict(errs.most_common()),
                 tries=dict(tries), accepted=dict(Counter(names[i] if j < 0 else 'chimera' for i, j in zip(p1[:n], p2[:n]))))
    (Path(out) / f'stats-{idx:05d}.json').write_text(json.dumps(stats, indent=1))
    return idx, n, att, time.time() - t0


def main():
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    a.add_argument('--out', type=Path, default=Path('../runs/fossil-v3/corpus')); a.add_argument('--shards', type=int, default=400)
    a.add_argument('--per', type=int, default=2000); a.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    a.add_argument('--chimera', type=float, default=.12); a.add_argument('--start', type=int, default=0); a.add_argument('--only', nargs='*')
    a = a.parse_args(); a.out.mkdir(parents=True, exist_ok=True)
    todo = [i for i in range(a.start, a.start + a.shards) if not (a.out / f'shard-{i:05d}.npz').exists()]
    print(f'{len(todo)} shards to write into {a.out.resolve()} with {a.workers} workers', flush=True); t0 = time.time(); rows = 0
    with Pool(a.workers, maxtasksperchild=1) as pool:                # a fresh process per shard picks up edited and new plan modules
        for k, (idx, n, att, dt) in enumerate(pool.imap_unordered(shard, [(i, a.per, str(a.out), a.chimera, a.only) for i in todo])):
            rows += n; print(f'shard {idx:5d}: {n} rows, {n / max(att, 1):.2f} accepted, {dt:.0f}s | {k + 1}/{len(todo)} done, {rows / (time.time() - t0):.1f} rows/s', flush=True)


if __name__ == '__main__':
    main()
