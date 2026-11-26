import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np
import pytest
from shapely import union_all
from shapely.affinity import translate
from shapely.geometry import Polygon, box

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pathnet.lizard_net import make_warp
from pathnet.lizards import (
    DEFAULT_WIDTH,
    audit_board,
    build,
    generate,
    svg_files,
    verify_cut,
    verify_engrave,
    verify_production_svg,
)


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
    for delta in [u, -v, 2 * u - v]:
        np.testing.assert_allclose(warp(pts + delta), warp(pts) + delta, atol=1.e-12)


def test_standalone_exports_and_duplicate_cut_detection(tmp_path):
    report = generate(tmp_path, seed=5, cols=2, rows=2, width=140.0, run_motion=True)
    assert report["verified_svg_faces"] == 24
    assert report["all_hard_gates_passed"] is True
    for name in (
        "cut.svg", "engrave.svg", "production.svg", "artwork.svg",
        "index.html", "payload.json", "quality.json",
        "production-audit.json", "thin-feature-audit.json",
        "finite-motion-audit.json", "rigid-motion-screen.json",
        "assembly.png", "detail.png", "detail2.png",
    ):
        assert (tmp_path / name).stat().st_size > 100, f"missing or empty {name}"
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "__PUZZLE_JSON__" not in html
    data = json.JSONDecoder().raw_decode(html.split('id="puzzle-data"', 1)[1].split('>', 1)[1].lstrip())[0]
    assert len(data["pieces"]) == 24
    assert all("gradient" in p and len(p["gradient"]["stops"]) == 3 for p in data["pieces"])

    # Motion audits must block every proper group and single-piece escape.
    finite = json.loads((tmp_path / "finite-motion-audit.json").read_text(encoding="utf-8"))
    rigid = json.loads((tmp_path / "rigid-motion-screen.json").read_text(encoding="utf-8"))
    assert finite["unblocked_directions_by_distance"]["1.0"] == 0
    assert finite["all_proper_groups_blocked_within_3mm"] is True
    assert rigid["pieces_with_rotation_4deg_or_more"] == 0
    assert rigid["pieces_with_translation_3mm"] == 0

    # Tampered cut SVG must fail duplicate-cut detection.
    cut = tmp_path / "cut.svg"
    doc = ET.parse(cut)
    group = doc.getroot()[0]
    group.append(group[0])
    doc.write(cut)
    with pytest.raises(ValueError, match="Duplicate"):
        verify_cut(cut, data["pieces"])


def test_dual_layer_svgs_and_engraving_clearance_checks(tmp_path):
    data, cuts, _ = build(seed=41, cols=2, rows=2, width=130.0)
    svg_files(data, cuts, tmp_path, color_mode="gradient")
    assert verify_cut(tmp_path / "cut.svg", data["pieces"]) == 24
    eng = verify_engrave(tmp_path / "engrave.svg", data["pieces"], kerf_mm=0.25)
    assert eng["verified_spines"] == 24
    assert eng["verified_eyes"] == 48
    assert eng["min_eye_clearance_mm"] > 0.5
    assert eng["min_spine_clearance_mm"] > 0.3
    prod = verify_production_svg(tmp_path / "production.svg", data["pieces"], kerf_mm=0.25)
    assert prod == dict(verified_cut_faces=24, verified_spines=24, verified_eyes=48)
    artwork = (tmp_path / "artwork.svg").read_text(encoding="utf-8")
    assert 'id="rainbow-artwork"' in artwork and 'clip-path="url(#board-silhouette)"' in artwork

    # Solid colour mode remains available.
    solid_dir = tmp_path / "solid"
    solid_dir.mkdir()
    svg_files(data, cuts, solid_dir, color_mode="solid")
    assert 'id="lizard-0"' in (solid_dir / "artwork.svg").read_text(encoding="utf-8")

    # Engraving contamination inside cut.svg must fail verify_cut.
    bad_cut = tmp_path / "bad_cut.svg"
    doc = ET.parse(tmp_path / "cut.svg")
    ET.SubElement(doc.getroot(), "{http://www.w3.org/2000/svg}circle", cx="0", cy="0", r="1")
    doc.write(bad_cut)
    with pytest.raises(ValueError, match="circles"):
        verify_cut(bad_cut, data["pieces"])

    # An eye displaced outside its lizard must fail verify_engrave.
    bad_eng = tmp_path / "bad_engrave.svg"
    edoc = ET.parse(tmp_path / "engrave.svg")
    eye = edoc.getroot().find(".//{http://www.w3.org/2000/svg}circle")
    eye.attrib["cx"] = "9999.0"
    edoc.write(bad_eng)
    with pytest.raises(ValueError, match="leaves kerf-eroded piece"):
        verify_engrave(bad_eng, data["pieces"], kerf_mm=0.25)


def test_full_216_piece_seed387_passes_fabrication_and_interlock_gates(tmp_path):
    data, cuts, report = build(seed=387, cols=9, rows=4, width=DEFAULT_WIDTH)
    assert len(data["pieces"]) == 216
    assert report["torus_area_error_mm2"] < 1e-8
    assert report["min_outline_iou"] >= 0.75
    assert report["median_outline_iou"] >= 0.85
    svg_files(data, cuts, tmp_path, color_mode="gradient")
    audits = audit_board(data, tmp_path, report, run_motion=False)
    pa = audits["production_audit"]
    th = audits["thin_feature_audit"]

    assert pa["geometry_pass"] is True
    assert pa["digital_screen_pass"] is True
    assert pa["lock_margin_mm"] >= 3.0
    assert pa["unlocked_directions_at_1mm"] == 0
    assert pa["unlocked_directions_at_2mm"] == 0
    assert pa["unlocked_directions_at_3mm"] == 0
    for k in ("0.10", "0.15", "0.20", "0.25"):
        assert pa[f"kerf_{k}"]["kerf_pass"] == 216
        assert pa[f"kerf_{k}"]["neck_pass"] == 216
        assert pa["strict_kerf_failures"][k] == 0
    assert pa["strict_neck_2_5mm_failures"] == 0
    assert pa["strict_neck_3_0mm_failures"] == 0
    assert pa["min_wood_width_mm"] >= 3.0
    assert th["worst_tip_reach_mm"] <= 3.0
    assert th["worst_tip_loss_mm"] <= 3.0
    assert pa["engrave_verification"]["min_eye_clearance_mm"] >= 1.5
    assert pa["engrave_verification"]["min_spine_clearance_mm"] >= 0.5
