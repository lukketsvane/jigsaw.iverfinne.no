# APHEX PathNet

A conditional neural decoder for complete closed vector pieces. Given a family ID and a six-dimensional latent vector, it predicts 96 periodic cubic B-spline controls, converted to SVG cubic Bézier curves. The model does not generate puzzle seams or retrieve a stored path at inference time.

## Run

`python -m pip install -r requirements.txt`

`python generate.py --seed 22 --count 60 --output ./generated`

`python puzzle.py --seed 22 --width 300 --height 220 --motifs 24 --logo-share 0.85 --output ./generated`

`python train.py` retrains the network from the included glyph dataset and reference image. The logo family has 2,600 augmented training targets. All validation contours use held-out deformation vectors on known families. Logo sampling uses a narrower latent range and rejects shapes below .85 IoU with the reference. New boards require every requested motif to fit and verify each logo against the transformed reference after the final SVG is exported and reparsed. The report records the minimum final logo IoU and the logo share of all pieces.

For a less regular layout, add --jitter .18 to the dense-board command; .11 remains the default. Higher jitter can fail placement.

For a denser motif board: `python puzzle.py --seed 22 --width 300 --height 220 --motifs 32 --logo-share 1 --output ./generated/dense32`.

## Outputs

- `aphex-pathnet-shapes.svg`: filled native cubic SVG shape sheet.
- `aphex-pathnet-shapes-cut.svg`: same closed curves as strokes, with no background.
- `aphex-pathnet-puzzle.svg`: final single-cut network in millimetres. Curves are flattened for deterministic geometric validation and snap-rounded at 0.01 mm.
- `aphex-pathnet-piece-outlines.svg`: closed individual piece paths; not for cutting assembled puzzles (shared borders are repeated).
- `pathnet.pt`: model weights; `decoder.json`: portable neural inference weights.
- `report.json`, `logo-sampling-report.json`, `puzzle-report.json`: measured results.

## Field composition: every piece a logo

`field.py` replaces "place logos, then fill the leftover with filler pieces". Every piece is a PathNet silhouette and the board is divided between them: a point belongs to the piece whose front reaches it first (an Eikonal first-arrival partition), so every seam is shared and there are no fillers. Run it from this folder with the repository's virtual environment; it uses the GPU when PyTorch sees one.

```powershell
..\.venv\Scripts\python.exe field.py --seed 22 --pieces 32 --output .\generated\my-board
```

```powershell
..\.venv\Scripts\python.exe field.py --resume .\generated\field-c\aphex-field-state.pt --field-steps 200 --output .\generated\field-d
```

The first command builds a board from a seed: layout search, joint refinement of poses and latents through the decoder, cutting, training, export. The second continues from a saved board: it keeps the silhouettes and poses in the `*-state.pt` file and goes on training its slowness network. With `--field-steps 0` it cuts the stored field again without training, and `--discard-field` drops the stored network and keeps only the layout.

### How the gaps are shared

Rigid logos cover about two thirds of a board (0.65 to 0.68 over twelve layout restarts on 300 x 220 mm), so a third of the wood is gap that has to go to some piece. Three rules decide which:

- **Bay rule (`--bay`, default 32).** A piece's own front is `1 + bay` times slower inside its own bays, the concave regions between its convex hull and its outline. A neighbour that reaches a bay mouth therefore fills the bay as a tab, and the piece keeps its negative space. With `--bay 0` the seams are the plain bisectors between silhouettes, which fill every bay with the piece's own material and leave nothing to hook on.
- **Spur repair.** Gap material that a 1.5 mm disc cannot enter (a lobe on a thin neck, a spur) is handed to the piece it lies against. A piece never loses any of its own silhouette.
- **Slowness network (`--field-steps`, default 100, about 7 s per step on a GTX 1070).** A small network sets a slowness field shared by all pieces, which moves the seams inside the gaps. Every ten steps the current field is cut into real pieces and measured. Training raises the assembly lock margin (below): each group of pieces that could still slide out at 1.5 mm above the current margin must gain a seam that stops it. A trained field is used only if its pieces pass the erosion test and beat the untrained ones on lock margin.

`--layouts` (default 3) refines that many of the best-packed layouts and keeps the one whose untrained pieces lock best. In a from-scratch run of seed 22 (`generated/field-fresh-22`, six restarts, no training, 5.6 minutes), the three best-packed layouts had near-equal packing scores (3.05, 3.05, 3.07) and lock margins of 4.2 mm, 0.8 mm and 0.0 mm under the bay rule, so the choice of layout matters more than the training. The board it kept holds at 4.1 mm on the full-resolution cut.

### Measures in `field-report.json`

- `assembly_lock_margin_mm`: the largest threshold at which no group of pieces can slide out of the assembled board in any of 360 directions. A seam stops a piece along a direction by the length of seam that faces against it; a group is free when no seam leaving it reaches the threshold. Translations in the plane only: rotation and lifting are not modelled, and it is not a kerf or finite-motion test.
- `locked_pairs` in `blocking_Nmm`: neighbouring pairs that hold each other in every direction by N mm (a true undercut).
- `mean_outline_kept_2mm`: share of each silhouette's outline that is still an edge of its piece, within 2 mm. `mean_own_bays_filled`: share of a silhouette's bays taken by its own piece.
- `mean_silhouette_iou`: overlap of a piece with its silhouette. It stays near the coverage (0.67) however the gaps are shared, so it does not separate a readable piece from a bloated one.

`aphex-field-piece-sheet.png` shows every piece alone, turned upright, with its silhouette outlined; `-plain` is the same without outlines or numbers. `aphex-field-puzzle-score.svg` carries the exact silhouettes as blue score lines over the red cuts.

### Seed 22, 32 logos, 300 x 220 mm

One layout, cut three ways. The first column is the board exported in `generated/field-a`; the others cut the same silhouettes and poses (`generated/` is not tracked, so these folders exist only where the runs were made).

| | Bisector seams with the first trained field (`field-a`) | Bay rule, no training (`field-b`) | Bay rule and resumed training (`field-c`) |
|---|---|---|---|
| Assembly lock margin | 0.19 mm | 4.78 mm | 6.00 mm |
| Directions (of 360) in which a group can slide out at 1 / 2 / 3 mm | 80 / 184 / 290 | 0 / 0 / 0 | 0 / 0 / 0 |
| Locked pairs of 77 at 1 / 2 / 3 mm | 0 / 0 / 0 | 23 / 20 / 18 | 24 / 19 / 17 (of 76) |
| Outline kept within 2 mm (mean, worst piece) | 0.50, 0.33 | 0.70, 0.54 | 0.70, 0.54 |
| Own bays filled | 0.36 | 0.15 | 0.15 |
| Piece against its silhouette, IoU (mean, worst) | 0.67, 0.54 | 0.67, 0.49 | 0.67, 0.49 |
| Distance from a piece's edge to its silhouette (mean, largest) | 2.5 mm, 13.6 mm | 3.2 mm, 29.4 mm | 3.2 mm, 29.5 mm |
| Pieces failing the 2.5 mm erosion test | 0 | 0 | 0 |
| Cut length | 4,839 mm | 5,903 mm | 5,954 mm |

The resumed run continued the 263,169-parameter network saved with `field-a` for 200 steps (27 minutes on a GTX 1070) and kept the field from step 80. During training the cut is measured on a 0.5 mm grid, where the margin rose from 4.88 mm to 5.44 mm and then stayed there; on the final 0.25 mm cut it is 6.00 mm against 4.78 mm untrained. The bay rule accounts for nearly all of the change from `field-a`, and training adds about a millimetre of margin on this board. Its effect on a layout that locks poorly has not been measured.

Every exported cut file is re-read, noded and polygonised, and compared face by face with the pieces before anything is measured (no gap, overlap or duplicate segment). No board from `field.py` has been laser-cut; the erosion test and the lock margin are geometric checks, not a physical one. Each piece still carries about half its silhouette's area again in gap material, so a cut piece is a logo with tabs, not a clean logo: the bay rule keeps more of every outline but lets tabs reach further from it, which the last row of the table shows.

## Placement and filler composition (`puzzle.py`)

Puzzle composition uses constrained geometry rather than a learned whole-board model. Filler seams preferentially receive undercut tabs, verified against the reparsed cut network. The report counts zero-clearance pairs whose shared contact normals prevent any infinitesimal in-plane translation; this is not a finite-motion or physical kerf test. The generator rejects configurations that fail its geometric checks; new seeds and sizes are not guaranteed. No physical laser cuts, mechanical interlocking, rights review, or human recognition tests have been performed. The erosion test detects certain narrow/disconnected sections but is not a complete structural or laser-kerf analysis. Source logo is traced from the supplied reference image. The augmented dataset contains 59 known families, not thousands of independent designs.
