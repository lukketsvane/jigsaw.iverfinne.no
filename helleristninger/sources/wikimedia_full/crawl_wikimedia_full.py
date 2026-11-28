#!/usr/bin/env python3
"""
Wikimedia Commons Full Nordic & Baltic Rock-Art Harvester.

Crawls all Nordic & Baltic rock-art categories and targeted CirrusSearch queries
via the Wikimedia Commons API (Norway, Sweden, Denmark, Finland, Karelia / Lake
Onega / White Sea / Kanozero), extracting:
  - Full geolocated metadata (EPSG:4326 from Wikimedia coordinates & EXIF GPS)
  - Structured chronology via helleristninger/metadata/chronology.py
  - Unmodified archival originals (SVG vectors, B&W tracings, rubbings/frottage,
    historical plates) with SHA-256 verification
  - Strict quarantine of any painted_photo (red/ochre modern paint)
  - Strict deduplication (by source_id and SHA-256) against all existing manifests
    in helleristninger/sources/*/manifest.jsonl
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import requests
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = 250_000_000

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCES_DIR = SCRIPT_DIR.parent
REPO_ROOT = SOURCES_DIR.parent.parent
METADATA_DIR = REPO_ROOT / "helleristninger" / "metadata"

if str(METADATA_DIR) not in sys.path:
    sys.path.insert(0, str(METADATA_DIR))

from chronology import resolve_chronology  # noqa: E402

RAW_DIR = SCRIPT_DIR / "raw"
QUARANTINE_DIR = SCRIPT_DIR / "quarantine_raw"
MANIFEST_PATH = SCRIPT_DIR / "manifest.jsonl"
CATALOG_PATH = SCRIPT_DIR / "catalog_all.jsonl"
CACHE_PATH = SCRIPT_DIR / "discovery_cache.json"

API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = (
    "HelleristningerCorpusHarvester/2.0 "
    "(https://jigsaw.iverfinne.no; academic-cultural-heritage-research) "
    "python-requests"
)

SEED_CATEGORIES: List[Tuple[str, str, int]] = [
    # Drawings / Tracings
    ("Category:Drawings of petroglyphs in Norway", "NO", 2),
    ("Category:Drawings of petroglyphs in Sweden", "SE", 2),
    ("Category:Drawings of petroglyphs in Finland", "FI", 2),
    ("Category:Drawings of petroglyphs in Russia", "RU", 1),
    ("Category:Drawings of petroglyphs", "NORDIC", 0),
    # Norway
    ("Category:Petroglyphs in Norway", "NO", 2),
    ("Category:Rock art in Norway", "NO", 2),
    ("Category:Rock art of Alta", "NO", 1),
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
    ("Category:Vingen rock art", "NO", 0),
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
    # Sweden
    ("Category:Petroglyphs in Sweden", "SE", 2),
    ("Category:Rock art in Sweden", "SE", 2),
    ("Category:Rock carvings in Tanum", "SE", 1),
    ("Category:Aspeberget, Tanum", "SE", 0),
    ("Category:Rock carvings at Fossum", "SE", 0),
    ("Category:Rock carvings at Kalleby, Tanum", "SE", 0),
    ("Category:Rock carvings at Litsleby", "SE", 0),
    ("Category:Lövåsen rock carvings, Tanum", "SE", 0),
    ("Category:Rock carvings at Gerum", "SE", 0),
    ("Category:Sotetorphällen", "SE", 0),
    ("Category:Rock carvings at Vitlycke", "SE", 0),
    ("Category:Petroglyphs in Tanum Municipality", "SE", 1),
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
    ("Category:Petroglyphs in Västergötland", "SE", 1),
    ("Category:Petroglyphs in Södermanland", "SE", 1),
    ("Category:Petroglyphs in Uppland", "SE", 1),
    ("Category:Petroglyphs in Dalsland", "SE", 1),
    ("Category:Petroglyphs in Blekinge", "SE", 1),
    ("Category:Petroglyphs in Jämtland", "SE", 1),
    ("Category:Hällmålningen i Tumlehed", "SE", 0),
    ("Category:Rock paintings in Sweden", "SE", 1),
    # Denmark
    ("Category:Petroglyphs in Denmark", "DK", 2),
    ("Category:Petroglyphs in Bornholm", "DK", 1),
    ("Category:Madsebakke", "DK", 0),
    ("Category:Helleristninger ved Strandbygård", "DK", 0),
    ("Category:Storeløkkebakken", "DK", 0),
    # Finland
    ("Category:Petroglyphs in Finland", "FI", 2),
    ("Category:Rock paintings in Finland", "FI", 2),
    ("Category:Värikallio rock paintings", "FI", 0),
    ("Category:Verla rock painting", "FI", 0),
    ("Category:Astuvansalmi rock paintings", "FI", 0),
    ("Category:Saraakallio", "FI", 0),
    # Karelia / Onega / White Sea / Kanozero / Baltic
    ("Category:Petroglyphs in Karelia", "RU", 2),
    ("Category:Onega Lake petroglyphs", "RU", 1),
    ("Category:Petroglyphs of Lake Onega", "RU", 1),
    ("Category:Besov Nos", "RU", 0),
    ("Category:White Sea petroglyphs", "RU", 1),
    ("Category:Zalavruga", "RU", 0),
    ("Category:Belomorsk petroglyphs", "RU", 1),
    ("Category:Kanozero petroglyphs", "RU", 1),
    ("Category:Petroglyphs in Murmansk Oblast", "RU", 1),
    ("Category:Petroglyphs in Estonia", "EE", 1),
    ("Category:Petroglyphs in Latvia", "LV", 1),
    # Cross-cutting
    ("Category:Petroglyphs of ships", "NORDIC", 0),
]

SEARCH_QUERIES: List[Tuple[str, str, int]] = [
    ("filetype:drawing hällristning", "NORDIC", 50),
    ("filetype:drawing helleristning", "NORDIC", 50),
    ("filetype:drawing kalliomaalaus OR kalliopiirros", "FI", 40),
    ('filetype:drawing "Nordic Bronze Age"', "NORDIC", 50),
    ("filetype:drawing petroglyph Onega OR Zalavruga OR Karelia OR Kanozero", "RU", 50),
    ("filetype:drawing tanum OR bohuslän OR alta OR kivik OR nämforsen", "NORDIC", 50),
    ("intitle:helleristning", "NO", 50),
    ("intitle:hällristning", "SE", 50),
    ("KMB hällristning teckning", "SE", 50),
    ("Baltzer Bohuslän hällristning", "SE", 50),
    ("Gjessing helleristning", "NO", 50),
    ("Hallström Nämforsen OR Leiknes", "NORDIC", 40),
    ("Onega petroglyph OR Zalavruga OR Kanozero", "RU", 50),
    ("Astuvansalmi OR Värikallio OR Saraakallio", "FI", 50),
    ("Madsebakke OR Bornholm helleristning", "DK", 40),
]

SKIP_SUBCAT_TERMS = (
    "museum", "cup mark", "skålgrop", "runestone", "runesten", "slipränne",
    "heraldry", "world heritage", "visitors", "signs", "maps", "baptismal",
    "gobustan", "cave painting", "dösen", "siberia", "altai", "tomskaya",
    "azerbaijan", "armenia", "kazakhstan", "mongolia", "ural", "shikachi",
)

NON_NORDIC_TERMS = (
    "valcamonica", "gobustan", "tassili", "bhimbetka", "utah", "arizona",
    "new mexico", "california", "nevada", "texas", "australia", "kakadu",
    "lascaux", "altamira", "chauvet", "azerbaijan", "kazakhstan", "tamgaly",
    "siberia", "khakassia", "tuva", "altai", "minusinsk", "amurskaya",
    "sikachi", "tomskaya pisanitsa", "india", "africa", "sahara", "patagonia",
    "cueva", "peru", "chile", "bolivia", "china", "mongolia", "japan",
    "korea", "italy", "spain", "portugal", "coa valley", "france", "scotland",
    "ireland", "newgrange", "knowth", "england", "northumberland", "alpine",
    "mont bego",
)

SITE_RULES: List[Tuple[str, str, str, str, str]] = [
    # Norway
    (r"\b(alta|hjemmeluft|jiepmaluokta|kåfjord|amtmannsnes|bergbukten|ole pedersen|storsteinen)\b", "Alta", "Finnmark", "NO", "stone_age_hunter"),
    (r"\bkvalsund\b", "Kvalsund", "Finnmark", "NO", "stone_age_hunter"),
    (r"\b(skavberg|skavberget)\b", "Skavberget", "Troms", "NO", "stone_age_hunter"),
    (r"\b(rødøy|rødøymannen|flatøya)\b", "Rødøy", "Nordland", "NO", "stone_age_hunter"),
    (r"\bleiknes\b", "Leiknes", "Nordland", "NO", "stone_age_hunter"),
    (r"\balstahaug\b", "Alstahaug", "Nordland", "NO", "bronze_age_agrarian"),
    (r"\b(bøla|bølareinen|bølarein)\b", "Bøla", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bbardal\b", "Bardal", "Trøndelag", "NO", "mixed"),
    (r"\bholtås\b", "Holtås", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bevenhus\b", "Evenhus", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bleirfall\b", "Leirfall", "Trøndelag", "NO", "bronze_age_agrarian"),
    (r"\bhegre\b", "Hegre", "Trøndelag", "NO", "bronze_age_agrarian"),
    (r"\b(hell rock|hell,?\s*stjørdal|hell\s*trøndelag)\b", "Hell", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bstrand\b.*(trøndelag|osen|rock|hellerist)", "Strand", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bstykket\b", "Stykket", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bkvennavika\b", "Kvennavika", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bsolsemhula\b", "Solsemhula", "Trøndelag", "NO", "stone_age_hunter"),
    (r"\bvingen\b", "Vingen", "Vestland", "NO", "stone_age_hunter"),
    (r"\bausevik\b", "Ausevik", "Vestland", "NO", "stone_age_hunter"),
    (r"\b(austre\s*åmøy|åmøy)\b", "Austre Åmøy", "Rogaland", "NO", "bronze_age_agrarian"),
    (r"\b(rudlå|fluberget|revheim)\b", "Stavanger / Rogaland", "Rogaland", "NO", "bronze_age_agrarian"),
    (r"\bpennefeltet|penne\b", "Pennefeltet", "Agder", "NO", "bronze_age_agrarian"),
    (r"\bmøllerstufossen\b", "Møllerstufossen", "Innlandet", "NO", "stone_age_hunter"),
    (r"\bdrotten\b", "Drotten", "Innlandet", "NO", "stone_age_hunter"),
    (r"\bskogerveien\b", "Skogerveien", "Buskerud", "NO", "stone_age_hunter"),
    (r"\b(ekeberg|sjømannsskolen)\b", "Ekeberg", "Oslo", "NO", "stone_age_hunter"),
    (r"\bbegby\b", "Begby", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\bsolberg\b", "Solberg", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\bkalnes\b", "Kalnes", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\bhornes\b", "Hornes", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\b(bjørnstadskipet|bjørnstad)\b", "Bjørnstad", "Østfold", "NO", "bronze_age_agrarian"),
    (r"\b(sarpsborg|fredrikstad|skjeberg|hunn\b|østfold)\b", "Østfold", "Østfold", "NO", "bronze_age_agrarian"),
    # Sweden
    (r"\bvitlycke\b", "Tanum / Vitlycke", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\baspeberget\b", "Tanum / Aspeberget", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bfossum\b", "Tanum / Fossum", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\blitsleby\b", "Tanum / Litsleby", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\b(lövåsen|lovasen)\b", "Tanum / Lövåsen", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bkalleby\b", "Tanum / Kalleby", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bgerum\b", "Tanum / Gerum", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bsotetorp\b", "Tanum / Sotetorp", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bmassleberg\b", "Massleberg", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\b(tanum|bohuslän|brastad|lysekil|sotenäs|munkedal|tossene|kville|svarteborg|askum)\b", "Tanum / Bohuslän", "Bohuslän", "SE", "bronze_age_agrarian"),
    (r"\bnämforsen\b", "Nämforsen", "Ångermanland", "SE", "stone_age_hunter"),
    (r"\b(himmelstalund|norrköping|leonardsberg|ekensberg|fiskeby)\b", "Himmelstalund / Norrköping", "Östergötland", "SE", "bronze_age_agrarian"),
    (r"\b(kivik|kiviksgraven|bredarör)\b", "Kivik", "Skåne", "SE", "bronze_age_agrarian"),
    (r"\b(järrestad|simrishamn|simrislund)\b", "Simrishamn / Järrestad", "Skåne", "SE", "bronze_age_agrarian"),
    (r"\bfrännarp\b", "Frännarp", "Skåne", "SE", "bronze_age_agrarian"),
    (r"\b(brandskog|brandskogsskeppet|enköping|boglösa|hemsta|rickeby)\b", "Enköping / Uppland", "Uppland", "SE", "bronze_age_agrarian"),
    (r"\bslagsta\b", "Slagsta", "Södermanland", "SE", "bronze_age_agrarian"),
    (r"\bflyhov\b", "Flyhov", "Västergötland", "SE", "bronze_age_agrarian"),
    (r"\bhögsbyn\b", "Högsbyn", "Dalsland", "SE", "bronze_age_agrarian"),
    (r"\bhästhallen\b", "Hästhallen", "Blekinge", "SE", "bronze_age_agrarian"),
    (r"\bhägvide\b", "Hägvide", "Gotland", "SE", "bronze_age_agrarian"),
    (r"\bnorrfors\b", "Norrfors", "Västerbotten", "SE", "stone_age_hunter"),
    (r"\btumlehed\b", "Tumlehed", "Bohuslän", "SE", "stone_age_hunter"),
    # Denmark
    (r"\bmadsebakke\b", "Madsebakke", "Bornholm", "DK", "bronze_age_agrarian"),
    (r"\bstrandbygård\b", "Strandbygård", "Bornholm", "DK", "bronze_age_agrarian"),
    (r"\bstoreløkkebakken\b", "Storeløkkebakken", "Bornholm", "DK", "bronze_age_agrarian"),
    (r"\bbornholm\b", "Bornholm", "Bornholm", "DK", "bronze_age_agrarian"),
    # Finland
    (r"\bvärikallio\b", "Värikallio", "Kainuu", "FI", "stone_age_hunter"),
    (r"\bverla\b", "Verla", "Kymenlaakso", "FI", "stone_age_hunter"),
    (r"\bastuvansalmi\b", "Astuvansalmi", "Southern Savonia", "FI", "stone_age_hunter"),
    (r"\bjuusjärvi\b", "Juusjärvi", "Uusimaa", "FI", "stone_age_hunter"),
    (r"\bsaraakallio\b", "Saraakallio", "Central Finland", "FI", "stone_age_hunter"),
    # Karelia / Lake Onega / White Sea / Kanozero
    (r"\b(onega|besov\s*nos|peri\s*nos|karetskiy)\b", "Lake Onega", "Karelia", "RU", "stone_age_hunter"),
    (r"\b(zalavruga|belomorsk|vygriver|vyg\b|white\s*sea)\b", "Zalavruga / White Sea", "Karelia", "RU", "stone_age_hunter"),
    (r"\bkanozero\b", "Kanozero", "Murmansk / Kola", "RU", "stone_age_hunter"),
    (r"\b(karelia|karjala)\b", "Karelia", "Karelia", "RU", "stone_age_hunter"),
]

MOTIF_PATTERNS: List[Tuple[str, str]] = [
    ("reindeer", r"\b(reindeer|reinsdyr|ren\b|renar|bølarein)\b"),
    ("elk_moose", r"\b(elk|moose|elg|älg|alces)\b"),
    ("bear", r"\b(bear|bjørn|björn)\b"),
    ("whale_cetacean", r"\b(whale|hval|val\b|porpoise|beluga|dolphin|seal|säl|sel\b)\b"),
    ("fish", r"\b(fish|fisk|halibut|kveite|salmon|laks)\b"),
    ("bird", r"\b(bird|fugl|fågel|swan|svane|crane|trane|waterfowl)\b"),
    ("horse", r"\b(horse|hest|häst|stallion)\b"),
    ("dog_wolf", r"\b(dog|hund|wolf|ulv|varg)\b"),
    ("snake", r"\b(snake|serpent|slange|orm)\b"),
    ("bronze_age_ship", r"\b(ship|skepps|skepp|skip\b|bjørnstadskip|brandskog)\b"),
    ("stone_age_boat", r"\b(boat|båt|umiak|canoe|skinnbåt)\b"),
    ("skier", r"\b(skier|ski\b|skiløper|skidåkare|rødøymannen)\b"),
    ("hunter_warrior", r"\b(warrior|kriger|krigare|hunter|jeger|archer|spearman|axe_bearer)\b"),
    ("lur_blower", r"\b(lur\b|lurblåser|hornblower)\b"),
    ("acrobat_dancer", r"\b(acrobat|dancer|danser|bridal|adorant|vaulter)\b"),
    ("rider", r"\b(rider|rytter|ryttare|horseman)\b"),
    ("anthropomorph", r"\b(human|anthropomorph|menneske|människa|shaman)\b"),
    ("sun_wheel", r"\b(sun\s*wheel|wheel\s*cross|solhjul|solkors|hjulkors)\b"),
    ("footprint", r"\b(footprint|foot\s*sole|shoe\s*sole|fotsåle|fotsula)\b"),
    ("handprint", r"\b(handprint|hand\s*sign|håndavtrykk)\b"),
    ("chariot_plough", r"\b(chariot|wagon|vogn|vagn|plough|ard|plog)\b"),
    ("axe_weapon", r"\b(axe|øks|yxa|sword|sverd|svärd|spear|weapon)\b"),
    ("tree", r"\b(tree\b|trees|träd)\b"),
    ("geometric_spiral", r"\b(spiral|labyrinth|concentric|circle|ring\b|zigzag)\b"),
]

JUNK_TITLE_RE = re.compile(
    r"\b(sign|skilt|skylt|information\s*board|info\s*board|plaque|poster|brochure|"
    r"museum|exhibition|building|entrance|parking|walkway|boardwalk|stairs|bridge|"
    r"landscape|panorama|coat\s*of\s*arms|herald|flag\b|stamp|coin|logo|runestone|"
    r"runesten|runsten|visitor|tourist|selfie|gravhaug|burial\s*mound|dolmen|"
    r"gravestone|church|banner|icon\b|location\s*map|overview\s*map)\b",
    re.IGNORECASE,
)

DRAWING_SIGNAL_RE = re.compile(
    r"\b(drawing|tracing|kalkering|kalkert|avritning|avtegn|teikning|tegning|teckning|ritning|"
    r"sketch|illustration|plate|plansch|plansje|baltzer|montelius|hallström|hallstrom|gjessing|"
    r"almgren|marstrander|mandt|bakka|petersen|sognnes|nordisk\s*familjebok|fornvännen|"
    r"line\s*art|contour|silhouette|vector|svg|woodcut|engraving|etching|lithograph)\b",
    re.IGNORECASE,
)

RUBBING_SIGNAL_RE = re.compile(
    r"\b(rubbing|frottage|frottasje|avgnidning|gnidbild|gnidning|avtrykk|avtryck)\b",
    re.IGNORECASE,
)

PAINTED_SIGNAL_RE = re.compile(
    r"\b(oppmalt|oppmalte|imålad|ifylld|röd\s*färg|red\s*paint|painted\s*petroglyph|målad|malt)\b",
    re.IGNORECASE,
)


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def sanitize_filename(name: str, max_len: int = 75) -> str:
    name = re.sub(r"^File:", "", name, flags=re.I)
    stem, ext = os.path.splitext(name)
    trans = str.maketrans({
        "å": "a", "Å": "A", "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O",
        "ä": "a", "Ä": "A", "ö": "o", "Ö": "O", "é": "e", "É": "E",
    })
    stem = stem.translate(trans)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "rockart"
    ext = ext.lower()
    if ext not in (".svg", ".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff"):
        ext = ".jpg"
    return f"{stem[:max_len]}{ext}"


def load_existing_dedup_keys() -> Tuple[Set[str], Set[str]]:
    """Collect existing source_ids and sha256 hashes from all prior source manifests."""
    existing_ids: Set[str] = set()
    existing_sha: Set[str] = set()
    skip_dirs = {"wikimedia_full", "nordic_neighbours"}
    for mpath in SOURCES_DIR.glob("*/manifest.jsonl"):
        if mpath.parent.name in skip_dirs:
            continue
        with open(mpath, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("source_id"):
                    existing_ids.add(str(rec["source_id"]))
                if rec.get("sha256"):
                    existing_sha.add(str(rec["sha256"]).lower())
    return existing_ids, existing_sha


def parse_license(extmeta: Dict[str, Any]) -> Tuple[str, str, bool, bool]:
    def val(key: str) -> str:
        v = extmeta.get(key, {})
        if isinstance(v, dict):
            return strip_html(str(v.get("value", ""))).strip()
        return strip_html(str(v or "")).strip()

    if val("NonFree").lower() in ("true", "1", "yes"):
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
    for tok in ("noncommercial", "non-commercial", "-nc", " nc", "noderivs", "no-derivatives", "-nd", " nd", "fair use", "all rights reserved", "unknown"):
        if tok in low:
            return (raw_lic, lic_url, False, False)

    if low in ("public domain", "pd", "pdm") or low.startswith("pd-") or "public domain" in low:
        return ("Public domain" if low == "public domain" else raw_lic, lic_url or "https://creativecommons.org/publicdomain/mark/1.0/", True, True)
    if "cc0" in low or "cc-zero" in low:
        return ("CC0 1.0", lic_url or "https://creativecommons.org/publicdomain/zero/1.0/", True, True)
    if re.search(r"\bcc[\s-]*by[\s-]*sa\b", low) or "attribution-sharealike" in low:
        ver = (re.search(r"(\d\.\d)", raw_lic) or [None, "4.0"])[1]
        return (raw_lic, lic_url or f"https://creativecommons.org/licenses/by-sa/{ver}/", True, True)
    if re.search(r"\bcc[\s-]*by\b", low) or ("attribution" in low and "creative commons" in low):
        ver = (re.search(r"(\d\.\d)", raw_lic) or [None, "4.0"])[1]
        return (raw_lic, lic_url or f"https://creativecommons.org/licenses/by/{ver}/", True, True)
    if "gfdl" in low or "gnu free documentation" in low:
        return (raw_lic, lic_url or "https://www.gnu.org/licenses/fdl-1.3.en.html", True, True)
    if "fal" in low or "free art license" in low:
        return (raw_lic, lic_url or "https://artlibre.org/licence/lal/en/", True, True)
    if copyrighted == "false":
        return (raw_lic, lic_url or "https://creativecommons.org/publicdomain/mark/1.0/", True, True)
    return (raw_lic, lic_url, False, False)


def extract_coordinates(page: Dict[str, Any], extmeta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    coords = page.get("coordinates") or []
    if coords and isinstance(coords, list):
        c0 = coords[0]
        lat, lon = c0.get("lat"), c0.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                return {
                    "crs": "EPSG:4326",
                    "lat": round(float(lat), 6),
                    "lon": round(float(lon), 6),
                    "precision": "wikimedia_page_coordinates",
                    "provenance": "Wikimedia Commons prop=coordinates API",
                }
    lat_raw = (extmeta.get("GPSLatitude") or {}).get("value")
    lon_raw = (extmeta.get("GPSLongitude") or {}).get("value")
    try:
        if lat_raw is not None and lon_raw is not None:
            lat_f = float(strip_html(str(lat_raw)))
            lon_f = float(strip_html(str(lon_raw)))
            if -90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0 and (abs(lat_f) > 0.1 or abs(lon_f) > 0.1):
                return {
                    "crs": "EPSG:4326",
                    "lat": round(lat_f, 6),
                    "lon": round(lon_f, 6),
                    "precision": "exif_gps",
                    "provenance": "Wikimedia Commons EXIF GPS extmetadata",
                }
    except (TypeError, ValueError):
        pass
    return None


def api_get(session: requests.Session, params: Dict[str, Any], delay: float = 0.12, use_post: bool = False) -> Dict[str, Any]:
    req_params = dict(params, format="json", formatversion=2)
    for attempt in range(5):
        try:
            if use_post:
                r = session.post(API_URL, data=req_params, timeout=30)
            else:
                r = session.get(API_URL, params=req_params, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(1.5 * (1.6 ** attempt), 15.0))
                continue
            r.raise_for_status()
            data = r.json()
            if "error" in data and data["error"].get("code") in ("maxlag", "ratelimited"):
                time.sleep(min(2.0 * (attempt + 1), 15.0))
                continue
            time.sleep(delay)
            return data
        except requests.RequestException:
            time.sleep(min(1.5 * (1.6 ** attempt), 15.0))
    raise RuntimeError(f"Wikimedia API failed: {params.get('action')}")


def infer_site_region_country_period(
    title: str, description: str, categories: List[str], country_hints: List[str]
) -> Tuple[str, str, str, str, bool]:
    combined = f"{title} | {description} | {' | '.join(categories)}".lower()
    for bad in NON_NORDIC_TERMS:
        if re.search(rf"\b{re.escape(bad)}\b", combined):
            return ("", "", "", "unknown", False)
    for pat, site, region, country, period in SITE_RULES:
        if re.search(pat, combined, flags=re.IGNORECASE):
            return (site, region, country, period, True)
    if re.search(r"\b(norway|norge|norwegian|helleristning|finnmark|trøndelag|østfold|rogaland|nordland|troms|vestland)\b", combined):
        return ("Norway", "Norway", "NO", "unknown", True)
    if re.search(r"\b(sweden|sverige|swedish|hällristning|bohuslän|skåne|östergötland|uppland|södermanland|småland)\b", combined):
        return ("Sweden", "Sweden", "SE", "bronze_age_agrarian", True)
    if re.search(r"\b(denmark|danmark|danish|bornholm)\b", combined):
        return ("Bornholm" if "bornholm" in combined else "Denmark", "Bornholm" if "bornholm" in combined else "Denmark", "DK", "bronze_age_agrarian", True)
    if re.search(r"\b(finland|suomi|finnish|kalliomaalaus)\b", combined):
        return ("Finland", "Finland", "FI", "stone_age_hunter", True)
    if re.search(r"\b(karelia|karjala|onega|zalavruga|kanozero|white\s*sea|belomorsk)\b", combined):
        return ("Karelia", "Karelia", "RU", "stone_age_hunter", True)
    if re.search(r"\b(estonia|latvia|baltic)\b", combined):
        return ("Baltic", "Baltic", "EE" if "estonia" in combined else "LV", "unknown", True)
    for ch in country_hints:
        if ch in ("NO", "SE", "DK", "FI", "RU", "EE", "LV"):
            p = "bronze_age_agrarian" if ch in ("SE", "DK") else "stone_age_hunter"
            return (ch, ch, ch, p, True)
    return ("", "", "", "unknown", False)


def extract_motif_hints(title: str, description: str, categories: List[str]) -> List[str]:
    text = f"{title} | {description} | {' '.join(categories)}".lower()
    hints = [m for m, pat in MOTIF_PATTERNS if re.search(pat, text, flags=re.IGNORECASE)]
    if len(hints) >= 2 and "multi_motif_panel" not in hints:
        hints.append("multi_motif_panel")
    return hints


def initial_media_type(mime: str, title: str, description: str, categories: List[str]) -> str:
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


def is_junk_candidate(title: str, description: str, categories: List[str], mime: str) -> Tuple[bool, str]:
    clean_title = re.sub(r"^File:", "", title, flags=re.I)
    cat_joined = " | ".join(categories)
    if mime not in ("image/svg+xml", "image/png", "image/jpeg", "image/gif", "image/tiff"):
        return True, f"unsupported_mime:{mime}"
    if any(b in cat_joined.lower() for b in ("category:maps", "location maps", "coat of arms", "heraldry", "runestones", "flags of", "logos")):
        return True, "bad_category"
    if JUNK_TITLE_RE.search(clean_title):
        if not (re.search(r"(alta|vitlycke)\s*museum", clean_title, re.I) and re.search(r"(petroglyph|carving|reindeer|elk|ship|boat|figure)", f"{clean_title} {description}", re.I)):
            return True, "junk_title"
    rockart_signal = re.search(
        r"(petroglyph|helleristning|hällristning|kalliomaalaus|kalliopiirros|rock\s*art|rock\s*carving|rock\s*painting|"
        r"bergkunst|hällmålning|veideristning|jordbruksristning|bølarein|bjørnstadskip|brandskog|kivik|"
        r"himmelstalund|nämforsen|madsebakke|vitlycke|aspeberget|bardal|evenhus|leirfall|vingen|onega|zalavruga|kanozero|astuvansalmi|värikallio)",
        f"{clean_title} | {description} | {cat_joined}",
        re.IGNORECASE,
    )
    if not rockart_signal:
        return True, "no_rockart_signal"
    return False, ""


def parse_svg_dimensions(svg_bytes: bytes, fallback_w: int, fallback_h: int) -> Tuple[bool, int, int, str]:
    try:
        root = ET.fromstring(svg_bytes)
    except ET.ParseError as exc:
        return False, 0, 0, f"svg_parse_error:{exc}"
    if not root.tag.lower().endswith("svg"):
        return False, 0, 0, "svg_root_not_svg"
    drawable_tags = {"path", "polygon", "polyline", "circle", "ellipse", "rect", "line"}
    if not any(e.tag.split("}")[-1].lower() in drawable_tags for e in root.iter()):
        return False, 0, 0, "svg_no_drawable"
    w, h = 0.0, 0.0
    vb = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if vb:
        nums = re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", vb)
        if len(nums) == 4:
            w, h = float(nums[2]), float(nums[3])
    if w <= 0 or h <= 0:
        w = float(fallback_w or 512)
        h = float(fallback_h or 512)
    return True, max(1, int(round(w))), max(1, int(round(h))), "ok"


def inspect_raster_bytes(raw_bytes: bytes, initial_mt: str) -> Tuple[bool, int, int, str, str]:
    import io
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if w < 180 or h < 180:
                return False, w, h, initial_mt, "too_small"
            small = im.convert("RGB")
            small.thumbnail((256, 256))
            pixels = list(small.getdata())
            sw, sh = small.size
    except Exception as exc:
        return False, 0, 0, initial_mt, f"corrupt:{exc}"

    n = len(pixels)
    if n == 0:
        return False, w, h, initial_mt, "empty"

    sat_sum = 0.0
    white_cnt = dark_cnt = red_paint_cnt = sky_top_cnt = green_cnt = light_cnt = 0
    top_n = max(1, sh // 4) * sw

    for idx, (r, g, b) in enumerate(pixels):
        mx, mn = max(r, g, b), min(r, g, b)
        diff = mx - mn
        sat = (diff / mx) if mx > 0 else 0.0
        sat_sum += sat
        if r > 215 and g > 215 and b > 215 and diff < 28:
            white_cnt += 1
        elif r < 75 and g < 75 and b < 75 and diff < 30:
            dark_cnt += 1
        if r > 170 and g > 170 and b > 170:
            light_cnt += 1
        if r >= 95 and (r - g) >= 35 and (r - b) >= 35 and sat >= 0.30:
            red_paint_cnt += 1
        if idx < top_n and b >= 145 and (b - r) >= 25 and mx >= 150:
            sky_top_cnt += 1
        if g >= 75 and (g - r) >= 18 and (g - b) >= 15 and sat >= 0.22:
            green_cnt += 1

    mean_sat = sat_sum / n
    white_frac = white_cnt / n
    dark_frac = dark_cnt / n
    light_frac = light_cnt / n
    red_frac = red_paint_cnt / n
    sky_top_frac = sky_top_cnt / max(1, top_n)
    green_frac = green_cnt / n

    mt = initial_mt
    if mt != "rubbing_frottage":
        if mean_sat < 0.09 and white_frac > 0.45 and dark_frac > 0.008:
            mt = "bw_tracing"
        elif mt == "bw_tracing":
            if light_frac < 0.35 or (mean_sat > 0.18 and white_frac < 0.25 and light_frac < 0.55):
                mt = "painted_photo" if red_frac >= 0.012 else "photo_other"

    if mt in ("painted_photo", "photo_other"):
        mt = "painted_photo" if red_frac >= 0.012 else "photo_other"
        if sky_top_frac > 0.42 or green_frac > 0.42:
            return False, w, h, mt, "scenic_landscape"
        if white_frac > 0.96 or dark_frac > 0.96:
            return False, w, h, mt, "nearly_blank"

    return True, w, h, mt, "ok"


def discover_all(session: requests.Session) -> List[Dict[str, Any]]:
    file_sources: Dict[str, Dict[str, Any]] = {}

    def record_file(title: str, src: str, chint: str) -> None:
        if not title.startswith("File:"):
            return
        entry = file_sources.setdefault(title, {"sources": set(), "country_hints": set()})
        entry["sources"].add(src)
        if chint:
            entry["country_hints"].add(chint)

    visited_cats: Set[str] = set()
    queue: deque[Tuple[str, str, int]] = deque()
    for cat, chint, max_d in SEED_CATEGORIES:
        if cat not in visited_cats:
            visited_cats.add(cat)
            queue.append((cat, chint, max_d))

    crawled = 0
    while queue:
        cat, chint, rem_d = queue.popleft()
        crawled += 1
        cmcont = None
        while True:
            params: Dict[str, Any] = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": cat,
                "cmtype": "file|subcat" if rem_d > 0 else "file",
                "cmlimit": 500,
            }
            if cmcont:
                params["cmcontinue"] = cmcont
            try:
                data = api_get(session, params, delay=0.10)
            except Exception:
                break
            for m in data.get("query", {}).get("categorymembers", []):
                ns, mtitle = m.get("ns"), m.get("title", "")
                if ns == 6:
                    record_file(mtitle, cat, chint)
                elif ns == 14 and rem_d > 0:
                    low = mtitle.lower()
                    if any(s in low for s in SKIP_SUBCAT_TERMS):
                        continue
                    if mtitle not in visited_cats:
                        visited_cats.add(mtitle)
                        queue.append((mtitle, chint, rem_d - 1))
            cmcont = data.get("continue", {}).get("cmcontinue")
            if not cmcont:
                break
        if crawled % 25 == 0:
            print(f"  Categories crawled: {crawled} | unique files: {len(file_sources)}", flush=True)

    for q, chint, limit in SEARCH_QUERIES:
        time.sleep(0.5)
        try:
            data = api_get(session, {
                "action": "query", "list": "search", "srnamespace": 6, "srsearch": q, "srlimit": min(limit, 50)
            }, delay=0.25)
            for h in data.get("query", {}).get("search", []):
                record_file(h["title"], f"search:{q}", chint)
        except Exception:
            pass

    all_titles = sorted(file_sources.keys())
    print(f"Discovered {len(all_titles)} unique Wikimedia Commons files across {crawled} categories.", flush=True)

    candidates: List[Dict[str, Any]] = []
    batch_size = 25
    for i in range(0, len(all_titles), batch_size):
        batch = all_titles[i:i + batch_size]
        try:
            data = api_get(session, {
                "action": "query",
                "titles": "|".join(batch),
                "prop": "imageinfo|categories|coordinates",
                "iiprop": "url|size|mime|sha1|extmetadata|mediatype|user",
                "cllimit": "max",
                "clshow": "!hidden",
            }, delay=0.08, use_post=True)
        except Exception as exc:
            print(f"  WARN batch {i}: {exc}", flush=True)
            continue
        for p in data.get("query", {}).get("pages", []):
            if p.get("missing") or not p.get("imageinfo"):
                continue
            ii = p["imageinfo"][0]
            title = p["title"]
            sinfo = file_sources.get(title, {"sources": set(), "country_hints": set()})
            candidates.append({
                "pageid": p.get("pageid"),
                "title": title,
                "mime": ii.get("mime", ""),
                "size": ii.get("size", 0),
                "width": ii.get("width", 0),
                "height": ii.get("height", 0),
                "sha1": ii.get("sha1", ""),
                "url": ii.get("url", ""),
                "descriptionurl": ii.get("descriptionurl", ""),
                "user": ii.get("user", ""),
                "extmetadata": ii.get("extmetadata", {}),
                "categories": [c["title"] for c in p.get("categories", [])],
                "coordinates_raw": p.get("coordinates") or [],
                "country_hints": sorted(sinfo["country_hints"]),
            })
        if (i // batch_size + 1) % 20 == 0 or i + batch_size >= len(all_titles):
            print(f"  Metadata fetched: {min(i + batch_size, len(all_titles))}/{len(all_titles)} ({len(candidates)} valid)", flush=True)
    return candidates


def download_bytes(session: requests.Session, url: str) -> Optional[bytes]:
    for attempt in range(4):
        try:
            r = session.get(url, timeout=45, stream=True)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2.0 * (attempt + 1))
                continue
            if r.status_code != 200:
                return None
            chunks = []
            total = 0
            for ch in r.iter_content(65536):
                if ch:
                    chunks.append(ch)
                    total += len(ch)
                    if total > 26_000_000:
                        return None
            time.sleep(0.25)
            return b"".join(chunks)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    return None


def main() -> int:
    t0 = time.time()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    existing_ids, existing_sha = load_existing_dedup_keys()
    print(f"Loaded dedup baseline: {len(existing_ids)} source_ids, {len(existing_sha)} sha256 hashes", flush=True)

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    if "--use-cache" in sys.argv and CACHE_PATH.exists():
        raw_cands = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    else:
        raw_cands = discover_all(session)
        CACHE_PATH.write_text(json.dumps(raw_cands, ensure_ascii=False), encoding="utf-8")

    # Build full geolocated catalog and select new non-paint tracings/SVGs/plates to download
    catalog_records: List[Dict[str, Any]] = []
    download_queue: List[Dict[str, Any]] = []
    seen_sha1: Set[str] = set()
    stats = Counter()

    for c in raw_cands:
        title = c["title"]
        ext = c.get("extmetadata", {})
        desc = strip_html(str(ext.get("ImageDescription", {}).get("value", "")))[:600]
        artist = strip_html(str(ext.get("Artist", {}).get("value", "")))[:200] or c.get("user", "Unknown")
        cats = c.get("categories", [])
        mime = c.get("mime", "")

        junk, reason = is_junk_candidate(title, desc, cats, mime)
        if junk:
            stats[f"drop_{reason}"] += 1
            continue

        site, region, country, period, is_nordic = infer_site_region_country_period(title, desc, cats, c.get("country_hints", []))
        if not is_nordic:
            stats["drop_non_nordic"] += 1
            continue

        lic_name, lic_url, commercial_ok, keep = parse_license(ext)
        if not keep or not commercial_ok:
            stats["drop_license"] += 1
            continue

        size = c.get("size", 0) or 0
        if size <= 0 or size > 25_000_000:
            stats["drop_size"] += 1
            continue

        sha1 = c.get("sha1", "")
        if sha1 and sha1 in seen_sha1:
            stats["drop_dup_sha1"] += 1
            continue
        if sha1:
            seen_sha1.add(sha1)

        sid = f"wikimedia_{c['pageid']}"
        mt = initial_media_type(mime, title, desc, cats)
        coords = extract_coordinates({"coordinates": c.get("coordinates_raw")}, ext)
        hints = extract_motif_hints(title, desc, cats)
        clean_title = os.path.splitext(re.sub(r"^File:", "", title, flags=re.I))[0].replace("_", " ").strip()

        chron = resolve_chronology(
            site=site, region=region, country=country, period=period,
            title=clean_title, description=desc, motif_hints=hints
        )

        cat_rec = {
            "source_id": sid,
            "source_archive": "wikimedia",
            "title": clean_title,
            "description": desc,
            "site": site,
            "region": region,
            "country": country,
            "period": period,
            "media_type": mt,
            "motif_hints": hints,
            "coordinates": coords,
            "source_url": c.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{quote(title.replace(' ', '_'))}",
            "download_url": c.get("url", ""),
            "creator": artist,
            "license": lic_name,
            "license_url": lic_url,
            "commercial_ok": commercial_ok,
            "width_px": int(c.get("width") or 0),
            "height_px": int(c.get("height") or 0),
            "already_in_baseline": sid in existing_ids,
            **chron,
        }
        catalog_records.append(cat_rec)

        if sid in existing_ids:
            stats["dedup_existing_source_id"] += 1
            continue

        # Download ALL non-paint traceable plates (svg_vector, bw_tracing, rubbing_frottage)
        # plus up to 120 geotagged clean plates (and inspect pixels to quarantine painted_photo)
        is_traceable_type = mt in ("svg_vector", "bw_tracing", "rubbing_frottage")
        if is_traceable_type or (mt == "photo_other" and coords is not None) or mt == "painted_photo":
            cat_rec["_priority"] = (
                100 if is_traceable_type else (50 if (mt == "photo_other" and coords is not None) else 10)
            )
            cat_rec["_orig_title"] = title
            cat_rec["_mime"] = mime
            download_queue.append(cat_rec)

    with CATALOG_PATH.open("w", encoding="utf-8") as fh:
        for r in catalog_records:
            clean = {k: v for k, v in r.items() if not k.startswith("_")}
            fh.write(json.dumps(clean, ensure_ascii=False) + "\n")

    # Prioritize traceable non-paint plates first, cap non-traceable photos at 120 to stay lean
    traceable_q = [r for r in download_queue if r["media_type"] in ("svg_vector", "bw_tracing", "rubbing_frottage")]
    photo_q = [r for r in download_queue if r["media_type"] not in ("svg_vector", "bw_tracing", "rubbing_frottage")]
    photo_q.sort(key=lambda r: (0 if r["coordinates"] else 1, 0 if r["media_type"] == "photo_other" else 1))
    final_q = traceable_q + photo_q[:120]

    print(f"Catalog records: {len(catalog_records)} | Download queue: {len(final_q)} (traceable={len(traceable_q)})", flush=True)

    manifest_records: List[Dict[str, Any]] = []
    for idx, rec in enumerate(final_q, 1):
        fname = f"wm{rec['source_id'].split('_', 1)[1]}_{sanitize_filename(rec['_orig_title'])}"
        # Check if already downloaded on disk
        existing_file = None
        for folder in (RAW_DIR, QUARANTINE_DIR):
            cand_p = folder / fname
            if cand_p.exists() and cand_p.stat().st_size > 0:
                existing_file = cand_p
                break

        if existing_file is not None:
            raw_bytes = existing_file.read_bytes()
        else:
            raw_bytes = download_bytes(session, rec["download_url"])
            if not raw_bytes:
                stats["dl_failed"] += 1
                continue

        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        if sha256 in existing_sha:
            if existing_file is not None:
                existing_file.unlink(missing_ok=True)
            stats["dedup_existing_sha256"] += 1
            continue

        if rec["_mime"] == "image/svg+xml":
            ok, w, h, note = parse_svg_dimensions(raw_bytes, rec["width_px"], rec["height_px"])
            refined_mt = "svg_vector"
        else:
            ok, w, h, refined_mt, note = inspect_raster_bytes(raw_bytes, rec["media_type"])

        if not ok:
            stats[f"qa_reject_{note}"] += 1
            continue

        quarantined = refined_mt == "painted_photo"
        target_dir = QUARANTINE_DIR if quarantined else RAW_DIR
        target_path = target_dir / fname
        target_path.write_bytes(raw_bytes)

        existing_sha.add(sha256)
        rel_path = f"sources/wikimedia_full/{'quarantine_raw' if quarantined else 'raw'}/{fname}"
        out_rec = {k: v for k, v in rec.items() if not k.startswith("_") and k != "already_in_baseline"}
        out_rec.update({
            "media_type": refined_mt,
            "raw_path": rel_path,
            "sha256": sha256,
            "width_px": int(w),
            "height_px": int(h),
            "rights_holder": rec["creator"],
            "audit_sha256_verified": True,
            "quarantined": quarantined,
            "quarantine_reason": "painted_photo: modern paint — NEVER trace" if quarantined else None,
            "traceable": refined_mt in ("svg_vector", "bw_tracing", "rubbing_frottage", "rti_3d"),
        })
        manifest_records.append(out_rec)
        if idx % 25 == 0:
            print(f"  Downloaded {idx}/{len(final_q)} | kept={len(manifest_records)} ({time.time()-t0:.1f}s)", flush=True)

    manifest_records.sort(key=lambda r: (r["country"], r["media_type"], r["source_id"]))
    tmp_m = SCRIPT_DIR / "manifest.jsonl.tmp"
    with tmp_m.open("w", encoding="utf-8") as fh:
        for r in manifest_records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp_m, MANIFEST_PATH)

    print(
        f"DONE Wikimedia Full in {time.time()-t0:.1f}s: catalog={len(catalog_records)}, "
        f"new_manifest={len(manifest_records)}, by_country={dict(Counter(r['country'] for r in manifest_records))}, "
        f"by_media={dict(Counter(r['media_type'] for r in manifest_records))}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
