# Development experiments

## How the Wave clearance regression is measured

At seed 42, a 200 × 160 mm Wave board with 14 pieces and 45 growth steps has **3.050 mm** between its nearest eligible settled rod segments. The regression requires at least 2.8 mm. During the collision sweep attached to the accepted snapshot, the solver observed a minimum of **2.723 mm** before applying the corresponding pairwise correction. Each number is a minimum over its own stage. The two minima can come from different pairs. [Board and audit](geometry.md#observed-result)

The final measurement uses line-to-line distances after smoothing (or the retained unsmoothed fallback) and rounding to the 0.01 mm grid, before intersections split the exported paths. A crossing between eligible segments gives zero. The calculation excludes edges sharing a node, nearby segments along the same rod, and fixed segments paired with segments in their low-mobility attachment zone. Tiny chains discarded before polygonization supply no cut segment. If there is no eligible pair the report stores `null`. [Distance calculation](../../dendrite_engine/rod_growth.py) · [Analytic geometry fixtures](../../tests/test_rod_clearance.py)

To investigate a poor cut, first check the report's `clearance_stage` and compare measurements from the same stage. Then reread the SVG to check the emitted pieces, coverage and overlap. To estimate material strength, measure the cut wood around the marked seams. [Maze and Wave regressions](../../tests/test_dendrite_engine.py) · [Calibration worksheet](../../docs/helleristning-750/calibration-measurement-worksheet.md)

The following experiments use the repository’s saved measurements as baselines. Keep the original inputs and reports so differences can be traced to a particular change.

## 1. Check whether a better outline makes a better board

The medium PathNet v2 decoder flagged 2 necks in 2,048 held-out logo outlines at 34 mm. The rule was a measured neck strictly below 2.75 mm, including the validator’s 0.05 mm tolerance on a 2.8 mm design limit. This is a piece-level result. On the seed-22, 368 × 368 mm board with 183 logo pieces, the v2 layout increased silhouette coverage while fewer adjacent pairs locked at a 1 mm threshold. [Neck measurements and source hashes](../../resources/nervous-system/visual-atlas/05-neck-validation.csv) · [Board measurements and source hashes](../../resources/nervous-system/visual-atlas/06-board-tradeoff.csv)

Read the existing comparison at a common bay strength. Run this Python snippet from the repository root. It needs only the standard library and the checked-in CSV.

```python
import csv
from pathlib import Path

results = Path("resources/nervous-system/visual-atlas/06-board-tradeoff.csv")
with results.open(encoding="utf-8", newline="") as stream:
    rows = sorted(csv.DictReader(stream), key=lambda row: row["version"])
for row in rows:
    if float(row["bay_rule_strength"]) == 64:
        coverage = float(row["silhouette_coverage_percent"])
        print(row["version"], f"{coverage:.2f}%",
              f'{row["locked_neighbor_pairs"]}/{row["adjacent_pairs_tested"]} pairs')
```

Expected output is `v1 68.48% 83/506 pairs` and `v2 77.38% 57/505 pairs`. The differing denominators matter because the layouts have different contact graphs. A next experiment would run both decoders at each of several **paired seeds** with common dimensions, piece count, bay strength, neck repair, layout-search budget and audit resolution. Save the checkpoint digest, layout or pose file, and cut SVG for each run. Compare coverage, locked pairs as a fraction of adjacent pairs, and both framed and frameless lock margin per seed. Re-cut one saved layout at multiple bay strengths to isolate the seam rule. [Layout options and measurements](../../pathnet/PATHNET_V2.md)

## 2. Calibrate the Dendrite parameter guide on failure cases

The saved guide used 80 simulated training patches and 20 validation patches. Lock-margin mean absolute error on validation was 0.452 mm against a mean target of 0.3492 mm. The archived report gave both pass-rate targets a mean of 1.0 and used 1.0 as its fallback R² for negligible target spread. New reports show a null R² in that case and count held-out patches with at least one failed piece for each rate. Those counts expose how many failure cases an evaluation covers. [Guide and checkpoint details](parameter-guide.md) · [Metric calculation](../../dendrite_engine/neural_guide.py)

For the next dataset, keep a record for each attempted patch, including those whose polygonization fails. Split by board seed before tuning. For each held-out board, compare the preset and the guide on identical outlines, fixed figures, seeds, scale and audit settings. Group results by style and observed failure type. Report failure counts, actual versus predicted pass fractions and lock-margin error in millimetres. A comparison of recommendations alone cannot measure how the exported boards perform. [Simulation and export](../../dendrite_engine/cli.py) · [SVG audit](../../dendrite_engine/postprocess.py)

## 3. Measure the boundary between a mask and a cut

The U²-Net replay scores a pixel mask. A disconnected foreground island or a thin appendage can change the topology of a physical outline even at high mask overlap. Start with the archived photograph, then add labeled examples with holes, fine tips and touching subjects. Keep the model, source image and final board dimensions fixed while varying the contour threshold. Record component and hole counts before selecting a perimeter, then measure contour displacement and the narrowest surviving feature in millimetres. Store the pixel-to-board transform with each result. [Mask replay](photo-mask.md) · [Current threshold illustration](../../resources/nervous-system/architecture-assets/mask-flow.png)

For a physical check, cut paired versions from the same material lot and record measured thickness, kerf, breakage and fit at the same marked seams. The repository includes a coupon worksheet for recording measurements. [Calibration worksheet](../../docs/helleristning-750/calibration-measurement-worksheet.md)
