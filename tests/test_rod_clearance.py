"""Known distances and intentional contacts in the rod solver's cut geometry."""

import numpy as np
import pytest
from shapely.geometry import box

from dendrite_engine.rod_growth import RodConfig, _snapped_rod_clearance, run_rod_growth


def measure(points, chains, mobility=None, radius=0.1):
    coords = np.asarray(points, dtype=float)
    inv_mass = np.ones(len(coords)) if mobility is None else np.asarray(mobility)
    return _snapped_rod_clearance(coords, [{"nodes": chain} for chain in chains], inv_mass, radius)


@pytest.mark.parametrize("height, expected", [(3.047, 3.05), (0.5, 0.5), (0.003, 0)])
def test_gap_is_measured_after_snapping(height, expected):
    points = [(0, 0.004), (8, 0.004), (0, height), (8, height)]
    assert measure(points, [[0, 1], [2, 3]]) == pytest.approx(expected)


def test_crossing_interiors_have_zero_clearance():
    points = [(0, 0), (2, 2), (0, 2), (2, 0)]
    assert measure(points, [[0, 1], [2, 3]]) == 0


def test_intentional_junction_and_near_neighbors_are_exempt():
    points = [(0, 0), (1, 0), (2, 2), (1, 3), (0, 2)]
    assert measure(points, [[0, 1], [0, 2], [0, 3]]) is None
    # Every edge of this pentagon is within two edges of every other edge.
    assert measure(points, [[0, 1, 2, 3, 4, 0]]) is None


@pytest.mark.parametrize("mobility, expected", [
    ([0, 0, 0, 0], None),
    ([0, 0, 0.1, 1], None),
    ([0, 0, 0.2, 1], 1.0),
])
def test_wall_clearance_depends_on_attachment_mobility(mobility, expected):
    points = [(0, 0), (3, 0), (0, 1), (3, 1)]
    assert measure(points, [[0, 1], [2, 3]], mobility) == expected


def test_short_and_collapsed_chains_cannot_supply_a_minimum():
    points = [(0, 0), (3, 0), (1, 0), (1.02, 0), (2, 0), (2.003, 0)]
    assert measure(points, [[0, 1], [2, 3], [4, 5]]) is None


def test_minimum_in_a_later_spatial_query_batch():
    # Adjacent horizontal bars are a millimetre apart. The final two bars
    # are much closer and fall beyond the first 256 queried segments.
    points = [(x, y) for y in range(257) for x in (0, 8)]
    points.extend([(0, 256.25), (8, 256.25)])
    rods = [[i, i + 1] for i in range(0, len(points), 2)]
    assert measure(points, rods, radius=2.0) == pytest.approx(0.25)


def test_initial_snapshot_has_geometry_and_no_collision_sweep():
    config = RodConfig(style="wave", width_mm=200, height_mm=160, num_pieces=14,
                       seed=42, growth_steps=0)
    result = run_rod_growth(box(0, 0, 200, 160), config)
    assert result.min_nonadjacent_clearance_mm > 0
    assert result.last_collision_sweep_min_mm is None
