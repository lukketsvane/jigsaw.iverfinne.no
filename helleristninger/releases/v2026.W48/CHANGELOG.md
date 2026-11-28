# Changelog — Release v2026.W48

## [v2026.W48] — 2026-11-28

### Added
- **Archival Digitisation & Gazetteer (`helleristninger/sources/`, `metadata/`):**
  - SHA-256 verification across raw archival sources and quarantine of `painted_photo` records prior to scientific tracing.
  - Verified site gazetteer (`site_gazetteer.json`) with Askeladden / RAÄ FMIS IDs and EPSG:4326 coordinates, plus curated peer-reviewed bibliography (`literature.json`).
  - Scientific taxonomy (`taxonomy.json`) with period and SVG layer metadata.
- **True-Scale Multi-Layer SVG Tracing (`helleristninger/scientific_record/`):**
  - Multi-layer SVG outputs (`#figures`, `#cup_marks`, `#cracks`, `#uncertain`, `#scale_bar`) with honest scale calibration (`scale_calibrated: true/false`).
  - Master per-figure records in `figures.jsonl`, `figures.csv`, and `schema.json`.
- **Independent Dual-Trace QA (`helleristninger/qa/`):**
  - Stratified >=5% dual-trace verification using two independent segmentation/contour pipelines with exact polygon IoU, boundary distance, and complete disagreement logging (`dual_trace_disagreements.jsonl`).
- **Reproducible Scientific Findings (`helleristninger/science/`):**
  - Morphometric comparison of Stone Age (*veideristninger*) vs. Bronze Age (*jordbruksristninger*) carvings (Mann-Whitney *U*, Cliff's $\delta$, rank-biserial $r$, 95% bootstrap CIs, Benjamini-Hochberg FDR).
  - Regional iconographic comparison across Alta, Østfold, Bohuslän/Tanum, Nämforsen, Vingen, and Trøndelag (Shannon $H'$, Pielou $J'$, Jaccard & Bray-Curtis matrices).
  - Dual-trace measurement reliability analysis and 4 publication-grade figures.
- **750-Piece Jigsaw & Commercial Pack Consolidation (`helleristninger/puzzle_750/`, `commercial_pack/`, `docs/`, `jigsaw_corpus/`):**
  - Consolidated 750-piece all-figurative wooden jigsaw deliverables, commercial calibration coupon, seam samples, and rights inventory (`639` Non-ShareAlike + `111` Unimus `CC BY-SA 4.0`, `NOT CLEARED FOR SALE`).
