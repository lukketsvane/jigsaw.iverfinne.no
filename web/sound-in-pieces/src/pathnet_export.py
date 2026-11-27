"""PathNet APHEX LOGO decoder for the browser: decoder.json (all 59 families) -> js/pathnet-logo.js (logo family only).
The logo's 48-d family embedding is folded into the first layer's bias, so the browser sees 6 -> 192 -> 192 -> 192."""
import base64, json, numpy as np
import os, sys
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')
d = json.load(open(os.path.join(ROOT, 'pathnet', 'decoder.json')))
k = d['names'].index('APHEX LOGO')
(w1, b1), (w2, b2), (w3, b3) = [(np.array(l['w'], np.float32), np.array(l['b'], np.float32)) for l in d['layers']]
emb = np.array(d['emb'][k], np.float32)
b1f = b1 + w1[:, :48] @ emb                     # Linear(48+6): emb first, then z (torch.cat([emb, z]))
w1z = w1[:, 48:]
f16 = lambda a: base64.b64encode(np.ascontiguousarray(a, '<f2').tobytes()).decode()
layers = [dict(i=int(w.shape[1]), o=int(w.shape[0]), w=f16(w), b=f16(b)) for w, b in ((w1z, b1f), (w2, b2), (w3, b3))]
ref = json.load(open(os.path.join(ROOT, 'pathnet', 'reference.json')))['logo_control_points']
out = dict(N=d['points'], Z=d['latent'], range=0.28, layers=layers, ref=ref)
open(os.path.join(ROOT, 'web', 'sound-in-pieces', 'js', 'pathnet-logo.js'), 'w').write('window.PATHNET_LOGO=' + json.dumps(out, separators=(',', ':')) + ';\n')
# check: decode z=0 with float32 numpy vs folded f16 path
silu = lambda x: x / (1 + np.exp(-x))
z = np.zeros(6, np.float32)
full = (np.concatenate([emb, z]))
h = silu(w1 @ full + b1); h = silu(w2 @ h + b2); q = (w3 @ h + b3).reshape(-1, 2)
h2 = silu(w1z @ z + b1f); h2 = silu(w2 @ h2 + b2); q2 = (w3 @ h2 + b3).reshape(-1, 2)
print('max diff', float(np.abs(q - q2).max()), 'span', q.min(0), q.max(0), 'bytes', len(open(os.path.join(ROOT, 'web', 'sound-in-pieces', 'js', 'pathnet-logo.js')).read()))
