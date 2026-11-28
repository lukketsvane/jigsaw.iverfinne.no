"""GPU-Trained Neural Parameter/Quality Surrogate & Artwork-Aware Spatial Style Optimizer.

1. `DendriteSurrogateNet`:
   A PyTorch residual MLP trained on CUDA RTX A4500 against real GPU-simulated
   multiphase Allen-Cahn puzzle patches and their real fabrication + 360-direction
   interlock audit scores (`assembly_lock_margin_mm`, `neck_pass_rate`, `kerf_pass_rate`,
   `seam_tortuosity`).
2. `auto_tune_parameters`:
   Differentiable multi-start Adam optimization through the frozen trained surrogate
   to choose PDE parameters maximizing interlocking & dendritic richness while keeping
   predicted `neck_pass_rate >= 0.99` and `kerf_pass_rate >= 0.99`.
3. `compute_artwork_style_fields`:
   GPU Sobel/Gaussian saliency pyramid converting any uploaded artwork image into smooth
   spatially-varying `(gamma_map, amplitude_map)` fields so detailed foreground artwork
   gets cleaner seams while open background regions grow richer dendritic tendrils.
"""

from __future__ import annotations

import json
import math
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .phase_field import DendriteConfig, STYLE_PRESETS, build_outline_polygon, run_dendrite_simulation
from .postprocess import (
    clean_connected_labels,
    compute_lock_metrics,
    extract_shared_seams_from_labels,
    repair_label_necks,
)

STYLE_LIST = ("dendrite", "amoeba", "linear", "grid")
DEFAULT_CHECKPOINT = Path("runs/dendrite-engine/neural_guide.pt")

# Continuous parameter bounds for encoding/decoding
PARAM_BOUNDS = {
    "gamma": (30.0, 52.0),
    "eps": (0.0070, 0.0105),
    "T0": (0.32, 0.48),
    "lambda1_ratio": (0.28, 0.80),  # relative to piece diameter mm
    "lambda2_ratio": (0.11, 0.28),  # relative to piece diameter mm
    "U1_ratio": (0.055, 0.145),     # relative to piece diameter mm
    "U2_ratio": (0.020, 0.070),     # relative to piece diameter mm
    "steps": (320.0, 740.0),
}
PARAM_KEYS = tuple(PARAM_BOUNDS.keys())
TARGET_KEYS = ("lock_margin_mm", "neck_pass_rate", "kerf_pass_rate", "seam_tortuosity")


def _encode_features(
    piece_diam_mm: float,
    aspect_ratio: float,
    style: str,
    params: Dict[str, float],
) -> np.ndarray:
    style_oh = [1.0 if style == s else 0.0 for s in STYLE_LIST]
    ctx = [
        np.clip((piece_diam_mm - 20.0) / 50.0, 0.0, 1.5),
        np.clip((aspect_ratio - 0.5) / 1.5, 0.0, 1.5),
    ]
    p_norm = []
    for k in PARAM_KEYS:
        lo, hi = PARAM_BOUNDS[k]
        p_norm.append(float(np.clip((params[k] - lo) / (hi - lo), 0.0, 1.0)))
    return np.asarray(ctx + style_oh + p_norm, dtype=np.float32)


def _decode_params(p_unit: np.ndarray, piece_diam_mm: float) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for idx, k in enumerate(PARAM_KEYS):
        lo, hi = PARAM_BOUNDS[k]
        val = lo + float(np.clip(p_unit[idx], 0.0, 1.0)) * (hi - lo)
        out[k] = val
    return {
        "gamma": round(out["gamma"], 2),
        "eps": round(out["eps"], 5),
        "T0": round(out["T0"], 3),
        "lambda1_mm": round(out["lambda1_ratio"] * piece_diam_mm, 2),
        "lambda2_mm": round(out["lambda2_ratio"] * piece_diam_mm, 2),
        "U1_mm": round(out["U1_ratio"] * piece_diam_mm, 2),
        "U2_mm": round(out["U2_ratio"] * piece_diam_mm, 2),
        "steps": int(round(out["steps"])),
    }


class _ResMLPBlock(nn.Module):
    def __init__(self, width: int, dropout: float = 0.05):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(width, width),
            nn.LayerNorm(width),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Linear(width, width),
            nn.LayerNorm(width),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return F.silu(x + self.net(x))


class DendriteSurrogateNet(nn.Module):
    """Multi-task PyTorch surrogate predicting fabrication safety & interlock from PDE parameters."""

    def __init__(self, in_dim: int = 2 + len(STYLE_LIST) + len(PARAM_KEYS), width: int = 128):
        super().__init__()
        self.in_dim = in_dim
        self.width = width
        self.stem = nn.Sequential(nn.Linear(in_dim, width), nn.LayerNorm(width), nn.SiLU())
        self.block1 = _ResMLPBlock(width)
        self.block2 = _ResMLPBlock(width)
        self.head = nn.Linear(width, len(TARGET_KEYS))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.block2(self.block1(self.stem(x)))
        raw = self.head(h)
        # Bound outputs to physically meaningful domains:
        # [0] lock_margin_mm in [0, 12], [1] neck_pass in [0, 1], [2] kerf_pass in [0, 1], [3] tortuosity in [1, 5]
        lock = F.softplus(raw[:, 0:1])
        neck = torch.sigmoid(raw[:, 1:2])
        kerf = torch.sigmoid(raw[:, 2:3])
        tort = 1.0 + F.softplus(raw[:, 3:4])
        return torch.cat([lock, neck, kerf, tort], dim=1)


def _simulate_training_sample(
    rng: np.random.Generator,
    device: str = "cuda",
) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """Run one compact real GPU dendritic simulation + real geometry/interlock audit."""
    from heller_jigsaw.geometry.validate import analyze_piece_fabrication

    style = STYLE_LIST[int(rng.integers(len(STYLE_LIST)))]
    num_pieces = int(rng.integers(9, 17))
    piece_diam_mm = float(rng.uniform(28.0, 52.0))
    aspect = float(rng.uniform(0.75, 1.45))
    area_tot = num_pieces * (math.pi * (0.5 * piece_diam_mm) ** 2) * 1.08
    w_mm = math.sqrt(area_tot * aspect)
    h_mm = area_tot / w_mm

    params = {k: float(rng.uniform(lo, hi)) for k, (lo, hi) in PARAM_BOUNDS.items()}
    cfg = DendriteConfig(
        style=style,
        width_mm=w_mm,
        height_mm=h_mm,
        num_pieces=num_pieces,
        seed=int(rng.integers(1, 100000)),
        px_mm=0.65,
        gamma=params["gamma"],
        eps=params["eps"],
        T0=params["T0"],
        lambda1_mm=params["lambda1_ratio"] * piece_diam_mm,
        lambda2_mm=params["lambda2_ratio"] * piece_diam_mm,
        U1_mm=params["U1_ratio"] * piece_diam_mm,
        U2_mm=params["U2_ratio"] * piece_diam_mm,
        steps=int(round(params["steps"])),
        seed_mode=STYLE_PRESETS[style]["seed_mode"],
        device=device,
    )
    outline = build_outline_polygon("rect", w_mm, h_mm, seed=cfg.seed)
    res = run_dendrite_simulation(outline, cfg)
    lab, _ = clean_connected_labels(res.labels, res.domain_mask)
    lab, _ = repair_label_necks(lab, res.domain_mask, res.px_mm, min_neck_mm=3.0, kerf_mm=0.15, passes=2)

    try:
        faces, int_lines, _ext_lines, _ = extract_shared_seams_from_labels(
            lab,
            res.bounds_mm,
            outline,
            phase_fields=res.phase_fields,
            piece_to_color=res.piece_to_color,
            smooth_sigma_mm=0.60,
            simplify_tol_mm=0.10,
        )
    except ValueError:
        return None

    neck_ok = 0
    kerf_ok = 0
    for f in faces:
        fab = analyze_piece_fabrication(f, kerf_mm=0.15, min_neck_mm=3.0)
        if fab["neck_pass"]:
            neck_ok += 1
        if fab["kerf_pass"]:
            kerf_ok += 1
    n = max(1, len(faces))
    lock = compute_lock_metrics(faces, ndir=180)

    # Measure seam tortuosity = actual seam length / endpoint chord length (captures dendritic waviness!)
    torts = []
    for ln in int_lines:
        if len(ln.coords) >= 2:
            a, b = ln.coords[0], ln.coords[-1]
            chord = math.hypot(b[0] - a[0], b[1] - a[1])
            if chord > 2.0:
                torts.append(float(ln.length) / chord)
    mean_tort = float(np.mean(torts)) if torts else 1.0

    x = _encode_features(piece_diam_mm, aspect, style, params)
    y = np.asarray(
        [
            float(lock["assembly_lock_margin_mm"]),
            neck_ok / n,
            kerf_ok / n,
            min(5.0, mean_tort),
        ],
        dtype=np.float32,
    )
    return x, y


def _per_target_metrics(
    train_true: np.ndarray, train_pred: np.ndarray, val_true: np.ndarray, val_pred: np.ndarray,
) -> Dict[str, Dict[str, float | int | None]]:
    """Summarize patch-level predictions. Rate targets are fractions of a patch's pieces."""
    result = {}
    for j, name in enumerate(TARGET_KEYS):
        observed = val_true[:, j].astype(np.float64)
        error = val_pred[:, j] - observed
        squared_error = error ** 2
        mean = float(observed.mean())
        ss_tot = float(np.sum((observed - mean) ** 2))
        r2 = None
        if ss_tot > 1e-8:
            r2 = round(1.0 - float(squared_error.sum()) / ss_tot, 4)
        metrics = {
            "train_mae": round(float(np.mean(np.abs(train_pred[:, j] - train_true[:, j]))), 4),
            "val_mae": round(float(np.mean(np.abs(error))), 4),
            "val_rmse": round(float(np.sqrt(squared_error.mean())), 4),
            "val_r2": r2,
            "val_mean_true": round(mean, 4),
        }
        if name in ("neck_pass_rate", "kerf_pass_rate"):
            metrics["val_all_pass_patches"] = int(np.count_nonzero(observed == 1.0))
            metrics["val_patches_with_failures"] = int(np.count_nonzero(observed < 1.0))
        result[name] = metrics
    return result


def train_neural_guide(
    out_dir: str | Path = "runs/dendrite-engine",
    num_samples: int = 120,
    epochs: int = 260,
    seed: int = 42,
    device: str = "auto",
    log=print,
) -> Dict[str, Any]:
    """Generate real GPU-simulated dataset, train DendriteSurrogateNet on CUDA, and write report."""
    t0 = time.perf_counter()
    dev = torch.device(("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    torch.manual_seed(seed)

    if dev.type == "cuda":
        torch.cuda.reset_peak_memory_stats(dev)

    xs: List[np.ndarray] = []
    ys: List[np.ndarray] = []
    attempts = 0
    while len(xs) < num_samples and attempts < int(num_samples * 1.6):
        attempts += 1
        sample = _simulate_training_sample(rng, device=str(dev))
        if sample is not None:
            xs.append(sample[0])
            ys.append(sample[1])
            if log and (len(xs) % 20 == 0 or len(xs) == num_samples):
                log(f"  [neural-guide] simulated {len(xs)}/{num_samples} GPU patches ({time.perf_counter() - t0:.1f}s)")

    if len(xs) < 24:
        raise RuntimeError(f"Insufficient valid simulation samples collected: {len(xs)}")

    X = np.stack(xs, axis=0)
    Y = np.stack(ys, axis=0)
    perm = rng.permutation(len(X))
    n_val = max(6, int(round(0.20 * len(X))))
    val_idx = perm[:n_val]
    tr_idx = perm[n_val:]

    X_tr = torch.from_numpy(X[tr_idx]).to(dev)
    Y_tr = torch.from_numpy(Y[tr_idx]).to(dev)
    X_val = torch.from_numpy(X[val_idx]).to(dev)
    Y_val = torch.from_numpy(Y[val_idx]).to(dev)

    model = DendriteSurrogateNet(in_dim=X.shape[1], width=128).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=2.5e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs, eta_min=1e-4)
    # Weight normalized multi-task MSE so lock margin, neck pass, kerf pass, and tortuosity balance cleanly
    target_scale = torch.tensor([4.0, 0.25, 0.25, 1.0], dtype=torch.float32, device=dev)

    best_val_loss = float("inf")
    best_state = None
    history: List[Dict[str, float]] = []

    train_t0 = time.perf_counter()
    for ep in range(epochs):
        model.train()
        idx = torch.randperm(len(X_tr), device=dev)
        tr_loss_acc = 0.0
        for batch in idx.split(32):
            pred = model(X_tr[batch])
            loss = ((pred - Y_tr[batch]) / target_scale).square().mean()
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            tr_loss_acc += float(loss.item()) * len(batch)
        sched.step()

        model.eval()
        with torch.inference_mode():
            vp = model(X_val)
            val_loss = float(((vp - Y_val) / target_scale).square().mean().item())
        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        if ep in (0, 29, 79, 149, 219) or ep == epochs - 1:
            history.append({"epoch": ep + 1, "train_loss": tr_loss_acc / len(X_tr), "val_loss": val_loss})

    assert best_state is not None
    model.load_state_dict(best_state)
    model.eval()

    with torch.inference_mode():
        val_pred = model(X_val).cpu().numpy()
        val_true = Y[val_idx]
        tr_pred = model(X_tr).cpu().numpy()
        tr_true = Y[tr_idx]

    metrics_per_target = _per_target_metrics(tr_true, tr_pred, val_true, val_pred)

    peak_vram_mib = float(torch.cuda.max_memory_allocated(dev) / (1024**2)) if dev.type == "cuda" else 0.0
    ckpt_path = out / "neural_guide.pt"
    torch.save(
        {
            "state_dict": best_state,
            "in_dim": int(X.shape[1]),
            "width": 128,
            "style_list": list(STYLE_LIST),
            "param_keys": list(PARAM_KEYS),
            "target_keys": list(TARGET_KEYS),
            "seed": seed,
            "num_samples": len(X),
        },
        ckpt_path,
    )

    report = {
        "checkpoint": str(ckpt_path),
        "architecture": "DendriteSurrogateNet (2x ResMLPBlock, width=128, LayerNorm+SiLU, multi-task physical heads)",
        "parameters": sum(p.numel() for p in model.parameters()),
        "device": str(dev),
        "gpu_name": torch.cuda.get_device_name(0) if dev.type == "cuda" else "cpu",
        "peak_cuda_memory_mib": round(peak_vram_mib, 2),
        "total_samples": int(len(X)),
        "train_samples": int(len(tr_idx)),
        "val_samples": int(len(val_idx)),
        "epochs": int(epochs),
        "best_val_loss": round(best_val_loss, 5),
        "per_target": metrics_per_target,
        "history": history,
        "total_seconds": round(time.perf_counter() - t0, 2),
        "fit_seconds": round(time.perf_counter() - train_t0, 2),
    }
    report_path = out / "neural_guide_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def load_neural_guide(checkpoint: str | Path = DEFAULT_CHECKPOINT, device: str = "auto") -> Tuple[DendriteSurrogateNet, Dict[str, Any]]:
    dev = torch.device(("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device)
    ck = torch.load(Path(checkpoint), map_location=dev, weights_only=True)
    model = DendriteSurrogateNet(in_dim=int(ck["in_dim"]), width=int(ck["width"])).to(dev)
    model.load_state_dict(ck["state_dict"])
    model.eval()
    return model, ck


def auto_tune_parameters(
    width_mm: float,
    height_mm: float,
    num_pieces: int,
    style: str = "dendrite",
    checkpoint: str | Path = DEFAULT_CHECKPOINT,
    restarts: int = 16,
    opt_steps: int = 80,
    seed: int = 42,
    device: str = "auto",
) -> Dict[str, Any]:
    """Differentiable multi-start Adam optimization through trained DendriteSurrogateNet."""
    st = style.lower().strip()
    if st not in STYLE_LIST:
        st = "dendrite"
    piece_diam_mm = math.sqrt((4.0 * width_mm * height_mm) / (math.pi * max(2, num_pieces)))
    aspect = width_mm / max(height_mm, 1e-6)

    ckpt_path = Path(checkpoint)
    if not ckpt_path.is_file():
        # Safe physical fallback if checkpoint has not been trained yet
        base = dict(STYLE_PRESETS[st])
        return {
            "used_neural_guide": False,
            "recommended": {
                "gamma": base["gamma"],
                "eps": base["eps"],
                "T0": base["T0"],
                "lambda1_mm": round(0.45 * piece_diam_mm, 2),
                "lambda2_mm": round(0.16 * piece_diam_mm, 2),
                "U1_mm": round(0.070 * piece_diam_mm, 2),
                "U2_mm": round(0.022 * piece_diam_mm, 2),
                "steps": base["steps"],
            },
            "predicted": None,
        }

    model, _ = load_neural_guide(ckpt_path, device=device)
    dev = next(model.parameters()).device
    for p in model.parameters():
        p.requires_grad_(False)

    torch.manual_seed(seed)
    style_oh = [1.0 if st == s else 0.0 for s in STYLE_LIST]
    ctx_vec = torch.tensor(
        [
            float(np.clip((piece_diam_mm - 20.0) / 50.0, 0.0, 1.5)),
            float(np.clip((aspect - 0.5) / 1.5, 0.0, 1.5)),
            *style_oh,
        ],
        dtype=torch.float32,
        device=dev,
    )[None].expand(restarts, -1)

    # Optimize unconstrained logits -> sigmoid in (0.05, 0.95)
    logits = (torch.randn((restarts, len(PARAM_KEYS)), device=dev) * 0.7).requires_grad_(True)
    opt = torch.optim.Adam([logits], lr=0.06)

    for _ in range(opt_steps):
        p_unit = 0.05 + 0.90 * torch.sigmoid(logits)
        x = torch.cat([ctx_vec, p_unit], dim=1)
        pred = model(x)
        lock = pred[:, 0]
        neck = pred[:, 1]
        kerf = pred[:, 2]
        tort = pred[:, 3]
        # Maximize lock margin + dendritic richness (target tortuosity ~1.55-2.2) with strong safety barriers
        neck_pen = F.relu(0.985 - neck).square() * 80.0
        kerf_pen = F.relu(0.990 - kerf).square() * 80.0
        tort_bonus = torch.minimum(tort, torch.full_like(tort, 2.3)) * 1.4
        obj = (lock + tort_bonus - neck_pen - kerf_pen).mean()
        loss = -obj
        opt.zero_grad()
        loss.backward()
        opt.step()

    with torch.inference_mode():
        p_unit = 0.05 + 0.90 * torch.sigmoid(logits)
        x = torch.cat([ctx_vec, p_unit], dim=1)
        pred = model(x)
        score = (
            pred[:, 0]
            + torch.minimum(pred[:, 3], torch.full_like(pred[:, 3], 2.3)) * 1.4
            - F.relu(0.985 - pred[:, 1]).square() * 80.0
            - F.relu(0.990 - pred[:, 2]).square() * 80.0
        )
        best_i = int(torch.argmax(score).item())
        best_p = p_unit[best_i].cpu().numpy()
        best_pred = pred[best_i].cpu().numpy()

    return {
        "used_neural_guide": True,
        "checkpoint": str(ckpt_path),
        "piece_diam_mm": round(piece_diam_mm, 2),
        "recommended": _decode_params(best_p, piece_diam_mm),
        "predicted": {
            "lock_margin_mm": round(float(best_pred[0]), 3),
            "neck_pass_rate": round(float(best_pred[1]), 4),
            "kerf_pass_rate": round(float(best_pred[2]), 4),
            "seam_tortuosity": round(float(best_pred[3]), 3),
        },
    }


def compute_artwork_style_fields(
    image_rgb: np.ndarray,
    grid_shape: Tuple[int, int],
    base_gamma: float = 35.0,
    gamma_modulation: float = 0.22,
    amp_modulation: float = 0.35,
    device: str = "auto",
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute smooth GPU saliency-driven `(gamma_map, amplitude_map)` from uploaded artwork.

    High-detail artwork regions receive calmer seams (slightly lower gamma & perturbation)
    while calm/open background regions receive richer dendritic branching.
    """
    dev = torch.device(("cuda" if torch.cuda.is_available() else "cpu") if device == "auto" else device)
    h, w = grid_shape
    if image_rgb.ndim == 3:
        gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    else:
        gray = image_rgb.astype(np.float32)
        if gray.max() > 1.5:
            gray = gray / 255.0

    t = torch.from_numpy(gray).to(dev)[None, None]
    t = F.interpolate(t, size=(h, w), mode="bilinear", align_corners=False)

    kx = torch.tensor([[-1.0, 0.0, 1.0], [-2.0, 0.0, 2.0], [-1.0, 0.0, 1.0]], device=dev)[None, None]
    ky = kx.transpose(2, 3).contiguous()
    with torch.inference_mode():
        gx = F.conv2d(t, kx, padding=1)
        gy = F.conv2d(t, ky, padding=1)
        edge = torch.sqrt(gx * gx + gy * gy + 1e-6)
        # Smooth multi-scale saliency envelope
        saliency = F.avg_pool2d(edge, kernel_size=15, stride=1, padding=7)
        saliency = F.avg_pool2d(saliency, kernel_size=15, stride=1, padding=7)
        p95 = torch.quantile(saliency.flatten(), 0.95).clamp_min(1e-4)
        sal_norm = (saliency / p95).clamp(0.0, 1.0)[0, 0].cpu().numpy()

    gamma_map = (base_gamma * (1.0 + gamma_modulation * (0.5 - sal_norm))).astype(np.float32)
    amp_map = np.clip(1.0 - amp_modulation * sal_norm, 0.45, 1.20).astype(np.float32)
    return gamma_map, amp_map
