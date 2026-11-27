// ---------------------------------------------------------------- logos
const zc = v => Math.max(-2 * ZR, Math.min(2 * ZR, v));
function logo(R) {                                    // a logo from the sampled latent range, held to the cut rules, or the mean logo
  for (let t = 0; t < 30; t++) { const z = new Float32Array(ZD); for (let i = 0; i < ZD; i++) z[i] = ZR * (2 * R() - 1); const c = make(z); if (c) return c; }
  const z = new Float32Array(ZD), out = decode(z);
  return { z, out, sh: shape(out) };
}

// ---------------------------------------------------------------- sound: the layout ticks, the cut crackles; only after a touch
let ac = null, last = 0, grain = null;
const audio = () => { try { ac = ac || new (window.AudioContext || window.webkitAudioContext)(); if (ac.state !== 'running') ac.resume(); } catch (e) { /* no sound */ } };
const out = x => { if (!ac.createStereoPanner) return ac.destination; const p = ac.createStereoPanner(); p.pan.value = Math.max(-1, Math.min(1, 2 * x / W - 1)) * .8; p.connect(ac.destination); return p; };
function tick(x, y) {                                 // one logo set down: a short pentatonic blip, higher towards the top
  if (!ac || ac.state !== 'running' || ac.currentTime - last < .014) return;
  const t = last = ac.currentTime, k = Math.max(0, Math.min(14, Math.round(14 * (1 - y / H)))), o = ac.createOscillator(), g = ac.createGain();
  o.frequency.value = 392 * 2 ** Math.floor(k / 5) * [1, 9 / 8, 5 / 4, 3 / 2, 5 / 3][k % 5];
  g.gain.setValueAtTime(.03, t); g.gain.exponentialRampToValueAtTime(1e-4, t + .08);
  o.connect(g).connect(out(x)); o.start(t); o.stop(t + .09);
}
function crackle(times) {                             // every seam a click as the laser reaches it
  if (!ac || ac.state !== 'running') return;
  if (!grain) { grain = ac.createBuffer(1, 192, ac.sampleRate); const d = grain.getChannelData(0); for (let i = 0; i < 192; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / 192) ** 2; }
  const t0 = ac.currentTime + .02;
  for (const [t, x, y] of times) {
    const s = ac.createBufferSource(), f = ac.createBiquadFilter(), g = ac.createGain();
    s.buffer = grain; f.type = 'bandpass'; f.frequency.value = 600 + 4200 * (1 - y / H); f.Q.value = 2.5; g.gain.value = .14;
    s.connect(f).connect(g).connect(out(x)); s.start(t0 + t);
  }
}

// ---------------------------------------------------------------- drawing
const fmt = v => (Math.round(v * 100) / 100).toString();
const dline = xy => 'M' + xy.map(p => fmt(p[0]) + ' ' + fmt(p[1])).join('L');
const dpoly = p => { let s = 'M'; for (let i = 0; i < p.length; i += 2) s += (i ? 'L' : '') + fmt(p[i]) + ' ' + fmt(p[i + 1]); return s + 'Z'; };
const el = (tag, attrs, parent) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; };
const px = () => W / (svg.clientWidth || W);           // one screen pixel in millimetres
const widths = () => { const k = px(); for (const g of svg.querySelectorAll('g')) g.setAttribute('stroke-width', (g.classList.contains('cut') ? 1.1 : .8) * k); };
const group = cls => { const g = el('g', { class: cls }, svg); widths(); return g; };
const print = document.getElementById('print');

let gen = 0, current = null, board = null, count = 183, settled = 0;
const FILL = .45;                                     // share of the field the silhouettes are sized to cover; logos pack loosely, so the cut grows them
const relax = () => new Promise(r => setTimeout(r, 0));
const ticker = me => { let t0 = performance.now(); return async () => { if (performance.now() - t0 > 12) { await relax(); t0 = performance.now(); } return me === gen; }; };
function clear(ms = 700) { clearTimeout(settled); for (const g of svg.querySelectorAll('g')) { g.classList.add('gone'); setTimeout(() => g.remove(), ms); } }

async function build(seed, at) {                      // field.pack: dock the largest first, fill what is left, then grow each into its room
  const me = ++gen, R = mulberry(seed), live = ticker(me);
  clear(); board = null; current = null; svg.classList.add('dev');
  const ghost = group('ghost'), all = [];
  while (all.length < count) { all.push(logo(R)); if (!(await live())) return; }
  const sum = all.reduce((a, c) => a + c.sh.area, 0), sref = Math.sqrt(FILL * .91 * W * H / sum), slo = Math.max(BASE, .75 * sref), shi = Math.max(slo * 1.05, 1.3 * sref);
  const s0 = Math.sqrt(FILL * W * H / sum), B = grid(W, H), pieces = [];
  all.forEach(c => { c.want = Math.min(shi, Math.max(slo, (.86 + .28 * R()) * s0)); });
  all.sort((a, b) => b.want - a.want);
  const put = c => { c.id = pieces.length; pieces.push(c); stamp(B, c, true, true); el('path', { d: dpoly(placed(c.sh, c.p)) }, ghost); tick(c.p.x, c.p.y); };
  for (const c of all) {
    c.id = pieces.length;
    for (const m of [1, .92, .84]) { const s = Math.max(slo, c.want * m); c.p = dock(B, c, s, R); if (c.p || s === slo) break; }
    if (c.p) put(c);
    if (!(await live())) return;
  }
  for (let miss = 0; miss < 18;) {
    const c = logo(R); c.id = pieces.length; c.p = dock(B, c, slo * (1 + .08 * R()), R);
    if (c.p) { put(c); miss = 0; } else miss++;
    if (!(await live())) return;
  }
  let budget = 700;                                    // ms of latent search: the network bends a logo into the room it has
  for (let sweep = 0; sweep < 3; sweep++) {
    for (const c of pieces) {
      stamp(B, c, false);
      let p = c.p;
      for (let t = 0; t < 6; t++) {
        const q = { x: p.x + .8 * gauss(R), y: p.y + .8 * gauss(R), th: p.th + .05 * gauss(R), s: Math.min(shi, p.s * (1.015 + .03 * R())) };
        if (q.s > p.s && fits(B, c.sh, q.x, q.y, Math.cos(q.th), Math.sin(q.th), q.s, c.id)) p = q;
      }
      if (budget > 0) {
        const t1 = performance.now(), n2 = make(c.z.map(v => Math.max(-ZR, Math.min(ZR, v + .07 * gauss(R))))), s = Math.min(shi, p.s * 1.01);
        if (n2 && fits(B, n2.sh, p.x, p.y, Math.cos(p.th), Math.sin(p.th), s, c.id)) { Object.assign(c, n2); p = { ...p, s }; }
        budget -= performance.now() - t1;
      }
      c.p = p; stamp(B, c, true);
      if (!(await live())) return;
    }
  }
  board = { pieces, B, slo, ghost };
  await cut(me, at);
}

async function cut(me, at) {                          // first arrival with the bay rule, spur repair and seams, drawn in from `at`
  const { pieces } = board, live = ticker(me), polys = pieces.map(c => placed(c.sh, c.p));
  board.ghost.replaceChildren(...polys.map(p => el('path', { d: dpoly(p), style: 'animation:none' })));
  const P = await arrival(polys, W, H, live); if (!P) return;
  connect(P);
  for (let i = 0, only = null; i < 3; i++) { const d = despike(P, only); if (!d.moved) break; only = d.touched; }
  connect(P);
  if (!(await live())) return;
  const arcs = trace(P).map(a => ({ pair: a.pair, xy: a.pair[0] < 0 ? [a.xy[0], a.xy[a.xy.length - 1]] : simplify(smooth(a.xy, .4, a.loop), .02), loop: a.loop }));
  if (!(await live())) return;
  const [ox, oy] = at ? local(at[0], at[1]) : [W / 2, H / 2], far = Math.hypot(Math.max(ox, W - ox), Math.max(oy, H - oy)), times = [];
  const g = el('g', { class: 'cut' });
  for (const a of arcs) {
    const m = a.xy[a.xy.length >> 1], t = .05 + 1.1 * Math.hypot(m[0] - ox, m[1] - oy) / far;
    el('path', { d: dline(a.loop ? [...a.xy, a.xy[0]] : a.xy), pathLength: 1, style: `--t:${t.toFixed(3)}s` }, g);
    times.push([t, m[0], m[1]]);
  }
  const gh = board.ghost; svg.append(g); widths(); gh.classList.add('gone'); setTimeout(() => gh.remove(), 800);
  crackle(times);
  settled = setTimeout(() => { g.classList.add('set'); svg.classList.remove('dev'); }, 1500);   // the laser cools, the print comes up
  current = { arcs, n: pieces.length }; svg.dataset.n = pieces.length;
}

// ---------------------------------------------------------------- sculpting: a drag stretches the logos it passes along its own direction
let drag = null;
function grab(at) {
  if (!board) return false;
  ++gen; clear(400); svg.classList.add('dev');
  const g = group('ghost');
  board.ghost = g; drag = { at, paths: board.pieces.map(c => el('path', { d: dpoly(placed(c.sh, c.p)), style: 'animation:none' }, g)), moved: new Set() };
  return true;
}
function knead(at) {                                  // stretch (latent 0, 4) and shear (1, 5) in each piece's own frame
  const dx = at[0] - drag.at[0], dy = at[1] - drag.at[1], step = Math.hypot(dx, dy); drag.at = at;
  if (step < 1e-3) return;
  for (const c of board.pieces) {
    const t = Math.min(1, step / 25 * Math.exp(-((c.p.x - at[0]) ** 2 + (c.p.y - at[1]) ** 2) / 1600)); if (t < .01) continue;
    const co = Math.cos(c.p.th), si = Math.sin(c.p.th), ux = (co * dx + si * dy) / step, uy = (-si * dx + co * dy) / step, k = .45 * t, z = Float32Array.from(c.z2 || c.z);
    z[0] = zc(z[0] + k * (ux * ux - uy * uy)); z[4] = zc(z[4] + k * (uy * uy - ux * ux)); z[1] = zc(z[1] + k * ux * uy); z[5] = zc(z[5] + k * ux * uy);
    c.z2 = z; drag.moved.add(c); drag.paths[c.id].setAttribute('d', dpoly(placed({ poly: flatten(decode(z), 3) }, c.p)));
  }
}
async function release(at) {                          // keep every reshaped logo that still holds together and fits, then cut again
  const me = ++gen, B = board.B;
  for (const c of drag.moved) {
    const n2 = make(c.z2); delete c.z2;
    if (!n2) continue;
    stamp(B, c, false); const old = { z: c.z, out: c.out, sh: c.sh, p: c.p }; Object.assign(c, n2);
    let p = c.p, ok = false;
    for (let t = 0; t < 14 && !ok; t++) { ok = fits(B, c.sh, p.x, p.y, Math.cos(p.th), Math.sin(p.th), p.s, c.id); if (!ok) p = { ...p, s: Math.max(board.slo, p.s * .96) }; }
    if (ok) c.p = p; else Object.assign(c, old);
    stamp(B, c, true);
  }
  drag = null;
  await cut(me, at);
}

// ---------------------------------------------------------------- size: pinch or wheel scales the cut under the finger; it is cut again with that many pieces
let pinch = 1, settle = 0, focus = null;
function magnify(k, client) {
  if (!board && !current) return;
  const at = local(client[0], client[1]);
  pinch = Math.max(Math.sqrt(count / 420), Math.min(Math.sqrt(count / 40), pinch * k)); focus = client;   // 40 to 420 pieces
  for (const g of svg.querySelectorAll('g')) g.setAttribute('transform', `translate(${at[0]} ${at[1]}) scale(${pinch}) translate(${-at[0]} ${-at[1]})`);
  clearTimeout(settle); settle = setTimeout(() => { count = Math.round(count / (pinch * pinch)); pinch = 1; build(seed(), focus); }, 420);
}

// ---------------------------------------------------------------- the print: any image, centre-square; kept at up to 3000 px for the file
let printData = null, printURL = print.getAttribute('href');
const pick = document.getElementById('pick');
function encode(im) {
  const s = Math.min(im.naturalWidth, im.naturalHeight), n = Math.min(3000, s), c = document.createElement('canvas'); c.width = c.height = n;
  c.getContext('2d').drawImage(im, (im.naturalWidth - s) / 2, (im.naturalHeight - s) / 2, s, s, 0, 0, n, n);
  try { printData = c.toDataURL('image/jpeg', .9); } catch (e) { printData = null; }
}
function use(file) {
  if (!file || !/^image\//.test(file.type)) return;
  const url = URL.createObjectURL(file), im = new Image();
  im.onload = () => { if (printURL.startsWith('blob:')) URL.revokeObjectURL(printURL); printURL = url; print.setAttribute('href', url); encode(im); };
  im.src = url;
}
{ const im = new Image(); im.onload = () => encode(im); im.src = printURL; }
pick.addEventListener('change', () => { use(pick.files[0]); pick.value = ''; });
addEventListener('dragover', e => e.preventDefault());
addEventListener('drop', e => { e.preventDefault(); use([...(e.dataTransfer?.files || [])].find(f => /^image\//.test(f.type))); });
addEventListener('paste', e => use([...(e.clipboardData?.files || [])].find(f => /^image\//.test(f.type))));

// ---------------------------------------------------------------- the file: cut red, print underneath, 1:1 mm
let downloads = null;
if (window.claude && window.claude.use) window.claude.use('downloads').then(d => { downloads = d; }, () => {});
const coarse = matchMedia('(pointer: coarse)').matches;
function production() {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}mm" height="${H}mm" viewBox="0 0 ${W} ${H}">\n`
    + (printData ? `<image id="print" width="${W}" height="${H}" href="${printData}"/>\n` : '')
    + `<g id="cut" fill="none" stroke="#ff0000" stroke-width="0.1" stroke-linecap="round" stroke-linejoin="round">${current.arcs.map(a => `<path d="${dline(a.loop ? [...a.xy, a.xy[0]] : a.xy)}"/>`).join('')}</g>\n</svg>`;
}
async function save() {
  if (!current) return;
  const name = `sound-in-pieces-${current.n}.svg`, data = production();
  if (downloads) { try { await downloads.save({ filename: name, data }); } catch (e) { /* declined or unavailable */ } return; }
  const file = new File([data], name, { type: 'image/svg+xml' });
  if (coarse && navigator.canShare && navigator.canShare({ files: [file] })) { try { await navigator.share({ files: [file] }); } catch (e) { /* dismissed */ } return; }
  if (window.claude) return;
  const a = document.createElement('a'); a.href = URL.createObjectURL(file); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}
const back = () => { if (history.length > 1 && document.referrer.startsWith(location.origin)) history.back(); else location.href = './'; };

// ---------------------------------------------------------------- no visible controls
const seed = () => (Math.random() * 2 ** 31) >>> 0;
const local = (x, y) => { const r = svg.getBoundingClientRect(); return [(x - r.left) / r.width * W, (y - r.top) / r.height * H]; };
const ptrs = new Map(); let gest = null, hold = 0, later = null;
const now = fn => { if (coarse) later = fn; else fn(); };          // on touch, pickers and the share sheet open from touchend
document.addEventListener('pointerdown', e => {
  audio();
  ptrs.set(e.pointerId, [e.clientX, e.clientY]);
  clearTimeout(hold); svg.classList.remove('hold');
  if (ptrs.size === 1) {
    gest = { kind: e.clientX < 24 ? 'edge' : 'tap', x: e.clientX, y: e.clientY };
    if (gest.kind === 'tap') hold = setTimeout(() => { if (gest && gest.kind === 'tap') { gest.kind = 'hold'; svg.classList.add('hold'); } }, 520);
  } else if (ptrs.size === 2) {
    if (drag) release();
    const [a, b] = [...ptrs.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]) || 1;
    gest = { kind: 'two', d, d0: d, t: e.timeStamp };
  }
});
document.addEventListener('pointermove', e => {
  if (!ptrs.has(e.pointerId) || !gest) return;
  ptrs.set(e.pointerId, [e.clientX, e.clientY]);
  const k = gest.kind, far = gest.x !== undefined && Math.hypot(e.clientX - gest.x, e.clientY - gest.y) > 6;
  if ((k === 'tap' || k === 'hold') && far) { clearTimeout(hold); svg.classList.remove('hold'); gest.kind = grab(local(gest.x, gest.y)) ? 'drag' : 'none'; }
  else if (k === 'edge' && e.clientX - gest.x > 70 && Math.abs(e.clientY - gest.y) < 60) { gest.kind = 'none'; back(); }
  else if ((k === 'two' || k === 'pinch') && ptrs.size === 2) {
    const [a, b] = [...ptrs.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]) || 1;
    if (k === 'two' && Math.abs(d / gest.d0 - 1) > .06) gest.kind = 'pinch';
    if (gest.kind === 'pinch') magnify(d / gest.d, [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2]);
    gest.d = d;
  }
  if (gest.kind === 'drag' && drag) { drag.next = local(e.clientX, e.clientY); if (!drag.raf) drag.raf = requestAnimationFrame(() => { if (drag) { drag.raf = 0; knead(drag.next); } }); }
});
const up = e => {
  if (!ptrs.delete(e.pointerId) || !gest) return;
  clearTimeout(hold); svg.classList.remove('hold');
  const k = gest.kind, done = e.type === 'pointerup';
  if (k === 'tap' && done) build(seed(), [e.clientX, e.clientY]);
  else if (k === 'hold' && done) now(() => pick.click());
  else if (k === 'drag' && drag) release([e.clientX, e.clientY]);
  else if (k === 'two' && done && !ptrs.size && e.timeStamp - gest.t < 450) now(save);
  if ((k !== 'two' && k !== 'pinch') || !ptrs.size) gest = ptrs.size ? { kind: 'none' } : null;
};
document.addEventListener('pointerup', up); document.addEventListener('pointercancel', up);
document.addEventListener('touchend', () => { audio(); if (later) { const fn = later; later = null; fn(); } });
document.addEventListener('gesturestart', e => e.preventDefault());
addEventListener('wheel', e => { e.preventDefault(); magnify(Math.exp(-e.deltaY * .0015), [e.clientX, e.clientY]); }, { passive: false });
addEventListener('resize', widths);
document.addEventListener('keydown', e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 's' || e.key === 'S') { e.preventDefault(); save(); }
  else if (e.key === 'o' || e.key === 'O') { e.preventDefault(); pick.click(); }
  else if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); audio(); build(seed()); }
  else if (e.key === 'Escape') back();
});
build(seed());
})();
