import argparse, json, math
from pathlib import Path
import numpy as np
from shapely.geometry import Polygon, Point, box, LineString
from shapely.ops import unary_union
from shapely import set_precision, get_parts, union_all
from scipy.spatial import Delaunay
from generate import ROOT, names, sample, flatten, ref, export, library

def build(seed,w,h,n,logo_share,jitter=.11):
    if not (w>0 and h>0 and n>=1 and 0<=logo_share<=1 and 0<=jitter<=.5): raise ValueError('positive board dimensions and motif count, logo share in [0,1], jitter in [0,.5] required')
    rng=np.random.default_rng(seed); board=box(0,0,w,h); placed=[]; ctr=[]; meta=[]; gap=3.5; precision=.0001
    cols=math.ceil(math.sqrt(n*w/h)); rows=math.ceil(n/cols); dx=w/cols; dy=h/rows; slots=[]
    for y in range(rows):
        for x in range(cols): slots.append(np.array([(x+.5)*dx,(y+.5)*dy]))
    rng.shuffle(slots)
    for i,pos in enumerate(slots[:n]):
        k=0 if i<round(n*logo_share) else int(rng.integers(1,len(names))); q,p,z=sample(rng,k); ok=False
        for factor in [1.0,.94,.87,.8,.72,.64]:
            for _ in range(22):
                a=rng.uniform(0,2*np.pi); mat=np.array([[np.cos(a),-np.sin(a)],[np.sin(a),np.cos(a)]]); size=min(dx,dy)*1.05*factor*rng.uniform(.93,1.07); xy=flatten(q@mat.T*size,4); c=pos+rng.uniform(-jitter,jitter,2)*[dx,dy]; pp=set_precision(Polygon(xy+c).simplify(.025,preserve_topology=True),precision)
                if not board.buffer(-gap).covers(pp) or not pp.is_valid or pp.area<100: continue
                if any(pp.distance(o)<gap for o in placed): continue
                er=pp.buffer(-1.25)
                if er.is_empty or er.geom_type!='Polygon': continue
                placed.append(pp); ctr.append(np.array(pp.representative_point().coords[0])); meta.append({'class':names[k],'k':k,'size':float(size),'latent':z.tolist(),'angle':float(a),'center_mm':c.tolist(),'reference_iou':float(p.intersection(ref).area/p.union(ref).area) if k==0 else None}); ok=True; break
            if ok:break
        if not ok: raise ValueError(f'Cannot place requested motif {i+1}/{n} on {w} x {h} mm board')
    corners=np.array([[0,0],[w,0],[w,h],[0,h]]); pts=np.vstack([ctr,corners]); tri=Delaunay(pts); free=board.difference(unary_union(placed)); free=set_precision(free,precision)
    others=[]
    for ix in tri.simplices:
        t=set_precision(Polygon(pts[ix]),precision); q=t.intersection(free)
        others.extend(p for p in get_parts(q) if p.geom_type=='Polygon' and p.area>1e-7)
    print('initial',len(placed),len(others),flush=True)
    def flag(p):
        e=p.buffer(-1.25); return int(p.area<130 or e.is_empty or e.geom_type!='Polygon' or len(p.interiors)>0)
    for step in range(130):
        bad=[i for i,p in enumerate(others) if flag(p)]
        if not bad:break
        changed=False
        for i in sorted(bad,key=lambda i:others[i].area):
            p=others[i]; candidates=[]
            for j,q in enumerate(others):
                if i==j or p.distance(q)>.00011: continue
                length=p.boundary.intersection(q.boundary).length
                if length<.001:continue
                u=p.union(q)
                if u.geom_type!='Polygon' or len(u.interiors)>0:continue
                candidates.append((flag(u),-length,j,u))
            if candidates:
                _,_,j,u=min(candidates,key=lambda e:e[:3]); others=[q for k,q in enumerate(others) if k not in (i,j)]+[u];changed=True;break
        if not changed:break
    ps=placed+others; cover=unary_union(ps); net=unary_union([p.boundary for p in ps]); overlap=sum(p.area for p in ps)-cover.area; holes=sum(len(p.interiors) for p in ps)
    r={'seed':seed,'jitter':jitter,'width_mm':w,'height_mm':h,'pieces':len(ps),'requested_motifs':n,'requested_logo_pieces':round(n*logo_share),'neural_motifs':len(placed),'logo_pieces':sum(m['k']==0 for m in meta),'placed_motifs':len(placed),'holes':holes,'coverage_error_mm2':board.symmetric_difference(cover).area,'overlap_mm2':max(0,overlap),'invalid_pieces':sum(not p.is_valid for p in ps),'erosion_flags_2_5mm':sum(flag(p) for p in ps),'min_area_mm2':min(p.area for p in ps),'motifs':meta}
    return ps,placed,net,r
if __name__=='__main__':
    import tempfile, shutil
    from refine import polish
    p=argparse.ArgumentParser();p.add_argument('--seed',type=int,default=22);p.add_argument('--width',type=float,default=300);p.add_argument('--height',type=float,default=220);p.add_argument('--motifs',type=int,default=24);p.add_argument('--logo-share',type=float,default=.85);p.add_argument('--jitter',type=float,default=.11);p.add_argument('--output',type=Path,default=ROOT);a=p.parse_args();a.output.mkdir(exist_ok=True,parents=True)
    with tempfile.TemporaryDirectory() as d:
        out=Path(d); ps,pl,net,r=build(a.seed,a.width,a.height,a.motifs,a.logo_share,a.jitter); export(out,ps,pl,net,r); polish(out)
        for f in out.iterdir(): shutil.copy2(f,a.output/f.name)
