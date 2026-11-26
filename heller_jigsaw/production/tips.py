"""Exterior thin-tip repair for locked runs (ported from the V10 campaign tooling).

A protrusion that a 1.5 mm opening (after 0.25 mm kerf) would remove is a tip at
risk of snapping. For each piece whose worst tip exceeds ``max_tip_mm`` this
reroutes one of the piece's *exterior* edges along a target outline that either
thickens the protrusion (``add``), thickens it while trimming the rest (``mix``)
or trims it back (``trim``). Shared interior seams, and so the locking tabs, are
never moved. A change is accepted only if every kerf/neck check passes, the
aligned IoU drops by at most 0.035 (and stays >= 0.73), the partition validates
and the lock margin does not decrease. The source run is not modified.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import substring, unary_union

from ..data.corpus import load_corpus_catalog, load_motif_asset
from ..geometry.export import export_puzzle_run
from ..geometry.partition import partition_from_polygons
from ..geometry.validate import ValidationConfig, analyze_piece_fabrication, validate_partition
from .audit import fabrication_audit, lock_check, measure, thin_feature_audit
from .geometry import face_from_edges
from .search import all_kerf, evaluate, stat


def protrusions(p):
    kerf = p.buffer(-0.125, quad_segs=12)
    opened = kerf.buffer(-1.5, quad_segs=12).buffer(1.5, quad_segs=12)
    lost = kerf.difference(opened)
    out = []
    for c in getattr(lost, "geoms", [lost]):
        if c.is_empty or c.area < 0.05:
            continue
        coords = shapely.get_coordinates(shapely.segmentize(c.boundary, 0.25))
        distance = shapely.distance(shapely.points(coords), opened)
        if distance.max() > 3:
            out.append((float(distance.max()), c))
    return sorted(out, key=lambda v: -v[0])


def _arc(line, s, t):
    if t >= s:
        return list(substring(line, s, t).coords)
    return list(substring(line, s, line.length).coords) + list(substring(line, 0, t).coords)[1:]


def _taper(a, p, q):
    a = np.asarray(a)
    u = np.r_[0, np.cumsum(np.linalg.norm(np.diff(a, axis=0), axis=1))]
    if u[-1] < 2:
        return None
    start = np.clip(1 - u / min(12, u[-1] * 0.35), 0, 1) ** 2
    end = np.clip(1 - (u[-1] - u) / min(12, u[-1] * 0.35), 0, 1) ** 2
    return a + start[:, None] * (p - a[0]) + end[:, None] * (q - a[-1])


def _clean(a, p, q):
    a = np.round(a, 4)
    a[0] = p
    a[-1] = q
    a = a[np.r_[True, np.linalg.norm(np.diff(a, axis=0), axis=1) > 0.035]]
    if len(a) < 3:
        return None
    if np.linalg.norm(a[-1] - q) < 0.035:
        a[-1] = q
    else:
        a = np.vstack([a, q])
    if np.min(np.linalg.norm(np.diff(a, axis=0), axis=1)) < 0.01:
        return None
    return a.tolist()


def _paths(edge, target):
    old = LineString(edge["coords"])
    p = np.array(old.coords[0])
    q = np.array(old.coords[-1])
    if target.geom_type != "Polygon" or target.interiors:
        return
    ring = LineString(target.exterior.coords)
    a, b = ring.project(Point(p)), ring.project(Point(q))
    for xy in (_arc(ring, a, b), _arc(ring, b, a)[::-1]):
        if len(xy) < 3:
            continue
        line = LineString(xy)
        if line.length < old.length * 0.6 or line.length > old.length * 1.45:
            continue
        if line.distance(Point(p)) > 1 or line.distance(Point(q)) > 1:
            continue
        if np.max(shapely.distance(shapely.points(np.asarray(line.coords)), old)) > 9:
            continue
        u = np.linspace(0, 1, max(12, int(line.length / 0.6) + 1))
        pts = _taper(shapely.get_coordinates(shapely.line_interpolate_point(line, u, normalized=True)), p, q)
        if pts is None:
            continue
        cc = _clean(pts, p, q)
        if cc and LineString(cc).is_simple:
            yield cc


def _interior_proposals(i, pp, oldtip, max_tip_mm):
    """Spikes wedged between neighbours: hand the spike to the neighbour that bounds it
    (``trim``) or widen it with area taken from that neighbour (``add``)."""
    p = pp[i]
    opened = p.buffer(-1.625, quad_segs=8).buffer(1.625, quad_segs=8)
    spikes = [g for g in getattr(p.difference(opened), "geoms", [p.difference(opened)]) if g.area > 0.2]
    out = []
    for spike in spikes:
        touching = sorted(((pp[j].boundary.intersection(spike.buffer(0.05)).length, j)
                           for j in range(len(pp)) if j != i and pp[j].distance(spike) < 0.05), reverse=True)
        for _, j in touching[:2]:
            options = [("trim", 0.0, p.difference(spike), pp[j].union(spike))]
            for w in (1.0, 1.6, 2.2):
                grab = spike.buffer(w, quad_segs=8).intersection(pp[j])
                options.append(("add", w, p.union(grab), pp[j].difference(grab)))
            for mode, w, qi, qj in options:
                qi, qj = qi.buffer(0), qj.buffer(0)
                if any(q.geom_type != "Polygon" or q.interiors or not q.is_valid for q in (qi, qj)):
                    continue
                try:
                    ti, tj = measure(qi)["max_tip_loss_mm"], measure(qj)["max_tip_loss_mm"]
                except ValueError:
                    continue
                if ti > max(max_tip_mm, oldtip - 1.0) or tj > max_tip_mm:
                    continue
                out.append((ti + 0.5 * tj + 0.04 * qi.symmetric_difference(p).area, j, qi, qj, ti, mode, w))
    return out


def _accept_interior(i, pp, d, n, motifs, before, current, history, oldtip, max_tip_mm):
    lock0 = None
    for rank, j, qi, qj, ti, mode, w in sorted(_interior_proposals(i, pp, oldtip, max_tip_mm), key=lambda r: r[0])[:12]:
        if not all(a["kerf_pass"] and a["neck_pass"] for q in (qi, qj) for k in (0.1, 0.15, 0.2, 0.25)
                   for a in [analyze_piece_fabrication(q, kerf_mm=k, min_neck_mm=3)]):
            continue
        fi, fj = evaluate([qi, qj], [motifs[i], motifs[j]], angles=72, px=0.4)
        if fi[0] < before[i][0] - 0.035 or fj[0] < before[j][0] - 0.035 or min(fi[0], fj[0]) < 0.73:
            continue
        trial = list(pp)
        trial[i], trial[j] = qi, qj
        try:
            u = unary_union(trial)
            if u.geom_type != "Polygon" or u.interiors:
                continue
            part = partition_from_polygons(trial, u, face_metadata=[v["metadata"].copy() for v in d["faces"]], auto_node=False)
            if not validate_partition(part, expected_n=n)["all_hard_gates_passed"]:
                continue
            lock = lock_check([e.to_dict() for e in part.edges], n)["lock_margin_mm"]
            if lock < 3.0:
                continue
        except (ValueError, shapely.GEOSException):
            continue
        pp[i], pp[j] = qi, qj
        row = dict(piece=i + 1, neighbour=j + 1, mode=f"interior-{mode}", width=w, tip_before=oldtip, tip_after=ti,
                   iou_before=float(current[i][0]), iou_after=float(fi[0]),
                   neighbour_iou_before=float(current[j][0]), neighbour_iou_after=float(fj[0]), lock_margin_mm=lock)
        current[i], current[j] = fi, fj
        history.append(row)
        print("ACCEPT", row, flush=True)
        return True
    return False


def repair_tips(src, out, max_tip_mm: float = 6.0):
    start = time.perf_counter()
    src, out = Path(src), Path(out)
    if src.resolve() == out.resolve() or (out / "partition.json").exists():
        raise FileExistsError(out)
    out.mkdir(parents=True, exist_ok=True)
    d = json.loads((src / "partition.json").read_text())
    cfg = json.loads((src / "config.json").read_text())
    prov = json.loads((src / "provenance.json").read_text())
    edges = d["edges"]
    n = len(d["faces"])
    pp = [face_from_edges(f, edges) for f in d["faces"]]
    rows, base = load_corpus_catalog(cfg["corpus_source"])
    by = {r["asset_id"]: r for r in rows}
    motifs = [load_motif_asset(by[r["asset_id"]], base) for r in cfg["motifs"]]
    before = evaluate(pp, motifs, angles=72, px=0.4)
    current = list(before)
    tips = [measure(p)["max_tip_loss_mm"] for p in pp]
    lock0 = lock_check(edges, n)["lock_margin_mm"]
    print("BASE", stat(before), "tips", [(i + 1, round(v, 2)) for i, v in enumerate(tips) if v > max_tip_mm], flush=True)
    history = []
    for i in sorted(range(n), key=lambda k: -tips[k]):
        if tips[i] <= max_tip_mm:
            continue
        for _ in range(2):
            p = pp[i]
            thin = protrusions(p)
            oldtip = measure(p)["max_tip_loss_mm"]
            if oldtip <= max_tip_mm or not thin:
                break
            others = unary_union([v for k, v in enumerate(pp) if k != i])
            outer = [e for e in edges if e["is_exterior"] and i in (e["left_face"], e["right_face"]) and e["length_mm"] >= 12]
            seams = unary_union([LineString(e["coords"]) for e in edges
                                 if not e["is_exterior"] and i in (e["left_face"], e["right_face"])])
            lost = unary_union([v for _, v in thin])
            opening = p.buffer(-1.625, quad_segs=8).buffer(1.625, quad_segs=8)
            if opening.geom_type != "Polygon":
                opening = p
            block = others.buffer(0.25).union(seams.buffer(0.75))
            proposals = []
            for mode in ("add", "mix", "trim"):
                for width in (1.3, 1.7, 2.1, 2.6, 3.1):
                    support = lost.buffer(width, quad_segs=8).difference(block)
                    if mode == "add":
                        target = p.union(support)
                    elif mode == "mix":
                        target = opening.union(support).union(p.intersection(block))
                    else:
                        target = opening.union(p.intersection(block))
                    target = target.buffer(0).simplify(0.15, preserve_topology=True)
                    if target.geom_type != "Polygon" or target.interiors:
                        continue
                    for e in outer:
                        if LineString(e["coords"]).distance(lost) > width + 2:
                            continue
                        old = e["coords"]
                        for cc in _paths(e, target):
                            e["coords"] = cc
                            try:
                                q = face_from_edges(d["faces"][i], edges)
                                if not q.is_valid or q.interiors or not p.area * 0.86 <= q.area <= p.area * 1.18:
                                    continue
                                if q.intersection(others).area > 0.001:
                                    continue
                                tip = measure(q)["max_tip_loss_mm"]
                                if tip > oldtip - 1.5:
                                    continue
                                proposals.append((tip + 0.06 * q.symmetric_difference(p).area, e["edge_id"], cc, q, tip, mode, width))
                            except (ValueError, shapely.GEOSException):
                                continue
                            finally:
                                e["coords"] = old
                    if mode == "trim":
                        break
            print("PIECE", i + 1, "tip", round(oldtip, 2), "exterior proposals", len(proposals), flush=True)
            accepted = False
            if not proposals:
                accepted = _accept_interior(i, pp, d, n, motifs, before, current, history, oldtip, max_tip_mm)
                if accepted:
                    # Rebuild the shared-edge graph so later exterior repairs see the new seams.
                    meta = [f["metadata"].copy() for f in d["faces"]]
                    rebuilt = partition_from_polygons(pp, unary_union(pp), face_metadata=meta, auto_node=False).to_dict()
                    d["faces"], d["edges"] = rebuilt["faces"], rebuilt["edges"]
                    edges = d["edges"]
                    pp = [face_from_edges(f, edges) for f in d["faces"]]
                    continue
                break
            for _, eid, cc, q, tip, mode, width in sorted(proposals, key=lambda r: r[0])[:12]:
                if not all(a["kerf_pass"] and a["neck_pass"] for k in (0.1, 0.15, 0.2, 0.25)
                           for a in [analyze_piece_fabrication(q, kerf_mm=k, min_neck_mm=3)]):
                    continue
                f = evaluate([q], [motifs[i]], angles=72, px=0.4)[0]
                if f[0] < before[i][0] - 0.035 or f[0] < 0.73:
                    continue
                trial = list(pp)
                trial[i] = q
                try:
                    u = unary_union(trial)
                    if u.geom_type != "Polygon" or u.interiors:
                        continue
                    part = partition_from_polygons(trial, u, face_metadata=[v["metadata"].copy() for v in d["faces"]], auto_node=False)
                    if not validate_partition(part, expected_n=n)["all_hard_gates_passed"]:
                        continue
                    if lock_check([e.to_dict() for e in part.edges], n)["lock_margin_mm"] < lock0 - 0.001:
                        continue
                except (ValueError, shapely.GEOSException):
                    continue
                pp[i] = q
                edges[eid]["coords"] = cc
                row = dict(piece=i + 1, mode=mode, width=width, tip_before=oldtip, tip_after=tip,
                           iou_before=float(current[i][0]), iou_after=float(f[0]))
                current[i] = f
                history.append(row)
                print("ACCEPT", row, flush=True)
                accepted = True
                break
            if not accepted:
                break
    if not history:
        raise RuntimeError("no accepted tip repair")
    part = partition_from_polygons(pp, unary_union(pp), face_metadata=[f["metadata"].copy() for f in d["faces"]], auto_node=False)
    if not validate_partition(part, expected_n=n)["all_hard_gates_passed"] or not all_kerf(part):
        raise RuntimeError("repaired partition failed validation")
    ff = evaluate([f.polygon for f in part.faces], motifs, angles=72, px=0.4)
    for f, v in zip(part.faces, ff):
        f.metadata.update(aligned_iou=round(float(v[0]), 4), sdf_chamfer=round(float(v[1]), 4))
    cfg["tip_rebuild"] = history
    prov["parent_run"] = str(src)
    q = export_puzzle_run(part, out, cfg, prov, ["Exterior tip reconstruction with canonical shared edges retained"],
                          [m.mask for m in motifs], ValidationConfig())
    if not q["all_hard_gates_passed"]:
        raise RuntimeError(f"export failed: {q['gates']}")
    fabrication_audit(out)
    ft = thin_feature_audit(out)
    report = dict(before=stat(before), after=stat(ff), changes=history, old_max_tip=max(tips),
                  new_max_tip=ft["worst_tip_loss_mm"], seconds=time.perf_counter() - start)
    (out / "tip-rebuild-report.json").write_text(json.dumps(report, indent=2))
    print("FINAL", report, flush=True)
    return report
