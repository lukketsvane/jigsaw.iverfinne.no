"""Frame (tray) for a cut puzzle board: production files, strips for 600 mm beds, backing plate and a preview.

    ..\\.venv\\Scripts\\python.exe frame_export.py <run-dir> --cut fossil-field-puzzle.svg --engrave fossil-field-engrave.svg

In a frame the board edge becomes a wall, so edge pieces can no longer slide outwards (see frame_lock.py). A frame cut
separately (ring or strips) has an opening of the board plus `--clearance` on every side, so the assembled puzzle drops in. Outputs in <run-dir>/frame:

* framed-production.svg   frame and puzzle on one sheet (cut red, engrave blue): the board edge is cut once and is also the
                          frame's inner edge, so the laser kerf is the play; for beds of at least board + 2 x width
* frame-ring.svg          the frame alone, one piece
* frame-strips.svg        the same frame as four straight strips (butt joints), which fit a 600 mm bed
* frame-backing.svg       the backing plate the frame or strips are glued to (a tray); hardboard cut to size works too
* framed-preview.png      plywood render of the framed board
* frame.json              dimensions, bed sizes, verification and the framed lock margin

Every file is in millimetres. Coordinates are written out directly, without SVG transforms.
"""
import argparse, json, math, re
from pathlib import Path
from xml.etree import ElementTree as E

import cv2
import numpy as np
from shapely import union_all
from shapely.geometry import LineString, Polygon, box
from shapely.ops import polygonize

NUM = re.compile(r'[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?')


def read_paths(path):
    """(viewBox width, height, list of (points (m,2), closed)) of an M/L/Z polyline SVG, in document order"""
    root = E.parse(path).getroot(); W, H = (float(v) for v in root.attrib['viewBox'].split()[2:4]); out = []
    for q in root.findall('.//{*}path'):
        d = q.attrib['d']
        for part in re.split(r'(?=M)', d.strip()):
            if not part.strip(): continue
            v = list(map(float, NUM.findall(part))); xy = np.array(list(zip(v[::2], v[1::2])))
            if len(xy) >= 2: out.append((xy, part.rstrip().endswith('Z')))
    return W, H, out


def rounded_rect(x0, y0, x1, y1, r, seg=12):
    """closed polyline of a rectangle with corner radius r (r = 0: square corners)"""
    if r <= 0: return np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
        for t in np.linspace(a0, a0 + 90, seg + 1):
            pts.append([cx + r * math.cos(math.radians(t)), cy + r * math.sin(math.radians(t))])
    return np.array(pts)


def svg(path, W, H, groups):
    """groups: list of (id, colour, [(points, closed)]) -> red/blue hairline SVG in mm"""
    def d(xy, closed): return 'M' + ' L'.join(f'{x:.3f} {y:.3f}' for x, y in xy) + (' Z' if closed else '')
    body = ''.join(f'<g id="{gid}" fill="none" stroke="{col}" stroke-width="0.1" stroke-linecap="round" stroke-linejoin="round">'
                   + ''.join(f'<path d="{d(xy, c)}"/>' for xy, c in items) + '</g>' for gid, col, items in groups)
    Path(path).write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.3f}mm" height="{H:.3f}mm" viewBox="0 0 {W:.3f} {H:.3f}">\n{body}\n</svg>')


def verify(path, expected_faces, W, H):
    """re-read the cut layers of a written file and polygonise them: face count, coverage, duplicate segments"""
    root = E.parse(path).getroot(); lines, segs = [], []
    for g in root.findall('.//{*}g'):
        if g.attrib.get('stroke', '').lower() != '#ff0000': continue
        for q in g.findall('.//{*}path'):
            v = list(map(float, NUM.findall(q.attrib['d']))); xy = list(zip(v[::2], v[1::2]))
            if q.attrib['d'].rstrip().endswith('Z'): xy.append(xy[0])
            lines.append(LineString(xy)); segs.extend(tuple(sorted((a, b))) for a, b in zip(xy, xy[1:]))
    faces = list(polygonize(union_all(lines, grid_size=.001)))
    cover = union_all(faces).area
    return dict(faces=len(faces), expected=expected_faces, area_mm2=round(cover, 2), sheet_area_mm2=round(W * H, 2),
                duplicate_segments=len(segs) - len(set(segs)), ok=len(faces) == expected_faces)


def render(path, W, H, frame, pieces, engrave, scale=None):
    scale = scale or 4600 / max(W, H); img = np.full((round(H * scale), round(W * scale), 3), 255, np.uint8)
    P = lambda xy: np.round(np.asarray(xy) * scale * 16).astype(np.int32)
    cv2.fillPoly(img, [P(frame.exterior.coords)], (118, 160, 196), cv2.LINE_AA, shift=4)
    cv2.fillPoly(img, [P(frame.interiors[0].coords)], (70, 90, 110), cv2.LINE_AA, shift=4)      # the tray floor in the gap
    tones = [(150, 184, 220), (172, 198, 214), (160, 190, 226), (182, 184, 208), (150, 198, 214), (172, 184, 226)]
    try:
        from field import colouring                 # neighbours never share a tone
        tone = colouring(pieces)
    except Exception:
        tone = list(range(len(pieces)))
    for t, p in zip(tone, pieces): cv2.fillPoly(img, [P(p.exterior.coords)], tones[t % len(tones)], cv2.LINE_AA, shift=4)
    cv2.polylines(img, [P(p.exterior.coords) for p in pieces], True, (60, 60, 60), max(1, int(scale * .25)), cv2.LINE_AA, shift=4)
    cv2.polylines(img, [P(frame.exterior.coords), P(frame.interiors[0].coords)], True, (50, 50, 50), max(1, int(scale * .3)), cv2.LINE_AA, shift=4)
    for xy, closed in engrave:
        if closed: cv2.fillPoly(img, [P(xy)], (40, 52, 70), cv2.LINE_AA, shift=4) if Polygon(xy).area < 40 else cv2.polylines(img, [P(xy)], True, (70, 90, 120), max(1, int(scale * .3)), cv2.LINE_AA, shift=4)
        else: cv2.polylines(img, [P(xy)], False, (70, 90, 120), max(1, int(scale * .3)), cv2.LINE_AA, shift=4)
    cv2.imwrite(str(path), img)


def frame(run, cut='fossil-field-puzzle.svg', engrave=None, width=15., clearance=.25, radius=4., gap=5., bed=(600., 400.)):
    run = Path(run); out = run / 'frame'; out.mkdir(exist_ok=True)
    W, H, cuts = read_paths(run / cut); eng = read_paths(run / engrave)[2] if engrave and (run / engrave).exists() else []
    # one sheet: the puzzle's own border is the frame's inner edge (one cut), so the kerf is the play
    W1, H1 = W + 2 * width, H + 2 * width; shift = lambda items: [(xy + [width, width], c) for xy, c in items]
    outer1 = rounded_rect(0, 0, W1, H1, radius)
    pieces = [Polygon(np.asarray(p.exterior.coords) + [width, width]) for p in polygonize(union_all([LineString(xy if not c else np.vstack([xy, xy[:1]])) for xy, c in cuts]))]
    svg(out / 'framed-production.svg', W1, H1, [('engrave', '#0000ff', shift(eng)), ('puzzle-cut', '#ff0000', shift(cuts)), ('frame-cut', '#ff0000', [(outer1, True)])])
    checks = {'framed-production.svg': verify(out / 'framed-production.svg', len(pieces) + 1, W1, H1)}      # pieces and the frame ring
    # a frame cut on its own: opening = board + clearance on every side
    Wo, Ho = W + 2 * (width + clearance), H + 2 * (width + clearance)
    outer = rounded_rect(0, 0, Wo, Ho, radius); inner = rounded_rect(width, width, Wo - width, Ho - width, 0)
    svg(out / 'frame-ring.svg', Wo, Ho, [('frame-cut', '#ff0000', [(inner, True), (outer, True)])])
    checks['frame-ring.svg'] = verify(out / 'frame-ring.svg', 2, Wo, Ho)      # the ring and the cut-out opening
    # strips: two long ones between the short ones (butt joints), laid out for the bed
    long_len, short_len = W + 2 * clearance, Ho
    strips = [(long_len, width), (long_len, width), (short_len, width), (short_len, width)]; items, y = [], 0.
    for L, w_ in strips:
        items.append((rounded_rect(0, y, L, y + w_, 0), True)); y += w_ + gap
    SW, SH = max(long_len, short_len), y - gap
    svg(out / 'frame-strips.svg', SW, SH, [('strip-cut', '#ff0000', items)])
    checks['frame-strips.svg'] = verify(out / 'frame-strips.svg', 4, SW, SH)
    svg(out / 'frame-backing.svg', Wo, Ho, [('backing-cut', '#ff0000', [(outer, True)])])
    render(out / 'framed-preview.png', W1, H1, Polygon(outer1, [rounded_rect(width, width, W1 - width, H1 - width, 0)]), pieces, shift(eng))
    lock = json.loads((run / 'frame-lock.json').read_text()) if (run / 'frame-lock.json').exists() else None
    fits = lambda w_, h_: (w_ <= bed[0] and h_ <= bed[1]) or (h_ <= bed[0] and w_ <= bed[1])
    report = dict(board_mm=[W, H], frame_width_mm=width, clearance_mm=clearance, corner_radius_mm=radius,
                  one_sheet_mm=[round(W1, 3), round(H1, 3)],
                  outer_mm=[round(Wo, 3), round(Ho, 3)], opening_mm=[round(W + 2 * clearance, 3), round(H + 2 * clearance, 3)],
                  strips_mm=[[round(L, 3), w_] for L, w_ in strips], strips_sheet_mm=[round(SW, 3), round(SH, 3)], pieces=len(pieces),
                  fits_600x400_bed={'one-sheet production': fits(W1, H1), 'frame ring': fits(Wo, Ho), 'strips': fits(SW, SH), 'backing': fits(Wo, Ho), 'puzzle alone': fits(W, H)},
                  verification=checks, lock_margin=lock,
                  assembly='Glue the frame ring, or the four strips (long ones between the short ones), onto the backing plate to make a tray. '
                           'Cut inner shapes before outer ones. The opening leaves the clearance on every side of the assembled puzzle.')
    (out / 'frame.json').write_text(json.dumps(report, indent=1))
    return report


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0]); a.add_argument('run', type=Path)
    a.add_argument('--cut', default='fossil-field-puzzle.svg'); a.add_argument('--engrave'); a.add_argument('--width', type=float, default=15.)
    a.add_argument('--clearance', type=float, default=.25); a.add_argument('--radius', type=float, default=4.); a = a.parse_args()
    r = frame(a.run, a.cut, a.engrave, a.width, a.clearance, a.radius)
    print(json.dumps({k: r[k] for k in ('one_sheet_mm', 'outer_mm', 'opening_mm', 'strips_sheet_mm', 'pieces', 'fits_600x400_bed', 'verification')}, indent=1))
