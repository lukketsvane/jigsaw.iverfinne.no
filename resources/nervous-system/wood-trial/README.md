# Formverk wood trial

[Download the complete shop kit](formverk-wood-trial.zip). Extract the ZIP and open `index.html`. The worksheet, four main cut jobs, six individual style recuts, locator maps, geometry audits and observer notes all work offline. The worksheet can be printed or saved and reopened as a JSON record.

| Job | Size | What to measure |
|---|---|---|
| Three square plugs in one sheet | 100 × 76 mm page | Six kerf estimates from the matched plugs and openings, across X and Y |
| Six styles on one sheet | 297 × 160 mm | Release damage and fit on Dendrite, Amoeba, Maze, Wave, Coral and Matrix |
| Board A | 368 × 368 mm, 183 pieces | Piece damage, pair fit and wear, whole-board movement and solve time |
| Board B | 368 × 368 mm, 183 pieces | Repeat the same procedure on the second matched design |

The board trial compares two local PathNet designs. Only digital measurements are available at packaging. The two Aphex-motif boards are for personal, non-commercial research. The records have room for a separate manufactured comparison when a physical studio puzzle is available. [Observer's study notes](observer.md) · [Technical figures and measured graphs](../visual-atlas/README.md)

## Rebuild locally

From the repository root, use the project Python environment and the two saved research runs under `runs/aphex-logo-398-v2/`. Their names and the expected cut hashes are recorded in the [board builder](../../../scripts/formverk_trial_boards.py).

```powershell
.\.venv\Scripts\python.exe scripts/formverk_wood_trial.py
```

The builder writes the unpacked files to `runs/formverk-wood-trial/` and verifies the archived bytes against the deliverables. The distributed ZIP includes all four cut jobs for use on a computer without the saved runs.
