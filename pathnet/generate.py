import argparse, json, math, os
from pathlib import Path
import numpy as np, torch
from shapely.geometry import Polygon, LineString, box, Point
from shapely.ops import unary_union, polygonize, nearest_points
from shapely import set_precision, get_parts
from scipy.spatial import Delaunay
from train import ROOT, Model, N, Z, path, flatten

torch.set_num_threads(1)
# PATHNET_CKPT=<file> loads another decoder, e.g. a PathNet v2 checkpoint from train_v2.py; unset, it is v1's pathnet.pt.
# ZLIM is the latent range per class that sample() draws from and field.refine may use; REF_IOU the IoU with the traced
# logo a logo latent must keep (v2's latent cube is calibrated to stay recognisable, so its threshold only catches failures).
CKPT=Path(os.environ.get('PATHNET_CKPT') or ROOT/'pathnet.pt')
ck=torch.load(CKPT,weights_only=True,map_location='cpu'); names=ck['names']
if ck.get('arch')=='pathnet-v2':
    from train_v2 import build
    model=build(ck); Z=ck['latent']; ZLIM_F=[float(v) for v in ck['zlim']]; REF_IOU=ck.get('ref_iou_min',.45)
else:
    model=Model(len(names)); model.load_state_dict(ck['state']); model.eval(); ZLIM_F=[.28]+[1.]*(len(names)-1); REF_IOU=.85
ZLIM=torch.tensor(ZLIM_F,dtype=torch.float32)          # what field.refine scales its tanh latents by
ref=Polygon(flatten(np.array(json.loads((ROOT/'reference.json').read_text())['logo_control_points']),8))

def decode(k,z):
    with torch.no_grad(): return model(torch.tensor([k]),torch.tensor(np.asarray(z)[None],dtype=torch.float32))[0].numpy()
def sample(rng,k):
    for _ in range(60):
        lim=ZLIM_F[k]; z=rng.uniform(-lim,lim,Z); q=decode(k,z); p=Polygon(flatten(q,6))
        if p.is_valid and p.area>.035 and (k!=0 or p.intersection(ref).area/p.union(ref).area>=REF_IOU): return q,p,z
    raise ValueError('no valid sample')
def points_svg(coords):
    return 'M'+' L'.join(f'{x:.5f} {y:.5f}' for x,y in coords)
def poly_svg(p): return points_svg(p.exterior.coords)+' Z'+''.join(points_svg(r.coords)+' Z' for r in p.interiors)
def outsvg(f,body,w,h,units='mm'):
    f.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}{units}" height="{h}{units}" viewBox="0 0 {w} {h}">\n{body}\n</svg>')
def library(out,rng,count=60):
    w=300; h=math.ceil(count/10)*29; items=[]; ids=[]
    for i in range(count):
        k=0 if i%3!=2 else int(rng.integers(1,len(names))); q,p,z=sample(rng,k); x=15+(i%10)*30;y=14.5+(i//10)*29; q=q*25+np.array([x,y]); items.append(f'<path id="piece-{i:03d}" data-family="{names[k]}" d="{path(q)}"/>'); ids.append(k)
    outsvg(out/'aphex-pathnet-shapes.svg','<g fill="black">'+''.join(items)+'</g>',w,h)
    outsvg(out/'aphex-pathnet-shapes-cut.svg','<g fill="none" stroke="#ff0000" stroke-width="0.1">'+''.join(items)+'</g>',w,h)
    return ids

def export(out,ps,placed,net,r):
    w=r['width_mm'];h=r['height_mm']; fills=[]; colors=['#efeee8','#d9d8d0','#e5e4dc','#c9cbc3','#f9f8f4'];
    for i,p in enumerate(ps):
        motif=next((j for j,q in enumerate(placed) if p.symmetric_difference(q).area<.002),None); color=('#282924' if r['motifs'][motif]['k']==0 else '#64675f') if motif is not None else colors[i%len(colors)]; fills.append(f'<path d="{poly_svg(p)}" fill="{color}" stroke="#111111" stroke-width="0.35" stroke-linejoin="round"/>')
    outsvg(out/'aphex-pathnet-puzzle-preview.svg',''.join(fills),w,h)
    lines=[l for l in get_parts(net) if l.geom_type=='LineString']; outsvg(out/'aphex-pathnet-puzzle.svg','<g fill="none" stroke="#ff0000" stroke-width="0.1" stroke-linecap="round" stroke-linejoin="round">'+''.join(f'<path d="{points_svg(l.coords)}"/>' for l in lines)+'</g>',w,h)
    (out/'puzzle-report.json').write_text(json.dumps(r,indent=2))
    outsvg(out/'aphex-pathnet-piece-outlines.svg','<g fill="none" stroke="#111" stroke-width=".2">'+''.join(f'<path id="p{i}" d="{poly_svg(p)}"/>' for i,p in enumerate(ps))+'</g>',w,h)

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--seed',type=int,default=22); p.add_argument('--count',type=int,default=60); p.add_argument('--output',type=Path,default=ROOT); a=p.parse_args(); a.output.mkdir(parents=True,exist_ok=True); library(a.output,np.random.default_rng(a.seed),a.count)
