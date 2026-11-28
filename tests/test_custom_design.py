import json
from xml.etree import ElementTree as ET

import numpy as np
import pytest
from shapely.geometry import box

from dendrite_engine.cli import generate_puzzle_run, parse_whimsy_arg
from dendrite_engine.design_art import (
    ART_PATTERNS,
    photo_board,
    render_pattern,
    suggest_outline,
)
from dendrite_engine.parts import export_parts
from dendrite_engine.phase_field import build_outline_polygon
from dendrite_engine.regions import solve_regions


def test_transparent_subject_and_seeded_prints():
    image = np.zeros((80, 140, 4), np.uint8)
    image[10:65, 30:100] = [21, 110, 200, 255]
    suggestion = suggest_outline(image)
    assert suggestion["mode"] == "alpha"
    board, crop = photo_board(image, 138)
    assert board.bounds == pytest.approx((0, 0, 138, 108))
    assert crop.shape[:2] == (55, 70)
    spiral = build_outline_polygon("spiral", 100, 80, seed=4)
    assert spiral.is_valid and spiral.area < 7700
    prints = [
        render_pattern(mode, spiral, 4, longest_side=100)
        for mode in ART_PATTERNS
        if mode != "wood"
    ]
    assert all(img.shape == (80, 100, 3) and img.std() > 8 for img in prints)
    assert len(set(p.tobytes() for p in prints)) == len(prints)
    assert np.array_equal(prints[0], render_pattern(ART_PATTERNS[1], spiral, 4, 100))


def test_regional_cut_and_registration(tmp_path):
    width, height = 160, 100
    outline = box(0, 0, width, height)
    figures = parse_whimsy_arg(["glyph:B:0.25,0.5,16"], outline)
    regions = [
        {
            "style": "coral",
            "pieces": 7,
            "points": [[0, 0], [0.55, 0], [0.43, 1], [0, 1]],
        },
        {"style": "matrix", "pieces": 6, "remaining": True},
    ]
    image = np.full((40, 64, 3), [76, 220, 49], np.uint8)
    result = generate_puzzle_run(
        tmp_path / "board",
        width_mm=width,
        height_mm=height,
        num_pieces=13,
        seed=19,
        whimsies=figures,
        regions=regions,
        title="Green field",
        artwork_image=image,
        reverse_text="19",
    )
    puzzle = json.loads((result.out_dir / "puzzle.json").read_text(encoding="utf-8"))
    assert result.audit["pieces"] == 13 and result.audit["duplicate_segments"] == 0
    assert result.audit["coverage_error_mm2"] < 0.5
    assert (
        puzzle["metadata"]["title"] == "Green field"
        and puzzle["metadata"]["style"] == "mixed"
    )
    assert sum(p["is_whimsy"] for p in puzzle["pieces"]) == 1
    cut = ET.parse(result.files["cut_svg"]).getroot()
    back = ET.parse(result.files["back_svg"]).getroot()
    assert back.get("viewBox") == cut.get("viewBox")
    assert back.find(".//{*}g[@id='back_engrave']/{*}path") is not None
    ids = [p["id"] for p in puzzle["pieces"] if p["is_whimsy"]]
    manifest = export_parts(
        result.out_dir, tmp_path / "replacement", ids, seed=9, rotate=True
    )
    assert manifest["pieces"][0]["piece"] == ids[0]
    assert (
        ET.parse(tmp_path / "replacement/parts-artwork.svg").find(".//{*}use")
        is not None
    )
    with pytest.raises(ValueError, match="cover"):
        solve_regions(outline, regions, 999, 1)
