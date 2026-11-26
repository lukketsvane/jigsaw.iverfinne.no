"""Atomic epoch checkpoints shared by the silhouette trainers."""

from __future__ import annotations

import hashlib
import os
import time
import tempfile
from pathlib import Path

import numpy as np
import torch

from ..data.corpus import resolve_recorded_path


def catalog_digest(path: str | Path) -> str:
    path = resolve_recorded_path(path)
    if path.is_dir():
        path = path / "catalog.json"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_atomic(payload, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
        tmp = Path(handle.name)
    try:
        torch.save(payload, tmp)
        # Windows virus scanners and indexers briefly lock freshly written files; retry the swap.
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.25 * 2 ** attempt)
    finally:
        tmp.unlink(missing_ok=True)


class EpochCheckpoint:
    """Track last and best states separately, including the random streams used during fitting."""

    def __init__(self, out_dir, model, optimizer, scheduler, config, epochs, rng, resume=False):
        if epochs < 1:
            raise ValueError("Specify at least one training epoch")
        self.path = Path(out_dir) / "training-last.pt"
        self.model = model
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.config = config
        self.epochs = epochs
        self.rng = rng
        self.history = []
        self.best_score = float("-inf")
        self.best_weights = None
        self.best_epoch = None
        self.initial_checkpoint = None
        if resume:
            self.restore()
        elif self.path.exists():
            raise FileExistsError(f"{self.path} already exists. Use --resume or a new output directory.")

    def restore(self):
        state = torch.load(self.path, map_location="cpu", weights_only=True)
        if state["config"] != self.config:
            changed = sorted(k for k in set(state["config"]) | set(self.config)
                             if state["config"].get(k) != self.config.get(k))
            raise ValueError(f"Training configuration changed: {', '.join(changed)}")
        if len(state["history"]) > self.epochs:
            raise ValueError(f"Checkpoint already contains {len(state['history'])} epochs")

        self.model.load_state_dict(state["current"])
        self.optimizer.load_state_dict(state["optimizer"])
        self.scheduler.load_state_dict(state["scheduler"])
        self.history = state["history"]
        self.best_weights = state["best"]
        self.best_score = state["best_score"]
        self.best_epoch = state["best_epoch"]
        self.initial_checkpoint = state["initial_checkpoint"]
        self.rng.bit_generator.state = state["augmentation_rng"]
        torch.set_rng_state(state["torch_rng"])
        if state["cuda_rng"] is not None:
            torch.cuda.set_rng_state(state["cuda_rng"])

        if self.epochs != state["target_epochs"] and self.epochs > len(self.history):
            # Continue from the current LR, or start another cosine cycle after a completed schedule.
            for group, base in zip(self.optimizer.param_groups, self.scheduler.base_lrs):
                group["lr"] = base if len(self.history) == state["target_epochs"] else group["lr"]
                group["initial_lr"] = group["lr"]
            self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
                self.optimizer, T_max=self.epochs - len(self.history)
            )

    def consider(self, score: float, epoch: int):
        if np.isfinite(score) and score > self.best_score:
            self.best_score = score
            self.best_epoch = epoch
            self.best_weights = {k: v.detach().cpu().clone() for k, v in self.model.state_dict().items()}

    def finish_epoch(self, metrics, score):
        epoch = len(self.history) + 1
        self.history.append(dict(epoch=epoch, **metrics))
        self.consider(score, epoch)
        self.scheduler.step()
        use_cuda = next(self.model.parameters()).is_cuda
        save_atomic({
            "format_version": 1, "config": self.config, "target_epochs": self.epochs,
            "history": self.history, "best": self.best_weights, "best_score": self.best_score,
            "best_epoch": self.best_epoch, "initial_checkpoint": self.initial_checkpoint,
            "current": self.model.state_dict(), "optimizer": self.optimizer.state_dict(),
            "scheduler": self.scheduler.state_dict(), "torch_rng": torch.get_rng_state(),
            "cuda_rng": torch.cuda.get_rng_state() if use_cuda else None,
            "augmentation_rng": self.rng.bit_generator.state,
        }, self.path)
