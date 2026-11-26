"""SVG parser, cleaner, and high-resolution rasterizer for Nordic rock-art vectors."""

from __future__ import annotations

import io
import math
import re
from pathlib import Path
from typing import Dict, Optional, Tuple
from xml.etree import ElementTree as ET

import cv2
import numpy as np
from PIL import Image


def _strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _parse_length(val: Optional[str]) -> Optional[float]:
    if not val:
        return None
    m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(px|mm|cm|in|pt|pc|%)?\s*$", str(val))
    if not m:
        return None
    num = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    if unit == "%":
        return None
    scale = {"px": 1.0, "pt": 1.3333, "pc": 16.0, "mm": 3.7795, "cm": 37.795, "in": 96.0}.get(unit, 1.0)
    return num * scale


def get_svg_dimensions(svg_bytes: bytes) -> Tuple[float, float]:
    """Extract (width, height) from an SVG's viewBox or width/height attributes."""
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return 512.0, 512.0

    vb = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if vb:
        parts = [p for p in re.split(r"[\s,]+", vb.strip()) if p]
        if len(parts) == 4:
            try:
                w, h = float(parts[2]), float(parts[3])
                if w > 0 and h > 0:
                    return w, h
            except ValueError:
                pass

    w = _parse_length(root.attrib.get("width"))
    h = _parse_length(root.attrib.get("height"))
    if w and h and w > 0 and h > 0:
        return w, h
    return 512.0, 512.0


def clean_svg_for_tracing(svg_bytes: bytes) -> Tuple[bytes, Dict[str, int]]:
    """Strip non-motif SVG annotations: text labels, metadata, full-canvas background rects.

    Returns cleaned SVG bytes and telemetry counts.
    """
    stats = {"removed_text": 0, "removed_bg_rects": 0, "total_shapes": 0}
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return svg_bytes, stats

    w_doc, h_doc = get_svg_dimensions(svg_bytes)
    doc_area = max(w_doc * h_doc, 1.0)

    # Ensure viewBox exists so CairoSVG scales cleanly
    if "viewBox" not in root.attrib and "viewbox" not in root.attrib:
        root.attrib["viewBox"] = f"0 0 {w_doc:.3f} {h_doc:.3f}"

    # Remove text, metadata, desc, title, script, style annotations (keep defs for clip/use)
    drop_tags = {"text", "tspan", "textPath", "title", "desc", "metadata", "script", "foreignObject"}
    shape_tags = {"path", "polygon", "polyline", "circle", "ellipse", "rect", "line"}

    def _prune(elem: ET.Element) -> None:
        to_remove = []
        for child in list(elem):
            tag = _strip_ns(child.tag)
            if tag in drop_tags:
                to_remove.append(child)
                if tag in ("text", "tspan", "textPath"):
                    stats["removed_text"] += 1
                continue
            if tag == "rect":
                rw = _parse_length(child.attrib.get("width")) or 0.0
                rh = _parse_length(child.attrib.get("height")) or 0.0
                style_fill = (child.attrib.get("fill") or "") + " " + (child.attrib.get("style") or "")
                is_pct_full = str(child.attrib.get("width", "")).strip() == "100%" and str(
                    child.attrib.get("height", "")
                ).strip() == "100%"
                if is_pct_full or (rw * rh >= 0.72 * doc_area):
                    # Likely a full-canvas background box or page frame rect
                    has_stroke = ("stroke:" in style_fill.lower() and "stroke:none" not in style_fill.lower()) or (
                        child.attrib.get("stroke") not in (None, "", "none")
                    )
                    if is_pct_full or not has_stroke or (rw >= 0.90 * w_doc and rh >= 0.90 * h_doc):
                        to_remove.append(child)
                        stats["removed_bg_rects"] += 1
                        continue
            if tag in shape_tags:
                stats["total_shapes"] += 1
            _prune(child)
        for ch in to_remove:
            elem.remove(ch)

    _prune(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), stats


def _rasterize_via_cairosvg(svg_bytes: bytes, max_side: int = 1600) -> Optional[np.ndarray]:
    """Rasterize SVG bytes to RGBA uint8 numpy array using CairoSVG."""
    try:
        import cairosvg
    except Exception:
        return None

    w, h = get_svg_dimensions(svg_bytes)
    scale = max_side / max(w, h, 1.0)
    out_w = max(64, min(max_side, int(round(w * scale))))
    out_h = max(64, min(max_side, int(round(h * scale))))

    try:
        png_bytes = cairosvg.svg2png(
            bytestring=svg_bytes,
            output_width=out_w,
            output_height=out_h,
        )
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        return np.array(img, dtype=np.uint8)
    except Exception:
        return None


def _rasterize_via_svgpathtools(svg_bytes: bytes, max_side: int = 1400) -> Optional[np.ndarray]:
    """Pure-Python fallback SVG rasterizer using svgpathtools + OpenCV."""
    try:
        import svgpathtools
    except Exception:
        return None

    try:
        paths, attributes, svg_attr = svgpathtools.svg2paths2(io.BytesIO(svg_bytes))
    except Exception:
        return None

    if not paths:
        return None

    w, h = get_svg_dimensions(svg_bytes)
    scale = max_side / max(w, h, 1.0)
    out_w = max(64, min(max_side, int(round(w * scale))))
    out_h = max(64, min(max_side, int(round(h * scale))))

    # Determine bounding box fallback if viewBox wasn't standard
    vb = svg_attr.get("viewBox") or svg_attr.get("viewbox")
    ox, oy = 0.0, 0.0
    if vb:
        parts = [float(p) for p in re.split(r"[\s,]+", vb.strip()) if p]
        if len(parts) == 4:
            ox, oy, w, h = parts
            scale = max_side / max(w, h, 1.0)
            out_w = max(64, min(max_side, int(round(w * scale))))
            out_h = max(64, min(max_side, int(round(h * scale))))

    canvas = np.zeros((out_h, out_w), dtype=np.uint8)
    for path, attr in zip(paths, attributes):
        style = (attr.get("style") or "").lower()
        fill = (attr.get("fill") or "").lower()
        stroke = (attr.get("stroke") or "").lower()
        is_no_fill = (fill == "none") or ("fill:none" in style.replace(" ", ""))
        stroke_w = _parse_length(attr.get("stroke-width")) or 1.5
        thickness = max(2, int(round(stroke_w * scale)))

        for sub in path.continuous_subpaths():
            length = sub.length()
            if length <= 1e-6:
                continue
            n_pts = max(8, min(1024, int(math.ceil(length * scale / 2.0))))
            ts = np.linspace(0.0, 1.0, n_pts)
            pts = []
            for t in ts:
                c = sub.point(float(t))
                pts.append([int(round((c.real - ox) * scale)), int(round((c.imag - oy) * scale))])
            arr = np.array(pts, dtype=np.int32).reshape((-1, 1, 2))
            if not is_no_fill and sub.isclosed():
                cv2.fillPoly(canvas, [arr], 255)
            else:
                cv2.polylines(canvas, [arr], sub.isclosed(), 255, thickness=thickness, lineType=cv2.LINE_AA)
                if stroke and stroke != "none" and not is_no_fill:
                    cv2.fillPoly(canvas, [arr], 255)

    rgba = np.zeros((out_h, out_w, 4), dtype=np.uint8)
    rgba[..., 3] = canvas
    return rgba


def svg_to_foreground_mask(
    svg_path: Path,
    max_side: int = 1600,
) -> Tuple[Optional[np.ndarray], Dict[str, Any]]:
    """Load an SVG rock-art file and return a clean binary uint8 mask (255=foreground, 0=bg)."""
    info: Dict[str, Any] = {"extractor": "svg_vector", "ok": False}
    try:
        raw_bytes = svg_path.read_bytes()
    except Exception as e:
        info["reject_reason"] = f"svg_read_error:{e}"
        return None, info

    if not raw_bytes.strip():
        info["reject_reason"] = "empty_svg"
        return None, info

    cleaned_bytes, clean_stats = clean_svg_for_tracing(raw_bytes)
    info.update(clean_stats)

    rgba = _rasterize_via_cairosvg(cleaned_bytes, max_side=max_side)
    used_backend = "cairosvg"
    if rgba is None:
        rgba = _rasterize_via_svgpathtools(cleaned_bytes, max_side=max_side)
        used_backend = "svgpathtools"
    if rgba is None:
        info["reject_reason"] = "svg_rasterize_failed"
        return None, info

    info["svg_backend"] = used_backend
    info["raster_shape"] = [int(rgba.shape[0]), int(rgba.shape[1])]

    alpha = rgba[..., 3]
    rgb = rgba[..., :3]
    transparent_frac = float(np.mean(alpha < 20))

    if transparent_frac >= 0.12:
        # Standard transparent-background SVG: foreground is opaque non-background pixels
        fg = alpha >= 32
        # Guard against a leftover opaque white background inside an SVG layer
        if float(np.mean(fg)) > 0.78:
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
            bg_val = float(np.median(border))
            if bg_val > 180:
                fg = fg & (gray < bg_val - 25)
            elif bg_val < 75:
                fg = fg & (gray > bg_val + 25)
    else:
        # Fully opaque SVG canvas: separate foreground from border median background color
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        border = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
        bg_val = float(np.median(border))
        if bg_val > 140:
            fg = gray < (bg_val - 28)
        else:
            fg = gray > (bg_val + 28)

    mask = (fg.astype(np.uint8)) * 255
    fg_frac = float(np.mean(fg))
    info["fg_fraction"] = round(fg_frac, 5)

    if fg_frac < 0.0004:
        info["reject_reason"] = "svg_empty_foreground"
        return None, info
    if fg_frac > 0.85:
        info["reject_reason"] = "svg_inverted_or_solid_canvas"
        return None, info

    info["ok"] = True
    return mask, info
