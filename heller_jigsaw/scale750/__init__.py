"""Scalable GPU + sparse-graph pipeline for 750-piece all-figurative Helleristning jigsaws."""

from .audit_sparse import (
    fabrication_audit_sparse,
    fidelity_audit_fast,
    lock_check_sparse,
    min_segment_length_mm,
    pair_margin_fast,
    rigid_motion_audit_fast,
    thin_feature_audit_fast,
    translation_audit_sparse,
)

__all__ = [
    "lock_check_sparse",
    "pair_margin_fast",
    "translation_audit_sparse",
    "thin_feature_audit_fast",
    "rigid_motion_audit_fast",
    "fidelity_audit_fast",
    "fabrication_audit_sparse",
    "min_segment_length_mm",
]
