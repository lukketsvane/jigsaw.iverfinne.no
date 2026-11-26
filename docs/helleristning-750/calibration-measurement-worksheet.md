# Supplier Laser Calibration Measurement Worksheet — Helleristning 750

**Coupon File:** [`runs/helleristning-750-commercial/calibration_coupon.svg`](../../runs/helleristning-750-commercial/calibration_coupon.svg) (`240 × 185 mm`, strict 1:1 mm)  
**Visual Preview:** [`runs/helleristning-750-commercial/calibration_coupon_preview.png`](../../runs/helleristning-750-commercial/calibration_coupon_preview.png)  
**Printable Form:** [`calibration-measurement-worksheet.html`](./calibration-measurement-worksheet.html)

---

## 1. Laser Layer Cut Order & Setup (Mandatory)

| SVG Group ID | Stroke Color | Operation | Order | Notes |
|---|---|---|---|---|
| `#engraving` | `#008800` (Green) | Surface score / light vector engrave | **1st** | Low power, readable without charring through top veneer |
| `#interior-cuts` | `#FF0000` (Red) | Full through-cut | **2nd** | Zero duplicate shared paths; do NOT auto-optimize away kerf pairs |
| `#exterior-cuts` | `#0000FF` (Blue) | Full through-cut | **3rd (Last)** | Releases the `240 × 185 mm` coupon from the sheet |

> **IMPORTANT:** Cut at **exact 100% scale (1:1 mm)** with **auto kerf-compensation DISABLED** in the laser controller so measured kerf reflects the true beam width. Orient sheet face grain **horizontally** along the `GRAIN --->` arrow engraved on the coupon.

---

## 2. Material, Machine & Compliance Header

| Field | Supplier Entry |
|---|---|
| Supplier / Workshop Name | ____________________________________________ |
| Test Date & Operator | ____________________________________________ |
| Wood Species & Grade (e.g., Finnish Birch B/BB) | ____________________________________________ |
| Mill Country of Origin (Non-RU / Non-BY confirmed?) | [ ] Yes, Country: __________  [ ] FSC/PEFC CoC Attached |
| Nominal vs. Measured Sheet Thickness (mm) | Nominal: _____ mm  /  Caliper (4 corners): _____, _____, _____, _____ mm |
| Ply Count & Glue Line Type (NAF / ULEF / PF / UF) | ____________________________________________ |
| REACH Entry 77 Formaldehyde Test (≤ 0.062 mg/m³) | [ ] Report Attached  [ ] Pending (Blocks EU Sale) |
| Masking Applied? (Top / Bottom / Unmasked) | ____________________________________________ |
| Laser Machine, Wattage, Lens Focal Length | ____________________________________________ |
| Cut Settings (Power %, Speed mm/s, PPI/Hz, Air Assist) | ____________________________________________ |
| Engrave Settings (Power %, Speed mm/s) | ____________________________________________ |

---

## 3. Section A — Kerf Measurement (`0.08–0.30 mm`)

1. **10-Cut Caliper Block:** Push all 10 cut strips tight against one side of the `100.00 mm` pocket. Measure total accumulated gap with digital calipers (0.01 mm resolution):
   - Total 10-cut gap: `__________ mm` → **Measured Average Kerf (`gap / 10`) = `__________ mm`**
2. **Kerf Step Comb (`10.00 mm` Reference Bar):** Insert the `10.00 mm` reference bar into each labeled slot (`0.08 / 0.10 / 0.12 / 0.15 / 0.18 / 0.20 / 0.22 / 0.25 / 0.28 / 0.30 mm`):

| Slot Label (mm) | 0.08 | 0.10 | 0.12 | 0.15 | 0.18 | 0.20 | 0.22 | 0.25 | 0.28 | 0.30 |
|---|---|---|---|---|---|---|---|---|---|---|
| Fit (`L`=Loose / `S`=Snug / `T`=Tight / `X`=No-go) | | | | | | | | | | |

- **Recommended Digital Design Kerf for Production:** `__________ mm`

---

## 4. Section B — Dimensional Accuracy (`50.00 × 50.00 mm` Square)

| Dimension | Nominal | Measured (mm) | Error (mm) | Pass (`±0.20 mm`)? |
|---|---:|---:|---:|---|
| X Width (along grain) | 50.00 mm | | | [ ] Pass [ ] Fail |
| Y Height (cross grain) | 50.00 mm | | | [ ] Pass [ ] Fail |
| Diagonal 1 / Diagonal 2 (Squareness) | 70.71 mm | _____ / _____ | | [ ] Pass [ ] Fail |

---

## 5. Section C — Neck-Strength Torture Bars (With-Grain `0°` vs. Cross-Grain `90°`)

Gently flex, twist, and perform thumb-press / drop-test evaluation on each bridge width (`2.0 mm, 2.5 mm, 3.0 mm, 3.5 mm, 4.0 mm, 5.0 mm`):

| Nominal Neck (mm) | `0°` Actual Caliper Width (mm) | `0°` Snap / Survival Rating (1–5) | `90°` Actual Caliper Width (mm) | `90°` Snap / Survival Rating (1–5) | Delamination / Char Notes |
|---:|---:|---|---:|---|---|
| **2.0 mm** | | | | | |
| **2.5 mm** | | | | | |
| **3.0 mm** | | | | | |
| **3.5 mm** | | | | | |
| **4.0 mm** | | | | | |
| **5.0 mm** | | | | | |

*Rating scale: `5` = rigid & unbreakable by normal hand force; `4` = solid slight flex; `3` = flexes noticeably; `2` = cracks under moderate pinch; `1` = breaks during removal/handling.*  
**Minimum Safe Neck Width Verified on This Plywood:** `0°`: _____ mm  /  `90°`: _____ mm

---

## 6. Section D — Interlocking Tab/Socket Fit & Surface Char Evaluation

Test assembly, in-plane shake/lift hold, and disassembly feel across the 5 interlocking pairs:

| Pair ID | Offset Spec | Fit Feel (Loose / Ideal / Tight / Jam) | Lifts Together as Pair? | Edge Char Wipe-Off (Clean / Light / Heavy) | Notes |
|---|---|---|---|---|---|
| **Pair 0** | `0.00 mm` (Common-Line) | | [ ] Yes [ ] No | | |
| **Pair 1** | `+0.05 mm` radial | | [ ] Yes [ ] No | | |
| **Pair 2** | `+0.10 mm` radial | | [ ] Yes [ ] No | | |
| **Pair 3** | `+0.15 mm` radial | | [ ] Yes [ ] No | | |
| **Pair 4** | `+0.20 mm` radial | | [ ] Yes [ ] No | | |

---

## 7. Real 750-Piece Seam Samples Sign-Off (Follow-Up Gate)

Once the 6 real high-stress seam samples (`runs/helleristning-750-commercial/seam_samples/seam_sample_01..06.svg`) are cut from the exact same plywood sheet:

| Sample | Piece Pair / Focus Neck | Intact After Cut? | Assembles Cleanly? | Survives Hand Flex? | Notes |
|---|---|---|---|---|---|
| `seam_sample_01.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_02.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_03.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_04.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_05.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_06.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |

**Supplier Sign-Off:** _______________________  **Producer Sign-Off:** _______________________  **Date:** _____________
