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

