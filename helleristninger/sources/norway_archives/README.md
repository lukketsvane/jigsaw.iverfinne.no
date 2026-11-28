# Norwegian Rock-Art Archives Harvest (`sources/norway_archives/`)

Programmatic, legally documented harvest of Norwegian rock carvings (*helleristninger* / *bergkunst*) — strongly prioritizing crisp black-and-white archaeological tracings (*kalkeringer* / *dokumentasjonstegninger*) plus high-contrast open-licensed close-up documentation across 12 Norwegian regions.

- **Harvester script:** `fetch_norway.py` (reproducible, resumable, per-host rate-limited, PIL-validated, SHA-256 deduplicated)
- **Immutable originals:** `raw/` (344 files, 202.1 MB)
- **Manifest:** `manifest.jsonl` (344 JSONL records, 0 integrity/schema errors, 0 duplicates, 0 orphan files)

---

## Harvest Summary (344 Verified Norwegian Files)

### By Source Archive
| `source_archive` | Portal / Institution | Files | Primary Media | `commercial_ok` |
|---|---|---:|---|---|
| `altarockart` | Alta Rock Art Archive (`altamuseum.fotoware.cloud`, Archive `5009-Kalkeringer`) | 150 | `bw_tracing` (150) | `false` (150) |
| `unimus` | Universitetsmuseenes fotoportal (`api.unimus.no/public-data-api`): KHM Oslo (65), UM Bergen (54), VM NTNU (28) | 147 | `bw_tracing` (131), `photo_other` (10), `painted_photo` (6) | `true` (147) |
| `other` | Nasjonalbiblioteket (`api.nb.no` + IIIF resolver, Public Domain collection) | 25 | `painted_photo` (14), `photo_other` (11) | `true` (25) |
| `kulturminnesok` | Riksantikvaren / Kulturminnesøk (`api.ra.no/brukerminner`) | 12 | `painted_photo` (9), `photo_other` (3) | `true` (12) |
| `digitaltmuseum` | DigitaltMuseum Norway via Europeana Open API (`api.europeana.eu`, `reusability=open`) | 10 | `photo_other` (10) | `true` (10) |
| **Total** | | **344** | **281 `bw_tracing`, 29 `painted_photo`, 34 `photo_other`** | **194 `true` / 150 `false`** |

### By Media Type
- `bw_tracing`: **281** (81.7% — 150 Alta digital tracings + 131 Unimus museum tracings/drawings)
- `photo_other`: **34** (9.9%)
- `painted_photo`: **29** (8.4%)

### By Norwegian Region (`country: "NO"`)
- **Finnmark:** 154 (Alta UNESCO World Heritage: Hjemmeluft/Bergbukten/Ole Pedersen/Apana Gård/Bergheim, Amtmannsnes, Transfarelv, Isnestoften, Kåfjord, Storsteinen, Bossekop, Kråknes, Kongshus, Årøya)
- **Vestland:** 58 (Vingen/Vingelva, Ausevik, Kårstadsteinen, Etne, Eidfjord)
- **Østfold:** 47 (Begby, Solberg, Kalnes, Skjeberg, Rolvsøy, Borge, Kråkerøy, Tune, Sarpsborg, Fredrikstad)
- **Innlandet:** 23 (Møllerstufossen / Nordre Land, Stein gård / Ringsaker, Nes / Hedmark)
- **Nordland:** 21 (Leiknes archaeological drawings, Skjomen / Forsheim, Vistnesdalen, Narvik, Helgeland)
- **Trøndelag:** 19 (Bardal, Leirfall, Stuberg, Hommelvik, Bøla, Evenhus, Hell, Holtås, Tessem, Namsskogan)
- **Møre og Romsdal:** 8 (Bogge, Herøy)
- **Akershus:** 4 (Bærum / Oslo area)
- **Buskerud:** 3 (Drammen / Åskollen)
- **Telemark:** 2 (Gjerpen / Skien)
- **Agder:** 2 (Erkleiv / Kristiansand)
- **Vestfold:** 1 (Sandefjord / Tjølling)
- **Norway (unspecified):** 2 (Unimus KHM/VM tracings without county metadata)

### By Archaeological Period
- `stone_age_hunter`: **245** (Alta, Vingen, Ausevik, Leiknes, Skjomen/Forsheim, Bøla, Møllerstufossen, Vistnesdalen, Bogge, Evenhus, Holtås)
- `bronze_age_agrarian`: **80** (Østfold, Vestfold, Telemark, Buskerud, Akershus, Etne, Kårstadsteinen, Leirfall, Stuberg, Hommelvik, Stein/Ringsaker)
- `unknown`: **15**
- `mixed`: **4** (Bardal, Hell)

### By License (100% Verified from Source API Metadata)
| License | Files | `commercial_ok` |
|---|---:|---|
| `CC BY-SA 4.0` | 150 | `true` |
| `Alta Museum Open Non-Commercial ShareAlike (CC BY-NC-SA-equivalent)` | 150 | `false` |
| `Public Domain` (Nasjonalbiblioteket `isPublicDomain: true`) | 25 | `true` |
| `Public Domain Mark 1.0` | 10 | `true` |
| `CC BY 4.0` | 7 | `true` |
| `CC0 1.0` | 2 | `true` |

---

## Archive Investigation & Blockers (Honest Audit)

### Open & Harvested
1. **Alta Rock Art Archive (`altarockart.no` / `altamuseum.fotoware.cloud`)**:
   - `robots.txt` allows crawling (`Disallow:` empty). Public FotoWeb JSON API exposes 6 archives including `5009-Kalkeringer` (267 tracings: 236 PDFs + 31 JPGs, with 2400px full-aspect JPEG server previews available via each asset's `.info` JSON endpoint).
   - **License nuance (critical):** IPTC field `40` on 265/267 assets grants free download, reuse, and adaptation for **non-commercial purposes** with attribution (`Verdensarvsenter for Bergkunst - Alta Museum` + creator) and ShareAlike (*"kan nedlastes og brukes fritt til ikke-kommersielle formål... Dersom du endrer eller bygger videre på materialet må du distribuere dine bidrag under samme vilkår som denne originalen"*). We excluded the 2 records lacking this notice and honestly marked all 150 Alta files `commercial_ok: false`. Downstream commercial workflows must filter on `commercial_ok == true`.
2. **Unimus — Universitetsmuseenes fotoportal (`api.unimus.no/public-data-api/media/search`)**:
   - Open public REST API serving all five Norwegian university museums.
   - **KHM (Oslo)**, **UM (Bergen — Vingen & Ausevik)**, and **VM (NTNU Vitenskapsmuseet)** publish their photographic and tracing archives under **`CC BY-SA 4.0`** (`commercial_ok: true`).
   - **Rate-limit / image-size note:** `web_hent_bilde.php` throttles high concurrency and serves very high-DPI scans (2 KHM tracings exceed Pillow's default 89 MP limit: `12093×9729` and `9360×10242`); downstream readers should set `PIL.Image.MAX_IMAGE_PIXELS = None`.
3. **Nasjonalbiblioteket (`api.nb.no/catalog/v1/items`)**:
   - Open JSON API + IIIF image resolver (`/services/image/resolver/{urn}/full/1600,/0/native.jpg`). Filtered strictly to `accessInfo.isPublicDomain == true` and `accessAllowedFrom == "EVERYWHERE"`.
4. **Riksantikvaren / Kulturminnesøk (`api.ra.no/brukerminner`)**:
   - Open OGC API Features endpoint returning user-contributed cultural heritage photos with explicit per-photo `lisens` tags (`CC0`, `CC BY`, `CC BY-SA`). Strictly filtered to genuine prehistoric rock art (excluded modern 1800s/1900s graffiti/initials, monuments, signs, landscapes, and non-rock-art features).
5. **DigitaltMuseum Norway via Europeana (`api.europeana.eu/record/v2/search.json`)**:
   - Used `reusability=open` + `COUNTRY:norway` + `TYPE:IMAGE` to obtain `Public Domain Mark 1.0` DigitaltMuseum records hosted on `pdms.dimu.org`, excluding Swedish sites (e.g., Aspeberget) and group/tourist photographs.

### Blocked, Restricted, or Excluded (Respected Without Bypass)
1. **Unimus — AM (Arkeologisk museum, Stavanger / Rogaland) & UMAK (Norges arktiske universitetsmuseum, Tromsø / Nordland & Troms)**:
   - Both museums license their Unimus rock-art photos (`AM`: 5,525 `helleristninger`, 118 `kalkering`; `UMAK`: 90 `bergkunst`) under **`CC BY-NC-ND 3.0`** (NoDerivatives). Because ND prohibits derivative tracing/jigsaw silhouette adaptation, **100% of AM and UMAK records were skipped**. (Rogaland & Nordland are instead partially covered via KHM/VM `CC BY-SA 4.0`, Nasjonalbiblioteket Public Domain, and Kulturminnesøk `CC BY`).
2. **DigitaltMuseum Direct API & Web Frontend (`api.dimu.org` / `digitaltmuseum.no`)**:
   - `https://api.dimu.org/api/solr/select?...&api.key=demo` and `/api/owners?country=no&api.key=demo` return HTTP `302` redirects to `/doc/public_api.html` — KulturIT's public `demo` key no longer serves live queries (an organization-issued API key is required).
   - `https://digitaltmuseum.no/robots.txt` explicitly disallows `/search`, `/api/`, and sets `Content-Signal: search=yes,ai-train=no,use=reference`. We strictly respected `robots.txt` and did not scrape the web frontend; open DigitaltMuseum Norway records were obtained via Europeana's official API instead.
3. **Virtuelle Vingen (`virtuellevingen.no`, University of Bergen)**:
   - Site explicitly states `"Alle foto Copyright Universitetet i Bergen / Copyright © Virtuelle Vingen 2018"` with no open Creative Commons license. **Skipped completely.** (Vingen tracings were harvested legally under `CC BY-SA 4.0` from Unimus UM / Per Fett instead.)
4. **Marcus — University of Bergen Library (`marcus.uib.no`)**:
   - `robots.txt` explicitly disallows `/search`, `/files/`, `/instance/page/`, and `/instance/digitalresource/`. Respected and skipped.
5. **NTNU Gunnerus (`ntnu.tind.io` / `gunnerus.no`)**:
   - `ntnu.tind.io/api/v1/search` returns HTTP `403` (`User guest is not authorized`), and `gunnerus.no` presents an SSL hostname mismatch. Skipped.
6. **Zenodo & DataverseNO**:
   - `dataverse.no` returned 0 datasets for `helleristning`, `helleristninger`, `bergkunst`, and `Vingen`.
   - `zenodo.org` returned only 3D mesh/paper records (`10255898` Vestre Vehammaren `.glb` mesh; `4074913` Alta Linked Reindeers PDF/PPTX slides) rather than 2D tracings/SVGs.
7. **GitHub (`Research-Squirrel-Engineers/alta-rock-art`)**:
   - MIT-licensed metadata spreadsheet (`alta.ods`) + Wikidata SPARQL queries only; contains no image/SVG files.

---

## Re-Running the Harvester

```powershell
.\.venv\Scripts\python.exe .\hellerisninger\sources\norway_archives\fetch_norway.py
```

The script reuses valid cached files in `raw/`, enforces a >=500 MB free-disk safety floor, validates every image with Pillow, deduplicates by SHA-256, prunes unreferenced files, and writes `manifest.jsonl`.
