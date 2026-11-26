"""FossilNet for the browser: pathnet/fossil_sources/fossilnet.pt and sources.json -> fossilnet.js beside index.html.

fossilnet.js holds the weights and the drawings' outlines, so keep it out of Git like the checkpoint. Re-run after
every retrain; it reads the checkpoint without PyTorch.

    ..\\..\\.venv\\Scripts\\python.exe export.py
"""
import base64, json, pickle, sys, zipfile
from collections import OrderedDict
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
DATA = HERE.parent.parent / 'pathnet' / 'fossil_sources'
DT = {'FloatStorage': np.float32, 'DoubleStorage': np.float64, 'HalfStorage': np.float16, 'LongStorage': np.int64, 'IntStorage': np.int32}


def load_pt(path):
    """torch.load for a plain state-dict checkpoint, reading the zip directly"""
    zf = zipfile.ZipFile(path); pkl = next(n for n in zf.namelist() if n.endswith('data.pkl')); pre = pkl[:-len('data.pkl')]

    def tensor(storage, offset, size, stride, *_):
        return np.array(np.lib.stride_tricks.as_strided(storage[offset:], tuple(size), [s * storage.itemsize for s in stride]))

    class Reader(pickle.Unpickler):
        def find_class(self, mod, name):
            if (mod, name) == ('torch._utils', '_rebuild_tensor_v2'): return tensor
            if (mod, name) == ('collections', 'OrderedDict'): return OrderedDict
            if mod == 'torch' and name in DT: return name
            raise pickle.UnpicklingError(f'{mod}.{name}')

        def persistent_load(self, pid):
            _, kind, key, *_ = pid
            return np.frombuffer(zf.read(f'{pre}data/{key}'), DT[kind if isinstance(kind, str) else kind.__name__])

    return Reader(zf.open(pkl)).load()


def controls(p, n):
    """fossil_net.controls: closed outline -> n periodic cubic B-spline controls"""
    p = np.vstack([p, p[0]]); d = np.r_[0, np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]; d, ix = np.unique(d, return_index=True); p = p[ix]
    t = np.linspace(0, d[-1], n, endpoint=False); r = np.stack([np.interp(t, d, p[:, i]) for i in range(2)], 1)
    return np.linalg.solve(np.eye(n) * 4 / 6 + np.roll(np.eye(n), 1, axis=1) / 6 + np.roll(np.eye(n), -1, axis=1) / 6, r)


def area(q, steps=6):
    a, b, c, d = np.roll(q, 1, 0), q, np.roll(q, -1, 0), np.roll(q, -2, 0)
    seg = np.stack([(a + 4 * b + c) / 6, (2 * b + c) / 3, (b + 2 * c) / 3, (b + 4 * c + d) / 6], 1)
    t = np.arange(steps) / steps; w = np.stack([(1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3], 1)
    x, y = np.einsum('tk,nkd->ntd', w, seg).reshape(-1, 2).T
    return float(abs(x @ np.roll(y, -1) - y @ np.roll(x, -1)) / 2)


def export(pt, src, dst):
    ck = load_pt(pt); st = ck['state']; meta = json.loads(Path(src).read_text()); N = ck['N']
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a, '<f4').tobytes()).decode()
    layers = [(st[f'net.{i}.weight'], st[f'net.{i}.bias']) for i in sorted({int(k.split('.')[1]) for k in st if k.startswith('net.') and k.endswith('weight')})]
    fams, qs = [], []
    for f in meta['families']:                         # the order of fossil_net.load_sources: every drawing, then its mirror
        for mirror in (False, True):
            o = np.array(f['outline']); o = o[::-1] * [-1, 1] if mirror else o; q = controls(o, N); qs.append(q)
            fams.append(dict(key=f['key'], c=f['category'], n=f['name'], m=int(mirror), e=len(f['features']), a=round(area(q), 6)))
    labels, zsafe = ck['labels'], ck.get('zsafe') or [.6] * ck['K']
    assert len(fams) == ck['K'] == len(labels), 'sources.json does not match the checkpoint'
    for f, r, lab in zip(fams, zsafe, labels):
        assert lab.startswith(f"{f['c']}/{f['n']}"), (lab, f['n']); f['r'] = float(r)
    out = dict(N=N, Z=ck['Z'], FEAT=ck['FEAT'], FP=ck['FP'], E=int(st['emb.weight'].shape[1]), base=meta.get('base_mm', 30.), fam=fams,
               emb=b64(st['emb.weight']), q=b64(np.array(qs)), layers=[dict(i=int(w.shape[1]), o=int(w.shape[0]), w=b64(w), b=b64(b)) for w, b in layers])
    Path(dst).write_text('window.FOSSILNET=' + json.dumps(out, separators=(',', ':')) + ';\n')
    return out, st, layers


if __name__ == '__main__':
    a = sys.argv[1:]
    out, *_ = export(*(a or (DATA / 'fossilnet.pt', DATA / 'sources.json', HERE / 'fossilnet.js')))
    print(f"{len(out['fam'])} families, {sum(l['i'] * l['o'] + l['o'] for l in out['layers'])} weights -> fossilnet.js")
