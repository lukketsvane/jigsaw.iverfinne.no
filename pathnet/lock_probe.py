"""Re-cut a saved layout with different bay-rule strengths (and optional strict-3mm fault locking)
and report lock margin, fragile pieces and weak spots.

    ..\\.venv\\Scripts\\python.exe lock_probe.py <layout-state.pt> --bays 16 32 64 --px .5 --target-lock 3.5
"""
import argparse, json, time
from pathlib import Path
import numpy as np, torch
import field as fl
import field_large as fll
import audit_field as af
from refine import flag

if __name__ == '__main__':
    a = argparse.ArgumentParser(); a.add_argument('state', type=Path); a.add_argument('--bays', type=float, nargs='+', default=[16, 32, 64, 128])
    a.add_argument('--px', type=float, default=.5); a.add_argument('--target-lock', type=float, default=0.)
    a.add_argument('--out', type=Path); a = a.parse_args()
    st = torch.load(a.state, map_location='cpu'); W, H = st['W'], st['H']
    polys = fl.outlines(*(st[q].to(fl.DEV) for q in ('k', 'z', 'c', 'th', 'size'))); rows = []
    for bay in a.bays:
        t0 = time.time()
        try: faces = fll.partition(polys, W, H, a.px, bay=bay)['faces']
        except ValueError as e: rows.append(dict(bay=bay, error=str(e))); print(rows[-1], flush=True); continue
        hooks = 0
        if a.target_lock > 0.:
            locked = fll.lock_fault_lines(faces, polys, W, H, target_margin=a.target_lock, log=None)
            faces = locked['faces']; hooks = len(locked['hooks'])
        pairs, block, _ = fl.blocking(faces); Wm = fl.stops(pairs, block, len(faces)); m = fl.lock_margin(faces, Wm=Wm)
        loose = fl.loose_groups(Wm, m + .25); small = [(t, g) for t, g in loose if g.sum() <= len(faces) // 2]
        strict_fails = sum(not fll._strict_piece_ok(f, min_area=130.0, max_tip_mm=3.0) for f in faces)
        rows.append(dict(bay=bay, hooks_added=hooks, lock_margin_mm=m, erosion_flags=int(sum(flag(f) for f in faces)),
                         strict_fails=int(strict_fails),
                         weak_pieces=sorted({int(i) for _, g in small for i in np.nonzero(g)[0]}),
                         weak_group_sizes=sorted(int(g.sum()) for _, g in small), open_directions=len({t for t, _ in loose}), seconds=round(time.time() - t0, 1)))
        print(json.dumps(rows[-1]), flush=True)
    if a.out: a.out.write_text(json.dumps(rows, indent=1))
