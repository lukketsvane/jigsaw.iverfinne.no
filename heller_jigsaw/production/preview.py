"""High-resolution product previews rendered from ``partition.json``.

Images are drawn at true physical scale (``dpi``) with 2x supersampling. Outputs:

* ``assembly-wood.png``    assembled puzzle as laser-cut birch ply, unlabelled
* ``assembly-colour.png``  assembled puzzle with one colour per piece
* ``exploded.png``         pieces pushed apart radially, showing every outline
* ``pieces/NN.png``        each piece alone, plus a ghost of its aligned source silhouette
* ``pieces-grid.png``      all pieces in a labelled grid with their fidelity scores

These are visual aids for review; they are not evidence of physical fit.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from shapely.affinity import translate
from shapely.geometry import Polygon

PALETTE = ["#c8553d", "#588b8b", "#f2a65a", "#6a4c93", "#8ab17d", "#e9c46a", "#3d5a80", "#b5838d",
           "#2a9d8f", "#d08c60", "#7d8597", "#bc4749", "#577590", "#a3b18a", "#9c6644", "#f4a261"]
BG = (246, 243, 236)
BURN = (74, 46, 26)


def _font(size):
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _wood(w, h, seed=7):
    """Procedural birch-ply texture (pale, fine straight grain)."""
    rng = np.random.default_rng(seed)
    y = np.arange(h, dtype=np.float32)[:, None]
    x = np.arange(w, dtype=np.float32)[None, :]
    low = rng.standard_normal((max(2, h // 64 + 2), max(2, w // 64 + 2))).astype(np.float32)
    warp = np.asarray(Image.fromarray(low).resize((w, h), Image.Resampling.BICUBIC), np.float32)
    grain = np.sin(y * 0.55 + warp * 1.4 + 0.002 * x) * 0.5 + 0.5
    streak = np.asarray(Image.fromarray(rng.random((h, max(2, w // 40)), dtype=np.float32)).resize((w, h), Image.Resampling.BILINEAR), np.float32)
    fine = rng.random((h, w), dtype=np.float32)
    v = 0.88 + 0.025 * grain + 0.04 * streak + 0.02 * fine
    base = np.array([232, 204, 166], np.float32)
    return Image.fromarray(np.clip(base[None, None, :] * v[..., None], 0, 255).astype(np.uint8))


class Canvas:
    def __init__(self, bounds, dpi, margin_mm=12, ss=2):
        x0, y0, x1, y1 = bounds
        self.x0, self.y0, self.ss = x0 - margin_mm, y0 - margin_mm, ss
        self.scale = dpi / 25.4 * ss
        self.w = int(math.ceil((x1 - x0 + 2 * margin_mm) * self.scale))
        self.h = int(math.ceil((y1 - y0 + 2 * margin_mm) * self.scale))

    def pts(self, coords):
        return [((x - self.x0) * self.scale, (y - self.y0) * self.scale) for x, y in coords]

    def mask(self, polys):
        m = Image.new("L", (self.w, self.h), 0)
        d = ImageDraw.Draw(m)
        for p in polys:
            d.polygon(self.pts(p.exterior.coords), fill=255)
        return m

    def wood(self, seed=7):
        # Texture at output resolution, upsampled to the supersampled canvas to bound memory.
        return _wood(self.w // self.ss, self.h // self.ss, seed).resize((self.w, self.h), Image.Resampling.BILINEAR)

    def finish(self, img):
        return img.resize((self.w // self.ss, self.h // self.ss), Image.Resampling.LANCZOS)


def _outline(draw, canvas, poly, width_mm, colour):
    w = max(1, int(round(width_mm * canvas.scale)))
    draw.line(canvas.pts(poly.exterior.coords), fill=colour, width=w, joint="curve")


def render_assembly(polys, out, dpi, wood=True):
    from shapely.ops import unary_union
    c = Canvas(unary_union(polys).bounds, dpi)
    img = Image.new("RGB", (c.w, c.h), BG)
    if wood:
        shadow = c.mask(polys).filter(ImageFilter.GaussianBlur(1.2 * c.scale))
        img.paste((190, 182, 168), (int(0.8 * c.scale), int(1.0 * c.scale)), shadow)
        img.paste(c.wood(), (0, 0), c.mask(polys))
    else:
        d = ImageDraw.Draw(img)
        for i, p in enumerate(polys):
            d.polygon(c.pts(p.exterior.coords), fill=_rgb(PALETTE[i % len(PALETTE)]))
    d = ImageDraw.Draw(img)
    for p in polys:
        _outline(d, c, p, 0.22 if wood else 0.3, BURN if wood else (28, 24, 20))
    c.finish(img).save(out, optimize=True)


def render_exploded(polys, out, dpi, factor=0.22):
    from shapely.ops import unary_union
    centre = unary_union(polys).centroid
    moved = []
    for p in polys:
        q = p.centroid
        moved.append(translate(p, (q.x - centre.x) * factor, (q.y - centre.y) * factor))
    render_assembly(moved, out, dpi, wood=True)


def render_piece(poly, target, out, dpi, title=None):
    c = Canvas(poly.union(target).bounds if target is not None else poly.bounds, dpi, margin_mm=6)
    img = Image.new("RGB", (c.w, c.h), BG)
    shadow = c.mask([poly]).filter(ImageFilter.GaussianBlur(1.0 * c.scale))
    img.paste((190, 182, 168), (int(0.6 * c.scale), int(0.8 * c.scale)), shadow)
    img.paste(c.wood(sum(map(ord, title or '')) % 1000), (0, 0), c.mask([poly]))
    d = ImageDraw.Draw(img)
    _outline(d, c, poly, 0.22, BURN)
    if target is not None:
        import shapely
        dash = c.pts(shapely.get_coordinates(shapely.segmentize(target.exterior, 1.0)))
        w = max(1, int(round(0.18 * c.scale)))
        for i in range(0, len(dash) - 1, 2):
            d.line(dash[i:i + 2], fill=(200, 40, 40), width=w)
    img = c.finish(img)
    if title:
        ImageDraw.Draw(img).text((8, 6), title, fill=(30, 28, 25), font=_font(max(12, img.width // 28)))
    img.save(out, optimize=True)
    return img


def render_grid(polys, labels, out, cell_px=360, cols=8):
    rows = math.ceil(len(polys) / cols)
    sheet = Image.new("RGB", (cols * cell_px, rows * (cell_px + 34)), BG)
    d = ImageDraw.Draw(sheet)
    span = max(max(p.bounds[2] - p.bounds[0], p.bounds[3] - p.bounds[1]) for p in polys) * 1.08
    font = _font(max(12, cell_px // 24))
    for i, (p, label) in enumerate(zip(polys, labels)):
        cx, cy = p.centroid.x, p.centroid.y
        s = cell_px / span
        ox, oy = (i % cols) * cell_px, (i // cols) * (cell_px + 34)
        pts = [(ox + cell_px / 2 + (x - cx) * s, oy + cell_px / 2 + (y - cy) * s) for x, y in p.exterior.coords]
        d.polygon(pts, fill=(36, 32, 28))
        d.text((ox + cell_px / 2, oy + cell_px + 14), label, fill=(60, 56, 50), font=font, anchor="mm")
    sheet.save(out, optimize=True)


def render_previews(run_dir, out_dir=None, dpi=300, piece_dpi=600, reference_catalog=None):
    run = Path(run_dir)
    dest = Path(out_dir) if out_dir else run / "previews"
    (dest / "pieces").mkdir(parents=True, exist_ok=True)
    d = json.loads((run / "partition.json").read_text())
    polys = [Polygon(f["exterior"]).buffer(0) for f in d["faces"]]
    render_assembly(polys, dest / "assembly-wood.png", dpi, wood=True)
    render_assembly(polys, dest / "assembly-colour.png", dpi, wood=False)
    render_exploded(polys, dest / "exploded.png", dpi)
    targets = [None] * len(polys)
    fid = None
    try:
        from .fidelity import aligned_targets, load_run, piece_fidelity
        _, _, motifs = load_run(run)
        if reference_catalog:
            from .fidelity import fidelity_audit
            fid = fidelity_audit(run, reference_catalog=reference_catalog, write=False)["per_piece"]
        targets = aligned_targets(polys, motifs)
        if fid is None:
            fid = [piece_fidelity(p, t) for p, t in zip(polys, targets)]
    except (FileNotFoundError, KeyError) as exc:  # previews still useful without the source catalog
        print("source silhouettes unavailable:", exc)
    labels = []
    for i, (p, f, t) in enumerate(zip(polys, d["faces"], targets)):
        cls = str(f["metadata"].get("motif_class", "")).replace("_", " ")
        score = f" | fidelity {fid[i]['fidelity']:.2f}" if fid else ""
        render_piece(p, t, dest / "pieces" / f"{i + 1:02d}.png", piece_dpi, title=f"#{i + 1} {cls}{score}")
        labels.append(f"#{i + 1} {cls}" + (f" {fid[i]['fidelity']:.2f}" if fid else ""))
    render_grid(polys, labels, dest / "pieces-grid.png")
    return dest
