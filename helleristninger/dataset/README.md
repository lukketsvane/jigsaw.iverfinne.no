---
pretty_name: Nordic Helleristninger — Open Archaeological Rock Art Dataset
license: other
license_name: mixed-open-nordic-rock-art
license_link: https://creativecommons.org/licenses/by-sa/4.0/
language:
  - en
  - no
  - sv
  - da
  - fi
task_categories:
  - image-segmentation
  - image-classification
  - object-detection
  - unconditional-image-generation
tags:
  - archaeology
  - rock-art
  - helleristninger
  - hallristningar
  - nordic-heritage
  - svg
  - vector-graphics
  - geospatial
  - geojson
  - cultural-heritage
size_categories:
  - 1K<n<10K
configs:
  - config_name: figures
    default: true
    data_files:
      - split: train
        path: figures.parquet
      - split: commercial_safe
        path: splits/figures_commercial_safe.parquet
      - split: research_only
        path: splits/figures_research_only.parquet
  - config_name: sites
    data_files:
      - split: train
        path: sites.parquet
  - config_name: sources
    data_files:
      - split: train
        path: sources.parquet
  - config_name: dual_trace_qa
    data_files:
      - split: train
        path: dual_trace_qa.parquet
---

# Nordic Helleristninger — Open Archaeological Rock Art Dataset (`v2026.W48`)

Production-grade open scientific dataset of Nordic prehistoric rock carvings (*helleristninger* / *hällristningar*) spanning Norway, Sweden, Denmark, and Finland (`9000 BCE – 500 CE`), combining:

1. **Multi-layer true-scale SVG figure tracings** (`figures`, **7,934** traced figures; `#figures`, `#cup_marks`, `#cracks`, `#uncertain`, `#scale_bar`).
2. **Consolidated geolocated site & panel register** (`sites`, **26,879** sites/panels across Askeladden/Kulturminnesøk, RAÄ/Fornsök/SHFA, Nationalmuseet, and Finna/Europeana).
3. **SHA-256 verified archival manifest index** (`sources`, **2,823** archival records with strict `painted_photo` quarantine).
4. **Independent >=5% stratified dual-trace QA** (`dual_trace_qa`, **397** evaluated figures comparing Sauvola/local-gradient vs Otsu/morphological marching-squares).

> **Scientific Integrity Policy:** Zero modern painted photos (`painted_photo`) are traced. Uncalibrated archival plates keep `scale_calibrated: false` (`width_mm=null`). Unverified coordinates remain `null` with explicit provenance. Radiocarbon `14C` is never fabricated.

## Dataset Summary (`2026-11-28T11:10:14Z`)

| Metric | Value |
|---|---|
| Traced Figures (`figures.parquet`) | **7,934** (`6,261` commercial-safe / `1,673` research-only) |
| Geolocated Figures (`EPSG:4326`) | **7,312** (92.16%) |
| Numerically Dated Figures | **7,918** (99.8%) |
| Consolidated Sites & Panels (`sites.parquet`) | **26,879** (97.06% geolocated, 98.1% dated) |
| Archival Source Index (`sources.parquet`) | **2,823** (373 `painted_photo` quarantined) |
| Dual-Trace QA Subset (`dual_trace_qa.parquet`) | **397** (mean IoU `0.6039`, median IoU `0.6561`) |

## Splits & Licensing

- `commercial_safe` (`splits/figures_commercial_safe.parquet`, **6,261** figures): `CC0 1.0`, `Public Domain`, `CC BY`, `CC BY-SA` (`commercial_ok == true`).
- `research_only` (`splits/figures_research_only.parquet`, **1,673** figures): Open non-commercial research collections (e.g., Alta Museum `CC BY-NC-SA`-equivalent, `commercial_ok == false`).

## Loading with Hugging Face `datasets` & PyTorch

```python
from datasets import load_dataset
import torch

# Load all traced figures (or split="commercial_safe" / "research_only")
ds_figures = load_dataset("helleristninger/dataset", name="figures", split="commercial_safe")
ds_sites = load_dataset("helleristninger/dataset", name="sites", split="train")
ds_qa = load_dataset("helleristninger/dataset", name="dual_trace_qa", split="train")

# Filter Bronze Age ships in Sweden with verified coordinates
ships_se = ds_figures.filter(
    lambda x: x["country"] == "SE"
    and x["motif_class"] == "bronze_age_ship"
    and x["lat"] is not None
)

# PyTorch DataLoader integration over shape morphometrics + SVG path tokens
torch_ds = ships_se.with_format(
    "torch",
    columns=["solidity", "compactness", "aspect_ratio", "date_midpoint_year", "cup_marks_count"],
)
loader = torch.utils.data.DataLoader(torch_ds, batch_size=32, shuffle=True)
```

## Config Schemas

- **`figures`**: `figure_id`, `site_panel_id`, `site`, `region`, `core_region`, `country`, `lat`, `lon`, `geo_precision_m`, `geo_level`, `date_from_year`, `date_to_year`, `date_midpoint_year`, `dating_method`, `period`, `period_label`, `dating_citation`, `motif_family`, `motif_class`, `svg_path`, `svg_figures_d`, `figures_path_closed`, `cup_marks_count`, `cracks_count`, `uncertain_count`, `scale_calibrated`, `width_mm`, `height_mm`, `area_mm2`, `licence`, `commercial_ok`, `confidence`, `dual_trace_iou`, `dual_trace_qa_status`, `literature_refs`.
- **`sites`**: `site_uid`, `site_panel_id`, `askeladden_id`, `raa_id`, `lamning_id`, `shfa_site_id`, `site_name`, `parish_or_kommune`, `region_or_fylke`, `country`, `lat`, `lon`, `geo_precision_m`, `geo_level`, `date_from_year`, `date_to_year`, `date_midpoint_year`, `dating_method`, `period`, `period_label`, `dating_citation`, `traced_figure_count`, `archives`, `source_url`.
- **`sources`**: `source_id`, `source_folder`, `source_archive`, `media_type`, `quarantined`, `sha256`, `raw_path`, `source_url`, `country`, `rights_holder`, `licence`, `commercial_ok`.
- **`dual_trace_qa`**: `figure_id`, `core_region`, `source_archive`, `media_type`, `motif_family`, `polygon_iou`, `hausdorff_px`, `mean_boundary_dist_px`, `area_delta_pct`, `topology_agreement`, `severity`, `cause_category`, `overlay_path`.
