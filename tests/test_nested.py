import json

import numpy as np
import pytest
from shapely.geometry import LineString

from dendrite_engine.nested import _make_cluster, generate_nested


def test_parent_connectors_have_two_matching_sides_and_distinct_profiles():
    group = _make_cluster(7, 40., 42, .15)
    assert len(group.groups) == 7 and len(group.connectors) == 12
    assert len(group.board.interiors) == 6
    assert group.board.area == pytest.approx(sum(p.area for p in group.groups))
    for connector in group.connectors:
        left, right = connector["groups"]
        shared = group.groups[left].boundary.intersection(group.groups[right].boundary)
        curve = LineString(connector["profile_mm"])
        assert shared.length == pytest.approx(curve.length)
        assert shared.hausdorff_distance(curve) < 1e-6
    assert min(c["nearest_prior_profile_error_mm"] for c in group.connectors[1:]) > .35
    other = _make_cluster(7, 40., 41, .15)
    assert not np.allclose(other.connectors[0]["profile_mm"], group.connectors[0]["profile_mm"])


def test_nested_group_membership_and_single_cut_svg(tmp_path):
    run = generate_nested(tmp_path, seed=42, groups=3, pieces_per_group=3)
    assert run.audit["pieces"] == 9
    assert run.audit["duplicate_segments"] == 0 and run.audit["coverage_error_mm2"] < .05
    assert run.audit["invalid_pieces"] == 0 and run.audit["overlap_mm2"] < 1e-5
    packet = json.loads((tmp_path/"puzzle.json").read_text())
    groups = packet["metadata"]["groups"]
    assert [len(g["piece_ids"]) for g in groups] == [3, 3, 3]
    assert sorted(i for g in groups for i in g["piece_ids"]) == list(range(9))
    assert all(g["coverage_error_mm2"] < .05 for g in groups)
    assert len(set(g["color"] for g in groups)) == 3
    assert (tmp_path/"cut.svg").is_file() and (tmp_path/"artwork.svg").is_file()


def test_nested_rejects_degenerate_cluster():
    with pytest.raises(ValueError, match="at least two"):
        _make_cluster(1, 40., 0, .15)
