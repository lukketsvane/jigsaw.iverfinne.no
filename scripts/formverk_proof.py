"""Prepare a registered A4 shop proof and a portable player from one Formverk run."""

import argparse
import csv
import json
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SVG = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG)
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
A4 = (297, 210)


def node(parent, tag, **attributes):
    return ET.SubElement(parent, f"{{{SVG}}}{tag}", {k: str(v) for k, v in attributes.items()})


def proof_sheets(run, out):
    """Give both files identical board placement and four targets in the waste."""
    art, cut = [ET.parse(run / name).getroot() for name in ("artwork.svg", "cut.svg")]
    if art.get("viewBox") != cut.get("viewBox"):
        raise ValueError("Print and cut viewBoxes differ")
    x, y, width, height = map(float, art.attrib["viewBox"].split())
    left, top = (A4[0] - width) / 2, (A4[1] - height) / 2
    if min(left, top) < 12:
        raise ValueError("The board needs at least 12 mm of waste on all sides on landscape A4")
    centers = [(a, b) for b in (top - 4, top + height + 4) for a in (left - 4, left + width + 4)]
    shift = f"translate({left-x} {top-y})"
    for source, printing in ((art, True), (cut, False)):
        sheet = ET.Element(f"{{{SVG}}}svg", width="297mm", height="210mm", viewBox="0 0 297 210")
        board = node(sheet, "g", id="board", transform=shift)
        board.extend(source)
        marks = node(sheet, "g", id="printed_targets" if printing else "registration_engrave",
                     fill="none", stroke="#181818" if printing else "#00aa00",
                     **{"stroke-width": ".12"})
        for cx, cy in centers:
            if printing:
                for radius in (.5, 1.5):
                    node(marks, "circle", cx=cx, cy=cy, r=radius)
            else:
                node(marks, "path", d=f"M{cx-.22} {cy}h.44 M{cx} {cy-.22}v.44")
        if printing:
            sx, sy = (A4[0]-100)/2, top+height+(A4[1]-top-height)/2
            node(marks, "path", d=f"M{sx} {sy}h100 M{sx} {sy-1}v2 M{sx+100} {sy-1}v2")
            label = node(sheet, "text", x=sx+50, y=sy-1, fill="#282828",
                         **{"text-anchor":"middle", "font-family":"sans-serif", "font-size":"2.4"})
            label.text = "100 mm"
        name = "print-a4.svg" if printing else "laser-a4.svg"
        ET.ElementTree(sheet).write(out / name, encoding="utf-8", xml_declaration=True)
    return centers


def standalone_player(run, out):
    puzzle = json.loads((run / "puzzle.json").read_text(encoding="utf-8"))
    if not puzzle.get("artwork", {}).get("href", "").startswith("data:image/"):
        raise ValueError("Expected a puzzle with an embedded photograph")
    # Escape raw HTML end tags even if a supplied title or filename contains one.
    payload = json.dumps(puzzle, separators=(",", ":")).replace("<", "\\u003c")
    engine = (ROOT / "web/formverk/player.js").read_text(encoding="utf-8")
    page = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Formverk · Fossil puzzle</title><style>
*{box-sizing:border-box}body{margin:0;background:#f7f4ec;color:#27352b;font-family:system-ui,sans-serif}
main{max-width:1260px;margin:auto;padding:clamp(12px,3vw,40px)}header,footer{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{margin:0;font:normal clamp(26px,4vw,48px)/1.1 Georgia,serif}p{margin:.25em 0;font-size:14px}
svg{width:100%;height:clamp(300px,70dvh,860px);display:block;touch-action:none}
button{font:inherit;border:1px solid #a0a99b;border-radius:6px;color:#203e31;background:#fcfbf5;padding:.55em .9em;cursor:pointer}button:first-child{background:#284737;color:white}
.play-piece{cursor:grab}.dragging{cursor:grabbing}footer{border-top:1px solid #cccbbb;padding-top:12px}</style>
<main><header><h1>Fossil puzzle</h1><p id="edition"></p></header>
<svg id="stage" role="application" aria-label="Drag the fossil puzzle pieces together"><g id="pieces"></g></svg>
<footer><div><button id="few">Scatter eight</button> <button id="all">Scatter all</button> <button id="solve">Assemble</button></div><p id="status" aria-live="polite"></p></footer></main>
<script id="board-data" type="application/json">__DATA__</script><script>__ENGINE__</script><script>
const board=JSON.parse(document.getElementById('board-data').textContent);
const stage=document.getElementById('stage'), layer=document.getElementById('pieces');
const player=new FormverkPlayer(stage,layer,({largest,total,groups})=>{
  document.getElementById('status').textContent=groups===1?'Assembled':largest+' of '+total+' pieces connected';
});
function fit(){const b=layer.getBBox(),p=Math.max(board.width_mm,board.height_mm)*.04;
stage.setAttribute('viewBox',[b.x-p,b.y-p,b.width+2*p,b.height+2*p].join(' '));}
document.getElementById('edition').textContent=board.pieces.length+' pieces · Seed '+board.metadata.seed;
player.load(board);player.assemble();fit();
for(const mode of ['few','all'])document.getElementById(mode).onclick=()=>{player.scatter(mode);fit();};
document.getElementById('solve').onclick=()=>{player.assemble();fit();};
</script></html>"""
    (out / "play.html").write_text(page.replace("__ENGINE__",engine).replace("__DATA__",payload), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    centers = proof_sheets(args.run, args.out)
    with (args.out / "registration.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("target", "x_mm_from_left", "y_mm_from_top", "measured_dx_mm", "measured_dy_mm", "radial_error_mm"))
        for name, (x, y) in zip(("top left", "top right", "bottom left", "bottom right"), centers):
            writer.writerow((name, x, y, "", "", ""))
    standalone_player(args.run, args.out)


if __name__ == "__main__":
    main()
