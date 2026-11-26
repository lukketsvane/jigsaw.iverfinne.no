/* Formverk Studio Frontend Controller */
(function () {
  'use strict';

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const $ = id => document.getElementById(id);

  function svgEl(tag, attrs) {
    const n = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    return n;
  }

  function polyD(pts) {
    return pts && pts.length ? 'M' + pts.map(p => `${p[0]},${p[1]}`).join('L') + 'Z' : '';
  }

  function lineD(pts) {
    return pts && pts.length >= 2 ? 'M' + pts.map(p => `${p[0]},${p[1]}`).join('L') : '';
  }

  const state = {
    view: 'design',
    style: 'dendrite',
    whimsyCatalog: [],
    placedWhimsies: [],
    artworkDataUrl: null,
    customOutlineSvg: null,
    currentPuzzle: null,
    currentUrls: null,
    tunedOverrides: {},
  };

  const stageSvg = $('stageSvg');
  const artworkLayer = $('artworkLayer');
  const pieceLayer = $('pieceLayer');
  const cutLayer = $('cutLayer');
  const engraveLayer = $('engraveLayer');
  const whimsyEditLayer = $('whimsyEditLayer');

  const player = new window.FormverkPlayer(stageSvg, pieceLayer, info => {
    $('playScore').textContent = info.groups === 1
      ? `Solved! (${info.total} / ${info.total} locked)`
      : `${info.largest} / ${info.total} connected (${info.groups} groups)`;
  });

  function boardDims() {
    const w = Math.max(80, Number($('widthInput').value) || 260);
    const h = $('outlineSelect').value === 'circle' ? w : Math.max(80, Number($('heightInput').value) || 190);
    return { w, h };
  }

  function setViewBox(w, h, padFrac = 0.08) {
    const pad = Math.max(w, h) * padFrac;
    stageSvg.setAttribute('viewBox', `${-pad} ${-pad} ${w + 2 * pad} ${h + 2 * pad}`);
  }

  function renderDesignCanvas() {
    artworkLayer.innerHTML = '';
    pieceLayer.innerHTML = '';
    cutLayer.innerHTML = '';
    engraveLayer.innerHTML = '';
    whimsyEditLayer.innerHTML = '';

    const pz = state.currentPuzzle;
    const W = pz ? pz.width_mm : boardDims().w;
    const H = pz ? pz.height_mm : boardDims().h;
    setViewBox(W, H, state.view === 'play' ? 0.42 : 0.07);

    if (state.view === 'play') {
      if (pz) player.load(pz);
      return;
    }

    // Optional uploaded artwork underlay
    if (state.artworkDataUrl && state.view === 'design') {
      artworkLayer.appendChild(
        svgEl('image', {
          href: state.artworkDataUrl,
          x: 0, y: 0, width: W, height: H,
          preserveAspectRatio: 'xMidYMid slice',
          opacity: pz ? 0.45 : 0.85,
        })
      );
    }

    if (pz) {
      if (state.view === 'design') {
        pz.pieces.forEach(p => {
          pieceLayer.appendChild(
            svgEl('path', {
              d: polyD(p.outline),
              fill: p.color,
              'fill-opacity': state.artworkDataUrl ? 0.52 : 1.0,
              stroke: '#251e18',
              'stroke-width': 0.30,
              'stroke-linejoin': 'round',
            })
          );
        });
        (pz.engrave_paths || []).forEach(pts => {
          engraveLayer.appendChild(
            svgEl('path', {
              d: lineD(pts),
              fill: 'none',
              stroke: '#5e3c1e',
              'stroke-width': 0.38,
              'stroke-linecap': 'round',
            })
          );
        });
      } else if (state.view === 'cut') {
        // Pure laser vector cut layer inspection
        if ($('showInt').checked) {
          (pz.interior_cuts || []).forEach(pts => {
            cutLayer.appendChild(
              svgEl('path', {
                d: lineD(pts), fill: 'none', stroke: '#e62222', 'stroke-width': 0.35, 'stroke-linecap': 'round',
              })
            );
          });
        }
        if ($('showExt').checked) {
          (pz.exterior_cuts || []).forEach(pts => {
            cutLayer.appendChild(
              svgEl('path', {
                d: lineD(pts), fill: 'none', stroke: '#1c4de0', 'stroke-width': 0.45, 'stroke-linecap': 'round',
              })
            );
          });
        }
        if ($('showEng').checked) {
          (pz.engrave_paths || []).forEach(pts => {
            engraveLayer.appendChild(
              svgEl('path', {
                d: lineD(pts), fill: 'none', stroke: '#14963f', 'stroke-width': 0.38, 'stroke-linecap': 'round',
              })
            );
          });
        }
      }
    } else {
      // Blank outline preview before first generation
      const kind = $('outlineSelect').value;
      if (kind === 'circle') {
        pieceLayer.appendChild(svgEl('circle', { cx: W / 2, cy: H / 2, r: Math.min(W, H) / 2, fill: '#eed8b4', stroke: '#251e18', 'stroke-width': 0.5 }));
      } else {
        pieceLayer.appendChild(svgEl('rect', { x: 0, y: 0, width: W, height: H, rx: kind === 'organic' ? 24 : 0, fill: '#eed8b4', stroke: '#251e18', 'stroke-width': 0.5 }));
      }
    }

    // Draggable whimsy placement handles in Design mode
    if (state.view === 'design') {
      state.placedWhimsies.forEach((w, idx) => {
        const spec = state.whimsyCatalog.find(s => s.name === w.name);
        if (!spec) return;
        const cx = w.x_frac * W;
        const cy = w.y_frac * H;
        const g = svgEl('g', {
          transform: `translate(${cx.toFixed(2)},${cy.toFixed(2)}) rotate(${w.angle_deg}) scale(${w.size_mm})`,
          class: 'whimsy-handle',
        });
        g.appendChild(svgEl('path', { d: polyD(spec.outline), 'vector-effect': 'non-scaling-stroke' }));
        g.addEventListener('pointerdown', e => startDragWhimsy(e, idx, W, H));
        whimsyEditLayer.appendChild(g);
      });
    }
  }

  function startDragWhimsy(e, idx, W, H) {
    e.stopPropagation();
    const toSvg = ev => {
      const pt = stageSvg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      return pt.matrixTransform(whimsyEditLayer.getScreenCTM().inverse());
    };
    const onMove = ev => {
      const p = toSvg(ev);
      state.placedWhimsies[idx].x_frac = Math.max(0.12, Math.min(0.88, p.x / W));
      state.placedWhimsies[idx].y_frac = Math.max(0.12, Math.min(0.88, p.y / H));
      renderDesignCanvas();
    };
    const onUp = () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  }

  function renderPlacedWhimsiesList() {
    const box = $('placedWhimsies');
    if (!state.placedWhimsies.length) {
      box.innerHTML = '<div class="hint">Click a figure above to add it, then drag on the board.</div>';
      return;
    }
    box.innerHTML = '';
    state.placedWhimsies.forEach((w, idx) => {
      const row = document.createElement('div');
      row.className = 'placed-item';
      row.innerHTML = `
        <strong>${w.name}</strong>
        <input type="number" title="Size (mm)" value="${w.size_mm}" min="24" max="140" step="2">
        <input type="number" title="Angle (deg)" value="${w.angle_deg}" min="-180" max="180" step="5">
        <button class="del-btn" title="Remove" type="button">&times;</button>
      `;
      const [szIn, angIn] = row.querySelectorAll('input');
      szIn.addEventListener('change', () => { w.size_mm = Number(szIn.value) || 56; renderDesignCanvas(); });
      angIn.addEventListener('change', () => { w.angle_deg = Number(angIn.value) || 0; renderDesignCanvas(); });
      row.querySelector('.del-btn').addEventListener('click', () => {
        state.placedWhimsies.splice(idx, 1);
        renderPlacedWhimsiesList();
        renderDesignCanvas();
      });
      box.appendChild(row);
    });
  }

  function updateAuditBar(audit) {
    if (!audit) return;
    const passed = !!audit.all_hard_gates_passed;
    $('mStatus').textContent = passed ? 'PASS (All Gates)' : 'CHECK WARNINGS';
    $('mStatus').className = passed ? 'pass' : 'fail';
    $('mPieces').textContent = `${audit.pieces}`;
    const lock = audit.interlock ? audit.interlock.assembly_lock_margin_mm : 0;
    $('mLock').textContent = `${Number(lock).toFixed(2)} mm`;
    $('mLock').className = lock >= 1.5 ? 'pass' : 'fail';
    const kerfFail = Object.values(audit.kerf_failures || {}).reduce((a, b) => a + b, 0);
    $('mKerf').textContent = kerfFail === 0 ? '100% pass' : `${kerfFail} fail`;
    $('mKerf').className = kerfFail === 0 ? 'pass' : 'fail';
    const neckFail = audit.heller_neck_failures ?? audit.neck_3mm_failures ?? 0;
    $('mNeck').textContent = neckFail === 0 ? '100% pass' : `${neckFail} fail`;
    $('mNeck').className = neckFail === 0 ? 'pass' : 'fail';
    $('mGeom').textContent = `${audit.duplicate_segments} dup / ${Number(audit.coverage_error_mm2 || 0).toFixed(3)} mm²`;
    $('mLen').textContent = `${(Number(audit.cut_length_mm || 0) / 1000).toFixed(2)} m`;
  }

  function setDownloads(urls) {
    state.currentUrls = urls;
    const map = [['dlCutSvg', urls?.cut_svg_url], ['dlArtSvg', urls?.artwork_svg_url], ['dlAuditJson', urls?.audit_url]];
    map.forEach(([id, href]) => {
      const a = $(id);
      if (href) {
        a.href = href;
        a.classList.remove('disabled');
      } else {
        a.removeAttribute('href');
        a.classList.add('disabled');
      }
    });
  }

  // Event wiring
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.view = btn.dataset.view;
      $('layerToggles').hidden = state.view !== 'cut';
      $('playControls').hidden = state.view !== 'play';
      renderDesignCanvas();
    });
  });

  document.querySelectorAll('.style-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.style-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      state.style = btn.dataset.style;
      state.tunedOverrides = {};
      $('borderlessRow').hidden = state.style !== 'maze';
      if (state.style === 'maze') $('themeSelect').value = 'walnut';
      else if (state.style === 'dendrite') $('themeSelect').value = 'birch';
    });
  });

  $('outlineSelect').addEventListener('change', () => {
    const v = $('outlineSelect').value;
    $('customSvgRow').hidden = v !== 'svg';
    $('heightInput').disabled = v === 'circle';
    renderDesignCanvas();
  });

  $('outlineSvgInput').addEventListener('change', ev => {
    const f = ev.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => { state.customOutlineSvg = String(reader.result); };
    reader.readAsText(f);
  });

  $('artworkInput').addEventListener('change', ev => {
    const f = ev.target.files[0];
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      state.artworkDataUrl = String(reader.result);
      $('clearArtBtn').hidden = false;
      renderDesignCanvas();
    };
    reader.readAsDataURL(f);
  });

  $('clearArtBtn').addEventListener('click', () => {
    state.artworkDataUrl = null;
    $('artworkInput').value = '';
    $('clearArtBtn').hidden = true;
    renderDesignCanvas();
  });

  $('gammaSlider').addEventListener('input', () => { $('gammaVal').textContent = $('gammaSlider').value; });
  $('stepsSlider').addEventListener('input', () => { $('stepsVal').textContent = $('stepsSlider').value; });
  $('kerfSlider').addEventListener('input', () => { $('kerfVal').textContent = Number($('kerfSlider').value).toFixed(2); });

  ['showInt', 'showExt', 'showEng'].forEach(id => $(id).addEventListener('change', renderDesignCanvas));
  $('scatterFewBtn').addEventListener('click', () => player.scatter('few'));
  $('scatterAllBtn').addEventListener('click', () => player.scatter('all'));
  $('solvePlayBtn').addEventListener('click', () => player.assemble());

  $('autoTuneBtn').addEventListener('click', async () => {
    $('neuralStatus').textContent = 'Running GPU neural surrogate optimization...';
    const { w, h } = boardDims();
    try {
      const resp = await fetch('/api/autotune', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          width_mm: w, height_mm: h,
          pieces: Number($('piecesInput').value) || 32,
          style: state.style,
          seed: Number($('seedInput').value) || 42,
        }),
      });
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      state.tunedOverrides = data.recommended || {};
      if (data.recommended) {
        $('gammaSlider').value = data.recommended.gamma;
        $('gammaVal').textContent = data.recommended.gamma;
        $('stepsSlider').value = data.recommended.steps;
        $('stepsVal').textContent = data.recommended.steps;
      }
      const p = data.predicted;
      $('neuralStatus').textContent = p
        ? `Tuned (${data.used_neural_guide ? 'Neural Guide' : 'Preset'}): pred lock ${p.lock_margin_mm} mm, neck ${(p.neck_pass_rate * 100).toFixed(1)}%, tortuosity ${p.seam_tortuosity}`
        : 'Applied scaled physical preset.';
    } catch (err) {
      $('neuralStatus').textContent = `Auto-tune error: ${err.message}`;
    }
  });

  $('generateBtn').addEventListener('click', async () => {
    const btn = $('generateBtn');
    btn.disabled = true;
    $('progressMsg').textContent = `Simulating ${state.style} on GPU...`;
    const { w, h } = boardDims();
    const payload = {
      style: state.style,
      outline: $('outlineSelect').value,
      outline_svg: $('outlineSelect').value === 'svg' ? state.customOutlineSvg : null,
      width_mm: w,
      height_mm: h,
      pieces: Number($('piecesInput').value) || 32,
      seed: Number($('seedInput').value) || 42,
      borderless: $('borderlessCheck').checked,
      theme: $('themeSelect').value,
      min_neck_mm: Number($('neckInput').value) || 3.0,
      kerf_mm: Number($('kerfSlider').value) || 0.15,
      engrave_text: $('engraveInput').value.trim() || null,
      artwork_data_url: state.artworkDataUrl,
      whimsies: state.placedWhimsies,
      ...state.tunedOverrides,
      gamma: Number($('gammaSlider').value),
      steps: state.style === 'maze' || state.style === 'wave' ? undefined : Number($('stepsSlider').value),
    };
    try {
      const resp = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
      state.currentPuzzle = data.puzzle;
      updateAuditBar(data.audit);
      setDownloads(data);
      renderDesignCanvas();
      $('progressMsg').textContent = `Generated ${data.audit.pieces} pieces in ${data.audit.metadata?.elapsed_sec ?? '?'}s (${data.run_id}).`;
      refreshShowcases();
    } catch (err) {
      $('progressMsg').textContent = `Error: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  $('showcaseSelect').addEventListener('change', async () => {
    const url = $('showcaseSelect').value;
    if (!url) return;
    $('progressMsg').textContent = 'Loading showcase...';
    try {
      const opt = $('showcaseSelect').selectedOptions[0];
      const pz = await (await fetch(url)).json();
      state.currentPuzzle = pz;
      state.placedWhimsies = [];
      renderPlacedWhimsiesList();
      updateAuditBar(pz.audit_summary);
      setDownloads({
        cut_svg_url: opt.dataset.cut,
        artwork_svg_url: opt.dataset.art,
        audit_url: opt.dataset.audit,
      });
      renderDesignCanvas();
      $('progressMsg').textContent = `Loaded showcase: ${opt.dataset.id}`;
    } catch (err) {
      $('progressMsg').textContent = `Load error: ${err.message}`;
    }
  });

  async function refreshShowcases() {
    try {
      const res = await (await fetch('/api/presets')).json();
      const sel = $('showcaseSelect');
      const cur = sel.value;
      sel.innerHTML = '<option value="">— Load generated run —</option>';
      (res.showcases || []).forEach(s => {
        const o = document.createElement('option');
        o.value = s.puzzle_url;
        o.dataset.id = s.id;
        o.dataset.cut = s.cut_svg_url;
        o.dataset.art = s.artwork_svg_url;
        o.dataset.audit = s.audit_url;
        o.textContent = `${s.id} (${s.pieces} pcs, lock ${s.lock_margin_mm} mm)`;
        sel.appendChild(o);
      });
      if (cur) sel.value = cur;
    } catch (_) { /* offline */ }
  }

  async function bootstrap() {
    try {
      const h = await (await fetch('/api/health')).json();
      $('gpuPill').textContent = `${h.cuda_available ? 'CUDA' : 'CPU'}: ${h.gpu_name}${h.neural_guide_ready ? ' · Guide Ready' : ''}`;
      const wh = await (await fetch('/api/whimsies')).json();
      state.whimsyCatalog = wh.builtin || [];
      const pal = $('whimsyPalette');
      pal.innerHTML = '';
      state.whimsyCatalog.forEach(item => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'whimsy-chip';
        b.textContent = `+ ${item.name}`;
        b.addEventListener('click', () => {
          const k = state.placedWhimsies.length;
          state.placedWhimsies.push({
            name: item.name,
            x_frac: 0.32 + 0.34 * (k % 2),
            y_frac: 0.35 + 0.28 * Math.floor(k / 2),
            size_mm: 56,
            angle_deg: 0,
          });
          renderPlacedWhimsiesList();
          renderDesignCanvas();
        });
        pal.appendChild(b);
      });
      await refreshShowcases();
      // Auto-load flagship showcase if present
      const sel = $('showcaseSelect');
      const flag = Array.from(sel.options).find(o => o.dataset.id === 'dendrite_rect_whimsies') || sel.options[1];
      if (flag && flag.value) {
        sel.value = flag.value;
        sel.dispatchEvent(new Event('change'));
      } else {
        renderDesignCanvas();
      }
    } catch (err) {
      $('gpuPill').textContent = 'Offline mode';
      renderDesignCanvas();
    }
  }

  bootstrap();
})();
