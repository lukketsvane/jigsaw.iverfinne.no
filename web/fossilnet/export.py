"""FossilNet v3 for the browser: pathnet/fossil_sources/fossilnet-v3.pt -> fossilnet.js beside index.html.

fossilnet.js holds the decoder and the latent codes of the owner's drawings, so keep it out of Git like the checkpoint.
Re-run after every retrain (pathnet/fossil_morph.py train); it reads the checkpoint without PyTorch.

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
    """torch.load for a checkpoint of tensors, numbers and lists, reading the zip directly"""
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


def export(pt, dst):
    ck = load_pt(pt); st = ck['state']
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a, '<f2').tobytes()).decode()          # half precision: 0.1 % of a weight, far below a 3 mm part
    layers = [(st[f'dec.{i}.weight'], st[f'dec.{i}.bias']) for i in sorted({int(k.split('.')[1]) for k in st if k.startswith('dec.') and k.endswith('weight')})]
    out = dict(N=ck['N'], Z=ck['Z'], FEAT=ck['FEAT'], FP=ck['FP'], base=30., layers=[dict(i=int(w.shape[1]), o=int(w.shape[0]), w=b64(w), b=b64(b)) for w, b in layers],
               std=b64(ck['std']), bank=b64(ck['bank']), anchors=b64(ck['anchors']), labels=ck['labels'])
    Path(dst).write_text('window.FOSSILNET=' + json.dumps(out, separators=(',', ':')) + ';\n')
    return out, st, layers


if __name__ == '__main__':
    a = sys.argv[1:]
    out, *_ = export(*(a or (DATA / 'fossilnet-v3.pt', HERE / 'fossilnet.js')))
    print(f"latent {out['Z']}, {sum(l['i'] * l['o'] + l['o'] for l in out['layers'])} decoder weights, {len(out['labels'])} drawings as anchors -> fossilnet.js")
