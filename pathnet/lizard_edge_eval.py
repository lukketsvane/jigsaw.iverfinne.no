"""Reimport, pair and measure the actual-size lizard cut files.

Use a baseline directory and one candidate directory exported in solid-lizard SVG
format. Both exporters must preserve piece IDs, the first sample, winding and
the 320 original contour sample slots. Measures are from the SVGs, in mm.
"""
import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import MultiLineString, Point, Polygon

from .lizards import SVG_NS, _parse_paths


SIZE_MM = (298.45, 236.22)
NECK_MM = 3.0
KERF_MM = 0.25
MIN_IOU = 0.75
MEDIAN_IOU = 0.90
TIP_ALERT_MM = 2.0
TICKS_PER_MM = 10000
EXPECTED_PIECES = 216
SAMPLES_PER_OUTLINE = 320
# Radial tips on the authored p3 template, in its existing contour order.
# The midpoints between neighboring tips delimit a separate search for each.
ANATOMICAL_TIPS = {
    "snout": 14, "foot_a": 58, "foot_b": 115, "tail": 164, "foot_c": 212, "foot_d": 279,
}
NS = {"s": SVG_NS}


@dataclass
class Piece:
    polygon: Polygon
    outline: np.ndarray
    neck_components: int
    kerf_components: int
    eye_clearance: float
    spine_clearance: float


def component_count(polygon, radius):
    core = polygon.buffer(-radius)
    return 0 if core.is_empty else int(shapely.get_num_geometries(core))


def ring_segments(coords):
    return np.concatenate([coords, coords[:1]], axis=0)


def read_periods(directory):
    for name in ("audit.json", "quality.json", "board.json"):
        file = directory / name
        if file.is_file():
            data = json.loads(file.read_text(encoding="utf-8"))
            matrix = data.get("periods_mm", data.get("periods"))
            if matrix is not None:
                matrix = np.asarray(matrix, dtype=float)
                if matrix.shape == (2, 2):
                    if not np.allclose(matrix, np.diag(np.diag(matrix))):
                        raise ValueError("The lizard exporter must provide an axis-aligned repeat")
                    matrix = np.diag(matrix)
                if matrix.shape == (2,) and np.all(matrix > 0):
                    return matrix
    raise ValueError(f"No rectangular repeat found for {directory}")


def check_periodic_paths(outlines, periods):
    # Index every directed SVG segment on the torus at the generator's precision.
    # Subtract a whole number of periods before comparing opposing traversals.
    period_ticks = np.rint(periods * TICKS_PER_MM).astype(np.int64)
    segments, ids = [], []
    for pid, coords in sorted(outlines.items()):
        pts = np.rint(ring_segments(coords) * TICKS_PER_MM).astype(np.int64) % period_ticks
        segments.append(np.column_stack([pts[:-1], pts[1:]]))
        ids.extend([pid] * (len(pts)-1))
    segments = np.vstack(segments)
    ids = np.array(ids)
    keys, sign = canonical_segments(segments)
    order = np.lexsort(keys.T[::-1])
    if len(order) % 2:
        raise ValueError("Odd number of periodic segment traversals")
    left, right = order[::2], order[1::2]
    if (not np.all(keys[left] == keys[right]) or np.any(sign[left] == sign[right])
            or np.any(ids[left] == ids[right])
            or np.any(np.all(keys[right[:-1]] == keys[left[1:]], axis=1))):
        raise ValueError("Periodic segments must have precisely two opposing uses by distinct pieces")
    neighbors = np.unique(np.sort(np.column_stack([ids[left], ids[right]]), axis=1), axis=0)
    if len(neighbors) != 648:
        raise ValueError(f"Unexpected number of periodic neighbor pairs: {len(neighbors)}")
    return len(left), len(neighbors)


def canonical_segments(segments):
    a, b = segments[:, :2], segments[:, 2:]
    if np.any(np.all(a == b, axis=1)):
        raise ValueError("Zero-length SVG segment")
    forward = (a[:, 0] < b[:, 0]) | ((a[:, 0] == b[:, 0]) & (a[:, 1] < b[:, 1]))
    return np.where(forward[:, None], segments, np.column_stack([b, a])), forward


def read_board(directory):
    directory = Path(directory)
    art = ET.parse(directory / "artwork.svg").getroot()
    cut = ET.parse(directory / "cut.svg").getroot()
    marks = ET.parse(directory / "engrave.svg").getroot()
    reference_page = np.fromstring(cut.attrib["viewBox"], sep=" ")
    for root in (art, cut, marks):
        if any("transform" in elem.attrib for elem in root.iter()):
            raise ValueError("The evaluator expects exported coordinates already in millimeters")
        vb = list(map(float, root.attrib["viewBox"].split()))
        dims = [float(root.attrib[key].removesuffix("mm")) for key in ("width", "height")]
        if (not np.allclose(vb[2:], dims, atol=0.00015, rtol=0)
                or not np.allclose(vb, reference_page, atol=0.00015, rtol=0)):
            raise ValueError("Print, engraving and cut must have a shared millimeter viewBox")
    if any(el.tag.split("}")[-1] not in {"svg", "g", "path"} for el in cut.iter()):
        raise ValueError("The cut file must contain only cutting paths")

    outlines, paths_by_face = {}, {}
    for group in art.findall(".//s:g", NS):
        name = group.get("id", "")
        if not name.startswith("lizard-"):
            continue
        pid = int(name.removeprefix("lizard-"))
        pts = np.asarray(_parse_paths([group.find("s:path", NS)])[0].coords)
        if len(pts) > 1 and np.array_equal(pts[0], pts[-1]):
            pts = pts[:-1]
        if len(pts) != SAMPLES_PER_OUTLINE or pid in outlines:
            raise ValueError(f"Unexpected outline sampling or repeated piece ID {pid}")
        poly = Polygon(pts)
        if not poly.is_valid or poly.area <= 0 or len(poly.interiors):
            raise ValueError(f"Invalid artwork polygon {pid}")
        paths_by_face[poly.normalize().wkb] = pid
        outlines[pid] = pts
    if set(outlines) != set(range(EXPECTED_PIECES)):
        raise ValueError("Expected solid artwork for each of the 216 original piece IDs")

    # Actually node and polygonize all exported physical cuts. Match by full
    # geometric equality to named artwork rings, not nearest center or ordering.
    cut_lines = _parse_paths(cut.findall(".//s:path", NS))
    ticks = [np.rint(np.asarray(c.coords) * TICKS_PER_MM).astype(np.int64) for c in cut_lines]
    keys, _ = canonical_segments(np.vstack([np.column_stack([p[:-1], p[1:]]) for p in ticks]))
    duplicate_segments = len(keys) - len(np.unique(keys, axis=0))
    if duplicate_segments:
        raise ValueError(f"Found {duplicate_segments} duplicated physical cuts")
    network = shapely.node(MultiLineString(cut_lines))
    collection, leftovers, dangles, invalid = shapely.polygonize_full(shapely.get_parts(network))
    if any(not shape.is_empty for shape in (leftovers, dangles, invalid)):
        raise ValueError("Found non-face paths in the noded cutting network")
    polygons = {}
    for poly in shapely.get_parts(collection):
        key = poly.normalize().wkb
        if key not in paths_by_face:
            raise ValueError("A physical face differs from its printed outline")
        polygons[paths_by_face[key]] = poly
    if set(outlines) != set(polygons):
        raise ValueError("The cut file does not recover every artwork face")
    periods = read_periods(directory)
    pair_count, neighbor_count = check_periodic_paths(outlines, periods)
    area_error = abs(sum(p.area for p in polygons.values()) - float(np.prod(periods)))
    if area_error > 1.e-4:
        raise ValueError(f"Periodic area mismatch {area_error:.7f} mm²")
    periodic_min = np.min(np.vstack(list(outlines.values())), axis=0)
    span = np.max(np.vstack(list(outlines.values())), axis=0) - periodic_min
    if not np.allclose(span, SIZE_MM, atol=0.001, rtol=0):
        raise ValueError(f"Actual puzzle envelope {span.tolist()} mm, expected {SIZE_MM}")

    spines = marks.findall(".//s:path", NS)
    eyes = marks.findall(".//s:circle", NS)
    if len(spines) != EXPECTED_PIECES or len(eyes) != 2 * EXPECTED_PIECES:
        raise ValueError("Mismatch in exported engraving count")
    spine_by_id = {int(el.attrib["id"].split("-")[1]): el for el in spines}
    eye_by_id = {tuple(map(int, el.attrib["id"].split("-")[1:])): el for el in eyes}
    pieces = {}
    for pid, poly in sorted(polygons.items()):
        spine = spine_by_id[pid]
        line = _parse_paths([spine])[0]
        # Positive clearance is usable material after simulated kerf and half
        # the exported engraving stroke. A crossing gets negative clearance.
        stroke_half = float(spine.get("stroke-width", "0")) * 0.5
        spine_clear = poly.exterior.distance(line) - KERF_MM / 2 - stroke_half
        if not poly.covers(line):
            spine_clear = -abs(spine_clear)
        eye_clear = []
        for idx in (0, 1):
            eye = eye_by_id[(pid, idx)]
            point = Point(float(eye.attrib["cx"]), float(eye.attrib["cy"]))
            edge = poly.exterior.distance(point) * (1 if poly.covers(point) else -1)
            eye_clear.append(edge - float(eye.attrib["r"]) - KERF_MM / 2)
        pieces[pid] = Piece(poly, outlines[pid], component_count(poly, NECK_MM / 2),
                            component_count(poly, KERF_MM / 2), min(eye_clear), spine_clear)
    checks = {"actual_size_mm": span.tolist(), "verified_cut_faces": len(polygons),
              "duplicate_cut_segments": duplicate_segments, "periodic_opposed_segment_pairs": pair_count,
              "periodic_neighbor_pairs": neighbor_count, "torus_area_error_mm2": area_error,
              "physical_cut_paths": len(cut_lines)}
    return pieces, checks


def center(poly):
    return np.asarray(poly.centroid.coords[0])


def tip_positions(piece):
    # Use each anatomical lobe's own contiguous baseline sample window. This
    # allows the actual distal peak within the lobe to move after a shape edit.
    coords = piece.outline - center(piece.polygon)
    radial = np.linalg.norm(coords, axis=1)
    peaks = list(ANATOMICAL_TIPS.values())
    boundaries = [(a + ((b-a) % SAMPLES_PER_OUTLINE)//2) % SAMPLES_PER_OUTLINE
                  for a, b in zip(peaks, peaks[1:] + peaks[:1])]
    tips = []
    for start, end in zip(boundaries[-1:] + boundaries[:-1], boundaries):
        indices = (start + np.arange((end-start) % SAMPLES_PER_OUTLINE)) % SAMPLES_PER_OUTLINE
        tips.append(coords[indices[np.argmax(radial[indices])]])
    return np.asarray(tips)


def contour_error(poly_a, poly_b):
    distances = []
    # Equally spaced samples by arc length avoid weighting dense Bézier regions more heavily.
    for a, b in ((poly_a, poly_b), (poly_b, poly_a)):
        ring = a.exterior
        count = int(np.ceil(ring.length / .3))
        points = shapely.line_interpolate_point(ring, np.arange(count) * ring.length / count)
        distances.append(shapely.distance(points, b.exterior))
    joined = np.concatenate(distances)
    return float(np.percentile(joined, 95))


def compare(baseline, candidate, output):
    before, before_checks = read_board(baseline)
    after, after_checks = read_board(candidate)
    rows = []
    for pid, current in sorted(after.items()):
        original = before[pid]
        delta = center(original.polygon) - center(current.polygon)
        moved = affinity.translate(current.polygon, *delta)
        intersection = original.polygon.intersection(moved).area
        # GEOS intersection can differ from the source area by roundoff on
        # identical rings. Keep a probability-scale score in its physical range.
        iou = min(1., intersection / (original.polygon.area + moved.area - intersection))
        tip_change = np.linalg.norm(tip_positions(original) - tip_positions(current), axis=1)
        rows.append({"piece": pid, "baseline_neck_components": original.neck_components,
                     "candidate_neck_components": current.neck_components,
                     "baseline_kerf_components": original.kerf_components,
                     "candidate_kerf_components": current.kerf_components,
                     "silhouette_iou": float(iou), "outline_p95_distance_mm": contour_error(original.polygon, moved),
                     "baseline_min_eye_clearance_mm": original.eye_clearance,
                     "baseline_spine_clearance_mm": original.spine_clearance,
                     "candidate_min_eye_clearance_mm": current.eye_clearance,
                     "candidate_spine_clearance_mm": current.spine_clearance,
                     **{f"{name}_travel_mm": float(disp) for name, disp in zip(ANATOMICAL_TIPS, tip_change)},
                     "max_tip_travel_mm": float(tip_change.max())})
    ious = np.array([row["silhouette_iou"] for row in rows])
    tip = np.array([row["max_tip_travel_mm"] for row in rows])
    geometry = {}
    for name, seq in (("baseline", before.values()), ("candidate", after.values())):
        geometry[name] = {"neck_3mm_failures": sum(p.neck_components != 1 for p in seq),
                          "kerf_0_25mm_failures": sum(p.kerf_components != 1 for p in seq),
                          "pieces_with_engraving_outside_kerf_core": sum(min(p.eye_clearance, p.spine_clearance) <= 0 for p in seq)}
    marks_ok = geometry["candidate"]["pieces_with_engraving_outside_kerf_core"] == 0
    report = {"baseline": str(baseline), "candidate": str(candidate), "pieces": len(rows),
              "baseline_reimport": before_checks, "candidate_reimport": after_checks,
              "fabrication": geometry, "silhouette_iou_min": float(ious.min()),
              "silhouette_iou_median": float(np.median(ious)), "silhouette_iou_p05": float(np.percentile(ious,5)),
              "worst_iou_piece": int(np.argmin(ious)),
              "pieces_below_0_75_iou": int((ious < MIN_IOU).sum()),
              "pieces_below_0_90_iou": int((ious < MEDIAN_IOU).sum()),
              "max_tip_travel_median_mm": float(np.median(tip)),
              "max_tip_travel_p95_mm": float(np.percentile(tip,95)),
              "max_tip_travel_worst_mm": float(tip.max()),
              "pieces_with_tip_over_2mm": int((tip > TIP_ALERT_MM).sum()),
              "median_within_piece_outline_p95_mm": float(np.median([r["outline_p95_distance_mm"] for r in rows])),
              "silhouette_proxy_gate": bool(ious.min() >= MIN_IOU and np.median(ious) >= MEDIAN_IOU and marks_ok),
              "rule": "Same-ID centroid-aligned silhouette IoU; no angle, scale or shear fit. Require all >=0.75, median >=0.90 and all eyes/spines inside simulated kerf. Flag, separately, distal landmark motion >2mm. These are geometric proxies, not a human recognition test."}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / "pieces.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output / "evaluation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = compare(args.baseline, args.candidate, args.output)
    print(json.dumps({k:report[k] for k in ("pieces","fabrication","silhouette_iou_min","silhouette_iou_median",
                                             "max_tip_travel_worst_mm","silhouette_proxy_gate")}, indent=2))


if __name__ == "__main__":
    main()

