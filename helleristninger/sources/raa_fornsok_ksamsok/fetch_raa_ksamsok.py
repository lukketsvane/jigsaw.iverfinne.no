#!/usr/bin/env python3
"""RAÄ / Fornsök / K-samsök (SOCH) Harvester for Sweden.

1. Harvests ALL open Hällristning & Hällmålning monument records from RAÄ Fornsök
   (serviceOrganization="raä") via kulturarvsdata.se/ksamsok/api (recordSchema=rdf)
   into helleristninger/sources/raa_fornsok_ksamsok/sites_sweden.jsonl with:
   - L-nummer (Lämningsnummer), RAÄ-nummer, entity URI, Fornsök URL
   - Exact EPSG:4326 lat/lon coordinates
   - Parish (socken), municipality, province, county
   - Dating / antiquarian status / full description
   - Linked media / related URIs
2. Harvests open non-paint rock-art plates (CC0 / PDM / CC BY / CC BY-SA) from
   non-DiMu, non-SHFA Swedish K-samsök heritage archives into
   helleristninger/sources/raa_fornsok_ksamsok/manifest.jsonl + raw/
   (deduplicated by SHA-256 against existing manifests; quarantining painted_photo).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
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
SITES_PATH = SCRIPT_DIR / "sites_sweden.jsonl"
MANIFEST_PATH = SCRIPT_DIR / "manifest.jsonl"

KSAMSOK_API = "https://kulturarvsdata.se/ksamsok/api"
USER_AGENT = (
    "NordicHelleristningerResearchHarvester/2.0 "
    "(open cultural heritage research; jigsaw.iverfinne.no; polite rate-limited)"
)
HTTP_HEADERS = {"User-Agent": USER_AGENT}

NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "ksam": "http://kulturarvsdata.se/ksamsok#",
    "pres": "http://kulturarvsdata.se/presentation#",
    "georss": "http://www.georss.org/georss",
    "gml": "http://www.opengis.net/gml",
    "owl": "http://www.w3.org/2002/07/owl#",
}

HUNTER_TOKENS = (
    "ådals-liden", "nämforsen", "alsen", "krokom", "glösa", "gärde",
    "norrfors", "hällmålning", "älghuvud", "älg", "ren", "björn", "skid",
    "stenålder", "fångst", "jägar",
)

BAD_OBJECT_TOKENS = (
    "tröja", "t-shirt", "frimärke", "souvenir", "skylt", "parkering", "tavelgalleri",
    "utställning", "låda m utrustn", "vykort", "bok", "tidning", "etnografiska",
    "världskultur", "bogota", "afrika", "nordamerika", "sydamerika", "indien",
)

DRAWING_TOKENS = (
    "avritning", "kalkering", "teckning", "ritning", "kopparstick", "grafik",
    "frottage", "gnidbild", "akvarell", "lavering", "skiss", "plansch", "uppmätning",
)

PAINT_TOKENS = (
    "imål", "ifylld", "uppmål", "målade", "målad", "röd färg", "kritad", "kritning",
)

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
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 40,
    delay: float = 0.08,
    retries: int = 4,
) -> Optional[requests.Response]:
    sess = get_session()
    for attempt in range(retries):
        try:
            if delay > 0:
                time.sleep(delay)
            resp = sess.get(url, params=params, headers=headers, timeout=timeout, allow_redirects=True)
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


def _txt(el: Optional[ET.Element]) -> str:
    return (el.text or "").strip() if el is not None else ""


def parse_rdf_record(rec_el: ET.Element) -> Optional[Dict[str, Any]]:
    ent = rec_el.find(".//ksam:Entity", NS)
    if ent is None:
        return None
    entity_uri = ent.attrib.get(f"{{{NS['rdf']}}}about", "")

    lamning_id = None
    raa_id = None
    other_numbers: Dict[str, str] = {}
    for inum in ent.findall(".//ksam:ItemNumber", NS):
        t = _txt(inum.find("ksam:type", NS))
        n = _txt(inum.find("ksam:number", NS))
        if not n:
            continue
        if t.lower() == "lämningsnummer":
            lamning_id = n
        elif t.lower() == "raä-nummer":
            raa_id = n
        elif t:
            other_numbers[t] = n
    if not lamning_id:
        id_lbl = _txt(ent.find(".//pres:idLabel", NS))
        if id_lbl.startswith("L"):
            lamning_id = id_lbl

    # Coordinates (EPSG:4326 lon,lat in gml:coordinates)
    coords = None
    for gcoord in ent.findall(".//gml:coordinates", NS):
        raw_c = _txt(gcoord)
        if "," in raw_c:
            parts = raw_c.split(",")[0:2]
            try:
                lon = float(parts[0].strip())
                lat = float(parts[1].strip().split()[0])
                if -180 <= lon <= 180 and -90 <= lat <= 90:
                    coords = {"lon": lon, "lat": lat, "crs": "EPSG:4326"}
                    break
            except ValueError:
                pass

    parish = _txt(ent.find(".//ksam:parishName", NS)) or None
    municipality = _txt(ent.find(".//ksam:municipalityName", NS)) or None
    province = _txt(ent.find(".//ksam:provinceName", NS)) or None
    county = _txt(ent.find(".//ksam:countyName", NS)) or None
    place_label = _txt(ent.find(".//pres:placeLabel", NS)) or None

    specs: Dict[str, str] = {}
    for sp in ent.findall(".//ksam:ItemSpecification", NS):
        k = _txt(sp.find("ksam:type", NS))
        v = _txt(sp.find("ksam:spec", NS))
        if k and v:
            specs[k] = v

    descs: Dict[str, str] = {}
    for dc_el in ent.findall(".//ksam:ItemDescription", NS):
        k = _txt(dc_el.find("ksam:type", NS))
        v = _txt(dc_el.find("ksam:desc", NS))
        if k and v:
            descs[k] = v

    main_desc = descs.get("Beskrivning") or _txt(ent.find(".//pres:description", NS)) or ""
    item_class = _txt(ent.find("ksam:itemClassName", NS)) or _txt(ent.find("ksam:itemLabel", NS)) or "Hällristning"
    fornsok_url = _txt(ent.find("ksam:url", NS)) or _txt(ent.find(".//pres:representation[@format='HTML']", NS)) or entity_uri

    # Dating extraction from ksam:timeLabel / specs / description
    time_labels = [_txt(t) for t in ent.findall(".//ksam:timeLabel", NS) if _txt(t)]
    time_labels.extend([_txt(t) for t in ent.findall(".//pres:timeLabel", NS) if _txt(t)])
    dating_raw = specs.get("Datering") or ("; ".join(dict.fromkeys(time_labels)) if time_labels else None)
    if not dating_raw:
        m_dat = re.search(r"\b(stenålder|mesolitikum|neolitikum|bronsålder|järnålder|vikingatid|medeltid)\b", main_desc, re.I)
        if m_dat:
            dating_raw = m_dat.group(1)

    low_all = f"{item_class} {main_desc} {province or ''} {parish or ''} {dating_raw or ''}".lower()
    if "hällmålning" in item_class.lower() or any(t in low_all for t in HUNTER_TOKENS):
        period = "stone_age_hunter"
    else:
        period = "bronze_age_agrarian"

    # Linked media & related URIs
    media_urls: List[str] = []
    for tag in ("ksam:mediaUrl", "ksam:thumbnail", "ksam:highres", "ksam:lowres", "pres:mediaUrl"):
        for el in ent.findall(f".//{tag}", NS):
            u = _txt(el)
            if u.startswith("http") and u not in media_urls:
                media_urls.append(u)
    for src_el in ent.findall(".//pres:src", NS):
        u = _txt(src_el)
        if u.startswith("http") and u not in media_urls:
            media_urls.append(u)

    replaces = [
        el.attrib.get(f"{{{NS['rdf']}}}resource")
        for el in ent.findall("dcterms:replaces", NS)
        if el.attrib.get(f"{{{NS['rdf']}}}resource")
    ]
    related = [
        el.attrib.get(f"{{{NS['rdf']}}}resource")
        for el in ent.findall(".//ksam:isRelatedTo", NS) + ent.findall(".//rdfs:seeAlso", NS) + ent.findall(".//owl:sameAs", NS)
        if el.attrib.get(f"{{{NS['rdf']}}}resource")
    ]

    lic_el = ent.find("ksam:itemLicenseUrl", NS)
    lic_url = lic_el.attrib.get(f"{{{NS['rdf']}}}resource") if lic_el is not None else "http://creativecommons.org/publicdomain/zero/1.0/"

    return {
        "entity_uri": entity_uri,
        "lamning_id": lamning_id,
        "raa_id": raa_id,
        "other_numbers": other_numbers or None,
        "fmis_replaces": replaces or None,
        "item_class": item_class,
        "fornsok_url": fornsok_url,
        "coordinates": coords,
        "parish": parish,
        "municipality": municipality,
        "province": province,
        "county": county,
        "place_label": place_label,
        "country": "SE",
        "period": period,
        "dating": dating_raw,
        "antiquarian_status": specs.get("Antikvarisk bedömning"),
        "field_status": specs.get("Aktualitetsstatus"),
        "damage_status": descs.get("Skadestatus"),
        "placement": descs.get("Placering"),
        "investigation_status": descs.get("Undersökningsstatus"),
        "description": main_desc,
        "linked_media_urls": media_urls,
        "related_uris": related or None,
        "license": "CC0 1.0" if "zero/1.0" in (lic_url or "") else lic_url,
        "license_url": lic_url,
        "last_changed": _txt(ent.find("ksam:lastChangedDate", NS)) or None,
    }


def harvest_all_raa_sites() -> List[Dict[str, Any]]:
    print("Harvesting ALL RAÄ Fornsök Hällristning & Hällmålning monuments via K-samsök RDF...")
    query = 'serviceOrganization="raä" AND (itemClassName="Hällristning" OR itemClassName="Hällmålning")'
    sites: List[Dict[str, Any]] = []
    seen_uris: Set[str] = set()
    start = 1
    hits_per_page = 500
    total_hits = None

    while total_hits is None or start <= total_hits:
        r = polite_get(
            KSAMSOK_API,
            params={
                "method": "search",
                "recordSchema": "rdf",
                "hitsPerPage": hits_per_page,
                "startRecord": start,
                "query": query,
            },
            delay=0.06,
            timeout=50,
        )
        if not r or not r.ok:
            print(f"  WARN: K-samsök RAÄ startRecord={start} failed ({getattr(r, 'status_code', None)})")
            break
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError as e:
            print(f"  WARN: XML parse error at start={start}: {e}")
            break
        if total_hits is None:
            th = root.findtext(".//totalHits")
            total_hits = int(th) if th and th.isdigit() else 0
            print(f"  RAÄ totalHits reported: {total_hits}")
        rec_els = root.findall(".//records/record")
        if not rec_els:
            break
        for rec_el in rec_els:
            parsed = parse_rdf_record(rec_el)
            if parsed and parsed["entity_uri"] not in seen_uris:
                seen_uris.add(parsed["entity_uri"])
                sites.append(parsed)
        start += len(rec_els)
        if len(sites) % 3000 < hits_per_page or start > total_hits:
            print(f"  RAÄ sites progress: {len(sites)}/{total_hits}")

    with open(SITES_PATH, "w", encoding="utf-8") as f:
        for s in sites:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"Saved {len(sites)} RAÄ sites -> {SITES_PATH}")
    return sites


def normalize_license(raw: str) -> Tuple[str, str, bool]:
    s = (raw or "").strip().lower()
    if "by-nc" in s or "-nc" in s or "by-nd" in s or "-nd" in s:
        return "Restricted", raw, False
    if "cc0" in s or "zero/1.0" in s:
        return "CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/", True
    if "pdmark" in s or "pdm" in s or "mark/1.0" in s or "publicdomain" in s:
        return "Public Domain Mark 1.0", "https://creativecommons.org/publicdomain/mark/1.0/", True
    if "by-sa" in s:
        return "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/", True
    if "#by" in s or "by/4.0" in s or "by/3.0" in s:
        return "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/", True
    return "Unknown", raw, False


def classify_visual_plate(raw_bytes: bytes, text_blob: str) -> Tuple[str, int, int, str]:
    """Classify downloaded image as bw_tracing, rubbing_frottage, painted_photo, photo_other, or reject."""
    if len(raw_bytes) < 2048:
        return "reject", 0, 0, "file_too_small"
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if w < 300 or h < 200:
                return "reject", w, h, f"too_small_{w}x{h}"
            small = im.convert("RGB")
            small.thumbnail((512, 512))
            arr = np.asarray(small, dtype=np.float32)
    except Exception as exc:
        return "reject", 0, 0, f"corrupt:{exc}"

    low = text_blob.lower()
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    sat = np.max(arr, axis=2) - np.min(arr, axis=2)
    red_paint = (r > 115) & ((r - np.maximum(g, b)) > 45)
    rock_bg = (lum > 35) & (lum < 175) & (sat < 35)
    red_frac = float(np.mean(red_paint))
    rock_frac = float(np.mean(rock_bg))

    if any(p in low for p in PAINT_TOKENS) or (red_frac > 0.008 and rock_frac > 0.28):
        return "painted_photo", w, h, "painted_photo_detected"

    p70 = float(np.percentile(lum, 70))
    p95 = float(np.percentile(lum, 95))
    p05 = float(np.percentile(lum, 5))
    white_frac = float(np.mean(lum > 195))
    mean_sat = float(np.mean(sat))

    if "frottage" in low or "gnidbild" in low or "rubbing" in low:
        if (p95 - p05) >= 60:
            return "rubbing_frottage", w, h, "ok_rubbing"
    if any(d in low for d in DRAWING_TOKENS):
        # Accept clean tracings/drawings/engravings (may be monochrome or aged cream paper)
        if mean_sat < 45 and (p95 - p05) >= 50:
            return "bw_tracing", w, h, "ok_drawing_meta"
    # Visual B&W tracing detection (high white paper fraction, low saturation, dark ink)
    ink_frac = float(np.mean(lum < 120))
    if p70 >= 195 and white_frac >= 0.50 and mean_sat < 28 and 0.003 <= ink_frac <= 0.45 and (p95 - p05) >= 60:
        return "bw_tracing", w, h, "ok_bw_visual"

    return "photo_other", w, h, "unpainted_or_color_photo"


def harvest_ksamsok_open_plates(
    existing_shas: Set[str],
    existing_sids: Set[str],
    existing_urls: Set[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Harvest open non-paint rock-art plates from non-SHFA, non-DiMu K-samsök providers."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    lic_clause = (
        '(mediaLicense="http://kulturarvsdata.se/resurser/License#cc0" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#pdm" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#pdmark" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#by" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#by-sa")'
    )
    # Query non-SHFA, non-DiMu Swedish providers in K-samsök (plus shm, s-kps, blm, jlm, upmu, gsm, mm, skm, slm, ka, jm, nbm)
    queries = [
        f'(text="hällristning" OR text="hällmålning") AND (text="ritning" OR text="teckning" OR text="avritning" OR text="kalkering" OR text="kopparstick" OR text="grafik" OR text="frottage" OR text="uppmätning") AND {lic_clause} AND thumbnailExists=j',
        f'(serviceOrganization="s-kps" OR serviceOrganization="blm" OR serviceOrganization="jlm" OR serviceOrganization="upmu" OR serviceOrganization="gsm" OR serviceOrganization="mm" OR serviceOrganization="skm" OR serviceOrganization="slm" OR serviceOrganization="ka" OR serviceOrganization="jm" OR serviceOrganization="shm") AND (text="hällristning" OR text="hällmålning") AND {lic_clause} AND thumbnailExists=j',
    ]

    candidates: Dict[str, Dict[str, Any]] = {}
    for q in queries:
        start = 1
        total = None
        while total is None or start <= min(total, 1500):
            r = polite_get(
                KSAMSOK_API,
                params={
                    "method": "search",
                    "recordSchema": "presentation",
                    "hitsPerPage": 200,
                    "startRecord": start,
                    "query": q,
                },
                headers={"Accept": "application/json"},
                delay=0.08,
            )
            if not r or not r.ok:
                break
            try:
                d = r.json().get("result", {})
            except ValueError:
                break
            if total is None:
                total = int(d.get("totalHits") or 0)
            recs = d.get("records", {})
            if isinstance(recs, dict):
                recs = recs.get("record", [])
            if isinstance(recs, dict):
                recs = [recs]
            if not recs:
                break
            for rec in recs:
                it = rec.get("pres:item") or {}
                if not it:
                    continue
                org_short = it.get("pres:organizationShort")
                org_short = str(org_short.get("content", "") if isinstance(org_short, dict) else (org_short or "")).strip().lower()
                if org_short in ("shfa", "bhm", "vbg", "s-ylm", "s-vlm", "s-uvlm", "s-vgm", "s-fbm", "s-hm", "s-xlm", "s-dm"):
                    continue  # Handled by shfa_full or dimu_se
                org = it.get("pres:organization")
                org = str(org.get("content", "") if isinstance(org, dict) else (org or "")).strip()
                lbl = it.get("pres:itemLabel")
                lbl = str(lbl.get("content", "") if isinstance(lbl, dict) else (lbl or "")).strip()
                desc = it.get("pres:description")
                desc = str(desc.get("content", "") if isinstance(desc, dict) else (desc or "")).strip()
                combo = f"{lbl} {desc} {org}".lower()
                if any(b in combo for b in BAD_OBJECT_TOKENS):
                    continue

                imgs = it.get("pres:image")
                if isinstance(imgs, dict):
                    imgs = [imgs]
                if not isinstance(imgs, list) or not imgs:
                    continue
                im0 = imgs[0]
                lic_name, lic_url, comm_ok = normalize_license(str(im0.get("pres:mediaLicense") or im0.get("pres:mediaLicenseUrl") or ""))
                if not comm_ok:
                    continue
                srcs = im0.get("pres:src") or []
                if isinstance(srcs, dict):
                    srcs = [srcs]
                img_url = None
                for pref in ("highres", "lowres", "thumbnail"):
                    for s in srcs:
                        if isinstance(s, dict) and s.get("type") == pref and s.get("content"):
                            img_url = s["content"]
                            break
                    if img_url:
                        break
                if not img_url or "dimu.org" in img_url:
                    continue
                ent_uri = str(it.get("pres:entityUri") or it.get("pres:id") or img_url)
                sid = "ksamsok_" + re.sub(r"[^a-zA-Z0-9]+", "_", ent_uri)[-40:]
                if sid in existing_sids or img_url in existing_urls or sid in candidates:
                    continue

                reps = (it.get("pres:representations") or {}).get("pres:representation") or []
                if isinstance(reps, dict):
                    reps = [reps]
                web_url = next((x.get("content") for x in reps if isinstance(x, dict) and x.get("format") == "HTML"), "") or ent_uri

                # Coords if present in pres:item
                coords_obj = None
                geow = it.get("georss:where") or {}
                if isinstance(geow, list):
                    geow = geow[0] if geow else {}
                pt_obj = (geow.get("gml:Point") or {}) if isinstance(geow, dict) else {}
                if isinstance(pt_obj, list):
                    pt_obj = pt_obj[0] if pt_obj else {}
                pts = pt_obj.get("gml:coordinates") if isinstance(pt_obj, dict) else None
                if isinstance(pts, dict):
                    pts = pts.get("content")
                if isinstance(pts, str) and "," in pts:
                    try:
                        lon_s, lat_s = pts.split(",")[:2]
                        coords_obj = {"lon": float(lon_s), "lat": float(lat_s), "crs": "EPSG:4326"}
                    except ValueError:
                        pass

                ctx = it.get("pres:context") or {}
                if isinstance(ctx, list):
                    ctx = next((c for c in ctx if isinstance(c, dict) and c.get("pres:placeLabel")), ctx[0] if ctx else {})
                place = ctx.get("pres:placeLabel") if isinstance(ctx, dict) else None
                if isinstance(place, dict):
                    place = place.get("content")
                elif isinstance(place, list):
                    place = place[0].get("content") if isinstance(place[0], dict) else str(place[0])
                byline = im0.get("pres:byline")
                if isinstance(byline, dict):
                    byline = byline.get("content")
                creator = f"{byline} / {org}" if byline and org else (byline or org or "K-samsök / SOCH")

                candidates[sid] = {
                    "source_id": sid,
                    "source_archive": "ksamsok",
                    "title": lbl or f"K-samsök {org}",
                    "description": desc or f"{lbl} ({org}).",
                    "site": str(place or lbl or org)[:120],
                    "region": str(place or org or "Sweden"),
                    "country": "SE",
                    "period": "stone_age_hunter" if any(h in combo for h in HUNTER_TOKENS) else "bronze_age_agrarian",
                    "motif_hints": ["bronze_age_ship", "anthropomorph"] if "hällristning" in combo else ["elk_moose"],
                    "source_url": str(web_url),
                    "download_url": img_url,
                    "creator": creator,
                    "rights_holder": org or creator,
                    "license": lic_name,
                    "license_url": lic_url,
                    "commercial_ok": True,
                    "coordinates": coords_obj,
                    "_text_blob": combo,
                }
            start += len(recs)

    print(f"K-samsök non-DiMu/non-SHFA candidate records to inspect: {len(candidates)}")
    lock = threading.Lock()
    seen_shas = set(existing_shas)
    manifest_recs: List[Dict[str, Any]] = []
    stats: Counter = Counter()

    def worker(cand: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = cand["download_url"]
        resp = polite_get(url, delay=0.05, timeout=35, retries=3)
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

        text_blob = cand.pop("_text_blob", "")
        mtype, w, h, note = classify_visual_plate(raw_bytes, text_blob)
        if mtype in ("reject", "photo_other"):
            with lock:
                stats[f"skipped_{mtype}"] += 1
            return None

        ct = (resp.headers.get("content-type") or "").lower().split(";")[0].strip()
        ext = ".png" if ct == "image/png" else (".webp" if ct == "image/webp" else ".jpg")
        fname = f"{cand['source_id']}_{sha[:8]}{ext}"
        out = dict(cand)
        out["sha256"] = sha
        out["width_px"] = w
        out["height_px"] = h
        out["media_type"] = mtype

        if mtype == "painted_photo":
            (QUARANTINE_DIR / fname).write_bytes(raw_bytes)
            out["raw_path"] = f"sources/raa_fornsok_ksamsok/quarantine_raw/{fname}"
            out["quarantined"] = True
            out["quarantine_reason"] = "painted_photo: modern paint detected — NEVER trace"
            out["traceable"] = False
            with lock:
                stats["quarantined_painted"] += 1
            return out

        (RAW_DIR / fname).write_bytes(raw_bytes)
        out["raw_path"] = f"sources/raa_fornsok_ksamsok/raw/{fname}"
        out["quarantined"] = False
        out["quarantine_reason"] = None
        out["traceable"] = True
        with lock:
            stats["downloaded_ok"] += 1
        return out

    with ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(worker, list(candidates.values())):
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
    if SITES_PATH.exists() and sum(1 for _ in open(SITES_PATH, "r", encoding="utf-8")) >= 21000:
        print(f"Loading cached RAÄ sites from {SITES_PATH}...")
        sites = [json.loads(l) for l in open(SITES_PATH, "r", encoding="utf-8") if l.strip()]
    else:
        sites = harvest_all_raa_sites()
    manifest_recs, dl_stats = harvest_ksamsok_open_plates(existing_shas, existing_sids, existing_urls)

    summary = {
        "sites_total": len(sites),
        "sites_hallristning": sum(1 for s in sites if "ristning" in (s.get("item_class") or "").lower()),
        "sites_hallmalning": sum(1 for s in sites if "målning" in (s.get("item_class") or "").lower()),
        "sites_with_coords": sum(1 for s in sites if s.get("coordinates")),
        "sites_with_lamning_id": sum(1 for s in sites if s.get("lamning_id")),
        "sites_with_raa_id": sum(1 for s in sites if s.get("raa_id")),
        "unique_parishes": len({s["parish"] for s in sites if s.get("parish")}),
        "manifest_total": len(manifest_recs),
        "manifest_traceable": sum(1 for r in manifest_recs if r.get("traceable")),
        "manifest_quarantined": sum(1 for r in manifest_recs if r.get("quarantined")),
        "download_stats": dl_stats,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (SCRIPT_DIR / "raa_crawl_stats.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print("RAÄ/K-samsök crawl complete:", json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
