# Jigsaw developer reference

Start with **PathNet** for the trained network that draws a piece, or **Formverk geometry** to follow one board all the way back from its exported SVG. A piece outline, a photo mask, and a partition of a whole board have different data contracts.

| Question | Executable material | Read next |
| --- | --- | --- |
| How does a neural network draw a closed piece? | [PathNet CPU probe](probe_model.py) using the [tracked v1 weights](../../pathnet/pathnet.pt) | [Network, tensor shapes, training and inference](neural-model.md) |
| How do eight cut styles produce auditable parts? | [Generate and inspect a Wave board](probe_geometry.py) | [Four solver families, their inputs and the SVG audit](geometry.md#cut-styles-and-solver-families) |
| What do the eight styles actually cut on the same rectangle? | [Repeat the comparison](compare_cuts.py) | [Eight-board plate, settings and measurements](geometry.md#compare-all-eight-exports) |
| How does a photo become a foreground mask? | [Archived U²-Net replay](../../resources/nervous-system/upstream/rembg/replay.py) | [Preprocessing, model weights and measured agreement](photo-mask.md) |
| What chooses the phase-field settings? | [CPU inference snippet](parameter-guide.md#cpu-inference) | [Surrogate inputs, predictions and checkpoint](parameter-guide.md) |
| Which comparisons should guide the next change? | [Read the paired board measurements in Python](development.md#1-check-whether-a-better-outline-makes-a-better-board) | [Development experiments and measurement criteria](development.md) |

## Run the two local probes

From the repository root in PowerShell, use the project’s Python 3.11 environment. Both examples run on CPU. The geometry probe creates a fresh run directory and prints its path. It accepts `--out-parent` to put the generated SVG, previews, player data and audit under a directory of your choice.

```powershell
.\.venv\Scripts\python.exe local/technical/probe_model.py
.\.venv\Scripts\python.exe local/technical/probe_geometry.py --out-parent runs/technical
```

On another checkout, install the [root requirements](../../requirements.txt) in a Python 3.11 environment and use that interpreter for the same scripts. The photo replay has its own dependencies and model downloads. Its instructions are in the [mask note](photo-mask.md).

## Where the work lives

| Location | Contents |
| --- | --- |
| [`pathnet/`](../../pathnet/README.md) | Whole-piece training and inference, shape metrics and board experiments. The v1 checkpoint ships with the repository. |
| [`dendrite_engine/`](../../dendrite_engine) | Formverk’s [style registry](../../dendrite_engine/styles.py) and [solver dispatcher](../../dendrite_engine/solvers.py) route cuts to phase fields, elastic rods, circular arcs or quadrilateral cell exchange. All four use the same SVG writer and readback audit. |
| [`resources/nervous-system/`](../../resources/nervous-system/README.md) | Publications, attributed studio figures, archived upstream software, checksums, the [interactive guide](../../resources/nervous-system/architecture.html) and [PDF companion](../../resources/nervous-system/architecture-guide.pdf). |
| `runs/` | Local experiments, optional checkpoints and generated outputs. The root [ignore rules](../../.gitignore) keep new runs out of the source tree. |

The [LaTeX manuscript](../../resources/nervous-system/architecture-assets/latex/main.tex) uses the same vector drawings as the interactive guide. With Inkscape and Tectonic installed, this command rebuilds the PDF from the repository root.

```powershell
.\.venv\Scripts\python.exe resources/nervous-system/architecture-assets/latex/build.py
```
