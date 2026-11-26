"""Whimsy silhouette library, custom SVG/PNG loaders, VAE/PathNet adapters, and vector engraving text.

Every built-in whimsy provides:
- A clean, laser-safe closed exterior polygon (passes >= 3.0 mm neck erosion at standard puzzle scale).
- Optional separate interior engraving polylines (NEVER mixed into cut paths).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import shapely
import shapely.affinity
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient


@dataclass
class WhimsySpec:
    """Normalized whimsy geometry centered near (0, 0) with max extent = 1.0."""

    name: str
    category: str
    polygon: Polygon
    engrave_paths: List[List[Tuple[float, float]]] = field(default_factory=list)
    source: str = "builtin"


@dataclass
class WhimsyPlacement:
    """A whimsy placed in physical board millimetres."""

    name: str
    center_mm: Tuple[float, float]
    size_mm: float
    angle_deg: float = 0.0
    polygon: Polygon = field(default_factory=Polygon)
    engrave_paths: List[List[Tuple[float, float]]] = field(default_factory=list)
    category: str = "builtin"
    source: str = "builtin"


def _normalize_whimsy(
    poly: Polygon,
    engrave_paths: Sequence[Sequence[Tuple[float, float]]] = (),
) -> Tuple[Polygon, List[List[Tuple[float, float]]]]:
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.geom_type != "Polygon":
        parts = [p for p in shapely.get_parts(poly) if p.geom_type == "Polygon" and p.area > 0]
        if not parts:
            raise ValueError("Whimsy geometry produced no valid Polygon")
        poly = max(parts, key=lambda p: p.area)
    poly = orient(Polygon(poly.exterior), 1.0)
    x0, y0, x1, y1 = poly.bounds
    cx = 0.5 * (x0 + x1)
    cy = 0.5 * (y0 + y1)
    scale = max(x1 - x0, y1 - y0, 1e-9)
    inv = 1.0 / scale
    poly_n = shapely.affinity.affine_transform(poly, [inv, 0.0, 0.0, inv, -cx * inv, -cy * inv])
    paths_n: List[List[Tuple[float, float]]] = []
    for path in engrave_paths:
        pts = [((float(x) - cx) * inv, (float(y) - cy) * inv) for x, y in path]
        if len(pts) >= 2:
            paths_n.append(pts)
    return orient(poly_n, 1.0), paths_n


def _smooth_closed_curve(pts: np.ndarray, samples: int = 160) -> np.ndarray:
    """Periodic cubic B-spline interpolation for clean organic whimsy outlines."""
    p = np.asarray(pts, dtype=np.float64)
    n = len(p)
    out = []
    sub = max(3, int(round(samples / max(1, n))))
    t = np.linspace(0.0, 1.0, sub, endpoint=False)
    w0 = ((1.0 - t) ** 3) / 6.0
    w1 = (3.0 * t**3 - 6.0 * t**2 + 4.0) / 6.0
    w2 = (-3.0 * t**3 + 3.0 * t**2 + 3.0 * t + 1.0) / 6.0
    w3 = (t**3) / 6.0
    for i in range(n):
        p0 = p[(i - 1) % n]
        p1 = p[i % n]
        p2 = p[(i + 1) % n]
        p3 = p[(i + 2) % n]
        seg = w0[:, None] * p0 + w1[:, None] * p1 + w2[:, None] * p2 + w3[:, None] * p3
        out.append(seg)
    return np.vstack(out)


def _make_trilobite() -> WhimsySpec:
    """Recognizable fossil trilobite: broad cephalon, gently scalloped thorax, rounded pygidium.

    Cut boundary stays structurally strong (>= 3 mm necks at >= 42 mm length);
    cephalon glabella, eyes, axial furrows, and thoracic ribs are strictly engraving lines.
    """
    # Right half from front of cephalon (y = +0.50) down to tail pygidium (y = -0.50)
    right = [
        (0.00, 0.50),
        (0.16, 0.48),
        (0.28, 0.40),
        (0.35, 0.27),
        (0.37, 0.14),   # genal shoulder (broad, strong)
        (0.30, 0.10),   # cephalon-thorax transition
        (0.32, 0.03),   # thoracic pleura 1
        (0.28, -0.02),
        (0.31, -0.08),  # pleura 2
        (0.27, -0.13),
        (0.29, -0.19),  # pleura 3
        (0.25, -0.24),
        (0.26, -0.30),  # pleura 4
        (0.21, -0.36),
        (0.17, -0.43),  # pygidium flank
        (0.08, -0.49),
        (0.00, -0.51),
    ]
    left = [(-x, y) for x, y in reversed(right[1:-1])]
    ctrl = np.asarray(right + left, dtype=np.float64)
    poly = Polygon(_smooth_closed_curve(ctrl, samples=180))

    engrave: List[List[Tuple[float, float]]] = []
    # Glabella (central head lobe)
    theta = np.linspace(0.0, 2.0 * math.pi, 36)
    glabella = [(0.11 * math.cos(a), 0.30 + 0.13 * math.sin(a)) for a in theta]
    engrave.append(glabella)
    # Compound eye arcs
    for sgn in (-1.0, 1.0):
        eye_a = np.linspace(-0.7, 0.85, 16)
        engrave.append([(sgn * (0.20 + 0.045 * math.cos(a)), 0.27 + 0.075 * math.sin(a)) for a in eye_a])
    # Cephalic rear suture
    engrave.append([(-0.27, 0.14), (-0.11, 0.16), (0.11, 0.16), (0.27, 0.14)])
    # Axial furrows down the central spine
    for sgn in (-1.0, 1.0):
        engrave.append([
            (sgn * 0.10, 0.15),
            (sgn * 0.095, 0.0),
            (sgn * 0.085, -0.16),
            (sgn * 0.065, -0.32),
            (sgn * 0.03, -0.43),
        ])
    # Thoracic pleural ribs (6 curved bands)
    for y in (0.06, -0.01, -0.08, -0.15, -0.22, -0.29, -0.36):
        half_w = 0.25 - 0.18 * max(0.0, -y)
        xs = np.linspace(-half_w, half_w, 15)
        engrave.append([(float(x), float(y - 0.025 * (abs(x) / max(half_w, 1e-6)) ** 1.5)) for x in xs])

    poly_n, eng_n = _normalize_whimsy(poly, engrave)
    return WhimsySpec("trilobite", "fossil", poly_n, eng_n)


def _make_bacteriophage() -> WhimsySpec:
    """Iconic bacteriophage whimsy inspired by microbiology puzzles (robust cut + head facets engrave)."""
    # Build via union of sturdy buffered strokes + hexagonal capsid so all necks stay laser-safe!
    capsid = Polygon([
        (0.00, 0.48),
        (0.20, 0.34),
        (0.19, 0.10),
        (0.00, -0.02),
        (-0.19, 0.10),
        (-0.20, 0.34),
    ]).buffer(0.025, join_style="round")
    sheath = LineString([(0.0, 0.06), (0.0, -0.26)]).buffer(0.068, cap_style="round")
    baseplate = LineString([(-0.13, -0.24), (0.13, -0.24)]).buffer(0.052, cap_style="round")
    legs = []
    # 6 sturdy splayed tail fibers (3 per side) with wood-safe thickness
    for sgn in (-1.0, 1.0):
        legs.append(LineString([(sgn * 0.07, -0.20), (sgn * 0.25, -0.16), (sgn * 0.37, -0.26)]).buffer(0.050, cap_style="round", join_style="round"))
        legs.append(LineString([(sgn * 0.08, -0.24), (sgn * 0.24, -0.30), (sgn * 0.32, -0.43)]).buffer(0.050, cap_style="round", join_style="round"))
        legs.append(LineString([(sgn * 0.05, -0.27), (sgn * 0.13, -0.38), (sgn * 0.17, -0.47)]).buffer(0.050, cap_style="round", join_style="round"))
    poly = shapely.unary_union([capsid, sheath, baseplate, *legs]).buffer(0.012).buffer(-0.012)

    # Separate engraving lines: icosahedral capsid facets + helical sheath rings
    engrave = [
        [(-0.14, 0.31), (0.0, 0.22), (0.14, 0.31)],
        [(0.0, 0.44), (0.0, 0.22), (0.0, 0.03)],
        [(-0.14, 0.13), (0.0, 0.22), (0.14, 0.13)],
        [(-0.045, -0.06), (0.045, -0.06)],
        [(-0.045, -0.12), (0.045, -0.12)],
        [(-0.045, -0.18), (0.045, -0.18)],
    ]
    poly_n, eng_n = _normalize_whimsy(poly, engrave)
    return WhimsySpec("bacteriophage", "microbe", poly_n, eng_n)


def _make_diatom() -> WhimsySpec:
    """Scalloped oval diatom / microorganism whimsy with radial symmetry engraving."""
    t = np.linspace(0.0, 2.0 * math.pi, 180, endpoint=False)
    rx, ry = 0.30, 0.48
    # Gentle sturdy lobes (low amplitude relative to body width for strong necks)
    scallop = 1.0 + 0.085 * np.cos(10.0 * t)
    xs = rx * scallop * np.cos(t)
    ys = ry * (1.0 + 0.045 * np.cos(10.0 * t)) * np.sin(t)
    poly = Polygon(np.stack([xs, ys], axis=1))

    engrave = [
        [(0.0, -0.37), (0.0, 0.37)],
    ]
    for y in np.linspace(-0.28, 0.28, 7):
        w = 0.20 * math.sqrt(max(0.05, 1.0 - (y / 0.44) ** 2))
        engrave.append([(-w, float(y)), (-0.04, float(y))])
        engrave.append([(0.04, float(y)), (w, float(y))])
    # Center ring
    a = np.linspace(0.0, 2.0 * math.pi, 24)
    engrave.append([(0.045 * math.cos(v), 0.065 * math.sin(v)) for v in a])

    poly_n, eng_n = _normalize_whimsy(poly, engrave)
    return WhimsySpec("diatom", "microbe", poly_n, eng_n)


def _make_neuron() -> WhimsySpec:
    """Organic branching neuron/dendrite whimsy (inspired by McCabe circular puzzle)."""
    soma = Polygon([
        (0.14 * math.cos(a), 0.14 * math.sin(a))
        for a in np.linspace(0.0, 2.0 * math.pi, 36, endpoint=False)
    ])
    branches = []
    # 5 main curving dendrite arms + secondary forks, all wood-safe width
    arm_angles = [0.2, 1.45, 2.65, 3.85, 5.05]
    for idx, ang in enumerate(arm_angles):
        r1 = 0.23
        r2 = 0.44 if idx != 3 else 0.49
        bend = 0.22 * (1.0 if idx % 2 == 0 else -1.0)
        p0 = (0.0, 0.0)
        p1 = (r1 * math.cos(ang + bend * 0.5), r1 * math.sin(ang + bend * 0.5))
        p2 = (r2 * math.cos(ang + bend), r2 * math.sin(ang + bend))
        branches.append(LineString([p0, p1, p2]).buffer(0.055, cap_style="round", join_style="round"))
        # Secondary fork
        fa = ang + bend + (0.58 if idx % 2 == 0 else -0.58)
        p3 = (p1[0] + 0.19 * math.cos(fa), p1[1] + 0.19 * math.sin(fa))
        branches.append(LineString([p1, p3]).buffer(0.048, cap_style="round", join_style="round"))
    poly = shapely.unary_union([soma, *branches]).buffer(0.018).buffer(-0.018)

    a = np.linspace(0.0, 2.0 * math.pi, 28)
    engrave = [[(0.065 * math.cos(v), 0.065 * math.sin(v)) for v in a]]
    poly_n, eng_n = _normalize_whimsy(poly, engrave)
    return WhimsySpec("neuron", "biology", poly_n, eng_n)


def _make_ammonite() -> WhimsySpec:
    """Fossil ammonite shell whimsy with engraved logarithmic spiral and septa."""
    # Outer shell profile: compact coiled spiral disc with prominent aperture lip
    t = np.linspace(0.0, 2.0 * math.pi, 140, endpoint=False)
    r = 0.34 + 0.11 * (t / (2.0 * math.pi)) + 0.015 * np.sin(14.0 * t)
    xs = r * np.cos(t)
    ys = r * np.sin(t)
    poly = Polygon(np.stack([xs, ys], axis=1)).buffer(0.01).buffer(-0.01)

    # Engraved logarithmic spiral + chamber septa
    ts = np.linspace(0.4, 4.6 * math.pi, 140)
    rs = 0.04 * np.exp(0.155 * ts)
    engrave = [[(float(rv * math.cos(tv)), float(rv * math.sin(tv))) for tv, rv in zip(ts, rs)]]
    for tv in np.linspace(1.6 * math.pi, 4.4 * math.pi, 14):
        r_in = 0.04 * np.exp(0.155 * (tv - 2.0 * math.pi))
        r_out = 0.04 * np.exp(0.155 * tv)
        if r_out < 0.40:
            engrave.append([
                (float(r_in * math.cos(tv)), float(r_in * math.sin(tv))),
                (float(0.5 * (r_in + r_out) * math.cos(tv + 0.14)), float(0.5 * (r_in + r_out) * math.sin(tv + 0.14))),
                (float(r_out * math.cos(tv)), float(r_out * math.sin(tv))),
            ])
    poly_n, eng_n = _normalize_whimsy(poly, engrave)
    return WhimsySpec("ammonite", "fossil", poly_n, eng_n)


def _make_nordic_ship() -> WhimsySpec:
    """Sturdy Bronze-Age Nordic rock-art ship whimsy (clean-room vector + crew stroke engrave)."""
    hull = LineString([
        (-0.44, 0.14),
        (-0.34, -0.06),
        (-0.16, -0.14),
        (0.16, -0.14),
        (0.34, -0.06),
        (0.44, 0.14),
    ]).buffer(0.075, cap_style="round", join_style="round")
    prow_l = LineString([(-0.42, 0.12), (-0.45, 0.24), (-0.36, 0.27)]).buffer(0.052, cap_style="round", join_style="round")
    prow_r = LineString([(0.42, 0.12), (0.45, 0.24), (0.36, 0.27)]).buffer(0.052, cap_style="round", join_style="round")
    keel = LineString([(-0.36, -0.11), (0.0, -0.18), (0.36, -0.11)]).buffer(0.060, cap_style="round")
    poly = shapely.unary_union([hull, prow_l, prow_r, keel]).buffer(0.015).buffer(-0.015)

    engrave = [
        [(-0.31, -0.05), (0.0, -0.10), (0.31, -0.05)],
    ]
    for x in np.linspace(-0.22, 0.22, 7):
        engrave.append([(float(x), -0.03), (float(x), 0.04)])
    poly_n, eng_n = _normalize_whimsy(poly, engrave)
    return WhimsySpec("nordic_ship", "helleristning", poly_n, eng_n)


def _make_star_crystal() -> WhimsySpec:
    """Six-fold hexagonal snow-crystal star whimsy with engraved crystalline axes."""
    arms = []
    for k in range(6):
        a = k * math.pi / 3.0
        p1 = (0.44 * math.cos(a), 0.44 * math.sin(a))
        arms.append(LineString([(0.0, 0.0), p1]).buffer(0.062, cap_style="round"))
        # Short broad side chevrons
        mid = (0.24 * math.cos(a), 0.24 * math.sin(a))
        for da in (-0.85, 0.85):
            tip = (mid[0] + 0.13 * math.cos(a + da), mid[1] + 0.13 * math.sin(a + da))
            arms.append(LineString([mid, tip]).buffer(0.048, cap_style="round"))
    core = Polygon([
        (0.18 * math.cos(a), 0.18 * math.sin(a))
        for a in np.linspace(0.0, 2.0 * math.pi, 18, endpoint=False)
    ])
    poly = shapely.unary_union([core, *arms]).buffer(0.015).buffer(-0.015)

    engrave = []
    for k in range(3):
        a = k * math.pi / 3.0
        engrave.append([
            (-0.35 * math.cos(a), -0.35 * math.sin(a)),
            (0.35 * math.cos(a), 0.35 * math.sin(a)),
        ])
    poly_n, eng_n = _normalize_whimsy(poly, engrave)
    return WhimsySpec("star_crystal", "crystal", poly_n, eng_n)


_BUILTIN_BUILDERS = {
    "trilobite": _make_trilobite,
    "bacteriophage": _make_bacteriophage,
    "diatom": _make_diatom,
    "neuron": _make_neuron,
    "ammonite": _make_ammonite,
    "nordic_ship": _make_nordic_ship,
    "star_crystal": _make_star_crystal,
}

_BUILTIN_CACHE: Dict[str, WhimsySpec] = {}


def list_builtin_whimsies() -> List[str]:
    return sorted(_BUILTIN_BUILDERS.keys())


def get_builtin_whimsy(name: str) -> WhimsySpec:
    key = name.strip().lower()
    if key not in _BUILTIN_BUILDERS:
        raise ValueError(f"Unknown built-in whimsy {name!r}; expected one of {list_builtin_whimsies()}")
    if key not in _BUILTIN_CACHE:
        _BUILTIN_CACHE[key] = _BUILTIN_BUILDERS[key]()
    return _BUILTIN_CACHE[key]


def load_whimsy_from_svg(svg_path: str | Path, name: Optional[str] = None) -> WhimsySpec:
    """Load a whimsy silhouette from an SVG file using heller_jigsaw's SVG parser."""
    from heller_jigsaw.geometry.outline import from_svg

    p = Path(svg_path)
    poly = from_svg(p, width_mm=100.0)
    poly_n, _ = _normalize_whimsy(poly)
    return WhimsySpec(name=name or p.stem, category="custom_svg", polygon=poly_n, source=str(p))


def load_whimsy_from_image(image_path: str | Path, name: Optional[str] = None, threshold: int = 128) -> WhimsySpec:
    """Load a single-shape black-and-white silhouette PNG/JPG as a whimsy."""
    p = Path(image_path)
    img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"Could not read silhouette image: {p}")
    # Treat dark pixels on light background (or light on dark if majority dark) as foreground
    fg = img < threshold if float((img < threshold).mean()) < 0.5 else img >= threshold
    fg_u8 = fg.astype(np.uint8) * 255
    contours, _ = cv2.findContours(fg_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        raise ValueError(f"No foreground contour found in {p}")
    cnt = max(contours, key=cv2.contourArea).reshape(-1, 2).astype(np.float64)
    # Flip Y so image-top maps to +Y or keep consistent screen-Y coordinates
    poly = Polygon(cnt).simplify(1.0, preserve_topology=True)
    poly_n, _ = _normalize_whimsy(poly)
    return WhimsySpec(name=name or p.stem, category="custom_image", polygon=poly_n, source=str(p))


def load_vae_whimsies(
    checkpoint: str | Path,
    count: int = 2,
    classes: Optional[Sequence[str]] = None,
    seed: int = 42,
) -> List[WhimsySpec]:
    """Sample neural motif whimsies from an existing trained AnimalShapeVAE checkpoint."""
    from heller_jigsaw.models.shape_prior import sample_motifs

    motifs = sample_motifs(checkpoint, count=count, classes=list(classes) if classes else None, seed=seed)
    specs: List[WhimsySpec] = []
    for m in motifs:
        fg = (m.mask > 0.5).astype(np.uint8) * 255
        contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea).reshape(-1, 2).astype(np.float64)
        poly = Polygon(cnt).simplify(1.2, preserve_topology=True)
        # Ensure laser-friendly smoothness
        poly = poly.buffer(2.0).buffer(-2.0)
        poly_n, _ = _normalize_whimsy(poly)
        specs.append(
            WhimsySpec(
                name=f"vae_{m.motif_class}",
                category=f"vae:{m.family}",
                polygon=poly_n,
                source=str(checkpoint),
            )
        )
    return specs


def load_pathnet_whimsies(
    checkpoint: str | Path = "pathnet/pathnet.pt",
    count: int = 2,
    seed: int = 22,
    logo_only: bool = True,
) -> List[WhimsySpec]:
    """Decode neural whimsies directly from the trained APHEX PathNet checkpoint (read-only)."""
    import torch
    from torch import nn

    ckpt_path = Path(checkpoint)
    if not ckpt_path.is_file():
        raise FileNotFoundError(f"PathNet checkpoint not found: {ckpt_path}")
    ck = torch.load(ckpt_path, weights_only=True, map_location="cpu")
    names = ck["names"]
    n_pts = int(ck["points"])
    z_dim = int(ck["latent"])

    class _PathNetDecoder(nn.Module):
        def __init__(self, k: int):
            super().__init__()
            self.emb = nn.Embedding(k, 48)
            self.net = nn.Sequential(
                nn.Linear(48 + z_dim, 192),
                nn.SiLU(),
                nn.Linear(192, 192),
                nn.SiLU(),
                nn.Linear(192, n_pts * 2),
            )

        def forward(self, k_t, z_t):
            return self.net(torch.cat([self.emb(k_t), z_t], dim=1)).reshape(-1, n_pts, 2)

    model = _PathNetDecoder(len(names))
    model.load_state_dict(ck["state"])
    model.eval()

    rng = np.random.default_rng(seed)
    specs: List[WhimsySpec] = []
    for i in range(count):
        k_idx = 0 if logo_only else int(rng.integers(0, len(names)))
        for _ in range(40):
            z = rng.uniform(-0.22, 0.22, z_dim) if k_idx == 0 else rng.uniform(-0.85, 0.85, z_dim)
            with torch.inference_mode():
                q = model(
                    torch.tensor([k_idx], dtype=torch.long),
                    torch.tensor(z[None], dtype=torch.float32),
                )[0].numpy()
            pts = _smooth_closed_curve(q, samples=n_pts * 3)
            poly = Polygon(pts)
            if poly.is_valid and poly.area > 0.03:
                poly_n, _ = _normalize_whimsy(poly)
                specs.append(
                    WhimsySpec(
                        name=f"pathnet_{names[k_idx].lower().replace(' ', '_')}_{i + 1}",
                        category="pathnet",
                        polygon=poly_n,
                        source=str(ckpt_path),
                    )
                )
                break
    return specs


def build_whimsy_geometry(
    spec: WhimsySpec,
    center_mm: Tuple[float, float],
    size_mm: float,
    angle_deg: float = 0.0,
) -> WhimsyPlacement:
    """Place a normalized WhimsySpec into physical board millimetres."""
    rad = math.radians(angle_deg)
    ca, sa = math.cos(rad), math.sin(rad)
    cx, cy = float(center_mm[0]), float(center_mm[1])
    s = float(size_mm)
    # Note: screen/SVG Y grows downward; flip normalized +Y -> -Y so head faces upward cleanly
    poly = shapely.affinity.affine_transform(
        spec.polygon,
        [s * ca, s * sa, s * sa, -s * ca, cx, cy],
    )
    poly = orient(Polygon(poly.exterior), 1.0)
    engrave_mm: List[List[Tuple[float, float]]] = []
    for path in spec.engrave_paths:
        pts_mm = [
            (
                cx + s * (ca * x + sa * y),
                cy + s * (sa * x - ca * y),
            )
            for x, y in path
        ]
        if len(pts_mm) >= 2:
            engrave_mm.append(pts_mm)
    return WhimsyPlacement(
        name=spec.name,
        center_mm=(cx, cy),
        size_mm=s,
        angle_deg=angle_deg,
        polygon=poly,
        engrave_paths=engrave_mm,
        category=spec.category,
        source=spec.source,
    )


# ---------------------------------------------------------------------------
# Clean single-stroke vector engraving font (zero external font dependencies)
# ---------------------------------------------------------------------------

_STROKE_FONT: Dict[str, List[List[Tuple[float, float]]]] = {
    "A": [[(0.0, 1.0), (0.35, 0.0), (0.7, 1.0)], [(0.14, 0.65), (0.56, 0.65)]],
    "B": [[(0.0, 0.0), (0.0, 1.0), (0.48, 1.0), (0.62, 0.88), (0.62, 0.62), (0.45, 0.5), (0.0, 0.5)],
          [(0.45, 0.5), (0.58, 0.38), (0.58, 0.12), (0.45, 0.0), (0.0, 0.0)]],
    "C": [[(0.65, 0.2), (0.5, 0.0), (0.2, 0.0), (0.0, 0.22), (0.0, 0.78), (0.2, 1.0), (0.5, 1.0), (0.65, 0.8)]],
    "D": [[(0.0, 0.0), (0.0, 1.0), (0.42, 1.0), (0.65, 0.78), (0.65, 0.22), (0.42, 0.0), (0.0, 0.0)]],
    "E": [[(0.6, 0.0), (0.0, 0.0), (0.0, 1.0), (0.6, 1.0)], [(0.0, 0.5), (0.48, 0.5)]],
    "F": [[(0.6, 0.0), (0.0, 0.0), (0.0, 1.0)], [(0.0, 0.5), (0.48, 0.5)]],
    "G": [[(0.65, 0.2), (0.5, 0.0), (0.2, 0.0), (0.0, 0.22), (0.0, 0.78), (0.2, 1.0), (0.52, 1.0), (0.65, 0.8), (0.65, 0.52), (0.36, 0.52)]],
    "H": [[(0.0, 0.0), (0.0, 1.0)], [(0.65, 0.0), (0.65, 1.0)], [(0.0, 0.5), (0.65, 0.5)]],
    "I": [[(0.15, 0.0), (0.45, 0.0)], [(0.3, 0.0), (0.3, 1.0)], [(0.15, 1.0), (0.45, 1.0)]],
    "J": [[(0.55, 0.0), (0.55, 0.78), (0.38, 1.0), (0.15, 1.0), (0.0, 0.82)]],
    "K": [[(0.0, 0.0), (0.0, 1.0)], [(0.62, 0.0), (0.0, 0.55), (0.65, 1.0)]],
    "L": [[(0.0, 0.0), (0.0, 1.0), (0.6, 1.0)]],
    "M": [[(0.0, 1.0), (0.0, 0.0), (0.35, 0.55), (0.7, 0.0), (0.7, 1.0)]],
    "N": [[(0.0, 1.0), (0.0, 0.0), (0.65, 1.0), (0.65, 0.0)]],
    "O": [[(0.2, 0.0), (0.45, 0.0), (0.65, 0.22), (0.65, 0.78), (0.45, 1.0), (0.2, 1.0), (0.0, 0.78), (0.0, 0.22), (0.2, 0.0)]],
    "P": [[(0.0, 1.0), (0.0, 0.0), (0.48, 0.0), (0.62, 0.14), (0.62, 0.42), (0.48, 0.55), (0.0, 0.55)]],
    "Q": [[(0.2, 0.0), (0.45, 0.0), (0.65, 0.22), (0.65, 0.78), (0.45, 1.0), (0.2, 1.0), (0.0, 0.78), (0.0, 0.22), (0.2, 0.0)],
          [(0.4, 0.7), (0.7, 1.05)]],
    "R": [[(0.0, 1.0), (0.0, 0.0), (0.48, 0.0), (0.62, 0.14), (0.62, 0.42), (0.48, 0.55), (0.0, 0.55)],
          [(0.38, 0.55), (0.65, 1.0)]],
    "S": [[(0.62, 0.16), (0.48, 0.0), (0.18, 0.0), (0.02, 0.16), (0.02, 0.36), (0.2, 0.48), (0.46, 0.54), (0.64, 0.68), (0.64, 0.85), (0.48, 1.0), (0.16, 1.0), (0.0, 0.84)]],
    "T": [[(0.0, 0.0), (0.7, 0.0)], [(0.35, 0.0), (0.35, 1.0)]],
    "U": [[(0.0, 0.0), (0.0, 0.78), (0.2, 1.0), (0.45, 1.0), (0.65, 0.78), (0.65, 0.0)]],
    "V": [[(0.0, 0.0), (0.35, 1.0), (0.7, 0.0)]],
    "W": [[(0.0, 0.0), (0.18, 1.0), (0.38, 0.45), (0.58, 1.0), (0.76, 0.0)]],
    "X": [[(0.0, 0.0), (0.65, 1.0)], [(0.65, 0.0), (0.0, 1.0)]],
    "Y": [[(0.0, 0.0), (0.35, 0.5), (0.7, 0.0)], [(0.35, 0.5), (0.35, 1.0)]],
    "Z": [[(0.0, 0.0), (0.65, 0.0), (0.0, 1.0), (0.65, 1.0)]],
    "0": [[(0.2, 0.0), (0.45, 0.0), (0.62, 0.22), (0.62, 0.78), (0.45, 1.0), (0.2, 1.0), (0.0, 0.78), (0.0, 0.22), (0.2, 0.0)]],
    "1": [[(0.15, 0.2), (0.35, 0.0), (0.35, 1.0)], [(0.15, 1.0), (0.55, 1.0)]],
    "2": [[(0.02, 0.22), (0.2, 0.0), (0.46, 0.0), (0.62, 0.18), (0.62, 0.36), (0.0, 1.0), (0.64, 1.0)]],
    "3": [[(0.04, 0.15), (0.2, 0.0), (0.46, 0.0), (0.62, 0.16), (0.62, 0.34), (0.42, 0.48), (0.24, 0.48)],
          [(0.42, 0.48), (0.64, 0.64), (0.64, 0.84), (0.46, 1.0), (0.18, 1.0), (0.02, 0.84)]],
    "4": [[(0.5, 1.0), (0.5, 0.0), (0.0, 0.68), (0.66, 0.68)]],
    "5": [[(0.6, 0.0), (0.04, 0.0), (0.04, 0.44), (0.46, 0.44), (0.64, 0.6), (0.64, 0.84), (0.46, 1.0), (0.16, 1.0), (0.0, 0.84)]],
    "6": [[(0.58, 0.12), (0.44, 0.0), (0.2, 0.0), (0.02, 0.25), (0.02, 0.8), (0.2, 1.0), (0.46, 1.0), (0.62, 0.82), (0.62, 0.62), (0.46, 0.48), (0.02, 0.48)]],
    "7": [[(0.0, 0.0), (0.64, 0.0), (0.22, 1.0)]],
    "8": [[(0.2, 0.48), (0.04, 0.34), (0.04, 0.14), (0.2, 0.0), (0.44, 0.0), (0.6, 0.14), (0.6, 0.34), (0.44, 0.48), (0.2, 0.48), (0.02, 0.64), (0.02, 0.84), (0.2, 1.0), (0.44, 1.0), (0.62, 0.84), (0.62, 0.64), (0.44, 0.48)]],
    "9": [[(0.62, 0.5), (0.18, 0.5), (0.02, 0.34), (0.02, 0.16), (0.18, 0.0), (0.44, 0.0), (0.62, 0.18), (0.62, 0.78), (0.44, 1.0), (0.18, 1.0), (0.04, 0.88)]],
    "-": [[(0.1, 0.5), (0.55, 0.5)]],
    ".": [[(0.28, 0.92), (0.34, 0.92), (0.34, 0.98), (0.28, 0.98), (0.28, 0.92)]],
    "/": [[(0.05, 1.0), (0.6, 0.0)]],
    " ": [],
}


def make_engrave_text_paths(
    text: str,
    center_mm: Tuple[float, float],
    height_mm: float = 5.5,
    angle_deg: float = 0.0,
) -> List[List[Tuple[float, float]]]:
    """Render single-stroke laser-engrave vector polylines for short inscriptions."""
    lines = [ln.strip().upper() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    rad = math.radians(angle_deg)
    ca, sa = math.cos(rad), math.sin(rad)
    cx, cy = float(center_mm[0]), float(center_mm[1])
    char_adv = 0.92 * height_mm
    line_adv = 1.45 * height_mm
    total_h = (len(lines) - 1) * line_adv + height_mm
    paths: List[List[Tuple[float, float]]] = []
    for li, line in enumerate(lines):
        total_w = max(0.0, (len(line) - 1) * char_adv + 0.68 * height_mm)
        y_off = -0.5 * total_h + li * line_adv
        for ci, ch in enumerate(line):
            strokes = _STROKE_FONT.get(ch, [])
            x_off = -0.5 * total_w + ci * char_adv
            for st in strokes:
                pts = []
                for gx, gy in st:
                    lx = x_off + gx * height_mm
                    ly = y_off + gy * height_mm
                    pts.append((cx + ca * lx - sa * ly, cy + sa * lx + ca * ly))
                if len(pts) >= 2:
                    paths.append(pts)
    return paths
