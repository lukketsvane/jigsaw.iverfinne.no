# Nervous System puzzle engine research

Research snapshot from November 26, 2026. The studio publishes the mathematics behind its original Dendrite solver and describes how it makes each of the six cuts in its custom puzzle maker. A search of its puzzle project, software catalog and the author's public repositories found no release of the complete production engine. The studio's app terms prohibit downloading or reverse engineering the hosted app. [Puzzle project](https://n-e-r-v-o-u-s.com/works/puzzles/) · [Software catalog](https://n-e-r-v-o-u-s.com/projects/software/) · [Author's repositories](https://github.com/nervoussystem?tab=repositories) · [App terms](https://n-e-r-v-o-u-s.com/tos.php)

## How the cuts are made

| Cut | How the studio constructs a shared edge | Local example | Studio source |
| --- | --- | --- | --- |
| Dendrite | A crystal-growth simulation evolves the boundary between neighboring cells. Small initial ripples develop into interlocking branches. | [Photo](images/style_dendrite_photo.jpg) | [Original project](https://n-e-r-v-o-u-s.com/works/puzzles/) |
| Amoeba | Changing parameters in the Dendrite simulation produces fine, closely spaced projections. | [Photo](images/style_amoeba_photo.jpg) | [Cut guide](https://n-e-r-v-o-u-s.com/blog/?p=9738) |
| Maze | The initial cells fill the space around fixed figures. Their edges become elastic rods that bend and push against each other as they lengthen. | [Photo](images/style_maze_photo.jpg) | [Maze development](https://n-e-r-v-o-u-s.com/blog/?p=7821) |
| Wave | A directional Voronoi layout elongates the starting pieces. Vortices travel along the growing rods and curl their edges into spirals. | [Photo](images/style_wave_photo.jpg) | [Wave development](https://n-e-r-v-o-u-s.com/blog/?p=9225) |
| Coral | The generator builds candidate edges from connected circular arcs. It scores their length and interlock, avoids crossings, and accounts for the thickness of the resulting pieces. | [Photo](images/style_coral_photo.jpg) | [Cut guide](https://n-e-r-v-o-u-s.com/blog/?p=9738) · [Coral development](https://n-e-r-v-o-u-s.com/blog/?p=9636) |
| Matrix | The outline and fixed figures orient a grid of mostly quadrilateral cells. Neighboring pieces trade cells. The optimizer measures interlock by how much of a circle the edge's tangent directions cover. | [Photo](images/style_matrix_photo.jpg) | [Matrix development](https://n-e-r-v-o-u-s.com/blog/?p=9465) |

## Reading the Dendrite paper

The [local 2012 paper](papers/nervousSystem_2012-jigsaw-puzzle-paper.pdf) by Jesse Louis-Rosenberg, Alec Resnick and Jessica Rosenkrantz is the closest source here to an implementation specification. The copy matches both the publisher's download and the PDF already present at the repository root. [Publisher PDF](https://n-e-r-v-o-u-s.com/education/papers/nervousSystem_2012-jigsaw-puzzle-paper.pdf)

1. **Place the pieces.** Section 4 gives the seed-spacing rule and a reaction–diffusion initialization in Equation 11. The seed regions expand until they fill the domain. Fixed figure pieces act as reflecting boundaries. Figure 5 shows the two scales of sinusoidal perturbation applied before growth.
2. **Grow the shared boundaries.** Section 3 gives the grid update, temperature coupling and pairwise interaction in Equations 5–10. The paper describes reusing five phase labels across nonadjacent pieces to reduce computation. Section 6 documents a cost of that shortcut. Heat can spread between distant pieces with the same label, softening fine branches in long runs.
3. **Prepare cut lines.** The companion project describes dilating each piece, removing disconnected fragments, tracing contours with modified marching squares and smoothing locations where cuts pass too close together. Numerical production tolerances and the studio's current style presets were unavailable in the materials reviewed. [Cleanup and export](https://n-e-r-v-o-u-s.com/works/puzzles/)

The draft's **CODE** and **WEB** buttons resolve to the same historical project address. That address returned HTTP 404 when checked. The [current Dendrite project](https://n-e-r-v-o-u-s.com/projects/albums/generative-puzzle-system/) supplies the paper and visual demonstrations.

Two public repositories from the same author surfaced in the source search. One explores anisotropic cellular patterns. The other takes existing SVG paths and reorders them for cutting. The project descriptions reviewed do not identify either as the production puzzle generator. [Cellular pattern source](https://github.com/nervoussystem/cellularPatterns) · [Laser path reordering source](https://github.com/nervoussystem/laserReordering)

## Rod solver reference

The 2017 Maze article credits openFrameworks, Clipper, Eigen and PositionBasedDynamics. For the rod mechanics, this folder includes the [2016 Cosserat rods paper](papers/2016-SCA-Cosserat-Rods.pdf) and [MIT-licensed C++ constraints and demo](upstream/PositionBasedDynamics/SOURCE.md). The snapshot uses the last upstream revision before the article appeared. The studio does not specify which revision it used, how it tuned growth, or how it integrated the solver. [Studio credits](https://n-e-r-v-o-u-s.com/blog/?p=7821) · [Rod paper and supplemental video](https://diglib.eg.org/items/eb5dd4ac-529c-4d6b-86ab-454cec714272)

## Configurator and visual references

- The [image inventory](configurator-assets.json) maps each local image to its original URL and its place in the form. It includes all six pairs of cut icons and photographs, the [custom outline guidelines](images/shapeDiagrams.png), the [animated process diagram](images/diagram2.gif), and a link to the [Vimeo process video](https://player.vimeo.com/video/238139162).
- The [figure index](figures/index.json) covers diagrams from the studio's technical write-ups. Start with the [six-cut comparison](figures/nervousSystem_puzzleCuts_2025-825x779.png), the [Wave seed-to-cut comparison](figures/waveDiagram-825x322.jpg) and the [Matrix grid sequence](figures/matrix4-825x279.png).
- The [browser file inventory](configurator-includes.json) lists the script and stylesheet URLs declared by the public landing page. The form accepts artwork, sets the size and outline, selects a cut, places whimsies and prepares an order. Those declarations alone do not establish where the production cut computation runs. [Live configurator](https://n-e-r-v-o-u-s.com/customPuzzle/)

## PDF checksums

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| [2012 puzzle paper](papers/nervousSystem_2012-jigsaw-puzzle-paper.pdf) | 847940 | `b6a0708d1ce7e92027e5507f1f9a7a6dc4aa412b45a496f6ce023a4aa26e4543` |
| [2016 rod paper](papers/2016-SCA-Cosserat-Rods.pdf) | 1883147 | `4ac603bda3fbce7f51f5a4a4ba20f7f98d64bd6f1574eadfbb0ffb8f4b6fdce4` |
