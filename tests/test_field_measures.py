"""Checks for the polygon and label measures behind pathnet/field.py (interlock, lock margin, spur repair).

Run from the repository root:  .venv\\Scripts\\python.exe -m pytest tests/test_field_measures.py
"""
import sys
from pathlib import Path

import numpy as np
import pytest

PATHNET = Path(__file__).resolve().parents[1] / "pathnet"
sys.path.insert(0, str(PATHNET))
pytest.importorskip("torch")
pytest.importorskip("cv2")
fl = pytest.importorskip("field")

from shapely import union_all  # noqa: E402
from shapely.geometry import LineString, box  # noqa: E402
from shapely.ops import polygonize  # noqa: E402


def cut(lines, w=40, h=20):
    """split a w x h board along the given polylines, pieces ordered left to right"""
    net = union_all([box(0, 0, w, h).exterior, *(LineString(xy) for xy in lines)], grid_size=0.01)
    return sorted(polygonize(net), key=lambda p: p.centroid.x)


STRAIGHT = [[(20, 0), (20, 20)]]
# a dovetail: the left piece reaches 6 mm into the right one and is 4 mm wider at its head than at its neck
KNOB = [[(20, 0), (20, 8), (26, 6), (26, 14), (20, 12), (20, 20)]]


def test_straight_seam_slides_apart():
    faces = cut(STRAIGHT)
    assert len(faces) == 2
    stats, margins = fl.lock_stats(faces, 1.0)
    assert stats["locked_pairs"] == 0
    assert stats["directions_where_a_group_can_slide_out"] > 0
    assert fl.lock_margin(faces) == 0.0


def test_dovetail_locks_with_its_undercut():
    faces = cut(KNOB)
    assert len(faces) == 2
    # pulling straight apart is stopped by the two 2 mm shoulders (4 mm in all), which is the weakest direction
    assert fl.lock_stats(faces, 3.5)[0]["directions_where_a_group_can_slide_out"] == 0
    assert fl.lock_stats(faces, 4.5)[0]["directions_where_a_group_can_slide_out"] > 0
    assert 3.5 <= fl.lock_margin(faces) <= 4.0
    assert fl.lock_stats(faces, 3.5)[0]["locked_pairs"] == 1


def test_loose_groups_agree_with_lock_stats():
    faces = cut(KNOB + [[(0, 10), (26, 10)]])          # the left piece, head included, split in two by a straight seam
    assert len(faces) == 3
    pairs, block, seam = fl.blocking(faces)
    stops = fl.stops(pairs, block, len(faces))
    for need in (0.5, 1.0, 3.0, 6.0):
        groups = fl.loose_groups(stops, need)
        assert len({t for t, _ in groups}) == fl.lock_stats(faces, need)[0]["directions_where_a_group_can_slide_out"]
    board_edge = 2 * (40 + 20)                         # every seam is shared by two pieces, the board edge by one
    assert seam.sum() == pytest.approx((sum(f.length for f in faces) - board_edge) / 2, abs=0.05)


def test_despike_moves_spurs_and_lobes_but_never_silhouette():
    px = 0.25
    lab = np.zeros((120, 200), np.int32)
    lab[:, 100:] = 1                                   # two pieces side by side
    lab[58:62, 100:140] = 0                            # a 1 mm wide, 10 mm long spur of piece 0 into piece 1
    lab[20:36, 150:166] = 0                            # a 4 mm lobe of piece 0 inside piece 1 ...
    lab[27:29, 100:150] = 0                            # ... hanging on a 0.5 mm neck
    solid = np.zeros_like(lab, bool)
    fixed, moved = fl.despike(lab, solid, 2, px)
    assert moved > 0
    # all of it goes to piece 1; at most a half-millimetre stub stays where the disc still reaches
    assert (fixed[:, 102:] == 1).all() and (fixed[:, :100] == 0).all()

    solid[58:62, 100:140] = True                       # the same spur, but now it is part of piece 0's own silhouette
    kept, _ = fl.despike(lab, solid, 2, px)
    assert (kept[58:62, 100:140] == 0).all()
    assert (kept[20:36, 150:166] == 1).all()           # the lobe is still handed over


def test_connect_keeps_one_component_per_piece():
    lab = np.zeros((40, 80), np.int32)
    lab[:, 40:] = 1
    lab[5:9, 60:64] = 0                                # an island of piece 0 inside piece 1
    out, stray = fl.connect(lab, 2)
    assert stray == 16
    assert (out[:, 40:] == 1).all()
