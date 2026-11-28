"""PathNet v2 (pathnet/train_v2.py): the shape space, the zero-initialised decoder, the learned cutting rules' geometry,
and that the board code keeps v1 bit-identical unless PATHNET_CKPT points at a v2 checkpoint."""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import torch

ROOT = Path(__file__).resolve().parents[1] / 'pathnet'
sys.path.insert(0, str(ROOT))

import train_v2 as t2  # noqa: E402
from train import flatten  # noqa: E402


@pytest.fixture(scope='module')
def fam():
    return t2.Family(device='cpu')


def test_zero_latent_is_the_base_outline(fam):
    k = torch.arange(len(fam.names)); z = torch.zeros(len(k), fam.z)
    assert torch.allclose(fam(k, z), fam.P, atol=1e-7)


def test_global_modes_match_v1(fam):
    """the first 6 latents are v1's deform, scaled by the family's amplitude"""
    import train as v1
    rng = np.random.default_rng(0); z6 = rng.uniform(-1, 1, (8, 6)).astype(np.float32)
    z = torch.zeros(8, fam.z); z[:, :6] = torch.tensor(z6)
    got = fam(torch.full((8,), 3), z).numpy(); want = v1.deform(fam.P[3].numpy(), z6, False)
    assert np.allclose(got, want, atol=1e-5)


def test_handles_are_farthest_points_on_the_outline(fam):
    h = np.asarray(fam.config['handles'][0]); q = flatten(fam.P[0].numpy(), 4)
    assert h.shape == (t2.HANDLES, 2)
    assert all(np.min(np.linalg.norm(q - p, axis=1)) < 1e-4 for p in h)       # stored rounded to 5 decimals
    d = np.linalg.norm(h[:, None] - h[None], axis=-1) + np.eye(len(h)) * 9
    assert d.min() > .15                                  # spread over the logo, not bunched


def test_untrained_decoder_draws_the_base_shapes(fam):
    m = t2.DecoderV2(len(fam.names), fam.P.numpy(), width=64, depth=2, latent=fam.z).eval()
    k = torch.tensor([0, 5, 9]); z = torch.rand(3, fam.z) * 2 - 1
    with torch.no_grad(): assert torch.allclose(m(k, z), fam.P[k])


def test_outline_matches_train_flatten(fam):
    q = fam.P[:3]; got = t2.outline(q, 5).numpy()
    for i in range(3): assert np.allclose(got[i], flatten(q[i].numpy(), 5), atol=1e-6)


def test_cut_rules_on_the_traced_logo_and_a_thin_bar():
    base = t2.Family(device='cpu').P[:1]
    pn, pc, dn, dc = t2.cut_rules(base)
    assert float(pn) == 0 and float(pc) == 0                       # the logo keeps both rules
    assert 2.85 < float(dn) * t2.SIZE_MM < 3.0                      # its own narrowest neck is 2.92 mm at 34 mm
    # a stadium 2 mm wide at 34 mm: a neck the rule must see
    t = np.linspace(0, 2 * np.pi, t2.N, endpoint=False); r = 1 / 34
    bar = np.stack([.4 * np.cos(t), r * np.sin(t)], 1).astype(np.float32)
    pn, _, dn, _ = t2.cut_rules(torch.tensor(bar)[None])
    assert float(pn) > 0 and float(dn) * t2.SIZE_MM < 2.2


def test_board_code_defaults_to_v1_bit_identical():
    """without PATHNET_CKPT, generate.sample draws exactly what the pre-v2 sampler drew"""
    code = r'''
import numpy as np, generate as g
def old(rng, k):
    for _ in range(60):
        z = rng.uniform(-.28, .28, g.Z) if k == 0 else rng.uniform(-1, 1, g.Z); q = g.decode(k, z); p = g.Polygon(g.flatten(q, 6))
        if p.is_valid and p.area > .035 and (k != 0 or p.intersection(g.ref).area / p.union(g.ref).area >= .85): return q, p, z
r1, r2 = np.random.default_rng(22), np.random.default_rng(22)
a = [g.sample(r1, k)[2] for k in (0, 0, 0, 4)]; b = [old(r2, k)[2] for k in (0, 0, 0, 4)]
assert g.CKPT.name == 'pathnet.pt' and g.Z == 6 and g.REF_IOU == .85
assert all(np.array_equal(x, y) for x, y in zip(a, b)); print('ok')
'''
    env = {k: v for k, v in os.environ.items() if k != 'PATHNET_CKPT'}; env['CUDA_VISIBLE_DEVICES'] = ''
    r = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and r.stdout.strip().endswith('ok'), r.stderr[-2000:]


def test_v2_checkpoint_round_trip(tmp_path, fam):
    """a saved v2 checkpoint loads through generate.py's switch with its own latent size and limits"""
    m = t2.DecoderV2(len(fam.names), fam.P.numpy(), width=32, depth=1, latent=fam.z)
    ck = dict(arch='pathnet-v2', state=m.state_dict(), names=fam.names, points=t2.N, latent=fam.z, zlim=[1.] * len(fam.names),
              ref_iou_min=.45, model=dict(width=32, depth=1, emb=64), family=fam.config)
    path = tmp_path / 'v2.pt'; torch.save(ck, path)
    code = r'''
import numpy as np, generate as g
assert g.Z == 18 and g.REF_IOU == .45 and float(g.ZLIM[0]) == 1.
q, p, z = g.sample(np.random.default_rng(1), 0); assert q.shape == (96, 2) and z.shape == (18,); print('ok')
'''
    env = dict(os.environ, PATHNET_CKPT=str(path), CUDA_VISIBLE_DEVICES='')
    r = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and r.stdout.strip().endswith('ok'), r.stderr[-2000:]
