import numpy as np
from shapely.geometry import LineString, box

from dendrite_engine.phase_field import rasterize_polygon
from dendrite_engine.postprocess import extract_shared_seams_from_labels
from dendrite_engine.whimsies import build_whimsy_geometry, get_builtin_whimsy


def test_neighbor_junctions_land_on_the_fixed_vector_silhouette():
    board = box(0, 0, 80, 60)
    figure = build_whimsy_geometry(get_builtin_whimsy("ammonite"), (41.3, 28.9), 37, angle_deg=18)
    y, x = np.indices((60, 80))
    labels = ((x >= 40) + 2 * (y >= 30)).astype(np.int32)
    labels[rasterize_polygon(figure.polygon, board.bounds, labels.shape)] = 4
    faces, *_ = extract_shared_seams_from_labels(
        labels, board.bounds, board, fixed_piece_ids=[4], whimsies=[figure],
    )
    assert len(faces) == 5
    # A vertex may move by half a 0.01 mm snap-grid cell on either axis.
    assert faces[4].boundary.hausdorff_distance(figure.polygon.boundary) < .008


def test_ammonite_engraving_stays_inside_the_printed_piece():
    figure = get_builtin_whimsy("ammonite")
    assert all(figure.polygon.contains(LineString(mark)) for mark in figure.engrave_paths)
