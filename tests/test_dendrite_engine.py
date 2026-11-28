"""Comprehensive pytest suite for Workstream 5 (`dendrite_engine` + Formverk Studio).

Run from repository root:
    .venv\\Scripts\\python.exe -m pytest -q tests/test_dendrite_engine.py
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from dendrite_engine.cli import generate_puzzle_run
from dendrite_engine.neural_guide import (
    DEFAULT_CHECKPOINT,
    DendriteSurrogateNet,
    auto_tune_parameters,
    compute_artwork_style_fields,
)
from dendrite_engine.phase_field import (
    DendriteConfig,
    build_outline_polygon,
    distance2_graph_coloring,
    rasterize_polygon,
    run_dendrite_simulation,
)
from dendrite_engine.postprocess import audit_cut_svg
from dendrite_engine.rod_growth import RodConfig, run_rod_growth
from dendrite_engine.server import create_server
from dendrite_engine.whimsies import (
    build_whimsy_geometry,
    get_builtin_whimsy,
    list_builtin_whimsies,
    load_pathnet_whimsies,
    load_vae_whimsies,
)
from heller_jigsaw.geometry.validate import analyze_piece_fabrication


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_distance2_graph_coloring_has_no_d1_or_d2_conflicts():
    # 5x5 planar grid graph: distance-2 coloring must separate both neighbors and neighbors-of-neighbors
    adj = {i: set() for i in range(25)}
    for r in range(5):
        for c in range(5):
            u = r * 5 + c
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                nr, nc = r + dr, c + dc
                if 0 <= nr < 5 and 0 <= nc < 5:
                    adj[u].add(nr * 5 + nc)
    colors, num_colors = distance2_graph_coloring(adj, distance=2)
    assert num_colors >= 5
    for u, nbrs in adj.items():
        for v in nbrs:
            assert colors[u] != colors[v]
            for w in adj[v]:
                if w != u:
                    assert colors[u] != colors[w]


def test_phase_field_conservation_and_symmetry():
    outline = build_outline_polygon("rect", 120.0, 120.0)
    # Build perfectly symmetric 2-piece initial label grid (left half vs right half + symmetric ripple)
    h = w = 120
    yy, xx = np.mgrid[0:h, 0:w]
    mid = 60.0 + 4.0 * np.sin(2.0 * np.pi * yy / 40.0)
    init_lab = np.where(xx < mid, 0, 1).astype(np.int32)

    cfg = DendriteConfig(
        style="dendrite",
        width_mm=120.0,
        height_mm=120.0,
        num_pieces=2,
        px_mm=1.0,
        U1_mm=0.0,
        U2_mm=0.0,
        steps=120,
        seed=7,
    )
    res = run_dendrite_simulation(outline, cfg, initial_labels=init_lab)
    assert res.conservation_error_max < 1e-4
    # Sum of phase fields inside active domain equals 1.0 everywhere
    p_sum = res.phase_fields.sum(axis=0)
    assert np.allclose(p_sum[res.domain_mask], 1.0, atol=1e-4)
    # Symmetric mutual growth: both phases retain balanced ~50% area (within 2%)
    a0 = float((res.labels == 0).sum())
    a1 = float((res.labels == 1).sum())
    assert abs(a0 - a1) / (a0 + a1) < 0.02


def test_whimsy_reflective_boundary_invariance_and_ws2_api():
    outline = build_outline_polygon("rect", 180.0, 140.0)
    wh = [
        build_whimsy_geometry(get_builtin_whimsy("trilobite"), (60.0, 70.0), 54.0),
        build_whimsy_geometry(get_builtin_whimsy("bacteriophage"), (125.0, 70.0), 54.0),
    ]
    cfg = DendriteConfig.from_style("dendrite", width_mm=180.0, height_mm=140.0, num_pieces=14, px_mm=0.75, steps=160, seed=11)
    # Also exercise Workstream 2 custom fixed_masks + spatial growth_mask API
    h_px = int(round(140.0 / 0.75))
    w_px = int(round(180.0 / 0.75))
    growth_mask = np.linspace(0.4, 1.0, w_px, dtype=np.float32)[None, :].repeat(h_px, axis=0)
    res = run_dendrite_simulation(outline, cfg, whimsies=wh, growth_mask=growth_mask)
    assert len(res.fixed_piece_ids) == 2
    assert res.conservation_error_max < 1e-4

    # Check exact pixel invariance of both fixed whimsies
    for pid, w_obj in zip(res.fixed_piece_ids, wh):
        expected_mask = rasterize_polygon(w_obj.polygon, res.bounds_mm, res.labels.shape) & res.domain_mask
        actual_mask = res.labels == pid
        iou = float((expected_mask & actual_mask).sum()) / float((expected_mask | actual_mask).sum())
        assert iou >= 0.999


def test_builtin_whimsies_fabrication_and_neural_adapters():
    names = list_builtin_whimsies()
    assert {"trilobite", "bacteriophage", "diatom", "neuron"}.issubset(set(names))
    for name in names:
        wp = build_whimsy_geometry(get_builtin_whimsy(name), (100.0, 100.0), 60.0)
        assert wp.polygon.is_valid and len(wp.polygon.interiors) == 0
        fab = analyze_piece_fabrication(wp.polygon, kerf_mm=0.15, min_neck_mm=3.0)
        assert fab["kerf_pass"], f"Whimsy {name} failed kerf check"
        assert fab["neck_pass"], f"Whimsy {name} failed 3mm neck check: {fab}"

    pathnet_ckpt = REPO_ROOT / "pathnet" / "pathnet.pt"
    if pathnet_ckpt.is_file():
        pn_specs = load_pathnet_whimsies(pathnet_ckpt, count=1, seed=22)
        assert len(pn_specs) == 1 and pn_specs[0].polygon.is_valid

    vae_ckpt = REPO_ROOT / "runs" / "v10-campaign" / "cc0-96-seed101" / "animal-shape-vae.pt"
    if vae_ckpt.is_file():
        vae_specs = load_vae_whimsies(vae_ckpt, count=1, seed=42)
        assert len(vae_specs) == 1 and vae_specs[0].polygon.is_valid


@pytest.mark.parametrize("style,borderless", [("maze", True), ("wave", False)])
def test_rod_growth_non_crossing_and_audit(tmp_path: Path, style: str, borderless: bool):
    res = generate_puzzle_run(
        out_dir=tmp_path / style,
        style=style,
        outline_kind="rect",
        width_mm=200.0,
        height_mm=160.0,
        num_pieces=14,
        seed=42,
        borderless=borderless,
        overrides={"growth_steps": 45},
    )
    audit = res.audit
    assert audit["pieces"] == 14
    assert audit["duplicate_segments"] == 0
    assert audit["overlap_mm2"] < 1e-2
    assert audit["invalid_pieces"] == 0
    assert audit["pieces_with_holes"] == 0
    assert all(v == 0 for v in audit["kerf_failures"].values())
    assert audit["heller_neck_failures"] == 0
    assert audit["interlock"]["assembly_lock_margin_mm"] > 0.0
    assert audit["all_hard_gates_passed"] is True
    assert audit["metadata"]["min_nonadjacent_clearance_mm"] >= 2.8
    assert audit["metadata"]["clearance_stage"] == "snapped_rod_centerlines"
    assert audit["metadata"]["last_collision_sweep_min_mm"] is not None


def test_dendrite_circle_svg_layers_and_audit_gates(tmp_path: Path):
    outline = build_outline_polygon("circle", 210.0, 210.0)
    wh = [build_whimsy_geometry(get_builtin_whimsy("bacteriophage"), (105.0, 105.0), 56.0)]
    res = generate_puzzle_run(
        out_dir=tmp_path / "circle",
        style="dendrite",
        outline_kind="circle",
        width_mm=210.0,
        height_mm=210.0,
        num_pieces=18,
        seed=42,
        whimsies=wh,
        engrave_text="FORMVERK",
        px_mm=0.60,
        overrides={"steps": 240},
    )
    audit = res.audit
    assert audit["duplicate_segments"] == 0
    assert audit["coverage_error_mm2"] < 0.5
    assert audit["overlap_mm2"] < 1e-2
    assert all(v == 0 for v in audit["kerf_failures"].values())
    assert audit["heller_neck_failures"] == 0
    assert audit["interlock"]["assembly_lock_margin_mm"] > 0.0
    assert audit["all_hard_gates_passed"] is True

    # Verify cut.svg explicitly separates interior_cut, exterior_cut, and engrave groups
    cut_svg = Path(res.files["cut_svg"])
    root = ET.parse(cut_svg).getroot()
    groups = {g.attrib.get("id"): list(g) for g in root.findall(".//{*}g")}
    assert "interior_cut" in groups and len(groups["interior_cut"]) > 0
    assert "exterior_cut" in groups and len(groups["exterior_cut"]) > 0
    assert "engrave" in groups and len(groups["engrave"]) > 0

    # Independent re-audit matches saved audit
    re_audit = audit_cut_svg(cut_svg, expected_outline=outline, min_neck_mm=3.0)
    assert re_audit["pieces"] == audit["pieces"]
    assert re_audit["duplicate_segments"] == 0
    assert re_audit["all_hard_gates_passed"] is True


def test_neural_guide_inference_and_artwork_saliency():
    # Verify trained checkpoint if present, or fallback
    ckpt = REPO_ROOT / DEFAULT_CHECKPOINT
    tuned = auto_tune_parameters(260.0, 190.0, 32, style="dendrite", checkpoint=ckpt, seed=42)
    rec = tuned["recommended"]
    assert 20.0 <= rec["gamma"] <= 50.0
    assert 150 <= rec["steps"] <= 600
    assert rec["lambda1_mm"] > rec["lambda2_mm"] > 0.0
    if ckpt.is_file():
        assert tuned["used_neural_guide"] is True
        assert tuned["predicted"]["neck_pass_rate"] > 0.5

    # Artwork saliency style field
    img = np.zeros((64, 80, 3), dtype=np.uint8)
    img[16:48, 20:60] = 255
    g_map, a_map = compute_artwork_style_fields(img, (64, 80), base_gamma=35.0)
    assert g_map.shape == (64, 80) and a_map.shape == (64, 80)
    assert np.isfinite(g_map).all() and np.isfinite(a_map).all()
    assert g_map.max() > g_map.min()


def test_formverk_server_api_smoke():
    srv = create_server("127.0.0.1", 0)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        # 1. Static index.html
        with urllib.request.urlopen(f"{base}/") as r:
            assert r.status == 200
            assert "Formverk" in r.read().decode("utf-8")

        # 2. Health, whimsies, presets
        health = json.loads(urllib.request.urlopen(f"{base}/api/health").read().decode("utf-8"))
        assert health["status"] == "ok"
        assert "dendrite" in health["styles"] and "maze" in health["styles"]

        wh = json.loads(urllib.request.urlopen(f"{base}/api/whimsies").read().decode("utf-8"))
        assert len(wh["builtin"]) >= 5

        presets = json.loads(urllib.request.urlopen(f"{base}/api/presets").read().decode("utf-8"))
        assert "showcases" in presets

        # 3. Neural auto-tune endpoint
        req = urllib.request.Request(
            f"{base}/api/autotune",
            data=json.dumps({"width_mm": 240, "height_mm": 180, "pieces": 24, "style": "dendrite"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        tuned = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
        assert "recommended" in tuned and "gamma" in tuned["recommended"]

        # 4. Path traversal protection
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/runs/../../README.md")
        assert exc.value.code == 404
    finally:
        srv.shutdown()
        srv.server_close()
