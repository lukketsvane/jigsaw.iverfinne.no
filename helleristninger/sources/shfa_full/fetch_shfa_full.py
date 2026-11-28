#!/usr/bin/env python3
"""Full SHFA (Swedish Rock Art Research Archives) Catalog & Non-Paint Plate Harvester.

1. Harvests the COMPLETE SHFA image/panel metadata catalog via https://shfa.dh.gu.se/api/image/?depth=1
   into helleristninger/sources/shfa_full/catalog_all.jsonl.
2. Selects and downloads a broad, high-diversity set of Swedish non-paint plates
   (bw_tracing, rubbing_frottage, rti_3d) covering every Swedish parish & major site,
   deduplicated by SHA-256 against all existing helleristninger/sources/*/manifest.jsonl,
   quarantining any painted_photo records into quarantine_raw/.
"""

from __future__ import annotations

import glob
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import numpy as np
import requests
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCES_ROOT = SCRIPT_DIR.parent
RAW_DIR = SCRIPT_DIR / "raw"
QUARANTINE_DIR = SCRIPT_DIR / "quarantine_raw"
CATALOG_PATH = SCRIPT_DIR / "catalog_all.jsonl"
MANIFEST_PATH = SCRIPT_DIR / "manifest.jsonl"

SHFA_BASE = "https://shfa.dh.gu.se"
SHFA_LICENSE = "CC BY 4.0"
SHFA_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"

USER_AGENT = (
    "NordicHelleristningerResearchHarvester/2.0 "
    "(open cultural heritage research; jigsaw.iverfinne.no; polite rate-limited)"
)
HTTP_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.8"}

SE_PROVINCE_MAP: Dict[int, str] = {
    1: "Stockholms län",
    3: "Uppsala län (Uppland)",
    4: "Södermanlands län",
    5: "Östergötlands län",
    6: "Jönköpings län",
    7: "Kronobergs län",
    8: "Kalmar län (Småland)",
    9: "Gotlands län",
    10: "Blekinge län",
    12: "Skåne län",
    13: "Hallands län",
    14: "Västra Götalands län (Bohuslän/Dalsland/Västergötland)",
    17: "Värmlands län",
    18: "Örebro län",
    19: "Västmanlands län",
    20: "Dalarnas län",
    21: "Gävleborgs län",
    22: "Västernorrlands län",
    23: "Jämtlands län",
    24: "Västernorrlands län (Ångermanland)",
    25: "Norrbottens län",
}

# SHFA documentation type IDs -> pipeline media_type
SHFA_TYPE_TO_MEDIA: Dict[int, str] = {
    944: "bw_tracing",         # Kalkering (Plast)
    955: "bw_tracing",         # Kalkering (Papper)
    951: "bw_tracing",         # Ritning
    946: "bw_tracing",         # Grafik
    941: "rubbing_frottage",   # Frottage
    943: "rti_3d",             # 3d-visualisering
    959: "rti_3d",             # 3d-laserskanning
    958: "rti_3d",             # 3d-SfM
    957: "rti_3d",             # Ortofoto (SfM)
    956: "rti_3d",             # Foto av SfM modell
    954: "rti_3d",             # DStretch-visualisering
}

SHFA_PHOTO_TYPES: Set[int] = {942, 949, 961, 948, 962, 947, 964, 965}

SHFA_KEYWORD_TO_HINT: Dict[int, str] = {
    5043: "elk_moose", 5038: "deer", 5042: "bear", 5037: "horse", 4992: "bird",
    5072: "snake", 4990: "fish", 5044: "cattle", 5040: "dog_canine", 5039: "wolf",
    5041: "pig", 5102: "goat", 4997: "quadruped_animal", 5113: "animal_procession",
    4991: "bronze_age_ship", 5099: "sun_ship", 5059: "bronze_age_ship",
    5060: "bronze_age_ship", 5061: "bronze_age_ship", 5062: "bronze_age_ship",
    5063: "bronze_age_ship", 5064: "bronze_age_ship", 5071: "iron_age_ship",
    5078: "bronze_age_ship", 4995: "anthropomorph", 5002: "human_female",
    5023: "warrior", 5045: "archer", 5055: "lur_blower", 5052: "acrobat_vaulter",
    5074: "dancer", 5025: "rider", 5053: "adorant", 5058: "bridal_couple",
    5067: "spear_bearer", 5076: "axe_bearer", 5066: "shield_bearer",
    5065: "ship_bearer", 5097: "sword_lifter", 5046: "human_procession",
    5088: "elk_head_staff_bearer", 5003: "chariot_wagon", 4994: "plough",
    5047: "sun_wheel", 5007: "circle", 5049: "concentric_circle", 5056: "cross",
    5048: "spiral", 5050: "double_spiral", 5051: "triple_spiral", 5077: "labyrinth",
    4999: "footprint", 5054: "handprint", 5075: "handprint", 5057: "axe",
    5068: "sword", 5073: "spear", 5069: "shield", 5083: "dagger",
    5108: "bow_arrow", 5112: "halberd", 4996: "weapon", 4993: "tree",
    5109: "lur_horn", 5101: "sun_holder", 5103: "fishing_scene",
    5015: "net_figure", 4989: "frame_figure",
}

HUNTER_REGIONS_SE = {
    "ådals-liden", "nämforsen", "alsen", "krokom", "glösa", "gärde",
    "umeå", "norrfors", "offerberget", "hästskotjärn", "flatruet", "brastad-hunter",
}

PRIORITY_PARISH_QUOTAS: Dict[str, int] = {
    # Bohuslän core
    "Tanum": 80, "Kville": 55, "Svenneby": 40, "Bottna": 40,
    "Tossene": 55, "Brastad": 55, "Askum": 55, "Skee": 55,
    "Bro": 45, "Foss": 45, "Lyse": 35, "Bäve": 30,
    "Hogdal": 30, "Lommeland": 25, "Svarteborg": 25, "Naverstad": 25,
    # Nämforsen
    "Ådals-Liden": 160,
    # Norrköping / Östergötland
    "Östra Eneby": 80, "Borg": 45, "Sankt Johannes": 35, "Kvillinge": 30,
    "Tåby": 25, "Östra Husby": 25, "Kuddby": 25,
    # Uppland / Enköping / Boglösa
    "Boglösa": 60, "Veckholm": 35, "Litslena": 35, "Villberga": 30,
    "Vårfrukyrka": 25, "Bred": 25, "Enköping": 30,
    # Skåne / Simrishamn / Kivik
    "Simris": 45, "Järrestad": 35, "Södra Mellby": 35, "Gladsax": 30,
    "Östra Tommarp": 25, "Valleberga": 25, "Gryt": 25,
    # Blekinge
    "Torhamn": 35, "Hällaryd": 25, "Mörrum": 20, "Jämjö": 20,
    # Småland
    "Törnsfall": 35, "Lofta": 25, "Västervik": 25, "Gamleby": 25,
    "Hallingeberg": 25, "Hjorted": 20,
    # Värmland
    "Millesvik": 25, "Eskilsäter": 25, "Gillberga": 25, "Hammarö": 20,
    # Dalsland
    "Tisselskog": 35, "Skållerud": 25, "Fröskog": 25, "Holm": 20,
    # Gotland & North
    "Alsen": 25, "Umeå": 25,
}

_thread_local = threading.local()


def get_session() -> requests.Session:
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HTTP_HEADERS)
        _thread_local.session = s
    return s


def polite_get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 35,
    delay: float = 0.04,
    retries: int = 4,
) -> Optional[requests.Response]:
    sess = get_session()
    for attempt in range(retries):
        try:
            if delay > 0:
                time.sleep(delay)
            resp = sess.get(url, params=params, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            return resp
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    return None


def load_existing_dedup_sets() -> Tuple[Set[str], Set[str], Set[str]]:
    """Load all existing sha256, source_id, download_url across sibling manifests."""
    shas: Set[str] = set()
    sids: Set[str] = set()
    urls: Set[str] = set()
    for mf in sorted(SOURCES_ROOT.glob("*/manifest.jsonl")):
        if mf.parent == SCRIPT_DIR:
            continue
        with open(mf, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("sha256"):
                    shas.add(str(rec["sha256"]).lower())
                if rec.get("source_id"):
                    sids.add(str(rec["source_id"]))
                if rec.get("download_url"):
                    urls.add(str(rec["download_url"]))
    return shas, sids, urls


def parse_parish_from_raa(raa_id: str) -> str:
    if not raa_id:
        return ""
    m = re.match(r"^\s*([A-ZÅÄÖa-zåäö\- ]+?)\s+\d+", raa_id)
    if m:
        return m.group(1).strip()
    return ""


def parse_scale_info(rec: Dict[str, Any]) -> Optional[str]:
    blob = " ".join(
        str(rec.get(k) or "")
        for k in ("date_note", "reference", "subtype", "group")
    )
    m = re.search(r"(1\s*:\s*\d+|\b\d+(?:[.,]\d+)?\s*(?:cm|mm|m)\b)", blob, flags=re.I)
    return m.group(1).strip() if m else None


def infer_country_and_region(rec: Dict[str, Any], region_by_parish: Dict[str, Dict[str, str]]) -> Tuple[str, str, str, str, str]:
    site_obj = rec.get("site") or {}
    rco = rec.get("rock_carving_object") or {}
    rco_name = (rco.get("name") or "").strip() if isinstance(rco, dict) else ""
    raa_id = (site_obj.get("raa_id") or "").strip()
    ask_id = (site_obj.get("askeladden_id") or "").strip()
    placename = (site_obj.get("placename") or "").strip()
    stednavn = (site_obj.get("stednavn") or "").strip()
    coll_name = ((rec.get("collection") or {}).get("name") or "").strip() if isinstance(rec.get("collection"), dict) else ""
    inst_name = ((rec.get("institution") or {}).get("name") or "").strip() if isinstance(rec.get("institution"), dict) else ""
    coords = (site_obj.get("coordinates") or {}).get("coordinates")

    if ask_id or "bergen" in inst_name.lower() or "vingen" in coll_name.lower() or "ausevik" in coll_name.lower():
        country = "NO"
    elif stednavn or site_obj.get("StedNr") or site_obj.get("FredningsNr") or any(
        k in (placename + " " + rco_name).lower()
        for k in ("bornholm", "själland", "sjælland", "madsebakke", "allinge", "sandvig", "odsherred", "lejre", "grevinge", "hammersholm")
    ):
        country = "DK"
    elif raa_id or site_obj.get("lamning_id") or site_obj.get("ksamsok_id") or site_obj.get("province"):
        country = "SE"
    elif site_obj.get("internationl_site"):
        if coords and len(coords) == 2 and coords[1] >= 54.4 and 4.0 <= coords[0] <= 32.0:
            if coords[0] < 10.9 and coords[1] > 57.8:
                country = "NO"
            elif coords[1] < 56.0 and 8.0 < coords[0] < 15.2:
                country = "DK"
            else:
                country = "SE"
        else:
            country = "NON_NORDIC"
    else:
        country = "SE" if not (coords and len(coords) == 2 and coords[1] < 54.3) else "NON_NORDIC"

    parish = parse_parish_from_raa(raa_id)
    prov_id = site_obj.get("province")
    prov_name = SE_PROVINCE_MAP.get(prov_id, "")
    if not prov_name and parish and parish.lower() in region_by_parish:
        prov_name = region_by_parish[parish.lower()].get("province") or ""

    site_parts = []
    if rco_name:
        site_parts.append(rco_name)
    if raa_id and raa_id.lower() not in rco_name.lower():
        site_parts.append(raa_id)
    elif placename and placename.lower() not in rco_name.lower():
        site_parts.append(placename)
    site_label = " / ".join(site_parts) if site_parts else (raa_id or placename or f"SHFA site {site_obj.get('id')}")

    if country == "SE":
        if prov_name and parish:
            region = f"{prov_name} — {parish}"
        else:
            region = prov_name or parish or "Sweden"
    elif country == "NO":
        region = "Norway"
    elif country == "DK":
        region = "Denmark"
    else:
        region = "International"

    dating_texts = [
        (d.get("english_translation") or d.get("text") or "").lower()
        for d in (rec.get("dating_tags") or []) + (site_obj.get("dating_tags") or [])
        if isinstance(d, dict)
    ]
    low_loc = (region + " " + site_label + " " + coll_name).lower()
    has_ba = any("bronze" in t or "brons" in t for t in dating_texts)
    has_sa = any("stone" in t or "neolith" in t or "mesolith" in t or "sten" in t for t in dating_texts)
    if has_ba and has_sa:
        period = "mixed"
    elif any(k in low_loc for k in HUNTER_REGIONS_SE):
        period = "mixed" if (has_ba and not has_sa) else "stone_age_hunter"
    elif country == "SE":
        period = "stone_age_hunter" if (has_sa and not has_ba) else "bronze_age_agrarian"
    else:
        period = "unknown"

    return country, region, parish, prov_name, site_label, period  # type: ignore[return-value]


def extract_keywords(rec: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str], bool]:
    tags: List[Dict[str, Any]] = []
    hints: List[str] = []
    is_painted = False
    for kw in rec.get("keywords") or []:
        if not isinstance(kw, dict):
            continue
        kid = kw.get("id")
        txt = (kw.get("text") or "").strip()
        en = (kw.get("english_translation") or "").strip()
        tags.append({
            "id": kid,
            "text": txt,
            "english_translation": en,
            "category": kw.get("category_translation") or kw.get("category"),
        })
        low = f"{txt} {en}".lower()
        if "imål" in low or "painted" in low or "uppmål" in low:
            is_painted = True
        if kid in SHFA_KEYWORD_TO_HINT:
            h = SHFA_KEYWORD_TO_HINT[kid]
            if h not in hints:
                hints.append(h)
    return tags, hints, is_painted


def build_catalog_record(rec: Dict[str, Any], region_by_parish: Dict[str, Dict[str, str]]) -> Dict[str, Any]:
    site_obj = rec.get("site") or {}
    rco = rec.get("rock_carving_object") or {}
    type_obj = rec.get("type") or {}
    type_id = type_obj.get("id") if isinstance(type_obj, dict) else None
    type_sv = (type_obj.get("text") or "").strip() if isinstance(type_obj, dict) else ""
    type_en = (type_obj.get("english_translation") or type_sv).strip() if isinstance(type_obj, dict) else ""

    country, region, parish, prov_name, site_label, period = infer_country_and_region(rec, region_by_parish)
    tags, hints, is_painted_kw = extract_keywords(rec)

    if type_id in SHFA_TYPE_TO_MEDIA:
        media_type = SHFA_TYPE_TO_MEDIA[type_id]
        # SfM orthophoto / photo of SfM model tagged 'painted' -> painted_photo
        if type_id in (956, 957) and is_painted_kw:
            media_type = "painted_photo"
    elif is_painted_kw or type_id in SHFA_PHOTO_TYPES:
        media_type = "painted_photo" if is_painted_kw else "photo_other"
    else:
        media_type = "photo_other"

    coords_raw = (site_obj.get("coordinates") or {}).get("coordinates")
    coords_obj = None
    if isinstance(coords_raw, list) and len(coords_raw) == 2 and all(isinstance(v, (int, float)) for v in coords_raw):
        coords_obj = {"lon": float(coords_raw[0]), "lat": float(coords_raw[1]), "crs": "EPSG:4326"}

    author_obj = rec.get("author") or {}
    author_name = (author_obj.get("name") or "").strip() if isinstance(author_obj, dict) else ""
    inst_obj = rec.get("institution") or {}
    inst_name = (inst_obj.get("name") or "").strip() if isinstance(inst_obj, dict) else ""
    coll_obj = rec.get("collection") or {}
    coll_name = (coll_obj.get("name") or "").strip() if isinstance(coll_obj, dict) else ""
    creator = " / ".join(p for p in (author_name, inst_name) if p) or "SHFA (University of Gothenburg)"

    dating_tags = [
        {"id": d.get("id"), "text": d.get("text"), "english_translation": d.get("english_translation")}
        for d in (rec.get("dating_tags") or []) + (site_obj.get("dating_tags") or [])
        if isinstance(d, dict)
    ]

    img_id = rec.get("id")
    return {
        "source_id": f"shfa_{img_id}",
        "source_archive": "shfa",
        "shfa_image_id": img_id,
        "uuid": rec.get("uuid"),
        "shfa_site_id": site_obj.get("id"),
        "rock_carving_object_id": rco.get("id") if isinstance(rco, dict) else None,
        "rock_carving_object_name": rco.get("name") if isinstance(rco, dict) else None,
        "raa_id": site_obj.get("raa_id"),
        "lamning_id": site_obj.get("lamning_id"),
        "ksamsok_id": site_obj.get("ksamsok_id"),
        "askeladden_id": site_obj.get("askeladden_id"),
        "lokalitet_id": site_obj.get("lokalitet_id"),
        "coordinates": coords_obj,
        "parish": parish or None,
        "parish_id": site_obj.get("parish"),
        "municipality_id": site_obj.get("municipality"),
        "province_id": site_obj.get("province"),
        "province": prov_name or None,
        "placename": site_obj.get("placename"),
        "country": country,
        "region": region,
        "site": site_label,
        "period": period,
        "dating_tags": dating_tags,
        "documentation_type_id": type_id,
        "documentation_type_sv": type_sv,
        "documentation_type_en": type_en,
        "media_type": media_type,
        "motif_tags": tags,
        "motif_hints": hints or ["unclassified_motif"],
        "is_painted_tagged": is_painted_kw,
        "creator": creator,
        "author": author_name or None,
        "institution": inst_name or None,
        "collection": coll_name or None,
        "rights_holder": f"{creator} (via SHFA / University of Gothenburg)",
        "year": rec.get("year"),
        "date_note": rec.get("date_note") or None,
        "reference": rec.get("reference"),
        "scale_info": parse_scale_info(rec),
        "width_px": rec.get("width"),
        "height_px": rec.get("height"),
        "license": SHFA_LICENSE,
        "license_url": SHFA_LICENSE_URL,
        "commercial_ok": True,
        "source_url": f"{SHFA_BASE}/image/{img_id}",
        "download_url": rec.get("file"),
        "iiif_file": rec.get("iiif_file"),
    }


def fetch_all_regions() -> Dict[str, Dict[str, str]]:
    r = polite_get(f"{SHFA_BASE}/api/search/region/", params={"limit": 500}, delay=0.05)
    mapping: Dict[str, Dict[str, str]] = {}
    if r and r.ok:
        for it in r.json().get("results") or []:
            p = (it.get("parish") or "").strip()
            if p:
                mapping[p.lower()] = {
                    "parish": p,
                    "municipality": it.get("municipality") or "",
                    "province": it.get("province") or "",
                    "country": it.get("country") or "",
                }
    return mapping


def fetch_full_shfa_catalog(region_by_parish: Dict[str, Dict[str, str]]) -> List[Dict[str, Any]]:
    print("Fetching COMPLETE SHFA image catalog via /api/image/?depth=1&limit=500 ...")
    catalog: List[Dict[str, Any]] = []
    seen_ids: Set[int] = set()
    offset = 0
    total = None
    limit = 500
    while total is None or offset < total:
        r = polite_get(
            f"{SHFA_BASE}/api/image/",
            params={"depth": 1, "limit": limit, "offset": offset},
            delay=0.04,
            timeout=45,
        )
        if not r or not r.ok:
            print(f"  WARN: SHFA catalog offset={offset} failed ({getattr(r, 'status_code', None)})")
            break
        d = r.json()
        if total is None:
            total = d.get("count", 0)
        batch = d.get("results") or []
        if not batch:
            break
        for raw in batch:
            iid = raw.get("id")
            if iid in seen_ids:
                continue
            seen_ids.add(iid)
            catalog.append(build_catalog_record(raw, region_by_parish))
        offset += len(batch)
        if offset % 5000 == 0 or offset >= (total or 0):
            print(f"  Catalog progress: {len(catalog)}/{total}")

    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        for rec in catalog:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"Saved {len(catalog)} SHFA catalog records -> {CATALOG_PATH}")
    return catalog


def detect_painted_plate(raw_bytes: bytes, doc_type_id: Optional[int]) -> Tuple[bool, int, int, str]:
    """Validate image integrity and detect modern painted rock photos (never misflag 3D colormaps)."""
    if len(raw_bytes) < 2048:
        return False, 0, 0, "file_too_small"
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if w < 300 or h < 200:
                return False, w, h, f"too_small_{w}x{h}"
            small = im.convert("RGB")
            small.thumbnail((512, 512))
            arr = np.asarray(small, dtype=np.float32)
    except Exception as exc:
        return False, 0, 0, f"corrupt_image:{exc}"

    # Only check paint on photo-derived SfM/orthophoto/DStretch/field drawings, never 3D hillshade/laser colormaps (943, 959, 958)
    if doc_type_id in (956, 957, 954):
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        # Rock surface background + vivid red paint strokes (typical Swedish imålning)
        red_paint = (r > 115) & ((r - np.maximum(g, b)) > 48)
        rock_bg = (lum > 35) & (lum < 175) & (np.abs(r - b) < 28)
        if float(np.mean(red_paint)) > 0.008 and float(np.mean(rock_bg)) > 0.28:
            return False, w, h, "painted_photo_detected"
    return True, w, h, "ok"


def select_diverse_swedish_plates(
    catalog: List[Dict[str, Any]],
    existing_sids: Set[str],
    existing_urls: Set[str],
    target_count: int = 1800,
) -> List[Dict[str, Any]]:
    """Select a high-diversity set of Swedish non-paint plates across every parish & major site."""
    eligible = [
        rec for rec in catalog
        if rec.get("country") == "SE"
        and rec.get("media_type") in ("bw_tracing", "rubbing_frottage", "rti_3d")
        and not rec.get("is_painted_tagged")
        and rec.get("download_url")
        and rec.get("source_id") not in existing_sids
        and rec.get("download_url") not in existing_urls
        and (rec.get("width_px") or 1000) >= 350
        and (rec.get("height_px") or 1000) >= 250
    ]
    print(f"Eligible new Swedish non-paint SHFA records: {len(eligible)}")

    # Richness score: prioritize figurative motifs, coordinates, diverse doc types, clean authors
    def score_rec(rec: Dict[str, Any]) -> Tuple[int, int, int, int]:
        has_coords = 1 if rec.get("coordinates") else 0
        hints = [h for h in rec.get("motif_hints") or [] if h != "unclassified_motif"]
        n_motifs = len(hints)
        mtype_prio = {"bw_tracing": 3, "rti_3d": 2, "rubbing_frottage": 1}.get(rec["media_type"], 0)
        area = min((rec.get("width_px") or 0) * (rec.get("height_px") or 0), 25_000_000)
        return (has_coords, min(n_motifs, 6), mtype_prio, area)

    by_parish: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for rec in eligible:
        pkey = rec.get("parish") or rec.get("province") or "Unknown_SE"
        by_parish[pkey].append(rec)

    for pkey in by_parish:
        by_parish[pkey].sort(key=score_rec, reverse=True)

    selected: Dict[str, Dict[str, Any]] = {}

    def pick_from_pool(pool: List[Dict[str, Any]], quota: int) -> int:
        # Interleave across media_type and unique raa_id sites so we don't take 20 of the same rock
        by_type: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for rec in pool:
            if rec["source_id"] not in selected:
                by_type[rec["media_type"]].append(rec)
        # Round-robin across (media_type, distinct raa_id)
        seen_site_counts: Counter = Counter()
        ordered: List[Dict[str, Any]] = []
        # Sort within each type favoring unseen raa_ids first
        for mt in by_type:
            by_type[mt].sort(key=lambda r: (seen_site_counts[r.get("raa_id") or r.get("site")], -score_rec(r)[1]))
        idx = 0
        types_cycle = ["bw_tracing", "rti_3d", "rubbing_frottage"]
        added = 0
        while added < quota:
            progressed = False
            for mt in types_cycle:
                lst = by_type.get(mt, [])
                # Find best remaining not yet picked, preferring fewer per raa_id
                best_i = None
                best_key = (10**9, 0)
                for i, r in enumerate(lst):
                    if r["source_id"] in selected:
                        continue
                    rk = (seen_site_counts[r.get("raa_id") or r.get("site")], i)
                    if rk < best_key:
                        best_key = rk
                        best_i = i
                    if rk[0] == 0:
                        break
                if best_i is not None:
                    cand = lst[best_i]
                    selected[cand["source_id"]] = cand
                    seen_site_counts[cand.get("raa_id") or cand.get("site")] += 1
                    added += 1
                    progressed = True
                    if added >= quota:
                        break
            if not progressed:
                break
            idx += 1
        return added

    # Pass 1: Guarantee coverage of EVERY Swedish parish in SHFA (up to 8 per parish)
    for pkey in sorted(by_parish.keys()):
        pick_from_pool(by_parish[pkey], 8)
    print(f"After Pass 1 (all {len(by_parish)} parishes): {len(selected)} selected")

    # Pass 2: Boost priority regions/parishes
    for pkey, quota in PRIORITY_PARISH_QUOTAS.items():
        if pkey in by_parish and len(selected) < target_count:
            already = sum(1 for r in selected.values() if (r.get("parish") or r.get("province")) == pkey)
            need = max(0, quota - already)
            if need > 0:
                pick_from_pool(by_parish[pkey], min(need, target_count - len(selected)))
    print(f"After Pass 2 (priority parishes/sites): {len(selected)} selected")

    # Pass 3: Fill remaining budget evenly across all parishes with remaining plates (prioritizing rti_3d + diverse sites)
    if len(selected) < target_count:
        remaining_parishes = sorted(by_parish.keys(), key=lambda k: len(by_parish[k]), reverse=True)
        while len(selected) < target_count:
            progressed = False
            for pkey in remaining_parishes:
                if len(selected) >= target_count:
                    break
                if pick_from_pool(by_parish[pkey], 2) > 0:
                    progressed = True
            if not progressed:
                break

    print(f"Final SHFA plate selection: {len(selected)} across {len({r.get('parish') or r.get('province') for r in selected.values()})} parishes/provinces")
    return list(selected.values())


def ext_from_response(resp: requests.Response, url: str) -> str:
    ct = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
    if ct == "image/jpeg":
        return ".jpg"
    if ct == "image/png":
        return ".png"
    if ct == "image/webp":
        return ".webp"
    if ct in ("image/tiff", "image/tif"):
        return ".tif"
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp", ".tif") else ".jpg"


def download_plates(
    candidates: List[Dict[str, Any]],
    existing_shas: Set[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    lock = threading.Lock()
    seen_shas = set(existing_shas)
    manifest_recs: List[Dict[str, Any]] = []
    stats: Counter = Counter()

    def worker(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = rec["download_url"]
        resp = polite_get(url, delay=0.03, timeout=45, retries=3)
        if not resp or not resp.ok or not resp.content:
            with lock:
                stats["download_failed"] += 1
            return None

        raw_bytes = resp.content
        sha = hashlib.sha256(raw_bytes).hexdigest()
        with lock:
            if sha in seen_shas:
                stats["sha256_duplicate_skipped"] += 1
                return None
            seen_shas.add(sha)

        ok, w, h, note = detect_painted_plate(raw_bytes, rec.get("documentation_type_id"))
        ext = ext_from_response(resp, url)
        fname = f"{rec['source_id']}_{sha[:8]}{ext}"

        out = dict(rec)
        out["sha256"] = sha
        out["width_px"] = w or rec.get("width_px")
        out["height_px"] = h or rec.get("height_px")

        if note == "painted_photo_detected":
            qpath = QUARANTINE_DIR / fname
            qpath.write_bytes(raw_bytes)
            out["media_type"] = "painted_photo"
            out["raw_path"] = f"sources/shfa_full/quarantine_raw/{fname}"
            out["quarantined"] = True
            out["quarantine_reason"] = "painted_photo: modern paint detected — NEVER trace"
            out["traceable"] = False
            with lock:
                stats["quarantined_painted"] += 1
            return out

        if not ok:
            with lock:
                stats[f"rejected_{note.split(':')[0]}"] += 1
            return None

        rpath = RAW_DIR / fname
        rpath.write_bytes(raw_bytes)
        out["raw_path"] = f"sources/shfa_full/raw/{fname}"
        out["quarantined"] = False
        out["quarantine_reason"] = None
        out["traceable"] = True
        out["title"] = f"{rec['site']} — {rec['documentation_type_en']}" + (f" ({rec['year']})" if rec.get("year") else "")
        out["description"] = (
            f"SHFA {rec['documentation_type_en']} ({rec['documentation_type_sv']}) from {rec['site']} "
            f"({rec['region']}, SE). RAÄ: {rec.get('raa_id') or 'n/a'}, L-nr: {rec.get('lamning_id') or 'n/a'}."
        )
        with lock:
            stats["downloaded_ok"] += 1
            if stats["downloaded_ok"] % 100 == 0:
                print(f"  Downloaded {stats['downloaded_ok']} SHFA plates...")
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        for res in pool.map(worker, candidates):
            if res is not None:
                manifest_recs.append(res)

    manifest_recs.sort(key=lambda r: r["source_id"])
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        for r in manifest_recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    return manifest_recs, dict(stats)


def main() -> None:
    t0 = time.time()
    existing_shas, existing_sids, existing_urls = load_existing_dedup_sets()
    print(f"Loaded existing dedup index: {len(existing_shas)} sha256, {len(existing_sids)} source_ids")

    region_by_parish = fetch_all_regions()
    print(f"Loaded {len(region_by_parish)} SHFA parish/region entries")

    catalog = fetch_full_shfa_catalog(region_by_parish)
    candidates = select_diverse_swedish_plates(catalog, existing_sids, existing_urls, target_count=1800)
    manifest_recs, dl_stats = download_plates(candidates, existing_shas)

    summary = {
        "catalog_total": len(catalog),
        "catalog_sweden": sum(1 for r in catalog if r["country"] == "SE"),
        "catalog_with_coords": sum(1 for r in catalog if r["coordinates"]),
        "manifest_total": len(manifest_recs),
        "manifest_traceable": sum(1 for r in manifest_recs if r.get("traceable")),
        "manifest_quarantined": sum(1 for r in manifest_recs if r.get("quarantined")),
        "by_media_type": dict(Counter(r["media_type"] for r in manifest_recs)),
        "unique_parishes_in_manifest": len({r["parish"] for r in manifest_recs if r.get("parish")}),
        "download_stats": dl_stats,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (SCRIPT_DIR / "shfa_crawl_stats.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print("SHFA crawl complete:", json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
