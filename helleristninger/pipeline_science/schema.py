"""JSON Schema definition and strict validator for scientific rock-art figure records."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$")

SCIENTIFIC_RECORD_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Nordic Helleristninger Scientific Figure Record",
    "type": "object",
    "required": [
        "figure_id",
        "site_panel_id",
        "coordinates",
        "coordinates_provenance",
        "motif",
        "size_mm",
        "period",
        "source_image",
        "crop_px",
        "method",
        "tracer",
        "date",
        "rights_holder",
        "licence",
        "licence_url",
        "confidence",
        "literature",
    ],
    "properties": {
        "figure_id": {"type": "string", "minLength": 3},
        "site_panel_id": {"type": ["string", "null"]},
        "site_panel_id_provenance": {"type": "string"},
        "site": {"type": "string"},
        "region": {"type": "string"},
        "core_region": {
            "type": "string",
            "enum": ["alta", "ostfold", "bohuslan_tanum", "namforsen", "vingen", "trondelag", "other"],
        },
        "country": {"type": "string"},
        "coordinates": {
            "type": ["object", "null"],
            "properties": {
                "crs": {"type": "string", "const": "EPSG:4326"},
                "lat": {"type": "number", "minimum": -90.0, "maximum": 90.0},
                "lon": {"type": "number", "minimum": -180.0, "maximum": 180.0},
                "precision": {"type": "string"},
                "provenance": {"type": "string"},
            },
            "required": ["crs", "lat", "lon", "provenance"],
        },
        "coordinates_provenance": {
            "type": "object",
            "required": ["crs", "status", "reason"],
            "properties": {
                "crs": {"type": "string"},
                "status": {"type": "string", "enum": ["verified", "unverified"]},
                "reason": {"type": "string"},
            },
        },
        "motif": {
            "type": "object",
            "required": ["family", "motif_class"],
            "properties": {
                "family": {"type": "string"},
                "motif_class": {"type": "string"},
            },
        },
        "size_mm": {
            "type": "object",
            "required": [
                "width_mm",
                "height_mm",
                "area_mm2",
                "px_per_mm",
                "scale_calibrated",
                "scale_source",
                "width_px",
                "height_px",
                "area_px2",
            ],
            "properties": {
                "width_mm": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "height_mm": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "area_mm2": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "px_per_mm": {"type": ["number", "null"], "exclusiveMinimum": 0},
                "scale_calibrated": {"type": "boolean"},
                "scale_source": {"type": "string", "minLength": 1},
                "width_px": {"type": "number", "exclusiveMinimum": 0},
                "height_px": {"type": "number", "exclusiveMinimum": 0},
                "area_px2": {"type": "number", "exclusiveMinimum": 0},
            },
        },
        "period": {"type": "string"},
        "source_image": {
            "type": "object",
            "required": ["source_id", "raw_path", "sha256"],
            "properties": {
                "source_id": {"type": "string", "minLength": 1},
                "source_archive": {"type": "string"},
                "raw_path": {"type": "string", "minLength": 1},
                "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "media_type": {
                    "type": "string",
                    "enum": ["bw_tracing", "svg_vector", "rubbing_frottage", "rti_3d"],
                },
                "source_url": {"type": "string"},
            },
        },
        "crop_px": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 4,
            "maxItems": 4,
        },
        "method": {"type": "string", "minLength": 1},
        "tracer": {"type": "string", "minLength": 1},
        "date": {"type": "string"},
        "rights_holder": {"type": "string"},
        "licence": {"type": "string"},
        "licence_url": {"type": "string"},
        "commercial_ok": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "literature": {"type": "array"},
        "svg_path": {"type": "string"},
        "layers": {
            "type": "object",
            "properties": {
                "figures_path_closed": {"type": "boolean"},
                "cup_marks_count": {"type": "integer", "minimum": 0},
                "cracks_count": {"type": "integer", "minimum": 0},
                "uncertain_count": {"type": "integer", "minimum": 0},
                "has_scale_bar": {"type": "boolean"},
            },
        },
        "geometry": {
            "type": "object",
            "properties": {
                "vertex_count": {"type": "integer", "minimum": 3},
                "solidity": {"type": "number"},
                "compactness": {"type": "number"},
                "aspect_ratio": {"type": "number"},
                "perimeter_px": {"type": "number"},
                "perimeter_mm": {"type": ["number", "null"]},
                "hole_count": {"type": "integer", "minimum": 0},
                "component_count": {"type": "integer", "minimum": 1},
                "euler_number": {"type": "integer"},
            },
        },
    },
}


def write_schema_json(out_path: Path) -> None:
    """Write SCIENTIFIC_RECORD_SCHEMA to disk as formatted JSON."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(SCIENTIFIC_RECORD_SCHEMA, f, indent=2, ensure_ascii=False)


def validate_figure_record(rec: Dict[str, Any]) -> List[str]:
    """Validate one scientific figure record against required fields and invariants."""
    errors: List[str] = []
    for key in SCIENTIFIC_RECORD_SCHEMA["required"]:
        if key not in rec:
            errors.append(f"missing_required:{key}")

    if errors:
        return errors

    if not isinstance(rec["figure_id"], str) or len(rec["figure_id"]) < 3:
        errors.append("invalid:figure_id")

    coords = rec["coordinates"]
    if coords is not None:
        if not isinstance(coords, dict):
            errors.append("invalid:coordinates_type")
        else:
            if coords.get("crs") != "EPSG:4326":
                errors.append("invalid:coordinates_crs")
            lat = coords.get("lat")
            lon = coords.get("lon")
            if not isinstance(lat, (int, float)) or not (-90.0 <= float(lat) <= 90.0):
                errors.append("invalid:coordinates_lat")
            if not isinstance(lon, (int, float)) or not (-180.0 <= float(lon) <= 180.0):
                errors.append("invalid:coordinates_lon")
            if not coords.get("provenance"):
                errors.append("invalid:coordinates_provenance_missing")

    motif = rec["motif"]
    if not isinstance(motif, dict) or not motif.get("family") or not motif.get("motif_class"):
        errors.append("invalid:motif")

    sm = rec["size_mm"]
    if not isinstance(sm, dict):
        errors.append("invalid:size_mm")
    else:
        cal = sm.get("scale_calibrated")
        if not isinstance(cal, bool):
            errors.append("invalid:scale_calibrated_bool")
        elif cal:
            for k in ("width_mm", "height_mm", "area_mm2", "px_per_mm"):
                v = sm.get(k)
                if not isinstance(v, (int, float)) or float(v) <= 0.0:
                    errors.append(f"invalid:calibrated_{k}")
            if not sm.get("scale_source"):
                errors.append("invalid:calibrated_scale_source")
        else:
            for k in ("width_mm", "height_mm", "area_mm2", "px_per_mm"):
                if sm.get(k) is not None:
                    errors.append(f"fabricating_mm_when_uncalibrated:{k}")
        for k in ("width_px", "height_px", "area_px2"):
            v = sm.get(k)
            if not isinstance(v, (int, float)) or float(v) <= 0.0:
                errors.append(f"invalid:{k}")

    src = rec["source_image"]
    if not isinstance(src, dict):
        errors.append("invalid:source_image")
    else:
        if src.get("media_type") in ("painted_photo", "photo_other"):
            errors.append(f"forbidden_media_type:{src.get('media_type')}")
        if not src.get("source_id") or not src.get("raw_path"):
            errors.append("invalid:source_image_ids")
        if not _SHA256_RE.match(str(src.get("sha256") or "").lower()):
            errors.append("invalid:source_image_sha256")

    crop = rec["crop_px"]
    if (
        not isinstance(crop, list)
        or len(crop) != 4
        or not all(isinstance(v, int) for v in crop)
        or crop[2] <= 0
        or crop[3] <= 0
    ):
        errors.append("invalid:crop_px")

    if not _ISO_DATE_RE.match(str(rec.get("date") or "")):
        errors.append("invalid:date_iso8601")

    conf = rec.get("confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= float(conf) <= 1.0):
        errors.append("invalid:confidence")

    if not isinstance(rec.get("literature"), list):
        errors.append("invalid:literature_list")

    return errors
