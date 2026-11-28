"""FossilNet v3 for the browser: pathnet/fossil_sources/v3/fossilnet3.pt -> fossilnet3.js beside index.html.

The decoder's weights are stored as int8 with one float scale per output row (about a quarter of float32), plus
the latents of the owner's drawings (anchors), a set of corpus landmarks with their body plans, and the principal
directions of the latent cloud. fossilnet3.js is rebuilt after every retrain and kept out of Git like the checkpoint.

    ..\\..\\.venv\\Scripts\\python.exe export.py [--fp16]
"""
import argparse, base64, json, sys
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
PT = HERE.parent.parent / 'pathnet' / 'fossil_sources' / 'v3' / 'fossilnet3.pt'


def b64(a, dt='<f4'):
    return base64.b64encode(np.ascontiguousarray(a, dt).tobytes()).decode()


def quant(w):
    """int8 per output row: w ~ q * s[:, None]"""
    s = np.abs(w).max(1) / 127 + 1e-12; return np.clip(np.round(w / s[:, None]), -127, 127).astype(np.int8), s.astype(np.float32)


def decode_np(layers, z):
    """the decoder in numpy from (weight, bias) pairs: silu(inp) -> residual silu blocks -> linear out"""
    silu = lambda x: x / (1 + np.exp(-x))
    (w, b), *blocks, (wo, bo) = layers; h = silu(z @ w.T + b)
    for w, b in blocks: h = h + silu(h @ w.T + b)
    return h @ wo.T + bo


def export(pt=PT, dst=HERE / 'fossilnet3.js', fp16=False):
    ck = torch.load(pt, map_location='cpu', weights_only=False); st = {k: v.numpy().astype(np.float64) for k, v in ck['decoder'].items()}
    names = ['inp'] + [f'blocks.{i}' for i in range(ck['blocks'])] + ['out']
    layers = [(st[f'{n}.weight'], st[f'{n}.bias']) for n in names]
    out, qlayers = [], []
    for w, b in layers:
        if fp16:
            out.append(dict(i=w.shape[1], o=w.shape[0], h=b64(w, '<f2'), b=b64(b))); qlayers.append((w.astype(np.float16).astype(np.float64), b))
        else:
            q, s = quant(w); out.append(dict(i=w.shape[1], o=w.shape[0], q=base64.b64encode(q.tobytes()).decode(), s=b64(s), b=b64(b))); qlayers.append((q * s[:, None].astype(np.float64), b))
    # parity of the stored weights against the float decoder on anchors and landmarks
    zs = np.concatenate([ck['anchors'].numpy(), ck['landmarks'].numpy()[:256]]).astype(np.float64)
    ref = decode_np(layers, zs); got = decode_np(qlayers, zs); err = np.abs(ref - got)[:, :2 * ck['N']].max()
    model = dict(v=3, Z=ck['Z'], N=ck['N'], FEAT=ck['FEAT'], FP=ck['FP'], base=30., maxFactor=1.8, blocks=ck['blocks'], layers=out,
                 anchors=dict(z=b64(ck['anchors'].numpy()), labels=ck['anchor_labels'], sf=[round(float(x), 4) for x in ck['anchor_sf']]),
                 landmarks=dict(z=b64(ck['landmarks'].numpy()), plan=[int(x) for x in ck['landmark_plan']], names=list(ck['plan_names'])),
                 pca=dict(mean=b64(ck['pca_mean'].numpy()), comp=b64(ck['pca'].numpy()), sd=[float(x) for x in ck['pca_sd']]),
                 step=int(ck['step']), hours=round(float(ck['hours']), 2), parity_max_control_error=float(err))
    dst.write_text('window.FOSSILNET=' + json.dumps(model, separators=(',', ':')) + ';\n')
    return model, err


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0]); a.add_argument('--fp16', action='store_true'); a.add_argument('--pt', type=Path, default=PT)
    a.add_argument('--out', type=Path, default=HERE / 'fossilnet3.js'); a = a.parse_args()
    m, err = export(a.pt, a.out, a.fp16)
    print(f"step {m['step']}: {sum(l['i'] * l['o'] + l['o'] for l in m['layers']) / 1e6:.2f} M weights, {len(m['anchors']['labels'])} anchors, "
          f"{len(m['landmarks']['plan'])} landmarks, largest control error from storage {err:.2e} -> {a.out} ({a.out.stat().st_size / 1e6:.1f} MB)")
    sys.exit(0 if err < 5e-3 else 1)
