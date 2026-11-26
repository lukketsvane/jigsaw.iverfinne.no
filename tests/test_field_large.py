"""The windowed partition in pathnet/field_large.py must cut exactly what field.partition cuts.

Run from the repository root:  .venv\\Scripts\\python.exe -m pytest tests/test_field_large.py
"""
import sys
from pathlib import Path

import numpy as np
import pytest

PATHNET = Path(__file__).resolve().parents[1] / "pathnet"
sys.path.insert(0, str(PATHNET))
pytest.importorskip("torch")
pytest.importorskip("cv2")
fl = pytest.importorskip("field")
fll = pytest.importorskip("field_large")

import torch  # noqa: E402

W, H = 240.0, 160.0


@pytest.fixture(scope="module")
def logos():
    """24 logos on a jittered 6 x 4 grid, 34 mm across, leaving gaps of a few millimetres"""
    rng = np.random.default_rng(7)
    ks, zs, cs, ths = [], [], [], []
    for r in range(4):
        for c in range(6):
            _, _, z = fl.sample(rng, 0)
            ks.append(0); zs.append(z)
            cs.append([20 + 40 * c + rng.uniform(-2, 2), 20 + 40 * r + rng.uniform(-2, 2)])
            ths.append(rng.uniform(0, 2 * np.pi))
    k = fl.T(ks, torch.long); z = fl.T(np.array(zs)); c = fl.T(np.array(cs)); th = fl.T(np.array(ths))
    return fl.outlines(k, z, c, th, torch.full((len(ks),), 34.0, device=fl.DEV))


@pytest.mark.parametrize("bay", [0.0, 32.0])
def test_windowed_partition_matches_dense(logos, bay):
    dense = fl.partition(logos, W, H, px=0.5, bay=bay)
    # a 24 mm margin gives 82 mm windows on a 240 x 160 mm board, so the windows really crop
    windowed = fll.partition(logos, W, H, px=0.5, bay=bay, margin=24.0)
    assert np.array_equal(dense["lab"], windowed["lab"])
    assert len(dense["faces"]) == len(windowed["faces"]) == len(logos)
    for a, b in zip(dense["faces"], windowed["faces"]):
        assert a.symmetric_difference(b).area < 1e-6
    assert dense["cut"].symmetric_difference(windowed["cut"]).length < 1e-6


def test_window_covers_its_silhouette(logos):
    phi, org, gmin, _ = fll.windows(logos, W, H, 0.5, 24.0)
    assert (phi.min(dim=2).values.min(dim=1).values < 0).all()          # every window contains its own silhouette
    assert phi.shape[1] < round(H / 0.5) and phi.shape[2] < round(W / 0.5)
