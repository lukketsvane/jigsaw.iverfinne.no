import json, re, math
from pathlib import Path
import numpy as np
from shapely import union_all, get_parts, set_precision
from shapely.geometry import Polygon,LineString,Point,box
from shapely.affinity import affine_transform
from shapely.ops import polygonize,linemerge
from generate import ROOT, outsvg, poly_svg, points_svg, ref
from collections import defaultdict
import xml.etree.ElementTree as E

def readlines(p):
    for q in E.parse(p).findall('.//{*}path'):
        v=list(map(float,re.findall(r'[-+]?[0-9]+(?:\.[0-9]+)?',q.attrib['d']))); yield LineString(list(zip(v[::2],v[1::2])))
def flag(p):
    e=p.buffer(-1.25);return e.is_empty or e.geom_type!='Polygon' or len(p.interiors)>0 or p.area<130

def edges(p):
    for r in [p.exterior,*p.interiors]:
        xy=list(r.coords)
        for a,b in zip(xy,xy[1:]):
            yield tuple(sorted((tuple(a),tuple(b))))

def tab_curve(a,b,width,depth,sign):
    d=np.linalg.norm(b-a); v=(b-a)/d; n=np.array([-v[1],v[0]])
    t=np.linspace(0,1,13); w=np.stack([(1-t)**3,3*(1-t)**2*t,3*(1-t)*t*t,t**3],1)
    half=np.vstack([w@np.array(c) for c in [
        [(-.35,0),(-.14,0),(-.16,.13),(-.20,.26)],
        [(-.20,.26),(-.24,.39),(-.50,.45),(-.50,.62)],
        [(-.50,.62),(-.50,.85),(-.23,1),(0,1)]
    ]])
    q=np.vstack([half,half[-2::-1]*[-1,1]])
    xy=(a+b)/2+q[:,0,None]*width*v+q[:,1,None]*depth*sign*n
    return LineString(np.round(np.vstack([a,xy,b]),2))

def backtrack(line):
    xy=np.array(line.coords); v=xy[-1]-xy[0]; t=(xy-xy[0])@v/np.linalg.norm(v)
    return float(np.max(np.maximum.accumulate(t)-t))

def translation_lock_stats(ps,logo_ids):
    from shapely.geometry.polygon import orient
    owners=defaultdict(list)
    for i,p in enumerate(ps):
        xy=list(orient(p,sign=1).exterior.coords)
        for a,b in zip(xy,xy[1:]):
            if a!=b:owners[tuple(sorted((a,b)))].append((i,math.atan2(b[1]-a[1],b[0]-a[0])))
    pairs=defaultdict(list)
    for items in owners.values():
        if len(items)==2:
            (i,a),(j,_)=items
            pairs[(i,j)].append(a)
    locked=[]
    for pair,angles in pairs.items():
        a=np.sort(angles)
        if np.max(np.diff(np.r_[a,a[0]+2*np.pi]))<np.pi-.05:locked.append(pair)
    linked={i for pair in locked for i in pair}
    return {'adjacent_pairs':len(pairs),'zero_clearance_translation_locked_pairs':len(locked),'pieces_with_translation_locked_neighbor':len(linked),'logo_pieces_with_translation_locked_neighbor':len(linked.intersection(logo_ids))}

def polish(out=ROOT):
    r=json.loads((out/'puzzle-report.json').read_text()); w=r['width_mm'];h=r['height_mm'];board=box(0,0,w,h); rng=np.random.default_rng(r['seed']+33)
    net=union_all(list(readlines(out/'aphex-pathnet-puzzle.svg')),grid_size=.01); ps=list(polygonize(net)); orig=[]
    for p in E.parse(out/'aphex-pathnet-piece-outlines.svg').findall('.//{*}path')[:r['neural_motifs']]:
        xy=list(map(float,re.findall(r'[-+]?[0-9]+(?:\.[0-9]+)?',p.attrib['d'])));orig.append(Polygon(list(zip(xy[::2],xy[1::2]))))
    motifs=[]
    for q in orig:
        p=max(ps,key=lambda p:p.intersection(q).area); assert p.intersection(q).area/q.area>.998; motifs.append(p)
    mids={p.wkb for p in motifs};other=[p for p in ps if p.wkb not in mids]; print('clean faces',len(ps),'motifs',len(motifs),flush=True)
    target=33
    while len(other)>target:
        candidates=[]
        for i,p in enumerate(other):
            for j,q in enumerate(other[:i]):
                if p.area+q.area>2500 or p.distance(q)>1e-8:continue
                l=p.boundary.intersection(q.boundary).length
                if l<1:continue
                u=p.union(q)
                if u.geom_type!='Polygon' or flag(u):continue
                candidates.append((u.area-.5*l,i,j,u))
        if not candidates:break
        _,i,j,u=min(candidates,key=lambda c:c[0]);other=[p for k,p in enumerate(other) if k not in (i,j)]+[u]
    print('merged',len(other),flush=True)
    allp=motifs+other; owners=defaultdict(list)
    for i,p in enumerate(allp):
        for e in edges(p): owners[e].append(i)
    fixed=[]; movable=[]
    for e,ids in owners.items():
        l=LineString(e)
        if len(ids)==2 and min(ids)>=len(motifs):movable.append(l)
        else:fixed.append(l)
    base=union_all(fixed); seams=list(get_parts(linemerge(movable))) if movable else []
    occupied=union_all(motifs); kept=seams.copy(); changed=0; tabs=[]
    for i,l in enumerate(seams):
        xy=np.array(l.coords); a,b=xy[0],xy[-1]; d=np.linalg.norm(b-a)
        if d<5 or np.max(abs(((b-a)[0]*(xy-a)[:,1]-(b-a)[1]*(xy-a)[:,0]))/d)>.035:continue
        v=(b-a)/d; n=np.array([-v[1],v[0]]); t=np.linspace(0,1,41); order=[1,-1] if rng.random()<.5 else [-1,1]
        candidates=[]
        for width,depth in [(min(14,d*.55),min(8,d*.30)),(min(11,d*.48),min(5.5,d*.22)),(min(9,d*.42),min(4,d*.17))]:
            if width>=8 and depth>=3:
                for sign in order:
                    c=tab_curve(a,b,width,depth,sign)
                    if backtrack(c)>=1: candidates.append((True,c))
        for amp in [min(6.0,d*.20),min(4.0,d*.13),min(2.5,d*.08),min(1.1,d*.04)]:
            for sign in order:
                disp=sign*amp*np.sin(2*np.pi*t)*np.sin(np.pi*t)**2
                candidates.append((False,LineString(np.round(a+t[:,None]*(b-a)+disp[:,None]*n,2))))
        for is_tab,c in candidates:
            if not c.is_simple or not board.covers(c) or c.intersection(occupied).length>.005:continue
            if any(c.crosses(k) or c.overlaps(k) or (c.intersection(k).geom_type=='Point' and c.intersection(k).distance(Point(a))>.01 and c.intersection(k).distance(Point(b))>.01) for j,k in enumerate(kept) if j!=i):continue
            if c.intersection(base).length>.01:continue
            cand=kept.copy();cand[i]=c
            test=list(polygonize(union_all([base,*cand],grid_size=.01)))
            if len(test)!=len(allp) or any(flag(p) for p in test):continue
            kept[i]=c;changed+=1
            if is_tab:tabs.append(c)
            break
    net=union_all([base,*kept],grid_size=.01); ps=list(polygonize(net)); assert len(ps)==len(allp); assert not any(flag(p) for p in ps)
    chosen=[]
    for q in motifs:
        p=max(ps,key=lambda p:p.intersection(q).area);assert p.intersection(q).area/q.area>.998;chosen.append(p)
    ids={p.wkb for p in chosen};ps=chosen+[p for p in ps if p.wkb not in ids];net=union_all([p.boundary for p in ps],grid_size=.01)
    cut='<g fill="none" stroke="#ff0000" stroke-width="0.1" stroke-linecap="round" stroke-linejoin="round">'+''.join(f'<path d="{points_svg(l.coords)}"/>' for l in get_parts(net) if l.geom_type=='LineString')+'</g>'
    outsvg(out/'aphex-pathnet-puzzle.svg',cut,w,h)
    exported=list(readlines(out/'aphex-pathnet-puzzle.svg')); actual=list(polygonize(union_all(exported))); segments=[]
    for l in exported:
        xy=list(l.coords);segments.extend(tuple(sorted((a,b))) for a,b in zip(xy,xy[1:]))
    cover=union_all(actual); gap=board.symmetric_difference(cover).area; overlap=sum(p.area for p in actual)-cover.area;dups=len(segments)-len(set(segments));
    assert len(actual)==len(ps) and gap<1e-5 and abs(overlap)<1e-5 and dups==0 and not any(flag(p) for p in actual)
    from shapely.strtree import STRtree
    tree=STRtree(actual); tab_faces=set(); buffers={}
    for tab in tabs:
        adjoining=[]
        for i in tree.query(tab.buffer(.015)):
            if i not in buffers:buffers[i]=actual[i].boundary.buffer(.015)
            if tab.difference(buffers[i]).length<.01:adjoining.append(int(i))
        assert len(adjoining)==2, 'tab must be shared by two pieces'
        tab_faces.update(adjoining)
    r['undercut_tabs']=len(tabs)
    r['pieces_with_undercut_tabs']=len(tab_faces)
    r['min_tab_backtrack_mm']=min(map(backtrack,tabs),default=0)
    logo_ious=[]; logo_faces=set()
    for i,m in enumerate(r['motifs']):
        if m['k']!=0:continue
        j=max(range(len(actual)),key=lambda j:actual[j].intersection(chosen[i]).area); face=actual[j]; logo_faces.add(j)
        if 'center_mm' not in m:continue
        a=m['angle']; s=m['size']; cx,cy=m['center_mm']; ca=math.cos(a)*s; sa=math.sin(a)*s
        expected=affine_transform(ref,[ca,-sa,sa,ca,cx,cy])
        iou=face.intersection(expected).area/face.union(expected).area
        assert iou>=.85, f'Logo {i} final reference IoU {iou:.4f} below .85'
        m['final_reference_iou']=iou; logo_ious.append(iou)
    r.update(translation_lock_stats(actual,logo_faces))
    r['logo_share_of_pieces']=r['logo_pieces']/len(actual)
    if logo_ious:r['min_final_logo_reference_iou']=min(logo_ious)
    colors=['#efeee8','#d9d8d0','#e5e4dc','#c9cbc3','#f9f8f4']; fill=[]
    for i,p in enumerate(ps):
        color=('#252622' if r['motifs'][i]['k']==0 else '#65675e') if i<len(chosen) else colors[i%len(colors)];fill.append(f'<path d="{poly_svg(p)}" fill="{color}" stroke="#161715" stroke-width="0.35" stroke-linejoin="round"/>')
    outsvg(out/'aphex-pathnet-puzzle-preview.svg',''.join(fill),w,h);outsvg(out/'aphex-pathnet-piece-outlines.svg','<g fill="none" stroke="#111" stroke-width=".2">'+''.join(f'<path id="p{i}" d="{poly_svg(p)}"/>' for i,p in enumerate(ps))+'</g>',w,h)
    r.update({'pieces':len(actual),'holes':sum(len(p.interiors) for p in actual),'coverage_error_mm2':gap,'overlap_mm2':max(0,overlap),'invalid_pieces':sum(not p.is_valid for p in actual),'erosion_flags_2_5mm':sum(flag(p) for p in actual),'min_area_mm2':min(p.area for p in actual),'duplicate_segments':dups,'curved_connectors':changed,'snap_precision_mm':.01,'verification':'reparsed exported SVG, noded and polygonized without tolerance snapping'});(out/'puzzle-report.json').write_text(json.dumps(r,indent=2));print({k:v for k,v in r.items() if k!='motifs'},flush=True)
if __name__=='__main__':polish()
