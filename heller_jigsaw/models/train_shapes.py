"""Fit and evaluate conditional SDF VAE shape priors on source-grouped catalog splits."""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import torch
import torch.nn.functional as F

from ..data.corpus import load_corpus_catalog, load_motif_asset
from .recognizer import mask_to_model_input
from .shape_prior import AnimalShapeVAE, usable_mask
from .training_state import EpochCheckpoint, catalog_digest, save_atomic
from .train_recognizer import _verify_leak_free_splits


def _eikonal_loss(pred_sdf: torch.Tensor, size: int) -> torch.Tensor:
    """Enforce |grad(sdf)| ≈ 1 in pixel units (scaled by mask_to_model_input's 0.25*size divisor)."""
    # In normalized coordinates, 1 pixel step changes normalized SDF by 1 / (0.25 * size) = 4 / size
    dx = (pred_sdf[:, :, :, 1:] - pred_sdf[:, :, :, :-1]) * (size * 0.25)
    dy = (pred_sdf[:, :, 1:, :] - pred_sdf[:, :, :-1, :]) * (size * 0.25)
    dx = dx[:, :, :-1, :]
    dy = dy[:, :, :, :-1]
    grad_norm = torch.sqrt(dx.square() + dy.square() + 1e-6)
    # Focus Eikonal regularization near the silhouette boundary (where SDF is not clamped to ±1)
    active = (pred_sdf[:, :, :-1, :-1].abs() < 0.85).float()
    return (((grad_norm - 1.0).square()) * active).sum() / active.sum().clamp_min(1.0)


def train_shapes(
    catalog,
    out_dir,
    epochs: int = 120,
    batch_size: int = 32,
    lr: float = 0.001,
    size: int = 64,
    latent_dim: int = 32,
    seed: int = 42,
    resume: bool = False,
    balanced: bool = False,
    families: Optional[Sequence[str]] = ("animal",),
    min_train_count: int = 1,
    eikonal_weight: float = 0.0,
    exclude_classes: Sequence[str] = ("unclassified_motif", "multi_motif_panel"),
):
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    records, root = load_corpus_catalog(catalog)
    excl = set(exclude_classes or ())
    if families is not None:
        fam_set = set(families)
        selected = [
            r for r in records
            if r.get("family") in fam_set and r.get("motif_class") not in excl
        ]
    else:
        selected = [r for r in records if r.get("motif_class") not in excl]

    hygiene = _verify_leak_free_splits(selected)
    train_counts = Counter(r["motif_class"] for r in selected if r.get("split") == "train")
    if min_train_count > 1:
        allowed_classes = {cls for cls, cnt in train_counts.items() if cnt >= min_train_count}
        selected = [r for r in selected if r["motif_class"] in allowed_classes]
    else:
        allowed_classes = set(train_counts.keys())

    groups = {sp: [r for r in selected if r.get("split") == sp] for sp in ("train", "val", "test")}
    if not all(groups.values()):
        raise ValueError("The shape catalog needs distinct train, val and test groups")
    names = sorted({r["motif_class"] for r in groups["train"]})
    unknown = {r["motif_class"] for r in selected} - set(names)
    if unknown:
        raise ValueError(f"Missing training examples for {sorted(unknown)}")

    # Majority family per motif class (so multi-motif Nordic checkpoints preserve true family metadata)
    fam_votes: dict[str, Counter] = {n: Counter() for n in names}
    for r in selected:
        fam_votes[r["motif_class"]][str(r.get("family", "animal"))] += 1
    class_to_family = {n: fam_votes[n].most_common(1)[0][0] for n in names}

    tensors = {}
    for sp, rows in groups.items():
        inputs, ids = [], []
        for row in rows:
            asset = load_motif_asset(row, root)
            sdf = mask_to_model_input(asset.mask, asset.sdf, size=size)[1:2]
            inputs.append(sdf)
            ids.append(names.index(asset.motif_class))
        tensors[sp] = (torch.from_numpy(np.stack(inputs)).to(device), torch.tensor(ids, device=device))

    fields = (
        "asset_id", "family", "motif_class", "split", "source_group", "sha256",
        "source_url", "creator", "license", "license_url", "commercial_ok",
    )
    source_list = [{k: r.get(k) for k in fields} for r in selected]
    model = AnimalShapeVAE(len(names), size, latent_dim).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=epochs)
    config = dict(
        trainer="animal_sdf_vae",
        classes=names,
        catalog_digest=catalog_digest(catalog),
        size=size,
        latent_dim=latent_dim,
        lr=lr,
        batch_size=batch_size,
        seed=seed,
        device=device.type,
    )
    if balanced:
        config["balanced"] = True
    if families != ("animal",):
        config["families"] = list(families) if families is not None else None
    if min_train_count > 1:
        config["min_train_count"] = int(min_train_count)
    if eikonal_weight > 0:
        config["eikonal_weight"] = float(eikonal_weight)

    run = EpochCheckpoint(folder, model, opt, sched, config, epochs, rng, resume=resume)
    (folder / "training-sources.json").write_text(json.dumps(source_list, indent=2), encoding="utf-8")

    @torch.inference_mode()
    def evaluate(split):
        model.eval()
        inputs, labels = tensors[split]
        scores = []
        for x, y in zip(inputs.split(batch_size), labels.split(batch_size)):
            mu, _ = model.encode(x, y)
            pred = model.decode(mu, y) < 0
            actual = x < 0
            inter = (pred & actual).flatten(1).sum(1)
            union = (pred | actual).flatten(1).sum(1)
            scores.extend((inter / union.clamp_min(1)).cpu().tolist())
        return np.asarray(scores)

    train_x, train_y = tensors["train"]
    freq = np.bincount(train_y.cpu().numpy(), minlength=len(names))
    weights = np.power(freq[train_y.cpu().numpy()].astype(float), -1.0 if balanced else -0.5)
    weights /= weights.sum()
    for epoch in range(len(run.history), epochs):
        model.train()
        indices = np.concatenate([np.arange(len(train_x)), rng.choice(len(train_x), len(train_x), p=weights)])
        if balanced:
            indices = rng.choice(len(train_x), 2 * len(train_x), p=weights)
        rng.shuffle(indices)
        loss_sum = 0.0
        for start in range(0, len(indices), batch_size):
            sel = torch.as_tensor(indices[start : start + batch_size], device=device)
            x, y = train_x[sel], train_y[sel]
            flip = torch.rand((len(x), 1, 1, 1), device=device) < 0.5
            x = torch.where(flip, x.flip(-1), x)
            if balanced:
                a = (torch.rand(len(x), device=device) - 0.5) * 0.35
                scale = 0.95 + torch.rand(len(x), device=device) * 0.15
                t = torch.zeros((len(x), 2, 3), device=device)
                t[:, 0, 0] = t[:, 1, 1] = a.cos() * scale
                t[:, 0, 1] = -a.sin() * scale
                t[:, 1, 0] = a.sin() * scale
                grid = F.affine_grid(t, x.shape, align_corners=False)
                x = F.grid_sample(x - 1, grid, align_corners=False) + 1
            mu, log_var = model.encode(x, y)
            z = mu + torch.randn_like(mu) * torch.exp(0.5 * log_var)
            pred = model.decode(z, y)
            mask = (x < 0).float()
            prob = torch.sigmoid(-18 * pred)
            inter = (prob * mask).flatten(1).sum(1)
            area = (prob + mask).flatten(1).sum(1)
            dice = 1 - ((2 * inter + 1) / (area + 1)).mean()
            sdf_error = ((pred - x).abs() * (1 + 2 * torch.exp(-4 * x.abs()))).mean()
            kl = 0.5 * (mu.square() + log_var.exp() - 1 - log_var).mean()
            loss = sdf_error + dice + 0.045 * min(1.0, (epoch + 1) / 15) * kl
            if eikonal_weight > 0.0:
                loss = loss + float(eikonal_weight) * _eikonal_loss(pred, size)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            loss_sum += loss.item() * len(sel)
        val_scores = evaluate("val")
        score = float(val_scores.mean())
        if balanced:
            val_labels = tensors["val"][1].cpu().numpy()
            score = float(np.mean([val_scores[val_labels == c].mean() for c in np.unique(val_labels)]))
        run.finish_epoch(dict(loss=loss_sum / len(indices), val_reconstruction_iou=score), score)
        if (epoch + 1) % 10 == 0 or epoch + 1 == epochs:
            print(
                f"epoch {epoch + 1}/{epochs}  validation reconstruction IoU {score:.3f}",
                file=sys.stderr,
                flush=True,
            )

    model.load_state_dict(run.best_weights)
    model.eval()
    path = folder / "animal-shape-vae.pt"
    ckpt_payload = dict(
        state_dict=model.state_dict(),
        classes=names,
        class_to_family=class_to_family,
        size=size,
        latent_dim=latent_dim,
        seed=seed,
        best_epoch=run.best_epoch,
        catalog_digest=config["catalog_digest"],
    )
    save_atomic(ckpt_payload, path)

    evaluation = {}
    for sp in ("val", "test"):
        ious = evaluate(sp)
        cats = np.array([r["motif_class"] for r in groups[sp]])
        evaluation[sp] = dict(
            mean_iou=float(ious.mean()),
            per_class={
                name: dict(count=int((cats == name).sum()), mean_iou=float(ious[cats == name].mean()))
                for name in sorted(set(cats.tolist()))
            },
        )

    # Evaluate raw valid shape yield, nearest-train IoU (memorization check), and pairwise diversity
    raw_rng = np.random.default_rng(seed + 19)
    valid = {}
    sample_stats = {}
    train_masks_by_cls = {}
    train_x_cpu = (tensors["train"][0][:, 0].cpu().numpy() < 0)
    train_y_cpu = tensors["train"][1].cpu().numpy()
    for idx, name in enumerate(names):
        train_masks_by_cls[name] = train_x_cpu[train_y_cpu == idx]

    with torch.inference_mode():
        for idx, name in enumerate(names):
            z = torch.from_numpy(raw_rng.standard_normal((32, latent_dim)).astype(np.float32)).to(device)
            y = torch.full((len(z),), idx, device=device)
            samples = model.decode(z, y)[:, 0].cpu().numpy()
            valid_lowres = []
            for sample in samples:
                if usable_mask(sample) is not None:
                    valid_lowres.append(sample < 0)
            valid[name] = len(valid_lowres)
            if valid_lowres:
                arr_v = np.stack(valid_lowres, axis=0)
                ref = train_masks_by_cls[name]
                # Max IoU of each generated sample to any training sample of same class
                nn_ious = []
                for sm in arr_v:
                    inter = (ref & sm[None]).sum(axis=(1, 2))
                    union = np.maximum(1, (ref | sm[None]).sum(axis=(1, 2)))
                    nn_ious.append(float((inter / union).max()))
                pair_ious = []
                for a_i in range(len(arr_v)):
                    for b_i in range(a_i + 1, len(arr_v)):
                        inter = float((arr_v[a_i] & arr_v[b_i]).sum())
                        union = max(1.0, float((arr_v[a_i] | arr_v[b_i]).sum()))
                        pair_ious.append(inter / union)
                sample_stats[name] = dict(
                    valid_count=len(valid_lowres),
                    yield_rate=round(len(valid_lowres) / 32.0, 4),
                    mean_nearest_train_iou=round(float(np.mean(nn_ious)), 4),
                    mean_pairwise_iou=round(float(np.mean(pair_ious)), 4) if pair_ious else 0.0,
                )
            else:
                sample_stats[name] = dict(valid_count=0, yield_rate=0.0, mean_nearest_train_iou=0.0, mean_pairwise_iou=0.0)

    report = dict(
        model="conditional SDF VAE" if families != ("animal",) else "conditional animal SDF VAE",
        epochs=epochs,
        best_epoch=run.best_epoch,
        device=str(device),
        config=config,
        class_to_family=class_to_family,
        history=run.history,
        checkpoint=str(path),
        source_groups=hygiene,
        counts={k: dict(Counter(r["motif_class"] for r in v)) for k, v in groups.items()},
        reconstruction=evaluation,
        valid_raw_shapes_per_class=valid,
        sample_diversity_vs_retrieval=sample_stats,
        raw_draws_per_class=32,
    )
    (folder / "metrics.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
