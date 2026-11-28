# PathNet v2: the Aphex decoder with a richer shape space and learned cutting rules

`train_v2.py` trains the network that draws the logo pieces on the boards (`field.py`, `field_large.py`). The interface is the same as v1 (`train.py`): a class index and a latent vector go in, and 96 periodic cubic B-spline controls (a closed outline) come out. `generate.py` loads a v2 checkpoint when `PATHNET_CKPT` points at one. Without it, every board script uses v1 exactly as before, and the sampler is bit-identical (`tests/test_pathnet_v2.py`).

## Why v2

v1 was measured before anything changed:

- **Drawing accuracy was not the limit.** v1 draws the logo at z = 0 with IoU 0.975 against the traced logo, and its worst error is 0.28 mm on a 34 mm logo. With 96 controls, the logo itself can be represented to IoU 0.998 (0.03 mm).
- **The shape space was the limit.** v1 learned 2600 fixed deformations made of 6 global modes (stretch, shear, two bends), and the board optimiser may move a logo latent only within ±0.28. On the 368 mm board (seed 22, 183 logos) the logos cover 68.5 % of the board. The remaining 31.5 % is filler handed to neighbours, and 8 refined latents had to be reverted.

## What changed

- **18 latents.** v1's 6 global modes (logo amplitude 0.174, about v1's ±0.3) plus 2 latents at each of 6 handles. The handles are farthest-point features of the outline: for the logo, both ends of the top bar, the right tower, both feet and the notch. A handle moves its part of the outline smoothly (Gaussian falloff, 0.2 of the logo's extent; amplitude 0.045, about 1.5 mm at 34 mm), so one arm can lean into a gap while the rest stays put.
- **The whole latent cube is usable.** The amplitudes were calibrated with `train_v2.py calibrate` (sample sheets in `runs/pathnet-v2/calibration-h*.png`). IoU against the undeformed logo turned out to be a poor recognisability test: a 1 mm shift of a thin arm drops it a lot while the logo stays obvious. So checkpoints carry `zlim = 1` for every class and `ref_iou_min = 0.45`, which only catches failed decodes.
- **Cutting rules learned on the decoder's own output (logo only).** No neck under 2.8 mm and no two parts of the outline closer than 0.7 mm, both at the smallest piece size (34 mm). The traced logo's own narrowest neck is 2.92 mm. A neck pair is two outline points, further apart along the outline than 1.6 × the limit, that see each other through the inside (each lies behind the other's outward normal); contact pairs see each other through the outside. About 10 % of the target shapes break the neck rule. Where a target breaks a rule, the decoder learns the nearest shape that keeps it, and fewer than 0.2 % of its outputs still do (see the run logs). On the 368 mm board every decoded logo kept necks of at least 2.83 mm at its own size. Glyph classes are not held to the rule: their strokes are thinner by design, and including them made training diverge.
- **Network.** A class embedding (64) and the latent feed residual blocks with class-conditioned feature modulation (FiLM, LayerNorm, SiLU). They predict a displacement from the class's base outline, scaled by 0.1 and zero at the start. Learning rates scale with width: 1e-3 at 512, 5e-4 at 1024, about 3e-4 above. At 1e-3, widths of 1024 and more tangled the outlines and diverged.
- **Training.** New targets are drawn on the GPU at every step (half or more of them logos, 15 % at the faces of the latent cube), so no shape is seen twice. AdamW with warm-up and a cosine decay over a time budget; the rule weight ramps in from 5 % to 15 % of the budget, with a soft cap so a tangled outline cannot drown the fidelity term. The saved weights are an exponential moving average.

```powershell
..\.venv\Scripts\python.exe train_v2.py calibrate --ag .174 --ah .03 .045                       # shape-space statistics and a sample sheet
..\.venv\Scripts\python.exe train_v2.py train --name m --width 1024 --depth 6 --lr 5e-4 --minutes 15
..\.venv\Scripts\python.exe train_v2.py overview ..\runs\pathnet-v2                              # latest progress sheet of every run
..\.venv\Scripts\python.exe train_v2.py evaluate ..\runs\pathnet-v2\m\pathnet-v2.pt               # exact polygon metrics (also for v1's pathnet.pt)
$env:PATHNET_CKPT = "..\runs\pathnet-v2\m\pathnet-v2.pt"
..\.venv\Scripts\python.exe field_large.py --seed 22 --pieces 183 --width 368 --height 368 --min-size 34 --restarts 4 --layouts 4 --bay 64 --neck-repair 1.25 --output ..\runs\aphex-logo-398-v2\seed22
Remove-Item Env:PATHNET_CKPT
```

Runs are written to `runs/pathnet-v2/<name>`: `progress-*.png` every 5 minutes (network output over its targets, with the error curve), `log.jsonl`, `report.json` and `pathnet-v2.pt`. Checkpoints are 10–160 MB, so they live in `runs/` and on Google Drive (`jigsaw-shadow-transfer/pathnet-v2`), never in Git.

## Sweep: 15 minutes each, four sizes side by side on one RTX A4500

The GPU was 97 % busy, shared with other jobs. Errors are measured at the smallest logo size (34 mm) on a fixed validation set: 2048 logos (348 of them at the cube's corners) and 16 shapes per glyph class. "Clean" means targets that keep the cutting rules.

| Run | Parameters | Steps | Shapes seen | Logo rms (clean) | Logo worst (clean) | Necks < 2.8 mm | Glyph rms |
|---|---|---|---|---|---|---|---|
| s: 512 × 4 | 2.5 M | 6,088 | 24.9 M | 0.0077 mm | 0.074 mm | 0.15 % | 0.033 mm |
| m: 1024 × 6 | 13.7 M | 3,928 | 16.1 M | 0.0080 mm | 0.072 mm | 0.10 % | 0.031 mm |
| w: 2048 × 4 | 35.2 M | 3,345 | 13.7 M | 0.0082 mm | 0.080 mm | 0.20 % | 0.033 mm |
| l: 1536 × 8 | 39.8 M | 2,605 | 10.7 M | 0.0090 mm | 0.069 mm | 0.20 % | 0.039 mm |

All four sizes reach errors far below the laser kerf (0.15 mm) within 15 minutes. From there the shape space and the rules decide the board, not the network's size.

## The 368 mm board, seed 22: v1 against v2

Same command, seed and board. v2 used the 15-minute `m` model; the cut was made with `--neck-repair 1.25` and `--bay 64` (see below).

| Measure | v1 | v2 |
|---|---|---|
| Board covered by logo silhouettes | 68.5 % | 77.4 % |
| Piece vs its own logo silhouette, mean IoU (min) | 0.686 (0.465) | 0.771 (0.564) |
| Outline kept within 1 mm | 0.653 | 0.716 |
| Latents reverted after refinement | 8 | 0 |
| Pieces failing any fabrication screen | 0 | 0 |
| Worst thin tip (reach beyond a 3 mm disc) | 2.07 mm | 1.27 mm |
| Assembly lock margin, frameless / in the frame | 0.84 / 9.66 mm | 1.03 / 11.63 mm |
| Neighbour pairs locked at 1 / 2 / 3 mm | 88 / 56 / 45 | 57 / 34 / 24 |

During refinement the gap energy fell to about a third of v1's (0.28 against 0.79), with less overlap (0.34 mm deepest, against 0.57 mm). The trade-off is in the last row. Pieces hug their logos, so less filler is left to form tabs, and fewer neighbour pairs lock on their own. The assembly as a whole locks better, most of all in the frame the board ships in.

**Neck repair.** Tighter packing leaves thinner gap wedges. With the default partition, one wedge 2.5 mm wide became a 0.006 mm² crumb under the audit's 2.5 mm neck test. `field_large.py --neck-repair 1.25` (also `partition(..., repair=1.25)`) runs `field.despike` once more at the audit's radius with no reach allowance, so gap material a 2.5 mm disc cannot reach goes to the neighbour it lies against. Logo silhouettes are never touched. It is off by default, so existing boards cut exactly as before.

**Bay rule.** Re-cutting the same v2 layout with `--bay 32 / 48 / 64` gives 48 / 52 / 57 pairs locked at 1 mm and assembly locks of 0.94 / 1.03 / 1.03 mm. Coverage and fit stay the same.

The Aphex Twin logo is not cleared for commercial use. These boards are for personal, non-commercial research (see `README.md`).
