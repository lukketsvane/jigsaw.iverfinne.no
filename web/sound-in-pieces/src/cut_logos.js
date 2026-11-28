// ---------------------------------------------------------------- logos
const zc = v => Math.max(-2 * ZR, Math.min(2 * ZR, v));
function logo(R) {                                    // a logo from the sampled latent range, held to the cut rules, or the mean logo
  for (let t = 0; t < 30; t++) { const z = new Float32Array(ZD); for (let i = 0; i < ZD; i++) z[i] = ZR * (2 * R() - 1); const c = make(z); if (c) return c; }
  const z = new Float32Array(ZD), out = decode(z);
  return { z, out, sh: shape(out) };
}
