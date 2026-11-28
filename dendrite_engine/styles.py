"""Cut names and their solver families, shared by the CLI and Studio."""

from .phase_field import STYLE_PRESETS

CUT_FAMILIES = {
    **dict.fromkeys(STYLE_PRESETS, "phase_field"),
    "maze": "elastic_rods",
    "wave": "elastic_rods",
    "coral": "circular_arcs",
    "matrix": "quadrilateral_exchange",
}
