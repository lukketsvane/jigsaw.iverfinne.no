"""Unit, property, negative-gate, gradient, and end-to-end tests for `heller_jigsaw`."""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import torch
from shapely.geometry import Polygon

from heller_jigsaw.cli import main as cli_main
from heller_jigsaw.data.corpus import make_synthetic_nordic_motifs, select_diverse_motifs
from heller_jigsaw.geometry.export import export_puzzle_run, write_outline_svg
from heller_jigsaw.geometry.outline import (
    circle,
    from_svg,
    grid_shape,
    inside_mask,
    organic_stone,
    rectangle,
)
from heller_jigsaw.geometry.partition import (
    CanonicalEdge,
    FaceRecord,
    PlanarPartition,
    extract_partition_from_labels,
    partition_from_polygons,
)
from heller_jigsaw.geometry.validate import (
    ValidationConfig,
    analyze_piece_fabrication,
    validate_partition,
    validate_run_dir,
)
from heller_jigsaw.models.recognizer import (
    HellerSilhouetteEncoder,
    ShapeDescriptorBaseline,
    extract_shape_descriptor,
    mask_to_model_input,
)
from heller_jigsaw.models.train_recognizer import _verify_leak_free_splits
from heller_jigsaw.solver.geodesic import (
    GeodesicSolverConfig,
    GridGraph,
    PiecePose,
    _aligned_iou_and_chamfer,
    geodesic_distances,
    solve_geodesic_puzzle,
    warp_motif_to_grid,
)


# ---------------------------------------------------------------------------
# 1. Outline & SVG round-trip
# ---------------------------------------------------------------------------

def test_outlines_and_svg_roundtrip(tmp_path: Path):
    rect = rectangle(240.0, 180.0)
    assert rect.is_valid and abs(rect.area - 240.0 * 180.0) < 1e-2

    circ = circle(250.0)
    expected_area = math.pi * (125.0 ** 2)
    assert circ.is_valid and abs(circ.area - expected_area) / expected_area < 5e-3

    stone = organic_stone(250.0, 210.0, seed=42)
    assert stone.is_valid and len(stone.interiors) == 0

    svg_file = tmp_path / "outline.svg"
    write_outline_svg(stone, svg_file)
    reloaded = from_svg(svg_file)
    rel_diff = stone.symmetric_difference(reloaded).area / stone.area
    assert rel_diff < 1e-5


# ---------------------------------------------------------------------------
# 2. Planar subdivision & label-grid extraction (with disconnected island repair)
# ---------------------------------------------------------------------------

def test_extract_partition_repairs_disconnected_island_and_checkerboard():
    outline = circle(200.0)
    px = 2.0
    h, w = grid_shape(outline, px)
    inside = inside_mask(outline, h, w, px)
    yy, xx = np.meshgrid(np.arange(h), np.arange(w), indexing="ij")
    cy, cx = h * 0.5, w * 0.5
    ang = np.arctan2(yy - cy, xx - cx)
    # 4 wavy quadrants
    wave = 0.25 * np.sin(np.hypot(xx - cx, yy - cy) * 0.16)
    labels = np.floor(((ang + wave + math.pi) / (2.0 * math.pi)) * 4.0).astype(np.int32) % 4
    labels[~inside] = -1

    # Inject a disconnected 2x2 island of label 0 inside label 2's quadrant
    q2_ys, q2_xs = np.where((labels == 2) & (np.hypot(xx - cx, yy - cy) > 20))
    if len(q2_ys) > 10:
        r0, c0 = int(q2_ys[len(q2_ys) // 2]), int(q2_xs[len(q2_xs) // 2])
        labels[r0 : r0 + 2, c0 : c0 + 2] = 0

    part = extract_partition_from_labels(labels, outline, px=px, n_pieces=4)
    assert part.num_pieces == 4

    report = validate_partition(part, expected_n=4)
    assert report["gates"]["piece_count_exact"]
    assert report["gates"]["all_faces_valid"]
    assert report["gates"]["no_interior_holes"]
    assert report["gates"]["coverage_exact"]
    assert report["gates"]["no_overlaps"]
    assert report["gates"]["planar_edge_incidence"]
    assert report["summary"]["euler_V_minus_E_plus_F"] == 1
    assert report["summary"]["coverage_rel_error"] < 1e-5


# ---------------------------------------------------------------------------
# 3. Negative geometry & fabrication gate tests (must FAIL on defects!)
# ---------------------------------------------------------------------------

def test_validator_catches_overlap_gap_and_hole():
    outline = rectangle(100.0, 100.0)
    # Overlapping pair
    p1 = Polygon([(0, 0), (60, 0), (60, 100), (0, 100)])
    p2 = Polygon([(40, 0), (100, 0), (100, 100), (40, 100)])
    bad_part = PlanarPartition(
        outline=outline,
        vertices=[(0, 0), (100, 100)],
        edges=[CanonicalEdge(0, 0, 1, 0, 1, [(50, 0), (50, 100)])],
        faces=[FaceRecord(0, [], p1), FaceRecord(1, [], p2)],
        adjacency={0: [1], 1: [0]},
    )
    rep = validate_partition(bad_part, expected_n=2)
    assert not rep["gates"]["no_overlaps"]
    assert not rep["all_hard_gates_passed"]

    # Gap (missing middle strip)
    g1 = Polygon([(0, 0), (45, 0), (45, 100), (0, 100)])
    g2 = Polygon([(55, 0), (100, 0), (100, 100), (55, 100)])
    gap_part = PlanarPartition(
        outline=outline,
        vertices=[(0, 0)],
        edges=[],
        faces=[FaceRecord(0, [], g1), FaceRecord(1, [], g2)],
        adjacency={0: [], 1: []},
    )
    rep_gap = validate_partition(gap_part, expected_n=2)
    assert not rep_gap["gates"]["coverage_exact"]
    assert not rep_gap["all_hard_gates_passed"]

    # Polygon with an internal hole
    holed = Polygon(
        [(0, 0), (100, 0), (100, 100), (0, 100)],
        holes=[[(35, 35), (65, 35), (65, 65), (35, 65)]],
    )
    with pytest.raises(ValueError, match="interior hole"):
        partition_from_polygons([holed], outline, auto_node=False)

    holed_part = PlanarPartition(
        outline=outline,
        vertices=[(0, 0)],
        edges=[],
        faces=[FaceRecord(0, [], holed)],
        adjacency={0: []},
    )
    rep_hole = validate_partition(holed_part, expected_n=1)
    assert not rep_hole["gates"]["no_interior_holes"]


def test_kerf_and_conservative_neck_erosion_catches_thin_bridge_but_allows_tapered_tip():
    # Dumbbell: two 30x30 mm lobes joined by a fragile 1.4 mm neck (< 3.0 + 0.15 mm!)
    dumbbell = Polygon([
        (0, 0), (30, 0), (30, 14.3), (42, 14.3), (42, 0), (72, 0),
        (72, 30), (42, 30), (42, 15.7), (30, 15.7), (30, 30), (0, 30),
    ])
    fab_bad = analyze_piece_fabrication(dumbbell, kerf_mm=0.15, min_neck_mm=3.0)
    assert fab_bad["kerf_pass"] is True          # survives 0.075 mm kerf
    assert fab_bad["neck_pass"] is False         # FAILS 3.0 + 0.15 mm load-bearing neck check!
    assert fab_bad["core_load_bearing_components"] == 2

    # Sturdy tapered triangle (sharp tip tapers smoothly; eroded core stays 1 connected body)
    tapered_wedge = Polygon([(0, 0), (50, 0), (25, 45)])
    fab_ok = analyze_piece_fabrication(tapered_wedge, kerf_mm=0.15, min_neck_mm=3.0)
    assert fab_ok["kerf_pass"] is True
    assert fab_ok["neck_pass"] is True
    assert fab_ok["core_load_bearing_components"] == 1


def test_validator_catches_duplicate_cut_edge_and_text_in_cut_svg(tmp_path: Path):
    outline = rectangle(120.0, 120.0)
    # Two clean curved-seam faces
    seam = [(60.0, 0.0), (68.0, 35.0), (52.0, 85.0), (60.0, 120.0)]
    p0 = Polygon([(0.0, 0.0)] + seam + [(0.0, 120.0)])
    p1 = Polygon([(120.0, 0.0), (120.0, 120.0)] + list(reversed(seam)))
    part = partition_from_polygons([p0, p1], outline, auto_node=True)
    export_puzzle_run(part, tmp_path, {"kerf_mm": 0.15, "min_neck_mm": 3.0}, {})

    clean_rep = validate_run_dir(tmp_path)
    assert clean_rep["gates"]["cut_svg_unique_edges_no_duplicates"]
    assert clean_rep["gates"]["cut_svg_no_labels_or_fills"]
    assert clean_rep["gates"]["cut_svg_reimport_exact_n"]
    assert clean_rep["gates"]["cut_svg_reimport_coverage"]

    cut_svg = tmp_path / "cut.svg"
    original_cut = cut_svg.read_bytes()
    tree = ET.parse(str(cut_svg))
    first = tree.getroot().find(".//{http://www.w3.org/2000/svg}path")
    assert first is not None
    import re
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", first.attrib["d"])]
    x0, y0, x1, y1 = nums[:4]
    length = np.hypot(x1-x0, y1-y0)
    assert length > .01
    k = .005 / length
    first.attrib["d"] = f"M {x0:.4f} {y0:.4f} L {x0+k*(x1-x0):.4f} {y0+k*(y1-y0):.4f} " + first.attrib["d"][first.attrib["d"].find("L"):]
    tree.write(str(cut_svg), encoding="utf-8", xml_declaration=True)
    assert not validate_run_dir(tmp_path)["gates"]["no_slivers_or_tiny_segments"]
    cut_svg.write_bytes(original_cut)

    # Now inject a duplicate interior cut edge AND a stray <text> label into cut.svg
    cut_svg = tmp_path / "cut.svg"
    tree = ET.parse(str(cut_svg))
    root = tree.getroot()
    ns = "{http://www.w3.org/2000/svg}"
    first_path = root.find(f".//{ns}path")
    assert first_path is not None
    dup = ET.Element(f"{ns}path", dict(first_path.attrib))
    dup.attrib["id"] = "duplicate-bad-cut"
    root.append(dup)
    txt = ET.Element(f"{ns}text", {"x": "10", "y": "10"})
    txt.text = "ILLEGAL LABEL"
    root.append(txt)
    tree.write(str(cut_svg), encoding="utf-8", xml_declaration=True)

    bad_rep = validate_run_dir(tmp_path)
    assert not bad_rep["gates"]["cut_svg_unique_edges_no_duplicates"]
    assert not bad_rep["gates"]["cut_svg_no_labels_or_fills"]
    assert not bad_rep["all_hard_gates_passed"]


# ---------------------------------------------------------------------------
# 4. Exact adjoint gradient check for shared-metric Dijkstra geodesic solver
# ---------------------------------------------------------------------------

def test_geodesic_dijkstra_adjoint_matches_finite_differences():
    mask = np.ones((12, 12), dtype=bool)
    graph = GridGraph(mask)
    seeds = [graph.coord_to_node[2, 2], graph.coord_to_node[9, 9]]

    rng = np.random.default_rng(7)
    s0 = torch.tensor(rng.uniform(0.5, 2.5, size=graph.num_nodes), dtype=torch.float64, requires_grad=True)
    weights = torch.tensor(rng.normal(size=(2, graph.num_nodes)), dtype=torch.float64)

    T = geodesic_distances(s0, graph, seeds)
    loss = (T * weights).sum()
    loss.backward()
    grad_analytic = s0.grad.detach().numpy()

    eps = 1e-6
    # Spot-check 15 nodes across the grid against central finite differences
    check_nodes = np.linspace(0, graph.num_nodes - 1, 15, dtype=int)
    for idx in check_nodes:
        s_plus = s0.detach().clone()
        s_plus[idx] += eps
        s_minus = s0.detach().clone()
        s_minus[idx] -= eps
        l_plus = float((geodesic_distances(s_plus, graph, seeds) * weights).sum().item())
        l_minus = float((geodesic_distances(s_minus, graph, seeds) * weights).sum().item())
        fd = (l_plus - l_minus) / (2.0 * eps)
        assert abs(grad_analytic[idx] - fd) < 1e-3 * max(1.0, abs(fd)), (
            f"Adjoint mismatch at node {idx}: analytic={grad_analytic[idx]:.6f}, fd={fd:.6f}"
        )


# ---------------------------------------------------------------------------
# 5. Model A recognizer & leak-free split guard
# ---------------------------------------------------------------------------

def test_model_a_encoder_descriptor_and_split_leak_guard():
    motifs = make_synthetic_nordic_motifs(6)
    inp = np.stack([mask_to_model_input(m.mask, m.sdf, size=128) for m in motifs], axis=0)
    model = HellerSilhouetteEncoder(num_families=4, num_classes=6)
    model.eval()
    with torch.no_grad():
        out = model(torch.from_numpy(inp))
    assert out["embedding"].shape == (6, 128)
    norms = torch.linalg.norm(out["embedding"], dim=1)
    assert torch.allclose(norms, torch.ones_like(norms), atol=1e-4)
    assert out["family_logits"].shape == (6, 4)
    assert out["class_logits"].shape == (6, 6)

    # Descriptor retrieval self-match under 90-degree rotation
    base = ShapeDescriptorBaseline().fit(
        [m.mask for m in motifs],
        [m.motif_class for m in motifs],
        [m.family for m in motifs],
        [m.asset_id for m in motifs],
    )
    rot_reindeer = np.rot90(motifs[0].mask, k=1)
    top1 = base.query(rot_reindeer, top_k=1)[0]
    assert top1["motif_class"] == motifs[0].motif_class

    # Split leakage detector MUST reject overlapping source_group or sha256
    leaky_sg = [
        {"split": "train", "source_group": "g1", "sha256": "a"},
        {"split": "val", "source_group": "g1", "sha256": "b"},
    ]
    with pytest.raises(ValueError, match="source_group"):
        _verify_leak_free_splits(leaky_sg)

    leaky_sha = [
        {"split": "train", "source_group": "g1", "sha256": "same"},
        {"split": "test", "source_group": "g2", "sha256": "same"},
    ]
    with pytest.raises(ValueError, match="sha256"):
        _verify_leak_free_splits(leaky_sha)


# ---------------------------------------------------------------------------
# 6. End-to-end synthetic 6-piece puzzle generation + CLI validation
# ---------------------------------------------------------------------------

def test_end_to_end_synthetic_6_piece_puzzle(tmp_path: Path):
    run_dir = tmp_path / "synthetic_6_seed42"
    rc = cli_main([
        "generate",
        "--pieces", "6",
        "--width-mm", "220",
        "--outline", "organic",
        "--seed", "42",
        "--synthetic",
        "--opt-grid", "72",
        "--final-grid", "160",
        "--steps", "40",
        "--out", str(run_dir),
    ])
    assert rc == 0

    # Verify full Dossier Section 5.2 bundle
    expected_files = [
        "config.json",
        "outline.svg",
        "cut.svg",
        "pieces.svg",
        "assembly.svg",
        "assembly.png",
        "pieces_contact_sheet.png",
        "pieces_unlabeled_sheet.png",
        "partition.json",
        "quality.json",
        "provenance.json",
        "metrics.csv",
        "logs.txt",
    ]
    for fname in expected_files:
        assert (run_dir / fname).exists(), f"Missing deliverable {fname}"
    for idx in range(1, 7):
        assert (run_dir / "pieces" / f"{idx:04d}.svg").exists()
        assert (run_dir / "pieces" / f"{idx:04d}.png").exists()

    with open(run_dir / "quality.json", "r", encoding="utf-8") as f:
        q = json.load(f)
    assert q["all_hard_gates_passed"] is True, f"Failed gates: {q['gates']}"
    assert q["summary"]["num_pieces"] == 6
    assert q["summary"]["coverage_rel_error"] < 1e-4
    assert q["summary"]["max_pairwise_overlap_mm2"] < 1e-3

    # Independent CLI validate check
    v_rc = cli_main(["validate", "--run-dir", str(run_dir)])
    assert v_rc == 0

    config_file = run_dir / "config.json"
    config = json.loads(config_file.read_text(encoding="utf-8"))
    config["num_pieces"] = 7
    config_file.write_text(json.dumps(config), encoding="utf-8")
    assert not validate_run_dir(run_dir)["gates"]["cut_svg_reimport_exact_n"]


def test_animal_family_never_backfills_with_other_families(tmp_path: Path):
    from PIL import Image

    motifs = make_synthetic_nordic_motifs(3)
    records = []
    for m in motifs:
        name = f"{m.asset_id}.png"
        Image.fromarray((m.mask * 255).astype(np.uint8)).save(tmp_path / name)
        records.append(dict(asset_id=m.asset_id, family=m.family, motif_class=m.motif_class,
                            jigsaw_tier=m.jigsaw_tier, mask_path=name))
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(records), encoding="utf-8")

    chosen = select_diverse_motifs(catalog, 2, requested_classes=["bronze_age_ship"], family="animal")
    assert {m.motif_class for m in chosen} == {"reindeer", "bear"}
    with pytest.raises(ValueError, match="found 2 distinct eligible motifs"):
        select_diverse_motifs(catalog, 3, family="animal")
    assert all(m.family == "animal" for m in make_synthetic_nordic_motifs(6, family="animal"))


def test_silhouette_overlap_counts_the_entire_target_at_the_board_edge():
    motif = make_synthetic_nordic_motifs(1)[0]
    cell = np.zeros((64, 64), dtype=bool)
    cell[:22, :38] = True
    padded_cell = np.pad(cell, 64)
    iou, _, pose = _aligned_iou_and_chamfer(cell, motif)
    padded_iou, _, _ = _aligned_iou_and_chamfer(padded_cell, motif)
    shifted = PiecePose(pose.cx + 64, pose.cy + 64, pose.angle_deg, pose.scale, pose.flip_x)
    warped, _ = warp_motif_to_grid(motif, shifted, *padded_cell.shape)
    target = warped > 0.5
    actual_iou = (target & padded_cell).sum() / (target | padded_cell).sum()
    assert iou == pytest.approx(actual_iou, abs=1e-3)
    assert iou == pytest.approx(padded_iou, abs=1e-6)


# ---------------------------------------------------------------------------
# 7. Upgraded solver: log-space slowness, saliency, Model A guidance & evaluate CLI
# ---------------------------------------------------------------------------

def test_log_space_slowness_and_saliency_properties():
    from heller_jigsaw.solver.geodesic import MultiScaleSlownessField, compute_motif_saliency

    s_min, s_max = 0.04, 24.0
    field = MultiScaleSlownessField(48, 56, s_min=s_min, s_max=s_max, log_space=True)
    s_grid, _ = field()
    assert s_grid.shape == (48, 56)
    assert float(s_grid.min()) >= s_min - 1e-6
    assert float(s_grid.max()) <= s_max + 1e-6
    # Zero raw field maps to geometric mean sqrt(s_min * s_max), not arithmetic mean
    geom_mean = math.sqrt(s_min * s_max)
    assert float(s_grid.mean()) == pytest.approx(geom_mean, rel=1e-3)

    motif = make_synthetic_nordic_motifs(1)[0]
    sal = compute_motif_saliency(motif.mask)
    assert sal.shape == motif.mask.shape
    assert float(sal.min()) >= 1.0 - 1e-5
    assert float(sal.max()) > 2.0


def test_recognizer_guidance_and_evaluate_cli(tmp_path: Path):
    # Create a tiny deterministic v2 Model A checkpoint strictly for testing guidance + evaluate CLI
    torch.manual_seed(1234)
    families = ["animal", "human", "symbol", "vessel"]
    classes = ["bear", "bronze_age_ship", "footprint", "reindeer", "skier", "whale_cetacean"]
    enc = HellerSilhouetteEncoder(num_families=len(families), num_classes=len(classes), embed_dim=128)
    ckpt_path = tmp_path / "tiny_model_a.pt"
    torch.save(
        {
            "state_dict": enc.state_dict(),
            "families": families,
            "classes": classes,
            "img_size": 64,
            "embed_dim": 128,
            "arch_version": 2,
            "temperature_family": 1.0,
            "temperature_class": 1.0,
        },
        ckpt_path,
    )

    run_dir = tmp_path / "guided_synth6"
    rc = cli_main([
        "generate",
        "--pieces", "6",
        "--width-mm", "220",
        "--outline", "organic",
        "--seed", "42",
        "--synthetic",
        "--opt-grid", "64",
        "--final-grid", "160",
        "--steps", "24",
        "--restarts", "2",
        "--pack-iters", "60",
        "--recognizer", str(ckpt_path),
        "--sem-weight", "0.25",
        "--device", "cpu",
        "--out", str(run_dir),
    ])
    assert rc == 0
    assert (run_dir / "recognition.json").exists()

    q = json.loads((run_dir / "quality.json").read_text(encoding="utf-8"))
    assert q["all_hard_gates_passed"] is True
    assert "num_edge_pieces" in q["summary"]
    assert "num_interior_pieces" in q["summary"]
    assert "mean_aligned_iou" in q["summary"]
    assert "recognition" in q
    assert q["recognition"]["num_pieces"] == 6

    eval_out = tmp_path / "eval_report.json"
    erc = cli_main([
        "evaluate",
        "--run-dir", str(run_dir),
        "--recognizer", str(ckpt_path),
        "--tta-rotations", "4",
        "--device", "cpu",
        "--out", str(eval_out),
    ])
    assert erc == 0
    ev = json.loads(eval_out.read_text(encoding="utf-8"))
    assert ev["all_hard_gates_passed"] is True
    assert ev["num_pieces"] == 6
    assert ev["recognition"]["num_pieces"] == 6


def test_corpus_selection_handles_phylopic_none_fields_and_symmetric_aspect(tmp_path: Path):
    from PIL import Image
    from heller_jigsaw.data.corpus import _score_candidate_for_jigsaw

    motifs = make_synthetic_nordic_motifs(2, family="animal")
    recs = []
    for i, m in enumerate(motifs):
        fname = f"pp_{i}.png"
        Image.fromarray((m.mask * 255).astype(np.uint8)).save(tmp_path / fname)
        recs.append({
            "asset_id": f"pp_{i}",
            "source_archive": "phylopic",
            "family": "animal",
            "motif_class": m.motif_class,
            "jigsaw_tier": None,
            "connected_components": None,
            "hole_count": None,
            "solidity": None,
            "aspect_ratio": None,
            "commercial_ok": True,
            "mask_path": fname,
        })
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps(recs), encoding="utf-8")
    selected = select_diverse_motifs(cat, 2, family="animal", commercial_only=True)
    assert len(selected) == 2

    # Extreme vertical stick (aspect_ratio = 0.2 -> 5:1) should be penalized same as horizontal 5:1
    s_tall = _score_candidate_for_jigsaw({"jigsaw_tier": "A_ready", "motif_class": "reindeer", "aspect_ratio": 0.2})
    s_wide = _score_candidate_for_jigsaw({"jigsaw_tier": "A_ready", "motif_class": "reindeer", "aspect_ratio": 5.0})
    s_bal = _score_candidate_for_jigsaw({"jigsaw_tier": "A_ready", "motif_class": "reindeer", "aspect_ratio": 1.5})
    assert s_tall == pytest.approx(s_wide, abs=1e-6)
    assert s_bal > s_tall
