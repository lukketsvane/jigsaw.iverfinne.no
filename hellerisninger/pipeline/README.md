# Nordic Helleristninger Shape Pipeline

Extracts, segments, normalizes, laser-tests, deduplicates, and exports individual Nordic rock-art silhouettes (*helleristninger*) for figurative jigsaw puzzles and shape-prior ML training.

## Running

```powershell
# Run unit tests
c:\Users\Iver\Documents\github\jigsaw.iverfinne.no\.venv\Scripts\python.exe -m pytest hellerisninger/pipeline/test_pipeline.py -v

# Run full batch ingestion over all hellerisninger/sources/*/manifest.jsonl
c:\Users\Iver\Documents\github\jigsaw.iverfinne.no\.venv\Scripts\python.exe -m hellerisninger.pipeline.run_pipeline
```

## Output Conventions (`hellerisninger/processed/`)

- **`svg/<asset_id>.svg`**: Single closed exterior `<path>` normalized to a `100x100 mm` viewBox (max motif span `80.0 mm`, centered at `(50, 50)`), valid & simple CCW polygon with `connected_components == 1` and `hole_count == 0`.
- **`polygons/<asset_id>.json`**: Exact rounded 3-decimal coordinates matching the exported SVG, plus geometry & fabrication QA metrics.
- **`masks/<asset_id>.png`**: `256x256` 8-bit grayscale binary PNG (`255` = white foreground silhouette, `0` = black background), rasterized directly from the validated SVG polygon.
- **`sdf/<asset_id>.npz`**: `256x256` `float32` Euclidean signed distance field (`key="sdf"` in pixel units with 0.5 px sub-pixel boundary offset: strictly negative `<= -0.5` inside foreground, strictly positive `>= +0.5` outside, `|grad sdf| ~ 1.0`; `key="sdf_norm"` = `sdf / 256.0`).
- **Laser Kerf / Neck Gate**: Conservative 60 mm nominal piece with `3.0 mm` minimum load-bearing neck + `0.15 mm` laser kerf (`erosion_radius_norm = 0.02625`). Every `A_ready` and `B_thickened` silhouette passes both Shapely negative-buffer and 256x256 raster erosion as a single connected component.
- **`contact_sheets/`**: High-resolution labeled PNG grids (black silhouettes on white tiles) for visual QA.
- **`figma_sheets/`**: Clean multi-motif Figma-ready SVG asset sheets with named `<g id="...">` groups and toggleable labels.
