import base64
import io
import json
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from PIL import Image
from shapely.geometry import LineString, Polygon, box

from dendrite_engine.artwork import read_artwork
from dendrite_engine.cli import generate_puzzle_run
from dendrite_engine.postprocess import export_puzzle
from dendrite_engine.server import _decode_data_url_image


def striped_photo():
    # The square board's centre crop must discard the outside two stripes.
    colors = [[255, 0, 0], [20, 180, 45], [120, 35, 220], [0, 0, 255]]
    stripes = np.repeat(np.array(colors, np.uint8), 24, axis=0)
    return np.tile(stripes, (48, 1, 1))


def test_translated_irregular_print_and_preview(tmp_path):
    image = striped_photo()
    board = Polygon([(12, 24), (132, 24), (132, 144), (12, 144), (72, 84)])
    # This engraving belongs in the laser layer and the preview, not in the ink.
    engraving = LineString([(85, 65), (95, 65)])
    result = export_puzzle(tmp_path, [board], [], [board.exterior], [engraving],
                           expected_outline=board, artwork_image=image)
    svg = ET.parse(result.files["artwork_svg"]).getroot()
    laser = ET.parse(result.files["cut_svg"]).getroot()
    assert svg.get("viewBox") == laser.get("viewBox") == "0 0 120.0000 120.0000"
    photo = svg.find("{*}image")
    assert photo.get("clip-path") == "url(#board-mask)"
    assert "60.000 60.000" in svg.find(".//{*}clipPath/{*}path").get("d")
    assert svg.find("./{*}g[@id='engrave_preview']") is None
    assert laser.find(".//{*}g[@id='engrave']/{*}path") is not None
    art = json.loads((tmp_path / "puzzle.json").read_text(encoding="utf-8"))["artwork"]
    assert (art["x"], art["y"], art["width"], art["height"]) == (0, 0, 120, 120)
    assert art["preserveAspectRatio"] == photo.get("preserveAspectRatio") == "xMidYMid slice"
    assert art["href"] == photo.get("href")
    restored = np.asarray(Image.open(io.BytesIO(base64.b64decode(art["href"].split(",")[1]))))
    np.testing.assert_array_equal(restored, image)
    preview = np.asarray(Image.open(result.files["preview_png"]).convert("RGB"))
    scale = preview.shape[1] / 136.0  # 120 mm board + 8 mm padding on both sides
    for (x, y), color in [((45, 15), [20, 180, 45]), ((85, 15), [120, 35, 220])]:
        np.testing.assert_array_equal(preview[round((8+y)*scale), round((8+x)*scale)], color)
    assert np.min(preview[round((8+60)*scale), round((8+10)*scale)]) > 200


@pytest.mark.parametrize("style", ["wave", "dendrite"])
def test_both_engines_export_supplied_photo(tmp_path, style):
    result = generate_puzzle_run(out_dir=tmp_path, style=style, width_mm=120, height_mm=100,
                                 num_pieces=6, seed=13, px_mm=1,
                                 overrides={"steps": 12, "growth_steps": 12},
                                 artwork_image=striped_photo())
    payload = json.loads((tmp_path / "puzzle.json").read_text(encoding="utf-8"))
    assert payload["artwork"]["href"] == ET.parse(result.files["artwork_svg"]).find("{*}image").get("href")


def test_without_photo_keeps_wood_fill(tmp_path):
    board = box(0, 0, 80, 60)
    result = export_puzzle(tmp_path, [board], [], [board.exterior], expected_outline=board)
    root = ET.parse(result.files["artwork_svg"]).getroot()
    assert root.find("{*}image") is None
    assert root.find("{*}g[@id='pieces']/{*}path").get("fill").startswith("#")
    assert "artwork" not in json.loads((tmp_path / "puzzle.json").read_text(encoding="utf-8"))


def test_upload_matches_oriented_file_and_retains_transparency(tmp_path):
    pixel = np.array([[[10, 120, 35, 80], [200, 18, 120, 0]]], dtype=np.uint8)
    exif = Image.Exif()
    exif[274] = 6
    file = tmp_path / "oriented.png"
    Image.fromarray(pixel).save(file, exif=exif)
    encoded = "data:image/png;base64," + base64.b64encode(file.read_bytes()).decode()
    decoded = _decode_data_url_image(encoded)
    np.testing.assert_array_equal(decoded, pixel.transpose(1, 0, 2))
    np.testing.assert_array_equal(read_artwork(file), decoded)
