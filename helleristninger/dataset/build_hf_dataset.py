"""Build production-grade Hugging Face & GitHub dataset + Explorer data bundle for Nordic Helleristninger."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET

import pyarrow as pa
import pyarrow.parquet as pq

from helleristninger.dataset.dating_geo import parse_geo_precision, resolve_dating

BASE_DIR = Path(__file__).resolve().parent.parent
DATASET_DIR = Path(__file__).resolve().parent
EXPLORER_DIR = BASE_DIR / "explorer"

_SVG_NS = "{http://www.w3.org/2000/svg}"


def _clean_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(f) or math.isinf(f)) else f


def _clean_int(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(round(f))


def _clean_str(val: Any, default: str = "") -> str:
    if val is None:
        return default
    s = str(val).strip()
    if s.lower() in ("nan", "none", "null"):
        return default
    return s


def _extract_coords(rec: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], str, str]:
    """Extract (lat, lon, precision_str, coord_source) from either flat or nested coordinates dict."""
    coords = rec.get("coordinates") if isinstance(rec.get("coordinates"), dict) else {}
    lat = _clean_float(rec.get("lat") if rec.get("lat") is not None else coords.get("lat"))
    lon = _clean_float(rec.get("lon") if rec.get("lon") is not None else coords.get("lon"))
    prec = _clean_str(coords.get("precision") or rec.get("precision") or rec.get("coord_precision") or rec.get("coord_source"))
    prov = _clean_str(coords.get("provenance") or rec.get("coord_source") or rec.get("coordinate_source"))
    return lat, lon, prec, prov


def _norm_name(text: str) -> str:
    s = (text or "").lower().strip()
    s = re.sub(r"[\s_\-—–,/]+", " ", s)
    return s.strip()


def _parse_svg_layers(svg_path: Path) -> Dict[str, Any]:
    """Parse multi-layer SVG file into vector geometry and metadata without external deps."""
    empty = {
        "viewBox": "0 0 100 100",
        "units": "px",
        "figures_d": "",
        "cup_marks": [],
        "cracks_d": [],
        "uncertain_d": [],
        "scale_bar_svg": "",
    }
    if not svg_path.is_file():
        return empty
    try:
        raw = svg_path.read_text(encoding="utf-8", errors="replace")
        root = ET.fromstring(raw)
    except Exception:
        return empty

    view_box = root.attrib.get("viewBox", "0 0 100 100")
    units = root.attrib.get("data-units", "px")
    figures_d = ""
    cup_marks: List[List[float]] = []
    cracks_d: List[str] = []
    uncertain_d: List[str] = []
    scale_bar_svg = ""

    for child in root:
        tag = child.tag.replace(_SVG_NS, "")
        gid = child.attrib.get("id", "")
        if tag != "g":
            continue
        if gid == "figures":
            paths = [el.attrib.get("d", "") for el in child if el.tag.endswith("path") and el.attrib.get("d")]
            figures_d = " ".join(paths)
        elif gid == "cup_marks":
            for el in child:
                if el.tag.endswith("circle"):
                    cx = _clean_float(el.attrib.get("cx"))
                    cy = _clean_float(el.attrib.get("cy"))
                    r = _clean_float(el.attrib.get("r"))
                    if cx is not None and cy is not None and r is not None:
                        cup_marks.append([round(cx, 2), round(cy, 2), round(r, 2)])
        elif gid == "cracks":
            for el in child:
                d = el.attrib.get("d")
                if d:
                    cracks_d.append(d)
        elif gid == "uncertain":
            for el in child:
                d = el.attrib.get("d")
                if d:
                    uncertain_d.append(d)
        elif gid == "scale_bar":
            inner_parts = [ET.tostring(el, encoding="unicode") for el in child]
            scale_bar_svg = "".join(inner_parts)

    return {
        "viewBox": view_box,
        "units": units,
        "figures_d": figures_d,
        "cup_marks": cup_marks,
        "cracks_d": cracks_d,
        "uncertain_d": uncertain_d,
        "scale_bar_svg": scale_bar_svg,
    }


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.is_file():
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                rows.append(json.loads(s))
            except json.JSONDecodeError:
                continue
    return rows


def _load_qa_lookup(qa_dir: Path) -> Dict[str, Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}
    report_path = qa_dir / "dual_trace_report.json"
    if report_path.is_file():
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            for item in data.get("figures", []):
                fid = item.get("figure_id")
                if fid:
                    lookup[fid] = item
        except Exception:
            pass
    dis_path = qa_dir / "dual_trace_disagreements.jsonl"
    for item in _load_jsonl(dis_path):
        fid = item.get("figure_id")
        if fid:
            merged = dict(lookup.get(fid, {}))
            merged.update(item)
            lookup[fid] = merged
    return lookup


def _load_sources_records(sources_dir: Path) -> List[Dict[str, Any]]:
    """Load all manifests across helleristninger/sources/*/manifest.jsonl."""
    rows: List[Dict[str, Any]] = []
    for mf in sorted(sources_dir.glob("*/manifest.jsonl")):
        folder = mf.parent.name
        for rec in _load_jsonl(mf):
            media_type = _clean_str(rec.get("media_type"), "unknown").lower()
            quarantined = bool(rec.get("quarantined")) or media_type == "painted_photo"
            coords = rec.get("coordinates") if isinstance(rec.get("coordinates"), dict) else {}
            lat = _clean_float(rec.get("lat") if rec.get("lat") is not None else coords.get("lat"))
            lon = _clean_float(rec.get("lon") if rec.get("lon") is not None else coords.get("lon"))
            licence = _clean_str(rec.get("licence") or rec.get("license"), "Unknown")
            comm = rec.get("commercial_ok")
            if comm is None:
                comm = not any(k in licence.lower() for k in ("nc", "non-commercial", "unknown", "reserved", "restricted"))
            rows.append({
                "source_id": _clean_str(rec.get("source_id")),
                "source_folder": folder,
                "source_archive": _clean_str(rec.get("source_archive"), folder),
                "media_type": media_type,
                "quarantined": bool(quarantined),
                "sha256": _clean_str(rec.get("sha256")).lower(),
                "raw_path": _clean_str(rec.get("raw_path")).replace("\\", "/"),
                "source_url": _clean_str(rec.get("source_url")),
                "site": _clean_str(rec.get("site")),
                "region": _clean_str(rec.get("region")),
                "country": _clean_str(rec.get("country"), "NORDIC").upper(),
                "period": _clean_str(rec.get("period"), "unknown").lower(),
                "lat": lat,
                "lon": lon,
                "rights_holder": _clean_str(rec.get("rights_holder") or rec.get("creator"), "Unknown"),
                "licence": licence,
                "licence_url": _clean_str(rec.get("licence_url") or rec.get("license_url")),
                "commercial_ok": bool(comm),
                "scale_calibrated": bool(rec.get("scale_calibrated", False)),
                "px_per_mm": _clean_float(rec.get("px_per_mm")),
            })
    return rows


def _build_figure_rows(
    base_dir: Path,
    qa_lookup: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Load figures.jsonl, parse SVG layers, enrich dating/geo/QA, return rows + SVG payloads."""
    raw_figures = _load_jsonl(base_dir / "scientific_record" / "figures.jsonl")
    figure_rows: List[Dict[str, Any]] = []
    svg_payloads: Dict[str, Dict[str, Any]] = {}

    for rec in raw_figures:
        fid = _clean_str(rec.get("figure_id"))
        if not fid:
            continue
        coords = rec.get("coordinates") if isinstance(rec.get("coordinates"), dict) else None
        lat = _clean_float(coords.get("lat")) if coords else None
        lon = _clean_float(coords.get("lon")) if coords else None
        if lat is not None and not (-90.0 <= lat <= 90.0):
            lat = None
        if lon is not None and not (-180.0 <= lon <= 180.0):
            lon = None
        has_coords = lat is not None and lon is not None

        prec_label = _clean_str(coords.get("precision")) if coords else ""
        coord_prov = _clean_str(coords.get("provenance")) if coords else _clean_str((rec.get("coordinates_provenance") or {}).get("reason"))
        geo_prec_m, geo_level = parse_geo_precision(prec_label, coord_prov, has_coords=has_coords)

        core_region = _clean_str(rec.get("core_region"), "other").lower()
        site_name = _clean_str(rec.get("site"))
        period_hint = _clean_str(rec.get("period"), "unknown").lower()
        motif = rec.get("motif") or {}
        motif_family = _clean_str(motif.get("family"), "scene_or_unclassified")
        motif_class = _clean_str(motif.get("motif_class"), "unclassified_motif")

        dating = resolve_dating(
            rec,
            core_region=core_region,
            site_name=site_name,
            period_hint=period_hint,
        )

        svg_rel = _clean_str(rec.get("svg_path"), f"scientific_record/svg/{fid}.svg").replace("\\", "/")
        svg_parsed = _parse_svg_layers(base_dir / svg_rel)
        svg_payloads[fid] = svg_parsed

        sm = rec.get("size_mm") or {}
        src = rec.get("source_image") or {}
        lay = rec.get("layers") or {}
        geom = rec.get("geometry") or {}
        crop = rec.get("crop_px") or [0, 0, 0, 0]

        qa_item = qa_lookup.get(fid)
        dual_iou = _clean_float(qa_item.get("polygon_iou")) if qa_item else None
        if qa_item:
            sev = _clean_str(qa_item.get("severity"))
            if sev in ("minor", "moderate", "major"):
                qa_status = f"disagreement_{sev}"
            else:
                qa_status = "verified_pass"
        else:
            qa_status = "not_dual_traced"

        lit_list = rec.get("literature") or []
        lit_ids = [_clean_str(item.get("ref_id") or item.get("citation_id") or item.get("title")) for item in lit_list if isinstance(item, dict)]
        lit_ids = [x for x in lit_ids if x]

        row = {
            "figure_id": fid,
            "site_panel_id": _clean_str(rec.get("site_panel_id")) or None,
            "site_panel_id_provenance": _clean_str(rec.get("site_panel_id_provenance")),
            "site": site_name,
            "region": _clean_str(rec.get("region")),
            "core_region": core_region,
            "country": _clean_str(rec.get("country"), "NORDIC").upper(),
            "lat": lat,
            "lon": lon,
            "geo_precision_m": geo_prec_m,
            "geo_precision_label": prec_label or ("unverified" if not has_coords else "site_centroid"),
            "geo_level": geo_level,
            "coordinates_provenance": coord_prov,
            "date_from_year": dating["date_from_year"],
            "date_to_year": dating["date_to_year"],
            "date_midpoint_year": _clean_float(dating["date_midpoint_year"]),
            "dating_method": dating["dating_method"],
            "dating_precision_years": dating["dating_precision_years"],
            "period": period_hint if period_hint != "unknown" else dating["period"],
            "period_label": dating["period_label"],
            "dating_citation": dating["dating_citation"],
            "elevation_m_asl": _clean_float(dating.get("elevation_m_asl")),
            "motif_family": motif_family,
            "motif_class": motif_class,
            "svg_path": svg_rel,
            "svg_figures_d": svg_parsed["figures_d"],
            "figures_path_closed": bool(lay.get("figures_path_closed", True)),
            "cup_marks_count": _clean_int(lay.get("cup_marks_count")) or 0,
            "cracks_count": _clean_int(lay.get("cracks_count")) or 0,
            "uncertain_count": _clean_int(lay.get("uncertain_count")) or 0,
            "has_scale_bar": bool(lay.get("has_scale_bar", False)),
            "scale_calibrated": bool(sm.get("scale_calibrated", False)),
            "scale_source": _clean_str(sm.get("scale_source")),
            "px_per_mm": _clean_float(sm.get("px_per_mm")),
            "width_mm": _clean_float(sm.get("width_mm")),
            "height_mm": _clean_float(sm.get("height_mm")),
            "area_mm2": _clean_float(sm.get("area_mm2")),
            "width_px": _clean_float(sm.get("width_px")) or 0.0,
            "height_px": _clean_float(sm.get("height_px")) or 0.0,
            "area_px2": _clean_float(sm.get("area_px2")) or 0.0,
            "vertex_count": _clean_int(geom.get("vertex_count")) or 0,
            "solidity": _clean_float(geom.get("solidity")) or 0.0,
            "compactness": _clean_float(geom.get("compactness")) or 0.0,
            "aspect_ratio": _clean_float(geom.get("aspect_ratio")) or 0.0,
            "hole_count": _clean_int(geom.get("hole_count")) or 0,
            "component_count": _clean_int(geom.get("component_count")) or 1,
            "source_id": _clean_str(src.get("source_id")),
            "source_archive": _clean_str(src.get("source_archive")),
            "raw_path": _clean_str(src.get("raw_path")).replace("\\", "/"),
            "sha256": _clean_str(src.get("sha256")).lower(),
            "media_type": _clean_str(src.get("media_type")),
            "source_url": _clean_str(src.get("source_url")),
            "crop_x": _clean_int(crop[0]) or 0,
            "crop_y": _clean_int(crop[1]) or 0,
            "crop_w": _clean_int(crop[2]) or 0,
            "crop_h": _clean_int(crop[3]) or 0,
            "method": _clean_str(rec.get("method")),
            "tracer": _clean_str(rec.get("tracer")),
            "date": _clean_str(rec.get("date")),
            "rights_holder": _clean_str(rec.get("rights_holder"), "Unknown"),
            "licence": _clean_str(rec.get("licence"), "Unknown"),
            "licence_url": _clean_str(rec.get("licence_url")),
            "commercial_ok": bool(rec.get("commercial_ok", False)),
            "confidence": _clean_float(rec.get("confidence")) or 0.0,
            "dual_trace_iou": dual_iou,
            "dual_trace_qa_status": qa_status,
            "literature_refs": ", ".join(lit_ids),
            "literature_count": len(lit_list),
            "literature_json": json.dumps(lit_list, ensure_ascii=False),
        }
        figure_rows.append(row)

    return figure_rows, svg_payloads


def _consolidate_sites(
    base_dir: Path,
    figure_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Consolidate ALL crawled rock-art sites/panels across NO, SE, DK, FI into one register."""
    sites_by_uid: Dict[str, Dict[str, Any]] = {}
    alias_to_uid: Dict[str, str] = {}

    def _register_site(entry: Dict[str, Any]) -> None:
        uid = entry["site_uid"]
        existing = sites_by_uid.get(uid)
        if existing is None:
            sites_by_uid[uid] = entry
        else:
            # Merge missing fields without overwriting verified coordinates
            for k in ("askeladden_id", "raa_id", "lamning_id", "shfa_site_id", "source_url", "dating_text", "motif_summary"):
                if not existing.get(k) and entry.get(k):
                    existing[k] = entry[k]
            if existing.get("lat") is None and entry.get("lat") is not None:
                for k in ("lat", "lon", "geo_precision_m", "geo_level", "coord_source"):
                    existing[k] = entry[k]
            if existing.get("date_from_year") is None and entry.get("date_from_year") is not None:
                for k in ("date_from_year", "date_to_year", "date_midpoint_year", "dating_method", "dating_precision_years", "period", "period_label", "dating_citation"):
                    existing[k] = entry[k]
            existing["archives_set"].update(entry.get("archives_set", set()))
            existing["source_record_count"] = max(existing["source_record_count"], entry.get("source_record_count", 0))

        for ak in entry.get("_alias_keys", []):
            if ak and ak not in alias_to_uid:
                alias_to_uid[ak] = uid

    def _make_site_entry(
        *,
        uid: str,
        site_panel_id: Optional[str],
        site_name: str,
        parish_or_kommune: str,
        region_or_fylke: str,
        country: str,
        lat: Optional[float],
        lon: Optional[float],
        precision_str: str,
        coord_source: str,
        period_hint: str,
        dating_text: str,
        dateringsmetode: str,
        elevation_m: Optional[float],
        motif_summary: str,
        archives: Iterable[str],
        source_url: str,
        askeladden_id: Optional[str] = None,
        raa_id: Optional[str] = None,
        lamning_id: Optional[str] = None,
        shfa_site_id: Optional[str] = None,
        source_record_count: int = 0,
        alias_keys: Iterable[str] = (),
        raw_rec: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if lat is not None and not (-90.0 <= lat <= 90.0):
            lat = None
        if lon is not None and not (-180.0 <= lon <= 180.0):
            lon = None
        has_coords = lat is not None and lon is not None
        geo_prec_m, geo_level = parse_geo_precision(precision_str, coord_source, has_coords=has_coords)

        core_guess = "other"
        blob = f"{region_or_fylke} {parish_or_kommune} {site_name}".lower()
        if "alta" in blob or "finnmark" in blob:
            core_guess = "alta"
        elif "østfold" in blob or "ostfold" in blob or "sarpsborg" in blob or "fredrikstad" in blob or "halden" in blob:
            core_guess = "ostfold"
        elif "tanum" in blob or "bohusl" in blob:
            core_guess = "bohuslan_tanum"
        elif "nämfors" in blob or "namfors" in blob or "ådals" in blob:
            core_guess = "namforsen"
        elif "vingen" in blob:
            core_guess = "vingen"
        elif "trøndelag" in blob or "trondelag" in blob:
            core_guess = "trondelag"

        rec_for_date = dict(raw_rec or {})
        rec_for_date.setdefault("site", site_name)
        rec_for_date.setdefault("region", region_or_fylke or parish_or_kommune)
        rec_for_date.setdefault("country", country)
        rec_for_date.setdefault("period", period_hint)
        dating = resolve_dating(
            rec_for_date,
            core_region=core_guess,
            site_name=site_name,
            period_hint=period_hint,
            dating_text=dating_text,
            dateringsmetode=dateringsmetode,
            elevation_m=elevation_m,
        )

        return {
            "site_uid": uid,
            "site_panel_id": site_panel_id,
            "askeladden_id": askeladden_id,
            "raa_id": raa_id,
            "lamning_id": lamning_id,
            "shfa_site_id": shfa_site_id,
            "site_name": site_name or uid,
            "parish_or_kommune": parish_or_kommune,
            "region_or_fylke": region_or_fylke,
            "core_region": core_guess,
            "country": country,
            "lat": lat,
            "lon": lon,
            "geo_precision_m": geo_prec_m,
            "geo_level": geo_level,
            "coord_source": coord_source if has_coords else "unverified",
            "date_from_year": dating["date_from_year"],
            "date_to_year": dating["date_to_year"],
            "date_midpoint_year": _clean_float(dating["date_midpoint_year"]),
            "dating_method": dating["dating_method"],
            "dating_precision_years": dating["dating_precision_years"],
            "period": dating["period"],
            "period_label": dating["period_label"],
            "dating_text": dating_text,
            "dating_citation": dating["dating_citation"],
            "elevation_m_asl": _clean_float(dating.get("elevation_m_asl")),
            "motif_summary": motif_summary,
            "archives_set": {a for a in archives if a},
            "source_url": source_url,
            "source_record_count": source_record_count,
            "traced_figure_count": 0,
            "commercial_ok_figure_count": 0,
            "sample_figure_ids_list": [],
            "_alias_keys": list(alias_keys),
        }

    # 1. Norway Askeladden / Kulturminnesøk full register
    ask_path = base_dir / "sources" / "askeladden_kulturminnesok" / "sites_norway.jsonl"
    for rec in _load_jsonl(ask_path):
        fid = _clean_str(rec.get("feature_id"))
        lok_id = _clean_str(rec.get("askeladden_lokalitet_id"))
        if not fid and not lok_id:
            continue
        uid = f"askeladden:{fid or lok_id}"
        gards = _clean_str(rec.get("gardsnavn"))
        navn = _clean_str(rec.get("navn"))
        site_name = f"{gards} — {navn}" if (gards and navn and gards.lower() not in navn.lower()) else (navn or gards or f"Askeladden {fid or lok_id}")
        art = ", ".join(x for x in (_clean_str(rec.get("enkeltminneart")), _clean_str(rec.get("enkeltminne_tilleggsart"))) if x)
        aliases = [
            f"askeladden:{fid}" if fid else "",
            f"askeladden:{lok_id}" if lok_id else "",
            f"no:{_norm_name(site_name)}",
        ]
        _register_site(_make_site_entry(
            uid=uid,
            site_panel_id=f"Askeladden:{fid or lok_id}",
            site_name=site_name,
            parish_or_kommune=_clean_str(rec.get("kommune")),
            region_or_fylke=_clean_str(rec.get("fylke")),
            country="NO",
            lat=_clean_float(rec.get("lat")),
            lon=_clean_float(rec.get("lon")),
            precision_str=_clean_str(rec.get("coord_source")),
            coord_source=_clean_str(rec.get("coord_source"), "askeladden_api"),
            period_hint="",
            dating_text=_clean_str(rec.get("datering")),
            dateringsmetode=_clean_str(rec.get("dateringsmetode")),
            elevation_m=None,
            motif_summary=art or _clean_str(rec.get("enkeltminnekategori")),
            archives=["askeladden_kulturminnesok"],
            source_url=_clean_str(rec.get("link_kulturminnesok") or rec.get("link_askeladden")),
            askeladden_id=fid or lok_id,
            alias_keys=aliases,
            raw_rec=rec,
        ))

    # 2. Sweden RAÄ / Fornsök / K-samsök register (when available)
    se_path = base_dir / "sources" / "raa_fornsok_ksamsok" / "sites_sweden.jsonl"
    for rec in _load_jsonl(se_path):
        lam = _clean_str(rec.get("lamning_id") or rec.get("l_nummer"))
        raa = _clean_str(rec.get("raa_id") or rec.get("raa_nummer"))
        sid = lam or raa or _clean_str(rec.get("site_id") or rec.get("id"))
        if not sid:
            continue
        uid = f"raa:{sid}"
        site_name = _clean_str(rec.get("site_name") or rec.get("name") or rec.get("site") or raa or lam)
        panel_id = f"{lam} ({raa})" if (lam and raa) else (lam or raa)
        lat, lon, prec_str, c_prov = _extract_coords(rec)
        aliases = [
            f"raa:{lam}" if lam else "",
            f"raa:{raa}" if raa else "",
            f"panel:{panel_id.lower()}" if panel_id else "",
            f"se:{_norm_name(site_name)}",
        ]
        _register_site(_make_site_entry(
            uid=uid,
            site_panel_id=panel_id,
            site_name=site_name,
            parish_or_kommune=_clean_str(rec.get("parish") or rec.get("socken") or rec.get("municipality")),
            region_or_fylke=_clean_str(rec.get("region") or rec.get("province") or rec.get("county") or rec.get("landskap")),
            country="SE",
            lat=lat,
            lon=lon,
            precision_str=prec_str or "raa_fornsok",
            coord_source=c_prov or "raa_fornsok_ksamsok",
            period_hint=_clean_str(rec.get("period")),
            dating_text=_clean_str(rec.get("dating") or rec.get("datering")),
            dateringsmetode=_clean_str(rec.get("dating_method")),
            elevation_m=_clean_float(rec.get("elevation_m_asl")),
            motif_summary=_clean_str(rec.get("item_class") or rec.get("motifs") or rec.get("type")),
            archives=["raa_fornsok_ksamsok"],
            source_url=_clean_str(rec.get("fornsok_url") or rec.get("source_url") or rec.get("url")),
            raa_id=raa or None,
            lamning_id=lam or None,
            alias_keys=aliases,
            raw_rec=rec,
        ))

    # 3. SHFA Full Catalog (when available)
    shfa_path = base_dir / "sources" / "shfa_full" / "catalog_all.jsonl"
    for rec in _load_jsonl(shfa_path):
        shfa_id = _clean_str(rec.get("shfa_site_id") or rec.get("site_id") or rec.get("id"))
        lam = _clean_str(rec.get("lamning_id") or rec.get("l_nummer"))
        raa = _clean_str(rec.get("raa_id"))
        if not shfa_id and not lam and not raa:
            continue
        uid = f"raa:{lam}" if lam else (f"shfa:{shfa_id}" if shfa_id else f"raa:{raa}")
        site_name = _clean_str(rec.get("rock_carving_object_name") or rec.get("site_name") or rec.get("site") or rec.get("name") or raa or lam or f"SHFA {shfa_id}")
        panel_id = f"{lam} ({raa})" if (lam and raa) else (lam or raa or f"SHFA:{shfa_id}")
        country = _clean_str(rec.get("country"), "SE").upper()
        lat, lon, prec_str, c_prov = _extract_coords(rec)
        dating_tags = rec.get("dating_tags") or []
        dating_txt = ", ".join(_clean_str(t.get("english_translation") or t.get("text")) for t in dating_tags if isinstance(t, dict))
        motif_hints = rec.get("motif_hints") or []
        motif_str = ", ".join(str(m) for m in motif_hints[:8]) if isinstance(motif_hints, list) else _clean_str(rec.get("motifs"))
        aliases = [
            f"shfa:{shfa_id}" if shfa_id else "",
            f"raa:{lam}" if lam else "",
            f"raa:{raa}" if raa else "",
            f"{country.lower()}:{_norm_name(site_name)}",
        ]
        _register_site(_make_site_entry(
            uid=uid,
            site_panel_id=panel_id,
            site_name=site_name,
            parish_or_kommune=_clean_str(rec.get("parish") or rec.get("socken")),
            region_or_fylke=_clean_str(rec.get("region") or rec.get("province") or rec.get("landscape")),
            country=country,
            lat=lat,
            lon=lon,
            precision_str=prec_str or "shfa_panel",
            coord_source=c_prov or "shfa_api",
            period_hint=_clean_str(rec.get("period")),
            dating_text=dating_txt or _clean_str(rec.get("dating")),
            dateringsmetode=_clean_str(rec.get("dating_method")),
            elevation_m=_clean_float(rec.get("elevation_m_asl")),
            motif_summary=motif_str,
            archives=["shfa"],
            source_url=_clean_str(rec.get("source_url") or rec.get("url")),
            raa_id=raa or None,
            lamning_id=lam or None,
            shfa_site_id=shfa_id or None,
            source_record_count=1,
            alias_keys=aliases,
            raw_rec=rec,
        ))

    # 4. Nordic Neighbours site files (Denmark / Finland / Europeana) if present
    nn_dir = base_dir / "sources" / "nordic_neighbours"
    if nn_dir.is_dir():
        for sp in sorted(nn_dir.glob("*sites*.jsonl")):
            for rec in _load_jsonl(sp):
                sid = _clean_str(rec.get("site_id") or rec.get("id") or rec.get("source_id"))
                site_name = _clean_str(rec.get("site") or rec.get("site_name") or rec.get("title"))
                country = _clean_str(rec.get("country"), "NORDIC").upper()
                if not sid and not site_name:
                    continue
                lat, lon, prec_str, c_prov = _extract_coords(rec)
                uid = f"{country.lower()}:{sid or _norm_name(site_name)}"
                _register_site(_make_site_entry(
                    uid=uid,
                    site_panel_id=_clean_str(rec.get("site_panel_id")) or sid or None,
                    site_name=site_name or uid,
                    parish_or_kommune=_clean_str(rec.get("municipality") or rec.get("parish")),
                    region_or_fylke=_clean_str(rec.get("region")),
                    country=country,
                    lat=lat,
                    lon=lon,
                    precision_str=prec_str or "site_centroid",
                    coord_source=c_prov or _clean_str(rec.get("source_archive"), "nordic_neighbours"),
                    period_hint=_clean_str(rec.get("period")),
                    dating_text=_clean_str(rec.get("raw_dating") or rec.get("dating")),
                    dateringsmetode=_clean_str(rec.get("dating_method")),
                    elevation_m=_clean_float(rec.get("elevation_m_asl")),
                    motif_summary=_clean_str(rec.get("subtype") or rec.get("site_type") or rec.get("motifs")),
                    archives=[_clean_str(rec.get("source_archive"), sp.stem)],
                    source_url=_clean_str(rec.get("source_url") or rec.get("url")),
                    alias_keys=[f"{country.lower()}:{_norm_name(site_name)}"],
                    raw_rec=rec,
                ))

    # 5. Baseline verified site_gazetteer.json
    gaz_path = base_dir / "metadata" / "site_gazetteer.json"
    if gaz_path.is_file():
        gaz = json.loads(gaz_path.read_text(encoding="utf-8"))
        for g in gaz.get("sites", []):
            ask = _clean_str(g.get("askeladden_id"))
            lam = _clean_str(g.get("lamning_id"))
            raa = _clean_str(g.get("raa_id"))
            country = _clean_str(g.get("country"), "NORDIC").upper()
            site_name = _clean_str(g.get("site"))
            region = _clean_str(g.get("region"))
            panel_id = f"Askeladden:{ask}" if ask else (f"{lam} ({raa})" if (lam and raa) else (lam or raa or None))

            # Match existing crawled site if present
            candidate_aliases = [
                f"askeladden:{ask}" if ask else "",
                f"raa:{lam}" if lam else "",
                f"raa:{raa}" if raa else "",
                f"{country.lower()}:{_norm_name(site_name)}",
            ]
            matched_uid = next((alias_to_uid[a] for a in candidate_aliases if a in alias_to_uid), None)
            uid = matched_uid or (f"askeladden:{ask}" if ask else (f"raa:{lam}" if lam else f"gazetteer:{_clean_str(g.get('site_key')) or _norm_name(site_name)}"))
            coords = g.get("coordinates") if isinstance(g.get("coordinates"), dict) else {}
            periods = g.get("periods") or []
            per_hint = _clean_str(periods[0]) if len(periods) == 1 else ("mixed" if len(periods) > 1 else "unknown")

            _register_site(_make_site_entry(
                uid=uid,
                site_panel_id=panel_id,
                site_name=site_name,
                parish_or_kommune="",
                region_or_fylke=region,
                country=country,
                lat=_clean_float(coords.get("lat")),
                lon=_clean_float(coords.get("lon")),
                precision_str=_clean_str(coords.get("precision")),
                coord_source=_clean_str(coords.get("provenance"), "site_gazetteer"),
                period_hint=per_hint,
                dating_text="",
                dateringsmetode="",
                elevation_m=None,
                motif_summary="",
                archives=["site_gazetteer"],
                source_url=_clean_str(g.get("kulturminnesok_url") or g.get("fornsok_url") or g.get("askeladden_url")),
                askeladden_id=ask or None,
                raa_id=raa or None,
                lamning_id=lam or None,
                source_record_count=_clean_int(g.get("record_count")) or 0,
                alias_keys=candidate_aliases,
                raw_rec=g,
            ))

    # 6. Link all traced figures back to sites (creating site entries for any unindexed figure sites)
    for fig in figure_rows:
        spid = _clean_str(fig.get("site_panel_id"))
        country = _clean_str(fig.get("country"), "NORDIC").upper()
        site_name = _clean_str(fig.get("site"))
        keys = []
        if spid.lower().startswith("askeladden:"):
            ask_num = spid.split(":", 1)[1].strip()
            keys.append(f"askeladden:{ask_num}")
        elif spid:
            keys.append(f"panel:{spid.lower()}")
            m_lam = re.match(r"^(L\d{4}:\d+)", spid)
            if m_lam:
                keys.append(f"raa:{m_lam.group(1)}")
        if site_name:
            keys.append(f"{country.lower()}:{_norm_name(site_name)}")

        uid = next((alias_to_uid[k] for k in keys if k in alias_to_uid), None)
        if uid is None:
            uid = keys[0] if keys else f"figsite:{fig['figure_id']}"
            _register_site(_make_site_entry(
                uid=uid,
                site_panel_id=spid or None,
                site_name=site_name or spid or uid,
                parish_or_kommune="",
                region_or_fylke=_clean_str(fig.get("region")),
                country=country,
                lat=fig.get("lat"),
                lon=fig.get("lon"),
                precision_str=_clean_str(fig.get("geo_precision_label")),
                coord_source=_clean_str(fig.get("coordinates_provenance"), "figures_record"),
                period_hint=_clean_str(fig.get("period")),
                dating_text="",
                dateringsmetode=_clean_str(fig.get("dating_method")),
                elevation_m=fig.get("elevation_m_asl"),
                motif_summary=fig.get("motif_class", ""),
                archives=[fig.get("source_archive", "")],
                source_url=fig.get("source_url", ""),
                alias_keys=keys,
                raw_rec=fig,
            ))

        s_entry = sites_by_uid[uid]
        s_entry["traced_figure_count"] += 1
        if fig.get("commercial_ok"):
            s_entry["commercial_ok_figure_count"] += 1
        if fig.get("source_archive"):
            s_entry["archives_set"].add(fig["source_archive"])
        if len(s_entry["sample_figure_ids_list"]) < 12:
            s_entry["sample_figure_ids_list"].append(fig["figure_id"])

    # Finalize clean serializable site rows
    site_rows: List[Dict[str, Any]] = []
    for uid in sorted(sites_by_uid.keys()):
        s = sites_by_uid[uid]
        site_rows.append({
            "site_uid": s["site_uid"],
            "site_panel_id": s["site_panel_id"],
            "askeladden_id": s["askeladden_id"],
            "raa_id": s["raa_id"],
            "lamning_id": s["lamning_id"],
            "shfa_site_id": s["shfa_site_id"],
            "site_name": s["site_name"],
            "parish_or_kommune": s["parish_or_kommune"],
            "region_or_fylke": s["region_or_fylke"],
            "core_region": s["core_region"],
            "country": s["country"],
            "lat": s["lat"],
            "lon": s["lon"],
            "geo_precision_m": s["geo_precision_m"],
            "geo_level": s["geo_level"],
            "coord_source": s["coord_source"],
            "date_from_year": s["date_from_year"],
            "date_to_year": s["date_to_year"],
            "date_midpoint_year": s["date_midpoint_year"],
            "dating_method": s["dating_method"],
            "dating_precision_years": s["dating_precision_years"],
            "period": s["period"],
            "period_label": s["period_label"],
            "dating_text": s["dating_text"],
            "dating_citation": s["dating_citation"],
            "elevation_m_asl": s["elevation_m_asl"],
            "motif_summary": s["motif_summary"],
            "archives": ", ".join(sorted(s["archives_set"])),
            "source_url": s["source_url"],
            "source_record_count": s["source_record_count"],
            "traced_figure_count": s["traced_figure_count"],
            "commercial_ok_figure_count": s["commercial_ok_figure_count"],
            "sample_figure_ids": json.dumps(s["sample_figure_ids_list"]),
        })
    return site_rows


def _write_parquet(rows: List[Dict[str, Any]], schema: pa.Schema, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        cols = {field.name: pa.array([], type=field.type) for field in schema}
        table = pa.table(cols, schema=schema)
    else:
        cols = {}
        for field in schema:
            vals = [r.get(field.name) for r in rows]
            cols[field.name] = pa.array(vals, type=field.type)
        table = pa.table(cols, schema=schema)
    pq.write_table(table, out_path, compression="zstd")


def _write_jsonl(rows: List[Dict[str, Any]], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _write_csv(rows: List[Dict[str, Any]], fieldnames: List[str], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            clean = {k: ("" if r.get(k) is None else r.get(k)) for k in fieldnames}
            writer.writerow(clean)


def _write_geojson(rows: List[Dict[str, Any]], out_path: Path, drop_keys: Tuple[str, ...] = ()) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    features = []
    for r in rows:
        lat = r.get("lat")
        lon = r.get("lon")
        geom = (
            {"type": "Point", "coordinates": [round(float(lon), 6), round(float(lat), 6)]}
            if (lat is not None and lon is not None)
            else None
        )
        props = {k: v for k, v in r.items() if k not in drop_keys}
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    fc = {
        "type": "FeatureCollection",
        "name": out_path.stem,
        "features": features,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False)


_FIGURES_SCHEMA = pa.schema([
    ("figure_id", pa.string()),
    ("site_panel_id", pa.string()),
    ("site_panel_id_provenance", pa.string()),
    ("site", pa.string()),
    ("region", pa.string()),
    ("core_region", pa.string()),
    ("country", pa.string()),
    ("lat", pa.float64()),
    ("lon", pa.float64()),
    ("geo_precision_m", pa.float64()),
    ("geo_precision_label", pa.string()),
    ("geo_level", pa.string()),
    ("coordinates_provenance", pa.string()),
    ("date_from_year", pa.int32()),
    ("date_to_year", pa.int32()),
    ("date_midpoint_year", pa.float64()),
    ("dating_method", pa.string()),
    ("dating_precision_years", pa.int32()),
    ("period", pa.string()),
    ("period_label", pa.string()),
    ("dating_citation", pa.string()),
    ("elevation_m_asl", pa.float64()),
    ("motif_family", pa.string()),
    ("motif_class", pa.string()),
    ("svg_path", pa.string()),
    ("svg_figures_d", pa.large_string()),
    ("figures_path_closed", pa.bool_()),
    ("cup_marks_count", pa.int32()),
    ("cracks_count", pa.int32()),
    ("uncertain_count", pa.int32()),
    ("has_scale_bar", pa.bool_()),
    ("scale_calibrated", pa.bool_()),
    ("scale_source", pa.string()),
    ("px_per_mm", pa.float64()),
    ("width_mm", pa.float64()),
    ("height_mm", pa.float64()),
    ("area_mm2", pa.float64()),
    ("width_px", pa.float64()),
    ("height_px", pa.float64()),
    ("area_px2", pa.float64()),
    ("vertex_count", pa.int32()),
    ("solidity", pa.float64()),
    ("compactness", pa.float64()),
    ("aspect_ratio", pa.float64()),
    ("hole_count", pa.int32()),
    ("component_count", pa.int32()),
    ("source_id", pa.string()),
    ("source_archive", pa.string()),
    ("raw_path", pa.string()),
    ("sha256", pa.string()),
    ("media_type", pa.string()),
    ("source_url", pa.string()),
    ("crop_x", pa.int32()),
    ("crop_y", pa.int32()),
    ("crop_w", pa.int32()),
    ("crop_h", pa.int32()),
    ("method", pa.string()),
    ("tracer", pa.string()),
    ("date", pa.string()),
    ("rights_holder", pa.string()),
    ("licence", pa.string()),
    ("licence_url", pa.string()),
    ("commercial_ok", pa.bool_()),
    ("confidence", pa.float64()),
    ("dual_trace_iou", pa.float64()),
    ("dual_trace_qa_status", pa.string()),
    ("literature_refs", pa.string()),
    ("literature_count", pa.int32()),
    ("literature_json", pa.large_string()),
])

_SITES_SCHEMA = pa.schema([
    ("site_uid", pa.string()),
    ("site_panel_id", pa.string()),
    ("askeladden_id", pa.string()),
    ("raa_id", pa.string()),
    ("lamning_id", pa.string()),
    ("shfa_site_id", pa.string()),
    ("site_name", pa.string()),
    ("parish_or_kommune", pa.string()),
    ("region_or_fylke", pa.string()),
    ("core_region", pa.string()),
    ("country", pa.string()),
    ("lat", pa.float64()),
    ("lon", pa.float64()),
    ("geo_precision_m", pa.float64()),
    ("geo_level", pa.string()),
    ("coord_source", pa.string()),
    ("date_from_year", pa.int32()),
    ("date_to_year", pa.int32()),
    ("date_midpoint_year", pa.float64()),
    ("dating_method", pa.string()),
    ("dating_precision_years", pa.int32()),
    ("period", pa.string()),
    ("period_label", pa.string()),
    ("dating_text", pa.string()),
    ("dating_citation", pa.string()),
    ("elevation_m_asl", pa.float64()),
    ("motif_summary", pa.string()),
    ("archives", pa.string()),
    ("source_url", pa.string()),
    ("source_record_count", pa.int32()),
    ("traced_figure_count", pa.int32()),
    ("commercial_ok_figure_count", pa.int32()),
    ("sample_figure_ids", pa.string()),
])

_SOURCES_SCHEMA = pa.schema([
    ("source_id", pa.string()),
    ("source_folder", pa.string()),
    ("source_archive", pa.string()),
    ("media_type", pa.string()),
    ("quarantined", pa.bool_()),
    ("sha256", pa.string()),
    ("raw_path", pa.string()),
    ("source_url", pa.string()),
    ("site", pa.string()),
    ("region", pa.string()),
    ("country", pa.string()),
    ("period", pa.string()),
    ("lat", pa.float64()),
    ("lon", pa.float64()),
    ("rights_holder", pa.string()),
    ("licence", pa.string()),
    ("licence_url", pa.string()),
    ("commercial_ok", pa.bool_()),
    ("scale_calibrated", pa.bool_()),
    ("px_per_mm", pa.float64()),
])

_QA_SCHEMA = pa.schema([
    ("figure_id", pa.string()),
    ("core_region", pa.string()),
    ("source_archive", pa.string()),
    ("media_type", pa.string()),
    ("motif_family", pa.string()),
    ("polygon_iou", pa.float64()),
    ("hausdorff_px", pa.float64()),
    ("mean_boundary_dist_px", pa.float64()),
    ("area_delta_pct", pa.float64()),
    ("topology_agreement", pa.bool_()),
    ("severity", pa.string()),
    ("cause_category", pa.string()),
    ("overlay_path", pa.string()),
])


def _build_explorer_bundle(
    explorer_dir: Path,
    site_rows: List[Dict[str, Any]],
    figure_rows: List[Dict[str, Any]],
    svg_payloads: Dict[str, Dict[str, Any]],
    stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Write compact explorer_data.json + chunked SVG vector payloads for offline-safe web exploration."""
    explorer_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir = explorer_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for old in chunks_dir.glob("svgs_*.json"):
        old.unlink()

    # Chunk SVG vector payloads (400 figures per chunk -> ~1.5-3 MB per chunk)
    chunk_size = 400
    fig_to_chunk: Dict[str, str] = {}
    sorted_fids = sorted(svg_payloads.keys())
    for idx in range(0, len(sorted_fids), chunk_size):
        chunk_id = f"svgs_{idx // chunk_size:03d}.json"
        batch = {fid: svg_payloads[fid] for fid in sorted_fids[idx:idx + chunk_size]}
        for fid in batch:
            fig_to_chunk[fid] = f"chunks/{chunk_id}"
        (chunks_dir / chunk_id).write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")

    # Intern repeated long strings (period_label, dating_citation, coord_source) to keep bundle compact
    intern_tables: Dict[str, List[str]] = {"pl": [], "dc": [], "cs": []}
    intern_maps: Dict[str, Dict[str, int]] = {"pl": {}, "dc": {}, "cs": {}}

    def _intern(key: str, val: str) -> int:
        s = val or ""
        m = intern_maps[key]
        if s not in m:
            m[s] = len(intern_tables[key])
            intern_tables[key].append(s)
        return m[s]

    # Compact site records for fast client-side filtering (no long raw prose)
    compact_sites = []
    for s in site_rows:
        compact_sites.append({
            "u": s["site_uid"],
            "p": s["site_panel_id"],
            "n": s["site_name"],
            "k": s["parish_or_kommune"],
            "r": s["region_or_fylke"],
            "cr": s["core_region"],
            "c": s["country"],
            "la": round(s["lat"], 6) if s["lat"] is not None else None,
            "lo": round(s["lon"], 6) if s["lon"] is not None else None,
            "gp": s["geo_precision_m"],
            "gl": s["geo_level"],
            "cs": _intern("cs", s["coord_source"]),
            "df": s["date_from_year"],
            "dt": s["date_to_year"],
            "dm": s["dating_method"],
            "pe": s["period"],
            "pl": _intern("pl", s["period_label"]),
            "dc": _intern("dc", s["dating_citation"]),
            "dx": s["dating_text"],
            "el": s["elevation_m_asl"],
            "ms": s["motif_summary"][:120],
            "ar": s["archives"],
            "ur": s["source_url"],
            "fc": s["traced_figure_count"],
            "cc": s["commercial_ok_figure_count"],
            "sf": json.loads(s["sample_figure_ids"]),
        })

    # Compact figure records (omit huge svg_figures_d & literature_json; kept in chunks & dataset)
    compact_figures = []
    for f in figure_rows:
        compact_figures.append({
            "id": f["figure_id"],
            "p": f["site_panel_id"],
            "s": f["site"],
            "r": f["region"],
            "cr": f["core_region"],
            "c": f["country"],
            "la": round(f["lat"], 6) if f["lat"] is not None else None,
            "lo": round(f["lon"], 6) if f["lon"] is not None else None,
            "gp": f["geo_precision_m"],
            "gl": f["geo_level"],
            "df": f["date_from_year"],
            "dt": f["date_to_year"],
            "dm": f["dating_method"],
            "pe": f["period"],
            "pl": _intern("pl", f["period_label"]),
            "dc": _intern("dc", f["dating_citation"]),
            "mf": f["motif_family"],
            "mc": f["motif_class"],
            "cm": f["cup_marks_count"],
            "ck": f["cracks_count"],
            "un": f["uncertain_count"],
            "sc": f["scale_calibrated"],
            "wm": f["width_mm"],
            "hm": f["height_mm"],
            "am": f["area_mm2"],
            "wp": f["width_px"],
            "hp": f["height_px"],
            "ar": f["source_archive"],
            "si": f["source_id"],
            "mt": f["media_type"],
            "sh": f["sha256"][:16],
            "ur": f["source_url"],
            "rh": f["rights_holder"],
            "li": f["licence"],
            "co": f["commercial_ok"],
            "cf": f["confidence"],
            "io": f["dual_trace_iou"],
            "qs": f["dual_trace_qa_status"],
            "lr": f["literature_refs"],
            "ch": fig_to_chunk.get(f["figure_id"], ""),
            "sp": f["svg_path"],
        })

    bundle = {
        "schema_version": "1.1.0",
        "generated_at": stats["generated_at"],
        "stats": stats,
        "strings": intern_tables,
        "sites": compact_sites,
        "figures": compact_figures,
    }
    bundle_path = explorer_dir / "explorer_data.json"
    bundle_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")

    chunks_bytes = sum(p.stat().st_size for p in chunks_dir.glob("svgs_*.json"))
    return {
        "explorer_data_bytes": bundle_path.stat().st_size,
        "svg_chunks_count": len(list(chunks_dir.glob("svgs_*.json"))),
        "svg_chunks_bytes": chunks_bytes,
        "total_explorer_bytes": bundle_path.stat().st_size + chunks_bytes,
    }


def _render_hf_readme(stats: Dict[str, Any]) -> str:
    n_fig = stats["figures_count"]
    n_comm = stats["figures_commercial_safe_count"]
    n_res = stats["figures_research_only_count"]
    n_sites = stats["sites_count"]
    n_src = stats["sources_count"]
    n_qa = stats["dual_trace_qa_count"]

    return f"""---
pretty_name: Nordic Helleristninger — Open Archaeological Rock Art Dataset
license: other
license_name: mixed-open-nordic-rock-art
license_link: https://creativecommons.org/licenses/by-sa/4.0/
language:
  - en
  - no
  - sv
  - da
  - fi
task_categories:
  - image-segmentation
  - image-classification
  - object-detection
  - unconditional-image-generation
tags:
  - archaeology
  - rock-art
  - helleristninger
  - hallristningar
  - nordic-heritage
  - svg
  - vector-graphics
  - geospatial
  - geojson
  - cultural-heritage
size_categories:
  - 1K<n<10K
configs:
  - config_name: figures
    default: true
    data_files:
      - split: train
        path: figures.parquet
      - split: commercial_safe
        path: splits/figures_commercial_safe.parquet
      - split: research_only
        path: splits/figures_research_only.parquet
  - config_name: sites
    data_files:
      - split: train
        path: sites.parquet
  - config_name: sources
    data_files:
      - split: train
        path: sources.parquet
  - config_name: dual_trace_qa
    data_files:
      - split: train
        path: dual_trace_qa.parquet
---

# Nordic Helleristninger — Open Archaeological Rock Art Dataset (`v2026.W48`)

Production-grade open scientific dataset of Nordic prehistoric rock carvings (*helleristninger* / *hällristningar*) spanning Norway, Sweden, Denmark, and Finland (`9000 BCE – 500 CE`), combining:

1. **Multi-layer true-scale SVG figure tracings** (`figures`, **{n_fig:,}** traced figures; `#figures`, `#cup_marks`, `#cracks`, `#uncertain`, `#scale_bar`).
2. **Consolidated geolocated site & panel register** (`sites`, **{n_sites:,}** sites/panels across Askeladden/Kulturminnesøk, RAÄ/Fornsök/SHFA, Nationalmuseet, and Finna/Europeana).
3. **SHA-256 verified archival manifest index** (`sources`, **{n_src:,}** archival records with strict `painted_photo` quarantine).
4. **Independent >=5% stratified dual-trace QA** (`dual_trace_qa`, **{n_qa:,}** evaluated figures comparing Sauvola/local-gradient vs Otsu/morphological marching-squares).

> **Scientific Integrity Policy:** Zero modern painted photos (`painted_photo`) are traced. Uncalibrated archival plates keep `scale_calibrated: false` (`width_mm=null`). Unverified coordinates remain `null` with explicit provenance. Radiocarbon `14C` is never fabricated.

## Dataset Summary (`{stats['generated_at']}`)

| Metric | Value |
|---|---|
| Traced Figures (`figures.parquet`) | **{n_fig:,}** (`{n_comm:,}` commercial-safe / `{n_res:,}` research-only) |
| Geolocated Figures (`EPSG:4326`) | **{stats['figures_geolocated_count']:,}** ({stats['figures_geolocated_pct']}%) |
| Numerically Dated Figures | **{stats['figures_dated_count']:,}** ({stats['figures_dated_pct']}%) |
| Consolidated Sites & Panels (`sites.parquet`) | **{n_sites:,}** ({stats['sites_geolocated_pct']}% geolocated, {stats['sites_dated_pct']}% dated) |
| Archival Source Index (`sources.parquet`) | **{n_src:,}** ({stats['sources_quarantined_count']:,} `painted_photo` quarantined) |
| Dual-Trace QA Subset (`dual_trace_qa.parquet`) | **{n_qa:,}** (mean IoU `{stats['qa_mean_iou']}`, median IoU `{stats['qa_median_iou']}`) |

## Splits & Licensing

- `commercial_safe` (`splits/figures_commercial_safe.parquet`, **{n_comm:,}** figures): `CC0 1.0`, `Public Domain`, `CC BY`, `CC BY-SA` (`commercial_ok == true`).
- `research_only` (`splits/figures_research_only.parquet`, **{n_res:,}** figures): Open non-commercial research collections (e.g., Alta Museum `CC BY-NC-SA`-equivalent, `commercial_ok == false`).

## Loading with Hugging Face `datasets` & PyTorch

```python
from datasets import load_dataset
import torch

# Load all traced figures (or split="commercial_safe" / "research_only")
ds_figures = load_dataset("helleristninger/dataset", name="figures", split="commercial_safe")
ds_sites = load_dataset("helleristninger/dataset", name="sites", split="train")
ds_qa = load_dataset("helleristninger/dataset", name="dual_trace_qa", split="train")

# Filter Bronze Age ships in Sweden with verified coordinates
ships_se = ds_figures.filter(
    lambda x: x["country"] == "SE"
    and x["motif_class"] == "bronze_age_ship"
    and x["lat"] is not None
)

# PyTorch DataLoader integration over shape morphometrics + SVG path tokens
torch_ds = ships_se.with_format(
    "torch",
    columns=["solidity", "compactness", "aspect_ratio", "date_midpoint_year", "cup_marks_count"],
)
loader = torch.utils.data.DataLoader(torch_ds, batch_size=32, shuffle=True)
```

## Config Schemas

- **`figures`**: `figure_id`, `site_panel_id`, `site`, `region`, `core_region`, `country`, `lat`, `lon`, `geo_precision_m`, `geo_level`, `date_from_year`, `date_to_year`, `date_midpoint_year`, `dating_method`, `period`, `period_label`, `dating_citation`, `motif_family`, `motif_class`, `svg_path`, `svg_figures_d`, `figures_path_closed`, `cup_marks_count`, `cracks_count`, `uncertain_count`, `scale_calibrated`, `width_mm`, `height_mm`, `area_mm2`, `licence`, `commercial_ok`, `confidence`, `dual_trace_iou`, `dual_trace_qa_status`, `literature_refs`.
- **`sites`**: `site_uid`, `site_panel_id`, `askeladden_id`, `raa_id`, `lamning_id`, `shfa_site_id`, `site_name`, `parish_or_kommune`, `region_or_fylke`, `country`, `lat`, `lon`, `geo_precision_m`, `geo_level`, `date_from_year`, `date_to_year`, `date_midpoint_year`, `dating_method`, `period`, `period_label`, `dating_citation`, `traced_figure_count`, `archives`, `source_url`.
- **`sources`**: `source_id`, `source_folder`, `source_archive`, `media_type`, `quarantined`, `sha256`, `raw_path`, `source_url`, `country`, `rights_holder`, `licence`, `commercial_ok`.
- **`dual_trace_qa`**: `figure_id`, `core_region`, `source_archive`, `media_type`, `motif_family`, `polygon_iou`, `hausdorff_px`, `mean_boundary_dist_px`, `area_delta_pct`, `topology_agreement`, `severity`, `cause_category`, `overlay_path`.
"""


def build_all(
    base_dir: Path = BASE_DIR,
    dataset_dir: Path = DATASET_DIR,
    explorer_dir: Path = EXPLORER_DIR,
) -> Dict[str, Any]:
    """Build all Hugging Face dataset artifacts and the interactive web Explorer bundle."""
    dataset_dir.mkdir(parents=True, exist_ok=True)
    qa_lookup = _load_qa_lookup(base_dir / "qa")

    figure_rows, svg_payloads = _build_figure_rows(base_dir, qa_lookup)
    source_rows = _load_sources_records(base_dir / "sources")
    site_rows = _consolidate_sites(base_dir, figure_rows)

    qa_rows = []
    for fid in sorted(qa_lookup.keys()):
        q = qa_lookup[fid]
        qa_rows.append({
            "figure_id": fid,
            "core_region": _clean_str(q.get("core_region")),
            "source_archive": _clean_str(q.get("source_archive")),
            "media_type": _clean_str(q.get("media_type")),
            "motif_family": _clean_str(q.get("motif_family")),
            "polygon_iou": _clean_float(q.get("polygon_iou")) or 0.0,
            "hausdorff_px": _clean_float(q.get("hausdorff_px")) or 0.0,
            "mean_boundary_dist_px": _clean_float(q.get("mean_boundary_dist_px")) or 0.0,
            "area_delta_pct": _clean_float(q.get("area_delta_pct")) or 0.0,
            "topology_agreement": bool(q.get("topology_agreement", False)),
            "severity": _clean_str(q.get("severity")),
            "cause_category": _clean_str(q.get("cause_category")),
            "overlay_path": _clean_str(q.get("overlay_path")).replace("\\", "/"),
        })

    # Write figures outputs + license splits
    _write_parquet(figure_rows, _FIGURES_SCHEMA, dataset_dir / "figures.parquet")
    comm_rows = [r for r in figure_rows if r["commercial_ok"]]
    res_rows = [r for r in figure_rows if not r["commercial_ok"]]
    _write_parquet(comm_rows, _FIGURES_SCHEMA, dataset_dir / "splits" / "figures_commercial_safe.parquet")
    _write_parquet(res_rows, _FIGURES_SCHEMA, dataset_dir / "splits" / "figures_research_only.parquet")
    _write_jsonl(figure_rows, dataset_dir / "figures.jsonl")
    fig_csv_cols = [f.name for f in _FIGURES_SCHEMA if f.name not in ("svg_figures_d", "literature_json")]
    _write_csv(figure_rows, fig_csv_cols, dataset_dir / "figures.csv")
    _write_geojson(figure_rows, dataset_dir / "figures.geojson", drop_keys=("svg_figures_d", "literature_json"))

    # Write sites outputs
    _write_parquet(site_rows, _SITES_SCHEMA, dataset_dir / "sites.parquet")
    _write_jsonl(site_rows, dataset_dir / "sites.jsonl")
    _write_geojson(site_rows, dataset_dir / "sites.geojson", drop_keys=("dating_text",))

    # Write sources & dual_trace_qa parquet
    _write_parquet(source_rows, _SOURCES_SCHEMA, dataset_dir / "sources.parquet")
    _write_parquet(qa_rows, _QA_SCHEMA, dataset_dir / "dual_trace_qa.parquet")

    n_fig = len(figure_rows)
    n_sites = len(site_rows)
    fig_geo = sum(1 for r in figure_rows if r["lat"] is not None and r["lon"] is not None)
    fig_dated = sum(1 for r in figure_rows if r["date_from_year"] is not None)
    site_geo = sum(1 for r in site_rows if r["lat"] is not None and r["lon"] is not None)
    site_dated = sum(1 for r in site_rows if r["date_from_year"] is not None)

    ious = [r["polygon_iou"] for r in qa_rows]
    ious_sorted = sorted(ious)
    mean_iou = round(sum(ious) / len(ious), 4) if ious else 0.0
    median_iou = round(ious_sorted[len(ious_sorted) // 2], 4) if ious_sorted else 0.0

    stats: Dict[str, Any] = {
        "schema_version": "1.0.0",
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "figures_count": n_fig,
        "figures_commercial_safe_count": len(comm_rows),
        "figures_research_only_count": len(res_rows),
        "figures_geolocated_count": fig_geo,
        "figures_geolocated_pct": round(100.0 * fig_geo / max(1, n_fig), 2),
        "figures_dated_count": fig_dated,
        "figures_dated_pct": round(100.0 * fig_dated / max(1, n_fig), 2),
        "figures_calibrated_count": sum(1 for r in figure_rows if r["scale_calibrated"]),
        "sites_count": n_sites,
        "sites_geolocated_count": site_geo,
        "sites_geolocated_pct": round(100.0 * site_geo / max(1, n_sites), 2),
        "sites_dated_count": site_dated,
        "sites_dated_pct": round(100.0 * site_dated / max(1, n_sites), 2),
        "sites_by_country": dict(Counter(r["country"] for r in site_rows)),
        "figures_by_country": dict(Counter(r["country"] for r in figure_rows)),
        "figures_by_core_region": dict(Counter(r["core_region"] for r in figure_rows)),
        "figures_by_dating_method": dict(Counter(r["dating_method"] for r in figure_rows)),
        "sites_by_dating_method": dict(Counter(r["dating_method"] for r in site_rows)),
        "figures_by_family": dict(Counter(r["motif_family"] for r in figure_rows)),
        "figures_by_archive": dict(Counter(r["source_archive"] for r in figure_rows)),
        "sources_count": len(source_rows),
        "sources_quarantined_count": sum(1 for r in source_rows if r["quarantined"]),
        "dual_trace_qa_count": len(qa_rows),
        "qa_mean_iou": mean_iou,
        "qa_median_iou": median_iou,
    }

    explorer_info = _build_explorer_bundle(explorer_dir, site_rows, figure_rows, svg_payloads, stats)
    stats["explorer_bundle"] = explorer_info

    (dataset_dir / "dataset_stats.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    (dataset_dir / "README.md").write_text(_render_hf_readme(stats), encoding="utf-8")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Nordic Helleristninger HF Dataset + Explorer Bundle")
    parser.add_argument("--base-dir", type=Path, default=BASE_DIR)
    parser.add_argument("--dataset-dir", type=Path, default=DATASET_DIR)
    parser.add_argument("--explorer-dir", type=Path, default=EXPLORER_DIR)
    args = parser.parse_args()
    stats = build_all(base_dir=args.base_dir, dataset_dir=args.dataset_dir, explorer_dir=args.explorer_dir)
    print(json.dumps(stats, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
