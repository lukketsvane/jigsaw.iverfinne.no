"""Render colorless paired silhouettes using the exported SVG faces and their audited IDs.

From the repository root, run this script with the baseline, candidate, per-piece
evaluation CSV, and desired output SVG. Every panel uses one common millimeter scale.
"""
import argparse
import csv
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pathnet.lizard_edge_eval import center, read_board, tip_positions


NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", NS)
INK = "#233934"
MUTED = "#586e62"
BASE = "#236c81"
MODEL = "#ab4230"
PAPER = "#fbf9f1"
def node(root, tag, **attrs):
    return ET.SubElement(root, f"{{{NS}}}{tag}", {k.replace("_","-"): str(v) for k,v in attrs.items()})


def label(root, x, y, words, size=16, fill=INK, weight=400, **attrs):
    result = node(root, "text", x=x, y=y, font_size=size, fill=fill, font_weight=weight, **attrs)
    result.text = words


def shape(root, coords, origin, scale, fill="none", stroke="none", width=0, dash=None):
    q = coords * scale + np.asarray(origin)
    d = "M" + " L".join(f"{x:.2f},{y:.2f}" for x,y in q) + " Z"
    attrs = {"d":d,"fill":fill,"stroke":stroke,"stroke_width":width,"stroke_linejoin":"round"}
    if dash:
        attrs["stroke_dasharray"]=dash
    node(root, "path", **attrs)


def create(baseline, candidate, comparison, output):
    before, _ = read_board(baseline)
    after, _ = read_board(candidate)
    with comparison.open(encoding="utf-8") as f:
        rows = {int(row["piece"]): row for row in csv.DictReader(f)}
    worst = min(rows, key=lambda k: float(rows[k]["silhouette_iou"]))
    fixed_ids = [0,54,108,162]
    ids = fixed_ids + ([worst] if worst not in fixed_ids else [next(k for k in sorted(rows) if k not in fixed_ids)])
    normalized = []
    for pid in ids:
        a,b = before[pid],after[pid]
        # Apply one common rotation to the pair, pointing the original snout up.
        snout = tip_positions(a)[0]
        angle = -np.pi/2 - np.arctan2(snout[1],snout[0])
        co,si=np.cos(angle),np.sin(angle)
        rot=np.array([[co,si],[-si,co]])
        aa=(a.outline-center(a.polygon))@rot
        bb=(b.outline-center(b.polygon))@rot
        merged=np.vstack((aa,bb))
        mid=(merged.min(0)+merged.max(0))/2
        normalized.append((aa-mid,bb-mid,np.ptp(merged,axis=0)))
    dims=np.max([item[2] for item in normalized],axis=0)
    card_width=233
    scale=min((card_width-24)/dims[0], 177/dims[1])
    width,height=1370,924
    svg=ET.Element(f"{{{NS}}}svg",{"width":str(width),"height":str(height),"viewBox":f"0 0 {width} {height}"})
    node(svg,"title").text="Do the exported pieces keep the baseline lizard silhouette?"
    node(svg,"desc").text="Five pairs of outlines from the laser cut, at a common physical scale. Ink, spine and eyes are omitted. A third row overlays each pair."
    node(svg,"rect",width=width,height=height,fill=PAPER)
    body=node(svg,"g",font_family="Arial, Helvetica, sans-serif")
    label(body,35,35,"FORMVERK    /    ACTUAL-SIZE LIZARD TRIAL",13,MUTED,700,letter_spacing=1.5)
    label(body,35,84,"Do the pieces still read as lizards?",43,INK,600)
    board = json.loads((candidate / "audit.json").read_text(encoding="utf-8"))
    width_mm, height_mm = board["assembled_outline_mm"]
    label(body, 36, 117,
          f"Seed {board['seed']}   ·   {len(after)} pieces   ·   {width_mm:.2f} × {height_mm:.2f} mm assembled footprint", 19)
    node(body,"path",d="M35 141H1335",stroke="#a4b3a8",stroke_width=1)
    for j,(pid,(aa,bb,_)) in enumerate(zip(ids,normalized)):
        x=168+j*card_width
        label(body,x+card_width/2,172,f"Piece {pid:03d}",19,INK,700,text_anchor="middle")
        sub="Lowest outline overlap" if pid==worst else ("Preset sample" if pid in fixed_ids else "Additional sample")
        label(body,x+card_width/2,193,sub,13,MUTED,400,text_anchor="middle")
        if j>0:
            node(body,"path",d=f"M{x} 156V831",stroke="#dce3db",stroke_width=1)
        ox=x+card_width/2
        shape(body,aa,(ox,303),scale,fill=INK)
        shape(body,bb,(ox,513),scale,fill=INK)
        shape(body,bb,(ox,726),scale,fill="#f3eadb")
        shape(body,bb,(ox,726),scale,stroke=MODEL,width=2.0)
        shape(body,aa,(ox,726),scale,stroke=BASE,width=1.7,dash="4 3")
        record=rows[pid]
        label(body,ox,836,f"Outline overlap  {float(record['silhouette_iou']):.1%}",14,INK,600,text_anchor="middle")
    for y,head,sub in [(297,"Original","generator"),(506,"Trained","generator"),(718,"Aligned","overlay")]:
        label(body,35,y,head,19,INK,700)
        label(body,35,y+23,sub,15,MUTED)
    for y in [410,622,857]:
        node(body,"path",d=f"M35 {y}H1335",stroke="#bfccbf",stroke_width=1)
    label(body,35,879,"All panels use the same physical scale. Each pair is centered and turned together. Eyes, spine and color are hidden.",14)
    node(body,"path",d="M35 901h26",stroke=BASE,stroke_width=2,stroke_dasharray="4 3")
    label(body,67,906,"Original cut",14)
    node(body,"path",d="M163 901h26",stroke=MODEL,stroke_width=2)
    label(body,195,906,"Trained cut",14)
    sx=1195;sw=5*scale
    node(body,"path",d=f"M{sx} 901h{sw:.2f} M{sx} 896v10 M{sx+sw:.2f} 896v10",stroke=INK,stroke_width=1.8)
    label(body,sx+sw+10,906,"5 mm",14)
    output.parent.mkdir(parents=True,exist_ok=True)
    ET.ElementTree(svg).write(output,encoding="utf-8",xml_declaration=True)
    return ids


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ("baseline","candidate","comparison","output"):
        parser.add_argument(arg,type=Path)
    args=parser.parse_args()
    print(create(args.baseline,args.candidate,args.comparison,args.output))

