import argparse
import json
import time
from pathlib import Path
import numpy as np, cv2, torch
from torch import nn
from scipy.ndimage import gaussian_filter1d
from shapely.geometry import Polygon
ROOT=Path(__file__).resolve().parent
N=96; Z=6

def norm(p):
    p=np.asarray(p,dtype=float); lo=p.min(0); hi=p.max(0); return (p-(lo+hi)/2)/max(hi-lo)
def resample(p,n):
    p=np.vstack([p,p[0]]); d=np.r_[0,np.cumsum(np.linalg.norm(np.diff(p,axis=0),axis=1))]; d,ix=np.unique(d,return_index=True); p=p[ix]; t=np.linspace(0,d[-1],n,endpoint=False); return np.stack([np.interp(t,d,p[:,i]) for i in range(2)],1)
def controls(p):
    a=np.eye(N)*4/6+np.roll(np.eye(N),1,axis=1)/6+np.roll(np.eye(N),-1,axis=1)/6; return np.linalg.solve(a,resample(p,N))
def bezier(q):
    a=np.roll(q,1,axis=0); b=q; c=np.roll(q,-1,axis=0); d=np.roll(q,-2,axis=0); return np.stack([(a+4*b+c)/6,(2*b+c)/3,(b+2*c)/3,(b+4*c+d)/6],1)
def flatten(q,steps=5):
    b=bezier(q); t=np.arange(steps)/steps; w=np.stack([(1-t)**3,3*(1-t)**2*t,3*(1-t)*t*t,t**3],1); return np.einsum('tk,nkd->ntd',w,b).reshape(-1,2)
def path(q):
    b=bezier(q); f=lambda p:' '.join(f'{v:.5f}' for v in p.reshape(-1)); return 'M '+f(b[0,0])+' '+' '.join('C '+f(v[1:]) for v in b)+' Z'
def load():
    d=json.loads((ROOT/'aphex-glyph-dataset.json').read_text()); im=cv2.imread(str(ROOT/'IMG_4659.png'),0); c=max(cv2.findContours(np.uint8(im<100)*255,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_NONE)[0],key=cv2.contourArea)[:,0]; p=gaussian_filter1d(resample(c,512),2.,axis=0,mode='wrap'); base=[('APHEX LOGO',norm(p))]+[(g['glyph'],norm(g['piece'])) for g in d['glyphs']]+[(g['id'],norm(g['piece'])) for g in d['variants']]; return [(k,controls(p)) for k,p in base]
def deform(p,z,logo=False):
    x, y = p.T
    a, b, c, d, e, g = np.moveaxis(z, -1, 0)[..., None] * (.58 if logo else 1.)
    return np.stack([
        x * (1 + .12 * a) + .09 * b * y + .10 * c * np.sin(np.pi * y) + .055 * d * np.sin(2 * np.pi * y),
        y * (1 + .12 * e) + .08 * g * x + .07 * c * np.sin(np.pi * x) - .035 * d * np.sin(2 * np.pi * x),
    ], axis=-1)
class Model(nn.Module):
    def __init__(self,k):
        super().__init__(); self.emb=nn.Embedding(k,48); self.net=nn.Sequential(nn.Linear(48+Z,192),nn.SiLU(),nn.Linear(192,192),nn.SiLU(),nn.Linear(192,N*2))
    def forward(self,k,z): return self.net(torch.cat([self.emb(k),z],1)).reshape(-1,N,2)
def main():
    parser = argparse.ArgumentParser(description="Train the Aphex PathNet decoder")
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--epochs", type=int, default=80)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--out", type=Path, default=ROOT)
    args = parser.parse_args()
    if args.epochs < 1 or args.batch_size < 1:
        parser.error("--epochs and --batch-size must be positive")
    if args.device == "cuda" and not torch.cuda.is_available():
        parser.error("CUDA was requested but is unavailable")
    device = torch.device(
        ("cuda" if torch.cuda.is_available() else "cpu") if args.device == "auto" else args.device
    )
    torch.manual_seed(22); torch.set_num_threads(2); rng=np.random.default_rng(22); base=load(); names=[x[0] for x in base]
    def data(test):
        ids=[]; zs=[]; ys=[]
        for k,(_,p) in enumerate(base):
            n=(400 if k==0 else 30) if test else (2600 if k==0 else 105)
            z = rng.uniform(-1, 1, (n, Z))
            ids.append(np.full(n, k, dtype=np.int64))
            zs.append(z.astype(np.float32))
            ys.append(deform(p, z, k == 0).astype(np.float32))
        return tuple(torch.from_numpy(np.concatenate(values)).to(device) for values in (ids, zs, ys))

    k, z, y = data(False)
    kt, zt, yt = data(True)
    model = Model(len(names)).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=.002, weight_decay=.0001, fused=device.type == "cuda")
    trace = []
    epochs = args.epochs
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
        torch.cuda.synchronize(device)
    started = time.perf_counter()
    training_started_at = time.time()
    for ep in range(epochs):
        log_epoch = ep in (0, 4, 9, 19, 39, 59) or ep + 1 == epochs
        tr = torch.zeros((), device=device) if log_epoch else None
        for ix in torch.randperm(len(k), device=device).split(args.batch_size):
            diff = model(k[ix], z[ix]) - y[ix]
            mse = diff.square().mean()
            loss = mse + 1.2 * (diff - torch.roll(diff, 1, 1)).square().mean()
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            if tr is not None:
                tr.add_(mse.detach(), alpha=len(ix))
        if log_epoch:
            with torch.no_grad(): q=model(kt,zt); val=(q-yt).square().mean().item(); logo=(q[:400]-yt[:400]).square().mean().item()
            trace.append({'epoch':ep+1,'train_mse':tr.item()/len(k),'validation_mse':val,'logo_mse':logo}); print(trace[-1],flush=True)
    training_seconds = time.perf_counter() - started
    training_finished_at = time.time()
    peak_memory = torch.cuda.max_memory_allocated(device) / 2**20 if device.type == "cuda" else None
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    model.eval()
    with torch.inference_mode():
        q = model(kt, zt).cpu().numpy()
    model.cpu()
    torch.save({'state':model.state_dict(),'names':names,'points':N,'latent':Z},out/'pathnet.pt'); layers=[{'w':l.weight.detach().numpy().round(7).tolist(),'b':l.bias.detach().numpy().round(7).tolist()} for l in model.net if isinstance(l,nn.Linear)]; (out/'decoder.json').write_text(json.dumps({'names':names,'points':N,'latent':Z,'emb':model.emb.weight.detach().numpy().round(7).tolist(),'layers':layers},separators=(',',':')))
    ref=Polygon(flatten(base[0][1],8)); valid=[]; ious=[]; test_ious=[]; all_iou=[]
    train_classes = k.cpu().numpy()
    validation_classes = kt.cpu().numpy()
    class_ious = [[] for _ in names]
    for i,(pred,target) in enumerate(zip(q,yt.cpu().numpy())):
        a=Polygon(flatten(pred)); b=Polygon(flatten(target)); good=a.is_valid and not a.is_empty and a.area>0; valid.append(good)
        if good and b.is_valid:
            iou = a.intersection(b).area / a.union(b).area
            all_iou.append(iou)
            class_ious[validation_classes[i]].append(iou)
        if i<400:
            ious.append(a.intersection(ref).area/a.union(ref).area if good else 0); test_ious.append(a.intersection(b).area/a.union(b).area if good and b.is_valid else 0)
    report={'architecture':'conditional neural decoder: class embedding + 6D latent -> 96 periodic cubic B-spline controls -> closed cubic Bezier SVG','parameters':sum(p.numel() for p in model.parameters()),'classes':len(names),'training_contours':len(k),'logo_training_contours':2600,'validation_contours':len(kt),'logo_validation_contours':400,'epochs':epochs,'trace':trace,'valid_validation_contours':int(sum(valid)),'mean_validation_target_iou_valid':float(np.mean(all_iou)),'logo_mean_reference_iou':float(np.mean(ious)),'logo_ref_iou_at_least_0_85':int(sum(i>=.85 for i in ious)),'logo_mean_augmented_target_iou':float(np.mean(test_ious)),'validation_scope':'held-out deformations of known shape families, not unseen families or human recognition'}
    train_counts = np.bincount(train_classes, minlength=len(names))
    validation_counts = np.bincount(validation_classes, minlength=len(names))
    valid_counts = np.bincount(validation_classes[np.asarray(valid, dtype=bool)], minlength=len(names))
    report["per_class"] = {
        name: dict(training_contours=int(train_counts[j]), validation_contours=int(validation_counts[j]),
                   valid_validation_contours=int(valid_counts[j]),
                   validation_mean_iou=float(np.mean(class_ious[j])) if class_ious[j] else None)
        for j, name in enumerate(names)
    }
    report.update(device=str(device), batch_size=args.batch_size, training_seconds=training_seconds,
                  training_started_at=training_started_at, training_finished_at=training_finished_at,
                  training_contours_per_second=len(k) * epochs / training_seconds, peak_cuda_memory_mib=peak_memory)
    (out/'report.json').write_text(json.dumps(report,indent=2)); (out/'reference.json').write_text(json.dumps({'logo_control_points':base[0][1].tolist(),'names':names})); print(report,flush=True)
if __name__=='__main__': main()
