"""App icons, favicon and iOS launch images from piece 093 of the cut field."""
import json, re
from PIL import Image, ImageDraw, ImageFont

import os
B = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(B, '..', 'public')
D = os.environ.get('SIP_SRC', os.path.join(B, '..', '..', '..', 'runs', 'sound-in-pieces'))  # private sources: renders, cut field, GLB
BG, FG, INK = (0, 0, 0), (204, 244, 75), (237, 240, 242)  # black, lime #ccf44b, ink #edf0f2
P = {p['n']: p for p in json.load(open(f'{SITE}/data/pieces.json'))}
PIECE = '093'


def points(d):
    nums = [float(v) for v in re.findall(r'-?\d+\.?\d*', d)]
    x, y = nums[0], nums[1]
    pts = [(x, y)]
    for dx, dy in zip(nums[2::2], nums[3::2]):
        x += dx; y += dy
        pts.append((x, y))
    return pts


PTS = points(P[PIECE]['d'])
x0, y0 = min(p[0] for p in PTS), min(p[1] for p in PTS)
x1, y1 = max(p[0] for p in PTS), max(p[1] for p in PTS)
cx, cy, span = (x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0)


def draw_piece(img, box_frac, center=None, color=FG, ss=4):
    W, H = img.size
    big = Image.new('L', (W * ss, H * ss), 0)
    s = box_frac * min(W, H) * ss / span
    ox, oy = center or (W / 2, H / 2)
    poly = [((px - cx) * s + ox * ss, (py - cy) * s + oy * ss) for px, py in PTS]
    ImageDraw.Draw(big).polygon(poly, fill=255)
    mask = big.resize((W, H), Image.LANCZOS)
    img.paste(Image.new('RGB', (W, H), color), (0, 0), mask)
    return img


def icon(size, frac):
    return draw_piece(Image.new('RGB', (size, size), BG), frac)


icon(180, 0.62).save(f'{SITE}/icons/apple-touch-icon.png')
icon(192, 0.62).save(f'{SITE}/icons/icon-192.png')
icon(512, 0.62).save(f'{SITE}/icons/icon-512.png')
icon(512, 0.50).save(f'{SITE}/icons/icon-maskable-512.png')  # inside the 80% safe circle

# favicon.svg: same piece, viewBox in mm around the piece
pad = span * 0.22
vb = f'{cx - span / 2 - pad:.1f} {cy - span / 2 - pad:.1f} {span + 2 * pad:.1f} {span + 2 * pad:.1f}'
open(f'{SITE}/icons/favicon.svg', 'w').write(
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}"><rect x="{cx - span / 2 - pad:.1f}" y="{cy - span / 2 - pad:.1f}" '
    f'width="{span + 2 * pad:.1f}" height="{span + 2 * pad:.1f}" rx="{span * 0.18:.1f}"/><path fill="#ccf44b" d="{P[PIECE]["d"]}"/></svg>')

# iOS launch images (portrait): (css w, css h, dpr)
DEVICES = [(440, 956, 3), (402, 874, 3), (430, 932, 3), (393, 852, 3), (390, 844, 3), (428, 926, 3),
           (375, 812, 3), (414, 896, 3), (414, 896, 2), (375, 667, 2), (1024, 1366, 2), (834, 1194, 2),
           (820, 1180, 2), (810, 1080, 2), (744, 1133, 2)]
try:
    font_path = os.path.join(B, 'node_modules', '@fontsource', 'ibm-plex-mono', 'files', 'ibm-plex-mono-latin-400-normal.woff')
    ImageFont.truetype(font_path, 20)
except Exception as e:
    font_path = None
    print('no mono font for splash:', e)
links = []
for w, h, r in DEVICES:
    W, H = w * r, h * r
    img = Image.new('RGB', (W, H), BG)
    draw_piece(img, 0.0, None)  # no-op keeps API symmetric
    s = 64 * r / span  # piece ~64 css px wide
    draw_piece(img, 64 * r / min(W, H), (W / 2, H / 2 - 10 * r), FG)
    if font_path:
        f = ImageFont.truetype(font_path, 11 * r)
        t = 'SOUND IN PIECES'
        tw = ImageDraw.Draw(img).textlength(t, font=f)
        ImageDraw.Draw(img).text(((W - tw) / 2, H / 2 + 44 * r), t, font=f, fill=INK)
    name = f'launch-{W}x{H}.png'
    img.save(f'{SITE}/splash/{name}', optimize=True)
    links.append(f'<link rel="apple-touch-startup-image" href="splash/{name}" media="(device-width: {w}px) and '
                 f'(device-height: {h}px) and (-webkit-device-pixel-ratio: {r}) and (orientation: portrait)">')
open(os.path.join(B, 'splash-links.html'), 'w').write('\n'.join(links))
print('icons + %d launch images' % len(links))
