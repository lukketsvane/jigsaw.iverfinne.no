import xml.etree.ElementTree as ET
from collections import Counter

import numpy as np
import pytest
from shapely import affinity
from shapely.geometry import Polygon

from dendrite_engine.chess import BACK_RANK, _load_paths, generate


def test_chess_inventory_registration_and_manufacturing_layers(tmp_path):
    run = generate(tmp_path)
    metrics = run["measurements"]
    assert metrics["printed_squares"] == 64 and metrics["board_pieces"] == 49
    assert metrics["token_count"] == metrics["frame_pockets"] == 32
    assert metrics["verified_lift_holes"] == 32
    assert metrics["min_radial_clearance_mm"] >= 0.275
    assert metrics["min_remaining_frame_web_mm"] >= 2
    assert (
        metrics["duplicate_cut_segments"] == 0
        and metrics["board_coverage_error_mm2"] < 0.01
    )
    inventory = run["chessmen"]
    assert len({r["square"] for r in inventory}) == 32
    paths = _load_paths(tmp_path / "cut.svg")
    for side, rank, pawn_rank in (("black", 8, 7), ("white", 1, 2)):
        bank = [r for r in inventory if r["side"] == side]
        assert [r["kind"] for r in bank if r["square"].endswith(str(rank))] == list(
            BACK_RANK
        )
        assert [r["kind"] for r in bank if r["square"].endswith(str(pawn_rank))] == [
            "pawn"
        ] * 8
        assert Counter(r["kind"] for r in bank)["bishop"] == 2
        for r in bank:
            piece = Polygon(paths["token-" + r["id"]]).buffer(-0.075)
            socket = Polygon(paths["pocket-" + r["id"]]).buffer(0.075)
            move = np.subtract(r["storage_center_mm"], r["cut_center_mm"])
            placed = affinity.translate(piece, *move)
            assert placed.within(socket)
            assert placed.boundary.distance(socket.boundary) == pytest.approx(
                0.30, abs=0.025
            )
    artwork = ET.parse(tmp_path / "artwork.svg").getroot()
    board = {el.attrib["id"]: el.attrib["fill"] for el in artwork.findall(".//{*}rect")}
    assert board["h1"] == board["a8"] and board["a1"] != board["h1"]
    assert (
        artwork.attrib["viewBox"]
        == ET.parse(tmp_path / "cut.svg").getroot().attrib["viewBox"]
    )
    for name in ("stored.svg", "playing.svg"):
        r = ET.parse(tmp_path / name).getroot()
        positions = {p.attrib["id"]: p.attrib["d"] for p in r.findall("{*}path")}
        assert {p["id"] for p in inventory} <= set(positions)


def test_chess_seed_and_parameter_screen(tmp_path):
    for seed in (2, 62):
        r = generate(
            tmp_path / str(seed),
            square_mm=24,
            pieces_per_side=4,
            kerf_mm=0.22,
            clearance_mm=0.5,
            seed=seed,
        )
        assert r["measurements"]["board_pieces"] == 16
        assert r["measurements"]["min_radial_clearance_mm"] > 0.47
    samples = [_load_paths(tmp_path / str(s) / "cut.svg") for s in (2, 62)]
    assert not samples[0]["board-1"].equals(samples[1]["board-1"])
    before = (tmp_path / "2" / "cut.svg").read_bytes()
    with pytest.raises(ValueError):
        generate(tmp_path / "2", min_web_mm=50)
    assert (tmp_path / "2" / "cut.svg").read_bytes() == before
