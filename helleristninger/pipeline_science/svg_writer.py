"""True-scale multi-layer SVG exporter for scientific rock-art figures."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

from .extract_layers import TracedFigure

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def _ring_to_d(pts: List[Tuple[float, float]], scale: float) -> str:
    if len(pts) < 3:
        return ""
    body = pts[:-1] if pts[0] == pts[-1] else pts
    if len(body) < 3:
        return ""
    parts = [f"M {body[0][0] * scale:.3f},{body[0][1] * scale:.3f}"]
    for x, y in body[1:]:
        parts.append(f"L {x * scale:.3f},{y * scale:.3f}")
    parts.append("Z")
    return " ".join(parts)


def _polyline_to_d(pts: List[Tuple[float, float]], scale: float) -> str:
    if len(pts) < 2:
        return ""
    parts = [f"M {pts[0][0] * scale:.3f},{pts[0][1] * scale:.3f}"]
    for x, y in pts[1:]:
        parts.append(f"L {x * scale:.3f},{y * scale:.3f}")
    return " ".join(parts)


def _choose_scale_bar_mm(width_mm: float) -> float:
    target = max(1.0, width_mm * 0.28)
    candidates = [1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, 200.0, 500.0, 1000.0]
    best = candidates[0]
    for c in candidates:
        if c <= width_mm * 0.75:
            if abs(c - target) <= abs(best - target):
                best = c
    return best


def write_scientific_svg(
    out_path: Path,
    figure_id: str,
    traced: TracedFigure,
    scale_info: Dict[str, Any],
    site_panel_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a true-scale multi-layer SVG with #figures, #cup_marks, #cracks, #uncertain, and #scale_bar."""
    calibrated = bool(scale_info.get("scale_calibrated"))
    px_per_mm = float(scale_info["px_per_mm"]) if calibrated and scale_info.get("px_per_mm") else None
    scale = (1.0 / px_per_mm) if (calibrated and px_per_mm) else 1.0
    units = "mm" if calibrated else "px"

    _, _, crop_w_px, crop_h_px = traced.crop_px
    content_w = max(1.0, crop_w_px * scale)
    content_h = max(1.0, crop_h_px * scale)
    bar_margin = max(10.0 * (1.0 if calibrated else 14.0), content_h * 0.14) if calibrated else 0.0
    vb_w = content_w
    vb_h = content_h + bar_margin

    stroke_thin = max(0.35, min(2.0, 0.0045 * max(content_w, content_h)))

    svg_attrs = {
        "version": "1.1",
        "viewBox": f"0 0 {vb_w:.3f} {vb_h:.3f}",
        "width": f"{vb_w:.3f}{units}",
        "height": f"{vb_h:.3f}{units}",
        "data-figure-id": figure_id,
        "data-scale-calibrated": "true" if calibrated else "false",
        "data-units": units,
        "data-scale-source": str(scale_info.get("scale_source") or ""),
    }
    if px_per_mm is not None:
        svg_attrs["data-px-per-mm"] = f"{px_per_mm:.6f}"
    if site_panel_id:
        svg_attrs["data-site-panel-id"] = str(site_panel_id)

    svg = ET.Element(f"{{{SVG_NS}}}svg", svg_attrs)

    # Layer 1: #figures (strictly ONE closed <path> per figure)
    g_figures = ET.SubElement(svg, f"{{{SVG_NS}}}g", {"id": "figures"})
    d_segments: List[str] = []
    for ext_pts, holes in traced.subpaths_px:
        ext_d = _ring_to_d(ext_pts, scale)
        if ext_d:
            d_segments.append(ext_d)
            for h_pts in holes:
                hd = _ring_to_d(h_pts, scale)
                if hd:
                    d_segments.append(hd)
    figure_d = " ".join(d_segments)
    ET.SubElement(
        g_figures,
        f"{{{SVG_NS}}}path",
        {
            "id": f"path_{figure_id}",
            "d": figure_d,
            "fill": "#18181B",
            "fill-rule": "evenodd",
            "stroke": "none",
        },
    )

    # Layer 2: #cup_marks (detected circular cup marks / skålgroper)
    g_cups = ET.SubElement(
        svg,
        f"{{{SVG_NS}}}g",
        {
            "id": "cup_marks",
            "fill": "#9A3412",
            "fill-opacity": "0.35",
            "stroke": "#9A3412",
            "stroke-width": f"{stroke_thin:.3f}",
        },
    )
    for idx, (cx, cy, r) in enumerate(traced.cup_marks_px):
        ET.SubElement(
            g_cups,
            f"{{{SVG_NS}}}circle",
            {
                "id": f"cupmark_{idx:03d}",
                "cx": f"{cx * scale:.3f}",
                "cy": f"{cy * scale:.3f}",
                "r": f"{max(0.25, r * scale):.3f}",
            },
        )

    # Layer 3: #cracks (linear geological fissures / fractures)
    g_cracks = ET.SubElement(
        svg,
        f"{{{SVG_NS}}}g",
        {
            "id": "cracks",
            "fill": "none",
            "stroke": "#DC2626",
            "stroke-width": f"{stroke_thin * 1.15:.3f}",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for idx, line in enumerate(traced.cracks_px):
        ld = _polyline_to_d(line, scale)
        if ld:
            ET.SubElement(g_cracks, f"{{{SVG_NS}}}path", {"id": f"crack_{idx:03d}", "d": ld})

    # Layer 4: #uncertain (low-confidence / weathered lines styled with stroke-dasharray="2 2")
    g_uncertain = ET.SubElement(
        svg,
        f"{{{SVG_NS}}}g",
        {
            "id": "uncertain",
            "fill": "none",
            "stroke": "#6B7280",
            "stroke-width": f"{stroke_thin:.3f}",
            "stroke-dasharray": "2 2",
            "stroke-linecap": "round",
        },
    )
    for idx, line in enumerate(traced.uncertain_px):
        ld = _polyline_to_d(line, scale)
        if ld:
            ET.SubElement(
                g_uncertain,
                f"{{{SVG_NS}}}path",
                {
                    "id": f"uncertain_{idx:03d}",
                    "d": ld,
                    "stroke-dasharray": "2 2",
                },
            )

    # Layer 5: #scale_bar (ONLY when physically calibrated in mm!)
    has_scale_bar = False
    if calibrated and px_per_mm:
        has_scale_bar = True
        bar_len_mm = _choose_scale_bar_mm(content_w)
        g_sb = ET.SubElement(svg, f"{{{SVG_NS}}}g", {"id": "scale_bar"})
        bx0 = max(1.0, content_w * 0.05)
        bx1 = bx0 + bar_len_mm
        by = content_h + bar_margin * 0.52
        tick_h = max(1.0, bar_margin * 0.18)
        sb_sw = max(0.3, stroke_thin * 1.1)
        ET.SubElement(
            g_sb,
            f"{{{SVG_NS}}}line",
            {
                "x1": f"{bx0:.3f}",
                "y1": f"{by:.3f}",
                "x2": f"{bx1:.3f}",
                "y2": f"{by:.3f}",
                "stroke": "#111827",
                "stroke-width": f"{sb_sw:.3f}",
            },
        )
        for tx in (bx0, bx1):
            ET.SubElement(
                g_sb,
                f"{{{SVG_NS}}}line",
                {
                    "x1": f"{tx:.3f}",
                    "y1": f"{by - tick_h:.3f}",
                    "x2": f"{tx:.3f}",
                    "y2": f"{by + tick_h:.3f}",
                    "stroke": "#111827",
                    "stroke-width": f"{sb_sw:.3f}",
                },
            )
        lbl = ET.SubElement(
            g_sb,
            f"{{{SVG_NS}}}text",
            {
                "x": f"{bx1 + max(1.5, bar_len_mm * 0.08):.3f}",
                "y": f"{by + tick_h * 0.45:.3f}",
                "font-family": "sans-serif",
                "font-size": f"{max(2.5, bar_margin * 0.34):.3f}",
                "fill": "#111827",
            },
        )
        lbl.text = f"{bar_len_mm:g} mm"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(svg).write(out_path, encoding="utf-8", xml_declaration=True)

    return {
        "figures_path_closed": bool(figure_d.startswith("M") and figure_d.endswith("Z")),
        "cup_marks_count": len(traced.cup_marks_px),
        "cracks_count": len(traced.cracks_px),
        "uncertain_count": len(traced.uncertain_px),
        "has_scale_bar": has_scale_bar,
    }
