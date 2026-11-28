# Packaging, Booklet & Retail Label Alignment — Helleristning 750

**Figma Concept Reference:** [`GK5-modul-2 · Northern · eige-dokument` (`node-id=299-65`)](https://www.figma.com/design/h8W3hu6PLLjmj2Tra8FgOH/GK5-modul-2-%C2%B7-Northern-%C2%B7-eige-dokument?node-id=299-65)  
**Board Geometry Sync Status:** LOCKED FROM `runs/helleristning-750/partition.json`  
**Commercial Readiness Notice:** Pre-production specification aligned with [`eu-norway-safety-provisional-review.md`](./eu-norway-safety-provisional-review.md) and [`rights-inventory.md`](./rights-inventory.md). Final print mechanicals require locked supplier thickness, final legal clearance, and physical prototype fit check.

The first [product image studies](product-studies/01/README.md) explore the birch pieces, companion guide and retail packaging.

---

## 1. Finalized Board & Product Specifications

| Specification | Value | Notes |
|---|---|---|
| **Product Title (NO / EN)** | *Helleristningar — 750 brikkers trepuslespel* / *Nordic Rock Art — 750-Piece Wooden Jigsaw* | Align exact typography & brand voice with Figma `Northern` concept (`node-id=299-65`) |
| **Piece Count** | **750 pieces** | 100% figurative helleristning silhouettes (0 filler cells, 0 Alta NC-SA) |
| **Assembled Board Dimensions** | **971.6 × 656.5 mm** | Footprint area `442,301.2 mm²` (`~589.7 mm²/piece`) |
| **Outline Style** | Organic / edge-extended rock-art silhouette perimeter (verified gapless planar partition, 750 pieces) | Border motifs preserved without straight-frame amputation |
| **Material Spec** | Nordic/Finnish Birch Plywood (`3.0 mm` or `4.0 mm`), Grade B/BB | REACH Annex XVII Entry 77 (`≤ 0.062 mg/m³`), Non-RU/BY origin, FSC/PEFC CoC |
| **Recommended Age Grade** | **14+ (Adult / Collector Puzzle)** | Exempt from Toy Safety Directive 2009/48/EC (>500 pieces); **DO NOT print CE mark** |

---

## 2. Box Volume, Weight & Inner Packaging Engineering

Calculated from actual puzzle wood area (`442,301 mm²`, birch plywood density `~680 kg/m³`, loose-piece packing factor `2.25×` inside a drawstring linen/cotton pouch):

| Plywood Thickness | Solid Wood Mass | Estimated Loose Piece Volume | Recommended Internal Box Cavity | Box Cavity Volume | Est. Total Gross Weight (Wood + Box + Pouch + Booklet) |
|---|---:|---:|---|---:|---:|
| **3.0 mm** | ~902 g | ~2,986 cm³ | `285 × 215 × 88 mm` | 5,392 cm³ | **~1,322 g** (`1.32 kg`) |
| **4.0 mm** | ~1,203 g | ~3,981 cm³ | `285 × 215 × 88 mm` (or `295 × 220 × 95 mm` for extra headspace) | 5,392 cm³ | **~1,623 g** (`1.62 kg`) |

### Structural Alignment with Figma `GK5-modul-2 · Northern` (`node-id=299-65`)
- **Nordic Tactile Presentation:** Two-piece rigid telescope box (uncoated matte FSC kraft/stone-toned paper wrap over 2.0 mm greyboard, embossed or ochre-foil rock-art emblem matching the *Northern* Scandinavian design direction).
- **Zero-Plastic Inner Protection:** Unbleached organic cotton/linen drawstring bag (breathable so faint fresh birch-laser scent dissipates naturally; eliminates polybag suffocation warning requirement).
- **A5 Companion Booklet Tray:** Internal cardboard lip or top-rest cavity sized cleanly for an **A5 (`148 × 210 mm`)** saddle-stitched or perfect-bound rock-art guide & attribution booklet (`285 × 215 mm` box fits A5 horizontally with generous finger notches).
- **Shipping Carton Compatibility:** External box `~292 × 222 × 94 mm` fits standard Posten/Bring *Norgespakke* and EU parcel tiers cleanly under 2.0 kg.

---

## 3. Companion Rock-Art Legend & Legal Attribution Booklet Specification

Because **750** archival motifs cannot carry individual legal credits on the wooden pieces themselves, the printed **A5 Companion Booklet** (paired with a permanent product attribution URL printed via QR code) is BOTH a collector highlight AND a core Creative Commons attribution compliance mechanism:

1. **Cover & Cultural Context (pp. 1–4):** Bilingual Norwegian (*nynorsk/bokmål*) + English introduction to Scandinavian Stone Age hunter-gatherer (*Vingen, Kvennavika, Nämforsen*) and Nordic Bronze Age agrarian (*Tanum/Bohuslän, Østfold, Trøndelag, Bornholm*) rock carvings.
2. **Fold-Out Reference Poster / Key Map:** High-contrast numbered reference outline map of the assembled 750-piece board so assemblers can identify individual motifs by piece number (`#1–#750`).
3. **750-Piece Provenance & Attribution Register (pp. 5–28):** Compact, scannable tabular index directly typeset from [`rights-inventory.csv`](./rights-inventory.csv) listing for every piece:
   - Piece `#` (`1..750`) · Motif / Site · Creator / Institution · Archive · License (`CC BY 4.0`, `CC BY 3.0`, `CC BY 2.5`, `CC0 1.0`, `Public Domain`, and — if retained post-clearance — `CC BY-SA 4.0` or negotiated museum license).
4. **Archive Credits & Adaptation Disclosure (Back Cover Inside):**
   - Explicitly states: *"Individual puzzle piece silhouettes were geometrically adapted and interlocked for laser-cut birch wood manufacture from the archival rock-art documentation listed herein."*
   - Lists full Creative Commons license deed URLs (`https://creativecommons.org/licenses/by/4.0/`, etc.) and QR code linking to the full machine-readable online attribution table (`rights-inventory.csv` / web mirror).
   - **Current Rights Split Recorded:** **639** Non-ShareAlike motifs + **111** Unimus `CC BY-SA 4.0` motifs (awaiting institutional clearance or swap prior to print!).

---

## 4. Mandatory Back-of-Box Compliance & Safety Label Block (Provisional)

Place the following structured label block cleanly on the bottom/back panel of the box (verify final wording with Norwegian/EU compliance counsel before print — **DO NOT place a toy CE mark**):

```text
------------------------------------------------------------------------
HELLERISTNINGAR — 750-PIECE WOODEN JIGSAW PUZZLE (14+)
Dimensions: 972 × 657 mm  |  Material: Nordic Birch Plywood (FSC/PEFC)
SKU / Batch: HELL-750-ED01-B2026-__  |  Made in: [Country of Manufacture]

[NO] ADVARSEL: Inneholder små deler. Kvelningsfare — ikke egnet for barn
     under 3 år. Puslespill for voksne og ungdom (14+ år). Ikke et leketøy.
[EN] WARNING: Contains small parts. Choking hazard — not suitable for
     children under 3 years. Adult/collector puzzle intended for ages 14+.
     Not a toy (exempt under Directive 2009/48/EC Annex I, >500 pieces).

Producer / Produsent:
  [Legal Company Name], [Street Address, Postal Code, City, Norway]
  Contact / Support: [compliance@domain.no]  |  [https://domain.no/helleristning-750]

EU Responsible Person (GPSR Art. 16 — required for EU Member State sales):
  [EU Authorized Representative Name, EU Street Address, Email — TBD]

Archival Credits & Full 750-Piece Source Register:
  See enclosed booklet & [https://domain.no/helleristning-750/credits]
  (Contains adapted Nordic rock-art documentation; zero non-commercial sources)

Packaging Disposal / Kildesortering:
  Box: Paper/Cardboard (Papir/Papp)  |  Pouch: Reusable Textile
  [Add destination-country EPR marks as registered: e.g. Grønt Punkt / LUCID]
------------------------------------------------------------------------
```

---

## 5. Pre-Print Sign-Off Gates

- [x] Sync final locked assembled dimensions from `runs/helleristning-750/partition.json` (locked: **971.6 × 656.5 mm**, `442,301.2 mm²`, 750 pieces).
- [ ] Confirm final birch plywood thickness (`3.0 mm` vs. `4.0 mm`) from physical calibration coupon testing and lock box depth (`88 mm` vs. `95 mm`).
- [ ] Close ShareAlike & PD-term legal gates in [`rights-inventory.md`](./rights-inventory.md) before typesetting the booklet.
- [ ] Complete all 6 safety gates (`SAF-01..SAF-06`) in [`eu-norway-safety-provisional-review.md`](./eu-norway-safety-provisional-review.md) and populate Producer / EU Responsible Person / Batch fields.
- [ ] Export final dielines from Figma (`node-id=299-65`) matching the locked `285 × 215 × 88 mm` (or adjusted) internal cavity.
