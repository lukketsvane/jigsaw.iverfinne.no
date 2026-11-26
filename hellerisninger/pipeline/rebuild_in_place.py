"""In-place surgical prune & exact rebuild preserving surviving asset_ids and splits."""

from __future__ import annotations

import gc
import json
import shutil
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .export import export_corpus
from .image_extract import extract_foreground_mask
from .segment import segment_motifs
from .silhouette import EROSION_RADIUS_NORM, NOMINAL_PIECE_MM, convert_to_jigsaw_silhouette

# Exact 18 defect IDs reported by @verifier (#21) + 2 extra Tier A visual-QA artifacts (00022 chevron, 00086 cupmark-tail)
DROP_IDS = {
    # Kerf/neck failure from @verifier
    "helleristning_00276",
    # Tier A printed glyphs / digits / bars
    "helleristning_00005",
    "helleristning_00071",
    "helleristning_00072",
    "helleristning_00077",
    "helleristning_00081",
    # Tier A cup-mark / circle blobs / chevron / balloon
    "helleristning_00022",
    "helleristning_00035",
    "helleristning_00073",
    "helleristning_00074",
    "helleristning_00086",
    "helleristning_00108",
    "helleristning_00109",
    # Tier B north arrows, handwritten/printed text captions, dumbbells
    "helleristning_00110",
    "helleristning_00117",
    "helleristning_00119",
    "helleristning_00120",
    "helleristning_01432",
    "helleristning_01657",
    "helleristning_01658",
}


def _strict_neck_ok(poly) -> bool:
    """Double-check conservative 3.0 + 0.15 mm neck survival at 60 mm nominal piece scale."""
    minx, miny, maxx, maxy = poly.bounds
    max_dim = max(maxx - minx, maxy - miny, 1e-6)
    # Test both exact 0.02625 and conservative +3% margin
    for mult in (1.0, 1.03):
        r = EROSION_RADIUS_NORM * max_dim * mult
        er = poly.buffer(-r)
        if er.is_empty or er.geom_type != "Polygon":
            return False
    return True


def _rebuild_one_raw(args: Tuple[str, List[Dict[str, Any]], str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    raw_rel, recs, base_str = args
    base_dir = Path(base_str)
    raw_abs = (base_dir / raw_rel).resolve()
    media_type = str(recs[0].get("media_type") or "bw_tracing")

    fg_mask, _ = extract_foreground_mask(raw_abs, media_type=media_type)
    if fg_mask is None:
        return [], [r["asset_id"] for r in recs]

    cands, _ = segment_motifs(fg_mask, media_type=media_type, max_motifs_per_panel=12)
    cand_by_bbox = {tuple(int(v) for v in c.bbox): c for c in cands}

    rebuilt: List[Dict[str, Any]] = []
    dropped: List[str] = []

    for rec in recs:
        target_bbox = tuple(int(v) for v in rec.get("raw_bbox_px", [0, 0, 0, 0]))
        cand = cand_by_bbox.get(target_bbox)
        if cand is None and cands:
            # Nearest bbox fallback within 4px tolerance
            best_c = min(
                cands,
                key=lambda c: sum(abs(int(a) - int(b)) for a, b in zip(c.bbox, target_bbox)),
            )
            if sum(abs(int(a) - int(b)) for a, b in zip(best_c.bbox, target_bbox)) <= 8:
                cand = best_c

        if cand is None:
            dropped.append(rec["asset_id"])
            continue

        proc, _ = convert_to_jigsaw_silhouette(cand.mask)
        if proc is None or not proc.metrics.get("passes_kerf_gate") or not _strict_neck_ok(proc.polygon):
            dropped.append(rec["asset_id"])
            continue

        item = dict(rec)
        item["coords"] = proc.coords
        item["mask_256"] = proc.mask_256
        item["metrics"] = proc.metrics
        item["render_style"] = proc.render_style
        item["was_thickened"] = proc.was_thickened
        item["jigsaw_tier"] = proc.jigsaw_tier
        item["raw_bbox_px"] = list(cand.bbox)
        item["raw_bbox_area_px"] = int(cand.bbox[2] * cand.bbox[3])
        rebuilt.append(item)

    gc.collect()
    return rebuilt, dropped


def main() -> None:
    t0 = time.time()
    base_dir = Path(__file__).resolve().parent.parent
    proc_dir = base_dir / "processed"
    cat_path = proc_dir / "catalog.json"
    stats_path = proc_dir / "stats.json"

    # Drop all 18 @verifier defect IDs + 2 extra Tier A visual-QA artifacts unconditionally
    effective_drop = set(DROP_IDS)

    catalog = json.loads(cat_path.read_text(encoding="utf-8"))
    old_stats = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}
    old_ingest = old_stats.get("pipeline_ingest_summary", {})

    surviving = [r for r in catalog if r["asset_id"] not in effective_drop]
    print(f"Catalog={len(catalog)}, dropping={len(catalog)-len(surviving)}, rebuilding={len(surviving)}", flush=True)

    by_raw: Dict[str, List[Dict[str, Any]]] = {}
    for r in surviving:
        by_raw.setdefault(r["raw_path"], []).append(r)

    tasks = [(raw_rel, recs, str(base_dir)) for raw_rel, recs in by_raw.items()]
    rebuilt_all: List[Dict[str, Any]] = []
    failed_ids: List[str] = []

    workers = 4
    max_in_flight = workers * 2
    idx_next = 0
    pending = set()
    done_files = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        while idx_next < len(tasks) and len(pending) < max_in_flight:
            pending.add(pool.submit(_rebuild_one_raw, tasks[idx_next]))
            idx_next += 1
        while pending:
            done_set, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done_set:
                rb, dr = fut.result()
                rebuilt_all.extend(rb)
                failed_ids.extend(dr)
                done_files += 1
                if done_files % 100 == 0 or done_files == len(tasks):
                    print(f"[rebuild] {done_files}/{len(tasks)} raw files -> {len(rebuilt_all)} rebuilt, {len(failed_ids)} dropped ({time.time()-t0:.1f}s)", flush=True)
            while idx_next < len(tasks) and len(pending) < max_in_flight:
                pending.add(pool.submit(_rebuild_one_raw, tasks[idx_next]))
                idx_next += 1

    # Preserve exact existing split summary
    split_counts = dict(Counter(it["split"] for it in rebuilt_all))
    split_groups = {sp: len({it["source_group"] for it in rebuilt_all if it["split"] == sp}) for sp in ("train", "val", "test")}
    split_summary = {"counts": split_counts, "groups": split_groups}

    # Verify zero split leakage
    for s1, s2 in (("train", "val"), ("train", "test"), ("val", "test")):
        g1 = {it["source_group"] for it in rebuilt_all if it["split"] == s1}
        g2 = {it["source_group"] for it in rebuilt_all if it["split"] == s2}
        assert g1.isdisjoint(g2), f"Split leak {s1}/{s2}"

    # Wipe partial/stale dirs and export clean in-place preserving exact surviving asset_ids
    for sub in ("svg", "masks", "polygons", "sdf", "contact_sheets", "figma_sheets"):
        p = proc_dir / sub
        if p.exists():
            shutil.rmtree(p)

    ingest_summary = dict(old_ingest)
    ingest_summary["verifier_surgical_drop_count"] = len(effective_drop)
    ingest_summary["verifier_surgical_drop_ids"] = sorted(effective_drop)
    ingest_summary["rebuild_strict_kerf_dropped"] = failed_ids
    ingest_summary["final_preserved_motifs"] = len(rebuilt_all)

    stats = export_corpus(
        rebuilt_all,
        output_dir=proc_dir,
        pipeline_summary=ingest_summary,
        split_summary=split_summary,
        dedup_dropped=[],
        preserve_asset_ids=True,
    )
    print(
        json.dumps(
            {
                "total_motifs": stats["total_motifs"],
                "tier_counts": stats["tier_counts"],
                "by_country": stats["by_country"],
                "by_family": stats["by_family"],
                "by_split": stats["by_split"],
                "commercial_ok_counts": stats["commercial_ok_counts"],
                "contact_sheets_count": len(stats["contact_sheets"]),
                "figma_sheets": stats["figma_sheets"],
                "failed_or_kerf_dropped": failed_ids,
                "elapsed_s": round(time.time() - t0, 1),
            },
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
