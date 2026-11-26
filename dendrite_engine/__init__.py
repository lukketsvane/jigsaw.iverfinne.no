"""Clean-room GPU Dendritic Solidification, Cosserat Rod Growth, and Neural Puzzle Engine.

Implements:
- Multiphase Kobayashi/Steinbach Allen-Cahn dendritic solidification (2012 paper Eqs. 1-11)
  accelerated on CUDA with distance-2 graph coloring and reflective whimsy/outline BCs.
- 2D planar Position-Based Cosserat Rod elastic growth (MIT PositionBasedDynamics,
  Kugelstadt & Schomer 2016) for Maze and Wave cuts.
- Fabrication-safe common-line 1:1 mm SVG export with separate interior/exterior/engrave layers
  and independent re-parse, kerf, 3 mm neck, and 360-direction interlock audits.
- GPU-trained neural parameter/quality surrogate and Formverk Studio web backend.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .phase_field import (
    DendriteConfig,
    DendriteResult,
    STYLE_PRESETS,
    build_outline_polygon,
    distance2_graph_coloring,
    run_dendrite_simulation,
)
from .rod_growth import RodConfig, RodResult, run_rod_growth
from .whimsies import WhimsySpec, WhimsyPlacement, build_whimsy_geometry, list_builtin_whimsies
from .postprocess import ExportResult, audit_cut_svg, export_puzzle

__all__ = [
    "__version__",
    "DendriteConfig",
    "DendriteResult",
    "STYLE_PRESETS",
    "build_outline_polygon",
    "distance2_graph_coloring",
    "run_dendrite_simulation",
    "RodConfig",
    "RodResult",
    "run_rod_growth",
    "WhimsySpec",
    "WhimsyPlacement",
    "build_whimsy_geometry",
    "list_builtin_whimsies",
    "ExportResult",
    "audit_cut_svg",
    "export_puzzle",
]
