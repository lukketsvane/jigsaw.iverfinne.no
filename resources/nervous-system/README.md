# Nervous System puzzle engine research

[Can every piece be an animal?](piece-ai/index.html) follows a lizard’s foreleg into its neighbor’s recess, diagrams the published neural partitioning method and our current generators, and tests the design against the dimensions of a 216-piece wooden puzzle. [Illustrated research PDF](piece-ai/neural-jigsaw-pieces.pdf) · [Benchmark data](piece-ai/evidence.csv)

[Open the jigsaw feature inventory and run the local generators](feature-inventory.md)

[Download the physical trial kit](wood-trial/README.md) for the six cut families and the two matched 183-piece boards.

[Open the illustrated architecture guide](architecture.html) · [Read the LaTeX research companion](architecture-guide.pdf) · [Explore the jigsaw experiments and figures](visual-atlas/README.md)

[Essential figures on the Figma Home page](https://www.figma.com/design/w0sThYnGJQo1cPrYx7GQR8/sound-in-pieces?node-id=64-2) · [LaTeX manuscript](architecture-assets/latex/main.tex)

The [local technical guide](../../local/technical/README.md) collects the runnable model and geometry examples, the photo-mask replay and the manuscript build command.

Research snapshot from November 26, 2026. The studio publishes the mathematics behind its original Dendrite solver and describes how it makes each of the six cuts in its custom puzzle maker. A search of its puzzle project, software catalog and the author's public repositories found no release of the complete production engine. The studio's app terms prohibit downloading or reverse engineering the hosted app. [Puzzle project](https://n-e-r-v-o-u-s.com/works/puzzles/) · [Software catalog](https://n-e-r-v-o-u-s.com/projects/software/) · [Author's repositories](https://github.com/nervoussystem?tab=repositories) · [App terms](https://n-e-r-v-o-u-s.com/tos.php)

## Source code and production files added November 28

| Stage | What is available locally | Evidence |
| --- | --- | --- |
| Photo to suggested outline | The complete [rembg source snapshot, two pretrained models and local replay](upstream/rembg/SOURCE.md). Both models ran on the upstream sample photograph. The full model reproduced its foreground with 0.99916 intersection over union. | Nervous System named this library when it introduced Magic Shape. [Studio announcement](https://n-e-r-v-o-u-s.com/blog/?p=8656) |
| Whimsy placement | The [complete Deepnest source](upstream/Deepnest/deepnest-2fb10513a306-full.zip) and [nesting algorithm manifest](upstream/Deepnest/source.json) include polygon geometry and placement search. | The studio packed the fixed silhouettes for *Herding Cats* with Deepnest, then adjusted their placement by hand before growing the pieces in the remaining space. This establishes its use for that puzzle. [Studio account](https://n-e-r-v-o-u-s.com/blog/?p=8637) |
| Crystal growth | A [FiPy example and its original numerical fixture](upstream/FiPy/SOURCE.md) reproduce the underlying single-crystal model. All 1,600 grid cells passed the upstream comparison with Kobayashi's Fortran output after ten steps. | FiPy is an independent numerical reference. The [studio paper](papers/nervousSystem_2012-jigsaw-puzzle-paper.pdf) describes the additional per-piece thermal fields in its puzzle solver. |
| Fabrication | The studio's original [air-assist nozzle STL, license and verification record](fabrication/air-assist/SOURCE.md). | Nervous System published the design used to improve its wooden puzzle cuts under CC BY-NC-SA 4.0. [Studio release](https://hackaday.io/project/176034-nervous-system-laminar-flow-laser-air-assist) |

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

The [local 2012 paper](papers/nervousSystem_2012-jigsaw-puzzle-paper.pdf) by Jesse Louis-Rosenberg, Alec Resnick and Jessica Rosenkrantz is the closest source here to an implementation specification. The copy matches the publisher's download. [Publisher PDF](https://n-e-r-v-o-u-s.com/education/papers/nervousSystem_2012-jigsaw-puzzle-paper.pdf)

1. **Place the pieces.** Section 4 gives the seed-spacing rule and a reaction–diffusion initialization in Equation 11. The seed regions expand until they fill the domain. Fixed figure pieces act as reflecting boundaries. Figure 5 shows the two scales of sinusoidal perturbation applied before growth.
2. **Grow the shared boundaries.** Section 3 gives the grid update, temperature coupling and pairwise interaction in Equations 5–10. The paper describes reusing five phase labels across nonadjacent pieces to reduce computation. Section 6 documents a cost of that shortcut. Heat can spread between distant pieces with the same label, softening fine branches in long runs.
3. **Prepare cut lines.** The companion project describes dilating each piece, removing disconnected fragments, tracing contours with modified marching squares and smoothing locations where cuts pass too close together. Numerical production tolerances and the studio's current style presets were unavailable in the materials reviewed. [Cleanup and export](https://n-e-r-v-o-u-s.com/works/puzzles/)

The draft's **CODE** and **WEB** buttons resolve to the same historical project address. That address returned HTTP 404 when checked. The [current Dendrite project](https://n-e-r-v-o-u-s.com/projects/albums/generative-puzzle-system/) supplies the paper and visual demonstrations.

The [May 2012 project page](https://web.archive.org/web/20120528081222/http://n-e-r-v-o-u-s.com:80/projects/puzzles/) and [August 2013 project page](https://web.archive.org/web/20130801052319/http://n-e-r-v-o-u-s.com:80/projects/puzzles/) both linked a paper. Neither capture identified a source archive. The earlier PDF could not be verified. The [captured 2013 PDF](https://web.archive.org/web/20131002140507/http://n-e-r-v-o-u-s.com/projects/puzzles/jigsaw-paper.pdf) differs from the local six-page draft only in document identifiers and timestamps.

Two public repositories from the same author surfaced in the source search. One explores anisotropic cellular patterns. The other takes existing SVG paths and reorders them for cutting. The project descriptions reviewed do not identify either as the production puzzle generator. [Cellular pattern source](https://github.com/nervoussystem/cellularPatterns) · [Laser path reordering source](https://github.com/nervoussystem/laserReordering)

## Libraries credited in 2017

The studio names four libraries in its Maze and custom puzzle announcement. The archive uses versions available by October 24, 2017. The studio did not specify its dependency versions, how it tuned growth, or how it integrated the solver. [Studio credits](https://n-e-r-v-o-u-s.com/blog/?p=7821)

| Library | Role in the studio's credits | Local material |
| --- | --- | --- |
| openFrameworks | C++ creative coding | [0.9.8 source archives and provenance](upstream/openFrameworks/SOURCE.md) |
| Clipper | Polygon Boolean operations | [Complete 6.4.2 release](upstream/Clipper/clipper_ver6.4.2.zip) · [Source and intersection check](upstream/Clipper/source.json) |
| Eigen | Matrix operations | [Complete 3.3.4 release and license notes](upstream/Eigen/SOURCE.md) |
| PositionBasedDynamics | Physics simulation | [Complete project and rod solver source](upstream/PositionBasedDynamics/SOURCE.md) · [2016 Cosserat rods paper](papers/2016-SCA-Cosserat-Rods.pdf) |

The two rembg weight files and the full openFrameworks ZIP are stored locally and ignored by Git. Their source records give the download URLs and checksums needed to restore them on another machine. The compact openFrameworks core ZIP is included for ordinary checkouts.

## Configurator and visual references

- The [image inventory](configurator-assets.json) maps each local image to its original URL and its place in the form. It includes all six pairs of cut icons and photographs, the [custom outline guidelines](images/shapeDiagrams.png), the [animated process diagram](images/diagram2.gif), and a link to the [Vimeo process video](https://player.vimeo.com/video/238139162).
- The [figure index](figures/index.json) covers diagrams from the studio's technical write-ups. Start with the [six-cut comparison](figures/nervousSystem_puzzleCuts_2025-825x779.png), the [Wave seed-to-cut comparison](figures/waveDiagram-825x322.jpg) and the [Matrix grid sequence](figures/matrix4-825x279.png).
- The [browser file inventory](configurator-includes.json) lists the script and stylesheet URLs declared by the public landing page. The form accepts artwork, sets the size and outline, selects a cut, places whimsies and prepares an order. Those declarations alone do not establish where the production cut computation runs. [Live configurator](https://n-e-r-v-o-u-s.com/customPuzzle/)

## PDF checksums

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| [2012 puzzle paper](papers/nervousSystem_2012-jigsaw-puzzle-paper.pdf) | 847940 | `b6a0708d1ce7e92027e5507f1f9a7a6dc4aa412b45a496f6ce023a4aa26e4543` |
| [2016 rod paper](papers/2016-SCA-Cosserat-Rods.pdf) | 1883147 | `4ac603bda3fbce7f51f5a4a4ba20f7f98d64bd6f1574eadfbb0ffb8f4b6fdce4` |
| [U²-Net paper, August 2020 version](papers/U2-Net-2005.09007v2.pdf) · [Authors' preprint](https://arxiv.org/abs/2005.09007v2) | 4689709 | `2243aa27574a8210918d28d9b95439dab8494720e1db3ecac7715f8fc58074c6` |
