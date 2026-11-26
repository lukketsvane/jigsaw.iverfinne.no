"""Leak-free training & evaluation pipeline for Model A (Helleristninger & Animal Silhouette Recognizer)."""

from __future__ import annotations

import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from PIL import Image
from scipy import ndimage
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler

from ..data.corpus import MotifAsset, load_corpus_catalog, load_motif_asset
from .recognizer import (
    HellerSilhouetteEncoder,
    ShapeDescriptorBaseline,
    fast_mask_to_model_input,
    load_recognizer_checkpoint,
    make_tta_views,
    mask_to_model_input,
    normalize_mask_frame,
    predict_silhouette,
)
from .training_state import EpochCheckpoint, catalog_digest, save_atomic


def _verify_leak_free_splits(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Assert zero `source_group` and zero `sha256` overlap across train/val/test splits."""
    sg_by_split: Dict[str, set[str]] = {"train": set(), "val": set(), "test": set()}
    sha_by_split: Dict[str, set[str]] = {"train": set(), "val": set(), "test": set()}
    counts: Dict[str, int] = {"train": 0, "val": 0, "test": 0}

    for r in records:
        sp = str(r.get("split", "train"))
        if sp not in counts:
            raise ValueError(f"Unknown dataset split {sp!r} for {r.get('asset_id')}")
        counts[sp] += 1
        if r.get("source_group"):
            sg_by_split[sp].add(str(r["source_group"]))
        if r.get("sha256"):
            sha_by_split[sp].add(str(r["sha256"]))

    splits = ("train", "val", "test")
    for i in range(3):
        for j in range(i + 1, 3):
            s1, s2 = splits[i], splits[j]
            sg_overlap = sg_by_split[s1] & sg_by_split[s2]
            sha_overlap = sha_by_split[s1] & sha_by_split[s2]
            if sg_overlap:
                raise ValueError(f"Split leakage detected! Overlapping source_group between {s1} and {s2}: {len(sg_overlap)}")
            if sha_overlap:
                raise ValueError(f"Split leakage detected! Overlapping sha256 between {s1} and {s2}: {len(sha_overlap)}")

    return {
        "leak_free_verified": True,
        "split_counts": counts,
        "unique_source_groups": {k: len(v) for k, v in sg_by_split.items()},
    }


# ---------------------------------------------------------------------------
# Anatomically-plausible geometric + puzzle-piece domain augmentations
# ---------------------------------------------------------------------------

def _keep_largest_filled(mask_bool: np.ndarray) -> np.ndarray:
    if not mask_bool.any():
        return mask_bool
    filled = ndimage.binary_fill_holes(mask_bool)
    labels, n = ndimage.label(filled)
    if n <= 1:
        return filled
    counts = np.bincount(labels.ravel())[1:]
    return labels == (int(np.argmax(counts)) + 1)


_MESHGRID_CACHE: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}


def _get_pixel_grid(size: int) -> Tuple[np.ndarray, np.ndarray]:
    if size not in _MESHGRID_CACHE:
        _MESHGRID_CACHE[size] = np.meshgrid(
            np.arange(size, dtype=np.float32), np.arange(size, dtype=np.float32), indexing="ij"
        )
    return _MESHGRID_CACHE[size]


def augment_silhouette_mask(
    mask: np.ndarray,
    rng: np.random.Generator,
    size: int = 128,
    puzzle_deform: bool = False,
) -> np.ndarray:
    """Transform a binary silhouette mask into a clean rotated view or puzzle-piece-deformed view."""
    base = (mask > 0.5).astype(np.float32)
    if base.shape != (size, size):
        base = normalize_mask_frame(base, size=size, pad_frac=0.10)

    if rng.random() < 0.5:
        base = base[:, ::-1].copy()

    # Full 360 rotation + mild anisotropic stretch
    angle = float(rng.uniform(-180.0, 180.0))
    if puzzle_deform:
        sx = float(rng.uniform(0.84, 1.16))
        sy = float(rng.uniform(0.84, 1.16))
        ratio = max(sx / sy, sy / sx)
        if ratio > 1.28:
            mid = math.sqrt(sx * sy)
            sx = mid * (1.13 if sx > sy else 1.0 / 1.13)
            sy = mid * (1.0 / 1.13 if sx > sy else 1.13)
    else:
        sx = float(rng.uniform(0.90, 1.10))
        sy = float(rng.uniform(0.90, 1.10))

    # Build centered rotate + anisotropic scale matrix
    cx, cy = size * 0.5, size * 0.5
    rad = math.radians(angle)
    ca, sa = math.cos(rad), math.sin(rad)
    M = np.array(
        [
            [ca * sx, -sa * sy, cx - cx * ca * sx + cy * sa * sy],
            [sa * sx, ca * sy, cy - cx * sa * sx - cy * ca * sy],
        ],
        dtype=np.float32,
    )
    warped = cv2.warpAffine(base, M, (size, size), flags=cv2.INTER_LINEAR, borderValue=0.0) >= 0.5
    if not warped.any():
        return normalize_mask_frame(base, size=size, pad_frac=0.10)

    clean_fallback = normalize_mask_frame(warped.astype(np.float32), size=size, pad_frac=0.10)
    if not puzzle_deform:
        return clean_fallback

    m = warped.astype(np.uint8)
    fill = float(m.mean())
    scale_f = size / 128.0

    # 1. Morphological thickening / closing to bridge thin rock-art tracings -> solid puzzle pieces
    if rng.random() < 0.80:
        if fill < 0.28:
            max_r = max(2, int(round((7 if fill < 0.18 else 5) * scale_f)))
            r = int(rng.integers(1, max_r + 1))
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
            m = cv2.dilate(m, k, iterations=1)
        elif rng.random() < 0.35:
            r = max(1, int(round(1.5 * scale_f)))
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
            m = cv2.erode(m, k, iterations=1)
        if rng.random() < 0.55:
            rc = max(2, int(round(float(rng.uniform(2.0, 5.5)) * scale_f)))
            kc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rc + 1, 2 * rc + 1))
            m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kc)
        m = _keep_largest_filled(m > 0).astype(np.uint8)

    # 2. Smooth low-frequency elastic boundary warp (mimics geodesic Voronoi cell distortion)
    if rng.random() < 0.72:
        grid_n = 4
        amp = float(rng.uniform(1.8, 5.5)) * scale_f
        dx_low = rng.normal(0.0, amp, size=(grid_n, grid_n)).astype(np.float32)
        dy_low = rng.normal(0.0, amp, size=(grid_n, grid_n)).astype(np.float32)
        dx = cv2.resize(dx_low, (size, size), interpolation=cv2.INTER_CUBIC)
        dy = cv2.resize(dy_low, (size, size), interpolation=cv2.INTER_CUBIC)
        yy, xx = _get_pixel_grid(size)
        remapped = cv2.remap(
            m.astype(np.float32),
            xx + dx,
            yy + dy,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0.0,
        )
        m = _keep_largest_filled(remapped >= 0.5).astype(np.uint8)

    # 3. Partial outer-frame edge flattening / chord trim (simulates edge pieces touching border)
    if rng.random() < 0.35 and m.any():
        ys, xs = np.where(m > 0)
        y0, y1 = int(ys.min()), int(ys.max())
        x0, x1 = int(xs.min()), int(xs.max())
        frac = float(rng.uniform(0.04, 0.13))
        side = int(rng.integers(0, 4))
        m_clip = m.copy()
        if side == 0 and (y1 - y0) > 10:
            m_clip[: y0 + int((y1 - y0) * frac), :] = 0
        elif side == 1 and (y1 - y0) > 10:
            m_clip[y1 - int((y1 - y0) * frac) :, :] = 0
        elif side == 2 and (x1 - x0) > 10:
            m_clip[:, : x0 + int((x1 - x0) * frac)] = 0
        elif (x1 - x0) > 10:
            m_clip[:, x1 - int((x1 - x0) * frac) :] = 0
        if m_clip.sum() >= 0.78 * m.sum():
            m = _keep_largest_filled(m_clip > 0).astype(np.uint8)

    pad_frac = float(rng.uniform(0.08, 0.13))
    out = normalize_mask_frame(m.astype(np.float32), size=size, pad_frac=pad_frac)
    area_frac = float(out.mean())
    if not (0.015 <= area_frac <= 0.68):
        return clean_fallback
    return out


class _TwoViewSilhouetteDataset(Dataset):
    """Yields `(view_clean, view_puzzle, fam_id, cls_id)` per asset for domain-bridging contrastive training."""

    def __init__(
        self,
        masks: List[np.ndarray],
        fam_ids: List[int],
        cls_ids: List[int],
        img_size: int = 128,
        puzzle_aug: bool = True,
        seed: int = 42,
    ):
        self.masks = masks
        self.fam_ids = fam_ids
        self.cls_ids = cls_ids
        self.img_size = img_size
        self.puzzle_aug = puzzle_aug
        self.rng = np.random.default_rng(seed)

    def __len__(self) -> int:
        return len(self.masks)

    def __getitem__(self, idx: int):
        m = self.masks[idx]
        m1 = augment_silhouette_mask(m, self.rng, size=self.img_size, puzzle_deform=False)
        m2 = augment_silhouette_mask(m, self.rng, size=self.img_size, puzzle_deform=self.puzzle_aug)
        x1 = fast_mask_to_model_input(m1, size=self.img_size)
        x2 = fast_mask_to_model_input(m2, size=self.img_size)
        return (
            torch.from_numpy(x1),
            torch.from_numpy(x2),
            torch.tensor(self.fam_ids[idx], dtype=torch.long),
            torch.tensor(self.cls_ids[idx], dtype=torch.long),
        )


# ---------------------------------------------------------------------------
# Metrics, confusion matrices, calibration & hard-negative rejection
# ---------------------------------------------------------------------------

def _classification_metrics(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    label_names: Sequence[str],
    include_confusion: bool = False,
) -> Dict[str, Any]:
    """Compute accuracy, balanced accuracy, macro-F1, per-class metrics, and optional confusion matrix."""
    y_t = np.asarray(y_true, dtype=np.int64)
    y_p = np.asarray(y_pred, dtype=np.int64)
    if len(y_t) == 0:
        return {"count": 0, "accuracy": 0.0, "balanced_accuracy": 0.0, "macro_f1": 0.0, "per_class": {}}

    acc = float(np.mean(y_t == y_p))
    present = sorted(set(int(v) for v in y_t))
    recalls: List[float] = []
    f1s: List[float] = []
    per_class: Dict[str, Dict[str, Any]] = {}

    for c in present:
        tp = int(np.sum((y_t == c) & (y_p == c)))
        fp = int(np.sum((y_t != c) & (y_p == c)))
        fn = int(np.sum((y_t == c) & (y_p != c)))
        prec = tp / float(tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / float(tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2.0 * prec * rec / (prec + rec)) if (prec + rec) > 0 else 0.0
        recalls.append(rec)
        f1s.append(f1)
        name = label_names[c] if c < len(label_names) else str(c)
        per_class[name] = {
            "support": int(tp + fn),
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(f1, 4),
        }

    out: Dict[str, Any] = {
        "count": int(len(y_t)),
        "accuracy": round(acc, 4),
        "balanced_accuracy": round(float(np.mean(recalls)) if recalls else 0.0, 4),
        "macro_f1": round(float(np.mean(f1s)) if f1s else 0.0, 4),
        "per_class": per_class,
    }
    if include_confusion:
        k = len(label_names)
        cm = np.zeros((k, k), dtype=np.int64)
        for t, p in zip(y_t, y_p):
            if 0 <= t < k and 0 <= p < k:
                cm[t, p] += 1
        out["confusion_labels"] = list(label_names)
        out["confusion_matrix"] = cm.tolist()
    return out


def _expected_calibration_error(probs: np.ndarray, y_true: Sequence[int], n_bins: int = 10) -> float:
    if len(y_true) == 0:
        return 0.0
    y_t = np.asarray(y_true, dtype=np.int64)
    conf = np.max(probs, axis=1)
    pred = np.argmax(probs, axis=1)
    correct = (pred == y_t).astype(np.float64)
    ece = 0.0
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    for b0, b1 in zip(bins[:-1], bins[1:]):
        mask = (conf >= b0) & (conf < b1 if b1 < 1.0 else conf <= b1)
        if np.any(mask):
            ece += float(np.mean(mask)) * abs(float(np.mean(correct[mask])) - float(np.mean(conf[mask])))
    return round(float(ece), 4)


def _fit_temperature(logits: np.ndarray, y_true: Sequence[int]) -> float:
    """Grid-search scalar softmax temperature T in [0.4, 4.0] minimizing validation NLL."""
    if len(y_true) < 4 or logits.shape[1] <= 1:
        return 1.0
    lg = torch.from_numpy(logits.astype(np.float32))
    yt = torch.tensor(list(y_true), dtype=torch.long)
    best_t, best_nll = 1.0, float("inf")
    for t in np.geomspace(0.4, 4.0, 45):
        nll = float(F.cross_entropy(lg / float(t), yt).item())
        if nll < best_nll:
            best_nll = nll
            best_t = float(t)
    return round(best_t, 4)


def _make_single_hard_negative(rng: np.random.Generator, size: int = 128) -> np.ndarray:
    """Generate one synthetic non-figurative hard-negative mask (ellipse, box, harmonic/Gaussian blob)."""
    yy, xx = np.meshgrid(np.linspace(-1.0, 1.0, size), np.linspace(-1.0, 1.0, size), indexing="ij")
    theta = np.arctan2(yy, xx)
    rad = np.hypot(xx, yy)
    kind = int(rng.integers(0, 4))
    if kind == 0:
        rx = float(rng.uniform(0.42, 0.82))
        ry = float(rng.uniform(0.32, 0.82))
        ang = float(rng.uniform(0, math.pi))
        xr = xx * math.cos(ang) + yy * math.sin(ang)
        yr = -xx * math.sin(ang) + yy * math.cos(ang)
        m = ((xr / rx) ** 2 + (yr / ry) ** 2) <= 1.0
    elif kind == 1:
        w = float(rng.uniform(0.32, 0.80))
        h = float(rng.uniform(0.28, 0.78))
        ang = float(rng.uniform(0, math.pi))
        xr = xx * math.cos(ang) + yy * math.sin(ang)
        yr = -xx * math.sin(ang) + yy * math.cos(ang)
        m = (np.abs(xr) <= w) & (np.abs(yr) <= h)
    elif kind == 2:
        r_b = np.full_like(theta, 0.58)
        for k in range(2, 6):
            a = float(rng.uniform(-0.14, 0.14))
            ph = float(rng.uniform(0, 2.0 * math.pi))
            r_b = r_b + a * np.cos(k * theta + ph)
        m = rad <= np.clip(r_b, 0.20, 0.85)
    else:
        noise = rng.normal(0.0, 1.0, size=(size, size)).astype(np.float32)
        smooth = cv2.GaussianBlur(noise, (0, 0), sigmaX=float(rng.uniform(9.0, 18.0)))
        falloff = smooth - 0.85 * (rad ** 2)
        thr = float(np.percentile(falloff, float(rng.uniform(62.0, 80.0))))
        m = _keep_largest_filled(falloff >= thr)
    return normalize_mask_frame(m.astype(np.float32), size=size, pad_frac=0.10)


def _generate_hard_negative_masks(
    n: int = 64,
    size: int = 128,
    seed: int = 999,
    include_unseen_shapes: bool = True,
) -> List[np.ndarray]:
    """Generate deterministic non-figurative hard negatives (blobs + unseen geometric/Voronoi cells)."""
    rng = np.random.default_rng(seed)
    out: List[np.ndarray] = []
    for i in range(n):
        if include_unseen_shapes and (i % 2 == 1):
            # Unseen non-figurative shapes (convex 3-6-gon Voronoi-like cells & smooth L-wedges)
            canvas = np.zeros((size, size), dtype=np.uint8)
            if (i // 2) % 2 == 0:
                n_v = int(rng.integers(3, 7))
                angs = np.sort(rng.uniform(0.0, 2.0 * math.pi, size=n_v))
                rads = rng.uniform(0.26 * size, 0.44 * size, size=n_v)
                pts = np.stack(
                    [size * 0.5 + rads * np.cos(angs), size * 0.5 + rads * np.sin(angs)], axis=1
                ).astype(np.int32)
                cv2.fillPoly(canvas, [pts], 255)
            else:
                # Random clipped wedge / crescent-like non-animal polygon
                ang0 = float(rng.uniform(0.0, 2.0 * math.pi))
                span = float(rng.uniform(1.1, 2.5))
                ts = np.linspace(ang0, ang0 + span, 18)
                r_out = float(rng.uniform(0.36, 0.44)) * size
                r_in = float(rng.uniform(0.10, 0.22)) * size
                pts_out = np.stack([size * 0.5 + r_out * np.cos(ts), size * 0.5 + r_out * np.sin(ts)], axis=1)
                pts_in = np.stack([size * 0.5 + r_in * np.cos(ts[::-1]), size * 0.5 + r_in * np.sin(ts[::-1])], axis=1)
                pts = np.vstack([pts_out, pts_in]).astype(np.int32)
                cv2.fillPoly(canvas, [pts], 255)
            out.append(normalize_mask_frame((canvas > 127).astype(np.float32), size=size, pad_frac=0.10))
        else:
            out.append(_make_single_hard_negative(rng, size=size))
    return out


def _binary_auroc(pos_scores: Sequence[float], neg_scores: Sequence[float]) -> float:
    if not pos_scores or not neg_scores:
        return 0.5
    p = np.asarray(pos_scores, dtype=np.float64)
    n = np.asarray(neg_scores, dtype=np.float64)
    wins = (p[:, None] > n[None, :]).astype(np.float64) + 0.5 * (p[:, None] == n[None, :]).astype(np.float64)
    return round(float(wins.mean()), 4)


def _supcon_loss(embeddings: torch.Tensor, labels: torch.Tensor, temp: float = 0.15) -> torch.Tensor:
    """Supervised contrastive loss over L2-normalized embeddings."""
    b = embeddings.shape[0]
    if b <= 1:
        return embeddings.new_tensor(0.0)
    sim = (embeddings @ embeddings.T) / temp
    eye = torch.eye(b, device=embeddings.device, dtype=torch.bool)
    sim = sim.masked_fill(eye, -1e9)
    pos_mask = (labels.unsqueeze(0) == labels.unsqueeze(1)) & (~eye)
    has_pos = pos_mask.any(dim=1)
    if not has_pos.any():
        return embeddings.new_tensor(0.0)
    log_prob = sim - torch.logsumexp(sim, dim=1, keepdim=True)
    mean_log_pos = (log_prob * pos_mask.float()).sum(dim=1) / pos_mask.float().sum(dim=1).clamp(min=1.0)
    return -mean_log_pos[has_pos].mean()


def _damped_class_weights(ids: List[int], k: int, power: float = 0.35, max_ratio: float = 3.5) -> np.ndarray:
    cnt = Counter(ids)
    w = np.ones(k, dtype=np.float32)
    for i in range(k):
        w[i] = 1.0 / (max(1, cnt.get(i, 1)) ** power)
    w = w / float(np.min(w))
    w = np.clip(w, 1.0, max_ratio)
    w = w / float(np.mean(w))
    return w


def train_and_evaluate_recognizer(
    catalog_path: str | Path,
    out_dir: str | Path,
    epochs: int = 60,
    batch_size: int = 32,
    lr: float = 1.5e-3,
    img_size: int = 128,
    seed: int = 42,
    exclude_unclassified: bool = True,
    init_from: Optional[str | Path] = None,
    resume: bool = False,
    sampling: str = "sqrt-balanced",
    puzzle_aug: bool = True,
    tta_rotations: int = 8,
    eval_puzzles: Sequence[str | Path] = (),
) -> Dict[str, Any]:
    """Train Model A v2 CNN with puzzle-domain augmentations & evaluate on clean, distorted, and negative sets."""
    t0 = time.perf_counter()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if resume and init_from:
        raise ValueError("Choose either a full resume or an initialization checkpoint")
    if batch_size < 2 or sampling not in ("shuffle", "sqrt-balanced"):
        raise ValueError("Use batch_size >= 2 and sampling shuffle or sqrt-balanced")
    torch.manual_seed(seed)
    np.random.seed(seed)

    records, corpus_dir = load_corpus_catalog(catalog_path)
    split_hygiene = _verify_leak_free_splits(records)

    usable = [
        r for r in records
        if (not exclude_unclassified)
        or (str(r.get("motif_class", "")) not in ("unclassified_motif", "multi_motif_panel"))
    ]
    if len(usable) < 20:
        usable = list(records)

    assets: List[MotifAsset] = []
    for r in usable:
        try:
            assets.append(load_motif_asset(r, corpus_dir))
        except FileNotFoundError:
            continue

    if len(assets) < 6:
        raise RuntimeError(f"Insufficient assets loaded ({len(assets)}) to train recognizer")

    families = sorted({a.family for a in assets})
    classes = sorted({a.motif_class for a in assets})
    fam_to_id = {f: i for i, f in enumerate(families)}
    cls_to_id = {c: i for i, c in enumerate(classes)}

    splits: Dict[str, List[MotifAsset]] = {"train": [], "val": [], "test": []}
    for a in assets:
        sp = a.split if a.split in splits else "train"
        splits[sp].append(a)

    if len(splits["train"]) < 2 or not splits["val"]:
        raise ValueError("Training requires two training assets and a separate validation split")

    # Precompute clean normalized masks + inputs for val/test, plus deterministic puzzle-deformed val/test
    def prep(alist: List[MotifAsset], deform_seed: int):
        raw_masks = [normalize_mask_frame(a.mask, size=img_size, pad_frac=0.10) for a in alist]
        xs = [mask_to_model_input(a.mask, a.sdf, size=img_size) for a in alist]
        drng = np.random.default_rng(deform_seed)
        def_masks = [augment_silhouette_mask(m, drng, size=img_size, puzzle_deform=True) for m in raw_masks]
        xs_def = [fast_mask_to_model_input(dm, size=img_size) for dm in def_masks]
        fs = [fam_to_id[a.family] for a in alist]
        cs = [cls_to_id[a.motif_class] for a in alist]
        return raw_masks, xs, def_masks, xs_def, fs, cs

    m_tr, x_tr, _, _, f_tr, c_tr = prep(splits["train"], seed + 101)
    m_va, x_va, dm_va, xd_va, f_va, c_va = prep(splits["val"], seed + 202)
    m_te, x_te, dm_te, xd_te, f_te, c_te = prep(splits["test"], seed + 303)

    # 1. Deterministic shape-descriptor retrieval baseline (zero training)
    desc_base = ShapeDescriptorBaseline().fit(
        masks=[a.mask for a in splits["train"]],
        labels=[a.motif_class for a in splits["train"]],
        families=[a.family for a in splits["train"]],
        asset_ids=[a.asset_id for a in splits["train"]],
    )

    def eval_desc(eval_assets: List[MotifAsset], override_masks: Optional[List[np.ndarray]] = None) -> Dict[str, Any]:
        y_f, p_f, y_c, p_c = [], [], [], []
        top5_hits = 0
        for idx, a in enumerate(eval_assets):
            qm = override_masks[idx] if override_masks is not None else a.mask
            res = desc_base.query(qm, top_k=5)
            if not res:
                continue
            y_f.append(fam_to_id[a.family])
            p_f.append(fam_to_id[str(res[0]["family"])])
            y_c.append(cls_to_id[a.motif_class])
            p_c.append(cls_to_id[str(res[0]["motif_class"])])
            if any(str(r["motif_class"]) == a.motif_class for r in res):
                top5_hits += 1
        return {
            "family": _classification_metrics(y_f, p_f, families),
            "motif_class": _classification_metrics(y_c, p_c, classes),
            "class_top5_retrieval": round(top5_hits / float(max(1, len(y_c))), 4),
        }

    baseline_val = eval_desc(splits["val"])
    baseline_test = eval_desc(splits["test"])
    baseline_test_distorted = eval_desc(splits["test"], override_masks=dm_te)

    # 2. Train Model A v2 CNN
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = HellerSilhouetteEncoder(
        num_families=len(families),
        num_classes=len(classes),
        in_channels=2,
        embed_dim=128,
        dropout=0.25,
        arch_version=2,
    ).to(device)

    # NO DOUBLE WEIGHTING:
    # - sqrt-balanced sampling already rebalances batches (capped at 8x oversampling so tiny classes don't overfit),
    #   so CE loss stays unweighted!
    # - shuffle sampling uses gently damped inverse-frequency weights clamped to [0.5, 3.5].
    sampler = None
    if sampling == "sqrt-balanced":
        counts = Counter(c_tr)
        max_cnt = max(counts.values())
        raw_w = [min(8.0, math.sqrt(max_cnt / float(counts[c]))) for c in c_tr]
        sampler = WeightedRandomSampler(raw_w, num_samples=len(raw_w), replacement=True)
        fam_w = None
        cls_w = None
    else:
        fam_w = torch.from_numpy(_damped_class_weights(f_tr, len(families))).to(device) if len(families) > 1 else None
        cls_w = torch.from_numpy(_damped_class_weights(c_tr, len(classes))).to(device)

    tr_ds = _TwoViewSilhouetteDataset(m_tr, f_tr, c_tr, img_size=img_size, puzzle_aug=puzzle_aug, seed=seed)
    eff_bs = min(batch_size, max(2, len(tr_ds)))
    tr_loader = DataLoader(
        tr_ds,
        batch_size=eff_bs,
        shuffle=sampler is None,
        sampler=sampler,
        drop_last=(len(tr_ds) > eff_bs),
    )

    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=2e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, epochs))

    config = dict(
        trainer="recognizer_v2",
        arch_version=2,
        catalog_digest=catalog_digest(catalog_path),
        families=families,
        classes=classes,
        seed=seed,
        img_size=img_size,
        batch_size=batch_size,
        lr=lr,
        sampling=sampling,
        puzzle_aug=puzzle_aug,
        exclude_unclassified=exclude_unclassified,
        device=device.type,
        active_asset_ids=[a.asset_id for a in assets],
    )
    run = EpochCheckpoint(out_dir, model, opt, sched, config, epochs, tr_ds.rng, resume=resume)

    @torch.inference_mode()
    def infer_single_view(xs_list: List[np.ndarray]) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        model.eval()
        if not xs_list:
            return np.zeros((0, 128), dtype=np.float32), np.zeros((0, len(families)), dtype=np.float32), np.zeros((0, len(classes)), dtype=np.float32)
        embs, flogs, clogs = [], [], []
        for start in range(0, len(xs_list), 64):
            bx = torch.from_numpy(np.stack(xs_list[start : start + 64], axis=0)).to(device)
            out = model(bx)
            embs.append(out["embedding"].cpu().numpy())
            flogs.append(out["family_logits"].cpu().numpy())
            clogs.append(out["class_logits"].cpu().numpy())
        return np.vstack(embs), np.vstack(flogs), np.vstack(clogs)

    @torch.inference_mode()
    def infer_tta(
        masks_list: List[np.ndarray],
        n_rot: int = 8,
        flips: bool = True,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        model.eval()
        if not masks_list:
            return np.zeros((0, 128), dtype=np.float32), np.zeros((0, len(families)), dtype=np.float32), np.zeros((0, len(classes)), dtype=np.float32)
        embs, flogs, clogs = [], [], []
        for m in masks_list:
            views = make_tta_views(m, size=img_size, n_rotations=n_rot, include_flips=flips)
            bx = torch.from_numpy(views).to(device)
            out = model(bx)
            e = F.normalize(out["embedding"].mean(dim=0, keepdim=True), p=2, dim=1)[0].cpu().numpy()
            fl = out["family_logits"].mean(dim=0).cpu().numpy()
            cl = out["class_logits"].mean(dim=0).cpu().numpy()
            embs.append(e)
            flogs.append(fl)
            clogs.append(cl)
        return np.vstack(embs), np.vstack(flogs), np.vstack(clogs)

    def validate_epoch():
        _, fl_c, cl_c = infer_single_view(x_va)
        _, fl_d, cl_d = infer_single_view(xd_va)
        cls_c = _classification_metrics(c_va, cl_c.argmax(axis=1), classes)
        cls_d = _classification_metrics(c_va, cl_d.argmax(axis=1), classes)
        fam_c = _classification_metrics(f_va, fl_c.argmax(axis=1), families)
        fam_d = _classification_metrics(f_va, fl_d.argmax(axis=1), families)
        cls_score = 0.5 * (0.5 * cls_c["balanced_accuracy"] + 0.5 * cls_c["macro_f1"]) + 0.5 * (
            0.5 * cls_d["balanced_accuracy"] + 0.5 * cls_d["macro_f1"]
        )
        if len(families) > 1:
            fam_score = 0.5 * fam_c["balanced_accuracy"] + 0.5 * fam_d["balanced_accuracy"]
            score = 0.65 * cls_score + 0.35 * fam_score
        else:
            score = cls_score
        return score, cls_c, cls_d, fam_c, fam_d

    if init_from:
        parent = torch.load(init_from, map_location="cpu", weights_only=False)
        if (parent["families"], parent["classes"], parent["img_size"]) != (families, classes, img_size):
            raise ValueError("Initialization checkpoint has different labels or input resolution")
        model.load_state_dict(parent["state_dict"])
        run.initial_checkpoint = str(init_from)
        run.consider(validate_epoch()[0], 0)

    # Pre-generate a disjoint pool of synthetic non-figurative hard negatives for Outlier Exposure (OE)
    # (teaches low softmax confidence & low cosine similarity on plain blobs/circles/rectangles).
    oe_rng = np.random.default_rng(seed + 5555)
    oe_pool_np = np.stack(
        [fast_mask_to_model_input(_make_single_hard_negative(oe_rng, size=img_size), size=img_size) for _ in range(192)],
        axis=0,
    )
    oe_pool = torch.from_numpy(oe_pool_np).to(device)
    n_oe = max(4, eff_bs // 4)
    log_c = math.log(max(2, len(classes)))
    log_f = math.log(max(2, len(families)))

    for epoch in range(len(run.history), epochs):
        model.train()
        ep_loss = 0.0
        n_batches = 0
        for bx1, bx2, bf, bc in tr_loader:
            b = bx1.shape[0]
            neg_idx = torch.randint(0, len(oe_pool), (n_oe,), device=device)
            bx_neg = oe_pool[neg_idx]
            if torch.rand(1).item() < 0.5:
                bx_neg = bx_neg.flip(-1)
            k_rot = int(torch.randint(0, 4, (1,)).item())
            if k_rot:
                bx_neg = torch.rot90(bx_neg, k=k_rot, dims=(-2, -1))

            bx = torch.cat([bx1.to(device), bx2.to(device), bx_neg], dim=0)
            bf2 = torch.cat([bf, bf], dim=0).to(device)
            bc2 = torch.cat([bc, bc], dim=0).to(device)

            opt.zero_grad(set_to_none=True)
            out = model(bx)
            pos_c_logits = out["class_logits"][: 2 * b]
            pos_f_logits = out["family_logits"][: 2 * b]
            pos_emb = out["embedding"][: 2 * b]
            neg_c_logits = out["class_logits"][2 * b :]
            neg_f_logits = out["family_logits"][2 * b :]
            neg_emb = out["embedding"][2 * b :]

            l_cls = F.cross_entropy(pos_c_logits, bc2, weight=cls_w, label_smoothing=0.05)
            l_fam = (
                F.cross_entropy(pos_f_logits, bf2, weight=fam_w, label_smoothing=0.03)
                if len(families) > 1
                else l_cls.new_tensor(0.0)
            )
            l_con = _supcon_loss(pos_emb, bc2, temp=0.14)
            l_fcon = _supcon_loss(pos_emb, bf2, temp=0.14) if len(families) > 1 else l_cls.new_tensor(0.0)
            # Explicit invariance pull between clean view and puzzle-deformed view of the same shape
            l_pair = (1.0 - (pos_emb[:b] * pos_emb[b:]).sum(dim=1)).mean()

            # Outlier Exposure: push non-figurative hard negatives toward uniform entropy & away from motif embeddings
            l_oe_cls = (-F.log_softmax(neg_c_logits, dim=1).mean(dim=1).mean() - log_c).clamp_min(0.0)
            l_oe_fam = (
                (-F.log_softmax(neg_f_logits, dim=1).mean(dim=1).mean() - log_f).clamp_min(0.0)
                if len(families) > 1
                else l_cls.new_tensor(0.0)
            )
            l_oe_emb = F.relu((neg_emb @ pos_emb.detach().T) - 0.15).square().mean()

            loss = (
                l_cls
                + (0.85 * l_fam if len(families) > 1 else 0.0)
                + 0.35 * l_con
                + (0.20 * l_fcon if len(families) > 1 else 0.0)
                + 0.20 * l_pair
                + 0.18 * l_oe_cls
                + (0.12 * l_oe_fam if len(families) > 1 else 0.0)
                + 0.22 * l_oe_emb
            )
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 3.0)
            opt.step()
            ep_loss += float(loss.item())
            n_batches += 1

        score, cls_c, cls_d, fam_c, fam_d = validate_epoch()
        run.finish_epoch(
            dict(
                loss=round(ep_loss / max(1, n_batches), 5),
                val_score=round(score, 4),
                class_macro_f1=cls_c["macro_f1"],
                class_balanced_accuracy=cls_c["balanced_accuracy"],
                distorted_class_macro_f1=cls_d["macro_f1"],
                distorted_class_balanced_accuracy=cls_d["balanced_accuracy"],
                family_balanced_accuracy=fam_c["balanced_accuracy"],
                distorted_family_balanced_accuracy=fam_d["balanced_accuracy"],
            ),
            score,
        )
        if (epoch + 1) % 10 == 0 or epoch + 1 == epochs:
            print(
                f"epoch {epoch + 1}/{epochs}  loss={ep_loss / max(1, n_batches):.3f}  "
                f"val_cls_f1={cls_c['macro_f1']:.3f}  def_cls_f1={cls_d['macro_f1']:.3f}  "
                f"val_fam_ba={fam_c['balanced_accuracy']:.3f}",
                file=sys.stderr,
                flush=True,
            )

    model.load_state_dict(run.best_weights)
    model.eval()

    # Compute TTA train embedding bank & class/family prototypes
    tr_emb_single, _, _ = infer_single_view(x_tr)
    tr_emb_tta, _, _ = infer_tta(m_tr, n_rot=tta_rotations, flips=True)

    def build_prototypes(embs: np.ndarray, ids: List[int], k: int) -> np.ndarray:
        protos = np.zeros((k, embs.shape[1]), dtype=np.float32)
        for c in range(k):
            mask_c = np.asarray(ids) == c
            if np.any(mask_c):
                v = embs[mask_c].mean(axis=0)
                nrm = float(np.linalg.norm(v))
                protos[c] = (v / nrm) if nrm > 1e-8 else v
        return protos

    class_prototypes = build_prototypes(tr_emb_tta, c_tr, len(classes))
    family_prototypes = build_prototypes(tr_emb_tta, f_tr, len(families))

    # Calibrate softmax temperatures on clean + deformed validation logits (TTA)
    _, va_fl_c, va_cl_c = infer_tta(m_va, n_rot=tta_rotations, flips=True)
    _, va_fl_d, va_cl_d = infer_tta(dm_va, n_rot=tta_rotations, flips=True)
    cal_fl = np.vstack([va_fl_c, va_fl_d])
    cal_cl = np.vstack([va_cl_c, va_cl_d])
    temp_fam = _fit_temperature(cal_fl, f_va + f_va)
    temp_cls = _fit_temperature(cal_cl, c_va + c_va)

    def eval_split_detailed(
        xs_list: List[np.ndarray],
        masks_list: List[np.ndarray],
        f_true: List[int],
        c_true: List[int],
        use_tta: bool = True,
        include_confusion: bool = True,
    ) -> Dict[str, Any]:
        if not xs_list:
            return {}
        if use_tta:
            emb, flog, clog = infer_tta(masks_list, n_rot=tta_rotations, flips=True)
            bank = tr_emb_tta
        else:
            emb, flog, clog = infer_single_view(xs_list)
            bank = tr_emb_single

        f_pred = np.argmax(flog, axis=1)
        c_pred = np.argmax(clog, axis=1)
        top3 = np.argsort(clog, axis=1)[:, -min(3, len(classes)) :]
        top3_acc = float(np.mean([c_true[i] in top3[i] for i in range(len(c_true))]))

        # Calibrated vs uncalibrated ECE
        raw_c_probs = F.softmax(torch.from_numpy(clog), dim=1).numpy()
        cal_c_probs = F.softmax(torch.from_numpy(clog / temp_cls), dim=1).numpy()

        # Cosine kNN retrieval against train split (1-NN & 5-NN weighted vote)
        sims = emb @ bank.T
        nn1_idx = np.argmax(sims, axis=1)
        knn1_cls_pred = [c_tr[int(j)] for j in nn1_idx]
        knn1_fam_pred = [f_tr[int(j)] for j in nn1_idx]
        ret_top1 = float(np.mean([knn1_cls_pred[i] == c_true[i] for i in range(len(c_true))]))
        k5 = min(5, len(c_tr))
        nn5_idx = np.argsort(sims, axis=1)[:, -k5:]
        ret_top5 = float(
            np.mean([any(c_tr[int(j)] == c_true[i] for j in nn5_idx[i]) for i in range(len(c_true))])
        )

        # Prototype nearest-centroid classification
        proto_sims = emb @ class_prototypes.T
        proto_pred = np.argmax(proto_sims, axis=1)

        return {
            "family": _classification_metrics(f_true, f_pred, families, include_confusion=include_confusion),
            "motif_class": _classification_metrics(c_true, c_pred, classes, include_confusion=include_confusion),
            "class_top3_accuracy": round(top3_acc, 4),
            "ece_uncalibrated": _expected_calibration_error(raw_c_probs, c_true),
            "ece_calibrated": _expected_calibration_error(cal_c_probs, c_true),
            "embedding_retrieval_top1": round(ret_top1, 4),
            "embedding_retrieval_top5": round(ret_top5, 4),
            "embedding_knn1_class": _classification_metrics(c_true, knn1_cls_pred, classes, include_confusion=False),
            "embedding_knn1_family": _classification_metrics(f_true, knn1_fam_pred, families, include_confusion=False),
            "prototype_class": _classification_metrics(c_true, proto_pred, classes, include_confusion=False),
        }

    cnn_val_single = eval_split_detailed(x_va, m_va, f_va, c_va, use_tta=False, include_confusion=False)
    cnn_val_tta = eval_split_detailed(x_va, m_va, f_va, c_va, use_tta=True, include_confusion=True)
    cnn_test_single = eval_split_detailed(x_te, m_te, f_te, c_te, use_tta=False, include_confusion=False)
    cnn_test_tta = eval_split_detailed(x_te, m_te, f_te, c_te, use_tta=True, include_confusion=True)
    cnn_test_distorted_tta = eval_split_detailed(xd_te, dm_te, f_te, c_te, use_tta=True, include_confusion=True)

    # Hard-negative / blob rejection check on held-out test vs synthetic non-motifs
    neg_masks = _generate_hard_negative_masks(n=64, size=img_size, seed=seed + 777)
    te_emb, _, te_clog = infer_tta(m_te if m_te else m_va, n_rot=tta_rotations, flips=True)
    neg_emb, _, neg_clog = infer_tta(neg_masks, n_rot=tta_rotations, flips=True)

    te_conf = F.softmax(torch.from_numpy(te_clog / temp_cls), dim=1).numpy().max(axis=1)
    neg_conf = F.softmax(torch.from_numpy(neg_clog / temp_cls), dim=1).numpy().max(axis=1)
    te_psim = (te_emb @ class_prototypes.T).max(axis=1)
    neg_psim = (neg_emb @ class_prototypes.T).max(axis=1)
    hard_negative_report = {
        "num_negatives": len(neg_masks),
        "num_positives_test": len(te_emb),
        "positive_mean_calibrated_conf": round(float(te_conf.mean()), 4),
        "negative_mean_calibrated_conf": round(float(neg_conf.mean()), 4),
        "conf_auroc": _binary_auroc(te_conf.tolist(), neg_conf.tolist()),
        "positive_mean_max_proto_sim": round(float(te_psim.mean()), 4),
        "negative_mean_max_proto_sim": round(float(neg_psim.mean()), 4),
        "proto_sim_auroc": _binary_auroc(te_psim.tolist(), neg_psim.tolist()),
    }

    # Save checkpoint with prototypes, train embedding bank, and calibrated temperatures
    ckpt_path = out_dir / "model_a_recognizer.pt"
    save_atomic(
        {
            "arch_version": 2,
            "state_dict": model.state_dict(),
            "families": families,
            "classes": classes,
            "img_size": img_size,
            "embed_dim": 128,
            "seed": seed,
            "temperature_family": temp_fam,
            "temperature_class": temp_cls,
            "class_prototypes": class_prototypes,
            "family_prototypes": family_prototypes,
            "train_embeddings": tr_emb_tta,
            "train_classes": c_tr,
            "train_families": f_tr,
        },
        ckpt_path,
    )

    puzzle_evals: Dict[str, Any] = {}
    for pdir in eval_puzzles:
        pp = Path(pdir)
        if (pp / "partition.json").exists():
            puzzle_evals[str(pdir)] = evaluate_encoder_on_puzzle(pp, ckpt_path, tta_rotations=tta_rotations)

    elapsed = time.perf_counter() - t0
    metrics = {
        "model": "HellerSilhouetteEncoder v2 (Model A)",
        "arch_version": 2,
        "device": device.type,
        "elapsed_sec": round(elapsed, 2),
        "epochs": epochs,
        "best_epoch": run.best_epoch,
        "best_val_score": round(float(run.best_score), 4),
        "initial_checkpoint": run.initial_checkpoint,
        "sampling": sampling,
        "puzzle_aug": puzzle_aug,
        "tta_rotations": tta_rotations,
        "temperature_family": temp_fam,
        "temperature_class": temp_cls,
        "history": run.history,
        "img_size": img_size,
        "num_assets_used": len(assets),
        "num_families": len(families),
        "num_classes": len(classes),
        "families": families,
        "classes": classes,
        "class_counts_by_split": {
            sp: dict(sorted(Counter(a.motif_class for a in alist).items()))
            for sp, alist in splits.items()
        },
        "split_hygiene": split_hygiene,
        "active_split_counts": {k: len(v) for k, v in splits.items()},
        "train_loss_first": run.history[0]["loss"],
        "train_loss_last": run.history[-1]["loss"],
        "shape_descriptor_baseline": {
            "val": baseline_val,
            "test": baseline_test,
            "test_distorted": baseline_test_distorted,
        },
        "cnn_model_a": {
            "val": cnn_val_tta,
            "test": cnn_test_tta,
            "val_single_view": cnn_val_single,
            "test_single_view": cnn_test_single,
            "test_distorted": cnn_test_distorted_tta,
        },
        "hard_negative_rejection": hard_negative_report,
        "puzzle_evaluations": puzzle_evals,
        "checkpoint_path": str(ckpt_path),
    }

    with open(out_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)
    return metrics


@torch.inference_mode()
def evaluate_encoder_on_puzzle(
    run_dir: str | Path,
    checkpoint_path: str | Path,
    tta_rotations: int = 8,
    include_flips: bool = True,
) -> Dict[str, Any]:
    """Independently score isolated piece PNGs of a generated puzzle using trained Model A (v1 or v2)."""
    run_dir = Path(run_dir)
    model, meta = load_recognizer_checkpoint(checkpoint_path, device="cpu")
    families: List[str] = meta["families"]
    classes: List[str] = meta["classes"]
    img_size = int(meta["img_size"])
    temp_f = float(meta["temperature_family"])
    temp_c = float(meta["temperature_class"])
    class_protos = meta.get("class_prototypes")
    tr_emb = meta.get("train_embeddings")
    tr_cls = meta.get("train_classes")

    with open(run_dir / "partition.json", "r", encoding="utf-8") as f:
        part = json.load(f)

    piece_results: List[Dict[str, Any]] = []
    fam_hits = 0
    cls_top1_hits = 0
    cls_top3_hits = 0
    proto_top3_hits = 0
    evaluated_fam = 0
    evaluated_cls = 0

    for idx, face in enumerate(part["faces"], start=1):
        png_path = run_dir / "pieces" / f"{idx:04d}.png"
        if not png_path.exists():
            continue
        mask = (np.asarray(Image.open(png_path).convert("L"), dtype=np.float32) >= 127.5).astype(np.float32)
        pred = predict_silhouette(
            model,
            mask,
            families=families,
            classes=classes,
            img_size=img_size,
            tta_rotations=tta_rotations,
            include_flips=include_flips,
            temperature_family=temp_f,
            temperature_class=temp_c,
            class_prototypes=class_protos,
            device="cpu",
        )

        face_meta = face.get("metadata", {})
        target_fam = str(face_meta.get("family", ""))
        target_cls = str(face_meta.get("motif_class", ""))
        if target_fam in families:
            evaluated_fam += 1
            if pred["pred_family"] == target_fam:
                fam_hits += 1
        if target_cls in classes:
            evaluated_cls += 1
            if pred["pred_class_top1"] == target_cls:
                cls_top1_hits += 1
            if target_cls in pred["pred_class_top3"]:
                cls_top3_hits += 1
            if "proto_class_top3" in pred and target_cls in pred["proto_class_top3"]:
                proto_top3_hits += 1

        entry: Dict[str, Any] = {
            "piece_num": idx,
            "target_family": target_fam,
            "target_class": target_cls,
            "pred_family": pred["pred_family"],
            "pred_family_conf": round(pred["pred_family_conf"], 4),
            "pred_class_top1": pred["pred_class_top1"],
            "pred_class_conf": round(pred["pred_class_conf"], 4),
            "pred_class_top3": pred["pred_class_top3"],
        }
        if "proto_class_top1" in pred:
            entry["proto_class_top1"] = pred["proto_class_top1"]
            entry["proto_class_top3"] = pred["proto_class_top3"]
            entry["max_prototype_sim"] = round(pred["max_prototype_sim"], 4)
        if tr_emb is not None and tr_cls is not None and len(tr_emb) > 0:
            sims = np.asarray(tr_emb, dtype=np.float32) @ pred["embedding"]
            top5 = np.argsort(sims)[::-1][:5]
            entry["knn_top5_classes"] = [classes[int(tr_cls[int(j)])] for j in top5]
            entry["knn_top1_sim"] = round(float(sims[int(top5[0])]), 4)
        piece_results.append(entry)

    return {
        "checkpoint": str(checkpoint_path),
        "arch_version": meta["arch_version"],
        "tta_rotations": tta_rotations,
        "num_pieces": len(piece_results),
        "evaluated_family_count": evaluated_fam,
        "evaluated_class_count": evaluated_cls,
        "family_top1_match_rate": round(fam_hits / float(max(1, evaluated_fam)), 4),
        "class_top1_match_rate": round(cls_top1_hits / float(max(1, evaluated_cls)), 4),
        "class_top3_match_rate": round(cls_top3_hits / float(max(1, evaluated_cls)), 4),
        "prototype_top3_match_rate": round(proto_top3_hits / float(max(1, evaluated_cls)), 4) if class_protos is not None else None,
        "pieces": piece_results,
    }
