"""Draw a common-scale comparison from eight exported Formverk cut files."""

from pathlib import Path
from xml.etree import ElementTree as ET


FAMILY_LABELS = {
    "phase_field": "Phase field",
    "elastic_rods": "Elastic rods",
    "circular_arcs": "Circular arcs",
    "quadrilateral_exchange": "Quadrilateral exchange",
}
INK = "#20252a"
MUTED = "#515a61"


def _label(parent, x, y, value, size=15, weight="normal", color=INK):
    text = ET.SubElement(parent, "text", {
        "x": str(x), "y": str(y), "font-size": str(size),
        "font-weight": weight, "fill": color,
    })
    text.text = value


def _read_cut(file):
    svg = ET.parse(file).getroot()
    x, y, width, height = map(float, svg.attrib["viewBox"].split())
    if width <= 0 or height <= 0:
        raise ValueError(f"Empty board viewBox in {file}")
    layers = {}
    for name in ("interior_cut", "exterior_cut"):
        paths = svg.findall(f"./{{*}}g[@id='{name}']/{{*}}path")
        if not paths:
            raise ValueError(f"No paths in {name} in {file}")
        layers[name] = [path.attrib["d"] for path in paths]
    return (x, y, width, height), layers


def write_plate(folder: Path, rows: list[dict]) -> None:
    """Write cut-comparison.svg using the unmodified path data and a shared geometric scale."""
    if len(rows) != 8 or len({row["style"] for row in rows}) != 8:
        raise ValueError("The comparison plate needs eight distinct cut styles")
    boards = [_read_cut(folder / row["style"] / "cut.svg") for row in rows]
    scale = min(236 / max(200, *(bounds[2] for bounds, _ in boards)),
                189 / max(160, *(bounds[3] for bounds, _ in boards)))
    root = ET.Element("svg", {
        "xmlns": "http://www.w3.org/2000/svg", "viewBox": "0 0 1120 735",
        "width": "1120", "height": "735", "role": "img", "aria-labelledby": "title desc",
        "font-family": "Arial, Helvetica, sans-serif",
    })
    ET.SubElement(root, "title", id="title").text = "Eight puzzle cuts on the same rectangular input"
    desc = ET.SubElement(root, "desc", id="desc")
    descriptions = []
    ET.SubElement(root, "rect", width="1120", height="735", fill="white")
    _label(root, 30, 43, "Eight ways to cut the same board", 32, "bold")
    _label(root, 31, 71, "Rectangular input 200 × 160 mm  ·  Target 14 pieces  ·  Seed 42", 17, color=MUTED)
    ET.SubElement(root, "path", d="M30 87 H1090", stroke="#939ba1", fill="none")

    for index, (row, (bounds, layers)) in enumerate(zip(rows, boards)):
        style = row["style"]
        family = FAMILY_LABELS[row["family"]]
        passed = row["all_hard_gates_passed"]
        if not isinstance(passed, bool):
            raise ValueError(f"Expected a boolean audit result for {style}")
        name = style.capitalize()
        audit = "Audit passed" if passed else "Audit failed"
        pieces = int(row["pieces"])
        length = float(row["cut_length_mm"])
        margin = float(row["assembly_lock_margin_mm"])
        descriptions.append(f"{name}, {family}, {pieces} pieces, {length:.2f} mm cut, "
                            f"{margin:.3f} mm lock margin. {audit}.")
        col = index % 4
        line = index // 4
        panel = ET.SubElement(root, "g", id=f"cut-{style}", transform=f"translate({30 + col * 270} {101 + line * 301})")
        _label(panel, 0, 21, name, 23, "bold")
        _label(panel, 1, 42, family, 15, color=MUTED)

        x, y, width, height = bounds
        dx = (250 - width * scale) / 2 - x * scale
        dy = 55 + (189 - height * scale) / 2 - y * scale
        drawing = ET.SubElement(panel, "g", {
            "transform": f"translate({dx:.6f} {dy:.6f}) scale({scale:.8f})",
            "fill": "none", "stroke-linejoin": "round", "stroke-linecap": "round",
        })
        for layer_name, stroke_width in (("interior_cut", "0.87"), ("exterior_cut", "1.45")):
            layer = ET.SubElement(drawing, "g", {"data-layer": layer_name, "stroke": INK, "stroke-width": stroke_width})
            for path in layers[layer_name]:
                ET.SubElement(layer, "path", d=path)

        _label(panel, 2, 267, f"{pieces} pieces  ·  {length:,.2f} mm cut", 15)
        status_color = "#285d80" if passed else "#9a392a"
        symbol = ET.SubElement(panel, "g", fill="none", stroke=status_color)
        ET.SubElement(symbol, "circle", cx="8", cy="283", r="6")
        mark = "M4.9 283 l2.2 2.4 4.4 -5" if passed else "M5.7 280.7 l4.6 4.6 m0 -4.6 l-4.6 4.6"
        ET.SubElement(symbol, "path", {"d": mark, "stroke-width": "1.5", "stroke-linecap": "round"})
        _label(panel, 20, 288, audit, 14, "bold", status_color)
        _label(panel, 125, 288, f"Lock {margin:.3f} mm", 14, color=MUTED)

    desc.text = ("All eight exported boards use one drawing scale. Lock is the audited planar translation margin. "
                 + " ".join(descriptions))
    ET.SubElement(root, "path", d="M30 696 H1090", stroke="#b9bec2", fill="none")
    _label(root, 31, 720, "Lock is the SVG audit’s planar translation margin. All boards share one drawing scale.",
           15, color=MUTED)
    ET.ElementTree(root).write(folder / "cut-comparison.svg", encoding="utf-8", xml_declaration=True)
