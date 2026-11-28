
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

const DATA = {"kicker": "COMMERCIAL 750-PIECE PRODUCTION & CONSOLIDATION", "subtitle": "Verified 750-piece figurative birch plywood puzzle partition (0 filler cells, 0 Alta NC-SA), packaging & seam test alignment, and unified self-contained helleristninger/ release.", "gate_badge": "GEOMETRY PASS \u00b7 1 TIP > 6MM", "gate_fg": "amber", "gate_bg": "amberTint", "release_badge": "WAVE 1 \u00b7 PRE-CONSOLIDATION", "kpis": [{"label": "PIECE COUNT", "value": "750", "note": "100% figurative \u00b7 0 filler cells", "accent": "ink"}, {"label": "BOARD DIMENSIONS", "value": "970.6 \u00d7 680.4", "note": "mm \u00b7 area 438,190 mm\u00b2", "accent": "fjord"}, {"label": "360\u00b0 LOCK MARGIN", "value": "3.42 mm", "note": "gate >= 3.0 mm \u00b7 0 unlocked", "accent": "pine"}, {"label": "WORST TIP LOSS", "value": "6.42 mm", "note": "1 piece > 6.0 mm gate", "accent": "amber"}, {"label": "MEAN SILHOUETTE IoU", "value": "0.721", "note": "mean fidelity 0.767", "accent": "pine"}], "gate_rows": [{"k": "Kerf 0.10\u20130.25 mm Pass", "v": "750 / 750 PASS", "color": "pine"}, {"k": "Min Neck >= 3.0 mm (all kerfs)", "v": "750 / 750 PASS", "color": "pine"}, {"k": "Min Segment >= 0.05 mm", "v": "0.06037 mm PASS", "color": "pine"}, {"k": "360\u00b0 Translation Lock (>=3.0 mm)", "v": "3.4219 mm (0 unlocked)", "color": "pine"}, {"k": "Seam Test Samples Generated", "v": "6 clusters (3-piece)", "color": "fjord"}, {"k": "Box / A5 Booklet Cavity Spec", "v": "285 x 215 x 88 mm (birch 3-4 mm)"}], "rights_rows": [{"k": "Commercial-OK Source Pool", "v": "750 / 750 (0 Alta NC-SA)", "color": "pine"}, {"k": "Non-ShareAlike (CC BY / PD / CC0)", "v": "639 pieces (85.2%)"}, {"k": "ShareAlike (Unimus CC BY-SA 4.0)", "v": "111 pieces (14.8%)", "color": "amber"}, {"k": "Unknown Creator Records", "v": "0 (6 excluded)", "color": "pine"}, {"k": "Release v2026.W48 Manifest", "v": "Pending Wave 2 release manifest", "color": "amber"}, {"k": "Seam & Packaging Artifacts", "v": "production-audit + 6 seam SVGs", "color": "fjord"}], "dirs": [{"name": "helleristninger/sources/", "desc": "Verified non-paint archival plates & SHA-256 manifests (+ quarantine)"}, {"name": "helleristninger/metadata/", "desc": "site_gazetteer.json, literature.json, chronology & coverage reports"}, {"name": "helleristninger/scientific_record/", "desc": "Multi-layer SVGs (#figures, #cup_marks, #cracks, #uncertain) + figures.jsonl"}, {"name": "helleristninger/qa/", "desc": "Independent Method A/B dual-trace IoU QA, overlays & disagreements.jsonl"}, {"name": "helleristninger/science/", "desc": "analyze_corpus.py, statistical findings_v2026.W48.md & publication figures"}, {"name": "helleristninger/releases/v2026.W48/", "desc": "RELEASE_MANIFEST.json (SHA-256), CITATION.cff, datapackage.json"}, {"name": "helleristninger/puzzle_750/", "desc": "Locked 750-piece production cut files, audits & fidelity metrics"}, {"name": "helleristninger/commercial_pack/", "desc": "Seam test samples, rights inventory & packaging alignment"}]};
const { section, root } = createEditorialSection("Helleristning 750 — Production & Consolidation", 5770, -546, 1400);

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

const midRow = track(figma.createAutoLayout("HORIZONTAL", { name: "ProdGrid", itemSpacing: 20 }));
midRow.fills = [];
root.appendChild(midRow);
midRow.layoutSizingHorizontal = "FILL";
midRow.layoutSizingVertical = "HUG";

addKeyValCard(midRow, "Hard-Gate Verification & Seam Samples", DATA.gate_rows, 642, ["KERF 0.10-0.25 MM", "pine", "pineTint"]);
addKeyValCard(midRow, "Commercial Rights Split & Release v2026.W48", DATA.rights_rows, 642, ["750 / 750 COMMERCIAL_OK", "amber", "amberTint"]);

const archCard = makeCard(root, "Card/ConsolidatedStructure", { pad: 24, gap: 14, width: 1304 });
const archTop = track(figma.createAutoLayout("HORIZONTAL", { name: "ArchHeader", itemSpacing: 12 }));
archTop.primaryAxisAlignItems = "SPACE_BETWEEN";
archTop.counterAxisAlignItems = "CENTER";
archTop.fills = [];
archCard.appendChild(archTop);
archTop.layoutSizingHorizontal = "FILL";
archTop.layoutSizingVertical = "HUG";

addText(archTop, "Consolidated helleristninger/ Repository Architecture", {
  family: "Newsreader",
  style: "SemiBold",
  size: 22,
  color: C.ink,
  lh: { value: 28, unit: "PIXELS" }
});
makeBadge(archTop, DATA.release_badge, "fjord", "fjordTint");

const dirGrid = track(figma.createAutoLayout("HORIZONTAL", { name: "DirGrid", itemSpacing: 14, counterAxisSpacing: 14 }));
dirGrid.layoutWrap = "WRAP";
dirGrid.fills = [];
archCard.appendChild(dirGrid);
dirGrid.resize(1256, 100);
dirGrid.layoutSizingHorizontal = "FIXED";
dirGrid.layoutSizingVertical = "HUG";

for (const d of DATA.dirs) {
  const dc = makeCard(dirGrid, "Dir/" + d.name, { pad: 14, gap: 4, bg: C.viewportBg, radius: 8, width: 303 });
  addText(dc, d.name, {
    family: "IBM Plex Mono",
    style: "SemiBold",
    size: 12,
    color: C.ochre,
    lh: { value: 16, unit: "PIXELS" },
    width: 275
  });
  addText(dc, d.desc, {
    family: "IBM Plex Sans",
    style: "Regular",
    size: 12,
    color: C.inkSec,
    lh: { value: 17, unit: "PIXELS" },
    width: 275
  });
}

section.resize(1400, Math.ceil(root.height));
return {
  success: true,
  sectionId: section.id,
  sectionName: section.name,
  bounds: { x: section.x, y: section.y, width: section.width, height: section.height },
  createdCount: createdNodeIds.length
};
