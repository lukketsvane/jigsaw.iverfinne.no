"""Resize + encode every source image to WebP widths. Writes site/img and build/images.json."""
import json, os, sys
from PIL import Image, ImageOps

import os
B = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(B, '..', 'public')
D = os.environ.get('SIP_SRC', os.path.join(B, '..', '..', '..', 'runs', 'sound-in-pieces'))  # private sources: renders, cut field, GLB
OUT = os.path.join(SITE, 'img')
# name: (source, widths, quality, ops)
SRC = {
    'hero':        (f'{D}/figma/s01-board.jpg',        [800, 1400, 2000, 2480], 80, {}),
    'surface':     (f'{D}/figma/s02-surface.jpg',      [800, 1400, 2000, 2560], 78, {}),
    'specimen':    (f'{D}/figma/s03-specimen.jpg',     [700, 1100, 1420], 80, {}),
    'opening':     (f'{D}/figma/s04-opening.jpg',      [800, 1400, 2112], 78, {}),
    'terrain':     (f'{D}/figma/s07-alt.jpg',          [800, 1400, 1912], 78, {}),
    'tape':        (f'{D}/figma/s08-tape-still.jpg',   [800, 1320], 80, {}),
    'macro':       (f'{D}/figma/s09-macro.jpg',        [700, 1200], 80, {}),
    'frontback':   (f'{D}/figma/s09-frontback.jpg',    [800, 1376], 80, {}),
    'terrainform': (f'{D}/figma/s10-terrain.jpg',      [700, 1200], 80, {}),
    'freeform':    (f'{D}/figma/s10-freeform.jpg',     [700, 1200], 80, {}),
    'angled':      (f'{D}/figma/s11-angled.jpg',       [800, 1376], 80, {}),
    'boxpouch':    (f'{D}/figma/s11-boxpouch.jpg',     [800, 1376], 80, {}),
    'kraft-hero':  (f'{D}/figma/s12-kraft-hero.png',   [800, 1400, 1920], 80, {}),
    'kraft-macro': (f'{D}/figma/s12-kraft-macro.png',  [800, 1400, 1920], 78, {}),
    'kraft-open':  (f'{D}/figma/s12-kraft-open.png',   [800, 1400, 1920], 80, {}),
    'seed-1':      (f'{D}/drive/seed-1-layout.png',    [700, 1200, 1800], 82, {'crop_top': 44, 'invert': True}),
    'seed-2':      (f'{D}/drive/seed-2-refined.png',   [700, 1200, 1800], 82, {'crop_top': 44, 'invert': True}),
    'seed-3':      (f'{D}/drive/seed-3-cut.png',       [700, 1200, 1800], 82, {'crop_top': 44, 'invert': True}),
    'exploded':    (f'{D}/drive/st07-exploded.png',    [700, 1100, 1500], 80, {}),
    'pack-closed': (f'{D}/drive/st07-pack-closed.png', [700, 1100, 1500], 80, {}),
    'pack-open':   (f'{D}/drive/st07-pack-open.png',   [700, 1200, 1800], 80, {}),
    'cut-detail':  (f'{D}/drive/cut-detail.png',       [700, 1400], 80, {}),
    'box-original':  (f'{D}/figma/k04-box-original.jpg',  [700, 1200, 1900], 80, {}),
    'box-landscape': (f'{D}/figma/k05-box-landscape.jpg', [700, 1200, 1900], 78, {}),
    'box-wood':      (f'{D}/figma/k06-box-wood.jpg',      [700, 1200, 1900], 80, {}),
}
for i, n in enumerate(['original-hero', 'original-top', 'original-profile', 'portrait-hero', 'portrait-top',
                       'landscape-hero', 'landscape-top', 'wood-hero', 'wood-top', 'original-close',
                       'wood-close', 'landscape-close'], 1):
    SRC[f'r-{n}'] = (f'{D}/figma/r{i:02d}-{n}.jpg', [700, 1200, 2000], 78, {})
for i, n in enumerate(['portrait-hero', 'original-hero', 'landscape-hero', 'wood-hero', 'original-top',
                       'landscape-top', 'not-normal', 'pattern'], 1):
    SRC[f'poster-{i}'] = (f'{D}/figma/p{i:02d}-{n}.jpg', [500, 900, 1400], 80, {})

meta = {}
only = set(sys.argv[1:])
for name, (src, widths, q, ops) in SRC.items():
    if only and name not in only:
        continue
    im = Image.open(src)
    im = im.convert('RGB')
    if ops.get('crop_top'):
        im = im.crop((0, ops['crop_top'], im.width, im.height))
    if ops.get('invert'):
        im = ImageOps.invert(im)
    W, H = im.size
    ws = sorted({min(w, W) for w in widths})
    for w in ws:
        h = round(H * w / W)
        out = im if w == W else im.resize((w, h), Image.LANCZOS)
        out.save(f'{OUT}/{name}-{w}.webp', 'WEBP', quality=q, method=6)
    meta[name] = {'w': W, 'h': H, 'ws': ws}
    print(name, W, H, ws, sum(os.path.getsize(f'{OUT}/{name}-{w}.webp') for w in ws) // 1024, 'KB')

path = os.path.join(B, 'images.json')
old = json.load(open(path)) if os.path.exists(path) else {}
old.update(meta)
json.dump(old, open(path, 'w'), indent=1)
