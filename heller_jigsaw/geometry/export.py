"""Dossier Section 5.2 puzzle deliverable exporter (mm-scale SVGs, isolated masks, contact sheets, JSON/CSV)."""

from __future__ import annotations

import csv
import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import Polygon

from .partition import CanonicalEdge, PlanarPartition
from .validate import ValidationConfig, validate_partition, validate_run_dir

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

# Earthy Nordic rock-art palette (ochre, slate, birch, fjord, lichen, granite, rust, moss)
NORDIC_PALETTE = [
    "#D97D54",  # red ochre (oppmalt helleristning)
    "#6B8E9B",  # fjord slate blue
    "#C8A96E",  # warm birch gold
    "#7D9B76",  # tundra lichen green
    "#B66E6E",  # iron oxide terracotta
    "#8D7B9B",  # twilight heather
    "#5E8C7F",  # boreal pine teal
    "#D49A6A",  # amber sandstone
    "#7E8FA6",  # granite blue-grey
    "#A88C68",  # weathered driftwood
    "#C27A84",  # berry ochre
    "#6E9B88",  # coastal sage
    "#B59E5F",  # bronze patina
    "#877F99",  # arctic slate
    "#CE8B63",  # roasted sienna
    "#698396",  # deep fjord
]


def _load_font(size: int = 14) -> ImageFont.ImageFont:
    for candidate in (
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _coords_to_open_d(coords: Sequence[Tuple[float, float]]) -> str:
    if not coords:
        return ""
    parts = [f"M {coords[0][0]:.4f} {coords[0][1]:.4f}"]
    for x, y in coords[1:]:
        parts.append(f"L {x:.4f} {y:.4f}")
    return " ".join(parts)


def _coords_to_closed_d(coords: Sequence[Tuple[float, float]]) -> str:
    pts = list(coords)
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return ""
    parts = [f"M {pts[0][0]:.4f} {pts[0][1]:.4f}"]
    for x, y in pts[1:]:
        parts.append(f"L {x:.4f} {y:.4f}")
    parts.append("Z")
    return " ".join(parts)


def _make_svg_root(width_mm: float, height_mm: float, x0: float = 0.0, y0: float = 0.0) -> ET.Element:
    return ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": f"{width_mm:.4f}mm",
            "height": f"{height_mm:.4f}mm",
            "viewBox": f"{x0:.4f} {y0:.4f} {width_mm:.4f} {height_mm:.4f}",
            "version": "1.1",
        },
    )


def _write_svg(root: ET.Element, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(str(path), encoding="utf-8", xml_declaration=True)


def write_outline_svg(outline: Polygon, path: Path) -> None:
    x0, y0, x1, y1 = outline.bounds
    root = _make_svg_root(x1 - x0, y1 - y0, x0, y0)
    ET.SubElement(
        root,
        f"{{{SVG_NS}}}path",
        {
            "id": "target-outline",
            "d": _coords_to_closed_d(list(outline.exterior.coords)),
            "fill": "none",
            "stroke": "#000000",
            "stroke-width": "0.25",
        },
    )
    _write_svg(root, path)


def write_cut_svg(partition: PlanarPartition, path: Path, kerf_mm: float = 0.15) -> None:
    """Export unique shared laser-cut paths ONLY (interior cuts first, then exterior outline).

    Contains NO fills, NO text labels, NO duplicate coincident edges. Units in mm.
    """
    x0, y0, x1, y1 = partition.outline.bounds
    root = _make_svg_root(x1 - x0, y1 - y0, x0, y0)
    stroke_w = f"{kerf_mm:.4f}"

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


def write_pieces_svg(partition: PlanarPartition, path: Path) -> None:
    """Export closed labeled piece polygons for inspection (NOT a common-line cut file)."""
    x0, y0, x1, y1 = partition.outline.bounds
    root = _make_svg_root(x1 - x0, y1 - y0, x0, y0)
    g = ET.SubElement(root, f"{{{SVG_NS}}}g", {"id": "closed-pieces"})
    for f in partition.faces:
        motif = str(f.metadata.get("motif_class", f"piece_{f.face_id + 1}"))
        ET.SubElement(
            g,
            f"{{{SVG_NS}}}path",
            {
                "id": f"piece-{f.face_id + 1:04d}",
                "d": _coords_to_closed_d(list(f.polygon.exterior.coords)),
                "fill": "#F2EDE4",
                "fill-opacity": "0.65",
                "stroke": "#1F2328",
                "stroke-width": "0.35",
                "data-face-id": str(f.face_id),
                "data-motif-class": motif,
            },
        )
    _write_svg(root, path)


def write_assembly_svg(partition: PlanarPartition, path: Path) -> None:
    """Export color-coded assembled puzzle preview with piece labels."""
    x0, y0, x1, y1 = partition.outline.bounds
    w_mm, h_mm = x1 - x0, y1 - y0
    root = _make_svg_root(w_mm, h_mm, x0, y0)
    ET.SubElement(
        root,
        f"{{{SVG_NS}}}rect",
        {"x": f"{x0:.4f}", "y": f"{y0:.4f}", "width": f"{w_mm:.4f}", "height": f"{h_mm:.4f}", "fill": "#FAF7F2"},
    )
    g_faces = ET.SubElement(root, f"{{{SVG_NS}}}g", {"id": "assembly-faces"})
    g_labels = ET.SubElement(root, f"{{{SVG_NS}}}g", {"id": "assembly-labels"})
    font_size = max(3.5, min(7.0, min(w_mm, h_mm) / 38.0))

    for idx, f in enumerate(partition.faces):
        color = NORDIC_PALETTE[idx % len(NORDIC_PALETTE)]
        ET.SubElement(
            g_faces,
            f"{{{SVG_NS}}}path",
            {
                "id": f"asm-piece-{idx + 1:04d}",
                "d": _coords_to_closed_d(list(f.polygon.exterior.coords)),
                "fill": color,
                "fill-opacity": "0.86",
                "stroke": "#221E1B",
                "stroke-width": "0.6",
                "stroke-linejoin": "round",
            },
        )
        rp = f.polygon.representative_point()
        motif = str(f.metadata.get("motif_class", "")).replace("_", " ")
        label_txt = f"#{idx + 1}" + (f" {motif}" if motif else "")
        t_el = ET.SubElement(
            g_labels,
            f"{{{SVG_NS}}}text",
            {
                "x": f"{rp.x:.3f}",
                "y": f"{rp.y:.3f}",
                "text-anchor": "middle",
                "dominant-baseline": "central",
                "font-family": "sans-serif",
                "font-size": f"{font_size:.2f}",
                "font-weight": "bold",
                "fill": "#1A1816",
            },
        )
        t_el.text = label_txt

    _write_svg(root, path)


def rasterize_normalized_piece_mask(poly: Polygon, size: int = 256, pad_frac: float = 0.10) -> np.ndarray:
    """Render an isolated piece polygon into a normalized 256x256 uint8 mask (255=fg, 0=bg)."""
    x0, y0, x1, y1 = poly.bounds
    bw = max(x1 - x0, 1e-6)
    bh = max(y1 - y0, 1e-6)
    span = max(bw, bh)
    usable = size * (1.0 - 2.0 * pad_frac)
    scale = usable / span
    cx = 0.5 * (x0 + x1)
    cy = 0.5 * (y0 + y1)

    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    pts = [
        ((x - cx) * scale + size * 0.5, (y - cy) * scale + size * 0.5)
        for x, y in poly.exterior.coords
    ]
    if len(pts) >= 3:
        draw.polygon(pts, fill=255)
    return np.asarray(img, dtype=np.uint8)


def write_isolated_piece_svg(poly: Polygon, path: Path, piece_num: int, meta: Dict[str, Any]) -> None:
    x0, y0, x1, y1 = poly.bounds
    pad = 2.0
    w_mm = (x1 - x0) + 2.0 * pad
    h_mm = (y1 - y0) + 2.0 * pad
    shifted = [(x - x0 + pad, y - y0 + pad) for x, y in poly.exterior.coords]
    root = _make_svg_root(w_mm, h_mm, 0.0, 0.0)
    ET.SubElement(
        root,
        f"{{{SVG_NS}}}path",
        {
            "id": f"piece-{piece_num:04d}",
            "d": _coords_to_closed_d(shifted),
            "fill": "#111111",
            "stroke": "none",
            "data-motif-class": str(meta.get("motif_class", "")),
            "data-asset-id": str(meta.get("asset_id", "")),
        },
    )
    _write_svg(root, path)


def render_assembly_png(partition: PlanarPartition, path: Path, size_px: int = 1200) -> None:
    """Render a crisp high-res PNG preview of the assembled puzzle for visual inspection."""
    x0, y0, x1, y1 = partition.outline.bounds
    w_mm = max(x1 - x0, 1e-3)
    h_mm = max(y1 - y0, 1e-3)
    margin = 48
    scale = (size_px - 2 * margin) / max(w_mm, h_mm)
    img_w = int(round(w_mm * scale)) + 2 * margin
    img_h = int(round(h_mm * scale)) + 2 * margin

    img = Image.new("RGB", (img_w, img_h), (249, 246, 240))
    draw = ImageDraw.Draw(img)
    font = _load_font(max(13, int(size_px / 62)))
    font_small = _load_font(max(11, int(size_px / 78)))

    def to_px(pt: Tuple[float, float]) -> Tuple[float, float]:
        return ((pt[0] - x0) * scale + margin, (pt[1] - y0) * scale + margin)

    # Fill each piece
    for idx, f in enumerate(partition.faces):
        hex_col = NORDIC_PALETTE[idx % len(NORDIC_PALETTE)].lstrip("#")
        rgb = tuple(int(hex_col[k : k + 2], 16) for k in (0, 2, 4))
        pts = [to_px(p) for p in f.polygon.exterior.coords]
        draw.polygon(pts, fill=rgb)

    # Draw canonical shared cuts crisply
    for e in partition.interior_edges:
        pts = [to_px(p) for p in e.coords]
        draw.line(pts, fill=(32, 26, 22), width=3, joint="curve")
    for e in partition.exterior_edges:
        pts = [to_px(p) for p in e.coords]
        draw.line(pts, fill=(18, 16, 14), width=4, joint="curve")

    # Draw labels at pole of inaccessibility (representative point)
    for idx, f in enumerate(partition.faces):
        rp = f.polygon.representative_point()
        px_x, px_y = to_px((rp.x, rp.y))
        motif = str(f.metadata.get("motif_class", f"piece {idx + 1}")).replace("_", " ")
        iou = f.metadata.get("aligned_iou")
        line1 = f"#{idx + 1} {motif}"
        line2 = f"IoU {iou:.2f}" if isinstance(iou, (int, float)) else f"{f.area_mm2:.0f} mm²"
        for dy, txt, fnt in ((-9, line1, font), (9, line2, font_small)):
            bbox = draw.textbbox((px_x, px_y + dy), txt, font=fnt, anchor="mm")
            draw.rectangle(
                [bbox[0] - 4, bbox[1] - 2, bbox[2] + 4, bbox[3] + 2],
                fill=(255, 252, 246),
            )
            draw.text((px_x, px_y + dy), txt, fill=(22, 20, 18), font=fnt, anchor="mm")

    # Header bar with physical scale info
    hdr_font = _load_font(16)
    draw.text(
        (margin, 14),
        f"Helleristninger Jigsaw — {partition.num_pieces} pieces | {w_mm:.1f} x {h_mm:.1f} mm | "
        f"{len(partition.interior_edges)} shared interior cuts",
        fill=(30, 28, 25),
        font=hdr_font,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), format="PNG", optimize=True)


def render_pieces_contact_sheet(
    partition: PlanarPartition,
    piece_masks: List[np.ndarray],
    target_masks: Optional[List[Optional[np.ndarray]]],
    quality: Dict[str, Any],
    out_path: Path,
    unlabeled: bool = False,
) -> None:
    """Render crisp isolated black-on-white piece silhouette contact sheet."""
    n = len(partition.faces)
    if n == 0:
        return
    cols = min(4, n) if n <= 8 else min(4, max(3, int(math.ceil(math.sqrt(n)))))
    rows = int(math.ceil(n / float(cols)))
    tile_w = 300
    tile_h = 260 if unlabeled else 340
    pad = 16
    hdr_h = 48

    img_w = pad + cols * (tile_w + pad)
    img_h = hdr_h + pad + rows * (tile_h + pad)
    canvas = Image.new("RGB", (img_w, img_h), (242, 240, 236))
    draw = ImageDraw.Draw(canvas)

    font_hdr = _load_font(17)
    font_bold = _load_font(14)
    font_sm = _load_font(12)

    title = (
        f"Isolated Unlabeled Silhouettes ({n} pieces — blind recognition check)"
        if unlabeled
        else f"Helleristninger Puzzle Pieces ({n} pieces) — Isolated Piece vs Target Motif"
    )
    draw.text((pad, 14), title, fill=(24, 24, 24), font=font_hdr)

    per_piece_q = {p["face_id"]: p for p in quality.get("per_piece", [])}

    for idx, f in enumerate(partition.faces):
        r = idx // cols
        c = idx % cols
        tx = pad + c * (tile_w + pad)
        ty = hdr_h + pad + r * (tile_h + pad)

        draw.rectangle([tx, ty, tx + tile_w, ty + tile_h], fill=(255, 255, 255), outline=(210, 206, 200), width=1)

        # Render isolated black silhouette on white background
        mask = piece_masks[idx]
        sil_arr = np.where(mask > 127, 18, 255).astype(np.uint8)
        sil_img = Image.fromarray(sil_arr, mode="L").convert("RGB")
        sil_sz = 216 if unlabeled else 210
        sil_img = sil_img.resize((sil_sz, sil_sz), Image.Resampling.BILINEAR)
        canvas.paste(sil_img, (tx + (tile_w - sil_sz) // 2, ty + 14))

        if unlabeled:
            draw.text((tx + tile_w // 2, ty + tile_h - 18), f"Piece {idx + 1}", fill=(120, 120, 120), font=font_sm, anchor="mm")
            continue

        # Target motif reference thumbnail in top-right corner
        if target_masks and idx < len(target_masks) and target_masks[idx] is not None:
            tm = target_masks[idx]
            assert tm is not None
            t_arr = np.where(tm > 0.5, 165, 250).astype(np.uint8)
            t_img = Image.fromarray(t_arr, mode="L").convert("RGB").resize((66, 66), Image.Resampling.BILINEAR)
            t_rgb = Image.new("RGB", (66, 66), (250, 248, 245))
            t_mask_pil = Image.fromarray(((tm > 0.5).astype(np.uint8) * 255), mode="L").resize((66, 66), Image.Resampling.BILINEAR)
            t_rgb.paste((185, 78, 52), (0, 0), t_mask_pil)
            bx = tx + tile_w - 74
            by = ty + 8
            draw.rectangle([bx - 1, by - 1, bx + 67, by + 67], outline=(195, 180, 170), width=1)
            canvas.paste(t_rgb, (bx, by))
            draw.text((bx + 33, by + 74), "target", fill=(150, 85, 65), font=_load_font(10), anchor="mm")

        # Captions & QA metrics
        pq = per_piece_q.get(f.face_id, {})
        motif = str(f.metadata.get("motif_class", "motif")).replace("_", " ")
        site = str(f.metadata.get("site", ""))[:26]
        iou = f.metadata.get("aligned_iou")
        neck_ok = pq.get("neck_pass", True) and pq.get("kerf_pass", True)
        status_col = (34, 130, 62) if neck_ok else (195, 45, 45)
        status_txt = "Kerf/neck pass" if neck_ok else "Kerf/neck fail"

        cap_y = ty + 234
        draw.text((tx + 12, cap_y), f"#{idx + 1}: {motif}", fill=(20, 20, 20), font=font_bold)
        draw.text((tx + tile_w - 12, cap_y), status_txt, fill=status_col, font=font_bold, anchor="ra")
        iou_str = f"IoU={iou:.2f}" if isinstance(iou, (int, float)) else ""
        diam = pq.get("max_inscribed_diameter_mm", 0.0)
        draw.text(
            (tx + 12, cap_y + 24),
            f"Area {f.area_mm2:.0f} mm² | Ø {diam:.1f} mm | {iou_str}",
            fill=(65, 65, 65),
            font=font_sm,
        )
        blk = pq.get("blocked_fraction", 0.0)
        draw.text(
            (tx + 12, cap_y + 44),
            f"Block {blk * 100:.0f}% | core {pq.get('core_survival_ratio', 0.0) * 100:.0f}%",
            fill=(85, 85, 85),
            font=font_sm,
        )
        if site:
            draw.text((tx + 12, cap_y + 64), f"Origin: {site}", fill=(110, 105, 98), font=font_sm)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(out_path), format="PNG", optimize=True)


def export_puzzle_run(
    partition: PlanarPartition,
    out_dir: str | Path,
    config_payload: Dict[str, Any],
    provenance_payload: Dict[str, Any],
    log_lines: Sequence[str] = (),
    target_masks: Optional[List[Optional[np.ndarray]]] = None,
    validation_config: Optional[ValidationConfig] = None,
    recognition_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Export the complete Dossier Section 5.2 bundle and run independent validation including SVG re-import."""
    out_dir = Path(out_dir)
    pieces_dir = out_dir / "pieces"
    pieces_dir.mkdir(parents=True, exist_ok=True)

    vcfg = validation_config or ValidationConfig(
        kerf_mm=float(config_payload.get("kerf_mm", 0.15)),
        min_neck_mm=float(config_payload.get("min_neck_mm", 3.0)),
    )

    # 1. Config & provenance
    with open(out_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config_payload, f, indent=2)
    with open(out_dir / "provenance.json", "w", encoding="utf-8") as f:
        json.dump(provenance_payload, f, indent=2)

    # 2. Authoritative geometry SVGs & JSON
    write_outline_svg(partition.outline, out_dir / "outline.svg")
    write_cut_svg(partition, out_dir / "cut.svg", kerf_mm=vcfg.kerf_mm)
    write_pieces_svg(partition, out_dir / "pieces.svg")
    write_assembly_svg(partition, out_dir / "assembly.svg")
    render_assembly_png(partition, out_dir / "assembly.png")

    with open(out_dir / "partition.json", "w", encoding="utf-8") as f:
        json.dump(partition.to_dict(), f, indent=2)

    # 3. Isolated piece SVGs and 256x256 normalized silhouette PNGs
    piece_masks: List[np.ndarray] = []
    for idx, face in enumerate(partition.faces, start=1):
        write_isolated_piece_svg(face.polygon, pieces_dir / f"{idx:04d}.svg", idx, face.metadata)
        mask = rasterize_normalized_piece_mask(face.polygon, size=256)
        piece_masks.append(mask)
        Image.fromarray(mask, mode="L").save(str(pieces_dir / f"{idx:04d}.png"), format="PNG", optimize=True)

    # 4. Run partition validation + independent SVG re-import validation
    requested_n = int(config_payload.get("num_pieces", partition.num_pieces))
    part_quality = validate_partition(partition, expected_n=requested_n, config=vcfg)

    edge_face_ids = {
        e.left_face if e.left_face >= 0 else e.right_face
        for e in partition.exterior_edges
    }
    edge_ious: List[float] = []
    int_ious: List[float] = []
    all_ious: List[float] = []
    rec_by_num = {
        int(p["piece_num"]): p
        for p in (recognition_report.get("pieces", []) if recognition_report else [])
    }

    # Write initial metrics.csv before validate_run_dir checks required files
    csv_cols = [
        "piece_num",
        "face_id",
        "asset_id",
        "family",
        "motif_class",
        "site",
        "country",
        "is_edge_piece",
        "area_mm2",
        "perimeter_mm",
        "aligned_iou",
        "sdf_chamfer",
        "max_inscribed_diameter_mm",
        "core_survival_ratio",
        "kerf_pass",
        "neck_pass",
        "blocked_fraction",
        "escape_cone_deg",
        "num_neighbors",
        "pred_family",
        "pred_class_top1",
        "embed_target_cos",
    ]
    with open(out_dir / "metrics.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols)
        writer.writeheader()
        for idx, pp in enumerate(part_quality["per_piece"], start=1):
            meta = partition.faces[idx - 1].metadata
            is_edge = pp["face_id"] in edge_face_ids
            pp["is_edge_piece"] = is_edge
            iou_val = meta.get("aligned_iou")
            if isinstance(iou_val, (int, float)):
                all_ious.append(float(iou_val))
                (edge_ious if is_edge else int_ious).append(float(iou_val))
            rec_p = rec_by_num.get(idx, {})
            writer.writerow({
                "piece_num": idx,
                "face_id": pp["face_id"],
                "asset_id": meta.get("asset_id", ""),
                "family": meta.get("family", ""),
                "motif_class": meta.get("motif_class", ""),
                "site": meta.get("site", ""),
                "country": meta.get("country", ""),
                "is_edge_piece": is_edge,
                "area_mm2": pp["area_mm2"],
                "perimeter_mm": pp["perimeter_mm"],
                "aligned_iou": meta.get("aligned_iou", ""),
                "sdf_chamfer": meta.get("sdf_chamfer", ""),
                "max_inscribed_diameter_mm": pp["max_inscribed_diameter_mm"],
                "core_survival_ratio": pp["core_survival_ratio"],
                "kerf_pass": pp["kerf_pass"],
                "neck_pass": pp["neck_pass"],
                "blocked_fraction": pp.get("blocked_fraction", ""),
                "escape_cone_deg": pp.get("escape_cone_deg", ""),
                "num_neighbors": pp["num_neighbors"],
                "pred_family": rec_p.get("pred_family", ""),
                "pred_class_top1": rec_p.get("pred_class_top1", ""),
                "embed_target_cos": rec_p.get("embed_target_cos", ""),
            })

    summary = dict(part_quality["summary"])
    summary["num_edge_pieces"] = sum(1 for f in partition.faces if f.face_id in edge_face_ids)
    summary["num_interior_pieces"] = partition.num_pieces - summary["num_edge_pieces"]
    if all_ious:
        summary["mean_aligned_iou"] = round(float(np.mean(all_ious)), 4)
        summary["min_aligned_iou"] = round(float(np.min(all_ious)), 4)
    if edge_ious:
        summary["edge_mean_aligned_iou"] = round(float(np.mean(edge_ious)), 4)
    if int_ious:
        summary["interior_mean_aligned_iou"] = round(float(np.mean(int_ious)), 4)

    # Independent run directory + SVG re-import validation
    reimport_quality = validate_run_dir(out_dir, config=vcfg)

    combined_gates = dict(part_quality["gates"])
    for k, v in reimport_quality["gates"].items():
        combined_gates[k] = bool(combined_gates.get(k, True) and v)

    quality_payload: Dict[str, Any] = {
        "all_hard_gates_passed": all(combined_gates.values()),
        "gates": combined_gates,
        "summary": summary,
        "diagnostics": part_quality["diagnostics"],
        "svg_reimport": reimport_quality["svg_reimport"],
        "per_piece": part_quality["per_piece"],
        "config": part_quality["config"],
    }
    if recognition_report is not None:
        quality_payload["recognition"] = recognition_report
        with open(out_dir / "recognition.json", "w", encoding="utf-8") as f:
            json.dump(recognition_report, f, indent=2)

    with open(out_dir / "quality.json", "w", encoding="utf-8") as f:
        json.dump(quality_payload, f, indent=2)

    # 5. Contact sheets (labeled with target motif + unlabeled blind silhouette sheet)
    render_pieces_contact_sheet(
        partition,
        piece_masks=piece_masks,
        target_masks=target_masks,
        quality=quality_payload,
        out_path=out_dir / "pieces_contact_sheet.png",
        unlabeled=False,
    )
    render_pieces_contact_sheet(
        partition,
        piece_masks=piece_masks,
        target_masks=None,
        quality=quality_payload,
        out_path=out_dir / "pieces_unlabeled_sheet.png",
        unlabeled=True,
    )

    # 6. Logs
    log_text = list(log_lines)
    log_text.append(f"Exported bundle to: {out_dir}")
    log_text.append(f"All hard gates passed: {quality_payload['all_hard_gates_passed']}")
    for gk, gv in combined_gates.items():
        log_text.append(f"  gate[{gk}] = {gv}")
    with open(out_dir / "logs.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(log_text) + "\n")

    return quality_payload
