"""Reproducible scientific analysis of the Nordic Helleristninger corpus.

Reads:
  - helleristninger/scientific_record/figures.jsonl
  - helleristninger/scientific_record/svg/<figure_id>.svg
  - helleristninger/qa/dual_trace_report.json
  - helleristninger/qa/dual_trace_disagreements.jsonl
  - helleristninger/metadata/site_gazetteer.json (optional enrichment)
  - helleristninger/taxonomy.json (optional enrichment)

Produces:
  - helleristninger/science/findings_metrics.json
  - helleristninger/science/findings_v2026.W48.md
  - helleristninger/science/figures/fig1_corpus_regions_periods.png (+ .svg)
  - helleristninger/science/figures/fig2_morphometrics_veide_vs_jordbruk.png (+ .svg)
  - helleristninger/science/figures/fig3_regional_iconography_heatmap.png (+ .svg)
  - helleristninger/science/figures/fig4_dual_trace_reliability.png (+ .svg)
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import platform
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import scipy
from scipy import stats
from shapely.geometry import Polygon
from svgpathtools import parse_path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from helleristninger.science.plots import (  # noqa: E402
    plot_corpus_overview,
    plot_dual_trace_reliability,
    plot_morphometric_comparison,
    plot_regional_iconography,
)

CORE_REGIONS = [
    "Alta",
    "Østfold",
    "Bohuslän/Tanum",
    "Nämforsen",
    "Vingen",
    "Trøndelag",
]

RANDOM_SEED = 42
N_BOOTSTRAP = 2000


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if s:
                rows.append(json.loads(s))
    return rows


CORE_REGION_MAP = {
    "alta": "Alta",
    "ostfold": "Østfold",
    "bohuslan_tanum": "Bohuslän/Tanum",
    "namforsen": "Nämforsen",
    "vingen": "Vingen",
    "trondelag": "Trøndelag",
    "other": "Other Nordic",
}


def normalize_region(rec: Dict[str, Any], gazetteer_by_id: Dict[str, Dict[str, Any]]) -> str:
    """Map archival site/region strings cleanly to canonical core & neighbouring regions."""
    cr = str(rec.get("core_region") or "").strip().lower()
    if cr in CORE_REGION_MAP:
        return CORE_REGION_MAP[cr]
    site_id = str(rec.get("site_panel_id") or "")
    gaz = gazetteer_by_id.get(site_id, {})
    fields = " ".join(
        str(x or "")
        for x in (
            rec.get("region"),
            rec.get("site"),
            rec.get("site_name"),
            rec.get("title"),
            rec.get("site_panel_id"),
            gaz.get("region"),
            gaz.get("site_name"),
            gaz.get("municipality"),
            gaz.get("county"),
        )
    ).lower()

    if any(k in fields for k in ("alta", "hjemmeluft", "kåfjord", "kafjord", "storsteinen", "amtmannsnes", "bergbukten", "ole pedersen", "finnmark")):
        return "Alta"
    if any(k in fields for k in ("østfold", "ostfold", "begby", "kalnes", "solberg", "skjeberg", "fredrikstad", "sarpsborg", "tune", "borgen", "hornnes", "hafslund", "halden")):
        return "Østfold"
    if any(k in fields for k in ("bohuslän", "bohuslan", "tanum", "vitlycke", "aspeberget", "fossum", "litsleby", "tegneby", "kalleby", "askum", "tossene", "bottna", "svarteborg", "västra götaland", "vastra gotaland", "lysekil", "mellerud")):
        return "Bohuslän/Tanum"
    if any(k in fields for k in ("nämforsen", "namforsen", "ådals-liden", "adals-liden", "ångermanland", "angermanland", "brådön", "bradon", "notön", "noton", "laxön", "laxon")):
        return "Nämforsen"
    if any(k in fields for k in ("vingen", "bremanger", "vestland", "sogn og fjordane")):
        return "Vingen"
    if any(k in fields for k in ("trøndelag", "trondelag", "bardal", "leirfall", "bøla", "bola", "kvennavika", "evenhus", "ydstines", "hell", "stjørdal", "steinkjer", "verdal", "levanger", "skavberget", "hegra")):
        return "Trøndelag"
    return "Other Nordic"


def normalize_period(raw_period: Any) -> str:
    p = str(raw_period or "").strip().lower()
    if p in ("stone_age_hunter", "veideristninger", "veidekunst", "stone_age", "mesolithic", "neolithic_hunter"):
        return "stone_age_hunter"
    if p in ("bronze_age_agrarian", "jordbruksristninger", "bronze_age", "nordic_bronze_age"):
        return "bronze_age_agrarian"
    if p in ("mixed", "transitional", "multi_period"):
        return "mixed"
    return "unknown"


def _svg_geometry_metrics(svg_path: Path) -> Dict[str, Any]:
    """Parse closed <path> in #figures and count elements in #cup_marks, #cracks, #uncertain."""
    out: Dict[str, Any] = {
        "svg_valid": False,
        "svg_area": None,
        "svg_perimeter": None,
        "compactness": None,
        "solidity": None,
        "aspect_ratio": None,
        "vertex_count": None,
        "shape_complexity_index": None,
        "cup_marks_count": 0,
        "cracks_count": 0,
        "uncertain_count": 0,
        "has_scale_bar": False,
    }
    if not svg_path.exists():
        return out
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
    except Exception:
        return out

    def strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    fig_paths: List[str] = []
    for elem in root.iter():
        if strip_ns(elem.tag) == "g":
            gid = elem.attrib.get("id", "")
            if gid == "figures":
                for child in elem.iter():
                    if strip_ns(child.tag) == "path" and child.attrib.get("d"):
                        fig_paths.append(child.attrib["d"])
            elif gid == "cup_marks":
                out["cup_marks_count"] = sum(
                    1 for c in elem.iter() if strip_ns(c.tag) in ("circle", "ellipse", "path")
                )
            elif gid == "cracks":
                out["cracks_count"] = sum(
                    1 for c in elem.iter() if strip_ns(c.tag) in ("line", "polyline", "path")
                )
            elif gid == "uncertain":
                out["uncertain_count"] = sum(
                    1 for c in elem.iter() if strip_ns(c.tag) in ("line", "polyline", "path", "circle")
                )
            elif gid == "scale_bar":
                out["has_scale_bar"] = len(list(elem)) > 0

    if not fig_paths:
        return out

    # Take largest closed path polygon in #figures
    best_poly: Optional[Polygon] = None
    best_verts = 0
    for d in fig_paths:
        try:
            path_obj = parse_path(d)
            pts: List[Tuple[float, float]] = []
            for seg in path_obj:
                seg_name = type(seg).__name__
                if seg_name == "Line":
                    pts.append((seg.start.real, seg.start.imag))
                else:
                    n_samples = max(4, min(16, int(math.ceil(seg.length() / 1.5))))
                    for k in range(n_samples):
                        c = seg.point(k / n_samples)
                        pts.append((c.real, c.imag))
            if len(pts) < 3:
                continue
            poly = Polygon(pts)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.geom_type == "MultiPolygon":
                poly = max(poly.geoms, key=lambda g: g.area)
            if poly.area > 0 and (best_poly is None or poly.area > best_poly.area):
                best_poly = poly
                best_verts = len(pts)
        except Exception:
            continue

    if best_poly is None or best_poly.area <= 0:
        return out

    area = float(best_poly.area)
    perim = float(best_poly.length)
    minx, miny, maxx, maxy = best_poly.bounds
    bw = max(1e-6, maxx - minx)
    bh = max(1e-6, maxy - miny)
    aspect = max(bw, bh) / min(bw, bh)
    compactness = min(1.0, (4.0 * math.pi * area) / (perim * perim)) if perim > 0 else 0.0
    hull_area = float(best_poly.convex_hull.area)
    solidity = min(1.0, area / hull_area) if hull_area > 0 else 0.0
    complexity = perim / math.sqrt(area) if area > 0 else 0.0

    out.update({
        "svg_valid": True,
        "svg_area": area,
        "svg_perimeter": perim,
        "compactness": compactness,
        "solidity": solidity,
        "aspect_ratio": aspect,
        "vertex_count": best_verts,
        "shape_complexity_index": complexity,
    })
    return out


def cliffs_delta(x: np.ndarray, y: np.ndarray) -> float:
    """Exact Cliff's delta: P(X > Y) - P(X < Y)."""
    y_sorted = np.sort(y)
    n_less = np.searchsorted(y_sorted, x, side="left")
    n_le = np.searchsorted(y_sorted, x, side="right")
    more = float(np.sum(n_less))
    less = float(np.sum(len(y) - n_le))
    return (more - less) / float(len(x) * len(y))


def interpret_cliffs_delta(d: float) -> str:
    ad = abs(d)
    if ad < 0.147:
        return "negligible"
    if ad < 0.33:
        return "small"
    if ad < 0.474:
        return "medium"
    return "large"


def benjamini_hochberg(p_values: Sequence[float]) -> List[float]:
    m = len(p_values)
    if m == 0:
        return []
    indexed = sorted(enumerate(p_values), key=lambda kv: kv[1])
    q_raw = [0.0] * m
    for rank_idx, (orig_idx, p) in enumerate(indexed, start=1):
        q_raw[rank_idx - 1] = (m / float(rank_idx)) * p
    q_monotone = [0.0] * m
    running = 1.0
    for i in range(m - 1, -1, -1):
        running = min(running, q_raw[i])
        q_monotone[i] = running
    q_out = [0.0] * m
    for rank_idx, (orig_idx, _) in enumerate(indexed):
        q_out[orig_idx] = min(1.0, q_monotone[rank_idx])
    return q_out


def bootstrap_ci_median_and_delta(
    x: np.ndarray,
    y: np.ndarray,
    rng: np.random.Generator,
    n_boot: int = N_BOOTSTRAP,
) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    nx, ny = len(x), len(y)
    med_diffs = np.empty(n_boot, dtype=np.float64)
    deltas = np.empty(n_boot, dtype=np.float64)
    for b in range(n_boot):
        xb = x[rng.integers(0, nx, size=nx)]
        yb = y[rng.integers(0, ny, size=ny)]
        med_diffs[b] = float(np.median(xb) - np.median(yb))
        deltas[b] = cliffs_delta(xb, yb)
    ci_med = (float(np.percentile(med_diffs, 2.5)), float(np.percentile(med_diffs, 97.5)))
    ci_delta = (float(np.percentile(deltas, 2.5)), float(np.percentile(deltas, 97.5)))
    return ci_med, ci_delta


def _group_stats(arr: np.ndarray) -> Dict[str, Any]:
    if len(arr) == 0:
        return {"n": 0, "mean": None, "std": None, "median": None, "q25": None, "q75": None, "iqr": None}
    q25, q50, q75 = np.percentile(arr, [25, 50, 75])
    return {
        "n": int(len(arr)),
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr, ddof=1)) if len(arr) > 1 else 0.0,
        "median": float(q50),
        "q25": float(q25),
        "q75": float(q75),
        "iqr": float(q75 - q25),
    }


def run_morphometric_comparison(records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, List[float]]]]:
    stone = [r for r in records if r["period_norm"] == "stone_age_hunter"]
    bronze = [r for r in records if r["period_norm"] == "bronze_age_agrarian"]

    metric_specs = [
        ("area_mm2", "Calibrated Area (mm²)", True),
        ("width_mm", "Calibrated Width (mm)", True),
        ("height_mm", "Calibrated Height (mm)", True),
        ("perimeter_mm", "Calibrated Perimeter (mm)", True),
        ("aspect_ratio", "Aspect Ratio (L/W)", False),
        ("compactness", "Compactness (4πA/P²)", False),
        ("solidity", "Convex Solidity (A/A_hull)", False),
        ("shape_complexity_index", "Shape Complexity (P/√A)", False),
        ("vertex_count", "Contour Vertex Count", False),
        ("hole_count", "Internal Hole Count", False),
        ("cup_marks_count", "Associated Cup Marks", False),
    ]

    rng = np.random.default_rng(RANDOM_SEED)
    results: List[Dict[str, Any]] = []
    raw_groups: Dict[str, Dict[str, List[float]]] = {
        "stone_age_hunter": {},
        "bronze_age_agrarian": {},
    }

    for key, label, calibrated_only in metric_specs:
        s_vals = np.array(
            [
                float(r[key])
                for r in stone
                if r.get(key) is not None and (not calibrated_only or r.get("scale_calibrated"))
            ],
            dtype=np.float64,
        )
        b_vals = np.array(
            [
                float(r[key])
                for r in bronze
                if r.get(key) is not None and (not calibrated_only or r.get("scale_calibrated"))
            ],
            dtype=np.float64,
        )
        raw_groups["stone_age_hunter"][key] = s_vals.tolist()
        raw_groups["bronze_age_agrarian"][key] = b_vals.tolist()

        entry: Dict[str, Any] = {
            "metric": key,
            "label": label,
            "calibrated_only": calibrated_only,
            "stone_age_hunter": _group_stats(s_vals),
            "bronze_age_agrarian": _group_stats(b_vals),
            "tested": False,
            "u_stat": None,
            "p_value": None,
            "q_value": None,
            "rank_biserial_r": None,
            "cliffs_delta": None,
            "effect_magnitude": None,
            "median_diff_stone_minus_bronze": None,
            "ci95_median_diff": None,
            "ci95_cliffs_delta": None,
            "significant_fdr_0_05": False,
        }

        if len(s_vals) >= 3 and len(b_vals) >= 3:
            mw = stats.mannwhitneyu(s_vals, b_vals, alternative="two-sided")
            u_stat = float(mw.statistic)
            p_val = float(mw.pvalue)
            r_rb = (2.0 * u_stat) / float(len(s_vals) * len(b_vals)) - 1.0
            delta = cliffs_delta(s_vals, b_vals)
            ci_med, ci_delta = bootstrap_ci_median_and_delta(s_vals, b_vals, rng)
            entry.update({
                "tested": True,
                "u_stat": u_stat,
                "p_value": p_val,
                "rank_biserial_r": float(r_rb),
                "cliffs_delta": float(delta),
                "effect_magnitude": interpret_cliffs_delta(delta),
                "median_diff_stone_minus_bronze": float(np.median(s_vals) - np.median(b_vals)),
                "ci95_median_diff": [ci_med[0], ci_med[1]],
                "ci95_cliffs_delta": [ci_delta[0], ci_delta[1]],
            })
        results.append(entry)

    tested_idx = [i for i, r in enumerate(results) if r["tested"]]
    if tested_idx:
        q_vals = benjamini_hochberg([results[i]["p_value"] for i in tested_idx])
        for idx, q in zip(tested_idx, q_vals):
            results[idx]["q_value"] = float(q)
            results[idx]["significant_fdr_0_05"] = bool(q < 0.05)

    return results, raw_groups


def run_regional_comparison(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    all_regions_present = sorted(set(r["region_norm"] for r in records))
    regions_compared = [r for r in CORE_REGIONS if any(rec["region_norm"] == r for rec in records)]
    if not regions_compared:
        regions_compared = all_regions_present

    families = sorted(set(r["motif_family"] for r in records))
    classes = sorted(set(r["motif_class"] for r in records))

    family_counts: Dict[str, Dict[str, int]] = {}
    family_proportions: Dict[str, Dict[str, float]] = {}
    class_counts: Dict[str, Dict[str, int]] = {}
    diversity: Dict[str, Dict[str, Any]] = {}

    for reg in regions_compared:
        sub = [r for r in records if r["region_norm"] == reg]
        n = len(sub)
        fc = collections.Counter(r["motif_family"] for r in sub)
        cc = collections.Counter(r["motif_class"] for r in sub)
        family_counts[reg] = {f: int(fc.get(f, 0)) for f in families}
        family_proportions[reg] = {f: float(fc.get(f, 0) / n) if n else 0.0 for f in families}
        class_counts[reg] = {c: int(cc.get(c, 0)) for c in classes if cc.get(c, 0) > 0}

        # Diversity indices on motif_class abundances
        counts_vec = np.array([cc[c] for c in cc if cc[c] > 0], dtype=np.float64)
        s_richness = int(len(counts_vec))
        if n > 0 and s_richness > 0:
            probs = counts_vec / float(n)
            shannon_h = float(-np.sum(probs * np.log(probs)))
            if abs(shannon_h) < 1e-12:
                shannon_h = 0.0
            eff_classes = float(math.exp(shannon_h))
            pielou_j = float(shannon_h / math.log(s_richness)) if s_richness > 1 else None
            simpson_1_d = float(1.0 - np.sum(probs * probs))
        else:
            shannon_h, eff_classes, pielou_j, simpson_1_d = 0.0, 0.0, None, 0.0

        top_classes = [{"motif_class": k, "count": int(v), "share": float(v / n)} for k, v in cc.most_common(5)]
        diversity[reg] = {
            "n": n,
            "richness_S": s_richness,
            "shannon_h": shannon_h,
            "effective_classes_exp_h": eff_classes,
            "pielou_j": pielou_j,
            "simpson_1_minus_d": simpson_1_d,
            "top_classes": top_classes,
        }

    # Pairwise Jaccard (presence-absence of motif_class) & Bray-Curtis (relative abundance of motif_class)
    k_reg = len(regions_compared)
    jaccard_sim = [[0.0] * k_reg for _ in range(k_reg)]
    jaccard_dist = [[0.0] * k_reg for _ in range(k_reg)]
    bc_rel = [[0.0] * k_reg for _ in range(k_reg)]
    bc_raw = [[0.0] * k_reg for _ in range(k_reg)]

    class_sets = {r: set(class_counts[r].keys()) for r in regions_compared}
    rel_vecs = {}
    raw_vecs = {}
    for r in regions_compared:
        tot = float(diversity[r]["n"]) or 1.0
        raw_v = np.array([class_counts[r].get(c, 0) for c in classes], dtype=np.float64)
        raw_vecs[r] = raw_v
        rel_vecs[r] = raw_v / tot

    for i, r1 in enumerate(regions_compared):
        for j, r2 in enumerate(regions_compared):
            s1, s2 = class_sets[r1], class_sets[r2]
            union = len(s1 | s2)
            inter = len(s1 & s2)
            js = float(inter / union) if union > 0 else 0.0
            jaccard_sim[i][j] = js
            jaccard_dist[i][j] = 1.0 - js

            p1, p2 = rel_vecs[r1], rel_vecs[r2]
            denom_rel = float(np.sum(p1 + p2))
            bc_rel[i][j] = float(np.sum(np.abs(p1 - p2)) / denom_rel) if denom_rel > 0 else 0.0

            c1, c2 = raw_vecs[r1], raw_vecs[r2]
            denom_raw = float(np.sum(c1 + c2))
            bc_raw[i][j] = float(np.sum(np.abs(c1 - c2)) / denom_raw) if denom_raw > 0 else 0.0

    # Chi-square test across regions x families (filtering zero columns/rows)
    chi2_result: Dict[str, Any] = {"tested": False}
    if k_reg >= 2 and len(families) >= 2:
        table = np.array([[family_counts[r][f] for f in families] for r in regions_compared], dtype=np.int64)
        col_mask = table.sum(axis=0) > 0
        row_mask = table.sum(axis=1) > 0
        sub_table = table[row_mask][:, col_mask]
        if sub_table.shape[0] >= 2 and sub_table.shape[1] >= 2:
            chi2, p_val, dof, exp = stats.chi2_contingency(sub_table)
            min_exp = float(np.min(exp))
            chi2_result = {
                "tested": True,
                "chi2": float(chi2),
                "dof": int(dof),
                "p_value": float(p_val),
                "min_expected_count": min_exp,
                "cramers_v": float(math.sqrt(chi2 / (sub_table.sum() * (min(sub_table.shape) - 1)))),
                "sparse_cells_warning": bool(min_exp < 5.0),
            }

    return {
        "regions_compared": regions_compared,
        "all_regions_present": all_regions_present,
        "families": families,
        "classes": classes,
        "family_counts": family_counts,
        "family_proportions": family_proportions,
        "diversity": diversity,
        "jaccard_similarity_matrix": jaccard_sim,
        "jaccard_distance_matrix": jaccard_dist,
        "bray_curtis_matrix": bc_rel,
        "bray_curtis_raw_counts_matrix": bc_raw,
        "contingency_chi2": chi2_result,
    }


def _summarize_iou_bucket(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    ious = np.array([float(it["iou"]) for it in items if it.get("iou") is not None], dtype=np.float64)
    b_dists = np.array(
        [float(it["mean_boundary_dist"]) for it in items if it.get("mean_boundary_dist") is not None],
        dtype=np.float64,
    )
    if len(ious) == 0:
        return {"n": 0, "mean_iou": None, "median_iou": None, "q25_iou": None, "q75_iou": None, "min_iou": None, "max_iou": None}
    q25, q50, q75 = np.percentile(ious, [25, 50, 75])
    out: Dict[str, Any] = {
        "n": int(len(ious)),
        "mean_iou": float(np.mean(ious)),
        "std_iou": float(np.std(ious, ddof=1)) if len(ious) > 1 else 0.0,
        "median_iou": float(q50),
        "q25_iou": float(q25),
        "q75_iou": float(q75),
        "min_iou": float(np.min(ious)),
        "max_iou": float(np.max(ious)),
    }
    if len(b_dists) > 0:
        out["mean_boundary_dist"] = float(np.mean(b_dists))
        out["median_boundary_dist"] = float(np.median(b_dists))
    return out


def run_dual_trace_analysis(
    qa_report: Dict[str, Any],
    disagreements: List[Dict[str, Any]],
    rec_by_id: Dict[str, Dict[str, Any]],
    total_figures: int,
) -> Dict[str, Any]:
    # Support flexible report keys (items / results / figures / records)
    raw_items = (
        qa_report.get("figures")
        or qa_report.get("items")
        or qa_report.get("results")
        or qa_report.get("records")
        or []
    )
    enriched: List[Dict[str, Any]] = []
    for it in raw_items:
        fid = str(it.get("figure_id") or "")
        meta = rec_by_id.get(fid, {})
        iou = it.get("iou")
        if iou is None:
            iou = it.get("polygon_iou")
        b_dist = (
            it.get("mean_boundary_dist")
            or it.get("mean_boundary_distance")
            or it.get("mean_boundary_dist_mm")
            or it.get("mean_boundary_dist_px")
        )
        area_delta = it.get("area_delta_pct")
        if area_delta is None:
            area_delta = it.get("area_diff_pct")

        enriched.append({
            "figure_id": fid,
            "iou": float(iou) if iou is not None else None,
            "mean_boundary_dist": float(b_dist) if b_dist is not None else None,
            "hausdorff": it.get("hausdorff") or it.get("symmetric_hausdorff"),
            "area_delta_pct": float(area_delta) if area_delta is not None else None,
            "topology_agree": it.get("topology_agree", it.get("topology_agreement")),
            "archive": str(it.get("source_archive") or meta.get("archive") or "unknown"),
            "family": str(it.get("motif_family") or meta.get("motif_family") or "unknown"),
            "period": str(it.get("period") or meta.get("period_norm") or "unknown"),
            "complexity": meta.get("shape_complexity_index"),
        })

    # Complexity tertiles
    comp_vals = [e["complexity"] for e in enriched if e["complexity"] is not None]
    if len(comp_vals) >= 3:
        t1, t2 = np.percentile(comp_vals, [33.33, 66.67])
        for e in enriched:
            c = e["complexity"]
            if c is None:
                e["complexity_tier"] = "unknown"
            elif c <= t1:
                e["complexity_tier"] = f"low (P/√A ≤ {t1:.2f})"
            elif c <= t2:
                e["complexity_tier"] = f"medium ({t1:.2f} < P/√A ≤ {t2:.2f})"
            else:
                e["complexity_tier"] = f"high (P/√A > {t2:.2f})"
    else:
        for e in enriched:
            e["complexity_tier"] = "unknown"

    overall = _summarize_iou_bucket(enriched)
    overall["coverage_pct"] = (100.0 * overall["n"] / total_figures) if total_figures else 0.0

    def group_by(key: str) -> Dict[str, Dict[str, Any]]:
        buckets: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
        for e in enriched:
            buckets[str(e.get(key) or "unknown")].append(e)
        return {
            k: _summarize_iou_bucket(v)
            for k, v in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        }

    by_archive = group_by("archive")
    by_family = group_by("family")
    by_complexity = group_by("complexity_tier")
    by_period = group_by("period")

    # Kruskal-Wallis across strata where >=2 groups have n >= 3
    kw_tests: Dict[str, Any] = {}
    for sname, sdict in [("archive", by_archive), ("family", by_family), ("complexity", by_complexity), ("period", by_period)]:
        valid_keys = [k for k, v in sdict.items() if v["n"] >= 3]
        if len(valid_keys) >= 2:
            if sname == "archive":
                samples = [[e["iou"] for e in enriched if e["archive"] == k and e["iou"] is not None] for k in valid_keys]
            elif sname == "family":
                samples = [[e["iou"] for e in enriched if e["family"] == k and e["iou"] is not None] for k in valid_keys]
            elif sname == "complexity":
                samples = [[e["iou"] for e in enriched if e["complexity_tier"] == k and e["iou"] is not None] for k in valid_keys]
            else:
                samples = [[e["iou"] for e in enriched if e["period"] == k and e["iou"] is not None] for k in valid_keys]
            if all(len(s) >= 3 for s in samples):
                h_stat, p_val = stats.kruskal(*samples)
                kw_tests[sname] = {"groups": valid_keys, "H": float(h_stat), "p_value": float(p_val)}

    dis_reasons = collections.Counter(
        str(d.get("reason") or d.get("disagreement_type") or d.get("severity") or "logged")
        for d in disagreements
    )

    return {
        "overall": overall,
        "report_summary_raw": qa_report.get("summary", {}),
        "by_archive": by_archive,
        "by_family": by_family,
        "by_complexity": by_complexity,
        "by_period": by_period,
        "kruskal_wallis": kw_tests,
        "disagreement_count": len(disagreements),
        "disagreement_breakdown": dict(dis_reasons),
    }


def _fmt_num(val: Optional[float], digits: int = 2) -> str:
    if val is None:
        return "n/a"
    return f"{val:.{digits}f}"


def _fmt_p(val: Optional[float]) -> str:
    if val is None:
        return "n/a"
    if val < 1e-4:
        return f"{val:.2e}"
    return f"{val:.4f}"


def build_markdown_report(metrics: Dict[str, Any]) -> str:
    ov = metrics["corpus_overview"]
    morph = metrics["morphometrics_veide_vs_jordbruk"]
    reg = metrics["regional_iconography"]
    qa = metrics["dual_trace_reliability"]
    env = metrics["reproducibility"]

    lines: List[str] = []
    lines.append("# Nordic Helleristninger Corpus — Scientific Findings Report (v2026.W48)")
    lines.append("")
    lines.append(f"**Generated:** {env['generated_utc']}  ")
    lines.append(f"**Corpus Release:** `v2026.W48` (`N = {ov['total_figures']}` verified carved figures; `painted_photo` strictly excluded)  ")
    lines.append(f"**Random Seed:** `{env['random_seed']}` (`n_boot = {env['n_bootstrap']}`) · **Environment:** Python `{env['python_version']}`, SciPy `{env['scipy_version']}`, NumPy `{env['numpy_version']}`")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 1. Corpus Overview & Archival Coverage")
    lines.append("")
    lines.append("| Metric | Value | Notes |")
    lines.append("|---|---:|---|")
    lines.append(f"| Total traced figures | **{ov['total_figures']}** | Carved documentation only (`bw_tracing`, `svg_vector`, `rubbing_frottage`, `rti_3d`) |")
    lines.append(f"| Scale-calibrated figures (mm) | **{ov['calibrated_count']}** ({100.0 * ov['calibrated_count'] / max(1, ov['total_figures']):.1f}%) | Verified physical scale bar (`scale_calibrated: true`) |")
    lines.append(f"| Uncalibrated figures (px space) | **{ov['uncalibrated_count']}** ({100.0 * ov['uncalibrated_count'] / max(1, ov['total_figures']):.1f}%) | Flagged `scale_calibrated: false`; no fabricated mm scale |")
    lines.append(f"| Verified EPSG:4326 coordinates | **{ov['coords_count']}** ({100.0 * ov['coords_count'] / max(1, ov['total_figures']):.1f}%) | Site/panel-level geospatial linkage |")
    lines.append(f"| Verified Askeladden / RAÄ FMIS ID | **{ov['site_id_count']}** ({100.0 * ov['site_id_count'] / max(1, ov['total_figures']):.1f}%) | Heritage register linkage |")
    lines.append(f"| Commercial-eligible records | **{ov['commercial_ok_count']}** ({100.0 * ov['commercial_ok_count'] / max(1, ov['total_figures']):.1f}%) | Scientific corpus retains NC-SA (`CC BY-NC-SA`) for scholarship; puzzle excludes NC-SA |")
    lines.append("")
    lines.append("### Regional & Chronological Distribution")
    lines.append("")
    lines.append("| Region | Total | Stone Age (`veideristninger`) | Bronze Age (`jordbruksristninger`) | Mixed | Unknown |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for rname, pcounts in ov["region_period_counts"].items():
        tot = sum(pcounts.values())
        lines.append(
            f"| {rname} | **{tot}** | {pcounts.get('stone_age_hunter', 0)} | {pcounts.get('bronze_age_agrarian', 0)} | {pcounts.get('mixed', 0)} | {pcounts.get('unknown', 0)} |"
        )
    lines.append("")
    lines.append("![Corpus Overview](figures/fig1_corpus_regions_periods.png)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 2. Morphometric Comparison: Stone Age (*Veideristninger*) vs. Bronze Age (*Jordbruksristninger*)")
    lines.append("")
    lines.append(
        "Non-parametric two-sided Mann-Whitney *U* tests compare Stone Age hunter-gatherer carvings (`stone_age_hunter`) "
        "against Bronze Age agrarian carvings (`bronze_age_agrarian`). Effect sizes report both rank-biserial correlation "
        "($r_{rb} = 2U/(n_1 n_2) - 1$) and exact Cliff's $\\delta$ ($P(X_{\\text{stone}} > X_{\\text{bronze}}) - P(X_{\\text{stone}} < X_{\\text{bronze}})$) "
        f"with {env['n_bootstrap']}-replicate percentile 95% bootstrap confidence intervals (seed `{env['random_seed']}`). "
        "Multiplicity is controlled via Benjamini-Hochberg False Discovery Rate ($q$-value)."
    )
    lines.append("")
    lines.append("| Metric | Calibrated Only? | Stone Age $n$ / Median [IQR] | Bronze Age $n$ / Median [IQR] | Mann-Whitney $U$ | $p$-value | BH $q$-value | Cliff's $\\delta$ [95% CI] | Magnitude | Med. Diff [95% CI] |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|---|---|")
    for m in morph:
        s = m["stone_age_hunter"]
        b = m["bronze_age_agrarian"]
        s_str = f"{s['n']} / {_fmt_num(s['median'])} [{_fmt_num(s['q25'])}, {_fmt_num(s['q75'])}]" if s["n"] else "0 / n/a"
        b_str = f"{b['n']} / {_fmt_num(b['median'])} [{_fmt_num(b['q25'])}, {_fmt_num(b['q75'])}]" if b["n"] else "0 / n/a"
        if m["tested"]:
            d_ci = m["ci95_cliffs_delta"]
            md_ci = m["ci95_median_diff"]
            d_str = f"{m['cliffs_delta']:+.3f} [{d_ci[0]:+.3f}, {d_ci[1]:+.3f}]"
            md_str = f"{m['median_diff_stone_minus_bronze']:+.2f} [{md_ci[0]:+.2f}, {md_ci[1]:+.2f}]"
            lines.append(
                f"| {m['label']} | {'Yes' if m['calibrated_only'] else 'No'} | {s_str} | {b_str} | {m['u_stat']:.0f} | {_fmt_p(m['p_value'])} | **{_fmt_p(m['q_value'])}** | {d_str} | {m['effect_magnitude']} | {md_str} |"
            )
        else:
            lines.append(
                f"| {m['label']} | {'Yes' if m['calibrated_only'] else 'No'} | {s_str} | {b_str} | n/a | n/a | n/a | n/a | insufficient n | n/a |"
            )
    lines.append("")
    lines.append("![Morphometric Comparison](figures/fig2_morphometrics_veide_vs_jordbruk.png)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 3. Regional Iconographic Diversity & Beta-Diversity")
    lines.append("")
    lines.append("### Alpha Diversity Across Regions (Motif Class Level)")
    lines.append("")
    lines.append("| Region | $n$ | Richness ($S$) | Shannon $H'$ (nats) | Effective Classes ($e^{H'}$) | Pielou Evenness ($J'$) | Simpson ($1 - D$) | Top Motif Classes |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|")
    for r in reg["regions_compared"]:
        d = reg["diversity"][r]
        top_str = ", ".join(f"`{t['motif_class']}` ({t['count']})" for t in d["top_classes"][:3])
        lines.append(
            f"| {r} | {d['n']} | {d['richness_S']} | {d['shannon_h']:.3f} | {d['effective_classes_exp_h']:.2f} | {_fmt_num(d['pielou_j'], 3)} | {d['simpson_1_minus_d']:.3f} | {top_str} |"
        )
    lines.append("")

    chi = reg["contingency_chi2"]
    if chi.get("tested"):
        warn = " *(note: sparse cells with expected count < 5)*" if chi.get("sparse_cells_warning") else ""
        lines.append(
            f"**Region × Motif Family Contingency Test:** $\\chi^2({chi['dof']}) = {chi['chi2']:.2f}$, "
            f"$p = {_fmt_p(chi['p_value'])}$, Cramér's $V = {chi['cramers_v']:.3f}${warn}."
        )
        lines.append("")

    def _matrix_md(title: str, mat: List[List[float]], regions: List[str]) -> None:
        lines.append(f"### {title}")
        lines.append("")
        lines.append("| Region | " + " | ".join(regions) + " |")
        lines.append("|---|" + "|".join(["---:"] * len(regions)) + "|")
        for i, r1 in enumerate(regions):
            row_vals = " | ".join(f"{mat[i][j]:.3f}" for j in range(len(regions)))
            lines.append(f"| {r1} | {row_vals} |")
        lines.append("")

    _matrix_md("Pairwise Jaccard Distance Matrix ($1 - J$, Presence-Absence)", reg["jaccard_distance_matrix"], reg["regions_compared"])
    _matrix_md("Pairwise Bray-Curtis Dissimilarity Matrix (Relative Motif-Class Abundances)", reg["bray_curtis_matrix"], reg["regions_compared"])
    lines.append("![Regional Iconography](figures/fig3_regional_iconography_heatmap.png)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 4. Dual-Trace Measurement Reliability (QA Audit)")
    lines.append("")
    qov = qa["overall"]
    lines.append(
        f"- **Stratified QA Sample:** `{qov['n']}` figures (`{qov['coverage_pct']:.2f}%` of corpus; target $\\ge 5.0\\%$)\n"
        f"- **Overall Polygon IoU:** mean = `{_fmt_num(qov.get('mean_iou'), 4)}`, median = `{_fmt_num(qov.get('median_iou'), 4)}` "
        f"(IQR `{_fmt_num(qov.get('q25_iou'), 4)}`–`{_fmt_num(qov.get('q75_iou'), 4)}`, min `{_fmt_num(qov.get('min_iou'), 4)}`, max `{_fmt_num(qov.get('max_iou'), 4)}`)\n"
        f"- **Logged Disagreements:** `{qa['disagreement_count']}` entries in `helleristninger/qa/dual_trace_disagreements.jsonl`"
    )
    lines.append("")

    for heading, key in [
        ("By Source Archive", "by_archive"),
        ("By Motif Family", "by_family"),
        ("By Geometric Complexity ($P / \\sqrt{A}$)", "by_complexity"),
        ("By Chronological Period", "by_period"),
    ]:
        lines.append(f"### {heading}")
        lines.append("")
        lines.append("| Stratum | $n$ | Mean IoU | Median IoU | IQR (Q25–Q75) | Min IoU | Mean Boundary Dist |")
        lines.append("|---|---:|---:|---:|---|---:|---:|")
        for sname, st in qa[key].items():
            iqr_s = f"{_fmt_num(st.get('q25_iou'), 3)}–{_fmt_num(st.get('q75_iou'), 3)}"
            lines.append(
                f"| {sname} | {st['n']} | {_fmt_num(st.get('mean_iou'), 4)} | {_fmt_num(st.get('median_iou'), 4)} | {iqr_s} | {_fmt_num(st.get('min_iou'), 4)} | {_fmt_num(st.get('mean_boundary_dist'), 3)} |"
            )
        lines.append("")

    if qa.get("kruskal_wallis"):
        lines.append("**Kruskal-Wallis Tests Across Reliability Strata:**")
        for sname, kw in qa["kruskal_wallis"].items():
            lines.append(f"- `{sname}` ({len(kw['groups'])} strata with $n \\ge 3$): $H = {kw['H']:.3f}$, $p = {_fmt_p(kw['p_value'])}$")
        lines.append("")

    lines.append("![Dual-Trace Reliability](figures/fig4_dual_trace_reliability.png)")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 5. Methodology, Reproducibility & Honest Limitations")
    lines.append("")
    lines.append("### Reproducibility")
    lines.append("```powershell")
    lines.append(".venv\\Scripts\\python.exe helleristninger/science/analyze_corpus.py")
    lines.append("```")
    lines.append("- Reads `helleristninger/scientific_record/figures.jsonl`, `helleristninger/scientific_record/svg/*.svg`, `helleristninger/qa/dual_trace_report.json`, and `helleristninger/qa/dual_trace_disagreements.jsonl`.")
    lines.append("- Deterministic seed (`42`) for bootstrap confidence intervals (`2000` resamples).")
    lines.append("")
    lines.append("### Explicit Methodological Limitations")
    lines.append("1. **Archive Sampling Bias:** Corpus density reflects open digital archive availability (e.g., high SHFA Bohuslän/Tanum representation relative to Nämforsen or Østfold), not true prehistoric carving density.")
    lines.append(f"2. **Scale Calibration Coverage:** Only `{ov['calibrated_count']}/{ov['total_figures']}` (`{100.0 * ov['calibrated_count'] / max(1, ov['total_figures']):.1f}%`) figures possess verified archival scale bars (`scale_calibrated: true`). Uncalibrated archival plates remain strictly in pixel space and are excluded from physical `mm` / `mm²` morphometrics.")
    lines.append("3. **Chronological & Taxonomic Attribution:** Period tags inherit site/panel provenance and heuristic motif-class priors; undated or `mixed` panels are excluded from the strict Stone Age vs. Bronze Age hypothesis tests.")
    lines.append("4. **Painted Documentation Quarantine:** All `painted_photo` records are excluded prior to scientific tracing because modern conservation paint reflects curatorial interpretation rather than primary pecked groove boundaries.")
    lines.append("5. **Licensing Stratification:** Non-commercial records (`CC BY-NC-SA`, Alta Museum) are retained here solely for open scholarship and strictly excluded from all 750-piece commercial production artifacts (`commercial_ok: true`).")
    lines.append("")
    return "\n".join(lines)


def analyze(
    figures_path: Path,
    qa_report_path: Path,
    disagreements_path: Path,
    gazetteer_path: Path,
    out_dir: Path,
) -> Dict[str, Any]:
    raw_figures = _load_jsonl(figures_path)
    qa_report = json.loads(qa_report_path.read_text(encoding="utf-8")) if qa_report_path.exists() else {}
    disagreements = _load_jsonl(disagreements_path)
    gaz_raw = json.loads(gazetteer_path.read_text(encoding="utf-8")) if gazetteer_path.exists() else {}

    gaz_sites = gaz_raw.get("sites") if isinstance(gaz_raw, dict) else gaz_raw
    gaz_by_id: Dict[str, Dict[str, Any]] = {}
    if isinstance(gaz_sites, list):
        for s in gaz_sites:
            for k in (s.get("site_key"), s.get("askeladden_id"), s.get("raa_id"), s.get("lamning_id"), s.get("site_panel_id"), s.get("id")):
                if k:
                    gaz_by_id[str(k)] = s
    elif isinstance(gaz_sites, dict):
        gaz_by_id = {str(k): v for k, v in gaz_sites.items() if isinstance(v, dict)}

    svg_dir = figures_path.parent / "svg"

    enriched_records: List[Dict[str, Any]] = []
    region_period_counts: Dict[str, Dict[str, int]] = collections.defaultdict(lambda: collections.defaultdict(int))
    licence_counts: collections.Counter = collections.Counter()
    archive_counts: collections.Counter = collections.Counter()

    calibrated_count = 0
    coords_count = 0
    site_id_count = 0
    commercial_ok_count = 0

    for rec in raw_figures:
        fid = str(rec.get("figure_id") or "")
        size_mm = rec.get("size_mm") or {}
        scale_cal = bool(size_mm.get("scale_calibrated", False))
        if scale_cal:
            calibrated_count += 1

        coords = rec.get("coordinates")
        if isinstance(coords, dict) and coords.get("lat") is not None and coords.get("lon") is not None:
            coords_count += 1

        site_panel_id = rec.get("site_panel_id")
        if site_panel_id not in (None, "", "null", "unknown"):
            site_id_count += 1

        lic = str(rec.get("licence") or rec.get("license") or "Unknown")
        licence_counts[lic] += 1
        lic_low = lic.lower()
        comm_ok = rec.get("commercial_ok")
        if comm_ok is None:
            comm_ok = (
                "nc" not in lic_low
                and "non-commercial" not in lic_low
                and "unknown" not in lic_low
                and lic.strip() != ""
            )
        if comm_ok:
            commercial_ok_count += 1

        src_img = rec.get("source_image") or {}
        archive = str(rec.get("source_archive") or src_img.get("source_archive") or src_img.get("source_id", "").split("_")[0] or "unknown")
        archive_counts[archive] += 1

        reg_norm = normalize_region(rec, gaz_by_id)
        per_norm = normalize_period(rec.get("period"))
        region_period_counts[reg_norm][per_norm] += 1

        motif = rec.get("motif") or {}
        fam = str(motif.get("family") or rec.get("family") or "scene_or_unclassified")
        mcls = str(motif.get("motif_class") or rec.get("motif_class") or "unclassified_motif")

        geom_rec = rec.get("geometry")
        layers_rec = rec.get("layers")
        if isinstance(geom_rec, dict) and isinstance(layers_rec, dict) and geom_rec.get("aspect_ratio") is not None:
            perim_px = geom_rec.get("perimeter_px")
            area_px2 = size_mm.get("area_px2")
            sci = None
            if perim_px is not None and area_px2 is not None and float(area_px2) > 0:
                sci = float(perim_px) / math.sqrt(float(area_px2))
            geom = {
                "svg_perimeter": geom_rec.get("perimeter_mm"),
                "aspect_ratio": geom_rec.get("aspect_ratio"),
                "compactness": geom_rec.get("compactness"),
                "solidity": geom_rec.get("solidity"),
                "vertex_count": geom_rec.get("vertex_count"),
                "hole_count": geom_rec.get("hole_count"),
                "shape_complexity_index": sci,
                "cup_marks_count": layers_rec.get("cup_marks_count", 0),
                "cracks_count": layers_rec.get("cracks_count", 0),
                "uncertain_count": layers_rec.get("uncertain_count", 0),
            }
        else:
            svg_path = Path(rec["svg_path"]) if rec.get("svg_path") and Path(rec["svg_path"]).exists() else (svg_dir / f"{fid}.svg")
            geom = _svg_geometry_metrics(svg_path)
            geom["hole_count"] = None

        width_mm = float(size_mm["width_mm"]) if (scale_cal and size_mm.get("width_mm") is not None) else None
        height_mm = float(size_mm["height_mm"]) if (scale_cal and size_mm.get("height_mm") is not None) else None
        area_mm2 = float(size_mm["area_mm2"]) if (scale_cal and size_mm.get("area_mm2") is not None) else None
        perim_mm = float(geom["svg_perimeter"]) if (scale_cal and geom.get("svg_perimeter") is not None) else None

        enriched_records.append({
            "figure_id": fid,
            "region_norm": reg_norm,
            "period_norm": per_norm,
            "motif_family": fam,
            "motif_class": mcls,
            "archive": archive,
            "licence": lic,
            "commercial_ok": bool(comm_ok),
            "scale_calibrated": scale_cal,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "area_mm2": area_mm2,
            "perimeter_mm": perim_mm,
            "aspect_ratio": geom["aspect_ratio"],
            "compactness": geom["compactness"],
            "solidity": geom["solidity"],
            "vertex_count": geom["vertex_count"],
            "hole_count": geom.get("hole_count"),
            "shape_complexity_index": geom["shape_complexity_index"],
            "cup_marks_count": geom["cup_marks_count"],
            "cracks_count": geom["cracks_count"],
            "uncertain_count": geom["uncertain_count"],
        })

    ordered_regions = [r for r in CORE_REGIONS if r in region_period_counts]
    for r in sorted(region_period_counts.keys()):
        if r not in ordered_regions:
            ordered_regions.append(r)

    corpus_overview = {
        "total_figures": len(enriched_records),
        "calibrated_count": calibrated_count,
        "uncalibrated_count": len(enriched_records) - calibrated_count,
        "coords_count": coords_count,
        "site_id_count": site_id_count,
        "commercial_ok_count": commercial_ok_count,
        "region_period_counts": {r: dict(region_period_counts[r]) for r in ordered_regions},
        "licence_counts": dict(licence_counts),
        "archive_counts": dict(archive_counts),
    }

    morph_results, raw_groups = run_morphometric_comparison(enriched_records)
    regional_results = run_regional_comparison(enriched_records)
    rec_by_id = {r["figure_id"]: r for r in enriched_records}
    qa_results = run_dual_trace_analysis(qa_report, disagreements, rec_by_id, len(enriched_records))

    metrics = {
        "release": "v2026.W48",
        "reproducibility": {
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "random_seed": RANDOM_SEED,
            "n_bootstrap": N_BOOTSTRAP,
            "python_version": platform.python_version(),
            "scipy_version": scipy.__version__,
            "numpy_version": np.__version__,
        },
        "corpus_overview": corpus_overview,
        "morphometrics_veide_vs_jordbruk": morph_results,
        "regional_iconography": regional_results,
        "dual_trace_reliability": qa_results,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    fig_dir = out_dir / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    plot_corpus_overview(corpus_overview, fig_dir / "fig1_corpus_regions_periods.png")
    plot_morphometric_comparison(morph_results, raw_groups, fig_dir / "fig2_morphometrics_veide_vs_jordbruk.png")
    plot_regional_iconography(regional_results, fig_dir / "fig3_regional_iconography_heatmap.png")
    plot_dual_trace_reliability(qa_results, fig_dir / "fig4_dual_trace_reliability.png")

    (out_dir / "findings_metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "findings_v2026.W48.md").write_text(build_markdown_report(metrics), encoding="utf-8")
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze Nordic Helleristninger scientific corpus.")
    parser.add_argument("--figures", type=Path, default=ROOT / "helleristninger" / "scientific_record" / "figures.jsonl")
    parser.add_argument("--qa-report", type=Path, default=ROOT / "helleristninger" / "qa" / "dual_trace_report.json")
    parser.add_argument("--disagreements", type=Path, default=ROOT / "helleristninger" / "qa" / "dual_trace_disagreements.jsonl")
    parser.add_argument("--gazetteer", type=Path, default=ROOT / "helleristninger" / "metadata" / "site_gazetteer.json")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "helleristninger" / "science")
    args = parser.parse_args()

    metrics = analyze(
        figures_path=args.figures,
        qa_report_path=args.qa_report,
        disagreements_path=args.disagreements,
        gazetteer_path=args.gazetteer,
        out_dir=args.out_dir,
    )
    ov = metrics["corpus_overview"]
    qa = metrics["dual_trace_reliability"]["overall"]
    print(
        f"Analyzed {ov['total_figures']} figures "
        f"(calibrated={ov['calibrated_count']}, coords={ov['coords_count']}); "
        f"dual-trace n={qa['n']} mean_iou={qa.get('mean_iou')}"
    )


if __name__ == "__main__":
    main()
