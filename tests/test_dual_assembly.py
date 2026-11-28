import json
import xml.etree.ElementTree as ET

import pytest

from dendrite_engine.dual import generate_dual, verify_dual


def test_numbered_pieces_rigidly_fill_the_second_assembly(tmp_path):
    verified = generate_dual(tmp_path, pieces=18, disc_pieces=4, seed=12)
    assert verified == verify_dual(tmp_path)
    assert verified["geometry_passed"]
    assert verified["rectangle_faces"] == 18
    assert verified["disc_faces"] == 4
    assert (
        verified["rectangle_duplicate_segments"]
        == verified["disc_duplicate_segments"]
        == 0
    )
    assert verified["disc_overlap_mm2"] < 0.12
    assert verified["max_piece_difference_mm2"] < 0.12
    layout = json.loads((tmp_path / "assemblies.json").read_text(encoding="utf-8"))
    puzzle = json.loads((tmp_path / "puzzle.json").read_text(encoding="utf-8"))
    for mark in layout["numbered_pieces"]:
        piece = puzzle["pieces"][mark["rectangle_piece_id"]]
        assert piece["is_whimsy"] and piece["engrave"]
    guide = ET.parse(tmp_path / "disc-assembly.svg").getroot()
    assert len(guide.find("{*}g[@id='engrave']")) > 0
    # A rigid placement error must fail even though both individual SVGs still close.
    layout["numbered_pieces"][0]["rectangle_to_disc"][4] += 2
    (tmp_path / "assemblies.json").write_text(json.dumps(layout), encoding="utf-8")
    with pytest.raises(ValueError, match="geometry check"):
        verify_dual(tmp_path)


def test_numbered_set_requires_room_for_the_base_cut(tmp_path):
    with pytest.raises(ValueError, match="two ordinary"):
        generate_dual(tmp_path, pieces=5, disc_pieces=4)
