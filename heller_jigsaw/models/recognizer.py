"""Model A (Dossier Section 4.2): silhouette CNN encoder, TTA inference, and shape-descriptor retrieval baseline."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from scipy import ndimage


def mask_to_model_input(
    mask: np.ndarray,
    sdf: Optional[np.ndarray] = None,
    size: int = 128,
) -> np.ndarray:
    """Convert a silhouette mask (and optional SDF) into a 2-channel `(2, size, size)` float32 tensor.

    Channel 0: binary mask in [0, 1].
    Channel 1: clamped normalized SDF in [-1, 1] (negative inside, positive outside).
    """
    m = (mask > 0.5).astype(np.float32)
    if sdf is None:
        fg = m > 0.5
        if np.any(fg) and not np.all(fg):
            s = (ndimage.distance_transform_edt(~fg) - ndimage.distance_transform_edt(fg)).astype(np.float32)
        else:
            s = np.zeros_like(m, dtype=np.float32)
    else:
        s = sdf.astype(np.float32)

    if m.shape != (size, size):
        scale = size / float(max(m.shape))
        m = cv2.resize(m, (size, size), interpolation=cv2.INTER_LINEAR)
        s = cv2.resize(s * scale, (size, size), interpolation=cv2.INTER_LINEAR)

    s_norm = np.clip(s / (size * 0.25), -1.0, 1.0)
    return np.stack([np.clip(m, 0.0, 1.0), s_norm], axis=0).astype(np.float32)


def fast_mask_to_model_input(mask: np.ndarray, size: int = 128) -> np.ndarray:
    """Fast OpenCV-EDT equivalent of `mask_to_model_input` for training/TTA loops."""
    if mask.shape != (size, size):
        m_u8 = (cv2.resize((mask > 0.5).astype(np.uint8) * 255, (size, size), interpolation=cv2.INTER_AREA) >= 127).astype(np.uint8)
    else:
        m_u8 = (mask > 0.5).astype(np.uint8)
    m = m_u8.astype(np.float32)
    fg_sum = int(m_u8.sum())
    if 0 < fg_sum < size * size:
        d_out = cv2.distanceTransform(1 - m_u8, cv2.DIST_L2, 5)
        d_in = cv2.distanceTransform(m_u8, cv2.DIST_L2, 5)
        s = d_out - d_in
    else:
        s = np.zeros((size, size), dtype=np.float32)
    s_norm = np.clip(s / (size * 0.25), -1.0, 1.0)
    return np.stack([m, s_norm], axis=0).astype(np.float32)


def normalize_mask_frame(mask: np.ndarray, size: int = 128, pad_frac: float = 0.10) -> np.ndarray:
    """Center and uniformly scale foreground bounding box into `(size, size)` binary float32 mask."""
    fg = mask > 0.5
    ys, xs = np.where(fg)
    if len(xs) == 0:
        return np.zeros((size, size), dtype=np.float32)
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    crop = fg[y0:y1, x0:x1].astype(np.uint8) * 255
    bh, bw = crop.shape
    usable = max(2, int(round(size * (1.0 - 2.0 * pad_frac))))
    scale = usable / float(max(bh, bw))
    nw = max(1, min(size - 2, int(round(bw * scale))))
    nh = max(1, min(size - 2, int(round(bh * scale))))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(crop, (nw, nh), interpolation=interp) >= 127
    canvas = np.zeros((size, size), dtype=np.float32)
    ox = (size - nw) // 2
    oy = (size - nh) // 2
    canvas[oy : oy + nh, ox : ox + nw] = resized.astype(np.float32)
    return canvas


def make_tta_views(
    mask: np.ndarray,
    size: int = 128,
    n_rotations: int = 8,
    include_flips: bool = True,
) -> np.ndarray:
    """Generate `(V, 2, size, size)` orientation-invariant TTA inputs (rotations x optional flip).

    Rotates on an unclipped padded canvas and re-frames each rotated view with `pad_frac=0.10`
    so every TTA view matches the exact bounding-box framing used during training.
    """
    base = normalize_mask_frame(mask, size=size, pad_frac=0.10)
    # Pad canvas by ~50% so diagonal rotations of wide silhouettes never clip at borders
    pad = size // 2
    padded_sz = size + 2 * pad
    angles = np.linspace(0.0, 360.0, max(1, int(n_rotations)), endpoint=False)
    views: List[np.ndarray] = []
    flips = (False, True) if include_flips else (False,)
    for flip in flips:
        src = base[:, ::-1].copy() if flip else base
        src_pad = np.pad(src, pad, mode="constant", constant_values=0.0)
        for ang in angles:
            if abs(ang) < 1e-6:
                rot = src
            else:
                M = cv2.getRotationMatrix2D((padded_sz * 0.5, padded_sz * 0.5), float(ang), 1.0)
                rot_pad = (
                    cv2.warpAffine(src_pad, M, (padded_sz, padded_sz), flags=cv2.INTER_LINEAR, borderValue=0.0) >= 0.5
                ).astype(np.float32)
                rot = normalize_mask_frame(rot_pad, size=size, pad_frac=0.10)
            views.append(fast_mask_to_model_input(rot, size=size))
    return np.stack(views, axis=0).astype(np.float32)


# ---------------------------------------------------------------------------
# Deterministic rotation/scale-invariant shape descriptor retrieval baseline
# ---------------------------------------------------------------------------

def extract_shape_descriptor(mask: np.ndarray, n_radial_bins: int = 64, n_fft: int = 16) -> np.ndarray:
    """Extract a 28-dim rotation/translation/scale-invariant silhouette descriptor (Hu + radial FFT + geometry)."""
    fg = (mask > 0.5).astype(np.uint8)
    area = float(np.sum(fg))
    if area < 4.0:
        return np.zeros(7 + n_fft + 5, dtype=np.float32)

    # 1. Log-scaled Hu moments (7 dims)
    moments = cv2.moments(fg)
    hu = cv2.HuMoments(moments).ravel()
    hu_log = np.sign(hu) * np.log10(np.abs(hu) + 1e-12)
    hu_log = np.clip(hu_log / 10.0, -2.0, 2.0)

    # 2. Boundary radial profile FFT magnitudes (rotation-invariant, n_fft dims)
    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if contours:
        cnt = max(contours, key=cv2.contourArea)
        pts = cnt.reshape(-1, 2).astype(np.float64)
        perim = float(cv2.arcLength(cnt, closed=True))
        hull = cv2.convexHull(cnt)
        hull_area = max(float(cv2.contourArea(hull)), 1.0)
        solidity = area / hull_area
        rect = cv2.minAreaRect(cnt)
        rw, rh = max(rect[1][0], 1.0), max(rect[1][1], 1.0)
        aspect = min(rw, rh) / max(rw, rh)
        extent = area / max(rw * rh, 1.0)
        compactness = (4.0 * math.pi * area) / max(perim * perim, 1.0)

        cx = float(moments["m10"] / max(moments["m00"], 1.0))
        cy = float(moments["m01"] / max(moments["m00"], 1.0))
        idx = np.linspace(0, len(pts) - 1, n_radial_bins).astype(np.int32)
        radii = np.hypot(pts[idx, 0] - cx, pts[idx, 1] - cy)
        r_mean = max(float(np.mean(radii)), 1e-6)
        r_norm = radii / r_mean
        fft_mag = np.abs(np.fft.rfft(r_norm))[:n_fft] / float(n_radial_bins)
        fft_mag = fft_mag / max(float(fft_mag[0]), 1e-6)
        geom = np.array([solidity, aspect, extent, compactness, math.sqrt(area) / float(mask.shape[0])], dtype=np.float64)
    else:
        fft_mag = np.zeros(n_fft, dtype=np.float64)
        geom = np.zeros(5, dtype=np.float64)

    feat = np.concatenate([hu_log, fft_mag, geom]).astype(np.float32)
    norm = float(np.linalg.norm(feat))
    return (feat / norm) if norm > 1e-8 else feat


class ShapeDescriptorBaseline:
    """Zero-training cosine-similarity kNN retrieval baseline on silhouette shape descriptors."""

    def __init__(self):
        self.bank_feats: Optional[np.ndarray] = None
        self.bank_labels: List[str] = []
        self.bank_families: List[str] = []
        self.bank_ids: List[str] = []

    def fit(
        self,
        masks: Sequence[np.ndarray],
        labels: Sequence[str],
        families: Sequence[str],
        asset_ids: Sequence[str],
    ) -> "ShapeDescriptorBaseline":
        feats = [extract_shape_descriptor(m) for m in masks]
        self.bank_feats = np.stack(feats, axis=0).astype(np.float32) if feats else np.zeros((0, 28), dtype=np.float32)
        self.bank_labels = list(labels)
        self.bank_families = list(families)
        self.bank_ids = list(asset_ids)
        return self

    def query(self, mask: np.ndarray, top_k: int = 5) -> List[Dict[str, object]]:
        if self.bank_feats is None or len(self.bank_feats) == 0:
            return []
        q = extract_shape_descriptor(mask)
        sims = self.bank_feats @ q
        order = np.argsort(sims)[::-1][:top_k]
        return [
            {
                "asset_id": self.bank_ids[int(i)],
                "motif_class": self.bank_labels[int(i)],
                "family": self.bank_families[int(i)],
                "similarity": float(sims[int(i)]),
            }
            for i in order
        ]


# ---------------------------------------------------------------------------
# Model A: PyTorch Silhouette CNN Encoder + Cosine-Scaled Family & Class Heads
# ---------------------------------------------------------------------------

class _ResBlock(nn.Module):
    """Legacy v1 BatchNorm residual block (kept for loading v1 checkpoints)."""

    def __init__(self, in_ch: int, out_ch: int, stride: int = 1):
        super().__init__()
        self.conv1 = nn.Conv2d(in_ch, out_ch, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(out_ch)
        self.conv2 = nn.Conv2d(out_ch, out_ch, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(out_ch)
        self.shortcut = (
            nn.Sequential(
                nn.Conv2d(in_ch, out_ch, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(out_ch),
            )
            if (stride != 1 or in_ch != out_ch)
            else nn.Identity()
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = F.silu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        return F.silu(out + self.shortcut(x))


def _gn(ch: int) -> nn.GroupNorm:
    groups = 8 if ch % 8 == 0 else (4 if ch % 4 == 0 else 1)
    return nn.GroupNorm(groups, ch)


class _GNResBlock(nn.Module):
    """Domain-shift-resilient GroupNorm residual block (no running BN stats mismatch on thick puzzle pieces)."""

    def __init__(self, in_ch: int, out_ch: int, stride: int = 1):
        super().__init__()
        self.conv1 = nn.Conv2d(in_ch, out_ch, kernel_size=3, stride=stride, padding=1, bias=False)
        self.gn1 = _gn(out_ch)
        self.conv2 = nn.Conv2d(out_ch, out_ch, kernel_size=3, stride=1, padding=1, bias=False)
        self.gn2 = _gn(out_ch)
        self.shortcut = (
            nn.Sequential(
                nn.Conv2d(in_ch, out_ch, kernel_size=1, stride=stride, bias=False),
                _gn(out_ch),
            )
            if (stride != 1 or in_ch != out_ch)
            else nn.Identity()
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = F.silu(self.gn1(self.conv1(x)))
        out = self.gn2(self.conv2(out))
        return F.silu(out + self.shortcut(x))


class _CosineHead(nn.Module):
    """Cosine-similarity classifier with learnable logit scale so unit-norm embeddings produce sharp logits."""

    def __init__(self, in_features: int, out_features: int, init_scale: float = 12.0):
        super().__init__()
        self.weight = nn.Parameter(torch.empty(out_features, in_features))
        self.bias = nn.Parameter(torch.zeros(out_features))
        self.log_scale = nn.Parameter(torch.tensor(math.log(init_scale), dtype=torch.float32))
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))

    def forward(self, emb: torch.Tensor) -> torch.Tensor:
        w = F.normalize(self.weight, p=2, dim=1)
        scale = self.log_scale.exp().clamp(2.0, 32.0)
        return scale * F.linear(emb, w) + self.bias


class HellerSilhouetteEncoder(nn.Module):
    """Silhouette CNN outputting L2-normalized embedding + family/class logits.

    `arch_version=2` (default): GroupNorm residual backbone + boundary-invariant SDF lifting
    (exp(-8|sdf|) and tanh(4*sdf)) + learnable-scale cosine heads.
    `arch_version=1`: Legacy BatchNorm + unscaled linear head (for loading v1 checkpoints).
    """

    def __init__(
        self,
        num_families: int,
        num_classes: int,
        in_channels: int = 2,
        embed_dim: int = 128,
        dropout: float = 0.25,
        arch_version: int = 2,
    ):
        super().__init__()
        self.embed_dim = embed_dim
        self.in_channels = in_channels
        self.arch_version = int(arch_version)

        if self.arch_version == 1:
            self.stem = nn.Sequential(
                nn.Conv2d(in_channels, 32, kernel_size=5, stride=2, padding=2, bias=False),
                nn.BatchNorm2d(32),
                nn.SiLU(),
            )
            self.stage1 = _ResBlock(32, 64, stride=2)
            self.stage2 = _ResBlock(64, 128, stride=2)
            self.stage3 = _ResBlock(128, 192, stride=2)
            self.stage4 = _ResBlock(192, 192, stride=2)
            self.proj = nn.Sequential(
                nn.Linear(192 * 2, 256),
                nn.BatchNorm1d(256),
                nn.SiLU(),
                nn.Dropout(dropout),
                nn.Linear(256, embed_dim),
            )
            self.family_head: nn.Module = nn.Linear(embed_dim, num_families)
            self.class_head: nn.Module = nn.Linear(embed_dim, num_classes)
        else:
            # Lift standard 2-channel (mask, sdf) into 4 boundary-focused channels internally
            eff_in = 4 if in_channels == 2 else in_channels
            self.stem = nn.Sequential(
                nn.Conv2d(eff_in, 32, kernel_size=5, stride=2, padding=2, bias=False),
                _gn(32),
                nn.SiLU(),
            )
            self.stage1 = _GNResBlock(32, 64, stride=2)
            self.stage2 = nn.Sequential(
                _GNResBlock(64, 128, stride=2),
                _GNResBlock(128, 128, stride=1),
            )
            self.stage3 = nn.Sequential(
                _GNResBlock(128, 192, stride=2),
                _GNResBlock(192, 192, stride=1),
            )
            self.stage4 = _GNResBlock(192, 256, stride=2)
            self.proj = nn.Sequential(
                nn.Linear(256 * 2, 256),
                nn.LayerNorm(256),
                nn.SiLU(),
                nn.Dropout(dropout),
                nn.Linear(256, embed_dim),
            )
            self.family_head = _CosineHead(embed_dim, num_families, init_scale=10.0)
            self.class_head = _CosineHead(embed_dim, num_classes, init_scale=12.0)

    def _lift_input(self, x: torch.Tensor) -> torch.Tensor:
        if self.arch_version == 1 or x.shape[1] != 2:
            return x
        mask = x[:, 0:1]
        sdf = x[:, 1:2]
        # Crisp boundary-band + interior-saturated SDF reduce sensitivity to piece thickness
        boundary = torch.exp(-8.0 * sdf.abs())
        local_sdf = torch.tanh(4.0 * sdf)
        return torch.cat([mask, sdf, boundary, local_sdf], dim=1)

    def forward(self, x: torch.Tensor) -> Dict[str, torch.Tensor]:
        h = self.stem(self._lift_input(x))
        h = self.stage1(h)
        h = self.stage2(h)
        h = self.stage3(h)
        h = self.stage4(h)
        gap = h.mean(dim=(2, 3))
        gmp = h.amax(dim=(2, 3))
        pooled = torch.cat([gap, gmp], dim=1)
        emb = F.normalize(self.proj(pooled), p=2, dim=1)
        return {
            "embedding": emb,
            "family_logits": self.family_head(emb),
            "class_logits": self.class_head(emb),
        }


def load_recognizer_checkpoint(
    checkpoint_path: str | Path,
    device: str | torch.device = "cpu",
) -> Tuple[HellerSilhouetteEncoder, Dict[str, Any]]:
    """Load any v1 or v2 Model A checkpoint cleanly and return `(model, metadata)`."""
    ckpt = torch.load(str(checkpoint_path), map_location="cpu", weights_only=False)
    sd = ckpt["state_dict"]
    arch_version = int(ckpt.get("arch_version", 2 if "class_head.log_scale" in sd else 1))
    families = list(ckpt["families"])
    classes = list(ckpt["classes"])
    embed_dim = int(ckpt.get("embed_dim", 128))
    model = HellerSilhouetteEncoder(
        num_families=len(families),
        num_classes=len(classes),
        embed_dim=embed_dim,
        arch_version=arch_version,
    )
    model.load_state_dict(sd)
    model.to(device)
    model.eval()
    meta: Dict[str, Any] = {
        "families": families,
        "classes": classes,
        "img_size": int(ckpt.get("img_size", 128)),
        "embed_dim": embed_dim,
        "arch_version": arch_version,
        "temperature_family": float(ckpt.get("temperature_family", 1.0)),
        "temperature_class": float(ckpt.get("temperature_class", 1.0)),
        "class_prototypes": ckpt.get("class_prototypes"),
        "family_prototypes": ckpt.get("family_prototypes"),
        "train_embeddings": ckpt.get("train_embeddings"),
        "train_classes": ckpt.get("train_classes"),
        "train_families": ckpt.get("train_families"),
    }
    return model, meta


@torch.inference_mode()
def predict_silhouette(
    model: HellerSilhouetteEncoder,
    mask: np.ndarray,
    families: Sequence[str],
    classes: Sequence[str],
    img_size: int = 128,
    tta_rotations: int = 8,
    include_flips: bool = True,
    temperature_family: float = 1.0,
    temperature_class: float = 1.0,
    class_prototypes: Optional[np.ndarray] = None,
    device: Optional[str | torch.device] = None,
) -> Dict[str, Any]:
    """Run calibrated single-view or orientation-invariant TTA inference on a single silhouette mask."""
    dev = torch.device(device) if device is not None else next(model.parameters()).device
    model.eval()
    if tta_rotations > 1 or include_flips:
        batch_np = make_tta_views(mask, size=img_size, n_rotations=tta_rotations, include_flips=include_flips)
    else:
        batch_np = mask_to_model_input(normalize_mask_frame(mask, size=img_size), size=img_size)[None, ...]

    bx = torch.from_numpy(batch_np).to(dev)
    out = model(bx)

    emb = F.normalize(out["embedding"].mean(dim=0, keepdim=True), p=2, dim=1)[0].cpu().numpy()
    mean_f_logits = out["family_logits"].mean(dim=0)
    mean_c_logits = out["class_logits"].mean(dim=0)
    f_probs = F.softmax(mean_f_logits / max(0.05, float(temperature_family)), dim=0).cpu().numpy()
    c_probs = F.softmax(mean_c_logits / max(0.05, float(temperature_class)), dim=0).cpu().numpy()

    top_f = int(np.argmax(f_probs))
    top_c_order = np.argsort(c_probs)[::-1]
    top3_c = [classes[int(k)] for k in top_c_order[: min(3, len(classes))]]

    result: Dict[str, Any] = {
        "embedding": emb,
        "family_probs": f_probs,
        "class_probs": c_probs,
        "pred_family": families[top_f],
        "pred_family_conf": float(f_probs[top_f]),
        "pred_class_top1": classes[int(top_c_order[0])],
        "pred_class_conf": float(c_probs[int(top_c_order[0])]),
        "pred_class_top3": top3_c,
    }
    if class_prototypes is not None and len(class_prototypes) == len(classes):
        proto = np.asarray(class_prototypes, dtype=np.float32)
        sims = proto @ emb
        p_order = np.argsort(sims)[::-1]
        result["max_prototype_sim"] = float(sims[int(p_order[0])])
        result["proto_class_top1"] = classes[int(p_order[0])]
        result["proto_class_top3"] = [classes[int(k)] for k in p_order[: min(3, len(classes))]]
    return result
