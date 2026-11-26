"""Comprehensive verification suite for the 750-Piece Dendrite + Trilobites + Helleristninger Jigsaw."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pytest
from shapely.geometry import Polygon

from heller_jigsaw.geometry.outline import organic_stone
from heller_jigsaw.geometry.partition import extract_partition_from_labels
from heller_jigsaw.geometry.validate import analyze_piece_fabrication
from heller_jigsaw.production.audit import lock_check as dense_lock_check
from heller_jigsaw.production.audit import measure as measure_tip
from heller_jigsaw.trilobite_dendrite.audit_and_lock import (
    clean_partition_micro_segments,
    sparse_lock_check,
)
from heller_jigsaw.trilobite_dendrite.catalog import curate_750_motifs
from heller_jigsaw.trilobite_dendrite.dendrite_seams import (
    DendriteConfig,
    _compute_distance2_coloring,
    evolve_dendritic_seams_gpu,
)
from heller_jigsaw.trilobite_dendrite.export_and_render import verify_exported_svgs
from heller_jigsaw.trilobite_dendrite.trilobites import (
    TRILOBITE_GENERA,
    TRILOBITE_POSES,
    clip_and_verify_engraving,
    generate_trilobite,
    verify_engraving_clearance,
)

PROD_DIR = ROOT / "runs" / "dendrite-trilobite-750"


def test_all_trilobite_genera_and_poses_fabrication_and_engraving_clearance():
    """Every genus & pose produces a kerf/neck/tip-safe cut polygon and >= 1.50 mm inset engraving."""
    for r, genus in enumerate(TRILOBITE_GENERA):
        for c, pose in enumerate(TRILOBITE_POSES):
            spec = generate_trilobite(genus=genus, pose=pose, target_area_mm2=760.0, seed=500 + r * 10 + c)
            assert spec.polygon.is_valid
            assert spec.polygon.geom_type == "Polygon"
            assert len(spec.polygon.interiors) == 0
            assert 650.0 <= spec.polygon.area <= 900.0

            for k in (0.10, 0.15, 0.20, 0.25):
                fab = analyze_piece_fabrication(spec.polygon, kerf_mm=k, min_neck_mm=3.0)
                assert fab["kerf_pass"], f"{genus}/{pose} failed kerf {k}"
                assert fab["neck_pass"], f"{genus}/{pose} failed neck at kerf {k}"

            tip = measure_tip(spec.polygon, kerf=0.25, r=1.5)["max_tip_loss_mm"]
            assert tip <= 5.5, f"{genus}/{pose} tip loss {tip:.2f} mm > 5.5 mm"

            ver = verify_engraving_clearance(spec.engrave_paths, spec.polygon, min_clearance_mm=1.50)
            assert ver["passed"], f"{genus}/{pose} engraving clearance failed: {ver}"
            assert ver["num_strokes"] >= 10
            assert ver["min_clearance_mm"] >= 1.50


def test_clip_and_verify_engraving_enforces_strict_clearance():
    """Clipping arbitrarily close/crossing strokes guarantees strict >= 1.50 mm boundary clearance."""
    spec = generate_trilobite("Paradoxides", "straight", target_area_mm2=780.0, seed=7)
    x0, y0, x1, y1 = spec.polygon.bounds
    # Inject adversarial strokes running straight through the boundary
    adversarial = list(spec.engrave_paths) + [
        [(x0 - 5.0, 0.0), (x1 + 5.0, 0.0)],
        [(0.0, y0 - 5.0), (0.0, y1 + 5.0)],
    ]
    clipped, min_clr = clip_and_verify_engraving(adversarial, spec.polygon, min_clearance_mm=1.50)
    ver = verify_engraving_clearance(clipped, spec.polygon, min_clearance_mm=1.50)
    assert ver["passed"]
    assert ver["violations"] == 0
    assert ver["outside_strokes"] == 0
    assert min_clr >= 1.50


def test_catalog_curation_commercial_clean_zero_filler():
    """Curated mix contains zero Alta NC-SA, 100% commercial_ok, and zero anonymous filler cells."""
    motifs, rights = curate_750_motifs(
        catalog_path=ROOT / "hellerisninger" / "processed" / "catalog.json",
        n_total=40,
        n_trilobites=10,
        seed=42,
        base_area_mm2=720.0,
        allow_share_alike=False,
    )
    assert len(motifs) == 40
    assert rights["trilobite_count"] == 10
    assert rights["helleristning_count"] == 30
    assert rights["anonymous_filler_count"] == 0
    assert rights["alta_nc_sa_count"] == 0
    assert rights["commercial_ok_count"] == 40
    assert rights["share_alike_count"] == 0
    assert all(m.kind in ("trilobite", "helleristning") for m in motifs)
    assert all(m.polygon_local.is_valid and m.polygon_local.area > 400.0 for m in motifs)


def test_sparse_lock_check_exact_parity_with_dense_audit():
    """Sparse O(E) 360-degree lock check must match dense `heller_jigsaw.production.audit.lock_check` identically."""
    outline = organic_stone(width_mm=160.0, height_mm=130.0, seed=11)
    px = 0.5
    x0, y0, x1, y1 = outline.bounds
    gw, gh = int(math.ceil((x1 - x0) / px)), int(math.ceil((y1 - y0) / px))
    yy, xx = np.meshgrid(np.arange(gh) * px + y0 + 0.5 * px, np.arange(gw) * px + x0 + 0.5 * px, indexing="ij")
    import shapely
    inside = np.asarray(shapely.contains_xy(outline, xx, yy), dtype=bool)

    # Create 12 undulating Voronoi cells inside outline
    rng = np.random.default_rng(19)
    pts = np.column_stack([
        rng.uniform(x0 + 20, x1 - 20, size=12),
        rng.uniform(y0 + 20, y1 - 20, size=12),
    ])
    d2 = (xx[:, :, None] - pts[None, None, :, 0]) ** 2 + (yy[:, :, None] - pts[None, None, :, 1]) ** 2
    labels = np.argmin(d2, axis=2).astype(np.int32)
    labels[~inside] = -1

    part = extract_partition_from_labels(labels, outline, px=px, n_pieces=12, smooth_iters=2)
    part = clean_partition_micro_segments(part, min_seg_mm=0.06)
    edges_dict = [e.to_dict() for e in part.edges]

    dense_res = dense_lock_check(edges_dict, part.num_pieces)
    sparse_res = sparse_lock_check(edges_dict, part.num_pieces)
    assert sparse_res == dense_res


def test_dendritic_solver_distance2_coloring_and_corridor_evolution():
    """GPU/CPU corridor dendritic solver maintains distance-2 coloring and preserves all N connected pieces."""
    outline = organic_stone(width_mm=150.0, height_mm=120.0, seed=5)
    px = 0.5
    x0, y0, x1, y1 = outline.bounds
    gw, gh = int(math.ceil((x1 - x0) / px)), int(math.ceil((y1 - y0) / px))
    yy, xx = np.meshgrid(np.arange(gh) * px + y0 + 0.5 * px, np.arange(gw) * px + x0 + 0.5 * px, indexing="ij")
    import shapely
    inside = np.asarray(shapely.contains_xy(outline, xx, yy), dtype=bool)

    pts = np.array([
        [35, 35], [75, 30], [115, 35],
        [32, 62], [75, 60], [118, 62],
        [38, 90], [75, 92], [112, 88],
    ], dtype=np.float64)
    dists = np.hypot(xx[:, :, None] - pts[None, None, :, 0], yy[:, :, None] - pts[None, None, :, 1])
    order = np.argsort(dists, axis=2)
    labels = order[:, :, 0].astype(np.int32)
    v1 = np.take_along_axis(dists, order[:, :, 0:1], axis=2)[:, :, 0]
    v2 = np.take_along_axis(dists, order[:, :, 1:2], axis=2)[:, :, 0]
    phi_own = (v1 - 12.0).astype(np.float32)
    dist_sec = (v2 - v1).astype(np.float32)
    labels[~inside] = -1

    colors, adj = _compute_distance2_coloring(labels, len(pts))
    for u, nbrs in adj.items():
        for v in nbrs:
            assert colors[u] != colors[v]
            for w in adj[v]:
                if w != u:
                    assert colors[u] != colors[w]

    cfg = DendriteConfig(steps=40, seed=99)
    evolved, metrics = evolve_dendritic_seams_gpu(
        labels_init=labels,
        inside=inside,
        phi_own=phi_own,
        dist_second=dist_sec,
        piece_kinds=["trilobite" if i % 3 == 0 else "helleristning" for i in range(len(pts))],
        px=px,
        cfg=cfg,
        log=lambda _: None,
    )
    assert set(np.unique(evolved[inside]).tolist()) == set(range(len(pts)))
    assert metrics["corridor_reassigned_fraction"] > 0.005


@pytest.mark.skipif(not (PROD_DIR / "production-audit.json").exists(), reason="Final 750-piece bundle not built yet")
def test_final_750_piece_production_bundle_passes_all_hard_gates():
    """Verify every hard user requirement on the final exported 750-piece bundle."""
    audit = json.loads((PROD_DIR / "production-audit.json").read_text(encoding="utf-8"))
    rights = json.loads((PROD_DIR / "rights_summary.json").read_text(encoding="utf-8"))
    part = json.loads((PROD_DIR / "partition.json").read_text(encoding="utf-8"))

    # 1. Exact 750 pieces, zero anonymous filler, zero Alta NC-SA
    assert part["num_pieces"] == 750
    assert audit["n"] == 750
    assert audit["trilobite_count"] >= 100
    assert audit["helleristning_count"] == 750 - audit["trilobite_count"]
    assert audit["anonymous_filler_count"] == 0
    assert audit["alta_nc_sa_count"] == 0
    assert rights["total_pieces"] == 750
    assert rights["commercial_ok_count"] == 750
    assert rights["alta_nc_sa_count"] == 0
    assert len(rights["pieces"]) == 750

    # 2. All production hard gates green
    assert audit["all_production_gates_passed"] is True, f"Failed gates: {audit['gates']}"
    assert audit["coverage_sym_diff_mm2"] <= 1e-2
    assert audit["max_pairwise_overlap_mm2"] <= 1e-3
    assert audit["min_segment_mm"] >= 0.05

    # 3. Kerf (0.10-0.25 mm), neck (>= 3.0 mm), thin tips (0 > 6 mm), 360-deg lock (>= 3.0 mm)
    for k in ("kerf_0.10", "kerf_0.15", "kerf_0.20", "kerf_0.25"):
        assert audit["kerf_sweep"][k]["kerf_pass"] == 750
        assert audit["kerf_sweep"][k]["neck_pass"] == 750
    assert audit["thin_feature"]["pieces_tip_loss_over_6mm"] == 0
    assert audit["lock_360"]["lock_margin_mm"] >= 3.0
    assert audit["lock_360"]["unlocked_directions_at_3mm"] == 0
    assert audit["finite_motion_translation"]["all_proper_groups_blocked_within_3mm"] is True

    # 4. Engraving clearance >= 1.50 mm on every trilobite
    assert audit["engraving_clearance"]["all_passed"] is True
    assert audit["engraving_clearance"]["global_min_clearance_mm"] >= 1.50

    # 5. Live independent SVG re-import check on cut.svg, engrave.svg, production.svg
    svg_check = verify_exported_svgs(
        PROD_DIR,
        expected_n=750,
        expected_trilobites=audit["trilobite_count"],
        min_segment_mm=0.05,
        min_engrave_clearance_mm=1.50,
    )
    assert svg_check["all_svg_gates_passed"] is True, f"SVG reimport gates failed: {svg_check['gates']}"
    assert svg_check["reimported_face_count"] == 750
    assert svg_check["duplicate_cut_pairs"] == 0
    assert svg_check["engrave_reimport_violations"] == 0
    assert svg_check["engrave_reimport_min_clearance_mm"] >= 1.50
