import xml.etree.ElementTree as ET
from collections import Counter

import numpy as np
import pytest

from dendrite_engine.spectre import RULES, generate


@pytest.mark.parametrize(
    "root,depth,n",
    [
        ("Delta", 0, 1),
        ("Gamma", 0, 2),
        ("Gamma", 1, 8),
        ("Delta", 1, 9),
        ("Delta", 2, 71),
    ],
)
def test_spectre_substitution_counts_handedness_and_adjacency(tmp_path, root, depth, n):
    result = generate(tmp_path, depth=depth, root=root)
    assert result["pieces"] == result["verified_svg_pieces"] == n
    assert result["tile_chiralities"] == 1
    assert result["hole_count"] == 0 and result["overlap_mm2"] < 0.0001
    seen, uses = {0}, Counter()
    for tile in result["tiles"]:
        assert len(tile["signed_cut_ids"]) == 14
        transform = np.array(tile["transform"])[:, :2]
        assert (
            np.allclose(transform @ transform.T, np.eye(2))
            and np.linalg.det(transform) > 0
        )
        uses.update(tile["signed_cut_ids"])
        for j in tile["neighbors"]:
            assert tile["id"] in result["tiles"][j]["neighbors"]
    while len(seen) < n:
        grown = seen.union(*(set(result["tiles"][i]["neighbors"]) for i in seen))
        assert grown != seen
        seen = grown
    assert result["cut_paths"] == result["shared_edges"] + result["outer_edges"]
    assert 2 * result["shared_edges"] + result["outer_edges"] == 14 * n
    for edge in range(1, result["cut_paths"] + 1):
        assert max(uses[edge], uses[-edge]) == 1


def test_polygon_and_curved_variants_are_different_congruent_patches(tmp_path):
    straight = generate(tmp_path / "straight", 1, "Xi", 9, "polygon")
    curved = generate(tmp_path / "curved", 1, "Xi", 9, "curved")
    assert straight["family"] == "Tile(1,1)" and curved["family"] == "Spectre"
    assert straight["area_mm2"] == pytest.approx(curved["area_mm2"], abs=0.01)
    for name, samples in [("straight", 2), ("curved", 17)]:
        svg = ET.parse(tmp_path / name / "cut.svg").getroot()
        art = ET.parse(tmp_path / name / "artwork.svg").getroot()
        assert svg.attrib["viewBox"] == art.attrib["viewBox"]
        assert len(art.findall(".//{*}pattern[@id='design-0']")) == 1
        assert len(art.findall(".//{*}pattern[@id='design-1']")) == 1
        assert {tile["print_design"] for tile in curved["tiles"]} == {
            "orbits",
            "ribbons",
        }
        for path in svg.findall(".//{*}path"):
            xy = np.fromstring(path.attrib["d"][1:].replace(",", " "), sep=" ").reshape(
                -1, 2
            )
            assert len(xy) == samples
            assert np.linalg.norm(xy[0] - xy[-1]) == pytest.approx(9, abs=0.0002)
    assert set(RULES) == {
        "Gamma",
        "Delta",
        "Theta",
        "Lambda",
        "Xi",
        "Pi",
        "Sigma",
        "Phi",
        "Psi",
    }
