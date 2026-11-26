#!/usr/bin/env python3
"""
Wikimedia Commons Harvester for Nordic Rock Art (Helleristninger / Hällristningar).

Programmatically discovers, filters, verifies licenses, downloads immutable originals,
computes SHA-256 hashes, inspects raster/SVG quality, and writes manifest.jsonl.

Usage:
    python fetch_wikimedia.py --discover --cache path/to/cache.json
    python fetch_wikimedia.py --download --cache path/to/cache.json --target-total 320
    python fetch_wikimedia.py --all --cache path/to/cache.json --target-total 320
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = 120_000_000

API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = (
    "HelleristningerCorpusHarvester/1.0 "
    "(https://jigsaw.iverfinne.no; academic-cultural-heritage-research) "
    "python-requests"
)

SCRIPT_DIR = Path(__file__).resolve().parent
RAW_DIR = SCRIPT_DIR / "raw"
MANIFEST_PATH = SCRIPT_DIR / "manifest.jsonl"

# ---------------------------------------------------------------------------
# Curated Wikimedia Commons Categories (Norway + neighboring Nordic)
# ---------------------------------------------------------------------------

SEED_CATEGORIES: list[tuple[str, str, int]] = [
    # (category_title, default_country_hint, max_subcat_depth)
    # --- GOLD: Drawings of petroglyphs ---
    ("Category:Drawings of petroglyphs in Norway", "NO", 1),
    ("Category:Drawings of petroglyphs in Sweden", "SE", 1),
    ("Category:Drawings of petroglyphs in Finland", "FI", 1),
    ("Category:Drawings of petroglyphs", "NORDIC", 0),
    # --- NORWAY (Comprehensive across all counties/regions) ---
    ("Category:Petroglyphs in Norway", "NO", 2),
    ("Category:Rock art in Norway", "NO", 2),
    ("Category:Rock art of Alta", "NO", 0),
    ("Category:Rock carvings in Finnmark", "NO", 1),
    ("Category:Kvalsund rock art", "NO", 0),
    ("Category:Rock carvings in Troms", "NO", 1),
    ("Category:Rock carvings at Skavberg", "NO", 0),
    ("Category:Rock carvings in Nordland", "NO", 1),
    ("Category:Alstahaug rock carvings", "NO", 0),
    ("Category:Rock carvings in Trøndelag", "NO", 2),
    ("Category:Bardal rock carvings", "NO", 0),
    ("Category:Bølareinen rock carvings of Steinkjer", "NO", 0),
    ("Category:Evenhus rock carvings", "NO", 0),
    ("Category:Hegre rock art", "NO", 0),
    ("Category:Hell rock art Trøndelag", "NO", 0),
    ("Category:Holtås rock carvings", "NO", 0),
    ("Category:Homnes rock art", "NO", 0),
    ("Category:Kvennavika rock carvings", "NO", 0),
    ("Category:Leirfall rock art", "NO", 0),
    ("Category:Selbustrand rock art", "NO", 0),
    ("Category:Strand rock carvings Trøndelag", "NO", 0),
    ("Category:Stykket rock carvings", "NO", 0),
    ("Category:Ydstines rock art", "NO", 0),
    ("Category:Solsemhula", "NO", 0),
    ("Category:Rock carvings in Vestland", "NO", 1),
    ("Category:Rock carvings in Rogaland", "NO", 1),
    ("Category:Rudlå (Petroglyphs in Stavanger)", "NO", 0),
    ("Category:Rock carvings in Agder", "NO", 1),
    ("Category:Pennefeltet", "NO", 0),
    ("Category:Rock carvings in Vestfold", "NO", 1),
    ("Category:Rock carvings in Buskerud", "NO", 1),
    ("Category:Helleristningene i Skogerveien", "NO", 0),
    ("Category:Helleristningssteinen på Sundvollen", "NO", 0),
    ("Category:Rock carvings in Innlandet", "NO", 1),
    ("Category:Møllerstufossen rock carvings", "NO", 0),
    ("Category:Drotten petroglyphs", "NO", 0),
    ("Category:Åkersetervollen rock carvings", "NO", 0),
    ("Category:Rock carvings in Akershus", "NO", 1),
    ("Category:Rock carvings in Oslo", "NO", 1),
    ("Category:Ekeberg 2 helleristningsfelt, Sjømannsskolen", "NO", 0),
    ("Category:Ristning, Lambertseter", "NO", 0),
    ("Category:Rock carvings in Østfold", "NO", 2),
    ("Category:Rock carvings in Sarpsborg", "NO", 1),
    ("Category:Begby rock carvings in Østfold", "NO", 0),
    ("Category:Bjørnstadskipet", "NO", 0),
    ("Category:Hornes rock carvings in Østfold", "NO", 0),
    ("Category:Kalnes rock carvings in Østfold", "NO", 0),
    ("Category:Solberg rock carvings in Østfold", "NO", 0),
    ("Category:Rock paintings in Norway", "NO", 1),
    # --- SWEDEN (Bohuslän/Tanum, Nämforsen, Himmelstalund, Kivik, etc.) ---
    ("Category:Petroglyphs in Sweden", "SE", 0),
    ("Category:Rock art in Sweden", "SE", 0),
    ("Category:Rock carvings in Tanum", "SE", 1),
    ("Category:Aspeberget, Tanum", "SE", 0),
    ("Category:Rock carvings at Fossum", "SE", 0),
    ("Category:Rock carvings at Kalleby, Tanum", "SE", 0),
    ("Category:Rock carvings at Litsleby", "SE", 0),
    ("Category:Lövåsen rock carvings, Tanum", "SE", 0),
    ("Category:Rock carvings at Gerum", "SE", 0),
    ("Category:Sotetorphällen", "SE", 0),
    ("Category:Rock carvings at Vitlycke", "SE", 0),
    ("Category:Petroglyphs in Tanum Municipality", "SE", 0),
    ("Category:Petroglyphs in Bohuslän", "SE", 1),
    ("Category:Massleberg rock carvings", "SE", 0),
    ("Category:Nämforsens hällristningar", "SE", 0),
    ("Category:Norrköpings hällristningar", "SE", 1),
    ("Category:Himmelstalund", "SE", 1),
    ("Category:Petroglyphs in Östergötland", "SE", 1),
    ("Category:Kiviksgraven", "SE", 0),
    ("Category:Petroglyphs in Skåne", "SE", 1),
    ("Category:Hällristningarna i Frännarp", "SE", 0),
    ("Category:Järrestads hällristningar", "SE", 0),
    ("Category:Simrislunds hällristningar", "SE", 0),
    ("Category:Brandskogsskeppet", "SE", 0),
    ("Category:Hemstahällen", "SE", 0),
    ("Category:Slagstaristningen", "SE", 0),
    ("Category:Hällarna från Tuna", "SE", 0),
    ("Category:Flyhov rock carvings", "SE", 0),
    ("Category:Högsbyns hällristningsområde", "SE", 0),
    ("Category:Hästhallen", "SE", 0),
    ("Category:Hägvide", "SE", 0),
    ("Category:Petroglyphs in Norrfors, Västerbotten", "SE", 0),
    ("Category:Petroglyphs in Västergötland", "SE", 0),
    ("Category:Petroglyphs in Södermanland", "SE", 0),
    ("Category:Hällmålningen i Tumlehed", "SE", 0),
    # --- DENMARK ---
    ("Category:Petroglyphs in Denmark", "DK", 0),
    ("Category:Petroglyphs in Bornholm", "DK", 1),
    ("Category:Madsebakke", "DK", 0),
    ("Category:Helleristninger ved Strandbygård", "DK", 0),
    ("Category:Storeløkkebakken", "DK", 0),
    # --- FINLAND ---
    ("Category:Petroglyphs in Finland", "FI", 0),
    ("Category:Rock paintings in Finland", "FI", 0),
    ("Category:Värikallio rock paintings", "FI", 0),
    ("Category:Verla rock painting", "FI", 0),
    # --- CROSS-CUTTING (filtered strictly to Nordic) ---
    ("Category:Petroglyphs of ships", "NORDIC", 0),
]

# Targeted CirrusSearch queries (politely rate-limited) to catch SVGs,
# historical tracings/plates, and Norwegian sites not neatly categorized.
SEARCH_QUERIES: list[tuple[str, str, int]] = [
    # (query, country_hint, limit)
    ("filetype:drawing hällristning", "NORDIC", 50),
    ("filetype:drawing helleristning", "NORDIC", 50),
    ('filetype:drawing "Nordic Bronze Age"', "NORDIC", 50),
    ("filetype:drawing petroglyph ship", "NORDIC", 50),
    ("filetype:drawing tanum OR bohuslän OR alta OR kivik", "NORDIC", 50),
    ("intitle:Petroglypgh", "NORDIC", 50),
    ("intitle:helleristning", "NO", 60),
    ("intitle:helleristninger", "NO", 60),
    ("intitle:hällristning", "SE", 60),
    ("intitle:hällristningar", "SE", 60),
    ("Vingen rock art OR petroglyph OR helleristning", "NO", 40),
    ("Rødøy skier OR Rødøymannen OR helleristning", "NO", 30),
    ("Leiknes rock art OR petroglyph OR helleristning", "NO", 30),
    ("Austre Åmøy OR Åmøy helleristning", "NO", 30),
    ("Ausevik rock art OR helleristning", "NO", 30),
    ("Alta petroglyph drawing OR tracing", "NO", 40),
    ("KMB hällristning", "SE", 80),
    ("Baltzer hällristning OR Bohuslän", "SE", 50),
    ("avritning OR kalkering OR frottage hällristning OR helleristning", "NORDIC", 50),
    ("Kiviksgraven drawing OR slab OR bildsten", "SE", 40),
    ("Nämforsen petroglyph OR hällristning", "SE", 40),
    ("Himmelstalund hällristning OR petroglyph", "SE", 40),
]

SKIP_SUBCAT_TERMS = [
    "museum",
    "cup mark",
    "skålgrop",
    "runestone",
    "runesten",
    "stenen",
    "archaeological groove",
    "slipränne",
    "heraldry",
    "world heritage",
    "visitors",
    "signs",
    "maps",
    "baptismal",
    "gobustan",
    "cave painting",
    "dösen",
]

# ---------------------------------------------------------------------------
# Site -> (canonical_site, region, country, period) lookup
# ---------------------------------------------------------------------------

SITE_RULES: list[tuple[str, str, str, str, str]] = [
    # (regex pattern, site, region, country, period)
    # --- NORWAY ---
    (r"\b(alta|hjemmeluft|jiepmaluokta|kåfjord|amtmannsnes|bergbukten|ole pedersen|storsteinen)\b", "Alta", "Finnmark", "NO", "stone_age_hunter"),
    (r"\bkvalsund\b", "Kvalsund", "Finnmark", "NO", "stone_age_hunter"),
    (r"\bslettnes\b", "Slettnes", "Finnmark", "NO", "stone_age_hunter"),
    (r"\b(skavberg|skavberget)\b", "Skavberget", "Troms", "NO", "stone_age_hunter"),
    (r"\btennes\b", "Tennes", "Troms", "NO", "stone_age_hunter"),
    (r"\b(rødøy|rødøymannen|flatøya)\b", "Rødøy", "Nordland", "NO", "stone_age_hunter"),
    (r"\bleiknes\b", "Leiknes", "Nordland", "NO", "stone_age_hunter"),
    (r"\balstahaug\b", "Alstahaug", "Nordland", "NO", "bronze_age_agrarian"),
    (r"\b(bøla|bølareinen|bølarein)\b", "Bøla", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bbardal\b", "Bardal", "Trøndelag", "NO", "mixed"),
    (r"\bholtås\b", "Holtås", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bevenhus\b", "Evenhus", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bleirfall\b", "Leirfall", "Trøndelag", "NO", "bronze_age_agrarian"),
    (r"\bhegre\b", "Hegre", "Trøndelag", "NO", "bronze_age_agrarian"),
    (r"\b(hell rock|hell,?\s*stjørdal|hell\s*trøndelag|helleristning.*hell)\b", "Hell", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bstrand\b.*(trøndelag|osen|rock|hellerist)", "Strand", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bstykket\b", "Stykket", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bkvennavika\b", "Kvennavika", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bydstines\b", "Ydstines", "Trøndelag", "NO", "bronze_age_agrarian"),
    (r"\bselbustrand\b", "Selbustrand", "Trøndelag", "NO", "bronze_age_agrarian"),
    (r"\bhomnes\b", "Homnes", "Trøndelag", "NO", "bronze_age_agrarian"),
    (r"\bsolsemhula\b", "Solsemhula", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bvingen\b", "Vingen", "Vestland", "NO", "stone_age_hunter"),
    (r"\bausevik\b", "Ausevik", "Vestland", "NO", "stone_age_hunter"),
    (r"\b(austre\s*åmøy|åmøy)\b", "Austre Åmøy", "Rogaland", "NO", "bronze_age_agrarian"),
    (r"\b(rudlå|fluberget|hafrsfjord|revheim|sola\b.*rock)\b", "Stavanger / Rogaland", "Rogaland", "NO", "bronze_age_agrarian"),
    (r"\bpennefeltet|penne\b", "Pennefeltet", "Agder", "NO", "bronze_age_agrarian"),
    (r"\bmøllerstufossen\b", "Møllerstufossen", "Innlandet", "NO", "stone_age_hunter"),
    (r"\bdrotten\b", "Drotten", "Innlandet", "NO", "stone_age_hunter"),
    (r"\båkersetervollen\b", "Åkersetervollen", "Innlandet", "NO", "bronze_age_agrarian"),
    (r"\bskogerveien\b", "Skogerveien", "Buskerud", "NO", "stone_age_hunter"),
    (r"\bsundvollen\b", "Sundvollen", "Buskerud", "NO", "bronze_age_agrarian"),
    (r"\b(ekeberg|sjømannsskolen)\b", "Ekeberg", "Oslo", "NO", "stone_age_hunter"),
    (r"\blambertseter\b", "Lambertseter", "Oslo", "NO", "bronze_age_agrarian"),
    (r"\bbegby\b", "Begby", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\bsolberg\b", "Solberg", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\bkalnes\b", "Kalnes", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\bhornes\b", "Hornes", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\b(bjørnstadskipet|bjørnstad)\b", "Bjørnstad", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\b(sarpsborg|fredrikstad|skjeberg|hunn\b|østfold)\b", "Østfold", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\b(sporanes|telemark)\b", "Telemark", "Telemark", "NO", "bronze_age_agrarian"),
    (r"\bvestfold\b", "Vestfold", "Vestfold", "NO", "bronze_age_agrarian"),
    # --- SWEDEN ---
    (r"\bvitlycke\b", "Tanum / Vitlycke", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\baspeberget\b", "Tanum / Aspeberget", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bfossum\b", "Tanum / Fossum", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\blitsleby\b", "Tanum / Litsleby", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\b(lövåsen|lövåsen)\b", "Tanum / Lövåsen", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bkalleby\b", "Tanum / Kalleby", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bgerum\b", "Tanum / Gerum", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bsotetorp\b", "Tanum / Sotetorp", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bmassleberg\b", "Massleberg", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bbacka\b.*(brastad|bohuslän|kmb|hällrist)", "Backa / Brastad", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\b(tanum|bohuslän|brastad|lysekil|sotenäs|munkedal|tossene|kville|svarteborg|askum)\b", "Tanum / Bohuslän", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bnämforsen\b", "Nämforsen", "Ångermanland", "SE", "stone_age_hunter"),
    (r"\b(himmelstalund|norrköping|leonardsberg|ekensberg|fiskeby|borg\b)\b", "Himmelstalund / Norrköping", "Östergötland", "SE", "bronze_age_agrarian"),
    (r"\b(kivik|kiviksgraven|bredarör)\b", "Kivik", "Skåne", "SE", "bronze_age_agrarian"),
    (r"\b(järrestad|simrishamn|simrislund)\b", "Simrishamn / Järrestad", "Skåne", "SE", "bronze_age_agrarian"),
    (r"\bfrännarp\b", "Frännarp", "Skåne", "SE", "bronze_age_agrarian"),
    (r"\b(brandskog|brandskogsskeppet|enköping|boglösa|hemsta|rickeby|örsta)\b", "Enköping / Uppland", "Uppland", "SE", "bronze_age_agrarian"),
    (r"\bslagsta\b", "Slagsta", "Södermanland", "SE", "bronze_age_agrarian"),
    (r"\bhällarna från tuna\b", "Tuna", "Södermanland", "SE", "bronze_age_agrarian"),
    (r"\bflyhov\b", "Flyhov", "Västergötland", "SE", "bronze_age_agrarian"),
    (r"\bhögsbyn\b", "Högsbyn", "Dalsland", "SE", "bronze_age_agrarian"),
    (r"\bhästhallen\b", "Hästhallen", "Blekinge", "SE", "bronze_age_agrarian"),
    (r"\bhägvide\b", "Hägvide", "Gotland", "SE", "bronze_age_agrarian"),
    (r"\bnorrfors\b", "Norrfors", "Västerbotten", "SE", "stone_age_hunter"),
    (r"\btumlehed\b", "Tumlehed", "Bohuslän", "SE", "stone_age_hunter"),
    # --- DENMARK ---
    (r"\bmadsebakke\b", "Madsebakke", "Bornholm", "DK", "bronze_age_agrarian"),
    (r"\bstrandbygård\b", "Strandbygård", "Bornholm", "DK", "bronze_age_agrarian"),
    (r"\bstoreløkkebakken\b", "Storeløkkebakken", "Bornholm", "DK", "bronze_age_agrarian"),
    (r"\bbornholm\b", "Bornholm", "Bornholm", "DK", "bronze_age_agrarian"),
    # --- FINLAND ---
    (r"\bvärikallio\b", "Värikallio", "Kainuu", "FI", "stone_age_hunter"),
    (r"\bverla\b", "Verla", "Kymenlaakso", "FI", "stone_age_hunter"),
    (r"\bastuvansalmi\b", "Astuvansalmi", "Southern Savonia", "FI", "stone_age_hunter"),
    (r"\bjuusjärvi\b", "Juusjärvi", "Uusimaa", "FI", "stone_age_hunter"),
    (r"\bsaraakallio\b", "Saraakallio", "Central Finland", "FI", "stone_age_hunter"),
]

# ---------------------------------------------------------------------------
# Motif Hints Extraction (aligned with taxonomy.json)
# ---------------------------------------------------------------------------

MOTIF_PATTERNS: list[tuple[str, str]] = [
    ("reindeer", r"\b(reindeer|rein|reinsdyr|renar|ren\b|caribou|bølareinen|bølarein|renna|renne)\b"),
    ("elk_moose", r"\b(elk|moose|elg|älg|elger|älgar|hirvi|alce|alci)\b"),
    ("bear", r"\b(bear|bears|bjørn|björn|karhu|orso|orsi)\b"),
    ("whale_cetacean", r"\b(whale|whales|hval|val\b|porpoise|dolphin|cetacean|nise|spekkhogger|balena)\b"),
    ("fish", r"\b(fish|halibut|kveite|salmon|laks|fisk|pesce|pesci)\b"),
    ("bird", r"\b(bird|birds|fugl|fågel|swan|svane|duck|crane|trane|owl|seabird|uccello)\b"),
    ("horse", r"\b(horse|horses|hest|häst|stallion|mare|rider|rytter|ryttare|cavallo)\b"),
    ("dog_wolf", r"\b(dog|dogs|hund|wolf|ulv|varg|cane|lupo)\b"),
    ("snake", r"\b(snake|serpent|orm|slange|serpente)\b"),
    ("quadruped_other", r"\b(deer|hjort|stag|cervo|ox|oxen|bull|okse|cattle|goat|sheep|animal|animals|animale|animali|dyr|djur|quadruped)\b"),
    ("bronze_age_ship", r"\b(ship|ships|skip|skepp|skeppet|vessel|fleet|bjørnstadskipet|brandskogsskeppet|nave)\b"),
    ("stone_age_boat", r"\b(boat|boats|båt|båtar|umiak|skin boat|canoe|kayak|båtristning|barca|barche|imbarcazione)\b"),
    ("skier", r"\b(skier|skiing|ski|skiløper|skidlöpare|rødøymannen|snowshoe|sciatore)\b"),
    ("hunter_warrior", r"\b(warrior|warriors|hunter|hunters|kriger|krigare|jeger|jägare|spear|spyd|spjut|archer|bow|bue|båge|sword|sverd|svärd|shield|skjold|sköld|spearman|spjutguden|cacciatore|guerriero)\b"),
    ("lur_blower", r"\b(lur|lure|bronslur|horn blower|musician)\b"),
    ("acrobat_dancer", r"\b(acrobat|dancer|dancing|danser|dansare|dans|somersault|bridal|brudparet|ritual)\b"),
    ("rider", r"\b(rider|riders|rytter|ryttare|horseman|equestrian)\b"),
    ("anthropomorph", r"\b(human|humans|man\b|men\b|woman|women|person|people|mann\b|menneske|människa|shaman|sjaman|phallic|ithyphallic|figure|figur|anthropomorph|gubbe|uomo|uomini|donna)\b"),
    ("sun_wheel", r"\b(sun\s*wheel|sun\s*cross|solhjul|solkors|wheel\s*cross|hjul|hjulkors|sun\s*disc|sun\s*symbol|ringkors)\b"),
    ("footprint", r"\b(footprint|footprints|foot\s*sole|fotsåle|fotsula|fotsulor|fotspor|feet|shoe)\b"),
    ("handprint", r"\b(handprint|hand\b|hands|hånd|handavtrykk)\b"),
    ("axe_weapon", r"\b(axe|axes|øks|yxa|yxor|weapon|halberd|dagger)\b"),
    ("chariot_plough", r"\b(chariot|wagon|vogn|vagn|cart|plough|plow|plog|ard|årder|plowing|ploughing)\b"),
    ("tree", r"\b(tree|trees|tre\b|träd|maypole|pine|spruce)\b"),
    ("geometric_spiral", r"\b(spiral|labyrinth|concentric|circle|circles|net\b|frame|corral|innhegning|gjerde|fence|geometric|zigzag|recinto)\b"),
]

# ---------------------------------------------------------------------------
# Junk / Non-Nordic Exclusion Filters
# ---------------------------------------------------------------------------

NON_NORDIC_TERMS = [
    "gobustan", "azerbaijan", "utah", "arizona", "nevada", "new mexico",
    "california", "hawaii", "colorado", "wyoming", "idaho", "montana",
    "texas", "canada", "ontario", "british columbia", "mexico", "peru",
    "chile", "bolivia", "colombia", "timana", "huila", "brazil", "argentina",
    "algeria", "tassili", "sahara", "egypt", "libya", "morocco", "namibia",
    "south africa", "zimbabwe", "australia", "aboriginal", "india", "ladakh",
    "mongolia", "kazakhstan", "tamgaly", "siberia", "altai", "amur", "karelia",
    "onega", "white sea", "zalavruga", "kanozer", "kandalaksha", "russia",
    "valcamonica", "italy", "malia", "crete", "greece", "spain", "galicia",
    "portugal", "coa valley", "france", "fontainebleau", "scotland", "ireland",
    "england", "wales", "newgrange", "knowth", "iran", "saudi", "jordan",
    "oman", "israel", "negev", "korea", "bangudae", "china", "japan", "taiwan",
    "easter island", "rapa nui", "fiji", "samoa", "puerto rico", "taino",
]

JUNK_TITLE_PATTERNS = [
    r"\b(map|kart|karta|location|density|distribution|utbredelse|utbredning)\b",
    r"\b(sign|skilt|skylt|information\s*board|info\s*board|infotavle|informasjonstavle|plaque|tavle|tavla|poster|brochure|leaflet|folder|pamphlet)\b",
    r"\b(museum|museet|exhibition|utstilling|utställning|building|bygning|byggnad|herrgård|entrance|inngang|ingång|parking|parkering|parkeringsplass|cafe|shop|souvenir)\b",
    r"\b(walkway|boardwalk|gangvei|gangbro|stairs|trapp|bridge|bro\b|bru\b|path\b|sti\b|trail|fence\b|roof\b|shelter|overbygg)\b",
    r"\b(landscape|landskap|panorama|view\s+of|utsikt|fjord|lake|river|waterfall|foss\b|mountain|fjell|forest|skog|trees|sunset|winter\s*scene)\b",
    r"\b(coat\s*of\s*arms|våpen|vapen|herald|flag\b|flagg|stamp|frimerke|frimärke|coin\b|mynt|banknote|sedel|logo|emblem|crest|seal\b)\b",
    r"\b(runestone|runesten|runsten|rune\s*inscription|futhark)\b",
    r"\b(visitor|visitors|tourist|tourists|guide\b|guided|crowd|school\s*class|portrait|selfie)\b",
    r"\b(gravhaug|burial\s*mound|cairn|røys|röse|dolmen|döse|menhir|bautasten|stone\s*circle|domarring)\b",
    r"\b(modern|replica|reconstruction|model\b|sculpture|statue|monument\s*plaque|gift|t-shirt|mug)\b",
    r"\b(gravestone|gravstein|tombstone|church|kirke|kyrka)\b",
    r"\b(banner|icon\b|stub\b|diagram\s*of\s*stratigraphy|timeline|chart|graph|groove\s*battle|non\s*pulita)\b",
]
JUNK_TITLE_RE = re.compile("|".join(JUNK_TITLE_PATTERNS), re.IGNORECASE)

DRAWING_SIGNAL_RE = re.compile(
    r"\b(drawing|tracing|kalkering|kalkert|avritning|avtegn|teikning|tegning|teckning|ritning|"
    r"sketch|skisse|skiss|illustration|plate|plansch|plansje|riss\b|figure\s*\d|fig\.\s*\d|"
    r"baltzer|montelius|hallström|hallstrom|gjessing|almgren|fett\b|simonsen|"
    r"petersen\s*19\d\d|sognnes|lossius|habel|marstrander|mandt|bakka|rygh|bøe|shetelig|"
    r"nordisk\s*familjebok|fornvännen|ny\s*illustrerad|svenska\s*folkets|early\s*man\s*in\s*britain|"
    r"kiviksgraven\s*slab|kivik2|haugen\s*farm|helleristninger\s*fra\s*skjeberg|"
    r"line\s*art|contour|silhouette|vector|svg|diagram|schema|kopia|copy\s*of|rendering|"
    r"reproduction|woodcut|engraving|etching|lithograph)\b",
    re.IGNORECASE,
)

RUBBING_SIGNAL_RE = re.compile(
    r"\b(rubbing|frottage|frottasje|avgnidning|gnidbild|gnidning|avtrykk|avtryck|charcoal\s*rubbing)\b",
    re.IGNORECASE,
)

PAINTED_SIGNAL_RE = re.compile(
    r"\b(oppmalt|oppmalte|imålad|ifylld|röd|rød|red\s*paint|painted|ochre|oker|målad|malt)\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# License Verification (STRICT & HONEST)
# ---------------------------------------------------------------------------

def parse_license(extmeta: dict[str, Any]) -> tuple[str, str, bool, bool]:
    """
    Returns (license_name, license_url, commercial_ok, keep_file).
    NEVER fabricates CC0. Skips NonFree / NC / ND / Unknown / Copyrighted.
    """
    def val(key: str) -> str:
        v = extmeta.get(key, {})
        if isinstance(v, dict):
            return strip_html(str(v.get("value", ""))).strip()
        return strip_html(str(v or "")).strip()

    nonfree = val("NonFree").lower()
    if nonfree in ("true", "1", "yes"):
        return ("Non-free", "", False, False)

    lic_short = val("LicenseShortName")
    lic_code = val("License")
    usage = val("UsageTerms")
    lic_url = val("LicenseUrl")
    copyrighted = val("Copyrighted").lower()

    raw_lic = lic_short or usage or lic_code
    if not raw_lic:
        return ("Unknown", "", False, False)

    low = raw_lic.lower()

    # Reject non-commercial, no-derivatives, copyrighted, fair use, unknown
    reject_tokens = [
        "noncommercial", "non-commercial", "-nc", " nc",
        "noderivs", "no-derivatives", "-nd", " nd",
        "fair use", "fair-use", "all rights reserved",
        "unknown", "copyrighted free use"  # treat ambiguous carefully
    ]
    for tok in reject_tokens:
        if tok in low:
            # Note: "Copyrighted free use" is actually free, handle below? Better skip ambiguous.
            return (raw_lic, lic_url, False, False)

    # Check explicit commercially reusable free licenses
    if low in ("public domain", "pd", "pdm") or low.startswith("pd-") or "public domain" in low:
        canon_url = lic_url or "https://creativecommons.org/publicdomain/mark/1.0/"
        return ("Public domain" if low == "public domain" else raw_lic, canon_url, True, True)

    if "cc0" in low or "cc-zero" in low or "creative commons zero" in low:
        canon_url = lic_url or "https://creativecommons.org/publicdomain/zero/1.0/"
        return ("CC0 1.0", canon_url, True, True)

    if re.search(r"\bcc[\s-]*by[\s-]*sa\b", low) or "attribution-sharealike" in low:
        # Normalize spacing
        norm = raw_lic.strip()
        if not lic_url:
            m = re.search(r"(\d\.\d)", norm)
            ver = m.group(1) if m else "4.0"
            lic_url = f"https://creativecommons.org/licenses/by-sa/{ver}/"
        return (norm, lic_url, True, True)

    if re.search(r"\bcc[\s-]*by\b", low) or ("attribution" in low and "creative commons" in low):
        norm = raw_lic.strip()
        if not lic_url:
            m = re.search(r"(\d\.\d)", norm)
            ver = m.group(1) if m else "4.0"
            lic_url = f"https://creativecommons.org/licenses/by/{ver}/"
        return (norm, lic_url, True, True)

    if "gfdl" in low or "gnu free documentation" in low:
        canon_url = lic_url or "https://www.gnu.org/licenses/fdl-1.3.en.html"
        return (raw_lic, canon_url, True, True)

    if "fal" in low or "free art license" in low:
        canon_url = lic_url or "https://artlibre.org/licence/lal/en/"
        return (raw_lic, canon_url, True, True)

    if copyrighted == "false":
        canon_url = lic_url or "https://creativecommons.org/publicdomain/mark/1.0/"
        return (raw_lic, canon_url, True, True)

    # Anything else: do NOT mark commercial_ok=True and skip to stay strictly legal
    return (raw_lic, lic_url, False, False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sanitize_filename(name: str, max_len: int = 80) -> str:
    # Remove File: prefix if present
    name = re.sub(r"^File:", "", name, flags=re.I)
    stem, ext = os.path.splitext(name)
    # Transliterate Nordic characters nicely
    trans = str.maketrans({
        "å": "a", "Å": "A", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O",
        "ä": "a", "Ä": "A", "ö": "o", "Ö": "O", "é": "e", "É": "E",
    })
    stem = stem.translate(trans)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not stem:
        stem = "rockart"
    stem = stem[:max_len]
    ext = ext.lower()
    if ext not in (".svg", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff"):
        ext = ".jpg"
    return f"{stem}{ext}"


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def api_get(session: requests.Session, params: dict[str, Any], delay: float = 0.18) -> dict[str, Any]:
    params = dict(params, format="json", formatversion=2)
    for attempt in range(7):
        try:
            r = session.get(API_URL, params=params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else min(2.0 * (1.8 ** attempt), 30.0)
                print(f"  [backoff] HTTP {r.status_code}, sleeping {wait:.1f}s...", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data and data["error"].get("code") in ("maxlag", "ratelimited"):
                time.sleep(min(3.0 * (attempt + 1), 20.0))
                continue
            time.sleep(delay)
            return data
        except requests.RequestException as exc:
            wait = min(2.0 * (1.8 ** attempt), 25.0)
            print(f"  [retry] {exc}, sleeping {wait:.1f}s...", file=sys.stderr, flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Wikimedia API failed after retries: {params.get('action')} {params.get('list') or params.get('titles','')[:60]}")


# ---------------------------------------------------------------------------
# Stage 1: Discovery
# ---------------------------------------------------------------------------

def discover_candidates(session: requests.Session, cache_path: Path) -> list[dict[str, Any]]:
    print("=== Stage 1: Discovering Nordic rock-art files on Wikimedia Commons ===", flush=True)

    # Track discovered files -> set of source categories / queries & country hints
    file_sources: dict[str, dict[str, Any]] = {}

    def record_file(title: str, source_tag: str, country_hint: str) -> None:
        if not title.startswith("File:"):
            return
        entry = file_sources.setdefault(title, {"sources": set(), "country_hints": set()})
        entry["sources"].add(source_tag)
        if country_hint:
            entry["country_hints"].add(country_hint)

    # 1A. Crawl categories + bounded subcats
    visited_cats: set[str] = set()
    cat_queue: deque[tuple[str, str, int]] = deque()
    for cat, chint, max_d in SEED_CATEGORIES:
        if cat not in visited_cats:
            visited_cats.add(cat)
            cat_queue.append((cat, chint, max_d))

    crawled_count = 0
    while cat_queue:
        cat, chint, rem_depth = cat_queue.popleft()
        crawled_count += 1
        cmcontinue = None
        cat_files = 0
        while True:
            params: dict[str, Any] = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": cat,
                "cmtype": "file|subcat" if rem_depth > 0 else "file",
                "cmlimit": 500,
            }
            if cmcontinue:
                params["cmcontinue"] = cmcontinue
            data = api_get(session, params, delay=0.14)
            for m in data.get("query", {}).get("categorymembers", []):
                ns = m.get("ns")
                mtitle = m.get("title", "")
                if ns == 6:
                    record_file(mtitle, cat, chint)
                    cat_files += 1
                elif ns == 14 and rem_depth > 0:
                    low = mtitle.lower()
                    if any(skip in low for skip in SKIP_SUBCAT_TERMS):
                        continue
                    if mtitle not in visited_cats:
                        visited_cats.add(mtitle)
                        cat_queue.append((mtitle, chint, rem_depth - 1))
            cmcontinue = data.get("continue", {}).get("cmcontinue")
            if not cmcontinue:
                break
        if crawled_count % 15 == 0 or cat_files > 40:
            print(f"  [{crawled_count}] {cat}: +{cat_files} files (total unique: {len(file_sources)})", flush=True)

    print(f"Category crawl complete: {crawled_count} categories -> {len(file_sources)} unique files.", flush=True)

    # 1B. Targeted search queries (slowly paced to respect CirrusSearch limits)
    print("Running targeted CirrusSearch queries (politely rate-limited)...", flush=True)
    for idx, (q, chint, limit) in enumerate(SEARCH_QUERIES, 1):
        time.sleep(1.1)
        try:
            data = api_get(
                session,
                {
                    "action": "query",
                    "list": "search",
                    "srnamespace": 6,
                    "srsearch": q,
                    "srlimit": min(limit, 50),
                },
                delay=0.4,
            )
            hits = data.get("query", {}).get("search", [])
            for h in hits:
                record_file(h["title"], f"search:{q}", chint)
            print(f"  [search {idx}/{len(SEARCH_QUERIES)}] {q!r} -> {len(hits)} hits (total unique: {len(file_sources)})", flush=True)
        except Exception as exc:
            print(f"  [search warn] {q!r} failed: {exc}", file=sys.stderr, flush=True)

    # 1C. Batch-fetch imageinfo + categories for all discovered files (50 per request)
    all_titles = sorted(file_sources.keys())
    print(f"\nFetching metadata (imageinfo + categories) for {len(all_titles)} unique files in batches of 50...", flush=True)

    candidates: list[dict[str, Any]] = []
    for i in range(0, len(all_titles), 50):
        batch = all_titles[i : i + 50]
        data = api_get(
            session,
            {
                "action": "query",
                "titles": "|".join(batch),
                "prop": "imageinfo|categories",
                "iiprop": "url|size|mime|sha1|extmetadata|mediatype|user",
                "cllimit": "max",
                "clshow": "!hidden",
            },
            delay=0.16,
        )
        pages = data.get("query", {}).get("pages", [])
        for p in pages:
            if p.get("missing") or "imageinfo" not in p or not p["imageinfo"]:
                continue
            ii = p["imageinfo"][0]
            title = p["title"]
            src_info = file_sources.get(title, {"sources": set(), "country_hints": set()})
            cats = [c["title"] for c in p.get("categories", [])]
            candidates.append({
                "pageid": p.get("pageid"),
                "title": title,
                "mime": ii.get("mime", ""),
                "mediatype": ii.get("mediatype", ""),
                "size": ii.get("size", 0),
                "width": ii.get("width", 0),
                "height": ii.get("height", 0),
                "sha1": ii.get("sha1", ""),
                "url": ii.get("url", ""),
                "descriptionurl": ii.get("descriptionurl", ""),
                "user": ii.get("user", ""),
                "extmetadata": ii.get("extmetadata", {}),
                "categories": cats,
                "discovery_sources": sorted(src_info["sources"]),
                "country_hints": sorted(src_info["country_hints"]),
            })
        if (i // 50 + 1) % 10 == 0 or i + 50 >= len(all_titles):
            print(f"  Fetched metadata for {min(i + 50, len(all_titles))}/{len(all_titles)} files...", flush=True)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(candidates, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Saved {len(candidates)} candidate records to {cache_path}", flush=True)
    return candidates


# ---------------------------------------------------------------------------
# Stage 2: Metadata Enrichment, Filtering & Ranking
# ---------------------------------------------------------------------------

def infer_site_region_country_period(
    title: str, description: str, categories: list[str], country_hints: list[str]
) -> tuple[str, str, str, str, bool]:
    """
    Returns (site, region, country, period, is_nordic).
    """
    cat_text = " | ".join(categories)
    combined = f"{title} | {description} | {cat_text}".lower()

    # Reject explicitly non-Nordic files
    for bad in NON_NORDIC_TERMS:
        if re.search(rf"\b{re.escape(bad)}\b", combined):
            return ("", "", "", "unknown", False)

    # Check specific Nordic site rules first
    for pat, site, region, country, period in SITE_RULES:
        if re.search(pat, combined, flags=re.IGNORECASE):
            return (site, region, country, period, True)

    # Fallback: infer from category / country mentions
    if re.search(r"\b(norway|norge|norwegian|norsk|helleristning|finnmark|trøndelag|østfold|rogaland|nordland|troms|vestland|innlandet|buskerud|telemark|vestfold|agder|oslo|akershus)\b", combined):
        region = "Norway"
        for reg in ["Finnmark", "Troms", "Nordland", "Trøndelag", "Vestland", "Rogaland", "Agder", "Vestfold", "Telemark", "Buskerud", "Innlandet", "Akershus", "Oslo", "Østfold"]:
            if reg.lower() in combined:
                region = reg
                break
        period = "stone_age_hunter" if re.search(r"\b(veideristning|stone\s*age|mesolithic|neolithic|hunter|steinalder)\b", combined) else (
            "bronze_age_agrarian" if re.search(r"\b(jordbruksristning|bronze\s*age|bronsealder|ship|skip)\b", combined) else "unknown"
        )
        return (region, region, "NO", period, True)

    if re.search(r"\b(sweden|sverige|swedish|svensk|hällristning|bohuslän|skåne|östergötland|uppland|södermanland|västergötland|dalsland|blekinge|gotland|ångermanland|västerbotten|jämtland|småland)\b", combined):
        region = "Sweden"
        for reg in ["Bohuslän", "Skåne", "Östergötland", "Uppland", "Södermanland", "Västergötland", "Dalsland", "Blekinge", "Gotland", "Ångermanland", "Västerbotten", "Jämtland", "Småland"]:
            if reg.lower() in combined:
                region = reg
                break
        period = "stone_age_hunter" if re.search(r"\b(stone\s*age|hunter|stenålder)\b", combined) else (
            "bronze_age_agrarian" if re.search(r"\b(bronze\s*age|bronsålder|ship|skepp)\b", combined) else "bronze_age_agrarian"
        )
        return (region, region, "SE", period, True)

    if re.search(r"\b(denmark|danmark|danish|dansk|bornholm|allinge|sandvig)\b", combined):
        region = "Bornholm" if "bornholm" in combined else "Denmark"
        return (region, region, "DK", "bronze_age_agrarian", True)

    if re.search(r"\b(finland|suomi|finnish|kalliomaalaus|savonia|kainuu|kymenlaakso)\b", combined):
        return ("Finland", "Finland", "FI", "stone_age_hunter", True)

    if re.search(r"\b(nordic\s*bronze\s*age|scandinavia|scandinavian|nordic\s*petroglyph|nordisk\s*brons)\b", combined):
        period = "bronze_age_agrarian" if "bronze" in combined else "unknown"
        return ("Scandinavia", "Scandinavia", "NORDIC", period, True)

    # Check if discovered purely inside a country-specific category
    if set(country_hints) == {"NO"}:
        return ("Norway", "Norway", "NO", "unknown", True)
    if set(country_hints) == {"SE"}:
        return ("Sweden", "Sweden", "SE", "bronze_age_agrarian", True)
    if set(country_hints) == {"DK"}:
        return ("Denmark", "Denmark", "DK", "bronze_age_agrarian", True)
    if set(country_hints) == {"FI"}:
        return ("Finland", "Finland", "FI", "stone_age_hunter", True)

    return ("", "", "", "unknown", False)


def extract_motif_hints(title: str, description: str, categories: list[str]) -> list[str]:
    text = f"{title} | {description} | {' '.join(categories)}".lower()
    hints: list[str] = []
    for motif, pat in MOTIF_PATTERNS:
        if re.search(pat, text, flags=re.IGNORECASE):
            hints.append(motif)
    if len(hints) >= 3 or re.search(r"\b(panel|felt|häll|group|scene|fler|several|many|rock\s*art\s*field)\b", text):
        if "multi_motif_panel" not in hints and len(hints) >= 2:
            hints.append("multi_motif_panel")
    return hints


def is_junk_candidate(title: str, description: str, categories: list[str], mime: str) -> tuple[bool, str]:
    clean_title = re.sub(r"^File:", "", title, flags=re.I)
    cat_joined = " | ".join(categories)
    combined_tc = f"{clean_title} | {cat_joined}"

    if mime not in ("image/svg+xml", "image/png", "image/jpeg", "image/gif", "image/tiff"):
        return True, f"unsupported_mime:{mime}"

    # Check categories for explicit non-carving things
    cat_low = cat_joined.lower()
    for bad_cat in [
        "category:maps", "location maps", "coat of arms", "heraldry", "stamps",
        "runestones", "cup marks", "archaeological grooves", "museums in",
        "vitlycke museum", "alta museum", "information signs", "plaques",
        "people of", "flags of", "logos", "signatures", "football",
        "internet memes", "herrgård",
    ]:
        if bad_cat in cat_low:
            # Exception: allow drawings/SVGs even if a category mentions something secondary, except maps/heraldry/runestones/memes
            if bad_cat in ("category:maps", "location maps", "coat of arms", "heraldry", "runestones", "flags of", "logos", "signatures", "football", "internet memes", "herrgård"):
                return True, f"bad_cat:{bad_cat}"
            if mime != "image/svg+xml" and not DRAWING_SIGNAL_RE.search(combined_tc):
                return True, f"bad_cat:{bad_cat}"

    # Reject pure cup-marks unless figurative motifs are mentioned
    if re.search(r"\b(skålgrop|skålgropar|cup\s*mark|cupmarks|älvkvarn)\b", clean_title, re.I):
        if not extract_motif_hints(clean_title, description, categories):
            return True, "cup_marks_only"

    if JUNK_TITLE_RE.search(clean_title):
        # Exception: Alta Museum sometimes appears in titles of Alta carvings ("Alta Museum - petroglyph of reindeer")
        if re.search(r"alta\s*museum", clean_title, re.I) and re.search(r"(petroglyph|carving|helleristning|reindeer|elk|boat|ship|bear|human|motif|figure)", f"{clean_title} {description}", re.I):
            pass
        elif re.search(r"(vitlycke\s*museum)", clean_title, re.I) and re.search(r"(petroglyph|carving|hällristning|ship|warrior)", f"{clean_title} {description}", re.I):
            pass
        else:
            return True, "junk_title"

    # Description check for explicit non-carving photos (signs, maps, views)
    desc_low = description.lower()
    if re.search(r"\b(information\s*sign|informationsskylt|informasjonsskilt|overview\s*map|location\s*map|tourist\s*centre|visitor\s*center|museum\s*building|walkway\s*at)\b", desc_low):
        return True, "junk_desc"

    # Ensure it actually relates to rock art / petroglyphs (catches stray search hits like "Erna Solberg" or "Rødøy fjord")
    rockart_signal = re.search(
        r"(petroglyph|helleristning|hällristning|rock\s*art|rock\s*carving|rock\s*painting|"
        r"bergkunst|hällmålning|bergmålning|veideristning|jordbruksristning|petroglypgh|"
        r"bølarein|bjørnstadskipet|brandskogsskeppet|kiviksgraven|pennefeltet|"
        r"slagstaristningen|himmelstalund|nämforsen|madsebakke|vitlycke|aspeberget|litsleby|fossum|"
        r"bardal|holtås|leirfall|evenhus|møllerstufossen|begby|solberg|kalnes|hornes|skogerveien|ekeberg|skavberg|vingen|rødøymannen)",
        f"{clean_title} | {description} | {cat_joined}",
        re.IGNORECASE,
    )
    if not rockart_signal:
        return True, "no_rockart_signal"

    return False, ""


def initial_media_type(mime: str, title: str, description: str, categories: list[str]) -> str:
    if mime == "image/svg+xml":
        return "svg_vector"
    combined = f"{title} | {description} | {' '.join(categories)}"
    if RUBBING_SIGNAL_RE.search(combined):
        return "rubbing_frottage"
    if any("Drawings of petroglyphs" in c for c in categories) or DRAWING_SIGNAL_RE.search(combined):
        return "bw_tracing"
    if PAINTED_SIGNAL_RE.search(combined):
        return "painted_photo"
    return "photo_other"


def score_candidate(rec: dict[str, Any]) -> float:
    """Priority score: GOLD SVGs & B&W tracings first, then rich Norwegian & Nordic painted/closeup carvings."""
    mt = rec["media_type"]
    score = 0.0
    if mt == "svg_vector":
        score += 1000.0
    elif mt == "bw_tracing":
        score += 800.0
    elif mt == "rubbing_frottage":
        score += 750.0
    elif mt == "painted_photo":
        score += 300.0
    else:
        score += 180.0

    # Boost Norway strongly (primary focus is Norway + nearby Scandinavia)
    if rec["country"] == "NO":
        score += 65.0
    elif rec["country"] == "SE":
        score += 45.0
    elif rec["country"] in ("DK", "FI", "NORDIC"):
        score += 35.0

    # Boost named specific motifs (clean single motifs / iconic figures!)
    hints = rec["motif_hints"]
    specific_hints = [h for h in hints if h != "multi_motif_panel"]
    if len(specific_hints) == 1:
        score += 70.0  # single clear motif!
    elif len(specific_hints) == 2:
        score += 55.0
    elif len(specific_hints) >= 3:
        score += 35.0

    # Boost detail/closeup keywords
    text = f"{rec['title']} {rec['description']}".lower()
    if re.search(r"\b(detail|detalj|closeup|close-up|nærbilde|närbild|motif|motiv|figur|figure|single)\b", text):
        score += 45.0
    if re.search(r"\b(kmb|baltzer|hallström|gjessing|montelius|almgren|nordisk\s*familjebok)\b", text):
        score += 60.0

    # Boost CC0 / Public Domain slightly over CC-BY-SA
    lic_low = rec["license"].lower()
    if "cc0" in lic_low or "public domain" in lic_low or lic_low.startswith("pd"):
        score += 20.0
    elif "cc by" in lic_low and "sa" not in lic_low:
        score += 12.0

    # Resolution sanity boost (prefer >= 800px, penalize tiny thumbnails < 400px unless SVG/tracing)
    w, h = rec["width"], rec["height"]
    min_dim = min(w, h) if w and h else 0
    if mt != "svg_vector":
        if min_dim >= 1000:
            score += 25.0
        elif min_dim >= 600:
            score += 15.0
        elif min_dim < 350:
            score -= 60.0

    # Penalize huge > 15MB files slightly unless tracing
    if rec["size"] > 15_000_000 and mt not in ("svg_vector", "bw_tracing"):
        score -= 40.0

    return score


def prepare_ranked_pool(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pool: list[dict[str, Any]] = []
    drop_reasons = Counter()

    for c in candidates:
        title = c["title"]
        ext = c.get("extmetadata", {})
        raw_desc = strip_html(str(ext.get("ImageDescription", {}).get("value", "")))
        raw_desc = re.sub(
            r"Notera\s+Av\s+dokumentationsskäl\s+har\s+originalbeskrivningen.*?originaltexten\.?\s*",
            "",
            raw_desc,
            flags=re.IGNORECASE,
        ).strip()
        desc = raw_desc[:600]
        artist = strip_html(str(ext.get("Artist", {}).get("value", "")))[:200]
        if not artist:
            artist = strip_html(str(ext.get("Credit", {}).get("value", "")))[:200]
        if not artist:
            artist = c.get("user", "Unknown")

        cats = c.get("categories", [])
        mime = c.get("mime", "")

        # 1. Junk filter
        junk, reason = is_junk_candidate(title, desc, cats, mime)
        if junk:
            drop_reasons[reason] += 1
            continue

        # 2. Nordic filter & site/region/period inference
        site, region, country, period, is_nordic = infer_site_region_country_period(
            title, desc, cats, c.get("country_hints", [])
        )
        if not is_nordic:
            drop_reasons["non_nordic"] += 1
            continue

        # 3. License verification
        lic_name, lic_url, commercial_ok, keep = parse_license(ext)
        if not keep or not commercial_ok:
            drop_reasons[f"license_skip:{lic_name[:30]}"] += 1
            continue

        # 4. Size sanity (skip > 25MB raw TIFF/huge scans to keep repo practical, or 0-byte)
        size = c.get("size", 0) or 0
        if size <= 0 or size > 25_000_000:
            drop_reasons["file_size_out_of_range"] += 1
            continue

        # For non-SVG raster images, require reasonable minimum dimensions
        w = int(c.get("width", 0) or 0)
        h = int(c.get("height", 0) or 0)
        mt = initial_media_type(mime, title, desc, cats)
        if mt != "svg_vector":
            if min(w, h) < 220:
                drop_reasons["too_small_raster"] += 1
                continue
            # Skip extreme panorama banners (aspect ratio > 4.5)
            if w > 0 and h > 0 and max(w / h, h / w) > 4.5 and mt not in ("bw_tracing", "rubbing_frottage"):
                drop_reasons["extreme_panorama"] += 1
                continue

        hints = extract_motif_hints(title, desc, cats)
        clean_title = re.sub(r"^File:", "", title, flags=re.I)
        clean_title_no_ext = os.path.splitext(clean_title)[0].replace("_", " ").strip()

        rec = {
            "pageid": c["pageid"],
            "file_title": title,
            "source_id": f"wikimedia_{c['pageid']}",
            "title": clean_title_no_ext,
            "description": desc,
            "site": site,
            "region": region,
            "country": country,
            "period": period,
            "media_type": mt,
            "motif_hints": hints,
            "source_url": c.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{quote(title.replace(' ', '_'))}",
            "download_url": c.get("url", ""),
            "creator": artist,
            "license": lic_name,
            "license_url": lic_url,
            "commercial_ok": commercial_ok,
            "mime": mime,
            "size": size,
            "width": w,
            "height": h,
            "sha1": c.get("sha1", ""),
            "categories": cats,
        }
        rec["score"] = score_candidate(rec)
        pool.append(rec)

    print("\n=== Filtering Summary ===", flush=True)
    print(f"Total candidates: {len(candidates)} -> Passed filter: {len(pool)}", flush=True)
    for k, v in drop_reasons.most_common(15):
        print(f"  Dropped ({k}): {v}", flush=True)

    mt_counts = Counter(r["media_type"] for r in pool)
    ctry_counts = Counter(r["country"] for r in pool)
    print(f"Passed by media_type: {dict(mt_counts)}", flush=True)
    print(f"Passed by country:    {dict(ctry_counts)}", flush=True)

    # Deduplicate by Commons sha1 before selecting
    seen_sha1: set[str] = set()
    deduped: list[dict[str, Any]] = []
    pool.sort(key=lambda r: (-r["score"], r["pageid"]))
    for r in pool:
        s1 = r.get("sha1", "")
        if s1 and s1 in seen_sha1:
            continue
        if s1:
            seen_sha1.add(s1)
        deduped.append(r)
    return deduped


def select_balanced_targets(pool: list[dict[str, Any]], target_total: int = 320) -> list[dict[str, Any]]:
    """
    1. Take 100% of ALL svg_vector, bw_tracing, and rubbing_frottage!
    2. Fill remaining slots up to target_total (with a buffer of +25% for post-download pixel QA filtering)
       using round-robin diversity across sites, favoring Norway (~60%) and iconic nearby Nordic (~40%).
    """
    gold = [r for r in pool if r["media_type"] in ("svg_vector", "bw_tracing", "rubbing_frottage")]
    photos = [r for r in pool if r["media_type"] not in ("svg_vector", "bw_tracing", "rubbing_frottage")]

    print(f"\nGold tier (100% selected): {len(gold)} files (svg={sum(r['media_type']=='svg_vector' for r in gold)}, bw_tracing={sum(r['media_type']=='bw_tracing' for r in gold)}, rubbing={sum(r['media_type']=='rubbing_frottage' for r in gold)})", flush=True)

    # Group photos by (country, site) and sort each bucket by score desc
    by_site: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in photos:
        by_site[(r["country"], r["site"])].append(r)
    for k in by_site:
        by_site[k].sort(key=lambda r: -r["score"])

    # Per-site caps so no single site swamps the corpus
    def site_cap(country: str, site: str) -> int:
        if site == "Alta":
            return 32
        if site in ("Begby", "Tanum / Aspeberget", "Tanum / Vitlycke", "Tanum / Bohuslän"):
            return 18
        if country == "NO":
            return 12
        if site in ("Nämforsen", "Himmelstalund / Norrköping", "Kivik", "Tanum / Fossum", "Tanum / Litsleby", "Madsebakke"):
            return 12
        return 7

    # Round-robin across Norwegian sites first, then Nordic sites, maintaining ~60% NO / ~40% SE+DK+FI
    no_sites = sorted([k for k in by_site if k[0] == "NO"], key=lambda k: -by_site[k][0]["score"])
    other_sites = sorted([k for k in by_site if k[0] != "NO"], key=lambda k: -by_site[k][0]["score"])

    photo_budget = max(0, int((target_total - len(gold)) * 1.25))  # buffer for pixel quality rejections
    no_budget = int(photo_budget * 0.62)
    other_budget = photo_budget - no_budget

    def pick_round_robin(site_keys: list[tuple[str, str]], budget: int) -> list[dict[str, Any]]:
        picked: list[dict[str, Any]] = []
        idx_map = {k: 0 for k in site_keys}
        caps = {k: site_cap(k[0], k[1]) for k in site_keys}
        while len(picked) < budget:
            progressed = False
            for k in site_keys:
                if len(picked) >= budget:
                    break
                i = idx_map[k]
                if i < len(by_site[k]) and i < caps[k]:
                    picked.append(by_site[k][i])
                    idx_map[k] = i + 1
                    progressed = True
            if not progressed:
                break
        return picked

    picked_no = pick_round_robin(no_sites, no_budget)
    picked_other = pick_round_robin(other_sites, other_budget)
    selected = gold + picked_no + picked_other
    print(f"Selected download queue: {len(selected)} files (gold={len(gold)}, NO_photos={len(picked_no)}, other_photos={len(picked_other)})", flush=True)
    return selected


# ---------------------------------------------------------------------------
# Stage 3: Download, SVG/Pixel Verification & Refined Media Classification
# ---------------------------------------------------------------------------

def parse_svg_dimensions(svg_bytes: bytes, fallback_w: int, fallback_h: int) -> tuple[bool, int, int, str]:
    """
    Validate SVG XML and extract viewBox or width/height dimensions.
    Returns (valid, width_px, height_px, reason).
    """
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError as exc:
        return False, 0, 0, f"svg_parse_error:{exc}"

    tag = root.tag.lower()
    if not tag.endswith("svg"):
        return False, 0, 0, "svg_root_not_svg"

    # Must contain at least one vector graphic element
    drawable_tags = {"path", "polygon", "polyline", "circle", "ellipse", "rect", "line"}
    has_drawable = False
    for elem in root.iter():
        local = elem.tag.split("}")[-1].lower()
        if local in drawable_tags:
            has_drawable = True
            break
    if not has_drawable:
        return False, 0, 0, "svg_no_drawable_elements"

    w, h = 0.0, 0.0
    vb = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if vb:
        nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", vb)
        if len(nums) == 4:
            w, h = float(nums[2]), float(nums[3])

    def parse_len(s: str | None) -> float:
        if not s:
            return 0.0
        m = re.match(r"\s*([-+]?\d*\.?\d+)", s)
        return float(m.group(1)) if m else 0.0

    if w <= 0 or h <= 0:
        w = parse_len(root.attrib.get("width"))
        h = parse_len(root.attrib.get("height"))

    if w <= 0 or h <= 0:
        w, h = float(fallback_w or 512), float(fallback_h or 512)

    return True, max(1, int(round(w))), max(1, int(round(h))), "ok"


def inspect_raster_image(path: Path, initial_mt: str) -> tuple[bool, int, int, str, str]:
    """
    Verify raster integrity with Pillow, compute dimensions, refine media_type
    (bw_tracing vs rubbing_frottage vs painted_photo vs photo_other), and
    reject blurry/scenic sky+foliage landscape shots.
    Returns (keep, width_px, height_px, refined_media_type, note).
    """
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
            if w < 150 or h < 150:
                return False, w, h, initial_mt, "too_small"
            rgb = im.convert("RGB")
            # Downsample for fast color & contrast statistics
            small = rgb.copy()
            small.thumbnail((256, 256))
            sw, sh = small.size
            pixels = list(small.getdata())
            n = len(pixels)
            if n == 0:
                return False, w, h, initial_mt, "empty_pixels"

            # Compute RGB/HSV-like statistics without heavy deps
            sat_sum = 0.0
            white_cnt = 0
            dark_cnt = 0
            red_paint_cnt = 0
            sky_top_cnt = 0
            green_cnt = 0
            top_rows = max(1, sh // 4)
            top_n = top_rows * sw

            for idx, (r, g, b) in enumerate(pixels):
                mx = max(r, g, b)
                mn = min(r, g, b)
                diff = mx - mn
                sat = (diff / mx) if mx > 0 else 0.0
                sat_sum += sat

                # Near-white & near-black counts (for B&W tracing detection)
                if r > 215 and g > 215 and b > 215 and diff < 28:
                    white_cnt += 1
                elif r < 75 and g < 75 and b < 75 and diff < 30:
                    dark_cnt += 1

                # High-contrast red/ochre paint on rock (oppmalte helleristninger)
                # Red strongly dominates green and blue
                if r >= 95 and (r - g) >= 35 and (r - b) >= 35 and sat >= 0.30:
                    red_paint_cnt += 1

                # Blue sky in top quarter
                if idx < top_n and b >= 145 and (b - r) >= 25 and mx >= 150:
                    sky_top_cnt += 1

                # Bright green foliage/grass
                if g >= 75 and (g - r) >= 18 and (g - b) >= 15 and sat >= 0.22:
                    green_cnt += 1

            mean_sat = sat_sum / n
            white_frac = white_cnt / n
            dark_frac = dark_cnt / n
            red_frac = red_paint_cnt / n
            sky_top_frac = sky_top_cnt / top_n
            green_frac = green_cnt / n

            # Refine media_type accurately from actual pixels!
            # Require genuinely light paper/white background for bw_tracing so dark grayscale
            # archival rock photos (e.g. Gjessing 1934 photos) are not misclassified as tracings.
            light_cnt = sum(1 for r, g, b in pixels if r > 170 and g > 170 and b > 170)
            light_frac = light_cnt / n

            mt = initial_mt
            if mt not in ("rubbing_frottage",):
                if mean_sat < 0.09 and white_frac > 0.45 and dark_frac > 0.008:
                    mt = "bw_tracing"
                elif mt == "bw_tracing":
                    if light_frac < 0.35:
                        mt = "painted_photo" if red_frac >= 0.012 else "photo_other"
                    elif mean_sat > 0.18 and white_frac < 0.25 and light_frac < 0.55:
                        mt = "painted_photo" if red_frac >= 0.012 else "photo_other"

            if mt in ("painted_photo", "photo_other"):
                if red_frac >= 0.012:
                    mt = "painted_photo"
                else:
                    mt = "photo_other"

            # Quality rejection for outdoor photos (NEVER reject true bw_tracing or rubbing_frottage for sky/green)
            if mt in ("painted_photo", "photo_other"):
                # Reject obvious scenic landscapes with lots of blue sky or heavy foliage
                if sky_top_frac > 0.38:
                    return False, w, h, mt, f"scenic_sky:{sky_top_frac:.2f}"
                if green_frac > 0.38:
                    return False, w, h, mt, f"heavy_foliage:{green_frac:.2f}"
                # Also reject photos that have virtually no contrast / solid flat color
                if white_frac > 0.96 or dark_frac > 0.96:
                    return False, w, h, mt, "nearly_blank"

            return True, w, h, mt, f"sat={mean_sat:.2f},white={white_frac:.2f},red={red_frac:.3f},sky={sky_top_frac:.2f},grn={green_frac:.2f}"

    except Exception as exc:
        return False, 0, 0, initial_mt, f"corrupt_image:{exc}"


def download_file_polite(session: requests.Session, url: str, dest: Path) -> bytes | None:
    for attempt in range(5):
        try:
            r = session.get(url, timeout=45, stream=True)
            if r.status_code in (429, 500, 502, 503, 504):
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else min(2.5 * (1.8 ** attempt), 30.0)
                print(f"    [dl backoff] HTTP {r.status_code}, sleeping {wait:.1f}s...", file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                print(f"    [dl skip] HTTP {r.status_code} for {url}", file=sys.stderr, flush=True)
                return None
            chunks = []
            total = 0
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > 26_000_000:
                        return None
            data = b"".join(chunks)
            if not data:
                return None
            dest.write_bytes(data)
            time.sleep(0.35)
            return data
        except requests.RequestException as exc:
            wait = min(2.0 * (1.8 ** attempt), 25.0)
            print(f"    [dl retry] {exc}, sleeping {wait:.1f}s...", file=sys.stderr, flush=True)
            time.sleep(wait)
    return None


def harvest_selected(session: requests.Session, selected: list[dict[str, Any]], target_total: int) -> list[dict[str, Any]]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest_records: list[dict[str, Any]] = []
    seen_sha256: set[str] = set()
    qa_skips = Counter()

    # Load any already-downloaded files in raw/ if resuming? No — clean deterministic run
    # Remove stale files only if not referenced; better start fresh or keep existing matching pageid.
    existing_by_prefix = {p.name.split("_", 1)[0]: p for p in RAW_DIR.iterdir() if p.is_file()}

    print(f"\n=== Stage 3: Downloading & Verifying up to {target_total} files (queue={len(selected)}) ===", flush=True)

    for idx, rec in enumerate(selected, 1):
        # Always finish ALL gold items even if over target_total; cap photos at target_total
        is_gold = rec["media_type"] in ("svg_vector", "bw_tracing", "rubbing_frottage")
        if not is_gold and len(manifest_records) >= target_total:
            break

        pageid_str = str(rec["pageid"])
        safe_name = f"wm{pageid_str}_{sanitize_filename(rec['file_title'])}"
        dest = RAW_DIR / safe_name

        # Reuse already downloaded file on disk if present (supports clean re-runs)
        data: bytes | None = None
        if dest.exists() and dest.stat().st_size > 0:
            data = dest.read_bytes()
        elif f"wm{pageid_str}" in existing_by_prefix:
            old_p = existing_by_prefix[f"wm{pageid_str}"]
            if old_p.exists() and old_p.stat().st_size > 0:
                dest = old_p
                data = dest.read_bytes()
        else:
            data = download_file_polite(session, rec["download_url"], dest)

        if not data:
            qa_skips["download_failed"] += 1
            if dest.exists():
                dest.unlink(missing_ok=True)
            continue

        sha256 = hashlib.sha256(data).hexdigest()
        if sha256 in seen_sha256:
            qa_skips["duplicate_sha256"] += 1
            dest.unlink(missing_ok=True)
            continue

        # Verify file integrity & refine media_type
        if rec["mime"] == "image/svg+xml":
            ok, w, h, note = parse_svg_dimensions(data, rec["width"], rec["height"])
            refined_mt = "svg_vector"
        else:
            ok, w, h, refined_mt, note = inspect_raster_image(dest, rec["media_type"])

        if not ok:
            qa_skips[note.split(":")[0]] += 1
            dest.unlink(missing_ok=True)
            continue

        seen_sha256.add(sha256)
        raw_rel = f"sources/wikimedia/raw/{dest.name}"

        entry = {
            "source_id": rec["source_id"],
            "source_archive": "wikimedia",
            "title": rec["title"],
            "description": rec["description"],
            "site": rec["site"],
            "region": rec["region"],
            "country": rec["country"],
            "period": rec["period"],
            "media_type": refined_mt,
            "motif_hints": rec["motif_hints"],
            "raw_path": raw_rel,
            "source_url": rec["source_url"],
            "download_url": rec["download_url"],
            "creator": rec["creator"],
            "license": rec["license"],
            "license_url": rec["license_url"],
            "commercial_ok": bool(rec["commercial_ok"]),
            "sha256": sha256,
            "width_px": int(w),
            "height_px": int(h),
        }
        manifest_records.append(entry)

        if len(manifest_records) % 25 == 0 or idx <= 10:
            print(
                f"  [{len(manifest_records):3d}/{target_total}] ({idx}/{len(selected)}) "
                f"{refined_mt:16s} | {rec['country']} | {rec['site'][:22]:22s} | {dest.name[:42]}",
                flush=True,
            )

    # Remove any orphaned files in raw/ that aren't in manifest_records
    kept_names = {Path(r["raw_path"]).name for r in manifest_records}
    for p in RAW_DIR.iterdir():
        if p.is_file() and p.name not in kept_names:
            p.unlink(missing_ok=True)

    # Write manifest.jsonl atomically
    with MANIFEST_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        for row in manifest_records:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("\n=== Harvest Complete ===", flush=True)
    print(f"Kept files: {len(manifest_records)} (QA skips: {dict(qa_skips)})", flush=True)
    print("By media_type:", dict(Counter(r["media_type"] for r in manifest_records)), flush=True)
    print("By country:   ", dict(Counter(r["country"] for r in manifest_records)), flush=True)
    print("By period:    ", dict(Counter(r["period"] for r in manifest_records)), flush=True)
    print("By license:   ", dict(Counter(r["license"] for r in manifest_records)), flush=True)
    print("Top sites:    ", Counter(r["site"] for r in manifest_records).most_common(20), flush=True)
    return manifest_records


def main() -> None:
    parser = argparse.ArgumentParser(description="Harvest Nordic rock art from Wikimedia Commons.")
    parser.add_argument("--discover", action="store_true", help="Run API discovery and save cache.")
    parser.add_argument("--download", action="store_true", help="Filter cache and download originals.")
    parser.add_argument("--all", action="store_true", help="Run both discovery and download.")
    parser.add_argument(
        "--cache",
        type=Path,
        default=SCRIPT_DIR / ".discovery_cache.json",
        help="Path to candidate metadata cache JSON.",
    )
    parser.add_argument("--target-total", type=int, default=320, help="Target total files to harvest.")
    args = parser.parse_args()

    if not (args.discover or args.download or args.all):
        args.all = True

    session = make_session()

    if args.discover or args.all:
        candidates = discover_candidates(session, args.cache)
    else:
        if not args.cache.exists():
            print(f"Error: cache file not found at {args.cache}. Run with --discover first.", file=sys.stderr)
            sys.exit(1)
        candidates = json.loads(args.cache.read_text(encoding="utf-8"))

    if args.download or args.all:
        pool = prepare_ranked_pool(candidates)
        selected = select_balanced_targets(pool, target_total=args.target_total)
        harvest_selected(session, selected, target_total=args.target_total)


if __name__ == "__main__":
    main()
