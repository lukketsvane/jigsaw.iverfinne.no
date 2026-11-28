(() => {
  "use strict";

  // Simplified Fennoscandian + Danish coastline polygons for offline fallback projection
  const NORDIC_POLYS = [
    // Norway + Sweden main peninsula outline
    [
      [4.9, 60.4], [5.2, 61.9], [6.8, 62.9], [9.3, 63.6], [10.8, 64.9], [12.5, 66.4],
      [14.5, 67.8], [16.8, 68.9], [18.9, 69.8], [22.0, 70.3], [25.5, 71.0], [28.8, 70.8],
      [31.1, 70.1], [29.4, 69.0], [28.5, 68.5], [25.3, 68.4], [22.2, 68.9], [20.5, 68.3],
      [21.5, 65.8], [21.3, 64.6], [18.7, 63.1], [17.4, 62.2], [17.2, 60.6], [18.8, 59.8],
      [18.1, 59.1], [16.6, 58.3], [16.6, 56.8], [15.6, 56.1], [14.1, 55.4], [12.8, 55.4],
      [12.7, 56.2], [12.0, 57.4], [11.2, 58.6], [11.1, 59.0], [10.4, 59.2], [10.7, 59.9],
      [9.7, 59.0], [8.1, 58.1], [6.5, 58.1], [5.4, 59.0], [4.9, 60.4],
    ],
    // Finland outline
    [
      [21.0, 68.8], [25.3, 68.4], [28.5, 69.8], [29.9, 66.5], [30.4, 63.5], [28.1, 60.6],
      [25.0, 60.1], [21.3, 60.5], [21.1, 62.8], [22.7, 64.0], [24.8, 65.1], [24.1, 65.8],
      [23.5, 67.6], [21.0, 68.8],
    ],
    // Jutland + Denmark
    [
      [8.2, 55.1], [8.1, 56.9], [9.9, 57.7], [10.6, 57.1], [10.9, 56.3], [9.9, 55.4],
      [9.4, 54.8], [8.5, 54.9], [8.2, 55.1],
    ],
    // Zealand & Bornholm
    [[11.1, 55.5], [12.3, 56.1], [12.6, 55.5], [11.9, 55.0], [11.1, 55.5]],
    [[14.7, 55.1], [15.1, 55.3], [15.1, 55.0], [14.7, 55.1]],
    // Gotland
    [[18.1, 56.9], [19.1, 57.9], [18.8, 57.1], [18.1, 56.9]],
  ];

  const state = {
    data: null,
    mode: "sites",
    yearMin: -9000,
    yearMax: 500,
    includeUndated: true,
    commercialOnly: false,
    methods: new Set([
      "radiocarbon_14c",
      "shoreline_displacement",
      "stratigraphic",
      "typological_stylistic",
      "composite_archival",
      "unknown",
    ]),
    country: "",
    region: "",
    family: "",
    motifClass: "",
    archive: "",
    search: "",
    filteredSites: [],
    filteredFigures: [],
    selectedItem: null,
    selectedType: null,
    svgZoom: 1.0,
    currentSvgXml: "",
    figureById: new Map(),
    chunkCache: new Map(),
    mapEngine: "auto", // "auto" | "leaflet" | "offline"
    leafletMap: null,
    clusterLayer: null,
    offView: { lon: 15.5, lat: 62.8, zoom: 4.3 },
    offDragging: false,
    offLast: null,
    offPoints: [],
  };

  const els = {
    searchInput: document.getElementById("searchInput"),
    commercialOnlyToggle: document.getElementById("commercialOnlyToggle"),
    mapEngineToggle: document.getElementById("mapEngineToggle"),
    yearMinSlider: document.getElementById("yearMinSlider"),
    yearMaxSlider: document.getElementById("yearMaxSlider"),
    yearMinLabel: document.getElementById("yearMinLabel"),
    yearMaxLabel: document.getElementById("yearMaxLabel"),
    includeUndatedToggle: document.getElementById("includeUndatedToggle"),
    datingMethodChecks: document.getElementById("datingMethodChecks"),
    countrySelect: document.getElementById("countrySelect"),
    regionSelect: document.getElementById("regionSelect"),
    familySelect: document.getElementById("familySelect"),
    classSelect: document.getElementById("classSelect"),
    archiveSelect: document.getElementById("archiveSelect"),
    resultsList: document.getElementById("resultsList"),
    listCountBadge: document.getElementById("listCountBadge"),
    statSitesShown: document.getElementById("statSitesShown"),
    statFigsShown: document.getElementById("statFigsShown"),
    statGeoPct: document.getElementById("statGeoPct"),
    statDatedPct: document.getElementById("statDatedPct"),
    statRangeLabel: document.getElementById("statRangeLabel"),
    timelineHistogram: document.getElementById("timelineHistogram"),
    leafletMapDiv: document.getElementById("leafletMap"),
    offlineCanvas: document.getElementById("offlineCanvas"),
    offlineControls: document.getElementById("offlineControls"),
    loadingOverlay: document.getElementById("loadingOverlay"),
    inspectorEmpty: document.getElementById("inspectorEmpty"),
    inspectorContent: document.getElementById("inspectorContent"),
    inspBadges: document.getElementById("inspBadges"),
    inspTitle: document.getElementById("inspTitle"),
    inspSubtitle: document.getElementById("inspSubtitle"),
    svgViewerCard: document.getElementById("svgViewerCard"),
    svgContainer: document.getElementById("svgContainer"),
    svgCaption: document.getElementById("svgCaption"),
    siteFiguresCard: document.getElementById("siteFiguresCard"),
    siteFiguresList: document.getElementById("siteFiguresList"),
    inspChronology: document.getElementById("inspChronology"),
    inspGeo: document.getElementById("inspGeo"),
    inspProvenance: document.getElementById("inspProvenance"),
  };

  function fmtYear(y) {
    if (y === null || y === undefined) return "Undated";
    if (y < 0) return `${Math.abs(Math.round(y))} BCE`;
    if (y === 0) return "1 BCE / 1 CE";
    return `${Math.round(y)} CE`;
  }

  function escHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function colorForRecord(rec, isFigure) {
    if (isFigure) return "#dc2626";
    if (rec.pe === "stone_age_hunter") return "#0f766e";
    if (rec.pe === "bronze_age_agrarian") return "#b45309";
    return "#6d28d9";
  }

  function matchesFilters(rec, isFigure) {
    if (state.commercialOnly && isFigure && !rec.co) return false;
    if (state.commercialOnly && !isFigure && rec.fc > 0 && rec.cc === 0) return false;

    if (state.country && rec.c !== state.country) return false;
    if (state.region && rec.cr !== state.region) return false;
    if (state.archive && !(rec.ar || "").toLowerCase().includes(state.archive.toLowerCase())) return false;

    if (isFigure) {
      if (state.family && rec.mf !== state.family) return false;
      if (state.motifClass && rec.mc !== state.motifClass) return false;
    } else if (state.family || state.motifClass) {
      // When motif family/class is selected, restrict sites to those matching or linked
      const ms = (rec.ms || "").toLowerCase();
      if (state.motifClass && !ms.includes(state.motifClass.replace(/_/g, " ")) && rec.fc === 0) return false;
    }

    const dm = rec.dm || "unknown";
    if (!state.methods.has(dm)) return false;

    if (rec.df === null || rec.dt === null) {
      if (!state.includeUndated) return false;
    } else {
      if (rec.dt < state.yearMin || rec.df > state.yearMax) return false;
    }

    if (state.search) {
      const q = state.search;
      const hay = isFigure
        ? `${rec.id} ${rec.p || ""} ${rec.s || ""} ${rec.r || ""} ${rec.mc || ""} ${rec.si || ""}`.toLowerCase()
        : `${rec.u} ${rec.p || ""} ${rec.n || ""} ${rec.k || ""} ${rec.r || ""} ${rec.ms || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }

  function populateDropdowns() {
    const regions = new Set();
    const families = new Set();
    const classes = new Set();
    const archives = new Set();

    for (const s of state.data.sites) {
      if (s.cr) regions.add(s.cr);
      for (const a of (s.ar || "").split(",")) {
        const t = a.trim();
        if (t) archives.add(t);
      }
    }
    for (const f of state.data.figures) {
      if (f.cr) regions.add(f.cr);
      if (f.mf) families.add(f.mf);
      if (f.mc) classes.add(f.mc);
      if (f.ar) archives.add(f.ar);
    }

    const fill = (sel, values) => {
      for (const v of Array.from(values).sort()) {
        const opt = document.createElement("option");
        opt.value = v;
        opt.textContent = v;
        sel.appendChild(opt);
      }
    };
    fill(els.regionSelect, regions);
    fill(els.familySelect, families);
    fill(els.classSelect, classes);
    fill(els.archiveSelect, archives);
  }

  function renderTimelineHistogram() {
    const bins = 38; // 250-year bins from -9000 to +500
    const counts = new Array(bins).fill(0);
    const items = state.mode === "figures" ? state.data.figures : state.data.sites;

    for (const rec of items) {
      if (rec.df === null || rec.dt === null) continue;
      const mid = (rec.df + rec.dt) * 0.5;
      const idx = Math.max(0, Math.min(bins - 1, Math.floor(((mid + 9000) / 9500) * bins)));
      counts[idx] += 1;
    }
    const maxC = Math.max(1, ...counts);
    const w = 280 / bins;
    let svg = "";
    for (let i = 0; i < bins; i++) {
      const bStart = -9000 + (i / bins) * 9500;
      const bEnd = -9000 + ((i + 1) / bins) * 9500;
      const active = bEnd >= state.yearMin && bStart <= state.yearMax;
      const h = Math.max(2, Math.round((counts[i] / maxC) * 38));
      const fill = active ? "#9a3412" : "#cbd5e1";
      svg += `<rect x="${(i * w).toFixed(1)}" y="${44 - h}" width="${Math.max(2, w - 1).toFixed(1)}" height="${h}" fill="${fill}" rx="1" />`;
    }
    els.timelineHistogram.innerHTML = svg;
  }

  function usingLeaflet() {
    if (state.mapEngine === "offline") return false;
    return typeof window.L !== "undefined" && typeof window.L.markerClusterGroup === "function";
  }

  function initMaps() {
    if (usingLeaflet()) {
      els.leafletMapDiv.classList.remove("hidden");
      els.offlineCanvas.classList.add("hidden");
      els.offlineControls.classList.add("hidden");
      els.mapEngineToggle.textContent = "Map: Leaflet";

      if (!state.leafletMap) {
        const map = window.L.map("leafletMap", { preferCanvas: true }).setView([62.8, 15.5], 4);
        window.L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", {
          attribution: "&copy; OpenStreetMap &copy; CARTO",
          maxZoom: 18,
        }).addTo(map);
        state.clusterLayer = window.L.markerClusterGroup({
          chunkedLoading: true,
          maxClusterRadius: 48,
          spiderfyOnMaxZoom: true,
        });
        map.addLayer(state.clusterLayer);
        state.leafletMap = map;
      }
    } else {
      els.leafletMapDiv.classList.add("hidden");
      els.offlineCanvas.classList.remove("hidden");
      els.offlineControls.classList.remove("hidden");
      els.mapEngineToggle.textContent = "Map: Offline Vector";
      resizeOfflineCanvas();
    }
  }

  function renderMapPoints() {
    const markersData = [];
    if (state.mode === "sites" || state.mode === "both") {
      for (const s of state.filteredSites) {
        if (s.la !== null && s.lo !== null) {
          markersData.push({ rec: s, isFigure: false });
        }
      }
    }
    if (state.mode === "figures" || state.mode === "both") {
      for (const f of state.filteredFigures) {
        if (f.la !== null && f.lo !== null) {
          markersData.push({ rec: f, isFigure: true });
        }
      }
    }

    if (usingLeaflet() && state.leafletMap && state.clusterLayer) {
      state.clusterLayer.clearLayers();
      const layers = [];
      // Cap direct Leaflet markers at 15,000 for smooth 60fps clustering
      const step = markersData.length > 15000 ? Math.ceil(markersData.length / 15000) : 1;
      for (let i = 0; i < markersData.length; i += step) {
        const { rec, isFigure } = markersData[i];
        const marker = window.L.circleMarker([rec.la, rec.lo], {
          radius: isFigure ? 5 : 6,
          fillColor: colorForRecord(rec, isFigure),
          color: "#ffffff",
          weight: 1.2,
          fillOpacity: 0.88,
        });
        marker.bindTooltip(escHtml(isFigure ? `${rec.id} (${rec.mc})` : `${rec.n} (${rec.p || rec.c})`));
        marker.on("click", () => inspectRecord(rec, isFigure));
        layers.push(marker);
      }
      state.clusterLayer.addLayers(layers);
    } else {
      state.offPoints = markersData;
      drawOfflineMap();
    }
  }

  function resizeOfflineCanvas() {
    const c = els.offlineCanvas;
    const rect = c.parentElement.getBoundingClientRect();
    c.width = Math.max(300, Math.floor(rect.width));
    c.height = Math.max(300, Math.floor(rect.height));
  }

  function projectLonLat(lon, lat, w, h) {
    const scale = 24 * Math.pow(1.45, state.offView.zoom - 4);
    const latFactor = 1 / Math.cos((state.offView.lat * Math.PI) / 180);
    const x = w * 0.5 + (lon - state.offView.lon) * scale;
    const y = h * 0.5 - (lat - state.offView.lat) * scale * latFactor;
    return [x, y];
  }

  function drawOfflineMap() {
    const c = els.offlineCanvas;
    if (c.classList.contains("hidden")) return;
    const ctx = c.getContext("2d");
    const w = c.width;
    const h = c.height;

    ctx.fillStyle = "#dce8ef";
    ctx.fillRect(0, 0, w, h);

    // Subtle graticule
    ctx.strokeStyle = "rgba(15, 76, 92, 0.12)";
    ctx.lineWidth = 1;
    for (let lon = 0; lon <= 35; lon += 5) {
      const [x0, y0] = projectLonLat(lon, 72, w, h);
      const [x1, y1] = projectLonLat(lon, 54, w, h);
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
    }
    for (let lat = 55; lat <= 70; lat += 5) {
      const [x0, y0] = projectLonLat(2, lat, w, h);
      const [x1, y1] = projectLonLat(34, lat, w, h);
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.stroke();
    }

    // Nordic land masses
    ctx.fillStyle = "#f2efe6";
    ctx.strokeStyle = "#b8b2a2";
    ctx.lineWidth = 1.2;
    for (const poly of NORDIC_POLYS) {
      ctx.beginPath();
      poly.forEach(([lo, la], idx) => {
        const [x, y] = projectLonLat(lo, la, w, h);
        if (idx === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    }

    // Simple grid-cell clustering on canvas
    const cell = 24;
    const buckets = new Map();
    for (const pt of state.offPoints) {
      const [x, y] = projectLonLat(pt.rec.lo, pt.rec.la, w, h);
      if (x < -20 || y < -20 || x > w + 20 || y > h + 20) continue;
      const key = `${Math.floor(x / cell)},${Math.floor(y / cell)}`;
      let b = buckets.get(key);
      if (!b) {
        b = { x: 0, y: 0, count: 0, first: pt };
        buckets.set(key, b);
      }
      b.x += x;
      b.y += y;
      b.count += 1;
    }

    state._drawnClusters = [];
    for (const b of buckets.values()) {
      const cx = b.x / b.count;
      const cy = b.y / b.count;
      if (b.count === 1) {
        ctx.beginPath();
        ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
        ctx.fillStyle = colorForRecord(b.first.rec, b.first.isFigure);
        ctx.fill();
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1;
        ctx.stroke();
        state._drawnClusters.push({ cx, cy, r: 7, item: b.first });
      } else {
        const rad = Math.min(18, 8 + Math.log2(b.count) * 2.1);
        ctx.beginPath();
        ctx.arc(cx, cy, rad, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(154, 52, 18, 0.88)";
        ctx.fill();
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 1.5;
        ctx.stroke();
        ctx.fillStyle = "#fff";
        ctx.font = "bold 10px monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(String(b.count), cx, cy);
        state._drawnClusters.push({ cx, cy, r: rad + 2, count: b.count, item: b.first });
      }
    }
  }

  function updateView() {
    state.filteredSites = state.data.sites.filter((s) => matchesFilters(s, false));
    state.filteredFigures = state.data.figures.filter((f) => matchesFilters(f, true));

    const activeList = state.mode === "figures" ? state.filteredFigures : state.filteredSites;
    const isFigMode = state.mode === "figures";

    const geoTotal = activeList.filter((r) => r.la !== null && r.lo !== null).length;
    const datedTotal = activeList.filter((r) => r.df !== null).length;

    els.statSitesShown.textContent = state.filteredSites.length.toLocaleString();
    els.statFigsShown.textContent = state.filteredFigures.length.toLocaleString();
    els.statGeoPct.textContent = activeList.length ? `${((100 * geoTotal) / activeList.length).toFixed(1)}%` : "0%";
    els.statDatedPct.textContent = activeList.length ? `${((100 * datedTotal) / activeList.length).toFixed(1)}%` : "0%";
    els.statRangeLabel.textContent = `${fmtYear(state.yearMin)} – ${fmtYear(state.yearMax)}`;
    els.listCountBadge.textContent = activeList.length.toLocaleString();

    renderTimelineHistogram();
    renderResultsList(activeList, isFigMode);
    renderMapPoints();
  }

  function renderResultsList(items, isFigMode) {
    const cap = items.slice(0, 120);
    els.resultsList.innerHTML = "";
    const frag = document.createDocumentFragment();
    for (const rec of cap) {
      const btn = document.createElement("button");
      btn.className = "result-item";
      const title = isFigMode ? `${rec.id} · ${rec.mc}` : rec.n;
      const span = rec.df !== null ? `${fmtYear(rec.df)}–${fmtYear(rec.dt)}` : "Undated";
      const sub = isFigMode
        ? `${rec.s || rec.r || rec.c} · ${span} · ${rec.ar}`
        : `${rec.p || rec.c} · ${rec.r || rec.k || ""} · ${span}${rec.fc ? ` · ${rec.fc} traced` : ""}`;
      btn.innerHTML = `<div class="r-title">${escHtml(title)}</div><div class="r-meta">${escHtml(sub)}</div>`;
      btn.addEventListener("click", () => {
        inspectRecord(rec, isFigMode);
        if (rec.la !== null && rec.lo !== null) {
          if (usingLeaflet() && state.leafletMap) {
            state.leafletMap.setView([rec.la, rec.lo], 11);
          } else {
            state.offView = { lon: rec.lo, lat: rec.la, zoom: 7.2 };
            drawOfflineMap();
          }
        }
      });
      frag.appendChild(btn);
    }
    els.resultsList.appendChild(frag);
  }

  async function loadSvgForFigure(fig) {
    els.svgViewerCard.classList.remove("hidden");
    els.svgContainer.innerHTML = "<p>Loading vector layers…</p>";
    els.svgCaption.textContent = "";

    let payload = null;
    if (fig.ch) {
      if (!state.chunkCache.has(fig.ch)) {
        try {
          const resp = await fetch(fig.ch);
          if (resp.ok) {
            state.chunkCache.set(fig.ch, await resp.json());
          }
        } catch (_) {
          // Fallback to direct SVG fetch below
        }
      }
      const chunk = state.chunkCache.get(fig.ch);
      if (chunk && chunk[fig.id]) {
        payload = chunk[fig.id];
      }
    }

    if (payload) {
      const vbParts = (payload.viewBox || "0 0 100 100").split(/\s+/).map(Number);
      const maxDim = Math.max(vbParts[2] || 100, vbParts[3] || 100);
      const sw = Math.max(0.4, Math.min(2.2, maxDim * 0.0045)).toFixed(2);
      const circles = (payload.cup_marks || [])
        .map(([cx, cy, r], i) => `<circle id="cm_${i}" cx="${cx}" cy="${cy}" r="${r}" />`)
        .join("");
      const cracks = (payload.cracks_d || [])
        .map((d, i) => `<path id="crk_${i}" d="${escHtml(d)}" />`)
        .join("");
      const uncert = (payload.uncertain_d || [])
        .map((d, i) => `<path id="unc_${i}" d="${escHtml(d)}" stroke-dasharray="2 2" />`)
        .join("");

      const svgHtml = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${escHtml(payload.viewBox)}" width="100%" height="100%">
        <g id="figures"><path d="${escHtml(payload.figures_d)}" fill="#18181b" fill-rule="evenodd" stroke="none"/></g>
        <g id="cup_marks" fill="#9a3412" fill-opacity="0.35" stroke="#9a3412" stroke-width="${sw}">${circles}</g>
        <g id="cracks" fill="none" stroke="#dc2626" stroke-width="${sw}" stroke-linecap="round">${cracks}</g>
        <g id="uncertain" fill="none" stroke="#6b7280" stroke-width="${sw}" stroke-dasharray="2 2">${uncert}</g>
        <g id="scale_bar">${payload.scale_bar_svg || ""}</g>
      </svg>`;
      state.currentSvgXml = svgHtml;
      els.svgContainer.innerHTML = svgHtml;
      applyLayerToggles();
      const dimText = fig.sc
        ? `${fig.wm} × ${fig.hm} mm (calibrated)`
        : `${Math.round(fig.wp)} × ${Math.round(fig.hp)} px (uncalibrated plate)`;
      els.svgCaption.textContent = `${dimText} · Cup marks: ${fig.cm} · Cracks: ${fig.ck} · Uncertain: ${fig.un}`;
      return;
    }

    // Fallback direct fetch
    try {
      const resp = await fetch(`../${fig.sp}`);
      if (resp.ok) {
        const xml = await resp.text();
        state.currentSvgXml = xml;
        els.svgContainer.innerHTML = xml;
        applyLayerToggles();
        return;
      }
    } catch (_) {}
    els.svgContainer.innerHTML = "<p>SVG preview unavailable in this context.</p>";
  }

  function applyLayerToggles() {
    const svg = els.svgContainer.querySelector("svg");
    if (!svg) return;
    svg.style.transform = `scale(${state.svgZoom})`;
    svg.style.transformOrigin = "center center";
    document.querySelectorAll(".layer-cb").forEach((cb) => {
      const g = svg.querySelector(`#${cb.dataset.layer}`);
      if (g) g.style.display = cb.checked ? "" : "none";
    });
  }

  function inspectRecord(rec, isFigure) {
    state.selectedItem = rec;
    state.selectedType = isFigure ? "figure" : "site";
    state.svgZoom = 1.0;

    els.inspectorEmpty.classList.add("hidden");
    els.inspectorContent.classList.remove("hidden");

    const badges = [];
    if (rec.p) badges.push(`<span class="badge">${escHtml(rec.p)}</span>`);
    badges.push(`<span class="badge badge-period">${escHtml(rec.pe || "unknown")}</span>`);
    if (isFigure) {
      badges.push(
        rec.co
          ? `<span class="badge badge-ok">Commercial-OK</span>`
          : `<span class="badge badge-nc">Research-Only (NC)</span>`
      );
      if (rec.io !== null && rec.io !== undefined) {
        badges.push(`<span class="badge badge-qa">Dual-Trace IoU: ${Number(rec.io).toFixed(3)}</span>`);
      }
    }
    els.inspBadges.innerHTML = badges.join("");
    els.inspTitle.textContent = isFigure ? `${rec.id} (${rec.mc})` : rec.n;
    els.inspSubtitle.textContent = isFigure
      ? `${rec.s || "Unknown site"} · ${rec.r || ""} · ${rec.c}`
      : `${rec.r || ""} ${rec.k ? `· ${rec.k}` : ""} · ${rec.c}`;

    if (isFigure) {
      els.siteFiguresCard.classList.add("hidden");
      loadSvgForFigure(rec);
    } else {
      // If site has sample figures, load first traced figure into SVG viewer and list all samples
      const linked = (rec.sf || []).map((id) => state.figureById.get(id)).filter(Boolean);
      if (linked.length > 0) {
        els.siteFiguresCard.classList.remove("hidden");
        els.siteFiguresList.innerHTML = "";
        for (const lf of linked) {
          const b = document.createElement("button");
          b.className = "fig-chip";
          b.textContent = `${lf.id} (${lf.mc})`;
          b.addEventListener("click", () => inspectRecord(lf, true));
          els.siteFiguresList.appendChild(b);
        }
        loadSvgForFigure(linked[0]);
      } else {
        els.siteFiguresCard.classList.add("hidden");
        els.svgViewerCard.classList.add("hidden");
      }
    }

    const spanStr = rec.df !== null ? `${fmtYear(rec.df)} — ${fmtYear(rec.dt)}` : "Undated";
    els.inspChronology.innerHTML = `
      <dt>Period</dt><dd>${escHtml(rec.pl || rec.pe || "Unknown")}</dd>
      <dt>Date Span</dt><dd><code>${escHtml(spanStr)}</code></dd>
      <dt>Dating Method</dt><dd><code>${escHtml(rec.dm || "unknown")}</code></dd>
      ${rec.dx ? `<dt>Archival Text</dt><dd>${escHtml(rec.dx)}</dd>` : ""}
      ${rec.el !== null && rec.el !== undefined ? `<dt>Elevation</dt><dd>${rec.el} m a.s.l.</dd>` : ""}
      <dt>Citation</dt><dd>${escHtml(rec.dc || "—")}</dd>
    `;

    const coordsStr = rec.la !== null && rec.lo !== null ? `${rec.la.toFixed(6)}, ${rec.lo.toFixed(6)}` : "Unverified (null)";
    els.inspGeo.innerHTML = `
      <dt>EPSG:4326</dt><dd><code>${escHtml(coordsStr)}</code></dd>
      <dt>Geo Level</dt><dd><code>${escHtml(rec.gl || "unverified")}</code></dd>
      <dt>Precision (m)</dt><dd>${rec.gp !== null && rec.gp !== undefined ? `±${rec.gp} m` : "—"}</dd>
      ${rec.cs ? `<dt>Source</dt><dd>${escHtml(rec.cs)}</dd>` : ""}
    `;

    const extLink = rec.ur
      ? `<a href="${escHtml(rec.ur)}" target="_blank" rel="noopener">Open archival source ↗</a>`
      : "—";
    els.inspProvenance.innerHTML = isFigure
      ? `
        <dt>Archive</dt><dd>${escHtml(rec.ar)} (<code>${escHtml(rec.si)}</code>)</dd>
        <dt>Media Type</dt><dd><code>${escHtml(rec.mt)}</code></dd>
        <dt>SHA-256</dt><dd><code>${escHtml(rec.sh)}…</code></dd>
        <dt>Rights Holder</dt><dd>${escHtml(rec.rh)}</dd>
        <dt>Licence</dt><dd>${escHtml(rec.li)}</dd>
        <dt>Confidence</dt><dd><code>${Number(rec.cf).toFixed(3)}</code></dd>
        <dt>Dual-Trace QA</dt><dd><code>${escHtml(rec.qs)}</code>${rec.io !== null && rec.io !== undefined ? ` (IoU ${Number(rec.io).toFixed(4)})` : ""}</dd>
        <dt>Literature</dt><dd>${escHtml(rec.lr || "—")}</dd>
        <dt>Source URL</dt><dd>${extLink}</dd>
      `
      : `
        <dt>Archives</dt><dd>${escHtml(rec.ar || "—")}</dd>
        <dt>Traced Figures</dt><dd><code>${rec.fc || 0}</code> (${rec.cc || 0} commercial-OK)</dd>
        <dt>Motifs</dt><dd>${escHtml(rec.ms || "—")}</dd>
        <dt>Source URL</dt><dd>${extLink}</dd>
      `;
  }

  function wireEvents() {
    document.querySelectorAll(".seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.mode = btn.dataset.mode;
        updateView();
      });
    });

    els.searchInput.addEventListener("input", () => {
      state.search = els.searchInput.value.trim().toLowerCase();
      updateView();
    });

    els.commercialOnlyToggle.addEventListener("change", () => {
      state.commercialOnly = els.commercialOnlyToggle.checked;
      updateView();
    });

    els.includeUndatedToggle.addEventListener("change", () => {
      state.includeUndated = els.includeUndatedToggle.checked;
      updateView();
    });

    const syncSliders = () => {
      let a = Number(els.yearMinSlider.value);
      let b = Number(els.yearMaxSlider.value);
      if (a > b) [a, b] = [b, a];
      state.yearMin = a;
      state.yearMax = b;
      els.yearMinLabel.textContent = fmtYear(a);
      els.yearMaxLabel.textContent = fmtYear(b);
      updateView();
    };
    els.yearMinSlider.addEventListener("input", syncSliders);
    els.yearMaxSlider.addEventListener("input", syncSliders);

    document.querySelectorAll(".era-presets .chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        document.querySelectorAll(".era-presets .chip").forEach((c) => c.classList.remove("active"));
        chip.classList.add("active");
        const [a, b] = chip.dataset.range.split(",").map(Number);
        els.yearMinSlider.value = a;
        els.yearMaxSlider.value = b;
        syncSliders();
      });
    });

    els.datingMethodChecks.addEventListener("change", () => {
      const checked = new Set();
      els.datingMethodChecks.querySelectorAll("input:checked").forEach((cb) => checked.add(cb.value));
      state.methods = checked;
      updateView();
    });

    els.countrySelect.addEventListener("change", () => { state.country = els.countrySelect.value; updateView(); });
    els.regionSelect.addEventListener("change", () => { state.region = els.regionSelect.value; updateView(); });
    els.familySelect.addEventListener("change", () => { state.family = els.familySelect.value; updateView(); });
    els.classSelect.addEventListener("change", () => { state.motifClass = els.classSelect.value; updateView(); });
    els.archiveSelect.addEventListener("change", () => { state.archive = els.archiveSelect.value; updateView(); });

    document.getElementById("resetFiltersBtn").addEventListener("click", () => {
      els.searchInput.value = "";
      state.search = "";
      els.yearMinSlider.value = -9000;
      els.yearMaxSlider.value = 500;
      state.yearMin = -9000;
      state.yearMax = 500;
      els.yearMinLabel.textContent = fmtYear(-9000);
      els.yearMaxLabel.textContent = fmtYear(500);
      els.includeUndatedToggle.checked = true;
      state.includeUndated = true;
      els.commercialOnlyToggle.checked = false;
      state.commercialOnly = false;
      els.countrySelect.value = "";
      els.regionSelect.value = "";
      els.familySelect.value = "";
      els.classSelect.value = "";
      els.archiveSelect.value = "";
      state.country = state.region = state.family = state.motifClass = state.archive = "";
      els.datingMethodChecks.querySelectorAll("input").forEach((cb) => {
        cb.checked = true;
        state.methods.add(cb.value);
      });
      updateView();
    });

    els.mapEngineToggle.addEventListener("click", () => {
      state.mapEngine = usingLeaflet() ? "offline" : "leaflet";
      initMaps();
      renderMapPoints();
    });

    // Offline canvas pan/zoom/click
    els.offlineCanvas.addEventListener("mousedown", (e) => {
      state.offDragging = true;
      state.offLast = [e.clientX, e.clientY];
    });
    window.addEventListener("mouseup", () => { state.offDragging = false; });
    window.addEventListener("mousemove", (e) => {
      if (!state.offDragging || !state.offLast) return;
      const dx = e.clientX - state.offLast[0];
      const dy = e.clientY - state.offLast[1];
      state.offLast = [e.clientX, e.clientY];
      const scale = 24 * Math.pow(1.45, state.offView.zoom - 4);
      state.offView.lon -= dx / scale;
      state.offView.lat += dy / (scale * 1.9);
      drawOfflineMap();
    });
    els.offlineCanvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      state.offView.zoom = Math.max(2.5, Math.min(12, state.offView.zoom + (e.deltaY < 0 ? 0.5 : -0.5)));
      drawOfflineMap();
    }, { passive: false });
    els.offlineCanvas.addEventListener("click", (e) => {
      const rect = els.offlineCanvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      for (const c of (state._drawnClusters || [])) {
        if (Math.hypot(mx - c.cx, my - c.cy) <= c.r) {
          if (c.count && c.count > 1) {
            state.offView.lon = c.item.rec.lo;
            state.offView.lat = c.item.rec.la;
            state.offView.zoom = Math.min(12, state.offView.zoom + 1.5);
            drawOfflineMap();
          } else {
            inspectRecord(c.item.rec, c.item.isFigure);
          }
          break;
        }
      }
    });
    document.getElementById("zoomInBtn").addEventListener("click", () => {
      state.offView.zoom = Math.min(12, state.offView.zoom + 0.8);
      drawOfflineMap();
    });
    document.getElementById("zoomOutBtn").addEventListener("click", () => {
      state.offView.zoom = Math.max(2.5, state.offView.zoom - 0.8);
      drawOfflineMap();
    });
    document.getElementById("resetViewBtn").addEventListener("click", () => {
      state.offView = { lon: 15.5, lat: 62.8, zoom: 4.3 };
      drawOfflineMap();
    });
    window.addEventListener("resize", () => {
      if (!els.offlineCanvas.classList.contains("hidden")) {
        resizeOfflineCanvas();
        drawOfflineMap();
      }
    });

    // Inspector & SVG controls
    document.getElementById("closeInspectorBtn").addEventListener("click", () => {
      els.inspectorContent.classList.add("hidden");
      els.inspectorEmpty.classList.remove("hidden");
    });
    document.querySelectorAll(".layer-cb").forEach((cb) => {
      cb.addEventListener("change", applyLayerToggles);
    });
    document.getElementById("svgZoomIn").addEventListener("click", () => {
      state.svgZoom = Math.min(4.0, state.svgZoom + 0.25);
      applyLayerToggles();
    });
    document.getElementById("svgZoomOut").addEventListener("click", () => {
      state.svgZoom = Math.max(0.4, state.svgZoom - 0.25);
      applyLayerToggles();
    });
    document.getElementById("svgZoomReset").addEventListener("click", () => {
      state.svgZoom = 1.0;
      applyLayerToggles();
    });
    document.getElementById("downloadSvgBtn").addEventListener("click", () => {
      if (!state.currentSvgXml) return;
      const blob = new Blob([state.currentSvgXml], { type: "image/svg+xml;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${(state.selectedItem && (state.selectedItem.id || state.selectedItem.u)) || "figure"}.svg`;
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  async function boot() {
    try {
      const resp = await fetch("explorer_data.json");
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      state.data = await resp.json();
      const strTables = state.data.strings || {};
      const decodeStr = (k, v) => (typeof v === "number" && strTables[k] ? strTables[k][v] : v);
      for (const s of state.data.sites) {
        s.pl = decodeStr("pl", s.pl);
        s.dc = decodeStr("dc", s.dc);
        s.cs = decodeStr("cs", s.cs);
      }
      for (const f of state.data.figures) {
        f.pl = decodeStr("pl", f.pl);
        f.dc = decodeStr("dc", f.dc);
      }
      state.figureById = new Map(state.data.figures.map((f) => [f.id, f]));
      populateDropdowns();
      wireEvents();
      initMaps();
      updateView();
      els.loadingOverlay.classList.add("hidden");
    } catch (err) {
      els.loadingOverlay.innerHTML = `<p style="color:#dc2626">Failed to load explorer_data.json (${escHtml(err.message)}). Serve via HTTP (e.g. <code>python -m http.server</code>).</p>`;
    }
  }

  boot();
})();
