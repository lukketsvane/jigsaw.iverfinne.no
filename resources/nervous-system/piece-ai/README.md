# Can every piece be an animal?

[Read the eight-page illustrated study](neural-jigsaw-pieces.pdf) or [open the browser edition](index.html). The figures and repository measurements below accompany the study.

| Figure | How to read it |
| --- | --- |
| [Seed 387 specimen](lizard-specimen.svg) | Piece 049 touches piece 003 across one horizontal period. The blue segments come from their actual shared outline. |
| [A foreleg and its recess](seam-detail.svg) | A schematic enlargement shows one stored cut and its two oriented uses. |
| [Published prompt-to-piece solver](whimsy-system.svg) | Read the green arrows through the travel-time partition, then follow the coral semantic feedback back to the field. |
| [How travel cost moves the cut](travel-cost.svg) | A worked slice shows why both cells agree even after their shared boundary moves 15 mm. |
| [Two existing neural generators](current-system.svg) | Compare the task performed by the elastic CNN with the task performed by the fossil contour decoder. |
| [Proposed shared-edge model](proposed-system.svg) | A graph model receives geometry from both incident faces. Reimport and shop outcomes supply different forms of feedback. |
| [Physical scale comparison](scale-test.svg) | Both files recover all 216 faces. The 3 mm erosion test flags every piece in the smaller export. |

## Primary literature

Chew, Tojo, Umetani and Bickel’s *Generative Whimsy Jigsaws* is the closest published method in this review. Figure 4 shows the gradient path from the rendered silhouettes to the travel-cost field. Figure 8 documents failures on repeated runs and Figure 10 shows fabricated examples. Supplement §1 explains how guarantees change on a discrete grid. The authors released a two-stage implementation. [Project](https://cdl.ethz.ch/publications/generative-whimsy-jigsaws/) · [Paper](https://cdl.ethz.ch/wp-content/uploads/2026/10/main-1.pdf) · [Supplement](https://cdl.ethz.ch/wp-content/uploads/2026/10/supplementary.pdf) · [Source](https://github.com/mickey1356/generative_jigsaws)

Kaplan and Salesin optimize a closed outline over a family of isohedral tilings. Aigerman and Groueix couple a periodic tile parametrization to diffusion guidance for shape and texture. Lau and colleagues optimize cuts for image contours, interlock and minimum width. Together these papers provide three useful baselines for resemblance, periodicity and physical integrity. [Escherization](https://doi.org/10.1145/344779.345022) · [Generative Escher Meshes](https://arxiv.org/abs/2309.14564) · [Creating Personalized Jigsaw Puzzles](https://www.epfl.ch/labs/ivrl/research/jigsaw-puzzles/)

The studio describes differently morphed lizards, periodic assembly and color-line cutting in its own design account. Its separate Magic Shape release identifies U²-Net as the basis for suggesting the board’s outer shape. Its Herding Cats account names the human whimsy designer and describes packing fixed figures before growing the surrounding pieces. [Lizard Infinity](https://n-e-r-v-o-u-s.com/blog/?p=8832) · [Magic Shape](https://n-e-r-v-o-u-s.com/blog/?p=8656) · [Herding Cats](https://n-e-r-v-o-u-s.com/blog/?p=8637)

## Evidence from this checkout

The [measurement extract](evidence.csv) records the run, scale, units, denominator where applicable and original report path. The source report for each scale is available locally. The larger dimension is the tiling period. The smaller pair of dimensions is the assembled outer envelope. [Large-repeat audit](../../../runs/lizards-387/quality.json) · [Reference-size audit](../../../runs/lizards-387/reference-size/quality.json) · [64-seed reimport](../../../runs/lizards-387/seed-audit.json)

The archived [matched board comparison](../visual-atlas/06-board-tradeoff.csv) uses seed 22 and equal bay settings for the two rows cited. It compares an earlier decoder and a 15-minute v2-M checkpoint on one brief. The counts measure neighboring pairs passing a digital lock screen. A wrong-mate test requires a different population of candidate joins.

The local [Heller solver](../../../heller_jigsaw/solver/geodesic.py) differentiates a shared travel cost through shortest paths on a grid. The [PathNet board field](../../../pathnet/field.py) starts fronts at placed silhouettes and optimizes a separate cost field for each layout. These are working baselines for the neural experiment. The [existing board study](../../../pathnet/README.md) records a fixed-bay baseline alongside its trained version.

The MIT, BGU and massive archives in the resources root contain indexed image fragments. Reassembly data can help construct distracting wrong-neighbor candidates. Training a cut generator needs additional labels about the intended outline, shared edge, physical scale and the results of fabrication.

The proposed graph network, material-aware training loss and blinded shop trial are designs for a future experiment. The local generator archives supply the existing digital measurements.
