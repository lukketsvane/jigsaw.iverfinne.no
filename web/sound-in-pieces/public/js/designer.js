// SOUND IN PIECES / design
// Every piece an Aphex Twin logo. PathNet's logo decoder (pathnet/decoder.json, family APHEX LOGO, folded to 6 -> 192 -> 192 -> 192)
// draws the silhouettes; they are docked into the 368 x 368 mm field and the field is divided between them by first arrival,
// with the layout, partition and seam code of web/fossilnet. Any image goes on top.
// No visible controls. Tap: a new cut. Drag: stretch the logos under the finger. Pinch or wheel: larger or smaller pieces.
// Press and hold (or O, drop, paste): your image. Two-finger tap (or S): the cut file. Swipe in from the left edge: back.
(() => {
'use strict';
const M = window.PATHNET_LOGO, svg = document.getElementById('board'), NS = 'http://www.w3.org/2000/svg';
if (!M) return;

// ---------------------------------------------------------------- the network (inference only)
const NC = M.N, ZD = M.Z, ZR = M.range, BASE = 30, W = 368, H = 368;
const f16 = s => {                                   // half-precision weights, little-endian
  const b = atob(s), n = b.length >> 1, o = new Float32Array(n);
  for (let i = 0; i < n; i++) { const h = b.charCodeAt(2 * i) | b.charCodeAt(2 * i + 1) << 8, e = h >> 10 & 31, m = h & 1023; o[i] = (h >> 15 ? -1 : 1) * (e ? (1 + m / 1024) * 2 ** (e - 15) : m * 5.960464477539063e-8); }
  return o;
};
const LAY = M.layers.map(l => ({ i: l.i, o: l.o, w: f16(l.w), b: f16(l.b) }));
const silu = x => x / (1 + Math.exp(-x));
function decode(z) {                                  // latent (6) -> 96 periodic cubic B-spline controls, flat x,y
  let x = z;
  LAY.forEach((l, k) => {
    const y = new Float32Array(l.o);
    for (let r = 0; r < l.o; r++) { let s = l.b[r]; const o = r * l.i; for (let c = 0; c < l.i; c++) s += l.w[o + c] * x[c]; y[r] = k < LAY.length - 1 ? silu(s) : s; }
    x = y;
  });
  return x;
}

// the geometry, layout, partition and seams below are web/fossilnet/index.html, unchanged
// ---------------------------------------------------------------- small geometry
function mulberry(a) { return () => { a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
const gauss = R => Math.sqrt(-2 * Math.log(1 - R())) * Math.cos(6.283185307 * R());

function flatten(q, steps, off = 0, n = NC) {         // periodic cubic B-spline controls -> closed polyline
  const out = new Float64Array(2 * n * steps); let o = 0;
  for (let i = 0; i < n; i++) {
    const a = off + 2 * ((i + n - 1) % n), b = off + 2 * i, c = off + 2 * ((i + 1) % n), d = off + 2 * ((i + 2) % n);
    const x0 = (q[a] + 4 * q[b] + q[c]) / 6, y0 = (q[a + 1] + 4 * q[b + 1] + q[c + 1]) / 6, x1 = (2 * q[b] + q[c]) / 3, y1 = (2 * q[b + 1] + q[c + 1]) / 3;
    const x2 = (q[b] + 2 * q[c]) / 3, y2 = (q[b + 1] + 2 * q[c + 1]) / 3, x3 = (q[b] + 4 * q[c] + q[d]) / 6, y3 = (q[b + 1] + 4 * q[c + 1] + q[d + 1]) / 6;
    for (let s = 0; s < steps; s++) {
      const t = s / steps, u = 1 - t, w0 = u * u * u, w1 = 3 * u * u * t, w2 = 3 * u * t * t, w3 = t * t * t;
      out[o++] = w0 * x0 + w1 * x1 + w2 * x2 + w3 * x3; out[o++] = w0 * y0 + w1 * y1 + w2 * y2 + w3 * y3;
    }
  }
  return out;
}
function area(p) { let s = 0; const n = p.length >> 1; for (let i = 0, j = n - 1; i < n; j = i++) s += p[2 * j] * p[2 * i + 1] - p[2 * i] * p[2 * j + 1]; return s / 2; }
function box(p) { let a = Infinity, b = Infinity, c = -Infinity, d = -Infinity; for (let i = 0; i < p.length; i += 2) { a = Math.min(a, p[i]); c = Math.max(c, p[i]); b = Math.min(b, p[i + 1]); d = Math.max(d, p[i + 1]); } return [a, b, c, d]; }
function hull(p) {
  const n = p.length >> 1, ix = Array.from({ length: n }, (_, i) => i).sort((a, b) => p[2 * a] - p[2 * b] || p[2 * a + 1] - p[2 * b + 1]), h = [];
  const cr = (o, a, b) => (p[2 * a] - p[2 * o]) * (p[2 * b + 1] - p[2 * o + 1]) - (p[2 * a + 1] - p[2 * o + 1]) * (p[2 * b] - p[2 * o]);
  for (const i of ix) { while (h.length >= 2 && cr(h[h.length - 2], h[h.length - 1], i) <= 0) h.pop(); h.push(i); }
  const lo = h.length + 1;
  for (let k = n - 2; k >= 0; k--) { const i = ix[k]; while (h.length >= lo && cr(h[h.length - 2], h[h.length - 1], i) <= 0) h.pop(); h.push(i); }
  h.pop(); const out = new Float64Array(2 * h.length); h.forEach((i, j) => { out[2 * j] = p[2 * i]; out[2 * j + 1] = p[2 * i + 1]; }); return out;
}
function scan(p, px, gw, gh, fn) {                    // cells whose centres are inside polygon p (board mm), even-odd
  const n = p.length >> 1, [, y0, , y1] = box(p), xs = [];
  const r0 = Math.max(0, Math.ceil(y0 / px - .5)), r1 = Math.min(gh - 1, Math.floor(y1 / px - .5));
  for (let r = r0; r <= r1; r++) {
    const yc = (r + .5) * px; xs.length = 0;
    for (let i = 0, j = n - 1; i < n; j = i++) {
      const yi = p[2 * i + 1], yj = p[2 * j + 1];
      if ((yi > yc) !== (yj > yc)) xs.push(p[2 * j] + (yc - yj) * (p[2 * i] - p[2 * j]) / (yi - yj));
    }
    xs.sort((a, b) => a - b);
    for (let m = 0; m + 1 < xs.length; m += 2) {
      const c0 = Math.max(0, Math.ceil(xs[m] / px - .5)), c1 = Math.min(gw - 1, Math.floor(xs[m + 1] / px - .5));
      for (let c = c0; c <= c1; c++) fn(r * gw + c, r, c);
    }
  }
}
function simple(p) {                                  // no two non-adjacent edges cross (grid-hashed)
  const n = p.length >> 1, [x0, y0, x1, y1] = box(p), G = 24, sx = G / (x1 - x0 + 1e-9), sy = G / (y1 - y0 + 1e-9), cells = Array.from({ length: G * G }, () => []);
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n, a = Math.min(G - 1, (Math.min(p[2 * i], p[2 * j]) - x0) * sx | 0), b = Math.min(G - 1, (Math.max(p[2 * i], p[2 * j]) - x0) * sx | 0);
    const c = Math.min(G - 1, (Math.min(p[2 * i + 1], p[2 * j + 1]) - y0) * sy | 0), d = Math.min(G - 1, (Math.max(p[2 * i + 1], p[2 * j + 1]) - y0) * sy | 0);
    for (let u = a; u <= b; u++) for (let v = c; v <= d; v++) cells[v * G + u].push(i);
  }
  const X = (ax, ay, bx, by, cx, cy) => (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
  for (const L of cells) for (let s = 0; s < L.length; s++) for (let t = s + 1; t < L.length; t++) {
    const i = L[s], k = L[t], d = Math.abs(i - k); if (d <= 1 || d >= n - 1) continue;
    const i2 = (i + 1) % n, k2 = (k + 1) % n, ax = p[2 * i], ay = p[2 * i + 1], bx = p[2 * i2], by = p[2 * i2 + 1], cx = p[2 * k], cy = p[2 * k + 1], dx = p[2 * k2], dy = p[2 * k2 + 1];
    if (X(ax, ay, bx, by, cx, cy) * X(ax, ay, bx, by, dx, dy) < 0 && X(cx, cy, dx, dy, ax, ay) * X(cx, cy, dx, dy, bx, by) < 0) return false;
  }
  return true;
}
function edt2(seed, w, h) {                           // squared Euclidean distance (cells) to the nearest seed cell
  const n = Math.max(w, h), f = new Float64Array(n), d = new Float64Array(n), v = new Int32Array(n), z = new Float64Array(n + 1), out = new Float32Array(w * h);
  const dt = len => {
    let k = 0; v[0] = 0; z[0] = -Infinity; z[1] = Infinity;
    for (let q = 1; q < len; q++) {
      let s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]);
      while (s <= z[k]) { k--; s = ((f[q] + q * q) - (f[v[k]] + v[k] * v[k])) / (2 * q - 2 * v[k]); }
      k++; v[k] = q; z[k] = s; z[k + 1] = Infinity;
    }
    for (let q = 0, k2 = 0; q < len; q++) { while (z[k2 + 1] < q) k2++; d[q] = (q - v[k2]) * (q - v[k2]) + f[v[k2]]; }
  };
  for (let x = 0; x < w; x++) { for (let y = 0; y < h; y++) f[y] = seed[y * w + x] ? 0 : 1e20; dt(h); for (let y = 0; y < h; y++) out[y * w + x] = d[y]; }
  for (let y = 0; y < h; y++) { const r = y * w; for (let x = 0; x < w; x++) f[x] = out[r + x]; dt(w); for (let x = 0; x < w; x++) out[r + x] = d[x]; }
  return out;
}
function components(mask, w, h) {                     // 4-connected components of mask (0 = background) -> ids 1.., sizes
  const id = new Int32Array(w * h), size = [0], st = [];
  for (let s = 0; s < w * h; s++) {
    if (!mask[s] || id[s]) continue;
    const c = size.length; let k = 0; id[s] = c; st.push(s);
    while (st.length) {
      const i = st.pop(), x = i % w; k++;
      if (x > 0 && mask[i - 1] && !id[i - 1]) { id[i - 1] = c; st.push(i - 1); }
      if (x < w - 1 && mask[i + 1] && !id[i + 1]) { id[i + 1] = c; st.push(i + 1); }
      if (i >= w && mask[i - w] && !id[i - w]) { id[i - w] = c; st.push(i - w); }
      if (i < w * h - w && mask[i + w] && !id[i + w]) { id[i + w] = c; st.push(i + w); }
    }
    size.push(k);
  }
  return { id, size };
}

// ---------------------------------------------------------------- creatures
const DB = 1 / 34, DI = 2.2 / 34;                     // fit samples: ~1 mm along the outline, ~2.2 mm inside, at a 34 mm piece

function shape(out) {
  const poly = flatten(out, 3), n = poly.length >> 1, sg = area(poly) >= 0 ? 1 : -1;
  const bnd = [], nrm = [];
  let carry = 0;
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n, ax = poly[2 * i], ay = poly[2 * i + 1], dx = poly[2 * j] - ax, dy = poly[2 * j + 1] - ay, L = Math.hypot(dx, dy) || 1e-12;
    for (let t = carry; t < L; t += DB) { bnd.push(ax + dx * t / L, ay + dy * t / L); nrm.push(sg * dy / L, -sg * dx / L); }
    carry = (carry - L) % DB; if (carry < 0) carry += DB;
  }
  const [x0, y0, x1, y1] = box(poly), inn = [], xs = [];
  for (let y = y0 + DI / 2; y < y1; y += DI) {
    xs.length = 0;
    for (let i = 0, j = n - 1; i < n; j = i++) { const yi = poly[2 * i + 1], yj = poly[2 * j + 1]; if ((yi > y) !== (yj > y)) xs.push(poly[2 * j] + (y - yj) * (poly[2 * i] - poly[2 * j]) / (yi - yj)); }
    xs.sort((a, b) => a - b);
    for (let m = 0; m + 1 < xs.length; m += 2) for (let x = x0 + DI / 2 + Math.ceil((xs[m] - x0 - DI / 2) / DI) * DI; x < xs[m + 1]; x += DI) inn.push(x, y);
  }
  return { poly, area: Math.abs(area(poly)), bnd: Float64Array.from(bnd), nrm: Float64Array.from(nrm), inn: Float64Array.from(inn), tol: Math.floor(.01 * (bnd.length + inn.length) / 2), rin: -1, pole: null };
}
function inner(sh) {                                  // pole of inaccessibility: the widest disc inside the outline
  if (sh.rin >= 0) return sh;
  const { poly, inn } = sh, n = poly.length >> 1, [x0, y0, x1, y1] = box(poly); let rin = 0, pole = [(x0 + x1) / 2, (y0 + y1) / 2];
  for (let a = 0; a < inn.length; a += 2) {
    let d = Infinity; const px = inn[a], py = inn[a + 1];
    for (let i = 0, j = n - 1; i < n; j = i++) {
      const ax = poly[2 * j], ay = poly[2 * j + 1], ex = poly[2 * i] - ax, ey = poly[2 * i + 1] - ay;
      const t = Math.max(0, Math.min(1, ((px - ax) * ex + (py - ay) * ey) / (ex * ex + ey * ey + 1e-18))), qx = ax + t * ex - px, qy = ay + t * ey - py;
      d = Math.min(d, qx * qx + qy * qy); if (d < rin * rin) break;
    }
    if (d > rin * rin) { rin = Math.sqrt(d); pole = [px, py]; }
  }
  sh.rin = rin; sh.pole = pole; return sh;
}
function thin(p) {                                   // fossil_field v2: a 1.5 mm erosion of the cut-size outline must stay one piece
  const px = .3, [x0, y0, x1, y1] = box(p), w = Math.ceil((x1 - x0) / px) + 4, h = Math.ceil((y1 - y0) / px) + 4, q = new Float64Array(p.length), out = new Uint8Array(w * h).fill(1);
  for (let i = 0; i < p.length; i += 2) { q[i] = p[i] - x0 + 2 * px; q[i + 1] = p[i + 1] - y0 + 2 * px; }
  scan(q, px, w, h, j => { out[j] = 0; });
  const d2 = edt2(out, w, h), core = new Uint8Array(w * h), lim = (1.5 / px + .5) ** 2;
  for (let j = 0; j < w * h; j++) core[j] = d2[j] >= lim ? 1 : 0;
  return components(core, w, h).size.length !== 2;
}
function make(z) {                                   // a latent -> a creature whose 3 mm parts hold at the smallest piece size, or null
  const out = decode(z), sh = shape(out), p = sh.poly.map(v => v * BASE);
  return simple(sh.poly) && !thin(p) ? { z, out, sh } : null;
}
// ---------------------------------------------------------------- layout (field.pack, greedy docking then growth)
const G1 = 1;                                          // layout grid, mm
function grid(W, H) {
  const gw = W / G1, gh = H / G1, own = new Int32Array(gw * gh).fill(-1), D = new Float32Array(gw * gh);
  for (let r = 0; r < gh; r++) for (let c = 0; c < gw; c++) { const x = (c + .5) * G1, y = (r + .5) * G1; D[r * gw + c] = Math.min(x, W - x, y, H - y); }
  return { W, H, gw, gh, own, D, spots: [] };
}
function placed(sh, p) {
  const co = Math.cos(p.th), si = Math.sin(p.th), a = sh.poly, o = new Float64Array(a.length);
  for (let i = 0; i < a.length; i += 2) { o[i] = p.x + p.s * (co * a[i] - si * a[i + 1]); o[i + 1] = p.y + p.s * (si * a[i] + co * a[i + 1]); }
  return o;
}
function fits(B, sh, x, y, co, si, s, id) {
  const { W, H, own, gw } = B; let bad = 0;
  for (const pts of [sh.bnd, sh.inn]) for (let j = 0; j < pts.length; j += 2) {
    const u = pts[j], v = pts[j + 1], X = x + s * (co * u - si * v), Y = y + s * (si * u + co * v);
    if (X < 0 || Y < 0 || X >= W || Y >= H) { if (++bad > sh.tol) return false; continue; }
    const o = own[(Y / G1 | 0) * gw + (X / G1 | 0)];
    if (o >= 0 && o !== id && ++bad > sh.tol) return false;
  }
  return true;
}
function touch(B, sh, x, y, co, si, s, id) {
  const { W, H, own, gw } = B, b = sh.bnd, nr = sh.nrm, off = 1.5 / s; let t = 0;
  for (let j = 0; j < b.length; j += 2) {
    const u = b[j] + nr[j] * off, v = b[j + 1] + nr[j + 1] * off, X = x + s * (co * u - si * v), Y = y + s * (si * u + co * v);
    if (X < 0 || Y < 0 || X >= W || Y >= H) { t++; continue; }
    const o = own[(Y / G1 | 0) * gw + (X / G1 | 0)]; if (o >= 0 && o !== id) t++;
  }
  return t;
}
function dock(B, c, s, R) {
  const { gw, gh, own, D, spots } = B, sh = inner(c.sh), need = sh.rin * s;
  spots.length = 0;
  for (let r = 0; r < gh; r += 2) for (let x = (r >> 1) & 1; x < gw; x += 2) { const i = r * gw + x, d = D[i]; if (d >= need && d <= need + 3 && own[i] < 0) spots.push(i); }
  if (spots.length < 6) for (let i = 0; i < gw * gh; i += 3) if (D[i] >= need && own[i] < 0) spots.push(i);
  if (!spots.length) return null;
  let best = null, top = -Infinity;
  for (let t = 0; t < 36; t++) {
    const i = spots[R() * spots.length | 0], sx = (i % gw + .5) * G1, sy = ((i / gw | 0) + .5) * G1, a0 = R() * Math.PI / 8;
    for (let a = 0; a < 16; a++) {
      const th = a0 + a * Math.PI / 8, co = Math.cos(th), si = Math.sin(th), x = sx - s * (co * sh.pole[0] - si * sh.pole[1]), y = sy - s * (si * sh.pole[0] + co * sh.pole[1]);
      if (!fits(B, sh, x, y, co, si, s, c.id)) continue;
      const sc = touch(B, sh, x, y, co, si, s, c.id) + .5 * R();
      if (sc > top) { top = sc; best = { x, y, th, s }; }
    }
  }
  return best;
}
function stamp(B, c, on, near) {
  const { gw, gh, own, D } = B, poly = placed(c.sh, c.p);
  let r0 = Infinity, r1 = -1, c0 = Infinity, c1 = -1;
  scan(poly, G1, gw, gh, (i, r, x) => { if (on) { if (own[i] < 0) own[i] = c.id; } else if (own[i] === c.id) own[i] = -1; r0 = Math.min(r0, r); r1 = Math.max(r1, r); c0 = Math.min(c0, x); c1 = Math.max(c1, x); });
  if (!on || !near || r1 < 0) return;
  const P = 26, a = Math.max(0, r0 - P), b = Math.min(gh - 1, r1 + P), e = Math.max(0, c0 - P), f = Math.min(gw - 1, c1 + P), w = f - e + 1, h = b - a + 1, seed = new Uint8Array(w * h);
  for (let r = a; r <= b; r++) for (let x = e; x <= f; x++) if (own[r * gw + x] === c.id) seed[(r - a) * w + x - e] = 1;
  const d2 = edt2(seed, w, h);
  for (let r = a; r <= b; r++) for (let x = e; x <= f; x++) { const i = r * gw + x, d = (Math.sqrt(d2[(r - a) * w + x - e]) - .5) * G1; if (d < D[i]) D[i] = d; }
}

// ---------------------------------------------------------------- partition (field_large.partition: first arrival, bay rule, spur repair)
const HP = .5, BAY = 32;
class Heap {
  constructor(n) { this.k = new Float32Array(n); this.v = new Int32Array(n); this.n = 0; this.top = 0; }
  push(key, val) {
    if (this.n === this.k.length) { const k = new Float32Array(this.n * 2), v = new Int32Array(this.n * 2); k.set(this.k); v.set(this.v); this.k = k; this.v = v; }
    const k = this.k, v = this.v; let i = this.n++;
    while (i > 0) { const p = (i - 1) >> 1; if (k[p] <= key) break; k[i] = k[p]; v[i] = v[p]; i = p; }
    k[i] = key; v[i] = val;
  }
  pop() {
    const k = this.k, v = this.v, rv = v[0]; this.top = k[0];
    const n = --this.n, lk = k[n], lv = v[n]; let i = 0;
    for (;;) { let c = 2 * i + 1; if (c >= n) break; if (c + 1 < n && k[c + 1] < k[c]) c++; if (k[c] >= lk) break; k[i] = k[c]; v[i] = v[c]; i = c; }
    k[i] = lk; v[i] = lv; return rv;
  }
}
async function arrival(polys, W, H, live) {
  const w = W / HP, hh = H / HP, N = w * hh, lab = new Int32Array(N).fill(-1), T = new Float32Array(N).fill(Infinity), fin = new Uint8Array(N), solid = new Uint8Array(N);
  const BD = new Float32Array(N).fill(Infinity), heap = new Heap(N);
  const hulls = polys.map(p => {                       // a piece's own front is 1 + BAY times slower inside its convex hull
    const hp = hull(p), [x0, y0, x1, y1] = box(hp), c0 = Math.max(0, Math.floor(x0 / HP)), r0 = Math.max(0, Math.floor(y0 / HP));
    const hw = Math.min(w - 1, Math.ceil(x1 / HP)) - c0 + 1, hh2 = Math.min(hh - 1, Math.ceil(y1 / HP)) - r0 + 1, m = new Uint8Array(hw * hh2);
    for (let i = 0; i < hp.length; i += 2) { hp[i] -= c0 * HP; hp[i + 1] -= r0 * HP; }
    scan(hp, HP, hw, hh2, j => { m[j] = 1; });
    return { c0, r0, w: hw, h: hh2, m };
  });
  const slow = (L, r, c) => { const q = hulls[L], qr = r - q.r0, qc = c - q.c0; return (qr >= 0 && qc >= 0 && qr < q.h && qc < q.w && q.m[qr * q.w + qc]) ? 1 + BAY : 1; };
  polys.forEach((p, i) => scan(p, HP, w, hh, j => { if (lab[j] < 0) { lab[j] = i; T[j] = 0; } solid[j] = 1; }));
  const k2 = (1.5 / HP) ** 2, din = edt2(solid.map(v => 1 - v), w, hh), core = din.map(v => v > k2 ? 1 : 0), dc = edt2(core, w, hh), tip = new Uint8Array(N);
  for (let j = 0; j < N; j++) if (solid[j] && dc[j] > k2 + 1e-6) { solid[j] = 0; lab[j] = -1; T[j] = Infinity; tip[j] = 1; }   // thin tips are opened off the cut outline
  const dist = new Float32Array(N).fill(Infinity), band = 1.5 * HP, touched = [];
  polys.forEach((p, i) => {                            // exact distance to the outline in a thin band: the fronts start sub-pixel exact
    const n = p.length >> 1; touched.length = 0;
    for (let s = 0, t = n - 1; s < n; t = s++) {
      const ax = p[2 * t], ay = p[2 * t + 1], ex = p[2 * s] - ax, ey = p[2 * s + 1] - ay, ee = ex * ex + ey * ey + 1e-18;
      const c0 = Math.max(0, Math.ceil((Math.min(ax, ax + ex) - band) / HP - .5)), c1 = Math.min(w - 1, Math.floor((Math.max(ax, ax + ex) + band) / HP - .5));
      const r0 = Math.max(0, Math.ceil((Math.min(ay, ay + ey) - band) / HP - .5)), r1 = Math.min(hh - 1, Math.floor((Math.max(ay, ay + ey) + band) / HP - .5));
      for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
        const x = (c + .5) * HP - ax, y = (r + .5) * HP - ay, u = Math.max(0, Math.min(1, (x * ex + y * ey) / ee)), dx = x - u * ex, dy = y - u * ey, d = Math.sqrt(dx * dx + dy * dy), j = r * w + c;
        if (d < dist[j]) { if (dist[j] === Infinity) touched.push(j); dist[j] = d; }
      }
    }
    for (const j of touched) {
      const d = dist[j]; dist[j] = Infinity; if (d > band) continue;
      if (tip[j]) continue;
      if (solid[j]) { if (lab[j] === i) T[j] = -d; continue; }
      if (d < BD[j]) BD[j] = d;
      const r = j / w | 0, t = d * slow(i, r, j - r * w);
      if (t < T[j]) { T[j] = t; lab[j] = i; }
    }
  });
  for (let j = 0; j < N; j++) if (lab[j] >= 0) heap.push(T[j], j);
  const upd = (x, r, c, L) => {
    let a = Infinity, b = Infinity;
    if (c > 0 && fin[x - 1] && lab[x - 1] === L) a = T[x - 1];
    if (c < w - 1 && fin[x + 1] && lab[x + 1] === L && T[x + 1] < a) a = T[x + 1];
    if (r > 0 && fin[x - w] && lab[x - w] === L) b = T[x - w];
    if (r < hh - 1 && fin[x + w] && lab[x + w] === L && T[x + w] < b) b = T[x + w];
    const f = HP * slow(L, r, c);
    let t;
    if (a === Infinity) t = b + f; else if (b === Infinity) t = a + f;
    else { const d = a - b; t = Math.abs(d) < f ? .5 * (a + b + Math.sqrt(2 * f * f - d * d)) : Math.min(a, b) + f; }
    if (t < T[x]) { T[x] = t; lab[x] = L; heap.push(t, x); }
  };
  let ops = 0;
  while (heap.n) {
    const y = heap.pop();
    if (fin[y] || heap.top > T[y]) continue;
    fin[y] = 1; const L = lab[y], r = y / w | 0, c = y - r * w;
    if (c > 0 && !fin[y - 1]) upd(y - 1, r, c - 1, L);
    if (c < w - 1 && !fin[y + 1]) upd(y + 1, r, c + 1, L);
    if (r > 0 && !fin[y - w]) upd(y - w, r - 1, c, L);
    if (r < hh - 1 && !fin[y + w]) upd(y + w, r + 1, c, L);
    if ((++ops & 16383) === 0 && !(await live())) return null;
  }
  return { lab, first: lab.slice(), T, solid, BD, slow, w, hh, n: polys.length };
}
function bounds(P) {
  const { lab, w, hh, n } = P, b = new Int32Array(4 * n).fill(-1);
  for (let i = 0; i < n; i++) { b[4 * i] = hh; b[4 * i + 2] = w; }
  for (let r = 0; r < hh; r++) for (let c = 0; c < w; c++) {
    const L = lab[r * w + c]; if (L < 0) continue; const o = 4 * L;
    if (r < b[o]) b[o] = r; if (r > b[o + 1]) b[o + 1] = r; if (c < b[o + 2]) b[o + 2] = c; if (c > b[o + 3]) b[o + 3] = c;
  }
  return b;
}
function flood(lab, w, hh, from) {                    // hand cells marked -1 to the label that reaches them first
  let q = from, k = 0;
  while (q.length) {
    const nx = [];
    for (const i of q) {
      const L = lab[i], x = i % w;
      if (x > 0 && lab[i - 1] === -1) { lab[i - 1] = L; nx.push(i - 1); k++; }
      if (x < w - 1 && lab[i + 1] === -1) { lab[i + 1] = L; nx.push(i + 1); k++; }
      if (i >= w && lab[i - w] === -1) { lab[i - w] = L; nx.push(i - w); k++; }
      if (i < w * hh - w && lab[i + w] === -1) { lab[i + w] = L; nx.push(i + w); k++; }
    }
    q = nx;
  }
  return k;
}
function connect(P) {                                 // field.connect: keep each piece's largest 4-connected part
  const { lab, w, hh, n } = P, N = w * hh, id = new Int32Array(N), size = [0], owner = [-1], st = [];
  for (let s = 0; s < N; s++) {
    if (id[s] || lab[s] < 0) continue;
    const c = size.length, L = lab[s]; let k = 0; id[s] = c; st.push(s);
    while (st.length) {
      const i = st.pop(), x = i % w; k++;
      if (x > 0 && !id[i - 1] && lab[i - 1] === L) { id[i - 1] = c; st.push(i - 1); }
      if (x < w - 1 && !id[i + 1] && lab[i + 1] === L) { id[i + 1] = c; st.push(i + 1); }
      if (i >= w && !id[i - w] && lab[i - w] === L) { id[i - w] = c; st.push(i - w); }
      if (i < N - w && !id[i + w] && lab[i + w] === L) { id[i + w] = c; st.push(i + w); }
    }
    size.push(k); owner.push(L);
  }
  const keep = new Int32Array(n).fill(0);
  for (let c = 1; c < size.length; c++) { const L = owner[c]; if (!keep[L] || size[c] > size[keep[L]]) keep[L] = c; }
  let stray = 0;
  for (let i = 0; i < N; i++) if (lab[i] >= 0 && id[i] !== keep[lab[i]]) { lab[i] = -1; stray++; }
  if (!stray) return 0;
  const from = [];
  for (let i = 0; i < N; i++) if (lab[i] >= 0) { const x = i % w; if ((x > 0 && lab[i - 1] < 0) || (x < w - 1 && lab[i + 1] < 0) || (i >= w && lab[i - w] < 0) || (i < N - w && lab[i + w] < 0)) from.push(i); }
  flood(lab, w, hh, from);
  return stray;
}
function despike(P, only) {                           // field.despike: gap material a 3.5 mm disc cannot enter goes to the neighbour
  const { lab, solid, w, hh, n } = P, k = Math.max(1, Math.round(1.75 / HP)), pad = k + 1, reach = 2 / HP, bb = bounds(P), touched = new Set();
  let moved = 0;
  for (let i = 0; i < n; i++) {
    if (bb[4 * i + 1] < 0 || (only && !only.has(i))) continue;
    const r0 = Math.max(0, bb[4 * i] - pad), r1 = Math.min(hh - 1, bb[4 * i + 1] + pad), c0 = Math.max(0, bb[4 * i + 2] - pad), c1 = Math.min(w - 1, bb[4 * i + 3] + pad);
    const sw = c1 - c0 + 1 + 2 * pad, sh = r1 - r0 + 1 + 2 * pad, S = sw * sh, m = new Uint8Array(S), non = new Uint8Array(S).fill(1);
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) if (lab[r * w + c] === i) { const j = (r - r0 + pad) * sw + c - c0 + pad; m[j] = 1; non[j] = 0; }
    const din = edt2(non, sw, sh), core = new Uint8Array(S);
    for (let j = 0; j < S; j++) core[j] = m[j] && din[j] > k * k ? 1 : 0;
    const cc = components(core, sw, sh); if (cc.size.length < 2) continue;
    let main = 1; for (let c = 2; c < cc.size.length; c++) if (cc.size[c] > cc.size[main]) main = c;
    const seed = new Uint8Array(S); for (let j = 0; j < S; j++) seed[j] = cc.id[j] === main ? 1 : 0;
    const dm = edt2(seed, sw, sh), body = new Uint8Array(S), loose = new Uint8Array(S); let any = false;
    for (let j = 0; j < S; j++) { if (!m[j]) continue; if (dm[j] <= (k + 1) * (k + 1)) body[j] = 1; else { loose[j] = 1; any = true; } }
    if (!any) continue;
    const far = edt2(body, sw, sh), lc = components(loose, sw, sh), hit = new Uint8Array(lc.size.length);
    for (let j = 0; j < S; j++) { const c = lc.id[j]; if (!c) continue; if ((cc.id[j] && cc.id[j] !== main) || far[j] > reach * reach) hit[c] = 1; }
    const drop = [];
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) {
      const j = (r - r0 + pad) * sw + c - c0 + pad, g = r * w + c;
      if (lc.id[j] && hit[lc.id[j]] && !solid[g]) { lab[g] = -1; drop.push(g); }
    }
    if (!drop.length) continue;
    const from = [];
    for (const g of drop) { const x = g % w; for (const nb of [x > 0 ? g - 1 : -1, x < w - 1 ? g + 1 : -1, g - w, g + w]) if (nb >= 0 && nb < w * hh && lab[nb] >= 0 && lab[nb] !== i) { from.push(nb); } }
    const got = flood(lab, w, hh, from);
    for (const g of drop) { if (lab[g] === -1) lab[g] = i; else touched.add(lab[g]); }  // unreachable spur cells stay where they were
    if (got) { moved += got; touched.add(i); }
  }
  return { moved, touched };
}

// ---------------------------------------------------------------- seams (field.trace + field.seams)
function trace(P) {
  const { lab, first, T, solid, BD, slow, w, hh } = P, NH = (hh + 1) * w, NV = hh * (w + 1), W1 = w + 1;
  const cross = (a, b, ra, ca, rb, cb) => {            // where the two competing fronts meet between cell centres a and b (field.trace)
    const A = lab[a], B = lab[b];
    if (first[a] !== A || first[b] !== B || (solid[a] && solid[b])) return .5;
    let t;
    if (solid[a]) t = -T[a] / (-T[a] + Math.min(BD[b], HP));
    else if (solid[b]) t = 1 + T[b] / (-T[b] + Math.min(BD[a], HP));
    else { const sA = slow(A, rb, cb), sB = slow(B, ra, ca); t = (T[b] - T[a] + HP * sB) / (HP * (sA + sB)); }
    return t > .98 ? .98 : t < .02 ? .02 : t;
  };
  const la = (r, c) => (r >= 0 && r < hh && c >= 0 && c < w) ? lab[r * w + c] : -1;
  const ex = new Uint8Array(NH + NV);
  for (let r = 0; r <= hh; r++) for (let c = 0; c < w; c++) if (la(r - 1, c) !== la(r, c)) ex[r * w + c] = 1;
  for (let r = 0; r < hh; r++) for (let c = 0; c <= w; c++) if (la(r, c - 1) !== la(r, c)) ex[NH + r * W1 + c] = 1;
  const inc = (nd, out) => {                           // cracks meeting at grid corner nd
    const r = nd / W1 | 0, c = nd - r * W1; let k = 0;
    if (c > 0 && ex[r * w + c - 1]) out[k++] = r * w + c - 1;
    if (c < w && r <= hh && ex[r * w + c]) out[k++] = r * w + c;
    if (r > 0 && ex[NH + (r - 1) * W1 + c]) out[k++] = NH + (r - 1) * W1 + c;
    if (r < hh && ex[NH + r * W1 + c]) out[k++] = NH + r * W1 + c;
    return k;
  };
  const ends = e => e < NH ? [((e / w | 0) * W1) + e % w, ((e / w | 0) * W1) + e % w + 1] : [e - NH, e - NH + W1];
  const pt = e => {
    if (e < NH) { const r = e / w | 0, c = e % w; return [(c + .5) * HP, r > 0 && r < hh ? (r - .5 + cross((r - 1) * w + c, r * w + c, r - 1, c, r, c)) * HP : r * HP]; }
    const v = e - NH, r = v / W1 | 0, c = v % W1;
    return [c > 0 && c < w ? (c - .5 + cross(r * w + c - 1, r * w + c, r, c - 1, r, c)) * HP : c * HP, (r + .5) * HP];
  };
  const pair = e => { let a, b; if (e < NH) { const r = e / w | 0, c = e % w; a = la(r - 1, c); b = la(r, c); } else { const v = e - NH, r = v / W1 | 0, c = v % W1; a = la(r, c - 1); b = la(r, c); } return a < b ? [a, b] : [b, a]; };
  const NN = (hh + 1) * W1, stop = new Uint8Array(NN), tmp = [0, 0, 0, 0];
  for (let nd = 0; nd < NN; nd++) {
    const k = inc(nd, tmp); if (!k) continue; const r = nd / W1 | 0, c = nd - r * W1;
    if (k !== 2 || ((r === 0 || r === hh) && (c === 0 || c === w))) stop[nd] = 1;
  }
  const pos = nd => {
    const r = nd / W1 | 0, c = nd - r * W1, k = inc(nd, tmp); let x = 0, y = 0, m = 0;
    for (let i = 0; i < k; i++) { const [a] = pair(tmp[i]); if (a < 0) continue; const p = pt(tmp[i]); x += p[0]; y += p[1]; m++; }
    if (!m) { x = c * HP; y = r * HP; } else { x /= m; y /= m; }
    if (r === 0 || r === hh) y = r * HP; if (c === 0 || c === w) x = c * HP;
    return [x, y];
  };
  const used = new Uint8Array(NH + NV), arcs = [], t2 = [0, 0, 0, 0];
  const walk = (n0, e0) => {
    const path = [e0]; used[e0] = 1; let [a, b] = ends(e0), nd = a === n0 ? b : a, prev = e0, loop = false;
    while (!stop[nd]) {
      inc(nd, t2); const e = t2[0] === prev ? t2[1] : t2[0];
      if (used[e]) { loop = true; break; }
      used[e] = 1; path.push(e); prev = e; [a, b] = ends(e); nd = a === nd ? b : a;
    }
    const xy = []; if (!loop) xy.push(pos(n0)); for (const e of path) xy.push(pt(e)); if (!loop) xy.push(pos(nd));
    arcs.push({ pair: pair(e0), xy, loop });
  };
  for (let nd = 0; nd < NN; nd++) if (stop[nd]) { const k = inc(nd, tmp), es = tmp.slice(0, k); for (const e of es) if (!used[e]) walk(nd, e); }
  for (let e = 0; e < NH + NV; e++) if (ex[e] && !used[e]) walk(ends(e)[0], e);
  return arcs;
}
function smooth(xy, sigma, loop) {                    // Gaussian along the arc, ends pinned
  const n = xy.length; if (n < 4) return xy;
  let L = 0; for (let i = 1; i < n; i++) L += Math.hypot(xy[i][0] - xy[i - 1][0], xy[i][1] - xy[i - 1][1]);
  const s = sigma / (L / (n - 1) + 1e-9), R = Math.ceil(3 * s), k = []; let ks = 0;
  for (let i = -R; i <= R; i++) { const v = Math.exp(-.5 * i * i / (s * s)); k.push(v); ks += v; }
  const get = loop ? i => xy[((i % n) + n) % n] : i => {
    if (i < 0) { const a = xy[0], b = xy[Math.min(n - 1, -i)]; return [2 * a[0] - b[0], 2 * a[1] - b[1]]; }
    if (i >= n) { const a = xy[n - 1], b = xy[Math.max(0, 2 * (n - 1) - i)]; return [2 * a[0] - b[0], 2 * a[1] - b[1]]; }
    return xy[i];
  };
  const out = xy.map((_, i) => { let x = 0, y = 0; for (let j = -R; j <= R; j++) { const p = get(i + j), v = k[j + R]; x += v * p[0]; y += v * p[1]; } return [x / ks, y / ks]; });
  if (!loop) { out[0] = xy[0]; out[n - 1] = xy[n - 1]; }
  return out;
}
function simplify(xy, tol) {                          // Douglas-Peucker
  if (xy.length < 3) return xy;
  const keep = new Uint8Array(xy.length); keep[0] = keep[xy.length - 1] = 1; const st = [[0, xy.length - 1]];
  while (st.length) {
    const [a, b] = st.pop(), [ax, ay] = xy[a], [bx, by] = xy[b], dx = bx - ax, dy = by - ay, L = Math.hypot(dx, dy) || 1e-12; let m = -1, md = tol;
    for (let i = a + 1; i < b; i++) { const d = Math.abs((xy[i][0] - ax) * dy - (xy[i][1] - ay) * dx) / L; if (d > md) { md = d; m = i; } }
    if (m > 0) { keep[m] = 1; st.push([a, m], [m, b]); }
  }
  return xy.filter((_, i) => keep[i]);
}

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
