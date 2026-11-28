"""End-to-end CLI and runner for scientific rock-art tracing, metadata export, and dual-trace QA."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gc
import hashlib
import json
import os
import re
import shutil
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import numpy as np
from shapely.geometry import Polygon

from .dual_trace_qa import run_dual_trace_qa
from .extract_layers import TracedFigure, is_allowed_record, trace_panel_scientific
from .provenance import (
    classify_core_region,
    classify_motif_scientific,
    load_gazetteer,
    load_literature,
    load_taxonomy_index,
    resolve_literature,
    resolve_scale_calibration,
    resolve_site_and_coords,
    validate_commercial_ok,
)
from .schema import validate_figure_record, write_schema_json
from .svg_writer import write_scientific_svg

DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_manifests(sources_dir: Path) -> List[Path]:
    return sorted(m for m in sources_dir.glob("*/manifest.jsonl") if m.is_file())


def load_verified_source_records(
    manifests: List[Path],
    base_dir: Path,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Load manifests, verify SHA-256 on disk, and strictly exclude painted_photo / photo_other."""
    accepted: List[Dict[str, Any]] = []
    excluded_counts: Counter = Counter()
    issues: List[Dict[str, Any]] = []
    seen_sha: set[str] = set()
    dup_skipped = 0

    for mf in manifests:
        with open(mf, "r", encoding="utf-8") as f:
            for line_no, raw_line in enumerate(f, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    issues.append({"manifest": str(mf), "line": line_no, "error": f"json_error:{e}"})
                    continue

                ok_media, reason = is_allowed_record(rec)
                if not ok_media:
                    excluded_counts[reason] += 1
                    continue

                raw_rel = str(rec.get("raw_path") or "").replace("\\", "/")
                raw_abs = (base_dir / raw_rel).resolve()
                if not raw_abs.is_file():
                    alt = (mf.parent / Path(raw_rel).name).resolve()
                    if alt.is_file():
                        raw_abs = alt
                        raw_rel = str(raw_abs.relative_to(base_dir)).replace("\\", "/")
                    else:
                        # Also check sibling 'raw' subfolder
                        alt2 = (mf.parent / "raw" / Path(raw_rel).name).resolve()
                        if alt2.is_file():
                            raw_abs = alt2
                            raw_rel = str(raw_abs.relative_to(base_dir)).replace("\\", "/")
                        else:
                            issues.append({"manifest": str(mf), "source_id": rec.get("source_id"), "error": f"missing_raw:{raw_rel}"})
                            continue

                if raw_abs.stat().st_size == 0:
                    issues.append({"manifest": str(mf), "source_id": rec.get("source_id"), "error": "empty_file"})
                    continue

                actual_sha = compute_sha256(raw_abs)
                manifest_sha = str(rec.get("sha256") or "").strip().lower()
                if manifest_sha and manifest_sha != actual_sha:
                    issues.append({
                        "manifest": str(mf),
                        "source_id": rec.get("source_id"),
                        "error": "sha256_mismatch",
                        "manifest_sha256": manifest_sha,
                        "actual_sha256": actual_sha,
                    })

                if actual_sha in seen_sha:
                    dup_skipped += 1
                    continue
                seen_sha.add(actual_sha)

                rec = dict(rec)
                rec["raw_path"] = raw_rel
                rec["_raw_abs_path"] = raw_abs
                rec["sha256"] = actual_sha
                rec["licence"] = str(rec.get("licence") or rec.get("license") or "").strip()
                rec["licence_url"] = str(rec.get("licence_url") or rec.get("license_url") or "").strip()
                rec["rights_holder"] = str(rec.get("rights_holder") or rec.get("creator") or "Unknown").strip() or "Unknown"
                rec["commercial_ok"] = validate_commercial_ok(rec["licence"], rec.get("commercial_ok"))
                rec["country"] = str(rec.get("country") or "NORDIC").upper()
                if rec["country"] not in ("NO", "SE", "DK", "FI", "NORDIC"):
                    rec["country"] = "NORDIC"
                accepted.append(rec)

    return accepted, {
        "manifests_read": [str(m.relative_to(base_dir)).replace("\\", "/") for m in manifests],
        "accepted_carved_sources": len(accepted),
        "excluded_by_reason": dict(excluded_counts),
        "duplicate_sha256_skipped": dup_skipped,
        "manifest_issues_count": len(issues),
        "manifest_issues_sample": issues[:25],
    }


def _write_figures_csv(records: List[Dict[str, Any]], out_path: Path) -> None:
    fields = [
        "figure_id", "site_panel_id", "site_panel_id_provenance", "site", "core_region", "region", "country",
        "lat", "lon", "coordinates_crs", "coordinates_provenance",
        "motif_family", "motif_class", "period",
        "width_mm", "height_mm", "area_mm2", "px_per_mm", "scale_calibrated", "scale_source",
        "width_px", "height_px", "area_px2",
        "source_id", "source_archive", "raw_path", "sha256", "media_type",
        "crop_x", "crop_y", "crop_w", "crop_h",
        "method", "tracer", "date", "rights_holder", "licence", "licence_url", "commercial_ok",
        "confidence", "literature_count", "svg_path",
        "cup_marks_count", "cracks_count", "uncertain_count", "has_scale_bar",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in records:
            coords = r.get("coordinates") or {}
            sm = r["size_mm"]
            src = r["source_image"]
            crop = r["crop_px"]
            lay = r.get("layers", {})
            writer.writerow({
                "figure_id": r["figure_id"],
                "site_panel_id": r["site_panel_id"] if r["site_panel_id"] is not None else "",
                "site_panel_id_provenance": r.get("site_panel_id_provenance", ""),
                "site": r.get("site", ""),
                "core_region": r.get("core_region", ""),
                "region": r.get("region", ""),
                "country": r.get("country", ""),
                "lat": coords.get("lat", ""),
                "lon": coords.get("lon", ""),
                "coordinates_crs": coords.get("crs", ""),
                "coordinates_provenance": r["coordinates_provenance"]["reason"],
                "motif_family": r["motif"]["family"],
                "motif_class": r["motif"]["motif_class"],
                "period": r["period"],
                "width_mm": sm["width_mm"] if sm["width_mm"] is not None else "",
                "height_mm": sm["height_mm"] if sm["height_mm"] is not None else "",
                "area_mm2": sm["area_mm2"] if sm["area_mm2"] is not None else "",
                "px_per_mm": sm["px_per_mm"] if sm["px_per_mm"] is not None else "",
                "scale_calibrated": sm["scale_calibrated"],
                "scale_source": sm["scale_source"],
                "width_px": sm["width_px"],
                "height_px": sm["height_px"],
                "area_px2": sm["area_px2"],
                "source_id": src["source_id"],
                "source_archive": src.get("source_archive", ""),
                "raw_path": src["raw_path"],
                "sha256": src["sha256"],
                "media_type": src.get("media_type", ""),
                "crop_x": crop[0],
                "crop_y": crop[1],
                "crop_w": crop[2],
                "crop_h": crop[3],
                "method": r["method"],
                "tracer": r["tracer"],
                "date": r["date"],
                "rights_holder": r["rights_holder"],
                "licence": r["licence"],
                "licence_url": r["licence_url"],
                "commercial_ok": r["commercial_ok"],
                "confidence": r["confidence"],
                "literature_count": len(r["literature"]),
                "svg_path": r["svg_path"],
                "cup_marks_count": lay.get("cup_marks_count", 0),
                "cracks_count": lay.get("cracks_count", 0),
                "uncertain_count": lay.get("uncertain_count", 0),
                "has_scale_bar": lay.get("has_scale_bar", False),
            })


def _worker_trace_source(
    args: Tuple[int, Path, str, int]
) -> Tuple[int, List[TracedFigure], Dict[str, Any]]:
    idx_rec, raw_abs, media_type, max_motifs_per_panel = args
    try:
        traced_list, pinfo = trace_panel_scientific(
            raw_abs, media_type=media_type, max_motifs_per_panel=max_motifs_per_panel
        )
    except Exception as e:
        return idx_rec, [], {"status": "rejected_exception", "reason": f"exception:{type(e).__name__}:{e}"}
    empty_mask = np.empty((0, 0), dtype=np.uint8)
    for tf in traced_list:
        tf.mask_crop = empty_mask
    gc.collect()
    return idx_rec, traced_list, pinfo


def run_science_pipeline(
    base_dir: Path = DEFAULT_BASE_DIR,
    sources_dir: Optional[Path] = None,
    metadata_dir: Optional[Path] = None,
    record_dir: Optional[Path] = None,
    qa_dir: Optional[Path] = None,
    taxonomy_path: Optional[Path] = None,
    clean_output: bool = True,
    max_motifs_per_panel: int = 24,
    qa_min_fraction: float = 0.05,
    workers: int = 1,
) -> Dict[str, Any]:
    """Run full scientific tracing, metadata generation, and >=5% dual-trace QA."""
    t0 = time.time()
    sources_dir = sources_dir or (base_dir / "sources")
    metadata_dir = metadata_dir or (base_dir / "metadata")
    record_dir = record_dir or (base_dir / "scientific_record")
    qa_dir = qa_dir or (base_dir / "qa")
    taxonomy_path = taxonomy_path or (base_dir / "taxonomy.json")

    if not taxonomy_path.is_file():
        fallback = base_dir.parent / "hellerisninger" / "taxonomy.json"
        if fallback.is_file():
            taxonomy_path = fallback

    _, class_index = load_taxonomy_index(taxonomy_path)
    gazetteer = load_gazetteer(metadata_dir / "site_gazetteer.json")
    literature_entries = load_literature(metadata_dir / "literature.json")

    manifests = discover_manifests(sources_dir)
    source_records, ingest_summary = load_verified_source_records(manifests, base_dir=base_dir)

    if clean_output:
        for sub in (record_dir / "svg", qa_dir / "overlays"):
            if sub.exists():
                shutil.rmtree(sub)
    svg_dir = record_dir / "svg"
    svg_dir.mkdir(parents=True, exist_ok=True)
    qa_dir.mkdir(parents=True, exist_ok=True)

    run_date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tracer_id = "helleristninger.pipeline_science v1.0.0"

    figure_records: List[Dict[str, Any]] = []
    traced_by_id: Dict[str, TracedFigure] = {}
    plate_status: Counter = Counter()
    plate_rejects: Counter = Counter()
    validation_errors: List[Dict[str, Any]] = []

    def _consume_plate(idx_rec: int, traced_list: List[TracedFigure], pinfo: Dict[str, Any]) -> None:
        src_rec = source_records[idx_rec]
        raw_abs: Path = src_rec["_raw_abs_path"]
        media_type = str(src_rec["media_type"]).strip().lower()
        plate_status[pinfo.get("status", "unknown")] += 1
        if not traced_list:
            plate_rejects[pinfo.get("reason", "no_figures")] += 1
            return

        core_region = classify_core_region(src_rec)
        panel_id, panel_id_prov, coords_obj, coords_prov = resolve_site_and_coords(
            src_rec, gazetteer, core_region=core_region
        )
        scale_info = resolve_scale_calibration(src_rec)
        lit_refs = resolve_literature(src_rec, core_region=core_region, site_panel_id=panel_id, literature_entries=literature_entries)

        n_figs = len(traced_list)
        sha_short = src_rec["sha256"][:12]

        for tf in traced_list:
            figure_id = f"hr_{sha_short}_{tf.panel_index:03d}"
            family, motif_class, cls_conf = classify_motif_scientific(
                src_rec, class_index=class_index, num_extracted_from_source=n_figs, shape_metrics=tf.metrics
            )

            cal = bool(scale_info["scale_calibrated"])
            px_per_mm = float(scale_info["px_per_mm"]) if cal and scale_info.get("px_per_mm") else None
            w_px = float(tf.metrics["width_px"])
            h_px = float(tf.metrics["height_px"])
            a_px2 = float(tf.metrics["area_px2"])
            p_px = float(tf.metrics["perimeter_px"])

            if cal and px_per_mm:
                w_mm = round(w_px / px_per_mm, 3)
                h_mm = round(h_px / px_per_mm, 3)
                a_mm2 = round(a_px2 / (px_per_mm * px_per_mm), 3)
                p_mm = round(p_px / px_per_mm, 3)
            else:
                w_mm = None
                h_mm = None
                a_mm2 = None
                p_mm = None

            svg_file = svg_dir / f"{figure_id}.svg"
            layer_stats = write_scientific_svg(
                out_path=svg_file,
                figure_id=figure_id,
                traced=tf,
                scale_info=scale_info,
                site_panel_id=panel_id,
            )

            geom_bonus = 0.08 if tf.metrics["solidity"] >= 0.35 else 0.0
            conf = round(max(0.0, min(1.0, 0.85 * cls_conf + geom_bonus + (0.05 if cal else 0.0))), 3)

            period = str(src_rec.get("period") or "").strip().lower()
            if not period or period == "unknown":
                period = str(class_index.get(motif_class, {}).get("typical_period") or "unknown")

            fig_rec: Dict[str, Any] = {
                "figure_id": figure_id,
                "site_panel_id": panel_id,
                "site_panel_id_provenance": panel_id_prov,
                "site": str(src_rec.get("site") or ""),
                "region": str(src_rec.get("region") or ""),
                "core_region": core_region,
                "country": src_rec["country"],
                "coordinates": coords_obj,
                "coordinates_provenance": coords_prov,
                "motif": {
                    "family": family,
                    "motif_class": motif_class,
                },
                "size_mm": {
                    "width_mm": w_mm,
                    "height_mm": h_mm,
                    "area_mm2": a_mm2,
                    "px_per_mm": px_per_mm,
                    "scale_calibrated": cal,
                    "scale_source": scale_info["scale_source"],
                    "width_px": round(w_px, 2),
                    "height_px": round(h_px, 2),
                    "area_px2": round(a_px2, 2),
                },
                "period": period,
                "source_image": {
                    "source_id": str(src_rec.get("source_id") or raw_abs.stem),
                    "source_archive": str(src_rec.get("source_archive") or "unknown"),
                    "raw_path": src_rec["raw_path"],
                    "sha256": src_rec["sha256"],
                    "media_type": media_type,
                    "source_url": str(src_rec.get("source_url") or ""),
                },
                "crop_px": tf.crop_px,
                "method": tf.method,
                "tracer": tracer_id,
                "date": run_date,
                "rights_holder": src_rec["rights_holder"],
                "licence": src_rec["licence"],
                "licence_url": src_rec["licence_url"],
                "commercial_ok": bool(src_rec["commercial_ok"]),
                "confidence": conf,
                "literature": lit_refs,
                "svg_path": f"scientific_record/svg/{figure_id}.svg",
                "layers": layer_stats,
                "geometry": {
                    "vertex_count": int(tf.metrics["vertex_count"]),
                    "solidity": float(tf.metrics["solidity"]),
                    "compactness": float(tf.metrics["compactness"]),
                    "aspect_ratio": float(tf.metrics["aspect_ratio"]),
                    "perimeter_px": round(p_px, 2),
                    "perimeter_mm": p_mm,
                    "hole_count": int(tf.metrics["hole_count"]),
                    "component_count": int(tf.metrics["component_count"]),
                    "euler_number": int(tf.metrics["euler_number"]),
                },
            }

            errs = validate_figure_record(fig_rec)
            if errs:
                validation_errors.append({"figure_id": figure_id, "errors": errs})
                continue

            figure_records.append(fig_rec)
            traced_by_id[figure_id] = tf

    eff_workers = max(1, min(workers, len(source_records) if source_records else 1))
    done_count = 0
    total_src = len(source_records)

    if eff_workers == 1:
        for idx_rec, src_rec in enumerate(source_records):
            r_idx, traced_list, pinfo = _worker_trace_source(
                (idx_rec, src_rec["_raw_abs_path"], str(src_rec["media_type"]).strip().lower(), max_motifs_per_panel)
            )
            _consume_plate(r_idx, traced_list, pinfo)
            done_count += 1
            if done_count % 100 == 0 or done_count == total_src:
                print(f"[science] {done_count}/{total_src} sources -> {len(figure_records)} figures ({time.time()-t0:.1f}s)", flush=True)
    else:
        max_in_flight = eff_workers * 2
        next_idx = 0
        pending = set()
        with ProcessPoolExecutor(max_workers=eff_workers) as pool:
            while next_idx < total_src and len(pending) < max_in_flight:
                sr = source_records[next_idx]
                pending.add(pool.submit(_worker_trace_source, (next_idx, sr["_raw_abs_path"], str(sr["media_type"]).strip().lower(), max_motifs_per_panel)))
                next_idx += 1
            while pending:
                done_set, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done_set:
                    r_idx, traced_list, pinfo = fut.result()
                    _consume_plate(r_idx, traced_list, pinfo)
                    done_count += 1
                    if done_count % 100 == 0 or done_count == total_src:
                        print(f"[science] {done_count}/{total_src} sources -> {len(figure_records)} figures ({time.time()-t0:.1f}s)", flush=True)
                while next_idx < total_src and len(pending) < max_in_flight:
                    sr = source_records[next_idx]
                    pending.add(pool.submit(_worker_trace_source, (next_idx, sr["_raw_abs_path"], str(sr["media_type"]).strip().lower(), max_motifs_per_panel)))
                    next_idx += 1

    # Sort deterministically by figure_id
    figure_records.sort(key=lambda r: r["figure_id"])

    # Write schema.json, figures.jsonl, figures.csv
    write_schema_json(record_dir / "schema.json")
    with open(record_dir / "figures.jsonl", "w", encoding="utf-8") as f:
        for rec in figure_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    _write_figures_csv(figure_records, record_dir / "figures.csv")

    # Run >=5% stratified independent dual-trace QA
    qa_report = run_dual_trace_qa(
        records=figure_records,
        traced_by_id=traced_by_id,
        base_dir=base_dir,
        qa_dir=qa_dir,
        min_fraction=qa_min_fraction,
    )

    summary = {
        "total_figures": len(figure_records),
        "calibrated_count": sum(1 for r in figure_records if r["size_mm"]["scale_calibrated"]),
        "uncalibrated_count": sum(1 for r in figure_records if not r["size_mm"]["scale_calibrated"]),
        "by_core_region": dict(Counter(r["core_region"] for r in figure_records)),
        "by_period": dict(Counter(r["period"] for r in figure_records)),
        "by_media_type": dict(Counter(r["source_image"]["media_type"] for r in figure_records)),
        "by_family": dict(Counter(r["motif"]["family"] for r in figure_records)),
        "by_archive": dict(Counter(r["source_image"]["source_archive"] for r in figure_records)),
        "coords_verified_count": sum(1 for r in figure_records if r["coordinates"] is not None),
        "site_panel_id_verified_count": sum(1 for r in figure_records if r["site_panel_id"] is not None),
        "ingest_summary": ingest_summary,
        "plate_status": dict(plate_status),
        "plate_rejects": dict(plate_rejects),
        "schema_validation_errors": len(validation_errors),
        "qa_summary": {
            "dual_traced_count": qa_report["dual_traced_count"],
            "dual_traced_pct": qa_report["dual_traced_pct"],
            "mean_iou": qa_report["mean_iou"],
            "median_iou": qa_report["median_iou"],
            "disagreement_count": qa_report["disagreement_count"],
        },
        "elapsed_seconds": round(time.time() - t0, 2),
    }
    with open(record_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    return summary


def _svg_to_shapely_geom(svg_path: Path, px_per_mm: Optional[float] = None) -> Any:
    """Reconstruct Method A Shapely geometry in raw pixel units directly from saved SVG #figures path."""
    root = ET.parse(svg_path).getroot()
    ns = "{http://www.w3.org/2000/svg}"
    path_el = root.find(f"{ns}g[@id='figures']/{ns}path")
    if path_el is None:
        return None
    d = path_el.attrib.get("d", "")
    scale = float(px_per_mm) if (px_per_mm and px_per_mm > 0) else 1.0
    rings: List[Any] = []
    for sub in d.split("Z"):
        sub = sub.strip()
        if not sub:
            continue
        coords = [(float(x) * scale, float(y) * scale) for x, y in re.findall(r"([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)", sub)]
        if len(coords) >= 3:
            p = Polygon(coords)
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 0.5:
                rings.append(p)
    if not rings:
        return None
    geom = rings[0]
    for r in rings[1:]:
        geom = geom.symmetric_difference(r)
    if not geom.is_valid:
        geom = geom.buffer(0)
    return geom


def rerun_qa_from_saved_record(
    base_dir: Path = DEFAULT_BASE_DIR,
    record_dir: Optional[Path] = None,
    qa_dir: Optional[Path] = None,
    qa_min_fraction: float = 0.05,
) -> Dict[str, Any]:
    """Re-run independent >=5% dual-trace QA directly from saved scientific_record/figures.jsonl & SVGs."""
    from .dual_trace_qa import select_stratified_qa_sample

    record_dir = record_dir or (base_dir / "scientific_record")
    qa_dir = qa_dir or (base_dir / "qa")
    overlays_dir = qa_dir / "overlays"
    if overlays_dir.exists():
        shutil.rmtree(overlays_dir)

    records = [
        json.loads(line)
        for line in (record_dir / "figures.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    sample_records = select_stratified_qa_sample(records, min_fraction=qa_min_fraction)
    traced_by_id: Dict[str, TracedFigure] = {}
    empty_mask = np.empty((0, 0), dtype=np.uint8)
    for rec in sample_records:
        fid = rec["figure_id"]
        px_per_mm = rec["size_mm"].get("px_per_mm") if rec["size_mm"].get("scale_calibrated") else None
        geom = _svg_to_shapely_geom(base_dir / rec["svg_path"], px_per_mm=px_per_mm)
        if geom is None:
            continue
        g_meta = rec["geometry"]
        traced_by_id[fid] = TracedFigure(
            panel_index=0,
            crop_px=rec["crop_px"],
            raw_shape_hw=(0, 0),
            work_scale=1.0,
            geometry=geom,
            mask_crop=empty_mask,
            subpaths_px=[],
            cup_marks_px=[],
            cracks_px=[],
            uncertain_px=[],
            metrics=g_meta,
            method=rec["method"],
            polarity="auto",
            ink_mode="standard_black_ink",
        )
    valid_sample = [r for r in sample_records if r["figure_id"] in traced_by_id]
    # Pass min_fraction=1.0 on the pre-selected stratified sample, then restore true corpus denominator
    qa_report = run_dual_trace_qa(
        records=valid_sample,
        traced_by_id=traced_by_id,
        base_dir=base_dir,
        qa_dir=qa_dir,
        min_fraction=1.0,
    )
    n_total = len(records)
    n_qa = qa_report["dual_traced_count"]
    qa_report["total_corpus_figures"] = n_total
    qa_report["dual_traced_pct"] = round(100.0 * n_qa / max(n_total, 1), 2)
    qa_report["meets_5pct_gate"] = (n_qa / float(max(n_total, 1))) >= qa_min_fraction if n_total > 0 else False
    with open(qa_dir / "dual_trace_report.json", "w", encoding="utf-8") as f:
        json.dump(qa_report, f, indent=2, ensure_ascii=False)
    # Refresh markdown header line with true total corpus figures
    md_path = qa_dir / "dual_trace_report.md"
    if md_path.is_file():
        md_txt = md_path.read_text(encoding="utf-8")
        md_txt = re.sub(r"- \*\*Corpus figures\*\*: \d+", f"- **Corpus figures**: {n_total}", md_txt)
        md_txt = re.sub(
            r"- \*\*Dual-traced sample\*\*: \d+ \([^)]+\)",
            f"- **Dual-traced sample**: {n_qa} ({qa_report['dual_traced_pct']:.2f}%, >=5% gate: {'PASS' if qa_report['meets_5pct_gate'] else 'FAIL'})",
            md_txt,
        )
        md_path.write_text(md_txt, encoding="utf-8")

    sum_path = record_dir / "summary.json"
    if sum_path.is_file():
        s = json.loads(sum_path.read_text(encoding="utf-8"))
        s["qa_summary"] = {
            "dual_traced_count": qa_report["dual_traced_count"],
            "dual_traced_pct": qa_report["dual_traced_pct"],
            "mean_iou": qa_report["mean_iou"],
            "median_iou": qa_report["median_iou"],
            "disagreement_count": qa_report["disagreement_count"],
        }
        sum_path.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    return qa_report


def main() -> None:
    parser = argparse.ArgumentParser(description="True-scale scientific rock-art tracing and dual-trace QA pipeline")
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--sources-dir", type=Path, default=None)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--record-dir", type=Path, default=None)
    parser.add_argument("--qa-dir", type=Path, default=None)
    parser.add_argument("--taxonomy", type=Path, default=None)
    parser.add_argument("--max-motifs-per-panel", type=int, default=24)
    parser.add_argument("--qa-min-fraction", type=float, default=0.05)
    parser.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)))
    parser.add_argument("--no-clean", action="store_true")
    parser.add_argument("--qa-only", action="store_true")
    args = parser.parse_args()

    if args.qa_only:
        qa_rep = rerun_qa_from_saved_record(
            base_dir=args.base_dir,
            record_dir=args.record_dir,
            qa_dir=args.qa_dir,
            qa_min_fraction=args.qa_min_fraction,
        )
        print(json.dumps({
            "dual_traced_count": qa_rep["dual_traced_count"],
            "dual_traced_pct": qa_rep["dual_traced_pct"],
            "mean_iou": qa_rep["mean_iou"],
            "median_iou": qa_rep["median_iou"],
            "disagreement_count": qa_rep["disagreement_count"],
            "by_media_type": qa_rep["by_media_type"],
        }, indent=2))
        return

    summary = run_science_pipeline(
        base_dir=args.base_dir,
        sources_dir=args.sources_dir,
        metadata_dir=args.metadata_dir,
        record_dir=args.record_dir,
        qa_dir=args.qa_dir,
        taxonomy_path=args.taxonomy,
        clean_output=not args.no_clean,
        max_motifs_per_panel=args.max_motifs_per_panel,
        qa_min_fraction=args.qa_min_fraction,
        workers=args.workers,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
