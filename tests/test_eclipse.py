import json
from xml.etree import ElementTree as ET

import pytest

from dendrite_engine.eclipse import generate_eclipse, verify_eclipse


def test_six_assemblies_reimport_the_same_reversible_pieces(tmp_path):
    report = generate_eclipse(tmp_path, diameter_mm=120, sectors=8, bands=2, seed=4)
    assert report == verify_eclipse(tmp_path)
    assert report["pieces"] == 17 and report["duplicate_cut_segments"] == 0
    assert report["states"]["sun"]["front"] == report["states"]["moon"]["back"] == 17
    assert report["states"]["corona"]["back"] == 9
    assert report["states"]["ingress"]["back"] == 2
    assert report["states"]["partial"]["back"] == 8
    assert report["states"]["late"]["back"] == 15
    assert len(report["states"]) == 6
    assert (tmp_path / "six-states.png").is_file()
    assert all(s["max_piece_difference_mm2"] < 0.01 for s in report["states"].values())
    assert all(s["coverage_error_mm2"] < 0.01 for s in report["states"].values())
    assert (tmp_path / "four-states.png").is_file()
    for art in ("artwork.svg", "back.svg"):
        assert (
            ET.parse(tmp_path / art)
            .find(".//{*}image")
            .get("href")
            .startswith("data:image/png;")
        )
    plan = json.loads((tmp_path / "states.json").read_text(encoding="utf-8"))
    plan["states"]["moon"][1]["slot"] = plan["states"]["moon"][2]["slot"]
    (tmp_path / "states.json").write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(ValueError, match="slot"):
        verify_eclipse(tmp_path)


def test_sector_count_supports_both_reflection_axes(tmp_path):
    with pytest.raises(ValueError, match="multiple of four"):
        generate_eclipse(tmp_path, sectors=7)
