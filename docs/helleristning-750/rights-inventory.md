# Rights & Provenance Inventory — 750-Piece Exclusive Helleristning Wooden Jigsaw

> **EVIDENCE FOR LEGAL REVIEW — NOT CLEARED FOR SALE.**
> This inventory compiles primary archive metadata (`source_url`, `creator`, `license`, `license_url`, `site`, `sha256`) for every single one of the **750 locked puzzle pieces** in [`selected-750-catalog.json`](./selected-750-catalog.json). Recording open-license metadata is **necessary evidence for legal review**, **NOT** legal clearance to manufacture or sell.

---

## 1. Executive Rights Summary (750 / 750 Pieces)

| Metric | Count | Status / Legal Hygiene Note |
|---|---:|---|
| **Total locked pieces** | **750** | 1:1 matched to `selected-750-catalog.json` (`HELL-750-001`..`HELL-750-750`) |
| **Source `commercial_ok = true`** | **750 / 750 (100%)** | **0** Alta Museum NC-SA pieces; **0** synthetic / VAE-generated shapes |
| **Non-ShareAlike (`Public Domain` / `CC0` / `CC BY`)** | **639** (85.2%) | No copyleft ShareAlike trigger; attribution & PD-term checks still required |
| **ShareAlike (`CC BY-SA 4.0` — Unimus only)** | **111** (14.8%) | **OPEN LEGAL GATE:** Triggers ShareAlike review unless cleared or swapped |
| **Missing `source_url` / `license` / `license_url` / `creator`** | **0** | All 6 unknown-creator source records (incl. `helleristning_01460`) cleanly excluded |
| **Current legal clearance status** | **0 cleared** | All 750 rows flagged `evidence_recorded_pending_legal_review` |

---

## 2. Breakdown by License & ShareAlike Obligation

### A. Non-ShareAlike Pool (No Copyleft Trigger)
| License | Count | Primary Archive(s) | Core Legal Obligation |
|---|---:|---|---|
| `CC BY 4.0` | 541 | shfa, wikimedia | Give full CC BY 4.0 credit (creator, title, archive, license URL, adaptation notice) |
| `Public domain` | 80 | wikimedia | Verify EU/NO 70-yr p.m.a. term on historical drawings; courtesy credit recommended |
| `CC BY 3.0` | 8 | ksamsok, wikimedia | Give full CC BY 3.0 credit (creator, title, archive, license URL, adaptation notice) |
| `CC BY 2.5` | 3 | wikimedia | Give full CC BY 2.5 credit (creator, title, archive, license URL, adaptation notice) |
| `CC0 1.0` | 3 | wikimedia | Verify EU/NO 70-yr p.m.a. term on historical drawings; courtesy credit recommended |
| `Public Domain` | 3 | other | Verify EU/NO 70-yr p.m.a. term on historical drawings; courtesy credit recommended |
| `Public Domain Mark 1.0` | 1 | europeana | Verify EU/NO 70-yr p.m.a. term on historical drawings; courtesy credit recommended |

### B. ShareAlike Pool (Copyleft Trigger — Consolidated to Single Norwegian Museum Consortium)

Because the clean, kerf-safe, non-unknown-creator Non-ShareAlike pool in `hellerisninger/processed/catalog.json` contains **639** passing motifs (short of 750 by **111**), the remaining **111** slots were **deliberately consolidated exclusively into Unimus `CC BY-SA 4.0` records** (Norwegian University Museums: UiB Bergen, KHM UiO, NTNU Vitenskapsmuseet) rather than dispersing ShareAlike obligations across dozens of individual Wikimedia uploaders.

| Unimus Creator / Holding Institution | License | Count |
|---|---|---:|
| Universitetsmuseet i Bergen | `CC BY-SA 4.0` | 102 |
| Eva Lindgaard / NTNU Vitenskapsmuseet | `CC BY-SA 4.0` | 4 |
| Vogt, David / Kulturhistorisk museum, UiO | `CC BY-SA 4.0` | 3 |
| Heidrun Stebergløkken og Eva Lindgaard / NTNU Vitenskapsmuseet | `CC BY-SA 4.0` | 2 |

**Two clean paths to close the ShareAlike gate before commercial sale (confirm with IP counsel):**
1. **Path A — Institutional Non-SA Permission (Recommended if keeping Vingen/KHM icons):** Send the prepared UNSENT draft letters in [`archive-inquiry-drafts.md`](./archive-inquiry-drafts.md) to University Museum of Bergen (`post@um.uib.no`), KHM UiO, and NTNU Vitenskapsmuseet requesting written non-ShareAlike commercial licensing for the **111** specific Unimus motifs.
2. **Path B — 100% Non-ShareAlike Replacement:** Replace pieces `HELL-750-640`..`HELL-750-750` (111 slots) by harvesting 111 additional kerf-safe `CC BY 4.0` B&W tracings from SHFA (SHFA holds 3,846 `CC BY 4.0` tracings, of which only 286 plates are currently harvested) or verified Public Domain tracings, yielding a **750/750 Non-ShareAlike** edition.
3. **Path C — ShareAlike Compliance:** If legal counsel advises that derivative vector cut files (and/or physical adaptations) must remain under `CC BY-SA 4.0`, publish the derivative cut vector geometry under `CC BY-SA 4.0` with full attribution (note: this permits third parties to legally copy and sell the same cut pattern).

---

## 3. Archive Breakdown & Geographic Representation

| Archive | Piece Count | ShareAlike? | Primary Sites / Collections |
|---|---:|---|---|
| SHFA (Swedish Rock Art Research Archives, University of Gothenburg) (`shfa`) | 538 | No | Tegneby Skatteklåvan Tanum / Tanum 66:1, Fossum Tanum / Tanum 255:1, Aspeberget Tanum baksidan / Tanum 18:1, Tegneby Tanum / Tanum 51:1 |
| Unimus (Universitetsmuseenes fotoportal, Norway) (`unimus`) | 111 | Yes (`CC BY-SA 4.0`) | Vingen, Bremanger, Vistnesdalen, Vevelstad, Evje, Fredrikstad, Bogge , Nesset |
| Wikimedia Commons (`wikimedia`) | 94 | No | Bardal, Leirfall, Ydstines, Evenhus |
| K-samsök / SOCH (Swedish Open Cultural Heritage) (`ksamsok`) | 3 | No | Kopparstick |
| Nasjonalbiblioteket (National Library of Norway) (`other`) | 3 | No | Alta, Alta |
| Europeana (`europeana`) | 1 | No | Hällristning |

**Country split:** `SE`: 509, `NO`: 238, `DK`: 3

---

## 4. Excluded Assets & Legal Hygiene Audit Log

Out of **1446** records in `hellerisninger/processed/catalog.json`, **462** were explicitly excluded:

| Exclusion Category | Count | Rationale |
|---|---:|---|
| commercial_ok=false | 298 | Automated / visual QA exclusion |
| fails 3.0 mm neck screen at 650 mm² | 95 | Automated / visual QA exclusion |
| thin-tip loss exceeds 6.0 mm gate at 650 mm² | 40 | Automated / visual QA exclusion |
| raw source crop too small | 11 | Automated / visual QA exclusion |
| unknown/missing creator | 6 | Automated / visual QA exclusion |
| extreme aspect ratio | 5 | Automated / visual QA exclusion |
| visually audited blob/fragment outlier | 5 | Automated / visual QA exclusion |
| fails kerf screen at 650 mm² | 2 | Automated / visual QA exclusion |

### Specifically Excluded Attribution-Defect Records (Zero Unknown Creators Retained)
| Excluded `asset_id` | Archive | License | Recorded `creator` | Why Excluded |
|---|---|---|---|---|
| `helleristning_00552` | `unimus` | `CC BY-SA 4.0` | `Ukjent / Kulturhistorisk museum, UiO` | Fails zero-unknown-creator commercial hygiene rule |
| `helleristning_00787` | `unimus` | `CC BY-SA 4.0` | `Ukjent / Kulturhistorisk museum, UiO` | Fails zero-unknown-creator commercial hygiene rule |
| `helleristning_00788` | `unimus` | `CC BY-SA 4.0` | `Fotograf ukjent / Kulturhistorisk museum, UiO` | Fails zero-unknown-creator commercial hygiene rule |
| `helleristning_00789` | `unimus` | `CC BY-SA 4.0` | `Fotograf ukjent / Kulturhistorisk museum, UiO` | Fails zero-unknown-creator commercial hygiene rule |
| `helleristning_00964` | `unimus` | `CC BY-SA 4.0` | `Ukjent / Kulturhistorisk museum, UiO` | Fails zero-unknown-creator commercial hygiene rule |
| `helleristning_01460` | `wikimedia` | `Public domain` | `unknown photographer/artist` | Fails zero-unknown-creator commercial hygiene rule |

### Special Legal Watch Item — 20th-Century Historical Tracings Tagged `Public domain` on Wikimedia Commons
- **34** Wikimedia Commons pieces derive from 20th-century archaeological tracings (e.g., Gutorm Gjessing d. 1979, Mary Wilhelmsen, Th. Petersen d. 1952) tagged `Public domain` on Commons.
- Under Norwegian (*Åndsverkloven*) and EU copyright law, copyright in original artistic drawings runs **70 years post mortem auctoris** (Gjessing: 1979 + 70 = 2049), though purely faithful scientific/archaeological tracings of prehistoric carvings may lack *verkshøyde* (originality) or fall under legacy publication rules. **Action:** Flag these specific records for Norwegian IP counsel confirmation (or replace with SHFA `CC BY 4.0` equivalents if counsel deems risk non-zero).

---

## 5. Companion Machine-Readable Files

- [`selected-750-catalog.json`](./selected-750-catalog.json) — Locked 750-motif production catalog consumed by the puzzle generator.
- [`rights-inventory.csv`](./rights-inventory.csv) — Flat 750-row spreadsheet for legal counsel, archive outreach, and printed booklet typesetting.
- [`rights-inventory.json`](./rights-inventory.json) — Structured 750-row JSON with SHA-256 hashes and per-piece `open_actions`.
- [`archive-inquiry-drafts.md`](./archive-inquiry-drafts.md) — Ready-to-send **UNSENT** institutional clearance & attribution letters.
