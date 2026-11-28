"""Build the Formverk three-site kerf coupon and its printable measuring sheet."""

import argparse
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from shapely.geometry import Polygon, box
from shapely.ops import polygonize, unary_union
from svgpathtools import Line, parse_path


ROOT = Path(__file__).resolve().parents[1]
SVG = "http://www.w3.org/2000/svg"
WIDTH, HEIGHT, SIDE = 100, 76, 20
SITES = {"A": (8, 9), "B": (72, 9), "C": (40, 48)}


def write_svg(root, path):
    ET.indent(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def cut_sheet():
    page = ET.Element("svg", xmlns=SVG, width=f"{WIDTH}mm", height=f"{HEIGHT}mm",
                      viewBox=f"0 0 {WIDTH} {HEIGHT}")
    for name, (x, y) in SITES.items():
        path = f"M {x} {y} L {x+SIDE} {y} L {x+SIDE} {y+SIDE} L {x} {y+SIDE} Z"
        ET.SubElement(page, "path", id=f"site-{name}", d=path, stroke="#ff0000",
                      fill="none", **{"stroke-width": "0.1"})
    return page


def measuring_sheet():
    page = ET.Element("svg", xmlns=SVG, width="210mm", height="297mm", viewBox="0 0 210 297",
                      fill="#192b2c", **{"font-family": "Arial, sans-serif"})

    def label(x, y, text, size=3.4, **attrs):
        element = ET.SubElement(page, "text", x=str(x), y=str(y),
                                **{"font-size": str(size), **attrs})
        element.text = text

    def line(x1, y1, x2, y2, stroke="#82908d"):
        ET.SubElement(page, "line", x1=str(x1), x2=str(x2), y1=str(y1), y2=str(y2),
                      stroke=stroke, **{"stroke-width": ".30"})

    def rect(x, y, width, height, fill, stroke):
        ET.SubElement(page, "rect", x=str(x), y=str(y), width=str(width), height=str(height),
                      fill=fill, stroke=stroke, **{"stroke-width": ".35"})

    label(15, 15, "Formverk   /   Three-site kerf coupon", 6.5)
    label(15, 23, "Paper locator and measurement record   ·   laser input is coupon-cut.svg")
    label(15, 33, "Stock ID ______________________     Machine / settings ___________________________")

    left, top = 15, 43
    rect(left, top, WIDTH, HEIGHT, "#f1f5f3", "#738784")
    for site, (x, y) in SITES.items():
        rect(left+x, top+y, SIDE, SIDE, "white", "#284a47")
        label(left+x+SIDE/2, top+y+SIDE/2+2, site, 6, **{"text-anchor": "middle"})
    label(122, 48, "Direction on the job", 4.1)
    label(122, 56, "X runs across the page.")
    label(122, 63, "Y runs down the page.")
    line(129, 73, 148, 73, "#284a47")
    line(148, 73, 145, 71.4, "#284a47")
    line(148, 73, 145, 74.6, "#284a47")
    line(129, 73, 129, 90, "#284a47")
    line(129, 90, 127.4, 87, "#284a47")
    line(129, 90, 130.6, 87, "#284a47")
    label(151, 74, "X")
    label(127.8, 96, "Y")
    label(122, 106, "Face-grain direction")
    label(122, 113, "________________________")

    label(15, 131, "Cut and measure", 4.7)
    protocol = (
        "Cut at 100% scale with kerf compensation off. Each 20 mm square releases one plug.",
        "Keep the surrounding frame. Pencil the site letter and X direction on each plug before lifting.",
        "Use the same stock face and jaw depth throughout. Measure midway along opposite flats.",
        "Use the inside caliper jaws on the opening and the outside jaws on its matching plug.",
        "Measure across X and across Y. Record both widths in mm to the caliper’s resolution.",
        "For each row, kerf = (opening − plug) / 2. Record individual values before averaging.",
    )
    for i, step in enumerate(protocol, 1):
        label(15, 132+6.1*i, f"{i}.   {step}", 3.2)

    columns = (15, 37, 66, 103, 140, 195)
    header_y, row_h = 179, 7.5
    for x, name in zip(columns, ("Site", "Axis", "Opening (mm)", "Plug (mm)", "Kerf (mm)")):
        label(x+2, header_y, name, 3.4)
    line(15, header_y+2, 195, header_y+2, "#284a47")
    for i, (site, axis) in enumerate((s, a) for s in SITES for a in ("X", "Y")):
        y = header_y + (i+1)*row_h
        label(17, y, site)
        label(39, y, axis)
        line(15, y+2, 195, y+2)
    for x in columns[1:-1]:
        line(x, header_y+2, x, header_y+6*row_h+2)
    label(15, 240, "Mean X __________________ mm     Mean Y __________________ mm")
    label(15, 250, "Operator _________________________     Date _________________________")
    label(15, 260, "Face / jaw depth, stock location or taper observations")
    line(15, 269, 195, 269)
    line(15, 279, 195, 279)
    return page


def verify_cut(path):
    """Reimport the written SVG and verify every centerline and bounded face."""
    root = ET.parse(path).getroot()
    expected_page = {"width": f"{WIDTH}mm", "height": f"{HEIGHT}mm", "viewBox": f"0 0 {WIDTH} {HEIGHT}"}
    if root.tag != f"{{{SVG}}}svg" or root.attrib != expected_page:
        raise ValueError("Unexpected coupon page, units or transform")
    rings = []
    sites = {}
    for element in root:
        if element.tag != f"{{{SVG}}}path" or set(element.attrib) != {"id", "d", "stroke", "fill", "stroke-width"}:
            raise ValueError("Only plain through-cut paths are allowed on the coupon")
        if element.get("stroke") != "#ff0000" or element.get("fill") != "none" or len(element):
            raise ValueError("Unexpected cut operation or additional geometry")
        site = element.attrib["id"].removeprefix("site-")
        if site not in SITES or site in sites:
            raise ValueError(f"Unexpected or duplicate site {site}")
        contour = parse_path(element.attrib["d"])
        if len(contour) != 4 or not contour.iscontinuous() or not contour.isclosed() or not all(
            isinstance(segment, Line) and segment.length() == SIDE for segment in contour
        ):
            raise ValueError(f"Site {site} must be one closed, four-sided punch")
        polygon = Polygon([(segment.start.real, segment.start.imag) for segment in contour])
        x, y = SITES[site]
        if not polygon.is_valid or not polygon.equals(box(x, y, x+SIDE, y+SIDE)):
            raise ValueError(f"Incorrect position or shape at site {site}")
        if not box(0, 0, WIDTH, HEIGHT).contains_properly(polygon) or any(
            not polygon.disjoint(previous) for previous in rings
        ):
            raise ValueError("Square punches overlap each other or the page edge")
        rings.append(polygon)
        sites[site] = {"bounds_mm": list(polygon.bounds), "nominal_side_mm": SIDE}
    faces = list(polygonize(unary_union([polygon.boundary for polygon in rings])))
    if sites.keys() != SITES.keys() or len(faces) != 3:
        raise ValueError("Expected three enclosed plug faces and three matching frame openings")
    return {
        "digital_geometry_passed": True, "physical_measurements_recorded": False,
        "cut_file": path.name, "cut_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "page_mm": [WIDTH, HEIGHT], "closed_cut_paths": len(rings),
        "punch_count": len(faces), "unlabelled_cut_geometry_only": True,
        "frame_perimeter_cut": False, "sites": sites,
        "axes": ["X", "Y"], "expected_paired_measurements": 2*len(sites),
        "kerf_formula": "(opening_mm - plug_mm) / 2",
    }


def build_coupon(out_folder: Path):
    """Write the cut, locator and digital verification to the specified directory."""
    out_folder = Path(out_folder)
    out_folder.mkdir(parents=True, exist_ok=True)
    cut = out_folder / "coupon-cut.svg"
    write_svg(cut_sheet(), cut)
    write_svg(measuring_sheet(), out_folder / "coupon-map.svg")
    verification = verify_cut(cut)
    (out_folder / "verification.json").write_text(json.dumps(verification, indent=2)+"\n", encoding="utf-8")
    return verification


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "runs/formverk-wood-trial/calibration")
    print(json.dumps(build_coupon(parser.parse_args().output), indent=2))
