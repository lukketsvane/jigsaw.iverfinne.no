# Formverk’s phase-field parameter guide

`DendriteSurrogateNet` predicts four audit metrics from a proposed phase-field configuration. During tuning, Adam changes the input parameters of a frozen model. The generator then simulates the recommended parameters and audits the exported cut. This route applies to Dendrite, Amoeba, Linear and Grid. [Model and optimizer](../../dendrite_engine/neural_guide.py) · [Solver dispatch](../../dendrite_engine/solvers.py)

## Tensor contract

| Tensor | Layout in the implementation |
| --- | --- |
| Input `float32[B,14]` | Two context values, normalized effective piece diameter and board aspect ratio. Four one-hot style entries in order `dendrite, amoeba, linear, grid`. Eight scaled inputs in order `gamma, eps, T0, lambda1_ratio, lambda2_ratio, U1_ratio, U2_ratio, steps`. |
| Hidden state `[B,128]` | Linear projection, LayerNorm and SiLU, followed by two residual blocks. Each block has two width-128 linear layers, LayerNorm, SiLU and training-time dropout. The local checkpoint has 69,764 trainable parameters. |
| Output `[B,4]` | Lock margin in mm via softplus, neck and kerf pass fractions via sigmoid, then seam tortuosity via one plus softplus. Tortuosity is arc length divided by chord length. The training target is clipped at 5. The prediction head has no upper clamp. |

At inference, effective diameter is `sqrt(4 × width_mm × height_mm / (π × max(2, piece_count)))`. Tuned wavelengths and amplitudes are converted from diameter ratios to millimetres. The objective rewards predicted lock margin and tortuosity, with a capped tortuosity reward and quadratic penalties below 0.985 predicted neck pass rate or 0.990 predicted kerf pass rate. The penalties influence selection. A recommendation still needs its own SVG audit. [Encoding, bounds, loss and heads](../../dendrite_engine/neural_guide.py)

## CPU inference

Run from the repository root in the project Python environment. The call only selects settings. It does not write files. The returned flag identifies whether the local checkpoint or the preset supplied them.

```python
from dendrite_engine.neural_guide import DEFAULT_CHECKPOINT, auto_tune_parameters

result = auto_tune_parameters(
    width_mm=150, height_mm=120, num_pieces=12,
    style="dendrite", seed=42, checkpoint=DEFAULT_CHECKPOINT, device="cpu",
)
print(result["used_neural_guide"], result["recommended"]["steps"], result["predicted"])
```

On this checkout the checkpoint produced **349 steps**, a predicted lock margin of **1.855 mm**, predicted neck and kerf pass rates of **0.9916** and **0.9963**, and predicted mean tortuosity of **1.832**. With the same request and an absent checkpoint the function returned **360 steps**, `False` and `None` for the prediction.

The checkpoint is local at `runs/dendrite-engine/neural_guide.pt`, SHA-256 `4465a7e3b342cdc1ba33cdeb890d723286d5685d0f4e7d7cb6a637e0ff46d186`. Its sibling `neural_guide_report.json` records 100 simulated patches, an 80/20 split and 280 epochs. The training target for lock margin used 180 sampled directions. The final exported SVG audit uses 360. The report hash is `f63e95e830ac46344ec443c373963b4dad900f572c41bbecbf82c9fe499ebc64`. [Training implementation](../../dendrite_engine/neural_guide.py) · [Export audit](../../dendrite_engine/postprocess.py)

## Interpreting the validation report

The archived report gives validation mean absolute errors of 0.452 mm for lock margin, 0.0037 for neck pass fraction and 0.0028 for kerf pass fraction. It shows a mean observed fraction of 1.0 and an R² of 1.0 for each rate. That R² was the historical fallback when the observations had negligible spread, so it cannot establish performance on failing pieces.

New reports retain the error metrics and write JSON `null` to `per_target.<target>.val_r2` when the sum of squared deviations from the validation mean is at most `1e-8`. For each pass-rate target, `val_all_pass_patches` counts validation patches where every piece passed, and `val_patches_with_failures` counts patches containing at least one failed piece. Together they sum to the top-level `val_samples`. Older reports omit both counts. [Metric calculation](../../dendrite_engine/neural_guide.py)

To train another guide, run `python -m dendrite_engine train-guide --samples 100 --epochs 280 --seed 42 --out runs/my-guide-trial` from the root with a fresh output directory. Compare the resulting predictions against measured values on additional boards before using them for style selection. [Development experiment](development.md#2-calibrate-the-dendrite-parameter-guide-on-failure-cases)
