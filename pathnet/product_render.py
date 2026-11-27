"""Finished-product preview: every cut piece extruded in plywood with the picture printed on top, in its frame tray.

    ..\\.venv\\Scripts\\python.exe product_render.py <run-dir> --cut aphex-field-puzzle.svg --image <picture> [--thickness 4]

The cut file is re-read and polygonised; every piece is shrunk by half the kerf, so the cut lines show as they will
in wood. The picture is cropped to the board's aspect ratio (centred) and mapped onto the board 1:1. Blender
(blender_puzzle.py) builds one extruded object per piece, a frame ring whose inner edge is the board edge and a
backing plate, then renders a top view, a product view and a close-up with a few pieces lifted out. It also exports
the assembled puzzle as GLB (with the printed picture) and STL (geometry only, opens in a slicer).

Outputs in <run-dir>/product. The picture's own resolution is reported: printing needs about 150-300 dpi at board size.
"""
import argparse, json, subprocess, time
from pathlib import Path

import numpy as np
from PIL import Image
from shapely import union_all
from shapely.geometry import LineString
from shapely.ops import polygonize

from frame_export import read_paths, rounded_rect

BLENDER = Path(r'C:\Program Files\Blender Foundation\Blender 5.3\blender.exe')


def prepare(run, cut, image, thickness=4., kerf=.15, frame=15., backing=3., radius=4., samples=96, views=('top', 'hero', 'detail'), lifted=9):
    run = Path(run); out = run / 'product'; out.mkdir(exist_ok=True)
    W, H, paths = read_paths(run / cut)
    faces = list(polygonize(union_all([LineString(xy if not c else np.vstack([xy, xy[:1]])) for xy, c in paths])))
    pieces = []
    for f in sorted(faces, key=lambda p: (round(p.centroid.y / 25), p.centroid.x)):
        g = f.buffer(-kerf / 2, join_style='mitre').simplify(.02, preserve_topology=True)
        if g.geom_type != 'Polygon': g = max(g.geoms, key=lambda q: q.area)
        pieces.append(np.round(np.asarray(g.exterior.coords)[:-1], 3).tolist())
    im = Image.open(image).convert('RGB'); w, h = im.size; target = W / H
    if abs(w / h - target) > 1e-3:                               # centre crop to the board's aspect ratio
        if w / h > target: nw = round(h * target); im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else: nh = round(w / target); im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    dpi = im.size[0] / (W / 25.4)
    tex = im.resize((4096, round(4096 * H / W)), Image.LANCZOS) if im.size[0] < 4096 else im
    tex_path = out / 'print-texture.png'; tex.save(tex_path)
    outer = rounded_rect(-frame, -frame, W + frame, H + frame, radius).tolist(); inner = rounded_rect(0, 0, W, H, 0)
    # the inner loop needs as many points as the outer one for bridging: resample the rectangle along its perimeter
    # inner loop = each outer point moved to the nearest point of the board rectangle: same count, exact corners
    inner_pts = [[min(max(x, 0.), W), min(max(y, 0.), H)] for x, y in outer]
    cfg = dict(W=W, H=H, thickness=thickness, frame=frame, backing=backing, kerf=kerf, pieces=pieces, frame_outer=outer, frame_inner=inner_pts,
               texture=str(tex_path.resolve()), out=str(out.resolve()), samples=samples, views=list(views), lifted=lifted,
               gltf=str((out / 'puzzle-assembled.glb').resolve()), stl=str((out / 'puzzle-assembled.stl').resolve()))
    (out / 'scene.json').write_text(json.dumps(cfg))
    info = dict(board_mm=[W, H], pieces=len(pieces), thickness_mm=thickness, kerf_mm=kerf, frame_mm=frame, backing_mm=backing,
                image_px=list(im.size), image_dpi_at_board_size=round(dpi, 1), print_needs_px_at_150_dpi=round(W / 25.4 * 150),
                print_needs_px_at_300_dpi=round(W / 25.4 * 300))
    (out / 'product.json').write_text(json.dumps(info, indent=1))
    return out, info


def render(out, timeout=3600):
    t0 = time.time()
    r = subprocess.run([str(BLENDER), '-b', '--factory-startup', '--python', str(Path(__file__).with_name('blender_puzzle.py')), '--', str(out / 'scene.json')],
                       capture_output=True, text=True, timeout=timeout)
    (out / 'blender.log').write_text(r.stdout + '\n' + r.stderr)
    return r.returncode, round(time.time() - t0, 1), [l for l in r.stdout.splitlines() if l.startswith(('rendered', 'exported', 'done', 'GPU'))]


if __name__ == '__main__':
    a = argparse.ArgumentParser(description=__doc__.split('\n')[0]); a.add_argument('run', type=Path)
    a.add_argument('--cut', default='aphex-field-puzzle.svg'); a.add_argument('--image', type=Path, required=True)
    a.add_argument('--thickness', type=float, default=4.); a.add_argument('--samples', type=int, default=96)
    a.add_argument('--views', nargs='+', default=['top', 'hero', 'detail']); a = a.parse_args()
    out, info = prepare(a.run, a.cut, a.image, a.thickness, samples=a.samples, views=a.views)
    print(json.dumps(info, indent=1), flush=True)
    code, secs, lines = render(out)
    print(json.dumps(dict(blender_exit=code, seconds=secs, log=lines), indent=1))
