"""End-to-end batch CLI for the Nordic helleristninger extraction and silhouette pipeline."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import shutil
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .dedup_split import assign_grouped_splits, deduplicate_motifs
from .export import export_corpus
from .image_extract import extract_foreground_mask
from .prune_corpus import evaluate_motif_quality
from .segment import segment_motifs
from .silhouette import convert_to_jigsaw_silhouette
from .taxonomy import build_class_index, classify_motif, load_taxonomy, validate_commercial_ok


DEFAULT_BASE_DIR = Path(__file__).resolve().parent.parent


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_manifests(sources_dir: Path) -> List[Path]:
    manifests = sorted(sources_dir.glob("*/manifest.jsonl"))
    return [m for m in manifests if m.is_file()]


def load_manifest_records(manifests: List[Path], base_dir: Path) -> Tuple_Records:
    """Read all JSONL manifest records and verify file existence & SHA-256."""
    records: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    seen_sha: set[str] = set()
    dup_raw_count = 0

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

                raw_rel = str(rec.get("raw_path") or "").replace("\\", "/")
                raw_abs = (base_dir / raw_rel).resolve()
                if not raw_abs.is_file():
                    # Try relative to manifest parent
                    alt = (mf.parent / Path(raw_rel).name).resolve()
                    if alt.is_file():
                        raw_abs = alt
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
                    issues.append(
                        {
                            "manifest": str(mf),
                            "source_id": rec.get("source_id"),
                            "warning": "sha256_mismatch_using_actual",
                            "manifest_sha256": manifest_sha,
                            "actual_sha256": actual_sha,
                        }
                    )

                if actual_sha in seen_sha:
                    dup_raw_count += 1
                    continue
                seen_sha.add(actual_sha)

                rec = dict(rec)
                rec["raw_path"] = raw_rel
                rec["_raw_abs_path"] = raw_abs
                rec["sha256"] = actual_sha
                rec["commercial_ok"] = validate_commercial_ok(rec.get("license"), rec.get("commercial_ok"))
                rec["country"] = str(rec.get("country") or "NORDIC").upper()
                if rec["country"] not in ("NO", "SE", "DK", "FI", "NORDIC"):
                    rec["country"] = "NORDIC"
                records.append(rec)

    return records, issues, dup_raw_count


Tuple_Records = tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]


def process_raw_record(
    rec: Dict[str, Any],
    class_index: Dict[str, Dict[str, Any]],
    max_motifs_per_panel: int = 30,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Run full extraction -> segmentation -> silhouette conversion -> classification on one raw file."""
    raw_abs: Path = rec["_raw_abs_path"]
    media_type = str(rec.get("media_type") or "photo_other")

    fg_mask, ext_info = extract_foreground_mask(raw_abs, media_type=media_type)
    if fg_mask is None:
        return [], {"status": "rejected_extract", "reason": ext_info.get("reject_reason", "extract_failed"), "detail": ext_info}

    candidates, seg_info = segment_motifs(
        fg_mask,
        media_type=media_type,
        max_motifs_per_panel=max_motifs_per_panel,
    )
    if not candidates:
        return [], {"status": "rejected_segment", "reason": "no_valid_motifs", "detail": seg_info}

    extracted: List[Dict[str, Any]] = []
    sil_rejects: Counter = Counter()

    # First convert all candidate crops to silhouettes so we know exact final count & shape metrics
    converted = []
    for cand in candidates:
        proc, sil_diag = convert_to_jigsaw_silhouette(cand.mask)
        if proc is None:
            sil_rejects[sil_diag.get("reject_reason", "silhouette_failed")] += 1
            continue
        converted.append((cand, proc))

    total_kept_from_panel = len(converted)
    source_group = f"sha256_{rec['sha256'][:16]}"

    for p_idx, (cand, proc) in enumerate(converted):
        m = proc.metrics
        # Ruthless silhouette quality gate: reject featureless blobs, ovals, sticks, scribbles, and fragile junk
        ar_sym = max(m["aspect_ratio"], 1.0 / max(m["aspect_ratio"], 1e-4))
        if proc.jigsaw_tier == "C_fragile_or_review" or not m["passes_kerf_gate"]:
            sil_rejects["rejected_tier_c_or_fragile"] += 1
            continue
        if m["erosion_survival_ratio"] < 0.24:
            sil_rejects["low_erosion_survival"] += 1
            continue
        if ar_sym > 5.2:
            sil_rejects["extreme_stick_aspect"] += 1
            continue
        if m["vertex_count"] < 14 or m["vertex_count"] > 180:
            sil_rejects["bad_vertex_complexity"] += 1
            continue
        # Reject featureless round/oval blobs & rectangles (allow distinct elongated footprints)
        if m["compactness"] >= 0.74 and m["solidity"] >= 0.87 and ar_sym < 1.85:
            sil_rejects["featureless_round_blob"] += 1
            continue
        if m["area_ratio"] >= 0.78 and m["solidity"] >= 0.90:
            sil_rejects["featureless_box_or_wedge"] += 1
            continue
        if m["compactness"] < 0.095 or m["solidity"] < 0.25:
            sil_rejects["spindly_crack_or_scribble"] += 1
            continue

        family, motif_class, conf = classify_motif(
            rec,
            class_index=class_index,
            num_extracted_from_source=total_kept_from_panel,
            shape_metrics=m,
        )

        # Extra strict filter for photographs: require clean silhouette solidity & reasonable complexity
        if media_type in ("painted_photo", "photo_other"):
            if m["vertex_count"] > 140 or m["solidity"] < 0.34 or m["erosion_survival_ratio"] < 0.30:
                sil_rejects["photo_noise_blob"] += 1
                continue

        # Apply the same geometric artifact & glyph quality gate used in prune_corpus
        probe_rec = {
            "media_type": media_type,
            "render_style": proc.render_style,
            "motif_class": motif_class,
            "jigsaw_tier": proc.jigsaw_tier,
            "solidity": m["solidity"],
            "compactness": m["compactness"],
            "aspect_ratio": m["aspect_ratio"],
            "min_neck_mm_at_60mm": m["min_neck_mm_at_60mm"],
            "erosion_survival_ratio": m["erosion_survival_ratio"],
            "vertex_count": m["vertex_count"],
        }
        q_ok, q_reason = evaluate_motif_quality(probe_rec, proc.polygon, proc.mask_256)
        if not q_ok:
            sil_rejects[q_reason] += 1
            continue

        extracted.append(
            {
                "source_id": str(rec.get("source_id") or raw_abs.stem),
                "source_archive": str(rec.get("source_archive") or "unknown"),
                "source_group": source_group,
                "panel_motif_index": p_idx,
                "panel_total_motifs": total_kept_from_panel,
                "title": str(rec.get("title") or ""),
                "description": str(rec.get("description") or ""),
                "site": str(rec.get("site") or ""),
                "region": str(rec.get("region") or ""),
                "country": str(rec.get("country") or "NORDIC"),
                "period": str(rec.get("period") or "unknown"),
                "media_type": media_type,
                "motif_hints": rec.get("motif_hints") or [],
                "raw_path": str(rec["raw_path"]),
                "source_url": str(rec.get("source_url") or ""),
                "download_url": str(rec.get("download_url") or ""),
                "creator": str(rec.get("creator") or ""),
                "license": str(rec.get("license") or ""),
                "license_url": str(rec.get("license_url") or ""),
                "commercial_ok": bool(rec["commercial_ok"]),
                "sha256": str(rec["sha256"]),
                "family": family,
                "motif_class": motif_class,
                "classification_confidence": conf,
                "render_style": proc.render_style,
                "was_thickened": proc.was_thickened,
                "jigsaw_tier": proc.jigsaw_tier,
                "polygon": proc.polygon,
                "coords": proc.coords,
                "mask_256": proc.mask_256,
                "sdf_256": proc.sdf_256,
                "metrics": proc.metrics,
                "raw_bbox_px": list(cand.bbox),
                "raw_bbox_area_px": int(cand.bbox[2] * cand.bbox[3]),
            }
        )

    return extracted, {
        "status": "ok" if extracted else "rejected_silhouette",
        "extracted_count": len(extracted),
        "sil_rejects": dict(sil_rejects),
        "seg_info": seg_info,
        "ext_info": ext_info,
    }


def _worker_process_record(
    args: Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], int, bool]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    rec, class_index, max_motifs_per_panel, keep_tier_c = args
    try:
        items, info = process_raw_record(rec, class_index=class_index, max_motifs_per_panel=max_motifs_per_panel)
    except Exception as e:
        return rec, [], {"status": "rejected_exception", "reason": f"exception:{type(e).__name__}:{e}"}
    if info["status"] == "ok" and not keep_tier_c:
        items = [it for it in items if it["jigsaw_tier"] != "C_fragile_or_review"]
    # Drop Shapely polygon & float32 SDF before IPC return (SDF is computed on-the-fly in export_corpus to save 5x RAM!)
    for it in items:
        it.pop("polygon", None)
        it.pop("sdf_256", None)
    gc.collect()
    return rec, items, info


def run_batch(
    base_dir: Path = DEFAULT_BASE_DIR,
    sources_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    taxonomy_path: Optional[Path] = None,
    clean_output: bool = True,
    max_motifs_per_panel: int = 30,
    keep_tier_c: bool = True,
    workers: int = 1,
) -> Dict[str, Any]:
    """Run the entire Nordic helleristninger batch pipeline end-to-end."""
    t0 = time.time()
    sources_dir = sources_dir or (base_dir / "sources")
    output_dir = output_dir or (base_dir / "processed")
    taxonomy_path = taxonomy_path or (base_dir / "taxonomy.json")

    taxonomy = load_taxonomy(taxonomy_path)
    class_index = build_class_index(taxonomy)

    manifests = discover_manifests(sources_dir)
    raw_records, manifest_issues, dup_raw_count = load_manifest_records(manifests, base_dir=base_dir)

    all_items: List[Dict[str, Any]] = []
    status_counts: Counter = Counter()
    reject_reasons: Counter = Counter()
    raw_by_archive: Counter = Counter()
    raw_by_media: Counter = Counter()
    per_file_log: List[Dict[str, Any]] = []

    def _consume_result(rec: Dict[str, Any], items: List[Dict[str, Any]], info: Dict[str, Any]) -> None:
        raw_by_archive[str(rec.get("source_archive", "unknown"))] += 1
        raw_by_media[str(rec.get("media_type", "unknown"))] += 1
        status_counts[info["status"]] += 1
        if info["status"] != "ok":
            reject_reasons[info.get("reason", info["status"])] += 1
        else:
            all_items.extend(items)
        per_file_log.append(
            {
                "source_id": rec.get("source_id"),
                "source_archive": rec.get("source_archive"),
                "media_type": rec.get("media_type"),
                "status": info["status"],
                "reason": info.get("reason", ""),
                "extracted_count": info.get("extracted_count", 0),
            }
        )

    eff_workers = max(1, min(workers, len(raw_records) if raw_records else 1))
    done_count = 0
    total_raw = len(raw_records)
    if eff_workers == 1:
        for rec in raw_records:
            r_out, items, info = _worker_process_record((rec, class_index, max_motifs_per_panel, keep_tier_c))
            _consume_result(r_out, items, info)
            done_count += 1
            if done_count % 100 == 0 or done_count == total_raw:
                print(f"[pipeline] {done_count}/{total_raw} raw files -> {len(all_items)} motifs ({time.time()-t0:.1f}s)", flush=True)
    else:
        max_in_flight = eff_workers * 2
        idx_next = 0
        pending = set()
        with ProcessPoolExecutor(max_workers=eff_workers) as pool:
            while idx_next < total_raw and len(pending) < max_in_flight:
                pending.add(
                    pool.submit(
                        _worker_process_record,
                        (raw_records[idx_next], class_index, max_motifs_per_panel, keep_tier_c),
                    )
                )
                idx_next += 1
            while pending:
                done_set, pending = wait(pending, return_when=FIRST_COMPLETED)
                for fut in done_set:
                    r_out, items, info = fut.result()
                    _consume_result(r_out, items, info)
                    done_count += 1
                    if done_count % 50 == 0 or done_count == total_raw:
                        print(f"[pipeline] {done_count}/{total_raw} raw files -> {len(all_items)} motifs ({time.time()-t0:.1f}s)", flush=True)
                while idx_next < total_raw and len(pending) < max_in_flight:
                    pending.add(
                        pool.submit(
                            _worker_process_record,
                            (raw_records[idx_next], class_index, max_motifs_per_panel, keep_tier_c),
                        )
                    )
                    idx_next += 1

    # Deduplicate near-identical silhouettes
    kept_items, dedup_dropped = deduplicate_motifs(all_items)

    # Assign leak-free grouped train/val/test splits
    split_summary = assign_grouped_splits(kept_items)

    # Clean stale output artifacts if requested
    if clean_output and output_dir.exists():
        for sub in ("svg", "masks", "polygons", "sdf", "contact_sheets", "figma_sheets"):
            p = output_dir / sub
            if p.exists():
                shutil.rmtree(p)
    output_dir.mkdir(parents=True, exist_ok=True)

    pipeline_summary = {
        "manifests_read": [str(m.relative_to(base_dir)).replace("\\", "/") for m in manifests],
        "raw_files_loaded": len(raw_records),
        "raw_duplicate_sha256_skipped": dup_raw_count,
        "manifest_issues_count": len(manifest_issues),
        "manifest_issues_sample": manifest_issues[:20],
        "raw_by_archive": dict(raw_by_archive),
        "raw_by_media_type": dict(raw_by_media),
        "raw_processing_status": dict(status_counts),
        "raw_reject_reasons": dict(reject_reasons),
        "pre_dedup_motifs": len(all_items),
        "post_dedup_motifs": len(kept_items),
        "elapsed_seconds": round(time.time() - t0, 2),
    }

    stats = export_corpus(
        kept_items,
        output_dir=output_dir,
        pipeline_summary=pipeline_summary,
        split_summary=split_summary,
        dedup_dropped=dedup_dropped,
    )

    with open(output_dir / "ingest_log.json", "w", encoding="utf-8") as f:
        json.dump({"summary": pipeline_summary, "files": per_file_log}, f, indent=2, ensure_ascii=False)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Nordic helleristninger rock-art silhouette pipeline")
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--sources-dir", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--taxonomy", type=Path, default=None)
    parser.add_argument("--max-motifs-per-panel", type=int, default=12)
    parser.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)))
    parser.add_argument("--no-clean", action="store_true")
    parser.add_argument("--keep-tier-c", action="store_true")
    args = parser.parse_args()

    stats = run_batch(
        base_dir=args.base_dir,
        sources_dir=args.sources_dir,
        output_dir=args.output_dir,
        taxonomy_path=args.taxonomy,
        clean_output=not args.no_clean,
        max_motifs_per_panel=args.max_motifs_per_panel,
        keep_tier_c=args.keep_tier_c,
        workers=args.workers,
    )
    print(json.dumps({
        "total_motifs": stats["total_motifs"],
        "tier_counts": stats["tier_counts"],
        "by_archive": stats["by_archive"],
        "by_country": stats["by_country"],
        "by_family": stats["by_family"],
        "by_render_style": stats["by_render_style"],
        "by_split": stats["by_split"],
        "commercial_ok_counts": stats["commercial_ok_counts"],
        "elapsed_seconds": stats["pipeline_ingest_summary"]["elapsed_seconds"],
    }, indent=2))


if __name__ == "__main__":
    main()
