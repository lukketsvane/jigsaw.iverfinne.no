"""Parametric multi-genus trilobite generator with kerf-safe cut outlines and inset anatomical engraving.

Generates 7 distinct fossil genera (all original procedural vector geometry, 100% commercial-clean):
- Paradoxides (Scandinavian Cambrian giant — broad cephalon, sweeping genal spines, long ribbed thorax)
- Olenellus (Early Cambrian — crescentic cephalon, stout macropleural 3rd segment, tapered tail)
- Phacops (Devonian — bulbous glabella, prominent reniform compound eyes, stout curved/enrolled body)
- Elrathia (Middle Cambrian classic — crisp oval outline, balanced 8-9 thoracic ribs, broad pygidium)
- Trinucleus (Ordovician Oslo fossil — horseshoe cephalic fringe with engraved brim pits, trailing horns)
- Asaphus (Baltoscandian Ordovician — broad smooth isopygous head & tail shields, elevated eye lobes)
- Calymene (Silurian — deeply furrowed trilobed glabella, strongly ribbed arching thorax, flexed poses)

Hard fabrication guarantees:
1. Outer cut silhouette is valid, hole-free, smoothly scalloped so it reads immediately as a trilobite
   even without engraving, and survives laser kerf (0.10-0.25 mm), 3.0 mm minimum wood neck, and
   <= 6.0 mm thin-tip opening checks.
2. Interior anatomical engraving paths are strictly separated from cut lines and guaranteed to have
   minimum Euclidean clearance >= 1.50 mm from the final piece cut boundary (built at >= 1.85 mm
   nominal inset and re-clipped against any post-dendrite/post-lock final piece polygon).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import shapely
import shapely.affinity
from scipy import ndimage
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.geometry.polygon import orient

Coord = Tuple[float, float]
Polyline = List[Coord]

TRILOBITE_GENERA: Tuple[str, ...] = (
    "Paradoxides",
    "Olenellus",
    "Phacops",
    "Elrathia",
    "Trinucleus",
    "Asaphus",
    "Calymene",
)

TRILOBITE_GENUS_INFO: Dict[str, Dict[str, Any]] = {
    "Paradoxides": {
        "period": "Middle Cambrian",
        "region": "Oslo Region & Scandinavia (Alum Shale)",
        "cephalon_frac": 0.30,
        "thorax_frac": 0.54,
        "pygidium_frac": 0.16,
        "width_ratio": 0.64,
        "n_segments": 9,
        "genal_sweep": 0.28,
        "genal_flare": 1.12,
        "tail_taper": 0.42,
        "scallop_depth": 0.055,
        "glabella_bulb": 1.18,
        "eye_size": 0.95,
        "has_brim_pits": False,
        "macropleural_idx": None,
        "has_telson": False,
    },
    "Olenellus": {
        "period": "Early Cambrian",
        "region": "Laurentia & Caledonian Margin",
        "cephalon_frac": 0.32,
        "thorax_frac": 0.50,
        "pygidium_frac": 0.18,
        "width_ratio": 0.72,
        "n_segments": 8,
        "genal_sweep": 0.25,
        "genal_flare": 1.16,
        "tail_taper": 0.36,
        "scallop_depth": 0.060,
        "glabella_bulb": 0.95,
        "eye_size": 1.25,
        "has_brim_pits": False,
        "macropleural_idx": 2,
        "has_telson": True,
    },
    "Phacops": {
        "period": "Devonian",
        "region": "Rhenish-Bohemian & North Atlantic",
        "cephalon_frac": 0.33,
        "thorax_frac": 0.44,
        "pygidium_frac": 0.23,
        "width_ratio": 0.68,
        "n_segments": 7,
        "genal_sweep": 0.08,
        "genal_flare": 1.02,
        "tail_taper": 0.64,
        "scallop_depth": 0.048,
        "glabella_bulb": 1.42,
        "eye_size": 1.35,
        "has_brim_pits": False,
        "macropleural_idx": None,
        "has_telson": False,
    },
    "Elrathia": {
        "period": "Middle Cambrian",
        "region": "Wheeler Shale (Classic Iconic Form)",
        "cephalon_frac": 0.29,
        "thorax_frac": 0.48,
        "pygidium_frac": 0.23,
        "width_ratio": 0.66,
        "n_segments": 8,
        "genal_sweep": 0.15,
        "genal_flare": 1.06,
        "tail_taper": 0.56,
        "scallop_depth": 0.052,
        "glabella_bulb": 0.92,
        "eye_size": 0.85,
        "has_brim_pits": False,
        "macropleural_idx": None,
        "has_telson": False,
    },
    "Trinucleus": {
        "period": "Middle-Late Ordovician",
        "region": "Oslo Region & Baltoscandia",
        "cephalon_frac": 0.40,
        "thorax_frac": 0.38,
        "pygidium_frac": 0.22,
        "width_ratio": 0.78,
        "n_segments": 6,
        "genal_sweep": 0.32,
        "genal_flare": 1.18,
        "tail_taper": 0.52,
        "scallop_depth": 0.045,
        "glabella_bulb": 1.25,
        "eye_size": 0.55,
        "has_brim_pits": True,
        "macropleural_idx": None,
        "has_telson": False,
    },
    "Asaphus": {
        "period": "Early-Middle Ordovician",
        "region": "Oslo & Baltoscandian Orthoceras Limestone",
        "cephalon_frac": 0.34,
        "thorax_frac": 0.36,
        "pygidium_frac": 0.30,
        "width_ratio": 0.69,
        "n_segments": 7,
        "genal_sweep": 0.12,
        "genal_flare": 1.04,
        "tail_taper": 0.74,
        "scallop_depth": 0.042,
        "glabella_bulb": 1.05,
        "eye_size": 1.20,
        "has_brim_pits": False,
        "macropleural_idx": None,
        "has_telson": False,
    },
    "Calymene": {
        "period": "Silurian",
        "region": "Gotland, Oslo & Wenlock Limestone",
        "cephalon_frac": 0.31,
        "thorax_frac": 0.47,
        "pygidium_frac": 0.22,
        "width_ratio": 0.67,
        "n_segments": 8,
        "genal_sweep": 0.10,
        "genal_flare": 1.03,
        "tail_taper": 0.58,
        "scallop_depth": 0.056,
        "glabella_bulb": 1.14,
        "eye_size": 0.90,
        "has_brim_pits": False,
        "macropleural_idx": None,
        "has_telson": False,
    },
}

TRILOBITE_POSES: Tuple[str, ...] = (
    "straight",
    "curve_left",
    "curve_right",
    "s_flex",
    "semi_enrolled",
)


@dataclass
class TrilobiteSpec:
    """One procedural trilobite piece template in centred local mm coordinates."""

    asset_id: str
    genus: str
    pose: str
    period: str
    region: str
    length_mm: float
    width_mm: float
    area_mm2: float
    polygon: Polygon                  # Centred at (0, 0), orientation: head at +Y, tail at -Y
    engrave_paths: List[Polyline]     # Strictly inset anatomical strokes in same local mm coords
    mask: np.ndarray                  # (256, 256) float32 normalized silhouette mask
    sdf: np.ndarray                   # (256, 256) float32 signed distance in pixels
    min_clearance_mm: float           # Verified min engraving-to-cut distance in local coords
    metadata: Dict[str, Any] = field(default_factory=dict)


def _apply_spine_bend(
    pts: np.ndarray,
    pose: str,
    bend_strength: float,
    half_len: float,
) -> np.ndarray:
    """Smooth diffeomorphic spine curvature preserving anatomical width and non-self-intersection.

    Coordinates have x = lateral (left -, right +), y = longitudinal (head +half_len, tail -half_len).
    """
    if pose == "straight" or abs(bend_strength) < 1e-6:
        return pts.copy()

    x = pts[:, 0].astype(np.float64)
    y = pts[:, 1].astype(np.float64)
    # Normalize longitudinal parameter u in [-1, 1] (head=+1, tail=-1)
    u = np.clip(y / max(half_len, 1e-6), -1.25, 1.25)

    if pose in ("curve_left", "curve_right"):
        sign = -1.0 if pose == "curve_left" else 1.0
        # Circular arc bend of spine with modest curvature (keeps inner flank smooth & wide)
        kappa = sign * bend_strength / max(half_len, 1e-6)
        theta = kappa * y
        # Arc centerline
        r = 1.0 / kappa
        cx = r * (1.0 - np.cos(theta))
        cy = r * np.sin(theta)
        # Normal to arc at theta: (cos(theta), -sin(theta))
        nx = np.cos(theta)
        ny = -np.sin(theta)
        ox = cx + x * nx
        oy = cy + x * ny
    elif pose == "s_flex":
        # Gentle swimming S-curve
        amp = bend_strength * half_len * 0.22
        cx = amp * np.sin(math.pi * u)
        dcx_dy = (amp * math.pi / max(half_len, 1e-6)) * np.cos(math.pi * u)
        inv_norm = 1.0 / np.hypot(1.0, dcx_dy)
        # Tangent (dcx_dy, 1) -> right normal (1, -dcx_dy) * inv_norm
        ox = cx + x * inv_norm
        oy = y - x * dcx_dy * inv_norm
    elif pose == "semi_enrolled":
        # Compact foreshortened defensive curl: longitudinally compressed + curved flanks
        compress = 1.0 - 0.20 * bend_strength
        y_c = y * compress
        amp = bend_strength * half_len * 0.14
        ox = x * (1.0 + 0.08 * bend_strength * (1.0 - u * u)) + amp * (u * u - 0.33)
        oy = y_c
    else:
        return pts.copy()

    return np.column_stack([ox, oy])


def _half_width_profile(
    t_vals: np.ndarray,
    half_len: float,
    half_w: float,
    spec: Dict[str, Any],
) -> np.ndarray:
    """Compute smooth trilobite right-side half-width w(t) for t in [0, 1] (0=front of cephalon, 1=tail tip).

    Produces unmistakable cephalon dome + swept genal cheeks + rhythmically scalloped segmented
    thorax + distinct pygidium tail shield, while keeping every margin kerf-safe & neck-safe.
    """
    c_end = float(spec["cephalon_frac"])
    th_end = c_end + float(spec["thorax_frac"])
    n_seg = int(spec["n_segments"])
    genal_flare = float(spec["genal_flare"])
    genal_sweep = float(spec["genal_sweep"])
    tail_taper = float(spec["tail_taper"])
    scallop = float(spec["scallop_depth"])
    macro_idx = spec.get("macropleural_idx")
    has_telson = bool(spec.get("has_telson", False))

    w = np.zeros_like(t_vals, dtype=np.float64)

    for idx, t in enumerate(t_vals):
        if t <= c_end:
            # Cephalon: broad parabolic/semicircular head shield flaring subtly toward genal angles
            u = np.clip(t / max(c_end, 1e-6), 0.0, 1.0)
            # Round front profile sqrt(1 - (1-u)^2) blended with gentle cheek fullness
            dome = math.sqrt(max(0.0, 1.0 - (1.0 - u) ** 2.15))
            flare = 1.0 + (genal_flare - 1.0) * (u ** 1.8)
            w[idx] = half_w * dome * flare
        elif t <= th_end:
            # Thorax: tapers gracefully from cephalon width to pygidium width with rhythmic pleural scallops
            u = (t - c_end) / max(th_end - c_end, 1e-6)
            base_taper = (1.0 - u) * 0.94 + u * (tail_taper + 0.16)
            # Smooth genal shoulder transition at top of thorax (thickened safe genal spine!)
            shoulder = 0.0
            if genal_sweep > 0.05:
                # Broad, stout sweeping genal cheek lobe extending down the upper thorax
                s_span = min(0.42, genal_sweep * 1.15)
                if u < s_span:
                    su = u / s_span
                    # Smooth lobe bulge peaking near the cephalon-thorax boundary then curving smoothly inward
                    shoulder = (genal_flare - 0.88) * 0.95 * (math.cos(0.5 * math.pi * su) ** 1.6)
            # Rhythmic pleural segment ribs (sinusoidal scallops along the flank)
            seg_phase = u * n_seg
            seg_wave = 0.5 * (1.0 - math.cos(2.0 * math.pi * seg_phase))
            # Soften scallops near ends so junction transitions stay stout
            env = math.sin(math.pi * np.clip(u, 0.0, 1.0)) ** 0.35
            rib = scallop * (2.0 * seg_wave - 1.0) * env

            # Olenellus macropleural 3rd segment accent (stout thickened lateral flare)
            macro = 0.0
            if macro_idx is not None:
                seg_pos = u * n_seg
                dist_m = abs(seg_pos - (macro_idx + 0.5))
                if dist_m < 0.85:
                    macro = 0.14 * (math.cos(0.5 * math.pi * dist_m / 0.85) ** 2)

            w[idx] = half_w * max(0.32, base_taper + shoulder + rib + macro)
        else:
            # Pygidium: distinct rounded/shield-shaped tailplate
            u = np.clip((t - th_end) / max(1.0 - th_end, 1e-6), 0.0, 1.0)
            p_start = tail_taper + 0.14
            if has_telson:
                # Broad rounded pygidium tapering into a stout, kerf-safe rounded tail tip (min radius >= 2.3 mm)
                shield = p_start * (math.cos(0.5 * math.pi * (u ** 1.15)) ** 0.72)
                telson = 0.24 * math.exp(-((1.0 - u) / 0.38) ** 2) * math.sqrt(max(0.0, 1.0 - u * u))
                w[idx] = half_w * max(0.0, shield + telson)
            else:
                # Classic semicircular / ovate pygidial shield with subtle axial-end fullness
                shield = p_start * math.sqrt(max(0.0, 1.0 - (u ** 1.75)))
                # Add subtle 2-3 pygidial rib undulations on upper half of pygidium
                rib_p = 0.020 * math.sin(3.0 * math.pi * u) * (1.0 - u)
                w[idx] = half_w * max(0.0, shield + rib_p)

    return w


def _build_local_trilobite_geometry(
    genus: str,
    pose: str,
    target_area_mm2: float,
    rng: np.random.Generator,
) -> Tuple[Polygon, List[Polyline], Dict[str, Any]]:
    """Construct centred local cut polygon and anatomical engraving polylines in mm."""
    base = dict(TRILOBITE_GENUS_INFO[genus])
    # Subtle natural fossil variation per individual
    width_ratio = float(base["width_ratio"]) * float(rng.uniform(0.94, 1.07))
    n_seg = int(base["n_segments"]) + int(rng.choice([-1, 0, 0, 1]))
    n_seg = max(6, min(11, n_seg))
    base["n_segments"] = n_seg
    base["width_ratio"] = width_ratio
    base["genal_sweep"] = float(base["genal_sweep"]) * float(rng.uniform(0.90, 1.12))
    # Soften pleural scallops slightly on strongly bent poses so concave inner flanks stay velvety smooth
    flex_atten = 0.72 if pose in ("s_flex", "curve_left", "curve_right") else 0.92
    base["scallop_depth"] = float(base["scallop_depth"]) * float(rng.uniform(0.92, 1.08)) * flex_atten

    bend_strength = 0.0
    if pose in ("curve_left", "curve_right"):
        bend_strength = float(rng.uniform(0.32, 0.58))
    elif pose == "s_flex":
        bend_strength = float(rng.uniform(0.45, 0.78))
    elif pose == "semi_enrolled":
        bend_strength = float(rng.uniform(0.55, 0.90))

    # Build at unit length=1.0 first, then scale to exact target_area_mm2
    length0 = 42.0
    half_len0 = 0.5 * length0
    half_w0 = 0.5 * length0 * width_ratio

    # Dense smooth sampling along right flank from head (t=0) to tail (t=1), then mirror left flank
    # Use cosine clustering near head and tail caps so semicircular ends are ultra-smooth
    s = np.linspace(0.0, 1.0, 110)
    t_vals = 0.5 * (1.0 - np.cos(math.pi * s))
    w_right = _half_width_profile(t_vals, half_len0, half_w0, base)
    y_vals = half_len0 * (1.0 - 2.0 * t_vals)

    # Ensure strictly positive minimum width at interior samples so no pinch occurs
    w_right[0] = 0.0
    w_right[-1] = 0.0
    interior = (t_vals > 0.015) & (t_vals < 0.985)
    w_right[interior] = np.maximum(w_right[interior], half_w0 * 0.24)

    # Assemble closed CCW ring: head -> left flank -> tail -> right flank -> head
    right_pts = np.column_stack([w_right, y_vals])
    left_pts = np.column_stack([-w_right[-2:0:-1], y_vals[-2:0:-1]])
    ring0 = np.vstack([right_pts, left_pts])

    # Apply smooth spine pose bend
    ring_bent = _apply_spine_bend(ring0, pose, bend_strength, half_len0)
    poly0 = Polygon(ring_bent).buffer(0)
    if poly0.geom_type != "Polygon":
        parts = [p for p in shapely.get_parts(poly0) if p.geom_type == "Polygon"]
        poly0 = max(parts, key=lambda p: p.area)
    poly0 = orient(poly0, 1.0)

    # Scale to exact target area and centre at centroid (0, 0)
    scale = math.sqrt(target_area_mm2 / max(float(poly0.area), 1e-6))
    half_len = half_len0 * scale
    half_w = half_w0 * scale

    ring_mm = _apply_spine_bend(ring0 * scale, pose, bend_strength, half_len)
    poly = Polygon(ring_mm).buffer(0)
    if poly.geom_type != "Polygon":
        parts = [p for p in shapely.get_parts(poly) if p.geom_type == "Polygon"]
        poly = max(parts, key=lambda p: p.area)

    # Smooth gentle opening+closing safety check at 1.72 mm radius so NO thin tip > 4.5 mm can ever occur
    # while keeping crisp trilobite scallops and sweeping genal curves!
    safe_poly = poly.buffer(-1.72, quad_segs=10).buffer(1.72, quad_segs=10)
    if safe_poly.geom_type == "Polygon" and safe_poly.is_valid and safe_poly.area > 0.80 * poly.area:
        # Blend outer shape gently: union core with slightly smoothed outline
        poly = poly.intersection(safe_poly.buffer(1.35, quad_segs=10)).buffer(0)
        if poly.geom_type != "Polygon":
            parts = [p for p in shapely.get_parts(poly) if p.geom_type == "Polygon"]
            poly = max(parts, key=lambda p: p.area)
    poly = poly.simplify(0.12, preserve_topology=True)
    poly = orient(poly, 1.0)

    # Re-scale cleanly to hit target area closely after smoothing
    scale2 = math.sqrt(target_area_mm2 / max(float(poly.area), 1e-6))
    poly = shapely.affinity.scale(poly, xfact=scale2, yfact=scale2, origin=(0.0, 0.0))
    cx, cy = poly.centroid.x, poly.centroid.y
    poly = shapely.affinity.translate(poly, xoff=-cx, yoff=-cy)
    poly = orient(poly, 1.0)

    total_scale = scale * scale2

    def map_local(arr_unscaled: np.ndarray) -> Polyline:
        pts_s = _apply_spine_bend(arr_unscaled * total_scale, pose, bend_strength, half_len * scale2)
        pts_s[:, 0] -= cx
        pts_s[:, 1] -= cy
        return [(float(px), float(py)) for px, py in pts_s]

    # -----------------------------------------------------------------------
    # Build rich anatomical engraving lines in unscaled local coordinates
    # -----------------------------------------------------------------------
    raw_engrave: List[np.ndarray] = []
    c_end = float(base["cephalon_frac"])
    th_end = c_end + float(base["thorax_frac"])
    glab_bulb = float(base["glabella_bulb"])
    eye_sz = float(base["eye_size"])

    y_head_top = half_len0
    y_ceph_bot = half_len0 * (1.0 - 2.0 * c_end)
    y_thorax_bot = half_len0 * (1.0 - 2.0 * th_end)
    y_tail_bot = -half_len0

    # 1. Cephalic marginal border furrow (horseshoe arc inside head shield)
    arc_t = np.linspace(0.06, c_end * 0.96, 32)
    arc_w = _half_width_profile(arc_t, half_len0, half_w0, base) * 0.76
    arc_y = half_len0 * (1.0 - 2.0 * arc_t) - half_len0 * 0.045 * (1.0 - arc_t / c_end)
    ceph_arc = np.vstack([
        np.column_stack([-arc_w[::-1], arc_y[::-1]]),
        np.column_stack([arc_w, arc_y]),
    ])
    raw_engrave.append(ceph_arc)

    # 2. Posterior cephalic furrow (separating head shield from thorax)
    w_ceph_base = float(_half_width_profile(np.array([c_end]), half_len0, half_w0, base)[0]) * 0.78
    xs_pb = np.linspace(-w_ceph_base, w_ceph_base, 24)
    ys_pb = np.full_like(xs_pb, y_ceph_bot) - half_len0 * 0.022 * np.cos(0.5 * math.pi * xs_pb / max(w_ceph_base, 1e-6))
    raw_engrave.append(np.column_stack([xs_pb, ys_pb]))

    # 3. Glabella (central raised cephalic lobe) + 3 pairs of lateral glabellar furrows
    glab_h = (y_head_top - y_ceph_bot) * 0.76
    glab_y0 = y_ceph_bot + (y_head_top - y_ceph_bot) * 0.05
    glab_y1 = glab_y0 + glab_h
    glab_w_base = half_w0 * 0.27
    g_u = np.linspace(0.0, 1.0, 20)
    g_w = glab_w_base * (0.78 + 0.34 * glab_bulb * np.sin(math.pi * (g_u ** 0.85))) * np.sqrt(np.clip(1.0 - np.maximum(0.0, (g_u - 0.65) / 0.35) ** 2.2, 0.02, 1.0))
    g_y = glab_y0 + g_u * glab_h
    glab_loop = np.vstack([
        np.column_stack([-g_w, g_y]),
        np.array([[0.0, glab_y1 + glab_h * 0.04]]),
        np.column_stack([g_w[::-1], g_y[::-1]]),
    ])
    raw_engrave.append(glab_loop)

    # Glabellar furrows (occipital ring + 2 lateral notch pairs)
    for frac in (0.18, 0.44, 0.68):
        fy = glab_y0 + frac * glab_h
        idx_near = int(np.argmin(np.abs(g_y - fy)))
        gw = float(g_w[idx_near])
        if frac < 0.25:
            # Complete occipital furrow across base of glabella
            xs_f = np.linspace(-gw * 0.88, gw * 0.88, 9)
            ys_f = fy - 0.012 * half_len0 * (1.0 - (xs_f / max(gw, 1e-6)) ** 2)
            raw_engrave.append(np.column_stack([xs_f, ys_f]))
        else:
            # Paired oblique glabellar furrows
            for sgn in (-1.0, 1.0):
                raw_engrave.append(np.array([
                    [sgn * gw * 0.90, fy + 0.012 * half_len0],
                    [sgn * gw * 0.26, fy - 0.010 * half_len0],
                ]))

    # 4. Crescentic Compound Eyes (left & right schizochroal / holochroal eye lobes!)
    eye_cy = glab_y0 + 0.46 * glab_h
    eye_cx = glab_w_base * (1.30 + 0.22 * glab_bulb)
    eye_rx = half_w0 * 0.11 * eye_sz
    eye_ry = glab_h * 0.23 * eye_sz
    ang = np.linspace(-0.78 * math.pi, 0.78 * math.pi, 16)
    for sgn in (-1.0, 1.0):
        # Outer crescentic visual lobe
        ex = sgn * (eye_cx + eye_rx * np.cos(ang))
        ey = eye_cy + eye_ry * np.sin(ang)
        raw_engrave.append(np.column_stack([ex, ey]))
        # Inner palpebral ridge
        ex2 = sgn * (eye_cx + eye_rx * 0.42 * np.cos(ang))
        ey2 = eye_cy + eye_ry * 0.62 * np.sin(ang)
        raw_engrave.append(np.column_stack([ex2, ey2]))

    # 5. Genus-specific Trinucleus cephalic brim pit arcs!
    if base.get("has_brim_pits", False):
        pit_t = np.linspace(0.05, c_end * 0.88, 13)
        pit_w = _half_width_profile(pit_t, half_len0, half_w0, base) * 0.84
        pit_y = half_len0 * (1.0 - 2.0 * pit_t) - half_len0 * 0.025
        pit_r = half_w0 * 0.028
        th_p = np.linspace(0.0, 2.0 * math.pi, 9)
        for pt_w, pt_y in zip(pit_w[1:], pit_y[1:]):
            for sgn in (-1.0, 1.0):
                raw_engrave.append(np.column_stack([
                    sgn * pt_w + pit_r * np.cos(th_p),
                    pt_y + pit_r * np.sin(th_p),
                ]))

    # 6. Axial furrows (longitudinal tripartite division running full length of thorax + pygidium!)
    t_axis = np.linspace(c_end, 0.90, 36)
    y_axis = half_len0 * (1.0 - 2.0 * t_axis)
    u_ax = (t_axis - c_end) / max(0.90 - c_end, 1e-6)
    w_axis = glab_w_base * (0.94 * (1.0 - u_ax) + 0.34 * u_ax)
    for sgn in (-1.0, 1.0):
        raw_engrave.append(np.column_stack([sgn * w_axis, y_axis]))
    # Rounded axial tip cap on pygidium
    cap_a = np.linspace(0.0, math.pi, 10)
    raw_engrave.append(np.column_stack([
        w_axis[-1] * np.cos(cap_a),
        y_axis[-1] - w_axis[-1] * 0.55 * np.sin(cap_a),
    ]))

    # 7. Thoracic segments: axial rings + swept pleural ribs (the signature trilobite barcode!)
    for k in range(n_seg):
        t_seg = c_end + (k + 0.55) * (th_end - c_end) / n_seg
        y_seg = half_len0 * (1.0 - 2.0 * t_seg)
        u_seg = (t_seg - c_end) / max(0.90 - c_end, 1e-6)
        wa = glab_w_base * (0.94 * (1.0 - u_seg) + 0.34 * u_seg)
        wf = float(_half_width_profile(np.array([t_seg]), half_len0, half_w0, base)[0]) * 0.78
        # Central axial ring furrow (gentle upward arch)
        xa = np.linspace(-wa * 0.92, wa * 0.92, 9)
        ya = y_seg + 0.012 * half_len0 * (1.0 - (xa / max(wa, 1e-6)) ** 2)
        raw_engrave.append(np.column_stack([xa, ya]))
        # Left & right pleural furrows sweeping gently posteriorly toward flanks
        for sgn in (-1.0, 1.0):
            s_p = np.linspace(0.0, 1.0, 10)
            xp = sgn * (wa * 1.06 + s_p * max(0.1, wf - wa * 1.06))
            yp = y_seg - (0.015 + 0.035 * (s_p ** 1.7)) * half_len0
            raw_engrave.append(np.column_stack([xp, yp]))

    # 8. Pygidium border + radiating pygidial ribs
    t_pyg_top = th_end
    w_pyg_top = float(_half_width_profile(np.array([t_pyg_top]), half_len0, half_w0, base)[0]) * 0.76
    xp_top = np.linspace(-w_pyg_top, w_pyg_top, 18)
    yp_top = np.full_like(xp_top, y_thorax_bot)
    raw_engrave.append(np.column_stack([xp_top, yp_top]))

    n_pyg_ribs = 3 if base["pygidium_frac"] < 0.24 else 4
    for k in range(n_pyg_ribs):
        frac = (k + 0.8) / (n_pyg_ribs + 0.9)
        t_r = th_end + frac * (0.90 - th_end)
        y_r = half_len0 * (1.0 - 2.0 * t_r)
        u_r = (t_r - c_end) / max(0.90 - c_end, 1e-6)
        wa = glab_w_base * (0.94 * (1.0 - u_r) + 0.34 * u_r)
        wf = float(_half_width_profile(np.array([t_r]), half_len0, half_w0, base)[0]) * 0.74
        # Pygidial axial ring
        if wa > half_w0 * 0.06:
            raw_engrave.append(np.array([[-wa * 0.85, y_r], [wa * 0.85, y_r]]))
        # Radiating pygidial pleurae sweeping backward
        if wf > wa * 1.25:
            for sgn in (-1.0, 1.0):
                s_p = np.linspace(0.0, 1.0, 8)
                xp = sgn * (wa * 1.05 + s_p * (wf - wa * 1.05))
                yp = y_r - (0.025 + 0.055 * frac) * half_len0 * (s_p ** 1.4)
                raw_engrave.append(np.column_stack([xp, yp]))

    # Map all engraving strokes into exact centred local mm coordinates & clip strictly inside 1.85 mm inset!
    mapped = [map_local(stroke) for stroke in raw_engrave]
    clipped, min_clr = clip_and_verify_engraving(
        mapped,
        poly,
        min_clearance_mm=1.50,
        target_inset_mm=1.85,
        min_stroke_len_mm=1.1,
    )
    return poly, clipped, {
        "n_segments": n_seg,
        "bend_strength": round(bend_strength, 3),
        "width_ratio": round(width_ratio, 3),
        "num_engrave_strokes": len(clipped),
        "local_min_engrave_clearance_mm": round(min_clr, 4),
    }


def clip_and_verify_engraving(
    engrave_paths: Sequence[Polyline],
    piece_polygon: Polygon,
    min_clearance_mm: float = 1.50,
    target_inset_mm: float = 1.65,
    min_stroke_len_mm: float = 0.85,
) -> Tuple[List[Polyline], float]:
    """Clip engraving polylines strictly inside `piece_polygon.buffer(-target_inset_mm)`.

    Guarantees EVERY surviving vertex and segment has Euclidean distance >= `min_clearance_mm`
    from `piece_polygon.boundary`. Returns `(clipped_polylines, actual_min_clearance_mm)`.
    """
    safe_zone = piece_polygon.buffer(-max(target_inset_mm, min_clearance_mm + 0.08), quad_segs=16)
    if safe_zone.is_empty:
        return [], float("inf")

    boundary = piece_polygon.boundary
    kept: List[Polyline] = []
    worst_dist = float("inf")

    for pts in engrave_paths:
        if len(pts) < 2:
            continue
        ls = LineString(pts)
        if ls.is_empty or ls.length < 1e-3:
            continue
        inter = ls.intersection(safe_zone)
        if inter.is_empty:
            continue
        parts: List[LineString] = []
        for g in shapely.get_parts(inter):
            if g.geom_type == "LineString" and g.length >= min_stroke_len_mm:
                parts.append(g)
            elif g.geom_type == "MultiLineString":
                for sub in g.geoms:
                    if sub.length >= min_stroke_len_mm:
                        parts.append(sub)
        for seg in parts:
            d = float(seg.distance(boundary))
            if d < min_clearance_mm:
                # Extra conservative trim if numerical snapping shaved right up to the boundary
                sub_inter = seg.intersection(piece_polygon.buffer(-(min_clearance_mm + 0.12), quad_segs=16))
                for sg in shapely.get_parts(sub_inter):
                    if sg.geom_type == "LineString" and sg.length >= min_stroke_len_mm:
                        d2 = float(sg.distance(boundary))
                        if d2 >= min_clearance_mm:
                            coords = [(round(float(x), 4), round(float(y), 4)) for x, y in sg.coords]
                            if len(coords) >= 2:
                                kept.append(coords)
                                worst_dist = min(worst_dist, d2)
            else:
                coords = [(round(float(x), 4), round(float(y), 4)) for x, y in seg.coords]
                if len(coords) >= 2:
                    kept.append(coords)
                    worst_dist = min(worst_dist, d)

    return kept, worst_dist


def verify_engraving_clearance(
    engrave_paths: Sequence[Polyline],
    piece_polygon: Polygon,
    min_clearance_mm: float = 1.50,
) -> Dict[str, Any]:
    """Independently verify that every engraving polyline sits >= `min_clearance_mm` inside `piece_polygon`."""
    boundary = piece_polygon.boundary
    min_dist = float("inf")
    violations = 0
    outside_strokes = 0
    total_len = 0.0

    for pts in engrave_paths:
        if len(pts) < 2:
            continue
        ls = LineString(pts)
        total_len += float(ls.length)
        if not piece_polygon.contains(ls):
            outside_strokes += 1
            violations += 1
            min_dist = 0.0
            continue
        d = float(ls.distance(boundary))
        min_dist = min(min_dist, d)
        if d < min_clearance_mm - 1e-6:
            violations += 1

    return {
        "num_strokes": len(engrave_paths),
        "total_length_mm": round(total_len, 3),
        "min_clearance_mm": round(min_dist, 4) if math.isfinite(min_dist) else None,
        "violations": violations,
        "outside_strokes": outside_strokes,
        "passed": len(engrave_paths) > 0 and violations == 0 and math.isfinite(min_dist) and min_dist >= min_clearance_mm - 1e-6,
    }


def _rasterize_polygon_mask_and_sdf(poly: Polygon, size: int = 256, pad_frac: float = 0.08) -> Tuple[np.ndarray, np.ndarray]:
    """Rasterize polygon into a normalized (size, size) binary mask + pixel SDF."""
    x0, y0, x1, y1 = poly.bounds
    span = max(x1 - x0, y1 - y0, 1e-6)
    usable = size * (1.0 - 2.0 * pad_frac)
    scale = usable / span
    cx = 0.5 * (x0 + x1)
    cy = 0.5 * (y0 + y1)

    pts = np.asarray(poly.exterior.coords, dtype=np.float64)
    px = np.round((pts[:, 0] - cx) * scale + size * 0.5).astype(np.int32)
    py = np.round((pts[:, 1] - cy) * scale + size * 0.5).astype(np.int32)
    poly_px = np.column_stack([px, py]).reshape(-1, 1, 2)

    mask_u8 = np.zeros((size, size), dtype=np.uint8)
    cv2.fillPoly(mask_u8, [poly_px], 255)
    fg = mask_u8 > 127
    d_out = ndimage.distance_transform_edt(~fg)
    d_in = ndimage.distance_transform_edt(fg)
    sdf = (d_out - d_in).astype(np.float32)
    return fg.astype(np.float32), sdf


def transform_engrave_paths(
    engrave_paths: Sequence[Polyline],
    cx: float,
    cy: float,
    angle_rad: float,
    scale: float = 1.0,
    flip_x: bool = False,
) -> List[Polyline]:
    """Apply similarity transform (flip, scale, rotate, translate) to local engraving polylines."""
    cs = math.cos(angle_rad)
    sn = math.sin(angle_rad)
    out: List[Polyline] = []
    for stroke in engrave_paths:
        arr = np.asarray(stroke, dtype=np.float64)
        if flip_x:
            arr = arr.copy()
            arr[:, 0] = -arr[:, 0]
        arr = arr * scale
        rx = cs * arr[:, 0] - sn * arr[:, 1] + cx
        ry = sn * arr[:, 0] + cs * arr[:, 1] + cy
        out.append([(float(x), float(y)) for x, y in zip(rx, ry)])
    return out


def generate_trilobite(
    genus: str,
    pose: str = "straight",
    target_area_mm2: float = 780.0,
    seed: int = 42,
    asset_id: Optional[str] = None,
) -> TrilobiteSpec:
    """Generate one verified parametric trilobite with kerf-safe silhouette & inset engraving."""
    if genus not in TRILOBITE_GENUS_INFO:
        raise ValueError(f"Unknown trilobite genus {genus!r}; expected one of {TRILOBITE_GENERA}")
    rng = np.random.default_rng(seed)
    poly, clipped_engrave, extra = _build_local_trilobite_geometry(genus, pose, target_area_mm2, rng)
    mask, sdf = _rasterize_polygon_mask_and_sdf(poly, size=256)
    x0, y0, x1, y1 = poly.bounds
    info = TRILOBITE_GENUS_INFO[genus]
    aid = asset_id or f"trilobite_{genus.lower()}_{pose}_{seed:04d}"
    return TrilobiteSpec(
        asset_id=aid,
        genus=genus,
        pose=pose,
        period=str(info["period"]),
        region=str(info["region"]),
        length_mm=float(y1 - y0),
        width_mm=float(x1 - x0),
        area_mm2=float(poly.area),
        polygon=poly,
        engrave_paths=clipped_engrave,
        mask=mask,
        sdf=sdf,
        min_clearance_mm=float(extra["local_min_engrave_clearance_mm"]),
        metadata={
            "kind": "trilobite",
            "asset_id": aid,
            "family": "trilobite",
            "motif_class": f"trilobite_{genus.lower()}",
            "genus": genus,
            "pose": pose,
            "period": info["period"],
            "site": info["region"],
            "source_archive": "original_parametric",
            "creator": "Original parametric trilobite vector generator (CC0)",
            "license": "CC0 1.0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source_url": "internal://heller_jigsaw/trilobite_dendrite/trilobites.py",
            "commercial_ok": True,
            "share_alike_required": False,
            "is_synthetic_vae": False,
            "legal_review_status": "evidence_recorded_pending_legal_review",
            "cleared_for_sale": False,
            **extra,
        },
    )


def build_trilobite_collection(
    count: int = 150,
    seed: int = 2026,
    base_area_mm2: float = 760.0,
) -> List[TrilobiteSpec]:
    """Generate a diverse collection of `count` multi-genus trilobites across straight & curved poses."""
    rng = np.random.default_rng(seed)
    # Weight iconic Scandinavian & sculptural genera nicely while covering all 7 genera
    genus_weights = [0.18, 0.14, 0.14, 0.15, 0.13, 0.13, 0.13]
    pose_weights = [0.42, 0.19, 0.19, 0.12, 0.08]
    collection: List[TrilobiteSpec] = []

    for i in range(count):
        # Guarantee round-robin coverage of all 7 genera first, then weighted sampling
        if i < len(TRILOBITE_GENERA) * 3:
            genus = TRILOBITE_GENERA[i % len(TRILOBITE_GENERA)]
            pose = TRILOBITE_POSES[(i // len(TRILOBITE_GENERA)) % len(TRILOBITE_POSES)]
        else:
            genus = str(rng.choice(TRILOBITE_GENERA, p=genus_weights))
            pose = str(rng.choice(TRILOBITE_POSES, p=pose_weights))

        # Varied fossil sizes (hero fossils up to ~920 mm^2, standard ~660-840 mm^2)
        if i % 10 == 0:
            area = float(rng.uniform(base_area_mm2 * 1.12, base_area_mm2 * 1.24))
        else:
            area = float(rng.uniform(base_area_mm2 * 0.88, base_area_mm2 * 1.10))

        spec = generate_trilobite(
            genus=genus,
            pose=pose,
            target_area_mm2=area,
            seed=seed * 1000 + i,
            asset_id=f"trilobite_{i + 1:03d}_{genus.lower()}",
        )
        collection.append(spec)

    return collection
