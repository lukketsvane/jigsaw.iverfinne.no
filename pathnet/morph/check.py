"""Contact sheet and acceptance statistics for one body-plan module (run from pathnet/).

    ..\\.venv\\Scripts\\python.exe -m morph.check trilobite --n 10 --out ..\\runs\\fossil-v3\\sheets\\trilobite.png

Prints, per plan: accepted share, rejection reasons, seconds per accepted creature, size factors (1.00 means the
creature keeps 3 mm parts at the 30 mm base size; up to 1.80 is allowed) and feature counts.
"""
import argparse, collections, importlib, json, sys, time
from pathlib import Path

import numpy as np

from morph.core import Reject, sheet


def run(module, n=10, seed=0, out=None, tries=6, plans=None, cell=150):
    m = importlib.import_module(f'morph.plans.{module}'); rows, stats = [], {}
    for name, (w, draw) in m.PLANS.items():
        if plans and name not in plans: continue
        rng = np.random.default_rng([seed, abs(hash(name)) % 2 ** 31]); got, why, t0, att = [], collections.Counter(), time.time(), 0
        while len(got) < n and att < n * tries:
            att += 1
            try: got.append(draw(rng).finish(rng, f'{module}.{name}'))
            except Reject as e: why[str(e)] += 1
        dt = time.time() - t0; sf = [c.size_factor for c in got]
        stats[name] = dict(weight=w, accepted=len(got), attempts=att, accept_rate=round(len(got) / max(att, 1), 3), reasons=dict(why),
                           seconds_per_creature=round(dt / max(len(got), 1), 3), size_factor_mean=round(float(np.mean(sf)), 3) if sf else None,
                           size_factor_max=round(float(np.max(sf)), 3) if sf else None, features_mean=round(float(np.mean([len(c.features) for c in got])), 2) if got else None,
                           solidity_mean=round(float(np.mean([c.solidity for c in got])), 3) if got else None)
        rows.append((f'{module}.{name}  accept {stats[name]["accept_rate"]:.2f}  {stats[name]["seconds_per_creature"]:.2f}s  sf {stats[name]["size_factor_mean"]}', got))
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True); sheet(rows, cell=cell, path=out)
    return stats


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    a.add_argument('module'); a.add_argument('--n', type=int, default=10); a.add_argument('--seed', type=int, default=0)
    a.add_argument('--out', type=Path); a.add_argument('--plans', nargs='*'); a.add_argument('--cell', type=int, default=150)
    a = a.parse_args()
    s = run(a.module, a.n, a.seed, a.out or Path('../runs/fossil-v3/sheets') / f'{a.module}.png', plans=a.plans, cell=a.cell)
    print(json.dumps(s, indent=1)); print('sheet:', (a.out or Path('../runs/fossil-v3/sheets') / f'{a.module}.png').resolve())
    sys.exit(0 if all(v['accepted'] for v in s.values()) else 1)
