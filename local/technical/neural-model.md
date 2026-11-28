# From a trained network to one closed outline

APHEX PathNet v1 is a working entry point into this repository’s neural geometry. Its trained weights, inference code, source contours and evaluation report are tracked in Git. Given a family ID and six deformation values, the model draws one closed silhouette. Board dimensions, neighboring pieces and shared seams enter later in the puzzle composer. This model comes from the local APHEX project. [Trained checkpoint](../../pathnet/pathnet.pt) · [Model and trainer](../../pathnet/train.py) · [Board composer](../../pathnet/puzzle.py)

## Decode a real example

From the repository root in PowerShell, run the probe with the project environment. The first command prints measurements. The second prints a self-contained SVG of the same contour, suitable for redirecting to a file. The seed selects the same accepted sample on repeat runs in the same environment.

```powershell
.\.venv\Scripts\python.exe -B local/technical/probe_model.py
.\.venv\Scripts\python.exe -B local/technical/probe_model.py --svg
.\.venv\Scripts\python.exe -B local/technical/probe_model.py --family 1 --seed 7
```

The probe imports the production sampler, which loads the checkpoint on CPU and evaluates it using one PyTorch thread. By default it selects family 0, `APHEX LOGO`, and seed 22. The SHA-256 of the tracked v1 checkpoint is `c54078013fbb5680da8f34704ac1b9be8172001901c93755f3c15818142af72c`. To examine another trained PathNet checkpoint, pass `--checkpoint PATH`. The sampler supports v1 and v2. [Probe](probe_model.py) · [Canonical sampler](../../pathnet/generate.py)

With the tracked weights, seed 22 produced a valid logo polygon of area **0.301794** in normalized units squared, with **0.928741** IoU against the undeformed logo. All 96 cubic joins, including the closing join, had zero measured gap. Family 1 with seed 7 produced the glyph `A` with area 0.275640. The repository also tests that the default sampler reproduces the original v1 results. [Sampler regression test](../../tests/test_pathnet_v2.py)

## Tensor and curve contracts

| Stage | Shape | Meaning |
| --- | --- | --- |
| Inputs | `k: int64[B]`, `z: float32[B,6]` | One ID from 0–58 and six continuous deformation coordinates per contour. |
| Embedding and prediction | `[B,48]` embedding plus `[B,6]` latent | Concatenate, then apply three linear layers of width 192, with SiLU after the first two. The 87,504 learned parameters include the family embeddings. |
| Decoder output | `float32[B,96,2]` | Each row is an ordered pair of unitless, periodic cubic B-spline control coordinates. A single decoded sample has shape `[96,2]`. The controls generally lie off the curve. |
| Vector outline | `[96,4,2]` | Each segment contains its start, two Bézier handles and end. The end of each segment meets the next start, including the last-to-first join. The SVG writer emits 96 cubic commands and closes the path. |
| Polygon used for measurements | 576 sampled positions before closure | Six positions per cubic approximate the curve for validity, area and intersection-over-union (IoU). Area is in normalized units squared. |

During v1 inference the sampler draws uniform latents within ±0.28 for the logo and ±1 for the other families. It makes up to 60 attempts to find a valid polygon with area above 0.035. A logo sample must also have IoU of at least 0.85 with the traced reference. IoU is intersection area divided by union area. That score measures silhouette agreement at the reference alignment. [Sampling and filters](../../pathnet/generate.py) · [Reference controls](../../pathnet/reference.json)

## What the weights learned

Training starts with the outer contour of the supplied logo image and the ordered glyph and variant outlines. Each base outline is centered on its bounding box, scaled by its larger extent and fitted to 96 equally spaced perimeter samples. The trainer generates targets by applying six known stretch, shear and bend coordinates to the base controls, with smaller deformation amplitudes for the logo. The network learns that mapping with AdamW and a loss on both coordinate errors and differences between consecutive errors around the loop. [Training target construction](../../pathnet/train.py) · [Glyph data](../../pathnet/aphex-glyph-dataset.json)

The saved 80-epoch run used 8,690 training contours across 59 families. All 2,140 held-out contours yielded valid sampled polygons. Mean IoU against their deformed targets was 0.963. The 400 held-out logo contours averaged 0.977 IoU against their own targets and 0.783 against the single undeformed logo over the wider training range. These are measurements on new deformations of known families. New drawing families, human recognition and physical fit require separate evaluation. The repository also records that the source logo and glyph-derived geometry have not been cleared for redistribution or commercial use. [Evaluation report](../../pathnet/report.json) · [Project scope and rights](../../README.md)

## Where the family branches

- **PathNet v2** keeps the 96-control outline interface and predicts a displacement from each family’s base controls. Its default 18-dimensional latent adds six movable two-axis handles. Every sampled outline contributes to the shape reconstruction loss. The optional neck and contact penalties apply to selected logos. A batch containing only glyphs, or a zero logo-rule budget, contributes zero to that penalty and still trains on its shape targets. In this workspace, `--checkpoint runs/pathnet-v2/final-m/pathnet-v2.pt` loaded a trained 13,681,536-parameter model. This run file is stored outside Git. [v2 architecture and training](../../pathnet/PATHNET_V2.md) · [Batch and gradient checks](../../tests/test_pathnet_v2.py)
- **FossilNet** extends the contour representation to the owner’s fossil drawings. Its class-conditioned decoder uses an eight-dimensional latent, 192 outline controls and up to four 16-point engraving loops. FossilNet v3 is a separate variational autoencoder with a 32-dimensional latent, 256 outline controls, up to six engraving loops and a size output. Both need their own loaders and checkpoints. [Class-conditioned FossilNet](../../pathnet/FOSSILNET.md) · [FossilNet v3](../../pathnet/fossil_net3.py)
- **Formverk’s neural guide** proposes numeric generator settings. Its output is a set of parameters for an independent puzzle simulation. The separate whimsy adapter can consume trained PathNet v1 weights to supply individual motifs. [Guide source](../../dendrite_engine/neural_guide.py) · [Whimsy adapter](../../dendrite_engine/whimsies.py)
