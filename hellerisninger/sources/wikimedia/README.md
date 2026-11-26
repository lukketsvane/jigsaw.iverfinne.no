# Wikimedia Commons Nordic Rock Art Harvest (`hellerisninger/sources/wikimedia`)

Programmatically harvested, license-verified, and visually audited collection of Nordic rock carvings (*helleristninger* / *hällristningar*) from [Wikimedia Commons](https://commons.wikimedia.org/), built for Phase 1 of the Nordic Rock-Art Figurative Jigsaw Corpus.

## Directory Layout

- `fetch_wikimedia.py` — Reproducible MediaWiki API harvester with polite `User-Agent`, rate limiting, exponential backoff on HTTP 429, strict `extmetadata` license checking, SVG XML validation, Pillow raster verification, and color-based media-type classification.
- `manifest.jsonl` — 286 one-JSON-object-per-line provenance records matching the shared corpus schema.
- `raw/` — 286 immutable original files (796.9 MB total), SHA-256 verified and deduplicated (`0` orphans, `0` hash mismatches).

## Harvest Methodology

1. **Discovery (`--discover`)**:
   - Crawled 124 curated Wikimedia Commons rock-art categories across Norway (Finnmark/Alta, Troms/Skavberg, Nordland/Alstahaug/Rødøy, Trøndelag—Bardal, Bøla, Evenhus, Hegre, Hell, Holtås, Homnes, Kvennavika, Leirfall, Selbustrand, Strand, Stykket, Ydstines, Solsemhula—Vestland/Vingen, Rogaland/Stavanger/Austre Åmøy, Agder/Pennefeltet, Vestfold, Buskerud/Skogerveien/Sundvollen, Innlandet/Møllerstufossen/Drotten/Åkersetervollen, Oslo/Ekeberg/Lambertseter, Akershus, Østfold/Begby/Solberg/Kalnes/Hornes/Bjørnstad), Sweden (Tanum/Vitlycke/Aspeberget/Fossum/Litsleby/Gerum/Kalleby/Lövåsen, Bohuslän, Nämforsen, Himmelstalund/Norrköping, Kivik, Simrishamn/Järrestad, Frännarp, Uppland/Brandskog/Hemsta, Södermanland/Slagsta/Tuna, Västergötland/Flyhov, Dalsland/Högsbyn, Blekinge/Hästhallen, Gotland/Hägvide), Denmark (Bornholm/Madsebakke), Finland (Värikallio/Verla), and `Category:Drawings of petroglyphs*` / `Category:Petroglyphs of ships`.
   - Supplemented with 22 politely paced CirrusSearch queries targeting SVG drawings (`filetype:drawing`), historical archaeological tracings (Gjessing, Petersen, Sognnes, Hallström, Lossius, Habel, Baltzer, KMB), and under-categorized Norwegian sites (Vingen, Rødøy, Ausevik, Austre Åmøy).
   - Discovered **4,459** unique candidate files; batch-fetched `imageinfo` (`url`, `size`, `mime`, `sha1`, `extmetadata`) and non-hidden categories in groups of 50.
2. **Filtering & Selection (`--download`)**:
   - Excluded non-Nordic petroglyphs, maps, heraldry/coats of arms, flags, runestones, pure cup-marks, museum exteriors/signs/plaques, videos/PDFs, files > 25 MB, and vandalized/meme uploads (e.g., `File:Groove Battle.svg`).
   - Strictly parsed Commons `extmetadata` (`LicenseShortName`, `LicenseUrl`, `UsageTerms`, `NonFree`, `Copyrighted`) and skipped any non-free, NC, ND, ambiguous (`No restrictions`), or unknown licenses. Every retained record has `commercial_ok: true` backed by verified open license metadata.
   - Selected 100% of verified Nordic `svg_vector`, `bw_tracing`, and `rubbing_frottage` candidates, plus a site-balanced selection of red-painted (`painted_photo`) and clear closeup (`photo_other`) carvings weighted toward Norwegian sites.
   - Automated pixel QA rejected 14 sky-heavy/foliage-heavy landscape photos during download.
3. **Visual QA Audit & Refinement**:
   - Generated full visual contact sheets across all downloaded `bw_tracing`, `rubbing_frottage`, `painted_photo`, and `photo_other` records and inspected them manually.
   - Pruned **34** non-carving/scenic/people/sign/waterfall/church/shelter photos and non-Nordic book engravings (e.g., Phoenician boat woodcuts) from both `raw/` and `manifest.jsonl`.
   - Corrected `media_type` on 22 dark-rock archival grayscale photos (reclassified from `bw_tracing` to `photo_other`), 12 red-painted carvings (reclassified to `painted_photo`), and 6 crisp archival paper drawings on cream stock (upgraded to `bw_tracing`, including Vingen deer `wikimedia_36368162` and Bardal/Tanum archival drawings).

## Final Corpus Summary (`N = 286`)

### By `media_type`
| `media_type` | Count | Notes |
|---|---:|---|
| `svg_vector` | 10 | All verified Nordic Bronze Age SVGs on Commons (`Petroglypgh Ship/Group Nordic Bronze Age 001–010`) |
| `bw_tracing` | 62 | Visually verified crisp archaeological paper tracings/plates (Gjessing, Petersen, Sognnes, Hallström, Lossius, Habel, KMB, Nordisk familjebok, Vingen, Kivik, Himmelstalund, etc.) |
| `rubbing_frottage` | 2 | Bohuslän/Tanum frottage rubbings |
| `painted_photo` | 71 | Visually verified high-contrast red-painted carvings (*oppmalte helleristninger*) across Norway & Sweden |
| `photo_other` | 141 | Visually audited closeup & chalked archival rock carving photographs |

### By `country`
| `country` | Count | Key Regions |
|---|---:|---|
| `NO` | 206 | Trøndelag (114), Østfold (20), Rogaland (10), Innlandet (10), Finnmark (9), Nordland (8), Buskerud (8), Troms (7), Vestland (6), Oslo (5), Norway-general (5), Akershus (2), Vestfold (1), Agder (1) |
| `SE` | 74 | Bohuslän/Tanum (30), Skåne/Kivik (8), Sweden-general (7), Södermanland (5), Uppland (4), Dalsland (4), Östergötland (3), Ångermanland/Nämforsen (3), Gotland (3), Blekinge (3), Västergötland (3), Jämtland (1) |
| `FI` | 4 | Finnish rock art / paintings (Verla, etc.) |
| `DK` | 2 | Denmark / Bornholm |

### By `period`
- `bronze_age_agrarian`: 152
- `stone_age_hunter`: 91
- `mixed`: 24
- `unknown`: 19

### By `license` (100% `commercial_ok: true`)
- `Public domain`: 102
- `CC BY-SA 4.0`: 81
- `CC BY-SA 3.0`: 33
- `CC BY 2.5`: 22
- `CC BY-SA 3.0 no`: 11
- `CC BY 3.0`: 10
- `CC BY 2.0`: 9
- `CC0 1.0`: 7
- `CC BY 4.0`: 6
- `CC BY-SA 2.5`: 3
- `CC BY-SA 2.0`: 2

## Pipeline Notes & Caveats

- **Max raster dimensions**: Largest image is `wikimedia_115640380` (`Kalkering Strand Tb6 G Gjessing 1933`, 8956×5338 px = 47.8 MP), within Pillow's default pixel limit, though setting `Image.MAX_IMAGE_PIXELS = None` is still recommended across manifests.
- **B&W tracing color variations**: Most `bw_tracing` files are black ink on white/cream paper; a few exceptions include red-ink-on-white (`wikimedia_1778709` Himmelstalund boars, `wikimedia_2827917` Ekeberg red-numbered figures) and sepia/aged paper stock (Petersen 1929 Ydstines, KMB Bohuslän drawings).
- **Chalked archival photos (`photo_other`)**: Several historical Norwegian photographs (e.g., Gjessing 1934 Bardal/Evenhus/Strand/Kvennavika, `wikimedia_126218382` Leirfall procession) depict bright white chalked outlines on dark rock rather than dark ink on white paper; they are accurately tagged `photo_other` so B&W paper-tracing thresholds are not misapplied.
- **SVG filename typo preserved from Commons**: The 10 Nordic Bronze Age SVGs use Wikimedia's original filename spelling `Petroglypgh_*` (all valid XML with clean `<path>` geometry and parsed `viewBox` dimensions).
- **Re-running**:
  ```powershell
  .venv\Scripts\python.exe hellerisninger\sources\wikimedia\fetch_wikimedia.py --all --target-total 320
  ```
