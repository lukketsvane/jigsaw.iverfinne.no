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
