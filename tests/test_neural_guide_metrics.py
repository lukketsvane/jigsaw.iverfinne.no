"""Validation-report checks on explicit targets, independent of model training."""

import json

import numpy as np
import pytest

from dendrite_engine.neural_guide import _per_target_metrics


def test_constant_rate_targets_have_null_r2_and_observed_patch_counts():
    # Columns are lock margin, neck pass fraction, kerf pass fraction and tortuosity.
    truth = np.array([[0, 1, 0.5, 1], [1, 1, 0.5, 2]], dtype=np.float32)
    pred = truth.copy()
    pred[:, 1] = 0.75
    report = json.loads(json.dumps(_per_target_metrics(truth, truth, truth, pred), allow_nan=False))
    neck = report["neck_pass_rate"]
    assert neck["val_r2"] is None
    assert neck["train_mae"] == 0.0
    assert neck["val_mae"] == neck["val_rmse"] == 0.25
    assert (neck["val_all_pass_patches"], neck["val_patches_with_failures"]) == (2, 0)
    kerf = report["kerf_pass_rate"]
    assert kerf["val_r2"] is None
    assert kerf["val_mae"] == 0.0
    assert (kerf["val_all_pass_patches"], kerf["val_patches_with_failures"]) == (0, 2)


def test_variable_targets_preserve_numeric_r2_and_count_partial_failures():
    truth = np.array([[0, 1, 0.25, 2], [1, 0.75, 1, 2], [2, 1, 0, 2]], dtype=np.float32)
    pred = truth.copy()
    pred[:, 0] = [0, 1, 0]
    report = _per_target_metrics(truth, pred, truth, pred)
    lock = report["lock_margin_mm"]
    assert lock["val_r2"] == -1.0
    assert lock["val_mae"] == pytest.approx(2 / 3, abs=0.0001)
    assert "val_all_pass_patches" not in lock
    assert report["neck_pass_rate"]["val_r2"] == 1.0
    assert report["neck_pass_rate"]["val_all_pass_patches"] == 2
    assert report["kerf_pass_rate"]["val_patches_with_failures"] == 2


def test_negligible_target_spread_preserves_errors_but_reports_null_r2():
    truth = np.ones((2, 4), dtype=np.float32)
    truth[1, 0] = 1.00001
    report = _per_target_metrics(truth, truth, truth, np.zeros_like(truth))
    assert report["lock_margin_mm"]["val_r2"] is None
    assert report["lock_margin_mm"]["val_mae"] == pytest.approx(1, abs=0.0001)
