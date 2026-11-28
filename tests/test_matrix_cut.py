"""Regression checks for Matrix cell ownership and the fabrication line network."""

import numpy as np
import pytest
import shapely
from shapely.geometry import LineString, Point, box
from shapely.ops import polygonize

from dendrite_engine.matrix import generate_matrix
from dendrite_engine.whimsies import WhimsyPlacement


def test_exchange_improves_interlocking_and_preserves_partition():
    outline = box(-5, 10, 105, 90)
    result = generate_matrix(outline, 24, seed=42)
    assert result.metrics["cell_exchanges"] > 0
    assert result.metrics["interfaces_over_180_deg"] > result.metrics["initial_interfaces_over_180_deg"]
    assert len(result.faces) == 24
    assert all(f.is_valid and f.geom_type == "Polygon" and f.area > 0 for f in result.faces)
    assert sum(f.area for f in result.faces) == pytest.approx(outline.area)
    assert shapely.union_all(result.faces).symmetric_difference(outline).area < 1e-8
    repeated = generate_matrix(outline, 24, seed=42)
    assert [p.wkb for p in result.faces] == [p.wkb for p in repeated.faces]
    changed = generate_matrix(outline, 24, seed=5)
    assert [p.wkb for p in result.faces] != [p.wkb for p in changed.faces]


def test_trimmed_grid_preserves_fixed_figures_and_holes_at_export_precision():
    hole = Point(13, 24).buffer(3)
    outline = Point(35, 29).buffer(28).difference(hole)
    fixed = Point(37, 44).buffer(5)
    placement = WhimsyPlacement("circle", (37, 44), 10, polygon=fixed)
    result = generate_matrix(outline, 12, seed=10, whimsies=[placement])
    assert result.whimsy_indices == [11]
    assert result.faces[11].equals(fixed)
    assert all(p.intersection(hole).area < 1e-9 for p in result.faces)
    lines = [*result.interior_lines, *result.exterior_lines]
    assert sum(g.length for g in lines) == pytest.approx(shapely.union_all(lines).length)
    rounded = [LineString(np.asarray(g.coords).round(3)) for g in lines]
    recovered = [p for p in polygonize(shapely.union_all(rounded))
                 if outline.covers(p.representative_point())]
    assert len(recovered) == 12
    assert shapely.union_all(recovered).symmetric_difference(outline).area < 0.05


def test_overlapping_and_outside_fixed_pieces_are_rejected():
    outline = box(0, 0, 50, 50)
    fixed = WhimsyPlacement("disc", (25, 25), 10, polygon=Point(25, 25).buffer(5))
    with pytest.raises(ValueError, match="separated"):
        generate_matrix(outline, 5, whimsies=[fixed, fixed])
    outside = WhimsyPlacement("disc", (0, 25), 10, polygon=Point(0, 25).buffer(5))
    with pytest.raises(ValueError, match="inside"):
        generate_matrix(outline, 5, whimsies=[outside])


def test_guide_warps_mesh_without_changing_domain_or_fixed_pieces():
    board = box(0, 0, 110, 80)
    fixed = WhimsyPlacement("disc", (55, 40), 14, polygon=Point(55, 40).buffer(7))
    guides = [[(-6, 12), (37, 28), (55, 40), (92, 58), (122, 60)]]
    unguided = generate_matrix(board, 16, seed=8, whimsies=[fixed])
    guided = generate_matrix(board, 16, seed=8, whimsies=[fixed], guide_curves=guides)
    again = generate_matrix(board, 16, seed=8, whimsies=[fixed], guide_curves=guides)
    assert [f.wkb for f in guided.faces] == [f.wkb for f in again.faces]
    assert [f.wkb for f in guided.faces] != [f.wkb for f in unguided.faces]
    assert len(guided.faces) == 16 and guided.faces[-1].equals(fixed.polygon)
    assert shapely.union_all(guided.faces).symmetric_difference(board).area < 1e-8
    expected = LineString(guides[0]).intersection(board.difference(fixed.polygon)).length
    assert guided.metrics["guide_length_inside_mm"] == pytest.approx(expected, abs=0.0001)
    with pytest.raises(ValueError, match="inside the moving board"):
        generate_matrix(board, 16, guide_curves=[[(120, 110), (200, 100)]])
