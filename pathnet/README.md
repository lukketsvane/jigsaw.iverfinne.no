# APHEX PathNet

A conditional neural decoder for complete closed vector pieces. Given a family ID and a six-dimensional latent vector, it predicts 96 periodic cubic B-spline controls, converted to SVG cubic Bézier curves. The model does not generate puzzle seams or retrieve a stored path at inference time.

## Run

`python -m pip install -r requirements.txt`

`python generate.py --seed 22 --count 60 --output ./generated`

`python puzzle.py --seed 22 --width 300 --height 220 --motifs 24 --logo-share 0.85 --output ./generated`

`python train.py` retrains the network from the included glyph dataset and reference image. The logo family has 2,600 augmented training targets. All validation contours use held-out deformation vectors on known families. Logo sampling uses a narrower latent range and rejects shapes below .85 IoU with the reference.

## Outputs

- `aphex-pathnet-shapes.svg`: filled native cubic SVG shape sheet.
- `aphex-pathnet-shapes-cut.svg`: same closed curves as strokes, with no background.
- `aphex-pathnet-puzzle.svg`: final single-cut network in millimetres. Curves are flattened for deterministic geometric validation and snap-rounded at 0.01 mm.
- `aphex-pathnet-piece-outlines.svg`: closed individual piece paths; not for cutting assembled puzzles (shared borders are repeated).
- `pathnet.pt`: model weights; `decoder.json`: portable neural inference weights.
- `report.json`, `logo-sampling-report.json`, `puzzle-report.json`: measured results.

Puzzle composition uses constrained geometry rather than a learned whole-board model. The generator rejects configurations that fail its geometric checks; new seeds and sizes are not guaranteed. No physical laser cuts, mechanical interlocking, rights review, or human recognition tests have been performed. The erosion test detects certain narrow/disconnected sections but is not a complete structural or laser-kerf analysis. Source logo is traced from the supplied reference image. The augmented dataset contains 59 known families, not thousands of independent designs.
