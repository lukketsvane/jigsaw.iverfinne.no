import json
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from shapely import union_all
from shapely.affinity import translate
from shapely.geometry import Polygon, box

from pathnet.lizard_net import make_warp
from pathnet.lizards import build, generate, verify_cut


@pytest.mark.parametrize(("cols", "rows", "seed"), [(1, 1, 0), (3, 2, 41), (9, 4, 387)])
def test_cut_covers_the_wrapped_window_exactly_once(cols, rows, seed):
    data, _, report = build(seed, cols, rows)
    (w, _), (_, h) = data["periods"]
    window = box(0, 0, w, h)
    fragments = []
    for piece in data["pieces"]:
        for dx in [-w, 0, w]:
            for dy in [-h, 0, h]:
                inside = translate(Polygon(piece["outline"]), dx, dy).intersection(window)
                if inside.area > 0:
                    fragments.append(inside)
    cover = union_all(fragments, grid_size=1.e-7)
    assert window.difference(cover).area < 1.e-5
    assert abs(sum(p.area for p in fragments) - cover.area) < 1.e-5
    assert report["torus_seams"] == 3 * len(data["pieces"])
    assert report["different_solid_colors"] == 6 * cols * rows


def test_seed_changes_shape_and_commutes_with_both_translations():
    u, v = np.array([18., 2.]), np.array([-1., 12.])
    pts = np.random.default_rng(27).uniform(-20, 20, (91, 2))
    warp = make_warp(17, u, v)
    assert np.array_equal(make_warp(17, u, v)(pts), warp(pts))
    assert np.max(np.abs(make_warp(18, u, v)(pts) - warp(pts))) > .01
    for delta in [u, -v, 2*u-v]:
        np.testing.assert_allclose(warp(pts + delta), warp(pts) + delta, atol=1.e-12)


def test_standalone_exports_and_duplicate_cut_detection(tmp_path):
    report = generate(tmp_path, seed=5, cols=2, rows=2)
    assert report["verified_svg_faces"] == 24
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "__PUZZLE_JSON__" not in html
    data = json.JSONDecoder().raw_decode(html.split('id="puzzle-data"', 1)[1].split('>', 1)[1].lstrip())[0]
    assert len(data["pieces"]) == 24
    assert (tmp_path / "assembly.png").stat().st_size > 100
    assert (tmp_path / "detail.png").stat().st_size > 100
    cut = tmp_path / "cut.svg"
    doc = ET.parse(cut)
    group = doc.getroot()[0]
    group.append(group[0])
    doc.write(cut)
    with pytest.raises(ValueError, match="Duplicate"):
        verify_cut(cut, data["pieces"])
