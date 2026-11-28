// Read-only inspection of page 27:338 (hellerisning), existing sections 42:18 & 42:17,
// available fonts, and local styles/variables.
// Usage: execute via use_figma on fileKey w0sThYnGJQo1cPrYx7GQR8 with skillNames=["figma-use"].

const page = await figma.getNodeByIdAsync("27:338");
if (!page || page.type !== "PAGE") {
  return { error: "Page 27:338 not found", pages: figma.root.children.map(p => ({ id: p.id, name: p.name })) };
}
await figma.setCurrentPageAsync(page);

const topChildren = page.children.map(n => ({
  id: n.id,
  name: n.name,
  type: n.type,
  x: Math.round(n.x * 10) / 10,
  y: Math.round(n.y * 10) / 10,
  width: Math.round(n.width * 10) / 10,
  height: Math.round(n.height * 10) / 10,
  right: Math.round((n.x + n.width) * 10) / 10,
  bottom: Math.round((n.y + n.height) * 10) / 10
}));

// Inspect fills/fonts used in 42:18 and 42:17 (read-only) to match existing page conventions
const refNodes = [];
for (const refId of ["42:18", "42:17"]) {
  const n = await figma.getNodeByIdAsync(refId);
  if (n) {
    const sampleTexts = "findAllWithCriteria" in n ? n.findAllWithCriteria({ types: ["TEXT"] }).slice(0, 6) : [];
    refNodes.push({
      id: n.id,
      name: n.name,
      type: n.type,
      x: n.x,
      y: n.y,
      width: n.width,
      height: n.height,
      fills: "fills" in n && Array.isArray(n.fills) ? n.fills.map(f => ({ type: f.type, color: f.color, opacity: f.opacity })) : null,
      sampleFonts: sampleTexts.map(t => ({ name: t.name, fontName: t.fontName, fontSize: t.fontSize }))
    });
  }
}

const allFonts = await figma.listAvailableFontsAsync();
const targetFamilies = new Set([
  "Inter", "IBM Plex Sans", "IBM Plex Mono", "JetBrains Mono", "Roboto Mono",
  "DM Sans", "Plus Jakarta Sans", "Source Serif 4", "Newsreader", "Instrument Serif",
  "Fraunces", "Merriweather", "Lora", "Geist", "Geist Mono", "Space Grotesk", "Space Mono"
]);
const fontMap = {};
for (const f of allFonts) {
  if (targetFamilies.has(f.fontName.family)) {
    if (!fontMap[f.fontName.family]) fontMap[f.fontName.family] = [];
    fontMap[f.fontName.family].push(f.fontName.style);
  }
}

const [textStyles, paintStyles, varCollections] = await Promise.all([
  figma.getLocalTextStylesAsync(),
  figma.getLocalPaintStylesAsync(),
  figma.variables.getLocalVariableCollectionsAsync()
]);

return {
  page: { id: page.id, name: page.name },
  topChildren,
  refNodes,
  availableFonts: fontMap,
  localTextStyles: textStyles.map(s => ({ id: s.id, name: s.name, fontSize: s.fontSize, fontName: s.fontName })),
  localPaintStyles: paintStyles.map(s => ({ id: s.id, name: s.name })),
  localVarCollections: varCollections.map(c => ({ id: c.id, name: c.name, varCount: c.variableIds.length }))
};
