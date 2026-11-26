"""Lock margin of a cut board with and without a fixed frame around it.

Without a frame the board edge holds nothing, so groups along the edge can slide outwards. With a frame (a tray, or a
border cut from the same sheet) every border segment of an edge piece is a wall: it stops that piece moving in any
direction with a component out of the board. The frame itself never moves.

    ..\\.venv\\Scripts\\python.exe frame_lock.py <run-dir>
"""
import json, sys
from pathlib import Path
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry.polygon import orient
import field as fl
from audit_field import read_cut
from shapely import union_all
from shapely.ops import polygonize


def frame_block(faces, W, H, ndir=360, tol=1e-6):
    """block[x, t]: length of piece x's border segments facing out of the board along direction t (what a frame stops)"""
    ang = np.arange(ndir) * 2 * np.pi / ndir; dirs = np.stack([np.cos(ang), np.sin(ang)], 1); out = np.zeros((len(faces), ndir))
    for i, p in enumerate(faces):
        xy = np.array(orient(p, 1).exterior.coords)
        for a, b in zip(xy[:-1], xy[1:]):
            on = ((abs(a[0]) < tol and abs(b[0]) < tol) or (abs(a[0] - W) < tol and abs(b[0] - W) < tol) or
                  (abs(a[1]) < tol and abs(b[1]) < tol) or (abs(a[1] - H) < tol and abs(b[1] - H) < tol))
            if not on: continue
            d = b - a; L = np.linalg.norm(d); nrm = np.array([d[1], -d[0]]) / L          # outward normal of a ccw ring
            out[i] += np.clip(dirs @ nrm, 0, None) * L
    return out


def free_groups(Wm, fb, need):
    """loose_groups with an extra fixed node (the frame) that edge pieces can be stopped by"""
    n = Wm.shape[1]; ndir = len(Wm); found = 0
    for t in range(ndir):
        A = np.zeros((n + 1, n + 1), bool); A[:n, :n] = Wm[t] >= need; A[:n, n] = fb[:, t] >= need
        k, comp = connected_components(csr_matrix(A), directed=True, connection='strong')
        if k == 1: continue
        r, c = np.nonzero(A & (comp[:, None] != comp[None, :])); leaves = np.zeros(k, bool); leaves[comp[r]] = True
        sinks = [s for s in np.nonzero(~leaves)[0] if s != comp[n]]
        found += len(sinks)
        if found: return True
    return False


def framed_margin(faces, W, H, top=12., step=.125):
    pairs, block, _ = fl.blocking(faces); Wm = fl.stops(pairs, block, len(faces)); fb = frame_block(faces, W, H)
    if not free_groups(Wm, fb, top): return top
    lo, hi = 0., top
    while hi - lo > step:
        mid = (lo + hi) / 2; lo, hi = (lo, mid) if free_groups(Wm, fb, mid) else (mid, hi)
    return lo


if __name__ == '__main__':
    run = Path(sys.argv[1]); lines, W, H = read_cut(run / 'aphex-field-puzzle.svg'); faces = list(polygonize(union_all(lines)))
    res = dict(pieces=len(faces), lock_margin_no_frame_mm=fl.lock_margin(faces), lock_margin_with_frame_mm=framed_margin(faces, W, H))
    print(json.dumps(res)); (run / 'frame-lock.json').write_text(json.dumps(res, indent=1))
