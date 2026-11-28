# Independent Dual-Trace QA Report

- **Corpus figures**: 7934
- **Dual-traced sample**: 397 (5.00%, >=5% gate: PASS)
- **Method A**: `sauvola_local_gradient_contour`
- **Method B**: `otsu_multiscale_morph_marching_squares` (independent raw-crop re-trace, zero shared masks)
- **Mean Polygon IoU**: 0.6039 | **Median IoU**: 0.6561 | **Min IoU**: 0.0000
- **Mean Boundary Distance**: 14.020 px | **Mean Hausdorff**: 102.615 px
- **Mean |Area Δ|**: 91.38% | **Topology Agreement**: 71/397 (17.9%)
- **Disagreements Logged**: 385 (see `dual_trace_disagreements.jsonl`)

## Stratified Breakdown by Core Region

| Core Region | Dual-Traced | Mean IoU | Median IoU | Min IoU | Topo Agree | Disagreements |
|---|---:|---:|---:|---:|---:|---:|
| alta | 101 | 0.5929 | 0.6561 | 0.0000 | 15.8% | 99 |
| bohuslan_tanum | 68 | 0.6770 | 0.6967 | 0.2492 | 23.5% | 65 |
| namforsen | 16 | 0.4272 | 0.3978 | 0.1301 | 6.2% | 15 |
| ostfold | 55 | 0.5833 | 0.6119 | 0.0000 | 18.2% | 53 |
| other | 64 | 0.5679 | 0.6156 | 0.0064 | 12.5% | 64 |
| trondelag | 24 | 0.5413 | 0.5606 | 0.2660 | 0.0% | 24 |
| vingen | 69 | 0.6606 | 0.7464 | 0.0000 | 29.0% | 65 |

## Disagreement Breakdown

- Severity counts: `{'major': 195, 'moderate': 169, 'minor': 21}`
- Cause categories: `{'component_split_or_crop_neighbor_fragment': 269, 'internal_hole_threshold_sensitivity': 57, 'stroke_thickness_or_threshold_shift': 30, 'boundary_quantization_or_weathered_edge': 29}`
