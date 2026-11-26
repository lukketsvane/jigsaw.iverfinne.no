"""750-Piece Dendrite + Trilobites + Helleristninger Wooden Jigsaw Engine.

Combines:
- Parametric multi-genus trilobites (cut silhouettes + strictly inset anatomical engraving)
- Curated commercial-clean Nordic helleristninger (zero Alta Museum NC-SA)
- GPU-accelerated gapless 750-motif board composition (RTX A4500)
- Corridor-constrained multiphase Allen-Cahn dendritic seam evolution
- Sparse 360-degree interlocking, kerf/neck/tip verification, and dual-layer SVG export
"""

from .trilobites import (
    TRILOBITE_GENERA,
    TrilobiteSpec,
    build_trilobite_collection,
    clip_and_verify_engraving,
    generate_trilobite,
    verify_engraving_clearance,
)

__all__ = [
    "TRILOBITE_GENERA",
    "TrilobiteSpec",
    "build_trilobite_collection",
    "clip_and_verify_engraving",
    "generate_trilobite",
    "verify_engraving_clearance",
]
