"""Automated verification tests for Workstream 4 — Aphex Twin Jigsaw Editions.

Verifies:
1. Unit checks for strict-3mm-safe interlocking tabs and 100-piece typeface catalog loading.
2. Deliverable A: Flagship 600x400 mm >= 100-piece Aphex Twin Typeface + Oversized Logo Jigsaw
   (`runs/aphex-typeface-100/final-60x40`).
3. Deliverable B: Upgraded 300-piece Aphex Logo Field with frameless lock margin >= 3.0 mm
   (`runs/aphex-logo-field-300/locked-300`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from shapely import union_all
from shapely.geometry import Polygon, box
from shapely.ops import polygonize

ROOT = Path(__file__).resolve().parents[1]
PATHNET = ROOT / "pathnet"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(PATHNET) not in sys.path:
    sys.path.insert(0, str(PATHNET))

import audit_field as af  # noqa: E402
import field as fl  # noqa: E402
import field_large as fll  # noqa: E402
from heller_jigsaw.geometry.validate import validate_run_dir  # noqa: E402
from pathnet.aphex_typeface_board import LETTERS, load_typeface_shapes  # noqa: E402

FINAL_60X40 = ROOT / "runs" / "aphex-typeface-100" / "final-60x40"
LOCKED_300 = ROOT / "runs" / "aphex-logo-field-300" / "locked-300"


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_strict_tab_passes_3mm_and_locks():
    seam = fll._strict_tab_coords([(0.0, 0.0), (30.0, 0.0)], frac=0.5, sign=1, neck_w=4.6, head_w=8.6, depth=7.6, span=15.0)
    assert seam is not None
    top = Polygon(seam + [(30.0, 25.0), (0.0, 25.0)]).buffer(0)
    bot = Polygon([(0.0, -25.0), (30.0, -25.0)] + seam[::-1]).buffer(0)
    assert fll._strict_piece_ok(top, min_area=150.0, max_tip_mm=2.9, check_hj_tip=True)
    assert fll._strict_piece_ok(bot, min_area=150.0, max_tip_mm=2.9, check_hj_tip=True)
    _, blk, _ = fl.blocking([top, bot])
    assert float(blk[0].min()) >= 3.5


def test_load_typeface_shapes_100():
    records, unit_polys, unit_areas = load_typeface_shapes(pieces=100, logo_scale=2.9)
    assert len(records) == len(unit_polys) == len(unit_areas) == 100
    assert records[0]["glyph_kind"] == "logo"
    assert sum(r["glyph_kind"] == "logo" for r in records) == 1
    letters = {r["glyph_label"] for r in records if r["glyph_kind"] == "letter"}
    assert letters == set(LETTERS)
    assert sum(r["glyph_kind"] == "blend" for r in records) >= 32
    assert all(r["glyph_kind"] in {"logo", "letter", "blend"} for r in records)
    assert all(r["commercial_ok"] is False for r in records)


# ---------------------------------------------------------------------------
# Deliverable A: Flagship 60x40 cm Aphex Twin Typeface + Oversized Logo Jigsaw
# ---------------------------------------------------------------------------

def test_final_60x40_bundle_and_rights():
    required = [
        "cut.svg",
        "artwork.svg",
        "assembly.png",
        "assembly_labeled.png",
        "pieces_sheet.png",
        "partition.json",
        "config.json",
        "provenance.json",
        "quality.json",
        "production-audit.json",
        "thin-feature-audit.json",
        "finite-motion-audit.json",
        "rigid-motion-screen.json",
        "fidelity-audit.json",
        "fabrication-audit.json",
        "README.md",
    ]
    for name in required:
        p = FINAL_60X40 / name
        assert p.is_file() and p.stat().st_size > 0, f"Missing or empty deliverable: {p}"

    cfg = json.loads((FINAL_60X40 / "config.json").read_text(encoding="utf-8"))
    prov = json.loads((FINAL_60X40 / "provenance.json").read_text(encoding="utf-8"))
    assert cfg.get("commercial_ok") is False
    assert prov.get("commercial_ok") is False
    assert prov.get("all_commercial_ok") is False
    assert all(a.get("commercial_ok") is False for a in prov["assets"])


def test_final_60x40_svg_reimport_and_dimensions():
    lines, W, H = af.read_cut(FINAL_60X40 / "cut.svg")
    assert 580.0 <= W <= 620.0 and 380.0 <= H <= 420.0

    faces = list(polygonize(union_all(lines)))
    part = json.loads((FINAL_60X40 / "partition.json").read_text(encoding="utf-8"))
    n = int(part["num_pieces"])
    assert n >= 100
    assert len(faces) == n
    assert all(p.is_valid and len(p.interiors) == 0 for p in faces)

    cover = union_all(faces)
    board = box(0.0, 0.0, W, H)
    assert board.symmetric_difference(cover).area < 1e-4
    assert abs(sum(p.area for p in faces) - cover.area) < 1e-4

    segs = []
    for ln in lines:
        xy = list(ln.coords)
        segs.extend(tuple(sorted((a, b))) for a, b in zip(xy, xy[1:]))
    assert len(segs) == len(set(segs)), "Duplicate cut segments found in cut.svg"

    reimport = validate_run_dir(FINAL_60X40)
    assert reimport["all_hard_gates_passed"] is True
    q = json.loads((FINAL_60X40 / "quality.json").read_text(encoding="utf-8"))
    assert q["all_hard_gates_passed"] is True


def test_final_60x40_composition_logo_and_glyphs():
    part = json.loads((FINAL_60X40 / "partition.json").read_text(encoding="utf-8"))
    faces = part["faces"]
    assert len(faces) >= 100

    logos = [f for f in faces if f["metadata"].get("glyph_kind") == "logo"]
    letters = [f for f in faces if f["metadata"].get("glyph_kind") == "letter"]
    blends = [f for f in faces if f["metadata"].get("glyph_kind") == "blend"]
    assert len(logos) == 1
    assert len(logos) + len(letters) + len(blends) == len(faces), "Anonymous filler pieces found"

    present_letters = {f["metadata"]["glyph_label"] for f in letters}
    assert present_letters == set(LETTERS), f"Missing letters: {set(LETTERS) - present_letters}"
    assert len({f["metadata"]["glyph_label"] for f in blends}) >= 20

    logo_poly = Polygon(logos[0]["exterior"])
    glyph_areas = [Polygon(f["exterior"]).area for f in faces if f["metadata"].get("glyph_kind") != "logo"]
    ratio = logo_poly.area / max(glyph_areas)
    assert ratio >= 2.0, f"Centerpiece logo ratio {ratio:.2f}x below 2.0x largest glyph"

    # Centerpiece should sit near the middle of the 600x400 mm board
    x0, y0, x1, y1 = part["outline_bounds_mm"]
    cx, cy = logo_poly.centroid.x, logo_poly.centroid.y
    assert abs(cx - 0.5 * (x0 + x1)) < 45.0
    assert abs(cy - 0.5 * (y0 + y1)) < 45.0


def test_final_60x40_fabrication_and_interlock():
    strict = json.loads((FINAL_60X40 / "fabrication-audit.json").read_text(encoding="utf-8"))["summary"]
    assert strict["pieces_failing_any"] == 0
    assert strict["coverage_error_mm2"] == 0.0
    assert strict["duplicate_segments"] == 0
    assert strict["pieces_with_holes"] == 0
    assert all(v == 0 for v in strict["kerf_failures"].values())
    assert strict["neck_2_5mm_failures"] == 0
    assert strict["neck_3mm_failures"] == 0
    assert strict["tips_over_3mm"] == 0
    assert strict["worst_tip_reach_mm"] <= 3.0
    assert strict["assembly_lock_margin_mm"] >= 3.0
    assert strict["loose_groups_by_threshold_mm"]["3"]["directions"] == 0

    prod = json.loads((FINAL_60X40 / "production-audit.json").read_text(encoding="utf-8"))
    assert prod["geometry_pass"] is True
    assert prod["lock_margin_mm"] >= 3.0
    assert prod["unlocked_directions_at_3mm"] == 0
    n = prod["n"]
    for k in ("0.10", "0.15", "0.20", "0.25"):
        assert prod[f"kerf_{k}"]["kerf_pass"] == n
        assert prod[f"kerf_{k}"]["neck_pass"] == n

    thin = json.loads((FINAL_60X40 / "thin-feature-audit.json").read_text(encoding="utf-8"))
    assert thin["pieces_tip_loss_over_6mm"] == 0
    assert thin["worst_tip_loss_mm"] <= 3.0

    finite = json.loads((FINAL_60X40 / "finite-motion-audit.json").read_text(encoding="utf-8"))
    assert finite["all_proper_groups_blocked_within_3mm"] is True

    rigid = json.loads((FINAL_60X40 / "rigid-motion-screen.json").read_text(encoding="utf-8"))
    assert rigid["pieces_with_rotation_4deg_or_more"] == 0
    assert rigid["pieces_with_translation_3mm"] == 0

    fid = json.loads((FINAL_60X40 / "fidelity-audit.json").read_text(encoding="utf-8"))
    assert fid["pieces"] == n
    assert fid["summary"]["iou"]["mean"] >= 0.65


# ---------------------------------------------------------------------------
# Deliverable B: Upgraded 300-piece Aphex Logo Field (frameless lock >= 3.0 mm)
# ---------------------------------------------------------------------------

def test_locked_300_svg_fabrication_and_frameless_lock():
    svg = LOCKED_300 / "aphex-field-puzzle.svg"
    assert svg.is_file() and svg.stat().st_size > 0
    assert (LOCKED_300 / "assembly.png").is_file() and (LOCKED_300 / "assembly.png").stat().st_size > 0

    lines, W, H = af.read_cut(svg)
    assert abs(W - 570.0) < 1e-3 and abs(H - 390.0) < 1e-3
    faces = list(polygonize(union_all(lines)))
    assert len(faces) == 300
    assert all(p.is_valid and len(p.interiors) == 0 for p in faces)
    cover = union_all(faces)
    assert box(0.0, 0.0, W, H).symmetric_difference(cover).area < 1e-4

    segs = []
    for ln in lines:
        xy = list(ln.coords)
        segs.extend(tuple(sorted((a, b))) for a, b in zip(xy, xy[1:]))
    assert len(segs) == len(set(segs))

    audit = json.loads((LOCKED_300 / "fabrication-audit.json").read_text(encoding="utf-8"))["summary"]
    assert audit["pieces"] == 300
    assert audit["pieces_failing_any"] == 0
    assert audit["coverage_error_mm2"] == 0.0
    assert audit["duplicate_segments"] == 0
    assert audit["pieces_with_holes"] == 0
    assert all(v == 0 for v in audit["kerf_failures"].values())
    assert audit["neck_2_5mm_failures"] == 0
    assert audit["neck_3mm_failures"] == 0
    assert audit["tips_over_3mm"] == 0
    assert audit["worst_tip_reach_mm"] <= 3.0
    assert audit["assembly_lock_margin_mm"] >= 3.0
    assert audit["loose_groups_by_threshold_mm"]["1"]["directions"] == 0
    assert audit["loose_groups_by_threshold_mm"]["2"]["directions"] == 0
    assert audit["loose_groups_by_threshold_mm"]["3"]["directions"] == 0

    rep = json.loads((LOCKED_300 / "field-report.json").read_text(encoding="utf-8"))
    assert rep.get("commercial_ok") is False
    assert rep["interlock"]["assembly_lock_margin_mm"] >= 3.0
