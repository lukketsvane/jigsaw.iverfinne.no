"""Seeded, periodic elastic deformation for the lizard tiling.

The small network learns how a sparse grid of virtual drag handles relaxes in
an elastic sheet. Inference uses only NumPy. Train the bundled model with
`python -m pathnet.lizard_net train`, or print its held-out scores with
`python -m pathnet.lizard_net report`.
"""

import argparse
from functools import lru_cache
import json
from pathlib import Path

import numpy as np


CHECKPOINT = Path(__file__).with_name("lizard_elastic.npz")
SLOPE_LIMIT = 0.60
LAYER_CHANNELS = (3, 20, 20, 16, 2)
OFFSETS = np.arange(-1, 3)


def _handles(rng, shape):
    """Independent drag directions and per-handle spring strengths."""
    pulls = rng.uniform(-1.0, 1.0, (*shape, 2))
    pinned = rng.random(shape) < 0.48
    # A sparse draw may leave a small practice board with no motion. Give
    # undersampled sheets enough independent handles to vary their pieces.
    sheets = pinned.reshape(-1, shape[-2] * shape[-1])
    counts = sheets.sum(axis=1)
    for i in np.flatnonzero(counts < 3):
        extra = rng.choice(np.flatnonzero(~sheets[i]), 3 - counts[i], replace=False)
        sheets[i, extra] = True
    springs = np.where(pinned, rng.uniform(0.4, 1, shape), 0)
    return np.concatenate((pulls * springs[..., None], springs[..., None]), axis=-1)


@lru_cache(maxsize=1)
def _weights():
    with np.load(CHECKPOINT, allow_pickle=False) as data:
        return tuple(
            (data[f"w{i}"], data[f"b{i}"])
            for i in range(len(LAYER_CHANNELS) - 1)
        )


def _decode(handles):
    # Correlation with circular padding, matching the trainer's Conv2d.
    state = handles.astype(np.float32)
    layers = _weights()
    for layer, (weight, bias) in enumerate(layers):
        out = np.broadcast_to(bias, (*state.shape[:2], len(bias))).copy()
        for row in range(3):
            for column in range(3):
                sample = np.roll(state, (1 - row, 1 - column), axis=(0, 1))
                out += sample @ weight[:, :, row, column].T
        state = np.maximum(out, out * 0.1) if layer < len(layers) - 1 else out
    return state.astype(float)


def _basis(t):
    """Four nonnegative uniform cubic B-spline basis functions."""
    return np.stack((
        (1 - t) ** 3,
        3 * t**3 - 6 * t**2 + 4,
        -3 * t**3 + 3 * t**2 + 3 * t + 1,
        t**3,
    ), axis=-1) / 6


class ElasticWarp:
    def __init__(self, periods, controls):
        self.size = np.array(controls.shape[:2][::-1], dtype=int)
        self.grid_to_world = periods / self.size[:, None]
        self.world_to_grid = np.linalg.inv(self.grid_to_world)
        self.controls = controls.copy()
        bounds = []
        for channel, axis in ((0, 1), (1, 0)):
            field = self.controls[..., channel]
            # Limit shear as well as contraction to keep the thinnest tails
            # and feet recognisable even for unusual arrangements of handles.
            max_slope = max(np.abs(np.roll(field, -1, a) - field).max() for a in (0, 1))
            if max_slope > SLOPE_LIMIT:
                field *= SLOPE_LIMIT / max_slope
            min_difference = float((np.roll(field, -1, axis=axis) - field).min())
            bounds.append(1 + min_difference)
        # Each stage changes a single coordinate. Its determinant is 1 + the
        # parallel partial derivative of the spline, a convex combination of
        # forward differences of neighbouring coefficients. Their product
        # bounds the full determinant, including after changing lattice basis.
        self.jacobian_lower_bound = float(np.prod(bounds))

    def _sample(self, q, channel):
        cell = np.floor(q)
        wx = _basis(q[..., 0] - cell[..., 0])
        wy = _basis(q[..., 1] - cell[..., 1])
        cell = cell.astype(np.int64)
        ix = (cell[..., 0, None] + OFFSETS) % self.size[0]
        iy = (cell[..., 1, None] + OFFSETS) % self.size[1]
        values = self.controls[iy[..., :, None], ix[..., None, :], channel]
        return np.einsum("...i,...ij,...j->...", wy, values, wx)

    def __call__(self, points):
        points = np.asarray(points, dtype=float)
        if points.shape[-1:] != (2,) or not np.isfinite(points).all():
            raise ValueError("points must be finite with final dimension 2")
        # Reduce before sampling, then add world-space displacement to the
        # original point, preserving the complete unwrapped board.
        q = (points @ self.world_to_grid) % self.size
        dx = self._sample(q, 0)
        q[..., 0] += dx
        dy = self._sample(q, 1)
        return points + np.stack((dx, dy), axis=-1) @ self.grid_to_world


def make_warp(seed, period_u, period_v, *, piece_count=216):
    """Build one translation-equivariant map; periods are full board vectors."""
    periods = np.asarray([period_u, period_v], dtype=float)
    if (periods.shape != (2, 2) or not np.isfinite(periods).all()
            or np.linalg.det(periods) == 0):
        raise ValueError("two independent finite period vectors are required")
    if piece_count < 1:
        raise ValueError("piece_count must be positive")
    lengths = np.linalg.norm(periods, axis=1)
    nx, ny = np.maximum(3, np.rint(np.sqrt(piece_count * lengths / lengths[::-1]))).astype(int)
    handles = _handles(np.random.default_rng(seed), (ny, nx))
    return ElasticWarp(periods, _decode(handles))


def training_report(path=CHECKPOINT):
    with np.load(path, allow_pickle=False) as data:
        return json.loads(data["training_report"].item())


def _train(steps, output, device):
    import torch
    from torch import nn

    torch.manual_seed(37231)
    torch.set_num_threads(2)
    model_layers = []
    for n_in, n_out in zip(LAYER_CHANNELS, LAYER_CHANNELS[1:]):
        model_layers.extend((
            nn.Conv2d(n_in, n_out, 3, padding=1, padding_mode="circular"),
            nn.LeakyReLU(0.1),
        ))
    model = nn.Sequential(*model_layers[:-1]).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.002, weight_decay=0.0001)
    schedule = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, steps, eta_min=0.00012)

    def tensors(seed, count):
        handles = _handles(np.random.default_rng(seed), (count, 12, 14))
        x = torch.from_numpy(handles.astype("float32")).permute(0, 3, 1, 2).contiguous().to(device)
        # Equilibrium of springs pulling the sheet towards the handles,
        # nearest-neighbour tension, and a light restoring spring to rest.
        pulls, springs = x[:, :2], x[:, 2:3]
        y = torch.zeros_like(pulls)
        for _ in range(32):
            neighbours = (y.roll(1, 2) + y.roll(-1, 2) + y.roll(1, 3) + y.roll(-1, 3)) / 4
            y = (pulls + 0.32 * neighbours) / (springs + 0.32 + 0.045)
        return x, y

    batch_size = 32
    # Independent seeds and fixed simulation grid, with fresh training sheets
    # on each update. Held-out data never enters the optimiser.
    for step in range(steps):
        x, y = tensors(37231 + step, batch_size)
        residual = model(x) - y
        mse = residual.square().mean()
        gradient_error = sum((residual - residual.roll(1, axis)).square().mean() for axis in (2, 3))
        optimizer.zero_grad(set_to_none=True)
        (mse + 0.20 * gradient_error).backward()
        optimizer.step()
        schedule.step()

    with torch.inference_mode():
        validation_count = 256
        x, y = tensors(170000129, validation_count)
        prediction = model(x)
        rmse = float((prediction - y).square().mean().sqrt().item())
        baseline = float(y.square().mean().sqrt().item())
        handle_baseline = float((x[:, :2] - y).square().mean().sqrt().item())
        gradient_rmse = float(np.sqrt(np.mean([
            ((prediction - y) - (prediction - y).roll(1, axis)).square().mean().item()
            for axis in (2, 3)
        ])))
    report = {
        "target": "periodic elastic-sheet equilibrium from sparse spring-loaded handles",
        "architecture": "four circular 3x3 convolutions with LeakyReLU hidden layers",
        "parameters": sum(p.numel() for p in model.parameters()),
        "training_seed_start": 37231, "steps": steps, "batch_size": batch_size,
        "training_sheets": steps * batch_size,
        "validation_seed": 170000129, "validation_sheets": validation_count,
        "network_rmse_grid_units": rmse, "identity_rmse_grid_units": baseline,
        "raw_handles_rmse_grid_units": handle_baseline,
        "neighbour_difference_rmse_grid_units": gradient_rmse,
    }
    arrays = {"training_report": np.array(json.dumps(report))}
    for i, layer in enumerate(model[::2]):
        arrays[f"w{i}"] = layer.weight.detach().cpu().numpy()
        arrays[f"b{i}"] = layer.bias.detach().cpu().numpy()
    np.savez_compressed(output, **arrays)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fit = sub.add_parser("train")
    fit.add_argument("--steps", type=int, default=1100)
    fit.add_argument("--output", type=Path, default=CHECKPOINT)
    fit.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    sub.add_parser("report")
    args = parser.parse_args()
    if args.command == "train" and args.steps < 1:
        parser.error("--steps must be positive")
    result = _train(args.steps, args.output, args.device) if args.command == "train" else training_report()
    print(json.dumps(result, indent=2))
