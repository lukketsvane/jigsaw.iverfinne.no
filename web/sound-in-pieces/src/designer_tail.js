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
