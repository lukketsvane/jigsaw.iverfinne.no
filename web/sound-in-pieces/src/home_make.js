// A screen cut needs a simple, non-empty silhouette. The designer also enforces
// the minimum material thickness required to manufacture its smallest pieces.
function make(z) {
  const out = decode(z);
  const sh = shape(out);
  if (!Number.isFinite(sh.area) || sh.area <= 0 || !simple(sh.poly)) return null;
  return { z, out, sh };
}
