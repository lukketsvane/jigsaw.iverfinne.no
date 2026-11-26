"""Rock-art-aware raster extractors for B&W tracings, rubbings, and red-painted photos."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import cv2
import numpy as np
from PIL import Image, ImageOps
from scipy import ndimage

# SHFA & museum archaeological tracings can exceed 200 MP (e.g., 12293x18572 px)
Image.MAX_IMAGE_PIXELS = None


def load_image_rgba(path: Path, max_side: int = 1800) -> Tuple[np.ndarray, bool]:
    """Load raster image respecting EXIF orientation, resized cleanly if oversized."""
    with Image.open(path) as img:
        # Fast JPEG draft subsampling for huge >50 MP museum/SHFA tracings
        if getattr(img, "format", None) == "JPEG" and max(img.size) > max_side * 2:
            try:
                img.draft("RGB", (max_side * 2, max_side * 2))
            except Exception:
                pass
        img = ImageOps.exif_transpose(img)
        has_alpha = img.mode in ("RGBA", "LA") or ("transparency" in img.info)
        w, h = img.size
        if max(w, h) > max_side:
            scale = max_side / float(max(w, h))
            img = img.resize((max(1, int(round(w * scale))), max(1, int(round(h * scale)))), Image.Resampling.BILINEAR)
        rgba = img.convert("RGBA")
        return np.array(rgba, dtype=np.uint8), has_alpha


def _fast_gaussian_bg(img_f32: np.ndarray, sigma: float) -> np.ndarray:
    """Compute large-sigma illumination background 20x faster via 8x downsampled Gaussian blur."""
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
    top = arr[:my, :].reshape(-1)
    bot = arr[-my:, :].reshape(-1)
    left = arr[:, :mx].reshape(-1)
    right = arr[:, -mx:].reshape(-1)
    return np.concatenate([top, bot, left, right])


def _is_color_photograph(rgb: np.ndarray) -> Tuple[bool, float]:
    """Detect whether an image is a colorful/textured photograph vs a B&W/monochrome tracing."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat = hsv[..., 1].astype(np.float32)
    # Fraction of pixels with moderate-to-high saturation
    sat_frac = float(np.mean(sat > 42))
    # Also check background texture variance (photos of rock have high local texture)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_32F).var())
    return sat_frac > 0.25 and lap_var > 60.0, sat_frac


def extract_bw_tracing(
    rgba: np.ndarray,
    has_alpha: bool,
    is_rubbing: bool = False,
) -> Tuple[Optional[np.ndarray], Dict[str, Any]]:
    """Extract clean foreground carving mask from B&W archaeological tracing or rubbing/frottage.

    Handles:
      - Transparent PNG tracings (alpha channel)
      - Dark ink on white/cream paper (standard kalkering)
      - White carvings on black background (inverted tracing / 3D scan / museum graphic)
      - Colored ink tracings (red/blue ink on white paper)
      - Rubbings / frottage with uneven lighting (illumination normalization)
    """
    info: Dict[str, Any] = {
        "extractor": "rubbing_frottage" if is_rubbing else "bw_tracing",
        "ok": False,
    }
    h, w = rgba.shape[:2]
    if min(h, w) < 40:
        info["reject_reason"] = "image_too_small"
        return None, info

    alpha = rgba[..., 3]
    rgb = rgba[..., :3]

    # Case 1: Transparent PNG tracing
    if has_alpha and float(np.mean(alpha < 25)) >= 0.15:
        fg = alpha >= 64
        fg_frac = float(np.mean(fg))
        if 0.0005 <= fg_frac <= 0.72:
            info["polarity"] = "alpha"
            info["fg_fraction"] = round(fg_frac, 5)
            info["ok"] = True
            return (fg.astype(np.uint8) * 255), info

    # Composite onto white if partial alpha
    if has_alpha:
        a = alpha.astype(np.float32)[..., None] / 255.0
        rgb = np.clip(rgb.astype(np.float32) * a + 255.0 * (1.0 - a), 0, 255).astype(np.uint8)

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat = hsv[..., 1]

    # Reject if actually a noisy full-color rock photograph mislabeled as bw_tracing
    # (Unless it has a clean uniform white/black paper background)
    border_gray = _border_samples(gray)
    border_med = float(np.median(border_gray))
    border_mad = float(np.median(np.abs(border_gray.astype(np.float32) - border_med)))
    global_med = float(np.median(gray))

    # Illumination normalization (crucial for scanned tracings & rubbings)
    sigma = max(15.0, min(h, w) / 14.0)
    bg_est = _fast_gaussian_bg(gray.astype(np.float32), sigma=sigma)

    # Determine polarity from border + global median
    # Light background (standard ink on paper): border_med >= 135
    # Dark background (white carving on black / dark frottage): border_med < 115
    if border_med >= 130:
        polarity = "dark_on_light"
        # Foreground is darker than local background OR strongly saturated ink on bright paper
        diff = bg_est - gray.astype(np.float32)
        # Also global contrast against paper
        global_dark = (border_med - gray.astype(np.float32))
        score = np.maximum(diff, global_dark * 0.85)

        # If scanned paper is very clean (low MAD), a crisp threshold works best
        if border_mad < 12.0 and not is_rubbing:
            # Distinguish two Nordic archaeological tracing conventions on white paper:
            #   (1) Toreld/Andersson & standard plates: solid BLACK carvings, sometimes with thin RED/BLUE rock-edge lines.
            #   (2) Torsten Högberg plates: crisp CRIMSON/RED ink carvings on pure white paper (no black carvings).
            neutral_black = (gray < 95) & (sat < 60)
            colored_ink = (sat >= 75) & (gray < 215) & (border_med > 195)
            neutral_black_frac = float(np.mean(neutral_black))
            colored_ink_frac = float(np.mean(colored_ink))

            inv_gray = 255 - gray
            otsu_t, _ = cv2.threshold(inv_gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            thr = max(35.0, min(145.0, float(otsu_t)))

            if colored_ink_frac >= 0.002 and neutral_black_frac < 0.0035:
                # Crimson/colored-ink-only tracing (e.g., Torsten Högberg)
                fg = colored_ink | ((inv_gray >= max(thr, 75.0)) & (score >= 30.0))
                info["ink_mode"] = "colored_ink_only"
            elif neutral_black_frac >= 0.002 and colored_ink_frac >= 0.0005:
                # Mixed black carvings + red/blue rock-crack annotation lines -> keep ONLY dark neutral black ink!
                fg = (inv_gray >= max(thr, 80.0)) & (score >= 28.0) & (sat < 75)
                info["ink_mode"] = "black_carvings_ignore_colored_cracks"
            else:
                # Standard black-on-white tracing
                fg = (inv_gray >= thr) & (score >= 22.0)
                info["ink_mode"] = "standard_black_ink"
        else:
            # Uneven lighting or rubbing on light paper: adaptive + hysteresis
            blur_score = cv2.medianBlur(np.clip(score, 0, 255).astype(np.uint8), 3)
            otsu_t, _ = cv2.threshold(blur_score, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            thr = max(28.0 if not is_rubbing else 36.0, float(otsu_t))
            fg = blur_score >= thr
    elif border_med < 110:
        polarity = "light_on_dark"
        diff = gray.astype(np.float32) - bg_est
        global_light = gray.astype(np.float32) - border_med
        score = np.maximum(diff, global_light * 0.85)
        if border_mad < 14.0 and not is_rubbing:
            otsu_t, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            thr = max(45.0, min(210.0, float(otsu_t)))
            fg = (gray >= thr) & (score >= 24.0)
        else:
            blur_score = cv2.medianBlur(np.clip(score, 0, 255).astype(np.uint8), 3)
            otsu_t, _ = cv2.threshold(blur_score, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            thr = max(32.0 if not is_rubbing else 40.0, float(otsu_t))
            fg = blur_score >= thr
    else:
        # Mid-gray textured background (often a frottage or rock photo)
        # Try picking the minority tail with higher contrast
        diff_dark = np.clip(bg_est - gray.astype(np.float32), 0, 255).astype(np.uint8)
        diff_light = np.clip(gray.astype(np.float32) - bg_est, 0, 255).astype(np.uint8)
        if np.percentile(diff_light, 97) > np.percentile(diff_dark, 97) * 1.15:
            polarity = "light_on_midgray"
            score_u8 = cv2.medianBlur(diff_light, 3)
        else:
            polarity = "dark_on_midgray"
            score_u8 = cv2.medianBlur(diff_dark, 3)
        otsu_t, _ = cv2.threshold(score_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        thr = max(38.0, float(otsu_t))
        fg = score_u8 >= thr

    mask = (fg.astype(np.uint8)) * 255

    # Denoise rubbings slightly harder to eliminate paper grain
    if is_rubbing:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)

    # Check foreground fraction and fragmentation/noise sanity
    fg_frac = float(np.mean(mask > 0))
    info["polarity"] = polarity
    info["border_med"] = round(border_med, 1)
    info["border_mad"] = round(border_mad, 1)
    info["fg_fraction"] = round(fg_frac, 5)

    if fg_frac < 0.0008:
        info["reject_reason"] = "bw_empty_foreground"
        return None, info
    if fg_frac > (0.38 if is_rubbing else 0.45):
        info["reject_reason"] = "bw_excessive_foreground"
        return None, info

    # Texture/speckle noise gate: reject grainy photographs & messy frottage sheets
    num_lbl, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if num_lbl > 1:
        areas = stats[1:, cv2.CC_STAT_AREA]
        total_area = float(np.sum(areas))
        tiny_area = float(np.sum(areas[areas < 35]))
        noise_ratio = tiny_area / max(total_area, 1.0)
        info["speckle_ratio"] = round(noise_ratio, 4)
        info["raw_cc_count"] = int(num_lbl - 1)
        if is_rubbing:
            # Strict gate for rubbings: only crisp, low-grain frottages survive
            if border_mad > 22.0 or noise_ratio > 0.22 or num_lbl > 450:
                info["reject_reason"] = "noisy_rubbing_rejected"
                return None, info
        else:
            if noise_ratio > 0.35 and border_mad > 12.0:
                info["reject_reason"] = "noisy_texture_not_tracing"
                return None, info
            if num_lbl > 2000 and noise_ratio > 0.25:
                info["reject_reason"] = "excessive_speckle_fragmentation"
                return None, info

    info["ok"] = True
    return mask, info


def extract_painted_photo(
    rgba: np.ndarray,
    strict_mode: bool = False,
) -> Tuple[Optional[np.ndarray], Dict[str, Any]]:
    """Extract red-ochre painted Nordic helleristninger from rock photographs.

    Uses CIE Lab a* redness contrast relative to rock background + HSV hue/saturation
    gating + hysteresis geodesic growth. Strictly rejects noisy/unpainted rock texture!
    """
    info: Dict[str, Any] = {"extractor": "painted_photo", "ok": False}
    h, w = rgba.shape[:2]
    if min(h, w) < 60:
        info["reject_reason"] = "image_too_small"
        return None, info

    rgb = rgba[..., :3]
    # Mild edge-preserving smoothing to suppress granite grain speckles
    rgb_smooth = cv2.bilateralFilter(rgb, d=5, sigmaColor=28, sigmaSpace=28)

    lab = cv2.cvtColor(rgb_smooth, cv2.COLOR_RGB2LAB).astype(np.float32)
    hsv = cv2.cvtColor(rgb_smooth, cv2.COLOR_RGB2HSV).astype(np.float32)

    L = lab[..., 0]  # 0..255
    a = lab[..., 1]  # 128 neutral; >128 red; <128 green
    b = lab[..., 2]  # 128 neutral; >128 yellow
    H = hsv[..., 0]  # 0..179 in OpenCV
    S = hsv[..., 1]  # 0..255
    V = hsv[..., 2]  # 0..255

    # Robust background redness statistics (rock surface is majority of photo)
    med_a = float(np.median(a))
    mad_a = float(np.median(np.abs(a - med_a)))
    scale_a = max(mad_a * 1.4826, 3.0)
    z_a = (a - med_a) / scale_a

    # Also compute local redness excess to handle shadows/sunlight across rock face
    sigma = max(18.0, min(h, w) / 10.0)
    local_a = _fast_gaussian_bg(a, sigma=sigma)
    rel_a = a - local_a

    # Red / terracotta / ochre / burgundy hue range (OpenCV H in [0..22] or [158..179])
    red_hue_strict = (H <= 18.0) | (H >= 160.0)
    red_hue_weak = (H <= 24.0) | (H >= 152.0)

    # Exclude bright yellow/orange lichen (where b is huge compared to a)
    not_yellow_lichen = (a - 128.0) >= 0.42 * np.maximum(b - 128.0, 0.0)

    # Strong core paint pixels (unmistakable red ochre paint)
    min_a_seed = 152.0 if strict_mode else 148.0
    min_s_seed = 76.0 if strict_mode else 68.0
    seed_mask = (
        red_hue_strict
        & not_yellow_lichen
        & (a >= min_a_seed)
        & ((z_a >= 3.5) | (rel_a >= 13.0))
        & (S >= min_s_seed)
        & (V >= 38.0)
        & (L >= 30.0)
    )

    seed_frac = float(np.mean(seed_mask))
    info["seed_fraction"] = round(seed_frac, 5)
    info["bg_a_median"] = round(med_a, 2)

    if seed_frac < 0.0004:
        info["reject_reason"] = "no_red_paint_signal"
        return None, info
    if seed_frac > 0.30:
        # Whole photo is red/orange rock or sunset -> not red paint on gray rock
        info["reject_reason"] = "red_background_not_carving_paint"
        return None, info

    # Weak paint envelope (softer edges of painted lines)
    weak_mask = (
        red_hue_weak
        & not_yellow_lichen
        & (a >= 137.0)
        & ((z_a >= 1.8) | (rel_a >= 6.0))
        & (S >= 38.0)
        & (V >= 28.0)
    )

    # Hysteresis reconstruction: keep only weak-paint components connected to strong seeds
    # Dilate seeds by 1px to ensure connectivity into weak edges
    seed_dil = ndimage.binary_dilation(seed_mask, iterations=1) & weak_mask
    labeled_weak, n_weak = ndimage.label(weak_mask, structure=np.ones((3, 3), dtype=int))
    if n_weak == 0:
        info["reject_reason"] = "no_connected_paint"
        return None, info

    seed_labels = np.unique(labeled_weak[seed_dil])
    seed_labels = seed_labels[seed_labels > 0]
    lut = np.zeros(n_weak + 1, dtype=bool)
    lut[seed_labels] = True
    fg = lut[labeled_weak]

    # Clean tiny pixel speckles
    k3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx((fg.astype(np.uint8) * 255), cv2.MORPH_OPEN, k3)

    fg_bool = mask > 0
    fg_frac = float(np.mean(fg_bool))
    info["fg_fraction"] = round(fg_frac, 5)

    if fg_frac < 0.0025:
        info["reject_reason"] = "painted_fg_too_sparse"
        return None, info
    if fg_frac > 0.24:
        info["reject_reason"] = "painted_fg_too_dense"
        return None, info

    # Strict color-contrast verification between extracted paint and surrounding rock
    a_fg = float(np.mean(a[fg_bool]))
    a_bg = float(np.mean(a[~fg_bool]))
    contrast_a = a_fg - a_bg
    info["lab_a_contrast"] = round(contrast_a, 2)
    min_contrast = 18.0 if strict_mode else 16.5
    if contrast_a < min_contrast:
        info["reject_reason"] = f"weak_paint_contrast:{contrast_a:.1f}"
        return None, info

    # Coherence / fragmentation gate: reject grainy mineral-speckle extractions
    num_lbl, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA] if num_lbl > 1 else np.array([], dtype=np.int32)
    if len(areas) == 0:
        info["reject_reason"] = "painted_no_components"
        return None, info

    total_area = float(np.sum(areas))
    small_area = float(np.sum(areas[areas < 80]))
    frag_ratio = small_area / max(total_area, 1.0)
    info["fragmentation_ratio"] = round(frag_ratio, 4)
    if frag_ratio > 0.30 or num_lbl > 180:
        info["reject_reason"] = f"painted_too_fragmented:{frag_ratio:.2f}"
        return None, info

    # Reject if largest component is tiny (just scattered dots)
    if int(np.max(areas)) < max(350, int(0.0012 * h * w)):
        info["reject_reason"] = "painted_no_coherent_figure"
        return None, info

    info["ok"] = True
    return mask, info


def extract_foreground_mask(
    raw_path: Path,
    media_type: str,
    max_side: int = 1800,
) -> Tuple[Optional[np.ndarray], Dict[str, Any]]:
    """Route a raw rock-art file to the appropriate extractor based on media_type."""
    from .svg_ingest import svg_to_foreground_mask

    ext = raw_path.suffix.lower()
    mtype = (media_type or "").strip().lower()

    if ext == ".svg" or mtype == "svg_vector":
        return svg_to_foreground_mask(raw_path, max_side=max_side)

    try:
        rgba, has_alpha = load_image_rgba(raw_path, max_side=max_side)
    except Exception as e:
        return None, {"extractor": mtype, "ok": False, "reject_reason": f"image_load_error:{e}"}

    if mtype == "bw_tracing":
        # Guard: if a file tagged bw_tracing is actually a painted color photo, route cleanly
        is_color_photo, sat_frac = _is_color_photograph(rgba[..., :3])
        if is_color_photo and not has_alpha:
            # Check if it's actually a red-painted photo
            painted_mask, p_info = extract_painted_photo(rgba, strict_mode=True)
            if painted_mask is not None:
                p_info["rerouted_from"] = "bw_tracing"
                return painted_mask, p_info
            # Otherwise still try bw_tracing only if border is uniform paper
            border_gray = _border_samples(cv2.cvtColor(rgba[..., :3], cv2.COLOR_RGB2GRAY))
            if float(np.median(np.abs(border_gray - np.median(border_gray)))) > 18.0:
                return None, {
                    "extractor": "bw_tracing",
                    "ok": False,
                    "reject_reason": f"mislabeled_color_photo:sat={sat_frac:.2f}",
                }
        return extract_bw_tracing(rgba, has_alpha=has_alpha, is_rubbing=False)

    if mtype == "rubbing_frottage":
        return extract_bw_tracing(rgba, has_alpha=has_alpha, is_rubbing=True)

    if mtype == "painted_photo":
        return extract_painted_photo(rgba, strict_mode=True)

    # Strictly skip photo_other (unpainted rock photographs yield noisy rock texture)
    return None, {
        "extractor": "photo_other",
        "ok": False,
        "reject_reason": "rejected_photo_other_skipped",
    }
