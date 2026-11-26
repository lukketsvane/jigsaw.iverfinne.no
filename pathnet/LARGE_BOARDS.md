# Large logo boards: hundreds of pieces

`field_large.py` builds boards in which every piece is an Aphex logo, using the same method as `field.py` (first-arrival partition, bay rule, re-read SVG export), at hundreds of pieces. `field.py` keeps one board-sized distance map per piece; at 300 pieces on 570 × 390 mm and 0.25 mm pixels that is 4 GB per map and well over the 20 GB of the card once the bay-rule Eikonal step runs. `field_large.py` computes each piece on a fixed window around its silhouette instead and merges the first and second arrivals into board-sized buffers. The fronts of different pieces never interact (other silhouettes are only obstacles), so the result is the same partition. `tests/test_field_large.py` checks this: with deliberately small windows the label image, every face and the cut network are identical to `field.partition`, with and without the bay rule.

Run from this folder with the repository's virtual environment:

```powershell
..\.venv\Scripts\python.exe field_large.py --seed 22 --pieces 300 --width 570 --height 390 --min-size 34 --restarts 4 --layouts 4 --output ..\runs\aphex-logo-field-300\seed22
..\.venv\Scripts\python.exe audit_field.py ..\runs\aphex-logo-field-300\seed22
```

The board fits a 600 × 400 mm laser bed. `--min-size 34` keeps the logo intact: measured on the traced reference, the logo survives a 1.5 mm erosion (3 mm features) from 34 mm across and splits at 26 mm or less. Every refined layout is saved as `layout-N-state.pt`; `--resume` cuts a saved layout again, and `lock_probe.py <state> --bays 16 32 64` re-cuts it at several bay-rule strengths. Slowness-field training is not used at this size (one training step costs more than the whole partition).

`audit_field.py` screens the exported cut file on its own: it re-reads and polygonises the SVG, checks coverage, overlap and duplicate segments, then every piece for kerf widths 0.10–0.25 mm, 2.5 mm and 3 mm necks, thin tips (material a 3 mm disc cannot reach, and how far it reaches beyond the robust body), holes and size. It recomputes the planar translation lock margin from the re-read pieces and outlines the group that would slide out first. Results go to `fabrication-audit.json` and `fabrication-audit.png`.

## Seed 22: 300 logos, 570 × 390 mm

24 minutes on an RTX A4500 (four packing restarts, four refined layouts compared by lock margin on a 0.5 mm cut).

| Measure | 300-logo board | 250-piece glyph-guided reference (600 × 400 mm), same audit |
|---|---|---|
| Pieces failing any fabrication screen | 0 | 144 |
| Necks under 2.5 mm / 3 mm | 0 / 0 | 67 / 74 |
| Thin tips reaching over 3 mm (worst) | 0 (2.37 mm) | 120 (6.78 mm) |
| Kerf 0.10–0.25 mm failures | 0 | 0 |
| Gap, overlap, duplicate cut segments | 0, 0, 0 | 0, 0, 0 |
| Piece area (min, median, max) | 430, 717, 1,328 mm² | 540, 942, 2,044 mm² |
| Assembly lock margin | 1.69 mm | 3.47 mm |
| Cut length | 30.6 m | 27.1 m |

Logo sizes run from 34 to 55 mm. The pieces keep 83% of their logo's outline within 2 mm on average (51% for the worst piece). Their overlap with their own silhouette averages 0.69, because about a third of the board is gap material shared out as tabs. The weakest interlock is a block of 56 pieces in one corner that can slide out together just above 1.69 mm; the other layouts of this seed reached 1.88 mm (on the coarse cut), 0 mm and a layout with fragile pieces. Raising the margin needs seams that hook across that fault line.

These are geometric checks of the cutting file. Nothing here has been cut, assembled or tested in a material, and the logo is not cleared for commercial use.
