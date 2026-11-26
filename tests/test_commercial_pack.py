"""Automated verification suite for the 750-Piece Exclusive Helleristning Commercial Readiness Pack.

Verifies:
  1. docs/helleristning-750/selected-750-catalog.json has exactly 750 unique archival motifs,
     100% commercial_ok=True (0 Alta NC-SA, 0 synthetic/VAE), 0 missing URLs/licenses/creators,
     excludes known unknown-creator defects (e.g. helleristning_01460), passes kerf/neck/tip
     screens, and loads cleanly via heller_jigsaw.data.corpus.load_corpus_catalog / load_motif_asset.
  2. docs/helleristning-750/rights-inventory.{csv,json,md} and archive-inquiry-drafts.md match
     selected-750-catalog.json 1:1 (750 rows), enforce honest legal hygiene (NOT cleared for sale),
     separate Non-ShareAlike vs ShareAlike obligations, and keep drafts marked UNSENT.
  3. docs/helleristning-750/eu-norway-safety-provisional-review.md keeps EU vs Norway distinct
     and explicitly declares provisional non-certification status.
  4. runs/helleristning-750-commercial/calibration_coupon.svg re-parses cleanly as 1:1 mm SVG
     with strictly separated #interior-cuts, #exterior-cuts, #engraving layers, valid geometry,
     zero duplicate cut paths, and accompanied by calibration_coupon_preview.png + worksheets.
  5. docs/helleristning-750/packaging-alignment.md references the Figma concept, 750 pieces,
     mass/volume estimates, booklet spec, and bilingual 14+ safety label without CE mark.
  6. Real seam sample extractor works cleanly (tested on synthetic partition fixture + live run
     if runs/helleristning-750/partition.json exists).
"""

from __future__ import annotations

import csv
import json
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from shapely.affinity import translate
from shapely.geometry import LineString, Polygon, box
from shapely.ops import polygonize, unary_union

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from heller_jigsaw.data.corpus import load_corpus_catalog, load_motif_asset  # noqa: E402
from scripts import build_commercial_pack as bcp  # noqa: E402

DOCS_DIR = ROOT / "docs" / "helleristning-750"
COMM_DIR = ROOT / "runs" / "helleristning-750-commercial"
LIVE_RUN_DIR = ROOT / "runs" / "helleristning-750"
LIVE_PARTITION = LIVE_RUN_DIR / "partition.json"
SVG_NS = "{http://www.w3.org/2000/svg}"


def test_selected_750_catalog_integrity_and_corpus_compat():
    cat_path = DOCS_DIR / "selected-750-catalog.json"
    assert cat_path.exists(), "selected-750-catalog.json missing"
    data = json.loads(cat_path.read_text(encoding="utf-8"))
    items = data["items"]
    assert len(items) == 750

    seen_ids = set()
    seen_piece_indices = set()
    seen_phashes = set()

    for it in items:
        assert it["commercial_ok"] is True
        lic = str(it["license"]).lower()
        assert "nc" not in lic and "non-commercial" not in lic and "alta museum" not in lic
        assert "vae" not in str(it.get("source_archive", "")).lower()
        assert "synthetic" not in str(it.get("source_archive", "")).lower()

        # Primary required metadata non-empty
        for key in (
            "piece_index",
            "piece_id",
            "motif_id",
            "motif_class",
            "site",
            "archive",
            "source_url",
            "creator",
            "license",
            "license_url",
            "attribution_text",
            "commercial_review_status",
        ):
            assert it.get(key) not in (None, ""), f"Missing {key} on {it.get('motif_id')}"

        assert not bcp.has_unknown_creator(it["creator"]), f"Unknown creator slipped in: {it['motif_id']}"
        assert it["commercial_review_status"] == "evidence_recorded_pending_legal_review"
        assert isinstance(it["open_actions"], list) and len(it["open_actions"]) >= 1
        assert isinstance(it["share_alike_required"], bool)
        assert it["share_alike_required"] == bcp.is_share_alike(it["license"])

        # Fabrication screen invariants
        cm = it["curation_metrics"]
        assert cm["kerf_pass_650mm2"] is True
        assert cm["neck_pass_650mm2"] is True
        assert cm["tip_loss_mm_650mm2"] <= 6.0

        seen_ids.add(it["motif_id"])
        seen_piece_indices.add(it["piece_index"])
        seen_phashes.add(it["shape_phash"])

    assert len(seen_ids) == 750
    assert seen_piece_indices == set(range(1, 751))
    assert len(seen_phashes) == 750
    assert "helleristning_01460" not in seen_ids

    # Non-ShareAlike maximization check: majority must be non-SA, and SA consolidated to Unimus
    non_sa = [it for it in items if not it["share_alike_required"]]
    sa = [it for it in items if it["share_alike_required"]]
    assert len(non_sa) >= 600, f"Expected >=600 non-SA motifs, got {len(non_sa)}"
    assert all(it["archive"] == "unimus" for it in sa), "All ShareAlike slots should be consolidated in Unimus"

    # Compatibility with heller_jigsaw.data.corpus loader
    records, base_dir = load_corpus_catalog(cat_path)
    assert len(records) == 750
    for sample_idx in (0, 374, 749):
        asset = load_motif_asset(records[sample_idx], base_dir)
        assert asset.mask.shape == (256, 256)
        assert asset.mask.sum() > 100


def test_rights_inventory_and_inquiry_drafts_1_to_1():
    cat = json.loads((DOCS_DIR / "selected-750-catalog.json").read_text(encoding="utf-8"))
    csv_path = DOCS_DIR / "rights-inventory.csv"
    json_path = DOCS_DIR / "rights-inventory.json"
    md_path = DOCS_DIR / "rights-inventory.md"
    drafts_path = DOCS_DIR / "archive-inquiry-drafts.md"

    for p in (csv_path, json_path, md_path, drafts_path):
        assert p.exists(), f"Missing {p}"

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        csv_rows = list(csv.DictReader(fh))
    assert len(csv_rows) == 750

    inv_json = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(inv_json["rows"]) == 750
    assert "NOT CLEARED FOR SALE" in inv_json["legal_disclaimer"].upper()

    # Exact 1:1 correspondence with selected-750-catalog.json
    for cat_it, csv_r, j_r in zip(cat["items"], csv_rows, inv_json["rows"]):
        assert int(csv_r["piece_index"]) == cat_it["piece_index"] == j_r["piece_index"]
        assert csv_r["piece_id"] == cat_it["piece_id"] == j_r["piece_id"]
        assert csv_r["motif_id"] == cat_it["motif_id"] == j_r["motif_id"]
        assert csv_r["source_url"].startswith("http")
        assert csv_r["license_url"].startswith("http")
        assert csv_r["commercial_review_status"] == "evidence_recorded_pending_legal_review"

    md_text = md_path.read_text(encoding="utf-8")
    assert "NOT CLEARED FOR SALE" in md_text.upper()
    assert "helleristning_01460" in md_text
    assert "ShareAlike" in md_text

    drafts_text = drafts_path.read_text(encoding="utf-8")
    assert "UNSENT" in drafts_text
    assert "shfa@gu.se" in drafts_text
    assert "post@um.uib.no" in drafts_text


def test_eu_norway_safety_and_packaging_docs():
    safety_path = DOCS_DIR / "eu-norway-safety-provisional-review.md"
    pkg_path = DOCS_DIR / "packaging-alignment.md"
    assert safety_path.exists()
    assert pkg_path.exists()

    s_txt = safety_path.read_text(encoding="utf-8")
    for token in (
        "NOT A COMPLIANCE CLAIM",
        "2023/988",
        "Produktkontrolloven",
        "2009/48/EC",
        "500 pieces",
        "0.062 mg/m",
        "Forbrukertilsynet",
        "Miljødirektoratet",
        "Leketøyforskriften",
        "2023/1115",
    ):
        assert token in s_txt, f"Missing regulatory token {token!r} in safety review"

    p_txt = pkg_path.read_text(encoding="utf-8")
    for token in (
        "node-id=299-65",
        "750",
        "14+",
        "DO NOT print CE mark",
        "rights-inventory.csv",
    ):
        assert token in p_txt, f"Missing packaging token {token!r} in packaging alignment"


def _parse_mlz_path(d: str):
    """Parse M / L / Z SVG path string into list of (x, y) coordinates and closed flag."""
    tokens = d.replace(",", " ").split()
    pts = []
    closed = False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("M", "L"):
            pts.append((float(tokens[i + 1]), float(tokens[i + 2])))
            i += 3
        elif tok in ("Z", "z"):
            closed = True
            i += 1
        else:
            pts.append((float(tok), float(tokens[i + 1])))
            i += 2
    return pts, closed


def test_calibration_coupon_svg_reparse_and_worksheets():
    svg_path = COMM_DIR / "calibration_coupon.svg"
    png_path = COMM_DIR / "calibration_coupon_preview.png"
    ws_md = DOCS_DIR / "calibration-measurement-worksheet.md"
    ws_html = DOCS_DIR / "calibration-measurement-worksheet.html"

    for p in (svg_path, png_path, ws_md, ws_html):
        assert p.exists(), f"Missing calibration artifact {p}"
        assert p.stat().st_size > 1000

    tree = ET.parse(svg_path)
    root = tree.getroot()
    assert root.attrib["width"].endswith("mm")
    assert root.attrib["height"].endswith("mm")
    w_mm = float(root.attrib["width"][:-2])
    h_mm = float(root.attrib["height"][:-2])
    assert w_mm == pytest.approx(240.0, abs=0.01)
    assert h_mm == pytest.approx(185.0, abs=0.01)

    groups = {g.attrib.get("id"): g for g in root.findall(f"{SVG_NS}g")}
    assert set(groups.keys()) == {"interior-cuts", "exterior-cuts", "engraving"}
    assert groups["interior-cuts"].attrib["stroke"] == "#FF0000"
    assert groups["exterior-cuts"].attrib["stroke"] == "#0000FF"
    assert groups["engraving"].attrib["stroke"] == "#008800"

    # Re-parse every single SVG path across all 3 layers and verify clean geometry + bounds
    seen_cut_ds = set()
    closed_cut_polys = []
    for gid in ("interior-cuts", "exterior-cuts", "engraving"):
        paths = groups[gid].findall(f"{SVG_NS}path")
        assert len(paths) >= 1
        for el in paths:
            d = el.attrib["d"]
            assert d.strip()
            if gid != "engraving":
                assert d not in seen_cut_ds, f"Duplicate cut path detected: {el.attrib.get('id')}"
                seen_cut_ds.add(d)
            pts, closed = _parse_mlz_path(d)
            assert len(pts) >= 2, f"Degenerate path {el.attrib.get('id')}"
            for x, y in pts:
                assert -0.5 <= x <= w_mm + 0.5
                assert -0.5 <= y <= h_mm + 0.5
            if gid == "interior-cuts" and closed:
                poly = Polygon(pts)
                assert poly.is_valid and poly.area > 5.0, f"Invalid closed cut polygon: {el.attrib.get('id')}"
                closed_cut_polys.append((el.attrib.get("id"), poly))

    # Verify zero overlapping closed interior cut shapes (e.g. fit pairs never collide!)
    for i in range(len(closed_cut_polys)):
        id_a, pa = closed_cut_polys[i]
        for j in range(i + 1, len(closed_cut_polys)):
            id_b, pb = closed_cut_polys[j]
            inter_area = pa.intersection(pb).area
            assert inter_area < 0.01, f"Overlapping interior cuts: {id_a} vs {id_b} ({inter_area:.2f} mm²)"

    # Verify outer frame is a valid closed polygon matching coupon bounds
    ext_paths = groups["exterior-cuts"].findall(f"{SVG_NS}path")
    assert len(ext_paths) == 1
    outer_pts, outer_closed = _parse_mlz_path(ext_paths[0].attrib["d"])
    assert outer_closed
    outer_poly = Polygon(outer_pts)
    assert outer_poly.is_valid and outer_poly.area > 42000.0

    # Verify required calibration features exist by ID
    int_ids = {el.attrib["id"] for el in groups["interior-cuts"].findall(f"{SVG_NS}path")}
    assert "kerf-step-comb" in int_ids
    assert "scale-square-50mm" in int_ids
    for nw in ("2.0", "2.5", "3.0", "3.5", "4.0", "5.0"):
        assert f"neck-h-{nw}mm" in int_ids
        assert f"neck-v-{nw}mm" in int_ids


def _verify_seam_sample_svg(
    svg_path: Path,
    expected_w_mm: float,
    expected_h_mm: float,
    expected_cluster_polys: list[Polygon],
    min_seg_mm: float = 0.0,
) -> None:
    """Re-parse a seam-sample SVG and assert 1:1 mm scale, strict layers, zero duplicates, and clean polygonization."""
    root = ET.parse(svg_path).getroot()
    assert root.attrib["width"].endswith("mm")
    assert root.attrib["height"].endswith("mm")
    w_mm = float(root.attrib["width"][:-2])
    h_mm = float(root.attrib["height"][:-2])
    assert w_mm == pytest.approx(expected_w_mm, abs=0.05)
    assert h_mm == pytest.approx(expected_h_mm, abs=0.05)

    groups = {g.attrib["id"]: g for g in root.findall(f"{SVG_NS}g")}
    assert set(groups.keys()) == {"interior-cuts", "exterior-cuts", "engraving"}
    assert groups["interior-cuts"].attrib["stroke"] == "#FF0000"
    assert groups["exterior-cuts"].attrib["stroke"] == "#0000FF"
    assert groups["engraving"].attrib["stroke"] == "#008800"

    seen_cut_ds = set()
    cut_lines = []
    eng_lines = []
    for gid in ("interior-cuts", "exterior-cuts", "engraving"):
        paths = groups[gid].findall(f"{SVG_NS}path")
        assert len(paths) >= 1, f"Layer {gid} empty in {svg_path.name}"
        for el in paths:
            d = el.attrib["d"]
            assert d.strip()
            pts, _ = _parse_mlz_path(d)
            assert len(pts) >= 2
            for x, y in pts:
                assert -0.1 <= x <= w_mm + 0.1, f"{svg_path.name}:{el.attrib.get('id')} x={x} out of [0, {w_mm}]"
                assert -0.1 <= y <= h_mm + 0.1, f"{svg_path.name}:{el.attrib.get('id')} y={y} out of [0, {h_mm}]"
            ls = LineString(pts)
            if gid != "engraving":
                assert d not in seen_cut_ds, f"Duplicate cut path in {svg_path.name}: {el.attrib.get('id')}"
                seen_cut_ds.add(d)
                cut_lines.append(ls)
                if min_seg_mm > 0.0:
                    for p0, p1 in zip(pts[:-1], pts[1:]):
                        seg_len = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
                        assert seg_len >= min_seg_mm - 1e-4, (
                            f"Micro-segment {seg_len:.5f} mm < {min_seg_mm} mm in {svg_path.name}:{el.attrib.get('id')}"
                        )
            else:
                eng_lines.append(ls)

    # Cut paths must polygonize back cleanly into the exact cluster faces (excluding any enclosed holes)
    cluster_union = unary_union(expected_cluster_polys)
    cx0, cy0, _, _ = cluster_union.bounds
    ox, oy = cx0 - 6.0, cy0 - 14.0
    shifted_union = translate(cluster_union, xoff=-ox, yoff=-oy)

    noded = unary_union(cut_lines)
    rebuilt_all = [p for p in polygonize(noded) if p.is_valid and p.area > 1.0]
    rebuilt_cluster = [p for p in rebuilt_all if shifted_union.contains(p.representative_point())]
    expected_holes = len(shifted_union.interiors) if shifted_union.geom_type == "Polygon" else 0
    assert len(rebuilt_cluster) == len(expected_cluster_polys), (
        f"{svg_path.name}: expected {len(expected_cluster_polys)} rebuilt pieces, got {len(rebuilt_cluster)}"
    )
    assert len(rebuilt_all) - len(rebuilt_cluster) == expected_holes, (
        f"{svg_path.name}: unexpected extra polygonized faces ({len(rebuilt_all) - len(rebuilt_cluster)} vs {expected_holes} holes)"
    )
    rebuilt_area = sum(p.area for p in rebuilt_cluster)
    assert rebuilt_area == pytest.approx(cluster_union.area, rel=1e-3, abs=0.5)
    assert unary_union(rebuilt_cluster).symmetric_difference(shifted_union).area < 0.05

    # Engraving strokes must never cross any cut line
    cut_union = unary_union(cut_lines)
    for eng in eng_lines:
        assert not eng.intersects(cut_union), f"Engraving stroke intersects a cut line in {svg_path.name}"


def test_packaging_sync_and_seam_sample_extractor_on_fixture(tmp_path):
    """Verify packaging board-dim sync & seam-sample extractor on a multi-edge fixture (incl. relative paths)."""
    # Construct a 3x3 grid partition fixture, deliberately splitting one shared edge into two canonical
    # segments to guard against duplicate-neighbor selection bugs.
    faces = []
    edges = []
    eid = 0
    cell = 28.0
    for r in range(3):
        for c in range(3):
            fid = r * 3 + c
            x0, y0 = c * cell, r * cell
            poly = box(x0, y0, x0 + cell, y0 + cell)
            faces.append(
                {
                    "face_id": fid,
                    "exterior": list(poly.exterior.coords),
                    "metadata": {"asset_id": f"fixture_{fid:03d}", "motif_class": "bronze_age_ship"},
                }
            )
    for r in range(3):
        for c in range(2):
            a, b = r * 3 + c, r * 3 + c + 1
            x = (c + 1) * cell
            if r == 1 and c == 0:
                # Split shared seam into 2 canonical edges between the same face pair
                ym = r * cell + cell * 0.5
                edges.append({"edge_id": eid, "left_face": a, "right_face": b, "is_exterior": False, "coords": [[x, r * cell], [x, ym]]})
                eid += 1
                edges.append({"edge_id": eid, "left_face": a, "right_face": b, "is_exterior": False, "coords": [[x, ym], [x, (r + 1) * cell]]})
                eid += 1
            else:
                edges.append({"edge_id": eid, "left_face": a, "right_face": b, "is_exterior": False, "coords": [[x, r * cell], [x, (r + 1) * cell]]})
                eid += 1
    for r in range(2):
        for c in range(3):
            a, b = r * 3 + c, (r + 1) * 3 + c
            y = (r + 1) * cell
            edges.append({"edge_id": eid, "left_face": a, "right_face": b, "is_exterior": False, "coords": [[c * cell, y], [(c + 1) * cell, y]]})
            eid += 1
    for r in range(3):
        edges.append({"edge_id": eid, "left_face": r * 3, "right_face": -1, "is_exterior": True, "coords": [[0.0, r * cell], [0.0, (r + 1) * cell]]})
        eid += 1
        edges.append({"edge_id": eid, "left_face": r * 3 + 2, "right_face": -1, "is_exterior": True, "coords": [[3 * cell, r * cell], [3 * cell, (r + 1) * cell]]})
        eid += 1
    for c in range(3):
        edges.append({"edge_id": eid, "left_face": c, "right_face": -1, "is_exterior": True, "coords": [[c * cell, 0.0], [(c + 1) * cell, 0.0]]})
        eid += 1
        edges.append({"edge_id": eid, "left_face": 6 + c, "right_face": -1, "is_exterior": True, "coords": [[c * cell, 3 * cell], [(c + 1) * cell, 3 * cell]]})
        eid += 1

    run_dir = tmp_path / "fixture_run"
    run_dir.mkdir()
    (run_dir / "partition.json").write_text(json.dumps({"faces": faces, "edges": edges}), encoding="utf-8")

    orig_comm_dir = bcp.COMMERCIAL_RUN_DIR
    orig_docs_dir = bcp.DOCS_DIR
    try:
        bcp.COMMERCIAL_RUN_DIR = tmp_path / "comm_out"
        bcp.DOCS_DIR = tmp_path / "docs_out"
        bcp.DOCS_DIR.mkdir(parents=True, exist_ok=True)

        # 1. Verify packaging sync on fixture (even when run_dir is outside ROOT)
        pkg = bcp.build_packaging_alignment(puzzle_run_dir=run_dir)
        assert pkg["board"]["is_locked"] is True
        assert pkg["board"]["piece_count"] == 9
        assert pkg["board"]["width_mm"] == pytest.approx(84.0, abs=0.01)
        assert pkg["board"]["height_mm"] == pytest.approx(84.0, abs=0.01)
        pkg_md = (bcp.DOCS_DIR / "packaging-alignment.md").read_text(encoding="utf-8")
        assert "LOCKED FROM" in pkg_md
        assert "PROVISIONAL (awaiting" not in pkg_md
        assert "- [x] Sync final locked assembled dimensions" in pkg_md
        assert "84.0 × 84.0 mm" in pkg_md

        # 2. Verify seam-sample extractor on fixture
        manifest = bcp.build_real_seam_samples(puzzle_run_dir=run_dir, n_samples=3)
        assert manifest is not None
        assert manifest["sample_count"] == 3
        assert (tmp_path / "comm_out" / "seam_samples" / "seam_samples_preview.png").exists()
        fixture_polys = [Polygon(f["exterior"]) for f in faces]
        for s in manifest["samples"]:
            # Zero duplicate piece numbers in any cluster
            assert len(s["cluster_piece_numbers"]) == len(set(s["cluster_piece_numbers"]))
            svg_p = tmp_path / "comm_out" / "seam_samples" / f"seam_sample_{s['sample_index']:02d}.svg"
            png_p = tmp_path / "comm_out" / "seam_samples" / f"seam_sample_{s['sample_index']:02d}_preview.png"
            assert svg_p.exists()
            assert png_p.exists() and png_p.stat().st_size > 1000
            _verify_seam_sample_svg(
                svg_p,
                expected_w_mm=s["width_mm"],
                expected_h_mm=s["height_mm"],
                expected_cluster_polys=[fixture_polys[pnum - 1] for pnum in s["cluster_piece_numbers"]],
            )
    finally:
        bcp.COMMERCIAL_RUN_DIR = orig_comm_dir
        bcp.DOCS_DIR = orig_docs_dir


@pytest.mark.skipif(not LIVE_PARTITION.exists(), reason="750-piece production partition not yet generated")
def test_live_750_packaging_synced_with_partition():
    """Verify docs/helleristning-750/packaging-alignment.md is synced to the exact locked 750-piece board."""
    board = bcp._probe_locked_board_dims(LIVE_RUN_DIR)
    assert board["is_locked"] is True
    assert board["piece_count"] == 750

    pkg_md = (DOCS_DIR / "packaging-alignment.md").read_text(encoding="utf-8")
    assert "LOCKED FROM `runs/helleristning-750/partition.json`" in pkg_md
    assert "PROVISIONAL (awaiting" not in pkg_md
    assert f"{board['width_mm']:.1f} × {board['height_mm']:.1f} mm" in pkg_md
    assert f"{board['board_area_mm2']:,.1f} mm²" in pkg_md
    assert "- [x] Sync final locked assembled dimensions" in pkg_md


@pytest.mark.skipif(not LIVE_PARTITION.exists(), reason="750-piece production partition not yet generated")
def test_live_750_seam_samples_svgs_and_previews():
    """Verify all 6 real 1:1 mm seam-sample SVGs + previews match runs/helleristning-750/partition.json."""
    seam_dir = COMM_DIR / "seam_samples"
    manifest_path = seam_dir / "seam_samples_manifest.json"
    contact_png = seam_dir / "seam_samples_preview.png"
    assert manifest_path.exists(), "seam_samples_manifest.json missing — run --stage seam-samples"
    assert contact_png.exists() and contact_png.stat().st_size > 5000

    part = json.loads(LIVE_PARTITION.read_text(encoding="utf-8"))
    polys = [Polygon(f["exterior"]).buffer(0) for f in part["faces"]]
    assert len(polys) == 750

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_partition"] == "runs/helleristning-750/partition.json"
    assert manifest["sample_count"] == 6
    samples = manifest["samples"]
    assert len(samples) == 6

    all_cluster_pieces = set()
    for idx, s in enumerate(samples, start=1):
        assert s["sample_index"] == idx
        cluster = s["cluster_piece_numbers"]
        assert len(cluster) >= 2
        assert len(cluster) == len(set(cluster))
        assert s["target_piece_number"] in cluster
        # Clusters must be spatially disjoint across all 6 samples
        assert all_cluster_pieces.isdisjoint(cluster)
        all_cluster_pieces.update(cluster)

        svg_p = ROOT / s["svg_path"]
        png_p = ROOT / s["preview_png_path"]
        assert svg_p.exists()
        assert png_p.exists() and png_p.stat().st_size > 5000

        _verify_seam_sample_svg(
            svg_p,
            expected_w_mm=s["width_mm"],
            expected_h_mm=s["height_mm"],
            expected_cluster_polys=[polys[pnum - 1] for pnum in cluster],
            min_seg_mm=0.05,
        )
