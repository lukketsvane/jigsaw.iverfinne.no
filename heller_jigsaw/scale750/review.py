"""Visual renders, zoomed detail crops, weakest-pieces review packet, and standalone 750-piece HTML review tool."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw
from shapely.geometry import Polygon, box as sbox

from ..geometry.export import NORDIC_PALETTE, _load_font


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _greedy_coloring(adjacency: Dict[str, List[int]], n: int, k: int) -> List[int]:
    colors = [-1] * n
    for i in range(n):
        used = {colors[nb] for nb in adjacency.get(str(i), []) if 0 <= nb < n and colors[nb] >= 0}
        for c in range(k):
            if c not in used:
                colors[i] = c
                break
        if colors[i] < 0:
            colors[i] = i % k
    return colors


def _hex_to_rgb(hx: str) -> Tuple[int, int, int]:
    hx = hx.lstrip("#")
    return int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)


def _to_px(coords: Sequence[Sequence[float]], x0: float, y0: float, scale: float, ox: float, oy: float) -> List[Tuple[float, float]]:
    return [(ox + (float(x) - x0) * scale, oy + (float(y) - y0) * scale) for x, y in coords]


def render_assembly_and_crops(
    run_dir: str | Path,
    fidelity_report: Optional[Dict[str, Any]] = None,
    aligned_targets: Optional[Sequence[Sequence[Sequence[float]]]] = None,
    audit_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """Render high-res clean assembly.png, labeled assembly_labeled.png, and 4 zoomed detail crops."""
    p = Path(run_dir)
    d = json.loads((p / "partition.json").read_text(encoding="utf-8"))
    faces = d["faces"]
    edges = d["edges"]
    adj = d.get("adjacency", {})
    n = len(faces)
    x0, y0, x1, y1 = d["outline_bounds_mm"]
    W_mm, H_mm = x1 - x0, y1 - y0

    colors = _greedy_coloring(adj, n, len(NORDIC_PALETTE))
    fid_by_piece: Dict[int, Dict[str, Any]] = {}
    if fidelity_report and "per_piece" in fidelity_report:
        fid_by_piece = {int(r["piece"]): r for r in fidelity_report["per_piece"]}

    # 1. Clean high-res wooden-toned assembly.png (~3600 px wide)
    target_w = 3600
    margin_px = 60
    header_px = 110
    scale = (target_w - 2 * margin_px) / max(W_mm, 1.0)
    img_h = int(round(H_mm * scale)) + 2 * margin_px + header_px
    img = Image.new("RGB", (target_w, img_h), (244, 240, 232))
    draw = ImageDraw.Draw(img)

    title_font = _load_font(34)
    sub_font = _load_font(20)
    mean_iou = fidelity_report["summary"]["iou"]["mean"] if fidelity_report else 0.0
    mean_fid = fidelity_report["summary"]["fidelity"]["mean"] if fidelity_report else 0.0
    lock_mm = audit_report.get("lock_margin_mm", 0.0) if audit_report else 0.0
    worst_tip = audit_report.get("worst_tip_loss_mm", 0.0) if audit_report else 0.0

    draw.text((margin_px, 22), f"HELLERISTNINGER — {n} ALL-FIGURATIVE WOODEN JIGSAW", fill=(32, 26, 20), font=title_font)
    draw.text(
        (margin_px, 68),
        f"{W_mm:.1f} x {H_mm:.1f} mm  |  Zero filler (100% rock-art motifs)  |  Mean IoU {mean_iou:.3f}  |  Fidelity {mean_fid:.3f}  |  Lock {lock_mm:.2f} mm  |  Max tip {worst_tip:.2f} mm",
        fill=(95, 82, 68),
        font=sub_font,
    )

    ox, oy = float(margin_px), float(margin_px + header_px)
    for idx, f in enumerate(faces):
        pts = _to_px(f["exterior"], x0, y0, scale, ox, oy)
        rgb = _hex_to_rgb(NORDIC_PALETTE[colors[idx] % len(NORDIC_PALETTE)])
        # Blend slightly toward warm birch wood tone for visual clarity
        warm = (int(rgb[0] * 0.55 + 224 * 0.45), int(rgb[1] * 0.55 + 204 * 0.45), int(rgb[2] * 0.55 + 174 * 0.45))
        draw.polygon(pts, fill=warm)

    for e in edges:
        pts = _to_px(e["coords"], x0, y0, scale, ox, oy)
        if len(pts) >= 2:
            if e["is_exterior"]:
                draw.line(pts, fill=(38, 28, 22), width=3)
            else:
                draw.line(pts, fill=(48, 36, 28), width=2)

    assembly_path = p / "assembly.png"
    img.save(assembly_path)

    # 2. Labeled assembly with fidelity heat-tint & piece numbers
    labeled = img.copy()
    ldraw = ImageDraw.Draw(labeled)
    small_font = _load_font(11)
    for idx, f in enumerate(faces):
        poly = Polygon(f["exterior"])
        rp = poly.representative_point()
        px_x = ox + (rp.x - x0) * scale
        px_y = oy + (rp.y - y0) * scale
        fr = fid_by_piece.get(idx + 1)
        if fr and fr["fidelity"] < 0.65:
            pts = _to_px(f["exterior"], x0, y0, scale, ox, oy)
            ldraw.polygon(pts, fill=(235, 130, 120), outline=(140, 20, 20))
        ldraw.text((px_x - 8, px_y - 6), str(idx + 1), fill=(22, 18, 16), font=small_font)
    for e in edges:
        pts = _to_px(e["coords"], x0, y0, scale, ox, oy)
        if len(pts) >= 2:
            ldraw.line(pts, fill=(40, 30, 24), width=2 if not e["is_exterior"] else 3)
    labeled_path = p / "assembly_labeled.png"
    labeled.save(labeled_path)

    # 3. Four zoomed 1:1 detail crops (~160 x 120 mm regions at 10 px/mm = 1600 x 1200 px)
    crops_dir = p / "detail_crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    weak_cx, weak_cy = x0 + W_mm * 0.5, y0 + H_mm * 0.5
    if fidelity_report and fidelity_report.get("weakest"):
        w_pid = int(fidelity_report["weakest"][0]["piece"]) - 1
        wc = Polygon(faces[w_pid]["exterior"]).centroid
        weak_cx, weak_cy = wc.x, wc.y

    # Anchor edge detail crops on actual exterior border pieces (works cleanly for organic oval/non-rectangular outlines)
    ext_face_ids = sorted({int(e["left_face"]) for e in edges if e.get("is_exterior") and int(e["left_face"]) >= 0})
    tl_cx, tl_cy = x0 + 85.0, y0 + 65.0
    br_cx, br_cy = x1 - 85.0, y1 - 65.0
    if ext_face_ids:
        ext_centroids = [(fid, Polygon(faces[fid]["exterior"]).centroid) for fid in ext_face_ids]
        tl_c = min(ext_centroids, key=lambda item: item[1].x + item[1].y)[1]
        br_c = max(ext_centroids, key=lambda item: item[1].x + item[1].y)[1]
        tl_cx, tl_cy = tl_c.x + 25.0, tl_c.y + 18.0
        br_cx, br_cy = br_c.x - 25.0, br_c.y - 18.0

    crop_specs = [
        ("crop_center", x0 + W_mm * 0.50, y0 + H_mm * 0.50, 160.0, 115.0, "Center Interior Motifs (1:1 detail)"),
        ("crop_top_left_edge", tl_cx, tl_cy, 170.0, 120.0, "Top-Left Organic Edge Extension"),
        ("crop_bottom_right_edge", br_cx, br_cy, 170.0, 120.0, "Bottom-Right Organic Edge Extension"),
        ("crop_weakest_region", weak_cx, weak_cy, 160.0, 115.0, "Weakest-Fidelity Piece Neighborhood"),
    ]
    out_paths: Dict[str, str] = {
        "assembly": str(assembly_path),
        "assembly_labeled": str(labeled_path),
    }

    crop_scale = 10.0  # 10 px/mm (~254 DPI crisp zoom)
    crop_font = _load_font(15)
    crop_title_font = _load_font(22)
    for cname, cx, cy, cw, ch, ctitle in crop_specs:
        bx0 = max(x0 - 6.0, cx - cw * 0.5)
        by0 = max(y0 - 6.0, cy - ch * 0.5)
        bx1 = bx0 + cw
        by1 = by0 + ch
        iw = int(round(cw * crop_scale))
        ih = int(round(ch * crop_scale)) + 52
        cimg = Image.new("RGB", (iw, ih), (246, 242, 234))
        cdraw = ImageDraw.Draw(cimg)
        cdraw.rectangle([0, 0, iw, 52], fill=(236, 230, 218))
        cdraw.text((16, 12), f"{ctitle}  ({bx0:.0f}..{bx1:.0f} x {by0:.0f}..{by1:.0f} mm)", fill=(32, 26, 20), font=crop_title_font)

        for idx, f in enumerate(faces):
            pts_raw = f["exterior"]
            xs = [pt[0] for pt in pts_raw]
            ys = [pt[1] for pt in pts_raw]
            if max(xs) < bx0 or min(xs) > bx1 or max(ys) < by0 or min(ys) > by1:
                continue
            pts = _to_px(pts_raw, bx0, by0, crop_scale, 0.0, 52.0)
            rgb = _hex_to_rgb(NORDIC_PALETTE[colors[idx] % len(NORDIC_PALETTE)])
            warm = (int(rgb[0] * 0.5 + 228 * 0.5), int(rgb[1] * 0.5 + 208 * 0.5), int(rgb[2] * 0.5 + 178 * 0.5))
            cdraw.polygon(pts, fill=warm)
            # Overlay source silhouette outline softly in ochre-red so fidelity is visually obvious!
            if aligned_targets and idx < len(aligned_targets) and len(aligned_targets[idx]) >= 3:
                tpts = _to_px(aligned_targets[idx], bx0, by0, crop_scale, 0.0, 52.0)
                cdraw.line(tpts + [tpts[0]], fill=(185, 52, 36), width=2)

        for e in edges:
            pts_raw = e["coords"]
            xs = [pt[0] for pt in pts_raw]
            ys = [pt[1] for pt in pts_raw]
            if max(xs) < bx0 or min(xs) > bx1 or max(ys) < by0 or min(ys) > by1:
                continue
            pts = _to_px(pts_raw, bx0, by0, crop_scale, 0.0, 52.0)
            if len(pts) >= 2:
                cdraw.line(pts, fill=(28, 22, 18), width=3 if e["is_exterior"] else 2)

        for idx, f in enumerate(faces):
            poly = Polygon(f["exterior"])
            rp = poly.representative_point()
            if bx0 + 4 <= rp.x <= bx1 - 4 and by0 + 4 <= rp.y <= by1 - 4:
                px_x = (rp.x - bx0) * crop_scale
                px_y = 52.0 + (rp.y - by0) * crop_scale
                cls_lbl = str(f["metadata"].get("motif_class", "")).replace("_", " ")[:12]
                cdraw.text((px_x - 24, px_y - 10), f"#{idx + 1} {cls_lbl}", fill=(25, 20, 16), font=crop_font)

        cpath = crops_dir / f"{cname}.png"
        cimg.save(cpath)
        out_paths[cname] = str(cpath)

    return out_paths


def build_weakest_review_packet(
    run_dir: str | Path,
    fidelity_report: Dict[str, Any],
    thin_report: Dict[str, Any],
    aligned_targets: Sequence[Sequence[Sequence[float]]],
    count: int = 123,
    per_page: int = 16,
) -> Dict[str, Any]:
    """Generate multi-page contact sheets + JSON + HTML of the weakest ``count`` pieces (with neighbors & source overlay)."""
    p = Path(run_dir)
    out_dir = p / "weakest_review_packet"
    out_dir.mkdir(parents=True, exist_ok=True)

    d = json.loads((p / "partition.json").read_text(encoding="utf-8"))
    faces = d["faces"]
    adj = d.get("adjacency", {})
    thin_by_piece = {int(r["piece"]): r for r in thin_report.get("per_piece", [])}

    # Rank pieces by weakness: prioritize low fidelity, low IoU, and high tip loss
    ranked: List[Dict[str, Any]] = []
    for row in fidelity_report.get("per_piece", []):
        pid = int(row["piece"])
        th = thin_by_piece.get(pid, {})
        tip = float(th.get("max_tip_loss_mm", 0.0))
        core_surv = float(th.get("core_survival", 0.0))
        weak_score = float(row["fidelity"]) - 0.03 * max(0.0, tip - 3.0)
        entry = dict(row)
        entry["max_tip_loss_mm"] = round(tip, 3)
        entry["core_survival"] = round(core_surv, 4)
        entry["weakness_rank_score"] = round(weak_score, 4)
        ranked.append(entry)
    ranked.sort(key=lambda r: r["weakness_rank_score"])
    weakest = ranked[: min(count, len(ranked))]

    cols = 4
    rows_per_page = int(math.ceil(per_page / cols))
    cell_w, cell_h = 460, 480
    hdr_h = 74
    title_font = _load_font(24)
    label_font = _load_font(15)
    meta_font = _load_font(13)

    page_files: List[str] = []
    n_pages = int(math.ceil(len(weakest) / per_page))
    for pg in range(n_pages):
        batch = weakest[pg * per_page : (pg + 1) * per_page]
        img = Image.new("RGB", (cols * cell_w, rows_per_page * cell_h + hdr_h), (248, 245, 240))
        draw = ImageDraw.Draw(img)
        draw.rectangle([0, 0, cols * cell_w, hdr_h], fill=(235, 229, 218))
        draw.text(
            (20, 14),
            f"Weakest Pieces Review Packet — Page {pg + 1}/{n_pages} (Ranks {pg * per_page + 1}..{pg * per_page + len(batch)} of {len(weakest)})",
            fill=(28, 22, 18),
            font=title_font,
        )
        draw.text(
            (20, 46),
            "Fill = cut piece | Light grey = actual neighbors | Red outline = aligned rock-art source silhouette",
            fill=(95, 80, 68),
            font=label_font,
        )

        for idx_in_pg, item in enumerate(batch):
            r_idx = idx_in_pg // cols
            c_idx = idx_in_pg % cols
            gx = c_idx * cell_w
            gy = hdr_h + r_idx * cell_h
            draw.rectangle([gx + 6, gy + 6, gx + cell_w - 6, gy + cell_h - 6], fill=(255, 254, 250), outline=(210, 200, 186), width=2)

            fid = int(item["face_id"])
            poly = Polygon(faces[fid]["exterior"])
            nbr_ids = [int(nb) for nb in adj.get(str(fid), []) if 0 <= int(nb) < len(faces)]
            x0, y0, x1, y1 = poly.bounds
            pad = max(8.0, 0.25 * max(x1 - x0, y1 - y0))
            bx0, by0, bx1, by1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
            plot_w, plot_h = cell_w - 36, cell_h - 110
            sc = min(plot_w / max(bx1 - bx0, 1.0), plot_h / max(by1 - by0, 1.0))
            ox = gx + 18 + (plot_w - (bx1 - bx0) * sc) * 0.5
            oy = gy + 16 + (plot_h - (by1 - by0) * sc) * 0.5

            # Clip helper: draw neighbors clipped to local box
            clip_box = sbox(bx0, by0, bx1, by1)
            for nb in nbr_ids:
                np_clip = Polygon(faces[nb]["exterior"]).intersection(clip_box)
                for part in (getattr(np_clip, "geoms", [np_clip])):
                    if part.geom_type == "Polygon" and not part.is_empty:
                        pts = _to_px(part.exterior.coords, bx0, by0, sc, ox, oy)
                        draw.polygon(pts, fill=(238, 234, 226), outline=(175, 168, 158))

            # Draw cut piece
            p_pts = _to_px(faces[fid]["exterior"], bx0, by0, sc, ox, oy)
            draw.polygon(p_pts, fill=(216, 178, 134), outline=(30, 24, 20))
            draw.line(p_pts, fill=(30, 24, 20), width=3)

            # Draw aligned source motif contour in crimson
            if fid < len(aligned_targets) and len(aligned_targets[fid]) >= 3:
                t_pts = _to_px(aligned_targets[fid], bx0, by0, sc, ox, oy)
                draw.line(t_pts + [t_pts[0]], fill=(205, 38, 28), width=3)

            # Footer metadata
            fy = gy + cell_h - 86
            rank_num = pg * per_page + idx_in_pg + 1
            draw.text(
                (gx + 16, fy),
                f"Rank #{rank_num} | Piece #{item['piece']} ({item['asset_id']})",
                fill=(26, 20, 16),
                font=label_font,
            )
            draw.text(
                (gx + 16, fy + 22),
                f"Class: {item['motif_class']} | Lic: {str(item.get('license', ''))[:20]}",
                fill=(80, 68, 56),
                font=meta_font,
            )
            draw.text(
                (gx + 16, fy + 42),
                f"Fid {item['fidelity']:.3f} | IoU {item['iou']:.3f} | BayF {item['bay_fill']:.2f} | Tip {item['max_tip_loss_mm']:.2f}mm",
                fill=(130, 42, 30) if item["fidelity"] < 0.65 or item["max_tip_loss_mm"] > 5.0 else (46, 90, 52),
                font=meta_font,
            )

        pg_path = out_dir / f"page_{pg + 1:02d}.png"
        img.save(pg_path)
        page_files.append(pg_path.name)

    summary = {
        "count": len(weakest),
        "pages": page_files,
        "weakest": weakest,
    }
    (out_dir / "weakest_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Lightweight index.html for browsing pages
    imgs_html = "\n".join(
        f'<section><h2>Page {i + 1}</h2><img src="{fn}" style="max-width:100%;border:1px solid #ccc;"></section>'
        for i, fn in enumerate(page_files)
    )
    (out_dir / "index.html").write_text(
        f"<!doctype html><html><head><meta charset='utf-8'><title>Weakest {len(weakest)} Review Packet</title>"
        f"<style>body{{font-family:system-ui,sans-serif;max-width:1500px;margin:20px auto;background:#f7f4ef;color:#221c16;}}</style>"
        f"</head><body><h1>Helleristning 750 — Weakest {len(weakest)} Pieces Review Packet</h1>"
        f"<p><strong>Notice:</strong> Visual recognition review only — does not certify wood strength, laser fit, or commercial legal clearance.</p>"
        f"{imgs_html}</body></html>",
        encoding="utf-8",
    )
    return summary


def build_standalone_review_tool(
    run_dir: str | Path,
    fidelity_report: Dict[str, Any],
    thin_report: Dict[str, Any],
    audit_report: Dict[str, Any],
    aligned_targets: Sequence[Sequence[Sequence[float]]],
    catalog_sha256: str = "",
) -> Path:
    """Build a standalone, zero-dependency local HTML/JS 750-piece review tool pinned to cut.svg SHA-256."""
    p = Path(run_dir)
    d = json.loads((p / "partition.json").read_text(encoding="utf-8"))
    cut_sha = _sha256_file(p / "cut.svg") if (p / "cut.svg").exists() else ""
    faces = d["faces"]
    adj = d.get("adjacency", {})
    fid_by_piece = {int(r["piece"]): r for r in fidelity_report.get("per_piece", [])}
    thin_by_piece = {int(r["piece"]): r for r in thin_report.get("per_piece", [])}

    # Build compact JSON payload embedded directly in review_tool.html (works cleanly from file://)
    ext_faces = {
        int(e["left_face"]) if int(e["left_face"]) >= 0 else int(e["right_face"])
        for e in d["edges"]
        if e["is_exterior"]
    }
    pieces_payload = []
    for i, f in enumerate(faces):
        pid = i + 1
        meta = f["metadata"]
        fr = fid_by_piece.get(pid, {})
        tr = thin_by_piece.get(pid, {})
        poly = Polygon(f["exterior"])
        # Simplify slightly only for embedded HTML display payload (~0.25mm visual tol)
        simp = poly.simplify(0.25, preserve_topology=True)
        ext_pts = [[round(float(x), 2), round(float(y), 2)] for x, y in simp.exterior.coords]
        tgt_pts = (
            [[round(float(x), 2), round(float(y), 2)] for x, y in aligned_targets[i]]
            if i < len(aligned_targets)
            else []
        )
        pieces_payload.append(
            {
                "piece": pid,
                "face_id": i,
                "asset_id": meta.get("asset_id", ""),
                "motif_class": meta.get("motif_class", ""),
                "family": meta.get("family", ""),
                "site": meta.get("site", ""),
                "country": meta.get("country", ""),
                "archive": meta.get("source_archive", ""),
                "creator": meta.get("creator", ""),
                "license": meta.get("license", ""),
                "commercial_ok": bool(meta.get("commercial_ok", False)),
                "is_edge": i in ext_faces,
                "area_mm2": round(float(f["area_mm2"]), 1),
                "iou": round(float(fr.get("iou", 0.0)), 4),
                "fidelity": round(float(fr.get("fidelity", 0.0)), 4),
                "bay_fill": round(float(fr.get("bay_fill", 0.0)), 4),
                "extremity_recall": round(float(fr.get("extremity_recall", 0.0)), 4),
                "tip_mm": round(float(tr.get("max_tip_loss_mm", 0.0)), 3),
                "neighbors": [int(nb) for nb in adj.get(str(i), [])],
                "poly": ext_pts,
                "target": tgt_pts,
            }
        )

    manifest_meta = {
        "edition": "Helleristning 750 Exclusive Wooden Jigsaw",
        "piece_count": len(pieces_payload),
        "bounds_mm": d["outline_bounds_mm"],
        "cut_svg_sha256": cut_sha,
        "catalog_sha256": catalog_sha256,
        "lock_margin_mm": audit_report.get("lock_margin_mm"),
        "worst_tip_loss_mm": audit_report.get("worst_tip_loss_mm"),
        "min_segment_mm": audit_report.get("min_segment_mm"),
        "digital_screen_pass": audit_report.get("digital_screen_pass"),
    }

    data_json = json.dumps({"meta": manifest_meta, "pieces": pieces_payload}, separators=(",", ":"), ensure_ascii=False)

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Helleristning 750 — Visual Piece Review Tool</title>
<style>
  :root {{ --bg:#f5f1ea; --card:#fffdf9; --ink:#221c16; --muted:#6f6254; --accent:#9e3b24; --ok:#2e6f40; --warn:#b06218; --border:#dcd2c2; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:system-ui,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--ink); display:flex; flex-direction:column; height:100vh; }}
  header {{ background:#262019; color:#f7f2ea; padding:10px 18px; display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }}
  header h1 {{ margin:0; font-size:16px; letter-spacing:0.3px; }}
  .banner {{ background:#fff3cd; color:#5a3e00; border-bottom:1px solid #e6d18a; padding:6px 18px; font-size:12px; }}
  .main {{ display:grid; grid-template-columns:310px 1fr 330px; flex:1; min-height:0; }}
  .sidebar {{ border-right:1px solid var(--border); background:var(--card); display:flex; flex-direction:column; min-height:0; }}
  .controls {{ padding:10px; border-bottom:1px solid var(--border); display:flex; flex-direction:column; gap:6px; }}
  .controls input, .controls select {{ padding:6px 8px; border:1px solid var(--border); border-radius:4px; font-size:13px; background:#fff; }}
  .list {{ flex:1; overflow-y:auto; }}
  .row {{ padding:7px 10px; border-bottom:1px solid #efe9df; cursor:pointer; display:flex; justify-content:space-between; align-items:center; font-size:12px; }}
  .row:hover {{ background:#f6efe4; }}
  .row.active {{ background:#ebdecb; font-weight:600; }}
  .badge {{ padding:2px 6px; border-radius:10px; font-size:11px; font-weight:600; }}
  .b-ok {{ background:#d8f0de; color:var(--ok); }}
  .b-rev {{ background:#f9dcd6; color:var(--accent); }}
  .b-pen {{ background:#ebe4d8; color:var(--muted); }}
  .stage {{ display:flex; flex-direction:column; align-items:center; justify-content:center; padding:14px; position:relative; min-height:0; }}
  svg#pieceSvg {{ width:100%; height:100%; max-height:calc(100vh - 200px); background:var(--card); border:1px solid var(--border); border-radius:6px; }}
  .toggles {{ display:flex; gap:12px; margin-top:8px; font-size:13px; flex-wrap:wrap; align-items:center; }}
  .right {{ border-left:1px solid var(--border); background:var(--card); padding:12px; overflow-y:auto; display:flex; flex-direction:column; gap:10px; }}
  .meta-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:6px; font-size:12px; }}
  .kv {{ background:#f7f3ec; padding:6px 8px; border-radius:4px; }}
  .kv span {{ display:block; color:var(--muted); font-size:10px; text-transform:uppercase; }}
  .btn-row {{ display:flex; gap:8px; flex-wrap:wrap; }}
  button {{ padding:7px 11px; border:1px solid var(--border); border-radius:4px; cursor:pointer; font-size:13px; background:#fff; }}
  button.approve {{ background:var(--ok); color:#fff; border-color:var(--ok); }}
  button.revise {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
  textarea {{ width:100%; min-height:66px; border:1px solid var(--border); border-radius:4px; padding:6px; font-family:inherit; font-size:12px; }}
  svg#minimap {{ width:100%; height:190px; background:#f2ece2; border:1px solid var(--border); border-radius:4px; cursor:pointer; }}
</style>
</head>
<body>
<header>
  <h1>HELLERISTNING 750 — Visual Piece Review Tool</h1>
  <div id="statsBar" style="font-size:12px;opacity:.9;"></div>
  <div class="btn-row">
    <button onclick="exportDecisions()">Export Review JSON</button>
    <label style="display:inline-block;padding:6px 10px;border:1px solid #665;border-radius:4px;cursor:pointer;font-size:12px;">
      Import JSON<input type="file" accept=".json" style="display:none" onchange="importDecisions(event)">
    </label>
  </div>
</header>
<div class="banner">
  <strong>SCOPE NOTICE:</strong> Visual silhouette recognition review ONLY. Approving pieces here does <strong>NOT</strong> certify physical wood strength, laser kerf fit, product safety compliance, or commercial legal rights clearance. Pinned to <code>cut.svg</code> SHA-256: <code id="shaPin"></code>.
</div>
<div class="main">
  <div class="sidebar">
    <div class="controls">
      <input id="q" placeholder="Search piece #, asset_id, class, site..." oninput="renderList()">
      <div style="display:flex;gap:6px;">
        <select id="sortBy" onchange="renderList()" style="flex:1">
          <option value="fidelity">Sort: Weakest Fidelity</option>
          <option value="iou">Sort: Lowest IoU</option>
          <option value="tip">Sort: Highest Tip Loss</option>
          <option value="piece">Sort: Piece #</option>
        </select>
        <select id="filterStatus" onchange="renderList()" style="flex:1">
          <option value="all">All 750</option>
          <option value="pending">Pending</option>
          <option value="approve">Approved</option>
          <option value="revise">Flagged Revise</option>
          <option value="edge">Border Pieces</option>
        </select>
      </div>
    </div>
    <div id="pieceList" class="list"></div>
  </div>
  <div class="stage">
    <svg id="pieceSvg" viewBox="0 0 100 100"></svg>
    <div class="toggles">
      <label><input type="checkbox" id="showSource" checked onchange="renderPiece()"> Source Silhouette Overlay (O)</label>
      <label><input type="checkbox" id="showNbrs" checked onchange="renderPiece()"> Neighbor Context (B)</label>
      <button onclick="step(-1)">&larr; Prev (P)</button>
      <button onclick="step(1)">Next (N) &rarr;</button>
      <button class="approve" onclick="setStatus('approve')">Approve (A)</button>
      <button class="revise" onclick="setStatus('revise')">Flag Revise (R)</button>
      <button onclick="setStatus('pending')">Reset</button>
    </div>
  </div>
  <div class="right">
    <div style="font-weight:700;font-size:15px;" id="pieceTitle"></div>
    <div class="meta-grid" id="metaGrid"></div>
    <div>
      <label style="font-size:12px;color:var(--muted);">Reviewer Note</label>
      <textarea id="noteBox" placeholder="Optional visual note (e.g., leg clipped, horn crisp)..." oninput="saveNote()"></textarea>
    </div>
    <div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:4px;">Full 750-Piece Board Minimap (click to inspect)</div>
      <svg id="minimap"></svg>
    </div>
  </div>
</div>
<script>
const DATA = {data_json};
const STORE_KEY = "heller750_review_" + (DATA.meta.cut_svg_sha256 || "dev").slice(0, 16);
let decisions = JSON.parse(localStorage.getItem(STORE_KEY) || "{{}}");
let filtered = [];
let curFace = 0;

document.getElementById("shaPin").textContent = (DATA.meta.cut_svg_sha256 || "n/a").slice(0, 16) + "...";

function ptsAttr(pts) {{ return pts.map(p => p[0] + "," + p[1]).join(" "); }}
function getDec(pid) {{ return decisions[pid] || {{ status: "pending", note: "" }}; }}
function persist() {{ localStorage.setItem(STORE_KEY, JSON.stringify(decisions)); updateStats(); }}

function updateStats() {{
  let app = 0, rev = 0;
  for (const p of DATA.pieces) {{
    const s = getDec(p.piece).status;
    if (s === "approve") app++; else if (s === "revise") rev++;
  }}
  document.getElementById("statsBar").textContent =
    `${{DATA.meta.piece_count}} pcs | Lock ${{DATA.meta.lock_margin_mm}}mm | Max tip ${{DATA.meta.worst_tip_loss_mm}}mm | Approved: ${{app}} | Revise: ${{rev}} | Pending: ${{DATA.meta.piece_count - app - rev}}`;
}}

function renderList() {{
  const q = document.getElementById("q").value.trim().toLowerCase();
  const sort = document.getElementById("sortBy").value;
  const filt = document.getElementById("filterStatus").value;
  filtered = DATA.pieces.filter(p => {{
    const st = getDec(p.piece).status;
    if (filt === "edge" && !p.is_edge) return false;
    if ((filt === "pending" || filt === "approve" || filt === "revise") && st !== filt) return false;
    if (!q) return true;
    return (`#${{p.piece}} ${{p.asset_id}} ${{p.motif_class}} ${{p.site}} ${{p.license}}`).toLowerCase().includes(q);
  }});
  filtered.sort((a, b) => {{
    if (sort === "fidelity") return a.fidelity - b.fidelity;
    if (sort === "iou") return a.iou - b.iou;
    if (sort === "tip") return b.tip_mm - a.tip_mm;
    return a.piece - b.piece;
  }});
  const el = document.getElementById("pieceList");
  el.innerHTML = filtered.map(p => {{
    const st = getDec(p.piece).status;
    const cls = st === "approve" ? "b-ok" : (st === "revise" ? "b-rev" : "b-pen");
    return `<div class="row ${{p.face_id === curFace ? "active" : ""}}" onclick="selectFace(${{p.face_id}})">
      <div><strong>#${{p.piece}}</strong> ${{p.motif_class}} <span style="color:#877">${{p.asset_id}}</span></div>
      <div><span style="margin-right:6px;color:#655">F:${{p.fidelity.toFixed(2)}}</span><span class="badge ${{cls}}">${{st}}</span></div>
    </div>`;
  }}).join("");
}}

function selectFace(fid) {{
  curFace = fid;
  renderList();
  renderPiece();
  renderMinimapHighlight();
}}

function renderPiece() {{
  const p = DATA.pieces[curFace];
  if (!p) return;
  const dec = getDec(p.piece);
  document.getElementById("pieceTitle").textContent = `Piece #${{p.piece}} — ${{p.motif_class}} (${{p.asset_id}})`;
  document.getElementById("noteBox").value = dec.note || "";
  document.getElementById("metaGrid").innerHTML = [
    ["Fidelity", p.fidelity.toFixed(3)], ["Aligned IoU", p.iou.toFixed(3)],
    ["Bay Fill", p.bay_fill.toFixed(3)], ["Extremity Recall", p.extremity_recall.toFixed(3)],
    ["Max Tip Loss", p.tip_mm + " mm"], ["Area", p.area_mm2 + " mm²"],
    ["Position", p.is_edge ? "Organic Border" : "Interior"], ["License", p.license],
    ["Site", p.site || "—"], ["Archive", p.archive || "—"],
  ].map(([k, v]) => `<div class="kv"><span>${{k}}</span>${{v}}</div>`).join("");

  let xs = p.poly.map(q => q[0]), ys = p.poly.map(q => q[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = Math.max(10, 0.35 * Math.max(x1 - x0, y1 - y0));
  const svg = document.getElementById("pieceSvg");
  svg.setAttribute("viewBox", `${{(x0 - pad).toFixed(2)}} ${{(y0 - pad).toFixed(2)}} ${{(x1 - x0 + 2*pad).toFixed(2)}} ${{(y1 - y0 + 2*pad).toFixed(2)}}`);

  let html = "";
  if (document.getElementById("showNbrs").checked) {{
    for (const nb of p.neighbors) {{
      const np = DATA.pieces[nb];
      if (np) html += `<polygon points="${{ptsAttr(np.poly)}}" fill="#ece6dc" stroke="#b9afa0" stroke-width="0.35"/>`;
    }}
  }}
  html += `<polygon points="${{ptsAttr(p.poly)}}" fill="#d8b286" stroke="#231c16" stroke-width="0.65"/>`;
  if (document.getElementById("showSource").checked && p.target && p.target.length >= 3) {{
    html += `<polygon points="${{ptsAttr(p.target)}}" fill="none" stroke="#c8261c" stroke-width="0.65" stroke-dasharray="1.6 1.0"/>`;
  }}
  svg.innerHTML = html;
}}

function renderMinimap() {{
  const [x0, y0, x1, y1] = DATA.meta.bounds_mm;
  const mm = document.getElementById("minimap");
  mm.setAttribute("viewBox", `${{x0 - 5}} ${{y0 - 5}} ${{x1 - x0 + 10}} ${{y1 - y0 + 10}}`);
  mm.innerHTML = DATA.pieces.map(p =>
    `<polygon data-fid="${{p.face_id}}" points="${{ptsAttr(p.poly)}}" fill="#d7c5ab" stroke="#4c3f32" stroke-width="0.8" onclick="selectFace(${{p.face_id}})"/>`
  ).join("");
}}

function renderMinimapHighlight() {{
  document.querySelectorAll("#minimap polygon").forEach(el => {{
    const fid = Number(el.dataset.fid);
    const st = getDec(DATA.pieces[fid].piece).status;
    if (fid === curFace) el.setAttribute("fill", "#c8261c");
    else if (st === "approve") el.setAttribute("fill", "#8cc096");
    else if (st === "revise") el.setAttribute("fill", "#e59a8c");
    else el.setAttribute("fill", "#d7c5ab");
  }});
}}

function setStatus(st) {{
  const p = DATA.pieces[curFace];
  decisions[p.piece] = {{ status: st, note: document.getElementById("noteBox").value, asset_id: p.asset_id, updated_utc: new Date().toISOString() }};
  persist(); renderList(); renderMinimapHighlight();
}}

function saveNote() {{
  const p = DATA.pieces[curFace];
  const prev = getDec(p.piece);
  decisions[p.piece] = {{ status: prev.status, note: document.getElementById("noteBox").value, asset_id: p.asset_id, updated_utc: new Date().toISOString() }};
  persist();
}}

function step(delta) {{
  const idx = filtered.findIndex(p => p.face_id === curFace);
  const nxt = idx < 0 ? 0 : Math.max(0, Math.min(filtered.length - 1, idx + delta));
  if (filtered[nxt]) selectFace(filtered[nxt].face_id);
}}

function exportDecisions() {{
  const payload = {{
    schema: "helleristning-750-visual-review-v1",
    scope_notice: "Visual recognition review only; does not certify physical wood strength or commercial legal clearance.",
    pinned_cut_svg_sha256: DATA.meta.cut_svg_sha256,
    pinned_catalog_sha256: DATA.meta.catalog_sha256,
    exported_utc: new Date().toISOString(),
    decisions
  }};
  const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: "application/json" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "helleristning-750-visual-review.json";
  a.click();
}}

function importDecisions(ev) {{
  const file = ev.target.files[0];
  if (!file) return;
  const r = new FileReader();
  r.onload = () => {{
    const obj = JSON.parse(r.result);
    if (obj.pinned_cut_svg_sha256 && DATA.meta.cut_svg_sha256 && obj.pinned_cut_svg_sha256 !== DATA.meta.cut_svg_sha256) {{
      if (!confirm("Warning: imported review JSON was pinned to a different cut.svg SHA-256. Load anyway?")) return;
    }}
    decisions = obj.decisions || {{}};
    persist(); renderList(); renderPiece(); renderMinimapHighlight();
  }};
  r.readAsText(file);
}}

document.addEventListener("keydown", e => {{
  if (document.activeElement.tagName === "INPUT" || document.activeElement.tagName === "TEXTAREA") return;
  if (e.key === "a" || e.key === "A") setStatus("approve");
  else if (e.key === "r" || e.key === "R") setStatus("revise");
  else if (e.key === "n" || e.key === "N" || e.key === "ArrowRight") step(1);
  else if (e.key === "p" || e.key === "P" || e.key === "ArrowLeft") step(-1);
  else if (e.key === "o" || e.key === "O") {{ const c = document.getElementById("showSource"); c.checked = !c.checked; renderPiece(); }}
  else if (e.key === "b" || e.key === "B") {{ const c = document.getElementById("showNbrs"); c.checked = !c.checked; renderPiece(); }}
}});

renderMinimap();
updateStats();
renderList();
if (filtered.length) selectFace(filtered[0].face_id);
</script>
</body>
</html>
"""
    out_path = p / "review_tool.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path
