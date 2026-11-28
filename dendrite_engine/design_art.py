"""Photo outline extraction and seeded prints for the custom puzzle workflow."""

from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile
import sys

import cv2
import numpy as np
from shapely.geometry import Polygon

ART_PATTERNS = (
    "wood",
    "agate",
    "geode",
    "radial",
    "marbling",
    "turing",
    "bismuth",
    "comb_marbling",
)
MODEL_DIR = (
    Path(__file__).resolve().parents[1] / "resources/nervous-system/upstream/rembg"
)


@lru_cache(maxsize=1)
def _predictor():
    weights = MODEL_DIR / "weights/u2netp.pth"
    archives = sorted(MODEL_DIR.glob("rembg-*.zip"))
    if not weights.is_file() or not archives:
        return None
    import torch

    with ZipFile(archives[0]) as source:
        entry = next(
            n for n in source.namelist() if n.endswith("/src/rembg/u2net/u2net.py")
        )
    src = f"{archives[0].as_posix()}/{entry.split('/rembg/u2net/')[0]}"
    sys.path.insert(0, src)
    try:
        from rembg.u2net import u2net
    finally:
        sys.path.remove(src)
    net = u2net.U2NETP(3, 1)
    net.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    net.to(device).eval()

    def predict(rgb):
        scaled = cv2.resize(rgb, (320, 320), interpolation=cv2.INTER_AREA).astype(
            np.float32
        )
        scaled /= max(float(scaled.max()), 1.0)
        scaled -= np.float32([0.485, 0.456, 0.406])
        scaled /= np.float32([0.229, 0.224, 0.225])
        tensor = torch.from_numpy(scaled).permute(2, 0, 1).unsqueeze(0).to(device)
        with torch.inference_mode():
            primary = net(tensor)[0][0, 0].cpu().numpy()
        return cv2.normalize(primary, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)

    return predict


def suggest_outline(image):
    """Return the largest connected silhouette in normalized source-image coordinates."""
    h, w = image.shape[:2]
    if min(h, w) < 8:
        raise ValueError("Choose an image at least 8 pixels wide and high.")
    scale = min(1, 768 / max(w, h))
    sw, sh = max(8, round(w * scale)), max(8, round(h * scale))
    small = cv2.resize(image, (sw, sh), interpolation=cv2.INTER_AREA)
    if small.shape[2] == 4 and small[:, :, 3].min() < 128:
        mask, mode = small[:, :, 3], "alpha"
    else:
        predict = _predictor()
        rgb = np.ascontiguousarray(small[:, :, :3])
        if predict:
            mask, mode = cv2.resize(predict(rgb), (sw, sh)), "u2netp"
        else:
            mask = np.zeros((sh, sw), np.uint8)
            margin = max(1, round(min(sh, sw) * 0.035))
            cv2.grabCut(
                rgb,
                mask,
                (margin, margin, sw - 2 * margin, sh - 2 * margin),
                np.zeros((1, 65)),
                np.zeros((1, 65)),
                5,
                cv2.GC_INIT_WITH_RECT,
            )
            mask, mode = (mask % 2) * 255, "grabcut"
    contours, _ = cv2.findContours(
        np.uint8(mask >= 128), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours or max(map(cv2.contourArea, contours)) < max(4, sw * sh * 0.001):
        raise ValueError(
            "No connected foreground found. Use a transparent image or draw an outline."
        )
    ring = max(contours, key=cv2.contourArea).reshape(-1, 2)
    poly = Polygon(ring).buffer(0).simplify(max(0.7, max(sw, sh) / 750))
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda p: p.area)
    return {
        "mode": mode,
        "points": [
            [round(x / (sw - 1), 6), round(y / (sh - 1), 6)]
            for x, y in list(poly.exterior.coords)[:-1]
        ],
    }


def photo_board(image, width_mm):
    from heller_jigsaw.geometry.outline import snap_polygon

    points = np.asarray(suggest_outline(image)["points"]) * [
        image.shape[1] - 1,
        image.shape[0] - 1,
    ]
    pixels = np.rint(points).astype(int)
    lo, hi = pixels.min(axis=0), pixels.max(axis=0)
    poly = snap_polygon(Polygon((pixels - lo) * (width_mm / (hi[0] - lo[0]))))
    return poly, image[lo[1] : hi[1] + 1, lo[0] : hi[0] + 1]


def render_pattern(pattern, outline, seed, longest_side=1400):
    """Agate layers follow the perimeter. Additional geode nuclei interrupt the bands."""
    from .phase_field import rasterize_polygon

    if pattern not in ART_PATTERNS[1:]:
        raise ValueError(f"Unknown artwork pattern {pattern!r}")
    rng = np.random.default_rng(seed)
    x0, y0, x1, y1 = outline.bounds
    scale = longest_side / max(x1 - x0, y1 - y0)
    w, h = max(2, round((x1 - x0) * scale)), max(2, round((y1 - y0) * scale))
    if pattern in ("turing", "bismuth", "comb_marbling"):
        return _simulate_print(pattern, rng, w, h)
    y, x = np.mgrid[:h, :w].astype(np.float32) / longest_side
    phase = rng.uniform(0, 2 * np.pi, 5)
    warp = np.sin(14 * x + phase[0]) * np.sin(11 * y + phase[1]) + 0.5 * np.sin(
        31 * x - 17 * y + phase[2]
    )
    if pattern in ("geode", "agate"):
        mask = rasterize_polygon(outline, outline.bounds, (h, w)).astype(np.uint8)
        distance = (
            cv2.distanceTransform(np.pad(mask, 1), cv2.DIST_L2, 5)[1:-1, 1:-1]
            / longest_side
        )
        if pattern == "geode":
            for _ in range(4):
                cx, cy = rng.uniform(0.12, 0.88, 2) * [
                    w / longest_side,
                    h / longest_side,
                ]
                distance = np.minimum(
                    distance, np.hypot(x - cx, y - cy) + rng.uniform(0.015, 0.07)
                )
        field = 28 * distance + 0.25 * warp * (1 - np.exp(-distance * 25))
        palette = [
            [43, 36, 61],
            [99, 69, 103],
            [195, 129, 132],
            [234, 200, 163],
            [253, 233, 199],
            [124, 88, 118],
        ]
    elif pattern == "radial":
        cx, cy = x - w / longest_side * 0.5, y - h / longest_side * 0.5
        field = (
            17 * np.hypot(cx, cy)
            + 0.45 * np.cos(8 * np.arctan2(cy, cx) + phase[0])
            + 0.1 * warp
        )
        palette = [
            [22, 43, 55],
            [46, 111, 115],
            [244, 213, 155],
            [202, 108, 73],
            [27, 55, 65],
        ]
    else:
        field = (
            9 * y
            + 2 * np.sin(7 * x + 1.8 * np.sin(6 * y + phase[1]) + phase[0])
            + 0.6 * warp
        )
        palette = [
            [22, 43, 55],
            [50, 101, 118],
            [189, 220, 202],
            [240, 186, 143],
            [169, 84, 94],
        ]
    colors = np.asarray(palette + [palette[0]], np.float32)
    bands = (
        (field + 0.19 * np.sin(2.3 * field + phase[3]) + phase[4])
        % 1
        * (len(colors) - 1)
    )
    idx = np.minimum(bands.astype(int), len(colors) - 2)
    frac = (bands - idx)[..., None]
    return np.uint8(colors[idx] * (1 - frac) + colors[idx + 1] * frac)


def _simulate_print(pattern, rng, width, height):
    """Run the field at working resolution, then sample it into the print rectangle."""
    scale = min(1, 580 / max(width, height))
    w, h = max(4, round(width * scale)), max(4, round(height * scale))
    y, x = np.mgrid[:h, :w].astype(np.float32)
    if pattern == "bismuth":
        # Each expanding rectangular crystal has a local axis, origin and nucleation time.
        time = np.full((h, w), np.inf)
        direction = np.zeros((h, w))
        for _ in range(26):
            cx, cy = rng.uniform([0, 0], [w, h])
            theta = rng.uniform(0, np.pi / 2)
            dx, dy = x - cx, y - cy
            arrival = np.maximum(
                abs(dx * np.cos(theta) + dy * np.sin(theta)),
                abs(dy * np.cos(theta) - dx * np.sin(theta)),
            ) + rng.uniform(0, 16)
            direction = np.where(arrival < time, theta, direction)
            time = np.minimum(time, arrival)
        terraces = np.floor(time / 4)
        hue = np.uint8((terraces * 6 + direction * 76) % 180)
        light = np.uint8(145 + 100 * np.minimum(1, (time % 4) / 1.4))
        image = cv2.cvtColor(
            np.stack((hue, np.full_like(hue, 170), light), -1), cv2.COLOR_HSV2RGB
        )
    elif pattern == "comb_marbling":
        palette = np.uint8(
            [
                [26, 56, 72],
                [53, 120, 125],
                [248, 214, 156],
                [188, 76, 77],
                [228, 148, 111],
            ]
        )
        image = np.full((h, w, 3), palette[0], np.uint8)
        for i in range(26):
            cx, cy = rng.uniform([0, 0], [w, h])
            radius = rng.uniform(0.035, 0.135) * min(w, h)
            dx, dy = x - cx, y - cy
            dist2 = np.maximum(1.0e-6, dx * dx + dy * dy)
            shrink = np.sqrt(np.maximum(0, 1 - radius**2 / dist2)).astype(np.float32)
            image = cv2.remap(
                image,
                np.float32(cx + dx * shrink),
                np.float32(cy + dy * shrink),
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT,
            )
            image[dist2 < radius**2] = palette[(i + 1) % len(palette)]
        spacing = max(4, min(w, h) / 8)
        offset = (y + spacing / 2) % spacing - spacing / 2
        shift = (
            np.exp(-abs(offset) / (0.25 * spacing))
            * np.cos(np.pi * np.round(y / spacing))
            * spacing
            * 1.3
        )
        image = cv2.remap(
            image,
            np.float32(x - shift),
            y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
    else:
        field = rng.normal(0, 0.1, (h, w)).astype(np.float32)
        phase = rng.uniform(0, 2 * np.pi)
        for step in range(80):
            # Select the local activator/inhibitor pair closest to equilibrium, then
            # transport its update through a smooth divergence-free velocity field.
            best_variation = np.full_like(field, np.inf)
            increment = np.zeros_like(field)
            for radius, gain in ((1.2, 0.018), (3.0, 0.038), (7.5, 0.075)):
                delta = cv2.GaussianBlur(field, (0, 0), radius) - cv2.GaussianBlur(
                    field, (0, 0), radius * 2.4
                )
                update = abs(delta) < best_variation
                increment[update] = np.sign(delta[update]) * gain
                best_variation = np.minimum(best_variation, abs(delta))
            mx = x + 0.48 * np.sin(y * 2 * np.pi / h + phase + step * 0.012)
            my = y + 0.48 * np.cos(x * 2 * np.pi / w + phase - step * 0.015)
            field = cv2.remap(
                field + increment,
                mx.astype(np.float32),
                my.astype(np.float32),
                cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT,
            )
        radius = np.hypot(x - w / 2, y - h / 2)
        theta = np.mod(np.arctan2(y - h / 2, x - w / 2), np.pi / 3)
        theta = np.minimum(theta, np.pi / 3 - theta)
        field = cv2.remap(
            field,
            np.float32(w / 2 + radius * np.cos(theta)),
            np.float32(h / 2 + radius * np.sin(theta)),
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        values = cv2.normalize(field, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)
        image = cv2.cvtColor(
            cv2.applyColorMap(values, cv2.COLORMAP_TWILIGHT), cv2.COLOR_BGR2RGB
        )
    return cv2.resize(image, (width, height), interpolation=cv2.INTER_CUBIC)
