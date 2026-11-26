"""Independent silhouette-fidelity audit for exported puzzle runs.

Area IoU rewards pieces that keep the bulk of a motif even when the features a
person uses to recognise it (legs, ears, fins, the gaps between them) are lost.
This audit aligns each source silhouette to its finished piece and measures, in
millimetres on vector geometry:

* ``boundary_f``: F-score of the two outlines within ``tol_mm``.
* ``bay_fill``: share of the motif's concavities (convex hull minus motif) that
  the piece fills in. Filled bays are what turn a bear into a blob.
* ``extremity_recall``: share of the motif's thin parts (removed by a
  morphological opening of ``feature_mm``) that the piece still covers.
* ``excess``: share of the piece area that lies outside the aligned motif.

It uses no learned model and does not modify the run, apart from writing
``fidelity-audit.json``.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Polygon
from shapely.ops import unary_union

from ..data.corpus import load_corpus_catalog, load_motif_asset
from ..solver.geodesic import _aligned_iou_and_chamfer
from .geometry import target_polygon

WEIGHTS = dict(boundary_f=0.4, iou=0.2, bay_kept=0.2, extremity_recall=0.2)


def _boundary_points(poly, step):
    return shapely.points(shapely.get_coordinates(shapely.segmentize(poly.boundary, step)))


def piece_fidelity(piece: Polygon, target: Polygon, tol_mm: float = 1.5, feature_mm: float = 3.0) -> dict:
    target = target.buffer(0)
    if target.is_empty or piece.is_empty:
        return dict(iou=0.0, boundary_f=0.0, bay_fill=1.0, extremity_recall=0.0, excess=1.0, fidelity=0.0)
    inter = piece.intersection(target).area
    iou = inter / max(1e-9, piece.area + target.area - inter)
    pp = _boundary_points(piece, 0.5)
    tp = _boundary_points(target, 0.5)
    precision = float(np.mean(shapely.distance(pp, target.boundary) <= tol_mm))
    recall = float(np.mean(shapely.distance(tp, piece.boundary) <= tol_mm))
    boundary_f = 2 * precision * recall / max(1e-9, precision + recall)
    bays = target.convex_hull.difference(target).buffer(-0.5).buffer(0.5)
    if bays.area > 0.04 * target.area:
        bay_fill = piece.intersection(bays).area / bays.area
    else:
        bay_fill = 0.0
    core = target.buffer(-feature_mm, quad_segs=8).buffer(feature_mm, quad_segs=8)
    extremities = target.difference(core).buffer(-0.25).buffer(0.25)
    if extremities.area > 0.01 * target.area:
        extremity_recall = piece.intersection(extremities).area / extremities.area
    else:
        extremity_recall = 1.0
    excess = piece.difference(target).area / piece.area
    fidelity = (
        WEIGHTS["boundary_f"] * boundary_f
        + WEIGHTS["iou"] * iou
        + WEIGHTS["bay_kept"] * (1.0 - bay_fill)
        + WEIGHTS["extremity_recall"] * extremity_recall
    )
    return dict(
        iou=float(iou), boundary_precision=precision, boundary_recall=recall, boundary_f=float(boundary_f),
        bay_area_ratio=float(bays.area / target.area), bay_fill=float(bay_fill),
        extremity_area_ratio=float(extremities.area / target.area), extremity_recall=float(extremity_recall),
        excess=float(excess), fidelity=float(fidelity),
    )


def summarize(values) -> dict:
    a = np.asarray(values, float)
    return dict(mean=float(a.mean()), p10=float(np.percentile(a, 10)), minimum=float(a.min()),
                score=float(a.mean() + 0.35 * np.percentile(a, 10) + 0.25 * a.min()))


def aligned_targets(polys, motifs, px: float = 0.5, n_angles: int = 48):
    x0, y0, x1, y1 = unary_union(polys).bounds
    x0 = math.floor(x0 / px) * px - 8 * px
    y0 = math.floor(y0 / px) * px - 8 * px
    x = x0 + (np.arange(math.ceil((x1 - x0) / px) + 9) + 0.5) * px
    y = y0 + (np.arange(math.ceil((y1 - y0) / px) + 9) + 0.5) * px
    out = []
    for p, m in zip(polys, motifs):
        a, b, c, d = p.bounds
        i0, i1 = max(0, int((a - x0) / px) - 2), min(len(x), int((c - x0) / px) + 3)
        j0, j1 = max(0, int((b - y0) / px) - 2), min(len(y), int((d - y0) / px) + 3)
        cell = np.zeros((len(y), len(x)), bool)
        cell[j0:j1, i0:i1] = shapely.contains_xy(p, x[None, i0:i1], y[j0:j1, None])
        _, _, pose = _aligned_iou_and_chamfer(cell, m, n_angles=n_angles)
        t = target_polygon(m, pose, x0, y0, px)
        if t.geom_type != "Polygon":
            t = max(getattr(t, "geoms", [t]), key=lambda g: g.area)
        out.append(t)
    return out


def load_run(run_dir, catalog=None):
    p = Path(run_dir)
    d = json.loads((p / "partition.json").read_text())
    cfg = json.loads((p / "config.json").read_text())
    rows, base = load_corpus_catalog(catalog or cfg["corpus_source"])
    by_id = {r["asset_id"]: r for r in rows}
    motifs = []
    for f in d["faces"]:
        rec = by_id[f["metadata"]["asset_id"]]
        motifs.append(load_motif_asset(rec, base))
    polys = [Polygon(f["exterior"]).buffer(0) for f in d["faces"]]
    return d, polys, motifs


def fidelity_audit(run_dir, catalog=None, reference_catalog=None, write=True) -> dict:
    """Audit every piece of an exported run against its catalog silhouette.

    With ``reference_catalog`` each record's ``parent_asset_id`` (or its own id)
    is looked up there, so modified manufacturing targets are judged against the
    unchanged source silhouettes.
    """
    p = Path(run_dir)
    d, polys, motifs = load_run(p, catalog)
    if reference_catalog:
        cfg = json.loads((p / "config.json").read_text())
        rows, _ = load_corpus_catalog(catalog or cfg["corpus_source"])
        parent = {r["asset_id"]: r.get("parent_asset_id", r["asset_id"]) for r in rows}
        ref_rows, ref_base = load_corpus_catalog(reference_catalog)
        ref = {r["asset_id"]: r for r in ref_rows}
        motifs = [load_motif_asset(ref[parent[f["metadata"]["asset_id"]]], ref_base) for f in d["faces"]]
    targets = aligned_targets(polys, motifs)
    per_piece = []
    for i, (poly, t, f) in enumerate(zip(polys, targets, d["faces"])):
        row = piece_fidelity(poly, t)
        row.update(piece=i + 1, asset_id=f["metadata"].get("asset_id"), motif_class=f["metadata"].get("motif_class"))
        per_piece.append(row)
    keys = ("fidelity", "boundary_f", "iou", "bay_fill", "extremity_recall", "excess")
    report = dict(
        scope="Geometric silhouette-fidelity screen against aligned source masks; not a human recognition study.",
        tolerance_mm=1.5, feature_mm=3.0, weights=WEIGHTS, pieces=len(per_piece),
        reference_catalog=str(reference_catalog) if reference_catalog else None,
        summary={k: summarize([r[k] for r in per_piece]) for k in keys},
        weakest=sorted(per_piece, key=lambda r: r["fidelity"])[:5],
        per_piece=per_piece,
    )
    if write:
        (p / "fidelity-audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
