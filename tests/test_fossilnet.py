"""Checks for pathnet/fossil_net.py: outline helpers, deformations, size factors and (when the private sources and
weights are present) that sampled creatures stay valid and close to their drawings.

Run from the repository root:  .venv\\Scripts\\python.exe -m pytest tests/test_fossilnet.py
"""
import sys
from pathlib import Path

import numpy as np
import pytest

PATHNET = Path(__file__).resolve().parents[1] / "pathnet"
sys.path.insert(0, str(PATHNET))
pytest.importorskip("torch")
fn = pytest.importorskip("fossil_net")

from shapely.geometry import Polygon, box  # noqa: E402


def circle(n=400, r=.5):
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return np.stack([r * np.cos(t), r * np.sin(t)], 1)


def test_controls_flatten_round_trip():
    q = fn.controls(circle())
    assert q.shape == (fn.N, 2)
    assert fn.iou(Polygon(fn.flatten(q)), Polygon(circle())) > .995


def test_deform_broadcasts_and_identity():
    q = fn.controls(circle())
    assert np.allclose(fn.deform(q, np.zeros(fn.Z))[..., 0], q[:, 0])            # zero latent keeps x
    batch = fn.deform(q, np.random.default_rng(0).uniform(-1, 1, (5, fn.Z)))
    assert batch.shape == (5, fn.N, 2)


def test_min_extent_scales_with_thinnest_part():
    # a bar 10% as wide as it is long: 3 mm wide (1.5 mm erosion survives) from about 30 mm long
    bar = box(-.5, -.05, .5, .05)
    assert 28 <= fn.min_extent(bar) <= 34
    assert fn.min_extent(box(-.5, -.1, .5, .1)) < fn.min_extent(bar)


def test_notch_extent_grows_for_narrow_notches():
    comb = box(-.5, -.5, .5, .5).difference(box(-.01, -.5, .01, .3))          # one 2%-wide slot
    wide = box(-.5, -.5, .5, .5).difference(box(-.1, -.5, .1, .3))            # one 20%-wide slot
    assert fn.min_notch_extent(comb) > fn.min_notch_extent(wide)


@pytest.mark.skipif(not (fn.DATA / "fossilnet.pt").exists() or not (fn.DATA / "sources.json").exists(),
                    reason="private drawings and weights are kept outside Git")
def test_samples_are_valid_and_faithful():
    import torch
    model, fams = fn.load(torch.device("cpu"))
    rng = np.random.default_rng(1)
    for k in range(0, len(fams), 5):
        q, p, z = fn.sample(rng, k)
        assert p.is_valid and np.abs(z).max() <= fams[k]["zsafe"] + 1e-9
        assert fn.iou(p, Polygon(fn.flatten(fams[k]["q"]))) >= .8
