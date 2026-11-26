# Nordic Rock Art Open Archives (`sources/nordic_archives`)

Programmatically harvested, legally documented Phase-1 source corpus of Nordic *helleristninger* / *hällristningar* (Scandinavian prehistoric rock carvings) for the figurative jigsaw silhouette pipeline (`hellerisninger/`).

All raw files stored in `raw/` are **immutable original downloads** verified for readability, contrast/cleanliness, and deduplicated by SHA-256. Every file is indexed in [`manifest.jsonl`](./manifest.jsonl).

---

## Harvested Open Archives & Verified Licenses

### 1. SHFA — Swedish Rock Art Research Archives (`shfa.dh.gu.se`)
- **Host:** University of Gothenburg / GRIDH (`https://shfa.dh.gu.se/api/`)
- **API Used:** Public Django REST API (`/api/image/?type={944,955,951,946}&depth=1`, `/api/search/advance/`) and direct original file hosting (`https://data.dh.gu.se/shfa/static/shfa/original/...`).
- **Verified License:** **Creative Commons Attribution 4.0 International (`CC BY 4.0`)** — `https://creativecommons.org/licenses/by/4.0/` (`commercial_ok: true`).
  - Verified directly from SHFA's official platform text (`/assets/index-*.js`):
    > *"The images are protected by a creative commons license (CC BY 4.0). For more information, please visit creative common's own website at: https://creativecommons.org/licenses/by/4.0/"*
  - **Attribution requirement:** Cite the original creator/photographer/institution recorded in each manifest entry's `creator` field together with SHFA (`https://shfa.dh.gu.se/`).
- **Curation & Quality Filtering:**
  - Prioritized pure black-on-white (and crisp red-on-white) archaeological tracings and graphics (`Kalkering (Plast)`, `Kalkering (Papper)`, `Ritning`, `Grafik`) from verified clean documentation collections (`Stiftelsen för dokumentation av Bohusläns hällristningar` / Toreld & Andersson, `Universitetet i Bergen` / Vingen Bakka/Bøe/Lødøen/Hallström, `Åke Fredsjö`, `Torsten Högberg`, `Gerhard Milstreu` / Tanums Hällristningsmuseum, `Sven-Gunnar Broström & Kenneth Ihrestam`, `Harald Faith-Ell` / Carl-Axel Althin, `Gustaf Hallström`).
  - Explicitly excluded cupmark-only plates (`Skålgropar`), cursive field notebooks (`Einar Kjellén`), coordinate-gridded plates (`Göran Burenhult`), yellowed 19th-century pencil sketches, stippled-background lithographs, and non-Nordic international SHFA records (Italy/Ireland/Spain).
  - Applied an automated pixel-level cleanliness check (bright neutral background, non-yellowed paper, sufficient ink signal) without altering any downloaded bytes.
  - Included a small curated set of high-contrast `Frottage` rubbings (`media_type: "rubbing_frottage"`) specifically for Norwegian (Alta, Ausevik/Flora, Fredrikstad, Sarpsborg, Steinkjer, Alstahaug, Narvik, Frosta, Balsfjord), Danish (Bornholm Madsebakke/Hammersholm, Odsherred, Lejre, Kalundborg), and Northern Swedish Stone Age hunter-gatherer sites (Nämforsen, Glösa/Jämtland).

### 2. Europeana Open Search API (`api.europeana.eu`)
- **API Used:** `https://api.europeana.eu/record/v2/search.json` filtered strictly with `reusability=open` and `media=true`.
- **Verified Licenses:** `CC BY-SA 4.0`, `Public Domain Mark 1.0`, and `CC0 1.0` (`commercial_ok: true`), verified per record from `rights`.
- **Curation:**
  - Harvested Vänersborgs museum's crisp black-on-white `avritning` tracings from Fröskog / Dalsland (`CC BY-SA 4.0`, `bw_tracing`).
  - Harvested a small curated set of high-contrast red-painted carvings from Bohuslän Museum (`Public Domain Mark 1.0`) and Upplands Museum (`CC0 1.0`).
  - Explicitly excluded `Wikimedia Commons Community` records so there is zero overlap with `sources/wikimedia/`.

### 3. K-samsök / SOCH — Swedish Open Cultural Heritage (`kulturarvsdata.se`)
- **API Used:** `https://kulturarvsdata.se/ksamsok/api` (`method=search`, `recordSchema=presentation`), filtered strictly on `mediaLicense` (`cc0`, `pdm`, `by`, `by-sa`) — explicitly excluding all `by-nc`, `by-nc-nd`, and `by-nd` museum records.
- **Verified Licenses:** `CC BY 3.0` and `CC0 1.0` (`commercial_ok: true`).
- **Curation:** Historic copper engravings (`Kopparstick` from Kulturparken Småland), museum drawings (`Vänersborgs museum`), and crisp rock-carving records (`Västernorrlands museum`, `Jönköpings läns museum`, `Göteborgs Naturhistoriska Museum` Evenhus panel), deduplicated against Europeana by SHA-256.

### 4. Nationalmuseet Denmark — Samlinger Online (`samlinger.natmus.dk`)
- **API Used:** Public `/api/Assetbrowse?category=DO ~ Emne ~ Kunst, kultur ~ Helleristninger` and verified per-object page license scraping (`https://samlinger.natmus.dk/do/asset/<id>`).
- **Verified License:** `CC BY-SA 4.0` (`https://creativecommons.org/licenses/by-sa/4.0/`, `commercial_ok: true`).
- **Curation:** Iconic Danish Bronze Age carved stones — `Helleristningssten fra Engelstrup` (red-painted crewed ship, sun circle, humans), `4 Helleristningssten med håndtegn fra Sandagergård`, `Figurfremstilling fra Bjergagergård`, and `Billedsten fra Kivik`.

---

## Archive Findings, Caveats & Respected Blockers

| Portal / Source | Status | Finding |
|---|---|---|
| **SHFA (`shfa.dh.gu.se`)** | Harvested (primary) | Open Django REST API + CC BY 4.0 across 26,915 records, including 3,846 B&W tracings/drawings/graphics and 2,189 rubbings across Sweden, Norway (550+ records incl. Vingen & Alta), and Denmark (415+ incl. Bornholm). |
| **Europeana (`api.europeana.eu`)** | Harvested | Open search works via `wskey=api2demo` with `reusability=open`; Vänersborgs museum provides crisp B&W Dalsland tracings (`CC BY-SA 4.0`). |
| **K-samsök (`kulturarvsdata.se`)** | Harvested | Open JSON/presentation API; most museum drawings from Bohusläns museum are `CC BY-NC-ND` (skipped), while Smålands museum / Vänersborg / Västernorrland / Jönköping / GNM offer open `CC BY 3.0` / `CC0` records. |
| **Nationalmuseet DK (`samlinger.natmus.dk`)** | Harvested | Public `/api/Assetbrowse` endpoint + direct `.jpg` GET downloads (HTTP `HEAD` returns 405, `GET` returns 200) under `CC BY-SA 4.0`. |
| **Danish Agency for Culture (`kulturarv.dk`)** | Inspected & Skipped | Accessible `PDM 1.0` images via Europeana (Madsebakke, Allinge, Sandvig, Hammersholm), but visual inspection showed unpainted outdoor granite/grass landscapes unsuitable for silhouette extraction. |
| **Finna (`api.finna.fi`)** | Blocked (respected) | Returns HTTP 403 Cloudflare anti-bot challenge (`Just a moment...`). Not bypassed. |
| **Finnish Heritage Agency (`mpfinlandrepox.zetcom.app`)** | Blocked (respected) | Listed in Europeana for Astuvansalmi (`CC BY 4.0`), but direct image download URLs return HTTP 401 Unauthorized. Not bypassed. |
| **Zenodo (`zenodo.org`)** | Inspected & Skipped | Hosts CC BY 4.0 SHFA 3D `.glb` meshes (Nämforsen, Tanum, Släbro) and MIT tooling (`DL-Tools`), rather than 2D B&W motif tracings or SVG silhouette packs. |
| **HuggingFace Datasets** | Inspected & Empty | Zero public datasets matching `petroglyph` or `rock art`. |

---

## Harvest Summary

Independent audit verified **301 unique, immutable original files** (`raw/` ↔ `manifest.jsonl` 1:1 parity, **0 SHA-256 / schema / readability errors**, **100% `commercial_ok: true`**):

| Breakdown | Counts |
|---|---|
| **Total Assets** | **301** (deduplicated by SHA-256) |
| **By Archive** | `shfa`: **286**, `ksamsok`: **6**, `nationalmuseet`: **5**, `europeana`: **4** |
| **By `media_type`** | `bw_tracing`: **241** (80.1%), `rubbing_frottage`: **49** (16.3%), `photo_other`: **7** (2.3%), `painted_photo`: **4** (1.3%) |
| **By `country`** | `SE`: **183** (`bw_tracing`: 169, `rubbing_frottage`: 10, `painted_photo`: 2, `photo_other`: 2), `NO`: **94** (`bw_tracing`: 66, `rubbing_frottage`: 25, `photo_other`: 3), `DK`: **24** (`bw_tracing`: 6, `rubbing_frottage`: 14, `painted_photo`: 2, `photo_other`: 2) |
| **By `period`** | `bronze_age_agrarian`: **187**, `stone_age_hunter`: **104**, `mixed`: **10** |
| **By `license`** | `CC BY 4.0`: **286**, `CC BY-SA 4.0`: **8**, `CC BY 3.0`: **6**, `Public Domain Mark 1.0`: **1** (all `commercial_ok: true`) |

### Regional & Motif Highlights
- **Top Regions:** Vestland — Bremanger / Vingen (66), Bohuslän — Tanum (53), Tossene (29), Brastad (16), Askum (14), Östergötland — Borg/Norrköping (12), Sjælland — Odsherred (10), Uppland — Boglösa/Enköping (10), Bornholm — Madsebakke/Hammersholm (8), Finnmark — Alta (7), Bohuslän — Svarteborg/Bro/Kville/Foss/Skee (15), Ångermanland — Nämforsen (4 + 2 K-samsök), Jämtland — Glösa (6), Dalsland — Fröskog (3), Trøndelag — Steinkjer/Frosta/Evenhus (8), Østfold — Fredrikstad/Sarpsborg (5), Vestland — Flora/Ausevik (3), Nordland — Alstahaug/Narvik (4), Skåne — Simrishamn/Kivik (3).
- **Top Motif Hints:** `bronze_age_ship` (188), `quadruped_animal` (163), `anthropomorph` (129), `deer` (83), `circle` (79), `footprint` (51), `sun_wheel` (48), `adorant` (27), `concentric_circle` (25), `chariot_wagon` (21), `bird` (20), `horse` (19), `elk_moose` (17), `spiral` (14), `axe_bearer` (13), `cattle` (12), `dog_canine` (11), `acrobat_vaulter` (11), `tree` (11), `plough` (10), `archer` (9), `spear_bearer` (9), plus `lur_blower`, `warrior`, `rider`, `snake`, `fish`, `bear`, `wolf`, `handprint`, `axe`, `labyrinth`.

### Pipeline Ingestion Notes for `shape-pipeline`
- **Large high-res plates:** 8 SHFA archival tracings exceed 50 megapixels (largest: `shfa_110655` at 12,293×18,572 px ≈ 228 MP, `shfa_110863` at 13,693×10,000 px ≈ 137 MP). Set `PIL.Image.MAX_IMAGE_PIXELS = None` (or ≥ 350M) and downsample huge sheets safely before connected-component segmentation.
- **Red-ink tracings on white:** Several Torsten Högberg `Kalkering (Papper)` plates use crisp dark-red ink on white paper; threshold on saturation/red-channel in addition to grayscale luminance. Similarly, some Toreld/Andersson `Kalkering (Plast)` sheets draw rock edges in thin red while carvings are thick solid black — prefer dark neutral/black ink when red rock-edge contours are present.
- **Visual QA pruning:** All 27 non-SHFA items and a 24-tile stratified sample were visually inspected; 12 non-carving items (RAÄ handwritten ledger pages, outdoor barn/tree photos, a Malung wooden souvenir box) were pruned from `raw/` and `manifest.jsonl`.

---

## Reproducibility

```powershell
& "c:\Users\Iver\Documents\github\jigsaw.iverfinne.no\.venv\Scripts\python.exe" "c:\Users\Iver\Documents\github\jigsaw.iverfinne.no\hellerisninger\sources\nordic_archives\fetch_nordic.py"
```

Pass `--clean` to clear `raw/` and re-harvest from scratch. All manifest lines conform to the shared Phase-1 schema (`source_id`, `source_archive`, `title`, `description`, `site`, `region`, `country`, `period`, `media_type`, `motif_hints`, `raw_path`, `source_url`, `download_url`, `creator`, `license`, `license_url`, `commercial_ok`, `sha256`, `width_px`, `height_px`).
