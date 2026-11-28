# Formverk wood trial · observer notes

Open `index.html` in the extracted kit for the bench worksheet. It calculates kerf, saves a record as JSON and can reopen a saved record. Use a separate record per solver. Blank fields represent measurements still to take.

## Why compare these boards?

The PathNet board study produced two competing predictions. The newer design covers more of its board with recognizable target silhouettes. Fewer of its neighboring pairs meet the digital locking criterion, yet the predicted margin for the assembled board increases. Pair fit and whole-board movement can therefore tell different stories.

| Saved digital measurement | A · PathNet v1 | B · PathNet v2 M |
|---|---:|---:|
| Board area inside target silhouettes | 68.48% | 77.38% |
| Adjacent pairs meeting the 1 mm blocking criterion | 83 of 506 | 57 of 505 |
| Predicted unframed assembly margin | 0.5625 mm | 1.03125 mm |
| Cut length | 18.76 m | 18.39 m |

The 1 mm criterion measures projected seam length opposing in-plane translation in the least-blocked of 360 tested directions. Actual clearance, holding force and breakage require separate measurements. Each board is 368 × 368 mm with 183 pieces, layout seed 22 and bay strength 64. The v2 board uses the archived 15-minute M decoder. Its optimization moved piece centers independently of the v1 board. Results describe these two complete layouts. The motif carries the project's personal, non-commercial research restriction.

Use the A/B labels on trays and soluble storage labels or bags. Keep the solver away from the machine files, observer key and seam maps during timed solves. Hold support, frame, surface, orientation, sorting rules and time limit constant. Match stock from the same sheet or lot. If repeating the trial, swap sheet positions and cut order. Use plain wood for both boards or apply the same artwork and finish to both.

## Choosing the pairs

S1, S2 and S3 come from matching normalized target locations near the upper left, center and lower right. The locator marks a point on a shared interior seam away from a junction. S4 deliberately samples a pair that meets the 1 mm digital criterion on each board. Compare its observed in-plane separation with the other pairs. This selection supports a mechanism check. It cannot estimate the fraction of locking pairs across a whole board.

The bench worksheet keeps breakage during release, changes during repeated handling and solve time separate. A slow solve may reflect difficulty or recognition. A pair that binds during assembly may reflect kerf or damage. Record what happened before assigning a cause. For a manufactured studio comparison, identify the physical edition and document differences in material, art, piece size, topology and wear.

## Six-style sheet

The sampler uses the same stock area, requested piece count and random seed for all six generators. The algorithms choose their own initial geometry. Each original 90 × 70 mm coupon is included for individual recuts. Preserve its scale and grain direction.

On the placement map, locate the Amoeba and Matrix pieces flagged by the digital 2.5 mm erosion screen. Erosion is a geometric check for slender connections. Give those pieces particular attention during release and reassembly, and record whether either one chips or cracks. A digital flag alone does not establish failure in wood.
