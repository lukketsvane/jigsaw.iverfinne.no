# Jigsaw / APHEX PathNet

Whole-piece neural SVG geometry and constrained puzzle composition for laser cutting.

The active implementation is in [`pathnet/`](pathnet/README.md). It predicts closed cubic Bézier shapes, with a dedicated Aphex Twin logo class, then arranges them into a shared cutting network.

## Run

```sh
cd pathnet
python -m pip install -r requirements.txt
python generate.py --seed 22 --count 60 --output ./generated
python puzzle.py --seed 22 --width 300 --height 220 --motifs 24 --logo-share 0.85 --output ./generated
python train.py
```

The supplied typeface is not included. The source logo, glyph-derived dataset, and generated geometry have not been cleared for redistribution or commercial use. Physical laser testing and mechanical interlocking remain unverified.
