"""Rigorous, reproducible chronological & dating engine for Nordic & Baltic rock art.

Assigns structured numeric dating fields grounded in peer-reviewed archaeological
literature (see ``chronology_bibliography.json``). Never fabricates radiocarbon (14C)
dates or lab codes: explicitly distinguishes actual 14C excavation contexts
(Vingen, Kivik/Bredarör) from post-glacial shoreline displacement curves
(Alta Phases 1-5, Bohuslän/Tanum, Østfold, Trøndelag, Nordland, Nämforsen, Finland)
and typological-stylistic metalwork correlation (Bornholm, Skåne, Montelius Per. I-VI).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BIBLIOGRAPHY_PATH = Path(__file__).resolve().parent / "chronology_bibliography.json"

VALID_DATING_METHODS = frozenset({
    "radiocarbon_14c",
    "shoreline_displacement",
    "stratigraphic",
    "typological_stylistic",
    "composite_archival",
    "unknown",
})

_ROMAN_TO_INT = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
}

_INT_TO_ROMAN = {v: k for k, v in _ROMAN_TO_INT.items()}


@dataclass(frozen=True)
class ChronologyRule:
    """Declarative archaeological dating rule tied to published literature."""

    rule_id: str
    pattern: str
    date_from_year: int
    date_to_year: int
    dating_method: str
    period_label: str
    dating_citation: str
    radiocarbon_refs: Tuple[str, ...] = ()
    elevation_m_asl: Optional[float] = None
    country: Optional[str] = None
    period_filter: Optional[str] = None


# Ordered from most specific (site/panel/phase/14C) to broader regional/tradition rules.
_ARCHAEOLOGICAL_RULES: Tuple[ChronologyRule, ...] = (
    # --- 1. ACTUAL RADIOCARBON (14C) CONTEXTS ---
    ChronologyRule(
        rule_id="kivik_bredaror_14c",
        pattern=r"\b(kivik|kiviksgraven|bredarör)\b",
        date_from_year=-1400,
        date_to_year=-1100,
        dating_method="radiocarbon_14c",
        period_label="Nordic Bronze Age — Montelius Per. II-III (Kivik 14C)",
        dating_citation="Goldhahn 2009; Goldhahn 2013",
        radiocarbon_refs=(
            "Goldhahn 2009 (AMS 14C on human bone/tooth from Bredarör cist, ca. 1400-1100 cal BCE)",
            "Goldhahn 2013",
        ),
        country="SE",
    ),
    ChronologyRule(
        rule_id="vingen_14c",
        pattern=r"\b(vingen|vingeneset|vindbakken|brattbakken|bakka.*vingen)\b",
        date_from_year=-5000,
        date_to_year=-4000,
        dating_method="radiocarbon_14c",
        period_label="Late Mesolithic — Vingen Hunter-Gatherer (Excavation 14C)",
        dating_citation="Lødøen 2010; Lødøen 2015; Lødøen & Mandt 2012",
        radiocarbon_refs=(
            "Lødøen 2010 (14C from Late Mesolithic cultural layers/dwelling depressions at Vingen, ca. 5000-4000 cal BCE)",
            "Lødøen 2015",
        ),
        elevation_m_asl=15.0,
        country="NO",
    ),
    # --- 2. ALTA PHASES 1-5 (Helskog 1988, 2014 — Shoreline Displacement) ---
    ChronologyRule(
        rule_id="alta_phase_1_panels",
        pattern=r"\b(alta.*phase\s*(1|i)\b|bergbukten\s*(1|4b)\b|ole\s*pedersen\s*(1|8)\b|apana\s*g[aå]rd)\b",
        date_from_year=-5200,
        date_to_year=-4200,
        dating_method="shoreline_displacement",
        period_label="Stone Age — Alta Phase 1 (Helskog 1988/2014)",
        dating_citation="Helskog 1988; Helskog 2014",
        elevation_m_asl=25.0,
        country="NO",
    ),
    ChronologyRule(
        rule_id="alta_phase_2_panels",
        pattern=r"\b(alta.*phase\s*(2|ii)\b|bergbukten\s*(3|4a)\b|bergheim\s*1\b)\b",
        date_from_year=-4200,
        date_to_year=-3000,
        dating_method="shoreline_displacement",
        period_label="Stone Age — Alta Phase 2 (Helskog 1988/2014)",
        dating_citation="Helskog 1988; Helskog 2014",
        elevation_m_asl=21.0,
        country="NO",
    ),
    ChronologyRule(
        rule_id="alta_phase_3_panels",
        pattern=r"\b(alta.*phase\s*(3|iii)\b|storsteinen\b|amtmannsnes)\b",
        date_from_year=-3000,
        date_to_year=-2000,
        dating_method="shoreline_displacement",
        period_label="Late Neolithic — Alta Phase 3 (Helskog 1988/2014)",
        dating_citation="Helskog 1988; Helskog 2014",
        elevation_m_asl=16.5,
        country="NO",
    ),
    ChronologyRule(
        rule_id="alta_phase_4_panels",
        pattern=r"\b(alta.*phase\s*(4|iv)\b|k[aå]fjord)\b",
        date_from_year=-2000,
        date_to_year=-1000,
        dating_method="shoreline_displacement",
        period_label="Bronze Age — Alta Phase 4 (Helskog 1988/2014)",
        dating_citation="Helskog 1988; Helskog 2014",
        elevation_m_asl=12.0,
        country="NO",
    ),
    ChronologyRule(
        rule_id="alta_phase_5_panels",
        pattern=r"\b(alta.*phase\s*(5|v)\b|bergbukten\s*6\b|ole\s*pedersen\s*11\b)\b",
        date_from_year=-1000,
        date_to_year=-200,
        dating_method="shoreline_displacement",
        period_label="Late Bronze / Early Iron Age — Alta Phase 5 (Helskog 1988/2014)",
        dating_citation="Helskog 1988; Helskog 2014",
        elevation_m_asl=9.0,
        country="NO",
    ),
    ChronologyRule(
        rule_id="alta_general",
        pattern=r"\b(alta|hjemmeluft|jiepmaluokta|transfarelv|bergbukten|ole\s*pedersen)\b",
        date_from_year=-5200,
        date_to_year=-200,
        dating_method="shoreline_displacement",
        period_label="Arctic Multi-Phase — Alta Phases 1-5 (Helskog 1988/2014)",
        dating_citation="Helskog 1988; Helskog 2014; Gjerde 2010",
        elevation_m_asl=17.5,
        country="NO",
    ),
    # --- 3. NORDLAND EARLY / MIDDLE STONE AGE (Gjessing 1932; Hesjedal 1994; Gjerde 2010) ---
    ChronologyRule(
        rule_id="nordland_polished_early",
        pattern=r"\b(leiknes|valle|fykanvatnet|forselv|skjomen|sagelva|nes\b.*nordland)\b",
        date_from_year=-9000,
        date_to_year=-6000,
        dating_method="shoreline_displacement",
        period_label="Early-Middle Mesolithic — Nordland Polished Hunter-Gatherer",
        dating_citation="Gjessing 1932; Hesjedal 1994; Gjerde 2010",
        elevation_m_asl=70.0,
        country="NO",
    ),
    ChronologyRule(
        rule_id="rodoy_nordland",
        pattern=r"\b(r[øo]d[øo]y|r[øo]d[øo]ymannen|flat[øo]ya|vistnesdal)\b",
        date_from_year=-4500,
        date_to_year=-2500,
        dating_method="shoreline_displacement",
        period_label="Late Mesolithic / Neolithic — Nordland Pecked Hunter-Gatherer",
        dating_citation="Gjessing 1932; Gjerde 2010",
        elevation_m_asl=25.0,
        country="NO",
    ),
    ChronologyRule(
        rule_id="alstahaug_nordland_ba",
        pattern=r"\b(alstahaug|tj[øo]tta)\b",
        date_from_year=-1500,
        date_to_year=-500,
        dating_method="typological_stylistic",
        period_label="Nordic Bronze Age — Nordland Agrarian",
        dating_citation="Gjessing 1932; Kaul 1998",
        country="NO",
    ),
    # --- 4. NÄMFORSEN (Hallström 1960; Forsberg 1993; Gjerde 2010) ---
    ChronologyRule(
        rule_id="namforsen_bronze",
        pattern=r"\b(n[äa]mforsen|ådals-?liden|brad[öo]n|not[öo]n|lax[öo]n)\b",
        date_from_year=-1800,
        date_to_year=-500,
        dating_method="typological_stylistic",
        period_label="Bronze Age Phase at Nämforsen (Forsberg 1993)",
        dating_citation="Hallström 1960; Forsberg 1993",
        country="SE",
        period_filter="bronze_age_agrarian",
    ),
    ChronologyRule(
        rule_id="namforsen_hunter",
        pattern=r"\b(n[äa]mforsen|ådals-?liden|brad[öo]n|not[öo]n|lax[öo]n)\b",
        date_from_year=-4200,
        date_to_year=-2000,
        dating_method="shoreline_displacement",
        period_label="Stone Age Hunter-Gatherer — Nämforsen (Gjerde 2010; Forsberg 1993)",
        dating_citation="Hallström 1960; Forsberg 1993; Gjerde 2010",
        elevation_m_asl=105.0,
        country="SE",
        period_filter="stone_age_hunter",
    ),
    ChronologyRule(
        rule_id="namforsen_general",
        pattern=r"\b(n[äa]mforsen|ådals-?liden|brad[öo]n|not[öo]n|lax[öo]n)\b",
        date_from_year=-4200,
        date_to_year=-500,
        dating_method="composite_archival",
        period_label="Multi-Period — Nämforsen Hunter-Gatherer & Bronze Age",
        dating_citation="Hallström 1960; Forsberg 1993; Gjerde 2010",
        elevation_m_asl=105.0,
        country="SE",
    ),
    # --- 5. TRØNDELAG (Sognnes 2001/2010; Gjessing 1936) ---
    ChronologyRule(
        rule_id="bardal_mixed",
        pattern=r"\bbardal\b",
        date_from_year=-5000,
        date_to_year=-500,
        dating_method="stratigraphic",
        period_label="Multi-Period Superposition — Bardal (Stone Age & Bronze Age)",
        dating_citation="Gjessing 1936; Sognnes 2001; Sognnes 2010",
        elevation_m_asl=40.0,
        country="NO",
    ),
    ChronologyRule(
        rule_id="trondelag_hunter",
        pattern=r"\b(b[øo]la|b[øo]larein\w*|evenhus|strand\b.*(osen|tr[øo]ndelag)|stykket|holt[åa]s|hell\b|kvennavika|hammer\b.*steinkjer)\b",
        date_from_year=-5000,
        date_to_year=-2000,
        dating_method="shoreline_displacement",
        period_label="Stone Age Hunter-Gatherer — Trøndelag (Sognnes 2001/2010)",
        dating_citation="Gjessing 1936; Sognnes 2001; Sognnes 2010",
        elevation_m_asl=38.0,
        country="NO",
    ),
    ChronologyRule(
        rule_id="solsemhula",
        pattern=r"\bsolsemhula\b",
        date_from_year=-2500,
        date_to_year=-1000,
        dating_method="stratigraphic",
        period_label="Neolithic / Early Bronze Age Cave Art — Solsemhula",
        dating_citation="Gjessing 1936; Sognnes 2010",
        country="NO",
    ),
    ChronologyRule(
        rule_id="trondelag_agrarian",
        pattern=r"\b(leirfall|hegra|ydstines|selbustrand|homnes|stj[øo]rdal|steinkjer|tr[øo]ndelag)\b",
        date_from_year=-1700,
        date_to_year=-500,
        dating_method="typological_stylistic",
        period_label="Nordic Bronze Age — Trøndelag Agrarian (Sognnes 2001)",
        dating_citation="Sognnes 2001; Sognnes 2010",
        country="NO",
        period_filter="bronze_age_agrarian",
    ),
    # --- 6. WESTERN & EASTERN NORWAY HUNTER-GATHERER ---
    ChronologyRule(
        rule_id="ausevik_vestland",
        pattern=r"\bausevik\b",
        date_from_year=-3500,
        date_to_year=-1500,
        dating_method="typological_stylistic",
        period_label="Neolithic / Early Bronze Age — Ausevik Transitional",
        dating_citation="Lødøen & Mandt 2010; Lødøen 2015",
        country="NO",
    ),
    ChronologyRule(
        rule_id="eastern_norway_hunter",
        pattern=r"\b(ekeberg|sj[øo]mannsskolen|skogerveien|m[øo]llerstufossen|drotten|geithus|kl[øo]ftefoss|åskollen)\b",
        date_from_year=-6000,
        date_to_year=-2500,
        dating_method="shoreline_displacement",
        period_label="Mesolithic / Neolithic — Eastern Norway Hunter-Gatherer",
        dating_citation="Gjessing 1936; Lødøen & Mandt 2010",
        elevation_m_asl=55.0,
        country="NO",
    ),
    # --- 7. BOHUSLÄN / TANUM (Ling 2008/2014 + Montelius I-VI) ---
    ChronologyRule(
        rule_id="tumlehed_bohuslan_painting",
        pattern=r"\btumlehed\b",
        date_from_year=-4500,
        date_to_year=-2000,
        dating_method="shoreline_displacement",
        period_label="Stone Age Hunter-Gatherer Painting — Tumlehed",
        dating_citation="Ling 2008; Bertilsson 1987",
        country="SE",
    ),
    ChronologyRule(
        rule_id="bohuslan_tanum_shoreline",
        pattern=r"\b(tanum|vitlycke|aspeberget|fossum|litsleby|l[öo]v[åa]sen|kalleby|gerum|sotetorp|massleberg|bohusl[äa]n|kville|brastad|backa|tossene|svenneby|bottna|askum|svarteborg|lysekil|soten[äa]s|munkedal|tegneby|finntorp)\b",
        date_from_year=-1700,
        date_to_year=-500,
        dating_method="shoreline_displacement",
        period_label="Nordic Bronze Age (Montelius Per. I-VI) — Bohuslän/Tanum Shoreline",
        dating_citation="Ling 2008; Ling 2014; Kaul 1998",
        elevation_m_asl=13.0,
        country="SE",
    ),
    # --- 8. ØSTFOLD (Vogt 2012; Marstrander 1963; Gjessing 1939) ---
    ChronologyRule(
        rule_id="ostfold_shoreline",
        pattern=r"\b([øo]stfold|sarpsborg|fredrikstad|skjeberg|halden|begby|solberg|kalnes|hornes|bj[øo]rnstad|hunn\b|hafslund|borgen\b.*ostfold)\b",
        date_from_year=-1700,
        date_to_year=-500,
        dating_method="shoreline_displacement",
        period_label="Nordic Bronze Age — Østfold Shoreline & Typology (Vogt 2012)",
        dating_citation="Vogt 2012; Marstrander 1963; Gjessing 1939",
        elevation_m_asl=14.0,
        country="NO",
    ),
    # --- 9. BORNHOLM & SKÅNE (Malmer 1981; Kaul 1998) ---
    ChronologyRule(
        rule_id="bornholm_denmark",
        pattern=r"\b(bornholm|madsebakke|madsel[öo]kke|storel[øo]kkebakke|storl[öo]kkebakke|sj[äa]leg[åa]rd|strandbygård|skrulle|allinge|hammersholm)\b",
        date_from_year=-1500,
        date_to_year=-500,
        dating_method="typological_stylistic",
        period_label="Nordic Bronze Age (Montelius Per. II-VI) — Bornholm",
        dating_citation="Malmer 1981; Kaul 1998",
        country="DK",
    ),
    ChronologyRule(
        rule_id="denmark_zealand_jutland",
        pattern=r"\b(engelstrup|grevinge|herrestrup|kirke\s*s[aå]by|jaegersborg|sandagerg[åa]rd|bjergagerg[åa]rd|odsherred|sj[æa]lland|sj[äa]lland|lejre|kalundborg)\b",
        date_from_year=-1500,
        date_to_year=-500,
        dating_method="typological_stylistic",
        period_label="Nordic Bronze Age (Montelius Per. II-VI) — Denmark",
        dating_citation="Kaul 1998; Malmer 1981",
        country="DK",
    ),
    ChronologyRule(
        rule_id="skane_sweden",
        pattern=r"\b(sk[åa]ne|simrishamn|simrislund|j[äa]rrestad|fr[äa]nnarp|gladsax)\b",
        date_from_year=-1700,
        date_to_year=-500,
        dating_method="typological_stylistic",
        period_label="Nordic Bronze Age — Skåne Typological",
        dating_citation="Malmer 1981; Kaul 1998",
        country="SE",
    ),
    # --- 10. OTHER SWEDISH REGIONS ---
    ChronologyRule(
        rule_id="sweden_northern_hunter",
        pattern=r"\b(gl[öo]sa|g[äa]rde|norrfors|flatruet|f[åa]ngsj[öo]n|h[äa]llberget|krokom|alsen)\b",
        date_from_year=-4500,
        date_to_year=-1800,
        dating_method="shoreline_displacement",
        period_label="Stone Age Hunter-Gatherer — Northern Sweden",
        dating_citation="Hallström 1960; Gjerde 2010",
        country="SE",
    ),
    ChronologyRule(
        rule_id="sweden_agrarian_regions",
        pattern=r"\b(himmelstalund|norrk[öo]ping|leonardsberg|ekensberg|fiskeby|[öo]sterg[öo]tland|enköping|enkoping|bogl[öo]sa|brandskog|hemsta|rickeby|uppland|slagsta|s[öo]dermanland|flyhov|v[äa]sterg[öo]tland|h[öo]gsbyn|fr[öo]skog|dalsland|h[äa]sthallen|blekinge|h[äa]gvide|gotland|sm[åa]land)\b",
        date_from_year=-1700,
        date_to_year=-500,
        dating_method="typological_stylistic",
        period_label="Nordic Bronze Age — Central/Southern Sweden",
        dating_citation="Malmer 1981; Kaul 1998; Ling 2014",
        country="SE",
    ),
    # --- 11. OTHER NORWEGIAN BRONZE AGE ---
    ChronologyRule(
        rule_id="norway_west_south_ba",
        pattern=r"\b(austre\s*[åa]m[øo]y|[åa]m[øo]y|rogaland|rudl[åa]|revheim|fluberget|pennefeltet|penne|agder|lambertseter|sundvollen|åkersetervollen|telemark|vestfold|sporanes)\b",
        date_from_year=-1700,
        date_to_year=-500,
        dating_method="typological_stylistic",
        period_label="Nordic Bronze Age — Southern/Western Norway",
        dating_citation="Lødøen & Mandt 2010; Kaul 1998",
        country="NO",
    ),
    # --- 12. FINLAND & KARELIA / WHITE SEA ---
    ChronologyRule(
        rule_id="finland_rock_paintings",
        pattern=r"\b(v[äa]rikallio|astuvansalmi|saraakallio|verla|juusj[äa]rvi|hossa|kainuu|saimaa|p[äa]ij[äa]nne|ristiina|laukaa|suomussalmi|kirkkonummi|finland)\b",
        date_from_year=-5000,
        date_to_year=-1500,
        dating_method="shoreline_displacement",
        period_label="Subneolithic / Combed Ware — Finnish Rock Paintings (Lahelma 2008)",
        dating_citation="Lahelma 2008",
        country="FI",
    ),
    ChronologyRule(
        rule_id="karelia_white_sea",
        pattern=r"\b(onega|besov\s*nos|zalavruga|kanozero|white\s*sea|belomorsk|karelia|karjala|vyg\b)\b",
        date_from_year=-4500,
        date_to_year=-2000,
        dating_method="shoreline_displacement",
        period_label="Neolithic Hunter-Gatherer — Karelia / White Sea (Gjerde 2010)",
        dating_citation="Gjerde 2010",
        country="RU",
    ),
)

_MONTELIUS_RE = re.compile(
    r"(?i)\b(?:montelius\s*)?(?:per\.?|period(?:e|en)?)\s*(VI|IV|V|III|II|I|[1-6])"
    r"(?:\s*[-–/]\s*(VI|IV|V|III|II|I|[1-6]))?\b"
)

_ALTA_PHASE_RE = re.compile(
    r"(?i)\b(?:alta\s*)?(?:phase|fase)\s*(V|IV|III|II|I|[1-5])\b"
)

_YEAR_SPAN_RE = re.compile(
    r"(?i)(\d{3,4})\s*[-–]\s*(\d{3,4})\s*(BCE|BC|f\.?\s*Kr\.?|CE|AD|e\.?\s*Kr\.?)"
)


@lru_cache(maxsize=1)
def load_chronology_bibliography(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the peer-reviewed chronological bibliographic table."""
    target = path or BIBLIOGRAPHY_PATH
    with open(target, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _norm_roman(token: str) -> Optional[str]:
    t = token.strip().upper()
    if t in _ROMAN_TO_INT:
        return t
    if t.isdigit() and int(t) in _INT_TO_ROMAN:
        return _INT_TO_ROMAN[int(t)]
    return None


def _compute_midpoint_and_precision(date_from: Optional[int], date_to: Optional[int]) -> Tuple[Optional[int], Optional[int]]:
    if date_from is None or date_to is None:
        return None, None
    lo, hi = min(date_from, date_to), max(date_from, date_to)
    mid = int(round((lo + hi) / 2.0))
    prec = int(round((hi - lo) / 2.0))
    return mid, prec


def _alta_dating_from_elevation(elev_m: float) -> Tuple[int, int, str]:
    """Map Alta elevation (m a.s.l.) onto Helskog (1988/2014) Phases 1-5."""
    if elev_m >= 23.0:
        return -5200, -4200, "Stone Age — Alta Phase 1 (Helskog 1988/2014)"
    if elev_m >= 19.0:
        return -4200, -3000, "Stone Age — Alta Phase 2 (Helskog 1988/2014)"
    if elev_m >= 14.0:
        return -3000, -2000, "Late Neolithic — Alta Phase 3 (Helskog 1988/2014)"
    if elev_m >= 10.0:
        return -2000, -1000, "Bronze Age — Alta Phase 4 (Helskog 1988/2014)"
    return -1000, -200, "Late Bronze / Early Iron Age — Alta Phase 5 (Helskog 1988/2014)"


def _bohuslan_dating_from_elevation(elev_m: float) -> Tuple[int, int, str]:
    """Map Bohuslän/Tanum elevation (m a.s.l.) using Ling (2008/2014) shoreline curve."""
    if elev_m >= 16.0:
        return -1700, -1500, "Nordic Bronze Age — Montelius Per. I (Bohuslän Shoreline)"
    if elev_m >= 14.5:
        return -1500, -1300, "Nordic Bronze Age — Montelius Per. II (Bohuslän Shoreline)"
    if elev_m >= 13.0:
        return -1300, -1100, "Nordic Bronze Age — Montelius Per. III (Bohuslän Shoreline)"
    if elev_m >= 11.5:
        return -1100, -900, "Nordic Bronze Age — Montelius Per. IV (Bohuslän Shoreline)"
    if elev_m >= 10.0:
        return -900, -700, "Nordic Bronze Age — Montelius Per. V (Bohuslän Shoreline)"
    if elev_m >= 8.5:
        return -700, -500, "Nordic Bronze Age — Montelius Per. VI (Bohuslän Shoreline)"
    return -500, -200, "Pre-Roman Iron Age — Bohuslän Low Shoreline"


def _make_result(
    *,
    date_from_year: Optional[int],
    date_to_year: Optional[int],
    dating_method: str,
    period_label: str,
    radiocarbon_refs: Iterable[str],
    elevation_m_asl: Optional[float],
    dating_citation: Optional[str],
) -> Dict[str, Any]:
    if dating_method not in VALID_DATING_METHODS:
        raise ValueError(f"Invalid dating_method: {dating_method!r}")
    if date_from_year is not None and date_to_year is not None and date_from_year > date_to_year:
        date_from_year, date_to_year = date_to_year, date_from_year
    midpoint, precision = _compute_midpoint_and_precision(date_from_year, date_to_year)
    return {
        "date_from_year": int(date_from_year) if date_from_year is not None else None,
        "date_to_year": int(date_to_year) if date_to_year is not None else None,
        "date_midpoint_year": midpoint,
        "dating_method": dating_method,
        "dating_precision_years": precision,
        "period_label": period_label,
        "radiocarbon_refs": list(radiocarbon_refs),
        "elevation_m_asl": float(elevation_m_asl) if elevation_m_asl is not None else None,
        "dating_citation": dating_citation,
    }


def resolve_chronology(
    record: Optional[Dict[str, Any]] = None,
    *,
    site: Optional[str] = None,
    region: Optional[str] = None,
    country: Optional[str] = None,
    period: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    motif_hints: Optional[List[str]] = None,
    elevation_m_asl: Optional[float] = None,
    raw_dating: Optional[str] = None,
    phase: Optional[str] = None,
) -> Dict[str, Any]:
    """Assign structured, peer-reviewed numeric dating fields to a site/panel/figure record."""
    rec = record or {}
    site_val = site if site is not None else str(rec.get("site") or "")
    region_val = region if region is not None else str(rec.get("region") or "")
    country_val = (country if country is not None else str(rec.get("country") or "")).upper().strip()
    period_val = (period if period is not None else str(rec.get("period") or "")).strip().lower()
    title_val = title if title is not None else str(rec.get("title") or "")
    desc_val = description if description is not None else str(rec.get("description") or "")
    raw_dating_val = raw_dating if raw_dating is not None else str(rec.get("raw_dating") or rec.get("dating") or "")
    phase_val = phase if phase is not None else str(rec.get("phase") or "")
    hints = motif_hints if motif_hints is not None else list(rec.get("motif_hints") or [])

    elev = elevation_m_asl
    if elev is None and rec.get("elevation_m_asl") is not None:
        try:
            elev = float(rec["elevation_m_asl"])
        except (TypeError, ValueError):
            elev = None

    bib = load_chronology_bibliography()
    search_blob = " ".join(
        p for p in (site_val, region_val, title_val, desc_val, raw_dating_val, phase_val) if p
    ).strip()

    # 1. Explicit Alta Phase string (e.g. phase="Phase 2" or "Alta Phase 1")
    m_alta = _ALTA_PHASE_RE.search(f"{phase_val} {raw_dating_val} {title_val}")
    if m_alta or ("alta" in search_blob.lower() and elev is not None):
        if m_alta:
            rkey = f"Phase {_ROMAN_TO_INT[_norm_roman(m_alta.group(1))]}"
            ap = bib["alta_phases"][rkey]
            return _make_result(
                date_from_year=ap["date_from_year"],
                date_to_year=ap["date_to_year"],
                dating_method="shoreline_displacement",
                period_label=ap["label"],
                radiocarbon_refs=[],
                elevation_m_asl=elev if elev is not None else ap["elevation_m_asl"],
                dating_citation="Helskog 1988; Helskog 2014",
            )
        assert elev is not None
        df, dt, lbl = _alta_dating_from_elevation(elev)
        return _make_result(
            date_from_year=df,
            date_to_year=dt,
            dating_method="shoreline_displacement",
            period_label=lbl,
            radiocarbon_refs=[],
            elevation_m_asl=elev,
            dating_citation="Helskog 1988; Helskog 2014",
        )

    # 2. Explicit Montelius period in raw_dating/phase (e.g. "Per. II", "Period III-IV")
    m_mont = _MONTELIUS_RE.search(f"{phase_val} {raw_dating_val}")
    if m_mont:
        r1 = _norm_roman(m_mont.group(1))
        r2 = _norm_roman(m_mont.group(2)) if m_mont.group(2) else r1
        if r1 and r2:
            p1 = bib["montelius_periods"][f"Period {r1}"]
            p2 = bib["montelius_periods"][f"Period {r2}"]
            df = min(p1["date_from_year"], p2["date_from_year"])
            dt = max(p1["date_to_year"], p2["date_to_year"])
            lbl = (
                p1["label"]
                if r1 == r2
                else f"Nordic Bronze Age — Montelius Period {r1}-{r2}"
            )
            is_bohuslan = bool(re.search(r"(?i)\b(bohusl[äa]n|tanum)\b", search_blob))
            is_ostfold = bool(re.search(r"(?i)\b([øo]stfold|sarpsborg|skjeberg|fredrikstad)\b", search_blob))
            method = "shoreline_displacement" if (is_bohuslan or is_ostfold) else "typological_stylistic"
            cite = (
                "Ling 2008; Ling 2014; Kaul 1998"
                if is_bohuslan
                else ("Vogt 2012; Kaul 1998" if is_ostfold else "Kaul 1998; Malmer 1981")
            )
            return _make_result(
                date_from_year=df,
                date_to_year=dt,
                dating_method=method,
                period_label=lbl,
                radiocarbon_refs=[],
                elevation_m_asl=elev,
                dating_citation=cite,
            )

    # 3. Bohuslän / Tanum with explicit elevation
    if elev is not None and re.search(r"(?i)\b(bohusl[äa]n|tanum|vitlycke|aspeberget|kville|tossene|brastad)\b", search_blob):
        df, dt, lbl = _bohuslan_dating_from_elevation(elev)
        return _make_result(
            date_from_year=df,
            date_to_year=dt,
            dating_method="shoreline_displacement",
            period_label=lbl,
            radiocarbon_refs=[],
            elevation_m_asl=elev,
            dating_citation="Ling 2008; Ling 2014; Kaul 1998",
        )

    # 4. Published site/panel/region rules
    for rule in _ARCHAEOLOGICAL_RULES:
        if rule.period_filter and period_val != rule.period_filter:
            continue
        if re.search(rule.pattern, search_blob, re.IGNORECASE):
            # Guard country if explicitly known and conflicting (unless mixed Nordic archive)
            if rule.country and country_val and country_val not in (rule.country, "NORDIC", "INTL"):
                # Allow Kivik / Skåne objects in Danish Nationalmuseet collections
                if not (rule.rule_id == "kivik_bredaror_14c" and country_val == "SE"):
                    continue
            return _make_result(
                date_from_year=rule.date_from_year,
                date_to_year=rule.date_to_year,
                dating_method=rule.dating_method,
                period_label=rule.period_label,
                radiocarbon_refs=rule.radiocarbon_refs,
                elevation_m_asl=elev if elev is not None else rule.elevation_m_asl,
                dating_citation=rule.dating_citation,
            )

    # 5. Explicit numeric year span in archival dating text (e.g. "1800-550 BCE")
    m_span = _YEAR_SPAN_RE.search(f"{raw_dating_val} {desc_val}")
    if m_span:
        y1, y2, era = int(m_span.group(1)), int(m_span.group(2)), m_span.group(3).lower()
        is_bce = any(tok in era for tok in ("bc", "bce", "f."))
        if is_bce:
            df, dt = -max(y1, y2), -min(y1, y2)
        else:
            df, dt = min(y1, y2), max(y1, y2)
        return _make_result(
            date_from_year=df,
            date_to_year=dt,
            dating_method="composite_archival",
            period_label="Archival Date Range",
            radiocarbon_refs=[],
            elevation_m_asl=elev,
            dating_citation="Archival catalogue record",
        )

    # 6. Tradition-level period / motif fallback
    if period_val == "stone_age_hunter":
        return _make_result(
            date_from_year=-7000,
            date_to_year=-1800,
            dating_method="typological_stylistic",
            period_label="Stone Age Hunter-Gatherer Tradition (Veideristninger)",
            radiocarbon_refs=[],
            elevation_m_asl=elev,
            dating_citation="Gjessing 1932; Hallström 1960; Gjerde 2010",
        )
    if period_val == "bronze_age_agrarian":
        return _make_result(
            date_from_year=-1800,
            date_to_year=-500,
            dating_method="typological_stylistic",
            period_label="Nordic Bronze Age Agrarian Tradition (Jordbruksristninger)",
            radiocarbon_refs=[],
            elevation_m_asl=elev,
            dating_citation="Malmer 1981; Kaul 1998; Ling 2008",
        )
    if period_val == "mixed":
        return _make_result(
            date_from_year=-5000,
            date_to_year=-500,
            dating_method="composite_archival",
            period_label="Multi-Period / Transitional Nordic Rock Art",
            radiocarbon_refs=[],
            elevation_m_asl=elev,
            dating_citation="Sognnes 2001; Helskog 2014; Forsberg 1993",
        )

    hints_low = {str(h).lower() for h in hints}
    if hints_low & {"bronze_age_ship", "sun_wheel", "chariot_plough", "lur_blower", "footprint"}:
        return _make_result(
            date_from_year=-1800,
            date_to_year=-500,
            dating_method="typological_stylistic",
            period_label="Nordic Bronze Age (Motif Typology)",
            radiocarbon_refs=[],
            elevation_m_asl=elev,
            dating_citation="Kaul 1998; Malmer 1981",
        )
    if hints_low & {"elk_moose", "reindeer", "skier", "stone_age_boat", "whale_cetacean"}:
        return _make_result(
            date_from_year=-7000,
            date_to_year=-1800,
            dating_method="typological_stylistic",
            period_label="Stone Age Hunter-Gatherer (Motif Typology)",
            radiocarbon_refs=[],
            elevation_m_asl=elev,
            dating_citation="Gjessing 1932; Gjerde 2010",
        )

    # 7. Honest unknown — never fabricate
    return _make_result(
        date_from_year=None,
        date_to_year=None,
        dating_method="unknown",
        period_label="Unknown / Unverified Chronology",
        radiocarbon_refs=[],
        elevation_m_asl=elev,
        dating_citation=None,
    )


def date_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience wrapper returning the structured dating dict for a record."""
    return resolve_chronology(record)


def enrich_record_with_chronology(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of ``record`` enriched with all structured chronology fields."""
    out = dict(record)
    out.update(resolve_chronology(record))
    return out
