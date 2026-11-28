/* Formverk Studio editor, cut-layer inspector and play tester. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const art = window.FormverkArtwork;
  const stage = $('stageSvg');
  const phaseDefaults = { dendrite: [40, 360], amoeba: [44, 340], linear: [38, 340], grid: [40, 350] };
  const state = {
    view: 'design',
    style: 'dendrite',
    catalog: [],
    placed: [],
    shapes: {},
    drawPoints: [],
    photo: null,
    back: null,
    result: null,
    styleOverrides: {},
    regions: [],
    revision: 0,
  };
  let previousHeight = 190,
    loading = 0,
    drawing = false,
    running = false,
    lookup = 0,
    placementEdit = false,
    boardLoad = 0;
  const photoLoads = { photo: 0, back: 0 };
  const pendingImages = new Set();
  function syncGenerate() {
    $('generateBtn').disabled = running || pendingImages.size > 0;
  }
  const metricIds = ['mStatus', 'mPieces', 'mLock', 'mKerf', 'mNeck', 'mGeom', 'mLen'];
  const player = new window.FormverkPlayer(stage, $('pieceLayer'), (info) => {
    $('playScore').textContent =
      info.groups === 1 ? 'Solved' : info.largest + ' / ' + info.total + ' connected';
  });

  function svg(tag, attrs = {}) {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
  }
  function line(points, close = false) {
    return points?.length ? 'M' + points.map((p) => p.join(',')).join('L') + (close ? 'Z' : '') : '';
  }
  function shape() {
    return state.shapes[$('outlineSelect').value];
  }
  function dims() {
    const w = Number($('widthInput').value) || 260,
      h = Number($('heightInput').value) || 190;
    return { w, h: $('outlineSelect').value === 'circle' ? w : shape()?.aspect ? w / shape().aspect : h };
  }
  function cropOf(w, h) {
    if (!state.photo || $('artPattern').value !== 'photo') return null;
    return art.cropToAspect(state.photo.image, w / h, shape()?.crop);
  }
  function point(e) {
    const pt = stage.createSVGPoint();
    pt.x = e.clientX;
    pt.y = e.clientY;
    return pt.matrixTransform(stage.getScreenCTM().inverse());
  }
  function message(msg) {
    $('progressMsg').textContent = msg;
  }
  async function request(path, body) {
    const res = await fetch(
      path,
      body === undefined
        ? undefined
        : {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          },
    );
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Request failed (' + res.status + ').');
    return data;
  }
  function setDownloads(urls) {
    for (const [id, key] of Object.entries({
      dlCutSvg: 'cut_svg_url',
      dlArtSvg: 'artwork_svg_url',
      dlBackSvg: 'back_svg_url',
      dlAuditJson: 'audit_url',
    })) {
      const a = $(id),
        href = urls?.[key];
      if (href) a.href = href;
      else a.removeAttribute('href');
      a.classList.toggle('disabled', !href);
      a.setAttribute('aria-disabled', String(!href));
      if (id === 'dlBackSvg') a.hidden = !href;
    }
  }
  function changeView(view) {
    state.view = view;
    document.querySelectorAll('.tab').forEach((btn) => {
      const active = btn.dataset.view === view;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', String(active));
    });
    $('layerToggles').hidden = view !== 'cut';
    $('playControls').hidden = view !== 'play';
  }
  function edit() {
    state.revision++;
    if (state.result) {
      state.result = null;
      $('showcaseSelect').value = '';
      setDownloads(null);
      metricIds.forEach((id) => {
        $(id).textContent = '—';
        $(id).className = '';
      });
      message('Design changed. Generate to update the cut files.');
    }
    placementEdit = true;
    changeView('design');
  }
  function syncDimensions() {
    const lock = $('outlineSelect').value === 'circle' || !!shape()?.aspect;
    if (lock) {
      if (!$('heightInput').disabled) previousHeight = Number($('heightInput').value) || 190;
      const { h } = dims();
      $('heightInput').value = +h.toFixed(2);
    } else if ($('heightInput').disabled) {
      $('heightInput').value = previousHeight;
    }
    $('heightInput').disabled = lock;
    photoInfo();
  }
  function photoInfo() {
    const { w, h } = dims(),
      crop = cropOf(w, h);
    if (!crop) {
      $('photoInfo').textContent = '';
      return;
    }
    const dpi = Math.round(25.4 * Math.min(crop.w / w, crop.h / h));
    $('photoInfo').textContent =
      state.photo.name + ' · ' + dpi + ' pixels/inch at this size' + (dpi < 100 ? ' · low resolution' : '');
  }
  function setPattern(url, w, h, image, crop) {
    const p = $('pieceArt');
    p.replaceChildren();
    if (!url) return false;
    p.setAttribute('width', w);
    p.setAttribute('height', h);
    const frame = crop
      ? {
          x: (-crop.x * w) / crop.w,
          y: (-crop.y * h) / crop.h,
          width: (image.naturalWidth * w) / crop.w,
          height: (image.naturalHeight * h) / crop.h,
        }
      : { x: 0, y: 0, width: w, height: h };
    p.append(svg('image', { href: url, ...frame, preserveAspectRatio: 'none' }));
    return true;
  }
  function drawLines(target, paths, color, width) {
    for (const pts of paths || [])
      target.append(
        svg('path', {
          d: line(pts),
          fill: 'none',
          stroke: color,
          'stroke-width': width,
          'stroke-linejoin': 'round',
          'stroke-linecap': 'round',
        }),
      );
  }
  function drawBoard(w, h, fill) {
    const attrs = { fill, stroke: '#514e43', 'stroke-width': 0.6 };
    let node;
    if ($('outlineSelect').value === 'circle')
      node = svg('circle', { cx: w / 2, cy: h / 2, r: w / 2, ...attrs });
    else if (shape())
      node = svg('path', {
        d: line(
          shape().points.map(([x, y]) => [x * w, y * h]),
          true,
        ),
        ...attrs,
      });
    else node = svg('rect', { x: 0, y: 0, width: w, height: h, ...attrs });
    $('pieceLayer').append(node);
  }
  function render() {
    const pz = state.result?.puzzle;
    const d = dims(),
      w = pz?.width_mm ?? d.w,
      h = pz?.height_mm ?? d.h;
    if (!(w > 0 && h > 0)) return;
    const pad = Math.max(w, h) * (state.view === 'play' ? 0.65 : 0.07);
    stage.setAttribute('viewBox', [-pad, -pad, w + 2 * pad, h + 2 * pad].join(' '));
    for (const id of ['artworkLayer', 'cutLayer', 'engraveLayer', 'whimsyEditLayer', 'drawingLayer'])
      $(id).replaceChildren();
    const crop = cropOf(w, h);
    let paint = null;
    if (!pz && crop && state.view === 'design') {
      setPattern(state.photo.url, w, h, state.photo.image, crop);
      paint = 'url(#pieceArt)';
    } else {
      $('pieceArt').replaceChildren();
    }
    if (state.view === 'play') {
      if (pz) {
        if (player.puzzle !== pz) player.load(pz);
        else player.render();
      } else {
        $('pieceLayer').replaceChildren();
        $('playScore').textContent = 'Generate a puzzle to play.';
      }
      return;
    }
    $('pieceLayer').replaceChildren();
    if (state.view === 'cut') {
      if (pz) {
        if ($('showInt').checked) drawLines($('cutLayer'), pz.interior_cuts, '#d02c29', 0.35);
        if ($('showExt').checked) drawLines($('cutLayer'), pz.exterior_cuts, '#2956b3', 0.45);
        if ($('showEng').checked) drawLines($('engraveLayer'), pz.engrave_paths, '#218653', 0.38);
      }
      return;
    }
    if (pz) {
      paint = window.FormverkPlayer.artworkPaint($('pieceLayer'), pz.artwork);
      for (const piece of pz.pieces)
        $('pieceLayer').append(
          svg('path', {
            d: line(piece.outline, true) + (piece.holes || []).map((ring) => line(ring, true)).join(''),
            'fill-rule': 'evenodd',
            fill: paint || piece.color,
            stroke: '#403a32',
            'stroke-width': 0.3,
            'stroke-linejoin': 'round',
          }),
        );
      drawLines($('engraveLayer'), pz.engrave_paths, '#583b28', 0.38);
    } else {
      drawBoard(w, h, paint || ($('themeSelect').value === 'walnut' ? '#a98e74' : '#ecd8b7'));
      for (const region of state.regions) {
        if (!region.points) continue;
        const overlay = svg('path', {
          d: line(
            region.points.map(([x, y]) => [x * w, y * h]),
            true,
          ),
          fill: '#246a7826',
          stroke: '#287386',
          'stroke-width': 0.6,
          'stroke-dasharray': '3 1.5',
        });
        const title = svg('title');
        title.textContent = region.style + ' · ' + region.pieces + ' pieces';
        overlay.append(title);
        $('drawingLayer').append(overlay);
      }
    }
    if (drawing && $('outlineSelect').value === 'draw') {
      const markers = $('drawingLayer'),
        pts = state.drawPoints.map(([x, y]) => [x * w, y * h]);
      drawLines(markers, [pts], '#224f40', 0.8);
      for (const [x, y] of pts)
        markers.append(svg('circle', { cx: x, cy: y, r: Math.max(w, h) * 0.006, fill: '#224f40' }));
      return;
    }
    if (pz && (!placementEdit || $('autoPlace').checked)) return;
    state.placed.forEach((item, index) => {
      const spec = state.catalog.find((s) => s.name === item.name);
      if (!spec) return;
      const group = svg('g', {
        class: 'whimsy-handle',
        transform:
          'translate(' +
          item.x_frac * w +
          ',' +
          item.y_frac * h +
          ') rotate(' +
          item.angle_deg +
          ') scale(' +
          item.size_mm +
          ')',
      });
      group.append(svg('path', { d: line(spec.outline, true), 'vector-effect': 'non-scaling-stroke' }));
      group.addEventListener('pointerdown', (e) => dragWhimsy(e, index, w, h));
      $('whimsyEditLayer').append(group);
    });
  }
  function dragWhimsy(e, index, w, h) {
    e.stopPropagation();
    const start = point(e),
      item = state.placed[index];
    const delta = [item.x_frac - start.x / w, item.y_frac - start.y / h];
    edit();
    const move = (ev) => {
      const p = point(ev);
      item.x_frac = Math.max(0, Math.min(1, p.x / w + delta[0]));
      item.y_frac = Math.max(0, Math.min(1, p.y / h + delta[1]));
      render();
    };
    const end = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
  }
  function labelOf(id) {
    return id.startsWith('glyph:') ? id.slice(6) : id;
  }
  function addPiece(name) {
    if (!name) return;
    edit();
    const { w, h } = dims(),
      n = state.placed.length;
    const x = [0.5, 0.29, 0.71],
      y = [0.5, 0.29, 0.71];
    state.placed.push({
      name,
      x_frac: x[n % 3],
      y_frac: y[Math.floor(n / 3) % 3],
      size_mm: Math.round(Math.min(w, h) * 0.21),
      angle_deg: 0,
    });
    placedList();
    render();
  }
  function placedList() {
    const list = $('placedWhimsies');
    list.replaceChildren();
    if (!state.placed.length) return;
    const hint = document.createElement('span');
    hint.className = 'hint';
    hint.textContent = $('autoPlace').checked
      ? 'Generation chooses positions. Set size and angle below.'
      : 'Drag each figure on the board. Size is in mm, rotation in degrees.';
    list.append(hint);
    state.placed.forEach((item, index) => {
      const row = document.createElement('div');
      row.className = 'placed-item';
      const label = document.createElement('strong');
      label.textContent = labelOf(item.name);
      row.append(label);
      for (const [field, title, min, max] of [
        ['size_mm', 'Size (mm)', 3, 650],
        ['angle_deg', 'Angle (degrees)', -180, 180],
      ]) {
        const input = document.createElement('input');
        input.type = 'number';
        input.step = 'any';
        input.min = min;
        input.max = max;
        input.required = true;
        input.value = item[field];
        input.title = title;
        input.setAttribute('aria-label', label.textContent + ' ' + title);
        input.addEventListener('input', () => {
          if (input.validity.valid && input.value) item[field] = Number(input.value);
        });
        row.append(input);
      }
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'del-btn';
      remove.textContent = '×';
      remove.setAttribute('aria-label', 'Remove ' + label.textContent);
      remove.addEventListener('click', () => {
        edit();
        state.placed.splice(index, 1);
        placedList();
        render();
      });
      row.append(remove);
      list.append(row);
    });
  }
  function auditBar(audit) {
    $('mStatus').textContent = audit.all_hard_gates_passed ? 'Passed' : 'Check audit';
    $('mStatus').className = audit.all_hard_gates_passed ? 'pass' : 'fail';
    $('mPieces').textContent = audit.pieces;
    $('mLock').textContent = Number(audit.interlock?.assembly_lock_margin_mm || 0).toFixed(2) + ' mm';
    const kerf = Object.values(audit.kerf_failures || {}).reduce((a, b) => a + b, 0);
    $('mKerf').textContent = kerf ? kerf + ' flagged' : 'Passed';
    $('mNeck').textContent = (audit.heller_neck_failures ?? audit.neck_3mm_failures ?? 0) + ' flagged';
    $('mGeom').textContent =
      audit.duplicate_segments + ' / ' + Number(audit.coverage_error_mm2 || 0).toFixed(3) + ' mm²';
    $('mLen').textContent = (Number(audit.cut_length_mm || 0) / 1000).toFixed(2) + ' m';
  }
  function showResult(data) {
    if (!data.puzzle.artwork && data.photo_url) {
      data.puzzle.artwork = {
        href: data.photo_url,
        x: 0,
        y: 0,
        width: data.puzzle.width_mm,
        height: data.puzzle.height_mm,
        preserveAspectRatio: 'none',
      };
    }
    state.result = data;
    placementEdit = false;
    setDownloads(data);
    auditBar(data.audit || data.puzzle.audit_summary);
    render();
  }
  function chooseStyle(key) {
    state.style = key;
    state.styleOverrides = {};
    for (const btn of document.querySelectorAll('.style-btn')) {
      const active = btn.dataset.style === key;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', String(active));
    }
    $('borderlessRow').hidden = key !== 'maze';
    $('phaseControls').hidden = !(key in phaseDefaults);
    if (phaseDefaults[key]) {
      const [gamma, steps] = phaseDefaults[key];
      $('gammaSlider').value = gamma;
      $('gammaVal').textContent = gamma;
      $('stepsSlider').value = steps;
      $('stepsVal').textContent = steps;
      $('neuralStatus').textContent = '';
    }
  }
  function chooseOutline() {
    const key = $('outlineSelect').value;
    $('customSvgRow').hidden = key !== 'svg';
    $('drawRow').hidden = key !== 'draw';
    drawing = key === 'draw' && !state.shapes.draw;
    stage.classList.toggle('drawing', drawing);
    $('applyDraw').disabled = !drawing || state.drawPoints.length < 3;
    $('undoPoint').disabled = !drawing || !state.drawPoints.length;
    syncDimensions();
    render();
  }
  function photoControls() {
    const hasPhoto = !!state.photo,
      visible = $('artPattern').value === 'photo';
    $('photoRow').hidden = !visible;
    $('clearArtBtn').hidden = !hasPhoto;
    $('suggestOutline').disabled = !visible || !hasPhoto;
    photoInfo();
  }
  let outlineTimer;
  function queueOutlinePreview() {
    const ticket = ++lookup;
    clearTimeout(outlineTimer);
    const kind = $('outlineSelect').value;
    if (!['organic', 'spiral'].includes(kind)) return;
    delete state.shapes[kind];
    const { w, h } = dims();
    outlineTimer = setTimeout(async () => {
      try {
        const result = await request('/api/outline', {
          outline: kind,
          width_mm: w,
          height_mm: h,
          seed: Number($('seedInput').value),
        });
        if (ticket !== lookup) return;
        state.shapes[kind] = { points: result.points };
        render();
      } catch (err) {
        if (ticket === lookup)
          $('outlineStatus').textContent = 'Showing the outline bounding box. ' + err.message;
      }
    }, 180);
  }
  document.querySelectorAll('.tab').forEach((b) =>
    b.addEventListener('click', () => {
      changeView(b.dataset.view);
      render();
    }),
  );
  document.querySelectorAll('.style-btn').forEach((b) =>
    b.addEventListener('click', () => {
      edit();
      chooseStyle(b.dataset.style);
      render();
    }),
  );
  for (const id of ['widthInput', 'heightInput', 'seedInput'])
    $(id).addEventListener('input', () => {
      syncDimensions();
      queueOutlinePreview();
    });
  $('designForm').addEventListener('input', (e) => {
    if (e.target.type === 'file') return;
    edit();
    render();
  });
  $('outlineSelect').addEventListener('change', () => {
    loading++;
    $('outlineStatus').textContent = '';
    chooseOutline();
    queueOutlinePreview();
  });
  $('artPattern').addEventListener('change', () => {
    if ($('artPattern').value !== 'photo' && $('outlineSelect').value === 'photo')
      $('outlineSelect').value = 'rect';
    photoControls();
    chooseOutline();
  });
  function replacePhoto(key, source) {
    if (state[key]?.url.startsWith('blob:')) URL.revokeObjectURL(state[key].url);
    state[key] = source;
    if (key === 'back') {
      $('clearBack').hidden = !source;
      return;
    }
    loading++;
    delete state.shapes.photo;
    if (state.shapes.draw) state.shapes.draw.crop = null;
    $('photoOutlineOption').disabled = true;
    if ($('outlineSelect').value === 'photo') $('outlineSelect').value = 'rect';
    photoControls();
    chooseOutline();
  }
  async function loadPhoto(file, key) {
    const token = ++photoLoads[key];
    edit();
    replacePhoto(key, null);
    if (file) pendingImages.add(key);
    else pendingImages.delete(key);
    syncGenerate();
    if (!file) return;
    try {
      const fresh = await art.openImage(file);
      if (token !== photoLoads[key]) {
        URL.revokeObjectURL(fresh.url);
        return;
      }
      replacePhoto(key, { ...fresh, file });
    } catch (err) {
      if (token === photoLoads[key]) message(err.message);
    } finally {
      if (token === photoLoads[key]) {
        pendingImages.delete(key);
        syncGenerate();
      }
    }
  }
  $('artworkInput').addEventListener('change', (e) => loadPhoto(e.target.files[0], 'photo'));
  $('backArtwork').addEventListener('change', (e) => loadPhoto(e.target.files[0], 'back'));
  $('clearArtBtn').addEventListener('click', () => {
    $('artworkInput').value = '';
    $('artPattern').value = 'wood';
    loadPhoto(null, 'photo');
  });
  $('clearBack').addEventListener('click', () => {
    $('backArtwork').value = '';
    loadPhoto(null, 'back');
  });
  async function traceSource(mode, file) {
    const ticket = ++loading;
    let source, temporary;
    try {
      if (mode === 'svg') {
        temporary = await art.openImage(file);
        source = temporary;
      } else source = state.photo;
      if (!source || ticket !== loading) return;
      $('outlineStatus').textContent = 'Tracing the silhouette…';
      const pts = await art.trace(source.image, mode === 'svg');
      if (ticket !== loading) return;
      const box = { x: 0, y: 0, w: source.image.naturalWidth, h: source.image.naturalHeight };
      state.shapes[mode] = art.makeShape(pts, box, mode === 'photo');
      if (mode === 'photo') $('photoOutlineOption').disabled = false;
      $('outlineSelect').value = mode;
      edit();
      chooseOutline();
      $('outlineStatus').textContent = 'Using the largest silhouette from ' + source.name + '.';
    } catch (err) {
      if (ticket === loading) $('outlineStatus').textContent = err.message;
    } finally {
      if (temporary) URL.revokeObjectURL(temporary.url);
    }
  }
  $('outlineSvgInput').addEventListener('change', (e) => {
    if (e.target.files[0]) traceSource('svg', e.target.files[0]);
  });
  $('suggestOutline').addEventListener('click', () => traceSource('photo'));
  stage.addEventListener('pointerdown', (e) => {
    if (state.view !== 'design' || !drawing || e.button !== 0) return;
    const { w, h } = dims(),
      p = point(e);
    if (p.x < 0 || p.y < 0 || p.x > w || p.y > h) return;
    const xy = [p.x / w, p.y / h],
      prev = state.drawPoints.at(-1);
    if (prev && Math.hypot(xy[0] - prev[0], xy[1] - prev[1]) < 0.001) return;
    edit();
    state.drawPoints.push(xy);
    chooseOutline();
  });
  $('undoPoint').addEventListener('click', () => {
    edit();
    state.drawPoints.pop();
    chooseOutline();
  });
  $('resetDraw').addEventListener('click', () => {
    edit();
    state.drawPoints = [];
    delete state.shapes.draw;
    $('outlineStatus').textContent = '';
    chooseOutline();
  });
  $('applyDraw').addEventListener('click', () => {
    try {
      art.drawable(state.drawPoints);
      const { w, h } = dims(),
        photoCrop = cropOf(w, h);
      state.shapes.draw = art.makeShape(state.drawPoints, photoCrop || { x: 0, y: 0, w, h }, !!photoCrop);
      edit();
      $('outlineStatus').textContent = 'Outline closed.';
      chooseOutline();
    } catch (err) {
      $('outlineStatus').textContent = err.message;
    }
  });
  $('addGlyph').addEventListener('click', () => addPiece($('glyphSelect').value));
  $('autoPlace').addEventListener('change', placedList);
  $('exampleRegions').addEventListener('click', () => {
    const n = Math.max(4, Number($('piecesInput').value) || 32),
      half = Math.floor(n / 2);
    const example = [
      {
        style: 'coral',
        pieces: half,
        points: [
          [0, 0],
          [0.5, 0],
          [0.5, 1],
          [0, 1],
        ],
      },
      { style: 'matrix', pieces: n - half, remaining: true },
    ];
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(example, null, 2)], { type: 'application/json' }),
    );
    const link = document.createElement('a');
    link.href = url;
    link.download = 'jigsaw-regions.json';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  let regionRead = 0;
  $('clearRegions').addEventListener('click', () => {
    regionRead++;
    edit();
    state.regions = [];
    $('regionFile').value = '';
    $('clearRegions').hidden = true;
    $('regionStatus').textContent = '';
    render();
  });
  $('regionFile').addEventListener('change', async (e) => {
    const ticket = ++regionRead,
      file = e.target.files[0];
    if (!file) return;
    try {
      if (file.size > 1024 * 1024) throw new Error('Choose a region plan smaller than 1 MB.');
      const parsed = JSON.parse(await file.text()),
        regions = Array.isArray(parsed) ? parsed : parsed.regions;
      if (ticket !== regionRead) return;
      if (!Array.isArray(regions) || regions.length < 2) throw new Error('Provide at least two regions.');
      const styles = Array.from(document.querySelectorAll('.style-btn'), (b) => b.dataset.style);
      let pieces = 0;
      regions.forEach((r, i) => {
        if (!styles.includes(r?.style) || !Number.isInteger(r.pieces) || r.pieces < 2)
          throw new Error('Each region needs a cut family and at least two pieces.');
        if (r.remaining === true) {
          if (i !== regions.length - 1 || r.points)
            throw new Error('Only the final region may use the remaining board area.');
        } else {
          if (
            !Array.isArray(r.points) ||
            r.points.length > 2048 ||
            !r.points.every(
              (p) =>
                Array.isArray(p) && p.length === 2 && p.every((v) => Number.isFinite(v) && v >= 0 && v <= 1),
            )
          )
            throw new Error('Region vertices must be coordinate pairs between 0 and 1.');
          art.drawable(r.points);
        }
        pieces += r.pieces;
      });
      if (pieces < 4 || pieces > 240) throw new Error('The combined piece count must be between 4 and 240.');
      edit();
      state.regions = regions;
      $('piecesInput').value = pieces;
      $('clearRegions').hidden = false;
      $('regionStatus').textContent = regions.map((r) => r.style + ' × ' + r.pieces).join(' · ');
      render();
    } catch (err) {
      if (ticket === regionRead) {
        $('regionStatus').textContent = err.message;
        $('regionFile').value = '';
      }
    }
  });
  for (const [slider, label] of [
    ['gammaSlider', 'gammaVal'],
    ['stepsSlider', 'stepsVal'],
    ['kerfSlider', 'kerfVal'],
  ]) {
    $(slider).addEventListener('input', () => {
      $(label).textContent = $(slider).value;
    });
  }
  for (const id of ['showInt', 'showExt', 'showEng']) $(id).addEventListener('change', render);
  $('scatterFewBtn').addEventListener('click', () => player.scatter('few'));
  $('scatterAllBtn').addEventListener('click', () => player.scatter('all'));
  $('solvePlayBtn').addEventListener('click', () => player.assemble());
  $('autoTuneBtn').addEventListener('click', async () => {
    edit();
    const revision = state.revision;
    try {
      $('neuralStatus').textContent = 'Estimating growth settings…';
      const { w, h } = dims();
      const data = await request('/api/autotune', {
        width_mm: w,
        height_mm: h,
        pieces: Number($('piecesInput').value),
        style: state.style,
        seed: Number($('seedInput').value),
      });
      if (revision !== state.revision) return;
      state.styleOverrides = data.recommended || {};
      for (const [field, slider, label] of [
        ['gamma', 'gammaSlider', 'gammaVal'],
        ['steps', 'stepsSlider', 'stepsVal'],
      ]) {
        if (data.recommended?.[field] !== undefined) {
          $(slider).value = data.recommended[field];
          $(label).textContent = $(slider).value;
        }
      }
      $('neuralStatus').textContent = data.used_neural_guide
        ? 'Neural settings applied.'
        : 'Scaled preset applied.';
    } catch (err) {
      if (revision === state.revision) $('neuralStatus').textContent = err.message;
    }
  });
  async function frontImage(crop) {
    if (!crop) return null;
    const { file, url, image } = state.photo;
    // The production renderer centers the original image on the board. Only traced
    // or drawn subject crops, and large uploads, need a separate print raster.
    if (!shape()?.crop) {
      if (url.startsWith('data:')) return url;
      if (file && file.size <= 16 * 1024 * 1024) {
        return new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = () => reject(new Error('The photograph could not be read.'));
          reader.readAsDataURL(file);
        });
      }
    }
    return art.printImage(image, crop);
  }
  $('designForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    if (running || pendingImages.size) return;
    const revision = ++state.revision;
    running = true;
    $('generateBtn').disabled = true;
    try {
      const { w, h } = dims(),
        mode = $('outlineSelect').value,
        custom = ['svg', 'draw', 'photo'].includes(mode);
      if (custom && !shape())
        throw new Error(mode === 'draw' ? 'Close the drawn outline first.' : 'Choose a silhouette first.');
      if (h < 80 || h > 650)
        throw new Error('Adjust the width so the silhouette height is between 80 and 650 mm.');
      const pattern = $('artPattern').value,
        crop = cropOf(w, h);
      if (pattern === 'photo' && !crop) throw new Error('Choose a photograph first.');
      if (
        state.regions.length &&
        state.regions.reduce((n, r) => n + r.pieces, 0) !== Number($('piecesInput').value)
      )
        throw new Error('The target piece count must match the total in the region plan.');
      message('Generating the ' + state.style + ' cut…');
      const payload = {
        style: state.style,
        outline: custom ? 'svg' : mode,
        outline_svg: custom ? art.asSvg(shape()) : null,
        width_mm: w,
        height_mm: h,
        pieces: Number($('piecesInput').value),
        seed: Number($('seedInput').value),
        borderless: state.style === 'maze' && $('borderlessCheck').checked,
        theme: $('themeSelect').value,
        min_neck_mm: Number($('neckInput').value),
        kerf_mm: Number($('kerfSlider').value),
        title: $('titleInput').value.trim(),
        engrave_text: $('engraveInput').value.trim(),
        reverse_text: $('reverseInput').value.trim(),
        artwork_pattern: pattern === 'photo' ? 'wood' : pattern,
        artwork_data_url: await frontImage(crop),
        back_artwork_data_url: state.back
          ? art.printImage(state.back.image, art.cropToAspect(state.back.image, w / h))
          : null,
        auto_place_whimsies: $('autoPlace').checked,
        whimsies: state.placed,
        ...state.styleOverrides,
      };
      if (state.regions.length) payload.regions = state.regions;
      if (state.style in phaseDefaults) {
        payload.gamma = Number($('gammaSlider').value);
        payload.steps = Number($('stepsSlider').value);
      }
      if (revision !== state.revision) return;
      const data = await request('/api/generate', payload);
      await refreshShowcases();
      if (revision !== state.revision) return;
      showResult(data);
      message((payload.title ? payload.title + ' · ' : '') + data.audit.pieces + ' pieces generated.');
    } catch (err) {
      if (revision === state.revision) message(err.message);
    } finally {
      running = false;
      syncGenerate();
    }
  });
  async function refreshShowcases() {
    try {
      const data = await request('/api/presets'),
        select = $('showcaseSelect'),
        prior = select.value;
      select.replaceChildren(new Option('Open a board…', ''));
      for (const run of data.showcases || []) {
        const o = new Option((run.title || run.id) + ' · ' + run.pieces + ' pcs', run.puzzle_url);
        o.run = run;
        select.add(o);
      }
      if (prior) select.value = prior;
    } catch {
      /* The editor and existing downloads remain usable. */
    }
  }
  $('showcaseSelect').addEventListener('change', async () => {
    const selected = $('showcaseSelect').selectedOptions[0],
      run = selected?.run;
    if (!run) return;
    const revision = ++state.revision;
    const ticket = ++boardLoad;
    for (const key of ['photo', 'back']) {
      photoLoads[key]++;
      pendingImages.delete(key);
    }
    pendingImages.add('board');
    syncGenerate();
    try {
      const puzzle = await request(run.puzzle_url);
      if (revision !== state.revision) return;
      let source = null;
      const url = puzzle.artwork?.href || run.photo_url;
      if (url) {
        const image = new Image();
        image.src = url;
        await image.decode();
        if (revision !== state.revision) return;
        source = { image, url, name: run.title || run.id };
      }
      $('artworkInput').value = '';
      $('backArtwork').value = '';
      $('artPattern').value = source ? 'photo' : puzzle.metadata?.artwork_pattern || 'wood';
      $('widthInput').value = puzzle.width_mm;
      $('heightInput').value = puzzle.height_mm;
      replacePhoto('photo', source);
      replacePhoto('back', null);
      changeView('design');
      showResult({ ...run, puzzle });
      message('Loaded ' + (run.title || run.id) + '.');
    } catch (err) {
      if (revision === state.revision) message(err.message);
    } finally {
      if (ticket === boardLoad) {
        pendingImages.delete('board');
        syncGenerate();
      }
    }
  });
  async function bootstrap() {
    chooseStyle('dendrite');
    chooseOutline();
    photoControls();
    placedList();
    try {
      const [health, whimsies] = await Promise.all([request('/api/health'), request('/api/whimsies')]);
      $('gpuPill').textContent = health.cuda_available ? health.gpu_name : 'CPU';
      state.catalog = whimsies.builtin || [];
      for (const spec of state.catalog) {
        if (spec.name.startsWith('glyph:')) {
          $('glyphSelect').add(new Option(labelOf(spec.name), spec.name));
          continue;
        }
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'whimsy-chip';
        b.textContent = spec.name;
        b.addEventListener('click', () => addPiece(spec.name));
        $('whimsyPalette').append(b);
      }
      $('glyphRow').hidden = !$('glyphSelect').options.length;
      await refreshShowcases();
    } catch (err) {
      $('gpuPill').textContent = 'Engine unavailable';
      message(err.message);
    }
  }
  bootstrap();
})();
