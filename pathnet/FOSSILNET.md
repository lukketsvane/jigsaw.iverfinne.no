# FossilNet: puzzles from hand-drawn trilobites, corals and dendrites

`fossil_net.py` learns the owner's own drawings: 13 white-on-black silhouettes (10 trilobites, 2 corals and an antler-shaped dendrite), each also mirrored, 26 shape families in all. Given a family and an 8-dimensional latent, the network predicts 192 periodic cubic B-spline controls of the outline (PathNet's construction at twice the resolution, so ribs, legs and branches survive) and up to four 16-point loops for the drawing's eyes and faces. It is trained on smooth deformations of every drawing (scale, shear, bends, waves, taper, arch): about 2 minutes on the RTX A4500, 99.3% mean outline overlap on 3,120 held-out variants.

`fossil_field.py` builds boards in which every piece is one of these creatures, with field_large's method (packing, joint refinement of poses, sizes and latents through the decoder, windowed first-arrival partition with the bay rule, re-read SVG export).

```powershell
..\.venv\Scripts\python.exe fossil_net.py sources --images <folder of drawings>
..\.venv\Scripts\python.exe fossil_net.py train
..\.venv\Scripts\python.exe fossil_field.py --mix trilobites --output ..\runs\fossil-boards\trilobites-v3-s1
..\.venv\Scripts\python.exe audit_field.py ..\runs\fossil-boards\trilobites-v3-s1 fossil-field-puzzle.svg
..\.venv\Scripts\python.exe frame_lock.py ..\runs\fossil-boards\trilobites-v3-s1 fossil-field-puzzle.svg
```

`--mix` is `trilobites`, `reef` (corals and dendrites) or `fossil-sea` (all). The piece count follows from `--mean-size` (default 34 mm before each family's size factor).

## Making drawings cuttable

- **Parts and notches at least 3 mm.** Every drawing gets a size factor so that at 30 mm its parts survive a 1.5 mm erosion and 80% of its notches stay at least 3 mm wide, room for a neighbour's tab. Narrow-notched trilobites would need 52–122 mm for that, so the factor is capped at 1.8. Notches still too narrow are engraved instead of cut.
- **Engraving layer.** The drawing's own outline is engraved wherever the cut does not follow it, together with the eyes and faces, at least 1 mm inside every piece. A piece therefore reads as the creature even where its outline could not be cut.
- **Safe latent radius.** Each family has a latent radius inside which 90% of extreme latents still overlap the drawing by at least 0.8 (0.15 for the antler, up to 0.6 for compact trilobites). Refinement stays inside it. A latent is put back if the decoded outline drifts from the drawing or loses a 3 mm part at its cut size, and poses then settle with latents fixed.
- **Rounded tips.** Pointed parts such as tail spines stay thin near their end at any size. The cut outline is opened with a 3 mm disc, and the full spine is still engraved.

## Boards (570 × 390 mm, seed 1)

| Board | Pieces | Fails any screen | Worst thin tip | Lock, frameless | Lock in a frame | Engraved |
|---|---|---|---|---|---|---|
| Trilobites | 97 | 0 | 1.87 mm | 0.66 mm | 12 mm (cap) | 19.5 m, 100 eyes |
| Fossil sea | 116 (89 trilobites, 18 corals, 9 dendrites) | 1 (a 2.5–3 mm neck) | 1.90 mm | 0.38 mm | 12 mm (cap) | 19.0 m, 90 eyes |
| Reef (`--mean-size 38`) | 292 (194 corals, 98 dendrites) | 0 | 2.62 mm | 1.31 mm | 9.09 mm | 19.0 m |

Screens are `audit_field.py`'s, run on the re-read cut file: kerf 0.10–0.25 mm, 2.5 and 3 mm necks, thin tips over 3 mm, holes, size. Every board has zero coverage error, overlap and duplicate cut segments. Corals and antlers keep 73% of their outline within 2 mm of the cut, trilobites about 51-58%: their notches are narrower, so more of their drawing is engraved rather than cut. In a frame or tray the boards lock firmly. Without one, edge pieces can slide outward at under 1 mm, the same limitation as the large Aphex boards (`LARGE_BOARDS.md`).

The first trilobite board (v1, before notch sizing and engraving) kept only 60% of its outlines within 2 mm and read as generic blobs. That board and the v2 boards are kept under `runs/fossil-boards` for comparison.

The drawings, network weights and boards are private and stored on Drive (`jigsaw-shadow-transfer/fossilnet`), not in Git. Nothing has been cut or assembled in wood yet.
