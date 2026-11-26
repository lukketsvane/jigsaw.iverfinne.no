#!/usr/bin/env python3
"""
Independent QA Verification Harness for the Nordic Helleristninger Silhouette Corpus.

Audits all hard gates from Animal Jigsaw AI Dossier (Sections 3.1, 5, 8) without
sharing any code with `hellerisninger/pipeline/`:
  1. Provenance & Integrity Gate (raw manifests, SHA-256, file integrity, honest licenses, catalog consistency)
  2. Split Hygiene Gate (zero source_group / sha256 / source_id / processed-mask leakage across train/val/test)
  3. Geometry & SVG Re-Import Gate (independent SVG & polygon JSON parsing with Shapely, Figma sheets XML/path validation)
  4. Mask & SDF Consistency Gate (256x256 binary masks, connectivity/holes, SDF sign & gradient checks, SVG-mask alignment)
  5. Fabrication Neck / Kerf Gate (independent polygon buffering & mask erosion for 0.15 mm kerf and 3.0 mm neck at 60 mm scale)
  6. Automated Shape-Quality / Anti-Junk Diagnostics (flags likely frames, text strips, noisy rock textures, duplicates for visual QA)

Usage:
    python hellerisninger/qa/verify_corpus.py [--root hellerisninger] [--write-reports]
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

Image.MAX_IMAGE_PIXELS = None
from shapely.geometry import LinearRing, MultiPolygon, Polygon
from shapely.ops import unary_union
from svgpathtools import Arc, CubicBezier, Line, Path as SvgPath, QuadraticBezier, parse_path

# ---------------------------------------------------------------------------
# Constants & Policy Definitions
# ---------------------------------------------------------------------------

EXPECTED_MANIFEST_FIELDS = {
    "source_id",
    "source_archive",
    "title",
    "description",
    "site",
    "region",
    "country",
    "period",
    "media_type",
    "motif_hints",
    "raw_path",
    "source_url",
    "download_url",
    "creator",
    "license",
    "license_url",
    "commercial_ok",
    "sha256",
    "width_px",
    "height_px",
}

VALID_COUNTRIES = {"NO", "SE", "DK", "FI", "NORDIC"}
VALID_PERIODS = {"stone_age_hunter", "bronze_age_agrarian", "mixed", "unknown"}
VALID_MEDIA_TYPES = {
    "svg_vector",
    "bw_tracing",
    "rubbing_frottage",
    "painted_photo",
    "photo_other",
}
VALID_TIERS = {"A_ready", "B_thickened", "C_fragile_or_review"}
CUTTABLE_TIERS = {"A_ready", "B_thickened"}
VALID_RENDER_STYLES = {"solid_pecked", "outline_enclosed", "linear_motif"}
VALID_SPLITS = {"train", "val", "test"}

# Fabrication parameters from Dossier Section 5 / 5.1 (at ~60 mm physical piece size)
TARGET_PIECE_MM = 60.0
KERF_MM = 0.15
MIN_NECK_MM = 3.0
KERF_RADIUS_MM = KERF_MM / 2.0                    # 0.075 mm
CONSERVATIVE_NECK_RADIUS_MM = (MIN_NECK_MM + KERF_MM) / 2.0  # 1.575 mm
DIAGNOSTIC_2MM_RADIUS_MM = (2.0 + KERF_MM) / 2.0  # 1.075 mm

# Expected mask / SDF / SVG dimensions
EXPECTED_MASK_SIZE = 256
EXPECTED_VIEWBOX_SIZE = 100.0

# Non-commercial / restricted / unknown license patterns
LICENSE_DISALLOWED_COMMERCIAL_PATTERNS = [
    r"\bnc\b",
    r"by-nc",
    r"by_nc",
    r"non-?commercial",
    r"noc-nc",
    r"noc-oklr",
    r"\bnd\b",
    r"by-nd",
    r"by_nd",
    r"no-?deriv",
    r"unknown",
    r"unspecified",
    r"\bcne\b",
    r"\bund\b",
    r"copyright\s+not\s+evaluated",
    r"copyright\s+undetermined",
    r"all\s+rights\s+reserved",
    r"in\s+copyright",
    r"\binc\b",
    r"inc-",
    r"copyrighted",
    r"fair\s+use",
    r"restricted",
    r"permission\s+required",
    r"educational\s+only",
    r"non-?free",
    r"©",
]

# Copyrighted / non-open licenses that harvesters should not ingest
LICENSE_NON_OPEN_PATTERNS = [
    r"all\s+rights\s+reserved",
    r"in\s+copyright",
    r"\binc\b",
    r"inc-",
    r"copyrighted",
    r"fair\s+use",
    r"non-?free",
]

# Clearly permissive licenses that permit commercial derivative works
LICENSE_PERMISSIVE_COMMERCIAL_PATTERNS = [
    r"\bcc0\b",
    r"cc-0",
    r"creative\s+commons\s+zero",
    r"\bpublic\s+domain\b",
    r"\bpd\b",
    r"\bpd-",
    r"\bpdm\b",
    r"\bcc[\s_-]?by([\s_-]?sa)?(\s+\d\.\d)?\b",
    r"\battribution([\s_-]?sharealike)?\b",
    r"\bnlod\b",
    r"\bmit\b",
    r"\bapache\b",
    r"\bbsd\b",
    r"\bgfdl\b",
    r"\bgpl\b",
    r"\blgpl\b",
    r"\bfree\s+art\b",
    r"\bfal\b",
    r"\bopen\s+government\b",
    r"\bogly\b",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_valid_http_url(url: Any) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlparse(url.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def classify_license(license_str: Any) -> Dict[str, Any]:
    """
    Classify a license string honestly for commercial derivative suitability.
    Returns dict with:
      - normalized: str
      - allows_commercial_derivatives: bool
      - is_non_open_or_copyrighted: bool
      - is_unknown: bool
      - reason: str
    """
    if not isinstance(license_str, str) or not license_str.strip():
        return {
            "normalized": "",
            "allows_commercial_derivatives": False,
            "is_non_open_or_copyrighted": False,
            "is_unknown": True,
            "reason": "empty_or_missing_license",
        }
    norm = license_str.strip()
    low = norm.lower()

    for pat in LICENSE_NON_OPEN_PATTERNS:
        if re.search(pat, low):
            return {
                "normalized": norm,
                "allows_commercial_derivatives": False,
                "is_non_open_or_copyrighted": True,
                "is_unknown": False,
                "reason": f"non_open_license_match:{pat}",
            }

    for pat in LICENSE_DISALLOWED_COMMERCIAL_PATTERNS:
        if re.search(pat, low):
            return {
                "normalized": norm,
                "allows_commercial_derivatives": False,
                "is_non_open_or_copyrighted": False,
                "is_unknown": ("unknown" in low or "unspecified" in low),
                "reason": f"non_commercial_or_restricted:{pat}",
            }

    for pat in LICENSE_PERMISSIVE_COMMERCIAL_PATTERNS:
        if re.search(pat, low):
            return {
                "normalized": norm,
                "allows_commercial_derivatives": True,
                "is_non_open_or_copyrighted": False,
                "is_unknown": False,
                "reason": f"permissive_match:{pat}",
            }

    # Default conservative stance: unrecognized license string cannot be assumed commercial_ok=True
    return {
        "normalized": norm,
        "allows_commercial_derivatives": False,
        "is_non_open_or_copyrighted": False,
        "is_unknown": True,
        "reason": "unrecognized_license_string",
    }


def resolve_path(root: Path, rel_or_abs: Any, subdirs: Sequence[str] = ()) -> Optional[Path]:
    """Resolve a path relative to `root` or `root/<subdir>`, or absolute if valid."""
    if not isinstance(rel_or_abs, str) or not rel_or_abs.strip():
        return None
    raw = rel_or_abs.strip().replace("\\", "/")
    p = Path(raw)
    if p.is_absolute():
        return p if p.exists() else p
    # Check direct relative to root
    cand = root / raw
    if cand.exists():
        return cand
    # Check if raw starts with root.name (e.g., "hellerisninger/...")
    if raw.startswith(root.name + "/"):
        cand2 = root.parent / raw
        if cand2.exists():
            return cand2
    for sub in subdirs:
        cand3 = root / sub / raw
        if cand3.exists():
            return cand3
    return cand


def parse_svg_length(val: Optional[str]) -> Optional[float]:
    if not val:
        return None
    m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(mm|cm|in|px|pt|pc|%)?\s*$", val)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or ""
    if unit == "cm":
        return num * 10.0
    if unit == "in":
        return num * 25.4
    return num


def parse_viewbox(vb: Optional[str]) -> Optional[Tuple[float, float, float, float]]:
    if not vb:
        return None
    parts = re.split(r"[\s,]+", vb.strip())
    if len(parts) != 4:
        return None
    try:
        vals = tuple(float(x) for x in parts)
        return vals  # type: ignore[return-value]
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Independent SVG Transform & Path Flattening -> Shapely Polygons
# ---------------------------------------------------------------------------

def _mat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return a @ b


def parse_svg_transform(transform_str: Optional[str]) -> np.ndarray:
    """Parse SVG transform attribute into a 3x3 affine matrix."""
    mat = np.eye(3, dtype=np.float64)
    if not transform_str:
        return mat
    for m in re.finditer(r"([a-zA-Z]+)\s*\(([^)]*)\)", transform_str):
        kind = m.group(1).lower()
        nums = [float(x) for x in re.findall(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", m.group(2))]
        t = np.eye(3, dtype=np.float64)
        if kind == "translate" and len(nums) >= 1:
            tx = nums[0]
            ty = nums[1] if len(nums) >= 2 else 0.0
            t[0, 2] = tx
            t[1, 2] = ty
        elif kind == "scale" and len(nums) >= 1:
            sx = nums[0]
            sy = nums[1] if len(nums) >= 2 else sx
            t[0, 0] = sx
            t[1, 1] = sy
        elif kind == "rotate" and len(nums) >= 1:
            ang = math.radians(nums[0])
            c, s = math.cos(ang), math.sin(ang)
            r = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
            if len(nums) >= 3:
                cx, cy = nums[1], nums[2]
                t1 = np.array([[1, 0, cx], [0, 1, cy], [0, 0, 1]], dtype=np.float64)
                t2 = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]], dtype=np.float64)
                t = t1 @ r @ t2
            else:
                t = r
        elif kind == "matrix" and len(nums) == 6:
            a, b, c, d, e, f = nums
            t = np.array([[a, c, e], [b, d, f], [0.0, 0.0, 1.0]], dtype=np.float64)
        elif kind == "skewx" and len(nums) >= 1:
            t[0, 1] = math.tan(math.radians(nums[0]))
        elif kind == "skewy" and len(nums) >= 1:
            t[1, 0] = math.tan(math.radians(nums[0]))
        mat = _mat_mul(mat, t)
    return mat


def apply_affine(points: Sequence[Tuple[float, float]], mat: np.ndarray) -> List[Tuple[float, float]]:
    if not points:
        return []
    arr = np.asarray(points, dtype=np.float64)
    ones = np.ones((arr.shape[0], 1), dtype=np.float64)
    hom = np.hstack([arr, ones])
    out = (mat @ hom.T).T
    return [(float(p[0]), float(p[1])) for p in out]


def flatten_svg_subpath(subpath: SvgPath, samples_per_curve: int = 16) -> List[Tuple[float, float]]:
    """Flatten a continuous svgpathtools subpath into an ordered list of (x, y) vertices."""
    pts: List[Tuple[float, float]] = []
    for seg in subpath:
        if isinstance(seg, Line):
            p0 = (seg.start.real, seg.start.imag)
            p1 = (seg.end.real, seg.end.imag)
            if not pts or abs(pts[-1][0] - p0[0]) > 1e-9 or abs(pts[-1][1] - p0[1]) > 1e-9:
                pts.append(p0)
            pts.append(p1)
        elif isinstance(seg, (CubicBezier, QuadraticBezier, Arc)):
            seg_len = 0.0
            try:
                seg_len = float(seg.length(error=1e-3))
            except Exception:
                seg_len = 1.0
            n = max(samples_per_curve, min(64, int(math.ceil(seg_len * 2.0))))
            ts = np.linspace(0.0, 1.0, n + 1)
            for idx, t in enumerate(ts):
                z = seg.point(float(t))
                pt = (float(z.real), float(z.imag))
                if idx == 0 and pts and abs(pts[-1][0] - pt[0]) <= 1e-9 and abs(pts[-1][1] - pt[1]) <= 1e-9:
                    continue
                pts.append(pt)
        else:
            p0 = (seg.start.real, seg.start.imag)
            p1 = (seg.end.real, seg.end.imag)
            if not pts:
                pts.append(p0)
            pts.append(p1)
    # Remove consecutive duplicate points
    cleaned: List[Tuple[float, float]] = []
    for p in pts:
        if not cleaned or math.hypot(p[0] - cleaned[-1][0], p[1] - cleaned[-1][1]) > 1e-7:
            cleaned.append(p)
    return cleaned


def _split_svg_path_d(d_str: str) -> List[Tuple[List[Tuple[float, float]], bool]]:
    """
    Parse an SVG path `d` attribute into a list of (ring_points, is_closed) tuples,
    one per continuous subpath. Checks for NaN/Inf coordinates.
    """
    if not d_str or not d_str.strip():
        return []
    path = parse_path(d_str)
    if len(path) == 0:
        return []
    subpaths = path.continuous_subpaths()
    results: List[Tuple[List[Tuple[float, float]], bool]] = []
    for sp in subpaths:
        pts = flatten_svg_subpath(sp)
        if not pts:
            continue
        for x, y in pts:
            if not (math.isfinite(x) and math.isfinite(y)):
                raise ValueError("Non-finite coordinate in SVG path")
        # Check closure: either subpath.isclosed() or first/last within small tolerance
        closed = False
        try:
            closed = bool(sp.isclosed())
        except Exception:
            closed = False
        if not closed and len(pts) >= 3:
            if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) <= 1e-3:
                closed = True
        results.append((pts, closed))
    return results


def parse_points_attr(points_str: str) -> List[Tuple[float, float]]:
    nums = [float(x) for x in re.findall(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", points_str or "")]
    if len(nums) % 2 != 0:
        raise ValueError("Odd number of coordinates in polygon/polyline points")
    return [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]


def extract_svg_geometry(svg_path: Path) -> Dict[str, Any]:
    """
    Independently parse an SVG file and extract its viewBox, raw rings/subpaths,
    and Shapely Polygon assembly.
    """
    info: Dict[str, Any] = {
        "valid_xml": False,
        "root_is_svg": False,
        "viewbox": None,
        "width": None,
        "height": None,
        "element_count": 0,
        "subpaths": [],       # list of dicts: {ring_pts, is_closed, ring_is_simple, polygon, area}
        "polygons": [],       # top-level exterior Shapely polygons (after hole nesting analysis)
        "hole_count": 0,
        "connected_components": 0,
        "unclosed_subpaths": 0,
        "errors": [],
    }
    try:
        tree = ET.parse(svg_path)
        root = tree.getroot()
        info["valid_xml"] = True
    except Exception as exc:
        info["errors"].append(f"XML parse error: {exc}")
        return info

    tag_local = root.tag.split("}")[-1].lower()
    if tag_local != "svg":
        info["errors"].append(f"Root tag is <{tag_local}>, expected <svg>")
        return info
    info["root_is_svg"] = True
    info["viewbox"] = parse_viewbox(root.attrib.get("viewBox"))
    info["width"] = parse_svg_length(root.attrib.get("width"))
    info["height"] = parse_svg_length(root.attrib.get("height"))

    raw_rings: List[Tuple[List[Tuple[float, float]], bool]] = []

    def walk(elem: ET.Element, parent_mat: np.ndarray) -> None:
        local = elem.tag.split("}")[-1].lower()
        if local in ("defs", "clippath", "mask", "title", "desc", "metadata", "style"):
            return
        cur_mat = _mat_mul(parent_mat, parse_svg_transform(elem.attrib.get("transform")))
        if local == "path":
            info["element_count"] += 1
            d = elem.attrib.get("d", "")
            try:
                subs = _split_svg_path_d(d)
                for pts, closed in subs:
                    raw_rings.append((apply_affine(pts, cur_mat), closed))
            except Exception as exc:
                info["errors"].append(f"Path parse error: {exc}")
        elif local == "polygon":
            info["element_count"] += 1
            try:
                pts = parse_points_attr(elem.attrib.get("points", ""))
                raw_rings.append((apply_affine(pts, cur_mat), True))
            except Exception as exc:
                info["errors"].append(f"Polygon parse error: {exc}")
        elif local == "polyline":
            info["element_count"] += 1
            try:
                pts = parse_points_attr(elem.attrib.get("points", ""))
                closed = len(pts) >= 3 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) <= 1e-3
                raw_rings.append((apply_affine(pts, cur_mat), closed))
            except Exception as exc:
                info["errors"].append(f"Polyline parse error: {exc}")
        elif local == "rect":
            # Skip obvious full-canvas background rect if fill is white/none? No, record honestly!
            # Wait: single motif SVGs should NOT have background rects; flag if present.
            info["element_count"] += 1
            try:
                x = float(elem.attrib.get("x", 0.0))
                y = float(elem.attrib.get("y", 0.0))
                w = float(elem.attrib.get("width", 0.0))
                h = float(elem.attrib.get("height", 0.0))
                pts = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
                raw_rings.append((apply_affine(pts, cur_mat), True))
            except Exception as exc:
                info["errors"].append(f"Rect parse error: {exc}")
        for child in elem:
            walk(child, cur_mat)

    walk(root, np.eye(3, dtype=np.float64))

    if not raw_rings:
        info["errors"].append("No path/polygon geometry found in SVG")
        return info

    # Build individual polygons from each subpath
    ring_polys: List[Polygon] = []
    for pts, closed in raw_rings:
        if not closed:
            info["unclosed_subpaths"] += 1
        # Close ring explicitly for Shapely if >= 3 unique points
        ring_pts = list(pts)
        if len(ring_pts) >= 3 and math.hypot(ring_pts[0][0] - ring_pts[-1][0], ring_pts[0][1] - ring_pts[-1][1]) > 1e-7:
            ring_pts.append(ring_pts[0])
        if len(ring_pts) < 4:
            info["errors"].append(f"Degenerate subpath with {len(pts)} points")
            continue
        lr = LinearRing(ring_pts)
        poly = Polygon(ring_pts)
        info["subpaths"].append({
            "num_points": len(ring_pts) - 1,
            "is_closed": closed,
            "ring_is_simple": bool(lr.is_simple),
            "poly_is_valid": bool(poly.is_valid),
            "poly_is_simple": bool(poly.is_simple),
            "area": float(poly.area),
        })
        ring_polys.append(poly)

    if not ring_polys:
        return info

    # Determine nesting (holes vs disconnected components)
    # Sort descending by area
    indexed = sorted(enumerate(ring_polys), key=lambda kv: kv[1].area, reverse=True)
    exteriors: List[Polygon] = []
    holes = 0
    for _, poly in indexed:
        if not poly.is_valid or poly.area <= 0:
            exteriors.append(poly)
            continue
        is_hole = False
        for ext in exteriors:
            if ext.is_valid and ext.contains(poly):
                is_hole = True
                holes += 1
                break
        if not is_hole:
            exteriors.append(poly)

    info["polygons"] = exteriors
    info["hole_count"] = holes
    info["connected_components"] = len(exteriors)
    return info


def extract_polygon_json(poly_path: Path) -> Dict[str, Any]:
    """
    Independently parse a processed polygon JSON file.
    Supports common schemas:
      - {"exterior": [[x,y], ...], "interiors": [...]}
      - {"coordinates": [[x,y], ...]} or GeoJSON {"type": "Polygon", "coordinates": [[[x,y], ...]]}
      - {"points": [[x,y], ...]}
      - {"polygon": [[x,y], ...]}
      - raw list [[x,y], ...]
    """
    res: Dict[str, Any] = {
        "valid_json": False,
        "polygon": None,
        "ring_is_simple": False,
        "poly_is_valid": False,
        "poly_is_simple": False,
        "hole_count": 0,
        "connected_components": 0,
        "num_vertices": 0,
        "bounds": None,
        "area": 0.0,
        "errors": [],
    }
    try:
        data = json.loads(poly_path.read_text(encoding="utf-8"))
        res["valid_json"] = True
    except Exception as exc:
        res["errors"].append(f"JSON parse error: {exc}")
        return res

    exterior_coords: Optional[List[Tuple[float, float]]] = None
    interiors_coords: List[List[Tuple[float, float]]] = []

    def _to_pts(seq: Any) -> List[Tuple[float, float]]:
        pts = []
        for item in seq:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                x, y = float(item[0]), float(item[1])
                if not (math.isfinite(x) and math.isfinite(y)):
                    raise ValueError("Non-finite coordinate in polygon JSON")
                pts.append((x, y))
            elif isinstance(item, dict) and "x" in item and "y" in item:
                x, y = float(item["x"]), float(item["y"])
                if not (math.isfinite(x) and math.isfinite(y)):
                    raise ValueError("Non-finite coordinate in polygon JSON")
                pts.append((x, y))
        return pts

    try:
        if isinstance(data, list):
            exterior_coords = _to_pts(data)
        elif isinstance(data, dict):
            if data.get("type") == "Polygon" and isinstance(data.get("coordinates"), list) and data["coordinates"]:
                exterior_coords = _to_pts(data["coordinates"][0])
                for hole in data["coordinates"][1:]:
                    interiors_coords.append(_to_pts(hole))
            elif data.get("type") == "MultiPolygon":
                res["connected_components"] = len(data.get("coordinates", []))
                res["errors"].append(f"GeoJSON MultiPolygon with {res['connected_components']} components")
                return res
            elif "exterior" in data and isinstance(data["exterior"], list):
                exterior_coords = _to_pts(data["exterior"])
                for hole in data.get("interiors", []) or data.get("holes", []) or []:
                    interiors_coords.append(_to_pts(hole))
            elif "exterior_mm" in data and isinstance(data["exterior_mm"], list):
                exterior_coords = _to_pts(data["exterior_mm"])
            elif "points" in data and isinstance(data["points"], list):
                exterior_coords = _to_pts(data["points"])
            elif "polygon" in data and isinstance(data["polygon"], list):
                if data["polygon"] and isinstance(data["polygon"][0], list) and data["polygon"][0] and isinstance(data["polygon"][0][0], (list, tuple)):
                    exterior_coords = _to_pts(data["polygon"][0])
                else:
                    exterior_coords = _to_pts(data["polygon"])
            elif "coordinates" in data and isinstance(data["coordinates"], list) and data["coordinates"]:
                first = data["coordinates"][0]
                if isinstance(first, (list, tuple)) and first and isinstance(first[0], (list, tuple)):
                    exterior_coords = _to_pts(first)
                    for hole in data["coordinates"][1:]:
                        interiors_coords.append(_to_pts(hole))
                else:
                    exterior_coords = _to_pts(data["coordinates"])
            else:
                res["errors"].append(f"Unrecognized polygon JSON keys: {sorted(data.keys())}")
                return res
    except Exception as exc:
        res["errors"].append(f"Coordinate extraction error: {exc}")
        return res

    if not exterior_coords or len(exterior_coords) < 3:
        res["errors"].append("Fewer than 3 exterior vertices in polygon JSON")
        return res

    # Deduplicate consecutive identical vertices
    cleaned: List[Tuple[float, float]] = []
    for p in exterior_coords:
        if not cleaned or math.hypot(p[0] - cleaned[-1][0], p[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(p)
    if len(cleaned) >= 2 and math.hypot(cleaned[0][0] - cleaned[-1][0], cleaned[0][1] - cleaned[-1][1]) <= 1e-9:
        cleaned.pop()
    res["num_vertices"] = len(cleaned)

    if len(cleaned) < 3:
        res["errors"].append("Degenerate ring (<3 unique vertices)")
        return res

    ring_closed = cleaned + [cleaned[0]]
    lr = LinearRing(ring_closed)
    poly = Polygon(ring_closed, interiors_coords if interiors_coords else None)
    res["polygon"] = poly
    res["ring_is_simple"] = bool(lr.is_simple)
    res["poly_is_valid"] = bool(poly.is_valid)
    res["poly_is_simple"] = bool(poly.is_simple)
    res["hole_count"] = len(poly.interiors)
    res["connected_components"] = 1
    res["bounds"] = tuple(float(v) for v in poly.bounds)
    res["area"] = float(poly.area)
    return res


def polygon_to_unit_space(poly: Polygon, viewbox: Optional[Tuple[float, float, float, float]] = None) -> Polygon:
    """Map a polygon to [0, 1] x [0, 1] based on detected coordinate domain (100mm, 256px, or unit)."""
    if viewbox is not None and viewbox[2] > 0 and viewbox[3] > 0:
        vx, vy, vw, vh = viewbox
        coords = [((x - vx) / vw, (y - vy) / vh) for x, y in poly.exterior.coords]
        return Polygon(coords)
    minx, miny, maxx, maxy = poly.bounds
    max_coord = max(abs(minx), abs(miny), abs(maxx), abs(maxy))
    if max_coord <= 1.05 and minx >= -0.05 and miny >= -0.05:
        return poly
    if max_coord <= 105.0 and minx >= -5.0 and miny >= -5.0:
        coords = [(x / 100.0, y / 100.0) for x, y in poly.exterior.coords]
        return Polygon(coords)
    if max_coord <= 260.0 and minx >= -5.0 and miny >= -5.0:
        coords = [(x / 256.0, y / 256.0) for x, y in poly.exterior.coords]
        return Polygon(coords)
    # Fallback: normalize to its own max scale
    span = max(maxx - minx, maxy - miny, 1e-9)
    coords = [((x - minx) / span, (y - miny) / span) for x, y in poly.exterior.coords]
    return Polygon(coords)


def polygon_iou(p1: Polygon, p2: Polygon) -> float:
    if not p1.is_valid:
        p1 = p1.buffer(0)
    if not p2.is_valid:
        p2 = p2.buffer(0)
    u = p1.union(p2).area
    if u <= 0:
        return 0.0
    return float(p1.intersection(p2).area / u)


def rasterize_polygon(poly: Polygon, viewbox: Tuple[float, float, float, float], size: int = 256) -> np.ndarray:
    """Rasterize a polygon defined in SVG viewBox coords onto a (size, size) boolean mask."""
    vx, vy, vw, vh = viewbox
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    pts = [((x - vx) / vw * size, (y - vy) / vh * size) for x, y in poly.exterior.coords]
    if len(pts) >= 3:
        draw.polygon(pts, fill=255)
    return np.asarray(img) > 127


# ---------------------------------------------------------------------------
# Independent Fabrication Erosion Analysis
# ---------------------------------------------------------------------------

def evaluate_fabrication_erosion(poly: Polygon, mask_fg: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """
    Independently test kerf and conservative neck survival via Shapely inward buffering
    (and cross-check on 256x256 mask EDT if provided).

    Physical model (Dossier Section 5 & 5.1):
      - Piece major dimension D_vb = max(bbox_w, bbox_h) scaled to TARGET_PIECE_MM (60 mm).
      - Kerf-only inward offset: r_kerf = 0.075 mm (kerf=0.15 mm).
      - Conservative neck+kerf inward offset: r_neck = (3.0 + 0.15) / 2 = 1.575 mm.
      - Also reports exact critical_neck_mm_at_60mm via binary search: largest diameter
        (2*r) at which `poly.buffer(-r)` remains a single connected polygon.
    """
    minx, miny, maxx, maxy = poly.bounds
    bw = maxx - minx
    bh = maxy - miny
    d_vb = max(bw, bh)
    if d_vb <= 0 or poly.area <= 0:
        return {
            "kerf_pass": False,
            "neck_3mm_pass": False,
            "neck_2mm_pass": False,
            "kerf_components": 0,
            "neck_3mm_components": 0,
            "neck_3mm_survival_ratio": 0.0,
            "neck_2mm_components": 0,
            "critical_neck_mm_at_60mm": 0.0,
            "max_inscribed_mm_at_60mm": 0.0,
            "mask_neck_3mm_components": None,
        }

    mm_to_vb = d_vb / TARGET_PIECE_MM
    vb_to_mm = TARGET_PIECE_MM / d_vb

    def _erosion_stats(r_mm: float) -> Tuple[int, float, float]:
        r_vb = r_mm * mm_to_vb
        er = poly.buffer(-r_vb, quad_segs=12)
        if er.is_empty:
            return 0, 0.0, 0.0
        if isinstance(er, Polygon):
            return 1, float(er.area / poly.area), 1.0
        if isinstance(er, MultiPolygon):
            areas = sorted((g.area for g in er.geoms if g.area > 1e-9), reverse=True)
            if not areas:
                return 0, 0.0, 0.0
            total = sum(areas)
            return len(areas), float(total / poly.area), float(areas[0] / total)
        # GeometryCollection edge case
        polys = [g for g in getattr(er, "geoms", []) if isinstance(g, Polygon) and g.area > 1e-9]
        if not polys:
            return 0, 0.0, 0.0
        areas = sorted((g.area for g in polys), reverse=True)
        total = sum(areas)
        return len(areas), float(total / poly.area), float(areas[0] / total)

    kerf_comp, kerf_ratio, _ = _erosion_stats(KERF_RADIUS_MM)
    neck3_comp, neck3_ratio, neck3_main_share = _erosion_stats(CONSERVATIVE_NECK_RADIUS_MM)
    neck2_comp, neck2_ratio, _ = _erosion_stats(DIAGNOSTIC_2MM_RADIUS_MM)

    # Binary search max inscribed radius (where eroded becomes empty)
    lo_e, hi_e = 0.0, TARGET_PIECE_MM * 0.5
    for _ in range(18):
        mid = 0.5 * (lo_e + hi_e)
        c, _, _ = _erosion_stats(mid)
        if c >= 1:
            lo_e = mid
        else:
            hi_e = mid
    max_inscribed_r_mm = lo_e

    # Scan + binary search critical fracture neck radius:
    # Smallest radius r > 0 where component count != 1 (either >=2 fracture or 0 vanish).
    # Note: component count can go 1 -> 2 -> 1 (if small island vanishes at larger r),
    # so sample a coarse grid first up to max_inscribed_r_mm to find the FIRST fracture!
    probe_grid = np.linspace(0.1, max(max_inscribed_r_mm, 0.2), 25)
    first_bad_r: Optional[float] = None
    last_good_r: float = 0.0
    for r_probe in probe_grid:
        c, _, _ = _erosion_stats(float(r_probe))
        if c == 1:
            last_good_r = float(r_probe)
        else:
            first_bad_r = float(r_probe)
            break

    if first_bad_r is not None:
        lo_c, hi_c = last_good_r, first_bad_r
        for _ in range(14):
            mid = 0.5 * (lo_c + hi_c)
            c, _, _ = _erosion_stats(mid)
            if c == 1:
                lo_c = mid
            else:
                hi_c = mid
        critical_r_mm = lo_c
    else:
        critical_r_mm = max_inscribed_r_mm

    # Optional independent mask-space morphological erosion cross-check
    mask_neck3_comp: Optional[int] = None
    if mask_fg is not None and np.any(mask_fg):
        ys, xs = np.where(mask_fg)
        d_px = float(max(xs.max() - xs.min() + 1, ys.max() - ys.min() + 1))
        r_px = CONSERVATIVE_NECK_RADIUS_MM * (d_px / TARGET_PIECE_MM)
        edt = ndimage.distance_transform_edt(mask_fg)
        eroded_mask = edt > r_px
        _, n_comp = ndimage.label(eroded_mask, structure=np.ones((3, 3), dtype=int))
        mask_neck3_comp = int(n_comp)

    return {
        "kerf_pass": kerf_comp == 1 and kerf_ratio > 0.0,
        "neck_3mm_pass": neck3_comp == 1 and neck3_ratio >= 0.05,
        "neck_2mm_pass": neck2_comp == 1 and neck2_ratio >= 0.05,
        "kerf_components": kerf_comp,
        "kerf_survival_ratio": round(kerf_ratio, 4),
        "neck_3mm_components": neck3_comp,
        "neck_3mm_survival_ratio": round(neck3_ratio, 4),
        "neck_3mm_main_share": round(neck3_main_share, 4),
        "neck_2mm_components": neck2_comp,
        "neck_2mm_survival_ratio": round(neck2_ratio, 4),
        "critical_neck_mm_at_60mm": round(2.0 * critical_r_mm, 3),
        "max_inscribed_mm_at_60mm": round(2.0 * max_inscribed_r_mm, 3),
        "mask_neck_3mm_components": mask_neck3_comp,
        "vb_major_dim_mm": round(d_vb, 3),
        "vb_to_60mm_scale": round(vb_to_mm, 4),
    }


def compute_shape_anomaly_flags(poly: Polygon, motif_class: str) -> List[str]:
    """Flag likely non-motif junk shapes (frames, scale bars, noisy rock blobs) for review."""
    flags: List[str] = []
    area = float(poly.area)
    perim = float(poly.length)
    minx, miny, maxx, maxy = poly.bounds
    bw = maxx - minx
    bh = maxy - miny
    if bw <= 0 or bh <= 0 or area <= 0:
        return ["degenerate_empty_shape"]

    aspect = max(bw / bh, bh / bw)
    hull_area = float(poly.convex_hull.area)
    solidity = area / hull_area if hull_area > 0 else 0.0
    mrr = poly.minimum_rotated_rectangle
    mrr_area = float(mrr.area) if mrr is not None else 0.0
    rectangularity = area / mrr_area if mrr_area > 0 else 0.0
    iso_q = (4.0 * math.pi * area) / (perim * perim) if perim > 0 else 0.0
    complexity = (perim * perim) / (4.0 * math.pi * area) if area > 0 else 999.0
    n_verts = len(poly.exterior.coords) - 1

    if rectangularity > 0.92 and solidity > 0.96:
        flags.append(f"suspected_border_rectangle(rect={rectangularity:.2f},sol={solidity:.2f})")
    if aspect > 8.5:
        flags.append(f"extreme_aspect_ratio({aspect:.1f})")
    if area < 60.0:
        flags.append(f"tiny_viewbox_area({area:.1f}mm2)")
    if area > 9000.0:
        flags.append(f"canvas_filling_blob({area:.0f}mm2)")
    if n_verts < 6:
        flags.append(f"very_low_vertex_count({n_verts})")
    if complexity > 35.0:
        flags.append(f"high_perimeter_noise(P2/4piA={complexity:.1f})")
    if solidity < 0.14:
        flags.append(f"very_low_solidity({solidity:.2f})")
    if iso_q > 0.955 and motif_class not in ("sun_wheel", "geometric_spiral"):
        flags.append(f"near_plain_circle_blob(iso={iso_q:.3f})")

    return flags


# ---------------------------------------------------------------------------
# Gate 1: Provenance & Integrity Gate
# ---------------------------------------------------------------------------

def audit_raw_manifests(root: Path) -> Dict[str, Any]:
    sources_dir = root / "sources"
    manifest_files = sorted(sources_dir.glob("*/manifest.jsonl")) if sources_dir.exists() else []
    report: Dict[str, Any] = {
        "manifest_files": [str(p.relative_to(root)).replace("\\", "/") for p in manifest_files],
        "total_raw_records": 0,
        "valid_raw_records": 0,
        "by_archive": collections.Counter(),
        "by_country": collections.Counter(),
        "by_media_type": collections.Counter(),
        "by_license": collections.Counter(),
        "by_commercial_ok": collections.Counter(),
        "cross_manifest_duplicate_sha256": [],
        "failures": [],
        "warnings": [],
        "index_by_source_id": {},
        "index_by_sha256": collections.defaultdict(list),
        "index_by_raw_path": {},
    }

    if not manifest_files:
        report["failures"].append({"scope": "manifests", "reason": "No sources/*/manifest.jsonl files found"})
        return report

    global_sha_map: Dict[str, List[Tuple[str, str]]] = collections.defaultdict(list)

    for mf in manifest_files:
        rel_mf = str(mf.relative_to(root)).replace("\\", "/")
        local_shas: Dict[str, str] = {}
        local_ids: Set[str] = set()
        lines = mf.read_text(encoding="utf-8").splitlines()
        for line_no, raw_line in enumerate(lines, start=1):
            if not raw_line.strip():
                continue
            report["total_raw_records"] += 1
            try:
                rec = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                report["failures"].append({
                    "scope": rel_mf,
                    "line": line_no,
                    "reason": f"Invalid JSON: {exc}",
                })
                continue

            if not isinstance(rec, dict):
                report["failures"].append({
                    "scope": rel_mf,
                    "line": line_no,
                    "reason": "JSONL line is not an object",
                })
                continue

            sid = str(rec.get("source_id", f"line_{line_no}"))
            rec_ok = True

            # Missing fields check
            missing = sorted(EXPECTED_MANIFEST_FIELDS - set(rec.keys()))
            if missing:
                rec_ok = False
                report["failures"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": f"Missing required manifest fields: {missing}",
                })

            if sid in local_ids:
                rec_ok = False
                report["failures"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": "Duplicate source_id within manifest",
                })
            local_ids.add(sid)

            # Enum checks
            country = rec.get("country")
            if country not in VALID_COUNTRIES:
                rec_ok = False
                report["failures"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": f"Invalid country '{country}' (expected one of {sorted(VALID_COUNTRIES)})",
                })
            period = rec.get("period")
            if period not in VALID_PERIODS:
                rec_ok = False
                report["failures"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": f"Invalid period '{period}'",
                })
            mtype = rec.get("media_type")
            if mtype not in VALID_MEDIA_TYPES:
                rec_ok = False
                report["failures"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": f"Invalid media_type '{mtype}'",
                })
            if not isinstance(rec.get("motif_hints"), list):
                rec_ok = False
                report["failures"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": "motif_hints must be a list",
                })

            # URL checks
            if not is_valid_http_url(rec.get("source_url")):
                rec_ok = False
                report["failures"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": f"Invalid source_url: {rec.get('source_url')!r}",
                })
            dl_url = rec.get("download_url")
            if dl_url and not is_valid_http_url(dl_url):
                report["warnings"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": f"Invalid download_url: {dl_url!r}",
                })

            # Honest license & commercial_ok checks
            lic = rec.get("license")
            comm = rec.get("commercial_ok")
            lic_info = classify_license(lic)
            if not isinstance(comm, bool):
                rec_ok = False
                report["failures"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": f"commercial_ok must be boolean, got {type(comm).__name__}: {comm!r}",
                })
            else:
                if lic_info["is_non_open_or_copyrighted"]:
                    rec_ok = False
                    report["failures"].append({
                        "scope": rel_mf,
                        "source_id": sid,
                        "reason": f"Non-open / copyrighted license ingested: {lic!r} ({lic_info['reason']})",
                    })
                if comm is True and not lic_info["allows_commercial_derivatives"]:
                    rec_ok = False
                    report["failures"].append({
                        "scope": rel_mf,
                        "source_id": sid,
                        "reason": f"Dishonest commercial_ok=True for license {lic!r} ({lic_info['reason']})",
                    })
                if comm is False and lic_info["allows_commercial_derivatives"]:
                    report["warnings"].append({
                        "scope": rel_mf,
                        "source_id": sid,
                        "reason": f"commercial_ok=False despite permissive-looking license {lic!r} (verify if intentional)",
                    })

            # File existence, SHA-256, and integrity check
            raw_path_str = rec.get("raw_path")
            raw_file = resolve_path(root, raw_path_str)
            declared_sha = str(rec.get("sha256", "")).strip().lower()
            if not raw_file or not raw_file.exists():
                rec_ok = False
                report["failures"].append({
                    "scope": rel_mf,
                    "source_id": sid,
                    "reason": f"Missing raw file: {raw_path_str!r}",
                })
            else:
                size = raw_file.stat().st_size
                if size == 0:
                    rec_ok = False
                    report["failures"].append({
                        "scope": rel_mf,
                        "source_id": sid,
                        "reason": f"Empty raw file (0 bytes): {raw_path_str}",
                    })
                else:
                    actual_sha = sha256_file(raw_file)
                    if not re.fullmatch(r"[0-9a-f]{64}", declared_sha):
                        rec_ok = False
                        report["failures"].append({
                            "scope": rel_mf,
                            "source_id": sid,
                            "reason": f"Malformed sha256: {declared_sha!r}",
                        })
                    elif actual_sha != declared_sha:
                        rec_ok = False
                        report["failures"].append({
                            "scope": rel_mf,
                            "source_id": sid,
                            "reason": f"SHA-256 mismatch for {raw_path_str}: declared={declared_sha[:12]} actual={actual_sha[:12]}",
                        })

                    if declared_sha in local_shas:
                        rec_ok = False
                        report["failures"].append({
                            "scope": rel_mf,
                            "source_id": sid,
                            "reason": f"Duplicate SHA-256 within manifest (also {local_shas[declared_sha]})",
                        })
                    else:
                        local_shas[declared_sha] = sid
                    global_sha_map[declared_sha].append((rel_mf, sid))

                    # Verify file is parseable (not corrupt HTML error page / truncated image)
                    suffix = raw_file.suffix.lower()
                    if suffix == ".svg" or mtype == "svg_vector":
                        try:
                            tree = ET.parse(raw_file)
                            rtag = tree.getroot().tag.split("}")[-1].lower()
                            if rtag != "svg":
                                rec_ok = False
                                report["failures"].append({
                                    "scope": rel_mf,
                                    "source_id": sid,
                                    "reason": f"Corrupt SVG (root tag <{rtag}>): {raw_path_str}",
                                })
                        except Exception as exc:
                            rec_ok = False
                            report["failures"].append({
                                "scope": rel_mf,
                                "source_id": sid,
                                "reason": f"Corrupt/unparseable SVG {raw_path_str}: {exc}",
                            })
                    elif suffix in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".gif", ".bmp"):
                        try:
                            with Image.open(raw_file) as im:
                                w, h = im.size
                                if w <= 0 or h <= 0:
                                    rec_ok = False
                                    report["failures"].append({
                                        "scope": rel_mf,
                                        "source_id": sid,
                                        "reason": f"Zero-dimension raster image: {raw_path_str}",
                                    })
                                else:
                                    if w * h > 40_000_000:
                                        im.verify()
                                    else:
                                        im.load()
                                mw, mh = rec.get("width_px"), rec.get("height_px")
                                if isinstance(mw, (int, float)) and isinstance(mh, (int, float)):
                                    if mw > 0 and mh > 0 and (abs(mw - w) > 2 or abs(mh - h) > 2):
                                        report["warnings"].append({
                                            "scope": rel_mf,
                                            "source_id": sid,
                                            "reason": f"Declared dims ({mw}x{mh}) != actual ({w}x{h})",
                                        })
                        except Exception as exc:
                            rec_ok = False
                            report["failures"].append({
                                "scope": rel_mf,
                                "source_id": sid,
                                "reason": f"Corrupt/unreadable raster {raw_path_str}: {exc}",
                            })
                    else:
                        report["warnings"].append({
                            "scope": rel_mf,
                            "source_id": sid,
                            "reason": f"Unexpected raw file extension '{suffix}': {raw_path_str}",
                        })

            if rec_ok:
                report["valid_raw_records"] += 1

            report["by_archive"][str(rec.get("source_archive", "unknown"))] += 1
            report["by_country"][str(country)] += 1
            report["by_media_type"][str(mtype)] += 1
            report["by_license"][str(lic)] += 1
            report["by_commercial_ok"][str(comm)] += 1

            # Store indices for cross-checking against processed/catalog.json
            rec_copy = dict(rec)
            rec_copy["_manifest"] = rel_mf
            report["index_by_source_id"][sid] = rec_copy
            if declared_sha:
                report["index_by_sha256"][declared_sha].append(rec_copy)
            if isinstance(raw_path_str, str):
                norm_rp = raw_path_str.replace("\\", "/")
                report["index_by_raw_path"][norm_rp] = rec_copy

    for sha, entries in global_sha_map.items():
        if len(entries) > 1:
            manifests_hit = sorted({m for m, _ in entries})
            if len(manifests_hit) > 1:
                report["cross_manifest_duplicate_sha256"].append({
                    "sha256": sha,
                    "entries": entries,
                })

    return report


# ---------------------------------------------------------------------------
# Load Taxonomy & Catalog Helpers
# ---------------------------------------------------------------------------

def load_taxonomy(root: Path) -> Tuple[Dict[str, Set[str]], List[str]]:
    tax_path = root / "taxonomy.json"
    errors: List[str] = []
    valid_map: Dict[str, Set[str]] = {}
    if not tax_path.exists():
        errors.append("Missing hellerisninger/taxonomy.json")
        return valid_map, errors
    try:
        tax = json.loads(tax_path.read_text(encoding="utf-8"))
        for fam, fobj in tax.get("families", {}).items():
            valid_map[fam] = set(fobj.get("classes", {}).keys())
    except Exception as exc:
        errors.append(f"Failed to parse taxonomy.json: {exc}")
    return valid_map, errors


def load_catalog(root: Path) -> Tuple[Optional[List[Dict[str, Any]]], List[str]]:
    cat_path = root / "processed" / "catalog.json"
    errors: List[str] = []
    if not cat_path.exists():
        errors.append("Missing processed/catalog.json")
        return None, errors
    try:
        raw = json.loads(cat_path.read_text(encoding="utf-8"))
    except Exception as exc:
        errors.append(f"Invalid JSON in processed/catalog.json: {exc}")
        return None, errors

    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        for key in ("records", "assets", "items", "motifs", "catalog"):
            if isinstance(raw.get(key), list):
                records = raw[key]
                break
        else:
            errors.append(f"Unexpected dict structure in catalog.json (keys={sorted(raw.keys())})")
            return None, errors
    else:
        errors.append(f"catalog.json root must be list or dict, got {type(raw).__name__}")
        return None, errors
    return records, errors


# ---------------------------------------------------------------------------
# Main Full Corpus Verification
# ---------------------------------------------------------------------------

def run_verification(root: Path) -> Dict[str, Any]:
    timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
    taxonomy_map, tax_errors = load_taxonomy(root)
    raw_audit = audit_raw_manifests(root)

    # Gate structures
    gates: Dict[str, Dict[str, Any]] = {
        "provenance_and_integrity": {"passed": False, "failures": [], "warnings": [], "metrics": {}},
        "split_hygiene": {"passed": False, "failures": [], "warnings": [], "metrics": {}},
        "geometry_and_svg_reimport": {"passed": False, "failures": [], "warnings": [], "metrics": {}},
        "mask_and_sdf_consistency": {"passed": False, "failures": [], "warnings": [], "metrics": {}},
        "fabrication_neck_and_kerf": {"passed": False, "failures": [], "warnings": [], "metrics": {}},
    }

    gates["provenance_and_integrity"]["failures"].extend(raw_audit["failures"])
    gates["provenance_and_integrity"]["warnings"].extend(raw_audit["warnings"])
    for te in tax_errors:
        gates["provenance_and_integrity"]["failures"].append({"scope": "taxonomy.json", "reason": te})

    records, cat_errors = load_catalog(root)
    if cat_errors or records is None:
        for ce in cat_errors:
            gates["provenance_and_integrity"]["failures"].append({"scope": "processed/catalog.json", "reason": ce})
        return {
            "timestamp_utc": timestamp,
            "root": str(root),
            "overall_passed": False,
            "corpus_ready": False,
            "gates": gates,
            "raw_summary": _clean_raw_summary(raw_audit),
            "processed_summary": {},
            "shape_anomalies": [],
        }

    if len(records) == 0:
        gates["provenance_and_integrity"]["failures"].append({
            "scope": "processed/catalog.json",
            "reason": "processed/catalog.json contains 0 records",
        })

    # Validate catalog.csv parity
    csv_path = root / "processed" / "catalog.csv"
    if not csv_path.exists():
        gates["provenance_and_integrity"]["failures"].append({
            "scope": "processed/catalog.csv",
            "reason": "Missing processed/catalog.csv",
        })
    else:
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as f:
                csv_rows = list(csv.DictReader(f))
            json_ids = [str(r.get("asset_id", "")) for r in records]
            csv_ids = [str(r.get("asset_id", "")) for r in csv_rows]
            if len(csv_rows) != len(records) or set(csv_ids) != set(json_ids):
                gates["provenance_and_integrity"]["failures"].append({
                    "scope": "processed/catalog.csv",
                    "reason": f"catalog.csv mismatch vs catalog.json (csv={len(csv_rows)}, json={len(records)})",
                })
        except Exception as exc:
            gates["provenance_and_integrity"]["failures"].append({
                "scope": "processed/catalog.csv",
                "reason": f"Failed to parse catalog.csv: {exc}",
            })

    # Validate stats.json exists
    stats_path = root / "processed" / "stats.json"
    if not stats_path.exists():
        gates["provenance_and_integrity"]["failures"].append({
            "scope": "processed/stats.json",
            "reason": "Missing processed/stats.json",
        })
    else:
        try:
            stats_obj = json.loads(stats_path.read_text(encoding="utf-8"))
            if not isinstance(stats_obj, dict):
                gates["provenance_and_integrity"]["failures"].append({
                    "scope": "processed/stats.json",
                    "reason": "stats.json is not a JSON object",
                })
        except Exception as exc:
            gates["provenance_and_integrity"]["failures"].append({
                "scope": "processed/stats.json",
                "reason": f"Failed to parse stats.json: {exc}",
            })

    # Cache recomputed raw SHA-256 so each source file is hashed once
    sha_cache: Dict[Path, str] = {}

    def _cached_sha(p: Path) -> str:
        if p not in sha_cache:
            sha_cache[p] = sha256_file(p)
        return sha_cache[p]

    # Trackers across catalog
    seen_asset_ids: Set[str] = set()
    split_groups: Dict[str, Set[str]] = {s: set() for s in VALID_SPLITS}
    split_shas: Dict[str, Set[str]] = {s: set() for s in VALID_SPLITS}
    split_source_ids: Dict[str, Set[str]] = {s: set() for s in VALID_SPLITS}
    split_mask_hashes: Dict[str, Set[str]] = {s: set() for s in VALID_SPLITS}
    all_mask_hashes: Dict[str, List[str]] = collections.defaultdict(list)

    # Summary counters
    counts_tier = collections.Counter()
    counts_family = collections.Counter()
    counts_class = collections.Counter()
    counts_country = collections.Counter()
    counts_region = collections.Counter()
    counts_archive = collections.Counter()
    counts_license = collections.Counter()
    counts_commercial = collections.Counter()
    counts_split = collections.Counter()
    counts_render_style = collections.Counter()
    counts_media_type = collections.Counter()

    # Mask convention consistency tracker
    mask_bg_values: Set[int] = set()
    sdf_unit_scales: List[float] = []

    # Per-asset detailed metrics & anomalies
    shape_anomalies: List[Dict[str, Any]] = []
    fab_metrics_by_tier: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    geom_checked = 0
    geom_cuttable_checked = 0
    mask_sdf_checked = 0
    fab_cuttable_checked = 0

    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            gates["provenance_and_integrity"]["failures"].append({
                "scope": "catalog.json",
                "index": idx,
                "reason": "Catalog record is not a JSON object",
            })
            continue

        aid = str(rec.get("asset_id") or f"index_{idx}")
        if aid in seen_asset_ids:
            gates["provenance_and_integrity"]["failures"].append({
                "asset_id": aid,
                "reason": "Duplicate asset_id in catalog.json",
            })
        seen_asset_ids.add(aid)

        tier = str(rec.get("jigsaw_tier", ""))
        is_cuttable = tier in CUTTABLE_TIERS
        fam = str(rec.get("family", ""))
        mcls = str(rec.get("motif_class") or rec.get("common_name") or "")
        rstyle = str(rec.get("render_style", ""))
        country = str(rec.get("country", "unknown"))
        region = str(rec.get("region", "unknown"))
        archive = str(rec.get("source_archive", "unknown"))
        lic = rec.get("license")
        comm = rec.get("commercial_ok")
        split = str(rec.get("split", ""))
        src_group = str(rec.get("source_group", "")).strip()
        src_id = str(rec.get("source_id", "")).strip()
        decl_sha = str(rec.get("sha256", "")).strip().lower()
        mtype = str(rec.get("media_type", "unknown"))

        counts_tier[tier] += 1
        counts_family[fam] += 1
        counts_class[f"{fam}/{mcls}"] += 1
        counts_country[country] += 1
        counts_region[f"{country}:{region}"] += 1
        counts_archive[archive] += 1
        counts_license[str(lic)] += 1
        counts_commercial[str(comm)] += 1
        counts_split[split] += 1
        counts_render_style[rstyle] += 1
        counts_media_type[mtype] += 1

        # Taxonomy & enum validation
        if tier not in VALID_TIERS:
            gates["provenance_and_integrity"]["failures"].append({
                "asset_id": aid,
                "reason": f"Invalid jigsaw_tier {tier!r}",
            })
        if rstyle not in VALID_RENDER_STYLES:
            gates["provenance_and_integrity"]["failures"].append({
                "asset_id": aid,
                "reason": f"Invalid render_style {rstyle!r}",
            })
        if taxonomy_map:
            if fam not in taxonomy_map:
                gates["provenance_and_integrity"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"Family {fam!r} not in taxonomy.json",
                })
            elif mcls not in taxonomy_map[fam]:
                gates["provenance_and_integrity"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"motif_class {mcls!r} not in taxonomy.json family {fam!r}",
                })

        # --- Gate 1: Catalog Provenance & Integrity ---
        if not is_valid_http_url(rec.get("source_url")):
            gates["provenance_and_integrity"]["failures"].append({
                "asset_id": aid,
                "reason": f"Invalid source_url: {rec.get('source_url')!r}",
            })

        lic_info = classify_license(lic)
        if not isinstance(comm, bool):
            gates["provenance_and_integrity"]["failures"].append({
                "asset_id": aid,
                "reason": f"commercial_ok is not boolean: {comm!r}",
            })
        else:
            if lic_info["is_non_open_or_copyrighted"]:
                gates["provenance_and_integrity"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"Non-open/copyrighted license in catalog: {lic!r}",
                })
            if comm is True and not lic_info["allows_commercial_derivatives"]:
                gates["provenance_and_integrity"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"Dishonest commercial_ok=True for license {lic!r} ({lic_info['reason']})",
                })

        # Locate raw file and verify SHA-256 + manifest consistency
        raw_path_field = rec.get("raw_path") or rec.get("source_raw_path")
        manifest_match = None
        if isinstance(raw_path_field, str):
            manifest_match = raw_audit["index_by_raw_path"].get(raw_path_field.replace("\\", "/"))
        if manifest_match is None and src_id:
            manifest_match = raw_audit["index_by_source_id"].get(src_id)
        if manifest_match is None and decl_sha in raw_audit["index_by_sha256"]:
            manifest_match = raw_audit["index_by_sha256"][decl_sha][0]

        if raw_path_field is None and manifest_match is not None:
            raw_path_field = manifest_match.get("raw_path")

        raw_file = resolve_path(root, raw_path_field)
        if not raw_file or not raw_file.exists():
            gates["provenance_and_integrity"]["failures"].append({
                "asset_id": aid,
                "reason": f"Referenced raw file missing: {raw_path_field!r}",
            })
        else:
            if raw_file.stat().st_size == 0:
                gates["provenance_and_integrity"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"Referenced raw file is empty: {raw_path_field}",
                })
            else:
                actual_sha = _cached_sha(raw_file)
                if not decl_sha or actual_sha != decl_sha:
                    gates["provenance_and_integrity"]["failures"].append({
                        "asset_id": aid,
                        "reason": f"Catalog SHA-256 mismatch vs raw file {raw_path_field}: catalog={decl_sha[:12]} actual={actual_sha[:12]}",
                    })

        if manifest_match is None:
            gates["provenance_and_integrity"]["failures"].append({
                "asset_id": aid,
                "reason": f"Catalog record does not trace back to any raw manifest entry (source_id={src_id!r}, raw_path={raw_path_field!r})",
            })
        else:
            m_comm = manifest_match.get("commercial_ok")
            if m_comm is False and comm is True:
                gates["provenance_and_integrity"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"Catalog upgraded commercial_ok from False (manifest license={manifest_match.get('license')!r}) to True!",
                })
            if decl_sha and str(manifest_match.get("sha256", "")).lower() != decl_sha:
                gates["provenance_and_integrity"]["failures"].append({
                    "asset_id": aid,
                    "reason": "Catalog sha256 disagrees with raw manifest sha256",
                })

        # Resolve processed artifact files
        svg_file = resolve_path(root, rec.get("svg_path"), subdirs=("processed",))
        mask_file = resolve_path(root, rec.get("mask_path"), subdirs=("processed",))
        poly_file = resolve_path(root, rec.get("polygon_path"), subdirs=("processed",))
        sdf_file = resolve_path(root, rec.get("sdf_path"), subdirs=("processed",))

        for label, fpath in (("svg_path", svg_file), ("mask_path", mask_file), ("polygon_path", poly_file), ("sdf_path", sdf_file)):
            if not fpath or not fpath.exists() or fpath.stat().st_size == 0:
                gates["provenance_and_integrity"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"Missing or empty {label}: {rec.get(label)!r}",
                })

        # --- Gate 2: Split Hygiene collection ---
        if not src_group:
            gates["split_hygiene"]["failures"].append({
                "asset_id": aid,
                "reason": "Missing or empty source_group",
            })
        if split in VALID_SPLITS:
            if src_group:
                split_groups[split].add(src_group)
            if decl_sha:
                split_shas[split].add(decl_sha)
            if src_id:
                split_source_ids[split].add(src_id)
        else:
            if is_cuttable:
                gates["split_hygiene"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"Cuttable tier {tier} has invalid split {split!r} (expected train/val/test)",
                })
            elif split not in ("review", "holdout", "unassigned", ""):
                gates["split_hygiene"]["warnings"].append({
                    "asset_id": aid,
                    "reason": f"Tier C record has non-standard split {split!r}",
                })

        # --- Gate 3: Geometry & SVG Re-Import ---
        svg_poly: Optional[Polygon] = None
        svg_vb: Optional[Tuple[float, float, float, float]] = None

        if svg_file and svg_file.exists() and svg_file.stat().st_size > 0:
            geom_checked += 1
            if is_cuttable:
                geom_cuttable_checked += 1
            svg_info = extract_svg_geometry(svg_file)
            svg_vb = svg_info["viewbox"]

            if svg_info["errors"]:
                target_list = gates["geometry_and_svg_reimport"]["failures"] if is_cuttable else gates["geometry_and_svg_reimport"]["warnings"]
                target_list.append({
                    "asset_id": aid,
                    "tier": tier,
                    "file": str(svg_file.relative_to(root)).replace("\\", "/"),
                    "reason": f"SVG parse errors: {svg_info['errors']}",
                })
            else:
                # ViewBox check (100x100 mm)
                if svg_vb is None:
                    target_list = gates["geometry_and_svg_reimport"]["failures"] if is_cuttable else gates["geometry_and_svg_reimport"]["warnings"]
                    target_list.append({
                        "asset_id": aid,
                        "tier": tier,
                        "reason": "SVG missing viewBox attribute",
                    })
                else:
                    vx, vy, vw, vh = svg_vb
                    if abs(vx) > 1e-3 or abs(vy) > 1e-3 or abs(vw - EXPECTED_VIEWBOX_SIZE) > 1e-2 or abs(vh - EXPECTED_VIEWBOX_SIZE) > 1e-2:
                        target_list = gates["geometry_and_svg_reimport"]["failures"] if is_cuttable else gates["geometry_and_svg_reimport"]["warnings"]
                        target_list.append({
                            "asset_id": aid,
                            "tier": tier,
                            "reason": f"SVG viewBox {svg_vb} != (0, 0, 100, 100)",
                        })

                if is_cuttable:
                    if svg_info["unclosed_subpaths"] > 0:
                        gates["geometry_and_svg_reimport"]["failures"].append({
                            "asset_id": aid,
                            "tier": tier,
                            "reason": f"SVG has {svg_info['unclosed_subpaths']} unclosed subpath(s)",
                        })
                    if svg_info["connected_components"] != 1:
                        gates["geometry_and_svg_reimport"]["failures"].append({
                            "asset_id": aid,
                            "tier": tier,
                            "reason": f"SVG has connected_components={svg_info['connected_components']} (expected 1)",
                        })
                    if svg_info["hole_count"] != 0:
                        gates["geometry_and_svg_reimport"]["failures"].append({
                            "asset_id": aid,
                            "tier": tier,
                            "reason": f"SVG has hole_count={svg_info['hole_count']} (expected 0)",
                        })
                    for sp_i, sp in enumerate(svg_info["subpaths"]):
                        if not sp["ring_is_simple"] or not sp["poly_is_valid"] or not sp["poly_is_simple"]:
                            gates["geometry_and_svg_reimport"]["failures"].append({
                                "asset_id": aid,
                                "tier": tier,
                                "reason": (
                                    f"SVG subpath[{sp_i}] invalid/self-intersecting "
                                    f"(ring_simple={sp['ring_is_simple']}, poly_valid={sp['poly_is_valid']}, poly_simple={sp['poly_is_simple']})"
                                ),
                            })
                        if sp["area"] <= 0:
                            gates["geometry_and_svg_reimport"]["failures"].append({
                                "asset_id": aid,
                                "tier": tier,
                                "reason": f"SVG subpath[{sp_i}] non-positive area ({sp['area']})",
                            })

                if svg_info["polygons"]:
                    svg_poly = max(svg_info["polygons"], key=lambda p: p.area)
                    minx, miny, maxx, maxy = svg_poly.bounds
                    if is_cuttable:
                        if minx < -0.5 or miny < -0.5 or maxx > EXPECTED_VIEWBOX_SIZE + 0.5 or maxy > EXPECTED_VIEWBOX_SIZE + 0.5:
                            gates["geometry_and_svg_reimport"]["failures"].append({
                                "asset_id": aid,
                                "tier": tier,
                                "reason": f"SVG polygon bounds ({minx:.2f},{miny:.2f},{maxx:.2f},{maxy:.2f}) exceed 100x100 mm viewBox",
                            })
                        if svg_poly.area < 15.0:
                            gates["geometry_and_svg_reimport"]["failures"].append({
                                "asset_id": aid,
                                "tier": tier,
                                "reason": f"SVG polygon area suspiciously tiny ({svg_poly.area:.2f} mm2 in 100x100 mm viewBox)",
                            })

                    # Shape anomaly pre-screening
                    anoms = compute_shape_anomaly_flags(svg_poly, mcls)
                    if anoms:
                        shape_anomalies.append({
                            "asset_id": aid,
                            "tier": tier,
                            "family": fam,
                            "motif_class": mcls,
                            "site": rec.get("site"),
                            "source_archive": archive,
                            "flags": anoms,
                        })

        # Parse polygons/<asset_id>.json independently and cross-check with SVG
        if poly_file and poly_file.exists() and poly_file.stat().st_size > 0:
            pj = extract_polygon_json(poly_file)
            if pj["errors"]:
                target_list = gates["geometry_and_svg_reimport"]["failures"] if is_cuttable else gates["geometry_and_svg_reimport"]["warnings"]
                target_list.append({
                    "asset_id": aid,
                    "tier": tier,
                    "file": str(poly_file.relative_to(root)).replace("\\", "/"),
                    "reason": f"Polygon JSON errors: {pj['errors']}",
                })
            elif is_cuttable:
                if not pj["ring_is_simple"] or not pj["poly_is_valid"] or not pj["poly_is_simple"]:
                    gates["geometry_and_svg_reimport"]["failures"].append({
                        "asset_id": aid,
                        "tier": tier,
                        "reason": (
                            f"Polygon JSON invalid/self-intersecting "
                            f"(ring_simple={pj['ring_is_simple']}, valid={pj['poly_is_valid']}, simple={pj['poly_is_simple']})"
                        ),
                    })
                if pj["connected_components"] != 1 or pj["hole_count"] != 0:
                    gates["geometry_and_svg_reimport"]["failures"].append({
                        "asset_id": aid,
                        "tier": tier,
                        "reason": f"Polygon JSON topology mismatch (cc={pj['connected_components']}, holes={pj['hole_count']})",
                    })
                if pj["area"] <= 0:
                    gates["geometry_and_svg_reimport"]["failures"].append({
                        "asset_id": aid,
                        "tier": tier,
                        "reason": f"Polygon JSON non-positive area ({pj['area']})",
                    })
                if svg_poly is not None and pj["polygon"] is not None:
                    u_svg = polygon_to_unit_space(svg_poly, svg_vb or (0.0, 0.0, 100.0, 100.0))
                    u_json = polygon_to_unit_space(pj["polygon"])
                    iou_svg_json = polygon_iou(u_svg, u_json)
                    if iou_svg_json < 0.95:
                        gates["geometry_and_svg_reimport"]["failures"].append({
                            "asset_id": aid,
                            "tier": tier,
                            "reason": f"SVG vs Polygon JSON geometry divergence (IoU={iou_svg_json:.4f} < 0.95)",
                        })

        # Cross-check catalog metadata topology claims for cuttable tiers
        if is_cuttable:
            if rec.get("connected_components") != 1:
                gates["geometry_and_svg_reimport"]["failures"].append({
                    "asset_id": aid,
                    "tier": tier,
                    "reason": f"Catalog connected_components={rec.get('connected_components')} != 1",
                })
            if rec.get("hole_count") != 0:
                gates["geometry_and_svg_reimport"]["failures"].append({
                    "asset_id": aid,
                    "tier": tier,
                    "reason": f"Catalog hole_count={rec.get('hole_count')} != 0",
                })

        # --- Gate 4: Mask & SDF Consistency ---
        mask_fg: Optional[np.ndarray] = None
        if mask_file and mask_file.exists() and mask_file.stat().st_size > 0:
            mask_sdf_checked += 1
            try:
                with Image.open(mask_file) as im:
                    arr = np.asarray(im)
                if arr.ndim == 3:
                    # Check if all RGB channels equal
                    if arr.shape[2] >= 3 and np.all(arr[..., 0] == arr[..., 1]) and np.all(arr[..., 0] == arr[..., 2]):
                        arr_2d = arr[..., 0]
                    else:
                        gates["mask_and_sdf_consistency"]["failures"].append({
                            "asset_id": aid,
                            "reason": f"Mask PNG is non-grayscale color image with shape {arr.shape}",
                        })
                        arr_2d = None
                elif arr.ndim == 2:
                    arr_2d = arr
                else:
                    gates["mask_and_sdf_consistency"]["failures"].append({
                        "asset_id": aid,
                        "reason": f"Mask PNG has unexpected shape {arr.shape}",
                    })
                    arr_2d = None

                if arr_2d is not None:
                    if arr_2d.shape != (EXPECTED_MASK_SIZE, EXPECTED_MASK_SIZE):
                        gates["mask_and_sdf_consistency"]["failures"].append({
                            "asset_id": aid,
                            "reason": f"Mask shape {arr_2d.shape} != ({EXPECTED_MASK_SIZE}, {EXPECTED_MASK_SIZE})",
                        })
                    else:
                        uniq = np.unique(arr_2d)
                        # Check binary values (allow bool / {0,1} / {0,255})
                        if not (set(uniq.tolist()).issubset({0, 255}) or set(uniq.tolist()).issubset({0, 1})):
                            gates["mask_and_sdf_consistency"]["warnings"].append({
                                "asset_id": aid,
                                "reason": f"Mask is not strictly binary (unique values count={len(uniq)}, sample={uniq[:6].tolist()})",
                            })
                        # Infer background from border pixels (padded silhouette)
                        border = np.concatenate([arr_2d[0, :], arr_2d[-1, :], arr_2d[:, 0], arr_2d[:, -1]])
                        bg_val = int(np.median(border))
                        bg_bin = 255 if bg_val > 127 else 0
                        mask_bg_values.add(bg_bin)
                        if bg_bin == 0:
                            mask_fg = arr_2d > 127
                        else:
                            mask_fg = arr_2d < 128

                        # Track exact processed mask hash for cross-split duplicate detection
                        mhash = hashlib.sha256(np.packbits(mask_fg.astype(np.uint8)).tobytes()).hexdigest()
                        all_mask_hashes[mhash].append(aid)
                        if split in VALID_SPLITS:
                            split_mask_hashes[split].add(mhash)

                        fg_frac = float(mask_fg.mean())
                        if fg_frac < 0.002 or fg_frac > 0.95:
                            target_list = gates["mask_and_sdf_consistency"]["failures"] if is_cuttable else gates["mask_and_sdf_consistency"]["warnings"]
                            target_list.append({
                                "asset_id": aid,
                                "tier": tier,
                                "reason": f"Mask foreground ratio out of range: {fg_frac:.4f}",
                            })

                        # Check mask connected components & holes independently
                        _, cc8 = ndimage.label(mask_fg, structure=np.ones((3, 3), dtype=int))
                        _, cc4 = ndimage.label(mask_fg, structure=np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=int))
                        filled = ndimage.binary_fill_holes(mask_fg)
                        hole_px = int(np.sum(filled & (~mask_fg)))

                        if is_cuttable:
                            if cc8 != 1:
                                gates["mask_and_sdf_consistency"]["failures"].append({
                                    "asset_id": aid,
                                    "tier": tier,
                                    "reason": f"Mask has {cc8} 8-connected components (expected 1)",
                                })
                            elif cc4 != 1:
                                gates["mask_and_sdf_consistency"]["warnings"].append({
                                    "asset_id": aid,
                                    "tier": tier,
                                    "reason": f"Mask is 8-connected (1) but has {cc4} 4-connected components (diagonal 1-px pinch)",
                                })
                            if hole_px > 0:
                                gates["mask_and_sdf_consistency"]["failures"].append({
                                    "asset_id": aid,
                                    "tier": tier,
                                    "reason": f"Mask has interior hole(s) totaling {hole_px} pixels (expected 0)",
                                })

                        # Cross-check SVG polygon alignment vs mask (catches Y-axis flip / translation bugs)
                        if svg_poly is not None and svg_poly.is_valid and is_cuttable:
                            vb = svg_vb or (0.0, 0.0, EXPECTED_VIEWBOX_SIZE, EXPECTED_VIEWBOX_SIZE)
                            r_svg = rasterize_polygon(svg_poly, vb, EXPECTED_MASK_SIZE)
                            inter = int(np.sum(r_svg & mask_fg))
                            union = int(np.sum(r_svg | mask_fg))
                            iou_sm = (inter / union) if union > 0 else 0.0
                            if iou_sm < 0.85:
                                # Test Y-flip hypothesis for clear diagnosis
                                r_flip = r_svg[::-1, :]
                                inter_f = int(np.sum(r_flip & mask_fg))
                                union_f = int(np.sum(r_flip | mask_fg))
                                iou_flip = (inter_f / union_f) if union_f > 0 else 0.0
                                hint = f" (Y-flipped IoU={iou_flip:.3f} -> LIKELY Y-AXIS FLIP!)" if iou_flip > iou_sm + 0.15 else ""
                                gates["mask_and_sdf_consistency"]["failures"].append({
                                    "asset_id": aid,
                                    "tier": tier,
                                    "reason": f"SVG-vs-Mask raster alignment mismatch (IoU={iou_sm:.3f} < 0.85){hint}",
                                })
            except Exception as exc:
                gates["mask_and_sdf_consistency"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"Failed to load mask PNG: {exc}",
                })

        # Load & verify SDF .npz
        if sdf_file and sdf_file.exists() and sdf_file.stat().st_size > 0:
            try:
                with np.load(sdf_file) as npz:
                    if "sdf" in npz.files:
                        sdf = npz["sdf"]
                    elif len(npz.files) == 1:
                        sdf = npz[npz.files[0]]
                        gates["mask_and_sdf_consistency"]["warnings"].append({
                            "asset_id": aid,
                            "reason": f"SDF npz uses key {npz.files[0]!r} instead of 'sdf'",
                        })
                    else:
                        gates["mask_and_sdf_consistency"]["failures"].append({
                            "asset_id": aid,
                            "reason": f"SDF npz missing 'sdf' key (found {npz.files})",
                        })
                        sdf = None

                if sdf is not None:
                    if sdf.shape != (EXPECTED_MASK_SIZE, EXPECTED_MASK_SIZE):
                        gates["mask_and_sdf_consistency"]["failures"].append({
                            "asset_id": aid,
                            "reason": f"SDF shape {sdf.shape} != ({EXPECTED_MASK_SIZE}, {EXPECTED_MASK_SIZE})",
                        })
                    elif not np.issubdtype(sdf.dtype, np.floating):
                        gates["mask_and_sdf_consistency"]["failures"].append({
                            "asset_id": aid,
                            "reason": f"SDF dtype {sdf.dtype} is not floating-point",
                        })
                    elif not np.all(np.isfinite(sdf)):
                        gates["mask_and_sdf_consistency"]["failures"].append({
                            "asset_id": aid,
                            "reason": "SDF contains NaN or Inf values",
                        })
                    elif mask_fg is not None:
                        inside_vals = sdf[mask_fg]
                        outside_vals = sdf[~mask_fg]
                        inside_nonneg = int(np.sum(inside_vals >= 0.0))
                        outside_nonpos = int(np.sum(outside_vals <= 0.0))

                        # Check strictly negative inside, strictly positive outside
                        if inside_nonneg > 0 or outside_nonpos > 0:
                            # Diagnose whether flipped sign vs boundary zeros vs mismatch
                            inside_pos = int(np.sum(inside_vals > 0.0))
                            outside_neg = int(np.sum(outside_vals < 0.0))
                            extra = ""
                            if inside_pos > inside_vals.size * 0.5 and outside_neg > outside_vals.size * 0.5:
                                extra = " [SIGN INVERTED: positive inside, negative outside]"
                            gates["mask_and_sdf_consistency"]["failures"].append({
                                "asset_id": aid,
                                "tier": tier,
                                "reason": (
                                    f"SDF sign violation: inside>=0 ({inside_nonneg}/{inside_vals.size}, max_in={float(inside_vals.max()):.5f}), "
                                    f"outside<=0 ({outside_nonpos}/{outside_vals.size}, min_out={float(outside_vals.min()):.5f}){extra}"
                                ),
                            })

                        # Check boundary gradient magnitude & correlation with Euclidean distance transform
                        gy, gx = np.gradient(sdf.astype(np.float64))
                        gmag = np.hypot(gx, gy)
                        edt_out = ndimage.distance_transform_edt(~mask_fg)
                        edt_in = ndimage.distance_transform_edt(mask_fg)
                        # Clean near-boundary shell (2..5 px outside, away from jump discontinuity and corners)
                        shell_out = (edt_out >= 2.0) & (edt_out <= 6.0)
                        interface_band = (edt_out == 1.0) | (edt_in == 1.0)

                        if np.any(shell_out):
                            med_grad = float(np.median(gmag[shell_out]))
                            sdf_unit_scales.append(med_grad)
                            # Sensible scales: pixel (1.0), mm (100/256=0.3906), unit (1/256=0.003906), [-1,1] (2/256=0.0078125), or 1/diag
                            if med_grad <= 1e-6 or med_grad > 10.0:
                                gates["mask_and_sdf_consistency"]["failures"].append({
                                    "asset_id": aid,
                                    "reason": f"Abnormal SDF near-boundary gradient magnitude: median={med_grad:.6f}",
                                })
                            else:
                                # Check gradient uniformity near boundary (relative to med_grad)
                                p10, p90 = np.percentile(gmag[shell_out], [10, 90])
                                if p10 < 0.5 * med_grad or p90 > 1.5 * med_grad:
                                    gates["mask_and_sdf_consistency"]["warnings"].append({
                                        "asset_id": aid,
                                        "reason": f"Noisy SDF boundary gradient spread: med={med_grad:.5f}, p10={p10:.5f}, p90={p90:.5f}",
                                    })
                                # Interface gradient shouldn't collapse to 0 or explode (> 2.5x shell)
                                if np.any(interface_band):
                                    med_iface = float(np.median(gmag[interface_band]))
                                    if med_iface < 0.5 * med_grad or med_iface > 2.5 * med_grad:
                                        gates["mask_and_sdf_consistency"]["failures"].append({
                                            "asset_id": aid,
                                            "reason": f"Abnormal SDF interface gradient: iface_med={med_iface:.5f} vs shell_med={med_grad:.5f}",
                                        })
                                # Compare correlation with exact Euclidean reference SDF
                                ref_sdf = edt_out - edt_in
                                corr = float(np.corrcoef(sdf.ravel(), ref_sdf.ravel())[0, 1])
                                if not math.isfinite(corr) or corr < 0.98:
                                    gates["mask_and_sdf_consistency"]["failures"].append({
                                        "asset_id": aid,
                                        "reason": f"SDF diverges from exact Euclidean distance transform (Pearson r={corr:.4f} < 0.98)",
                                    })
            except Exception as exc:
                gates["mask_and_sdf_consistency"]["failures"].append({
                    "asset_id": aid,
                    "reason": f"Failed to load SDF npz: {exc}",
                })

        # --- Gate 5: Fabrication Neck / Kerf Gate ---
        if svg_poly is not None and svg_poly.is_valid and svg_poly.area > 0:
            fab = evaluate_fabrication_erosion(svg_poly, mask_fg)
            fab_entry = {"asset_id": aid, "tier": tier, **fab}
            fab_metrics_by_tier[tier].append(fab_entry)

            if is_cuttable:
                fab_cuttable_checked += 1
                if not fab["kerf_pass"]:
                    gates["fabrication_neck_and_kerf"]["failures"].append({
                        "asset_id": aid,
                        "tier": tier,
                        "reason": (
                            f"Failed 0.15mm laser kerf erosion at 60mm scale "
                            f"(components={fab['kerf_components']}, critical_neck={fab['critical_neck_mm_at_60mm']}mm)"
                        ),
                    })
                elif not fab["neck_3mm_pass"]:
                    gates["fabrication_neck_and_kerf"]["failures"].append({
                        "asset_id": aid,
                        "tier": tier,
                        "reason": (
                            f"Failed conservative 3.0mm+0.15mm neck erosion at 60mm scale "
                            f"(components={fab['neck_3mm_components']}, survival={fab['neck_3mm_survival_ratio']}, "
                            f"critical_neck={fab['critical_neck_mm_at_60mm']}mm, mask_cc={fab['mask_neck_3mm_components']})"
                        ),
                    })

    # --- Finalize Gate 2: Split Hygiene Cross-Split Overlap Checks ---
    split_pairs = [("train", "val"), ("train", "test"), ("val", "test")]
    for s1, s2 in split_pairs:
        ov_groups = sorted(split_groups[s1] & split_groups[s2])
        ov_shas = sorted(split_shas[s1] & split_shas[s2])
        ov_sids = sorted(split_source_ids[s1] & split_source_ids[s2])
        ov_masks = sorted(split_mask_hashes[s1] & split_mask_hashes[s2])
        if ov_groups:
            gates["split_hygiene"]["failures"].append({
                "splits": f"{s1}/{s2}",
                "reason": f"source_group overlap ({len(ov_groups)} groups): {ov_groups[:10]}",
            })
        if ov_shas:
            gates["split_hygiene"]["failures"].append({
                "splits": f"{s1}/{s2}",
                "reason": f"raw sha256 overlap ({len(ov_shas)} hashes): {[x[:12] for x in ov_shas[:10]]}",
            })
        if ov_sids:
            gates["split_hygiene"]["failures"].append({
                "splits": f"{s1}/{s2}",
                "reason": f"source_id overlap ({len(ov_sids)} ids): {ov_sids[:10]}",
            })
        if ov_masks:
            leaked_assets = [all_mask_hashes[h] for h in ov_masks[:5]]
            gates["split_hygiene"]["failures"].append({
                "splits": f"{s1}/{s2}",
                "reason": f"Exact processed mask duplicate leaked across splits ({len(ov_masks)} masks): {leaked_assets}",
            })

    total_groups = len(split_groups["train"] | split_groups["val"] | split_groups["test"])
    if total_groups >= 10:
        for s in VALID_SPLITS:
            if len(split_groups[s]) == 0:
                gates["split_hygiene"]["failures"].append({
                    "split": s,
                    "reason": f"Split '{s}' is empty despite {total_groups} source groups",
                })

    gates["split_hygiene"]["metrics"] = {
        "total_source_groups": total_groups,
        "groups_per_split": {s: len(split_groups[s]) for s in sorted(VALID_SPLITS)},
        "assets_per_split": dict(counts_split),
        "exact_duplicate_processed_masks_within_corpus": sum(1 for v in all_mask_hashes.values() if len(v) > 1),
    }

    # --- Finalize Gate 3: Figma Sheets Validation ---
    figma_dir = root / "processed" / "figma_sheets"
    figma_results: List[Dict[str, Any]] = []
    if not figma_dir.exists():
        gates["geometry_and_svg_reimport"]["failures"].append({
            "scope": "processed/figma_sheets",
            "reason": "Missing processed/figma_sheets/ directory",
        })
    else:
        sheet_svgs = sorted(figma_dir.glob("*.svg"))
        if not sheet_svgs:
            gates["geometry_and_svg_reimport"]["failures"].append({
                "scope": "processed/figma_sheets",
                "reason": "No SVG files found in processed/figma_sheets/",
            })
        for sheet in sheet_svgs:
            rel_sheet = str(sheet.relative_to(root)).replace("\\", "/")
            s_res: Dict[str, Any] = {
                "file": rel_sheet,
                "valid_xml": False,
                "named_groups": 0,
                "path_count": 0,
                "valid_paths": 0,
                "invalid_paths": 0,
                "duplicate_ids": [],
                "errors": [],
            }
            try:
                tree = ET.parse(sheet)
                sroot = tree.getroot()
                s_res["valid_xml"] = True
                if sroot.tag.split("}")[-1].lower() != "svg":
                    s_res["errors"].append(f"Root tag is not <svg>: {sroot.tag}")
                vb = parse_viewbox(sroot.attrib.get("viewBox"))
                if vb is None or vb[2] <= 0 or vb[3] <= 0:
                    s_res["errors"].append(f"Invalid or missing viewBox: {sroot.attrib.get('viewBox')!r}")

                seen_ids: Set[str] = set()
                for elem in sroot.iter():
                    local = elem.tag.split("}")[-1].lower()
                    eid = elem.attrib.get("id")
                    if eid:
                        if eid in seen_ids:
                            s_res["duplicate_ids"].append(eid)
                        seen_ids.add(eid)
                        if local == "g":
                            s_res["named_groups"] += 1
                    if local == "path":
                        s_res["path_count"] += 1
                        d = elem.attrib.get("d", "")
                        try:
                            subs = _split_svg_path_d(d)
                            if not subs:
                                s_res["invalid_paths"] += 1
                                s_res["errors"].append(f"Empty path d in element id={eid!r}")
                            else:
                                path_ok = True
                                for pts, _ in subs:
                                    if len(pts) < 3:
                                        path_ok = False
                                        break
                                    r_pts = pts if math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) <= 1e-7 else pts + [pts[0]]
                                    p = Polygon(r_pts)
                                    if not p.is_valid or p.area <= 0:
                                        path_ok = False
                                        break
                                if path_ok:
                                    s_res["valid_paths"] += 1
                                else:
                                    s_res["invalid_paths"] += 1
                        except Exception as exc:
                            s_res["invalid_paths"] += 1
                            if len(s_res["errors"]) < 10:
                                s_res["errors"].append(f"Path parse error (id={eid!r}): {exc}")

                if s_res["path_count"] == 0:
                    s_res["errors"].append("Figma sheet contains 0 <path> elements")
                if s_res["named_groups"] == 0:
                    s_res["errors"].append("Figma sheet contains 0 named <g id='...'> groups")
                if s_res["duplicate_ids"]:
                    s_res["errors"].append(f"Duplicate element IDs: {s_res['duplicate_ids'][:10]}")
                if s_res["invalid_paths"] > 0:
                    s_res["errors"].append(f"{s_res['invalid_paths']} invalid/degenerate <path> element(s)")
            except Exception as exc:
                s_res["errors"].append(f"XML parse failure: {exc}")

            if s_res["errors"]:
                gates["geometry_and_svg_reimport"]["failures"].append({
                    "scope": rel_sheet,
                    "reason": "; ".join(s_res["errors"][:5]),
                })
            figma_results.append(s_res)

    # --- Finalize Gate 4: Cross-Corpus Mask Convention & SDF Scale Consistency ---
    if len(mask_bg_values) > 1:
        gates["mask_and_sdf_consistency"]["failures"].append({
            "scope": "processed/masks",
            "reason": f"Inconsistent mask background polarity across corpus: observed border values {sorted(mask_bg_values)}",
        })

    sdf_scale_summary: Dict[str, Any] = {}
    if sdf_unit_scales:
        arr_sc = np.asarray(sdf_unit_scales, dtype=np.float64)
        med_sc = float(np.median(arr_sc))
        min_sc = float(np.min(arr_sc))
        max_sc = float(np.max(arr_sc))
        sdf_scale_summary = {
            "median_boundary_grad_mag": round(med_sc, 6),
            "min_boundary_grad_mag": round(min_sc, 6),
            "max_boundary_grad_mag": round(max_sc, 6),
        }
        if max_sc > med_sc * 2.0 or min_sc < med_sc * 0.5:
            gates["mask_and_sdf_consistency"]["warnings"].append({
                "scope": "processed/sdf",
                "reason": f"Wide SDF gradient scale variation across corpus: min={min_sc:.5f}, med={med_sc:.5f}, max={max_sc:.5f}",
            })

    # --- Finalize Gate Metrics & Pass/Fail Flags ---
    gates["provenance_and_integrity"]["metrics"] = {
        "manifest_count": len(raw_audit["manifest_files"]),
        "total_raw_records": raw_audit["total_raw_records"],
        "valid_raw_records": raw_audit["valid_raw_records"],
        "catalog_records": len(records),
        "cross_manifest_duplicate_sha256_count": len(raw_audit["cross_manifest_duplicate_sha256"]),
    }
    gates["geometry_and_svg_reimport"]["metrics"] = {
        "svg_checked": geom_checked,
        "cuttable_checked": geom_cuttable_checked,
        "figma_sheets_count": len(figma_results),
        "figma_sheets": figma_results,
    }
    gates["mask_and_sdf_consistency"]["metrics"] = {
        "checked": mask_sdf_checked,
        "detected_mask_background_values": sorted(mask_bg_values),
        **sdf_scale_summary,
    }

    fab_summary_tiers: Dict[str, Any] = {}
    for t, items in fab_metrics_by_tier.items():
        if not items:
            continue
        necks = [x["critical_neck_mm_at_60mm"] for x in items]
        surv = [x["neck_3mm_survival_ratio"] for x in items]
        fab_summary_tiers[t] = {
            "count": len(items),
            "kerf_pass_count": sum(1 for x in items if x["kerf_pass"]),
            "neck_3mm_pass_count": sum(1 for x in items if x["neck_3mm_pass"]),
            "neck_2mm_pass_count": sum(1 for x in items if x["neck_2mm_pass"]),
            "critical_neck_mm_min": round(min(necks), 3),
            "critical_neck_mm_median": round(float(np.median(necks)), 3),
            "neck_3mm_survival_median": round(float(np.median(surv)), 4),
        }
    gates["fabrication_neck_and_kerf"]["metrics"] = {
        "cuttable_checked": fab_cuttable_checked,
        "target_piece_mm": TARGET_PIECE_MM,
        "kerf_mm": KERF_MM,
        "min_neck_mm": MIN_NECK_MM,
        "by_tier": fab_summary_tiers,
    }

    for g in gates.values():
        g["passed"] = len(g["failures"]) == 0
        g["failure_count"] = len(g["failures"])
        g["warning_count"] = len(g["warnings"])

    overall_passed = all(g["passed"] for g in gates.values()) and len(records) > 0

    # Check contact sheets presence (for visual QA)
    cs_dir = root / "processed" / "contact_sheets"
    contact_sheets = (
        [str(p.relative_to(root)).replace("\\", "/") for p in sorted(cs_dir.glob("*.png"))]
        if cs_dir.exists()
        else []
    )

    norway_count = counts_country.get("NO", 0)
    neighbor_nordic_count = sum(v for k, v in counts_country.items() if k != "NO")

    return {
        "timestamp_utc": timestamp,
        "root": str(root),
        "overall_passed": overall_passed,
        "corpus_ready": True,
        "gates": gates,
        "raw_summary": _clean_raw_summary(raw_audit),
        "processed_summary": {
            "total_silhouettes": len(records),
            "by_tier": dict(counts_tier),
            "norway_vs_neighbor_nordic": {
                "norway_NO": norway_count,
                "neighboring_nordic": neighbor_nordic_count,
            },
            "by_country": dict(counts_country),
            "by_region": dict(counts_region.most_common()),
            "by_archive": dict(counts_archive),
            "by_family": dict(counts_family),
            "by_motif_class": dict(counts_class.most_common()),
            "by_render_style": dict(counts_render_style),
            "by_media_type": dict(counts_media_type),
            "by_license": dict(counts_license),
            "by_commercial_ok": dict(counts_commercial),
            "by_split": dict(counts_split),
            "contact_sheets": contact_sheets,
            "figma_sheets": [f["file"] for f in figma_results],
        },
        "shape_anomalies": {
            "total_flagged": len(shape_anomalies),
            "cuttable_flagged": sum(1 for a in shape_anomalies if a["tier"] in CUTTABLE_TIERS),
            "items": shape_anomalies,
        },
    }


def _clean_raw_summary(raw_audit: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "manifest_files": raw_audit["manifest_files"],
        "total_raw_records": raw_audit["total_raw_records"],
        "valid_raw_records": raw_audit["valid_raw_records"],
        "by_archive": dict(raw_audit["by_archive"]),
        "by_country": dict(raw_audit["by_country"]),
        "by_media_type": dict(raw_audit["by_media_type"]),
        "by_license": dict(raw_audit["by_license"]),
        "by_commercial_ok": dict(raw_audit["by_commercial_ok"]),
        "cross_manifest_duplicate_sha256_count": len(raw_audit["cross_manifest_duplicate_sha256"]),
    }


# ---------------------------------------------------------------------------
# Markdown Report Generator
# ---------------------------------------------------------------------------

def render_markdown_report(report: Dict[str, Any], visual_qa_notes: Optional[str] = None) -> str:
    lines: List[str] = []
    status = "PASS" if report["overall_passed"] else "FAIL"
    lines.append(f"# Nordic Helleristninger Corpus — Independent QA Report ({status})")
    lines.append("")
    lines.append(f"- **Generated (UTC):** `{report['timestamp_utc']}`")
    lines.append(f"- **Corpus Root:** `{report['root']}`")
    lines.append(f"- **Automated Gates Verdict:** **{status}**")
    lines.append("")

    # Gate summary table
    lines.append("## 1. Hard Gate Summary")
    lines.append("")
    lines.append("| Gate | Status | Failures | Warnings |")
    lines.append("|---|---:|---:|---:|")
    for gname, gdata in report["gates"].items():
        st = "PASS" if gdata["passed"] else "FAIL"
        lines.append(f"| `{gname}` | **{st}** | {gdata['failure_count']} | {gdata['warning_count']} |")
    lines.append("")

    # Raw & Processed counts
    raw = report.get("raw_summary", {})
    proc = report.get("processed_summary", {})
    lines.append("## 2. Corpus Totals & Breakdowns")
    lines.append("")
    lines.append(f"- **Raw Originals Harvested:** {raw.get('total_raw_records', 0)} ({raw.get('valid_raw_records', 0)} verified valid across {len(raw.get('manifest_files', []))} manifests)")
    lines.append(f"- **Isolated Processed Silhouettes:** {proc.get('total_silhouettes', 0)}")
    if proc:
        tiers = proc.get("by_tier", {})
        lines.append(
            f"- **Jigsaw Tiers:** `A_ready`={tiers.get('A_ready', 0)}, "
            f"`B_thickened`={tiers.get('B_thickened', 0)}, "
            f"`C_fragile_or_review`={tiers.get('C_fragile_or_review', 0)}"
        )
        nv = proc.get("norway_vs_neighbor_nordic", {})
        lines.append(
            f"- **Regional Split:** Norway (`NO`)={nv.get('norway_NO', 0)}, "
            f"Neighboring Nordic={nv.get('neighboring_nordic', 0)}"
        )
        lines.append(f"- **Splits:** {proc.get('by_split', {})}")
        lines.append("")

        def _table(title: str, mapping: Dict[str, Any], col1: str = "Category") -> None:
            if not mapping:
                return
            lines.append(f"### {title}")
            lines.append("")
            lines.append(f"| {col1} | Count |")
            lines.append("|---|---:|")
            for k, v in sorted(mapping.items(), key=lambda kv: (-kv[1], kv[0])):
                lines.append(f"| `{k}` | {v} |")
            lines.append("")

        _table("Raw Originals by Archive", raw.get("by_archive", {}), "Archive")
        _table("Raw Originals by Country", raw.get("by_country", {}), "Country")
        _table("Raw Originals by Media Type", raw.get("by_media_type", {}), "Media Type")
        _table("Processed Silhouettes by Country", proc.get("by_country", {}), "Country")
        _table("Processed Silhouettes by Archive", proc.get("by_archive", {}), "Archive")
        _table("Processed Silhouettes by Family", proc.get("by_family", {}), "Family")
        _table("Processed Silhouettes by Motif Class", proc.get("by_motif_class", {}), "Family / Class")
        _table("Processed Silhouettes by License", proc.get("by_license", {}), "License")
        _table("Processed Silhouettes by Commercial OK", proc.get("by_commercial_ok", {}), "commercial_ok")

    # Fabrication summary
    fab_m = report["gates"]["fabrication_neck_and_kerf"].get("metrics", {})
    by_tier = fab_m.get("by_tier", {})
    if by_tier:
        lines.append("## 3. Independent Fabrication Erosion Metrics (60 mm Piece Scale)")
        lines.append("")
        lines.append("| Tier | Count | Kerf (0.15 mm) Pass | Neck (3.0+0.15 mm) Pass | Neck (2.0+0.15 mm) Pass | Min Critical Neck (mm) | Median Critical Neck (mm) |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")
        for t in ("A_ready", "B_thickened", "C_fragile_or_review"):
            if t in by_tier:
                d = by_tier[t]
                lines.append(
                    f"| `{t}` | {d['count']} | {d['kerf_pass_count']}/{d['count']} | "
                    f"{d['neck_3mm_pass_count']}/{d['count']} | {d['neck_2mm_pass_count']}/{d['count']} | "
                    f"{d['critical_neck_mm_min']:.2f} | {d['critical_neck_mm_median']:.2f} |"
                )
        lines.append("")

    # Figma sheets
    geom_m = report["gates"]["geometry_and_svg_reimport"].get("metrics", {})
    fsheets = geom_m.get("figma_sheets", [])
    if fsheets:
        lines.append("## 4. Figma-Ready SVG Sheets")
        lines.append("")
        lines.append("| File | Valid XML | Named Groups | Paths (Valid/Total) | Errors |")
        lines.append("|---|---|---:|---:|---|")
        for fs in fsheets:
            err_txt = "; ".join(fs["errors"][:3]) if fs["errors"] else "None"
            lines.append(
                f"| `{fs['file']}` | {fs['valid_xml']} | {fs['named_groups']} | "
                f"{fs['valid_paths']}/{fs['path_count']} | {err_txt} |"
            )
        lines.append("")

    # Gate failures detail
    lines.append("## 5. Gate Failures & Warnings")
    lines.append("")
    any_fail = False
    for gname, gdata in report["gates"].items():
        if gdata["failures"]:
            any_fail = True
            lines.append(f"### Failures in `{gname}` ({len(gdata['failures'])})")
            lines.append("")
            for item in gdata["failures"][:50]:
                lines.append(f"- `{json.dumps(item, ensure_ascii=False)}`")
            if len(gdata["failures"]) > 50:
                lines.append(f"- ... ({len(gdata['failures']) - 50} more; see `qa_report.json`)")
            lines.append("")
        if gdata["warnings"]:
            lines.append(f"### Warnings in `{gname}` ({len(gdata['warnings'])})")
            lines.append("")
            for item in gdata["warnings"][:25]:
                lines.append(f"- `{json.dumps(item, ensure_ascii=False)}`")
            if len(gdata["warnings"]) > 25:
                lines.append(f"- ... ({len(gdata['warnings']) - 25} more; see `qa_report.json`)")
            lines.append("")
    if not any_fail:
        lines.append("All automated hard gates passed with **0 failures**.")
        lines.append("")

    # Automated shape anomalies
    anoms = report.get("shape_anomalies", {})
    lines.append("## 6. Automated Shape-Quality Diagnostics (Pre-Visual Triage)")
    lines.append("")
    lines.append(f"- **Total flagged shapes:** {anoms.get('total_flagged', 0)} (cuttable Tiers A/B: {anoms.get('cuttable_flagged', 0)})")
    if anoms.get("items"):
        for item in anoms["items"][:30]:
            lines.append(
                f"- `{item['asset_id']}` (`{item['tier']}`, `{item['family']}/{item['motif_class']}`, site=`{item['site']}`): "
                + ", ".join(item["flags"])
            )
        if len(anoms["items"]) > 30:
            lines.append(f"- ... ({len(anoms['items']) - 30} more in `qa_report.json`)")
    lines.append("")

    # Visual QA section
    lines.append("## 7. Visual Silhouette Inspection")
    lines.append("")
    if visual_qa_notes:
        lines.append(visual_qa_notes.strip())
    else:
        lines.append("_Pending direct visual inspection of `processed/contact_sheets/*.png`._")
    lines.append("")

    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Independent QA verification for Nordic Helleristninger corpus.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Path to hellerisninger/ root directory (default: parent of qa/)",
    )
    parser.add_argument(
        "--write-reports",
        action="store_true",
        help="Write qa_report.json and qa_report.md into <root>/qa/",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Optional directory for qa_report.json and qa_report.md (default: <root>/qa/)",
    )
    parser.add_argument(
        "--visual-notes-file",
        type=Path,
        default=None,
        help="Optional markdown file with visual QA inspection findings to embed into qa_report.md",
    )
    args = parser.parse_args(argv)

    root = args.root.resolve()
    report = run_verification(root)

    visual_notes = None
    if args.visual_notes_file and args.visual_notes_file.exists():
        visual_notes = args.visual_notes_file.read_text(encoding="utf-8")
        report["visual_qa_notes"] = visual_notes

    if args.write_reports:
        out_dir = (args.out_dir or (root / "qa")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        json_out = out_dir / "qa_report.json"
        md_out = out_dir / "qa_report.md"
        json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        md_out.write_text(render_markdown_report(report, visual_qa_notes=visual_notes), encoding="utf-8")
        print(f"Wrote {json_out} and {md_out}")

    # Concise stdout summary
    print(f"=== Independent Corpus QA: {'PASS' if report['overall_passed'] else 'FAIL'} ===")
    raw = report.get("raw_summary", {})
    proc = report.get("processed_summary", {})
    print(f"Raw originals: {raw.get('total_raw_records', 0)} (valid: {raw.get('valid_raw_records', 0)})")
    print(f"Processed silhouettes: {proc.get('total_silhouettes', 0)} | tiers: {proc.get('by_tier', {})}")
    for gname, gdata in report["gates"].items():
        st = "PASS" if gdata["passed"] else "FAIL"
        print(f"  [{st}] {gname}: {gdata['failure_count']} failures, {gdata['warning_count']} warnings")
        for f in gdata["failures"][:5]:
            print(f"       -> {f}")
        if gdata["failure_count"] > 5:
            print(f"       -> ... ({gdata['failure_count'] - 5} more)")

    if not report.get("corpus_ready", False):
        return 2
    return 0 if report["overall_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
