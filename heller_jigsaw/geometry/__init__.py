"""Authoritative planar geometry kernel for laser-cut figurative jigsaw puzzles.

All coordinates and lengths are in millimetres (mm).
Every exported coordinate is snapped to the SNAP grid (0.1 micrometre).
"""

SNAP = 1e-4  # coordinate grid in mm (0.1 micrometre)
DEFAULT_KERF_MM = 0.15
DEFAULT_MIN_NECK_MM = 3.0

from .outline import (
    circle,
    from_svg,
    grid_shape,
    inside_mask,
    organic_stone,
    rectangle,
    snap_polygon,
)
from .partition import (
    CanonicalEdge,
    FaceRecord,
    PlanarPartition,
    extract_partition_from_labels,
    partition_from_polygons,
)
from .validate import ValidationConfig, validate_partition, validate_run_dir
from .export import export_puzzle_run

__all__ = [
    "SNAP",
    "DEFAULT_KERF_MM",
    "DEFAULT_MIN_NECK_MM",
    "circle",
    "rectangle",
    "organic_stone",
    "from_svg",
    "snap_polygon",
    "inside_mask",
    "grid_shape",
    "CanonicalEdge",
    "FaceRecord",
    "PlanarPartition",
    "extract_partition_from_labels",
    "partition_from_polygons",
    "ValidationConfig",
    "validate_partition",
    "validate_run_dir",
    "export_puzzle_run",
]
