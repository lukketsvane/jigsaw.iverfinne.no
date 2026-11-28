"""Comprehensive unit and integration tests for helleristninger.pipeline_science."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import cv2
import numpy as np
import pytest
from PIL import Image

from .dual_trace_qa import run_dual_trace_qa, select_stratified_qa_sample
from .extract_layers import (
    is_allowed_record,
    retrace_figure_method_b_otsu_marching_squares,
    trace_panel_scientific,
)
from .provenance import classify_core_region, resolve_scale_calibration, resolve_site_and_coords
from .run_science import run_science_pipeline
from .schema import validate_figure_record


def _draw_bear(canvas: np.ndarray, ox: int, oy: int, val: int = 20) -> None:
    cv2.ellipse(canvas, (ox + 110, oy + 95), (56, 34), 0, 0, 360, val, -1)
    cv2.ellipse(canvas, (ox + 48, oy + 82), (24, 18), -10, 0, 360, val, -1)
    cv2.circle(canvas, (ox + 58, oy + 64), 9, val, -1)
    cv2.rectangle(canvas, (ox + 68, oy + 112), (ox + 92, oy + 158), val, -1)
    cv2.rectangle(canvas, (ox + 130, oy + 112), (ox + 154, oy + 158), val, -1)


def _draw_ship(canvas: np.ndarray, ox: int, oy: int, val: int = 20) -> None:
    keel = np.array([[ox + 20, oy + 80], [ox + 50, oy + 135], [ox + 150, oy + 145], [ox + 250, oy + 135], [ox + 280, oy + 80]], np.int32)
    gunw = np.array([[ox + 35, oy + 92], [ox + 65, oy + 122], [ox + 150, oy + 128], [ox + 235, oy + 122], [ox + 265, oy + 92]], np.int32)
    cv2.polylines(canvas, [keel], False, val, 6)
    cv2.polylines(canvas, [gunw], False, val, 6)
    cv2.line(canvas, (ox + 20, oy + 80), (ox + 35, oy + 92), val, 6)
    cv2.line(canvas, (ox + 280, oy + 80), (ox + 265, oy + 92), val, 6)
    for x in range(ox + 75, ox + 230, 18):
        cv2.line(canvas, (x, oy + 124), (x, oy + 96), val, 5)


def test_painted_photo_and_mislabeled_photo_strictly_excluded(tmp_path: Path) -> None:
    assert is_allowed_record({"media_type": "painted_photo"}) == (False, "excluded_media_type:painted_photo")
    assert is_allowed_record({"media_type": "photo_other"}) == (False, "excluded_media_type:photo_other")
    assert is_allowed_record({"media_type": "bw_tracing", "quarantined": True}) == (False, "quarantined_record")
    assert is_allowed_record({"media_type": "bw_tracing"}) == (True, "ok")
    assert is_allowed_record({"media_type": "rubbing_frottage"}) == (True, "ok")
    assert is_allowed_record({"media_type": "svg_vector"}) == (True, "ok")
    assert is_allowed_record({"media_type": "rti_3d"}) == (True, "ok")

    # Full-color granite photo mislabeled as bw_tracing must be rejected
    rng = np.random.default_rng(7)
    h, w = 320, 420
    rock = np.zeros((h, w, 3), dtype=np.uint8)
    rock[..., 0] = rng.normal(135, 22, (h, w)).clip(20, 240).astype(np.uint8)
    rock[..., 1] = rng.normal(95, 20, (h, w)).clip(20, 240).astype(np.uint8)
    rock[..., 2] = rng.normal(70, 20, (h, w)).clip(20, 240).astype(np.uint8)
    p = tmp_path / "mislabeled.png"
    Image.fromarray(rock, mode="RGB").save(p)
    figs, info = trace_panel_scientific(p, media_type="bw_tracing")
    assert figs == []
    assert info["status"] == "rejected_extract"
    assert "mislabeled_color_photo" in info["reason"]


def test_scale_calibration_honesty_and_core_regions() -> None:
    # Uncalibrated plate must NEVER fabricate mm
    uncal = resolve_scale_calibration({"media_type": "bw_tracing", "width_px": 2400})
    assert uncal["scale_calibrated"] is False
    assert uncal["px_per_mm"] is None

    # Calibrated via explicit px_per_mm
    cal1 = resolve_scale_calibration({"media_type": "bw_tracing", "px_per_mm": 4.0, "scale_source": "1:1_scan_101.6dpi"})
    assert cal1["scale_calibrated"] is True
    assert cal1["px_per_mm"] == 4.0

    # Calibrated via scale bar measurements
    cal2 = resolve_scale_calibration({"media_type": "bw_tracing", "scale_bar_mm": 100.0, "scale_bar_px": 250.0})
    assert cal2["scale_calibrated"] is True
    assert cal2["px_per_mm"] == 2.5

    # Core regions
    assert classify_core_region({"site": "Hjemmeluft — Ole Pedersen 19", "region": "Finnmark"}) == "alta"
    assert classify_core_region({"site": "Begby", "region": "Østfold"}) == "ostfold"
    assert classify_core_region({"site": "Vitlycke", "region": "Västra Götaland — Tanum"}) == "bohuslan_tanum"
    assert classify_core_region({"site": "Nämforsen Bradön", "region": "Ångermanland"}) == "namforsen"
    assert classify_core_region({"site": "Vingen Vehammaren"}) == "vingen"
    assert classify_core_region({"site": "Bardal", "region": "Trøndelag"}) == "trondelag"
    assert classify_core_region({"site": "Madsebakke", "region": "Bornholm"}) == "other"


def test_multilayer_svg_layers_and_dual_trace_independence(tmp_path: Path) -> None:
    """Verify #figures (1 closed path), #cup_marks, #cracks, #uncertain (dashed), #scale_bar, and Method B."""
    h, w = 520, 680
    plate = np.full((h, w, 3), 252, dtype=np.uint8)

    # Main black figure (bear)
    gray_layer = np.full((h, w), 252, dtype=np.uint8)
    _draw_bear(gray_layer, 220, 160, val=18)
    plate[gray_layer < 100] = (18, 18, 22)

    # Isolated circular cup marks inside crop margin around bear
    for cx, cy in [(230, 195), (402, 200), (402, 305)]:
        cv2.circle(plate, (cx, cy), 7, (18, 18, 22), -1)

    # Faint weathered line near bear back (weak contrast ~ 18 gray delta for #uncertain)
    cv2.line(plate, (380, 215), (415, 228), (233, 233, 233), 3)

    # Red geological fissure annotation line passing near figure
    cv2.line(plate, (195, 325), (445, 345), (210, 30, 30), 3)

    raw_file = tmp_path / "test_plate.png"
    Image.fromarray(plate, mode="RGB").save(raw_file)

    traced, info = trace_panel_scientific(raw_file, media_type="bw_tracing")
    assert info["status"] == "ok" and len(traced) >= 1
    tf = traced[0]
    assert tf.metrics["hole_count"] == 0  # Solid bear must NOT be hollowed out!
    assert len(tf.cup_marks_px) >= 1
    assert len(tf.cracks_px) >= 1
    assert len(tf.uncertain_px) >= 1

    # Method B independent re-trace from raw crop
    geom_b, _, info_b = retrace_figure_method_b_otsu_marching_squares(
        raw_path=raw_file,
        crop_px=tf.crop_px,
        media_type="bw_tracing",
        polarity_hint=tf.polarity,
        ink_mode_hint=tf.ink_mode,
    )
    assert geom_b is not None and info_b["ok"] is True
    iou = tf.geometry.intersection(geom_b).area / tf.geometry.union(geom_b).area
    assert iou >= 0.85, f"Expected high Method A vs Method B IoU on clean bear, got {iou}"


def test_end_to_end_pipeline_science_and_disagreement_logging(tmp_path: Path) -> None:
    """Full pipeline test across Alta, Østfold, Bohuslän, Nämforsen, SVG, rubbing, RTI, + painted exclusion."""
    base = tmp_path / "helleristninger"
    src_dir = base / "sources" / "synthetic"
    raw_dir = src_dir / "raw"
    meta_dir = base / "metadata"
    raw_dir.mkdir(parents=True)
    meta_dir.mkdir(parents=True)

    # Gazetteer & literature
    gaz = [
        {
            "site_panel_id": "Askeladden:1001",
            "site_name": "Hjemmeluft — Bergbukten",
            "core_region": "alta",
            "lat": 69.9472,
            "lon": 23.1858,
            "coordinate_precision": "site_centroid",
            "coordinate_source": "Askeladden",
        },
        {
            "site_panel_id": "L1964:1234-1",
            "site_name": "Nämforsen Notön",
            "core_region": "namforsen",
            "lat": 63.4675,
            "lon": 16.8931,
            "coordinate_precision": "panel_centroid",
            "coordinate_source": "RAÄ FMIS",
        },
    ]
    (meta_dir / "site_gazetteer.json").write_text(json.dumps(gaz), encoding="utf-8")
    lit = [
        {
            "ref_id": "helskog_1988",
            "authors": "Helskog, Knut",
            "year": 1988,
            "title": "Helleristningene i Alta",
            "publication": "Alta Museum",
            "core_regions": ["alta"],
        },
        {
            "ref_id": "hallstrom_1960",
            "authors": "Hallström, Gustaf",
            "year": 1960,
            "title": "Monumental Art of Northern Sweden: Nämforsen",
            "publication": "Almqvist & Wiksell",
            "core_regions": ["namforsen"],
        },
    ]
    (meta_dir / "literature.json").write_text(json.dumps(lit), encoding="utf-8")

    # 1. Alta calibrated B&W tracing (bear)
    alta_img = np.full((340, 420), 250, dtype=np.uint8)
    _draw_bear(alta_img, 100, 80, val=15)
    p_alta = raw_dir / "alta_bear.png"
    Image.fromarray(alta_img, mode="L").save(p_alta)
    sha_alta = hashlib.sha256(p_alta.read_bytes()).hexdigest()

    # 2. Østfold uncalibrated rubbing (ship)
    ost_img = np.full((360, 460), 220, dtype=np.uint8)
    _draw_ship(ost_img, 80, 90, val=30)
    p_ost = raw_dir / "ostfold_ship.png"
    Image.fromarray(ost_img, mode="L").save(p_ost)
    sha_ost = hashlib.sha256(p_ost.read_bytes()).hexdigest()

    # 3. Bohuslän/Tanum SVG vector (ship)
    svg_txt = """<?xml version="1.0" encoding="UTF-8"?>
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 260" width="400" height="260">
      <rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>
      <path d="M 40,100 Q 200,200 360,100 L 340,118 Q 200,185 60,118 Z" fill="#111111"/>
      <path d="M 110,145 L 110,95 M 150,155 L 150,100 M 190,158 L 190,102 M 230,155 L 230,100" stroke="#111111" stroke-width="6" fill="none"/>
    </svg>"""
    p_tanum = raw_dir / "tanum_ship.svg"
    p_tanum.write_text(svg_txt, encoding="utf-8")
    sha_tanum = hashlib.sha256(p_tanum.read_bytes()).hexdigest()

    # 4. Nämforsen RTI shaded relief (elk/bear with intentional soft edge to trigger QA check)
    nam_img = np.full((340, 420), 185, dtype=np.uint8)
    _draw_bear(nam_img, 100, 80, val=120)
    nam_img = cv2.GaussianBlur(nam_img, (9, 9), 2.5)
    p_nam = raw_dir / "namforsen_rti.png"
    Image.fromarray(nam_img, mode="L").save(p_nam)
    sha_nam = hashlib.sha256(p_nam.read_bytes()).hexdigest()

    # 5. Painted photo (MUST be strictly excluded!)
    paint_img = np.full((300, 300, 3), 120, dtype=np.uint8)
    cv2.circle(paint_img, (150, 150), 60, (190, 40, 40), -1)
    p_paint = raw_dir / "painted_bear.jpg"
    Image.fromarray(paint_img, mode="RGB").save(p_paint)
    sha_paint = hashlib.sha256(p_paint.read_bytes()).hexdigest()

    manifest_rows = [
        {
            "source_id": "syn_alta_1", "source_archive": "altarockart", "title": "Bergbukten bear",
            "site": "Hjemmeluft — Bergbukten", "region": "Finnmark", "country": "NO", "period": "stone_age_hunter",
            "media_type": "bw_tracing", "raw_path": "sources/synthetic/raw/alta_bear.png",
            "sha256": sha_alta, "creator": "Alta Museum", "license": "CC BY-NC-SA 4.0", "license_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
            "px_per_mm": 2.0, "scale_source": "calibrated_1_to_1_scan",
        },
        {
            "source_id": "syn_ost_1", "source_archive": "unimus", "title": "Begby skip",
            "site": "Begby", "region": "Østfold", "country": "NO", "period": "bronze_age_agrarian",
            "media_type": "rubbing_frottage", "raw_path": "sources/synthetic/raw/ostfold_ship.png",
            "sha256": sha_ost, "creator": "KHM", "license": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
        },
        {
            "source_id": "syn_tanum_1", "source_archive": "wikimedia", "title": "Tanum skepp",
            "site": "Tanum", "region": "Bohuslän", "country": "SE", "period": "bronze_age_agrarian",
            "media_type": "svg_vector", "raw_path": "sources/synthetic/raw/tanum_ship.svg",
            "sha256": sha_tanum, "creator": "SHFA", "license": "CC0 1.0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        },
        {
            "source_id": "syn_nam_1", "source_archive": "shfa", "title": "Nämforsen Notön",
            "description": "SHFA RTI from Nämforsen. Motifs: Moose.",
            "site": "Nämforsen Notön", "region": "Ångermanland", "country": "SE", "period": "stone_age_hunter",
            "media_type": "rti_3d", "raw_path": "sources/synthetic/raw/namforsen_rti.png",
            "sha256": sha_nam, "creator": "SHFA", "license": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
        },
        {
            "source_id": "syn_paint_1", "source_archive": "wikimedia", "title": "Painted rock",
            "site": "Hell", "region": "Trøndelag", "country": "NO", "period": "stone_age_hunter",
            "media_type": "painted_photo", "raw_path": "sources/synthetic/raw/painted_bear.jpg",
            "sha256": sha_paint, "creator": "User", "license": "CC0 1.0", "license_url": "",
        },
    ]
    (src_dir / "manifest.jsonl").write_text("\n".join(json.dumps(r) for r in manifest_rows) + "\n", encoding="utf-8")

    summary = run_science_pipeline(
        base_dir=base,
        sources_dir=base / "sources",
        metadata_dir=meta_dir,
        record_dir=base / "scientific_record",
        qa_dir=base / "qa",
        taxonomy_path=Path(__file__).resolve().parent.parent.parent / "hellerisninger" / "taxonomy.json",
        qa_min_fraction=0.50,  # Force multiple dual-trace QA runs across regions
    )

    assert summary["total_figures"] == 4
    assert summary["calibrated_count"] == 1
    assert summary["uncalibrated_count"] == 3
    assert summary["schema_validation_errors"] == 0
    assert summary["ingest_summary"]["excluded_by_reason"].get("excluded_media_type:painted_photo") == 1

    # Verify figures.jsonl & schema.json & figures.csv
    assert (base / "scientific_record" / "schema.json").is_file()
    assert (base / "scientific_record" / "figures.csv").is_file()
    fig_lines = (base / "scientific_record" / "figures.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(fig_lines) == 4
    recs = [json.loads(l) for l in fig_lines]

    for r in recs:
        assert validate_figure_record(r) == []
        assert r["source_image"]["media_type"] != "painted_photo"
        svg_path = base / r["svg_path"]
        assert svg_path.is_file()
        root = ET.parse(svg_path).getroot()
        ns = "{http://www.w3.org/2000/svg}"
        groups = {g.attrib.get("id"): g for g in root.findall(f"{ns}g")}
        assert {"figures", "cup_marks", "cracks", "uncertain"}.issubset(groups.keys())
        fig_paths = groups["figures"].findall(f"{ns}path")
        assert len(fig_paths) == 1
        d_str = fig_paths[0].attrib["d"].strip()
        assert d_str.startswith("M") and d_str.endswith("Z")

        if r["size_mm"]["scale_calibrated"]:
            assert "scale_bar" in groups
            assert root.attrib["width"].endswith("mm")
            assert r["size_mm"]["width_mm"] > 0
        else:
            assert "scale_bar" not in groups
            assert root.attrib["width"].endswith("px")
            assert r["size_mm"]["width_mm"] is None

    # Check Alta gazetteer & literature match
    alta_rec = next(r for r in recs if r["core_region"] == "alta")
    assert alta_rec["site_panel_id"] == "Askeladden:1001"
    assert alta_rec["coordinates"]["crs"] == "EPSG:4326"
    assert len(alta_rec["literature"]) >= 1

    # Check Østfold unverified coordinates honestly flagged null
    ost_rec = next(r for r in recs if r["core_region"] == "ostfold")
    assert ost_rec["coordinates"] is None
    assert ost_rec["coordinates_provenance"]["status"] == "unverified"

    # Verify QA outputs
    qa_rep = json.loads((base / "qa" / "dual_trace_report.json").read_text(encoding="utf-8"))
    assert qa_rep["dual_traced_count"] >= 2
    assert qa_rep["meets_5pct_gate"] is True
    assert (base / "qa" / "dual_trace_report.md").is_file()
    assert (base / "qa" / "dual_trace_disagreements.jsonl").is_file()
    for qa_fig in qa_rep["figures"]:
        assert (base / qa_fig["overlay_path"]).is_file()

    # Check CSV row count matches JSONL
    with open(base / "scientific_record" / "figures.csv", "r", encoding="utf-8") as cf:
        rows = list(csv.DictReader(cf))
    assert len(rows) == 4


def test_rti_normal_map_and_stratified_sampling(tmp_path: Path) -> None:
    """Verify RGB RTI surface-normal extraction and >=5% stratified QA sampling across regions."""
    # Create synthetic RGB surface normal map (neutral flat normal = (128, 128, 255))
    h, w = 320, 400
    normal_map = np.zeros((h, w, 3), dtype=np.uint8)
    normal_map[..., 0] = 128
    normal_map[..., 1] = 128
    normal_map[..., 2] = 255
    # Carved groove bevels in normal map (tilted X/Y normals)
    bear = np.zeros((h, w), dtype=np.uint8)
    _draw_bear(bear, 100, 80, val=255)
    edges = cv2.Canny(bear, 50, 150)
    bevel = cv2.dilate(edges, np.ones((7, 7), np.uint8)) > 0
    normal_map[bevel, 0] = 220
    normal_map[bevel, 1] = 45
    normal_map[bevel, 2] = 190
    rti_path = tmp_path / "rti_normal.png"
    Image.fromarray(normal_map, mode="RGB").save(rti_path)

    traced, info = trace_panel_scientific(rti_path, media_type="rti_3d")
    assert info["status"] == "ok" and len(traced) >= 1

    # Stratified sampling across 5 regions with 100 records must pick >= 5% and cover every region
    regions = ["alta", "ostfold", "bohuslan_tanum", "namforsen", "other"]
    recs = [
        {"figure_id": f"hr_{i:04d}", "core_region": regions[i % len(regions)]}
        for i in range(100)
    ]
    sample = select_stratified_qa_sample(recs, min_fraction=0.05)
    assert len(sample) >= 5
    sampled_regions = {r["core_region"] for r in sample}
    assert sampled_regions == set(regions)

