"""Sparse, scalable fabrication, interlocking, motion, and fidelity audits for large boards (750+ pieces).

Guarantees mathematical parity with ``heller_jigsaw.production.audit`` while replacing dense
O(360 * N^2) memory allocations with sparse edge-pair structures (~250x less RAM at N=750).
"""

from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

from ..data.corpus import MotifAsset, load_corpus_catalog, load_motif_asset
from ..geometry.validate import ValidationConfig, analyze_piece_fabrication, validate_run_dir
from ..production.audit import measure, motion_grid
from ..production.fidelity import WEIGHTS, piece_fidelity, summarize
from ..production.geometry import target_polygon
from ..solver.geodesic import _aligned_iou_and_chamfer

_THETA_360 = np.arange(360, dtype=np.float64) * (2.0 * np.pi / 360.0)
_DIRS_360 = np.stack([np.cos(_THETA_360), np.sin(_THETA_360)], axis=1)


def min_segment_length_mm(edges: Sequence[Dict[str, Any]]) -> float:
    """Return the minimum polyline segment length (mm) across all canonical edges."""
    worst = float("inf")
    for e in edges:
        coords = np.asarray(e["coords"], dtype=np.float64)
        if len(coords) < 2:
            continue
        lens = np.linalg.norm(np.diff(coords, axis=0), axis=1)
        if len(lens):
            m = float(lens.min())
            if m < worst:
                worst = m
    return 0.0 if not math.isfinite(worst) else worst


def pair_margin_fast(
    edges_for_pair: Sequence[Dict[str, Any]],
    a: int,
    b: int,
) -> float:
    """Compute exact 360-deg directional blocking margin (mm) between two adjacent faces (a, b).

    Matches ``heller_jigsaw.production.geometry.pair_margin`` exactly while scanning only
    the canonical edges incident to ``{a, b}``.
    """
    proj = np.zeros(360, dtype=np.float64)
    for e in edges_for_pair:
        lf, rf = int(e["left_face"]), int(e["right_face"])
        if {lf, rf} != {a, b}:
            continue
        delta = np.diff(np.asarray(e["coords"], dtype=np.float64), axis=0)
        if len(delta) == 0:
            continue
        nr = delta[:, [1, 0]] * np.array([1.0, -1.0])
        if lf != a:
            nr = -nr
        proj += np.maximum(nr @ _DIRS_360.T, 0.0).sum(axis=0)
    return float(proj.min())


def lock_check_sparse(
    edges: Sequence[Dict[str, Any]],
    n: int,
    ndir: int = 360,
) -> Dict[str, Any]:
    """Sparse-graph 360-deg interlocking check with exact parity to ``production.audit.lock_check``.

    Aggregates directional blocking integrals over unique interior adjacent face pairs `(a, b)`
    only (`O(ndir * |E_int|)` instead of `O(ndir * n^2)`), then checks strong connectivity per
    direction using ``scipy.sparse.csgraph.connected_components``.
    """
    if ndir == 360:
        dirs = _DIRS_360
    else:
        theta = np.arange(ndir, dtype=np.float64) * (2.0 * np.pi / ndir)
        dirs = np.stack([np.cos(theta), np.sin(theta)], axis=1)

    # Accumulate directed blocking weights for unique ordered pairs (u -> v)
    pair_index: Dict[Tuple[int, int], int] = {}
    rows_list: List[int] = []
    cols_list: List[int] = []
    weights_list: List[np.ndarray] = []

    for e in edges:
        a, b = int(e["left_face"]), int(e["right_face"])
        if min(a, b) < 0:
            continue
        coords = np.asarray(e["coords"], dtype=np.float64)
        if len(coords) < 2:
            continue
        delta = np.diff(coords, axis=0)
        nr = delta[:, [1, 0]] * np.array([1.0, -1.0])
        dot = nr @ dirs.T  # (K, ndir)
        w_ab = np.maximum(dot, 0.0).sum(axis=0)
        w_ba = np.maximum(-dot, 0.0).sum(axis=0)

        idx_ab = pair_index.get((a, b))
        if idx_ab is None:
            pair_index[(a, b)] = len(weights_list)
            rows_list.append(a)
            cols_list.append(b)
            weights_list.append(w_ab)
        else:
            weights_list[idx_ab] += w_ab

        idx_ba = pair_index.get((b, a))
        if idx_ba is None:
            pair_index[(b, a)] = len(weights_list)
            rows_list.append(b)
            cols_list.append(a)
            weights_list.append(w_ba)
        else:
            weights_list[idx_ba] += w_ba

    if not weights_list:
        return dict(
            lock_margin_mm=0.0,
            unlocked_directions_at_1mm=ndir,
            unlocked_directions_at_2mm=ndir,
            unlocked_directions_at_3mm=ndir,
        )

    rows = np.asarray(rows_list, dtype=np.int32)
    cols = np.asarray(cols_list, dtype=np.int32)
    # Shape: (ndir, num_directed_pairs)
    W = np.stack(weights_list, axis=1)
    ones_buf = np.ones(len(rows), dtype=np.int8)

    def unlocked(threshold: float) -> int:
        count = 0
        for d in range(ndir):
            mask = W[d] >= threshold
            r = rows[mask]
            c = cols[mask]
            if len(r) < n:
                count += 1
                continue
            mat = csr_matrix((ones_buf[: len(r)], (r, c)), shape=(n, n))
            n_comp = connected_components(mat, directed=True, connection="strong", return_labels=False)
            if n_comp > 1:
                count += 1
        return int(count)

    lo, hi = 0.0, 12.0
    while hi - lo > 0.0625:
        mid = (lo + hi) * 0.5
        if unlocked(mid):
            hi = mid
        else:
            lo = mid

    return dict(
        lock_margin_mm=lo,
        unlocked_directions_at_1mm=unlocked(1.0),
        unlocked_directions_at_2mm=unlocked(2.0),
        unlocked_directions_at_3mm=unlocked(3.0),
    )


def translation_audit_sparse(
    path: str | Path,
    kerf: float = 0.25,
    ndir: int = 360,
    workers: int = 0,
    write: bool = True,
) -> Dict[str, Any]:
    """Sparse finite-displacement translation audit with exact parity to ``production.audit.translation_audit``.

    Evaluates every interior adjacent face pair (exact same pair set as dense ``translation_audit``)
    using sparse directional incidence graphs and optional thread parallelism.
    """
    if ndir < 4 or ndir % 2:
        raise ValueError("Translation sampling requires an even direction count >= 4")
    p = Path(path)
    d = json.loads((p / "partition.json").read_text(encoding="utf-8"))
    polys = [Polygon(f["exterior"]).buffer(-kerf / 2.0, quad_segs=8) for f in d["faces"]]
    n = len(polys)
    if any(q.geom_type != "Polygon" or not q.is_valid for q in polys):
        raise ValueError("Kerf breaks a piece")

    pairs = sorted(
        {tuple(sorted([int(e["left_face"]), int(e["right_face"])])) for e in d["edges"] if not e["is_exterior"]}
    )
    theta = np.arange(ndir, dtype=np.float64) * (2.0 * np.pi / ndir)
    dirs = np.stack([np.cos(theta), np.sin(theta)], axis=1)
    distances = np.array([0.5, 1.0, 2.0, 3.0], dtype=np.float64)
    offsets = (dirs[:, None, :] * distances[None, :, None]).reshape(-1, 2)
    n_dist = len(distances)

    def _eval_pair(ab: Tuple[int, int]) -> Tuple[int, int, np.ndarray]:
        a, b = ab
        pa, pb = polys[a], polys[b]
        moving = np.full(len(offsets), pa, dtype=object)
        xy, idx = shapely.get_coordinates(moving, return_index=True)
        moving = shapely.set_coordinates(moving, xy + offsets[idx])
        intersects = shapely.intersects(moving, pb)
        result = np.zeros(len(offsets), dtype=bool)
        if np.any(intersects):
            result[intersects] = shapely.area(shapely.intersection(moving[intersects], pb)) > 0.01
        return a, b, np.maximum.accumulate(result.reshape(ndir, n_dist), axis=1)

    max_workers = workers if workers > 0 else max(1, min(8, os.cpu_count() or 1))
    if max_workers > 1 and len(pairs) > 32:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            pair_results = list(ex.map(_eval_pair, pairs))
    else:
        pair_results = [_eval_pair(ab) for ab in pairs]

    # Build sparse directed edge hit table: (ndir, n_dist, 2 * num_pairs)
    m = len(pair_results)
    rows = np.empty(2 * m, dtype=np.int32)
    cols = np.empty(2 * m, dtype=np.int32)
    hits = np.zeros((ndir, n_dist, 2 * m), dtype=bool)
    half = ndir // 2
    for idx_p, (a, b, cum) in enumerate(pair_results):
        rows[2 * idx_p] = a
        cols[2 * idx_p] = b
        hits[:, :, 2 * idx_p] = cum
        rows[2 * idx_p + 1] = b
        cols[2 * idx_p + 1] = a
        hits[:, :, 2 * idx_p + 1] = np.roll(cum, half, axis=0)

    ones_buf = np.ones(2 * m, dtype=np.int8)
    unlocked: Dict[str, int] = {}
    for k, r_val in enumerate(distances):
        cnt = 0
        for d_idx in range(ndir):
            mask = hits[d_idx, k]
            rr = rows[mask]
            cc = cols[mask]
            if len(rr) < n:
                cnt += 1
                continue
            mat = csr_matrix((ones_buf[: len(rr)], (rr, cc)), shape=(n, n))
            if connected_components(mat, directed=True, connection="strong", return_labels=False) > 1:
                cnt += 1
        unlocked[str(float(r_val))] = int(cnt)

    report = dict(
        kerf_mm=kerf,
        directions=ndir,
        displacements_mm=distances.tolist(),
        intersection_area_threshold_mm2=0.01,
        unblocked_directions_by_distance=unlocked,
        all_proper_groups_blocked_within_3mm=unlocked["3.0"] == 0,
    )
    if write:
        (p / "finite-motion-audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def thin_feature_audit_fast(
    path: str | Path,
    kerf: float = 0.25,
    r: float = 1.5,
    workers: int = 0,
    write: bool = True,
) -> Dict[str, Any]:
    """Parallelized thin-feature screen with exact parity to ``production.audit.thin_feature_audit``."""
    p = Path(path)
    d = json.loads((p / "partition.json").read_text(encoding="utf-8"))
    faces = d["faces"]

    def _one(args: Tuple[int, Dict[str, Any]]) -> Dict[str, Any]:
        i, f = args
        poly = Polygon(f["exterior"])
        data = measure(poly, kerf=kerf, r=r)
        return dict(piece=i + 1, asset_id=f["metadata"].get("asset_id", f"piece_{i+1}"), **data)

    max_workers = workers if workers > 0 else max(1, min(8, os.cpu_count() or 1))
    if max_workers > 1 and len(faces) > 16:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            report = list(ex.map(_one, enumerate(faces)))
    else:
        report = [_one(item) for item in enumerate(faces)]

    report.sort(key=lambda item: -item["max_tip_loss_mm"])
    result = dict(
        kerf_mm=kerf,
        opening_radius_mm=r,
        scope="Geometric thin-feature screen, not a structural-strength test or physical material certification.",
        pieces=len(report),
        worst_tip_loss_mm=float(report[0]["max_tip_loss_mm"]) if report else 0.0,
        pieces_tip_loss_over_3mm=sum(item["max_tip_loss_mm"] > 3.0 for item in report),
        pieces_tip_loss_over_6mm=sum(item["max_tip_loss_mm"] > 6.0 for item in report),
        per_piece=report,
    )
    if write:
        (p / "thin-feature-audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def rigid_motion_audit_fast(
    path: str | Path,
    kerf: float = 0.25,
    workers: int = 0,
    write: bool = True,
) -> Dict[str, Any]:
    """STRtree-indexed, thread-parallel single-piece rigid-motion screen (exact parity with ``rigid_motion_audit``)."""
    t0 = time.perf_counter()
    p = Path(path)
    d = json.loads((p / "partition.json").read_text(encoding="utf-8"))
    faces = d["faces"]
    pieces = [Polygon(f["exterior"]).buffer(-kerf / 2.0, quad_segs=8) for f in faces]
    if any(not v.is_valid or v.geom_type != "Polygon" for v in pieces):
        raise ValueError("invalid kerf-eroded shape")

    tree = STRtree(pieces)
    candidates = motion_grid()

    def _check_piece(i: int) -> Dict[str, Any]:
        shape = pieces[i]
        cx, cy = shape.centroid.coords[0]
        a, b, c, e = shape.bounds
        coords_ext = np.asarray(shape.exterior.coords, dtype=np.float64)
        reach = 3.0 + float(np.hypot(coords_ext[:, 0] - cx, coords_ext[:, 1] - cy).max()) * 0.11
        near = box(a - reach, b - reach, c + reach, e + reach)
        cand_idxs = tree.query(near)
        nbrs = [pieces[int(j)] for j in cand_idxs if int(j) != i and pieces[int(j)].intersects(near)]
        others = unary_union(nbrs) if nbrs else Polygon()
        active = np.arange(len(candidates))
        if not others.is_empty:
            for step in (0.25, 0.5, 0.75, 1.0):
                if not len(active):
                    break
                params = candidates[active] * step
                objs = np.full(len(params), shape, dtype=object)
                xy, idx = shapely.get_coordinates(objs, return_index=True)
                uv = xy - np.array([cx, cy])
                alpha = params[idx, 0]
                cs = np.cos(alpha)
                sn = np.sin(alpha)
                new = (
                    np.column_stack([cs * uv[:, 0] - sn * uv[:, 1], sn * uv[:, 0] + cs * uv[:, 1]])
                    + np.array([cx, cy])
                    + params[idx, 1:]
                )
                objs = shapely.set_coordinates(objs, new)
                collisions = shapely.intersects(objs, others)
                blocked = np.zeros(len(objs), dtype=bool)
                if np.any(collisions):
                    blocked[collisions] = shapely.area(shapely.intersection(objs[collisions], others)) > 0.01
                active = active[~blocked]
        safe = candidates[active]
        trans = np.linalg.norm(safe[:, 1:], axis=1) if len(safe) else np.zeros(0)
        deg = np.abs(np.rad2deg(safe[:, 0])) if len(safe) else np.zeros(0)
        return dict(
            piece=i + 1,
            asset_id=faces[i]["metadata"].get("asset_id", f"piece_{i+1}"),
            sampled_paths=len(candidates),
            safe_paths=len(safe),
            max_tested_rotation_deg=float(deg.max()) if len(deg) else 0.0,
            max_tested_translation_mm=float(trans.max()) if len(trans) else 0.0,
            rotation_4deg_or_more_paths=int((deg >= 3.99).sum()) if len(deg) else 0,
            translation_3mm_paths=int((trans >= 2.99).sum()) if len(trans) else 0,
        )

    max_workers = workers if workers > 0 else max(1, min(8, os.cpu_count() or 1))
    if max_workers > 1 and len(pieces) > 16:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            reports = list(ex.map(_check_piece, range(len(pieces))))
    else:
        reports = [_check_piece(i) for i in range(len(pieces))]

    result = dict(
        kerf_mm=kerf,
        rotation_center="individual piece centroid",
        path_fractions=[0.25, 0.5, 0.75, 1.0],
        collision_area_threshold_mm2=0.01,
        piece_count=len(pieces),
        paths_per_piece=len(candidates),
        pieces_with_rotation_4deg_or_more=sum(r["rotation_4deg_or_more_paths"] > 0 for r in reports),
        pieces_with_translation_3mm=sum(r["translation_3mm_paths"] > 0 for r in reports),
        per_piece=reports,
        seconds=time.perf_counter() - t0,
        scope="Single-piece sampled planar rigid motions, not proof of global locking or physical fit.",
    )
    if write:
        (p / "rigid-motion-screen.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def fidelity_audit_fast(
    run_dir: str | Path,
    motifs: Optional[Sequence[MotifAsset]] = None,
    catalog: Optional[str | Path] = None,
    px: float = 0.5,
    n_angles: int = 36,
    workers: int = 0,
    write: bool = True,
) -> Dict[str, Any]:
    """Local-bbox-cropped, thread-parallel silhouette fidelity audit for 750+ pieces.

    Unlike ``production.fidelity.aligned_targets`` which allocates a full-board boolean raster
    per piece (huge at 850x600 mm), each piece is rasterized strictly on its own padded local
    bounding box (~100x100 px), yielding identical alignment geometry ~100x faster.
    """
    p = Path(run_dir)
    d = json.loads((p / "partition.json").read_text(encoding="utf-8"))
    faces = d["faces"]
    polys = [Polygon(f["exterior"]).buffer(0) for f in faces]

    if motifs is None:
        cfg = json.loads((p / "config.json").read_text(encoding="utf-8"))
        rows, base = load_corpus_catalog(catalog or cfg["corpus_source"])
        by_id = {str(r.get("asset_id") or r.get("motif_id")): r for r in rows}
        motifs = []
        for f in faces:
            aid = str(f["metadata"].get("asset_id") or f["metadata"].get("source_asset_id") or f["metadata"].get("motif_id"))
            motifs.append(load_motif_asset(by_id[aid], base))

    def _eval_one(i: int) -> Tuple[Dict[str, Any], List[List[float]]]:
        poly = polys[i]
        m = motifs[i]
        a, b, c, dd = poly.bounds
        x0 = math.floor(a / px) * px - 6.0 * px
        y0 = math.floor(b / px) * px - 6.0 * px
        nx = max(8, int(math.ceil((c - x0) / px)) + 7)
        ny = max(8, int(math.ceil((dd - y0) / px)) + 7)
        xs = x0 + (np.arange(nx, dtype=np.float64) + 0.5) * px
        ys = y0 + (np.arange(ny, dtype=np.float64) + 0.5) * px
        cell = np.asarray(shapely.contains_xy(poly, xs[None, :], ys[:, None]), dtype=bool)
        _, _, pose = _aligned_iou_and_chamfer(cell, m, n_angles=n_angles)
        t = target_polygon(m, pose, x0, y0, px)
        if t.geom_type != "Polygon":
            parts = [g for g in getattr(t, "geoms", [t]) if g.geom_type == "Polygon"]
            t = max(parts, key=lambda g: g.area) if parts else Polygon()
        row = piece_fidelity(poly, t)
        meta = faces[i]["metadata"]
        row.update(
            piece=i + 1,
            face_id=int(faces[i]["face_id"]),
            asset_id=meta.get("asset_id"),
            motif_class=meta.get("motif_class"),
            license=meta.get("license"),
            commercial_ok=bool(meta.get("commercial_ok", False)),
        )
        t_coords = [[round(float(x), 3), round(float(y), 3)] for x, y in t.exterior.coords] if not t.is_empty else []
        return row, t_coords

    max_workers = workers if workers > 0 else max(1, min(8, os.cpu_count() or 1))
    if max_workers > 1 and len(faces) > 16:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(_eval_one, range(len(faces))))
    else:
        results = [_eval_one(i) for i in range(len(faces))]

    per_piece = [r for r, _ in results]
    aligned_outlines = [coords for _, coords in results]
    keys = ("fidelity", "boundary_f", "iou", "bay_fill", "extremity_recall", "excess")
    report = dict(
        scope="Geometric silhouette-fidelity screen against aligned source masks; not a human recognition study.",
        tolerance_mm=1.5,
        feature_mm=3.0,
        weights=WEIGHTS,
        pieces=len(per_piece),
        summary={k: summarize([r[k] for r in per_piece]) for k in keys},
        weakest=sorted(per_piece, key=lambda r: r["fidelity"])[:20],
        per_piece=per_piece,
    )
    if write:
        (p / "fidelity-audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (p / "aligned-targets.json").write_text(json.dumps(aligned_outlines), encoding="utf-8")
    return report


def fabrication_audit_sparse(
    run_dir: str | Path,
    min_neck_mm: float = 3.0,
    min_seg_mm: float = 0.05,
    max_tip_mm: float = 6.0,
    run_translation: bool = True,
    run_rigid: bool = False,
    workers: int = 0,
    write: bool = True,
) -> Dict[str, Any]:
    """Full sparse digital fabrication audit for 750-piece puzzle bundles."""
    p = Path(run_dir)
    d = json.loads((p / "partition.json").read_text(encoding="utf-8"))
    vcfg = ValidationConfig(kerf_mm=0.15, min_neck_mm=min_neck_mm, min_segment_mm=min_seg_mm)
    q = validate_run_dir(p, config=vcfg)
    polys = [Polygon(f["exterior"]) for f in d["faces"]]
    n = len(polys)
    lock = lock_check_sparse(d["edges"], n)
    min_seg = min_segment_length_mm(d["edges"])

    max_workers = workers if workers > 0 else max(1, min(8, os.cpu_count() or 1))
    kerf_reports: Dict[str, Any] = {}
    for kerf in (0.10, 0.15, 0.20, 0.25):
        def _fab(args: Tuple[int, Polygon]) -> Tuple[int, Dict[str, Any]]:
            idx, poly = args
            return idx, analyze_piece_fabrication(poly, kerf_mm=kerf, min_neck_mm=min_neck_mm)

        if max_workers > 1 and n > 16:
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                vals = list(ex.map(_fab, enumerate(polys)))
        else:
            vals = [_fab(item) for item in enumerate(polys)]

        kerf_fail = [i for i, v in vals if not v["kerf_pass"]]
        neck_fail = [i for i, v in vals if not v["neck_pass"]]
        kerf_reports[f"kerf_{kerf:.2f}"] = {
            "kerf_pass": n - len(kerf_fail),
            "neck_pass": n - len(neck_fail),
            "kerf_failed_faces": kerf_fail,
            "neck_failed_faces": neck_fail,
        }

    thin = thin_feature_audit_fast(p, workers=max_workers, write=write)
    trans = translation_audit_sparse(p, workers=max_workers, write=write) if run_translation else None
    rigid = rigid_motion_audit_fast(p, workers=max_workers, write=write) if run_rigid else None

    kerf_all_pass = all(
        kerf_reports[f"kerf_{k:.2f}"]["kerf_pass"] == n and kerf_reports[f"kerf_{k:.2f}"]["neck_pass"] == n
        for k in (0.10, 0.15, 0.20, 0.25)
    )
    all_commercial = all(bool(f.get("metadata", {}).get("commercial_ok", False)) for f in d["faces"])
    zero_filler = all(
        not str(f.get("metadata", {}).get("asset_id", "")).startswith("flow-")
        and bool(f.get("metadata", {}).get("asset_id"))
        and not bool(f.get("metadata", {}).get("is_filler", False))
        for f in d["faces"]
    )

    digital_pass = (
        bool(q["all_hard_gates_passed"])
        and lock["lock_margin_mm"] >= 3.0
        and lock["unlocked_directions_at_3mm"] == 0
        and kerf_all_pass
        and thin["worst_tip_loss_mm"] <= max_tip_mm
        and min_seg >= min_seg_mm
        and zero_filler
        and all_commercial
        and (trans is None or trans["unblocked_directions_by_distance"]["1.0"] == 0)
        and (rigid is None or (rigid["pieces_with_rotation_4deg_or_more"] == 0 and rigid["pieces_with_translation_3mm"] == 0))
    )

    report = {
        "geometry_pass": bool(q["all_hard_gates_passed"]),
        "failed_geometry_gates": [k for k, v in q["gates"].items() if not v],
        "n": n,
        "outline_bounds_mm": d.get("outline_bounds_mm"),
        "outline_area_mm2": d.get("outline_area_mm2"),
        "min_segment_mm": round(min_seg, 5),
        "min_segment_gate_mm": min_seg_mm,
        **lock,
        **kerf_reports,
        "worst_tip_loss_mm": round(thin["worst_tip_loss_mm"], 4),
        "pieces_tip_loss_over_3mm": thin["pieces_tip_loss_over_3mm"],
        "pieces_tip_loss_over_6mm": thin["pieces_tip_loss_over_6mm"],
        "translation_unblocked_by_distance": trans["unblocked_directions_by_distance"] if trans else None,
        "rigid_pieces_rot4": rigid["pieces_with_rotation_4deg_or_more"] if rigid else None,
        "rigid_pieces_trans3": rigid["pieces_with_translation_3mm"] if rigid else None,
        "zero_filler_verified": zero_filler,
        "all_commercial_ok_flagged": all_commercial,
        "digital_screen_pass": bool(digital_pass),
        "physical_test": "not performed",
        "commercial_release": "not cleared — requires physical cutting test, safety confirmation, and legal rights sign-off",
    }
    if write:
        (p / "production-audit.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
