"""Perceptual shape deduplication and leak-free grouped train/val/test splitting."""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np
from scipy.fft import dctn


_MEDIA_PRIORITY = {
    "svg_vector": 4,
    "bw_tracing": 3,
    "rubbing_frottage": 2,
    "painted_photo": 1,
    "photo_other": 0,
}

_TIER_PRIORITY = {
    "A_ready": 3,
    "B_thickened": 2,
    "C_fragile_or_review": 1,
}


def compute_shape_phash(mask_256: np.ndarray) -> Tuple[int, int, str]:
    """Compute 64-bit DCT perceptual hash for a 256x256 binary mask and its horizontal mirror.

    Returns (phash_int, phash_flipped_int, phash_hex).
    """
    def _dct_hash(arr: np.ndarray) -> int:
        small = cv2.resize(arr.astype(np.float32), (32, 32), interpolation=cv2.INTER_AREA)
        dct = dctn(small, type=2, norm="ortho")
        low = dct[:8, :8].flatten()
        # Exclude DC component [0] for median threshold
        med = float(np.median(low[1:]))
        bits = low > med
        val = 0
        for b in bits:
            val = (val << 1) | int(bool(b))
        return val

    h_orig = _dct_hash(mask_256)
    h_flip = _dct_hash(np.fliplr(mask_256))
    return h_orig, h_flip, f"{h_orig:016x}"


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _mask_iou(m1: np.ndarray, m2: np.ndarray) -> float:
    b1 = m1 > 0
    b2 = m2 > 0
    inter = int(np.sum(b1 & b2))
    union = int(np.sum(b1 | b2))
    return inter / float(max(union, 1))


def _quality_rank_key(item: Dict[str, Any]) -> Tuple:
    """Higher tuple = preferred canonical representative when deduplicating near-identical shapes."""
    return (
        1 if item.get("commercial_ok") else 0,
        _TIER_PRIORITY.get(item.get("jigsaw_tier", ""), 0),
        _MEDIA_PRIORITY.get(item.get("media_type", ""), 0),
        1 if item.get("motif_class") not in ("unclassified_motif", "multi_motif_panel") else 0,
        float(item.get("metrics", {}).get("erosion_survival_ratio", 0.0)),
        int(item.get("raw_bbox_area_px", 0)),
        str(item.get("source_id", "")),
    )


def deduplicate_motifs(
    items: List[Dict[str, Any]],
    iou_threshold: float = 0.92,
    phash_max_hamming: int = 8,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Deduplicate near-identical silhouettes (including horizontal mirrors).

    Each item must have 'mask_256' and 'metrics'. Adds 'shape_phash' to every kept item.
    Returns (kept_items, dropped_duplicates_log).
    """
    if not items:
        return [], []

    # Sort best quality first so highest-quality/permissive-license original wins
    ranked = sorted(items, key=_quality_rank_key, reverse=True)

    kept: List[Dict[str, Any]] = []
    kept_hashes: List[Tuple[int, int]] = []
    kept_aspects: List[float] = []
    dropped: List[Dict[str, Any]] = []

    for item in ranked:
        mask = item["mask_256"]
        h_orig, h_flip, h_hex = compute_shape_phash(mask)
        item["shape_phash"] = h_hex
        ar = float(item["metrics"].get("aspect_ratio", 1.0))

        dup_of = None
        dup_iou = 0.0
        for idx, (kh, kh_flip) in enumerate(kept_hashes):
            # Fast aspect & perceptual hash pre-filter
            kar = kept_aspects[idx]
            if max(ar / max(kar, 1e-4), kar / max(ar, 1e-4)) > 1.18:
                continue
            hd = min(_hamming(h_orig, kh), _hamming(h_flip, kh), _hamming(h_orig, kh_flip))
            if hd > phash_max_hamming:
                continue
            kmask = kept[idx]["mask_256"]
            iou = max(_mask_iou(mask, kmask), _mask_iou(np.fliplr(mask), kmask))
            if iou >= iou_threshold:
                dup_of = kept[idx]
                dup_iou = iou
                break

        if dup_of is not None:
            dropped.append(
                {
                    "dropped_source_id": item.get("source_id"),
                    "dropped_sha256": item.get("sha256"),
                    "kept_source_id": dup_of.get("source_id"),
                    "kept_sha256": dup_of.get("sha256"),
                    "iou": round(dup_iou, 4),
                    "shape_phash": h_hex,
                }
            )
        else:
            kept.append(item)
            kept_hashes.append((h_orig, h_flip))
            kept_aspects.append(ar)

    return kept, dropped


def assign_grouped_splits(
    items: List[Dict[str, Any]],
    ratios: Tuple[float, float, float] = (0.80, 0.10, 0.10),
    seed: int = 20261125,
) -> Dict[str, Any]:
    """Assign leak-free train/val/test splits strictly grouped by source_group & sha256.

    No source_group AND no sha256 can ever appear in more than one split.
    Balances total count and family distribution deterministically across splits.
    """
    if not items:
        return {"train": 0, "val": 0, "test": 0, "groups": {"train": 0, "val": 0, "test": 0}}

    # Build atomic groups: link any items sharing EITHER source_group OR sha256
    # (Using union-find guarantees zero leakage on both source_group and sha256)
    parent = list(range(len(items)))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    seen_sg: Dict[str, int] = {}
    seen_sha: Dict[str, int] = {}
    for i, it in enumerate(items):
        sg = str(it.get("source_group") or "")
        sha = str(it.get("sha256") or "")
        if sg:
            if sg in seen_sg:
                _union(i, seen_sg[sg])
            else:
                seen_sg[sg] = i
        if sha:
            if sha in seen_sha:
                _union(i, seen_sha[sha])
            else:
                seen_sha[sha] = i

    groups: Dict[int, List[int]] = {}
    for i in range(len(items)):
        groups.setdefault(_find(i), []).append(i)

    # Sort groups deterministically: largest first with seeded hash tie-break for good bin packing
    def _group_sort_key(g_indices: List[int]) -> Tuple[int, str]:
        sg = str(items[g_indices[0]].get("source_group", ""))
        h = hashlib.sha256(f"{seed}:{sg}".encode("utf-8")).hexdigest()
        return (-len(g_indices), h)

    ordered_groups = sorted(groups.values(), key=_group_sort_key)

    total = len(items)
    target = {
        "train": total * ratios[0],
        "val": total * ratios[1],
        "test": total * ratios[2],
    }
    counts = {"train": 0, "val": 0, "test": 0}
    group_counts = {"train": 0, "val": 0, "test": 0}
    fam_counts: Dict[str, Dict[str, int]] = {"train": {}, "val": {}, "test": {}}

    # Compute overall family totals for soft stratification
    fam_totals: Dict[str, int] = {}
    for it in items:
        f = str(it.get("family", "scene_or_unclassified"))
        fam_totals[f] = fam_totals.get(f, 0) + 1

    split_names = ("train", "val", "test")
    ratio_map = {"train": ratios[0], "val": ratios[1], "test": ratios[2]}

    for g_idx_list in ordered_groups:
        g_size = len(g_idx_list)
        g_fams: Dict[str, int] = {}
        for idx in g_idx_list:
            f = str(items[idx].get("family", "scene_or_unclassified"))
            g_fams[f] = g_fams.get(f, 0) + 1

        # If we have >= 3 groups total, ensure val and test each receive at least 1 group before packing
        best_split = None
        if len(ordered_groups) >= 3:
            for sp in ("val", "test"):
                if group_counts[sp] == 0 and g_size <= max(1, int(round(0.35 * total))):
                    best_split = sp
                    break

        if best_split is None:
            # Choose split maximizing deficit ratio (total count deficit + family stratification deficit)
            best_score = -1e18
            for sp in split_names:
                rem = target[sp] - counts[sp]
                score = rem / max(target[sp], 1.0)
                # Family balance term
                fam_bonus = 0.0
                for f, cnt in g_fams.items():
                    f_target = fam_totals[f] * ratio_map[sp]
                    f_cur = fam_counts[sp].get(f, 0)
                    fam_bonus += (f_target - f_cur) / max(f_target, 1.0) * (cnt / float(g_size))
                score += 0.35 * fam_bonus
                # Slight preference for train if tiny dataset
                if score > best_score:
                    best_score = score
                    best_split = sp

        assert best_split is not None
        for idx in g_idx_list:
            items[idx]["split"] = best_split
        counts[best_split] += g_size
        group_counts[best_split] += 1
        for f, cnt in g_fams.items():
            fam_counts[best_split][f] = fam_counts[best_split].get(f, 0) + cnt

    # Hard leak-free assertion
    sg_by_split: Dict[str, set[str]] = {s: set() for s in split_names}
    sha_by_split: Dict[str, set[str]] = {s: set() for s in split_names}
    for it in items:
        sp = it["split"]
        if it.get("source_group"):
            sg_by_split[sp].add(str(it["source_group"]))
        if it.get("sha256"):
            sha_by_split[sp].add(str(it["sha256"]))

    for i in range(3):
        for j in range(i + 1, 3):
            s1, s2 = split_names[i], split_names[j]
            assert sg_by_split[s1].isdisjoint(sg_by_split[s2]), f"source_group leak between {s1} and {s2}"
            assert sha_by_split[s1].isdisjoint(sha_by_split[s2]), f"sha256 leak between {s1} and {s2}"

    return {"counts": counts, "groups": group_counts}
