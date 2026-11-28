from collections import Counter
import json
import xml.etree.ElementTree as ET

import pytest

from dendrite_engine import polyhex


def test_full_one_sided_kit_and_independent_solution():
    shapes = polyhex.tile_set()
    assert Counter(map(len, shapes)) == {1: 1, 2: 1, 3: 3, 4: 10, 5: 33}
    assert Counter(polyhex.COLORS) == {0: 12, 1: 12, 2: 12, 3: 12}
    assert [sum(len(s) for i, s in enumerate(shapes) if polyhex.COLORS[i] == c)
            for c in range(4)] == [54, 54, 54, 55]
    board = polyhex.hex_board()
    assert len(board) == 217
    for turn in range(6):
        reference = polyhex.reference_board(turn)
        cells = [p for tile in reference.values() for p in tile]
        assert len(cells) == 217 and set(cells) == board
        checked = polyhex.solve(board, shapes, polyhex.COLORS, reference)
        assert checked.exhausted and len(checked.solutions) == 1


def test_exact_cover_limits_and_edge_colors():
    shapes = (((0, 0),), ((0, 0),))
    board = {(0, 0), (1, 0)}
    assert len(polyhex.solve(board, shapes, [0, 1], limit=3).solutions) == 2
    conflict = polyhex.solve(board, shapes, [0, 0])
    assert conflict.exhausted and not conflict.solutions
    limited = polyhex.solve(board, shapes, [0, 1], node_limit=1)
    assert not limited.exhausted
    with pytest.raises(ValueError, match="share an edge"):
        polyhex.solve(board, shapes, [0, 0], {0: [(0, 0)], 1: [(1, 0)]})


def test_seeded_printable_challenge_has_one_solution(tmp_path):
    answer, first, proof = polyhex.make_challenge(42, 12)
    assert len(first) == 36 and proof.exhausted and proof.solutions == [answer]
    assert polyhex.make_challenge(42, 12)[1] == first
    assert polyhex.make_challenge(13, 12)[1] != first
    polyhex.main(["--seed", "42", "--missing", "12", "--out", str(tmp_path)])
    path = tmp_path / "challenge.json"
    game = json.loads(path.read_text())
    assert len(game["missing_tile_ids"]) == 12 and game["exhaustive"]
    assert polyhex.main(["--verify", str(path)]) == 0
    for name in ("board", "solution", "tiles"):
        svg = ET.parse(tmp_path / f"{name}.svg").getroot()
        assert svg.tag.endswith("svg") and svg.attrib["width"].endswith("mm")
    tiles = ET.parse(tmp_path / "tiles.svg").getroot()
    assert len(tiles.findall("{*}path")) == 48
