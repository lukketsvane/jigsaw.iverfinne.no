"""Compare trained shape VAEs on held-out silhouettes at one common raster resolution.

Each model encodes the source signed-distance field at its own training size; the
decoded field is resized to 256 px and compared with the original 256 px mask, so
models trained at 96, 128, 192 or 256 px are judged on identical pixels. The
selection metric is validation macro IoU; test scores are reported but never used
to choose.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import torch
from scipy import ndimage as ndi

from ..data.corpus import load_corpus_catalog, load_motif_asset
from .recognizer import mask_to_model_input
from .shape_prior import build_from_checkpoint, usable_mask
from .training_state import catalog_digest

COMMON = 256


def _iou(a, b):
    return float(np.logical_and(a, b).sum() / max(1, np.logical_or(a, b).sum()))


def _boundary_error(a, b):
    a = a ^ ndi.binary_erosion(a)
    b = b ^ ndi.binary_erosion(b)
    if not a.any() or not b.any():
        return float(COMMON)
    return float((ndi.distance_transform_edt(~a)[b].mean() + ndi.distance_transform_edt(~b)[a].mean()) / 2)


def _thin_recall(pred, actual, radius=4):
    """Recall of the mask's thin parts (removed by an opening of `radius` px at 256 px)."""
    disk = np.hypot(*np.ogrid[-radius:radius + 1, -radius:radius + 1]) <= radius
    thin = actual & ~ndi.binary_opening(actual, structure=disk)
    if thin.sum() < 20:
        return None
    return float((pred & thin).sum() / thin.sum())


def load_model(path, catalog, device):
    path = Path(path)
    ckpt = torch.load(path / "animal-shape-vae.pt", map_location="cpu", weights_only=True)
    if ckpt["catalog_digest"] != catalog_digest(catalog):
        raise ValueError(f"{path} was trained on a different catalog")
    model = build_from_checkpoint(ckpt)
    model.load_state_dict(ckpt["state_dict"])
    return ckpt, model.eval().to(device)


@torch.inference_mode()
def evaluate_model(ckpt, model, assets, device, batch=16):
    rows = defaultdict(list)
    started = time.perf_counter()
    for start in range(0, len(assets), batch):
        group = assets[start:start + batch]
        x = torch.from_numpy(np.stack([mask_to_model_input(a.mask, a.sdf, size=ckpt["size"])[1:2] for _, a in group])).to(device)
        y = torch.tensor([ckpt["classes"].index(a.motif_class) for _, a in group], device=device)
        mu, _ = model.encode(x, y)
        pred = model.decode(mu, y)[:, 0].float().cpu().numpy()
        for (rec, a), field in zip(group, pred):
            m = cv2.resize(field, (COMMON, COMMON), interpolation=cv2.INTER_CUBIC) < 0
            actual = a.mask > 0.5
            rows[(rec["split"], a.motif_class)].append(dict(
                iou=_iou(m, actual), boundary_error_px=_boundary_error(m, actual),
                thin_recall=_thin_recall(m, actual), connected_hole_free=usable_mask(field) is not None))
    report = {}
    for split in ("val", "test"):
        per_class = {}
        for name in ckpt["classes"]:
            vals = rows[(split, name)]
            if not vals:
                continue
            thin = [r["thin_recall"] for r in vals if r["thin_recall"] is not None]
            per_class[name] = dict(
                count=len(vals), iou=float(np.mean([r["iou"] for r in vals])),
                boundary_error_px=float(np.mean([r["boundary_error_px"] for r in vals])),
                thin_recall=float(np.mean(thin)) if thin else None,
                connected_hole_free_rate=float(np.mean([r["connected_hole_free"] for r in vals])))
        flat = [r for (s, _), rs in rows.items() if s == split for r in rs]
        thin = [r["thin_recall"] for r in flat if r["thin_recall"] is not None]
        report[split] = dict(
            count=len(flat), mean_iou=float(np.mean([r["iou"] for r in flat])),
            macro_iou=float(np.mean([v["iou"] for v in per_class.values()])),
            mean_boundary_error_px=float(np.mean([r["boundary_error_px"] for r in flat])),
            thin_recall=float(np.mean(thin)) if thin else None,
            valid_rate=float(np.mean([r["connected_hole_free"] for r in flat])), per_class=per_class)
    report["inference_seconds"] = round(time.perf_counter() - started, 2)
    return report


def benchmark(catalog, model_dirs, out=None, device=None):
    device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
    rows, base = load_corpus_catalog(catalog)
    assets = [(r, load_motif_asset(r, base)) for r in rows if r.get("split") in ("val", "test")]
    models = {}
    for path in model_dirs:
        ckpt, model = load_model(path, catalog, device)
        metrics_path = Path(path) / "metrics.json"
        train = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
        result = evaluate_model(ckpt, model, assets, device)
        result["training"] = dict(
            size=ckpt["size"], latent_dim=ckpt["latent_dim"], width=int(ckpt.get("width", 1)),
            stages=int(ckpt.get("stages", 3)), best_epoch=ckpt.get("best_epoch"),
            epochs=len(train.get("history", [])) or None,
            parameters=int(sum(p.numel() for p in model.parameters())),
            session=train.get("session"),
            raw_valid_shapes_per_class=train.get("valid_raw_shapes_per_class"))
        models[str(path)] = result
        print(Path(path).name, "val macro IoU", round(result["val"]["macro_iou"], 4),
              "valid", round(result["val"]["valid_rate"], 3), "thin", result["val"]["thin_recall"], flush=True)
    # Validity matters for manufacturing: a decoded field that is not one hole-free region is unusable.
    ranked = sorted(models, key=lambda k: -(models[k]["val"]["macro_iou"] + 0.05 * models[k]["val"]["valid_rate"]))
    report = dict(
        common_resolution_px=COMMON, catalog=str(catalog),
        selection_metric="validation macro IoU + 0.05 x validation connected-hole-free rate (test never used to choose)",
        ranking=ranked, selected_model=ranked[0], models=models)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
