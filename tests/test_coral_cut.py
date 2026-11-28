import math

import numpy as np
import pytest
import shapely
from shapely.geometry import box

from dendrite_engine.coral import _arc_lobe, generate_coral
from dendrite_engine.phase_field import build_outline_polygon
from dendrite_engine.postprocess import export_puzzle
from dendrite_engine.whimsies import build_whimsy_geometry, get_builtin_whimsy


def test_arc_basis_is_circular_with_fixed_endpoints_and_an_undercut():
    angle = math.radians(115)
    span, share = 16, 0.4
    curve = _arc_lobe(span, angle, share)
    assert np.allclose(curve[[0, -1]], [(0, 0), (span, 0)])
    assert np.diff(curve[:, 0]).min() < 0
    radii_sum = span / (2 * math.sin(angle))
    shoulder = radii_sum * share
    apex = np.argmax(curve[:, 1])
    assert curve[apex, 1] == pytest.approx(radii_sum * (1 - math.cos(angle)), abs=0.02)
    assert np.linalg.norm(curve[1:5] - (0, shoulder), axis=1) == pytest.approx([shoulder] * 4)


def test_coral_cut_reimports_with_fixed_whimsy_and_exact_piece_count(tmp_path):
    outline = build_outline_polygon("circle", 180, 160)
    figure = build_whimsy_geometry(get_builtin_whimsy("trilobite"), (80, 78), 40)
    result = generate_coral(outline, 14, 42, [figure])
    assert result.metrics["undercut_seams"] > 0
    assert result.metrics["circular_arcs"] >= result.metrics["arc_seams"] * 3
    assert len(result.whimsy_indices) == 1
    fixed = result.faces[result.whimsy_indices[0]]
    assert fixed.symmetric_difference(figure.polygon).area < 0.001 * fixed.length
    assert shapely.union_all(result.faces).symmetric_difference(outline).area < 0.15
    exported = export_puzzle(tmp_path, result.faces, result.interior_lines, result.exterior_lines,
                             expected_outline=outline, whimsy_indices=result.whimsy_indices)
    assert exported.audit["pieces"] == 14
    assert exported.audit["duplicate_segments"] == 0
    assert exported.audit["overlap_mm2"] < 1e-5
    assert exported.audit["coverage_error_mm2"] < 0.15
    assert exported.audit["all_hard_gates_passed"]


def test_coral_is_seeded_and_rejects_crossing_fixed_figures():
    board = box(0, 0, 140, 95)
    first = generate_coral(board, 7, 11)
    replay = generate_coral(board, 7, 11)
    alternative = generate_coral(board, 7, 12)
    assert [s.wkb for s in first.interior_lines] == [s.wkb for s in replay.interior_lines]
    assert [s.wkb for s in first.interior_lines] != [s.wkb for s in alternative.interior_lines]
    figure = build_whimsy_geometry(get_builtin_whimsy("trilobite"), (60, 50), 26)
    with pytest.raises(ValueError, match="disjoint"):
        generate_coral(board, 10, 11, [figure, figure])
