"""Export per-motif artifacts (SVG, mask PNG, polygon JSON, SDF NPZ), catalogs, contact sheets, and Figma SVG sheets."""

from __future__ import annotations

import csv
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .silhouette import MASK_SIZE_PX, NOMINAL_PIECE_MM, VIEWBOX_SIZE_MM, compute_sdf_256

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def _safe_xml_id(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_\-]+", "_", text.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "m_" + cleaned
    return cleaned[:96]


def coords_to_svg_path_d(coords: List[Tuple[float, float]]) -> str:
    """Convert closed polygon coordinate list [(x, y), ...] into a clean SVG path d string."""
    if not coords:
        return ""
    pts = list(coords)
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    parts = [f"M {pts[0][0]:.3f},{pts[0][1]:.3f}"]
    for x, y in pts[1:]:
        parts.append(f"L {x:.3f},{y:.3f}")
    parts.append("Z")
    return " ".join(parts)


def write_motif_svg(path: Path, asset_id: str, coords: List[Tuple[float, float]], meta: Optional[Dict[str, Any]] = None) -> None:
    """Write a clean single-path 100x100 mm SVG ready for Figma and the geometry kernel."""
    svg = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "version": "1.1",
            "viewBox": f"0 0 {int(VIEWBOX_SIZE_MM)} {int(VIEWBOX_SIZE_MM)}",
            "width": f"{int(VIEWBOX_SIZE_MM)}mm",
            "height": f"{int(VIEWBOX_SIZE_MM)}mm",
        },
    )
    attrs = {
        "id": _safe_xml_id(asset_id),
        "d": coords_to_svg_path_d(coords),
        "fill": "#111111",
        "stroke": "none",
    }
    if meta:
        if meta.get("motif_class"):
            attrs["data-motif-class"] = str(meta["motif_class"])
        if meta.get("jigsaw_tier"):
            attrs["data-jigsaw-tier"] = str(meta["jigsaw_tier"])
        if meta.get("render_style"):
            attrs["data-render-style"] = str(meta["render_style"])
    ET.SubElement(svg, f"{{{SVG_NS}}}path", attrs)
    tree = ET.ElementTree(svg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def write_motif_mask(path: Path, mask_256: np.ndarray) -> None:
    """Write 256x256 binary uint8 grayscale PNG (255=foreground white, 0=background black)."""
    assert mask_256.shape == (MASK_SIZE_PX, MASK_SIZE_PX), f"Bad mask shape {mask_256.shape}"
    out = ((mask_256 > 0).astype(np.uint8)) * 255
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(out, mode="L").save(path, format="PNG", optimize=True)


def write_motif_polygon_json(
    path: Path,
    asset_id: str,
    coords: List[Tuple[float, float]],
    metrics: Dict[str, Any],
) -> None:
    """Write normalized exterior polygon JSON with simplification & fabrication stats."""
    payload = {
        "asset_id": asset_id,
        "units": "mm",
        "viewbox_mm": [0.0, 0.0, VIEWBOX_SIZE_MM, VIEWBOX_SIZE_MM],
        "nominal_piece_mm": NOMINAL_PIECE_MM,
        "exterior": [[float(x), float(y)] for x, y in coords],
        "interiors": [],
        "connected_components": int(metrics.get("connected_components", 1)),
        "hole_count": int(metrics.get("hole_count", 0)),
        "vertex_count": int(metrics.get("vertex_count", max(0, len(coords) - 1))),
        "metrics": metrics,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def write_motif_sdf_npz(path: Path, sdf_256: np.ndarray) -> None:
    """Write 256x256 float32 signed distance field NPZ (negative inside, positive outside)."""
    assert sdf_256.shape == (MASK_SIZE_PX, MASK_SIZE_PX), f"Bad sdf shape {sdf_256.shape}"
    sdf_f32 = sdf_256.astype(np.float32)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        sdf=sdf_f32,
        resolution=np.int32(MASK_SIZE_PX),
    )


def _load_font(size: int = 12) -> ImageFont.ImageFont:
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


def render_contact_sheet_png(
    records: List[Dict[str, Any]],
    masks: List[np.ndarray],
    out_path: Path,
    title: str,
    cols: int = 6,
    rows: int = 5,
    tile_w: int = 250,
    tile_h: int = 292,
) -> None:
    """Render a crisp high-res labeled PNG contact sheet (black silhouettes on white tiles)."""
    if not records:
        return
    n = len(records)
    actual_rows = min(rows, max(1, math.ceil(n / float(cols))))
    header_h = 52
    pad = 12
    img_w = pad + cols * (tile_w + pad)
    img_h = header_h + pad + actual_rows * (tile_h + pad)

    canvas = Image.new("RGB", (img_w, img_h), (246, 247, 249))
    draw = ImageDraw.Draw(canvas)
    font_title = _load_font(20)
    font_bold = _load_font(12)
    font_small = _load_font(11)

    draw.rectangle([0, 0, img_w, header_h], fill=(24, 28, 36))
    draw.text((16, 14), f"{title}  ({n} motifs)", fill=(255, 255, 255), font=font_title)

    tier_colors = {
        "A_ready": (22, 130, 68),
        "B_thickened": (26, 102, 194),
        "C_fragile_or_review": (196, 108, 18),
    }

    sil_box = 204
    for idx, (rec, mask) in enumerate(zip(records, masks)):
        r = idx // cols
        c = idx % cols
        if r >= actual_rows:
            break
        tx = pad + c * (tile_w + pad)
        ty = header_h + pad + r * (tile_h + pad)

        # Crisp white tile with subtle border
        draw.rectangle([tx, ty, tx + tile_w, ty + tile_h], fill=(255, 255, 255), outline=(212, 216, 224), width=1)

        # Convert mask (white=fg on black=bg) -> crisp black silhouette on white tile
        sil_img = Image.fromarray(np.where(mask > 0, 18, 255).astype(np.uint8), mode="L").convert("RGB")
        sil_img = sil_img.resize((sil_box, sil_box), Image.Resampling.LANCZOS)
        canvas.paste(sil_img, (tx + (tile_w - sil_box) // 2, ty + 8))

        # Subtle divider above caption
        cap_y = ty + sil_box + 14
        draw.line([tx + 8, cap_y - 3, tx + tile_w - 8, cap_y - 3], fill=(232, 235, 240), width=1)

        tier = str(rec.get("jigsaw_tier", ""))
        tier_short = {"A_ready": "TIER A", "B_thickened": "TIER B", "C_fragile_or_review": "TIER C"}.get(tier, tier)
        t_col = tier_colors.get(tier, (90, 90, 90))

        asset_id = str(rec.get("asset_id", ""))
        motif_cls = str(rec.get("motif_class", "unclassified"))
        country = str(rec.get("country", ""))
        site = str(rec.get("site") or rec.get("region") or rec.get("source_archive") or "")
        if len(site) > 20:
            site = site[:19] + "…"
        style = str(rec.get("render_style", "")).replace("_", " ")
        neck = rec.get("min_neck_mm_at_60mm", 0.0)
        arch = str(rec.get("source_archive", ""))[:8]

        # Line 1: Asset ID + colored tier badge
        draw.text((tx + 8, cap_y), asset_id, fill=(20, 24, 31), font=font_bold)
        draw.rectangle([tx + tile_w - 58, cap_y, tx + tile_w - 8, cap_y + 15], fill=t_col)
        draw.text((tx + tile_w - 53, cap_y + 1), tier_short, fill=(255, 255, 255), font=font_small)

        # Line 2: motif class + country:site
        line2 = f"{motif_cls} | {country}:{site}"
        if len(line2) > 34:
            line2 = line2[:33] + "…"
        draw.text((tx + 8, cap_y + 19), line2, fill=(45, 55, 72), font=font_small)

        # Line 3: style, neck thickness, archive
        line3 = f"{style} | neck:{neck:.1f}mm | {arch}"
        if len(line3) > 36:
            line3 = line3[:35] + "…"
        draw.text((tx + 8, cap_y + 36), line3, fill=(105, 114, 128), font=font_small)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG", optimize=True)


def write_figma_svg_sheet(
    records: List[Dict[str, Any]],
    coords_by_id: Dict[str, List[Tuple[float, float]]],
    out_path: Path,
    sheet_title: str,
    cols: int = 10,
    cell_mm: float = 100.0,
    gutter_mm: float = 18.0,
) -> None:
    """Write a clean multi-motif Figma-ready SVG asset sheet with named <g> groups and clean <path> vectors."""
    n = len(records)
    rows = max(1, math.ceil(n / float(cols))) if n > 0 else 1
    actual_cols = min(cols, max(1, n)) if n > 0 else 1
    header_mm = 32.0
    label_mm = 16.0
    step_x = cell_mm + gutter_mm
    step_y = cell_mm + label_mm + gutter_mm

    total_w = gutter_mm + actual_cols * step_x
    total_h = header_mm + gutter_mm + rows * step_y

    svg = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "version": "1.1",
            "viewBox": f"0 0 {total_w:.1f} {total_h:.1f}",
            "width": f"{total_w:.1f}mm",
            "height": f"{total_h:.1f}mm",
        },
    )

    # Separate background/header layer (easily hidden in Figma)
    g_chrome = ET.SubElement(svg, f"{{{SVG_NS}}}g", {"id": "sheet_header"})
    ET.SubElement(
        g_chrome,
        f"{{{SVG_NS}}}rect",
        {"x": "0", "y": "0", "width": f"{total_w:.1f}", "height": f"{total_h:.1f}", "fill": "#FFFFFF"},
    )
    hdr = ET.SubElement(
        g_chrome,
        f"{{{SVG_NS}}}text",
        {
            "x": f"{gutter_mm:.1f}",
            "y": "21",
            "font-family": "sans-serif",
            "font-size": "12",
            "font-weight": "bold",
            "fill": "#111827",
        },
    )
    hdr.text = f"{sheet_title} ({n} motifs — 100x100 mm cells)"

    # Pure silhouette vectors layer — each motif in its own cleanly named <g id="...">
    g_sil = ET.SubElement(svg, f"{{{SVG_NS}}}g", {"id": "silhouettes"})
    # Separate toggleable labels layer so users can hide captions with 1 click in Figma
    g_lbl = ET.SubElement(svg, f"{{{SVG_NS}}}g", {"id": "labels"})

    used_ids: set[str] = {"sheet_header", "silhouettes", "labels"}

    for idx, rec in enumerate(records):
        aid = str(rec["asset_id"])
        coords = coords_by_id.get(aid)
        if not coords:
            continue
        r = idx // cols
        c = idx % cols
        ox = gutter_mm + c * step_x
        oy = header_mm + gutter_mm + r * step_y

        motif_cls = str(rec.get("motif_class", "unclassified"))
        country = str(rec.get("country", "NORDIC"))
        site = str(rec.get("site") or "unknown")
        tier = str(rec.get("jigsaw_tier", ""))

        raw_gid = _safe_xml_id(f"{aid}__{motif_cls}__{country}_{site}__{tier}")
        gid = raw_gid
        suffix = 2
        while gid in used_ids:
            gid = f"{raw_gid}_{suffix}"
            suffix += 1
        used_ids.add(gid)

        pid = _safe_xml_id(f"path_{aid}")
        while pid in used_ids:
            pid = f"{pid}_x"
        used_ids.add(pid)

        g_item = ET.SubElement(
            g_sil,
            f"{{{SVG_NS}}}g",
            {
                "id": gid,
                "transform": f"translate({ox:.2f},{oy:.2f})",
                "data-asset-id": aid,
                "data-motif-class": motif_cls,
                "data-family": str(rec.get("family", "")),
                "data-country": country,
                "data-site": site,
                "data-tier": tier,
                "data-license": str(rec.get("license", "")),
            },
        )
        ET.SubElement(
            g_item,
            f"{{{SVG_NS}}}path",
            {
                "id": pid,
                "d": coords_to_svg_path_d(coords),
                "fill": "#111111",
                "stroke": "none",
            },
        )

        # Subtle tile frame & caption in labels layer
        ET.SubElement(
            g_lbl,
            f"{{{SVG_NS}}}rect",
            {
                "x": f"{ox:.2f}",
                "y": f"{oy:.2f}",
                "width": f"{cell_mm:.1f}",
                "height": f"{cell_mm:.1f}",
                "fill": "none",
                "stroke": "#E5E7EB",
                "stroke-width": "0.5",
            },
        )
        t1 = ET.SubElement(
            g_lbl,
            f"{{{SVG_NS}}}text",
            {
                "x": f"{ox + 1.5:.2f}",
                "y": f"{oy + cell_mm + 6.5:.2f}",
                "font-family": "sans-serif",
                "font-size": "5.2",
                "fill": "#374151",
            },
        )
        t1.text = f"{aid} · {motif_cls}"

        t2 = ET.SubElement(
            g_lbl,
            f"{{{SVG_NS}}}text",
            {
                "x": f"{ox + 1.5:.2f}",
                "y": f"{oy + cell_mm + 12.8:.2f}",
                "font-family": "sans-serif",
                "font-size": "4.6",
                "fill": "#6B7280",
            },
        )
        short_site = site if len(site) <= 18 else site[:17] + "…"
        t2.text = f"{country}:{short_site} · {tier}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(svg).write(out_path, encoding="utf-8", xml_declaration=True)


def export_corpus(
    items: List[Dict[str, Any]],
    output_dir: Path,
    pipeline_summary: Dict[str, Any],
    split_summary: Dict[str, Any],
    dedup_dropped: List[Dict[str, Any]],
    preserve_asset_ids: bool = False,
) -> Dict[str, Any]:
    """Write all per-motif files, catalog.json, catalog.csv, stats.json, contact sheets, and Figma sheets."""
    svg_dir = output_dir / "svg"
    mask_dir = output_dir / "masks"
    poly_dir = output_dir / "polygons"
    sdf_dir = output_dir / "sdf"
    cs_dir = output_dir / "contact_sheets"
    figma_dir = output_dir / "figma_sheets"

    for d in (svg_dir, mask_dir, poly_dir, sdf_dir, cs_dir, figma_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Sort deterministically for stable asset IDs: preserve existing asset_id ordering when requested
    if preserve_asset_ids and all(it.get("asset_id") for it in items):
        items_sorted = sorted(items, key=lambda it: str(it["asset_id"]))
    else:
        tier_ord = {"A_ready": 0, "B_thickened": 1, "C_fragile_or_review": 2}
        country_ord = {"NO": 0, "SE": 1, "DK": 2, "FI": 3, "NORDIC": 4}
        items_sorted = sorted(
            items,
            key=lambda it: (
                tier_ord.get(it["jigsaw_tier"], 9),
                country_ord.get(str(it.get("country", "NORDIC")), 9),
                str(it.get("family", "")),
                str(it.get("motif_class", "")),
                str(it.get("source_archive", "")),
                str(it.get("source_id", "")),
                int(it.get("panel_motif_index", 0)),
            ),
        )

    catalog: List[Dict[str, Any]] = []
    coords_by_id: Dict[str, List[Tuple[float, float]]] = {}
    masks_by_id: Dict[str, np.ndarray] = {}

    for idx, it in enumerate(items_sorted, start=1):
        asset_id = str(it["asset_id"]) if (preserve_asset_ids and it.get("asset_id")) else f"helleristning_{idx:05d}"
        it["asset_id"] = asset_id
        coords = it["coords"]
        mask_256 = it["mask_256"]
        sdf_256 = it.get("sdf_256")
        if sdf_256 is None:
            sdf_256 = compute_sdf_256(mask_256)
        metrics = it["metrics"]

        svg_rel = f"processed/svg/{asset_id}.svg"
        mask_rel = f"processed/masks/{asset_id}.png"
        poly_rel = f"processed/polygons/{asset_id}.json"
        sdf_rel = f"processed/sdf/{asset_id}.npz"

        write_motif_svg(svg_dir / f"{asset_id}.svg", asset_id, coords, meta={
            "motif_class": it["motif_class"],
            "jigsaw_tier": it["jigsaw_tier"],
            "render_style": it["render_style"],
        })
        write_motif_mask(mask_dir / f"{asset_id}.png", mask_256)
        write_motif_polygon_json(poly_dir / f"{asset_id}.json", asset_id, coords, metrics)
        write_motif_sdf_npz(sdf_dir / f"{asset_id}.npz", sdf_256)

        coords_by_id[asset_id] = coords
        masks_by_id[asset_id] = mask_256

        rec = {
            "asset_id": asset_id,
            "source_id": it["source_id"],
            "source_archive": it["source_archive"],
            "source_group": it["source_group"],
            "panel_motif_index": int(it.get("panel_motif_index", 0)),
            "panel_total_motifs": int(it.get("panel_total_motifs", 1)),
            "title": it.get("title", ""),
            "description": it.get("description", ""),
            "site": it.get("site", ""),
            "region": it.get("region", ""),
            "country": it.get("country", "NORDIC"),
            "period": it.get("period", "unknown"),
            "media_type": it.get("media_type", ""),
            "family": it["family"],
            "motif_class": it["motif_class"],
            "classification_confidence": float(it.get("classification_confidence", 0.0)),
            "motif_hints": it.get("motif_hints", []),
            "render_style": it["render_style"],
            "was_thickened": bool(it["was_thickened"]),
            "jigsaw_tier": it["jigsaw_tier"],
            "split": it["split"],
            "raw_path": it["raw_path"],
            "svg_path": svg_rel,
            "mask_path": mask_rel,
            "polygon_path": poly_rel,
            "sdf_path": sdf_rel,
            "source_url": it.get("source_url", ""),
            "download_url": it.get("download_url", ""),
            "creator": it.get("creator", ""),
            "license": it.get("license", ""),
            "license_url": it.get("license_url", ""),
            "commercial_ok": bool(it["commercial_ok"]),
            "sha256": it["sha256"],
            "shape_phash": it.get("shape_phash", ""),
            "connected_components": int(metrics["connected_components"]),
            "hole_count": int(metrics["hole_count"]),
            "area_mm2": float(metrics["area_mm2"]),
            "area_ratio": float(metrics["area_ratio"]),
            "solidity": float(metrics["solidity"]),
            "compactness": float(metrics["compactness"]),
            "aspect_ratio": float(metrics["aspect_ratio"]),
            "vertex_count": int(metrics["vertex_count"]),
            "erosion_survival_ratio": float(metrics["erosion_survival_ratio"]),
            "min_neck_norm": float(metrics["min_neck_norm"]),
            "min_neck_mm_at_60mm": float(metrics["min_neck_mm_at_60mm"]),
            "passes_kerf_gate": bool(metrics["passes_kerf_gate"]),
            "raw_bbox_px": list(it.get("raw_bbox_px", [0, 0, 0, 0])),
            "landmarks": {},
            "qa": {
                "reviewed": False,
                "kerf_gate_passed": bool(metrics["passes_kerf_gate"]),
                "erosion_components": int(metrics["erosion_components"]),
                "raster_erosion_components": int(metrics["raster_erosion_components"]),
            },
        }
        catalog.append(rec)

    # Write catalog.json
    catalog_json_path = output_dir / "catalog.json"
    with open(catalog_json_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)

    # Write catalog.csv
    csv_fields = [
        "asset_id", "source_id", "source_archive", "source_group", "country", "region", "site",
        "period", "media_type", "family", "motif_class", "classification_confidence",
        "render_style", "was_thickened", "jigsaw_tier", "split",
        "connected_components", "hole_count", "area_ratio", "solidity", "compactness",
        "aspect_ratio", "vertex_count", "erosion_survival_ratio", "min_neck_norm",
        "min_neck_mm_at_60mm", "passes_kerf_gate", "commercial_ok", "license", "creator",
        "sha256", "shape_phash", "raw_path", "svg_path", "mask_path", "polygon_path", "sdf_path",
        "source_url",
    ]
    catalog_csv_path = output_dir / "catalog.csv"
    with open(catalog_csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_fields, extrasaction="ignore")
        writer.writeheader()
        for rec in catalog:
            writer.writerow(rec)

    # Generate high-res visual contact sheets
    contact_sheet_files: List[str] = []
    page_size = 30  # 6x5 tiles

    def _emit_paged_sheets(subset: List[Dict[str, Any]], prefix: str, title_base: str) -> None:
        if not subset:
            return
        n_pages = math.ceil(len(subset) / float(page_size))
        for p in range(n_pages):
            batch = subset[p * page_size : (p + 1) * page_size]
            batch_masks = [masks_by_id[r["asset_id"]] for r in batch]
            fname = f"{prefix}_page_{p + 1:02d}.png"
            render_contact_sheet_png(
                batch,
                batch_masks,
                cs_dir / fname,
                title=f"{title_base} — Page {p + 1}/{n_pages}",
            )
            contact_sheet_files.append(f"processed/contact_sheets/{fname}")

    tier_a = [r for r in catalog if r["jigsaw_tier"] == "A_ready"]
    tier_b = [r for r in catalog if r["jigsaw_tier"] == "B_thickened"]
    tier_c = [r for r in catalog if r["jigsaw_tier"] == "C_fragile_or_review"]

    # Overview showcase: balanced sample across families from Tier A & B
    ab_pool = tier_a + tier_b
    overview_recs: List[Dict[str, Any]] = []
    by_fam: Dict[str, List[Dict[str, Any]]] = {}
    for r in ab_pool:
        by_fam.setdefault(r["family"], []).append(r)
    fams_order = ["animal", "vessel", "human", "symbol", "scene_or_unclassified"]
    idx_f = {f: 0 for f in fams_order}
    while len(overview_recs) < min(30, len(ab_pool)):
        progressed = False
        for f in fams_order:
            lst = by_fam.get(f, [])
            if idx_f[f] < len(lst) and len(overview_recs) < 30:
                overview_recs.append(lst[idx_f[f]])
                idx_f[f] += 1
                progressed = True
        if not progressed:
            break
    if overview_recs:
        render_contact_sheet_png(
            overview_recs,
            [masks_by_id[r["asset_id"]] for r in overview_recs],
            cs_dir / "overview_showcase.png",
            title="Nordic Helleristninger — Diverse Tier A & B Overview",
        )
        contact_sheet_files.append("processed/contact_sheets/overview_showcase.png")

    _emit_paged_sheets(tier_a, "tierA_ready", "Tier A — Laser-Ready Solid Silhouettes")
    _emit_paged_sheets(tier_b, "tierB_thickened", "Tier B — Controlled Stroke-Envelope Silhouettes")
    _emit_paged_sheets(tier_c, "tierC_review", "Tier C — Fragile / Manual Figma Review")

    # Generate Figma-ready SVG asset sheets
    figma_sheets_meta: Dict[str, int] = {}
    figma_specs = [
        ("tierA_norway.svg", "Tier A Laser-Ready — Norway", [r for r in tier_a if r["country"] == "NO"]),
        ("tierA_nordic.svg", "Tier A Laser-Ready — All Nordic", tier_a),
        ("tierB_thickened.svg", "Tier B Controlled Thickened — All Nordic", tier_b),
        ("by_family_animals.svg", "Nordic Helleristninger — Animals (Tier A & B)", [r for r in ab_pool if r["family"] == "animal"]),
        ("by_family_vessels.svg", "Nordic Helleristninger — Ships & Boats (Tier A & B)", [r for r in ab_pool if r["family"] == "vessel"]),
        ("by_family_humans.svg", "Nordic Helleristninger — Humans & Figures (Tier A & B)", [r for r in ab_pool if r["family"] == "human"]),
        ("by_family_symbols.svg", "Nordic Helleristninger — Symbols & Tools (Tier A & B)", [r for r in ab_pool if r["family"] == "symbol"]),
    ]
    unclass_ab = [r for r in ab_pool if r["family"] == "scene_or_unclassified"]
    if unclass_ab:
        figma_specs.append(("by_family_unclassified.svg", "Nordic Helleristninger — Scenes & Unclassified (Tier A & B)", unclass_ab))

    for fname, stitle, subset in figma_specs:
        write_figma_svg_sheet(subset, coords_by_id, figma_dir / fname, sheet_title=stitle)
        figma_sheets_meta[f"processed/figma_sheets/{fname}"] = len(subset)

    # Rich summary stats.json
    stats = {
        "schema_version": "1.0.0",
        "conventions": {
            "svg_viewbox_mm": [0, 0, int(VIEWBOX_SIZE_MM), int(VIEWBOX_SIZE_MM)],
            "svg_motif_max_span_mm": 80.0,
            "nominal_piece_mm": NOMINAL_PIECE_MM,
            "kerf_gate_min_neck_mm": 3.0,
            "kerf_gate_kerf_mm": 0.15,
            "mask_convention": "256x256 uint8 grayscale PNG, foreground=255 (white), background=0 (black)",
            "sdf_convention": "256x256 float32 npz key='sdf' (pixel units with 0.5px boundary offset, strictly negative inside foreground, strictly positive outside, |grad| ~ 1.0)",
        },
        "total_motifs": len(catalog),
        "tier_counts": dict(Counter(r["jigsaw_tier"] for r in catalog)),
        "by_archive": dict(Counter(r["source_archive"] for r in catalog)),
        "by_country": dict(Counter(r["country"] for r in catalog)),
        "by_region": dict(Counter(r["region"] or "unknown" for r in catalog)),
        "by_period": dict(Counter(r["period"] for r in catalog)),
        "by_media_type": dict(Counter(r["media_type"] for r in catalog)),
        "by_render_style": dict(Counter(r["render_style"] for r in catalog)),
        "by_family": dict(Counter(r["family"] for r in catalog)),
        "by_motif_class": dict(Counter(r["motif_class"] for r in catalog)),
        "by_license": dict(Counter(r["license"] for r in catalog)),
        "commercial_ok_counts": {
            "true": sum(1 for r in catalog if r["commercial_ok"]),
            "false": sum(1 for r in catalog if not r["commercial_ok"]),
        },
        "by_split": dict(Counter(r["split"] for r in catalog)),
        "split_summary": split_summary,
        "dedup_dropped_count": len(dedup_dropped),
        "dedup_dropped_sample": dedup_dropped[:25],
        "pipeline_ingest_summary": pipeline_summary,
        "contact_sheets": contact_sheet_files,
        "figma_sheets": figma_sheets_meta,
    }

    with open(output_dir / "stats.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, ensure_ascii=False)

    return stats
