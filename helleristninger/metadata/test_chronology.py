"""Unit tests for helleristninger.metadata.chronology."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_META_DIR = Path(__file__).resolve().parent
if str(_META_DIR) not in sys.path:
    sys.path.insert(0, str(_META_DIR))

from chronology import (
    BIBLIOGRAPHY_PATH,
    VALID_DATING_METHODS,
    date_record,
    enrich_record_with_chronology,
    load_chronology_bibliography,
    resolve_chronology,
)


def test_bibliography_file_valid_and_complete() -> None:
    assert BIBLIOGRAPHY_PATH.exists()
    bib = load_chronology_bibliography()
    assert set(bib["valid_dating_methods"]) == set(VALID_DATING_METHODS)
    cids = {s["citation_id"] for s in bib["sources"]}
    expected = {
        "helskog_1988",
        "helskog_2014",
        "lodoen_2010",
        "lodoen_2015",
        "hallstrom_1960",
        "forsberg_1993",
        "gjerde_2010",
        "ling_2008",
        "ling_2014",
        "vogt_2012",
        "sognnes_2001",
        "sognnes_2010",
        "gjessing_1932",
        "gjessing_1936",
        "goldhahn_2009",
        "goldhahn_2013",
        "malmer_1981",
        "kaul_1998",
        "lahelma_2008",
        "hesjedal_1994",
    }
    assert expected.issubset(cids)


def test_radiocarbon_vs_shoreline_honesty() -> None:
    # Actual 14C excavation contexts: Vingen & Kivik
    vingen = resolve_chronology(site="Vingen", region="Vestland", country="NO")
    assert vingen["dating_method"] == "radiocarbon_14c"
    assert vingen["date_from_year"] == -5000
    assert vingen["date_to_year"] == -4000
    assert vingen["date_midpoint_year"] == -4500
    assert len(vingen["radiocarbon_refs"]) >= 1
    assert "Lødøen" in vingen["dating_citation"]

    kivik = resolve_chronology(site="Kivik", region="Skåne", country="SE")
    assert kivik["dating_method"] == "radiocarbon_14c"
    assert kivik["date_from_year"] == -1400
    assert kivik["date_to_year"] == -1100
    assert len(kivik["radiocarbon_refs"]) >= 1
    assert "Goldhahn" in kivik["dating_citation"]

    # Shoreline-only sites must NEVER claim radiocarbon_14c or fabricate 14C refs
    alta = resolve_chronology(site="Alta", region="Finnmark", country="NO")
    assert alta["dating_method"] == "shoreline_displacement"
    assert alta["radiocarbon_refs"] == []
    assert "Helskog" in alta["dating_citation"]

    tanum = resolve_chronology(site="Tanum / Vitlycke", region="Bohuslän", country="SE")
    assert tanum["dating_method"] == "shoreline_displacement"
    assert tanum["radiocarbon_refs"] == []
    assert "Ling" in tanum["dating_citation"]

    ostfold = resolve_chronology(site="Begby", region="Østfold", country="NO")
    assert ostfold["dating_method"] == "shoreline_displacement"
    assert ostfold["radiocarbon_refs"] == []
    assert "Vogt" in ostfold["dating_citation"]


def test_alta_phases_and_elevation_curves() -> None:
    p1 = resolve_chronology(site="Alta", country="NO", phase="Phase 1")
    p5 = resolve_chronology(site="Alta", country="NO", phase="Phase 5")
    assert p1["date_from_year"] == -5200 and p1["date_to_year"] == -4200
    assert p5["date_from_year"] == -1000 and p5["date_to_year"] == -200
    assert p1["date_midpoint_year"] < p5["date_midpoint_year"]

    high_alta = resolve_chronology(site="Hjemmeluft, Alta", country="NO", elevation_m_asl=25.5)
    low_alta = resolve_chronology(site="Hjemmeluft, Alta", country="NO", elevation_m_asl=9.0)
    assert high_alta["date_from_year"] == -5200
    assert low_alta["date_to_year"] == -200

    bohuslan_p2 = resolve_chronology(site="Tanum", region="Bohuslän", country="SE", elevation_m_asl=15.0)
    assert bohuslan_p2["date_from_year"] == -1500
    assert bohuslan_p2["date_to_year"] == -1300
    assert bohuslan_p2["dating_method"] == "shoreline_displacement"


def test_montelius_periods_and_regional_rules() -> None:
    m = resolve_chronology(site="Aspeberget", region="Bohuslän", country="SE", raw_dating="Montelius Per. II-III")
    assert m["date_from_year"] == -1500
    assert m["date_to_year"] == -1100
    assert m["dating_precision_years"] == 200

    nam_h = resolve_chronology(site="Nämforsen", region="Ångermanland", country="SE", period="stone_age_hunter")
    nam_b = resolve_chronology(site="Nämforsen", region="Ångermanland", country="SE", period="bronze_age_agrarian")
    assert nam_h["dating_method"] == "shoreline_displacement"
    assert nam_b["dating_method"] == "typological_stylistic"
    assert nam_h["date_to_year"] < nam_b["date_to_year"]

    leiknes = resolve_chronology(site="Leiknes", region="Nordland", country="NO")
    assert leiknes["date_from_year"] == -9000
    assert leiknes["date_to_year"] == -6000
    assert leiknes["dating_method"] == "shoreline_displacement"

    bornholm = resolve_chronology(site="Madsebakke", region="Bornholm", country="DK")
    assert bornholm["dating_method"] == "typological_stylistic"
    assert "Malmer" in bornholm["dating_citation"] or "Kaul" in bornholm["dating_citation"]

    finland = resolve_chronology(site="Astuvansalmi", region="Southern Savonia", country="FI")
    assert finland["dating_method"] == "shoreline_displacement"
    assert "Lahelma" in finland["dating_citation"]

    trond_h = resolve_chronology(site="Bøla", region="Trøndelag", country="NO")
    assert trond_h["dating_method"] == "shoreline_displacement"
    assert "Sognnes" in trond_h["dating_citation"]

    trond_ba = resolve_chronology(site="Leirfall", region="Trøndelag", country="NO", period="bronze_age_agrarian")
    assert trond_ba["dating_method"] == "typological_stylistic"
    assert "Sognnes" in trond_ba["dating_citation"]

    bardal = resolve_chronology(site="Bardal", region="Trøndelag", country="NO")
    assert bardal["dating_method"] == "stratigraphic"

    rodoy = resolve_chronology(site="Rødøy", region="Nordland", country="NO")
    assert rodoy["dating_method"] == "shoreline_displacement"
    assert "Gjessing" in rodoy["dating_citation"]

    karelia = resolve_chronology(site="Zalavruga", region="Karelia", country="RU")
    assert karelia["dating_method"] == "shoreline_displacement"
    assert "Gjerde" in karelia["dating_citation"]

    skane = resolve_chronology(site="Järrestad", region="Skåne", country="SE")
    assert skane["dating_method"] == "typological_stylistic"
    assert "Malmer" in skane["dating_citation"] or "Kaul" in skane["dating_citation"]


def test_unknown_and_enrichment() -> None:
    unk = resolve_chronology(site="Unlisted Mystery Rock", region="", country="", period="")
    assert unk["dating_method"] == "unknown"
    assert unk["date_from_year"] is None
    assert unk["date_to_year"] is None
    assert unk["date_midpoint_year"] is None
    assert unk["radiocarbon_refs"] == []

    rec = {"source_id": "test_1", "site": "Bøla", "region": "Trøndelag", "country": "NO"}
    d = date_record(rec)
    enriched = enrich_record_with_chronology(rec)
    assert enriched["source_id"] == "test_1"
    assert enriched["date_from_year"] == d["date_from_year"]
    assert enriched["dating_method"] in VALID_DATING_METHODS
    assert json.dumps(enriched)
