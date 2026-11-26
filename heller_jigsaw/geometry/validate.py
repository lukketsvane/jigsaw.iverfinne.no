"""Independent hard geometry, fabrication, interlocking, and SVG re-import validation gates.

Implements Dossier Sections 5 & 8 without trusting the optimizer or exporter.
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import numpy as np
import shapely
import shapely.affinity
from scipy import ndimage
from shapely.geometry import LineString, Polygon
from shapely.geometry.polygon import orient
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from .outline import SNAP, _flatten_svg_path, from_svg
from .partition import PlanarPartition


@dataclass
class ValidationConfig:
    """Configurable thresholds for hard fabrication and geometry gates."""

    coverage_rel_tol: float = 1e-4          # relative symmetric difference vs outline
    overlap_abs_tol_mm2: float = 1e-3       # max pairwise overlap area in mm^2
    exterior_dist_tol_mm: float = 0.05      # max distance from exterior edge to target outline
    min_segment_mm: float = 0.01            # min non-degenerate edge segment length
    min_area_mm2: float = 150.0             # absolute floor on piece area (mm^2)
    min_area_fraction_of_mean: float = 0.25 # no piece smaller than 25% of mean area
    max_area_ratio: float = 4.0             # max_area / min_area ceiling
    kerf_mm: float = 0.15                   # laser kerf width in mm (simulation)
    min_neck_mm: float = 3.0                # minimum finished load-bearing neck width in mm
    neck_secondary_lobe_mm2: float = 25.0   # detached eroded lobe >= this area counts as neck failure
    min_core_survival_ratio: float = 0.20   # fraction of piece surviving conservative neck erosion
    svg_reimport_rel_tol: float = 1e-3      # relative area correspondence on SVG re-import
    min_mean_blocked_fraction: float = 0.50 # directional in-plane blocking target
    min_interlocked_edge_fraction: float = 0.40  # fraction of interior edges with meaningful curvature/concavity
    interlock_deviation_mm: float = 1.5     # min chord-to-curve deviation for an interlocking edge


# ---------------------------------------------------------------------------
# Kerf, neck erosion, and medial-axis thickness analysis
# ---------------------------------------------------------------------------

def analyze_piece_fabrication(
    poly: Polygon,
    kerf_mm: float = 0.15,
    min_neck_mm: float = 3.0,
    secondary_lobe_mm2: float = 25.0,
    min_core_survival_ratio: float = 0.20,
) -> Dict[str, Any]:
    """Check laser kerf erosion and conservative neck-erosion survival on one piece."""
    area = float(poly.area)
    if area <= 0 or not poly.is_valid:
        return {
            "kerf_pass": False,
            "neck_pass": False,
            "kerf_components": 0,
            "kerf_area_ratio": 0.0,
            "core_components_all": 0,
            "core_load_bearing_components": 0,
            "core_survival_ratio": 0.0,
            "max_inscribed_diameter_mm": 0.0,
            "secondary_lobe_max_mm2": 0.0,
        }

    # 1. Laser kerf erosion: inward offset by kerf / 2 on each cut side
    kerf_r = kerf_mm * 0.5
    eroded_kerf = poly.buffer(-kerf_r, join_style="round", mitre_limit=2.0)
    kerf_parts = [
        p for p in shapely.get_parts(eroded_kerf)
        if p.geom_type == "Polygon" and p.area > 1e-3
    ]
    kerf_pass = len(kerf_parts) == 1 and kerf_parts[0].is_valid and (kerf_parts[0].area / area) > 0.5

    # 2. Conservative neck check: radius = (min_neck_mm + kerf_mm) / 2
    # Tapered tips cleanly shrink back without creating detached load-bearing lobes;
    # a narrow load-bearing bridge splits the eroded core into >= 2 substantial lobes!
    neck_r = (min_neck_mm + kerf_mm) * 0.5
    eroded_core = poly.buffer(-neck_r, join_style="round", mitre_limit=2.0)
    core_parts = sorted(
        [
            p for p in shapely.get_parts(eroded_core)
            if p.geom_type == "Polygon" and p.area > 0.5
        ],
        key=lambda p: p.area,
        reverse=True,
    )
    core_area = float(sum(p.area for p in core_parts))
    core_survival_ratio = core_area / area if area > 0 else 0.0
    load_bearing = [p for p in core_parts if p.area >= secondary_lobe_mm2]
    secondary_max = float(core_parts[1].area) if len(core_parts) >= 2 else 0.0

    # 3. Distance-transform inscribed diameter (fast 0.5 mm raster)
    x0, y0, x1, y1 = poly.bounds
    step = 0.5
    w = max(8, int(math.ceil((x1 - x0) / step)) + 4)
    h = max(8, int(math.ceil((y1 - y0) / step)) + 4)
    xx, yy = np.meshgrid(
        x0 + (np.arange(w) - 1.5) * step,
        y0 + (np.arange(h) - 1.5) * step,
    )
    mask = np.asarray(shapely.contains_xy(poly, xx, yy), dtype=bool)
    dist = ndimage.distance_transform_edt(mask) * step
    max_inscribed_diam_mm = float(2.0 * np.max(dist)) if mask.any() else 0.0

    # Pass criterion: core survives with meaningful area AND does NOT fracture into >= 2 load-bearing lobes
    neck_pass = (
        len(core_parts) >= 1
        and len(load_bearing) <= 1
        and core_survival_ratio >= min_core_survival_ratio
        and max_inscribed_diam_mm >= (min_neck_mm + kerf_mm) * 1.5
    )

    return {
        "kerf_pass": bool(kerf_pass),
        "neck_pass": bool(neck_pass),
        "kerf_components": len(kerf_parts),
        "kerf_area_ratio": round(float(sum(p.area for p in kerf_parts)) / area, 4),
        "core_components_all": len(core_parts),
        "core_load_bearing_components": len(load_bearing),
        "core_survival_ratio": round(core_survival_ratio, 4),
        "max_inscribed_diameter_mm": round(max_inscribed_diam_mm, 3),
        "secondary_lobe_max_mm2": round(secondary_max, 3),
    }


# ---------------------------------------------------------------------------
# Interlocking & contact concavity metrics
# ---------------------------------------------------------------------------

def analyze_edge_concavity(coords: List[Tuple[float, float]]) -> Dict[str, float]:
    """Measure chord-to-curve deviation, tortuosity, and bidirectional excursion of a shared edge."""
    arr = np.asarray(coords, dtype=np.float64)
    if len(arr) < 2:
        return {"chord_mm": 0.0, "arc_mm": 0.0, "tortuosity": 1.0, "max_dev_mm": 0.0, "pos_dev_mm": 0.0, "neg_dev_mm": 0.0}
    p0, p1 = arr[0], arr[-1]
    chord_vec = p1 - p0
    chord_len = float(np.hypot(chord_vec[0], chord_vec[1]))
    diffs = np.diff(arr, axis=0)
    arc_len = float(np.sum(np.hypot(diffs[:, 0], diffs[:, 1])))
    if chord_len < 1e-6:
        return {
            "chord_mm": 0.0,
            "arc_mm": round(arc_len, 4),
            "tortuosity": 10.0,
            "max_dev_mm": round(arc_len / math.pi, 4),
            "pos_dev_mm": round(arc_len / math.pi, 4),
            "neg_dev_mm": 0.0,
        }
    unit = chord_vec / chord_len
    normal = np.array([-unit[1], unit[0]], dtype=np.float64)
    rel = arr - p0
    signed_dist = rel @ normal
    pos_dev = float(np.max(signed_dist))
    neg_dev = float(-np.min(signed_dist))
    return {
        "chord_mm": round(chord_len, 4),
        "arc_mm": round(arc_len, 4),
        "tortuosity": round(arc_len / chord_len, 4),
        "max_dev_mm": round(max(pos_dev, neg_dev), 4),
        "pos_dev_mm": round(pos_dev, 4),
        "neg_dev_mm": round(neg_dev, 4),
    }


def analyze_directional_interlock(
    faces: List[Polygon],
    n_angles: int = 36,
    shift_mm: float = 2.0,
    overlap_tol_mm2: float = 0.25,
) -> List[Dict[str, Any]]:
    """Test in-plane sliding freedom of every piece against the assembled neighbors."""
    angles = np.linspace(0.0, 2.0 * math.pi, n_angles, endpoint=False)
    results: List[Dict[str, Any]] = []
    n = len(faces)
    bounds = [f.bounds for f in faces]
    pad = shift_mm + 1e-3
    for i, poly in enumerate(faces):
        x0, y0, x1, y1 = bounds[i]
        others = [
            faces[j]
            for j in range(n)
            if j != i
            and bounds[j][2] >= x0 - pad
            and bounds[j][0] <= x1 + pad
            and bounds[j][3] >= y0 - pad
            and bounds[j][1] <= y1 + pad
        ]
        if not others:
            results.append({"blocked_fraction": 0.0, "escape_cone_deg": 360.0})
            continue
        other_union = unary_union(others)
        blocked = 0
        for th in angles:
            dx = shift_mm * math.cos(th)
            dy = shift_mm * math.sin(th)
            shifted = shapely.affinity.translate(poly, xoff=dx, yoff=dy)
            if shifted.intersects(other_union):
                if shifted.intersection(other_union).area > overlap_tol_mm2:
                    blocked += 1
        frac = blocked / float(n_angles)
        results.append({
            "blocked_fraction": round(frac, 4),
            "escape_cone_deg": round((1.0 - frac) * 360.0, 2),
        })
    return results


# ---------------------------------------------------------------------------
# Core partition validator
# ---------------------------------------------------------------------------

def validate_partition(
    partition: PlanarPartition,
    expected_n: Optional[int] = None,
    config: Optional[ValidationConfig] = None,
) -> Dict[str, Any]:
    """Run all hard topology, coverage, overlap, incidence, fabrication, and interlock gates."""
    cfg = config or ValidationConfig()
    outline = partition.outline
    outline_area = float(outline.area)
    faces = partition.faces
    polys = [f.polygon for f in faces]
    n = len(faces)
    target_n = expected_n if expected_n is not None else n

    # 1. Exact piece count
    piece_count_ok = (n == target_n) and (n > 0)

    # 2. Valid connected polygons & zero interior holes
    invalid_faces = [
        f.face_id for f in faces
        if (f.polygon.is_empty or not f.polygon.is_valid or f.polygon.geom_type != "Polygon" or f.polygon.area <= 0)
    ]
    holed_faces = [f.face_id for f in faces if len(f.polygon.interiors) > 0]
    all_faces_valid = len(invalid_faces) == 0
    no_holes = len(holed_faces) == 0

    # 3. Coverage symmetric difference vs physical target outline
    if all_faces_valid and n > 0:
        union_poly = unary_union(polys)
        sym_diff_area = float(union_poly.symmetric_difference(outline).area)
        coverage_rel_err = sym_diff_area / outline_area if outline_area > 0 else float("inf")
    else:
        sym_diff_area = float("inf")
        coverage_rel_err = float("inf")
    coverage_ok = coverage_rel_err <= cfg.coverage_rel_tol

    # 4. Pairwise overlaps
    max_overlap_mm2 = 0.0
    overlapping_pairs: List[Tuple[int, int, float]] = []
    if all_faces_valid and n > 1:
        tree = STRtree(polys)
        for i, pi in enumerate(polys):
            for j in tree.query(pi):
                j = int(j)
                if j <= i:
                    continue
                inter = pi.intersection(polys[j])
                if not inter.is_empty and inter.area > 0:
                    ia = float(inter.area)
                    if ia > max_overlap_mm2:
                        max_overlap_mm2 = ia
                    if ia > cfg.overlap_abs_tol_mm2:
                        overlapping_pairs.append((i, j, round(ia, 6)))
    no_overlaps = len(overlapping_pairs) == 0

    # 5. Planar edge incidence, unique cuts, crossings, Euler characteristic, adjacency connectivity
    bad_interior_edges: List[int] = []
    bad_exterior_edges: List[int] = []
    self_intersecting_edges: List[int] = []
    tiny_segment_edges: List[int] = []
    edge_lines: List[LineString] = []
    outline_ring = outline.exterior

    min_seg_len = float("inf")
    for e in partition.edges:
        ls = e.to_linestring()
        edge_lines.append(ls)
        if not ls.is_simple:
            self_intersecting_edges.append(e.edge_id)
        arr = np.asarray(e.coords, dtype=np.float64)
        if len(arr) >= 2:
            seg_lens = np.hypot(np.diff(arr[:, 0]), np.diff(arr[:, 1]))
            smin = float(np.min(seg_lens))
            if smin < min_seg_len:
                min_seg_len = smin
            if smin < cfg.min_segment_mm:
                tiny_segment_edges.append(e.edge_id)
        if e.is_interior:
            if not (0 <= e.left_face < n and 0 <= e.right_face < n and e.left_face != e.right_face):
                bad_interior_edges.append(e.edge_id)
        elif e.is_exterior:
            valid_inc = (0 <= e.left_face < n and e.right_face == -1) or (0 <= e.right_face < n and e.left_face == -1)
            # Check midpoint and vertices lie on target outline
            dist_to_outline = float(ls.hausdorff_distance(ls.intersection(outline_ring.buffer(cfg.exterior_dist_tol_mm)))) if not ls.intersection(outline_ring.buffer(cfg.exterior_dist_tol_mm)).is_empty else float("inf")
            max_v_dist = float(max(outline_ring.distance(shapely.Point(c)) for c in e.coords))
            if not valid_inc or max_v_dist > cfg.exterior_dist_tol_mm or dist_to_outline > cfg.exterior_dist_tol_mm:
                bad_exterior_edges.append(e.edge_id)
        else:
            bad_exterior_edges.append(e.edge_id)

    # Check duplicate / overlapping or crossing edges
    duplicate_or_crossing_edges: List[Tuple[int, int, str]] = []
    if len(edge_lines) > 1:
        etree = STRtree(edge_lines)
        for i, la in enumerate(edge_lines):
            ea = partition.edges[i]
            ends_a = {ea.coords[0], ea.coords[-1]}
            for j in etree.query(la):
                j = int(j)
                if j <= i:
                    continue
                lb = edge_lines[j]
                if not la.intersects(lb):
                    continue
                inter = la.intersection(lb)
                if inter.is_empty:
                    continue
                eb = partition.edges[j]
                shared_ends = ends_a & {eb.coords[0], eb.coords[-1]}
                for part in shapely.get_parts(inter):
                    if part.geom_type in ("LineString", "MultiLineString") and part.length > SNAP * 10:
                        duplicate_or_crossing_edges.append((ea.edge_id, eb.edge_id, "duplicate_overlap"))
                        break
                    elif part.geom_type == "Point":
                        pt = (part.x, part.y)
                        if not any(math.hypot(pt[0] - s[0], pt[1] - s[1]) < SNAP * 10 for s in shared_ends):
                            duplicate_or_crossing_edges.append((ea.edge_id, eb.edge_id, "interior_crossing"))
                            break

    # Euler characteristic V - E + F = 1 for simply-connected domain with connected cut graph
    v_cnt = len(partition.vertices)
    e_cnt = len(partition.edges)
    euler_val = v_cnt - e_cnt + n
    cut_graph = nx.Graph()
    for e in partition.edges:
        cut_graph.add_edge(e.v_start, e.v_end)
    cut_graph_connected = nx.is_connected(cut_graph) if len(cut_graph) > 0 else False
    euler_ok = (euler_val == 1) and cut_graph_connected

    adj_graph = nx.Graph()
    adj_graph.add_nodes_from(range(n))
    for u, nbrs in partition.adjacency.items():
        for v in nbrs:
            adj_graph.add_edge(u, v)
    adj_connected = nx.is_connected(adj_graph) if n > 0 else False

    edge_incidence_ok = (
        len(bad_interior_edges) == 0
        and len(bad_exterior_edges) == 0
        and len(self_intersecting_edges) == 0
        and len(duplicate_or_crossing_edges) == 0
        and euler_ok
        and adj_connected
    )

    # 6. Area distribution & sliver check
    areas = [f.area_mm2 for f in faces] if faces else [0.0]
    min_area = float(min(areas))
    max_area = float(max(areas))
    mean_area = float(np.mean(areas))
    area_ratio = (max_area / min_area) if min_area > 0 else float("inf")
    area_floor = min(cfg.min_area_mm2, mean_area * cfg.min_area_fraction_of_mean)
    no_slivers_ok = (
        min_area >= area_floor
        and area_ratio <= cfg.max_area_ratio
        and len(tiny_segment_edges) == 0
    )

    # 7. Fabrication kerf & conservative neck erosion check
    fab_per_piece: List[Dict[str, Any]] = []
    kerf_failed: List[int] = []
    neck_failed: List[int] = []
    for f in faces:
        fab = analyze_piece_fabrication(
            f.polygon,
            kerf_mm=cfg.kerf_mm,
            min_neck_mm=cfg.min_neck_mm,
            secondary_lobe_mm2=cfg.neck_secondary_lobe_mm2,
            min_core_survival_ratio=cfg.min_core_survival_ratio,
        )
        fab["face_id"] = f.face_id
        fab_per_piece.append(fab)
        if not fab["kerf_pass"]:
            kerf_failed.append(f.face_id)
        if not fab["neck_pass"]:
            neck_failed.append(f.face_id)

    kerf_ok = len(kerf_failed) == 0
    neck_ok = len(neck_failed) == 0

    # 8. Interlocking & contact concavity
    int_edges = partition.interior_edges
    edge_concavities = [analyze_edge_concavity(e.coords) for e in int_edges]
    interlocked_edges = [
        c for c in edge_concavities
        if c["max_dev_mm"] >= cfg.interlock_deviation_mm or c["tortuosity"] >= 1.06
    ]
    interlocked_edge_frac = (
        len(interlocked_edges) / float(len(int_edges)) if int_edges else 0.0
    )
    mean_tortuosity = (
        float(np.mean([c["tortuosity"] for c in edge_concavities])) if edge_concavities else 1.0
    )
    mean_max_dev = (
        float(np.mean([c["max_dev_mm"] for c in edge_concavities])) if edge_concavities else 0.0
    )

    dir_interlock = analyze_directional_interlock(polys) if all_faces_valid else []
    mean_blocked_frac = (
        float(np.mean([d["blocked_fraction"] for d in dir_interlock])) if dir_interlock else 0.0
    )
    interlocking_ok = (
        mean_blocked_frac >= cfg.min_mean_blocked_fraction
        and interlocked_edge_frac >= cfg.min_interlocked_edge_fraction
    )

    gates = {
        "piece_count_exact": piece_count_ok,
        "all_faces_valid": all_faces_valid,
        "no_interior_holes": no_holes,
        "coverage_exact": coverage_ok,
        "no_overlaps": no_overlaps,
        "planar_edge_incidence": edge_incidence_ok,
        "no_slivers_or_tiny_segments": no_slivers_ok,
        "kerf_erosion_survival": kerf_ok,
        "neck_thickness_survival": neck_ok,
        "interlocking_pass": interlocking_ok,
    }

    per_piece_summary: List[Dict[str, Any]] = []
    for idx, f in enumerate(faces):
        rec: Dict[str, Any] = {
            "face_id": f.face_id,
            "area_mm2": round(f.area_mm2, 3),
            "perimeter_mm": round(f.perimeter_mm, 3),
            "num_neighbors": len(partition.adjacency.get(f.face_id, [])),
            **fab_per_piece[idx],
        }
        if idx < len(dir_interlock):
            rec.update(dir_interlock[idx])
        if f.metadata:
            for k in ("motif_class", "family", "asset_id", "aligned_iou", "sdf_chamfer"):
                if k in f.metadata:
                    rec[k] = f.metadata[k]
        per_piece_summary.append(rec)

    return {
        "all_hard_gates_passed": all(gates.values()),
        "gates": gates,
        "config": asdict(cfg),
        "summary": {
            "num_pieces": n,
            "expected_n": target_n,
            "outline_area_mm2": round(outline_area, 4),
            "coverage_sym_diff_mm2": round(sym_diff_area, 6) if math.isfinite(sym_diff_area) else None,
            "coverage_rel_error": coverage_rel_err if math.isfinite(coverage_rel_err) else None,
            "max_pairwise_overlap_mm2": round(max_overlap_mm2, 6),
            "num_vertices": v_cnt,
            "num_edges": e_cnt,
            "num_interior_edges": len(int_edges),
            "num_exterior_edges": len(partition.exterior_edges),
            "euler_V_minus_E_plus_F": euler_val,
            "cut_graph_connected": cut_graph_connected,
            "adjacency_connected": adj_connected,
            "total_cut_length_mm": round(partition.total_cut_length_mm(), 3),
            "min_segment_mm": round(min_seg_len, 5) if math.isfinite(min_seg_len) else 0.0,
            "min_piece_area_mm2": round(min_area, 3),
            "max_piece_area_mm2": round(max_area, 3),
            "mean_piece_area_mm2": round(mean_area, 3),
            "area_ratio_max_min": round(area_ratio, 4) if math.isfinite(area_ratio) else None,
            "mean_edge_tortuosity": round(mean_tortuosity, 4),
            "mean_edge_deviation_mm": round(mean_max_dev, 3),
            "interlocked_edge_fraction": round(interlocked_edge_frac, 4),
            "mean_blocked_direction_fraction": round(mean_blocked_frac, 4),
        },
        "diagnostics": {
            "invalid_faces": invalid_faces,
            "holed_faces": holed_faces,
            "overlapping_pairs": overlapping_pairs,
            "bad_interior_edges": bad_interior_edges,
            "bad_exterior_edges": bad_exterior_edges,
            "self_intersecting_edges": self_intersecting_edges,
            "duplicate_or_crossing_edges": duplicate_or_crossing_edges,
            "tiny_segment_edges": tiny_segment_edges,
            "kerf_failed_faces": kerf_failed,
            "neck_failed_faces": neck_failed,
        },
        "per_piece": per_piece_summary,
    }


# ---------------------------------------------------------------------------
# Independent SVG re-import validator
# ---------------------------------------------------------------------------

def _parse_cut_svg_paths(svg_path: Path) -> Tuple[List[LineString], Dict[str, Any]]:
    """Independently parse open/closed `<path>` elements from `cut.svg`."""
    root = ET.parse(str(svg_path)).getroot()
    width_attr = root.attrib.get("width", "")
    height_attr = root.attrib.get("height", "")
    viewbox = root.attrib.get("viewBox", "")

    lines: List[LineString] = []
    text_elements = 0
    filled_paths = 0

    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "text":
            text_elements += 1
        elif tag == "path":
            fill = el.attrib.get("fill", "none").strip().lower()
            if fill not in ("", "none"):
                filled_paths += 1
            d = el.attrib.get("d", "")
            # Parse open/closed paths using independent M/L flattening
            # Note: `_flatten_svg_path` requires >=3 pts per ring; handle 2-point straight lines too!
            import re
            tok = re.findall(r"[MmLlHhVvZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", d)
            cur: List[Tuple[float, float]] = []
            pos = [0.0, 0.0]
            start = [0.0, 0.0]
            i = 0
            cmd = None
            while i < len(tok):
                if re.match(r"^[A-Za-z]$", tok[i]):
                    cmd = tok[i]
                    i += 1
                if cmd is None:
                    break
                c = cmd.upper()
                rel = cmd.islower()
                if c == "M":
                    if len(cur) >= 2:
                        lines.append(LineString(cur))
                    x = float(tok[i]) + (pos[0] if rel else 0.0)
                    y = float(tok[i + 1]) + (pos[1] if rel else 0.0)
                    i += 2
                    pos = [x, y]
                    start = [x, y]
                    cur = [(x, y)]
                    cmd = "l" if rel else "L"
                elif c == "L":
                    x = float(tok[i]) + (pos[0] if rel else 0.0)
                    y = float(tok[i + 1]) + (pos[1] if rel else 0.0)
                    i += 2
                    pos = [x, y]
                    cur.append((x, y))
                elif c == "H":
                    x = float(tok[i]) + (pos[0] if rel else 0.0)
                    i += 1
                    pos[0] = x
                    cur.append((pos[0], pos[1]))
                elif c == "V":
                    y = float(tok[i]) + (pos[1] if rel else 0.0)
                    i += 1
                    pos[1] = y
                    cur.append((pos[0], pos[1]))
                elif c == "Z":
                    if cur:
                        cur.append((start[0], start[1]))
                        if len(cur) >= 2:
                            lines.append(LineString(cur))
                    cur = []
                    pos = list(start)
            if len(cur) >= 2:
                lines.append(LineString(cur))

    meta = {
        "width": width_attr,
        "height": height_attr,
        "viewBox": viewbox,
        "units_mm": width_attr.endswith("mm") and height_attr.endswith("mm"),
        "text_elements": text_elements,
        "filled_paths": filled_paths,
        "num_paths": len(lines),
    }
    return lines, meta


def _parse_pieces_svg_polygons(svg_path: Path) -> List[Polygon]:
    """Independently parse closed piece polygons from `pieces.svg`."""
    root = ET.parse(str(svg_path)).getroot()
    polys: List[Polygon] = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "path":
            for ring in _flatten_svg_path(el.attrib.get("d", "")):
                p = Polygon(ring)
                if p.is_valid and p.area > 0:
                    polys.append(orient(p, 1.0))
    return polys


def validate_run_dir(
    run_dir: str | Path,
    config: Optional[ValidationConfig] = None,
) -> Dict[str, Any]:
    """Independently validate an exported puzzle run directory (Section 5.2 bundle + SVG re-import)."""
    run_dir = Path(run_dir)
    cfg = config or ValidationConfig()

    required_files = [
        "config.json",
        "outline.svg",
        "cut.svg",
        "pieces.svg",
        "assembly.svg",
        "partition.json",
        "provenance.json",
        "metrics.csv",
    ]
    missing_files = [f for f in required_files if not (run_dir / f).exists()]

    outline_poly = from_svg(run_dir / "outline.svg")
    with open(run_dir / "partition.json", "r", encoding="utf-8") as f:
        part_json = json.load(f)
    declared_n = int(part_json["num_pieces"])
    config_path = run_dir / "config.json"
    run_config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
    expected_n = int(run_config.get("num_pieces", declared_n))

    # 1. Independently re-import cut.svg
    cut_lines, cut_meta = _parse_cut_svg_paths(run_dir / "cut.svg")
    # Check duplicate/overlapping cuts inside cut.svg
    dup_cut_pairs: List[Tuple[int, int]] = []
    if len(cut_lines) > 1:
        tree = STRtree(cut_lines)
        for i, la in enumerate(cut_lines):
            for j in tree.query(la):
                j = int(j)
                if j <= i:
                    continue
                inter = la.intersection(cut_lines[j])
                if not inter.is_empty:
                    for part in shapely.get_parts(inter):
                        if part.geom_type in ("LineString", "MultiLineString") and part.length > 0.01:
                            dup_cut_pairs.append((i, j))
                            break

    # Reconstruct faces strictly from cut.svg linework
    snapped_cuts = [shapely.set_precision(ls, grid_size=SNAP) for ls in cut_lines]
    reimported_faces = [
        orient(p, 1.0)
        for p in polygonize(unary_union(snapped_cuts))
        if p.geom_type == "Polygon" and p.is_valid and p.area > 1e-4
    ]
    # Keep faces inside outline
    reimported_faces = [
        p for p in reimported_faces if outline_poly.contains(p.representative_point())
    ]

    # Compare union of cut.svg-reconstructed faces against outline.svg
    if reimported_faces:
        cut_union = unary_union(reimported_faces)
        cut_cov_rel = float(cut_union.symmetric_difference(outline_poly).area) / float(outline_poly.area)
    else:
        cut_cov_rel = float("inf")

    # 2. Independently re-import pieces.svg and check 1-to-1 correspondence with cut.svg faces
    pieces_svg_polys = _parse_pieces_svg_polygons(run_dir / "pieces.svg")
    max_piece_correspondence_err = 0.0
    if len(reimported_faces) == expected_n and len(pieces_svg_polys) == expected_n:
        for pp in pieces_svg_polys:
            best_rel = min(
                float(pp.symmetric_difference(rf).area) / max(float(pp.area), 1e-6)
                for rf in reimported_faces
            )
            max_piece_correspondence_err = max(max_piece_correspondence_err, best_rel)
    else:
        max_piece_correspondence_err = float("inf")

    # 3. Check per-piece isolated SVG & PNG files
    pieces_dir = run_dir / "pieces"
    missing_piece_files: List[str] = []
    for idx in range(1, expected_n + 1):
        for ext in ("svg", "png"):
            pfile = pieces_dir / f"{idx:04d}.{ext}"
            if not pfile.exists():
                missing_piece_files.append(f"pieces/{idx:04d}.{ext}")

    # 4. Run full partition validation on the cut.svg-reconstructed partition!
    from .partition import partition_from_polygons
    part_report: Dict[str, Any]
    if len(reimported_faces) == expected_n:
        rebuilt_part = partition_from_polygons(reimported_faces, outline_poly, auto_node=False)
        part_report = validate_partition(rebuilt_part, expected_n=expected_n, config=cfg)
    else:
        part_report = {"all_hard_gates_passed": False, "gates": {"piece_count_exact": False}}

    svg_gates = {
        "required_bundle_files_present": len(missing_files) == 0 and len(missing_piece_files) == 0,
        "cut_svg_mm_units": bool(cut_meta["units_mm"]),
        "cut_svg_no_labels_or_fills": cut_meta["text_elements"] == 0 and cut_meta["filled_paths"] == 0,
        "cut_svg_unique_edges_no_duplicates": len(dup_cut_pairs) == 0,
        "cut_svg_reimport_exact_n": len(reimported_faces) == expected_n == declared_n,
        "cut_svg_reimport_coverage": cut_cov_rel <= cfg.svg_reimport_rel_tol,
        "cut_vs_pieces_svg_correspondence": max_piece_correspondence_err <= cfg.svg_reimport_rel_tol,
    }

    combined_gates = dict(part_report.get("gates", {}))
    combined_gates.update(svg_gates)

    return {
        "all_hard_gates_passed": all(combined_gates.values()),
        "gates": combined_gates,
        "svg_reimport": {
            "cut_svg_meta": cut_meta,
            "duplicate_cut_pairs": dup_cut_pairs,
            "reimported_face_count": len(reimported_faces),
            "pieces_svg_face_count": len(pieces_svg_polys),
            "cut_svg_coverage_rel_error": cut_cov_rel if math.isfinite(cut_cov_rel) else None,
            "max_piece_correspondence_rel_error": (
                max_piece_correspondence_err if math.isfinite(max_piece_correspondence_err) else None
            ),
            "missing_files": missing_files,
            "missing_piece_files": missing_piece_files,
        },
        "partition_validation": part_report,
    }
