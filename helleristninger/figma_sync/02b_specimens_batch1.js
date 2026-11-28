
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

const SPECS = [{"figure_id": "hr_5f21221f4b27_008", "region_label": "Alta \u00b7 NO", "site": "Amtmannsnes \u2014 Amtmannsnes 2A", "site_panel_id": "Askeladden:215875", "coordinates_str": "69.99031, 23.30089 (site_polygon_centroid_approx_10m_to_50m)", "motif_str": "human / anthropomorph", "size_str": "220.0 x 106.67 px (uncalibrated)", "scale_calibrated": false, "period": "stone_age_hunter", "licence": "Alta Museum Open Non-Commercial ShareAlike (CC BY-NC-SA-equivalent)", "rights_holder": "Verdensarvsenter for Bergkunst - Alta Museum", "confidence": "0.637", "iou": 0.7473, "hausdorff_px": 9.9, "layer_summary": "fig:1 \u00b7 cup:0 \u00b7 crk:0 \u00b7 unc:0", "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" version=\"1.1\" viewBox=\"0 0 275.000 162.000\" width=\"275.000px\" height=\"162.000px\" data-figure-id=\"hr_5f21221f4b27_008\" data-scale-calibrated=\"false\" data-units=\"px\" data-scale-source=\"uncalibrated_archival_plate_no_scale_bar\" data-site-panel-id=\"Askeladden:215875\"><g id=\"figures\"><path id=\"path_hr_5f21221f4b27_008\" d=\"M 138.667,29.333 L 125.333,41.333 L 116.000,56.000 L 116.000,61.333 L 114.667,62.667 L 112.000,62.667 L 97.333,54.667 L 89.333,44.000 L 85.333,46.667 L 72.000,46.667 L 70.667,48.000 L 53.333,46.667 L 52.000,45.333 L 53.333,34.667 L 50.667,34.667 L 48.000,38.667 L 45.333,38.667 L 42.667,44.000 L 26.667,53.333 L 30.667,57.333 L 30.667,60.000 L 36.000,60.000 L 40.000,57.333 L 61.333,57.333 L 65.333,58.667 L 69.333,64.000 L 69.333,68.000 L 74.667,76.000 L 74.667,81.333 L 72.000,85.333 L 72.000,89.333 L 64.000,102.667 L 57.333,106.667 L 57.333,109.333 L 54.667,110.667 L 56.000,116.000 L 62.667,117.333 L 76.000,108.000 L 76.000,104.000 L 81.333,90.667 L 86.667,89.333 L 102.667,102.667 L 110.667,105.333 L 117.333,110.667 L 126.667,110.667 L 130.667,108.000 L 136.000,108.000 L 137.333,112.000 L 121.333,130.667 L 121.333,133.333 L 125.333,133.333 L 141.333,113.333 L 144.000,112.000 L 148.000,100.000 L 144.000,98.667 L 128.000,105.333 L 120.000,105.333 L 117.333,102.667 L 114.667,102.667 L 113.333,98.667 L 118.667,85.333 L 118.667,81.333 L 116.000,81.333 L 114.667,77.333 L 117.333,74.667 L 128.000,74.667 L 144.000,82.667 L 149.333,82.667 L 150.667,81.333 L 149.333,78.667 L 145.333,77.333 L 141.333,73.333 L 121.333,66.667 L 122.667,58.667 L 134.667,42.667 L 140.000,32.000 Z M 112.000,86.667 L 113.333,90.667 L 110.667,93.333 L 110.667,97.333 L 109.333,98.667 L 104.000,98.667 L 102.667,94.667 L 109.333,86.667 Z M 106.667,80.000 L 108.000,84.000 L 101.333,90.667 L 98.667,90.667 L 97.333,86.667 L 104.000,80.000 Z M 116.000,66.667 L 117.333,70.667 L 113.333,74.667 L 110.667,74.667 L 109.333,70.667 L 113.333,66.667 Z M 101.333,66.667 L 102.667,70.667 L 96.000,84.000 L 93.333,86.667 L 89.333,86.667 L 88.000,85.333 L 88.000,80.000 L 92.000,76.000 L 93.333,72.000 L 98.667,66.667 Z M 96.000,57.333 L 97.333,58.667 L 96.000,64.000 L 89.333,72.000 L 86.667,72.000 L 85.333,66.667 L 93.333,57.333 Z M 88.000,49.333 L 89.333,53.333 L 84.000,58.667 L 84.000,61.333 L 80.000,65.333 L 80.000,68.000 L 76.000,69.333 L 74.667,65.333 L 77.333,64.000 L 78.667,58.667 L 81.333,57.333 L 82.667,50.667 Z M 174.667,28.000 L 174.667,41.333 L 173.333,42.667 L 174.667,56.000 L 173.333,61.333 L 169.333,66.667 L 169.333,69.333 L 162.667,81.333 L 162.667,84.000 L 166.667,84.000 L 169.333,80.000 L 176.000,80.000 L 178.667,76.000 L 182.667,76.000 L 189.333,82.667 L 200.000,89.333 L 206.667,89.333 L 214.667,86.667 L 216.000,90.667 L 218.667,90.667 L 232.000,85.333 L 236.000,78.667 L 246.667,80.000 L 246.667,77.333 L 237.333,74.667 L 226.667,68.000 L 222.667,68.000 L 221.333,65.333 L 213.333,61.333 L 205.333,53.333 L 198.667,53.333 L 190.667,61.333 L 185.333,69.333 L 181.333,70.667 L 178.667,68.000 L 178.667,62.667 L 177.333,61.333 L 178.667,28.000 L 177.333,26.667 Z M 229.333,74.667 L 229.333,77.333 L 222.667,82.667 L 217.333,81.333 L 217.333,78.667 L 222.667,72.000 Z M 194.667,65.333 L 194.667,62.667 L 197.333,60.000 L 201.333,61.333 L 201.333,64.000 L 198.667,66.667 Z\" fill=\"#18181B\" fill-rule=\"evenodd\" stroke=\"none\" /></g><g id=\"cup_marks\" fill=\"#9A3412\" fill-opacity=\"0.35\" stroke=\"#9A3412\" stroke-width=\"1.237\" /><g id=\"cracks\" fill=\"none\" stroke=\"#DC2626\" stroke-width=\"1.423\" stroke-linecap=\"round\" stroke-linejoin=\"round\" /><g id=\"uncertain\" fill=\"none\" stroke=\"#6B7280\" stroke-width=\"1.237\" stroke-dasharray=\"2 2\" stroke-linecap=\"round\" /></svg>"}, {"figure_id": "hr_d64cb06f1d2a_017", "region_label": "\u00d8stfold \u00b7 NO", "site": "Kalnes 1 / Askeladden 147294-1", "site_panel_id": "Askeladden:147294-1", "coordinates_str": "59.31547, 11.04595 (site_polygon_centroid_approx_10m_to_50m)", "motif_str": "vessel / bronze_age_ship", "size_str": "141.0 x 64.0 px (uncalibrated)", "scale_calibrated": false, "period": "bronze_age_agrarian", "licence": "CC BY 4.0", "rights_holder": "Evers, Dietrich / Tanums H\u00e4llristningsmuseum Undersl\u00f6s (via SHFA / University of Gothenburg)", "confidence": "0.544", "iou": 0.1692, "hausdorff_px": 55.3, "layer_summary": "fig:1 \u00b7 cup:2 \u00b7 crk:1 \u00b7 unc:31", "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" version=\"1.1\" viewBox=\"0 0 176.000 99.000\" width=\"176.000px\" height=\"99.000px\" data-figure-id=\"hr_d64cb06f1d2a_017\" data-scale-calibrated=\"false\" data-units=\"px\" data-scale-source=\"uncalibrated_archival_plate_no_scale_bar\" data-site-panel-id=\"Askeladden:147294-1\"><g id=\"figures\"><path id=\"path_hr_d64cb06f1d2a_017\" d=\"M 158.000,73.000 L 156.000,71.000 L 153.000,74.000 L 149.000,74.000 L 144.000,72.000 L 140.000,73.000 L 138.000,75.000 L 135.000,74.000 L 129.000,75.000 L 128.000,74.000 L 123.000,76.000 L 124.000,77.000 L 130.000,77.000 L 132.000,79.000 L 134.000,79.000 L 137.000,76.000 L 139.000,76.000 L 141.000,78.000 L 148.000,77.000 L 151.000,80.000 L 153.000,78.000 L 153.000,76.000 L 157.000,75.000 Z M 17.000,67.000 L 17.000,77.000 L 20.000,81.000 L 22.000,79.000 L 25.000,79.000 L 28.000,81.000 L 32.000,79.000 L 39.000,80.000 L 41.000,78.000 L 45.000,79.000 L 51.000,78.000 L 53.000,80.000 L 55.000,78.000 L 57.000,78.000 L 59.000,80.000 L 63.000,80.000 L 64.000,81.000 L 74.000,81.000 L 75.000,80.000 L 79.000,80.000 L 80.000,81.000 L 85.000,80.000 L 86.000,81.000 L 88.000,78.000 L 90.000,78.000 L 92.000,80.000 L 96.000,79.000 L 99.000,80.000 L 104.000,77.000 L 109.000,77.000 L 111.000,79.000 L 113.000,79.000 L 115.000,77.000 L 115.000,75.000 L 118.000,73.000 L 117.000,71.000 L 114.000,72.000 L 111.000,71.000 L 109.000,74.000 L 105.000,74.000 L 103.000,70.000 L 101.000,72.000 L 97.000,72.000 L 93.000,70.000 L 89.000,71.000 L 85.000,74.000 L 77.000,73.000 L 72.000,75.000 L 68.000,72.000 L 65.000,76.000 L 60.000,75.000 L 59.000,76.000 L 51.000,77.000 L 44.000,72.000 L 42.000,76.000 L 34.000,76.000 L 29.000,70.000 L 28.000,71.000 L 24.000,70.000 L 24.000,66.000 L 30.000,60.000 L 34.000,59.000 L 33.000,57.000 L 30.000,56.000 L 30.000,53.000 L 26.000,50.000 L 26.000,47.000 L 24.000,45.000 L 21.000,46.000 L 21.000,55.000 L 19.000,57.000 L 20.000,63.000 Z M 29.000,17.000 L 27.000,18.000 L 19.000,17.000 L 17.000,19.000 L 20.000,22.000 L 20.000,25.000 L 18.000,27.000 L 20.000,29.000 L 20.000,32.000 L 23.000,37.000 L 24.000,36.000 L 23.000,35.000 L 23.000,30.000 L 26.000,28.000 L 23.000,26.000 L 22.000,22.000 L 25.000,20.000 L 28.000,20.000 L 30.000,18.000 Z\" fill=\"#18181B\" fill-rule=\"evenodd\" stroke=\"none\" /></g><g id=\"cup_marks\" fill=\"#9A3412\" fill-opacity=\"0.35\" stroke=\"#9A3412\" stroke-width=\"0.792\"><circle id=\"cupmark_000\" cx=\"51.070\" cy=\"19.960\" r=\"2.990\" /><circle id=\"cupmark_001\" cx=\"22.370\" cy=\"40.790\" r=\"2.460\" /></g><g id=\"cracks\" fill=\"none\" stroke=\"#DC2626\" stroke-width=\"0.911\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path id=\"crack_000\" d=\"M 62.000,21.000 L 88.000,21.000 L 90.000,23.000 L 125.000,22.000\" /></g><g id=\"uncertain\" fill=\"none\" stroke=\"#6B7280\" stroke-width=\"0.792\" stroke-dasharray=\"2 2\" stroke-linecap=\"round\"><path id=\"uncertain_000\" d=\"M 44.000,22.000 L 45.000,24.000 L 41.000,27.000 L 40.000,24.000 L 39.000,27.000 L 31.000,28.000 L 34.000,32.000 L 34.000,37.000 L 32.000,37.000 L 36.000,40.000 L 38.000,38.000 L 44.000,41.000 L 47.000,40.000 L 50.000,44.000 L 48.000,40.000 L 55.000,38.000 L 56.000,44.000 L 50.000,50.000 L 50.000,53.000 L 51.000,49.000 L 56.000,51.000 L 59.000,47.000 L 65.000,48.000 L 66.000,51.000 L 68.000,44.000 L 74.000,44.000 L 76.000,50.000 L 75.000,44.000 L 77.000,43.000 L 82.000,49.000 L 88.000,50.000 L 91.000,55.000 L 85.000,48.000 L 86.000,45.000 L 94.000,42.000 L 98.000,51.000 L 107.000,51.000 L 107.000,45.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_001\" d=\"M 143.000,46.000 L 141.000,50.000 L 128.000,55.000 L 125.000,54.000 L 126.000,51.000 L 120.000,60.000 L 113.000,59.000 L 108.000,63.000 L 89.000,66.000 L 79.000,70.000 L 73.000,68.000 L 75.000,70.000 L 85.000,68.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_002\" d=\"M 151.000,25.000 L 148.000,28.000 L 140.000,29.000 L 139.000,27.000 L 139.000,35.000 L 146.000,35.000 L 148.000,39.000 L 145.000,41.000 L 155.000,39.000 L 160.000,43.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_003\" d=\"M 147.000,60.000 L 145.000,64.000 L 137.000,69.000 L 130.000,69.000 L 137.000,69.000 L 141.000,66.000 L 146.000,69.000 L 143.000,66.000 L 144.000,64.000 L 151.000,65.000 L 154.000,63.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_004\" d=\"M 108.000,14.000 L 105.000,17.000 L 96.000,16.000 L 92.000,19.000 L 89.000,17.000 L 94.000,19.000 L 96.000,16.000 L 107.000,16.000 L 110.000,19.000 L 107.000,16.000 L 108.000,14.000 L 114.000,15.000 L 118.000,19.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_005\" d=\"M 106.000,80.000 L 104.000,82.000 L 92.000,83.000 L 90.000,81.000 L 83.000,84.000 L 70.000,83.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_006\" d=\"M 16.000,16.000 L 13.000,21.000 L 16.000,28.000 L 14.000,36.000 L 17.000,40.000 L 18.000,48.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_007\" d=\"M 0.000,20.000 L 1.000,50.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_008\" d=\"M 15.000,67.000 L 16.000,81.000 L 14.000,82.000 L 19.000,84.000 L 26.000,83.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_009\" d=\"M 58.000,56.000 L 57.000,59.000 L 53.000,60.000 L 51.000,58.000 L 52.000,61.000 L 47.000,62.000 L 50.000,62.000 L 53.000,67.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_010\" d=\"M 155.000,17.000 L 150.000,18.000 L 161.000,17.000 L 164.000,19.000 L 168.000,17.000 L 170.000,19.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_011\" d=\"M 79.000,13.000 L 77.000,16.000 L 69.000,17.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_012\" d=\"M 144.000,0.000 L 167.000,0.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_013\" d=\"M 116.000,0.000 L 138.000,0.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_014\" d=\"M 79.000,0.000 L 89.000,0.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_015\" d=\"M 64.000,64.000 L 62.000,69.000 L 57.000,71.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_016\" d=\"M 80.000,55.000 L 81.000,60.000 L 78.000,63.000 L 82.000,62.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_017\" d=\"M 3.000,60.000 L 2.000,73.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_018\" d=\"M 164.000,76.000 L 163.000,79.000 L 158.000,78.000 L 153.000,81.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_019\" d=\"M 128.000,79.000 L 128.000,82.000 L 137.000,80.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_020\" d=\"M 50.000,81.000 L 61.000,82.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_021\" d=\"M 159.000,50.000 L 149.000,54.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_022\" d=\"M 72.000,54.000 L 70.000,58.000 L 73.000,61.000 L 71.000,60.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_023\" d=\"M 42.000,62.000 L 34.000,66.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_024\" d=\"M 117.000,76.000 L 117.000,80.000 L 122.000,81.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_025\" d=\"M 30.000,82.000 L 39.000,82.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_026\" d=\"M 31.000,15.000 L 39.000,18.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_027\" d=\"M 51.000,14.000 L 59.000,18.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_028\" d=\"M 38.000,46.000 L 37.000,48.000 L 38.000,46.000 L 42.000,47.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_029\" d=\"M 17.000,52.000 L 17.000,59.000\" stroke-dasharray=\"2 2\" /><path id=\"uncertain_030\" d=\"M 3.000,76.000 L 4.000,78.000 L 0.000,82.000\" stroke-dasharray=\"2 2\" /></g></svg>"}, {"figure_id": "hr_b6a8b964c77a_011", "region_label": "Bohusl\u00e4n / Tanum \u00b7 SE", "site": "Tegneby Skattekl\u00e5van Tanum / Tanum 65:1", "site_panel_id": "L1968:7500 (Tanum 65:1)", "coordinates_str": "58.68532, 11.33043 (site_centroid_approx_10m_to_100m)", "motif_str": "vessel / bronze_age_ship", "size_str": "489.93 x 229.65 px (uncalibrated)", "scale_calibrated": false, "period": "bronze_age_agrarian", "licence": "CC BY 4.0", "rights_holder": "RockCare / SHFA (via SHFA / University of Gothenburg)", "confidence": "0.544", "iou": 0.8819, "hausdorff_px": 33.9, "layer_summary": "fig:1 \u00b7 cup:0 \u00b7 crk:3 \u00b7 unc:1", "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" version=\"1.1\" viewBox=\"0 0 611.000 351.000\" width=\"611.000px\" height=\"351.000px\" data-figure-id=\"hr_b6a8b964c77a_011\" data-scale-calibrated=\"false\" data-units=\"px\" data-scale-source=\"uncalibrated_archival_plate_no_scale_bar\" data-site-panel-id=\"L1968:7500 (Tanum 65:1)\"><g id=\"figures\"><path id=\"path_hr_b6a8b964c77a_011\" d=\"M 548.979,231.840 L 540.231,229.653 L 533.669,234.027 L 531.482,240.589 L 524.921,238.401 L 531.482,212.155 L 529.295,192.471 L 520.546,185.909 L 509.610,183.722 L 507.423,185.909 L 507.423,196.845 L 513.985,207.781 L 513.985,218.717 L 507.423,227.466 L 503.049,227.466 L 498.675,223.091 L 489.926,201.220 L 481.177,199.032 L 481.177,209.968 L 478.990,212.155 L 465.867,209.968 L 459.306,220.904 L 443.995,218.717 L 441.808,225.278 L 435.247,227.466 L 433.060,220.904 L 428.685,218.717 L 422.124,227.466 L 424.311,236.214 L 424.311,253.712 L 422.124,255.899 L 409.001,253.712 L 404.626,249.337 L 387.129,242.776 L 374.006,231.840 L 363.070,229.653 L 354.321,240.589 L 389.316,260.273 L 391.503,266.835 L 389.316,269.022 L 378.380,269.022 L 358.696,255.899 L 354.321,273.396 L 365.257,282.145 L 371.819,282.145 L 382.755,288.706 L 391.503,288.706 L 398.065,284.332 L 411.188,284.332 L 441.808,275.583 L 474.616,273.396 L 494.300,266.835 L 498.675,262.460 L 505.236,262.460 L 518.359,255.899 L 544.605,249.337 L 548.979,242.776 Z M 542.418,133.417 L 540.231,124.669 L 533.669,122.481 L 507.423,122.481 L 503.049,118.107 L 503.049,113.733 L 516.172,100.610 L 520.546,91.861 L 518.359,69.989 L 509.610,59.054 L 500.862,59.054 L 496.487,63.428 L 503.049,76.551 L 503.049,91.861 L 496.487,100.610 L 492.113,100.610 L 476.803,85.300 L 470.241,96.235 L 459.306,96.235 L 457.118,98.423 L 457.118,107.171 L 454.931,109.358 L 450.557,109.358 L 443.995,98.423 L 437.434,98.423 L 433.060,109.358 L 419.937,107.171 L 415.562,113.733 L 417.749,115.920 L 415.562,122.481 L 393.690,131.230 L 384.942,131.230 L 380.567,118.107 L 371.819,109.358 L 365.257,109.358 L 354.321,120.294 L 354.321,133.417 L 356.509,135.604 L 354.321,142.166 L 314.952,137.792 L 312.765,135.604 L 284.332,135.604 L 282.145,133.417 L 249.337,135.604 L 231.840,131.230 L 227.466,133.417 L 216.530,133.417 L 214.343,131.230 L 148.728,133.417 L 137.792,129.043 L 129.043,131.230 L 122.481,126.856 L 109.358,126.856 L 107.171,124.669 L 78.738,120.294 L 63.428,111.546 L 59.054,113.733 L 59.054,120.294 L 63.428,126.856 L 89.674,139.979 L 107.171,139.979 L 109.358,142.166 L 126.856,144.353 L 139.979,150.915 L 153.102,150.915 L 155.289,153.102 L 185.909,150.915 L 188.097,148.728 L 236.214,150.915 L 238.401,153.102 L 236.214,161.851 L 242.776,168.412 L 255.899,168.412 L 262.460,164.038 L 269.022,146.540 L 323.701,148.728 L 336.824,153.102 L 363.070,155.289 L 365.257,153.102 L 398.065,150.915 L 411.188,146.540 L 481.177,137.792 L 492.113,133.417 L 503.049,133.417 L 518.359,137.792 L 538.044,137.792 Z M 255.899,148.728 L 258.086,155.289 L 251.524,161.851 L 247.150,161.851 L 242.776,157.476 L 242.776,153.102 Z\" fill=\"#18181B\" fill-rule=\"evenodd\" stroke=\"none\" /></g><g id=\"cup_marks\" fill=\"#9A3412\" fill-opacity=\"0.35\" stroke=\"#9A3412\" stroke-width=\"2.000\" /><g id=\"cracks\" fill=\"none\" stroke=\"#DC2626\" stroke-width=\"2.300\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path id=\"crack_000\" d=\"M 323.700,153.100 L 288.710,164.040 L 269.020,181.540 L 269.020,192.470 L 273.400,188.100 L 282.140,190.280 L 249.340,231.840 L 262.460,247.150 L 273.400,251.520 L 275.580,260.270 L 288.710,234.030 L 297.460,244.960 L 299.640,262.460 L 332.450,279.960 L 334.640,314.950 L 343.390,317.140 L 347.760,328.080 L 365.260,334.640 L 369.630,345.570 L 365.260,334.640 L 345.570,325.890 L 347.760,310.580 L 367.440,308.390 L 378.380,323.700\" /><path id=\"crack_001\" d=\"M 255.900,17.500 L 205.590,61.240 L 196.850,80.930 L 196.850,98.420 L 216.530,118.110 L 223.090,118.110\" /><path id=\"crack_002\" d=\"M 312.770,286.520 L 308.390,290.890 L 312.770,295.270\" /></g><g id=\"uncertain\" fill=\"none\" stroke=\"#6B7280\" stroke-width=\"2.000\" stroke-dasharray=\"2 2\" stroke-linecap=\"round\"><path id=\"uncertain_000\" d=\"M 273.400,43.740 L 273.400,69.990\" stroke-dasharray=\"2 2\" /></g></svg>"}, {"figure_id": "hr_530ba98b37d3_009", "region_label": "N\u00e4mforsen \u00b7 SE", "site": "N\u00e4mforsen Lax\u00f6n D5 / \u00c5dals-Liden 193:1", "site_panel_id": "L1935:5803 (\u00c5dals-Liden 193:1)", "coordinates_str": "63.44038, 16.88654 (site_centroid_approx_10m_to_100m)", "motif_str": "scene_or_unclassified / unclassified_motif", "size_str": "152.91 x 79.88 px (uncalibrated)", "scale_calibrated": false, "period": "stone_age_hunter", "licence": "CC BY 4.0", "rights_holder": "Hallstr\u00f6m, Gustaf (via SHFA / University of Gothenburg)", "confidence": "0.335", "iou": 0.956, "hausdorff_px": 9.2, "layer_summary": "fig:1 \u00b7 cup:0 \u00b7 crk:4 \u00b7 unc:0", "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" version=\"1.1\" viewBox=\"0 0 191.000 118.000\" width=\"191.000px\" height=\"118.000px\" data-figure-id=\"hr_530ba98b37d3_009\" data-scale-calibrated=\"false\" data-units=\"px\" data-scale-source=\"uncalibrated_archival_plate_no_scale_bar\" data-site-panel-id=\"L1935:5803 (\u00c5dals-Liden 193:1)\"><g id=\"figures\"><path id=\"path_hr_530ba98b37d3_009\" d=\"M 171.167,28.528 L 165.461,23.963 L 156.332,21.681 L 150.627,27.387 L 146.062,28.528 L 143.780,30.810 L 142.639,37.657 L 148.344,47.927 L 144.921,60.479 L 130.087,73.031 L 125.522,71.890 L 123.240,62.761 L 125.522,57.056 L 125.522,53.632 L 123.240,49.068 L 123.240,46.786 L 125.522,44.503 L 125.522,39.939 L 123.240,35.374 L 125.522,21.681 L 122.099,18.258 L 115.252,18.258 L 108.406,20.540 L 108.406,25.104 L 106.123,26.246 L 109.547,28.528 L 109.547,31.951 L 104.982,51.350 L 103.841,67.326 L 98.136,73.031 L 84.442,73.031 L 82.160,75.313 L 78.737,75.313 L 70.749,78.737 L 60.479,78.737 L 59.338,79.878 L 46.786,77.596 L 39.939,71.890 L 37.657,71.890 L 35.374,69.608 L 33.092,69.608 L 27.387,66.184 L 19.399,67.326 L 18.258,68.467 L 18.258,73.031 L 22.822,78.737 L 26.246,79.878 L 27.387,82.160 L 33.092,85.583 L 34.233,91.289 L 37.657,91.289 L 45.644,95.853 L 49.068,94.712 L 65.043,94.712 L 69.608,96.994 L 78.737,96.994 L 79.878,98.136 L 84.442,98.136 L 85.583,96.994 L 89.007,98.136 L 109.547,98.136 L 114.111,96.994 L 117.534,94.712 L 120.958,94.712 L 124.381,92.430 L 127.804,92.430 L 136.933,87.866 L 142.639,86.724 L 143.780,83.301 L 149.486,78.737 L 150.627,75.313 L 159.756,67.326 L 160.897,53.632 L 162.038,52.491 L 160.897,47.927 L 167.743,44.503 L 168.884,37.657 L 171.167,34.233 Z\" fill=\"#18181B\" fill-rule=\"evenodd\" stroke=\"none\" /></g><g id=\"cup_marks\" fill=\"#9A3412\" fill-opacity=\"0.35\" stroke=\"#9A3412\" stroke-width=\"0.859\" /><g id=\"cracks\" fill=\"none\" stroke=\"#DC2626\" stroke-width=\"0.988\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path id=\"crack_000\" d=\"M 159.760,59.340 L 159.760,62.760 L 156.330,63.900 L 158.610,65.040 L 156.330,69.610\" /><path id=\"crack_001\" d=\"M 103.840,73.030 L 99.280,78.740 L 103.840,78.740\" /><path id=\"crack_002\" d=\"M 49.070,84.440 L 44.500,86.720 L 51.350,87.870\" /><path id=\"crack_003\" d=\"M 91.290,75.310 L 93.570,76.450 L 91.290,78.740\" /></g><g id=\"uncertain\" fill=\"none\" stroke=\"#6B7280\" stroke-width=\"0.859\" stroke-dasharray=\"2 2\" stroke-linecap=\"round\" /></svg>"}, {"figure_id": "hr_4d58a8f0f28a_011", "region_label": "Vingen \u00b7 NO", "site": "Vingen, Bremanger", "site_panel_id": "Askeladden:117465", "coordinates_str": "61.82641, 5.31231 (site_polygon_centroid_approx_10m_to_50m)", "motif_str": "animal / quadruped_other", "size_str": "42.0 x 41.0 px (uncalibrated)", "scale_calibrated": false, "period": "stone_age_hunter", "licence": "CC BY-SA 4.0", "rights_holder": "Universitetsmuseet i Bergen", "confidence": "0.595", "iou": 0.8059, "hausdorff_px": 5.0, "layer_summary": "fig:1 \u00b7 cup:1 \u00b7 crk:0 \u00b7 unc:1", "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" version=\"1.1\" viewBox=\"0 0 71.000 70.000\" width=\"71.000px\" height=\"70.000px\" data-figure-id=\"hr_4d58a8f0f28a_011\" data-scale-calibrated=\"false\" data-units=\"px\" data-scale-source=\"uncalibrated_archival_plate_no_scale_bar\" data-site-panel-id=\"Askeladden:117465\"><g id=\"figures\"><path id=\"path_hr_4d58a8f0f28a_011\" d=\"M 33.000,50.000 L 29.000,49.000 L 14.000,54.000 L 15.000,55.000 L 21.000,55.000 L 25.000,53.000 L 32.000,52.000 Z M 49.000,15.000 L 38.000,15.000 L 37.000,14.000 L 28.000,14.000 L 26.000,15.000 L 26.000,21.000 L 31.000,33.000 L 31.000,40.000 L 33.000,45.000 L 36.000,44.000 L 29.000,18.000 L 31.000,16.000 L 33.000,16.000 L 37.000,18.000 L 44.000,18.000 L 49.000,20.000 L 55.000,20.000 L 56.000,18.000 Z\" fill=\"#18181B\" fill-rule=\"evenodd\" stroke=\"none\" /></g><g id=\"cup_marks\" fill=\"#9A3412\" fill-opacity=\"0.35\" stroke=\"#9A3412\" stroke-width=\"0.350\"><circle id=\"cupmark_000\" cx=\"59.730\" cy=\"58.670\" r=\"2.190\" /></g><g id=\"cracks\" fill=\"none\" stroke=\"#DC2626\" stroke-width=\"0.402\" stroke-linecap=\"round\" stroke-linejoin=\"round\" /><g id=\"uncertain\" fill=\"none\" stroke=\"#6B7280\" stroke-width=\"0.350\" stroke-dasharray=\"2 2\" stroke-linecap=\"round\"><path id=\"uncertain_000\" d=\"M 63.000,52.000 L 50.000,57.000 L 54.000,57.000 L 55.000,60.000\" stroke-dasharray=\"2 2\" /></g></svg>"}, {"figure_id": "hr_f3dccee22649_000", "region_label": "Tr\u00f8ndelag \u00b7 NO", "site": "Kvennavika", "site_panel_id": "Askeladden:55713", "coordinates_str": "63.86607, 11.04053 (site_polygon_centroid_approx_10m_to_50m)", "motif_str": "animal / fish", "size_str": "223.32 x 416.01 px (uncalibrated)", "scale_calibrated": false, "period": "stone_age_hunter", "licence": "Public domain", "rights_holder": "Public Domain / Anonymous rock carving dating from en:Nordic Bronze Age ; tracing paper work in situ by no:Gutorm Gjessing , final drawning by Mary Wilhelmsen.", "confidence": "0.705", "iou": 0.7544, "hausdorff_px": 26.5, "layer_summary": "fig:1 \u00b7 cup:1 \u00b7 crk:0 \u00b7 unc:0", "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" version=\"1.1\" viewBox=\"0 0 325.000 518.000\" width=\"325.000px\" height=\"518.000px\" data-figure-id=\"hr_f3dccee22649_000\" data-scale-calibrated=\"false\" data-units=\"px\" data-scale-source=\"uncalibrated_archival_plate_no_scale_bar\" data-site-panel-id=\"Askeladden:55713\"><g id=\"figures\"><path id=\"path_hr_f3dccee22649_000\" d=\"M 170.999,449.191 L 169.723,454.296 L 170.999,455.572 L 170.999,464.504 L 172.275,465.781 L 174.827,464.504 L 174.827,458.124 L 173.551,456.848 L 173.551,450.467 Z M 136.544,52.321 L 130.163,54.873 L 118.678,63.806 L 111.022,79.119 L 86.776,111.022 L 86.776,114.850 L 75.291,140.372 L 74.014,148.029 L 68.910,159.514 L 68.910,163.342 L 61.253,178.656 L 51.044,210.558 L 51.044,227.148 L 49.768,228.424 L 49.768,237.357 L 51.044,238.633 L 52.321,248.842 L 61.253,270.536 L 70.186,283.297 L 82.947,308.819 L 88.052,313.923 L 94.432,325.408 L 109.746,345.826 L 114.850,356.035 L 144.201,385.386 L 144.201,408.356 L 142.924,410.908 L 136.544,417.288 L 132.716,418.564 L 119.954,431.326 L 117.402,438.982 L 117.402,449.191 L 121.231,453.019 L 127.611,453.019 L 137.820,450.467 L 158.238,441.534 L 163.342,441.534 L 178.656,436.430 L 197.797,435.154 L 199.073,433.878 L 214.387,433.878 L 219.491,436.430 L 230.976,437.706 L 238.633,441.534 L 250.118,440.258 L 257.774,442.811 L 259.051,437.706 L 251.394,431.326 L 239.909,426.221 L 232.252,424.945 L 225.872,418.564 L 213.111,409.632 L 204.178,396.871 L 204.178,386.662 L 213.111,362.416 L 215.663,359.863 L 216.939,350.931 L 225.872,331.789 L 239.909,311.371 L 241.185,307.543 L 265.431,275.640 L 267.983,270.536 L 267.983,266.707 L 271.812,261.603 L 271.812,256.498 L 273.088,255.222 L 271.812,206.730 L 270.536,205.454 L 265.431,178.656 L 260.327,172.275 L 256.498,162.066 L 250.118,154.409 L 242.461,140.372 L 232.252,130.163 L 232.252,127.611 L 219.491,109.746 L 206.730,85.499 L 195.245,72.738 L 195.245,70.186 L 186.312,58.701 L 174.827,49.768 L 159.514,49.768 L 158.238,51.044 Z M 103.365,99.537 L 111.022,99.537 L 112.298,102.089 L 118.678,104.641 L 123.783,109.746 L 131.439,113.574 L 144.201,126.335 L 150.581,127.611 L 158.238,137.820 L 160.790,176.103 L 165.894,191.417 L 167.171,275.640 L 169.723,284.573 L 168.447,320.304 L 167.171,321.580 L 167.171,347.102 L 168.447,348.378 L 168.447,357.311 L 169.723,358.587 L 168.447,389.214 L 169.723,390.490 L 169.723,408.356 L 170.999,409.632 L 172.275,430.049 L 169.723,432.602 L 155.686,435.154 L 146.753,440.258 L 139.096,441.534 L 131.439,446.639 L 123.783,446.639 L 122.507,445.363 L 123.783,435.154 L 136.544,422.393 L 142.924,419.841 L 149.305,410.908 L 149.305,405.803 L 151.857,398.147 L 151.857,384.109 L 116.126,348.378 L 111.022,338.169 L 104.641,330.513 L 100.813,321.580 L 98.261,320.304 L 95.708,313.923 L 82.947,297.334 L 72.738,276.916 L 66.358,269.259 L 59.977,255.222 L 57.425,238.633 L 56.149,237.357 L 56.149,223.319 L 57.425,222.043 L 58.701,205.454 L 70.186,183.760 L 71.462,178.656 L 74.014,176.103 L 77.843,160.790 L 77.843,153.133 L 80.395,149.305 L 81.671,140.372 L 85.499,133.992 L 85.499,128.887 L 93.156,117.402 L 95.708,109.746 Z M 195.245,85.499 L 199.073,85.499 L 201.626,88.052 L 215.663,113.574 L 229.700,132.716 L 233.528,140.372 L 239.909,146.753 L 250.118,163.342 L 260.327,185.036 L 261.603,195.245 L 264.155,201.626 L 264.155,208.006 L 266.707,213.111 L 265.431,234.804 L 266.707,236.081 L 266.707,245.013 L 267.983,246.289 L 267.983,256.498 L 260.327,273.088 L 248.842,289.677 L 237.357,302.438 L 236.081,306.267 L 232.252,310.095 L 222.043,326.684 L 216.939,339.446 L 214.387,341.998 L 211.834,354.759 L 199.073,384.109 L 199.073,394.318 L 208.006,412.184 L 224.596,424.945 L 232.252,428.773 L 230.976,432.602 L 224.596,432.602 L 218.215,428.773 L 210.558,428.773 L 209.282,427.497 L 179.932,430.049 L 177.379,422.393 L 177.379,386.662 L 174.827,377.729 L 174.827,352.207 L 173.551,350.931 L 173.551,334.341 L 172.275,333.065 L 173.551,315.199 L 174.827,313.923 L 174.827,283.297 L 176.103,282.021 L 176.103,267.983 L 177.379,264.155 L 174.827,257.774 L 174.827,247.566 L 173.551,246.289 L 172.275,209.282 L 170.999,208.006 L 172.275,202.902 L 170.999,199.073 L 170.999,182.484 L 169.723,181.208 L 169.723,173.551 L 167.171,167.171 L 167.171,158.238 L 165.894,156.962 L 165.894,128.887 L 176.103,109.746 L 193.969,89.328 Z M 170.999,56.149 L 183.760,67.634 L 190.141,75.291 L 190.141,77.843 L 182.484,94.432 L 172.275,105.917 L 167.171,113.574 L 167.171,116.126 L 163.342,119.954 L 162.066,125.059 L 158.238,126.335 L 150.581,121.231 L 135.268,107.193 L 128.887,104.641 L 119.954,98.261 L 113.574,91.880 L 113.574,88.052 L 118.678,82.947 L 122.507,76.567 L 122.507,74.014 L 131.439,62.529 L 144.201,57.425 Z\" fill=\"#18181B\" fill-rule=\"evenodd\" stroke=\"none\" /></g><g id=\"cup_marks\" fill=\"#9A3412\" fill-opacity=\"0.35\" stroke=\"#9A3412\" stroke-width=\"2.000\"><circle id=\"cupmark_000\" cx=\"185.120\" cy=\"458.310\" r=\"7.820\" /></g><g id=\"cracks\" fill=\"none\" stroke=\"#DC2626\" stroke-width=\"2.300\" stroke-linecap=\"round\" stroke-linejoin=\"round\" /><g id=\"uncertain\" fill=\"none\" stroke=\"#6B7280\" stroke-width=\"2.000\" stroke-dasharray=\"2 2\" stroke-linecap=\"round\" /></svg>"}];
const SECTION_NAME = "True-Scale Multi-Layer Specimens (mm)";
const section = page.children.find(n => n.type === "SECTION" && n.name === SECTION_NAME && !PROTECTED_IDS.has(n.id));
if (!section) throw new Error("Section not found: " + SECTION_NAME);
const root = section.findOne(n => n.type === "FRAME" && n.name === "ContentRoot");
const grid = section.findOne(n => n.type === "FRAME" && n.name === "SpecimenGrid");
if (!root || !grid) throw new Error("SpecimenGrid or ContentRoot not found");

const importedSummary = [];

for (const s of SPECS) {
  const card = makeCard(grid, "Specimen/" + s.figure_id, { pad: 20, gap: 14, width: 640 });

  const badgeRow = track(figma.createAutoLayout("HORIZONTAL", { name: "Badges", itemSpacing: 8 }));
  badgeRow.fills = [];
  card.appendChild(badgeRow);
  badgeRow.layoutSizingHorizontal = "HUG";
  badgeRow.layoutSizingVertical = "HUG";
  makeBadge(badgeRow, s.region_label, "fjord", "fjordTint");
  makeBadge(badgeRow, s.period, "inkSec", "subtleBg");
  if (s.iou !== null) {
    const fg = s.iou >= 0.65 ? "pine" : "amber";
    const bg = s.iou >= 0.65 ? "pineTint" : "amberTint";
    makeBadge(badgeRow, "IoU " + s.iou.toFixed(3), fg, bg);
  }
  makeBadge(badgeRow, s.scale_calibrated ? "CALIBRATED mm" : "PIXEL SPACE", s.scale_calibrated ? "pine" : "amber", s.scale_calibrated ? "pineTint" : "amberTint");

  addText(card, s.figure_id, {
    family: "IBM Plex Mono",
    style: "SemiBold",
    size: 13,
    color: C.ochre,
    lh: { value: 18, unit: "PIXELS" },
    width: 600
  });
  addText(card, s.site, {
    family: "Newsreader",
    style: "SemiBold",
    size: 20,
    color: C.ink,
    lh: { value: 26, unit: "PIXELS" },
    width: 600
  });

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
  if (uncGroup && "findAllWithCriteria" in uncGroup) {
    for (const v of uncGroup.findAllWithCriteria({ types: ["VECTOR"] })) {
      if (!v.dashPattern || v.dashPattern.length === 0) {
        v.dashPattern = [3, 3];
      }
    }
  }

  const metaRows = [
    { k: "Askeladden / FMIS ID", v: s.site_panel_id },
    { k: "EPSG:4326 Coords", v: s.coordinates_str },
    { k: "Motif (Taxonomy)", v: s.motif_str },
    { k: "Size / Calibration", v: s.size_str },
    { k: "Licence / Holder", v: s.licence + " · " + s.rights_holder.slice(0, 28) },
    { k: "Confidence & Layers", v: "conf=" + s.confidence + " · " + s.layer_summary },
    { k: "Dual-Trace QA", v: s.iou !== null ? ("IoU=" + s.iou.toFixed(4) + " · Hausdorff=" + s.hausdorff_px + " px") : "Single-trace verified" }
  ];

  for (let i = 0; i < metaRows.length; i++) {
    const mr = metaRows[i];
    const rFrame = track(figma.createAutoLayout("HORIZONTAL", {
      name: "Meta/" + mr.k,
      paddingTop: 5,
      paddingBottom: 5,
      paddingLeft: 8,
      paddingRight: 8,
      itemSpacing: 10
    }));
    rFrame.primaryAxisAlignItems = "SPACE_BETWEEN";
    rFrame.counterAxisAlignItems = "CENTER";
    rFrame.fills = [{ type: "SOLID", color: i % 2 === 0 ? C.viewportBg : C.cardBg }];
    rFrame.cornerRadius = 5;
    card.appendChild(rFrame);
    rFrame.layoutSizingHorizontal = "FILL";
    rFrame.layoutSizingVertical = "HUG";

    addText(rFrame, mr.k, {
      family: "IBM Plex Sans",
      style: "Medium",
      size: 11,
      color: C.inkMuted,
      lh: { value: 15, unit: "PIXELS" },
      width: 165
    });
    const mv = addText(rFrame, mr.v, {
      family: "IBM Plex Mono",
      style: "Regular",
      size: 11,
      color: C.ink,
      lh: { value: 15, unit: "PIXELS" },
      width: 395
    });
    mv.textAlignHorizontal = "RIGHT";
  }

  const layerNames = "children" in svgNode ? svgNode.children.map(c => c.name) : [];
  importedSummary.push({ figure_id: s.figure_id, svgNodeId: svgNode.id, layers: layerNames });
}

section.resize(1400, Math.ceil(root.height));
return {
  success: true,
  imported: importedSummary,
  sectionBounds: { x: section.x, y: section.y, width: section.width, height: section.height },
  createdCount: createdNodeIds.length
};
