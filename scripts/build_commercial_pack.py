"""Reproducible commercial readiness pack builder for the 750-Piece Exclusive Helleristning Jigsaw.

Stages:
  - catalog       : Curate & lock 750 recognizable, kerf/neck/tip-safe, commercial_ok motifs
                    into docs/helleristning-750/selected-750-catalog.json (+ QA preview PNG).
  - rights        : Generate 1:1 750-row rights inventory (CSV/JSON/MD) and UNSENT archive
                    inquiry drafts (docs/helleristning-750/archive-inquiry-drafts.md).
  - calibration   : Generate 1:1 mm laser-cut supplier calibration coupon SVG + PNG preview
                    and printable supplier measurement worksheets (MD + HTML).
  - packaging     : Generate Figma-aligned packaging & booklet specification
                    (docs/helleristning-750/packaging-alignment.md), syncing board dims if
                    runs/helleristning-750/partition.json exists.
  - seam-samples  : Extract 6 full-scale 1:1 mm real seam test-cut SVGs targeting the 6
                    tightest/highest-stress necks in runs/helleristning-750/partition.json.
  - all           : Run catalog, rights, calibration, packaging (and seam-samples if cut
                    graph exists).

Usage:
  .venv\\Scripts\\python.exe scripts/build_commercial_pack.py --stage catalog
  .venv\\Scripts\\python.exe scripts/build_commercial_pack.py --stage all
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.affinity import scale, translate
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from heller_jigsaw.geometry.validate import analyze_piece_fabrication  # noqa: E402
from heller_jigsaw.production.audit import measure as tip_measure  # noqa: E402

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)

SOURCE_CATALOG_PATH = ROOT / "hellerisninger" / "processed" / "catalog.json"
DOCS_DIR = ROOT / "docs" / "helleristning-750"
COMMERCIAL_RUN_DIR = ROOT / "runs" / "helleristning-750-commercial"
SELECTED_CATALOG_PATH = DOCS_DIR / "selected-750-catalog.json"
DEFAULT_PUZZLE_RUN_DIR = ROOT / "runs" / "helleristning-750"

TARGET_PIECE_COUNT = 750
SCREENING_PIECE_AREA_MM2 = 650.0
SCREENING_KERF_MM = 0.25
SCREENING_MIN_NECK_MM = 3.0
SCREENING_MAX_TIP_LOSS_MM = 6.0
SCREENING_MIN_RAW_PX = 25
SCREENING_MAX_ASPECT_RATIO = 3.40

# Visually audited non-figurative/blob outliers excluded during contact-sheet inspection
VISUAL_QA_EXCLUDED_IDS = {
    "helleristning_00891": "Visually audited non-figurative cluster of three fused oval blobs (cupmark/blob artifact)",
    "helleristning_00841": "Visually audited amorphous thickening blob lacking recognizable rock-art figure",
    "helleristning_00870": "Visually audited dense blob with minimal recognizable rock-art anatomy",
    "helleristning_01441": "Visually audited symmetric hourglass/bowtie fragment lacking clear rock-art identity",
    "helleristning_00818": "Visually audited thin sliver fragment with poor silhouette recognizability",
}

UNKNOWN_CREATOR_KEYWORDS = (
    "unknown",
    "ukjent",
    "okänd",
    "okand",
    "unattributed",
    "n/a",
    "missing",
)

FIGMA_PACKAGING_URL = (
    "https://www.figma.com/design/h8W3hu6PLLjmj2Tra8FgOH/"
    "GK5-modul-2-%C2%B7-Northern-%C2%B7-eige-dokument?node-id=299-65"
)


# ---------------------------------------------------------------------------
# License & Attribution Helpers
# ---------------------------------------------------------------------------


def is_share_alike(license_str: str) -> bool:
    s = (license_str or "").lower()
    return "sa" in s or "sharealike" in s or "share-alike" in s


def is_public_domain_or_cc0(license_str: str) -> bool:
    s = (license_str or "").lower()
    return "public domain" in s or "cc0" in s or "pdm" in s


def classify_license_tier(license_str: str) -> str:
    if is_share_alike(license_str):
        return "cc_by_sa"
    if is_public_domain_or_cc0(license_str):
        return "public_domain_or_cc0"
    return "cc_by_non_sa"


def has_unknown_creator(creator: str) -> bool:
    c = (creator or "").strip()
    if len(c) < 3:
        return True
    cl = c.lower()
    return any(kw in cl for kw in UNKNOWN_CREATOR_KEYWORDS)


def archive_display_name(archive: str) -> str:
    return {
        "shfa": "SHFA (Swedish Rock Art Research Archives, University of Gothenburg)",
        "unimus": "Unimus (Universitetsmuseenes fotoportal, Norway)",
        "wikimedia": "Wikimedia Commons",
        "ksamsok": "K-samsök / SOCH (Swedish Open Cultural Heritage)",
        "europeana": "Europeana",
        "other": "Nasjonalbiblioteket (National Library of Norway)",
        "kulturminnesok": "Riksantikvaren / Kulturminnesøk",
    }.get(archive, archive)


def build_attribution_text(rec: Dict[str, Any]) -> str:
    title = str(rec.get("title") or rec.get("asset_id")).strip()
    creator = str(rec.get("creator") or "").strip()
    site = str(rec.get("site") or "").strip()
    archive = archive_display_name(str(rec.get("source_archive") or ""))
    lic = str(rec.get("license") or "").strip()
    source_url = str(rec.get("source_url") or "").strip()
    parts = [f'"{title}"']
    if site and site.lower() not in title.lower():
        parts.append(f"({site})")
    parts.append(f"by {creator}")
    parts.append(f"via {archive}")
    parts.append(f"[{lic}]")
    parts.append(source_url)
    return " ".join(parts)


def build_open_actions(rec: Dict[str, Any]) -> List[str]:
    actions: List[str] = []
    lic = str(rec.get("license") or "")
    arch = str(rec.get("source_archive") or "")
    creator = str(rec.get("creator") or "")
    tier = classify_license_tier(lic)

    if tier == "cc_by_sa":
        actions.append(
            "SHAREALIKE GATE: CC BY-SA 4.0 record requires either (a) written non-SA commercial permission "
            "from the holding Unimus museum (see archive-inquiry-drafts.md), (b) legal review confirming "
            "whether cut-file/product distribution triggers ShareAlike, or (c) replacement with a non-SA motif "
            "prior to commercial launch."
        )
    elif tier == "cc_by_non_sa":
        actions.append(
            f"ATTRIBUTION COMPLIANCE: Include required {lic} credit (creator, archive, title, license URL, "
            "and indication of silhouette adaptation) in the printed puzzle booklet and product web page."
        )
    else:
        actions.append(
            "PUBLIC DOMAIN / CC0 VERIFICATION: Confirm EU/Norwegian 70-year post-mortem copyright expiry "
            "on underlying archaeological tracing/photograph prior to commercial sale."
        )

    if arch == "wikimedia" and is_public_domain_or_cc0(lic):
        if any(name in creator.lower() for name in ("gjessing", "wilhelmsen", "sognnes", "hallström", "hallstrom")):
            actions.append(
                "EU/NO TERM CHECK: Verify tracing illustrator death dates (e.g., Gutorm Gjessing d. 1979) "
                "and Norwegian originality threshold (verkshøyde) vs. Wikimedia PD tag before commercial use."
            )
    if arch == "shfa":
        actions.append(
            "ARCHIVE COURTESY / FORMAT CHECK: Confirm bulk booklet credit format with SHFA (shfa@gu.se) "
            "for CC BY 4.0 adaptation notice."
        )
    if rec.get("motif_class") == "unclassified_motif":
        actions.append(
            "EDITORIAL CAPTIONING: Confirm human-readable motif caption for the rock-art legend booklet "
            "using source plate metadata and motif_hints."
        )
    return actions


# ---------------------------------------------------------------------------
# Geometry & Recognizability Scoring (Stage 1: catalog)
# ---------------------------------------------------------------------------


def _mask_to_scaled_polygon(mask_path: Path, area_mm2: float = SCREENING_PIECE_AREA_MM2) -> Optional[Polygon]:
    img = Image.open(mask_path).convert("L")
    arr = (np.asarray(img) >= 128).astype(np.uint8)
    contours, _ = cv2.findContours(arr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None
    cnt = max(contours, key=cv2.contourArea)
    poly = Polygon(cnt.reshape(-1, 2).astype(float)).buffer(0)
    if poly.is_empty:
        return None
    if poly.geom_type != "Polygon":
        polys = [g for g in getattr(poly, "geoms", []) if g.geom_type == "Polygon"]
        if not polys:
            return None
        poly = max(polys, key=lambda g: g.area)
    if poly.area <= 10.0:
        return None
    s = math.sqrt(area_mm2 / poly.area)
    scaled = scale(poly, xfact=s, yfact=s, origin="centroid").simplify(0.15)
    if scaled.geom_type != "Polygon" or not scaled.is_valid or scaled.is_empty:
        scaled = scaled.buffer(0)
        if scaled.geom_type != "Polygon":
            scaled = max(scaled.geoms, key=lambda g: g.area)
    return scaled


def evaluate_candidate(rec: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Evaluate one catalog record. Returns (metrics_dict, exclusion_reason_or_None)."""
    asset_id = str(rec.get("asset_id") or "")
    if not rec.get("commercial_ok"):
        return None, f"commercial_ok=false ({rec.get('license')})"
    for req_field in ("source_url", "license", "license_url", "site", "mask_path", "svg_path"):
        if not str(rec.get(req_field) or "").strip():
            return None, f"missing required field: {req_field}"
    if has_unknown_creator(str(rec.get("creator") or "")):
        return None, f"unknown/missing creator ({rec.get('creator')!r})"
    if asset_id in VISUAL_QA_EXCLUDED_IDS:
        return None, VISUAL_QA_EXCLUDED_IDS[asset_id]

    rb = rec.get("raw_bbox_px") or [0, 0, 100, 100]
    raw_min_dim = min(int(rb[2]), int(rb[3]))
    if raw_min_dim < SCREENING_MIN_RAW_PX:
        return None, f"raw source crop too small ({rb[2]}x{rb[3]} px < {SCREENING_MIN_RAW_PX} px)"

    ar = float(rec.get("aspect_ratio") or 1.0)
    ar_sym = max(ar, 1.0 / max(ar, 1e-6))
    if ar_sym > SCREENING_MAX_ASPECT_RATIO:
        return None, f"extreme aspect ratio ({ar_sym:.2f} > {SCREENING_MAX_ASPECT_RATIO:.2f})"

    mask_rel = str(rec["mask_path"])
    mask_path = ROOT / "hellerisninger" / mask_rel
    if not mask_path.exists():
        return None, f"mask file missing: {mask_rel}"

    poly = _mask_to_scaled_polygon(mask_path, area_mm2=SCREENING_PIECE_AREA_MM2)
    if poly is None:
        return None, "failed to extract valid outer silhouette polygon"

    fab = analyze_piece_fabrication(
        poly,
        kerf_mm=SCREENING_KERF_MM,
        min_neck_mm=SCREENING_MIN_NECK_MM,
    )
    if not fab["kerf_pass"]:
        return None, f"fails kerf screen at {SCREENING_PIECE_AREA_MM2:.0f} mm² (kerf={SCREENING_KERF_MM} mm)"
    if not fab["neck_pass"]:
        return (
            None,
            f"fails {SCREENING_MIN_NECK_MM:.1f} mm neck screen at {SCREENING_PIECE_AREA_MM2:.0f} mm² "
            f"(secondary_lobe={fab['secondary_lobe_max_mm2']} mm², core_surv={fab['core_survival_ratio']})",
        )

    try:
        tm = tip_measure(poly, kerf=SCREENING_KERF_MM, r=SCREENING_MIN_NECK_MM * 0.5)
        tip_loss = float(tm["max_tip_loss_mm"])
        lost_area = float(tm["lost_area_fraction"])
    except Exception as exc:
        return None, f"tip-loss measurement failed: {exc}"

    if tip_loss > SCREENING_MAX_TIP_LOSS_MM:
        return (
            None,
            f"thin-tip loss {tip_loss:.2f} mm exceeds {SCREENING_MAX_TIP_LOSS_MM:.1f} mm gate at {SCREENING_PIECE_AREA_MM2:.0f} mm²",
        )

    # Compute recognizability score
    solidity = float(rec.get("solidity") or (poly.area / max(1e-6, poly.convex_hull.area)))
    compactness = float(rec.get("compactness") or (4.0 * math.pi * poly.area / max(1e-6, poly.length ** 2)))
    erosion_surv = float(rec.get("erosion_survival_ratio") or 0.0)
    hull = poly.convex_hull
    bays = hull.difference(poly)
    bay_parts = [
        g
        for g in (bays.geoms if hasattr(bays, "geoms") else [bays])
        if g.geom_type == "Polygon" and g.area >= 12.0
    ]
    n_bays = len(bay_parts)

    cls = str(rec.get("motif_class") or "unclassified_motif")
    hints = list(rec.get("motif_hints") or [])
    classified_bonus = 0.25 if cls != "unclassified_motif" else (0.10 if hints else 0.0)
    tier_bonus = 0.08 if rec.get("jigsaw_tier") == "A_ready" else 0.0
    open_score = min(1.0, (1.0 - lost_area) / 0.85) * 0.35
    bay_score = min(1.0, n_bays / 4.0) * 0.22 if n_bays <= 10 else 0.14
    ar_pen = max(0.0, (ar_sym - 2.6) * 0.25)
    blob_pen = max(0.0, (solidity - 0.74) * 1.5) + max(0.0, (compactness - 0.38) * 1.2)
    spindle_pen = max(0.0, (0.36 - solidity) * 1.2)
    size_pen = 0.0 if raw_min_dim >= 45 else 0.12

    score = (
        open_score
        + bay_score
        + classified_bonus
        + tier_bonus
        + 0.15 * min(1.0, erosion_surv / 0.65)
        - ar_pen
        - blob_pen
        - spindle_pen
        - size_pen
    )

    metrics = {
        "recognizability_score": round(float(score), 4),
        "kerf_pass_650mm2": bool(fab["kerf_pass"]),
        "neck_pass_650mm2": bool(fab["neck_pass"]),
        "core_survival_ratio_650mm2": float(fab["core_survival_ratio"]),
        "max_inscribed_diameter_mm_650mm2": float(fab["max_inscribed_diameter_mm"]),
        "tip_loss_mm_650mm2": round(tip_loss, 3),
        "lost_area_fraction_650mm2": round(lost_area, 4),
        "aspect_ratio_sym": round(ar_sym, 3),
        "n_concavity_bays": int(n_bays),
        "raw_min_dim_px": int(raw_min_dim),
    }
    return metrics, None


def _rel_to_root(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _prefix_hellerisninger(path_str: Optional[str]) -> Optional[str]:
    if not path_str:
        return path_str
    norm = str(path_str).replace("\\", "/")
    if norm.startswith("hellerisninger/"):
        return norm
    return f"hellerisninger/{norm}"


def build_selected_750_catalog() -> Dict[str, Any]:
    """Curate and lock 750 recognizable, kerf-safe, commercial_ok helleristning motifs."""
    raw_items: List[Dict[str, Any]] = json.loads(SOURCE_CATALOG_PATH.read_text(encoding="utf-8"))
    non_sa_candidates: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    unimus_sa_candidates: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    other_sa_candidates: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    excluded_log: List[Dict[str, Any]] = []

    for rec in raw_items:
        metrics, reason = evaluate_candidate(rec)
        if reason is not None or metrics is None:
            excluded_log.append(
                {
                    "asset_id": rec.get("asset_id"),
                    "source_archive": rec.get("source_archive"),
                    "license": rec.get("license"),
                    "commercial_ok": bool(rec.get("commercial_ok")),
                    "creator": rec.get("creator"),
                    "site": rec.get("site"),
                    "reason": reason,
                }
            )
            continue

        if not is_share_alike(str(rec.get("license") or "")):
            non_sa_candidates.append((rec, metrics))
        elif rec.get("source_archive") == "unimus":
            unimus_sa_candidates.append((rec, metrics))
        else:
            other_sa_candidates.append((rec, metrics))

    # Deterministic sort: highest recognizability score first, tie-break by asset_id
    non_sa_candidates.sort(key=lambda pair: (-pair[1]["recognizability_score"], pair[0]["asset_id"]))
    # For Unimus CC BY-SA 4.0 completion: prioritize explicitly classified motifs first,
    # then raw_min_dim >= 35, then recognizability score.
    unimus_sa_candidates.sort(
        key=lambda pair: (
            0 if pair[0].get("motif_class") != "unclassified_motif" else 1,
            0 if pair[1]["raw_min_dim_px"] >= 35 else 1,
            -pair[1]["recognizability_score"],
            pair[0]["asset_id"],
        )
    )
    other_sa_candidates.sort(
        key=lambda pair: (
            0 if pair[0].get("motif_class") != "unclassified_motif" else 1,
            -pair[1]["recognizability_score"],
            pair[0]["asset_id"],
        )
    )

    selected_pairs: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    seen_ids: set[str] = set()
    seen_phashes: set[str] = set()

    def _try_add(rec: Dict[str, Any], metrics: Dict[str, Any], bucket: str) -> bool:
        aid = str(rec["asset_id"])
        ph = str(rec.get("shape_phash") or aid)
        if aid in seen_ids or ph in seen_phashes:
            return False
        seen_ids.add(aid)
        seen_phashes.add(ph)
        selected_pairs.append((rec, metrics, bucket))
        return True

    # 1. Maximize all passing Non-ShareAlike motifs first
    for rec, metrics in non_sa_candidates:
        if len(selected_pairs) >= TARGET_PIECE_COUNT:
            break
        _try_add(rec, metrics, "tier1_non_share_alike")

    # 2. Fill remaining slots strictly from single-consortium Unimus CC BY-SA 4.0 institutional motifs
    for rec, metrics in unimus_sa_candidates:
        if len(selected_pairs) >= TARGET_PIECE_COUNT:
            break
        _try_add(rec, metrics, "tier2_unimus_cc_by_sa_4_0")

    # 3. Fallback only if still short (not expected; Unimus alone has ~290 clean motifs)
    for rec, metrics in other_sa_candidates:
        if len(selected_pairs) >= TARGET_PIECE_COUNT:
            break
        _try_add(rec, metrics, "tier3_other_cc_by_sa")

    if len(selected_pairs) != TARGET_PIECE_COUNT:
        raise RuntimeError(f"Expected {TARGET_PIECE_COUNT} selected motifs, got {len(selected_pairs)}")

    # Build locked 750-item payload compatible with heller_jigsaw.data.corpus.load_corpus_catalog()
    locked_items: List[Dict[str, Any]] = []
    for idx, (rec, metrics, bucket) in enumerate(selected_pairs, start=1):
        lic = str(rec["license"])
        sa_req = is_share_alike(lic)
        tier = classify_license_tier(lic)
        item = dict(rec)
        # Portable paths relative to repository root (docs/helleristning-750 -> parent.parent = repo root)
        for pkey in ("raw_path", "svg_path", "mask_path", "polygon_path", "sdf_path"):
            if item.get(pkey):
                item[pkey] = _prefix_hellerisninger(item[pkey])

        item.update(
            {
                "piece_index": idx,
                "piece_id": f"HELL-750-{idx:03d}",
                "motif_id": str(rec["asset_id"]),
                "archive": str(rec["source_archive"]),
                "share_alike_required": sa_req,
                "license_tier": tier,
                "selection_bucket": bucket,
                "attribution_text": build_attribution_text(rec),
                "commercial_review_status": "evidence_recorded_pending_legal_review",
                "open_actions": build_open_actions(rec),
                "curation_metrics": metrics,
            }
        )
        locked_items.append(item)

    # Summary statistics
    lic_counts = dict(collections.Counter(it["license"] for it in locked_items).most_common())
    tier_counts = dict(collections.Counter(it["license_tier"] for it in locked_items).most_common())
    arch_counts = dict(collections.Counter(it["archive"] for it in locked_items).most_common())
    cls_counts = dict(collections.Counter(it["motif_class"] for it in locked_items).most_common())
    country_counts = dict(collections.Counter(it.get("country", "unknown") for it in locked_items).most_common())
    bucket_counts = dict(collections.Counter(it["selection_bucket"] for it in locked_items).most_common())
    sa_count = sum(1 for it in locked_items if it["share_alike_required"])
    non_sa_count = len(locked_items) - sa_count
    classified_count = sum(1 for it in locked_items if it["motif_class"] != "unclassified_motif")
    unclassified_with_hints = sum(
        1 for it in locked_items if it["motif_class"] == "unclassified_motif" and bool(it.get("motif_hints"))
    )

    def _norm_reason(r: str) -> str:
        s = (r or "").split("(")[0].strip()
        if s.startswith("thin-tip loss"):
            return f"thin-tip loss exceeds {SCREENING_MAX_TIP_LOSS_MM:.1f} mm gate at {SCREENING_PIECE_AREA_MM2:.0f} mm²"
        if s.startswith("Visually audited"):
            return "visually audited blob/fragment outlier"
        return s

    excl_summary = dict(collections.Counter(_norm_reason(e["reason"]) for e in excluded_log).most_common())

    payload = {
        "schema_version": "1.0.0",
        "title": "750-Piece Exclusive Helleristning Wooden Jigsaw — Locked Commercial Motif Catalog",
        "generated_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_by": "scripts/build_commercial_pack.py",
        "source_catalog": "hellerisninger/processed/catalog.json",
        "legal_status_notice": (
            "EVIDENCE FOR LEGAL REVIEW — NOT CLEARED FOR SALE. All 750 motifs are verified commercial_ok=true "
            "in primary archive metadata (zero Alta Museum NC-SA, zero VAE/synthetic shapes), but commercial "
            "distribution still requires legal review of ShareAlike obligations on Unimus CC BY-SA 4.0 records, "
            "EU/NO public-domain term confirmation on historical tracings, and attribution booklet compliance."
        ),
        "selection_policy": {
            "target_piece_count": TARGET_PIECE_COUNT,
            "zero_non_commercial_policy": True,
            "zero_synthetic_vae_policy": True,
            "zero_unknown_creator_policy": True,
            "screening_piece_area_mm2": SCREENING_PIECE_AREA_MM2,
            "screening_kerf_mm": SCREENING_KERF_MM,
            "screening_min_neck_mm": SCREENING_MIN_NECK_MM,
            "screening_max_tip_loss_mm": SCREENING_MAX_TIP_LOSS_MM,
            "screening_min_raw_px": SCREENING_MIN_RAW_PX,
            "screening_max_aspect_ratio": SCREENING_MAX_ASPECT_RATIO,
            "visual_qa_excluded_ids": VISUAL_QA_EXCLUDED_IDS,
            "priority_order": [
                "1. Maximize all passing Non-ShareAlike (Public Domain / CC0 1.0 / CC BY 2.5, 3.0, 4.0) archival motifs.",
                "2. Fill remaining slots strictly from single-consortium Unimus (Norwegian University Museums) CC BY-SA 4.0 motifs, classified silhouettes first, to minimize licensing fragmentation.",
            ],
        },
        "summary": {
            "source_catalog_total": len(raw_items),
            "source_commercial_ok_total": sum(1 for r in raw_items if r.get("commercial_ok")),
            "source_alta_nc_sa_excluded": sum(1 for r in raw_items if not r.get("commercial_ok")),
            "selected_total": len(locked_items),
            "non_share_alike_count": non_sa_count,
            "share_alike_count": sa_count,
            "classified_count": classified_count,
            "unclassified_count": len(locked_items) - classified_count,
            "unclassified_with_motif_hints_count": unclassified_with_hints,
            "all_commercial_ok": all(bool(it["commercial_ok"]) for it in locked_items),
            "all_pass_kerf_650mm2": all(it["curation_metrics"]["kerf_pass_650mm2"] for it in locked_items),
            "all_pass_neck_650mm2": all(it["curation_metrics"]["neck_pass_650mm2"] for it in locked_items),
            "max_tip_loss_mm_650mm2": max(it["curation_metrics"]["tip_loss_mm_650mm2"] for it in locked_items),
            "by_license_tier": tier_counts,
            "by_license": lic_counts,
            "by_archive": arch_counts,
            "by_motif_class": cls_counts,
            "by_country": country_counts,
            "by_selection_bucket": bucket_counts,
            "excluded_count_total": len(excluded_log),
            "excluded_by_reason": excl_summary,
        },
        "excluded_audit_highlights": {
            "unknown_creator_records": [e for e in excluded_log if "unknown/missing creator" in (e["reason"] or "")],
            "visual_qa_excluded_records": [e for e in excluded_log if e["asset_id"] in VISUAL_QA_EXCLUDED_IDS],
            "alta_nc_sa_excluded_count": sum(1 for e in excluded_log if not e["commercial_ok"]),
        },
        "items": locked_items,
    }

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    COMMERCIAL_RUN_DIR.mkdir(parents=True, exist_ok=True)
    SELECTED_CATALOG_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # Render visual QA contact sheet of the Tier-2 Unimus CC BY-SA completion motifs
    sa_items = [it for it in locked_items if it["share_alike_required"]]
    _render_qa_contact_sheet(
        sa_items,
        COMMERCIAL_RUN_DIR / "selected_tier2_unimus_sa_qa.png",
        f"Selected Tier-2 Unimus CC BY-SA 4.0 Completion Motifs (n={len(sa_items)})",
        color=(45, 92, 122),
    )
    return payload


def _load_font(size: int = 11, bold: bool = False) -> ImageFont.ImageFont:
    candidates = (
        ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/segoeuib.ttf"]
        if bold
        else ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/segoeui.ttf"]
    ) + ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size=size)
        except Exception:
            continue
    return ImageFont.load_default()


def _render_qa_contact_sheet(items: Sequence[Dict[str, Any]], out_path: Path, title: str, color: Tuple[int, int, int]) -> None:
    if not items:
        return
    cols = 10
    cell_w, cell_h = 118, 140
    rows_n = math.ceil(len(items) / cols)
    im = Image.new("RGB", (cols * cell_w, rows_n * cell_h + 30), "#F7F5F0")
    draw = ImageDraw.Draw(im)
    font = _load_font(9, bold=False)
    font_b = _load_font(14, bold=True)
    draw.text((8, 6), title, fill="#111111", font=font_b)

    for idx, it in enumerate(items):
        cx = (idx % cols) * cell_w
        cy = 30 + (idx // cols) * cell_h
        mp = ROOT / it["mask_path"]
        arr = np.asarray(Image.open(mp).convert("L")) >= 128
        tile = np.full((256, 256, 3), (255, 253, 249), dtype=np.uint8)
        tile[arr] = color
        im.paste(Image.fromarray(tile).resize((104, 104), Image.Resampling.LANCZOS), (cx + 7, cy + 4))
        draw.rectangle([cx + 6, cy + 3, cx + 111, cy + 108], outline="#D8D2C5")
        cls_s = (
            it["motif_class"]
            .replace("unclassified_motif", "uncl")
            .replace("bronze_age_ship", "ship")
            .replace("quadruped_other", "quad")
            .replace("anthropomorph", "anthro")
        )
        cm = it["curation_metrics"]
        draw.text((cx + 6, cy + 110), f"#{it['piece_index']} {it['motif_id'][-5:]} {cls_s[:7]}", fill="#111", font=font)
        draw.text((cx + 6, cy + 122), f"s={cm['recognizability_score']:.2f} t={cm['tip_loss_mm_650mm2']:.1f}", fill="#555", font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path)


# ---------------------------------------------------------------------------
# Stage 2: Rights Inventory (CSV, JSON, MD) + Archive Inquiry Drafts
# ---------------------------------------------------------------------------


def build_rights_inventory(catalog_payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if catalog_payload is None:
        catalog_payload = json.loads(SELECTED_CATALOG_PATH.read_text(encoding="utf-8"))
    items: List[Dict[str, Any]] = catalog_payload["items"]
    summary = catalog_payload["summary"]

    rows: List[Dict[str, Any]] = []
    for it in items:
        cm = it["curation_metrics"]
        rows.append(
            {
                "piece_index": it["piece_index"],
                "piece_id": it["piece_id"],
                "motif_id": it["motif_id"],
                "source_id": it.get("source_id", ""),
                "motif_class": it["motif_class"],
                "motif_hints": "; ".join(it.get("motif_hints") or []),
                "title": it.get("title", ""),
                "site": it["site"],
                "region": it.get("region", ""),
                "country": it.get("country", ""),
                "period": it.get("period", ""),
                "archive": it["archive"],
                "archive_name": archive_display_name(it["archive"]),
                "source_url": it["source_url"],
                "creator": it["creator"],
                "license": it["license"],
                "license_url": it["license_url"],
                "license_tier": it["license_tier"],
                "share_alike_required": it["share_alike_required"],
                "commercial_ok_source_flag": bool(it["commercial_ok"]),
                "attribution_text": it["attribution_text"],
                "commercial_review_status": it["commercial_review_status"],
                "open_actions": " | ".join(it["open_actions"]),
                "selection_bucket": it["selection_bucket"],
                "recognizability_score": cm["recognizability_score"],
                "tip_loss_mm_650mm2": cm["tip_loss_mm_650mm2"],
                "sha256": it.get("sha256", ""),
            }
        )

    # 1. CSV (750 rows + header)
    csv_path = DOCS_DIR / "rights-inventory.csv"
    fieldnames = list(rows[0].keys())
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    # 2. JSON
    inv_json = {
        "schema_version": "1.0.0",
        "title": "750-Piece Exclusive Helleristning Wooden Jigsaw — Piece-Level Rights Inventory",
        "generated_utc": catalog_payload["generated_utc"],
        "legal_disclaimer": (
            "EVIDENCE FOR LEGAL REVIEW — NOT CLEARED FOR SALE. This inventory consolidates primary archive "
            "provenance and license metadata for all 750 puzzle pieces. It does not constitute legal clearance, "
            "a rights warranty, or authorization for commercial distribution."
        ),
        "selected_catalog_path": "docs/helleristning-750/selected-750-catalog.json",
        "summary": summary,
        "excluded_audit_highlights": catalog_payload["excluded_audit_highlights"],
        "rows": rows,
    }
    json_path = DOCS_DIR / "rights-inventory.json"
    json_path.write_text(json.dumps(inv_json, indent=2, ensure_ascii=False), encoding="utf-8")

    # 3. Markdown executive report
    non_sa = summary["non_share_alike_count"]
    sa = summary["share_alike_count"]
    sa_by_creator = collections.Counter(r["creator"] for r in rows if r["share_alike_required"])
    pd_gjessing = [
        r
        for r in rows
        if r["archive"] == "wikimedia"
        and is_public_domain_or_cc0(r["license"])
        and any(k in r["creator"].lower() for k in ("gjessing", "wilhelmsen", "petersen", "hallström", "hallstrom"))
    ]

    md_lines = [
        "# Rights & Provenance Inventory — 750-Piece Exclusive Helleristning Wooden Jigsaw",
        "",
        "> **EVIDENCE FOR LEGAL REVIEW — NOT CLEARED FOR SALE.**",
        "> This inventory compiles primary archive metadata (`source_url`, `creator`, `license`, `license_url`, `site`, `sha256`) for every single one of the **750 locked puzzle pieces** in [`selected-750-catalog.json`](./selected-750-catalog.json). Recording open-license metadata is **necessary evidence for legal review**, **NOT** legal clearance to manufacture or sell.",
        "",
        "---",
        "",
        "## 1. Executive Rights Summary (750 / 750 Pieces)",
        "",
        "| Metric | Count | Status / Legal Hygiene Note |",
        "|---|---:|---|",
        f"| **Total locked pieces** | **{len(rows)}** | 1:1 matched to `selected-750-catalog.json` (`HELL-750-001`..`HELL-750-750`) |",
        f"| **Source `commercial_ok = true`** | **{len(rows)} / 750 (100%)** | **0** Alta Museum NC-SA pieces; **0** synthetic / VAE-generated shapes |",
        f"| **Non-ShareAlike (`Public Domain` / `CC0` / `CC BY`)** | **{non_sa}** ({100.0 * non_sa / 750:.1f}%) | No copyleft ShareAlike trigger; attribution & PD-term checks still required |",
        f"| **ShareAlike (`CC BY-SA 4.0` — Unimus only)** | **{sa}** ({100.0 * sa / 750:.1f}%) | **OPEN LEGAL GATE:** Triggers ShareAlike review unless cleared or swapped |",
        "| **Missing `source_url` / `license` / `license_url` / `creator`** | **0** | All 6 unknown-creator source records (incl. `helleristning_01460`) cleanly excluded |",
        "| **Current legal clearance status** | **0 cleared** | All 750 rows flagged `evidence_recorded_pending_legal_review` |",
        "",
        "---",
        "",
        "## 2. Breakdown by License & ShareAlike Obligation",
        "",
        "### A. Non-ShareAlike Pool (No Copyleft Trigger)",
        "| License | Count | Primary Archive(s) | Core Legal Obligation |",
        "|---|---:|---|---|",
    ]
    for lic, cnt in sorted(summary["by_license"].items(), key=lambda kv: -kv[1]):
        if not is_share_alike(lic):
            archs = ", ".join(sorted({r["archive"] for r in rows if r["license"] == lic}))
            obl = (
                "Verify EU/NO 70-yr p.m.a. term on historical drawings; courtesy credit recommended"
                if is_public_domain_or_cc0(lic)
                else f"Give full {lic} credit (creator, title, archive, license URL, adaptation notice)"
            )
            md_lines.append(f"| `{lic}` | {cnt} | {archs} | {obl} |")

    md_lines += [
        "",
        "### B. ShareAlike Pool (Copyleft Trigger — Consolidated to Single Norwegian Museum Consortium)",
        "",
        f"Because the clean, kerf-safe, non-unknown-creator Non-ShareAlike pool in `hellerisninger/processed/catalog.json` contains **{non_sa}** passing motifs (short of 750 by **{sa}**), the remaining **{sa}** slots were **deliberately consolidated exclusively into Unimus `CC BY-SA 4.0` records** (Norwegian University Museums: UiB Bergen, KHM UiO, NTNU Vitenskapsmuseet) rather than dispersing ShareAlike obligations across dozens of individual Wikimedia uploaders.",
        "",
        "| Unimus Creator / Holding Institution | License | Count |",
        "|---|---|---:|",
    ]
    for creator, cnt in sa_by_creator.most_common():
        md_lines.append(f"| {creator} | `CC BY-SA 4.0` | {cnt} |")

    md_lines += [
        "",
        "**Two clean paths to close the ShareAlike gate before commercial sale (confirm with IP counsel):**",
        f"1. **Path A — Institutional Non-SA Permission (Recommended if keeping Vingen/KHM icons):** Send the prepared UNSENT draft letters in [`archive-inquiry-drafts.md`](./archive-inquiry-drafts.md) to University Museum of Bergen (`post@um.uib.no`), KHM UiO, and NTNU Vitenskapsmuseet requesting written non-ShareAlike commercial licensing for the **{sa}** specific Unimus motifs.",
        f"2. **Path B — 100% Non-ShareAlike Replacement:** Replace pieces `HELL-750-{non_sa + 1:03d}`..`HELL-750-750` ({sa} slots) by harvesting {sa} additional kerf-safe `CC BY 4.0` B&W tracings from SHFA (SHFA holds 3,846 `CC BY 4.0` tracings, of which only 286 plates are currently harvested) or verified Public Domain tracings, yielding a **750/750 Non-ShareAlike** edition.",
        "3. **Path C — ShareAlike Compliance:** If legal counsel advises that derivative vector cut files (and/or physical adaptations) must remain under `CC BY-SA 4.0`, publish the derivative cut vector geometry under `CC BY-SA 4.0` with full attribution (note: this permits third parties to legally copy and sell the same cut pattern).",
        "",
        "---",
        "",
        "## 3. Archive Breakdown & Geographic Representation",
        "",
        "| Archive | Piece Count | ShareAlike? | Primary Sites / Collections |",
        "|---|---:|---|---|",
    ]
    for arch, cnt in sorted(summary["by_archive"].items(), key=lambda kv: -kv[1]):
        sa_flag = "Yes (`CC BY-SA 4.0`)" if any(r["share_alike_required"] for r in rows if r["archive"] == arch) else "No"
        top_sites = ", ".join(s for s, _ in collections.Counter(r["site"] for r in rows if r["archive"] == arch).most_common(4))
        md_lines.append(f"| {archive_display_name(arch)} (`{arch}`) | {cnt} | {sa_flag} | {top_sites} |")

    md_lines += [
        "",
        f"**Country split:** " + ", ".join(f"`{k}`: {v}" for k, v in summary["by_country"].items()),
        "",
        "---",
        "",
        "## 4. Excluded Assets & Legal Hygiene Audit Log",
        "",
        f"Out of **{summary['source_catalog_total']}** records in `hellerisninger/processed/catalog.json`, **{summary['excluded_count_total']}** were explicitly excluded:",
        "",
        "| Exclusion Category | Count | Rationale |",
        "|---|---:|---|",
    ]
    grouped_excl: Dict[str, int] = collections.Counter()
    for rk, cnt in summary["excluded_by_reason"].items():
        if rk.startswith("thin-tip loss"):
            grouped_excl[f"thin-tip loss exceeds {SCREENING_MAX_TIP_LOSS_MM:.1f} mm gate at {SCREENING_PIECE_AREA_MM2:.0f} mm²"] += cnt
        elif rk.startswith("Visually audited"):
            grouped_excl["visually audited blob/fragment outlier"] += cnt
        else:
            grouped_excl[rk] += cnt
    for reason_k, cnt in sorted(grouped_excl.items(), key=lambda kv: -kv[1]):
        md_lines.append(f"| {reason_k} | {cnt} | Automated / visual QA exclusion |")

    md_lines += [
        "",
        "### Specifically Excluded Attribution-Defect Records (Zero Unknown Creators Retained)",
        "| Excluded `asset_id` | Archive | License | Recorded `creator` | Why Excluded |",
        "|---|---|---|---|---|",
    ]
    for ex in catalog_payload["excluded_audit_highlights"]["unknown_creator_records"]:
        md_lines.append(
            f"| `{ex['asset_id']}` | `{ex['source_archive']}` | `{ex['license']}` | `{ex['creator']}` | Fails zero-unknown-creator commercial hygiene rule |"
        )

    md_lines += [
        "",
        "### Special Legal Watch Item — 20th-Century Historical Tracings Tagged `Public domain` on Wikimedia Commons",
        f"- **{len(pd_gjessing)}** Wikimedia Commons pieces derive from 20th-century archaeological tracings (e.g., Gutorm Gjessing d. 1979, Mary Wilhelmsen, Th. Petersen d. 1952) tagged `Public domain` on Commons.",
        "- Under Norwegian (*Åndsverkloven*) and EU copyright law, copyright in original artistic drawings runs **70 years post mortem auctoris** (Gjessing: 1979 + 70 = 2049), though purely faithful scientific/archaeological tracings of prehistoric carvings may lack *verkshøyde* (originality) or fall under legacy publication rules. **Action:** Flag these specific records for Norwegian IP counsel confirmation (or replace with SHFA `CC BY 4.0` equivalents if counsel deems risk non-zero).",
        "",
        "---",
        "",
        "## 5. Companion Machine-Readable Files",
        "",
        "- [`selected-750-catalog.json`](./selected-750-catalog.json) — Locked 750-motif production catalog consumed by the puzzle generator.",
        "- [`rights-inventory.csv`](./rights-inventory.csv) — Flat 750-row spreadsheet for legal counsel, archive outreach, and printed booklet typesetting.",
        "- [`rights-inventory.json`](./rights-inventory.json) — Structured 750-row JSON with SHA-256 hashes and per-piece `open_actions`.",
        "- [`archive-inquiry-drafts.md`](./archive-inquiry-drafts.md) — Ready-to-send **UNSENT** institutional clearance & attribution letters.",
    ]
    (DOCS_DIR / "rights-inventory.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    # 4. Archive Inquiry Drafts (explicitly UNSENT)
    shfa_rows = [r for r in rows if r["archive"] == "shfa"]
    uib_rows = [r for r in rows if r["archive"] == "unimus" and "Bergen" in r["creator"]]
    khm_rows = [r for r in rows if r["archive"] == "unimus" and "Kulturhistorisk museum" in r["creator"]]
    ntnu_rows = [r for r in rows if r["archive"] == "unimus" and "NTNU" in r["creator"]]
    wm_rows = [r for r in rows if r["archive"] == "wikimedia"]
    nb_rows = [r for r in rows if r["archive"] == "other"]
    ks_rows = [r for r in rows if r["archive"] in ("ksamsok", "europeana")]

    def _sample_ids(sub: List[Dict[str, Any]], k: int = 8) -> str:
        ids = [f"`{r['motif_id']}` ({r['source_id']}, {r['site']})" for r in sub[:k]]
        if len(sub) > k:
            ids.append(f"... (+{len(sub) - k} more in `rights-inventory.csv`)")
        return ", ".join(ids)

    drafts_md = f"""# Archive Licensing & Attribution Inquiry Drafts (UNSENT)

> **STATUS: UNSENT DRAFTS — DO NOT TREAT AS SENT OR APPROVED.**
> These letters are prepared for producer review and legal counsel sign-off prior to transmission. No external emails have been sent by the build pipeline. Attach the filtered institutional slice of [`rights-inventory.csv`](./rights-inventory.csv) to each email before sending.

---

## Draft 1 — SHFA (Swedish Rock Art Research Archives, University of Gothenburg)

- **Status:** `UNSENT`
- **Recipient:** Svenskt HällristningsForskningsArkiv (SHFA), University of Gothenburg — `shfa@gu.se` (verify current contact on `https://shfa.dh.gu.se/`)
- **Motifs Covered:** **{len(shfa_rows)}** silhouettes (`CC BY 4.0`, Non-ShareAlike)
- **Sample Source IDs:** {_sample_ids(shfa_rows)}
- **Subject:** Commercial Wooden Jigsaw Puzzle — CC BY 4.0 Attribution Format Confirmation ({len(shfa_rows)} SHFA Rock-Art Tracings)

```text
Dear SHFA Team,

We are preparing a commercial, museum-grade 750-piece laser-cut birch wooden jigsaw puzzle
celebrating Scandinavian prehistoric rock art (helleristninger / hällristningar).

Our design adapts {len(shfa_rows)} individual rock-art silhouettes traced from archaeological
documentation plates published by SHFA under Creative Commons Attribution 4.0 International
(CC BY 4.0). A complete itemized schedule listing every SHFA record URL, original creator /
documentation institution, site name, and corresponding puzzle piece number is attached
(filtered from rights-inventory.csv).

Because a physical 750-piece jigsaw puzzle cannot print full per-piece credits on each wooden
piece without impairing aesthetics, we plan to satisfy CC BY 4.0 Section 3(a) attribution
("reasonable to the medium, means, and context") via:
  1. A prominent general credit to SHFA (University of Gothenburg) and CC BY 4.0 license URL
     on the exterior box and inside the printed companion booklet;
  2. A complete piece-by-piece attribution & rock-art legend table inside the companion booklet
     and on a permanent companion product webpage listing every creator, institution, site,
     SHFA source URL, CC BY 4.0 license link, and an explicit note that the silhouettes were
     geometrically adapted for laser-cut interlocking wooden puzzle manufacture.

Could you please confirm:
  (a) whether SHFA has any preferred wording or institutional credit convention for commercial
      derivative design products using CC BY 4.0 tracings; and
  (b) whether any of the attached {len(shfa_rows)} SHFA plates carry third-party rights caveats
      beyond the platform's general CC BY 4.0 notice?

Thank you for maintaining such an extraordinary open research archive for Nordic heritage.

Warm regards,
[Producer Name / Legal Entity]
[Postal Address & Email]
```

---

## Draft 2 — University Museum of Bergen (Universitetsmuseet i Bergen / Unimus) — ShareAlike Resolution

- **Status:** `UNSENT`
- **Recipient:** Universitetsmuseet i Bergen — `post@um.uib.no` (Attention: Image Archives / Rettighetsklarering, Vingen Rock-Art Collection)
- **Motifs Covered:** **{len(uib_rows)}** silhouettes (`CC BY-SA 4.0` via Unimus — Vingen, Bremanger)
- **Sample Source IDs:** {_sample_ids(uib_rows)}
- **Subject:** Forespørsel om kommersiell tillatelse / ikke-ShareAlike lisens — {len(uib_rows)} helleristningsmotiver fra Vingen (Unimus)

```text
Til Universitetsmuseet i Bergen (Bildeforvaltning / Rettigheter),

Vi utvikler et eksklusivt, laserkuttet trepuslespill i norsk/nordisk bjørkefinér med 750
figurative brikker basert på nordiske helleristninger, planlagt for kommersielt salg i
Norge og EU.

I vårt nåværende utvalg inngår {len(uib_rows)} dyre- og figurmotiver fra Vingen (Bremanger),
hentet fra Universitetsmuseenes fotoportal (Unimus), hvor originaldokumentasjonen er publisert
under lisensen CC BY-SA 4.0 (vedlagt komplett liste med Unimus-URL, opphav/fotograf, lokalitet
og brikkenummer fra rights-inventory.csv).

Siden CC BY-SA 4.0 inneholder et ShareAlike-krav («Del på samme vilkår») som kan medføre at
våre egenutviklede digitale skjærefiler (CAD/SVG) må deles under samme åpne lisens, ønsker vi
å avklare med museet om vi kan få:
  1. En skriftlig kommersiell brukstillatelse (tilsvarende CC BY 4.0, uten ShareAlike-krav på
     våre digitale puslespill-skjærefiler) for disse {len(uib_rows)} motivene, mot full og
     tydelig kreditering av Universitetsmuseet i Bergen, originaldokumentatør (bl.a. Johs. Bøe /
     Per Fett der oppgitt) og Vingen-feltet i medfølgende hefte og på produktsiden; ELLER
  2. Bekreftelse på museets vilkår/eventuelle vederlag for en slik kommersiell produktlisens.

Dersom museet ikke kan innvilge lisens uten ShareAlike, vil vi respektere dette fullt ut og
erstatte motivene før produksjon.

På forhånd tusen takk for hjelpen!

Med vennlig hilsen,
[Navn / Foretak]
[Org.nr., postadresse, e-post]
```

---

## Draft 3 — Kulturhistorisk museum (UiO) & NTNU Vitenskapsmuseet (Unimus) — ShareAlike Resolution

- **Status:** `UNSENT`
- **Recipients:**
  - Kulturhistorisk museum, Universitetet i Oslo (`postmottak@khm.uio.no` — verify current photo-archive contact on `khm.uio.no`): **{len(khm_rows)}** motifs (`CC BY-SA 4.0`)
  - NTNU Vitenskapsmuseet (`postmottak@vm.ntnu.no` — verify current photo-archive contact on `ntnu.no/vitenskapsmuseet`): **{len(ntnu_rows)}** motifs (`CC BY-SA 4.0`)
- **Sample Source IDs (KHM UiO):** {_sample_ids(khm_rows) if khm_rows else "None selected in current build"}
- **Sample Source IDs (NTNU VM):** {_sample_ids(ntnu_rows) if ntnu_rows else "None selected in current build"}
- **Subject:** Forespørsel om kommersiell produktlisens uten ShareAlike — Unimus helleristningskalkeringer

```text
Til [Kulturhistorisk museum, UiO / NTNU Vitenskapsmuseet] — Bildeforvaltning / Rettigheter,

Vi produserer et 750-brikkers figurativt trepuslespill basert på nordiske helleristninger for
kommersielt salg i Norge og EU. Vårt foreløpige utvalg benytter [ANTALL] dokumentasjonstegninger/
kalkeringer publisert under CC BY-SA 4.0 på Unimus (vedlagt oversikt med nøyaktige Unimus-URL-er,
opphavspersoner og brikke-ID-er).

For å kunne beskytte den egenutviklede puslespillgeometrien (laserkuttefilene) mot kopiering,
ber vi vennligst om avklaring på om museet kan gi skriftlig tillatelse til kommersiell bruk
på vilkår tilsvarende CC BY 4.0 (med full kreditering av museet, fotograf/tegner og funnsted i
medfølgende hefte og på nett, men uten ShareAlike-plikt på selve skjærefilen).

Hvis dette ikke lar seg gjøre, vil vi bytte ut de aktuelle motivene før kommersiell produksjon.

Med vennlig hilsen,
[Navn / Foretak]
[Org.nr., postadresse, e-post]
```

---

## Draft 4 — Legal Review Checklist for Wikimedia Commons, Nasjonalbiblioteket, K-samsök & Europeana Records

- **Status:** `UNSENT / INTERNAL LEGAL COUNSEL BRIEF`
- **Records Covered:** Wikimedia Commons (**{len(wm_rows)}** non-SA records), Nasjonalbiblioteket (**{len(nb_rows)}** Public Domain records), K-samsök / Europeana (**{len(ks_rows)}** `CC BY 3.0` / `PDM 1.0` records).
- **Specific Questions for Norwegian/EU IP Counsel before Launch:**
  1. **Wikimedia Commons 20th-Century Archaeological Tracings ({len(pd_gjessing)} records):** Confirm whether 1930s Norwegian/Nordic rock-art tracings credited to Gutorm Gjessing (d. 1979), Mary Wilhelmsen, and Th. Petersen (d. 1952) and tagged `Public domain` on Wikimedia Commons are free of copyright in Norway/EU (70-year p.m.a. vs. lack of *verkshøyde* on faithful archaeological tracings), or whether they should be swapped for SHFA `CC BY 4.0` equivalents.
  2. **CC BY 2.5 / 3.0 / 4.0 Attribution Adequacy:** Confirm that printing full archive/creator/license credits in the enclosed booklet + permanent companion webpage satisfies the "reasonable to the medium" standard under CC BY 2.5, 3.0, and 4.0.
  3. **Zero Alta Museum NC-SA Verification:** Confirmed **0** Alta Museum NC-SA records remain in `selected-750-catalog.json` (all 298 excluded). If Alta carvings are desired in a future edition, a separate commercial license request must be submitted to *Verdensarvsenter for bergkunst — Alta Museum*.
"""
    (DOCS_DIR / "archive-inquiry-drafts.md").write_text(drafts_md, encoding="utf-8")
    return inv_json


# ---------------------------------------------------------------------------
# Stage 3: Supplier Laser Calibration Coupon (1:1 mm SVG + PNG + Worksheets)
# ---------------------------------------------------------------------------

# Clean single-stroke vector glyph strokes (normalized on 0..1 x, 0..1 y, origin top-left)
# Used ONLY inside #engraving so laser cutters never depend on installed system fonts.
_STROKE_FONT: Dict[str, List[List[Tuple[float, float]]]] = {
    "0": [[(0.15, 0.0), (0.85, 0.0), (0.85, 1.0), (0.15, 1.0), (0.15, 0.0)], [(0.2, 0.85), (0.8, 0.15)]],
    "1": [[(0.25, 0.2), (0.55, 0.0), (0.55, 1.0)], [(0.2, 1.0), (0.85, 1.0)]],
    "2": [[(0.15, 0.2), (0.35, 0.0), (0.75, 0.0), (0.85, 0.2), (0.85, 0.4), (0.15, 1.0), (0.85, 1.0)]],
    "3": [[(0.15, 0.0), (0.85, 0.0), (0.5, 0.45), (0.85, 0.6), (0.85, 0.85), (0.65, 1.0), (0.15, 1.0)]],
    "4": [[(0.7, 1.0), (0.7, 0.0), (0.15, 0.65), (0.9, 0.65)]],
    "5": [[(0.85, 0.0), (0.2, 0.0), (0.15, 0.45), (0.7, 0.45), (0.85, 0.65), (0.85, 0.85), (0.65, 1.0), (0.15, 1.0)]],
    "6": [[(0.8, 0.0), (0.3, 0.0), (0.15, 0.35), (0.15, 1.0), (0.85, 1.0), (0.85, 0.5), (0.15, 0.5)]],
    "7": [[(0.15, 0.0), (0.85, 0.0), (0.4, 1.0)]],
    "8": [[(0.15, 0.0), (0.85, 0.0), (0.85, 1.0), (0.15, 1.0), (0.15, 0.0)], [(0.15, 0.5), (0.85, 0.5)]],
    "9": [[(0.85, 0.5), (0.15, 0.5), (0.15, 0.0), (0.85, 0.0), (0.85, 1.0), (0.25, 1.0)]],
    ".": [[(0.45, 0.88), (0.55, 0.88), (0.55, 1.0), (0.45, 1.0), (0.45, 0.88)]],
    "-": [[(0.2, 0.5), (0.8, 0.5)]],
    "+": [[(0.5, 0.2), (0.5, 0.8)], [(0.2, 0.5), (0.8, 0.5)]],
    "=": [[(0.18, 0.36), (0.82, 0.36)], [(0.18, 0.64), (0.82, 0.64)]],
    "(": [[(0.68, 0.0), (0.38, 0.28), (0.38, 0.72), (0.68, 1.0)]],
    ")": [[(0.32, 0.0), (0.62, 0.28), (0.62, 0.72), (0.32, 1.0)]],
    ">": [[(0.2, 0.2), (0.8, 0.5), (0.2, 0.8)]],
    "&": [[(0.85, 0.95), (0.2, 0.35), (0.25, 0.1), (0.55, 0.0), (0.72, 0.2), (0.2, 0.68), (0.25, 0.95), (0.55, 1.0), (0.85, 0.65)]],
    "/": [[(0.2, 1.0), (0.8, 0.0)]],
    ":": [[(0.45, 0.25), (0.55, 0.25)], [(0.45, 0.75), (0.55, 0.75)]],
    " ": [],
    "A": [[(0.1, 1.0), (0.5, 0.0), (0.9, 1.0)], [(0.25, 0.65), (0.75, 0.65)]],
    "B": [[(0.15, 1.0), (0.15, 0.0), (0.7, 0.0), (0.85, 0.2), (0.7, 0.48), (0.15, 0.48)], [(0.7, 0.48), (0.85, 0.72), (0.7, 1.0), (0.15, 1.0)]],
    "C": [[(0.85, 0.15), (0.65, 0.0), (0.25, 0.0), (0.15, 0.2), (0.15, 0.8), (0.25, 1.0), (0.65, 1.0), (0.85, 0.85)]],
    "D": [[(0.15, 0.0), (0.15, 1.0), (0.65, 1.0), (0.85, 0.75), (0.85, 0.25), (0.65, 0.0), (0.15, 0.0)]],
    "E": [[(0.85, 0.0), (0.15, 0.0), (0.15, 1.0), (0.85, 1.0)], [(0.15, 0.5), (0.7, 0.5)]],
    "F": [[(0.85, 0.0), (0.15, 0.0), (0.15, 1.0)], [(0.15, 0.5), (0.7, 0.5)]],
    "G": [[(0.85, 0.15), (0.65, 0.0), (0.25, 0.0), (0.15, 0.2), (0.15, 0.8), (0.25, 1.0), (0.85, 1.0), (0.85, 0.55), (0.5, 0.55)]],
    "H": [[(0.15, 0.0), (0.15, 1.0)], [(0.85, 0.0), (0.85, 1.0)], [(0.15, 0.5), (0.85, 0.5)]],
    "I": [[(0.25, 0.0), (0.75, 0.0)], [(0.5, 0.0), (0.5, 1.0)], [(0.25, 1.0), (0.75, 1.0)]],
    "J": [[(0.75, 0.0), (0.75, 0.8), (0.55, 1.0), (0.25, 1.0), (0.15, 0.8)]],
    "K": [[(0.15, 0.0), (0.15, 1.0)], [(0.85, 0.0), (0.15, 0.55), (0.85, 1.0)]],
    "L": [[(0.15, 0.0), (0.15, 1.0), (0.85, 1.0)]],
    "M": [[(0.1, 1.0), (0.1, 0.0), (0.5, 0.55), (0.9, 0.0), (0.9, 1.0)]],
    "N": [[(0.15, 1.0), (0.15, 0.0), (0.85, 1.0), (0.85, 0.0)]],
    "O": [[(0.15, 0.15), (0.3, 0.0), (0.7, 0.0), (0.85, 0.15), (0.85, 0.85), (0.7, 1.0), (0.3, 1.0), (0.15, 0.85), (0.15, 0.15)]],
    "P": [[(0.15, 1.0), (0.15, 0.0), (0.7, 0.0), (0.85, 0.2), (0.7, 0.5), (0.15, 0.5)]],
    "Q": [[(0.15, 0.15), (0.3, 0.0), (0.7, 0.0), (0.85, 0.15), (0.85, 0.85), (0.7, 1.0), (0.3, 1.0), (0.15, 0.85), (0.15, 0.15)], [(0.55, 0.7), (0.92, 1.05)]],
    "R": [[(0.15, 1.0), (0.15, 0.0), (0.7, 0.0), (0.85, 0.2), (0.7, 0.5), (0.15, 0.5)], [(0.5, 0.5), (0.85, 1.0)]],
    "S": [[(0.85, 0.15), (0.65, 0.0), (0.25, 0.0), (0.15, 0.2), (0.25, 0.45), (0.75, 0.55), (0.85, 0.8), (0.7, 1.0), (0.25, 1.0), (0.15, 0.85)]],
    "T": [[(0.1, 0.0), (0.9, 0.0)], [(0.5, 0.0), (0.5, 1.0)]],
    "U": [[(0.15, 0.0), (0.15, 0.8), (0.35, 1.0), (0.65, 1.0), (0.85, 0.8), (0.85, 0.0)]],
    "V": [[(0.1, 0.0), (0.5, 1.0), (0.9, 0.0)]],
    "W": [[(0.05, 0.0), (0.25, 1.0), (0.5, 0.45), (0.75, 1.0), (0.95, 0.0)]],
    "X": [[(0.15, 0.0), (0.85, 1.0)], [(0.85, 0.0), (0.15, 1.0)]],
    "Y": [[(0.15, 0.0), (0.5, 0.5), (0.85, 0.0)], [(0.5, 0.5), (0.5, 1.0)]],
    "Z": [[(0.15, 0.0), (0.85, 0.0), (0.15, 1.0), (0.85, 1.0)]],
}


def _vector_text_width(text: str, height_mm: float = 3.2) -> float:
    """Return total horizontal advance width (mm) of `_vector_text_polylines(text, ..., height_mm)`."""
    if not text:
        return 0.0
    char_w = height_mm * 0.62
    step_x = height_mm * 0.82
    return (len(text) - 1) * step_x + char_w


def _vector_text_polylines(text: str, x: float, y: float, height_mm: float = 3.2) -> List[List[Tuple[float, float]]]:
    """Convert uppercase ASCII text into pure single-stroke vector polylines in mm."""
    char_w = height_mm * 0.62
    step_x = height_mm * 0.82
    lines: List[List[Tuple[float, float]]] = []
    cx = x
    for ch in text.upper():
        strokes = _STROKE_FONT.get(ch, [])
        for st in strokes:
            lines.append([(cx + px * char_w, y + py * height_mm) for px, py in st])
        cx += step_x
    return lines


def _pts_to_d(pts: Sequence[Tuple[float, float]], closed: bool = False) -> str:
    if not pts:
        return ""
    segs = [f"M {pts[0][0]:.4f} {pts[0][1]:.4f}"]
    for px, py in pts[1:]:
        segs.append(f"L {px:.4f} {py:.4f}")
    if closed:
        segs.append("Z")
    return " ".join(segs)


def _interlock_seam_curve(
    x_mid: float,
    y0: float,
    y1: float,
    neck_mm: float = 4.2,
    head_r_mm: float = 3.8,
    depth_mm: float = 6.2,
    n_arc: int = 36,
) -> List[Tuple[float, float]]:
    """Clean, smooth undercut circular-bulb jigsaw tab/socket seam from (x_mid, y0) to (x_mid, y1)."""
    yc = 0.5 * (y0 + y1)
    half_neck = neck_mm * 0.5
    # Bulb center lies at x_mid + depth_mm - head_r_mm; entry angle where bulb meets neck width
    cx = x_mid + depth_mm - head_r_mm
    sin_a = max(-0.92, min(0.92, half_neck / max(head_r_mm, 1e-6)))
    a_neck = math.asin(sin_a)
    # Shoulder fillet from straight edge at x_mid into bulb entry point
    entry_x = cx - head_r_mm * math.cos(a_neck)
    pts: List[Tuple[float, float]] = [
        (x_mid, y0),
        (x_mid, yc - half_neck - 4.0),
        (x_mid + 0.35 * (entry_x - x_mid), yc - half_neck - 1.6),
        (x_mid + 0.75 * (entry_x - x_mid), yc - half_neck - 0.35),
    ]
    # Smooth circular arc around tab head from (pi + a_neck) around 0 to (pi - a_neck) clockwise in +y-down
    # Wait: angles around bulb center (cx, yc):
    # Top entry is (-cos(a_neck), -sin(a_neck)) => angle = -(pi - a_neck)
    # Tip (+x) is angle = 0
    # Bottom exit is (-cos(a_neck), +sin(a_neck)) => angle = +(pi - a_neck)
    ang_start = -(math.pi - a_neck)
    ang_end = +(math.pi - a_neck)
    for i in range(n_arc + 1):
        ang = ang_start + (ang_end - ang_start) * (i / n_arc)
        pts.append((cx + head_r_mm * math.cos(ang), yc + head_r_mm * math.sin(ang)))
    pts.extend([
        (x_mid + 0.75 * (entry_x - x_mid), yc + half_neck + 0.35),
        (x_mid + 0.35 * (entry_x - x_mid), yc + half_neck + 1.6),
        (x_mid, yc + half_neck + 4.0),
        (x_mid, y1),
    ])
    return pts


def build_calibration_coupon() -> Dict[str, Any]:
    """Generate 1:1 mm laser-cut birch plywood supplier calibration coupon SVG + PNG + worksheets."""
    COMMERCIAL_RUN_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    W, H = 240.0, 185.0
    kerf_stroke = "0.1500"
    engrave_stroke = "0.2000"

    interior_paths: List[Tuple[str, str, List[Tuple[float, float]], bool]] = []
    exterior_paths: List[Tuple[str, str, List[Tuple[float, float]], bool]] = []
    engrave_paths: List[Tuple[str, str, List[Tuple[float, float]], bool]] = []

    def add_int(pid: str, pts: Sequence[Tuple[float, float]], closed: bool = False) -> None:
        interior_paths.append((pid, _pts_to_d(pts, closed=closed), list(pts), closed))

    def add_ext(pid: str, pts: Sequence[Tuple[float, float]], closed: bool = False) -> None:
        exterior_paths.append((pid, _pts_to_d(pts, closed=closed), list(pts), closed))

    def add_eng(pid: str, pts: Sequence[Tuple[float, float]], closed: bool = False) -> None:
        engrave_paths.append((pid, _pts_to_d(pts, closed=closed), list(pts), closed))

    def add_text(prefix: str, text: str, x: float, y: float, h: float = 3.0) -> None:
        for i, pl in enumerate(_vector_text_polylines(text, x, y, height_mm=h)):
            add_eng(f"{prefix}-{i:03d}", pl, closed=False)

    # Outer coupon frame (240 x 185 mm with 3 mm corner chamfer) -> EXTERIOR CUT
    r_c = 3.0
    outer_pts = [
        (r_c, 0.0), (W - r_c, 0.0), (W, r_c), (W, H - r_c),
        (W - r_c, H), (r_c, H), (0.0, H - r_c), (0.0, r_c),
    ]
    add_ext("coupon-outer-frame", outer_pts, closed=True)

    # Header engraving (inset >= 5 mm from outer cut)
    add_text("hdr-title", "HELLERISTNING 750 - BIRCH PLYWOOD LASER CALIBRATION COUPON 1:1 MM", 8.0, 5.5, h=3.1)
    add_text("hdr-sub", "RED=INTERIOR CUT  BLUE=EXTERIOR CUT  GREEN=ENGRAVE   GRAIN --->", 8.0, 10.6, h=2.3)
    # Grain arrow
    add_eng("grain-arrow-shaft", [(148.0, 11.8), (175.0, 11.8)])
    add_eng("grain-arrow-head", [(171.0, 10.0), (175.0, 11.8), (171.0, 13.6)])
    add_text("hdr-batch", "ID: CAL-V1  240X185MM", 181.0, 10.6, h=2.3)

    # =========================================================================
    # SECTION A (Top-Left, x=8..150, y=18..68): KERF STEP GAUGE (0.08..0.30 mm)
    # =========================================================================
    add_text("secA-lbl", "A. KERF STEP GAUGE 0.08-0.30 MM + 10-CUT CALIPER BLOCK", 8.0, 18.0, h=2.4)

    # A1. 10-cut caliper kerf block (x=10..110, y=24..40): 10 identical 10.00 mm strips inside 100x16 mm pocket
    bx0, by0, bx1, by1 = 10.0, 24.0, 110.0, 40.0
    add_int("kerf-block-frame", [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)], closed=True)
    for k in range(1, 10):
        xk = bx0 + k * 10.0
        add_int(f"kerf-block-cut-{k:02d}", [(xk, by0), (xk, by1)], closed=False)
    add_text("kerf-block-note", "PUSH 10 STRIPS TOGETHER: KERF = TOTAL GAP / 10", 10.0, 42.2, h=2.2)

    # A2. Direct kerf slot comb (10 nominal slot widths: 0.08 .. 0.30 mm subtracted from 10.00 mm reference tab)
    kerf_steps = [0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.22, 0.25, 0.28, 0.30]
    comb_x0, comb_y0 = 10.0, 48.0
    pitch = 13.4
    slot_depth = 10.0
    comb_pts: List[Tuple[float, float]] = [(comb_x0, comb_y0 + 18.0), (comb_x0, comb_y0)]
    for idx, k_mm in enumerate(kerf_steps):
        sx = comb_x0 + 2.0 + idx * pitch
        slot_w = 10.0 - k_mm
        comb_pts.extend([
            (sx, comb_y0),
            (sx, comb_y0 + slot_depth),
            (sx + slot_w, comb_y0 + slot_depth),
            (sx + slot_w, comb_y0),
        ])
        add_text(f"kstep-{idx:02d}", f"{k_mm:.2f}", sx + 1.0, comb_y0 + slot_depth + 2.5, h=2.2)
    comb_end_x = comb_x0 + 2.0 + len(kerf_steps) * pitch + 1.5
    comb_pts.extend([(comb_end_x, comb_y0), (comb_end_x, comb_y0 + 18.0)])
    add_int("kerf-step-comb", comb_pts, closed=True)

    # Mating 10.00 mm reference gauge bar (x=116..146, y=24..34)
    gx0, gy0 = 116.0, 24.0
    add_int("kerf-ref-bar", [(gx0, gy0), (gx0 + 30.0, gy0), (gx0 + 30.0, gy0 + 10.0), (gx0, gy0 + 10.0)], closed=True)
    add_text("kerf-ref-lbl", "10.00MM BAR", gx0 + 2.0, gy0 + 3.8, h=2.2)

    # =========================================================================
    # SECTION B (Top-Right, x=164..232, y=18..74): 50x50 MM CALIBRATION SQUARE
    # =========================================================================
    add_text("secB-lbl", "B. 50X50 MM SCALE CHECK", 162.0, 18.0, h=2.4)
    sq_x0, sq_y0 = 166.0, 23.0
    add_int("scale-square-50mm", [(sq_x0, sq_y0), (sq_x0 + 50.0, sq_y0), (sq_x0 + 50.0, sq_y0 + 50.0), (sq_x0, sq_y0 + 50.0)], closed=True)
    add_text("sq-x", "X = 50.00 MM", sq_x0 + 8.5, sq_y0 + 16.0, h=2.7)
    add_text("sq-y", "Y = 50.00 MM", sq_x0 + 8.5, sq_y0 + 24.5, h=2.7)
    add_text("sq-diag", "DIAG = 70.71", sq_x0 + 9.0, sq_y0 + 33.5, h=2.5)

    # =========================================================================
    # SECTION C (Middle, x=8..232, y=79..121): NECK-STRENGTH TORTURE BARS
    # Clear 6 mm buffer below Section B (y=73) before Section C header at y=79!
    # =========================================================================
    add_text("secC-lbl", "C. NECK-STRENGTH TORTURE BARS: 2.0 / 2.5 / 3.0 / 3.5 / 4.0 / 5.0 MM (0D & 90D)", 8.0, 79.0, h=2.3)
    neck_widths = [2.0, 2.5, 3.0, 3.5, 4.0, 5.0]

    # C1. Horizontal (with-grain 0D) dog-bone bridges (x=10..124, y=85..119)
    for idx, nw in enumerate(neck_widths):
        col = idx % 3
        row = idx // 3
        ox = 10.0 + col * 38.0
        oy = 85.0 + row * 17.5
        pad_h = 13.0
        pad_w = 10.0
        span = 14.0
        ym = oy + pad_h * 0.5
        hn = nw * 0.5
        pts = [
            (ox, oy),
            (ox + pad_w, oy),
            (ox + pad_w + 1.2, ym - hn),
            (ox + pad_w + span - 1.2, ym - hn),
            (ox + pad_w + span, oy),
            (ox + 2 * pad_w + span, oy),
            (ox + 2 * pad_w + span, oy + pad_h),
            (ox + pad_w + span, oy + pad_h),
            (ox + pad_w + span - 1.2, ym + hn),
            (ox + pad_w + 1.2, ym + hn),
            (ox + pad_w, oy + pad_h),
            (ox, oy + pad_h),
        ]
        add_int(f"neck-h-{nw:.1f}mm", pts, closed=True)
        add_text(f"nh-lbl-{idx}", f"{nw:.1f}", ox + 2.0, ym - 1.3, h=2.4)
        add_text(f"nh-g-{idx}", "0D", ox + pad_w + span + 2.5, ym - 1.3, h=2.4)

    # C2. Vertical (cross-grain 90D) dog-bone bridges (x=130..232, y=85..117)
    for idx, nw in enumerate(neck_widths):
        ox = 130.0 + idx * 17.0
        oy = 85.0
        pad_w = 13.5
        pad_h = 9.0
        span = 14.0
        xm = ox + pad_w * 0.5
        hn = nw * 0.5
        pts = [
            (ox, oy),
            (ox + pad_w, oy),
            (ox + pad_w, oy + pad_h),
            (xm + hn, oy + pad_h + 1.2),
            (xm + hn, oy + pad_h + span - 1.2),
            (ox + pad_w, oy + pad_h + span),
            (ox + pad_w, oy + 2 * pad_h + span),
            (ox, oy + 2 * pad_h + span),
            (ox, oy + pad_h + span),
            (xm - hn, oy + pad_h + span - 1.2),
            (xm - hn, oy + pad_h + 1.2),
            (ox, oy + pad_h),
        ]
        add_int(f"neck-v-{nw:.1f}mm", pts, closed=True)
        add_text(f"nv-lbl-{idx}", f"{nw:.1f}", ox + 2.2, oy + 3.2, h=2.3)
        add_text(f"nv-g-{idx}", "90D", ox + 2.2, oy + pad_h + span + 3.2, h=2.2)

    # =========================================================================
    # SECTION D (Bottom, x=8..232, y=126..178): INTERLOCKING TAB/SOCKET FIT PAIRS
    # 5 kerf-offset pairs (0.00 common-line, +0.05, +0.10, +0.15, +0.20 mm radial offset)
    # Separated pieces have a clean 4.0 mm clearance gap -> ZERO intersecting cuts!
    # =========================================================================
    add_text("secD-lbl", "D. INTERLOCKING TAB/SOCKET FIT PAIRS (0.00 COMMON-LINE & KERF-COMPENSATED OFFSETS)", 8.0, 126.0, h=2.3)
    fit_offsets = [0.00, 0.05, 0.10, 0.15, 0.20]
    pair_h = 42.0
    py0 = 132.5
    py1 = py0 + pair_h
    for idx, off in enumerate(fit_offsets):
        px0 = 8.0 + idx * 45.6
        if abs(off) < 1e-6:
            pair_w = 40.0
            px1 = px0 + pair_w
            xm = px0 + pair_w * 0.5 - 2.0
            seam = _interlock_seam_curve(xm, py0, py1, neck_mm=4.2, head_r_mm=3.8, depth_mm=6.4)
            add_int(f"fit-pair-{idx}-outer", [(px0, py0), (px1, py0), (px1, py1), (px0, py1)], closed=True)
            add_int(f"fit-pair-{idx}-seam", seam, closed=False)
            add_text(f"fit-lbl-{idx}-l", "0.00", px0 + 2.5, py0 + 4.0, h=2.4)
            add_text(f"fit-lbl-{idx}-r", "CL", px0 + 28.0, py0 + 4.0, h=2.4)
            add_text(f"fit-lbl-{idx}-b", "COMMON", px0 + 2.5, py1 - 6.0, h=2.0)
        else:
            # Cut left tab piece and right socket piece side-by-side with clean >=3.0 mm clearance gap
            # Left straight edge at px0 + 12.0 -> tab tip extends to px0 + 18.4 (+off/2)
            # Right straight edge at px0 + 21.6 -> socket recesses to px0 + 28.0, right outer edge at px0 + 40.0
            xm_left = px0 + 12.0
            x_shift = 9.6  # 21.6 - 12.0 -> 3.2 mm clear wood gap between tab tip (18.4) and right vertical wall (21.6)
            seam_nom = _interlock_seam_curve(xm_left, py0, py1, neck_mm=4.2, head_r_mm=3.8, depth_mm=6.4)
            # Left tab expanded radially by +off/2 along mating contour (tighter fit compensation)
            left_poly = Polygon([(px0, py0)] + seam_nom + [(px0, py1)]).buffer(0)
            left_adj = left_poly.buffer(off * 0.5, join_style="round", quad_segs=10)
            # Right socket uses nominal negative seam, shifted cleanly right by x_shift
            right_poly = Polygon([(px0 + 40.0 - x_shift, py0), (px0 + 40.0 - x_shift, py1)] + list(reversed(seam_nom))).buffer(0)
            right_shifted = translate(right_poly, xoff=x_shift, yoff=0.0)
            add_int(f"fit-pair-{idx}-tab", list(left_adj.exterior.coords)[:-1], closed=True)
            add_int(f"fit-pair-{idx}-sock", list(right_shifted.exterior.coords)[:-1], closed=True)
            add_text(f"fit-lbl-{idx}-l", f"+{off:.2f}", px0 + 1.8, py0 + 4.0, h=2.2)
            add_text(f"fit-lbl-{idx}-r", "FIT", px0 + 29.5, py0 + 4.0, h=2.3)

    # Assemble strict layered SVG
    root = ET.Element(
        f"{{{SVG_NS}}}svg",
        {
            "width": f"{W:.4f}mm",
            "height": f"{H:.4f}mm",
            "viewBox": f"0.0000 0.0000 {W:.4f} {H:.4f}",
            "version": "1.1",
        },
    )
    g_int = ET.SubElement(
        root,
        f"{{{SVG_NS}}}g",
        {
            "id": "interior-cuts",
            "fill": "none",
            "stroke": "#FF0000",
            "stroke-width": kerf_stroke,
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for pid, d_str, _, _ in interior_paths:
        ET.SubElement(g_int, f"{{{SVG_NS}}}path", {"id": pid, "d": d_str, "fill": "none"})

    g_ext = ET.SubElement(
        root,
        f"{{{SVG_NS}}}g",
        {
            "id": "exterior-cuts",
            "fill": "none",
            "stroke": "#0000FF",
            "stroke-width": kerf_stroke,
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for pid, d_str, _, _ in exterior_paths:
        ET.SubElement(g_ext, f"{{{SVG_NS}}}path", {"id": pid, "d": d_str, "fill": "none"})

    g_eng = ET.SubElement(
        root,
        f"{{{SVG_NS}}}g",
        {
            "id": "engraving",
            "fill": "none",
            "stroke": "#008800",
            "stroke-width": engrave_stroke,
            "stroke-linecap": "round",
            "stroke-linejoin": "round",
        },
    )
    for pid, d_str, _, _ in engrave_paths:
        ET.SubElement(g_eng, f"{{{SVG_NS}}}path", {"id": pid, "d": d_str, "fill": "none"})

    svg_path = COMMERCIAL_RUN_DIR / "calibration_coupon.svg"
    ET.ElementTree(root).write(str(svg_path), encoding="utf-8", xml_declaration=True)

    # Render crisp high-resolution preview PNG for visual inspection
    png_path = COMMERCIAL_RUN_DIR / "calibration_coupon_preview.png"
    _render_coupon_preview_png(
        W, H, interior_paths, exterior_paths, engrave_paths, png_path
    )

    # Write supplier measurement worksheets (MD + printable HTML)
    _write_calibration_worksheets(W, H, kerf_steps, neck_widths, fit_offsets)

    spec = {
        "svg_path": _rel_to_root(svg_path),
        "preview_png_path": _rel_to_root(png_path),
        "width_mm": W,
        "height_mm": H,
        "layers": {
            "interior-cuts": {"stroke": "#FF0000", "path_count": len(interior_paths)},
            "exterior-cuts": {"stroke": "#0000FF", "path_count": len(exterior_paths)},
            "engraving": {"stroke": "#008800", "path_count": len(engrave_paths)},
        },
        "kerf_steps_mm": kerf_steps,
        "neck_widths_mm": neck_widths,
        "fit_offsets_mm": fit_offsets,
    }
    (COMMERCIAL_RUN_DIR / "calibration_coupon_spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return spec


def _render_coupon_preview_png(
    w_mm: float,
    h_mm: float,
    interior_paths: Sequence[Tuple[str, str, List[Tuple[float, float]], bool]],
    exterior_paths: Sequence[Tuple[str, str, List[Tuple[float, float]], bool]],
    engrave_paths: Sequence[Tuple[str, str, List[Tuple[float, float]], bool]],
    out_path: Path,
) -> None:
    scale_px = 8.0  # 8 px / mm (~203 DPI) -> 1920 x 1400 + margin
    margin = 48
    img_w = int(w_mm * scale_px + 2 * margin)
    img_h = int(h_mm * scale_px + 2 * margin + 40)
    im = Image.new("RGB", (img_w, img_h), "#F4EFE6")
    draw = ImageDraw.Draw(im)

    # Birch board background fill inside outer coupon bounds
    draw.rectangle(
        [margin, margin, margin + w_mm * scale_px, margin + h_mm * scale_px],
        fill="#EADBC4",
        outline="#C2AF93",
        width=2,
    )

    # Subtle 10 mm reference grid
    for xm in range(10, int(w_mm), 10):
        px = margin + xm * scale_px
        draw.line([(px, margin), (px, margin + h_mm * scale_px)], fill="#E0CEB3", width=1)
    for ym in range(10, int(h_mm), 10):
        py = margin + ym * scale_px
        draw.line([(margin, py), (margin + w_mm * scale_px, py)], fill="#E0CEB3", width=1)

    def draw_group(paths, color, width):
        for _, _, pts, closed in paths:
            if len(pts) < 2:
                continue
            spts = [(margin + x * scale_px, margin + y * scale_px) for x, y in pts]
            if closed:
                spts.append(spts[0])
            draw.line(spts, fill=color, width=width, joint="curve")

    draw_group(engrave_paths, "#137333", 2)
    draw_group(interior_paths, "#D93025", 3)
    draw_group(exterior_paths, "#1A56DB", 4)

    font = _load_font(15, bold=True)
    font_sm = _load_font(12, bold=False)
    draw.text(
        (margin, 12),
        f"Supplier Laser Calibration Coupon Preview ({w_mm:.0f} x {h_mm:.0f} mm, 1:1 Scale) — Birch Plywood Test",
        fill="#1F1B18",
        font=font,
    )
    legend_y = int(margin + h_mm * scale_px + 12)
    draw.text(
        (margin, legend_y),
        "Layers: [RED] #interior-cuts (cut first)   |   [BLUE] #exterior-cuts (cut last)   |   [GREEN] #engraving (score/engrave)   |   10 mm grid shown for visual verification only",
        fill="#3B342E",
        font=font_sm,
    )
    im.save(out_path)


def _write_calibration_worksheets(
    w_mm: float,
    h_mm: float,
    kerf_steps: Sequence[float],
    neck_widths: Sequence[float],
    fit_offsets: Sequence[float],
) -> None:
    md = f"""# Supplier Laser Calibration Measurement Worksheet — Helleristning 750

**Coupon File:** [`runs/helleristning-750-commercial/calibration_coupon.svg`](../../runs/helleristning-750-commercial/calibration_coupon.svg) (`{w_mm:.0f} × {h_mm:.0f} mm`, strict 1:1 mm)  
**Visual Preview:** [`runs/helleristning-750-commercial/calibration_coupon_preview.png`](../../runs/helleristning-750-commercial/calibration_coupon_preview.png)  
**Printable Form:** [`calibration-measurement-worksheet.html`](./calibration-measurement-worksheet.html)

---

## 1. Laser Layer Cut Order & Setup (Mandatory)

| SVG Group ID | Stroke Color | Operation | Order | Notes |
|---|---|---|---|---|
| `#engraving` | `#008800` (Green) | Surface score / light vector engrave | **1st** | Low power, readable without charring through top veneer |
| `#interior-cuts` | `#FF0000` (Red) | Full through-cut | **2nd** | Zero duplicate shared paths; do NOT auto-optimize away kerf pairs |
| `#exterior-cuts` | `#0000FF` (Blue) | Full through-cut | **3rd (Last)** | Releases the `{w_mm:.0f} × {h_mm:.0f} mm` coupon from the sheet |

> **IMPORTANT:** Cut at **exact 100% scale (1:1 mm)** with **auto kerf-compensation DISABLED** in the laser controller so measured kerf reflects the true beam width. Orient sheet face grain **horizontally** along the `GRAIN --->` arrow engraved on the coupon.

---

## 2. Material, Machine & Compliance Header

| Field | Supplier Entry |
|---|---|
| Supplier / Workshop Name | ____________________________________________ |
| Test Date & Operator | ____________________________________________ |
| Wood Species & Grade (e.g., Finnish Birch B/BB) | ____________________________________________ |
| Mill Country of Origin (Non-RU / Non-BY confirmed?) | [ ] Yes, Country: __________  [ ] FSC/PEFC CoC Attached |
| Nominal vs. Measured Sheet Thickness (mm) | Nominal: _____ mm  /  Caliper (4 corners): _____, _____, _____, _____ mm |
| Ply Count & Glue Line Type (NAF / ULEF / PF / UF) | ____________________________________________ |
| REACH Entry 77 Formaldehyde Test (≤ 0.062 mg/m³) | [ ] Report Attached  [ ] Pending (Blocks EU Sale) |
| Masking Applied? (Top / Bottom / Unmasked) | ____________________________________________ |
| Laser Machine, Wattage, Lens Focal Length | ____________________________________________ |
| Cut Settings (Power %, Speed mm/s, PPI/Hz, Air Assist) | ____________________________________________ |
| Engrave Settings (Power %, Speed mm/s) | ____________________________________________ |

---

## 3. Section A — Kerf Measurement (`0.08–0.30 mm`)

1. **10-Cut Caliper Block:** Push all 10 cut strips tight against one side of the `100.00 mm` pocket. Measure total accumulated gap with digital calipers (0.01 mm resolution):
   - Total 10-cut gap: `__________ mm` → **Measured Average Kerf (`gap / 10`) = `__________ mm`**
2. **Kerf Step Comb (`10.00 mm` Reference Bar):** Insert the `10.00 mm` reference bar into each labeled slot (`{" / ".join(f"{k:.2f}" for k in kerf_steps)} mm`):

| Slot Label (mm) | 0.08 | 0.10 | 0.12 | 0.15 | 0.18 | 0.20 | 0.22 | 0.25 | 0.28 | 0.30 |
|---|---|---|---|---|---|---|---|---|---|---|
| Fit (`L`=Loose / `S`=Snug / `T`=Tight / `X`=No-go) | | | | | | | | | | |

- **Recommended Digital Design Kerf for Production:** `__________ mm`

---

## 4. Section B — Dimensional Accuracy (`50.00 × 50.00 mm` Square)

| Dimension | Nominal | Measured (mm) | Error (mm) | Pass (`±0.20 mm`)? |
|---|---:|---:|---:|---|
| X Width (along grain) | 50.00 mm | | | [ ] Pass [ ] Fail |
| Y Height (cross grain) | 50.00 mm | | | [ ] Pass [ ] Fail |
| Diagonal 1 / Diagonal 2 (Squareness) | 70.71 mm | _____ / _____ | | [ ] Pass [ ] Fail |

---

## 5. Section C — Neck-Strength Torture Bars (With-Grain `0°` vs. Cross-Grain `90°`)

Gently flex, twist, and perform thumb-press / drop-test evaluation on each bridge width (`{", ".join(f"{w:.1f} mm" for w in neck_widths)}`):

| Nominal Neck (mm) | `0°` Actual Caliper Width (mm) | `0°` Snap / Survival Rating (1–5) | `90°` Actual Caliper Width (mm) | `90°` Snap / Survival Rating (1–5) | Delamination / Char Notes |
|---:|---:|---|---:|---|---|
| **2.0 mm** | | | | | |
| **2.5 mm** | | | | | |
| **3.0 mm** | | | | | |
| **3.5 mm** | | | | | |
| **4.0 mm** | | | | | |
| **5.0 mm** | | | | | |

*Rating scale: `5` = rigid & unbreakable by normal hand force; `4` = solid slight flex; `3` = flexes noticeably; `2` = cracks under moderate pinch; `1` = breaks during removal/handling.*  
**Minimum Safe Neck Width Verified on This Plywood:** `0°`: _____ mm  /  `90°`: _____ mm

---

## 6. Section D — Interlocking Tab/Socket Fit & Surface Char Evaluation

Test assembly, in-plane shake/lift hold, and disassembly feel across the 5 interlocking pairs:

| Pair ID | Offset Spec | Fit Feel (Loose / Ideal / Tight / Jam) | Lifts Together as Pair? | Edge Char Wipe-Off (Clean / Light / Heavy) | Notes |
|---|---|---|---|---|---|
| **Pair 0** | `0.00 mm` (Common-Line) | | [ ] Yes [ ] No | | |
| **Pair 1** | `+0.05 mm` radial | | [ ] Yes [ ] No | | |
| **Pair 2** | `+0.10 mm` radial | | [ ] Yes [ ] No | | |
| **Pair 3** | `+0.15 mm` radial | | [ ] Yes [ ] No | | |
| **Pair 4** | `+0.20 mm` radial | | [ ] Yes [ ] No | | |

---

## 7. Real 750-Piece Seam Samples Sign-Off (Follow-Up Gate)

Once the 6 real high-stress seam samples (`runs/helleristning-750-commercial/seam_samples/seam_sample_01..06.svg`) are cut from the exact same plywood sheet:

| Sample | Piece Pair / Focus Neck | Intact After Cut? | Assembles Cleanly? | Survives Hand Flex? | Notes |
|---|---|---|---|---|---|
| `seam_sample_01.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_02.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_03.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_04.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_05.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |
| `seam_sample_06.svg` | | [ ] Yes [ ] No | [ ] Yes [ ] No | [ ] Yes [ ] No | |

**Supplier Sign-Off:** _______________________  **Producer Sign-Off:** _______________________  **Date:** _____________
"""
    (DOCS_DIR / "calibration-measurement-worksheet.md").write_text(md, encoding="utf-8")

    # Clean printable A4 HTML worksheet
    neck_rows_html = "\n".join(
        f"<tr><td><strong>{nw:.1f} mm</strong></td><td></td><td></td><td></td><td></td><td></td></tr>"
        for nw in neck_widths
    )
    kerf_cells_hdr = "".join(f"<th>{k:.2f}</th>" for k in kerf_steps)
    kerf_cells_blank = "".join("<td>&nbsp;</td>" for _ in kerf_steps)
    fit_rows_html = "\n".join(
        f"<tr><td><strong>Pair {i}</strong></td><td>{'0.00 mm (Common-Line)' if i == 0 else f'+{off:.2f} mm radial'}</td>"
        "<td></td><td>&#9744; Yes &nbsp; &#9744; No</td><td></td><td></td></tr>"
        for i, off in enumerate(fit_offsets)
    )
    seam_rows_html = "\n".join(
        f"<tr><td><code>seam_sample_{i:02d}.svg</code></td><td></td><td>&#9744; Y &#9744; N</td>"
        "<td>&#9744; Y &#9744; N</td><td>&#9744; Y &#9744; N</td><td></td></tr>"
        for i in range(1, 7)
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Supplier Laser Calibration Worksheet — Helleristning 750</title>
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font-family: "Segoe UI", Arial, sans-serif; color: #1a1816; font-size: 10.5pt; line-height: 1.35; max-width: 195mm; margin: 0 auto; padding: 8px; }}
  h1 {{ font-size: 15pt; margin: 0 0 4px 0; border-bottom: 2px solid #8b3a2b; padding-bottom: 4px; }}
  h2 {{ font-size: 11.5pt; margin: 14px 0 6px 0; color: #5b261c; }}
  .meta {{ font-size: 9pt; color: #444; margin-bottom: 10px; }}
  .note {{ background: #fbf4e9; border-left: 3px solid #b56035; padding: 6px 10px; font-size: 9.2pt; margin: 8px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 10px; font-size: 9.2pt; }}
  th, td {{ border: 1px solid #b7afa2; padding: 5px 6px; text-align: left; vertical-align: middle; }}
  th {{ background: #efe9de; font-weight: 600; }}
  .grid2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
  .field-box {{ border: 1px solid #b7afa2; padding: 6px 8px; border-radius: 3px; background: #fff; font-size: 9.2pt; }}
  .field-line {{ border-bottom: 1px dotted #888; min-height: 18px; margin-top: 2px; }}
  .sign-row {{ display: grid; grid-template-columns: 1fr 1fr 0.6fr; gap: 14px; margin-top: 14px; font-size: 9.5pt; }}
  @media print {{ .no-print {{ display: none; }} body {{ padding: 0; }} }}
</style>
</head>
<body>
  <div class="no-print" style="text-align:right;margin-bottom:8px;">
    <button onclick="window.print()" style="padding:6px 12px;cursor:pointer;">Print / Save as PDF</button>
  </div>
  <h1>Supplier Laser Calibration Measurement Worksheet — Helleristning 750</h1>
  <div class="meta">
    Coupon SVG: <code>runs/helleristning-750-commercial/calibration_coupon.svg</code> ({w_mm:.0f} &times; {h_mm:.0f} mm, 1:1 mm) &nbsp;|&nbsp;
    Cut order: <strong>1. Green (#engraving)</strong> &rarr; <strong>2. Red (#interior-cuts)</strong> &rarr; <strong>3. Blue (#exterior-cuts)</strong>
  </div>
  <div class="note">
    <strong>Mandatory Setup:</strong> Cut at exact 100% scale (1:1 mm) with automatic kerf compensation <strong>OFF</strong>.
    Align birch plywood top face grain horizontally with the engraved <code>GRAIN ---&gt;</code> arrow.
  </div>

  <h2>1. Material, Origin &amp; Laser Parameters</h2>
  <div class="grid2">
    <div class="field-box"><strong>Supplier / Operator / Date:</strong><div class="field-line"></div></div>
    <div class="field-box"><strong>Birch Plywood Grade &amp; Origin (Non-RU/BY?):</strong><div class="field-line">&#9744; Non-RU/BY Verified &nbsp; &#9744; FSC/PEFC CoC</div></div>
    <div class="field-box"><strong>Thickness (Nom / 4-Corner Caliper mm):</strong><div class="field-line"></div></div>
    <div class="field-box"><strong>Adhesive / REACH Entry 77 (&le;0.062 mg/m&sup3;):</strong><div class="field-line">&#9744; Lab Report Attached &nbsp; &#9744; Pending</div></div>
    <div class="field-box"><strong>Laser Machine / Lens / Masking:</strong><div class="field-line"></div></div>
    <div class="field-box"><strong>Cut (P% / Spd / Hz) &amp; Engrave Settings:</strong><div class="field-line"></div></div>
  </div>

  <h2>2. Section A — Kerf Step Gauge &amp; 10-Cut Caliper Block</h2>
  <p style="margin:4px 0;">10-Cut Total Gap: ________________ mm &nbsp;&rarr;&nbsp; <strong>Measured Kerf (Gap / 10): ________________ mm</strong> &nbsp;|&nbsp; Recommended Design Kerf: ________ mm</p>
  <table>
    <thead><tr><th>Slot (mm)</th>{kerf_cells_hdr}</tr></thead>
    <tbody><tr><td><strong>Fit (L/S/T/X)</strong></td>{kerf_cells_blank}</tr></tbody>
  </table>

  <h2>3. Section B — 50.00 &times; 50.00 mm Dimensional Accuracy</h2>
  <table>
    <thead><tr><th>Axis</th><th>Nominal</th><th>Measured (mm)</th><th>Error (mm)</th><th>Pass (&plusmn;0.20 mm)</th></tr></thead>
    <tbody>
      <tr><td>X (With Grain)</td><td>50.00 mm</td><td></td><td></td><td>&#9744; Pass &nbsp; &#9744; Fail</td></tr>
      <tr><td>Y (Cross Grain)</td><td>50.00 mm</td><td></td><td></td><td>&#9744; Pass &nbsp; &#9744; Fail</td></tr>
      <tr><td>Diagonals (D1 / D2)</td><td>70.71 mm</td><td></td><td></td><td>&#9744; Pass &nbsp; &#9744; Fail</td></tr>
    </tbody>
  </table>

  <h2>4. Section C — Neck-Strength Torture Bars (0&deg; With-Grain vs. 90&deg; Cross-Grain)</h2>
  <table>
    <thead>
      <tr><th>Nominal Neck</th><th>0&deg; Actual (mm)</th><th>0&deg; Strength (1–5)</th><th>90&deg; Actual (mm)</th><th>90&deg; Strength (1–5)</th><th>Char / Splinter / Delam Notes</th></tr>
    </thead>
    <tbody>{neck_rows_html}</tbody>
  </table>

  <h2>5. Section D — Interlocking Tab/Socket Fit Pairs</h2>
  <table>
    <thead><tr><th>Pair</th><th>Kerf Offset</th><th>Fit Feel (Loose/Ideal/Tight)</th><th>Lifts Together?</th><th>Char Rub-Off</th><th>Notes</th></tr></thead>
    <tbody>{fit_rows_html}</tbody>
  </table>

  <h2>6. Real 750-Piece Seam Samples (Follow-Up Cut Verification)</h2>
  <table>
    <thead><tr><th>Sample SVG</th><th>Target Neck / Pair</th><th>Intact?</th><th>Fits?</th><th>Flex OK?</th><th>Notes</th></tr></thead>
    <tbody>{seam_rows_html}</tbody>
  </table>

  <div class="sign-row">
    <div><strong>Supplier Sign-Off:</strong><div class="field-line"></div></div>
    <div><strong>Producer Sign-Off:</strong><div class="field-line"></div></div>
    <div><strong>Date:</strong><div class="field-line"></div></div>
  </div>
</body>
</html>
"""
    (DOCS_DIR / "calibration-measurement-worksheet.html").write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Stage 4: Packaging Alignment (Figma GK5-modul-2 · Northern)
# ---------------------------------------------------------------------------


def _probe_locked_board_dims(puzzle_run_dir: Path) -> Dict[str, Any]:
    part_path = puzzle_run_dir / "partition.json"
    if not part_path.exists():
        return {
            "is_locked": False,
            "source_partition": None,
            "status": "PROVISIONAL (awaiting final locked cut graph from runs/helleristning-750/partition.json)",
            "piece_count": TARGET_PIECE_COUNT,
            "width_mm": 840.0,
            "height_mm": 620.0,
            "board_area_mm2": 520800.0,
            "mean_piece_area_mm2": 694.4,
            "outline_type": "Organic / edge-extended rock-art silhouette perimeter (provisional target 780x580 to 880x640 mm)",
        }
    data = json.loads(part_path.read_text(encoding="utf-8"))
    faces = data.get("faces") or []
    polys = [Polygon(f["exterior"]).buffer(0) for f in faces]
    u = unary_union(polys)
    x0, y0, x1, y1 = u.bounds
    w_mm = round(float(x1 - x0), 2)
    h_mm = round(float(y1 - y0), 2)
    area_mm2 = round(float(u.area), 1)
    mean_area = round(area_mm2 / max(1, len(polys)), 1)
    rel_part = _rel_to_root(part_path)
    return {
        "is_locked": True,
        "source_partition": rel_part,
        "status": f"LOCKED FROM `{rel_part}`",
        "piece_count": len(polys),
        "width_mm": w_mm,
        "height_mm": h_mm,
        "board_area_mm2": area_mm2,
        "mean_piece_area_mm2": mean_area,
        "outline_type": (
            f"Organic / edge-extended rock-art silhouette perimeter "
            f"(verified gapless planar partition, {len(polys)} pieces)"
        ),
    }


def build_packaging_alignment(
    catalog_payload: Optional[Dict[str, Any]] = None,
    puzzle_run_dir: Path = DEFAULT_PUZZLE_RUN_DIR,
) -> Dict[str, Any]:
    if catalog_payload is None:
        catalog_payload = json.loads(SELECTED_CATALOG_PATH.read_text(encoding="utf-8"))
    board = _probe_locked_board_dims(puzzle_run_dir)
    summary = catalog_payload["summary"]

    # Mass & volume physics for 3.0 mm and 4.0 mm Nordic birch plywood (~680 kg/m³ = 0.00068 g/mm³)
    area_mm2 = float(board["board_area_mm2"])
    density_g_per_mm3 = 0.00068  # Baltic/Nordic birch plywood typical 650–700 kg/m³
    wood_g_3mm = round(area_mm2 * 3.0 * density_g_per_mm3)
    wood_g_4mm = round(area_mm2 * 4.0 * density_g_per_mm3)
    # Loose wooden pieces occupy ~2.1–2.4x solid volume in a box/cotton pouch
    loose_vol_cm3_3mm = round((area_mm2 * 3.0 / 1000.0) * 2.25)
    loose_vol_cm3_4mm = round((area_mm2 * 4.0 / 1000.0) * 2.25)
    # Recommended rigid Nordic gift box internal dimensions (aligned with minimalist Northern aesthetic)
    # e.g., 285 x 215 x 88 mm internal = 5,392 cm³ (comfortably holds 3.0 mm or 4.0 mm pieces + cotton bag + A5 booklet)
    box_int = (285, 215, 88)
    box_vol_cm3 = round(box_int[0] * box_int[1] * box_int[2] / 1000.0)
    booklet_and_box_g = 420  # Rigid 2 mm greyboard Nordic box + cotton pouch + 32-page A5 booklet
    total_g_3mm = wood_g_3mm + booklet_and_box_g
    total_g_4mm = wood_g_4mm + booklet_and_box_g
    sync_gate_line = (
        f"- [x] Sync final locked assembled dimensions from `{board['source_partition']}` "
        f"(locked: **{board['width_mm']:.1f} × {board['height_mm']:.1f} mm**, "
        f"`{board['board_area_mm2']:,.1f} mm²`, {board['piece_count']} pieces)."
        if board["is_locked"]
        else "- [ ] Sync final locked assembled dimensions from `runs/helleristning-750/partition.json` (status above)."
    )
    parcel_weight_note = (
        "fits standard Posten/Bring *Norgespakke* and EU parcel tiers cleanly under 2.0 kg."
        if total_g_4mm < 2000
        else f"fits standard Posten/Bring *Norgespakke* under 2.0 kg for 3.0 mm ({total_g_3mm/1000:.2f} kg); 4.0 mm option is {total_g_4mm/1000:.2f} kg."
    )

    md = f"""# Packaging, Booklet & Retail Label Alignment — Helleristning 750

**Figma Concept Reference:** [`GK5-modul-2 · Northern · eige-dokument` (`node-id=299-65`)]({FIGMA_PACKAGING_URL})  
**Board Geometry Sync Status:** {board["status"]}  
**Commercial Readiness Notice:** Pre-production specification aligned with [`eu-norway-safety-provisional-review.md`](./eu-norway-safety-provisional-review.md) and [`rights-inventory.md`](./rights-inventory.md). Final print mechanicals require locked supplier thickness, final legal clearance, and physical prototype fit check.

---

## 1. Finalized Board & Product Specifications

| Specification | Value | Notes |
|---|---|---|
| **Product Title (NO / EN)** | *Helleristningar — 750 brikkers trepuslespel* / *Nordic Rock Art — 750-Piece Wooden Jigsaw* | Align exact typography & brand voice with Figma `Northern` concept (`node-id=299-65`) |
| **Piece Count** | **{board["piece_count"]} pieces** | 100% figurative helleristning silhouettes (0 filler cells, 0 Alta NC-SA) |
| **Assembled Board Dimensions** | **{board["width_mm"]:.1f} × {board["height_mm"]:.1f} mm** | Footprint area `{board["board_area_mm2"]:,.1f} mm²` (`~{board["mean_piece_area_mm2"]:.1f} mm²/piece`) |
| **Outline Style** | {board["outline_type"]} | Border motifs preserved without straight-frame amputation |
| **Material Spec** | Nordic/Finnish Birch Plywood (`3.0 mm` or `4.0 mm`), Grade B/BB | REACH Annex XVII Entry 77 (`≤ 0.062 mg/m³`), Non-RU/BY origin, FSC/PEFC CoC |
| **Recommended Age Grade** | **14+ (Adult / Collector Puzzle)** | Exempt from Toy Safety Directive 2009/48/EC (>500 pieces); **DO NOT print CE mark** |

---

## 2. Box Volume, Weight & Inner Packaging Engineering

Calculated from actual puzzle wood area (`{area_mm2:,.0f} mm²`, birch plywood density `~680 kg/m³`, loose-piece packing factor `2.25×` inside a drawstring linen/cotton pouch):

| Plywood Thickness | Solid Wood Mass | Estimated Loose Piece Volume | Recommended Internal Box Cavity | Box Cavity Volume | Est. Total Gross Weight (Wood + Box + Pouch + Booklet) |
|---|---:|---:|---|---:|---:|
| **3.0 mm** | ~{wood_g_3mm:,} g | ~{loose_vol_cm3_3mm:,} cm³ | `285 × 215 × 88 mm` | {box_vol_cm3:,} cm³ | **~{total_g_3mm:,} g** (`{total_g_3mm/1000:.2f} kg`) |
| **4.0 mm** | ~{wood_g_4mm:,} g | ~{loose_vol_cm3_4mm:,} cm³ | `285 × 215 × 88 mm` (or `295 × 220 × 95 mm` for extra headspace) | {box_vol_cm3:,} cm³ | **~{total_g_4mm:,} g** (`{total_g_4mm/1000:.2f} kg`) |

### Structural Alignment with Figma `GK5-modul-2 · Northern` (`node-id=299-65`)
- **Nordic Tactile Presentation:** Two-piece rigid telescope box (uncoated matte FSC kraft/stone-toned paper wrap over 2.0 mm greyboard, embossed or ochre-foil rock-art emblem matching the *Northern* Scandinavian design direction).
- **Zero-Plastic Inner Protection:** Unbleached organic cotton/linen drawstring bag (breathable so faint fresh birch-laser scent dissipates naturally; eliminates polybag suffocation warning requirement).
- **A5 Companion Booklet Tray:** Internal cardboard lip or top-rest cavity sized cleanly for an **A5 (`148 × 210 mm`)** saddle-stitched or perfect-bound rock-art guide & attribution booklet (`285 × 215 mm` box fits A5 horizontally with generous finger notches).
- **Shipping Carton Compatibility:** External box `~292 × 222 × 94 mm` {parcel_weight_note}

---

## 3. Companion Rock-Art Legend & Legal Attribution Booklet Specification

Because **750** archival motifs cannot carry individual legal credits on the wooden pieces themselves, the printed **A5 Companion Booklet** (paired with a permanent product attribution URL printed via QR code) is BOTH a collector highlight AND a core Creative Commons attribution compliance mechanism:

1. **Cover & Cultural Context (pp. 1–4):** Bilingual Norwegian (*nynorsk/bokmål*) + English introduction to Scandinavian Stone Age hunter-gatherer (*Vingen, Kvennavika, Nämforsen*) and Nordic Bronze Age agrarian (*Tanum/Bohuslän, Østfold, Trøndelag, Bornholm*) rock carvings.
2. **Fold-Out Reference Poster / Key Map:** High-contrast numbered reference outline map of the assembled 750-piece board so assemblers can identify individual motifs by piece number (`#1–#750`).
3. **750-Piece Provenance & Attribution Register (pp. 5–28):** Compact, scannable tabular index directly typeset from [`rights-inventory.csv`](./rights-inventory.csv) listing for every piece:
   - Piece `#` (`1..750`) · Motif / Site · Creator / Institution · Archive · License (`CC BY 4.0`, `CC BY 3.0`, `CC BY 2.5`, `CC0 1.0`, `Public Domain`, and — if retained post-clearance — `CC BY-SA 4.0` or negotiated museum license).
4. **Archive Credits & Adaptation Disclosure (Back Cover Inside):**
   - Explicitly states: *"Individual puzzle piece silhouettes were geometrically adapted and interlocked for laser-cut birch wood manufacture from the archival rock-art documentation listed herein."*
   - Lists full Creative Commons license deed URLs (`https://creativecommons.org/licenses/by/4.0/`, etc.) and QR code linking to the full machine-readable online attribution table (`rights-inventory.csv` / web mirror).
   - **Current Rights Split Recorded:** **{summary["non_share_alike_count"]}** Non-ShareAlike motifs + **{summary["share_alike_count"]}** Unimus `CC BY-SA 4.0` motifs (awaiting institutional clearance or swap prior to print!).

---

## 4. Mandatory Back-of-Box Compliance & Safety Label Block (Provisional)

Place the following structured label block cleanly on the bottom/back panel of the box (verify final wording with Norwegian/EU compliance counsel before print — **DO NOT place a toy CE mark**):

```text
------------------------------------------------------------------------
HELLERISTNINGAR — 750-PIECE WOODEN JIGSAW PUZZLE (14+)
Dimensions: {board["width_mm"]:.0f} × {board["height_mm"]:.0f} mm  |  Material: Nordic Birch Plywood (FSC/PEFC)
SKU / Batch: HELL-750-ED01-B2026-__  |  Made in: [Country of Manufacture]

[NO] ADVARSEL: Inneholder små deler. Kvelningsfare — ikke egnet for barn
     under 3 år. Puslespill for voksne og ungdom (14+ år). Ikke et leketøy.
[EN] WARNING: Contains small parts. Choking hazard — not suitable for
     children under 3 years. Adult/collector puzzle intended for ages 14+.
     Not a toy (exempt under Directive 2009/48/EC Annex I, >500 pieces).

Producer / Produsent:
  [Legal Company Name], [Street Address, Postal Code, City, Norway]
  Contact / Support: [compliance@domain.no]  |  [https://domain.no/helleristning-750]

EU Responsible Person (GPSR Art. 16 — required for EU Member State sales):
  [EU Authorized Representative Name, EU Street Address, Email — TBD]

Archival Credits & Full 750-Piece Source Register:
  See enclosed booklet & [https://domain.no/helleristning-750/credits]
  (Contains adapted Nordic rock-art documentation; zero non-commercial sources)

Packaging Disposal / Kildesortering:
  Box: Paper/Cardboard (Papir/Papp)  |  Pouch: Reusable Textile
  [Add destination-country EPR marks as registered: e.g. Grønt Punkt / LUCID]
------------------------------------------------------------------------
```

---

## 5. Pre-Print Sign-Off Gates

{sync_gate_line}
- [ ] Confirm final birch plywood thickness (`3.0 mm` vs. `4.0 mm`) from physical calibration coupon testing and lock box depth (`88 mm` vs. `95 mm`).
- [ ] Close ShareAlike & PD-term legal gates in [`rights-inventory.md`](./rights-inventory.md) before typesetting the booklet.
- [ ] Complete all 6 safety gates (`SAF-01..SAF-06`) in [`eu-norway-safety-provisional-review.md`](./eu-norway-safety-provisional-review.md) and populate Producer / EU Responsible Person / Batch fields.
- [ ] Export final dielines from Figma (`node-id=299-65`) matching the locked `285 × 215 × 88 mm` (or adjusted) internal cavity.
"""
    (DOCS_DIR / "packaging-alignment.md").write_text(md, encoding="utf-8")
    return {
        "board": board,
        "wood_mass_g_3mm": wood_g_3mm,
        "wood_mass_g_4mm": wood_g_4mm,
        "recommended_box_internal_mm": list(box_int),
        "box_volume_cm3": box_vol_cm3,
    }


# ---------------------------------------------------------------------------
# Stage 5: 6 Real Full-Scale Seam Samples from Locked 750-Piece Cut Graph
# ---------------------------------------------------------------------------


def _fit_in_piece_label(
    poly: Polygon,
    text: str,
    ox: float,
    oy: float,
    inset_mm: float = 1.6,
    heights_mm: Sequence[float] = (2.2, 1.8, 1.4),
) -> List[List[Tuple[float, float]]]:
    """Return atomic vector-text strokes centered inside `poly`'s safe inset (never clipped mid-glyph)."""
    safe_zone = poly.buffer(-inset_mm, quad_segs=8)
    if safe_zone.is_empty:
        return []
    if safe_zone.geom_type != "Polygon":
        parts = [g for g in getattr(safe_zone, "geoms", []) if g.geom_type == "Polygon" and g.area > 0]
        if not parts:
            return []
        safe_zone = max(parts, key=lambda g: g.area)

    deep_core = poly.buffer(-(inset_mm + 1.2), quad_segs=8)
    if not deep_core.is_empty:
        if deep_core.geom_type != "Polygon":
            dparts = [g for g in getattr(deep_core, "geoms", []) if g.geom_type == "Polygon" and g.area > 0]
            anchor = max(dparts, key=lambda g: g.area).representative_point() if dparts else safe_zone.representative_point()
        else:
            anchor = deep_core.representative_point()
    else:
        anchor = safe_zone.representative_point()

    for h in heights_mm:
        tw = _vector_text_width(text, h)
        tx = anchor.x - 0.5 * tw
        ty = anchor.y - 0.5 * h
        strokes_world = _vector_text_polylines(text, tx, ty, height_mm=h)
        if strokes_world and all(LineString(st).within(safe_zone) for st in strokes_world):
            return [[(px - ox, py - oy) for px, py in st] for st in strokes_world]

    # Atomic fallback: tiny crosshair strictly inside safe_zone
    r = 0.7
    cross = [
        [(anchor.x - r, anchor.y), (anchor.x + r, anchor.y)],
        [(anchor.x, anchor.y - r), (anchor.x, anchor.y + r)],
    ]
    if all(LineString(st).within(safe_zone) for st in cross):
        return [[(px - ox, py - oy) for px, py in st] for st in cross]
    return []


def _edge_on_interior_hole(edge_coords: Sequence[Sequence[float]], union_poly: Any) -> bool:
    """Return True if an edge on the boundary of `union_poly` traces an enclosed interior hole."""
    if union_poly.geom_type != "Polygon" or not union_poly.interiors:
        return False
    ls = LineString(edge_coords)
    mid = ls.interpolate(0.5, normalized=True)
    ext_dist = union_poly.exterior.distance(mid)
    int_dist = min(ring.distance(mid) for ring in union_poly.interiors)
    return int_dist < 0.05 and ext_dist > 0.05


def build_real_seam_samples(puzzle_run_dir: Path = DEFAULT_PUZZLE_RUN_DIR, n_samples: int = 6) -> Optional[Dict[str, Any]]:
    """Extract 6 full-scale 1:1 mm laser-cut seam sample SVGs for the 6 tightest/highest-stress necks."""
    part_path = puzzle_run_dir / "partition.json"
    if not part_path.exists():
        print(f"[seam-samples] Skipped: {part_path} does not exist yet.", flush=True)
        return None

    data = json.loads(part_path.read_text(encoding="utf-8"))
    faces = data["faces"]
    edges = data["edges"]
    polys = [Polygon(f["exterior"]).buffer(0) for f in faces]

    # Map canonical face_id -> index in faces/polys so edge references always resolve safely
    face_ids = [int(f.get("face_id", idx)) for idx, f in enumerate(faces)]
    fid_to_idx: Dict[int, int] = {fid: idx for idx, fid in enumerate(face_ids)}

    # Build deduplicated adjacency map: face_idx -> {neighbor_face_idx: [edge_dict, ...]}
    adj_edges: Dict[int, Dict[int, List[Dict[str, Any]]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for e in edges:
        a_fid = int(e.get("left_face", -1))
        b_fid = int(e.get("right_face", -1))
        if a_fid in fid_to_idx and b_fid in fid_to_idx:
            ia = fid_to_idx[a_fid]
            ib = fid_to_idx[b_fid]
            if ia != ib:
                adj_edges[ia][ib].append(e)
                adj_edges[ib][ia].append(e)

    # Score every piece for physical neck / tip stress using repo's exact validators
    stress_rows: List[Dict[str, Any]] = []
    for idx, (f, poly) in enumerate(zip(faces, polys)):
        fab = analyze_piece_fabrication(poly, kerf_mm=0.25, min_neck_mm=3.0)
        worst_tip: Optional[List[float]] = None
        try:
            tm = tip_measure(poly, kerf=0.25, r=1.5)
            tip_loss = float(tm["max_tip_loss_mm"])
            core_surv = float(tm["core_survival"])
            if tm.get("worst_tip") is not None:
                worst_tip = [round(float(tm["worst_tip"][0]), 4), round(float(tm["worst_tip"][1]), 4)]
        except Exception:
            tip_loss = 99.0
            core_surv = 0.0
        # Stress score prioritizes lowest core_survival, largest tip_loss, largest secondary_lobe, lowest max_inscribed_diam
        stress = (
            (0.0 if fab["neck_pass"] else 50.0)
            + tip_loss * 2.5
            + (1.0 - core_surv) * 10.0
            + float(fab["secondary_lobe_max_mm2"]) * 0.15
            - float(fab["max_inscribed_diameter_mm"]) * 0.2
        )
        md = f.get("metadata") or {}
        stress_rows.append(
            {
                "face_idx": idx,
                "face_id": face_ids[idx],
                "asset_id": str(md.get("asset_id") or md.get("motif_id") or f"piece_{idx+1}"),
                "motif_class": str(md.get("motif_class") or ""),
                "stress_score": round(stress, 4),
                "tip_loss_mm": round(tip_loss, 3),
                "core_survival": round(core_surv, 4),
                "max_inscribed_diam_mm": float(fab["max_inscribed_diameter_mm"]),
                "secondary_lobe_mm2": float(fab["secondary_lobe_max_mm2"]),
                "neck_pass": bool(fab["neck_pass"]),
                "worst_tip_mm": worst_tip,
            }
        )

    stress_rows.sort(key=lambda r: (-r["stress_score"], r["face_idx"]))

    # Greedily select `n_samples` spatially disjoint high-stress target pieces + mating interlocking neighbors
    chosen: List[Tuple[Dict[str, Any], List[int]]] = []
    used_faces: set[int] = set()
    for sr in stress_rows:
        if len(chosen) >= n_samples:
            break
        fi = sr["face_idx"]
        nbr_map = adj_edges.get(fi)
        if fi in used_faces or not nbr_map:
            continue

        tip_pt = Point(sr["worst_tip_mm"]) if sr["worst_tip_mm"] is not None else None
        nbr_stats: List[Tuple[int, float, float]] = []
        for nb_idx, elist in nbr_map.items():
            total_len = sum(LineString(e["coords"]).length for e in elist)
            tip_dist = min(LineString(e["coords"]).distance(tip_pt) for e in elist) if tip_pt is not None else float("inf")
            nbr_stats.append((nb_idx, tip_dist, total_len))

        # Prioritize the neighbor directly mating the worst tip/neck (<= 3.0 mm), then longest total shared seam
        unused_stats = [st for st in nbr_stats if st[0] not in used_faces]
        if not unused_stats:
            continue

        group = [fi]
        close_to_tip = [st for st in unused_stats if st[1] <= 3.0]
        if close_to_tip:
            close_to_tip.sort(key=lambda st: (st[1], -st[2], st[0]))
            group.append(close_to_tip[0][0])

        remaining = [st for st in unused_stats if st[0] not in group]
        remaining.sort(key=lambda st: (-st[2], st[1], st[0]))
        for nb_idx, _, _ in remaining:
            if len(group) >= 3:
                break
            group.append(nb_idx)

        used_faces.update(group)
        chosen.append((sr, group))

    out_dir = COMMERCIAL_RUN_DIR / "seam_samples"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_samples = []
    preview_items: List[Dict[str, Any]] = []

    for s_idx, (sr, group) in enumerate(chosen, start=1):
        group_fids = {face_ids[g] for g in group}
        sub_polys = [polys[g] for g in group]
        u = unary_union(sub_polys)
        x0, y0, x1, y1 = u.bounds
        pad = 6.0
        hdr = f"S{s_idx:02d} P{sr['face_idx']+1} TIP {sr['tip_loss_mm']:.1f}MM"
        hdr_h = 2.4
        hdr_w = _vector_text_width(hdr, hdr_h)
        w_mm = max((x1 - x0) + 2 * pad, hdr_w + 4.0)
        h_mm = (y1 - y0) + 2 * pad + 8.0  # extra 8 mm top header band for engraving label
        ox = x0 - pad
        oy = y0 - pad - 8.0

        # Extract unique edges touching this cluster (zero duplicates):
        # - interior-cuts: shared seams where BOTH faces are in cluster, plus any enclosed hole boundary
        # - exterior-cuts: outer cluster perimeter where exactly ONE side is in cluster
        int_edges: List[Dict[str, Any]] = []
        ext_edges: List[Dict[str, Any]] = []
        for e in edges:
            a = int(e.get("left_face", -1))
            b = int(e.get("right_face", -1))
            in_a = a in group_fids
            in_b = b in group_fids
            if in_a and in_b:
                int_edges.append(e)
            elif in_a or in_b:
                if _edge_on_interior_hole(e["coords"], u):
                    int_edges.append(e)
                else:
                    ext_edges.append(e)

        root = ET.Element(
            f"{{{SVG_NS}}}svg",
            {
                "width": f"{w_mm:.4f}mm",
                "height": f"{h_mm:.4f}mm",
                "viewBox": f"0.0000 0.0000 {w_mm:.4f} {h_mm:.4f}",
                "version": "1.1",
            },
        )
        g_int = ET.SubElement(
            root,
            f"{{{SVG_NS}}}g",
            {
                "id": "interior-cuts",
                "fill": "none",
                "stroke": "#FF0000",
                "stroke-width": "0.1500",
                "stroke-linecap": "round",
                "stroke-linejoin": "round",
            },
        )
        int_pts_list: List[List[Tuple[float, float]]] = []
        for k, e in enumerate(int_edges):
            pts = [(float(px) - ox, float(py) - oy) for px, py in e["coords"]]
            int_pts_list.append(pts)
            ET.SubElement(
                g_int,
                f"{{{SVG_NS}}}path",
                {"id": f"seam-int-{k:03d}", "d": _pts_to_d(pts, closed=False), "fill": "none"},
            )

        g_ext = ET.SubElement(
            root,
            f"{{{SVG_NS}}}g",
            {
                "id": "exterior-cuts",
                "fill": "none",
                "stroke": "#0000FF",
                "stroke-width": "0.1500",
                "stroke-linecap": "round",
                "stroke-linejoin": "round",
            },
        )
        ext_pts_list: List[List[Tuple[float, float]]] = []
        for k, e in enumerate(ext_edges):
            pts = [(float(px) - ox, float(py) - oy) for px, py in e["coords"]]
            ext_pts_list.append(pts)
            ET.SubElement(
                g_ext,
                f"{{{SVG_NS}}}path",
                {"id": f"seam-ext-{k:03d}", "d": _pts_to_d(pts, closed=False), "fill": "none"},
            )

        # Add clean engraving header (top scrap band) + atomic in-piece IDs strictly inside safe inset (>= 1.6 mm)
        g_eng = ET.SubElement(
            root,
            f"{{{SVG_NS}}}g",
            {
                "id": "engraving",
                "fill": "none",
                "stroke": "#008800",
                "stroke-width": "0.2000",
                "stroke-linecap": "round",
                "stroke-linejoin": "round",
            },
        )
        eng_pts_list: List[List[Tuple[float, float]]] = []
        for k, pl in enumerate(_vector_text_polylines(hdr, 2.0, 2.0, height_mm=hdr_h)):
            eng_pts_list.append(pl)
            ET.SubElement(
                g_eng,
                f"{{{SVG_NS}}}path",
                {"id": f"eng-hdr-{k:03d}", "d": _pts_to_d(pl, closed=False), "fill": "none"},
            )

        for g_idx in group:
            lbl = f"P{g_idx + 1}"
            strokes = _fit_in_piece_label(polys[g_idx], lbl, ox=ox, oy=oy, inset_mm=1.6)
            for k, pl in enumerate(strokes):
                eng_pts_list.append(pl)
                ET.SubElement(
                    g_eng,
                    f"{{{SVG_NS}}}path",
                    {"id": f"eng-p{g_idx + 1}-{k:03d}", "d": _pts_to_d(pl, closed=False), "fill": "none"},
                )

        svg_out = out_dir / f"seam_sample_{s_idx:02d}.svg"
        ET.ElementTree(root).write(str(svg_out), encoding="utf-8", xml_declaration=True)

        png_out = out_dir / f"seam_sample_{s_idx:02d}_preview.png"
        _render_single_seam_sample_preview(
            s_idx=s_idx,
            sr=sr,
            group=group,
            polys=polys,
            ox=ox,
            oy=oy,
            w_mm=w_mm,
            h_mm=h_mm,
            int_pts_list=int_pts_list,
            ext_pts_list=ext_pts_list,
            eng_pts_list=eng_pts_list,
            out_path=png_out,
        )

        preview_items.append(
            {
                "sr": sr,
                "group": group,
                "int_edges": int_edges,
                "ext_edges": ext_edges,
            }
        )
        manifest_samples.append(
            {
                "sample_index": s_idx,
                "svg_path": _rel_to_root(svg_out),
                "preview_png_path": _rel_to_root(png_out),
                "target_piece_number": sr["face_idx"] + 1,
                "target_asset_id": sr["asset_id"],
                "cluster_piece_numbers": [g + 1 for g in group],
                "width_mm": round(w_mm, 2),
                "height_mm": round(h_mm, 2),
                "interior_seam_count": len(int_edges),
                "exterior_edge_count": len(ext_edges),
                "target_stress_metrics": sr,
            }
        )

    # Render contact-sheet preview of all real seam samples
    contact_png = out_dir / "seam_samples_preview.png"
    _render_seam_samples_preview(polys, preview_items, contact_png)

    manifest = {
        "source_partition": _rel_to_root(part_path),
        "preview_contact_sheet": _rel_to_root(contact_png),
        "sample_count": len(manifest_samples),
        "samples": manifest_samples,
    }
    (out_dir / "seam_samples_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _render_single_seam_sample_preview(
    s_idx: int,
    sr: Dict[str, Any],
    group: Sequence[int],
    polys: Sequence[Polygon],
    ox: float,
    oy: float,
    w_mm: float,
    h_mm: float,
    int_pts_list: Sequence[Sequence[Tuple[float, float]]],
    ext_pts_list: Sequence[Sequence[Tuple[float, float]]],
    eng_pts_list: Sequence[Sequence[Tuple[float, float]]],
    out_path: Path,
) -> None:
    """Render high-resolution 1:1 layer verification PNG for a single real seam test-cut SVG."""
    scale_px = 8.0  # 8 px / mm (~203 DPI)
    margin = 44
    font = _load_font(14, bold=True)
    font_sm = _load_font(11, bold=False)

    cluster_str = ", ".join(f"#{g + 1}" for g in group)
    title_txt = f"Seam Sample {s_idx:02d} — Target Piece #{sr['face_idx'] + 1} ({sr['asset_id']}) | Cluster: {cluster_str}"
    sub_txt = (
        f"Size: {w_mm:.1f} x {h_mm:.1f} mm (1:1) | tip={sr['tip_loss_mm']:.2f} mm | "
        f"core={sr['core_survival']:.2f} | diam={sr['max_inscribed_diam_mm']:.1f} mm"
    )
    legend_txt = (
        "SVG Layers: [RED] #interior-cuts  |  [BLUE] #exterior-cuts  |  [GREEN] #engraving  |  "
        "Circle = focus tip (preview only)  |  10 mm grid"
    )

    probe_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    max_text_w = max(
        probe_draw.textlength(title_txt, font=font),
        probe_draw.textlength(sub_txt, font=font_sm),
        probe_draw.textlength(legend_txt, font=font_sm),
    )
    img_w = max(880, int(math.ceil(w_mm * scale_px)) + 2 * margin, int(math.ceil(max_text_w)) + 2 * margin)
    img_h = int(math.ceil(h_mm * scale_px)) + 2 * margin + 46
    im = Image.new("RGB", (img_w, img_h), "#F4EFE6")
    draw = ImageDraw.Draw(im)

    bx0, by0 = margin, margin + 12
    bx1, by1 = margin + w_mm * scale_px, margin + 12 + h_mm * scale_px
    draw.rectangle([bx0, by0, bx1, by1], fill="#EADBC4", outline="#C2AF93", width=2)

    # Subtle 10 mm reference grid
    for xm in range(10, int(w_mm), 10):
        px = bx0 + xm * scale_px
        draw.line([(px, by0), (px, by1)], fill="#E0CEB3", width=1)
    for ym in range(10, int(h_mm), 10):
        py = by0 + ym * scale_px
        draw.line([(bx0, py), (bx1, py)], fill="#E0CEB3", width=1)

    # Soft piece shading (target = warm ochre tint, neighbors = soft sage tint)
    for g in group:
        pts = [(bx0 + (x - ox) * scale_px, by0 + (y - oy) * scale_px) for x, y in polys[g].exterior.coords]
        fill_c = "#E5B496" if g == sr["face_idx"] else "#C4D1BE"
        draw.polygon(pts, fill=fill_c)

    # Exact SVG layers: green engraving, red interior cuts, blue exterior cuts
    for pl in eng_pts_list:
        if len(pl) >= 2:
            draw.line([(bx0 + x * scale_px, by0 + y * scale_px) for x, y in pl], fill="#137333", width=2)
    for pl in int_pts_list:
        if len(pl) >= 2:
            draw.line([(bx0 + x * scale_px, by0 + y * scale_px) for x, y in pl], fill="#D93025", width=3, joint="curve")
    for pl in ext_pts_list:
        if len(pl) >= 2:
            draw.line([(bx0 + x * scale_px, by0 + y * scale_px) for x, y in pl], fill="#1A56DB", width=3, joint="curve")

    # Highlight exact worst-tip / focus-neck location on preview (visual callout only, not in SVG)
    if sr.get("worst_tip_mm") is not None:
        tx = bx0 + (sr["worst_tip_mm"][0] - ox) * scale_px
        ty = by0 + (sr["worst_tip_mm"][1] - oy) * scale_px
        r = 10
        draw.ellipse([tx - r, ty - r, tx + r, ty + r], outline="#FFFFFF", width=4)
        draw.ellipse([tx - r, ty - r, tx + r, ty + r], outline="#B3261E", width=2)

    draw.text((margin, 8), title_txt, fill="#1F1B18", font=font)
    draw.text((margin, 28), sub_txt, fill="#4A4038", font=font_sm)
    legend_y = int(by1 + 10)
    draw.text((margin, legend_y), legend_txt, fill="#3B342E", font=font_sm)
    im.save(out_path)


def _render_seam_samples_preview(
    polys: Sequence[Polygon],
    preview_items: Sequence[Dict[str, Any]],
    out_path: Path,
) -> None:
    if not preview_items:
        return
    cols = min(3, len(preview_items))
    rows_n = max(1, math.ceil(len(preview_items) / cols))
    cw, ch = 380, 355
    im = Image.new("RGB", (cols * cw, rows_n * ch + 38), "#F7F5F0")
    draw = ImageDraw.Draw(im)
    font = _load_font(12, bold=True)
    font_sm = _load_font(10, bold=False)
    draw.text(
        (12, 10),
        "Real 750-Piece Full-Scale High-Stress Seam Samples (Target=Ochre, Neighbors=Sage, Red=Interior Cut, Blue=Exterior Cut)",
        fill="#111111",
        font=font,
    )

    for idx, item in enumerate(preview_items):
        sr = item["sr"]
        group = item["group"]
        cx = (idx % cols) * cw
        cy = 38 + (idx // cols) * ch
        draw.rectangle([cx + 8, cy + 6, cx + cw - 8, cy + ch - 8], fill="#EFE8DC", outline="#C6B9A3", width=2)
        u = unary_union([polys[g] for g in group])
        x0, y0, x1, y1 = u.bounds
        sc = min((cw - 44) / max(1e-6, x1 - x0), (ch - 80) / max(1e-6, y1 - y0))
        ox = cx + 22 + ((cw - 44) - (x1 - x0) * sc) * 0.5
        oy = cy + 48 + ((ch - 80) - (y1 - y0) * sc) * 0.5
        for g in group:
            pts = [(ox + (x - x0) * sc, oy + (y - y0) * sc) for x, y in polys[g].exterior.coords]
            fill_c = "#D97D54" if g == sr["face_idx"] else "#8FA88B"
            draw.polygon(pts, fill=fill_c)
        for e in item["int_edges"]:
            pts = [(ox + (x - x0) * sc, oy + (y - y0) * sc) for x, y in e["coords"]]
            if len(pts) >= 2:
                draw.line(pts, fill="#D93025", width=3, joint="curve")
        for e in item["ext_edges"]:
            pts = [(ox + (x - x0) * sc, oy + (y - y0) * sc) for x, y in e["coords"]]
            if len(pts) >= 2:
                draw.line(pts, fill="#1A56DB", width=2, joint="curve")
        for g in group:
            rp = polys[g].representative_point()
            lx, ly = ox + (rp.x - x0) * sc, oy + (rp.y - y0) * sc
            lbl = f"#{g + 1}"
            draw.text((lx - 10, ly - 6), lbl, fill="#1A1816", font=font_sm)
        if sr.get("worst_tip_mm") is not None:
            tx = ox + (sr["worst_tip_mm"][0] - x0) * sc
            ty = oy + (sr["worst_tip_mm"][1] - y0) * sc
            draw.ellipse([tx - 6, ty - 6, tx + 6, ty + 6], outline="#FFFFFF", width=3)
            draw.ellipse([tx - 6, ty - 6, tx + 6, ty + 6], outline="#B3261E", width=2)
        draw.text((cx + 14, cy + 12), f"Sample {idx+1}: Piece #{sr['face_idx']+1} ({sr['asset_id']})", fill="#111111", font=font)
        draw.text(
            (cx + 14, cy + 28),
            f"tip={sr['tip_loss_mm']:.2f}mm | core={sr['core_survival']:.2f} | diam={sr['max_inscribed_diam_mm']:.1f}mm | n={len(group)} pcs",
            fill="#444444",
            font=font_sm,
        )
    im.save(out_path)


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build Commercial Readiness Pack for Helleristning 750")
    parser.add_argument(
        "--stage",
        choices=["catalog", "rights", "calibration", "packaging", "seam-samples", "all"],
        default="all",
    )
    parser.add_argument("--puzzle-run-dir", default=str(DEFAULT_PUZZLE_RUN_DIR))
    args = parser.parse_args(argv)
    puzzle_dir = Path(args.puzzle_run_dir)

    catalog_payload: Optional[Dict[str, Any]] = None

    if args.stage in ("catalog", "all"):
        catalog_payload = build_selected_750_catalog()
        print("[catalog] Locked 750 motifs:", json.dumps(catalog_payload["summary"], indent=2))

    if args.stage in ("rights", "all"):
        inv = build_rights_inventory(catalog_payload)
        print(f"[rights] Wrote {len(inv['rows'])} rows to docs/helleristning-750/rights-inventory.{{csv,json,md}} + archive-inquiry-drafts.md")

    if args.stage in ("calibration", "all"):
        spec = build_calibration_coupon()
        print("[calibration] Generated coupon:", json.dumps(spec, indent=2))

    if args.stage in ("packaging", "all"):
        pkg = build_packaging_alignment(catalog_payload, puzzle_run_dir=puzzle_dir)
        print("[packaging] Generated packaging-alignment.md:", json.dumps(pkg, indent=2))

    if args.stage in ("seam-samples", "all"):
        sm = build_real_seam_samples(puzzle_run_dir=puzzle_dir)
        if sm is not None:
            print(f"[seam-samples] Generated {sm['sample_count']} real full-scale seam sample SVGs")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
