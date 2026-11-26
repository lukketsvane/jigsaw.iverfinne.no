"""Model A & Model B: silhouette recognition encoder, shape-descriptor baseline, and conditional SDF VAE."""

from .recognizer import (
    HellerSilhouetteEncoder,
    ShapeDescriptorBaseline,
    extract_shape_descriptor,
    load_recognizer_checkpoint,
    make_tta_views,
    mask_to_model_input,
    normalize_mask_frame,
    predict_silhouette,
)
from .train_recognizer import evaluate_encoder_on_puzzle, train_and_evaluate_recognizer

__all__ = [
    "HellerSilhouetteEncoder",
    "ShapeDescriptorBaseline",
    "extract_shape_descriptor",
    "load_recognizer_checkpoint",
    "make_tta_views",
    "mask_to_model_input",
    "normalize_mask_frame",
    "predict_silhouette",
    "train_and_evaluate_recognizer",
    "evaluate_encoder_on_puzzle",
]
