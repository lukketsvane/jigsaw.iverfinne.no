"""Commercial-clean 750-motif curator combining multi-genus trilobites + Nordic helleristninger.

Hard guarantees:
- Exact N=750 motifs (default 150 varied multi-genus trilobites + 600 distinct helleristninger).
- ZERO anonymous filler cells: every single motif has an explicit source polygon silhouette.
- ZERO Alta Museum NC-SA / non-commercial motifs (`commercial_ok == True` strictly asserted).
- Maximizes clean Non-ShareAlike (Public Domain / CC0 / CC BY) helleristninger first,
  recording exact provenance and license metadata per piece in `rights_summary.json`.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
import shapely
import shapely.affinity
from scipy import ndimage
from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

from heller_jigsaw.data.corpus import (
    _score_candidate_for_jigsaw,
    load_corpus_catalog,
    load_motif_asset,
)
from heller_jigsaw.geometry.validate import analyze_piece_fabrication
from heller_jigsaw.production.audit import measure as measure_tip

from .trilobites import Polyline, TrilobiteSpec, build_trilobite_collection


@dataclass
class PuzzleMotif:
    """Unified motif record (either parametric trilobite or archival helleristning)."""

    piece_slot: int
    kind: str                          # "trilobite" or "helleristning"
    asset_id: str
    family: str
    motif_class: str
    polygon_local: Polygon             # Centred at (0, 0), scaled to target_area_mm2 in mm
    target_area_mm2: float
    engrave_paths_local: List[Polyline] = field(default_factory=list)
    mask: Optional[np.ndarray] = None  # (256, 256) float32 normalized silhouette
    sdf: Optional[np.ndarray] = None   # (256, 256) float32 normalized SDF
    metadata: Dict[str, Any] = field(default_factory=dict)


def _is_share_alike(license_str: str) -> bool:
    u = str(license_str or "").upper()
    return "BY-SA" in u or "SHAREALIKE" in u or "-SA" in u


def _is_non_commercial(license_str: str, commercial_ok: bool) -> bool:
    u = str(license_str or "").upper()
    if not commercial_ok:
        return True
    return "NC" in u or "NON-COMMERCIAL" in u or "NONCOMMERCIAL" in u or "ALTA MUSEUM" in u


def _is_synthetic_or_vae(rec: Dict[str, Any]) -> bool:
    fields = (
        str(rec.get("source_archive", "")),
        str(rec.get("asset_id", "")),
        str(rec.get("creator", "")),
        str(rec.get("title", "")),
    )
    joined = " ".join(fields).lower()
    return any(tok in joined for tok in ("vae", "synthetic", "latent_prior"))


def _has_complete_provenance(rec: Dict[str, Any]) -> bool:
    creator = str(rec.get("creator") or "").strip()
    source_url = str(rec.get("source_url") or "").strip()
    lic = str(rec.get("license") or "").strip()
    if not creator or creator.lower() in ("ukjent", "unknown", "n/a", "none"):
        return False
    if not source_url or not lic:
        return False
    return True


def _extract_clean_helleristning_polygon(
    mask: np.ndarray,
    target_area_mm2: float,
) -> Polygon:
    """Convert a 256x256 binary helleristning mask into a centred, kerf-safe mm Polygon."""
    m = (mask > 0.5).astype(np.uint8)
    # Remove tiny pixel noise and fill any 1-px interior pinholes
    m = ndimage.binary_fill_holes(m > 0).astype(np.uint8)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        raise ValueError("Empty helleristning mask")
    cnt = max(contours, key=cv2.contourArea).reshape(-1, 2).astype(np.float64)
    # Mild circular Gaussian smoothing of the dense contour to remove pixel staircase
    if len(cnt) >= 12:
        cnt[:, 0] = ndimage.gaussian_filter1d(cnt[:, 0], sigma=1.4, mode="wrap")
        cnt[:, 1] = ndimage.gaussian_filter1d(cnt[:, 1], sigma=1.4, mode="wrap")

    poly = Polygon(cnt).buffer(0)
    if poly.geom_type != "Polygon":
        parts = [p for p in shapely.get_parts(poly) if p.geom_type == "Polygon" and p.area > 0]
        poly = max(parts, key=lambda p: p.area)
    poly = orient(Polygon(poly.exterior), 1.0)

    # Scale to target_area_mm2 and centre at centroid (0, 0)
    scale = math.sqrt(target_area_mm2 / max(float(poly.area), 1e-6))
    poly = shapely.affinity.scale(poly, xfact=scale, yfact=scale, origin=(0.0, 0.0))
    cx, cy = poly.centroid.x, poly.centroid.y
    poly = shapely.affinity.translate(poly, xoff=-cx, yoff=-cy)
    poly = poly.simplify(0.15, preserve_topology=True).buffer(0)
    if poly.geom_type != "Polygon":
        parts = [p for p in shapely.get_parts(poly) if p.geom_type == "Polygon" and p.area > 0]
        poly = max(parts, key=lambda p: p.area)
    poly = orient(Polygon(poly.exterior), 1.0)
    return poly


def _ensure_fabrication_friendly_helleristning(
    poly: Polygon,
    target_area_mm2: float,
) -> Tuple[Polygon, bool]:
    """Verify (and if needed gently thicken thin appendages) so source motif itself passes kerf/neck/tip screens.

    Keeps full recognizability (high IoU vs original mask) while preventing brittle hair-thin limbs.
    """
    orig = poly
    for widen in (0.0, 0.35, 0.70, 1.05):
        cand = orig if widen == 0.0 else orig.buffer(widen, quad_segs=10).buffer(-widen * 0.35, quad_segs=10)
        if cand.geom_type != "Polygon":
            parts = [p for p in shapely.get_parts(cand) if p.geom_type == "Polygon" and p.area > 0]
            if not parts:
                continue
            cand = max(parts, key=lambda p: p.area)
        cand = orient(Polygon(cand.exterior), 1.0)
        # Re-scale to target_area_mm2
        sc = math.sqrt(target_area_mm2 / max(float(cand.area), 1e-6))
        cand = shapely.affinity.scale(cand, xfact=sc, yfact=sc, origin=(0.0, 0.0))
        cx, cy = cand.centroid.x, cand.centroid.y
        cand = shapely.affinity.translate(cand, xoff=-cx, yoff=-cy)
        cand = cand.simplify(0.15, preserve_topology=True)
        if not cand.is_valid or cand.geom_type != "Polygon" or cand.interiors:
            continue
        fab = analyze_piece_fabrication(cand, kerf_mm=0.25, min_neck_mm=3.0)
        if not (fab["kerf_pass"] and fab["neck_pass"]):
            continue
        try:
            tip = measure_tip(cand, kerf=0.25, r=1.5)["max_tip_loss_mm"]
        except ValueError:
            continue
        if tip <= 5.6:
            return cand, True
    return orig, False


def _score_helleristning_record(rec: Dict[str, Any]) -> float:
    """Score commercial-clean helleristning for visual recognizability + fabrication quality."""
    base = _score_candidate_for_jigsaw(rec, prefer_norway=True)
    mcls = str(rec.get("motif_class", "unclassified_motif"))
    hints = rec.get("motif_hints") or []
    solidity = float(rec.get("solidity", 0.65) or 0.65)
    raw_asp = float(rec.get("aspect_ratio", 1.5) or 1.5)
    aspect = max(raw_asp, 1.0 / max(raw_asp, 1e-3))
    neck60 = float(rec.get("min_neck_mm_at_60mm", 3.5) or 3.5)

    # Strong bonus for unmistakable figurative silhouette vs generic oval/circle blobs
    fig_bonus = 0.0
    if mcls in ("bronze_age_ship", "reindeer", "elk_moose", "fish", "footprint", "anthropomorph",
                "acrobat_dancer", "stone_age_boat", "quadruped_other"):
        fig_bonus += 3.2
    elif hints:
        fig_bonus += 1.6
    else:
        fig_bonus -= 1.2

    # Penalize overly round featureless blobs (solidity > 0.80) or razor-thin fragile branching shapes (solidity < 0.42, aspect > 2.35)
    if solidity > 0.80:
        fig_bonus -= 5.0 * (solidity - 0.80)
    if solidity < 0.42:
        fig_bonus -= 6.0 * (0.42 - solidity)
    if aspect > 2.35:
        fig_bonus -= 3.0 * (aspect - 2.35)

    # Reward stout natural necks
    neck_bonus = min(1.5, max(-1.5, (neck60 - 3.5) * 0.45))

    # Prefer Non-ShareAlike cleanly when scores are close
    non_sa_bonus = 2.8 if not _is_share_alike(str(rec.get("license", ""))) else 0.0

    # Mild penalty if creator string is empty
    creator_ok = bool(str(rec.get("creator") or "").strip())
    creator_bonus = 0.4 if creator_ok else -2.0

    return base + fig_bonus + neck_bonus + non_sa_bonus + creator_bonus


def curate_750_motifs(
    catalog_path: str | Path = "hellerisninger/processed/catalog.json",
    n_total: int = 750,
    n_trilobites: int = 150,
    seed: int = 2026,
    base_area_mm2: float = 720.0,
    allow_share_alike: bool = False,
) -> Tuple[List[PuzzleMotif], Dict[str, Any]]:
    """Curate `n_total` all-figurative commercial-clean motifs (`n_trilobites` + `n_helleristninger`).

    Strictly enforces ZERO Alta Museum NC-SA / non-commercial motifs and ZERO anonymous filler cells.
    By default `allow_share_alike=False` selects strictly from Non-ShareAlike (PD / CC0 / CC BY)
    records so the entire 750-piece puzzle has zero ShareAlike encumbrances; falls back to crisp
    CC BY-SA motifs only if fewer than `n_helleristninger` fabrication-safe Non-SA motifs exist.
    """
    n_heller = n_total - n_trilobites
    if n_heller < 0 or n_trilobites < 0:
        raise ValueError(f"Invalid counts: n_total={n_total}, n_trilobites={n_trilobites}")

    rng = np.random.default_rng(seed)

    # 1. Generate parametric multi-genus trilobites (slightly larger hero fossil presence ~1.06x)
    trilo_specs: List[TrilobiteSpec] = build_trilobite_collection(
        count=n_trilobites,
        seed=seed,
        base_area_mm2=base_area_mm2 * 1.06,
    )

    # 2. Load & filter commercial-clean helleristninger from catalog.json
    records, corpus_dir = load_corpus_catalog(catalog_path)
    non_sa_cands: List[Tuple[float, Dict[str, Any]]] = []
    sa_cands: List[Tuple[float, Dict[str, Any]]] = []

    for rec in records:
        comm_ok = bool(rec.get("commercial_ok", False))
        lic = str(rec.get("license", ""))
        if _is_non_commercial(lic, comm_ok):
            continue
        if _is_synthetic_or_vae(rec):
            continue
        if not _has_complete_provenance(rec):
            continue
        if int(rec.get("connected_components", 1) or 1) != 1:
            continue
        if int(rec.get("hole_count", 0) or 0) != 0:
            continue
        if str(rec.get("jigsaw_tier", "")) not in ("A_ready", "B_thickened"):
            continue
        score = _score_helleristning_record(rec) + float(rng.uniform(0.0, 0.05))
        if _is_share_alike(lic):
            sa_cands.append((score, rec))
        else:
            non_sa_cands.append((score, rec))

    non_sa_cands.sort(key=lambda x: x[0], reverse=True)
    sa_cands.sort(key=lambda x: x[0], reverse=True)

    ordered_pool = list(non_sa_cands)
    if allow_share_alike or len(non_sa_cands) < n_heller:
        ordered_pool.extend(sa_cands)

    selected_heller: List[PuzzleMotif] = []
    used_ids: set[str] = set()
    used_hashes: set[str] = set()
    skipped_unfab = 0

    for _, rec in ordered_pool:
        if len(selected_heller) >= n_heller:
            break
        aid = str(rec.get("asset_id", ""))
        phash = str(rec.get("shape_phash", ""))
        if aid in used_ids or (phash and phash in used_hashes):
            continue
        try:
            asset = load_motif_asset(rec, corpus_dir)
            # Target area variation matching natural rock-art figure balance (~0.88x..1.10x)
            t_area = float(rng.uniform(base_area_mm2 * 0.88, base_area_mm2 * 1.10))
            poly_raw = _extract_clean_helleristning_polygon(asset.mask, t_area)
            poly_safe, ok = _ensure_fabrication_friendly_helleristning(poly_raw, t_area)
            if not ok:
                skipped_unfab += 1
                continue
        except Exception:
            skipped_unfab += 1
            continue

        lic = str(rec.get("license", ""))
        sa_req = _is_share_alike(lic)
        hints = list(rec.get("motif_hints") or [])
        mcls = str(rec.get("motif_class", "unclassified_motif"))
        # Readable display class (using panel hint if unclassified)
        display_cls = mcls if mcls != "unclassified_motif" else (f"petroglyph_{hints[0]}" if hints else "petroglyph_figure")

        meta = {
            "kind": "helleristning",
            "asset_id": aid,
            "family": str(rec.get("family", "scene_or_unclassified")),
            "motif_class": mcls,
            "display_class": display_cls,
            "motif_hints": hints,
            "title": str(rec.get("title", "")),
            "site": str(rec.get("site", "")),
            "region": str(rec.get("region", "")),
            "country": str(rec.get("country", "")),
            "period": str(rec.get("period", "")),
            "source_archive": str(rec.get("source_archive", "")),
            "source_url": str(rec.get("source_url", "")),
            "creator": str(rec.get("creator", "")),
            "license": lic,
            "license_url": str(rec.get("license_url", "")),
            "commercial_ok": True,
            "share_alike_required": sa_req,
            "is_synthetic_vae": False,
            "legal_review_status": "evidence_recorded_pending_legal_review",
            "cleared_for_sale": False,
            "jigsaw_tier": str(rec.get("jigsaw_tier", "")),
            "sha256": str(rec.get("sha256", "")),
        }
        selected_heller.append(
            PuzzleMotif(
                piece_slot=-1,
                kind="helleristning",
                asset_id=aid,
                family=meta["family"],
                motif_class=display_cls,
                polygon_local=poly_safe,
                target_area_mm2=float(poly_safe.area),
                engrave_paths_local=[],
                mask=asset.mask,
                sdf=asset.sdf,
                metadata=meta,
            )
        )
        used_ids.add(aid)
        if phash:
            used_hashes.add(phash)

    if len(selected_heller) < n_heller:
        # Second pass if Non-SA strict fabrication filter left a few slots short: include top SA candidates
        for _, rec in sa_cands:
            if len(selected_heller) >= n_heller:
                break
            aid = str(rec.get("asset_id", ""))
            if aid in used_ids:
                continue
            try:
                asset = load_motif_asset(rec, corpus_dir)
                t_area = float(rng.uniform(base_area_mm2 * 0.88, base_area_mm2 * 1.10))
                poly_raw = _extract_clean_helleristning_polygon(asset.mask, t_area)
                poly_safe, ok = _ensure_fabrication_friendly_helleristning(poly_raw, t_area)
                if not ok:
                    continue
            except Exception:
                continue
            lic = str(rec.get("license", ""))
            hints = list(rec.get("motif_hints") or [])
            mcls = str(rec.get("motif_class", "unclassified_motif"))
            display_cls = mcls if mcls != "unclassified_motif" else (f"petroglyph_{hints[0]}" if hints else "petroglyph_figure")
            meta = {
                "kind": "helleristning",
                "asset_id": aid,
                "family": str(rec.get("family", "scene_or_unclassified")),
                "motif_class": mcls,
                "display_class": display_cls,
                "motif_hints": hints,
                "title": str(rec.get("title", "")),
                "site": str(rec.get("site", "")),
                "region": str(rec.get("region", "")),
                "country": str(rec.get("country", "")),
                "period": str(rec.get("period", "")),
                "source_archive": str(rec.get("source_archive", "")),
                "source_url": str(rec.get("source_url", "")),
                "creator": str(rec.get("creator", "")),
                "license": lic,
                "license_url": str(rec.get("license_url", "")),
                "commercial_ok": True,
                "share_alike_required": _is_share_alike(lic),
                "is_synthetic_vae": False,
                "legal_review_status": "evidence_recorded_pending_legal_review",
                "cleared_for_sale": False,
                "jigsaw_tier": str(rec.get("jigsaw_tier", "")),
                "sha256": str(rec.get("sha256", "")),
            }
            selected_heller.append(
                PuzzleMotif(
                    piece_slot=-1,
                    kind="helleristning",
                    asset_id=aid,
                    family=meta["family"],
                    motif_class=display_cls,
                    polygon_local=poly_safe,
                    target_area_mm2=float(poly_safe.area),
                    engrave_paths_local=[],
                    mask=asset.mask,
                    sdf=asset.sdf,
                    metadata=meta,
                )
            )
            used_ids.add(aid)

    if len(selected_heller) != n_heller:
        raise RuntimeError(
            f"Could only curate {len(selected_heller)} fabrication-safe commercial helleristninger (needed {n_heller})"
        )

    # Convert TrilobiteSpecs to unified PuzzleMotifs
    trilo_motifs: List[PuzzleMotif] = []
    for ts in trilo_specs:
        trilo_motifs.append(
            PuzzleMotif(
                piece_slot=-1,
                kind="trilobite",
                asset_id=ts.asset_id,
                family="trilobite",
                motif_class=f"trilobite_{ts.genus.lower()}",
                polygon_local=ts.polygon,
                target_area_mm2=float(ts.area_mm2),
                engrave_paths_local=ts.engrave_paths,
                mask=ts.mask,
                sdf=ts.sdf,
                metadata=dict(ts.metadata),
            )
        )

    all_motifs: List[PuzzleMotif] = trilo_motifs + selected_heller
    for idx, m in enumerate(all_motifs):
        m.piece_slot = idx

    # Build strict rights summary payload
    lic_counts = Counter(m.metadata["license"] for m in all_motifs)
    arch_counts = Counter(m.metadata["source_archive"] for m in all_motifs)
    genus_counts = Counter(m.metadata["genus"] for m in trilo_motifs)
    heller_cls_counts = Counter(m.metadata["motif_class"] for m in selected_heller)
    alta_nc_count = sum(
        1 for m in all_motifs
        if _is_non_commercial(m.metadata["license"], bool(m.metadata.get("commercial_ok", False)))
    )
    sa_count = sum(1 for m in all_motifs if m.metadata.get("share_alike_required", False))
    synthetic_vae_count = sum(1 for m in all_motifs if m.metadata.get("is_synthetic_vae", False))
    missing_meta_count = sum(
        1 for m in all_motifs
        if not (
            str(m.metadata.get("creator") or "").strip()
            and str(m.metadata.get("license") or "").strip()
            and str(m.metadata.get("source_url") or "").strip()
        )
    )

    assert alta_nc_count == 0, f"Found {alta_nc_count} non-commercial motifs!"
    assert synthetic_vae_count == 0, f"Found {synthetic_vae_count} synthetic VAE motifs!"
    assert missing_meta_count == 0, f"Found {missing_meta_count} motifs with missing provenance metadata!"

    rights_summary: Dict[str, Any] = {
        "total_pieces": len(all_motifs),
        "trilobite_count": len(trilo_motifs),
        "helleristning_count": len(selected_heller),
        "anonymous_filler_count": 0,
        "synthetic_vae_count": synthetic_vae_count,
        "missing_metadata_count": missing_meta_count,
        "commercial_ok_count": sum(1 for m in all_motifs if m.metadata.get("commercial_ok")),
        "alta_nc_sa_count": alta_nc_count,
        "non_commercial_count": alta_nc_count,
        "share_alike_count": sa_count,
        "non_share_alike_count": len(all_motifs) - sa_count,
        "skipped_unfabricable_candidates": skipped_unfab,
        "legal_review_status": "evidence_recorded_pending_legal_review",
        "cleared_for_sale": False,
        "license_breakdown": dict(lic_counts),
        "archive_breakdown": dict(arch_counts),
        "trilobite_genus_breakdown": dict(genus_counts),
        "helleristning_class_breakdown": dict(heller_cls_counts),
        "legal_note": (
            "Preliminary commercial-source curation ONLY — NOT CLEARED FOR COMMERCIAL SALE. "
            "All 750 motifs are filtered to commercial_ok=True with 0 Alta Museum NC-SA motifs, "
            "0 synthetic VAE shapes, and 0 missing creator/license/source_url fields. "
            "Trilobites are original procedural CC0 artwork; archival rock-art records retain "
            "primary source URLs, creators, and license metadata and are honestly flagged "
            "evidence_recorded_pending_legal_review pending formal legal/attribution counsel review "
            "(including EU/NO 70-yr p.m.a. checks on 20th-century archival tracings)."
        ),
        "pieces": [
            {
                "piece_index": idx + 1,
                "kind": m.kind,
                "asset_id": m.asset_id,
                "motif_class": m.motif_class,
                "genus": m.metadata.get("genus"),
                "site": m.metadata.get("site", ""),
                "source_archive": m.metadata.get("source_archive", ""),
                "creator": m.metadata.get("creator", ""),
                "license": m.metadata.get("license", ""),
                "license_url": m.metadata.get("license_url", ""),
                "source_url": m.metadata.get("source_url", ""),
                "commercial_ok": bool(m.metadata.get("commercial_ok", False)),
                "share_alike_required": bool(m.metadata.get("share_alike_required", False)),
                "is_synthetic_vae": bool(m.metadata.get("is_synthetic_vae", False)),
                "legal_review_status": str(m.metadata.get("legal_review_status", "evidence_recorded_pending_legal_review")),
                "cleared_for_sale": bool(m.metadata.get("cleared_for_sale", False)),
            }
            for idx, m in enumerate(all_motifs)
        ],
    }

    return all_motifs, rights_summary
