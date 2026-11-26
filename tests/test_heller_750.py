"""Tests for the 750-piece Exclusive All-Figurative Helleristning Wooden Jigsaw pipeline."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from heller_jigsaw.geometry.validate import ValidationConfig, validate_run_dir
from heller_jigsaw.production.audit import lock_check
from heller_jigsaw.scale750.audit_sparse import (
    lock_check_sparse,
    min_segment_length_mm,
    rigid_motion_audit_fast,
    thin_feature_audit_fast,
    translation_audit_sparse,
)

RUN_32 = ROOT / "runs" / "v10-campaign" / "verified-winners" / "helleristning"
RUN_750 = ROOT / "runs" / "helleristning-750"


@pytest.mark.skipif(not (RUN_32 / "partition.json").exists(), reason="V10 32-piece reference run not present")
def test_sparse_audit_exact_parity_vs_production_audit() -> None:
    """Sparse 750-piece audit routines must match ``heller_jigsaw.production.audit`` identically."""
    d = json.loads((RUN_32 / "partition.json").read_text(encoding="utf-8"))
    n = len(d["faces"])

    ref_lock = lock_check(d["edges"], n)
    sp_lock = lock_check_sparse(d["edges"], n)
    assert sp_lock == ref_lock

    ref_trans = json.loads((RUN_32 / "finite-motion-audit.json").read_text(encoding="utf-8"))
    sp_trans = translation_audit_sparse(RUN_32, write=False)
    assert sp_trans["unblocked_directions_by_distance"] == ref_trans["unblocked_directions_by_distance"]
    assert sp_trans["all_proper_groups_blocked_within_3mm"] == ref_trans["all_proper_groups_blocked_within_3mm"]

    ref_thin = json.loads((RUN_32 / "thin-feature-audit.json").read_text(encoding="utf-8"))
    sp_thin = thin_feature_audit_fast(RUN_32, write=False)
    assert abs(sp_thin["worst_tip_loss_mm"] - ref_thin["worst_tip_loss_mm"]) < 1e-6
    assert sp_thin["pieces_tip_loss_over_3mm"] == ref_thin["pieces_tip_loss_over_3mm"]
    assert sp_thin["pieces_tip_loss_over_6mm"] == ref_thin["pieces_tip_loss_over_6mm"]

    ref_rigid = json.loads((RUN_32 / "rigid-motion-screen.json").read_text(encoding="utf-8"))
    sp_rigid = rigid_motion_audit_fast(RUN_32, write=False)
    assert sp_rigid["pieces_with_rotation_4deg_or_more"] == ref_rigid["pieces_with_rotation_4deg_or_more"]
    assert sp_rigid["pieces_with_translation_3mm"] == ref_rigid["pieces_with_translation_3mm"]


def test_end_to_end_small_gpu_pipeline(tmp_path: Path) -> None:
    """Run the complete scale750 pipeline on 16 synthetic/real commercial motifs and verify all hard gates."""
    cat_path = ROOT / "hellerisninger" / "processed" / "catalog.json"
    if not cat_path.exists():
        pytest.skip("Processed hellerisninger catalog not available")
    rows = json.loads(cat_path.read_text(encoding="utf-8"))
    comm = [r for r in rows if r.get("commercial_ok") and r.get("motif_class") != "unclassified_motif"][:16]
    assert len(comm) == 16
    mini_cat = tmp_path / "mini16.json"
    mini_cat.write_text(json.dumps(comm), encoding="utf-8")

    from scripts.build_helleristning_750 import build_puzzle

    out_dir = tmp_path / "out16"
    summary = build_puzzle(
        catalog_path=mini_cat,
        out_dir=out_dir,
        width_mm=155.0,
        height_mm=115.0,
        seed=17,
        restarts=1,
        px=0.45,
        bay=14.0,
        min_neck_mm=3.0,
        max_tip_mm=6.0,
        min_seg_mm=0.05,
        weakest_count=8,
    )
    assert summary["piece_count"] == 16
    assert summary["zero_filler_verified"] is True
    assert summary["all_commercial_ok_flagged"] is True
    assert summary["lock_margin_mm"] >= 3.0
    assert summary["worst_tip_loss_mm"] <= 6.0
    assert summary["min_segment_mm"] >= 0.05
    assert summary["digital_screen_pass"] is True
    assert (out_dir / "cut.svg").exists()
    assert (out_dir / "review_tool.html").exists()
    assert (out_dir / "weakest_review_packet" / "page_01.png").exists()


@pytest.mark.skipif(not (RUN_750 / "partition.json").exists(), reason="750-piece production run not yet generated")
def test_production_750_piece_gates() -> None:
    """Verify every hard user requirement on the final 750-piece Helleristning production bundle."""
    d = json.loads((RUN_750 / "partition.json").read_text(encoding="utf-8"))
    audit = json.loads((RUN_750 / "production-audit.json").read_text(encoding="utf-8"))
    fid = json.loads((RUN_750 / "fidelity-audit.json").read_text(encoding="utf-8"))
    prov = json.loads((RUN_750 / "provenance.json").read_text(encoding="utf-8"))

    # 1. Exact 750 all-figurative pieces, zero filler, zero NC-SA
    assert len(d["faces"]) == 750
    assert audit["n"] == 750
    assert audit["zero_filler_verified"] is True
    assert audit["all_commercial_ok_flagged"] is True
    assert prov["all_commercial_ok"] is True
    assert prov["zero_nc_sa_verified"] is True
    for f in d["faces"]:
        md = f["metadata"]
        assert not str(md.get("asset_id", "")).startswith("flow-")
        assert md.get("is_filler") is False
        assert md.get("commercial_ok") is True
        lic = str(md.get("license", "")).lower()
        assert "non-commercial" not in lic and "nc-sa" not in lic

    # 2. Exact SVG re-import, gapless coverage, zero overlaps, zero micro-segments < 0.05 mm
    vcfg = ValidationConfig(kerf_mm=0.15, min_neck_mm=3.0, min_segment_mm=0.05)
    val = validate_run_dir(RUN_750, config=vcfg)
    assert val["all_hard_gates_passed"] is True, f"Failed gates: {[k for k, v in val['gates'].items() if not v]}"
    val_summary = val["partition_validation"]["summary"]
    assert val_summary["num_pieces"] == 750
    assert val_summary["coverage_sym_diff_mm2"] < 1e-3
    assert val_summary["max_pairwise_overlap_mm2"] < 1e-3
    assert min_segment_length_mm(d["edges"]) >= 0.05

    # 3. Kerf (0.10-0.25 mm), 3.0 mm neck, <= 6.0 mm tip, >= 3.0 mm 360-deg lock
    assert audit["digital_screen_pass"] is True
    assert audit["lock_margin_mm"] >= 3.0
    assert audit["unlocked_directions_at_1mm"] == 0
    assert audit["unlocked_directions_at_2mm"] == 0
    assert audit["unlocked_directions_at_3mm"] == 0
    for k in ("0.10", "0.15", "0.20", "0.25"):
        assert audit[f"kerf_{k}"]["kerf_pass"] == 750
        assert audit[f"kerf_{k}"]["neck_pass"] == 750
    assert audit["worst_tip_loss_mm"] <= 6.0
    assert audit["pieces_tip_loss_over_6mm"] == 0
    assert audit["translation_unblocked_by_distance"]["1.0"] == 0

    # 4. Recognizable silhouette fidelity & complete review deliverables
    assert fid["pieces"] == 750
    assert fid["summary"]["iou"]["mean"] >= 0.72
    assert (RUN_750 / "cut.svg").exists()
    assert (RUN_750 / "assembly.png").exists()
    assert (RUN_750 / "assembly_labeled.png").exists()
    assert (RUN_750 / "review_tool.html").exists()
    assert (RUN_750 / "weakest_review_packet" / "weakest_summary.json").exists()
    assert (RUN_750 / "weakest_review_packet" / "page_01.png").exists()
