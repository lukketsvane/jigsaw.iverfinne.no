# Baseline Source Audit & Regional Gap Report

- **Audited baseline records:** 931
- **SHA-256 verified OK:** 931 / 931 (`0` failures, `0` missing, `0` unreadable)
- **Missing licence/creator/URL:** 0
- **Quarantined `painted_photo` records (NEVER trace modern paint):** 104
- **Baseline traceable non-painted records (`bw_tracing`, `svg_vector`, `rubbing_frottage`, `rti_3d`):** 645

## Core & Neighbouring Region Coverage (Baseline)

| Region | Total | Traceable | Painted (Quarantined) | Photo Other | Gap Severity |
|---|---:|---:|---:|---:|---|
| Alta (Finnmark, NO) | 170 | 157 | 5 | 8 | MODERATE_OR_GOOD |
| Bohuslän / Tanum (SE) | 163 | 147 | 9 | 7 | MODERATE_OR_GOOD |
| Nämforsen (Ångermanland, SE) | 7 | 5 | 2 | 0 | CRITICAL |
| Other (DK) | 26 | 20 | 2 | 4 | HIGH |
| Other (FI) | 4 | 0 | 2 | 2 | CRITICAL |
| Other (NO) | 139 | 48 | 31 | 60 | HIGH |
| Other (SE) | 87 | 52 | 13 | 22 | MODERATE_OR_GOOD |
| Trøndelag (NO) | 141 | 56 | 15 | 70 | MODERATE_OR_GOOD |
| Vingen / Vestland (NO) | 122 | 121 | 0 | 1 | MODERATE_OR_GOOD |
| Østfold (NO) | 72 | 39 | 25 | 8 | HIGH |

## Key Identified Gaps

- Nämforsen (Ångermanland, SE) has only 7 baseline raw files (5 traceable: 1 bw_tracing, 4 rubbing_frottage; 2 painted_photo quarantined) — CRITICAL gap.
- Østfold (NO) has 72 baseline files, but 25 are painted_photo (quarantined) and 8 photo_other, leaving only 39 traceable records (34 bw_tracing, 5 rubbing_frottage) despite 200+ KHM CC BY-SA 4.0 tracings existing in Unimus — HIGH gap.
- Zero rti_3d records in baseline despite 884 CC BY 4.0 SHFA 3d-visualisering relief maps available (including 19 in Nämforsen and hundreds in Bohuslän/Tanum).
- Alta (Finnmark, NO) has 150 commercial_ok=false tracings + 7 SHFA rubbings, while SHFA holds 41 CC BY 4.0 Alta rubbings and Alta Museum holds ~115 additional 5009-Kalkeringer tracings.
