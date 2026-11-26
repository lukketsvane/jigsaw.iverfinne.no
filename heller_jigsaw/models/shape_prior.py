"""A conditional signed-distance VAE for rock-art and animal silhouettes."""

from __future__ import annotations

import hashlib
from pathlib import Path

import cv2
import numpy as np
import torch
from scipy import ndimage
from torch import nn

from ..data.corpus import MotifAsset, compute_sdf_from_mask


def _block(in_ch: int, out_ch: int, stride: int = 1) -> nn.Sequential:
    return nn.Sequential(nn.Conv2d(in_ch, out_ch, 3, stride, 1), nn.GroupNorm(4, out_ch), nn.SiLU())


class AnimalShapeVAE(nn.Module):
    def __init__(self, num_classes: int, size: int = 64, latent_dim: int = 32):
        super().__init__()
        if size < 32 or size % 8:
            raise ValueError("Shape resolution must be at least 32 and divisible by 8")
        self.size = size
        self.latent_dim = latent_dim
        self.side = size // 8
        self.label = nn.Embedding(num_classes, 16)
        self.encoder = nn.Sequential(_block(1, 20, 2), _block(20, 40, 2), _block(40, 64, 2))
        hidden = self.side * self.side * 64
        self.posterior = nn.Linear(hidden + 16, 2 * latent_dim)
        self.expand = nn.Linear(latent_dim + 16, hidden)
        up = lambda: nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False)
        self.decoder = nn.Sequential(up(), _block(64, 40), up(), _block(40, 24),
                                     up(), _block(24, 16), nn.Conv2d(16, 1, 3, padding=1), nn.Tanh())

    def encode(self, sdf, labels):
        features = self.encoder(sdf).flatten(1)
        mu, log_var = self.posterior(torch.cat([features, self.label(labels)], dim=1)).chunk(2, dim=1)
        return mu, log_var.clamp(-9, 5)

    def decode(self, z, labels):
        h = self.expand(torch.cat([z, self.label(labels)], dim=1))
        return self.decoder(h.reshape(-1, 64, self.side, self.side))


def usable_mask(sdf: np.ndarray) -> np.ndarray | None:
    """Check connectivity on the same 256 px mask that the puzzle solver receives."""
    mask = cv2.resize(sdf, (256, 256), interpolation=cv2.INTER_CUBIC) < 0
    if not 0.025 <= mask.mean() <= 0.75 or mask[[0, -1]].any() or mask[:, [0, -1]].any():
        return None
    if ndimage.label(mask)[1] != 1 or not np.array_equal(mask, ndimage.binary_fill_holes(mask)):
        return None
    return mask.astype(np.float32)


@torch.inference_mode()
def sample_motifs(
    checkpoint: str | Path,
    count: int,
    classes: list[str] | None,
    seed: int,
    latent_scale: float = 1.0,
):
    path = Path(checkpoint)
    ckpt = torch.load(path, map_location="cpu", weights_only=True)
    names = ckpt["classes"]
    class_to_family = ckpt.get("class_to_family") or {}
    requested = classes or names
    if count < 1 or set(requested) - set(names):
        raise ValueError(f"Choose a positive count and classes from {', '.join(names)}")
    model = AnimalShapeVAE(len(names), ckpt["size"], ckpt["latent_dim"])
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    rng = np.random.default_rng(seed)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    picked = []
    latent_bank = ckpt.get("latent_bank")
    for idx in range(count):
        name = requested[idx % len(requested)]
        fam = str(class_to_family.get(name, "animal"))
        label = torch.tensor([names.index(name)])
        for draw in range(1, 129):
            z = torch.from_numpy((rng.standard_normal((1, model.latent_dim)) * float(latent_scale)).astype(np.float32))
            if latent_bank is not None:
                bank = latent_bank[name]
                z = bank[int(rng.integers(len(bank)))].unsqueeze(0) + float(ckpt["latent_bank_jitter"]) * z
            mask = usable_mask(model.decode(z, label)[0, 0].numpy())
            if mask is None:
                continue
            asset_id = f"vae-{digest[:8]}-{seed}-{idx + 1:02d}-{draw}"
            picked.append(MotifAsset(
                asset_id=asset_id, family=fam, motif_class=name, mask=mask,
                sdf=compute_sdf_from_mask(mask), country="", jigsaw_tier="generated",
                source_archive="conditional_sdf_vae", source_group=digest, split="generated",
                creator="AnimalShapeVAE", license="See training-sources.json", commercial_ok=False,
                sha256=hashlib.sha256(mask.astype(np.uint8).tobytes()).hexdigest(),
                classification_confidence=0.0,
            ))
            break
        else:
            raise RuntimeError(f"No connected, hole-free {name} proposal in 128 draws for piece {idx + 1}")
    return picked
