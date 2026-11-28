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


@pytest.mark.parametrize(("classes", "limit"), [([1, 2], 2), ([0, 1], 0)])
def test_training_batch_without_selected_logos_uses_only_fidelity(classes, limit):
    controls = torch.ones(2, t2.N, 2, requires_grad=True)
    fidelity = controls.square().mean()
    penalty = t2.logo_rule_penalty(controls, torch.tensor(classes), limit)
    assert penalty.item() == 0
    (fidelity + penalty).backward()
    torch.testing.assert_close(controls.grad, 2 * controls.detach() / controls.numel())


def test_mixed_training_batch_preserves_selected_logo_penalty(fam):
    controls = torch.stack([fam.P[1], .4 * fam.P[0], fam.P[0]]).requires_grad_()
    classes = torch.tensor([1, 0, 0])
    penalty = t2.logo_rule_penalty(controls, classes, limit=1)
    neck, contact, _, _ = t2.cut_rules(controls[1:2])
    expected = neck + contact
    expected = (expected / (1 + expected / 1e-4)).mean()
    assert expected.item() > 0
    torch.testing.assert_close(penalty, expected)
    penalty.backward()
    assert torch.isfinite(controls.grad).all()
    assert controls.grad[1].abs().sum() > 0
    assert controls.grad[[0, 2]].count_nonzero() == 0


def test_board_code_defaults_to_v1_bit_identical():
    """without PATHNET_CKPT, generate.sample draws exactly what the pre-v2 sampler drew"""
    code = r'''
import numpy as np
import torch
from shapely.geometry import Polygon
import generate as g
from train import ROOT, Model, flatten

checkpoint_path = ROOT / 'pathnet.pt'
checkpoint = torch.load(checkpoint_path, map_location='cpu', weights_only=True)
legacy_model = Model(len(checkpoint['names']))
legacy_model.load_state_dict(checkpoint['state'])
legacy_model.eval()

@torch.inference_mode()
def old(rng, k):
    limit = .28 if k == 0 else 1.
    for _ in range(60):
        z = rng.uniform(-limit, limit, 6)
        q = legacy_model(torch.tensor([k]), torch.tensor(z[None], dtype=torch.float32))[0].numpy()
        p = Polygon(flatten(q, 6))
        if p.is_valid and p.area > .035 and (k != 0 or p.intersection(g.ref).area / p.union(g.ref).area >= .85):
            return q, z
    raise AssertionError('Legacy sampler accepted no shape')

assert g.CKPT.resolve() == checkpoint_path and g.Z == 6 and g.REF_IOU == .85
current_rng, old_rng = np.random.default_rng(22), np.random.default_rng(22)
for index, k in enumerate((0, 0, 0, 4)):
    controls, polygon, latent = g.sample(current_rng, k)
    old_controls, old_latent = old(old_rng, k)
    np.testing.assert_array_equal(latent, old_latent)
    np.testing.assert_array_equal(controls, old_controls)
    if index == 0:
        iou = polygon.intersection(g.ref).area / polygon.union(g.ref).area
        np.testing.assert_allclose([polygon.area, iou], [.301794, .928741], rtol=0, atol=3e-6)
print('ok')
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
