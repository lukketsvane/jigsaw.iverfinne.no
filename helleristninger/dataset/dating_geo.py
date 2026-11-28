"""Geospatial precision and chronological dating resolution for Nordic rock-art records.

Uses helleristninger.metadata.chronology when available (Wave 2+), with an honest,
literature-grounded fallback for Wave 1 and unannotated archival strings.
Never fabricates radiocarbon 14C dates.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

try:
    from helleristninger.metadata import chronology as _ext_chronology  # type: ignore
except Exception:
    _ext_chronology = None


DATING_METHODS = {
    "radiocarbon_14c",
    "shoreline_displacement",
    "stratigraphic",
    "typological_stylistic",
    "composite_archival",
    "unknown",
}

PERIOD_LABELS: Dict[str, str] = {
    "stone_age_hunter": "Stone Age Hunter-Gatherer (Veideristninger, c. 7000–1800 BCE)",
    "bronze_age_agrarian": "Bronze Age Agrarian (Jordbruksristninger, c. 1800–500 BCE)",
    "mixed": "Multi-Period / Transitional Panel (c. 5000 BCE–500 CE)",
    "iron_age": "Iron Age / Post-Bronze Tradition (c. 500 BCE–1050 CE)",
    "unknown": "Undetermined Chronology",
}


def parse_geo_precision(
    precision_str: Optional[str],
    coord_source: Optional[str] = None,
    has_coords: bool = True,
) -> Tuple[Optional[float], str]:
    """Convert raw archival coordinate precision/source strings into (meters, geo_level)."""
    if not has_coords:
        return None, "unverified"

    raw = f"{precision_str or ''} {coord_source or ''}".lower()
    m_range = re.search(r"(\d+)\s*m\s*(?:to|-)\s*(\d+)\s*m", raw)
    if m_range:
        lo, hi = float(m_range.group(1)), float(m_range.group(2))
        return round((lo + hi) / 2.0, 1), "site_centroid"

    m_single = re.search(r"(?:approx[_\s]*|within[_\s]*)?(\d+(?:\.\d+)?)\s*m\b", raw)
    if m_single:
        meters = float(m_single.group(1))
        level = "panel_exact" if meters <= 10.0 else "site_centroid"
        return meters, level

    if any(k in raw for k in ("panel_exact", "gps", "rtk", "exact_point", "shfa_panel")):
        return 5.0, "panel_exact"
    if any(k in raw for k in ("askeladden_senterpunkt", "senterpunkt", "multipolygon_centroid", "shfa", "raa", "fornsok", "lamning")):
        return 25.0, "site_centroid"
    if any(k in raw for k in ("parish", "socken")):
        return 2000.0, "parish_centroid"
    if any(k in raw for k in ("municipality", "kommune", "fylke", "region")):
        return 5000.0, "municipality_centroid"
    if any(k in raw for k in ("manifest", "gazetteer", "wikimedia", "finna", "nationalmuseet", "europeana")):
        return 100.0, "site_centroid"
    return 50.0, "site_centroid"


def _normalize_dating_method(raw_method: Optional[str]) -> str:
    if not raw_method:
        return "unknown"
    s = str(raw_method).strip().lower()
    if s in DATING_METHODS:
        return s
    if any(k in s for k in ("14c", "radiocarbon", "c14", "ams")):
        return "radiocarbon_14c"
    if any(k in s for k in ("shore", "strand", "elevation", "asl", "isostat")):
        return "shoreline_displacement"
    if any(k in s for k in ("stratig", "superpos", "overlagr", "excavat", "utgrav")):
        return "stratigraphic"
    if any(k in s for k in ("typolog", "stylist", "stil", "montelius", "artefact")):
        return "typological_stylistic"
    if any(k in s for k in ("composite", "multi", "archival", "sammensatt")):
        return "composite_archival"
    return "unknown"


def _parse_archival_dating_text(
    dating_text: str,
    dateringsmetode: str = "",
) -> Optional[Dict[str, Any]]:
    """Parse Norwegian/Swedish/English heritage dating strings without fabricating precision."""
    t = (dating_text or "").strip().lower()
    if not t or t in ("ukjent", "okänd", "unknown", "udatert", "datering mangler"):
        return None

    method = _normalize_dating_method(dateringsmetode)

    # Explicit numeric BCE/CE ranges in text (e.g. "1800-500 f.Kr." or "4200-3600 BCE")
    m_bce = re.search(r"(\d{3,4})\s*[-–]\s*(\d{3,4})\s*(?:f\.?\s*kr\.?|bce|bc|fvt)", t)
    if m_bce:
        a, b = -int(m_bce.group(1)), -int(m_bce.group(2))
        lo, hi = min(a, b), max(a, b)
        return {
            "date_from_year": lo,
            "date_to_year": hi,
            "period": "stone_age_hunter" if hi <= -1800 else ("bronze_age_agrarian" if lo >= -1900 else "mixed"),
            "dating_method": method if method != "unknown" else "composite_archival",
            "dating_citation": "Archival heritage register explicit date span",
        }

    # Combined spans first
    if ("steinalder" in t or "stenålder" in t) and ("bronsealder" in t or "bronsålder" in t):
        return {
            "date_from_year": -4500,
            "date_to_year": -500,
            "period": "mixed",
            "dating_method": method if method != "unknown" else "composite_archival",
            "dating_citation": "Riksantikvaren / RAÄ archival period classification (Stone Age–Bronze Age)",
        }
    if ("bronsealder" in t or "bronsålder" in t) and ("jernalder" in t or "järnålder" in t):
        return {
            "date_from_year": -1800,
            "date_to_year": 500,
            "period": "bronze_age_agrarian",
            "dating_method": method if method != "unknown" else "typological_stylistic",
            "dating_citation": "Riksantikvaren / RAÄ archival period classification (Bronze Age–Iron Age)",
        }

    # Stone Age phases
    if any(k in t for k in ("eldre steinalder", "äldre stenålder", "mesolit", "mesolithic")):
        return {
            "date_from_year": -9000,
            "date_to_year": -4000,
            "period": "stone_age_hunter",
            "dating_method": method if method != "unknown" else "shoreline_displacement",
            "dating_citation": "Riksantikvaren / RAÄ Mesolithic classification (c. 9000–4000 BCE)",
        }
    if any(k in t for k in ("yngre steinalder", "yngre stenålder", "neolit", "neolithic")):
        return {
            "date_from_year": -4000,
            "date_to_year": -1800,
            "period": "stone_age_hunter",
            "dating_method": method if method != "unknown" else "typological_stylistic",
            "dating_citation": "Riksantikvaren / RAÄ Neolithic classification (c. 4000–1800 BCE)",
        }
    if any(k in t for k in ("steinalder", "stenålder", "stone age", "veideristning", "fangstfolk")):
        return {
            "date_from_year": -7000,
            "date_to_year": -1800,
            "period": "stone_age_hunter",
            "dating_method": method if method != "unknown" else "typological_stylistic",
            "dating_citation": "Nordic Stone Age hunter-gatherer tradition (c. 7000–1800 BCE)",
        }

    # Bronze Age phases
    if any(k in t for k in ("eldre bronsealder", "äldre bronsålder", "early bronze")):
        return {
            "date_from_year": -1800,
            "date_to_year": -1100,
            "period": "bronze_age_agrarian",
            "dating_method": method if method != "unknown" else "typological_stylistic",
            "dating_citation": "Montelius Periods I–III (c. 1800–1100 BCE)",
        }
    if any(k in t for k in ("yngre bronsealder", "yngre bronsålder", "late bronze")):
        return {
            "date_from_year": -1100,
            "date_to_year": -500,
            "period": "bronze_age_agrarian",
            "dating_method": method if method != "unknown" else "typological_stylistic",
            "dating_citation": "Montelius Periods IV–VI (c. 1100–500 BCE)",
        }
    if any(k in t for k in ("bronsealder", "bronsålder", "bronze age", "jordbruksristning", "hällristning")):
        return {
            "date_from_year": -1800,
            "date_to_year": -500,
            "period": "bronze_age_agrarian",
            "dating_method": method if method != "unknown" else "typological_stylistic",
            "dating_citation": "Nordic Bronze Age agrarian tradition (c. 1800–500 BCE)",
        }

    # Iron Age / Historical
    if any(k in t for k in ("førromersk", "förromersk", "pre-roman")):
        return {
            "date_from_year": -500,
            "date_to_year": 0,
            "period": "bronze_age_agrarian",
            "dating_method": method if method != "unknown" else "typological_stylistic",
            "dating_citation": "Pre-Roman Iron Age (c. 500–1 BCE)",
        }
    if any(k in t for k in ("jernalder", "järnålder", "iron age", "romertid", "folkevandring", "merovingertid", "vikingtid")):
        return {
            "date_from_year": -500,
            "date_to_year": 500,
            "period": "mixed",
            "dating_method": method if method != "unknown" else "typological_stylistic",
            "dating_citation": "Nordic Iron Age archival classification (c. 500 BCE–500 CE)",
        }
    if any(k in t for k in ("middelalder", "medeltid", "nyere tid", "efterreformatorisk", "historisk")):
        return {
            "date_from_year": 500,
            "date_to_year": 1800,
            "period": "mixed",
            "dating_method": method if method != "unknown" else "typological_stylistic",
            "dating_citation": "Post-prehistoric / historical archival classification",
        }
    return None


_REGIONAL_DEFAULTS: Dict[Tuple[str, str], Tuple[int, int, str, str]] = {
    ("alta", "stone_age_hunter"): (-5200, -1800, "shoreline_displacement", "Helskog 1988, 2012 (Alta Phases I–IV shoreline displacement)"),
    ("alta", "bronze_age_agrarian"): (-1800, -200, "shoreline_displacement", "Helskog 1988, 2012 (Alta Phase V shoreline displacement)"),
    ("alta", "mixed"): (-5200, -200, "shoreline_displacement", "Helskog 1988, 2012 (Alta Phases I–V)"),
    ("vingen", "stone_age_hunter"): (-4900, -2200, "composite_archival", "Lødøen & Mandt 2010, 2012; Lødøen 2015 (Vingen shoreline & excavation 14C context)"),
    ("namforsen", "stone_age_hunter"): (-4200, -1800, "shoreline_displacement", "Hallström 1960; Forsberg 1993; Gjerde 2010 (Nämforsen shoreline & seriation)"),
    ("namforsen", "mixed"): (-4200, -500, "composite_archival", "Hallström 1960; Forsberg 1993 (Nämforsen Stone Age–Bronze Age)"),
    ("bohuslan_tanum", "bronze_age_agrarian"): (-1800, -500, "shoreline_displacement", "Ling 2008, 2014; Milstreu & Prøhl 1996 (Bohuslän shoreline & Montelius typology)"),
    ("ostfold", "bronze_age_agrarian"): (-1800, -500, "shoreline_displacement", "Vogt 2011; Marstrander 1963 (Østfold shoreline & typology)"),
    ("trondelag", "stone_age_hunter"): (-6000, -1800, "shoreline_displacement", "Gjessing 1936; Sognnes 2001 (Trøndelag hunter-gatherer shoreline)"),
    ("trondelag", "bronze_age_agrarian"): (-1800, -500, "typological_stylistic", "Sognnes 2001 (Trøndelag agrarian carvings)"),
    ("trondelag", "mixed"): (-5000, -500, "composite_archival", "Gjessing 1936; Sognnes 2001 (Bardal / Trøndelag multi-period)"),
}

_PERIOD_DEFAULTS: Dict[str, Tuple[int, int, str, str]] = {
    "stone_age_hunter": (-7000, -1800, "typological_stylistic", "Nordic Veideristninger typology (c. 7000–1800 BCE)"),
    "bronze_age_agrarian": (-1800, -500, "typological_stylistic", "Nordic Jordbruksristninger typology (c. 1800–500 BCE)"),
    "mixed": (-5000, 500, "composite_archival", "Multi-period Nordic rock-art sequence (c. 5000 BCE–500 CE)"),
}


def resolve_dating(
    record: Dict[str, Any],
    core_region: str = "other",
    site_name: str = "",
    period_hint: str = "",
    dating_text: str = "",
    dateringsmetode: str = "",
    elevation_m: Optional[float] = None,
) -> Dict[str, Any]:
    """Return structured numeric chronology fields for a figure or site record."""
    # 1. Delegate to chronology.py if available in Wave 2+
    if _ext_chronology is not None and hasattr(_ext_chronology, "resolve_chronology"):
        try:
            res = _ext_chronology.resolve_chronology(
                record,
                site=site_name or record.get("site"),
                region=record.get("region") or record.get("region_or_fylke") or record.get("kommune"),
                country=record.get("country"),
                period=period_hint or record.get("period"),
                title=record.get("title") or record.get("navn"),
                description=record.get("description") or record.get("informasjon"),
                raw_dating=dating_text or record.get("raw_dating") or record.get("datering"),
                elevation_m_asl=elevation_m if elevation_m is not None else record.get("elevation_m_asl"),
            )
            if isinstance(res, dict) and res.get("date_from_year") is not None:
                df = int(res["date_from_year"])
                dt = int(res["date_to_year"]) if res.get("date_to_year") is not None else df
                if df > dt:
                    df, dt = dt, df
                mid = res.get("date_midpoint_year")
                mid_val = float(mid) if mid is not None else round((df + dt) / 2.0, 1)
                per = str(period_hint or record.get("period") or "").strip().lower()
                if not per or per == "unknown":
                    if dt <= -1800:
                        per = "stone_age_hunter"
                    elif df >= -1900 and dt <= 0:
                        per = "bronze_age_agrarian"
                    else:
                        per = "mixed"
                return {
                    "date_from_year": df,
                    "date_to_year": dt,
                    "date_midpoint_year": mid_val,
                    "dating_method": _normalize_dating_method(res.get("dating_method")),
                    "dating_precision_years": int(res.get("dating_precision_years") or max(1, (dt - df) // 2)),
                    "period": per,
                    "period_label": str(res.get("period_label") or PERIOD_LABELS.get(per, PERIOD_LABELS["unknown"])),
                    "dating_citation": str(res.get("dating_citation") or "chronology.py"),
                    "elevation_m_asl": res.get("elevation_m_asl", elevation_m),
                }
        except Exception:
            pass

    # 2. Check if record already carries numeric dating fields
    if record.get("date_from_year") is not None and record.get("date_to_year") is not None:
        df = int(record["date_from_year"])
        dt = int(record["date_to_year"])
        if df > dt:
            df, dt = dt, df
        per = str(record.get("period") or period_hint or "unknown").lower()
        return {
            "date_from_year": df,
            "date_to_year": dt,
            "date_midpoint_year": round((df + dt) / 2.0, 1),
            "dating_method": _normalize_dating_method(record.get("dating_method") or dateringsmetode),
            "dating_precision_years": max(1, (dt - df) // 2),
            "period": per,
            "period_label": str(record.get("period_label") or PERIOD_LABELS.get(per, PERIOD_LABELS["unknown"])),
            "dating_citation": str(record.get("dating_citation") or "Archival record"),
            "elevation_m_asl": record.get("elevation_m_asl", elevation_m),
        }

    # 3. Parse archival dating text (e.g. Riksantikvaren datering)
    parsed = _parse_archival_dating_text(dating_text, dateringsmetode)
    if parsed is not None:
        df = parsed["date_from_year"]
        dt = parsed["date_to_year"]
        per = parsed["period"]
        return {
            "date_from_year": df,
            "date_to_year": dt,
            "date_midpoint_year": round((df + dt) / 2.0, 1),
            "dating_method": parsed["dating_method"],
            "dating_precision_years": max(1, (dt - df) // 2),
            "period": per,
            "period_label": PERIOD_LABELS.get(per, PERIOD_LABELS["unknown"]),
            "dating_citation": parsed["dating_citation"],
            "elevation_m_asl": elevation_m,
        }

    # 4. Regional + period defaults grounded in Nordic rock-art literature
    per = str(period_hint or record.get("period") or "unknown").strip().lower()
    cr = str(core_region or "other").strip().lower()
    site_l = (site_name or "").lower()

    # Special well-dated sites (e.g. Kivik 14C, Alta, Vingen, Bøla, Leiknes)
    if "kivik" in site_l:
        df, dt, meth, cit = -1400, -1100, "radiocarbon_14c", "Goldhahn 2009, 2013 (Kivik cairn AMS 14C dates)"
        per = "bronze_age_agrarian"
    elif (cr, per) in _REGIONAL_DEFAULTS:
        df, dt, meth, cit = _REGIONAL_DEFAULTS[(cr, per)]
    elif per in _PERIOD_DEFAULTS:
        df, dt, meth, cit = _PERIOD_DEFAULTS[per]
    else:
        return {
            "date_from_year": None,
            "date_to_year": None,
            "date_midpoint_year": None,
            "dating_method": "unknown",
            "dating_precision_years": None,
            "period": "unknown",
            "period_label": PERIOD_LABELS["unknown"],
            "dating_citation": "Undated in source register",
            "elevation_m_asl": elevation_m,
        }

    return {
        "date_from_year": df,
        "date_to_year": dt,
        "date_midpoint_year": round((df + dt) / 2.0, 1),
        "dating_method": meth,
        "dating_precision_years": max(1, (dt - df) // 2),
        "period": per,
        "period_label": PERIOD_LABELS.get(per, PERIOD_LABELS["unknown"]),
        "dating_citation": cit,
        "elevation_m_asl": elevation_m,
    }
