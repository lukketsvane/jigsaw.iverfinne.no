"""Dual-layer laser SVG exporter, independent SVG re-import verifier, and high-res visual renderer.

Exports to `runs/dendrite-trilobite-750/`:
- `cut.svg`         — Pure laser-cut common-line paths ONLY (`#interior-cuts`, `#exterior-cuts`)
- `engrave.svg`     — Pure trilobite anatomical engraving paths ONLY (`#engraving`), >= 1.5 mm inset
- `production.svg`  — Combined layered laser SVG (`#engraving`, `#interior-cuts`, `#exterior-cuts`)
- `outline.svg`, `pieces.svg`, `assembly.svg`, `pieces/0001..0750.{svg,png}`
- `partition.json`, `config.json`, `provenance.json`, `rights_summary.json`,
  `quality.json`, `production-audit.json`, `metrics.csv`
- High-res PNG visual inspection renders:
  `assembly.png`, `assembly_colored.png`, `crop_trilobite_dendrite_center.png`,
  `crop_trilobite_dendrite_border.png`, `contact_sheet_trilobites.png`,
  `contact_sheet_helleristninger.png`, `contact_sheet_weakest.png`
"""

from __future__ import annotations

import csv
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import shapely
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from heller_jigsaw.geometry.export import (
    NORDIC_PALETTE,
    SVG_NS,
    _coords_to_closed_d,
    _coords_to_open_d,
    _load_font,
    _make_svg_root,
    _write_svg,
    rasterize_normalized_piece_mask,
    write_cut_svg,
    write_isolated_piece_svg,
    write_outline_svg,
    write_pieces_svg,
)
from heller_jigsaw.geometry.outline import SNAP, from_svg
from heller_jigsaw.geometry.partition import PlanarPartition
from heller_jigsaw.geometry.validate import _parse_cut_svg_paths, _parse_pieces_svg_polygons

from .catalog import PuzzleMotif
from .trilobites import Polyline


# ---------------------------------------------------------------------------
# Dual-layer SVG writers (`engrave.svg` + `production.svg` + `assembly.svg`)
# ---------------------------------------------------------------------------

def write_engrave_svg(
    partition: PlanarPartition,
    engrave_by_face: Dict[int, List[Polyline]],
    path: Path,
    stroke_mm: float = 0.20,
) -> None:
    """Export pure anatomical engraving layer ONLY (matching `cut.svg` mm viewBox exactly)."""
    x0, y0, x1, y1 = partition.outline.bounds
    root = _make_svg_root(x1 - x0, y1 - y0, x0, y0)
    g_eng = ET.SubElement(
        root,
        f"{{{SVG_NS}}}g",
        {
            "id": "engraving",
            "fill": "none",
            "stroke": "#008000",
            "stroke-width": f"{stroke_mm:.4f}",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for fid in sorted(engrave_by_face.keys()):
        strokes = engrave_by_face[fid]
        if not strokes:
            continue
        meta = partition.faces[fid].metadata
        g_piece = ET.SubElement(
            g_eng,
            f"{{{SVG_NS}}}g",
            {
                "id": f"engrave-piece-{fid + 1:04d}",
                "data-face-id": str(fid),
                "data-asset-id": str(meta.get("asset_id", "")),
                "data-genus": str(meta.get("genus", "")),
            },
        )
        for s_idx, pts in enumerate(strokes):
            ET.SubElement(
                g_piece,
                f"{{{SVG_NS}}}path",
                {
                    "id": f"eng-{fid + 1:04d}-{s_idx + 1:02d}",
                    "d": _coords_to_open_d(pts),
                    "fill": "none",
                },
            )
    _write_svg(root, path)


def write_production_svg(
    partition: PlanarPartition,
    engrave_by_face: Dict[int, List[Polyline]],
    path: Path,
    kerf_mm: float = 0.15,
    engrave_stroke_mm: float = 0.20,
) -> None:
    """Export laser-ready combined layered SVG (`engraving` -> `interior-cuts` -> `exterior-cuts`)."""
    x0, y0, x1, y1 = partition.outline.bounds
    root = _make_svg_root(x1 - x0, y1 - y0, x0, y0)
    stroke_w = f"{kerf_mm:.4f}"

    # Layer 1: Engraving (runs FIRST while wood panel is anchored flat)
    g_eng = ET.SubElement(
        root,
        f"{{{SVG_NS}}}g",
        {
            "id": "engraving",
            "fill": "none",
            "stroke": "#008000",
            "stroke-width": f"{engrave_stroke_mm:.4f}",
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for fid in sorted(engrave_by_face.keys()):
        strokes = engrave_by_face[fid]
        if not strokes:
            continue
        meta = partition.faces[fid].metadata
        g_piece = ET.SubElement(
            g_eng,
            f"{{{SVG_NS}}}g",
            {
                "id": f"prod-engrave-{fid + 1:04d}",
                "data-face-id": str(fid),
                "data-genus": str(meta.get("genus", "")),
            },
        )
        for s_idx, pts in enumerate(strokes):
            ET.SubElement(
                g_piece,
                f"{{{SVG_NS}}}path",
                {
                    "id": f"peng-{fid + 1:04d}-{s_idx + 1:02d}",
                    "d": _coords_to_open_d(pts),
                    "fill": "none",
                },
            )

    # Layer 2: Interior shared cuts (unique canonical seams, zero double-cutting)
    g_int = ET.SubElement(
        root,
        f"{{{SVG_NS}}}g",
        {
            "id": "interior-cuts",
            "fill": "none",
            "stroke": "#FF0000",
            "stroke-width": stroke_w,
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for e in partition.interior_edges:
        ET.SubElement(
            g_int,
            f"{{{SVG_NS}}}path",
            {
                "id": f"cut-int-{e.edge_id:04d}",
                "d": _coords_to_open_d(e.coords),
                "fill": "none",
                "data-faces": f"{e.left_face},{e.right_face}",
            },
        )

    # Layer 3: Exterior perimeter cuts (runs LAST)
    g_ext = ET.SubElement(
        root,
        f"{{{SVG_NS}}}g",
        {
            "id": "exterior-cuts",
            "fill": "none",
            "stroke": "#0000FF",
            "stroke-width": stroke_w,
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for e in partition.exterior_edges:
        ET.SubElement(
            g_ext,
            f"{{{SVG_NS}}}path",
            {
                "id": f"cut-ext-{e.edge_id:04d}",
                "d": _coords_to_open_d(e.coords),
                "fill": "none",
                "data-face": str(e.left_face if e.left_face >= 0 else e.right_face),
            },
        )

    _write_svg(root, path)


def write_dendrite_assembly_svg(
    partition: PlanarPartition,
    engrave_by_face: Dict[int, List[Polyline]],
    path: Path,
) -> None:
    """Export warm birch-plywood vector preview SVG with engraved trilobites."""
    x0, y0, x1, y1 = partition.outline.bounds
    w_mm, h_mm = x1 - x0, y1 - y0
    root = _make_svg_root(w_mm, h_mm, x0, y0)
    ET.SubElement(
        root,
        f"{{{SVG_NS}}}rect",
        {"x": f"{x0:.4f}", "y": f"{y0:.4f}", "width": f"{w_mm:.4f}", "height": f"{h_mm:.4f}", "fill": "#FAF7F2"},
    )
    g_faces = ET.SubElement(root, f"{{{SVG_NS}}}g", {"id": "assembly-faces"})
    wood_heller = ["#EFE2CA", "#EAD8BC", "#F3E8D4", "#E5D1B2", "#EDE0C6", "#E8D5B8"]
    wood_trilo = ["#D9B47E", "#D2A96E", "#DFBD88", "#CCA064"]

    for f in partition.faces:
        is_trilo = f.metadata.get("kind") == "trilobite"
        fill = wood_trilo[f.face_id % len(wood_trilo)] if is_trilo else wood_heller[f.face_id % len(wood_heller)]
        ET.SubElement(
            g_faces,
            f"{{{SVG_NS}}}path",
            {
                "id": f"asm-piece-{f.face_id + 1:04d}",
                "d": _coords_to_closed_d(list(f.polygon.exterior.coords)),
                "fill": fill,
                "stroke": "#331D0F",
                "stroke-width": "0.45",
                "stroke-linejoin": "round",
            },
        )

    g_eng = ET.SubElement(
        root,
        f"{{{SVG_NS}}}g",
        {"id": "assembly-engraving", "fill": "none", "stroke": "#5A2E14", "stroke-width": "0.35", "stroke-linecap": "round"},
    )
    for fid, strokes in sorted(engrave_by_face.items()):
        for pts in strokes:
            ET.SubElement(g_eng, f"{{{SVG_NS}}}path", {"d": _coords_to_open_d(pts)})

    _write_svg(root, path)


# ---------------------------------------------------------------------------
# Fast O(N) Independent SVG Re-Import Verifier (Exact 750 faces, 0 dup cuts)
# ---------------------------------------------------------------------------

def verify_exported_svgs(
    out_dir: Path,
    expected_n: int,
    expected_trilobites: int,
    min_segment_mm: float = 0.05,
    min_engrave_clearance_mm: float = 1.50,
) -> Dict[str, Any]:
    """Independently re-import `cut.svg`, `engrave.svg`, `production.svg`, `pieces.svg`, `outline.svg`.

    Uses STRtree for O(N) exact correspondence instead of O(N^2), verifying:
    - `cut.svg` re-imports to exact `expected_n` valid hole-free faces
    - zero gaps (`coverage_rel_error <= 1e-3`), zero overlaps, zero duplicate cuts, zero micro-segments < 0.05 mm
    - `cut.svg` has ZERO engraving / text / filled paths
    - `engrave.svg` & `production.svg` have strictly separated layers and every engraving path sits >= 1.50 mm
      inside its re-imported cut face!
    """
    outline_poly = from_svg(out_dir / "outline.svg")
    cut_lines, cut_meta = _parse_cut_svg_paths(out_dir / "cut.svg")

    # 1. Check duplicate/overlapping cuts and micro-segments inside cut.svg
    dup_pairs: List[Tuple[int, int]] = []
    min_seg = float("inf")
    for ls in cut_lines:
        arr = np.asarray(ls.coords, dtype=np.float64)
        if len(arr) >= 2:
            lens = np.hypot(np.diff(arr[:, 0]), np.diff(arr[:, 1]))
            min_seg = min(min_seg, float(np.min(lens)))
    if len(cut_lines) > 1:
        tree = STRtree(cut_lines)
        for i, la in enumerate(cut_lines):
            for j in tree.query(la):
                ji = int(j)
                if ji <= i:
                    continue
                inter = la.intersection(cut_lines[ji])
                if not inter.is_empty:
                    for part in shapely.get_parts(inter):
                        if part.geom_type in ("LineString", "MultiLineString") and part.length > 0.01:
                            dup_pairs.append((i, ji))
                            break

    # 2. Reconstruct faces strictly from cut.svg linework
    snapped_cuts = [shapely.set_precision(ls, grid_size=SNAP) for ls in cut_lines]
    raw_faces = [
        orient(p, 1.0)
        for p in polygonize(unary_union(snapped_cuts))
        if p.geom_type == "Polygon" and p.is_valid and p.area > 1e-4
    ]
    reimported_faces = [p for p in raw_faces if outline_poly.contains(p.representative_point())]
    holed_reimported = sum(1 for p in reimported_faces if len(p.interiors) > 0)

    if reimported_faces:
        cut_union = unary_union(reimported_faces)
        cut_cov_sym = float(cut_union.symmetric_difference(outline_poly).area)
        cut_cov_rel = cut_cov_sym / max(float(outline_poly.area), 1e-9)
    else:
        cut_cov_sym = float("inf")
        cut_cov_rel = float("inf")

    # Check pairwise overlaps among reimported faces using STRtree
    max_ovl = 0.0
    if len(reimported_faces) > 1:
        ftree = STRtree(reimported_faces)
        for i, pi in enumerate(reimported_faces):
            for j in ftree.query(pi):
                ji = int(j)
                if ji <= i:
                    continue
                ia = float(pi.intersection(reimported_faces[ji]).area)
                if ia > max_ovl:
                    max_ovl = ia

    # 3. STRtree O(N) correspondence between cut.svg faces and pieces.svg polygons
    pieces_polys = _parse_pieces_svg_polygons(out_dir / "pieces.svg")
    max_corr_err = 0.0
    if len(reimported_faces) == expected_n and len(pieces_polys) == expected_n:
        ftree = STRtree(reimported_faces)
        for pp in pieces_polys:
            cands = ftree.query(pp)
            if len(cands) == 0:
                max_corr_err = float("inf")
                break
            best_rel = min(
                float(pp.symmetric_difference(reimported_faces[int(k)]).area) / max(float(pp.area), 1e-6)
                for k in cands
            )
            max_corr_err = max(max_corr_err, best_rel)
    else:
        max_corr_err = float("inf")

    # 4. Verify engrave.svg independently against the re-imported cut.svg faces!
    eng_lines, eng_meta = _parse_cut_svg_paths(out_dir / "engrave.svg")
    eng_min_clr = float("inf")
    eng_violations = 0
    if reimported_faces and eng_lines:
        ftree = STRtree(reimported_faces)
        for ls in eng_lines:
            cands = ftree.query(ls)
            owner = None
            for k in cands:
                fp = reimported_faces[int(k)]
                if fp.contains(ls):
                    owner = fp
                    break
            if owner is None:
                eng_violations += 1
                eng_min_clr = 0.0
            else:
                d = float(ls.distance(owner.boundary))
                eng_min_clr = min(eng_min_clr, d)
                if d < min_engrave_clearance_mm - 1e-4:
                    eng_violations += 1

    # 5. Verify production.svg has all 3 strictly separated groups: engraving, interior-cuts, exterior-cuts
    prod_root = ET.parse(str(out_dir / "production.svg")).getroot()
    prod_group_ids = {
        el.attrib.get("id", "")
        for el in prod_root
        if el.tag.split("}")[-1] == "g"
    }
    prod_layers_ok = {"engraving", "interior-cuts", "exterior-cuts"}.issubset(prod_group_ids)

    # Check cut.svg strictly has NO engraving group
    cut_root = ET.parse(str(out_dir / "cut.svg")).getroot()
    cut_group_ids = {el.attrib.get("id", "") for el in cut_root.iter() if el.tag.split("}")[-1] == "g"}
    cut_has_no_engrave = "engraving" not in cut_group_ids

    gates = {
        "cut_svg_mm_units": bool(cut_meta["units_mm"]),
        "cut_svg_pure_no_labels_fills_engrave": cut_meta["text_elements"] == 0 and cut_meta["filled_paths"] == 0 and cut_has_no_engrave,
        "cut_svg_zero_duplicate_seams": len(dup_pairs) == 0,
        "cut_svg_zero_micro_segments": math.isfinite(min_seg) and min_seg >= min_segment_mm - 1e-6,
        "cut_svg_reimport_exact_n": len(reimported_faces) == expected_n and holed_reimported == 0,
        "cut_svg_reimport_zero_gaps": cut_cov_rel <= 1e-3,
        "cut_svg_reimport_zero_overlaps": max_ovl <= 1e-3,
        "cut_vs_pieces_svg_exact_match": max_corr_err <= 1e-3,
        "engrave_svg_mm_units": bool(eng_meta["units_mm"]),
        "engrave_svg_has_paths": len(eng_lines) >= expected_trilobites * 6,
        "engrave_svg_clearance_ge_1_5mm": eng_violations == 0 and math.isfinite(eng_min_clr) and eng_min_clr >= min_engrave_clearance_mm - 1e-4,
        "production_svg_three_strict_layers": prod_layers_ok,
    }

    return {
        "all_svg_gates_passed": all(gates.values()),
        "gates": gates,
        "reimported_face_count": len(reimported_faces),
        "holed_reimported_faces": holed_reimported,
        "duplicate_cut_pairs": len(dup_pairs),
        "cut_min_segment_mm": round(min_seg, 5) if math.isfinite(min_seg) else 0.0,
        "cut_coverage_sym_diff_mm2": round(cut_cov_sym, 6) if math.isfinite(cut_cov_sym) else None,
        "cut_coverage_rel_error": cut_cov_rel if math.isfinite(cut_cov_rel) else None,
        "cut_max_overlap_mm2": round(max_ovl, 6),
        "pieces_svg_face_count": len(pieces_polys),
        "max_piece_correspondence_rel_error": max_corr_err if math.isfinite(max_corr_err) else None,
        "engrave_path_count": len(eng_lines),
        "engrave_reimport_min_clearance_mm": round(eng_min_clr, 4) if math.isfinite(eng_min_clr) else None,
        "engrave_reimport_violations": eng_violations,
        "production_svg_groups": sorted(prod_group_ids),
    }


# ---------------------------------------------------------------------------
# High-Resolution Visual Inspection Renders (OpenCV + Pillow anti-aliased)
# ---------------------------------------------------------------------------

def _greedy_face_colors(partition: PlanarPartition) -> List[int]:
    """Graph coloring of faces so no two adjacent pieces share the same display shade."""
    n = partition.num_pieces
    order = sorted(range(n), key=lambda i: len(partition.adjacency.get(i, [])), reverse=True)
    col = [-1] * n
    for u in order:
        used = {col[v] for v in partition.adjacency.get(u, []) if col[v] >= 0}
        c = 0
        while c in used:
            c += 1
        col[u] = c
    return col


def _render_region_cv2(
    partition: PlanarPartition,
    engrave_by_face: Dict[int, List[Polyline]],
    roi_mm: Tuple[float, float, float, float],
    width_px: int,
    wood_mode: bool = True,
    show_silhouettes: Optional[Sequence[Polygon]] = None,
    title: str = "",
) -> Image.Image:
    """Anti-aliased high-res render of any mm bounding box `(rx0, ry0, rx1, ry1)`."""
    rx0, ry0, rx1, ry1 = roi_mm
    rw = max(rx1 - rx0, 1e-3)
    rh = max(ry1 - ry0, 1e-3)
    margin = 48 if title else 16
    hdr = 52 if title else 0
    scale = (width_px - 2 * margin) / rw
    height_px = int(round(rh * scale)) + 2 * margin + hdr

    # 2x supersampling for razor-sharp laser lines & engraving!
    ss = 2
    cw, ch = width_px * ss, height_px * ss
    bg_bgr = (242, 245, 249)  # warm ivory paper backdrop
    canvas = np.full((ch, cw, 3), bg_bgr, dtype=np.uint8)

    def to_px_arr(coords: Sequence[Tuple[float, float]]) -> np.ndarray:
        arr = np.asarray(coords, dtype=np.float64)
        px = np.round(((arr[:, 0] - rx0) * scale + margin) * ss).astype(np.int32)
        py = np.round(((arr[:, 1] - ry0) * scale + margin + hdr) * ss).astype(np.int32)
        return np.column_stack([px, py]).reshape(-1, 1, 2)

    colors_idx = _greedy_face_colors(partition)
    # Warm birch plywood BGR tones (lighter pale birch for helleristninger, richer golden amber for trilobites)
    birch_bgr = [
        (196, 224, 242),
        (184, 215, 236),
        (206, 232, 246),
        (178, 209, 230),
        (192, 220, 238),
        (200, 227, 244),
    ]
    trilo_bgr = [
        (124, 178, 218),
        (110, 166, 210),
        (136, 188, 224),
        (118, 172, 214),
    ]

    roi_box = shapely.geometry.box(rx0 - 5.0, ry0 - 5.0, rx1 + 5.0, ry1 + 5.0)

    # 1. Fill faces
    for f in partition.faces:
        if not f.polygon.intersects(roi_box):
            continue
        is_trilo = f.metadata.get("kind") == "trilobite"
        if wood_mode:
            bgr = trilo_bgr[colors_idx[f.face_id] % len(trilo_bgr)] if is_trilo else birch_bgr[colors_idx[f.face_id] % len(birch_bgr)]
        else:
            hex_c = NORDIC_PALETTE[colors_idx[f.face_id] % len(NORDIC_PALETTE)].lstrip("#")
            r, g, b = int(hex_c[0:2], 16), int(hex_c[2:4], 16), int(hex_c[4:6], 16)
            if is_trilo:
                r = min(255, int(r * 1.10 + 18))
                g = min(255, int(g * 0.96 + 10))
                b = max(0, int(b * 0.80))
            bgr = (b, g, r)
        pts = to_px_arr(list(f.polygon.exterior.coords))
        cv2.fillPoly(canvas, [pts], bgr, lineType=cv2.LINE_AA)

    # Optional faint target silhouette overlay for visual fidelity debugging
    if show_silhouettes is not None:
        for fid, sil in enumerate(show_silhouettes):
            if not sil.intersects(roi_box):
                continue
            pts = to_px_arr(list(sil.exterior.coords))
            cv2.polylines(canvas, [pts], True, (170, 95, 60), thickness=max(1, int(round(0.22 * scale * ss))), lineType=cv2.LINE_AA)

    # 2. Draw trilobite anatomical engraving lines (rich laser-etched umber brown)
    eng_thick = max(1, int(round(0.32 * scale * ss)))
    for fid, strokes in engrave_by_face.items():
        if not partition.faces[fid].polygon.intersects(roi_box):
            continue
        for st in strokes:
            pts = to_px_arr(st)
            cv2.polylines(canvas, [pts], False, (28, 58, 108), thickness=eng_thick, lineType=cv2.LINE_AA)

    # 3. Draw canonical interior & exterior dendritic cut seams (crisp dark laser-char line)
    cut_thick = max(2, int(round(0.45 * scale * ss)))
    ext_thick = max(3, int(round(0.68 * scale * ss)))
    for e in partition.interior_edges:
        ls = e.to_linestring()
        if not ls.intersects(roi_box):
            continue
        pts = to_px_arr(e.coords)
        cv2.polylines(canvas, [pts], False, (18, 28, 44), thickness=cut_thick, lineType=cv2.LINE_AA)
    for e in partition.exterior_edges:
        ls = e.to_linestring()
        if not ls.intersects(roi_box):
            continue
        pts = to_px_arr(e.coords)
        cv2.polylines(canvas, [pts], False, (12, 18, 28), thickness=ext_thick, lineType=cv2.LINE_AA)

    # Downsample 2x with Lanczos/AREA for buttery anti-aliased crispness
    small = cv2.resize(canvas, (width_px, height_px), interpolation=cv2.INTER_AREA)
    rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb)

    if title:
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, width_px, hdr + 6], fill=(246, 243, 237))
        font = _load_font(max(18, int(width_px / 90)))
        draw.text((margin, 14), title, fill=(28, 24, 20), font=font)
    return img


def render_contact_sheet_subset(
    partition: PlanarPartition,
    face_ids: Sequence[int],
    placed_silhouettes: Sequence[Polygon],
    engrave_by_face: Dict[int, List[Polyline]],
    engrave_audit_by_face: Dict[int, Dict[str, Any]],
    tip_by_face: Dict[int, float],
    title: str,
    out_path: Path,
    cols: int = 6,
) -> None:
    """Render a crisp visual QA contact sheet of selected isolated pieces with final cut + target + engraving."""
    n = len(face_ids)
    if n == 0:
        return
    rows = int(math.ceil(n / float(cols)))
    tile_w, tile_h = 260, 296
    pad, hdr = 14, 52
    W = pad + cols * (tile_w + pad)
    H = hdr + pad + rows * (tile_h + pad)
    canvas = Image.new("RGB", (W, H), (244, 241, 235))
    draw = ImageDraw.Draw(canvas)
    font_hdr = _load_font(20)
    font_b = _load_font(13)
    font_s = _load_font(11)
    draw.text((pad, 14), title, fill=(24, 22, 20), font=font_hdr)

    for idx, fid in enumerate(face_ids):
        r, c = divmod(idx, cols)
        tx = pad + c * (tile_w + pad)
        ty = hdr + pad + r * (tile_h + pad)
        draw.rectangle([tx, ty, tx + tile_w, ty + tile_h], fill=(255, 255, 255), outline=(206, 200, 192), width=1)

        face = partition.faces[fid]
        poly = face.polygon
        sil = placed_silhouettes[fid]
        comb = unary_union([poly, sil])
        bx0, by0, bx1, by1 = comb.bounds
        span = max(bx1 - bx0, by1 - by0, 1.0)
        box_sz = 196
        sc = (box_sz - 16) / span
        ox = tx + tile_w * 0.5 - 0.5 * (bx0 + bx1) * sc
        oy = ty + 112 - 0.5 * (by0 + by1) * sc

        def mpt(pt: Tuple[float, float]) -> Tuple[float, float]:
            return (ox + pt[0] * sc, oy + pt[1] * sc)

        is_trilo = face.metadata.get("kind") == "trilobite"
        fill_col = (224, 192, 142) if is_trilo else (238, 225, 200)
        # Actual cut piece
        draw.polygon([mpt(p) for p in poly.exterior.coords], fill=fill_col, outline=(36, 22, 14), width=2)
        # Target source silhouette outline in terracotta red for instant shape-fidelity check!
        sil_pts = [mpt(p) for p in sil.exterior.coords]
        for k in range(0, len(sil_pts) - 1, 2):
            draw.line([sil_pts[k], sil_pts[k + 1]], fill=(195, 68, 42), width=2)
        # Final clipped engraving paths on trilobites
        for st in engrave_by_face.get(fid, []):
            draw.line([mpt(p) for p in st], fill=(82, 40, 18), width=2)

        # Captions
        meta = face.metadata
        name = str(meta.get("genus") or meta.get("display_class") or meta.get("motif_class") or "motif")
        iou = float(meta.get("aligned_iou", 0.0))
        tip = tip_by_face.get(fid, 0.0)
        draw.text((tx + 10, ty + 216), f"#{fid + 1} [{meta.get('kind', '')[:5]}] {name[:18]}", fill=(22, 20, 18), font=font_b)
        draw.text((tx + 10, ty + 236), f"Area {face.area_mm2:.0f} mm² | IoU {iou:.3f} | Tip {tip:.1f} mm", fill=(65, 60, 56), font=font_s)
        if is_trilo:
            ea = engrave_audit_by_face.get(fid, {})
            clr = ea.get("min_clearance_mm")
            clr_s = f"{clr:.2f} mm" if isinstance(clr, (int, float)) else "n/a"
            draw.text((tx + 10, ty + 254), f"Engrave {ea.get('num_strokes', 0)} strokes | clr {clr_s}", fill=(38, 105, 58), font=font_s)
        else:
            site = str(meta.get("site", ""))[:26]
            lic = str(meta.get("license", ""))[:18]
            draw.text((tx + 10, ty + 254), f"{site} ({lic})", fill=(95, 88, 82), font=font_s)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out_path), format="PNG", optimize=True)


# ---------------------------------------------------------------------------
# Complete Bundle Export Entrypoint
# ---------------------------------------------------------------------------

def export_dendrite_trilobite_bundle(
    out_dir: str | Path,
    partition: PlanarPartition,
    motifs: Sequence[PuzzleMotif],
    placed_silhouettes: Sequence[Polygon],
    engrave_by_face: Dict[int, List[Polyline]],
    engrave_audit: Dict[str, Any],
    rights_summary: Dict[str, Any],
    prod_audit: Dict[str, Any],
    val_report: Dict[str, Any],
    config_payload: Dict[str, Any],
    log: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """Export all laser SVGs, JSON audits, per-piece files, and high-res visual QA renders."""
    out_dir = Path(out_dir)
    pieces_dir = out_dir / "pieces"
    pieces_dir.mkdir(parents=True, exist_ok=True)

    # 1. Authoritative SVGs (strictly separated cut.svg, engrave.svg, production.svg)
    write_outline_svg(partition.outline, out_dir / "outline.svg")
    write_cut_svg(partition, out_dir / "cut.svg", kerf_mm=0.15)
    write_engrave_svg(partition, engrave_by_face, out_dir / "engrave.svg", stroke_mm=0.20)
    write_production_svg(partition, engrave_by_face, out_dir / "production.svg", kerf_mm=0.15, engrave_stroke_mm=0.20)
    write_pieces_svg(partition, out_dir / "pieces.svg")
    write_dendrite_assembly_svg(partition, engrave_by_face, out_dir / "assembly.svg")

    # 2. Isolated per-piece SVGs + 256x256 normalized silhouette PNGs
    for idx, face in enumerate(partition.faces, start=1):
        write_isolated_piece_svg(face.polygon, pieces_dir / f"{idx:04d}.svg", idx, face.metadata)
        mask = rasterize_normalized_piece_mask(face.polygon, size=256)
        Image.fromarray(mask, mode="L").save(str(pieces_dir / f"{idx:04d}.png"), format="PNG")

    # 3. Independent SVG re-import verification!
    svg_verify = verify_exported_svgs(
        out_dir=out_dir,
        expected_n=partition.num_pieces,
        expected_trilobites=int(engrave_audit["trilobite_count"]),
        min_segment_mm=0.05,
        min_engrave_clearance_mm=1.50,
    )
    prod_audit["svg_reimport"] = svg_verify
    prod_audit["gates"]["svg_reimport_all_passed"] = bool(svg_verify["all_svg_gates_passed"])
    prod_audit["all_production_gates_passed"] = all(prod_audit["gates"].values())

    # 4. Write all JSON & CSV artifacts
    (out_dir / "config.json").write_text(json.dumps(config_payload, indent=2), encoding="utf-8")
    (out_dir / "partition.json").write_text(json.dumps(partition.to_dict(), indent=2), encoding="utf-8")
    (out_dir / "rights_summary.json").write_text(json.dumps(rights_summary, indent=2), encoding="utf-8")
    (out_dir / "provenance.json").write_text(
        json.dumps(
            {
                "edition": "dendrite-trilobite-750",
                "num_pieces": partition.num_pieces,
                "trilobites": rights_summary["trilobite_count"],
                "helleristninger": rights_summary["helleristning_count"],
                "alta_nc_sa_count": rights_summary["alta_nc_sa_count"],
                "share_alike_count": rights_summary["share_alike_count"],
                "license_breakdown": rights_summary["license_breakdown"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    quality_payload = {
        "all_hard_gates_passed": bool(val_report["all_hard_gates_passed"] and svg_verify["all_svg_gates_passed"]),
        "gates": {**val_report["gates"], **svg_verify["gates"]},
        "summary": {**val_report["summary"], **prod_audit["fidelity"]},
        "diagnostics": val_report["diagnostics"],
        "svg_reimport": svg_verify,
        "engraving_clearance": prod_audit["engraving_clearance"],
        "lock_360": prod_audit["lock_360"],
        "per_piece": val_report["per_piece"],
    }
    (out_dir / "quality.json").write_text(json.dumps(quality_payload, indent=2), encoding="utf-8")
    (out_dir / "production-audit.json").write_text(json.dumps(prod_audit, indent=2), encoding="utf-8")
    (out_dir / "engraving-audit.json").write_text(json.dumps(engrave_audit, indent=2), encoding="utf-8")

    # Metrics CSV
    tip_by_face = {}
    for row in prod_audit["thin_feature"].get("top_10_tips", []):
        tip_by_face[int(row["piece"]) - 1] = float(row["max_tip_loss_mm"])
    # Compute exact tip map for all 750 for CSV & contact sheets
    from heller_jigsaw.production.audit import measure as measure_tip
    for f in partition.faces:
        if f.face_id not in tip_by_face:
            tip_by_face[f.face_id] = float(measure_tip(f.polygon, kerf=0.25, r=1.5)["max_tip_loss_mm"])

    eng_by_face_audit = {int(r["face_id"]): r for r in engrave_audit.get("per_trilobite", [])}
    csv_cols = [
        "piece_num", "face_id", "kind", "asset_id", "genus", "motif_class", "site", "license",
        "commercial_ok", "area_mm2", "perimeter_mm", "aligned_iou", "direct_iou",
        "max_tip_loss_mm", "kerf_pass", "neck_pass", "engrave_strokes", "engrave_min_clearance_mm",
    ]
    with open(out_dir / "metrics.csv", "w", encoding="utf-8", newline="") as cf:
        writer = csv.DictWriter(cf, fieldnames=csv_cols)
        writer.writeheader()
        for idx, pp in enumerate(val_report["per_piece"]):
            f = partition.faces[idx]
            m = f.metadata
            ea = eng_by_face_audit.get(idx, {})
            writer.writerow({
                "piece_num": idx + 1,
                "face_id": idx,
                "kind": m.get("kind", ""),
                "asset_id": m.get("asset_id", ""),
                "genus": m.get("genus", ""),
                "motif_class": m.get("display_class") or m.get("motif_class", ""),
                "site": m.get("site", ""),
                "license": m.get("license", ""),
                "commercial_ok": m.get("commercial_ok", False),
                "area_mm2": round(f.area_mm2, 2),
                "perimeter_mm": round(f.perimeter_mm, 2),
                "aligned_iou": m.get("aligned_iou", ""),
                "direct_iou": m.get("direct_iou", ""),
                "max_tip_loss_mm": round(tip_by_face[idx], 3),
                "kerf_pass": pp["kerf_pass"],
                "neck_pass": pp["neck_pass"],
                "engrave_strokes": ea.get("num_strokes", 0),
                "engrave_min_clearance_mm": ea.get("min_clearance_mm", ""),
            })

    # 5. High-resolution visual inspection renders!
    bx0, by0, bx1, by1 = partition.outline.bounds
    w_mm, h_mm = bx1 - bx0, by1 - by0
    title_str = (
        f"{partition.num_pieces}-Piece Dendrite + Trilobites ({rights_summary['trilobite_count']}) + "
        f"Helleristninger ({rights_summary['helleristning_count']}) | "
        f"{w_mm:.1f} x {h_mm:.1f} mm | IoU {prod_audit['fidelity']['mean_aligned_iou']:.3f} | "
        f"Lock {prod_audit['lock_360']['lock_margin_mm']:.2f} mm"
    )
    img_wood = _render_region_cv2(
        partition, engrave_by_face, (bx0, by0, bx1, by1), width_px=3400, wood_mode=True, title=title_str
    )
    img_wood.save(str(out_dir / "assembly.png"), format="PNG", optimize=True)

    img_col = _render_region_cv2(
        partition, engrave_by_face, (bx0, by0, bx1, by1), width_px=3000, wood_mode=False, title=title_str
    )
    img_col.save(str(out_dir / "assembly_colored.png"), format="PNG", optimize=True)

    # Zoomed 1:1 high-detail crops: center (trilobites + helleristninger + dendritic curls) & border!
    cx_m, cy_m = 0.5 * (bx0 + bx1), 0.5 * (by0 + by1)
    crop_w, crop_h = 210.0, 155.0
    crop_center = _render_region_cv2(
        partition,
        engrave_by_face,
        (cx_m - crop_w * 0.5, cy_m - crop_h * 0.5, cx_m + crop_w * 0.5, cy_m + crop_h * 0.5),
        width_px=1800,
        wood_mode=True,
        title="Zoomed Center Detail — Multi-Genus Engraved Trilobites + Helleristninger + Dendritic Seams",
    )
    crop_center.save(str(out_dir / "crop_trilobite_dendrite_center.png"), format="PNG", optimize=True)

    crop_border = _render_region_cv2(
        partition,
        engrave_by_face,
        (bx0 - 6.0, cy_m - crop_h * 0.45, bx0 + crop_w - 6.0, cy_m + crop_h * 0.55),
        width_px=1800,
        wood_mode=True,
        title="Zoomed Organic Rock-Slab Border Detail — Intact Border Motifs + Dendritic Interlock",
    )
    crop_border.save(str(out_dir / "crop_trilobite_dendrite_border.png"), format="PNG", optimize=True)

    # Contact sheets: 24 trilobites, 24 helleristninger, and 24 weakest pieces (honest QA!)
    trilo_fids = [f.face_id for f in partition.faces if f.metadata.get("kind") == "trilobite"]
    heller_fids = [f.face_id for f in partition.faces if f.metadata.get("kind") == "helleristning"]

    sample_trilos = trilo_fids[::max(1, len(trilo_fids) // 24)][:24]
    sample_hellers = heller_fids[::max(1, len(heller_fids) // 24)][:24]
    weakest_iou = sorted(range(partition.num_pieces), key=lambda i: float(partition.faces[i].metadata.get("aligned_iou", 0.0)))[:18]
    worst_tips = sorted(range(partition.num_pieces), key=lambda i: -tip_by_face[i])[:6]
    weakest_24 = list(dict.fromkeys(weakest_iou + worst_tips))[:24]

    render_contact_sheet_subset(
        partition, sample_trilos, placed_silhouettes, engrave_by_face, eng_by_face_audit, tip_by_face,
        title="Sample Final Cut Trilobite Pieces (24 of 150) — Cut + Engraving + Red Target Outline",
        out_path=out_dir / "contact_sheet_trilobites.png",
    )
    render_contact_sheet_subset(
        partition, sample_hellers, placed_silhouettes, engrave_by_face, eng_by_face_audit, tip_by_face,
        title="Sample Final Cut Helleristning Pieces (24 of 600) — Cut + Red Target Outline",
        out_path=out_dir / "contact_sheet_helleristninger.png",
    )
    render_contact_sheet_subset(
        partition, weakest_24, placed_silhouettes, engrave_by_face, eng_by_face_audit, tip_by_face,
        title="Weakest 24 Pieces (18 Lowest IoU + 6 Tightest Tips Across All 750 Pieces)",
        out_path=out_dir / "contact_sheet_weakest.png",
    )

    log(f"[export] Complete bundle written to {out_dir} | svg_reimport_passed={svg_verify['all_svg_gates_passed']} | all_prod_passed={prod_audit['all_production_gates_passed']}")
    return prod_audit
