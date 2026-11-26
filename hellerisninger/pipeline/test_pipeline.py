"""Comprehensive synthetic unit tests for the Nordic helleristninger pipeline."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import cv2
import numpy as np
import pytest
from PIL import Image
from shapely.geometry import Polygon

from .dedup_split import assign_grouped_splits, deduplicate_motifs
from .image_extract import extract_bw_tracing, extract_foreground_mask, extract_painted_photo
from .run_pipeline import run_batch
from .segment import segment_motifs
from .silhouette import (
    EROSION_RADIUS_NORM,
    MASK_SIZE_PX,
    VIEWBOX_SIZE_MM,
    convert_to_jigsaw_silhouette,
)
from .svg_ingest import svg_to_foreground_mask
from .taxonomy import build_class_index, classify_motif, load_taxonomy, validate_commercial_ok


def _make_solid_bear_mask(size: int = 240) -> np.ndarray:
    """Create a chunky solid_pecked bear/quadruped silhouette mask."""
    img = np.zeros((size, size), dtype=np.uint8)
    # Torso
    cv2.ellipse(img, (125, 115), (62, 38), 0, 0, 360, 255, -1)
    # Head & snout
    cv2.ellipse(img, (55, 102), (28, 22), -10, 0, 360, 255, -1)
    cv2.circle(img, (68, 82), 11, 255, -1)  # ear
    # Thick legs
    cv2.rectangle(img, (78, 132), (104, 185), 255, -1)
    cv2.rectangle(img, (145, 132), (172, 185), 255, -1)
    # Tail nub
    cv2.circle(img, (186, 108), 10, 255, -1)
    return img


def _make_outline_whale_mask(size: int = 260, gap: bool = True) -> np.ndarray:
    """Create a Stone Age contour/outline whale carving with optional small outline break."""
    img = np.zeros((size, size), dtype=np.uint8)
    pts = np.array(
        [
            [35, 130], [60, 90], [115, 78], [175, 92], [210, 112],
            [238, 85], [225, 128], [240, 165], [208, 142], [165, 162],
            [105, 170], [55, 158],
        ],
        dtype=np.int32,
    )
    cv2.polylines(img, [pts], isClosed=True, color=255, thickness=7)
    if gap:
        # Cut a small 6px break in the belly outline
        cv2.rectangle(img, (120, 160), (126, 176), 0, -1)
    return img


def _make_linear_bronze_ship_mask(size: int = 320, detached_crew: bool = False) -> np.ndarray:
    """Create a linear Bronze Age ship with upturned prows and crew strokes."""
    img = np.zeros((size, size), dtype=np.uint8)
    # Lower keel arc & upper gunwale arc
    keel = np.array([[40, 120], [65, 180], [160, 192], [255, 180], [280, 120]], dtype=np.int32)
    gunwale = np.array([[55, 135], [80, 168], [160, 176], [240, 168], [265, 135]], dtype=np.int32)
    cv2.polylines(img, [keel], False, 255, thickness=4)
    cv2.polylines(img, [gunwale], False, 255, thickness=4)
    # Connect stem & stern
    cv2.line(img, (40, 120), (55, 135), 255, 4)
    cv2.line(img, (280, 120), (265, 135), 255, 4)
    # Crew strokes sticking up from gunwale
    y_bot = 165 if detached_crew else 173
    for x in range(92, 236, 16):
        cv2.line(img, (x, y_bot), (x, 140), 255, 3)
    return img


def test_taxonomy_and_license_validation() -> None:
    tax = load_taxonomy()
    idx = build_class_index(tax)
    assert set(tax["families"].keys()) == {"animal", "vessel", "human", "symbol", "scene_or_unclassified"}
    for req in ("reindeer", "elk_moose", "bear", "whale_cetacean", "bronze_age_ship", "skier", "sun_wheel", "footprint"):
        assert req in idx

    # Honest commercial license enforcement
    assert validate_commercial_ok("CC0 1.0") is True
    assert validate_commercial_ok("Public domain") is True
    assert validate_commercial_ok("CC-BY-SA-4.0") is True
    assert validate_commercial_ok("NLOD 2.0") is True
    assert validate_commercial_ok("CC-BY-NC-4.0", manifest_flag=True) is False
    assert validate_commercial_ok("CC BY-NC-SA 3.0", manifest_flag=True) is False
    assert validate_commercial_ok("Unknown", manifest_flag=True) is False
    assert validate_commercial_ok("", manifest_flag=True) is False
    assert validate_commercial_ok("CC-BY-4.0", manifest_flag=False) is False

    # Multilingual classification
    fam, cls, conf = classify_motif({"title": "Bølareinen kalkering", "period": "stone_age_hunter"}, idx)
    assert (fam, cls) == ("animal", "reindeer") and conf >= 0.6

    fam, cls, _ = classify_motif({"title": "Rødøymannen skiløper Nordland", "period": "stone_age_hunter"}, idx)
    assert (fam, cls) == ("human", "skier")

    fam, cls, _ = classify_motif({"title": "Hällristning skepp Tanum", "period": "bronze_age_agrarian"}, idx)
    assert (fam, cls) == ("vessel", "bronze_age_ship")

    # Regression tests: zero false substring hits!
    # 1. 'shield_bearer' / 'spear_bearer' / 'axe_bearer' MUST NOT match 'bear'
    fam, cls, _ = classify_motif(
        {
            "source_archive": "shfa",
            "title": "Långeland Askum / Askum 75:1",
            "description": "SHFA Tracing from Långeland Askum. Motifs: Ship or Boat, Shield bearer, Adorant.",
            "motif_hints": ["bronze_age_ship", "shield_bearer", "adorant"],
            "period": "bronze_age_agrarian",
        },
        idx,
        num_extracted_from_source=5,
        shape_metrics={"aspect_ratio": 1.2, "solidity": 0.55, "compactness": 0.25},
    )
    assert cls != "bear"

    # 2. 'Skien' and 'skipsfigurer' MUST NOT match 'skier'
    fam, cls, _ = classify_motif(
        {
            "source_archive": "unimus",
            "site": "Mælagata 36b, Skien",
            "title": "Helleristninger. Kalkering Mælagata 36b, Skien, Telemark",
            "description": "Helleristninger. Kalkering Mælagata 36b, Skien, Telemark",
            "motif_hints": ["skier"],
            "period": "bronze_age_agrarian",
        },
        idx,
        num_extracted_from_source=4,
    )
    assert cls == "unclassified_motif"

    fam, cls, _ = classify_motif(
        {
            "source_archive": "unimus",
            "site": "Evje, Fredrikstad",
            "title": "Kalkering av helleristninger: skipsfigurer",
            "description": "Kalkering av helleristninger: skipsfigurer",
            "motif_hints": ["ship", "skier"],
            "period": "bronze_age_agrarian",
        },
        idx,
        num_extracted_from_source=4,
    )
    assert (fam, cls) == ("vessel", "bronze_age_ship")

    # 3. 'Oppland'/'Nordland'/'Nordre Land' MUST NOT match 'bird', and 'topografisk' MUST NOT match 'fish'
    fam, cls, _ = classify_motif(
        {
            "source_archive": "unimus",
            "site": "Nørstebøen, Nordre Land",
            "title": "Helleristning, tegnet i blekk, orginal",
            "description": "Fra topografisk arkiv. Oppland, Nordre Land.",
            "motif_hints": ["fish", "bird"],
            "period": "stone_age_hunter",
        },
        idx,
        num_extracted_from_source=1,
    )
    assert cls == "unclassified_motif"

    # 4. 'Amtmannsnes' MUST NOT trigger 'anthropomorph', and generic Alta boilerplate MUST NOT force 'stone_age_boat'
    fam, cls, _ = classify_motif(
        {
            "source_archive": "altarockart",
            "site": "Amtmannsnes — Amtmannsnes 1B",
            "title": "Kalkering Amtmannsnes 1B",
            "description": "Digital rock-art tracing (kalkering) from Alta Rock Art Archive.",
            "motif_hints": ["human"],
            "period": "stone_age_hunter",
        },
        idx,
        num_extracted_from_source=5,
        shape_metrics={"aspect_ratio": 1.80, "solidity": 0.76, "compactness": 0.43},
    )
    assert cls == "unclassified_motif"

    # 5. 'Bjørnstadskipet' MUST match 'bronze_age_ship', NEVER 'bear'
    fam, cls, _ = classify_motif(
        {
            "source_archive": "wikimedia",
            "site": "Bjørnstad",
            "title": "Bjørnstadskipet 1",
            "description": "Helleristningen Bjørnstadskipet i Skjeberg.",
            "motif_hints": ["bronze_age_ship"],
            "period": "bronze_age_agrarian",
        },
        idx,
        num_extracted_from_source=1,
    )
    assert (fam, cls) == ("vessel", "bronze_age_ship")

    # 6. Authoritative SHFA single-class 'Ship or Boat' + 'Cupmarks' recovers bronze_age_ship cleanly
    fam, cls, _ = classify_motif(
        {
            "source_archive": "shfa",
            "site": "Hemsta Boglösa / Boglösa 128:1",
            "title": "Hemsta Boglösa / Boglösa 128:1 — Tracing (Plastic)",
            "description": "SHFA Tracing from Hemsta Boglösa. Motifs: Ship or Boat, Cupmarks.",
            "motif_hints": ["bronze_age_ship"],
            "period": "stone_age_hunter",
        },
        idx,
        num_extracted_from_source=5,
    )
    assert (fam, cls) == ("vessel", "bronze_age_ship")


def test_pruner_artifact_detectors() -> None:
    from .prune_corpus import _is_cupmark_cluster, _is_printed_T_or_m, _is_straight_north_arrow

    # Cupmark 2-dot dumbbell
    db = np.zeros((256, 256), dtype=np.uint8)
    cv2.circle(db, (85, 128), 38, 255, -1)
    cv2.circle(db, (171, 128), 38, 255, -1)
    cv2.rectangle(db, (105, 118), (151, 138), 255, -1)
    assert _is_cupmark_cluster(db) is True

    # Printed lowercase 'm'
    m_img = np.zeros((256, 256), dtype=np.uint8)
    cv2.rectangle(m_img, (48, 60), (68, 200), 255, -1)
    cv2.rectangle(m_img, (118, 60), (138, 200), 255, -1)
    cv2.rectangle(m_img, (188, 60), (208, 200), 255, -1)
    cv2.rectangle(m_img, (48, 56), (208, 78), 255, -1)
    assert _is_printed_T_or_m(m_img, neck_mm=5.0) is True

    # Straight-shafted north arrow
    arr = np.zeros((256, 256), dtype=np.uint8)
    cv2.rectangle(arr, (122, 85), (134, 225), 255, -1)
    tri = np.array([[128, 30], [92, 92], [164, 92]], dtype=np.int32)
    cv2.fillPoly(arr, [tri], 255)
    assert _is_straight_north_arrow(arr) is True

    # Real solid bear must NOT trigger any junk detector
    bear = _make_solid_bear_mask(256)
    assert _is_cupmark_cluster(bear) is False
    assert _is_printed_T_or_m(bear, neck_mm=15.0) is False
    assert _is_straight_north_arrow(bear) is False


def test_three_morphologies_and_sdf_consistency() -> None:
    # (a) solid_pecked
    bear = _make_solid_bear_mask()
    proc_bear, diag_bear = convert_to_jigsaw_silhouette(bear)
    assert proc_bear is not None, diag_bear
    assert proc_bear.render_style == "solid_pecked"
    assert proc_bear.jigsaw_tier == "A_ready"
    assert proc_bear.was_thickened is False
    assert proc_bear.metrics["connected_components"] == 1
    assert proc_bear.metrics["hole_count"] == 0
    assert proc_bear.metrics["passes_kerf_gate"] is True

    # (b) outline_enclosed (with small break in belly line)
    whale = _make_outline_whale_mask(gap=True)
    proc_whale, diag_whale = convert_to_jigsaw_silhouette(whale)
    assert proc_whale is not None, diag_whale
    assert proc_whale.render_style == "outline_enclosed"
    assert proc_whale.jigsaw_tier in ("A_ready", "B_thickened")
    assert proc_whale.metrics["hole_count"] == 0
    assert proc_whale.metrics["passes_kerf_gate"] is True
    assert proc_whale.metrics["area_ratio"] > 0.35  # Confirm interior was actually filled!

    # (c) linear_motif (thin-stroke Bronze Age ship)
    ship = _make_linear_bronze_ship_mask(detached_crew=False)
    proc_ship, diag_ship = convert_to_jigsaw_silhouette(ship)
    assert proc_ship is not None, diag_ship
    assert proc_ship.render_style == "linear_motif"
    assert proc_ship.jigsaw_tier == "B_thickened"
    assert proc_ship.was_thickened is True
    assert proc_ship.metrics["connected_components"] == 1
    assert proc_ship.metrics["hole_count"] == 0
    assert proc_ship.metrics["passes_kerf_gate"] is True

    # Independent Shapely & SDF consistency check across all three
    for proc in (proc_bear, proc_whale, proc_ship):
        poly = proc.polygon
        assert poly.is_valid and poly.is_simple and len(poly.interiors) == 0
        minx, miny, maxx, maxy = poly.bounds
        assert 0.0 <= minx < maxx <= VIEWBOX_SIZE_MM
        assert 0.0 <= miny < maxy <= VIEWBOX_SIZE_MM

        # Independent laser kerf/neck erosion survival check
        r_erode = EROSION_RADIUS_NORM * max(maxx - minx, maxy - miny)
        eroded = poly.buffer(-r_erode)
        assert (not eroded.is_empty) and eroded.geom_type == "Polygon"

        # Mask & SDF sign + boundary gradient check
        mask = proc.mask_256
        sdf = proc.sdf_256
        assert mask.shape == (MASK_SIZE_PX, MASK_SIZE_PX)
        assert sdf.shape == (MASK_SIZE_PX, MASK_SIZE_PX)
        assert sdf.dtype == np.float32
        assert np.all(sdf[mask == 255] < 0.0)
        assert np.all(sdf[mask == 0] > 0.0)
        gy, gx = np.gradient(sdf)
        grad_mag = np.hypot(gy, gx)
        near_boundary = np.abs(sdf) <= 2.5
        med_grad = float(np.median(grad_mag[near_boundary]))
        assert 0.75 <= med_grad <= 1.25


def test_panel_cleaning_and_multi_motif_segmentation() -> None:
    """Test removing border frame, scale bar, caption text, and cupmarks while isolating 3 motifs."""
    h, w = 900, 1200
    canvas = np.zeros((h, w), dtype=np.uint8)

    # 1. Outer rectangular border frame
    cv2.rectangle(canvas, (10, 10), (w - 11, h - 11), 255, thickness=4)

    # 2. Bottom scale bar
    cv2.rectangle(canvas, (860, 825), (1120, 840), 255, -1)

    # 3. Bottom caption text line
    cv2.putText(canvas, "Fig 14 Alta Bergbukten 1m", (60, 845), cv2.FONT_HERSHEY_SIMPLEX, 0.85, 255, 2)

    # 4. Eight tiny featureless cupmark dots scattered across panel
    for cx, cy in [(420, 120), (460, 145), (500, 115), (760, 130), (805, 150), (840, 125), (600, 680), (645, 700)]:
        cv2.circle(canvas, (cx, cy), 8, 255, -1)

    # 5. Three real rock carvings: solid bear, outline whale, linear ship with detached crew strokes
    bear = _make_solid_bear_mask(210)
    whale = _make_outline_whale_mask(220, gap=True)
    ship = _make_linear_bronze_ship_mask(280, detached_crew=True)

    canvas[120 : 120 + 210, 90 : 90 + 210] = np.maximum(canvas[120 : 120 + 210, 90 : 90 + 210], bear)
    canvas[130 : 130 + 220, 850 : 850 + 220] = np.maximum(canvas[130 : 130 + 220, 850 : 850 + 220], whale)
    canvas[440 : 440 + 280, 260 : 260 + 280] = np.maximum(canvas[440 : 440 + 280, 260 : 260 + 280], ship)

    cands, tele = segment_motifs(canvas, media_type="bw_tracing")
    assert tele["dropped_border_frames"] >= 1
    assert tele["dropped_scale_bars"] >= 1
    assert tele["dropped_text_glyphs"] >= 5
    # Exactly the 3 real motifs should be isolated (crew strokes grouped with ship, cupmarks dropped!)
    assert len(cands) == 3, f"Expected 3 motifs, got {len(cands)}; telemetry={tele}"


def test_svg_ingestion_and_cleaning(tmp_path: Path) -> None:
    svg_content = """<?xml version="1.0" encoding="UTF-8"?>
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 260" width="400" height="260">
      <rect x="0" y="0" width="100%" height="100%" fill="#ffffff"/>
      <text x="20" y="245" font-size="18" fill="#000000">Solberg Ship (Østfold)</text>
      <path d="M 40,100 Q 200,200 360,100 L 340,118 Q 200,185 60,118 Z" fill="#111111"/>
      <path d="M 110,145 L 110,95 M 145,155 L 145,100 M 180,158 L 180,102 M 215,158 L 215,102 M 250,155 L 250,100 M 285,145 L 285,95"
            stroke="#111111" stroke-width="6" fill="none"/>
    </svg>
    """
    svg_file = tmp_path / "ship.svg"
    svg_file.write_text(svg_content, encoding="utf-8")

    mask, info = svg_to_foreground_mask(svg_file)
    assert mask is not None and info["ok"] is True
    assert info["removed_text"] >= 1
    assert info["removed_bg_rects"] >= 1

    cands, _ = segment_motifs(mask, media_type="svg_vector")
    assert len(cands) == 1
    proc, _ = convert_to_jigsaw_silhouette(cands[0].mask)
    assert proc is not None
    assert proc.jigsaw_tier in ("A_ready", "B_thickened")
    assert proc.metrics["passes_kerf_gate"] is True


def test_painted_photo_extraction_and_noisy_rock_rejection() -> None:
    rng = np.random.default_rng(42)

    # 1. Synthetic gray granite rock with red-ochre painted bear
    h, w = 420, 520
    rock_gray = rng.normal(125, 16, size=(h, w, 1)).clip(40, 210)
    rock_rgb = np.repeat(rock_gray, 3, axis=2)
    # Slight realistic stone tint noise
    rock_rgb[..., 0] = np.clip(rock_rgb[..., 0] + rng.normal(2, 4, (h, w)), 0, 255)
    rock_rgb[..., 2] = np.clip(rock_rgb[..., 2] + rng.normal(-2, 4, (h, w)), 0, 255)

    bear = _make_solid_bear_mask(220)
    painted = rock_rgb.copy()
    sub = painted[100:320, 150:370]
    fg = bear > 0
    sub[fg, 0] = 178  # iron oxide red
    sub[fg, 1] = 48
    sub[fg, 2] = 42
    painted[100:320, 150:370] = sub
    rgba_painted = np.dstack([painted.astype(np.uint8), np.full((h, w), 255, dtype=np.uint8)])

    mask_p, info_p = extract_painted_photo(rgba_painted)
    assert mask_p is not None and info_p["ok"] is True
    cands, _ = segment_motifs(mask_p, media_type="painted_photo")
    assert len(cands) == 1

    # 2. Unpainted gray granite rock photo -> MUST BE REJECTED
    rgba_unpainted = np.dstack([rock_rgb.astype(np.uint8), np.full((h, w), 255, dtype=np.uint8)])
    mask_u, info_u = extract_painted_photo(rgba_unpainted)
    assert mask_u is None and info_u["ok"] is False

    # 3. Uniformly reddish sandstone rock (no distinct carving paint) -> MUST BE REJECTED
    red_rock = np.zeros((h, w, 4), dtype=np.uint8)
    red_rock[..., 0] = rng.normal(175, 12, (h, w)).clip(0, 255).astype(np.uint8)
    red_rock[..., 1] = rng.normal(85, 10, (h, w)).clip(0, 255).astype(np.uint8)
    red_rock[..., 2] = rng.normal(72, 10, (h, w)).clip(0, 255).astype(np.uint8)
    red_rock[..., 3] = 255
    mask_r, info_r = extract_painted_photo(red_rock)
    assert mask_r is None and info_r["ok"] is False


def test_end_to_end_batch_dedup_splits_and_exports(tmp_path: Path) -> None:
    """Full end-to-end run_batch test verifying dedup, leak-free splits, contact sheets, and Figma sheets."""
    base = tmp_path / "hellerisninger"
    src_dir = base / "sources" / "synthetic_test"
    raw_dir = src_dir / "raw"
    raw_dir.mkdir(parents=True)

    # Create 5 distinct raw files + 1 mirrored duplicate of bear to test deduplication
    bear = _make_solid_bear_mask(240)
    whale = _make_outline_whale_mask(260, gap=True)
    ship = _make_linear_bronze_ship_mask(300, detached_crew=False)

    # White-bg black-ink tracings
    def _save_tracing(name: str, fg_mask: np.ndarray) -> tuple[str, str]:
        pad = np.full((fg_mask.shape[0] + 60, fg_mask.shape[1] + 60), 255, dtype=np.uint8)
        pad[30 : 30 + fg_mask.shape[0], 30 : 30 + fg_mask.shape[1]][fg_mask > 0] = 15
        p = raw_dir / name
        Image.fromarray(pad, mode="L").save(p)
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        return f"sources/synthetic_test/raw/{name}", sha

    files_meta = []
    # File 1: Bear (Norway, Alta, CC0)
    rp, sha = _save_tracing("alta_bear.png", bear)
    files_meta.append({
        "source_id": "syn_bear_1", "source_archive": "synthetic_test", "title": "Alta bear rock carving",
        "description": "Stone age bear", "site": "Alta", "region": "Finnmark", "country": "NO",
        "period": "stone_age_hunter", "media_type": "bw_tracing", "motif_hints": ["bear"],
        "raw_path": rp, "source_url": "https://example.org/bear1", "download_url": "https://example.org/bear1.png",
        "creator": "Test", "license": "CC0 1.0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "commercial_ok": True, "sha256": sha, "width_px": 300, "height_px": 300,
    })

    # File 2: Mirrored duplicate of exact same Bear (CC-BY-NC -> should lose to CC0 in dedup!)
    rp2, sha2 = _save_tracing("alta_bear_mirror.png", np.fliplr(bear))
    files_meta.append({
        "source_id": "syn_bear_dup", "source_archive": "synthetic_test", "title": "Alta bear mirror copy",
        "description": "Duplicate bear", "site": "Alta", "region": "Finnmark", "country": "NO",
        "period": "stone_age_hunter", "media_type": "bw_tracing", "motif_hints": ["bear"],
        "raw_path": rp2, "source_url": "https://example.org/bear2", "download_url": "https://example.org/bear2.png",
        "creator": "Test", "license": "CC-BY-NC-4.0", "license_url": "",
        "commercial_ok": True, "sha256": sha2, "width_px": 300, "height_px": 300,
    })

    # File 3: Whale (Norway, Leiknes, CC-BY)
    rp3, sha3 = _save_tracing("leiknes_whale.png", whale)
    files_meta.append({
        "source_id": "syn_whale_1", "source_archive": "synthetic_test", "title": "Leiknes whale contour",
        "description": "Stone age whale", "site": "Leiknes", "region": "Nordland", "country": "NO",
        "period": "stone_age_hunter", "media_type": "bw_tracing", "motif_hints": ["whale_cetacean"],
        "raw_path": rp3, "source_url": "https://example.org/whale1", "download_url": "https://example.org/whale1.png",
        "creator": "Test", "license": "CC-BY-4.0", "license_url": "",
        "commercial_ok": True, "sha256": sha3, "width_px": 320, "height_px": 320,
    })

    # File 4: Ship (Sweden, Tanum, CC-BY-NC -> verify commercial_ok forced False!)
    rp4, sha4 = _save_tracing("tanum_ship.png", ship)
    files_meta.append({
        "source_id": "syn_ship_1", "source_archive": "synthetic_test", "title": "Tanum bronze age ship",
        "description": "Ship with crew", "site": "Tanum", "region": "Bohuslän", "country": "SE",
        "period": "bronze_age_agrarian", "media_type": "bw_tracing", "motif_hints": ["bronze_age_ship"],
        "raw_path": rp4, "source_url": "https://example.org/ship1", "download_url": "https://example.org/ship1.png",
        "creator": "Test", "license": "CC-BY-NC-4.0", "license_url": "",
        "commercial_ok": True, "sha256": sha4, "width_px": 360, "height_px": 360,
    })

    # File 5: Multi-motif panel (2 different figures) to verify sibling split grouping
    panel = np.zeros((350, 720), dtype=np.uint8)
    # Left: chunky footprint with sole, heel, and 5 toes (like Tegneby)
    cv2.ellipse(panel, (140, 150), (34, 58), -5, 0, 360, 255, -1)
    cv2.ellipse(panel, (146, 224), (24, 40), 4, 0, 360, 255, -1)
    for tx, ty, tr in [(112, 86, 11), (128, 79, 10), (144, 80, 9), (158, 86, 8), (170, 96, 8)]:
        cv2.circle(panel, (tx, ty), tr, 255, -1)
    # Right: iconic skier silhouette (head, torso, angled arms, bent legs, curved ski)
    cv2.circle(panel, (520, 62), 20, 255, -1)
    cv2.line(panel, (520, 82), (512, 165), 255, 24)
    cv2.line(panel, (516, 110), (578, 92), 255, 16)   # raised arm holding pole
    cv2.line(panel, (516, 110), (465, 130), 255, 16)  # back arm
    cv2.line(panel, (512, 162), (485, 220), 255, 20)  # back leg
    cv2.line(panel, (512, 162), (545, 220), 255, 20)  # front leg
    ski_pts = np.array([[420, 228], [520, 226], [610, 215], [630, 195]], dtype=np.int32)
    cv2.polylines(panel, [ski_pts], False, 255, 18)
    rp5, sha5 = _save_tracing("begby_panel.png", panel)
    files_meta.append({
        "source_id": "syn_panel_1", "source_archive": "synthetic_test", "title": "Begby panel footprint and skier",
        "description": "Two motifs", "site": "Begby", "region": "Østfold", "country": "NO",
        "period": "bronze_age_agrarian", "media_type": "bw_tracing", "motif_hints": ["footprint", "skier"],
        "raw_path": rp5, "source_url": "https://example.org/panel1", "download_url": "https://example.org/panel1.png",
        "creator": "Test", "license": "CC0", "license_url": "",
        "commercial_ok": True, "sha256": sha5, "width_px": 780, "height_px": 410,
    })

    mf_path = src_dir / "manifest.jsonl"
    mf_path.write_text("\n".join(json.dumps(r) for r in files_meta) + "\n", encoding="utf-8")

    stats = run_batch(
        base_dir=base,
        sources_dir=base / "sources",
        output_dir=base / "processed",
        taxonomy_path=Path(__file__).resolve().parent.parent / "taxonomy.json",
    )

    # Duplicate mirrored bear must be collapsed! 5 unique motifs remain (bear, whale, ship, footprint, skier)
    assert stats["dedup_dropped_count"] == 1
    assert stats["total_motifs"] == 5

    catalog = json.loads((base / "processed" / "catalog.json").read_text(encoding="utf-8"))
    assert len(catalog) == 5

    # Verify NC ship had commercial_ok forced False
    ship_rec = next(r for r in catalog if r["source_id"] == "syn_ship_1")
    assert ship_rec["commercial_ok"] is False

    # Verify sibling motifs from syn_panel_1 are in the EXACT same split
    panel_recs = [r for r in catalog if r["source_id"] == "syn_panel_1"]
    assert len(panel_recs) == 2
    assert panel_recs[0]["split"] == panel_recs[1]["split"]
    assert panel_recs[0]["source_group"] == panel_recs[1]["source_group"]

    # Verify zero source_group / sha256 overlap across splits
    splits = {}
    for r in catalog:
        splits.setdefault(r["split"], set()).add(r["source_group"])
    s_list = list(splits.values())
    for i in range(len(s_list)):
        for j in range(i + 1, len(s_list)):
            assert s_list[i].isdisjoint(s_list[j])

    # Verify all per-motif files exist and Figma SVGs are well-formed XML
    for r in catalog:
        assert (base / r["svg_path"]).is_file()
        assert (base / r["mask_path"]).is_file()
        assert (base / r["polygon_path"]).is_file()
        assert (base / r["sdf_path"]).is_file()
        ET.parse(base / r["svg_path"])

    for sheet_rel in stats["figma_sheets"]:
        p = base / sheet_rel
        assert p.is_file()
        ET.parse(p)

    assert len(stats["contact_sheets"]) >= 1
    for cs_rel in stats["contact_sheets"]:
        assert (base / cs_rel).is_file()
