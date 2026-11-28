# Nordic Helleristninger — Scientific Archive, Dual-Trace QA & 750-Piece Production Consolidation

Self-contained repository hub for the Nordic rock-art (*helleristninger* / *hällristningar*) archival foundation, multi-layer SVG scientific record, independent dual-trace quality assurance, reproducible statistical analysis, and the 750-piece laser-cut birch wooden jigsaw edition.

> **Status:** Scaffolding initialized for weekly release `v2026.W48`. Final figures, statistics, and release checksums will be populated upon pipeline and verification completion.

## Directory Layout

| Path | Role |
|---|---|
| `sources/` | Verified raw archival documentation (`SHFA`, `Unimus`, `Alta`, `Wikimedia`, `K-samsök`, `Europeana`, `Kulturminnesøk`, etc.) with SHA-256 manifests and `painted_photo` quarantine |
| `metadata/` | Verified `site_gazetteer.json` (Askeladden / RAÄ FMIS IDs, EPSG:4326) and `literature.json` |
| `taxonomy.json` | Hierarchical Nordic rock-art motif taxonomy with period & SVG layer metadata |
| `pipeline_science/` | True-scale multi-layer SVG tracing pipeline, dual-trace QA harness, and unit tests |
| `scientific_record/` | Per-figure multi-layer SVGs (`#figures`, `#cup_marks`, `#cracks`, `#uncertain`, `#scale_bar`), `figures.jsonl`, `figures.csv`, and `schema.json` |
| `qa/` | Independent >=5% stratified dual-trace QA (`dual_trace_report.json`, `dual_trace_report.md`, `dual_trace_disagreements.jsonl`, `overlays/`) |
| `science/` | Reproducible analysis script (`analyze_corpus.py`, `plots.py`), `findings_v2026.W48.md`, `findings_metrics.json`, and `figures/` |
| `puzzle_750/` | Consolidated 750-piece all-figurative jigsaw run (`cut.svg`, `partition.json`, audits, review packet) |
| `commercial_pack/` | Calibration coupon (`1:1 mm` SVG + preview), supplier worksheets, and 6 real high-stress seam sample SVGs |
| `docs/` | `selected-750-catalog.json`, `rights-inventory.{csv,json,md}`, `archive-inquiry-drafts.md`, `packaging-alignment.md`, safety review |
| `jigsaw_corpus/` | Consolidated `256x256` binary masks, SDFs, normalized 100 mm jigsaw SVGs, and `catalog.json` |
| `releases/v2026.W48/` | Frozen weekly release snapshot with `RELEASE_MANIFEST.json` (SHA-256), `CITATION.cff`, `datapackage.json`, `CHANGELOG.md` |

## Reproducing Scientific Findings & Verification

```powershell
# Re-run scientific analysis
.venv\Scripts\python.exe helleristninger/science/analyze_corpus.py

# Re-run consolidation and v2026.W48 release manifest
.venv\Scripts\python.exe helleristninger/consolidate.py --stage all

# Run full test suite across 750-piece puzzle, commercial pack, and both pipelines
.venv\Scripts\python.exe -m pytest tests/test_heller_750.py tests/test_commercial_pack.py hellerisninger/pipeline/test_pipeline.py helleristninger/pipeline_science/test_science_pipeline.py
```

## Legal & Ethical Hygiene

- **No Tracing of Modern Paint:** `painted_photo` records are quarantined in `sources/` and excluded from `scientific_record/`.
- **No Fabricated Scale or Coordinates:** Uncalibrated archival plates retain `scale_calibrated: false`; unverified coordinates remain `null` with explicit reason.
- **Commercial Production Separation:** All 750 jigsaw pieces carry `commercial_ok: true` (`0` Alta `CC BY-NC-SA` pieces, `0` synthetic/VAE shapes, `0` unknown creators). Full commercial sale remains `NOT CLEARED FOR SALE` pending institutional review of `111` Unimus `CC BY-SA 4.0` slots and 20th-century PD-term confirmation.
