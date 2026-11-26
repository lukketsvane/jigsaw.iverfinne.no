# Shadow PC migration

The source-only repository excludes generated puzzles, source raster caches, neural checkpoints, downloaded resource archives and virtual environments. Keep those in private storage. Do not publish the source imagery or Aphex-derived assets without clearance.

## Windows setup

From PowerShell on the new machine, clone the repository and use a fresh Python 3.11 environment:

```powershell
git clone https://github.com/lukketsvane/jigsaw.iverfinne.no.git
Set-Location jigsaw.iverfinne.no
py -3.11 -m venv .venv
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
```

Install a PyTorch CUDA wheel compatible with the actual NVIDIA GPU and driver using the instructions at https://pytorch.org/get-started/locally/. Then run:

```powershell
& $py -m pip install -r requirements.txt
nvidia-smi
& $py -c "import torch; assert torch.cuda.is_available(); print(torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0), round(torch.cuda.get_device_properties(0).total_memory/2**30,2))"
& $py -m pytest -q tests hellerisninger/pipeline/test_pipeline.py
```

The source-machine stack was Python 3.11, PyTorch 2.1.0+cu121 and Shapely 2.1.2. Do not assume the old CUDA wheel supports the new GPU. CairoSVG requires a native Cairo installation if the corpus rasterization pipeline is used.

## Private data snapshot

Extract the CYGNUS transfer ZIP into the repository root, merging `runs/` and `hellerisninger/processed/`. The archive preserves original catalog bytes, source masks, provenance, trained weights, optimizer/RNG state and selected verified exports. Catalogs recorded under the original Windows repository can resolve their suffix relative to the cloned repository. No need to rewrite catalogs: doing so changes the SHA-256 recorded in the checkpoint.

Run `python scripts/transfer.py verify --root .` after extraction. Keep the source computer and archive until all hashes and the GPU smoke test pass. The manifest lists snapshot members, their sizes and SHA-256 values. Experimental scripts are retained in the private archive, not part of the public CLI contract. Jobs running at snapshot time are not live-transferred.

## Repeatable composition

```powershell
& $py -m heller_jigsaw compose --catalog runs/scale32_targets/aphex/catalog.json --seed 103 --mode 3 --out runs/shadow-layout
& $py -m heller_jigsaw lock --run-dir runs/shadow-layout --method standard --out runs/shadow-locked
& $py -m heller_jigsaw audit-fabrication --run-dir runs/shadow-locked --rigid
```

The audit returns exit code 2 when its digital screening criteria fail. Use a different output folder for each trial. The selected catalog must contain 2–32 ordered records, each with a loadable silhouette. For an experimental modified-target catalog, provide its original catalog with `--reference-catalog` for ranking against the unchanged source silhouettes.

## Training

A higher-resolution new run, rather than changing the configuration of an existing checkpoint:

```powershell
& $py -m heller_jigsaw train-shapes --corpus runs/animal-cc0-only/catalog.json --epochs 300 --img-size 192 --latent-dim 64 --batch-size 32 --balanced --seed 501 --out runs/shadow-192-seed501
```

The 192px configuration is an unbenchmarked starting point; check actual peak VRAM and validation metrics before expanding. More VRAM does not guarantee better shape fidelity. Compare at a common raster resolution, use source-grouped splits, and choose by validation rather than test score. A resume must use the checkpoint's original configuration and catalog hash (the epoch limit can be extended). For example, the saved 96px run can be continued with its original parameters:

```powershell
& $py -m heller_jigsaw train-shapes --corpus runs/animal-cc0-only/catalog.json --epochs 600 --img-size 96 --latent-dim 48 --batch-size 16 --balanced --seed 101 --out runs/v10-campaign/cc0-96-seed101 --resume
```

The CPU-based free-form layout search and Shapely validation do not automatically benefit from additional VRAM. Benchmark separate CPU workers and GPU training independently.

To train all three editions together on the RTX A4500, point the launcher at completed Animal and Helleristning run directories containing `metrics.json` and `training-last.pt`.

```powershell
& $py scripts/train_editions.py --animal-from $animalRun --helleristning-from $hellerRun --out runs/editions-next
```

The launcher copies both checkpoints, continues each VAE for 24 epochs, and retrains Aphex PathNet for 80 epochs at batch size 256. It caps each worker at two CPU threads and starts Aphex four seconds after the VAEs to overlap training. The VAE workers save their first and final epochs and wait at least ten seconds between intermediate saves. Set `--checkpoint-seconds 0` to save every epoch.

## Release limits

Use `cut.svg` (not `pieces.svg`) for common-line cutting at 1:1 millimetre scale. The geometry audit, sampled motion screens and thin-feature analysis do not establish material strength, physical fit, complete rigid-body locking, or legal permission to sell. Physical cutting/assembly tests and source-rights review remain required.
