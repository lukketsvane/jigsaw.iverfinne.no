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
