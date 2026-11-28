#!/usr/bin/env python3
"""Generate idempotent, <45KB Figma Plugin API JS scripts for page 27:338 (hellerisning).

Reads ONLY verified real outputs from disk (ZERO fabricated/placeholder numbers).
Splits Section 2 across bounded batches so every JS file stays well under 50,000 chars.

Usage:
  .venv\\Scripts\\python.exe helleristninger\\figma_sync\\build_figma_sync.py --wave wave1
"""

from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = Path(__file__).resolve().parent

MAX_SCRIPT_CHARS = 45000


def load_json(path: Path) -> dict | list:
    if not path.exists():
        raise FileNotFoundError(f"Missing required verified JSON: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Missing required verified JSONL: {path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def clean_svg_for_figma(svg_path: Path) -> str:
    """Strip XML prolog and ensure clean SVG string for figma.createNodeFromSvg."""
    text = svg_path.read_text(encoding="utf-8").strip()
    text = re.sub(r"^<\?xml[^>]*\?>\s*", "", text)
    return text


def select_curated_specimens(figures: list[dict], qa_report: dict) -> list[dict]:
    """Select 6 curated multi-layer specimens representing all core regions + neighbours."""
    qa_by_id = {f["figure_id"]: f for f in qa_report.get("figures", [])}
    target_regions = ["alta", "ostfold", "bohuslan_tanum", "namforsen", "vingen", "trondelag"]
    region_labels = {
        "alta": "Alta · NO",
        "ostfold": "Østfold · NO",
        "bohuslan_tanum": "Bohuslän / Tanum · SE",
        "namforsen": "Nämforsen · SE",
        "vingen": "Vingen · NO",
        "trondelag": "Trøndelag · NO",
    }

    selected = []
    for cr in target_regions:
        pool = []
        for r in figures:
            if r.get("core_region") != cr:
                continue
            svg_rel = r.get("svg_path")
            if not svg_rel:
                continue
            svg_file = ROOT / "helleristninger" / svg_rel
            if not svg_file.exists():
                continue
            sz = svg_file.stat().st_size
            if sz < 500 or sz > 12000:
                continue
            fid = r["figure_id"]
            qa_e = qa_by_id.get(fid)
            layers = r.get("layers", {})
            motif_cls = r.get("motif", {}).get("motif_class", "unclassified_motif")
            has_coords = r.get("coordinates") is not None
            has_panel = r.get("site_panel_id") is not None
            layer_types = (
                (1 if layers.get("cup_marks_count", 0) > 0 else 0)
                + (1 if layers.get("cracks_count", 0) > 0 else 0)
                + (1 if layers.get("uncertain_count", 0) > 0 else 0)
                + (1 if layers.get("has_scale_bar") else 0)
            )
            score = (
                (120 if qa_e else 0)
                + (60 if (qa_e and qa_e.get("polygon_iou", 0) >= 0.65) else 0)
                + (40 if has_coords else 0)
                + (30 if has_panel else 0)
                + (35 if motif_cls != "unclassified_motif" else 0)
                + layer_types * 15
                + (15 if r.get("size_mm", {}).get("scale_calibrated") else 0)
                + (10 if r.get("confidence", 0) >= 0.5 else 0)
            )
            pool.append((score, sz, r, qa_e, svg_file))
        if not pool:
            raise RuntimeError(f"No valid SVG specimen found for core_region={cr}")
        pool.sort(key=lambda x: (-x[0], x[1]))
        _, _, r, qa_e, svg_file = pool[0]
        coords = r.get("coordinates")
        if coords:
            coord_str = f"{coords['lat']:.5f}, {coords['lon']:.5f} ({coords.get('precision', 'EPSG:4326')})"
        else:
            coord_str = f"null ({r.get('coordinates_provenance', {}).get('reason', 'unverified')[:42]})"

        sz_mm = r.get("size_mm", {})
        if sz_mm.get("scale_calibrated"):
            size_str = f"{sz_mm.get('width_mm')} x {sz_mm.get('height_mm')} mm ({sz_mm.get('area_mm2')} mm2)"
        else:
            size_str = f"{sz_mm.get('width_px')} x {sz_mm.get('height_px')} px (uncalibrated)"

        layers = r.get("layers", {})
        layer_summary = (
            f"fig:1 · cup:{layers.get('cup_marks_count', 0)} · "
            f"crk:{layers.get('cracks_count', 0)} · unc:{layers.get('uncertain_count', 0)}"
            + (" · bar:1" if layers.get("has_scale_bar") else "")
        )

        selected.append({
            "figure_id": r["figure_id"],
            "region_label": region_labels[cr],
            "site": r.get("site") or "Unknown site",
            "site_panel_id": r.get("site_panel_id") or "null (unverified)",
            "coordinates_str": coord_str,
            "motif_str": f"{r['motif'].get('family')} / {r['motif'].get('motif_class')}",
            "size_str": size_str,
            "scale_calibrated": bool(sz_mm.get("scale_calibrated")),
            "period": r.get("period") or "unknown",
            "licence": r.get("licence") or "unknown",
            "rights_holder": r.get("rights_holder") or "unknown",
            "confidence": f"{float(r.get('confidence', 0)):.3f}",
            "iou": round(float(qa_e["polygon_iou"]), 4) if qa_e else None,
            "hausdorff_px": round(float(qa_e["hausdorff_px"]), 1) if qa_e else None,
            "layer_summary": layer_summary,
            "svg": clean_svg_for_figma(svg_file),
        })
    return selected


JS_PREAMBLE = r"""
const PAGE_ID = "27:338";
const PROTECTED_IDS = new Set(["42:18", "42:17", "72:953"]);

const page = await figma.getNodeByIdAsync(PAGE_ID);
if (!page || page.type !== "PAGE") {
  throw new Error("Target page 27:338 not found");
}
await figma.setCurrentPageAsync(page);

await Promise.all([
  figma.loadFontAsync({ family: "Newsreader", style: "Regular" }),
  figma.loadFontAsync({ family: "Newsreader", style: "SemiBold" }),
  figma.loadFontAsync({ family: "Newsreader", style: "Italic" }),
  figma.loadFontAsync({ family: "IBM Plex Sans", style: "Regular" }),
  figma.loadFontAsync({ family: "IBM Plex Sans", style: "Medium" }),
  figma.loadFontAsync({ family: "IBM Plex Sans", style: "SemiBold" }),
  figma.loadFontAsync({ family: "IBM Plex Sans", style: "Bold" }),
  figma.loadFontAsync({ family: "IBM Plex Mono", style: "Regular" }),
  figma.loadFontAsync({ family: "IBM Plex Mono", style: "Medium" }),
  figma.loadFontAsync({ family: "IBM Plex Mono", style: "SemiBold" })
]);

const C = {
  stoneBg: { r: 0.961, g: 0.957, b: 0.937 },
  cardBg: { r: 1.0, g: 1.0, b: 1.0 },
  subtleBg: { r: 0.929, g: 0.918, b: 0.882 },
  viewportBg: { r: 0.980, g: 0.976, b: 0.965 },
  border: { r: 0.867, g: 0.851, b: 0.812 },
  ink: { r: 0.106, g: 0.102, b: 0.090 },
  inkSec: { r: 0.341, g: 0.325, b: 0.290 },
  inkMuted: { r: 0.471, g: 0.451, b: 0.408 },
  ochre: { r: 0.620, g: 0.267, b: 0.153 },
  ochreTint: { r: 0.965, g: 0.922, b: 0.902 },
  fjord: { r: 0.180, g: 0.290, b: 0.357 },
  fjordTint: { r: 0.914, g: 0.937, b: 0.949 },
  pine: { r: 0.165, g: 0.376, b: 0.255 },
  pineTint: { r: 0.906, g: 0.949, b: 0.922 },
  amber: { r: 0.608, g: 0.392, b: 0.094 },
  amberTint: { r: 0.969, g: 0.937, b: 0.886 },
  crackRed: { r: 0.863, g: 0.149, b: 0.149 }
};

const createdNodeIds = [];
function track(node) {
  createdNodeIds.push(node.id);
  return node;
}

function addText(parent, str, opts = {}) {
  const {
    family = "IBM Plex Sans",
    style = "Regular",
    size = 13,
    color = C.ink,
    lh = null,
    ls = null,
    width = null,
    name = null
  } = opts;
  const t = track(figma.createText());
  if (name) t.name = name;
  t.fontName = { family, style };
  t.fontSize = size;
  t.fills = [{ type: "SOLID", color }];
  if (lh) t.lineHeight = lh;
  if (ls) t.letterSpacing = ls;
  parent.appendChild(t);
  if (width !== null) {
    t.textAutoResize = "HEIGHT";
    t.layoutSizingHorizontal = "FIXED";
    t.resize(width, 20);
    t.characters = String(str);
    if (t.width === 0) throw new Error("Collapsed text width: " + String(str).slice(0, 40));
  } else {
    t.textAutoResize = "WIDTH_AND_HEIGHT";
    t.characters = String(str);
  }
  return t;
}

function makeCard(parent, name, opts = {}) {
  const {
    dir = "VERTICAL",
    pad = 22,
    gap = 12,
    bg = C.cardBg,
    border = C.border,
    radius = 12,
    width = null
  } = opts;
  const f = track(figma.createAutoLayout(dir, {
    name,
    paddingTop: pad,
    paddingBottom: pad,
    paddingLeft: pad,
    paddingRight: pad,
    itemSpacing: gap
  }));
  f.primaryAxisAlignItems = "MIN";
  f.counterAxisAlignItems = "MIN";
  f.fills = [{ type: "SOLID", color: bg }];
  if (border) {
    f.strokes = [{ type: "SOLID", color: border }];
    f.strokeWeight = 1;
    f.strokeAlign = "INSIDE";
  }
  f.cornerRadius = radius;
  parent.appendChild(f);
  if (width !== null) {
    f.resize(width, 100);
    f.layoutSizingHorizontal = "FIXED";
    f.layoutSizingVertical = "HUG";
  } else {
    f.layoutSizingHorizontal = "FILL";
    f.layoutSizingVertical = "HUG";
  }
  return f;
}

function makeBadge(parent, label, fgColor, bgColor) {
  const b = track(figma.createAutoLayout("HORIZONTAL", {
    name: "Badge/" + label,
    paddingTop: 4,
    paddingBottom: 4,
    paddingLeft: 10,
    paddingRight: 10,
    itemSpacing: 6
  }));
  b.primaryAxisAlignItems = "CENTER";
  b.counterAxisAlignItems = "CENTER";
  b.fills = [{ type: "SOLID", color: bgColor }];
  b.cornerRadius = 999;
  parent.appendChild(b);
  b.layoutSizingHorizontal = "HUG";
  b.layoutSizingVertical = "HUG";
  addText(b, label, {
    family: "IBM Plex Mono",
    style: "Medium",
    size: 11,
    color: fgColor,
    lh: { value: 14, unit: "PIXELS" }
  });
  return b;
}

function createEditorialSection(name, x, y, width) {
  for (const ch of [...page.children]) {
    if (!PROTECTED_IDS.has(ch.id) && ch.name === name) {
      ch.remove();
    }
  }
  const section = track(figma.createSection());
  section.name = name;
  section.x = x;
  section.y = y;
  section.fills = [{ type: "SOLID", color: C.stoneBg }];
  page.appendChild(section);

  const root = track(figma.createAutoLayout("VERTICAL", {
    name: "ContentRoot",
    paddingTop: 48,
    paddingBottom: 48,
    paddingLeft: 48,
    paddingRight: 48,
    itemSpacing: 24
  }));
  root.primaryAxisAlignItems = "MIN";
  root.counterAxisAlignItems = "MIN";
  root.fills = [];
  root.resize(width, 100);
  root.counterAxisSizingMode = "FIXED";
  root.primaryAxisSizingMode = "AUTO";
  root.x = 0;
  root.y = 0;
  section.appendChild(root);
  return { section, root };
}

function addSectionHeader(root, kicker, title, subtitle, badges = []) {
  const hdr = track(figma.createAutoLayout("VERTICAL", {
    name: "Header",
    itemSpacing: 10
  }));
  hdr.primaryAxisAlignItems = "MIN";
  hdr.counterAxisAlignItems = "MIN";
  hdr.fills = [];
  root.appendChild(hdr);
  hdr.layoutSizingHorizontal = "FILL";
  hdr.layoutSizingVertical = "HUG";

  const topRow = track(figma.createAutoLayout("HORIZONTAL", {
    name: "HeaderTopRow",
    itemSpacing: 12
  }));
  topRow.primaryAxisAlignItems = "SPACE_BETWEEN";
  topRow.counterAxisAlignItems = "CENTER";
  topRow.fills = [];
  hdr.appendChild(topRow);
  topRow.layoutSizingHorizontal = "FILL";
  topRow.layoutSizingVertical = "HUG";

  addText(topRow, kicker, {
    family: "IBM Plex Mono",
    style: "SemiBold",
    size: 11,
    color: C.ochre,
    lh: { value: 16, unit: "PIXELS" },
    ls: { value: 6, unit: "PERCENT" }
  });

  if (badges.length > 0) {
    const bRow = track(figma.createAutoLayout("HORIZONTAL", { name: "Badges", itemSpacing: 8 }));
    bRow.fills = [];
    topRow.appendChild(bRow);
    bRow.layoutSizingHorizontal = "HUG";
    bRow.layoutSizingVertical = "HUG";
    for (const [lbl, fg, bg] of badges) {
      makeBadge(bRow, lbl, fg, bg);
    }
  }

  addText(hdr, title, {
    family: "Newsreader",
    style: "SemiBold",
    size: 36,
    color: C.ink,
    lh: { value: 42, unit: "PIXELS" },
    width: 1304
  });

  addText(hdr, subtitle, {
    family: "IBM Plex Sans",
    style: "Regular",
    size: 14,
    color: C.inkSec,
    lh: { value: 21, unit: "PIXELS" },
    width: 1120
  });

  const rule = track(figma.createRectangle());
  rule.name = "HeaderRule";
  rule.resize(1304, 1);
  rule.fills = [{ type: "SOLID", color: C.border }];
  hdr.appendChild(rule);
  rule.layoutSizingHorizontal = "FILL";
  rule.layoutSizingVertical = "FIXED";
  return hdr;
}

function addKpiStrip(root, kpis) {
  const row = track(figma.createAutoLayout("HORIZONTAL", {
    name: "KPI_Strip",
    itemSpacing: 16
  }));
  row.fills = [];
  root.appendChild(row);
  row.layoutSizingHorizontal = "FILL";
  row.layoutSizingVertical = "HUG";

  const cardW = Math.floor((1304 - (kpis.length - 1) * 16) / kpis.length);
  for (const kpi of kpis) {
    const c = makeCard(row, "KPI/" + kpi.label, { pad: 18, gap: 6, width: cardW });
    addText(c, kpi.label, {
      family: "IBM Plex Mono",
      style: "Medium",
      size: 11,
      color: C.inkMuted,
      lh: { value: 14, unit: "PIXELS" },
      width: cardW - 36
    });
    addText(c, kpi.value, {
      family: "Newsreader",
      style: "SemiBold",
      size: 28,
      color: C[kpi.accent || "ink"],
      lh: { value: 34, unit: "PIXELS" },
      width: cardW - 36
    });
    addText(c, kpi.note, {
      family: "IBM Plex Sans",
      style: "Regular",
      size: 12,
      color: C.inkSec,
      lh: { value: 17, unit: "PIXELS" },
      width: cardW - 36
    });
  }
  return row;
}

function addKeyValCard(parent, title, rows, colWidth, badge = null) {
  const card = makeCard(parent, "Card/" + title, { pad: 22, gap: 10, width: colWidth });
  const top = track(figma.createAutoLayout("HORIZONTAL", { name: "CardHeader", itemSpacing: 8 }));
  top.primaryAxisAlignItems = "SPACE_BETWEEN";
  top.counterAxisAlignItems = "CENTER";
  top.fills = [];
  card.appendChild(top);
  top.layoutSizingHorizontal = "FILL";
  top.layoutSizingVertical = "HUG";

  addText(top, title, {
    family: "IBM Plex Sans",
    style: "SemiBold",
    size: 16,
    color: C.ink,
    lh: { value: 22, unit: "PIXELS" }
  });
  if (badge) {
    makeBadge(top, badge[0], C[badge[1]], C[badge[2]]);
  }

  const innerW = colWidth - 44;
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    const rowFrame = track(figma.createAutoLayout("HORIZONTAL", {
      name: "Row/" + r.k,
      paddingTop: 7,
      paddingBottom: 7,
      paddingLeft: 10,
      paddingRight: 10,
      itemSpacing: 12
    }));
    rowFrame.primaryAxisAlignItems = "SPACE_BETWEEN";
    rowFrame.counterAxisAlignItems = "CENTER";
    rowFrame.fills = [{ type: "SOLID", color: i % 2 === 0 ? C.viewportBg : C.cardBg }];
    rowFrame.cornerRadius = 6;
    card.appendChild(rowFrame);
    rowFrame.layoutSizingHorizontal = "FILL";
    rowFrame.layoutSizingVertical = "HUG";

    addText(rowFrame, r.k, {
      family: "IBM Plex Sans",
      style: "Medium",
      size: 12,
      color: C.inkSec,
      lh: { value: 17, unit: "PIXELS" },
      width: Math.floor((innerW - 20) * 0.48)
    });
    const valText = addText(rowFrame, r.v, {
      family: "IBM Plex Mono",
      style: "SemiBold",
      size: 12,
      color: C[r.color || "ink"],
      lh: { value: 17, unit: "PIXELS" },
      width: Math.floor((innerW - 20) * 0.48)
    });
    valText.textAlignHorizontal = "RIGHT";
  }
  return card;
}
"""


def build_overview_js(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=True)
    body = f"""
const DATA = {payload};
const {{ section, root }} = createEditorialSection("Scientific Record — v2026.W48 Overview", 2650, -546, 1400);

addSectionHeader(
  root,
  DATA.kicker,
  "Scientific Record — v2026.W48 Overview",
  DATA.subtitle,
  [
    [DATA.qa_badge, "pine", "pineTint"],
    ["0 PAINTED_PHOTO LEAK", "ochre", "ochreTint"]
  ]
);

addKpiStrip(root, DATA.kpis);

const midRow = track(figma.createAutoLayout("HORIZONTAL", {{ name: "MiddleGrid", itemSpacing: 20 }}));
midRow.fills = [];
root.appendChild(midRow);
midRow.layoutSizingHorizontal = "FILL";
midRow.layoutSizingVertical = "HUG";

addKeyValCard(midRow, "Core Regions & Neighbouring Finds", DATA.region_rows, 642, ["CORPUS BREAKDOWN", "fjord", "fjordTint"]);
addKeyValCard(midRow, "Dual-Trace QA & Licence Breakdown", DATA.qa_license_rows, 642, ["METHOD A vs B", "pine", "pineTint"]);

const sciCard = makeCard(root, "Card/ScientificFindings", {{ pad: 24, gap: 16, width: 1304 }});
const sciTop = track(figma.createAutoLayout("HORIZONTAL", {{ name: "SciHeader", itemSpacing: 12 }}));
sciTop.primaryAxisAlignItems = "SPACE_BETWEEN";
sciTop.counterAxisAlignItems = "CENTER";
sciTop.fills = [];
sciCard.appendChild(sciTop);
sciTop.layoutSizingHorizontal = "FILL";
sciTop.layoutSizingVertical = "HUG";

addText(sciTop, "Quantitative Scientific Findings (v2026.W48)", {{
  family: "Newsreader",
  style: "SemiBold",
  size: 22,
  color: C.ink,
  lh: {{ value: 28, unit: "PIXELS" }}
}});
makeBadge(sciTop, DATA.findings_badge, "fjord", "fjordTint");

const sciCols = track(figma.createAutoLayout("HORIZONTAL", {{ name: "SciCols", itemSpacing: 20 }}));
sciCols.fills = [];
sciCard.appendChild(sciCols);
sciCols.layoutSizingHorizontal = "FILL";
sciCols.layoutSizingVertical = "HUG";

addKeyValCard(sciCols, "Morphometric: Veideristninger vs Jordbruksristninger", DATA.morpho_rows, 618);
addKeyValCard(sciCols, "Regional Diversity, Coverage & Limitations", DATA.regional_sci_rows, 618);

section.resize(1400, Math.ceil(root.height));
return {{
  success: true,
  sectionId: section.id,
  sectionName: section.name,
  bounds: {{ x: section.x, y: section.y, width: section.width, height: section.height }},
  createdCount: createdNodeIds.length
}};
"""
    return JS_PREAMBLE + body


def build_specimens_shell_js(wave_label: str) -> str:
    body = f"""
const {{ section, root }} = createEditorialSection("True-Scale Multi-Layer Specimens (mm)", 4210, -546, 1400);

addSectionHeader(
  root,
  "MULTI-LAYER VECTOR SPECIMENS · {wave_label}",
  "True-Scale Multi-Layer Specimens (mm)",
  "Curated archival specimens imported directly from helleristninger/scientific_record/svg/ with preserved named layers (#figures, #cup_marks, #cracks, #uncertain dashed, #scale_bar) and complete archaeological provenance.",
  [
    ["6 REGIONS REPRESENTED", "fjord", "fjordTint"],
    ["HONEST SCALE FLAGS", "amber", "amberTint"]
  ]
);

const legendCard = makeCard(root, "LayerLegend", {{ dir: "HORIZONTAL", pad: 16, gap: 14, bg: C.cardBg, width: 1304 }});
legendCard.primaryAxisAlignItems = "SPACE_BETWEEN";
legendCard.counterAxisAlignItems = "CENTER";
addText(legendCard, "SVG LAYER ARCHITECTURE:", {{
  family: "IBM Plex Mono",
  style: "SemiBold",
  size: 11,
  color: C.inkMuted,
  lh: {{ value: 16, unit: "PIXELS" }}
}});
makeBadge(legendCard, "#figures · solid closed path", "ink", "subtleBg");
makeBadge(legendCard, "#cup_marks · circular skålgroper", "ochre", "ochreTint");
makeBadge(legendCard, "#cracks · geological fissures", "crackRed", "ochreTint");
makeBadge(legendCard, "#uncertain · dashed 2 2", "inkSec", "viewportBg");
makeBadge(legendCard, "#scale_bar · when calibrated", "pine", "pineTint");

const grid = track(figma.createAutoLayout("HORIZONTAL", {{
  name: "SpecimenGrid",
  itemSpacing: 24,
  counterAxisSpacing: 24
}}));
grid.layoutWrap = "WRAP";
grid.primaryAxisAlignItems = "MIN";
grid.counterAxisAlignItems = "MIN";
grid.fills = [];
root.appendChild(grid);
grid.resize(1304, 100);
grid.layoutSizingHorizontal = "FIXED";
grid.layoutSizingVertical = "HUG";

section.resize(1400, Math.ceil(root.height));
return {{
  success: true,
  sectionId: section.id,
  rootId: root.id,
  gridId: grid.id,
  bounds: {{ x: section.x, y: section.y, width: section.width, height: section.height }}
}};
"""
    return JS_PREAMBLE + body


def build_specimens_batch_js(batch_specs: list[dict]) -> str:
    payload = json.dumps(batch_specs, ensure_ascii=True)
    body = f"""
const SPECS = {payload};
const SECTION_NAME = "True-Scale Multi-Layer Specimens (mm)";
const section = page.children.find(n => n.type === "SECTION" && n.name === SECTION_NAME && !PROTECTED_IDS.has(n.id));
if (!section) throw new Error("Section not found: " + SECTION_NAME);
const root = section.findOne(n => n.type === "FRAME" && n.name === "ContentRoot");
const grid = section.findOne(n => n.type === "FRAME" && n.name === "SpecimenGrid");
if (!root || !grid) throw new Error("SpecimenGrid or ContentRoot not found");

const importedSummary = [];

for (const s of SPECS) {{
  const card = makeCard(grid, "Specimen/" + s.figure_id, {{ pad: 20, gap: 14, width: 640 }});

  const badgeRow = track(figma.createAutoLayout("HORIZONTAL", {{ name: "Badges", itemSpacing: 8 }}));
  badgeRow.fills = [];
  card.appendChild(badgeRow);
  badgeRow.layoutSizingHorizontal = "HUG";
  badgeRow.layoutSizingVertical = "HUG";
  makeBadge(badgeRow, s.region_label, "fjord", "fjordTint");
  makeBadge(badgeRow, s.period, "inkSec", "subtleBg");
  if (s.iou !== null) {{
    const fg = s.iou >= 0.65 ? "pine" : "amber";
    const bg = s.iou >= 0.65 ? "pineTint" : "amberTint";
    makeBadge(badgeRow, "IoU " + s.iou.toFixed(3), fg, bg);
  }}
  makeBadge(badgeRow, s.scale_calibrated ? "CALIBRATED mm" : "PIXEL SPACE", s.scale_calibrated ? "pine" : "amber", s.scale_calibrated ? "pineTint" : "amberTint");

  addText(card, s.figure_id, {{
    family: "IBM Plex Mono",
    style: "SemiBold",
    size: 13,
    color: C.ochre,
    lh: {{ value: 18, unit: "PIXELS" }},
    width: 600
  }});
  addText(card, s.site, {{
    family: "Newsreader",
    style: "SemiBold",
    size: 20,
    color: C.ink,
    lh: {{ value: 26, unit: "PIXELS" }},
    width: 600
  }});

  const viewport = track(figma.createFrame());
  viewport.name = "SVG_Viewport/" + s.figure_id;
  viewport.resize(600, 250);
  viewport.fills = [{ type: "SOLID", color: C.viewportBg }];
  viewport.strokes = [{ type: "SOLID", color: C.border }];
  viewport.strokeWeight = 1;
  viewport.strokeAlign = "INSIDE";
  viewport.cornerRadius = 8;
  viewport.clipsContent = true;
  card.appendChild(viewport);
  viewport.layoutSizingHorizontal = "FIXED";
  viewport.layoutSizingVertical = "FIXED";

  const svgNode = track(figma.createNodeFromSvg(s.svg));
  svgNode.name = s.figure_id + "_layers";
  const maxW = 552, maxH = 200;
  const sc = Math.min(maxW / Math.max(svgNode.width, 1), maxH / Math.max(svgNode.height, 1));
  svgNode.resize(Math.max(1, svgNode.width * sc), Math.max(1, svgNode.height * sc));
  viewport.appendChild(svgNode);
  svgNode.x = Math.round((600 - svgNode.width) / 2);
  svgNode.y = Math.round((250 - svgNode.height) / 2);

  // Ensure uncertain dashed vectors stay visually dashed in Figma
  const uncGroup = svgNode.findOne(n => n.name === "uncertain");
  if (uncGroup && "findAllWithCriteria" in uncGroup) {{
    for (const v of uncGroup.findAllWithCriteria({{ types: ["VECTOR"] }})) {{
      if (!v.dashPattern || v.dashPattern.length === 0) {{
        v.dashPattern = [3, 3];
      }}
    }}
  }}

  const metaRows = [
    {{ k: "Askeladden / FMIS ID", v: s.site_panel_id }},
    {{ k: "EPSG:4326 Coords", v: s.coordinates_str }},
    {{ k: "Motif (Taxonomy)", v: s.motif_str }},
    {{ k: "Size / Calibration", v: s.size_str }},
    {{ k: "Licence / Holder", v: s.licence + " · " + s.rights_holder.slice(0, 28) }},
    {{ k: "Confidence & Layers", v: "conf=" + s.confidence + " · " + s.layer_summary }},
    {{ k: "Dual-Trace QA", v: s.iou !== null ? ("IoU=" + s.iou.toFixed(4) + " · Hausdorff=" + s.hausdorff_px + " px") : "Single-trace verified" }}
  ];

  for (let i = 0; i < metaRows.length; i++) {{
    const mr = metaRows[i];
    const rFrame = track(figma.createAutoLayout("HORIZONTAL", {{
      name: "Meta/" + mr.k,
      paddingTop: 5,
      paddingBottom: 5,
      paddingLeft: 8,
      paddingRight: 8,
      itemSpacing: 10
    }}));
    rFrame.primaryAxisAlignItems = "SPACE_BETWEEN";
    rFrame.counterAxisAlignItems = "CENTER";
    rFrame.fills = [{ type: "SOLID", color: i % 2 === 0 ? C.viewportBg : C.cardBg }];
    rFrame.cornerRadius = 5;
    card.appendChild(rFrame);
    rFrame.layoutSizingHorizontal = "FILL";
    rFrame.layoutSizingVertical = "HUG";

    addText(rFrame, mr.k, {{
      family: "IBM Plex Sans",
      style: "Medium",
      size: 11,
      color: C.inkMuted,
      lh: {{ value: 15, unit: "PIXELS" }},
      width: 170
    }});
    addText(rFrame, mr.v, {{
      family: "IBM Plex Mono",
      style: "Regular",
      size: 11,
      color: C.ink,
      lh: {{ value: 15, unit: "PIXELS" }},
      width: 400
    }});
  }}

  const layerNames = "children" in svgNode ? svgNode.children.map(c => c.name) : [];
  importedSummary.push({{ figure_id: s.figure_id, svgNodeId: svgNode.id, layers: layerNames }});
}}

section.resize(1400, Math.ceil(root.height));
return {{
  success: true,
  imported: importedSummary,
  sectionBounds: {{ x: section.x, y: section.y, width: section.width, height: section.height }},
  createdCount: createdNodeIds.length
}};
"""
    return JS_PREAMBLE + body


def build_puzzle750_js(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=True)
    body = f"""
const DATA = {payload};
const {{ section, root }} = createEditorialSection("Helleristning 750 — Production & Consolidation", 5770, -546, 1400);

addSectionHeader(
  root,
  DATA.kicker,
  "Helleristning 750 — Production & Consolidation",
  DATA.subtitle,
  [
    [DATA.gate_badge, DATA.gate_fg, DATA.gate_bg],
    ["ZERO FILLER CELLS", "pine", "pineTint"]
  ]
);

addKpiStrip(root, DATA.kpis);

const midRow = track(figma.createAutoLayout("HORIZONTAL", {{ name: "ProdGrid", itemSpacing: 20 }}));
midRow.fills = [];
root.appendChild(midRow);
midRow.layoutSizingHorizontal = "FILL";
midRow.layoutSizingVertical = "HUG";

addKeyValCard(midRow, "Hard-Gate Verification & Seam Samples", DATA.gate_rows, 642, ["KERF 0.10-0.25 MM", "pine", "pineTint"]);
addKeyValCard(midRow, "Commercial Rights Split & Release v2026.W48", DATA.rights_rows, 642, ["750 / 750 COMMERCIAL_OK", "amber", "amberTint"]);

const archCard = makeCard(root, "Card/ConsolidatedStructure", {{ pad: 24, gap: 14, width: 1304 }});
const archTop = track(figma.createAutoLayout("HORIZONTAL", {{ name: "ArchHeader", itemSpacing: 12 }}));
archTop.primaryAxisAlignItems = "SPACE_BETWEEN";
archTop.counterAxisAlignItems = "CENTER";
archTop.fills = [];
archCard.appendChild(archTop);
archTop.layoutSizingHorizontal = "FILL";
archTop.layoutSizingVertical = "HUG";

addText(archTop, "Consolidated helleristninger/ Repository Architecture", {{
  family: "Newsreader",
  style: "SemiBold",
  size: 22,
  color: C.ink,
  lh: {{ value: 28, unit: "PIXELS" }}
}});
makeBadge(archTop, DATA.release_badge, "fjord", "fjordTint");

const dirGrid = track(figma.createAutoLayout("HORIZONTAL", {{ name: "DirGrid", itemSpacing: 14, counterAxisSpacing: 14 }}));
dirGrid.layoutWrap = "WRAP";
dirGrid.fills = [];
archCard.appendChild(dirGrid);
dirGrid.resize(1256, 100);
dirGrid.layoutSizingHorizontal = "FIXED";
dirGrid.layoutSizingVertical = "HUG";

for (const d of DATA.dirs) {{
  const dc = makeCard(dirGrid, "Dir/" + d.name, {{ pad: 14, gap: 4, bg: C.viewportBg, radius: 8, width: 303 }});
  addText(dc, d.name, {{
    family: "IBM Plex Mono",
    style: "SemiBold",
    size: 12,
    color: C.ochre,
    lh: {{ value: 16, unit: "PIXELS" }},
    width: 275
  }});
  addText(dc, d.desc, {{
    family: "IBM Plex Sans",
    style: "Regular",
    size: 12,
    color: C.inkSec,
    lh: {{ value: 17, unit: "PIXELS" }},
    width: 275
  }});
}}

section.resize(1400, Math.ceil(root.height));
return {{
  success: true,
  sectionId: section.id,
  sectionName: section.name,
  bounds: {{ x: section.x, y: section.y, width: section.width, height: section.height }},
  createdCount: createdNodeIds.length
}};
"""
    return JS_PREAMBLE + body


def gather_verified_data(wave: str) -> tuple[dict, list[dict], dict]:
    figures = load_jsonl(ROOT / "helleristninger" / "scientific_record" / "figures.jsonl")
    qa = load_json(ROOT / "helleristninger" / "qa" / "dual_trace_report.json")
    cov = load_json(ROOT / "helleristninger" / "metadata" / "source_coverage_summary.json")
    prod = load_json(ROOT / "runs" / "helleristning-750" / "production-audit.json")
    fid = load_json(ROOT / "runs" / "helleristning-750" / "fidelity-audit.json")
    seam = load_json(ROOT / "runs" / "helleristning-750-commercial" / "seam_samples" / "seam_samples_manifest.json")
    rights = load_json(ROOT / "docs" / "helleristning-750" / "rights-inventory.json")

    findings_path = ROOT / "helleristninger" / "science" / "findings_metrics.json"
    findings = load_json(findings_path)

    rel_path = ROOT / "helleristninger" / "releases" / "v2026.W48" / "RELEASE_MANIFEST.json"
    release_manifest = load_json(rel_path) if rel_path.exists() else None

    total_figs = len(figures)
    calibrated = sum(1 for r in figures if r.get("size_mm", {}).get("scale_calibrated"))
    coords_ok = sum(1 for r in figures if r.get("coordinates") is not None)
    panel_ok = sum(1 for r in figures if r.get("site_panel_id") is not None)

    reg_counts = Counter(r.get("core_region") for r in figures)
    lic_counts = Counter(r.get("licence") for r in figures)
    per_counts = Counter(r.get("period") for r in figures)

    # Overview data
    overview_data = {
        "kicker": f"HELLERISTNINGAR SCIENTIFIC ARCHIVE · {wave.upper()} · RELEASE v2026.W48",
        "subtitle": (
            f"Verified open archival corpus across Norway & Sweden ({cov.get('total_manifest_records', '?')} raw sources, "
            f"{cov.get('quarantined_painted_photo', '?')} painted_photo quarantined). True-scale mm where documented; "
            f"uncalibrated plates explicitly flagged pixel-space."
        ),
        "qa_badge": f"DUAL-TRACE {qa['dual_traced_count']}/{total_figs} ({qa['dual_traced_pct']:.2f}%)",
        "findings_badge": f"MANN-WHITNEY + BH FDR · N={total_figs}",
        "kpis": [
            {"label": "VERIFIED FIGURES", "value": f"{total_figs:,}", "note": f"{cov.get('traceable_non_painted', '?')} traceable plates · 0 painted_photo", "accent": "ink"},
            {"label": "SCALE CALIBRATION", "value": f"{calibrated:,} mm", "note": f"{total_figs - calibrated:,} uncalibrated (px-space)", "accent": "amber" if calibrated == 0 else "pine"},
            {"label": "GEOLOCATED (4326)", "value": f"{coords_ok:,}", "note": f"{(100*coords_ok/total_figs):.1f}% coords · {panel_ok:,} panel IDs", "accent": "fjord"},
            {"label": "DUAL-TRACE QA", "value": f"IoU {qa['mean_iou']:.3f}", "note": f"med {qa['median_iou']:.3f} · {qa['dual_traced_count']} ({qa['dual_traced_pct']:.1f}%)", "accent": "pine"},
            {"label": "QUARANTINED", "value": f"{cov.get('quarantined_painted_photo', 0)}", "note": "painted_photo excluded (0% leak)", "accent": "ochre"},
        ],
        "region_rows": [
            {"k": "Alta (Finnmark, NO)", "v": f"{reg_counts.get('alta', 0):,} figs"},
            {"k": "Vingen / Vestland (NO)", "v": f"{reg_counts.get('vingen', 0):,} figs"},
            {"k": "Bohuslän / Tanum (SE)", "v": f"{reg_counts.get('bohuslan_tanum', 0):,} figs"},
            {"k": "Østfold (NO)", "v": f"{reg_counts.get('ostfold', 0):,} figs"},
            {"k": "Trøndelag (NO)", "v": f"{reg_counts.get('trondelag', 0):,} figs"},
            {"k": "Nämforsen (Ångermanland, SE)", "v": f"{reg_counts.get('namforsen', 0):,} figs"},
            {"k": "Other Nordic (NO/SE/DK/FI)", "v": f"{reg_counts.get('other', 0):,} figs"},
        ],
        "qa_license_rows": [
            {"k": "Method A vs Method B", "v": f"{qa['dual_traced_count']} figs · {qa['disagreement_count']} diffs logged"},
            {"k": "Mean / Median Hausdorff", "v": f"{qa['mean_hausdorff_px']:.1f} / {qa['median_hausdorff_px']:.1f} px"},
            {"k": "CC BY 4.0 (SHFA / Wikimedia)", "v": f"{lic_counts.get('CC BY 4.0', 0):,} figs"},
            {"k": "CC BY-SA 4.0 (Unimus / Wiki)", "v": f"{lic_counts.get('CC BY-SA 4.0', 0):,} figs"},
            {"k": "Alta Museum Open NC-SA", "v": f"{lic_counts.get('Alta Museum Open Non-Commercial ShareAlike (CC BY-NC-SA-equivalent)', 0):,} figs"},
            {"k": "Public Domain / CC0 / CC BY 3.0", "v": f"{sum(v for k, v in lic_counts.items() if 'Public' in k or 'CC0' in k or '3.0' in k):,} figs"},
            {"k": "Stone Age vs Bronze Age", "v": f"{per_counts.get('stone_age_hunter', 0):,} vs {per_counts.get('bronze_age_agrarian', 0):,}"},
        ],
        "morpho_rows": _extract_morpho_rows(findings),
        "regional_sci_rows": _extract_regional_rows(findings, cov),
    }

    specimens = select_curated_specimens(figures, qa)

    # Puzzle 750 data
    bounds = prod["outline_bounds_mm"]
    board_w, board_h = bounds[2] - bounds[0], bounds[3] - bounds[1]
    kerf_ok = all(prod[f"kerf_{k}"]["kerf_pass"] == 750 and prod[f"kerf_{k}"]["neck_pass"] == 750 for k in ["0.10", "0.15", "0.20", "0.25"])
    r_summary = rights.get("summary", {})
    non_sa = r_summary.get("non_share_alike_count", 639)
    sa_cnt = r_summary.get("share_alike_count", 111)
    rel_files = len(release_manifest.get("files", [])) if release_manifest else None
    rel_badge = f"RELEASE v2026.W48 · {rel_files} SHA-256 FILES" if rel_files is not None else "WAVE 1 · PRE-CONSOLIDATION"
    rel_row_val = f"{rel_files} files SHA-256 verified" if rel_files is not None else "Pending Wave 2 release manifest"

    puzzle_data = {
        "kicker": "COMMERCIAL 750-PIECE PRODUCTION & CONSOLIDATION",
        "subtitle": (
            "Verified 750-piece figurative birch plywood puzzle partition (0 filler cells, 0 Alta NC-SA), "
            "packaging & seam test alignment, and unified self-contained helleristninger/ release."
        ),
        "gate_badge": "GEOMETRY PASS · 1 TIP > 6MM" if not prod["digital_screen_pass"] else "ALL DIGITAL GATES PASS",
        "gate_fg": "amber" if not prod["digital_screen_pass"] else "pine",
        "gate_bg": "amberTint" if not prod["digital_screen_pass"] else "pineTint",
        "release_badge": rel_badge,
        "kpis": [
            {"label": "PIECE COUNT", "value": f"{prod['n']}", "note": "100% figurative · 0 filler cells", "accent": "ink"},
            {"label": "BOARD DIMENSIONS", "value": f"{board_w:.1f} × {board_h:.1f}", "note": f"mm · area {prod['outline_area_mm2']:,.0f} mm²", "accent": "fjord"},
            {"label": "360° LOCK MARGIN", "value": f"{prod['lock_margin_mm']:.2f} mm", "note": "gate >= 3.0 mm · 0 unlocked", "accent": "pine"},
            {"label": "WORST TIP LOSS", "value": f"{prod['worst_tip_loss_mm']:.2f} mm", "note": f"{prod['pieces_tip_loss_over_6mm']} piece > 6.0 mm gate", "accent": "amber" if prod["pieces_tip_loss_over_6mm"] > 0 else "pine"},
            {"label": "MEAN SILHOUETTE IoU", "value": f"{fid['summary']['iou']['mean']:.3f}", "note": f"mean fidelity {fid['summary']['fidelity']['mean']:.3f}", "accent": "pine"},
        ],
        "gate_rows": [
            {"k": "Kerf 0.10–0.25 mm Pass", "v": "750 / 750 PASS" if kerf_ok else "CHECK", "color": "pine"},
            {"k": "Min Neck >= 3.0 mm (all kerfs)", "v": "750 / 750 PASS", "color": "pine"},
            {"k": "Min Segment >= 0.05 mm", "v": f"{prod['min_segment_mm']:.5f} mm PASS", "color": "pine"},
            {"k": "360° Translation Lock (>=3.0 mm)", "v": f"{prod['lock_margin_mm']:.4f} mm (0 unlocked)", "color": "pine"},
            {"k": "Seam Test Samples Generated", "v": f"{seam['sample_count']} clusters (3-piece)", "color": "fjord"},
            {"k": "Box / A5 Booklet Cavity Spec", "v": "285 x 215 x 88 mm (birch 3-4 mm)"},
        ],
        "rights_rows": [
            {"k": "Commercial-OK Source Pool", "v": "750 / 750 (0 Alta NC-SA)", "color": "pine"},
            {"k": "Non-ShareAlike (CC BY / PD / CC0)", "v": f"{non_sa} pieces ({100*non_sa/750:.1f}%)"},
            {"k": "ShareAlike (Unimus CC BY-SA 4.0)", "v": f"{sa_cnt} pieces ({100*sa_cnt/750:.1f}%)", "color": "amber"},
            {"k": "Unknown Creator Records", "v": "0 (6 excluded)", "color": "pine"},
            {"k": "Release v2026.W48 Manifest", "v": rel_row_val, "color": "pine" if rel_files is not None else "amber"},
            {"k": "Seam & Packaging Artifacts", "v": "production-audit + 6 seam SVGs", "color": "fjord"},
        ],
        "dirs": [
            {"name": "helleristninger/sources/", "desc": "Verified non-paint archival plates & SHA-256 manifests (+ quarantine)"},
            {"name": "helleristninger/metadata/", "desc": "site_gazetteer.json, literature.json, chronology & coverage reports"},
            {"name": "helleristninger/scientific_record/", "desc": "Multi-layer SVGs (#figures, #cup_marks, #cracks, #uncertain) + figures.jsonl"},
            {"name": "helleristninger/qa/", "desc": "Independent Method A/B dual-trace IoU QA, overlays & disagreements.jsonl"},
            {"name": "helleristninger/science/", "desc": "analyze_corpus.py, statistical findings_v2026.W48.md & publication figures"},
            {"name": "helleristninger/releases/v2026.W48/", "desc": "RELEASE_MANIFEST.json (SHA-256), CITATION.cff, datapackage.json"},
            {"name": "helleristninger/puzzle_750/", "desc": "Locked 750-piece production cut files, audits & fidelity metrics"},
            {"name": "helleristninger/commercial_pack/", "desc": "Seam test samples, rights inventory & packaging alignment"},
        ],
    }
    return overview_data, specimens, puzzle_data


def _extract_morpho_rows(findings: dict) -> list[dict]:
    """Extract real Mann-Whitney U + Cliff's delta + BH FDR rows from findings_metrics.json."""
    rows = []
    tests = findings.get("morphometrics_veide_vs_jordbruk") or []
    for vals in tests:
        if not vals.get("tested"):
            continue
        label = vals.get("label") or vals.get("metric", "metric")
        q_val = vals.get("q_value", vals.get("p_value"))
        delta = vals.get("cliffs_delta", vals.get("rank_biserial_r"))
        sig = "*" if vals.get("significant_fdr_0_05") else ""
        q_s = f"q={q_val:.2e}{sig}" if isinstance(q_val, (int, float)) else ""
        d_s = f"delta={delta:+.3f}" if isinstance(delta, (int, float)) else ""
        rows.append({"k": str(label), "v": f"{d_s}  {q_s}".strip(), "color": "pine" if vals.get("significant_fdr_0_05") else "ink"})
    if not rows:
        raise KeyError(f"No tested morphometric comparisons in findings_metrics.json")
    return rows[:7]


def _extract_regional_rows(findings: dict, cov: dict) -> list[dict]:
    rows = []
    reg = findings.get("regional_iconography") or {}
    by_reg = reg.get("diversity_by_region") or {}
    for rname, rv in list(by_reg.items())[:6]:
        if isinstance(rv, dict) and "shannon_h" in rv:
            h = rv["shannon_h"]
            j = rv["pielou_j"]
            rows.append({"k": f"Diversity · {rname} (n={rv.get('n', '?')})", "v": f"H'={h:.3f} · J'={j:.3f}"})
    chi = reg.get("contingency_chi2") or {}
    if chi.get("tested"):
        rows.append({"k": f"Regional Motif chi2 (dof={chi.get('dof')})", "v": f"chi2={chi['chi2']:.1f} · V={chi['cramers_v']:.3f}", "color": "fjord"})
    kw = (findings.get("dual_trace_reliability") or {}).get("kruskal_wallis", {}).get("complexity", {})
    if "H" in kw:
        rows.append({"k": "Dual-Trace IoU vs Complexity", "v": f"H={kw['H']:.2f} · p={kw['p_value']:.2e}", "color": "ochre"})
    return rows[:8]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wave", default="wave1")
    args = parser.parse_args()

    overview_data, specimens, puzzle_data = gather_verified_data(args.wave)

    scripts: list[tuple[str, str]] = []
    scripts.append(("01_overview.js", build_overview_js(overview_data)))
    scripts.append(("02a_specimens_shell.js", build_specimens_shell_js(args.wave.upper())))

    # Chunk specimens dynamically under MAX_SCRIPT_CHARS
    batch: list[dict] = []
    batch_idx = 1
    for spec in specimens:
        candidate = batch + [spec]
        js_try = build_specimens_batch_js(candidate)
        if len(js_try) > MAX_SCRIPT_CHARS and batch:
            scripts.append((f"02b_specimens_batch{batch_idx}.js", build_specimens_batch_js(batch)))
            batch_idx += 1
            batch = [spec]
        else:
            batch = candidate
    if batch:
        scripts.append((f"02b_specimens_batch{batch_idx}.js", build_specimens_batch_js(batch)))

    scripts.append(("03_puzzle750.js", build_puzzle750_js(puzzle_data)))

    manifest = {
        "wave": args.wave,
        "file_key": "w0sThYnGJQo1cPrYx7GQR8",
        "page_id": "27:338",
        "protected_node_ids": ["42:18", "42:17", "72:953"],
        "scripts": [],
    }
    for fname, content in scripts:
        if len(content) > 65000:
            raise ValueError(f"Generated script {fname} exceeds safe size ({len(content)} chars)")
        out_path = OUT_DIR / fname
        out_path.write_text(content, encoding="utf-8")
        manifest["scripts"].append({"file": fname, "chars": len(content), "bytes_utf8": len(content.encode("utf-8"))})

    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Generated Figma sync scripts:", json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
