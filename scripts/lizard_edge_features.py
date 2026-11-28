"""Track whether the pieces pass the 3 mm screen by reconnecting or losing original thin features."""

import argparse
import csv
import json
from pathlib import Path

import numpy as np
from shapely import affinity
from shapely.geometry import Polygon


def parts(shape):
    if shape.is_empty:
        return []
    return sorted(getattr(shape, "geoms", [shape]), key=lambda p: p.area, reverse=True)


def crosscheck(baseline, candidate, output):
    originals = {p["id"]: Polygon(p["outline"]) for p in baseline["pieces"]}
    proposals = {p["id"]: Polygon(p["outline"]) for p in candidate["pieces"]}
    if originals.keys() != proposals.keys():
        raise ValueError("Piece IDs differ")
    rows = []
    widths = (2.5, 2.8, 3.0, 3.2, 3.5)
    failures = {kind: {w: 0 for w in widths} for kind in ("baseline", "candidate")}
    retained_fractions = {"baseline": [], "candidate": []}
    for pid, base in originals.items():
        trial = proposals[pid]
        shift = np.array(base.centroid.coords[0]) - np.array(trial.centroid.coords[0])
        trial = affinity.translate(trial, *shift)
        for name, board_piece in (("baseline", base), ("candidate", trial)):
            for width in widths:
                components = parts(board_piece.buffer(-width / 2))
                failures[name][width] += int(len(components) != 1)
                if width == 3.0:
                    retained_fractions[name].append(sum(p.area for p in components) / board_piece.area)
        original = parts(base.buffer(-1.5))
        edited = parts(trial.buffer(-1.5))
        for core_id, lobe in enumerate(original[1:], 1):
            if lobe.area < .25:
                continue
            overlaps = [lobe.intersection(p).area / lobe.area for p in edited]
            if overlaps and overlaps[0] >= .25:
                state = "overlaps main core"
            elif any(frac >= .25 for frac in overlaps[1:]):
                state = "overlaps detached core"
            else:
                state = "shifted or lost"
            rows.append({"piece_id": pid, "lobe_id": core_id, "baseline_area_mm2": lobe.area,
                         "main_core_overlap_fraction": overlaps[0] if overlaps else 0.0,
                         "best_overlap_fraction": max(overlaps, default=0.), "state": state})
    output.mkdir(parents=True, exist_ok=True)
    with (output / "lobe-retention.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["piece_id", "lobe_id", "baseline_area_mm2", "main_core_overlap_fraction",
                  "best_overlap_fraction", "state"]
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        writer.writerows(rows)
    result = {
        "width_sweep_failures": failures,
        "median_core_area_fraction": {name:float(np.median(a)) for name,a in retained_fractions.items()},
        "counted_detached_lobes": len(rows),
        "lobe_states": {state: sum(row["state"] == state for row in rows)
                        for state in ("overlaps main core", "overlaps detached core", "shifted or lost")},
        "method": "At 3 mm, count baseline satellite cores >= 0.25 mm2, after centroid alignment. "
                  "Require overlap >= 25% of the satellite's original area. Shape shifts can reduce overlap."
    }
    (output / "features.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(crosscheck(json.loads(args.baseline.read_text(encoding="utf-8")),
                                json.loads(args.candidate.read_text(encoding="utf-8")),args.out),indent=2))

