"""Compare a paired sample of exported lizard silhouettes with the four loose pieces in the supplied photos.

Register each original baseline piece to the reference by a similarity transform. Apply exactly
that transform to its centroid-aligned candidate. This measures resemblance to these photographs,
which informed the template. It is a sensitivity check, not a blind recognition experiment.
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import minimize
from shapely.geometry import Point, Polygon


# Snout coordinates and fixed sample IDs follow the earlier reference comparison.
PHOTO_SNOUTS = (((557, 473), (434, 655), (544, 793)), ((403, 717),))
PIECE_IDS = (0, 54, 108, 162)


def extract_references(photo_paths):
    references = []
    for path, snouts in zip(photo_paths, PHOTO_SNOUTS):
        rgb = cv2.imread(str(path))
        if rgb is None:
            raise ValueError(f"Cannot read {path}")
        hsv = cv2.cvtColor(rgb, cv2.COLOR_BGR2HSV)
        fg = np.uint8((hsv[:, :, 1] > 100) & (hsv[:, :, 2] > 90))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(fg)
        polygons = []
        for label in range(1, count):
            if 8000 < stats[label, cv2.CC_STAT_AREA] < 25000:
                contours, _ = cv2.findContours(np.uint8(labels == label), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                pts = cv2.approxPolyDP(max(contours, key=cv2.contourArea), .65, True)[:, 0]
                poly = Polygon(pts).buffer(0)
                if poly.geom_type != "Polygon":
                    poly = max(poly.geoms, key=lambda p: p.area)
                polygons.append(poly)
        if len(polygons) != len(snouts):
            raise ValueError(f"{path.name}: expected {len(snouts)} isolated colored pieces, found {len(polygons)}")
        for snout in snouts:
            # Each snout must identify its own silhouette regardless of connected-component order.
            landmark = Point(snout)
            idx = min(range(len(polygons)), key=lambda i: polygons[i].distance(landmark))
            polygon = polygons.pop(idx)
            if polygon.distance(landmark) > 8:
                raise ValueError(f"{path.name}: snout outside reference silhouette at {snout}")
            references.append((polygon, np.asarray(snout)))
    return references


def iou(a, b):
    area = a.intersection(b).area
    return area / (a.area + b.area - area)


def center_coords(piece):
    coordinates = np.asarray(piece["outline"], dtype=float)
    centroid = np.asarray(Polygon(coordinates).centroid.coords[0])
    return coordinates - centroid, centroid


def fit_similarity(piece, reference, ref_snout):
    points, center = center_coords(piece)
    ref_center = np.asarray(reference.centroid.coords[0])
    scale = np.sqrt(reference.area / Polygon(points).area)
    head = np.asarray(piece["spine"][0]) - center
    target = ref_snout - ref_center
    theta = np.arctan2(*target[::-1]) - np.arctan2(*head[::-1])

    def transform(xy, parameters):
        dx, dy, log_scale, angle = parameters
        c, s = np.cos(angle), np.sin(angle)
        return (xy @ np.array([[c, s], [-s, c]])) * (scale * np.exp(log_scale)) + ref_center + (dx, dy)

    def error(v):
        return 1 - iou(reference, Polygon(transform(points, v)))

    starts = ([0, 0, 0, a] for a in np.linspace(theta-.49, theta+.49, 17))
    guess = min(starts, key=error)
    unit = np.array([50., 50., 1., 1.])
    bounds = np.array([(-45., 45.), (-45., 45.), (-.4, .4), (theta-.65, theta+.65)])
    solution = minimize(lambda q: error(q * unit), guess / unit, method="SLSQP",
                        bounds=bounds / unit[:, None], options={"maxiter": 100, "ftol": 1e-9})
    params = min((np.asarray(guess), solution.x * unit), key=error)
    return lambda xy: Polygon(transform(xy, params))


def compare(baseline, candidate, photo_paths, out):
    references = extract_references(photo_paths)
    before = {p["id"]: p for p in baseline["pieces"]}
    after = {p["id"]: p for p in candidate["pieces"]}
    rows = []
    for pid in PIECE_IDS:
        a, _ = center_coords(before[pid])
        b, _ = center_coords(after[pid])
        for ref_id, (reference, snout) in enumerate(references, 1):
            registered = fit_similarity(before[pid], reference, snout)
            base_score = iou(reference, registered(a))
            model_score = iou(reference, registered(b))
            rows.append({"piece_id": pid, "photo_piece": ref_id, "baseline_iou": base_score,
                         "model_iou": model_score, "difference": model_score - base_score})
    out.mkdir(parents=True, exist_ok=True)
    with (out / "photo-silhouettes.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    summary = {key: float(np.mean([x[key] for x in rows])) for key in ("baseline_iou", "model_iou", "difference")}
    summary.update(pieces=list(PIECE_IDS), photographed_pieces=len(references), pairs=len(rows),
                   registration="translation, uniform scale and rotation fitted to baseline and then frozen",
                   source_sha256=[hashlib.sha256(p.read_bytes()).hexdigest() for p in photo_paths])
    (out / "photo-silhouettes.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("photos", nargs=2, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(json.loads(args.baseline.read_text(encoding="utf-8")),
                             json.loads(args.model.read_text(encoding="utf-8")), args.photos, args.output), indent=2))

