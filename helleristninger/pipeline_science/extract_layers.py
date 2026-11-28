"""True-scale multi-layer rock-art extraction (Method A: Sauvola+gradient; Method B: Otsu+marching-squares)."""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from xml.etree import ElementTree as ET

import cv2
import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage
from scipy.spatial import cKDTree
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union
from skimage import filters, measure, morphology

Image.MAX_IMAGE_PIXELS = None
ET.register_namespace("", "http://www.w3.org/2000/svg")
_CAIROSVG_AVAILABLE: Optional[bool] = None

ALLOWED_MEDIA_TYPES = {"bw_tracing", "svg_vector", "rubbing_frottage", "rti_3d"}
EXCLUDED_MEDIA_TYPES = {"painted_photo", "photo_other"}


@dataclass
class TracedFigure:
    """One traced rock-art figure with separated scientific SVG layers and raw-pixel geometry."""

    panel_index: int
    crop_px: List[int]  # [x0, y0, w, h] in native raw image pixels
    raw_shape_hw: Tuple[int, int]  # (raw_h, raw_w)
    work_scale: float  # work_px / raw_px
    geometry: Any  # Shapely Polygon or MultiPolygon in local crop pixel coords [0..crop_w, 0..crop_h]
    mask_crop: np.ndarray  # uint8 {0, 255} in local crop pixel coords (at crop_work_shape)
    subpaths_px: List[Tuple[List[Tuple[float, float]], List[List[Tuple[float, float]]]]]  # [(exterior, [holes])]
    cup_marks_px: List[Tuple[float, float, float]]  # (cx, cy, r) in local crop raw px
    cracks_px: List[List[Tuple[float, float]]]  # polylines in local crop raw px
    uncertain_px: List[List[Tuple[float, float]]]  # polylines in local crop raw px
    metrics: Dict[str, Any] = field(default_factory=dict)
    method: str = "sauvola_local_gradient_contour"
    polarity: str = "dark_on_light"
    ink_mode: str = "standard_black_ink"


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def is_allowed_record(rec: Dict[str, Any]) -> Tuple[bool, str]:
    """Strictly exclude painted_photo, photo_other, and quarantined records."""
    if rec.get("quarantined") is True:
        return False, "quarantined_record"
    raw_path = str(rec.get("raw_path") or "").lower()
    if "quarantine" in raw_path:
        return False, "quarantined_path"
    mtype = str(rec.get("media_type") or "").strip().lower()
    if mtype in EXCLUDED_MEDIA_TYPES:
        return False, f"excluded_media_type:{mtype}"
    if mtype not in ALLOWED_MEDIA_TYPES:
        return False, f"unsupported_media_type:{mtype or 'empty'}"
    return True, "ok"


def _fast_gaussian_bg(img_f32: np.ndarray, sigma: float) -> np.ndarray:
    h, w = img_f32.shape[:2]
    ds = 8 if min(h, w) >= 256 else (4 if min(h, w) >= 128 else 1)
    if ds == 1:
        return cv2.GaussianBlur(img_f32, (0, 0), sigmaX=sigma, sigmaY=sigma)
    sw, sh = max(8, w // ds), max(8, h // ds)
    small = cv2.resize(img_f32, (sw, sh), interpolation=cv2.INTER_AREA)
    s_blur = cv2.GaussianBlur(small, (0, 0), sigmaX=max(1.0, sigma / ds), sigmaY=max(1.0, sigma / ds))
    return cv2.resize(s_blur, (w, h), interpolation=cv2.INTER_LINEAR)


def _border_samples(arr: np.ndarray, margin_frac: float = 0.04) -> np.ndarray:
    h, w = arr.shape[:2]
    my = max(2, int(round(h * margin_frac)))
    mx = max(2, int(round(w * margin_frac)))
    return np.concatenate([
        arr[:my, :].reshape(-1),
        arr[-my:, :].reshape(-1),
        arr[:, :mx].reshape(-1),
        arr[:, -mx:].reshape(-1),
    ])


def _is_color_photograph(rgb: np.ndarray) -> Tuple[bool, float]:
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat_frac = float(np.mean(hsv[..., 1] > 42))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_32F).var())
    return (sat_frac > 0.25 and lap_var > 60.0), sat_frac


def load_raw_rgba_with_shape(path: Path, max_side: int = 1800) -> Tuple[np.ndarray, bool, Tuple[int, int], float]:
    """Load raster image preserving EXIF orientation, returning (rgba, has_alpha, (raw_h, raw_w), work_scale)."""
    with Image.open(path) as img:
        orig_w, orig_h = img.size
        if getattr(img, "format", None) == "JPEG" and max(orig_w, orig_h) > max_side * 2:
            try:
                img.draft("RGB", (max_side * 2, max_side * 2))
            except Exception:
                pass
        img = ImageOps.exif_transpose(img)
        # Note: if draft() subsampled JPEG, keep original pre-transpose raw dims adjusted for orientation
        raw_w, raw_h = img.size
        # If draft reduced size, compute exact raw dimensions from orig_w/orig_h
        if max(orig_w, orig_h) > max_side * 2 and max(raw_w, raw_h) < max(orig_w, orig_h):
            if (orig_w >= orig_h) == (raw_w >= raw_h):
                raw_w, raw_h = orig_w, orig_h
            else:
                raw_w, raw_h = orig_h, orig_w
        has_alpha = img.mode in ("RGBA", "LA") or ("transparency" in img.info)
        cur_w, cur_h = img.size
        if max(cur_w, cur_h) > max_side:
            s = max_side / float(max(cur_w, cur_h))
            nw = max(1, int(round(cur_w * s)))
            nh = max(1, int(round(cur_h * s)))
            img = img.resize((nw, nh), Image.Resampling.BILINEAR)
            cur_w, cur_h = nw, nh
        work_scale = cur_w / float(max(raw_w, 1))
        rgba = np.array(img.convert("RGBA"), dtype=np.uint8)
        return rgba, has_alpha, (raw_h, raw_w), work_scale


def _parse_svg_dims(svg_bytes: bytes) -> Tuple[float, float, float, float]:
    """Return (ox, oy, w, h) from SVG viewBox or width/height."""
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return 0.0, 0.0, 512.0, 512.0
    vb = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if vb:
        parts = [p for p in re.split(r"[\s,]+", vb.strip()) if p]
        if len(parts) == 4:
            try:
                ox, oy, w, h = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
                if w > 0 and h > 0:
                    return ox, oy, w, h
            except ValueError:
                pass
    def _plen(val: Optional[str]) -> Optional[float]:
        if not val:
            return None
        m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(px|mm|cm|in|pt|pc|%)?\s*$", str(val))
        if not m or (m.group(2) or "").lower() == "%":
            return None
        scale = {"px": 1.0, "pt": 1.3333, "pc": 16.0, "mm": 3.7795, "cm": 37.795, "in": 96.0}.get((m.group(2) or "px").lower(), 1.0)
        return float(m.group(1)) * scale
    w = _plen(root.attrib.get("width")) or 512.0
    h = _plen(root.attrib.get("height")) or 512.0
    return 0.0, 0.0, max(w, 1.0), max(h, 1.0)


def _clean_svg_bytes(svg_bytes: bytes) -> bytes:
    """Remove text captions, metadata, and full-canvas background rects from SVG."""
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError:
        return svg_bytes
    _, _, w_doc, h_doc = _parse_svg_dims(svg_bytes)
    doc_area = max(w_doc * h_doc, 1.0)
    if "viewBox" not in root.attrib and "viewbox" not in root.attrib:
        root.attrib["viewBox"] = f"0 0 {w_doc:.3f} {h_doc:.3f}"
    drop_tags = {"text", "tspan", "textPath", "title", "desc", "metadata", "script", "foreignObject"}

    def _prune(elem: ET.Element) -> None:
        to_remove = []
        for child in list(elem):
            tag = child.tag.split("}", 1)[-1] if "}" in child.tag else child.tag
            if tag in drop_tags:
                to_remove.append(child)
                continue
            if tag == "rect":
                is_pct = str(child.attrib.get("width", "")).strip() == "100%" and str(child.attrib.get("height", "")).strip() == "100%"
                try:
                    rw = float(re.sub(r"[a-zA-Z%]+", "", str(child.attrib.get("width", "0"))))
                    rh = float(re.sub(r"[a-zA-Z%]+", "", str(child.attrib.get("height", "0"))))
                except ValueError:
                    rw, rh = 0.0, 0.0
                if is_pct or (rw * rh >= 0.72 * doc_area):
                    to_remove.append(child)
                    continue
            _prune(child)
        for ch in to_remove:
            elem.remove(ch)

    _prune(root)
    xml_out = ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8", errors="ignore")
    xml_out = re.sub(r"<(/?)ns\d+:", r"<\1", xml_out)
    xml_out = re.sub(r"\sxmlns:ns\d+=", " xmlns=", xml_out)
    return xml_out.encode("utf-8")


def _rasterize_svg_via_svgpathtools(svg_bytes: bytes, max_side: int = 1600) -> Optional[np.ndarray]:
    import svgpathtools
    try:
        paths, attributes, _ = svgpathtools.svg2paths2(io.BytesIO(svg_bytes))
    except Exception:
        return None
    if not paths:
        return None
    ox, oy, w, h = _parse_svg_dims(svg_bytes)
    scale = max_side / max(w, h, 1.0)
    out_w = max(64, min(max_side, int(round(w * scale))))
    out_h = max(64, min(max_side, int(round(h * scale))))
    canvas = np.zeros((out_h, out_w), dtype=np.uint8)
    for path, attr in zip(paths, attributes):
        style = (attr.get("style") or "").lower().replace(" ", "")
        fill = (attr.get("fill") or "").lower()
        stroke = (attr.get("stroke") or "").lower()
        is_no_fill = (fill == "none") or ("fill:none" in style)
        sw_str = re.sub(r"[^0-9.\-]", "", str(attr.get("stroke-width") or "1.5"))
        try:
            sw = float(sw_str) if sw_str else 1.5
        except ValueError:
            sw = 1.5
        thickness = max(2, int(round(sw * scale)))
        closed_arrays = []
        for sub in path.continuous_subpaths():
            length = sub.length()
            if length <= 1e-6:
                continue
            n_pts = max(8, min(1024, int(math.ceil(length * scale / 2.0))))
            pts = []
            for t in np.linspace(0.0, 1.0, n_pts):
                c = sub.point(float(t))
                pts.append([int(round((c.real - ox) * scale)), int(round((c.imag - oy) * scale))])
            arr = np.array(pts, dtype=np.int32).reshape((-1, 1, 2))
            if not is_no_fill and sub.isclosed():
                closed_arrays.append(arr)
            else:
                cv2.polylines(canvas, [arr], sub.isclosed(), 255, thickness=thickness, lineType=cv2.LINE_AA)
                if stroke and stroke != "none" and not is_no_fill:
                    closed_arrays.append(arr)
        if closed_arrays:
            # Fill all subpaths together so OpenCV uses even-odd rule and preserves compound holes
            cv2.fillPoly(canvas, closed_arrays, 255)
    rgba = np.zeros((out_h, out_w, 4), dtype=np.uint8)
    rgba[..., 3] = canvas
    return rgba


def _rasterize_svg_via_cairosvg(svg_bytes: bytes, max_side: int = 1600) -> Optional[np.ndarray]:
    global _CAIROSVG_AVAILABLE
    if _CAIROSVG_AVAILABLE is False:
        return None
    try:
        import cairosvg
        _CAIROSVG_AVAILABLE = True
    except Exception:
        _CAIROSVG_AVAILABLE = False
        return None
    _, _, w, h = _parse_svg_dims(svg_bytes)
    scale = max_side / max(w, h, 1.0)
    out_w = max(64, min(max_side, int(round(w * scale))))
    out_h = max(64, min(max_side, int(round(h * scale))))
    try:
        png = cairosvg.svg2png(bytestring=svg_bytes, output_width=out_w, output_height=out_h)
        return np.array(Image.open(io.BytesIO(png)).convert("RGBA"), dtype=np.uint8)
    except Exception:
        return None


def _extract_svg_mask(raw_path: Path, max_side: int = 1600, prefer_backend: str = "cairosvg") -> Tuple[Optional[np.ndarray], Dict[str, Any]]:
    raw_bytes = raw_path.read_bytes()
    if not raw_bytes.strip():
        return None, {"ok": False, "reject_reason": "empty_svg"}
    cleaned = _clean_svg_bytes(raw_bytes)
    rgba = None
    used = None
    order = ("cairosvg", "svgpathtools") if prefer_backend == "cairosvg" else ("svgpathtools", "cairosvg")
    for bk in order:
        rgba = _rasterize_svg_via_cairosvg(cleaned, max_side) if bk == "cairosvg" else _rasterize_svg_via_svgpathtools(cleaned, max_side)
        if rgba is not None:
            used = bk
            break
    if rgba is None:
        return None, {"ok": False, "reject_reason": "svg_rasterize_failed"}

    alpha = rgba[..., 3]
    rgb = rgba[..., :3]
    if float(np.mean(alpha < 20)) >= 0.12:
        fg = alpha >= 32
        if float(np.mean(fg)) > 0.78:
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            bg_val = float(np.median(_border_samples(gray)))
            fg = fg & (gray < bg_val - 25 if bg_val > 180 else gray > bg_val + 25)
    else:
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        bg_val = float(np.median(_border_samples(gray)))
        fg = (gray < bg_val - 28) if bg_val > 140 else (gray > bg_val + 28)

    mask = (fg.astype(np.uint8)) * 255
    fg_frac = float(np.mean(fg))
    if fg_frac < 0.0004:
        return None, {"ok": False, "reject_reason": "svg_empty_foreground"}
    if fg_frac > 0.85:
        return None, {"ok": False, "reject_reason": "svg_solid_canvas"}
    return mask, {"ok": True, "backend": used, "fg_fraction": round(fg_frac, 5), "shape": mask.shape}


def extract_mask_method_a_sauvola(
    raw_path: Path,
    media_type: str,
    max_side: int = 1800,
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray], Dict[str, Any]]:
    """Method A: Adaptive Sauvola + Scharr local-gradient contour mask extraction.

    Returns (fg_mask, weak_band_mask, colored_crack_mask, info).
    """
    mtype = media_type.strip().lower()
    info: Dict[str, Any] = {"method": "sauvola_local_gradient_contour", "media_type": mtype, "ok": False}

    if raw_path.suffix.lower() == ".svg" or mtype == "svg_vector":
        mask, sinfo = _extract_svg_mask(raw_path, max_side=min(max_side, 1600), prefer_backend="cairosvg")
        info.update(sinfo)
        if mask is None:
            return None, None, None, info
        info["raw_shape_hw"] = (mask.shape[0], mask.shape[1])
        info["work_scale"] = 1.0
        info["polarity"] = "vector"
        info["ink_mode"] = "svg_vector"
        return mask, np.zeros_like(mask), np.zeros_like(mask), info

    try:
        rgba, has_alpha, raw_hw, work_scale = load_raw_rgba_with_shape(raw_path, max_side=max_side)
    except Exception as e:
        info["reject_reason"] = f"image_load_error:{e}"
        return None, None, None, info

    info["raw_shape_hw"] = raw_hw
    info["work_scale"] = work_scale
    h, w = rgba.shape[:2]
    if min(h, w) < 40:
        info["reject_reason"] = "image_too_small"
        return None, None, None, info

    rgb = rgba[..., :3]
    alpha = rgba[..., 3]

    # Guard: strictly reject mislabeled full-color rock photographs tagged as bw_tracing
    if mtype == "bw_tracing":
        is_photo, sat_frac = _is_color_photograph(rgb)
        if is_photo and not has_alpha:
            border_g = _border_samples(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
            border_s = _border_samples(cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)[..., 1])
            b_mad = float(np.median(np.abs(border_g.astype(np.float32) - float(np.median(border_g)))))
            if b_mad > 6.0 or float(np.median(border_s)) > 40.0:
                info["reject_reason"] = f"mislabeled_color_photo:sat={sat_frac:.2f}"
                return None, None, None, info

    # Alpha PNG tracing
    if has_alpha and float(np.mean(alpha < 25)) >= 0.15:
        fg = alpha >= 64
        fg_frac = float(np.mean(fg))
        if 0.0005 <= fg_frac <= 0.72:
            weak = ((alpha >= 20) & (alpha < 64)).astype(np.uint8) * 255
            info.update({"ok": True, "polarity": "alpha", "ink_mode": "alpha_channel", "fg_fraction": round(fg_frac, 5)})
            return (fg.astype(np.uint8) * 255), weak, np.zeros((h, w), dtype=np.uint8), info

    if has_alpha:
        a = alpha.astype(np.float32)[..., None] / 255.0
        rgb = np.clip(rgb.astype(np.float32) * a + 255.0 * (1.0 - a), 0, 255).astype(np.uint8)

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat = hsv[..., 1]

    # Special handling for RTI/3D normal maps or depth/shaded relief
    if mtype == "rti_3d":
        sat_mean = float(np.mean(sat))
        win = max(21, min(101, int(round(min(h, w) / 14.0)) | 1))
        if sat_mean > 45.0:
            # RGB surface normal map: carved grooves have steep normal gradients
            gx = cv2.Scharr(rgb.astype(np.float32), cv2.CV_32F, 1, 0)
            gy = cv2.Scharr(rgb.astype(np.float32), cv2.CV_32F, 0, 1)
            mag = np.sqrt(np.sum(gx * gx + gy * gy, axis=2))
            p99 = max(float(np.percentile(mag, 99.5)), 1.0)
            score_u8 = np.clip(mag / p99 * 255.0, 0, 255).astype(np.uint8)
            bright_thr = 255.0 - filters.threshold_sauvola(255 - score_u8, window_size=win, k=0.20)
            fg = score_u8 >= np.maximum(bright_thr, 48.0)
            weak = ((score_u8 >= 24.0) & (~fg)).astype(np.uint8) * 255
            polarity = "rti_normal_gradient"
        else:
            # Grayscale 3D shaded relief: carved grooves are locally darker than surrounding rock
            sigma = max(40.0, min(h, w) / 6.0)
            bg_est = _fast_gaussian_bg(gray.astype(np.float32), sigma)
            ref_hi = float(np.percentile(gray, 85))
            diff_loc = bg_est - gray.astype(np.float32)
            diff_glob = (ref_hi - gray.astype(np.float32)) * 0.85
            score_f = np.clip(np.maximum(diff_loc, diff_glob), 0, 255)
            score_u8 = cv2.medianBlur(score_f.astype(np.uint8), 3)
            sauv_dark = filters.threshold_sauvola(gray, window_size=win, k=0.22)
            fg = ((gray <= sauv_dark) | (score_u8 >= 42.0)) & (score_u8 >= 24.0)
            weak = ((score_u8 >= 12.0) & (score_u8 < 24.0) & (~fg)).astype(np.uint8) * 255
            polarity = "dark_on_light"
        k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx((fg.astype(np.uint8)) * 255, cv2.MORPH_OPEN, k3)
        fg_frac = float(np.mean(mask > 0))
        info.update({"polarity": polarity, "ink_mode": "rti_3d", "fg_fraction": round(fg_frac, 5)})
        if fg_frac < 0.0008:
            info["reject_reason"] = "rti_empty_foreground"
            return None, None, None, info
        if fg_frac > 0.36:
            info["reject_reason"] = "rti_excessive_foreground"
            return None, None, None, info
        info["ok"] = True
        return mask, weak, np.zeros((h, w), dtype=np.uint8), info

    is_rubbing = (mtype == "rubbing_frottage")
    border_gray = _border_samples(gray)
    border_med = float(np.median(border_gray))
    border_mad = float(np.median(np.abs(border_gray.astype(np.float32) - border_med)))
    sigma = max(24.0, min(h, w) / 8.0)
    bg_est = _fast_gaussian_bg(gray.astype(np.float32), sigma=sigma)

    colored_crack_mask = np.zeros((h, w), dtype=np.uint8)
    ink_mode = "standard_black_ink"

    if border_med >= 130:
        polarity = "dark_on_light"
        diff = bg_est - gray.astype(np.float32)
        global_dark = border_med - gray.astype(np.float32)
        score_f = np.clip(np.maximum(diff, global_dark * 0.85), 0, 255)
        score_u8 = score_f.astype(np.uint8)

        # Scharr local gradient magnitude on inverted luminance
        gx = cv2.Scharr(255.0 - gray.astype(np.float32), cv2.CV_32F, 1, 0)
        gy = cv2.Scharr(255.0 - gray.astype(np.float32), cv2.CV_32F, 0, 1)
        grad_mag = np.sqrt(gx * gx + gy * gy)

        win = max(21, min(101, int(round(min(h, w) / 14.0)) | 1))
        sauv_dark_thr = filters.threshold_sauvola(gray, window_size=win, k=0.22)

        if border_mad < 12.0 and not is_rubbing:
            neutral_black = (gray < 95) & (sat < 60)
            colored_ink = (sat >= 75) & (gray < 215) & (border_med > 195)
            nb_frac = float(np.mean(neutral_black))
            ci_frac = float(np.mean(colored_ink))

            if ci_frac >= 0.002 and nb_frac < 0.0035:
                fg = colored_ink | ((gray <= sauv_dark_thr) & (score_f >= 32.0))
                ink_mode = "colored_ink_only"
            elif nb_frac >= 0.002 and ci_frac >= 0.0005:
                # Black carvings; extract colored crack/fissure annotation lines separately!
                fg = ((gray <= np.minimum(sauv_dark_thr, 165.0)) | (score_f >= 55.0)) & (score_f >= 30.0) & (sat < 75)
                colored_crack_mask = ((colored_ink & (score_f >= 20.0)).astype(np.uint8)) * 255
                ink_mode = "black_carvings_with_colored_cracks"
            else:
                fg = ((gray <= np.minimum(sauv_dark_thr, 195.0)) | (score_f >= 55.0)) & (score_f >= 24.0)
                # Anchor sharp local-gradient boundary pixels near fg
                fg_dil = cv2.dilate(fg.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
                fg = fg | (fg_dil & (grad_mag >= 180.0) & (score_f >= 18.0))
                ink_mode = "standard_black_ink"
            weak = ((score_f >= 12.0) & (score_f < 26.0) & (~fg) & (sat < 75)).astype(np.uint8) * 255
        else:
            blur_score = cv2.medianBlur(score_u8, 3)
            min_sc = 36.0 if is_rubbing else 28.0
            fg = ((gray <= sauv_dark_thr) | (blur_score >= 58.0)) & (blur_score >= min_sc)
            weak = ((blur_score >= min_sc * 0.50) & (blur_score < min_sc) & (~fg)).astype(np.uint8) * 255
    elif border_med < 110:
        polarity = "light_on_dark"
        diff = gray.astype(np.float32) - bg_est
        global_light = gray.astype(np.float32) - border_med
        score_f = np.clip(np.maximum(diff, global_light * 0.85), 0, 255)
        score_u8 = score_f.astype(np.uint8)
        win = max(21, min(101, int(round(min(h, w) / 14.0)) | 1))
        inv_thr = filters.threshold_sauvola(255 - gray, window_size=win, k=0.22)
        bright_thr = 255.0 - inv_thr
        min_sc = 40.0 if is_rubbing else 26.0
        fg = ((gray >= bright_thr) | (score_u8 >= 58.0)) & (score_u8 >= min_sc)
        weak = ((score_u8 >= min_sc * 0.5) & (score_u8 < min_sc) & (~fg)).astype(np.uint8) * 255
        ink_mode = "light_on_dark"
    else:
        diff_dark = np.clip(bg_est - gray.astype(np.float32), 0, 255).astype(np.uint8)
        diff_light = np.clip(gray.astype(np.float32) - bg_est, 0, 255).astype(np.uint8)
        if np.percentile(diff_light, 97) > np.percentile(diff_dark, 97) * 1.15:
            polarity = "light_on_midgray"
            score_u8 = cv2.medianBlur(diff_light, 3)
        else:
            polarity = "dark_on_midgray"
            score_u8 = cv2.medianBlur(diff_dark, 3)
        win = max(21, min(101, int(round(min(h, w) / 16.0)) | 1))
        sauv = filters.threshold_sauvola(255 - score_u8, window_size=win, k=0.20)
        fg = score_u8 >= np.maximum(255.0 - sauv, 38.0)
        weak = ((score_u8 >= 20.0) & (score_u8 < 38.0) & (~fg)).astype(np.uint8) * 255
        ink_mode = polarity

    mask = (fg.astype(np.uint8)) * 255
    if is_rubbing:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)

    fg_frac = float(np.mean(mask > 0))
    info.update({
        "polarity": polarity,
        "ink_mode": ink_mode,
        "border_med": round(border_med, 1),
        "border_mad": round(border_mad, 1),
        "fg_fraction": round(fg_frac, 5),
    })

    if fg_frac < 0.0008:
        info["reject_reason"] = "bw_empty_foreground"
        return None, None, None, info
    if fg_frac > (0.38 if is_rubbing else 0.45):
        info["reject_reason"] = "bw_excessive_foreground"
        return None, None, None, info

    num_lbl, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if num_lbl > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        total_area = float(np.sum(areas))
        tiny_area = float(np.sum(areas[areas < 35]))
        noise_ratio = tiny_area / max(total_area, 1.0)
        info["speckle_ratio"] = round(noise_ratio, 4)
        if is_rubbing:
            if border_mad > 25.0 or noise_ratio > 0.28 or (num_lbl > 1800 and noise_ratio > 0.15):
                info["reject_reason"] = "noisy_rubbing_rejected"
                return None, None, None, info
        else:
            if noise_ratio > 0.36 and border_mad > 12.0:
                info["reject_reason"] = "noisy_texture_not_tracing"
                return None, None, None, info
            if num_lbl > 2500 and noise_ratio > 0.28:
                info["reject_reason"] = "excessive_speckle_fragmentation"
                return None, None, None, info

    info["ok"] = True
    return mask, weak, colored_crack_mask, info


def _estimate_stroke_width(mask_u8: np.ndarray) -> float:
    bin_u8 = (mask_u8 > 0).astype(np.uint8)
    if not np.any(bin_u8):
        return 3.0
    dt = cv2.distanceTransform(bin_u8, cv2.DIST_L2, 3)
    vals = dt[bin_u8 > 0]
    if len(vals) == 0:
        return 3.0
    return max(1.5, min(28.0, 2.0 * float(np.percentile(vals, 75))))


def _remove_panel_chrome(labels: np.ndarray, stats: np.ndarray, h: int, w: int) -> set[int]:
    """Identify border frames, straight ruler scale bars, and printed caption text lines."""
    drop: set[int] = set()
    img_area = float(h * w)
    edge_pad = max(3, int(round(0.008 * min(h, w))))

    # 1. Border frames
    for cid in range(1, len(stats)):
        x, y, bw, bh, area = [int(v) for v in stats[cid]]
        bbox_area = max(bw * bh, 1)
        fill = area / float(bbox_area)
        touches_l = x <= edge_pad
        touches_r = (x + bw) >= (w - edge_pad)
        touches_t = y <= edge_pad
        touches_b = (y + bh) >= (h - edge_pad)
        n_edges = int(touches_l) + int(touches_r) + int(touches_t) + int(touches_b)
        if bw >= 0.72 * w and bh >= 0.72 * h and fill < 0.14:
            drop.add(cid)
        elif n_edges >= 2 and (bw >= 0.80 * w or bh >= 0.80 * h) and fill < 0.25:
            drop.add(cid)
        elif n_edges >= 1 and ((touches_t or touches_b) and bw >= 0.85 * w and bh <= 0.08 * h):
            drop.add(cid)
        elif n_edges >= 1 and ((touches_l or touches_r) and bh >= 0.85 * h and bw <= 0.08 * w):
            drop.add(cid)
        elif bbox_area >= 0.55 * img_area and fill < 0.06 and bw >= 0.65 * w and bh >= 0.65 * h:
            drop.add(cid)

    # 2. Scale bars in margins
    margin_top, margin_bot = int(0.20 * h), int(0.80 * h)
    for cid in range(1, len(stats)):
        if cid in drop:
            continue
        x, y, bw, bh, area = [int(v) for v in stats[cid]]
        if area < 40:
            continue
        rect_ratio = area / float(max(bw * bh, 1))
        ar = max(bw / max(bh, 1), bh / max(bw, 1))
        cy = y + 0.5 * bh
        in_margin = (cy <= margin_top) or (cy >= margin_bot)
        if ar >= 7.5 and rect_ratio >= 0.68 and (in_margin or ar >= 14.0):
            drop.add(cid)
        elif in_margin and 0.08 * w <= bw <= 0.65 * w and bh <= 0.045 * h and (bw / max(bh, 1)) >= 6.0:
            sub = (labels[y : y + bh, x : x + bw] == cid).astype(np.uint8)
            cnts, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            if cnts:
                hull_area = cv2.contourArea(cv2.convexHull(cnts[0]))
                if (area / max(hull_area, 1.0)) >= 0.82 and rect_ratio >= 0.42:
                    drop.add(cid)

    # 3. Printed text lines (protecting Bronze Age ship crew strokes)
    min_dim = min(h, w)
    max_gh = max(36, int(round(0.048 * h)))
    min_gh = max(5, int(round(0.005 * h)))
    glyphs: List[int] = []
    large_boxes: List[Tuple[int, int, int, int]] = []
    for cid in range(1, len(stats)):
        if cid in drop:
            continue
        x, y, bw, bh, area = [int(v) for v in stats[cid]]
        if max(bw, bh) >= max(55, int(0.065 * min_dim)) or area >= max(500, int(0.0015 * img_area)):
            large_boxes.append((x, y, bw, bh))
        if min_gh <= bh <= max_gh and 2 <= bw <= max(48, int(0.055 * w)):
            ar = bw / max(bh, 1.0)
            if 0.12 <= ar <= 2.4 and area >= 8:
                glyphs.append(cid)

    if len(glyphs) >= 3:
        glyphs.sort(key=lambda c: int(stats[c, cv2.CC_STAT_LEFT]))
        uf = _UnionFind(len(glyphs))
        for i in range(len(glyphs)):
            ci = glyphs[i]
            xi, yi, wi, hi = [int(stats[ci, k]) for k in range(4)]
            cyi = yi + 0.5 * hi
            for j in range(i + 1, len(glyphs)):
                cj = glyphs[j]
                xj, yj, wj, hj = [int(stats[cj, k]) for k in range(4)]
                mh = max(hi, hj)
                if xj - (xi + wi) > 2.2 * mh:
                    break
                cyj = yj + 0.5 * hj
                if abs(cyi - cyj) <= 0.48 * mh and 0.42 <= (hi / max(hj, 1.0)) <= 2.4:
                    uf.union(i, j)
        groups: Dict[int, List[int]] = {}
        for idx, cid in enumerate(glyphs):
            groups.setdefault(uf.find(idx), []).append(cid)
        for members in groups.values():
            if len(members) < 3:
                continue
            gx0 = min(int(stats[c, 0]) for c in members)
            gy0 = min(int(stats[c, 1]) for c in members)
            gx1 = max(int(stats[c, 0] + stats[c, 2]) for c in members)
            gy1 = max(int(stats[c, 1] + stats[c, 3]) for c in members)
            med_h = float(np.median([int(stats[c, 3]) for c in members]))
            gw = gx1 - gx0
            gcy = 0.5 * (gy0 + gy1)
            in_margin = (gcy <= 0.15 * h) or (gcy >= 0.84 * h)
            thin_bar = float(np.mean([(int(stats[c, 2]) / max(int(stats[c, 3]), 1.0)) < 0.34 for c in members]))
            pad_c = 2.0 * med_h
            near_large = any(
                not (gx1 + pad_c < lx or gx0 - pad_c > lx + lw or gy1 + pad_c < ly or gy0 - pad_c > ly + lh)
                for lx, ly, lw, lh in large_boxes
            )
            if near_large and thin_bar >= 0.55:
                continue
            if (in_margin and len(members) >= 3 and gw >= 2.2 * med_h) or (
                not near_large and len(members) >= 5 and gw >= 4.2 * med_h and thin_bar < 0.65
            ):
                drop.update(members)

    return drop


def _mask_to_polylines(mask_u8: np.ndarray, min_len: int = 8, max_lines: int = 40) -> List[List[Tuple[float, float]]]:
    """Fast O(N) skeleton/thin-line polyline extractor using OpenCV connected components + contours."""
    if not np.any(mask_u8):
        return []
    h, w = mask_u8.shape[:2]
    # Downsample huge crops for auxiliary crack/uncertain line tracing to stay sub-millisecond
    ds = 1.0
    work = (mask_u8 > 0).astype(np.uint8)
    if max(h, w) > 640:
        ds = 640.0 / float(max(h, w))
        nw, nh = max(8, int(round(w * ds))), max(8, int(round(h * ds)))
        work = (cv2.resize(work * 255, (nw, nh), interpolation=cv2.INTER_AREA) > 64).astype(np.uint8)
    inv_ds = 1.0 / ds

    skel = morphology.skeletonize(work > 0).astype(np.uint8) * 255
    n_lbl, lbls, stats, _ = cv2.connectedComponentsWithStats(skel, connectivity=8)
    if n_lbl <= 1:
        return []

    # Keep top max_lines longest skeleton components
    comp_ids = [
        cid for cid in range(1, n_lbl)
        if int(stats[cid, cv2.CC_STAT_AREA]) >= max(4, int(round(min_len * ds)))
    ]
    comp_ids.sort(key=lambda c: int(stats[c, cv2.CC_STAT_AREA]), reverse=True)
    comp_ids = comp_ids[:max_lines]

    lines: List[List[Tuple[float, float]]] = []
    for cid in comp_ids:
        x, y, bw, bh, _ = [int(v) for v in stats[cid]]
        sub = ((lbls[y : y + bh, x : x + bw] == cid).astype(np.uint8)) * 255
        cnts, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cnts:
            continue
        cnt = max(cnts, key=len).reshape(-1, 2)
        if len(cnt) < 4:
            continue
        # On a 1px skeleton, external contour walks A -> B -> A; take half-loop A -> B
        half_n = max(2, len(cnt) // 2 + 1)
        half = cnt[:half_n]
        approx = cv2.approxPolyDP(half.astype(np.float32), 1.5, closed=False).reshape(-1, 2)
        if len(approx) < 2:
            continue
        pts = [(round((float(px) + x) * inv_ds, 2), round((float(py) + y) * inv_ds, 2)) for px, py in approx]
        lines.append(pts)
    return lines


def _extract_contours_as_subpaths(
    crop_mask: np.ndarray,
    inv_scale: float,
    simplify_px: float = 0.75,
) -> Tuple[List[Tuple[List[Tuple[float, float]], List[List[Tuple[float, float]]]]], Any]:
    """Convert binary mask crop into exact closed SVG subpaths + Shapely Polygon/MultiPolygon in raw pixel units."""
    # Close tiny 1px pecking pinholes without distorting silhouette
    work = cv2.morphologyEx((crop_mask > 0).astype(np.uint8) * 255, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, hierarchy = cv2.findContours(work, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if not contours or hierarchy is None:
        return [], None

    hier = hierarchy[0]
    subpaths: List[Tuple[List[Tuple[float, float]], List[List[Tuple[float, float]]]]] = []
    polys: List[Polygon] = []

    for idx, cnt in enumerate(contours):
        # Outer rings have parent == -1
        if hier[idx][3] != -1:
            continue
        if len(cnt) < 4 or cv2.contourArea(cnt) < 12.0:
            continue
        eps = simplify_px
        approx = cv2.approxPolyDP(cnt, eps, closed=True).reshape(-1, 2)
        if len(approx) < 3:
            continue
        ext_pts = [(round(float(x) * inv_scale, 3), round(float(y) * inv_scale, 3)) for x, y in approx]
        if ext_pts[0] != ext_pts[-1]:
            ext_pts.append(ext_pts[0])

        # Collect direct child holes
        holes_pts: List[List[Tuple[float, float]]] = []
        child_idx = hier[idx][2]
        while child_idx != -1:
            h_cnt = contours[child_idx]
            if len(h_cnt) >= 4 and cv2.contourArea(h_cnt) >= 20.0:
                h_approx = cv2.approxPolyDP(h_cnt, eps, closed=True).reshape(-1, 2)
                if len(h_approx) >= 3:
                    hp = [(round(float(x) * inv_scale, 3), round(float(y) * inv_scale, 3)) for x, y in h_approx]
                    if hp[0] != hp[-1]:
                        hp.append(hp[0])
                    holes_pts.append(hp)
            child_idx = hier[child_idx][0]

        try:
            poly = Polygon(ext_pts, holes_pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.area <= 1.0:
                continue
            if poly.geom_type == "MultiPolygon":
                for g in poly.geoms:
                    if g.area > 1.0:
                        polys.append(g)
            elif poly.geom_type == "Polygon":
                polys.append(poly)
            subpaths.append((ext_pts, holes_pts))
        except Exception:
            continue

    if not polys:
        return [], None
    geom = polys[0] if len(polys) == 1 else unary_union(polys)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return subpaths, geom


def trace_panel_scientific(
    raw_path: Path,
    media_type: str,
    max_motifs_per_panel: int = 24,
    work_max_side: int = 1800,
) -> Tuple[List[TracedFigure], Dict[str, Any]]:
    """Run Method A (Sauvola + local gradient) across one verified carved document."""
    fg_mask, weak_mask, colored_crack_mask, ext_info = extract_mask_method_a_sauvola(
        raw_path, media_type=media_type, max_side=work_max_side
    )
    if fg_mask is None:
        return [], {"status": "rejected_extract", "reason": ext_info.get("reject_reason", "extract_failed"), "detail": ext_info}

    h, w = fg_mask.shape[:2]
    raw_h, raw_w = ext_info["raw_shape_hw"]
    work_scale = float(ext_info["work_scale"])
    inv_scale = 1.0 / max(work_scale, 1e-6)
    min_dim = min(h, w)
    img_area = float(h * w)

    bin_mask = ((fg_mask > 0).astype(np.uint8)) * 255
    num_lbl, labels, stats, _ = cv2.connectedComponentsWithStats(bin_mask, connectivity=8)
    if num_lbl <= 1:
        return [], {"status": "rejected_empty", "reason": "no_components"}

    drop_chrome = _remove_panel_chrome(labels, stats, h, w)
    if drop_chrome:
        keep_lut = np.ones(num_lbl, dtype=np.uint8)
        keep_lut[0] = 0
        for cid in drop_chrome:
            keep_lut[cid] = 0
        clean_mask = (keep_lut[labels] * 255).astype(np.uint8)
    else:
        clean_mask = bin_mask

    num_lbl, labels, stats, centroids = cv2.connectedComponentsWithStats(clean_mask, connectivity=8)
    if num_lbl <= 1:
        return [], {"status": "rejected_empty", "reason": "empty_after_chrome_removal"}

    w_stroke = _estimate_stroke_width(clean_mask)
    min_speck_area = max(8, int(round(0.000012 * img_area)))
    min_motif_dim = max(36, int(round(0.045 * min_dim)))
    min_motif_area = max(200, int(round(0.00035 * img_area)))

    # Collect isolated circular cup marks & linear crack components BEFORE motif grouping
    detected_cup_marks_work: List[Tuple[float, float, float]] = []
    crack_mask_work = (colored_crack_mask > 0).astype(np.uint8) * 255 if colored_crack_mask is not None else np.zeros((h, w), dtype=np.uint8)

    comp_ids: List[int] = []
    for cid in range(1, num_lbl):
        x, y, bw, bh, area = [int(v) for v in stats[cid]]
        if area < min_speck_area:
            continue
        ar = max(bw / max(bh, 1.0), bh / max(bw, 1.0))
        sub = (labels[y : y + bh, x : x + bw] == cid).astype(np.uint8)
        cnts, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if cnts:
            hull = cv2.convexHull(cnts[0])
            hull_area = max(float(cv2.contourArea(hull)), 1.0)
            hull_perim = max(float(cv2.arcLength(hull, True)), 1.0)
            sol = area / hull_area
            circ = 4.0 * math.pi * hull_area / (hull_perim * hull_perim)

            # Isolated circular cup mark (skålgrop): compact, solid, circular, small-to-medium
            if max(bw, bh) < min_motif_dim * 1.15 and ar < 1.55 and sol >= 0.80 and circ >= 0.72 and area >= 14:
                cx_w, cy_w = float(centroids[cid][0]), float(centroids[cid][1])
                r_w = math.sqrt(area / math.pi)
                detected_cup_marks_work.append((cx_w, cy_w, r_w))
                continue

            # Straight linear rock crack / geological fissure
            if ar >= 6.5 and sol >= 0.72 and max(bw, bh) >= 24:
                crack_mask_work[y : y + bh, x : x + bw] = np.maximum(crack_mask_work[y : y + bh, x : x + bw], sub * 255)
                continue

        comp_ids.append(cid)

    if len(comp_ids) > 350:
        comp_ids.sort(key=lambda c: int(stats[c, cv2.CC_STAT_AREA]), reverse=True)
        comp_ids = comp_ids[:350]
    comp_ids.sort(key=lambda c: int(stats[c, cv2.CC_STAT_LEFT]))

    if not comp_ids:
        return [], {"status": "rejected_segment", "reason": "no_figurative_components"}

    n_comp = len(comp_ids)
    boxes = np.zeros((n_comp, 4), dtype=np.int32)
    areas = np.zeros(n_comp, dtype=np.int32)
    diags = np.zeros(n_comp, dtype=np.float32)
    pts_list: List[np.ndarray] = []
    trees: List[cKDTree] = []

    for i, cid in enumerate(comp_ids):
        x, y, bw, bh, area = [int(v) for v in stats[cid]]
        boxes[i] = (x, y, bw, bh)
        areas[i] = area
        diags[i] = math.hypot(bw, bh)
        sub = (labels[y : y + bh, x : x + bw] == cid).astype(np.uint8)
        cnts, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if cnts:
            pts = np.vstack([c.reshape(-1, 2) for c in cnts]).astype(np.float32)
            pts[:, 0] += x
            pts[:, 1] += y
        else:
            pts = np.array([[x, y]], dtype=np.float32)
        if len(pts) > 64:
            pts = pts[np.floor(np.linspace(0, len(pts) - 1, 64)).astype(int)]
        pts_list.append(pts)
        trees.append(cKDTree(pts))

    uf = _UnionFind(n_comp)
    max_search_r = min(max(3.6 * w_stroke, 0.042 * min_dim), 75.0)

    for i in range(n_comp):
        xi, yi, wi, hi = boxes[i]
        xi2, yi2 = xi + wi, yi + hi
        for j in range(i + 1, n_comp):
            xj, yj, wj, hj = boxes[j]
            if xj > xi2 + max_search_r:
                break
            xj2, yj2 = xj + wj, yj + hj
            if max(0, max(yi - yj2, yj - yi2)) > max_search_r:
                continue
            dists, _ = trees[j].query(pts_list[i], k=1)
            d_min = float(np.min(dists))
            s_small = float(min(diags[i], diags[j]))
            s_big = float(max(diags[i], diags[j]))

            ix0, iy0 = max(xi, xj), max(yi, yj)
            ix1, iy1 = min(xi2, xj2), min(yi2, yj2)
            inter = max(0, ix1 - ix0) * max(0, iy1 - iy0)
            containment = inter / float(max(1, min(wi * hi, wj * hj)))
            if containment >= 0.65 and d_min <= min(0.16 * s_big, max_search_r):
                uf.union(i, j)
                continue
            if s_small <= 0.45 * s_big and d_min <= min(max(3.0 * w_stroke, 0.085 * s_big), max_search_r):
                uf.union(i, j)
                continue
            if d_min <= min(max(2.0 * w_stroke, 8.0), 0.014 * min_dim):
                uf.union(i, j)

    clusters: Dict[int, List[int]] = {}
    for i in range(n_comp):
        clusters.setdefault(uf.find(i), []).append(i)

    cluster_infos = []
    for members in clusters.values():
        x0 = min(int(boxes[m, 0]) for m in members)
        y0 = min(int(boxes[m, 1]) for m in members)
        x1 = max(int(boxes[m, 0] + boxes[m, 2]) for m in members)
        y1 = max(int(boxes[m, 1] + boxes[m, 3]) for m in members)
        cluster_infos.append((int(sum(areas[m] for m in members)), x0, y0, x1 - x0, y1 - y0, members))
    cluster_infos.sort(key=lambda t: t[0], reverse=True)

    is_multi = len(cluster_infos) >= 3
    traced: List[TracedFigure] = []

    for total_area, x0, y0, bw, bh, members in cluster_infos:
        if len(traced) >= max_motifs_per_panel:
            break
        if max(bw, bh) < min_motif_dim or total_area < min_motif_area:
            continue
        in_cx = (x0 + bw <= 0.16 * w) or (x0 >= 0.84 * w)
        in_cy = (y0 + bh <= 0.16 * h) or (y0 >= 0.84 * h)
        if is_multi and in_cx and in_cy and max(bw, bh) < 0.16 * min_dim:
            continue
        touches_edge = (x0 <= 2) or (y0 <= 2) or (x0 + bw >= w - 2) or (y0 + bh >= h - 2)
        if is_multi and touches_edge and max(bw, bh) < 0.55 * max(h, w):
            continue
        if is_multi and bw >= 0.78 * w and bh >= 0.78 * h and len(members) >= 10:
            continue

        # Context-padded crop around figure for layers (cup_marks, cracks, uncertain)
        pad_w = max(14, int(round(0.12 * max(bw, bh))))
        cx0 = max(0, x0 - pad_w)
        cy0 = max(0, y0 - pad_w)
        cx1 = min(w, x0 + bw + pad_w)
        cy1 = min(h, y0 + bh + pad_w)
        cbw, cbh = cx1 - cx0, cy1 - cy0

        fig_crop = np.zeros((cbh, cbw), dtype=np.uint8)
        for m in members:
            cid = comp_ids[m]
            mx, my, mw, mh = boxes[m]
            sub = labels[my : my + mh, mx : mx + mw] == cid
            fig_crop[my - cy0 : my - cy0 + mh, mx - cx0 : mx - cx0 + mw][sub] = 255

        # Quick shape sanity checks
        cnts, _ = cv2.findContours(fig_crop, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            continue
        hull = cv2.convexHull(np.vstack(cnts))
        hull_area = max(float(cv2.contourArea(hull)), 1.0)
        hull_perim = max(float(cv2.arcLength(hull, True)), 1.0)
        sol = total_area / hull_area
        hull_circ = 4.0 * math.pi * hull_area / (hull_perim * hull_perim)
        ar = max(bw / max(bh, 1.0), bh / max(bw, 1.0))
        rect_ratio = total_area / float(max(bw * bh, 1))

        if ar >= 6.5 and sol >= 0.72:
            continue
        if rect_ratio >= 0.80 and sol >= 0.90 and ar <= 6.5:
            continue
        filled_c = ndimage.binary_fill_holes(fig_crop > 0)
        int_holes = int(np.sum(filled_c)) - int(np.sum(fig_crop > 0))
        has_struct = int_holes >= max(40, int(0.10 * total_area))
        if (not has_struct) and sol >= 0.87 and hull_circ >= 0.78 and ar < 1.85:
            # Record as large cupmark/oval if round, skip as figure
            continue
        if len(members) >= 14 and (total_area / float(max(bw * bh, 1))) < 0.06:
            continue

        # Convert crop bbox to native raw pixel coordinates
        raw_x0 = max(0, int(math.floor(cx0 * inv_scale)))
        raw_y0 = max(0, int(math.floor(cy0 * inv_scale)))
        raw_x1 = min(raw_w, int(math.ceil(cx1 * inv_scale)))
        raw_y1 = min(raw_h, int(math.ceil(cy1 * inv_scale)))
        raw_cw = max(1, raw_x1 - raw_x0)
        raw_ch = max(1, raw_y1 - raw_y0)

        subpaths, geom = _extract_contours_as_subpaths(fig_crop, inv_scale=inv_scale, simplify_px=0.75)
        if not subpaths or geom is None:
            continue

        # Cup marks inside padded crop
        local_cups: List[Tuple[float, float, float]] = []
        for cm_x, cm_y, cm_r in detected_cup_marks_work:
            if cx0 <= cm_x <= cx1 and cy0 <= cm_y <= cy1:
                lx = round((cm_x - cx0) * inv_scale, 2)
                ly = round((cm_y - cy0) * inv_scale, 2)
                lr = round(max(1.5, cm_r * inv_scale), 2)
                local_cups.append((lx, ly, lr))

        # Cracks inside padded crop
        crack_sub = crack_mask_work[cy0:cy1, cx0:cx1]
        local_cracks = [
            [(round(px * inv_scale, 2), round(py * inv_scale, 2)) for px, py in line]
            for line in _mask_to_polylines(crack_sub, min_len=10)
        ]

        # Uncertain weathered lines inside padded crop (excluding solid figure pixels)
        local_uncertain: List[List[Tuple[float, float]]] = []
        if weak_mask is not None:
            fig_dil = cv2.dilate(fig_crop, np.ones((3, 3), np.uint8)) > 0
            weak_sub = (weak_mask[cy0:cy1, cx0:cx1] > 0) & (~fig_dil)
            weak_u8 = (weak_sub.astype(np.uint8)) * 255
            local_uncertain = [
                [(round(px * inv_scale, 2), round(py * inv_scale, 2)) for px, py in line]
                for line in _mask_to_polylines(weak_u8, min_len=8)
            ]

        # Geometry metrics in raw pixel units
        minx, miny, maxx, maxy = geom.bounds
        width_px = max(1.0, float(maxx - minx))
        height_px = max(1.0, float(maxy - miny))
        area_px2 = float(geom.area)
        perim_px = float(geom.length)
        comp_px = (4.0 * math.pi * area_px2 / max(perim_px * perim_px, 1e-6))
        hull_g = geom.convex_hull
        sol_g = area_px2 / max(float(hull_g.area), 1e-6)
        v_count = sum(len(ext) - 1 for ext, _ in subpaths)
        h_count = sum(len(hs) for _, hs in subpaths)
        comp_count = len(subpaths)
        euler_num = comp_count - h_count

        traced.append(
            TracedFigure(
                panel_index=len(traced),
                crop_px=[raw_x0, raw_y0, raw_cw, raw_ch],
                raw_shape_hw=(raw_h, raw_w),
                work_scale=work_scale,
                geometry=geom,
                mask_crop=fig_crop,
                subpaths_px=subpaths,
                cup_marks_px=local_cups,
                cracks_px=local_cracks,
                uncertain_px=local_uncertain,
                polarity=str(ext_info.get("polarity", "dark_on_light")),
                ink_mode=str(ext_info.get("ink_mode", "standard_black_ink")),
                method="svg_vector_contour" if media_type.strip().lower() == "svg_vector" else "sauvola_local_gradient_contour",
                metrics={
                    "width_px": round(width_px, 3),
                    "height_px": round(height_px, 3),
                    "area_px2": round(area_px2, 3),
                    "perimeter_px": round(perim_px, 3),
                    "aspect_ratio": round(width_px / max(height_px, 1e-6), 4),
                    "solidity": round(sol_g, 4),
                    "compactness": round(comp_px, 4),
                    "vertex_count": int(v_count),
                    "hole_count": int(h_count),
                    "component_count": int(comp_count),
                    "euler_number": int(euler_num),
                },
            )
        )

    return traced, {
        "status": "ok" if traced else "rejected_no_figures",
        "figure_count": len(traced),
        "cup_marks_panel_total": len(detected_cup_marks_work),
        "ext_info": ext_info,
    }


def retrace_figure_method_b_otsu_marching_squares(
    raw_path: Path,
    crop_px: List[int],
    media_type: str,
    polarity_hint: str = "auto",
    ink_mode_hint: str = "auto",
    work_scale_hint: float = 1.0,
) -> Tuple[Optional[Any], Optional[np.ndarray], Dict[str, Any]]:
    """Method B (Independent QA re-trace): Multi-scale morphological + Otsu + sub-pixel marching squares.

    Operates DIRECTLY on the raw source file + crop_px with ZERO shared intermediate mask from Method A.
    Returns (shapely_geometry_in_raw_crop_px, binary_mask_in_work_crop, info).
    """
    mtype = media_type.strip().lower()
    rx, ry, rw, rh = [int(v) for v in crop_px]
    info: Dict[str, Any] = {"method": "otsu_multiscale_morph_marching_squares", "ok": False}

    # For SVG, rasterize independently at a distinct resolution (1200px) + marching squares
    if raw_path.suffix.lower() == ".svg" or mtype == "svg_vector":
        mask_full, sinfo = _extract_svg_mask(raw_path, max_side=1200, prefer_backend="svgpathtools")
        if mask_full is None:
            info["error"] = sinfo.get("reject_reason", "svg_b_failed")
            return None, None, info
        # Resample crop coords from Method A's 1600px frame to Method B's 1200px frame and scale back
        fh, fw = mask_full.shape[:2]
        # Note: scale ratio between Method A SVG raster (max 1600) and Method B SVG raster (max 1200)
        b_scale = fw / float(max(1, rw if rx == 0 else (rx + rw)))
        # Better: compute exact aspect-preserving scale from svg_vector raw dims
        raw_bytes = raw_path.read_bytes()
        _, _, doc_w, doc_h = _parse_svg_dims(raw_bytes)
        a_scale = min(1600, max(64, int(round(max(doc_w, doc_h)))))
        a_max = 1600.0 / max(doc_w, doc_h, 1.0)
        a_w = max(64, min(1600, int(round(doc_w * a_max))))
        a_h = max(64, min(1600, int(round(doc_h * a_max))))
        sx_ba = fw / float(max(a_w, 1))
        sy_ba = fh / float(max(a_h, 1))
        x0 = max(0, min(fw - 1, int(round(rx * sx_ba))))
        y0 = max(0, min(fh - 1, int(round(ry * sy_ba))))
        x1 = max(x0 + 1, min(fw, int(round((rx + rw) * sx_ba))))
        y1 = max(y0 + 1, min(fh, int(round((ry + rh) * sy_ba))))
        crop_bin = (mask_full[y0:y1, x0:x1] > 0).astype(np.float32)
        inv_scale = rw / float(max(x1 - x0, 1))
    else:
        with Image.open(raw_path) as img:
            img = ImageOps.exif_transpose(img)
            raw_w, raw_h = img.size
            x0 = max(0, min(raw_w - 1, rx))
            y0 = max(0, min(raw_h - 1, ry))
            x1 = max(x0 + 1, min(raw_w, rx + rw))
            y1 = max(y0 + 1, min(raw_h, ry + rh))
            crop_img = img.crop((x0, y0, x1, y1))
            cw_raw, ch_raw = crop_img.size
            # Cap oversized crops cleanly while preserving exact scale factor back to raw crop px
            max_crop_side = 1200
            c_scale = 1.0
            if max(cw_raw, ch_raw) > max_crop_side:
                c_scale = max_crop_side / float(max(cw_raw, ch_raw))
                crop_img = crop_img.resize(
                    (max(8, int(round(cw_raw * c_scale))), max(8, int(round(ch_raw * c_scale)))),
                    Image.Resampling.BILINEAR,
                )
            has_alpha = crop_img.mode in ("RGBA", "LA") or ("transparency" in crop_img.info)
            rgba = np.array(crop_img.convert("RGBA"), dtype=np.uint8)
            inv_scale = cw_raw / float(max(rgba.shape[1], 1))

        alpha = rgba[..., 3]
        rgb = rgba[..., :3]

        if has_alpha and float(np.mean(alpha < 25)) >= 0.12:
            # Independent Otsu on alpha channel
            _, bin_u8 = cv2.threshold(alpha, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            crop_bin = (bin_u8 > 0).astype(np.float32)
        else:
            if has_alpha:
                a = alpha.astype(np.float32)[..., None] / 255.0
                rgb = np.clip(rgb.astype(np.float32) * a + 255.0 * (1.0 - a), 0, 255).astype(np.uint8)
            gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
            hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
            sat = hsv[..., 1]

            if mtype == "rti_3d" and float(np.mean(sat)) > 45.0:
                lap = np.abs(cv2.Laplacian(rgb.astype(np.float32), cv2.CV_32F, ksize=3)).mean(axis=2)
                lap_u8 = np.clip(lap / max(float(np.percentile(lap, 99.0)), 1.0) * 255.0, 0, 255).astype(np.uint8)
                _, bin_u8 = cv2.threshold(lap_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                crop_bin = (bin_u8 > 0).astype(np.float32)
            else:
                # Infer polarity independently from crop outer 2px border
                b_vals = np.concatenate([gray[0, :], gray[-1, :], gray[:, 0], gray[:, -1]])
                b_med = float(np.median(b_vals))
                is_light_bg = (polarity_hint == "dark_on_light") or (polarity_hint == "auto" and b_med >= 125.0)

                # Multi-scale morphological black-hat / top-hat contrast (completely independent of Gaussian blur!)
                responses = []
                for rad in (7, 15, 29):
                    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * rad + 1, 2 * rad + 1))
                    if is_light_bg:
                        responses.append(cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, k))
                    else:
                        responses.append(cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, k))
                ms_resp = np.maximum.reduce(responses)

                ch_c, cw_c = gray.shape[:2]
                b_mad = float(np.median(np.abs(b_vals.astype(np.float32) - b_med)))
                ds_m = 4 if min(ch_c, cw_c) >= 64 else 1
                g_sm = cv2.resize(gray, (max(4, cw_c // ds_m), max(4, ch_c // ds_m)), interpolation=cv2.INTER_AREA) if ds_m > 1 else gray
                k_bg_r = max(9, min(45, (min(g_sm.shape[:2]) // 2) | 1))
                k_bg = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_bg_r, k_bg_r))

                if is_light_bg:
                    bg_sm = cv2.morphologyEx(g_sm, cv2.MORPH_CLOSE, k_bg)
                    bg_morph = cv2.resize(bg_sm, (cw_c, ch_c), interpolation=cv2.INTER_LINEAR) if ds_m > 1 else bg_sm
                    if b_mad < 15.0 and b_med >= 150.0:
                        bg_morph = np.maximum(bg_morph, int(round(b_med)))
                    loc_contrast = np.clip(bg_morph.astype(np.float32) - gray.astype(np.float32), 0, 255).astype(np.uint8)
                    combined = np.maximum(ms_resp, (loc_contrast.astype(np.float32) * 0.9).astype(np.uint8))
                    b_s_vals = np.concatenate([sat[0, :], sat[-1, :], sat[:, 0], sat[:, -1]])
                    b_s_med = float(np.median(b_s_vals))
                    is_colored_ink_plate = (
                        ink_mode_hint == "colored_ink_only"
                        or (b_med > 205.0 and b_s_med < 28.0 and float(np.mean((gray < 95) & (sat < 60))) < 0.003 and float(np.mean((sat >= 75) & (gray < 215))) >= 0.002)
                    )
                    if is_colored_ink_plate:
                        combined = np.maximum(combined, sat)
                else:
                    bg_sm = cv2.morphologyEx(g_sm, cv2.MORPH_OPEN, k_bg)
                    bg_morph = cv2.resize(bg_sm, (cw_c, ch_c), interpolation=cv2.INTER_LINEAR) if ds_m > 1 else bg_sm
                    if b_mad < 15.0 and b_med <= 110.0:
                        bg_morph = np.minimum(bg_morph, int(round(b_med)))
                    loc_contrast = np.clip(gray.astype(np.float32) - bg_morph.astype(np.float32), 0, 255).astype(np.uint8)
                    combined = np.maximum(ms_resp, (loc_contrast.astype(np.float32) * 0.9).astype(np.uint8))

                combined = cv2.medianBlur(combined, 3)
                # Otsu threshold on multi-scale morphological contrast
                otsu_t, _ = cv2.threshold(combined, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                eff_thr = max(32.0, float(otsu_t))
                fg = combined >= eff_thr
                if ink_mode_hint == "black_carvings_with_colored_cracks":
                    fg = fg & (sat < 75)
                # Mild morphological closing of 1px gaps
                k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
                fg_u8 = cv2.morphologyEx((fg.astype(np.uint8) * 255), cv2.MORPH_CLOSE, k3)
                # Drop tiny specks and minor margin-border fragments clipped in padding when central figure exists
                n_l, lbls, st, cents = cv2.connectedComponentsWithStats(fg_u8, connectivity=8)
                if n_l > 1:
                    ch_c, cw_c = fg_u8.shape[:2]
                    keep = np.zeros_like(fg_u8)
                    max_a = int(np.max(st[1:, cv2.CC_STAT_AREA]))
                    min_a = max(20, int(0.025 * max_a))
                    for c in range(1, n_l):
                        cx_i, cy_i, bw_i, bh_i, area_i = [int(v) for v in st[c]]
                        if area_i < min_a:
                            continue
                        touches_edge = (cx_i <= 1) or (cy_i <= 1) or (cx_i + bw_i >= cw_c - 1) or (cy_i + bh_i >= ch_c - 1)
                        c_x, c_y = float(cents[c][0]), float(cents[c][1])
                        in_pad_edge = (c_x < 0.08 * cw_c or c_x > 0.92 * cw_c or c_y < 0.08 * ch_c or c_y > 0.92 * ch_c)
                        if touches_edge and in_pad_edge and area_i < 0.30 * max_a:
                            continue
                        keep[lbls == c] = 255
                    fg_u8 = keep
                crop_bin = (fg_u8 > 0).astype(np.float32)

    if not np.any(crop_bin):
        info["error"] = "method_b_empty_mask"
        return None, None, info

    # Sub-pixel Marching Squares (skimage.measure.find_contours)
    smoothed = cv2.GaussianBlur(crop_bin, (0, 0), sigmaX=0.75, sigmaY=0.75)
    # Pad by 1px zero border so contours touching crop edge close cleanly
    padded = np.pad(smoothed, 1, mode="constant", constant_values=0.0)
    raw_contours = measure.find_contours(padded, level=0.5)
    if not raw_contours:
        info["error"] = "method_b_no_contours"
        return None, None, info

    # Cap pathological noisy texture crops to top 64 longest contours
    if len(raw_contours) > 64:
        raw_contours = sorted(raw_contours, key=len, reverse=True)[:64]

    polys: List[Polygon] = []
    for cnt in raw_contours:
        if len(cnt) < 4:
            continue
        # Subtract 1px pad; (row=y, col=x) -> (x, y) in raw crop px
        pts = [((float(c[1]) - 1.0) * inv_scale, (float(c[0]) - 1.0) * inv_scale) for c in cnt]
        if pts[0] != pts[-1]:
            pts.append(pts[0])
        try:
            p = Polygon(pts)
            if not p.is_valid:
                p = p.buffer(0)
            if p.is_empty or p.area <= 18.0:
                continue
            p_simp = p.simplify(0.65, preserve_topology=True)
            if p_simp.is_valid and not p_simp.is_empty:
                p = p_simp
            if p.geom_type == "MultiPolygon":
                for g in p.geoms:
                    if g.area > 4.0:
                        polys.append(g)
            elif p.geom_type == "Polygon":
                polys.append(p)
        except Exception:
            continue

    if not polys:
        info["error"] = "method_b_no_valid_polygons"
        return None, None, info

    # Separate outer shells vs holes via symmetric difference (even-odd) or union
    # Measure.find_contours returns both outer and hole loops; symmetric difference reconstructs exact solid-with-holes!
    geom_b = polys[0]
    for p in polys[1:]:
        geom_b = geom_b.symmetric_difference(p)
    if not geom_b.is_valid:
        geom_b = geom_b.buffer(0)
    if geom_b.is_empty or geom_b.area <= 2.0:
        info["error"] = "method_b_empty_geometry"
        return None, None, info

    # Compute topology stats
    if geom_b.geom_type == "Polygon":
        comp_count = 1
        hole_count = len(geom_b.interiors)
    elif geom_b.geom_type == "MultiPolygon":
        comp_count = len(geom_b.geoms)
        hole_count = sum(len(g.interiors) for g in geom_b.geoms)
    else:
        comp_count = 1
        hole_count = 0

    info.update({
        "ok": True,
        "area_px2": round(float(geom_b.area), 3),
        "component_count": comp_count,
        "hole_count": hole_count,
        "euler_number": comp_count - hole_count,
    })
    return geom_b, (crop_bin > 0.5).astype(np.uint8) * 255, info
