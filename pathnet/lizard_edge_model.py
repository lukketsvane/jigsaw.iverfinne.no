"""Learn vector edits to a periodic gecko's shared edges at physical cutting scale.

Each physical seam has one prediction. Both incident outlines, including wrapped
neighbors, use the same edited samples in opposite order. All lengths are mm.
"""

import argparse
from functools import lru_cache
import json
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicSpline
from shapely import contains_xy
from shapely.geometry import LineString, Polygon

from .lizard_net import make_warp
from .lizard_tile import LizardTile, _template, make_tiles


CHECKPOINT = Path(__file__).with_suffix(".npz")
MASTER = (0, 1, 3)
ROLE = (0, 1, 1, 2, 2, 0)
DIRECT = tuple(i in MASTER for i in range(6))
CONTROLS = 23
NOMINAL_MM = 18.0
ORIGINAL_EDGES, ORIGINAL_SPINE, ORIGINAL_EYES = _template()
ORIGINAL = np.concatenate([e[:-1] for e in ORIGINAL_EDGES])
OFFSETS = np.cumsum([0, *(len(edge) - 1 for edge in ORIGINAL_EDGES)])
SIDE = np.repeat(np.arange(6), np.diff(OFFSETS))
TEMPLATE_INVERSE = np.linalg.pinv(ORIGINAL - ORIGINAL.mean(0))
TRAIN_SEEDS = tuple(range(1400, 1432))
DEV_SEEDS = (101, 102, 103)
SHAPE_WEIGHT = 0.48
CLEARANCE_TARGET = 3.37


@lru_cache(maxsize=16)
def _spline_basis(n, controls=CONTROLS):
    """Interpolating cubic cardinal controls; displacements at both junctions are zero."""
    knots = np.linspace(0, 1, controls + 2)
    basis = np.pad(np.eye(controls), ((1, 1), (0, 0)))
    return CubicSpline(knots, basis, axis=0, bc_type="natural")(np.linspace(0, 1, n))


BASES = [
    (_spline_basis(len(c)) if direct else _spline_basis(len(c))[::-1])[:-1]
    for c, direct in zip(ORIGINAL_EDGES, DIRECT)
]
VERTEX_BASIS = np.concatenate(BASES)


def _opposed_samples():
    """Training witnesses: material between facing, nonlocal sections of an outline."""
    p = ORIGINAL
    arc = np.r_[
        0, np.cumsum(np.linalg.norm(np.diff(np.vstack((p, p[0])), axis=0), axis=1))
    ]
    delta = np.abs(arc[:-1, None] - arc[None, :-1])
    separation = np.minimum(delta, arc[-1] - delta)
    tangent = np.roll(p, -1, axis=0) - np.roll(p, 1, axis=0)
    inward = tangent[:, ::-1] * [-1, 1]
    inward /= np.linalg.norm(inward, axis=1)[:, None]
    distance = np.linalg.norm(p[:, None] - p[None], axis=2)
    poly = Polygon(p)
    result = []
    for i, j in zip(*np.triu((distance < 0.28) & (separation > 0.40), 1).nonzero()):
        # Saddles separating the three persistent 1.5 mm erosion cores from
        # the torso on the authored gecko. Other opposing samples include
        # narrow terminal toes whose thickening adds residual contour error.
        side_pair = (SIDE[i], SIDE[j])
        if side_pair not in ((0, 1), (3, 4), (4, 5), (1, 2), (2, 2), (5, 5)):
            continue
        ray = (p[j] - p[i]) / distance[i, j]
        if inward[i] @ ray > 0.80 and -inward[j] @ ray > 0.80:
            # Midpoint and quarterpoints reject pairs across concave exterior bays.
            q = p[i] + np.array([0.25, 0.5, 0.75])[:, None] * (p[j] - p[i])
            if contains_xy(poly, q[:, 0], q[:, 1]).all():
                result.append((i, j))
    return np.asarray(result, dtype=int).T


WITNESS_A, WITNESS_B = _opposed_samples()


def _frame(line):
    along = line[-1] - line[0]
    along /= np.linalg.norm(along)
    return np.stack((along, along[::-1] * [-1, 1]))


REGIONS = [(0, 1), (3, 4), (4, 5), (1, 2), (2, 2), (5, 5)]
REGION_SAMPLES = [
    (
        WITNESS_A[(SIDE[WITNESS_A] == a) & (SIDE[WITNESS_B] == b)],
        WITNESS_B[(SIDE[WITNESS_A] == a) & (SIDE[WITNESS_B] == b)],
    )
    for a, b in REGIONS
]


def _face_feature(which):
    factors = TEMPLATE_INVERSE @ (which.outline - which.outline.mean(0))
    gram = factors @ factors.T / NOMINAL_MM**2
    clearances = [
        np.linalg.norm(which.outline[a] - which.outline[b], axis=1).min() - 3.0
        for a, b in REGION_SAMPLES
    ]
    return (gram[0, 0] - 1, gram[1, 1] - 1, gram[0, 1], *clearances)


def _pairs(tiles, period):
    size = np.diag(period) if np.asarray(period).ndim == 2 else np.asarray(period)
    paired = {}
    for i, tile in enumerate(tiles):
        for side, edge in enumerate(tile.edges):
            canonical = edge if DIRECT[side] else edge[::-1]
            # Quantize only the lookup key. Geometry is maintained at input precision.
            q = np.rint((canonical / size) * 1.0e6).astype(np.int64) % 1_000_000
            key = (ROLE[side], q.tobytes())
            paired.setdefault(key, []).append((i, side))
    result = []
    for pair in paired.values():
        if len(pair) != 2:
            raise ValueError("An input edge has no exact periodic mate")
        a, b = sorted(pair, key=lambda pair: not DIRECT[pair[1]])
        if not DIRECT[a[1]] or DIRECT[b[1]]:
            raise ValueError("Shared edge roles disagree")
        result.append((a, b))
    return result


def _features(tiles, pairs):
    contexts = np.array([_face_feature(tile) for tile in tiles])
    rows = []
    for (ia, sa), (ib, sb) in pairs:
        edge = tiles[ia].edges[sa]
        family = ROLE[sa]
        onehot = np.eye(3)[family]
        native = ORIGINAL_EDGES[sa]
        sections = np.array_split(np.linalg.norm(np.diff(edge, axis=0), axis=1), 3)
        reference = np.array_split(np.linalg.norm(np.diff(native, axis=0), axis=1), 3)
        ratios = [
            a.sum() / (b.sum() * NOMINAL_MM) - 1 for a, b in zip(sections, reference)
        ]
        rows.append([*onehot, *contexts[ia], *contexts[ib], *ratios])
    return np.asarray(rows, np.float32)


def _prepare(tiles, period):
    pairs = _pairs(tiles, period)
    features = _features(tiles, pairs)
    x = np.empty((len(tiles), 6, features.shape[-1]), np.float32)
    frames = np.empty((len(tiles), 6, 2, 2), np.float32)
    for k, ((ia, sa), (ib, sb)) in enumerate(pairs):
        frame = _frame(tiles[ia].edges[sa])
        x[ia, sa] = x[ib, sb] = features[k]
        frames[ia, sa] = frames[ib, sb] = frame
    return (
        x,
        np.asarray([t.outline for t in tiles], np.float32),
        frames,
        np.asarray([np.vstack((t.spine, t.eyes)) for t in tiles], np.float32),
    )


def _predict(x, w):
    if w["w0"].shape[1] == 12 and x.shape[-1] == 24:
        x = x[..., [0, 1, 2, 3, 4, 5, 12, 13, 14, 21, 22, 23]]
    h = np.tanh(x @ w["w0"].T + w["b0"])
    h = np.tanh(h @ w["w1"].T + w["b1"])
    return (2.5 * np.tanh(h @ w["w2"].T + w["b2"])).reshape(
        *h.shape[:-1], w["w2"].shape[0] // 2, 2
    )


def edit_tile_edges(tiles, period_mm, checkpoint=CHECKPOINT, mode="model"):
    """Return physical-mm tiles, using one trained prediction for each shared seam."""
    if mode not in ("model", "fixed", "baseline"):
        raise ValueError("mode must be model, fixed, or baseline")
    if mode == "baseline":
        return tiles
    pairs = _pairs(tiles, period_mm)
    features = _features(tiles, pairs)
    with np.load(checkpoint, allow_pickle=False) as w:
        ctrl = (
            _predict(features, w)
            if mode == "model"
            else w["mean_controls"][np.argmax(features[:, :3], axis=1)]
        )
    updated = [[None] * 6 for _ in tiles]
    size = (
        np.diag(period_mm) if np.asarray(period_mm).ndim == 2 else np.asarray(period_mm)
    )
    for k, ((ia, sa), (ib, sb)) in enumerate(pairs):
        edge = tiles[ia].edges[sa]
        fixed = edge + (_spline_basis(len(edge), ctrl.shape[1]) @ ctrl[k]) @ _frame(
            edge
        )
        fixed[[0, -1]] = edge[[0, -1]]
        updated[ia][sa] = fixed
        translation = np.rint((tiles[ib].edges[sb][0] - edge[-1]) / size) * size
        updated[ib][sb] = fixed[::-1] + translation
    return [
        LizardTile(
            np.concatenate([e[:-1] for e in edges]),
            tuple(edges),
            t.spine.copy(),
            t.eyes.copy(),
        )
        for t, edges in zip(tiles, updated)
    ]


def _training_board(seed, scale):
    periods, tiles = make_tiles(3, 2)
    warp = make_warp(seed, *periods, piece_count=len(tiles))
    materialized = []
    for tile in tiles:
        samples = [*tile.edges, tile.spine, tile.eyes]
        allpoints = np.concatenate(samples)
        transformed = np.split(
            warp(allpoints) * scale, np.cumsum([len(s) for s in samples[:-1]])
        )
        edges = tuple(transformed[:6])
        materialized.append(
            LizardTile(np.concatenate([e[:-1] for e in edges]), edges, *transformed[6:])
        )
    return materialized, np.diag(periods) * scale


def _neck_and_shape(tiles, edited):
    bad, sc, invalid, decoration = 0, [], 0, 0
    for a, b in zip(tiles, edited):
        p, q = Polygon(a.outline), Polygon(b.outline)
        if not q.is_valid:
            bad += 1
            invalid += 1
            sc.append(0.0)
            continue
        core = q.buffer(-1.5)
        bad += int(core.is_empty or core.geom_type != "Polygon")
        sc.append(float(p.intersection(q).area / p.union(q).area))
        decoration += int(not q.covers(LineString(b.spine)))
    return bad, np.mean(sc), invalid, decoration


def _save_training_record(output, logdir, weights, report):
    report.update(
        version="v4",
        torch_rng_seed=71723,
        physical_scale_rng_seed=71819,
        training_axes_mm_per_template_unit=[15.0, 22.0],
        training_grid_cells=[3, 2],
        inputs=[
            "three seam-role indicators",
            "two incident faces, each with three metric coefficients and six measured widths relative to 3 mm",
            "three seam-length ratios relative to the nominal 18 mm template scale",
        ],
        outputs="23 interior 2D spline controls in the canonical edge frame, fixed endpoints",
        batch_size=48,
        original_root_target_mm=3.37,
        distal_pad_target_mm=3.12,
        validation_scope="development uses 36-face unseen 3x2 periodic boards at 17.9 mm per template unit",
    )
    serialized = json.dumps(report, indent=2)
    np.savez_compressed(output, **weights, training_report=np.array(serialized))
    (logdir / "training.json").write_text(serialized, encoding="utf8")


def train(out, logdir, steps=650):
    import torch
    from torch import nn

    torch.set_num_threads(2)
    torch.manual_seed(71723)
    rng = np.random.default_rng(71819)
    model = nn.Sequential(
        nn.Linear(24, 48),
        nn.Tanh(),
        nn.Linear(48, 48),
        nn.Tanh(),
        nn.Linear(48, 2 * CONTROLS),
        nn.Tanh(),
    )
    nn.init.zeros_(model[-2].weight)
    nn.init.zeros_(model[-2].bias)
    train_rows = []
    logdir.mkdir(parents=True, exist_ok=True)
    for seed in TRAIN_SEEDS:
        # Include material scales on both sides of the specified retail dimensions.
        scale = rng.uniform(15.0, 22.0, size=2)
        train_rows.append(_prepare(*_training_board(seed, scale)))
    X, P, F, markers = [
        torch.from_numpy(np.concatenate(rows)) for rows in zip(*train_rows)
    ]
    arcs = torch.from_numpy(VERTEX_BASIS.astype(np.float32))
    index = torch.tensor(SIDE)
    ids_a, ids_b = map(torch.from_numpy, (WITNESS_A, WITNESS_B))
    train_axis = P[:, ids_b] - P[:, ids_a]
    train_axis = torch.nn.functional.normalize(train_axis, dim=-1)
    # Engraving samples are fixed anchors. Nearby walls must remain on their
    # original side and leave clearance, especially along the curled tail.
    marker_sample = np.vstack((ORIGINAL_SPINE, ORIGINAL_EYES))
    marks, walls = np.where(
        np.linalg.norm(marker_sample[:, None] - ORIGINAL[None], axis=2) < 0.16
    )
    marks_t, walls_t = torch.from_numpy(marks), torch.from_numpy(walls)
    ink_axis = torch.nn.functional.normalize(
        markers[:, marks_t] - P[:, walls_t], dim=-1
    )
    desired_ink = torch.tensor(
        np.where(marks >= len(ORIGINAL_SPINE), 0.56, 0.30), dtype=torch.float32
    )
    direction = P.roll(-1, 1) - P
    original_step = direction.norm(dim=-1).clamp_min(0.001)
    tangent = direction / original_step[..., None]
    optim = torch.optim.AdamW(model.parameters(), lr=0.004, weight_decay=0.0001)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(optim, steps, eta_min=0.00035)
    history = []
    for step in range(steps):
        pick = torch.randint(len(P), (48,))
        coeff = 2.5 * model(X[pick]).reshape(len(pick), 6, CONTROLS, 2)
        physical_coeff = torch.matmul(coeff, F[pick])
        displacement = (physical_coeff[:, index] * arcs[None, :, :, None]).sum(-2)
        predicted = P[pick] + displacement
        axis = train_axis[pick]
        width = ((predicted[:, ids_b] - predicted[:, ids_a]) * axis).sum(-1)
        target = torch.where(
            torch.as_tensor(
                np.isin(SIDE[WITNESS_A] * 6 + SIDE[WITNESS_B], [1, 22, 29])
            ),
            CLEARANCE_TARGET,
            3.12,
        )
        violations = torch.relu(target - width).square()
        # A single bad neck can split a face. Keep the worst contacts in the objective.
        clearance = violations.mean() + 0.5 * violations.topk(18, dim=1).values.mean()
        residual = (
            displacement.square().sum(-1).mean()
            + 0.4 * torch.sqrt(displacement.square().sum(-1) + 1.0e-5).mean()
        )
        step_vector = predicted.roll(-1, 1) - predicted
        stretch = (step_vector * tangent[pick]).sum(-1) / original_step[pick]
        fold = torch.relu(0.40 - stretch).square().amax(1).mean()
        ink_distance = (
            (markers[pick][:, marks_t] - predicted[:, walls_t]) * ink_axis[pick]
        ).sum(-1)
        ink_loss = torch.relu(desired_ink - ink_distance).square().amax(1).mean()
        pad = torch.nn.functional.pad(coeff, (0, 0, 1, 1))
        smoothness = (
            (pad[:, :, 2:] - 2 * pad[:, :, 1:-1] + pad[:, :, :-2]).square().mean()
        )
        loss = (
            clearance
            + SHAPE_WEIGHT * residual
            + 0.55 * smoothness
            + 2.5 * fold
            + 5.0 * ink_loss
        )
        optim.zero_grad()
        loss.backward()
        optim.step()
        sched.step()
        if step % 50 == 0 or step + 1 == steps:
            history.append(
                dict(
                    step=step + 1,
                    loss=float(loss.item()),
                    clearance=float(clearance.item()),
                )
            )
    w = {}
    for i, layer in enumerate([model[0], model[2], model[4]]):
        w["w" + str(i)] = layer.weight.detach().numpy()
        w["b" + str(i)] = layer.bias.detach().numpy()
    flat = X.numpy().reshape(-1, 24)
    values = _predict(flat, w)
    w["mean_controls"] = np.stack([values[flat[:, i] > 0.5].mean(0) for i in range(3)])
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, **w)
    dev = []
    for seed in DEV_SEEDS:
        tiles, period = _training_board(seed, np.array([17.9, 17.9]))
        for mode in ("baseline", "fixed", "model"):
            faces = edit_tile_edges(tiles, period, out, mode)
            bad, iou, invalid, escape = _neck_and_shape(tiles, faces)
            dev.append(
                dict(
                    seed=seed,
                    mode=mode,
                    pieces=len(tiles),
                    neck_failures=bad,
                    mean_same_piece_iou=iou,
                    invalid_faces=invalid,
                    escaped_spines=escape,
                )
            )
    report = dict(
        training_seeds=list(TRAIN_SEEDS),
        dev_seeds=list(DEV_SEEDS),
        excluded_benchmark_seeds=[387, 421, 512],
        train_faces=len(X),
        train_distinct_seams=int(len(X) * 3),
        steps=steps,
        network="24-48-48-46 tanh MLP",
        parameters=sum(p.numel() for p in model.parameters()),
        method="direct geometric self-supervision",
        max_control_component_mm=2.5,
        clearance_target_mm=CLEARANCE_TARGET,
        shape_weight=SHAPE_WEIGHT,
        template_contact_witnesses=len(WITNESS_A),
        fixed_control="mean predicted controls for a seam role on the training examples",
        history=history,
        development=dev,
    )
    _save_training_record(out, logdir, w, report)
    print(json.dumps(dev, indent=2), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=CHECKPOINT)
    parser.add_argument(
        "--logs", type=Path, default=CHECKPOINT.parents[1] / "runs/lizard-shared-edge"
    )
    parser.add_argument("--steps", type=int, default=650)
    args = parser.parse_args()
    train(args.output, args.logs, args.steps)
