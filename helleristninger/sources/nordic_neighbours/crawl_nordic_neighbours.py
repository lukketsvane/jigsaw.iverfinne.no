#!/usr/bin/env python3
"""
Nordic Neighbours Open-Archive Harvester (Denmark, Finland, Europeana).

Harvests open-licensed rock-art sites and archival plates with exact EPSG:4326
coordinates, explicit licences, SHA-256 verification, and structured chronology:
  1. Finland — Museovirasto (Finnish Heritage Agency) GeoServer WFS:
     ALL registered rock paintings (kalliomaalaukset), rock carvings
     (kalliopiirrokset), and cup-marked rocks (kuppikalliot) with exact
     EPSG:4326 coords -> sites_finland.jsonl.
     Note: api.finna.fi is Cloudflare-gated (403) and never bypassed; Finnish
     archival media (CC BY 4.0) is harvested via Europeana open API and
     geocoded by matching Kohdenimi + Kunta against Museovirasto WFS.
  2. Denmark — Nationalmuseet (samlinger.natmus.dk/api/assetbrowse) &
     Danish Agency for Culture (KUAS / kulturarv.dk/fundogfortidsminder via
     Europeana + site pages for exact WGS84 coordinates) -> sites_denmark.jsonl.
  3. Europeana Open Rock-Art Records across Denmark, Finland, Norway, Sweden,
     and Baltic states (CC0, PDM, CC BY, CC BY-SA).

Deduplicates by source_id and SHA-256 against all existing manifests in
helleristninger/sources/*/manifest.jsonl. Quarantines painted_photo carvings.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

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
SITES_FI_PATH = SCRIPT_DIR / "sites_finland.jsonl"
SITES_DK_PATH = SCRIPT_DIR / "sites_denmark.jsonl"

USER_AGENT = (
    "HelleristningerCorpusHarvester/2.0 "
    "(https://jigsaw.iverfinne.no; academic-cultural-heritage-research) "
    "python-requests"
)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json, text/html, */*;q=0.8"})
    return s


def load_existing_dedup_keys() -> Tuple[Set[str], Set[str]]:
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


def normalize_license(raw: str) -> Tuple[str, str, bool]:
    s = (raw or "").strip()
    low = s.lower()
    if not low or any(x in low for x in ("nc", "nd", "noncommercial", "noderiv", "restricted", "in copyright", "unknown")):
        return s or "Unknown", "", False
    if "zero/1.0" in low or "cc0" in low:
        return "CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/", True
    if "mark/1.0" in low or "public domain" in low or "pdm" in low:
        return "Public Domain Mark 1.0", "https://creativecommons.org/publicdomain/mark/1.0/", True
    if "by-sa" in low:
        ver = (re.search(r"(\d\.\d)", low) or [None, "4.0"])[1]
        return f"CC BY-SA {ver}", f"https://creativecommons.org/licenses/by-sa/{ver}/", True
    if "/by/" in low or "cc by" in low or "cc-by" in low:
        ver = (re.search(r"(\d\.\d)", low) or [None, "4.0"])[1]
        return f"CC BY {ver}", f"https://creativecommons.org/licenses/by/{ver}/", True
    return s, s if s.startswith("http") else "", False


def infer_motif_hints(text: str) -> List[str]:
    low = text.lower()
    patterns = [
        ("elk_moose", r"\b(hirvi|elk|moose|älg|elg)\b"),
        ("reindeer", r"\b(peura|poro|reindeer|ren)\b"),
        ("bronze_age_ship", r"\b(skib|skepp|skip|laiva|ship)\b"),
        ("stone_age_boat", r"\b(vene|boat|båt)\b"),
        ("anthropomorph", r"\b(ihminen|ihmis|människ|mennesk|human|anthropomorph)\b"),
        ("handprint", r"\b(käsi|kämmen|hand|hånd)\b"),
        ("footprint", r"\b(fotsål|fotsul|fodspor|footprint|jalka)\b"),
        ("sun_wheel", r"\b(solhjul|solkors|hjulkors|hjul|wheel)\b"),
        ("bird", r"\b(lintu|fågel|fugl|bird|joutsen|swan)\b"),
        ("snake", r"\b(käärme|snake|orm|slange)\b"),
        ("horse", r"\b(hevonen|häst|hest|horse)\b"),
    ]
    return [m for m, p in patterns if re.search(p, low)]


def inspect_image_bytes(raw_bytes: bytes, initial_mt: str, *, is_rock_painting: bool = False) -> Tuple[bool, int, int, str, str]:
    if len(raw_bytes) < 1500:
        return False, 0, 0, initial_mt, "too_small_bytes"
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if w < 180 or h < 180:
                return False, w, h, initial_mt, f"too_small_{w}x{h}"
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
    white_cnt = dark_cnt = red_paint_cnt = light_cnt = 0
    for r, g, b in pixels:
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

    mean_sat = sat_sum / n
    white_frac = white_cnt / n
    dark_frac = dark_cnt / n
    light_frac = light_cnt / n
    red_frac = red_paint_cnt / n

    mt = initial_mt
    if mean_sat < 0.09 and white_frac > 0.45 and dark_frac > 0.008:
        mt = "bw_tracing"
    elif mt == "bw_tracing" and (light_frac < 0.35 or (mean_sat > 0.18 and white_frac < 0.25)):
        mt = "photo_other" if is_rock_painting else ("painted_photo" if red_frac >= 0.012 else "photo_other")
    elif mt in ("painted_photo", "photo_other") and not is_rock_painting:
        mt = "painted_photo" if red_frac >= 0.012 else "photo_other"

    return True, w, h, mt, "ok"


# ---------------------------------------------------------------------------
# 1. Finland: Museovirasto GeoServer WFS
# ---------------------------------------------------------------------------

def harvest_finland_wfs(session: requests.Session) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    print("[1/4] Fetching Finnish Heritage Agency (Museovirasto) GeoServer WFS...", flush=True)
    r = session.get(
        "https://geoserver.museovirasto.fi/geoserver/wfs",
        params={
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeNames": "museokartta:kiinteat_muinaisjaannokset_pisteet",
            "CQL_FILTER": "Alatyyppi LIKE '%kallio%' OR Alatyyppi LIKE '%piirros%' OR Alatyyppi LIKE '%maalaus%'",
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
        },
        timeout=30,
    )
    r.raise_for_status()
    feats = r.json().get("features", [])
    sites: List[Dict[str, Any]] = []
    lookup: Dict[str, Dict[str, Any]] = {}

    for f in feats:
        props = f.get("properties") or {}
        geom = f.get("geometry") or {}
        coords_raw = geom.get("coordinates") or []
        if len(coords_raw) < 2:
            continue
        lon, lat = round(float(coords_raw[0]), 6), round(float(coords_raw[1]), 6)
        name = str(props.get("Kohdenimi") or "").strip()
        muni = str(props.get("Kunta") or "").strip()
        subtype = str(props.get("Alatyyppi") or "").strip()
        dating_txt = str(props.get("Ajoitus") or "").strip()
        kid = props.get("Kohdetunnus")
        kyppi_url = str(props.get("Kohteen_tiedot") or f"https://www.kyppi.fi/to.aspx?id=112.{kid}")

        period = "stone_age_hunter" if ("maalaus" in subtype.lower() or "kivikaut" in dating_txt.lower()) else (
            "bronze_age_agrarian" if "pronssi" in dating_txt.lower() else "mixed"
        )
        chron = resolve_chronology(
            site=name, region=muni, country="FI", period=period,
            title=f"{name} {subtype}", raw_dating=dating_txt,
        )
        site_rec = {
            "site_id": f"museovirasto_{kid}",
            "kohdetunnus": kid,
            "site": name,
            "region": muni,
            "country": "FI",
            "site_type": props.get("Tyyppi"),
            "subtype": subtype,
            "raw_dating": dating_txt,
            "period": period,
            "coordinates": {
                "crs": "EPSG:4326",
                "lat": lat,
                "lon": lon,
                "precision": "museovirasto_wfs_point",
                "provenance": "Museovirasto GeoServer WFS museokartta:kiinteat_muinaisjaannokset_pisteet",
            },
            "source_url": kyppi_url,
            "license": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            **chron,
        }
        sites.append(site_rec)

        # Index normalized site stems for geocoding Europeana Finnish records
        norm = re.sub(r"\s+", " ", name.lower()).strip()
        if norm:
            lookup[norm] = site_rec
            # Also stem without common Finnish genitive -n / compound suffixes
            for stem in (norm.rstrip("n"), f"{norm} {muni.lower()}"):
                if len(stem) >= 4:
                    lookup.setdefault(stem, site_rec)

    sites.sort(key=lambda s: str(s["site_id"]))
    with SITES_FI_PATH.open("w", encoding="utf-8") as fh:
        for s in sites:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"  Museovirasto WFS: {len(sites)} Finnish rock-art sites saved to {SITES_FI_PATH.name}", flush=True)
    return sites, lookup


def match_finland_site(title: str, spatial: str, fi_lookup: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    blob = f"{title} {spatial}".lower()
    # Check longest keys first to avoid short false matches
    for key in sorted(fi_lookup.keys(), key=len, reverse=True):
        if len(key) >= 5 and key in blob:
            return fi_lookup[key]
    return None


# ---------------------------------------------------------------------------
# 2. Nationalmuseet Denmark (samlinger.natmus.dk)
# ---------------------------------------------------------------------------

def collect_natmus_dk(session: requests.Session) -> List[Dict[str, Any]]:
    print("[2/4] Crawling Nationalmuseet Denmark (samlinger.natmus.dk)...", flush=True)
    keywords = ["helleristning", "helleristninger", "helleristningssten", "skåltegn"]
    hits_by_url: Dict[str, Dict[str, Any]] = {}
    for kw in keywords:
        try:
            r = session.get("https://samlinger.natmus.dk/api/assetbrowse", params={"keyword": kw, "size": 100}, timeout=15)
            if r.ok:
                for h in r.json().get("hits") or []:
                    u = h.get("url")
                    if u and u not in hits_by_url:
                        hits_by_url[u] = h
        except Exception:
            pass

    cands: List[Dict[str, Any]] = []
    for path, h in hits_by_url.items():
        title = str(h.get("title") or "").strip()
        low = title.lower()
        # Exclude non-Nordic items (e.g., Virgin Islands Reef Bay)
        if any(bad in low for bad in ("virgin islands", "st. john", "reef bay", "caribbean", "amerika", "grønland")):
            continue
        page_url = f"https://samlinger.natmus.dk{path}"
        try:
            pr = session.get(page_url, timeout=12)
            if not pr.ok:
                continue
        except Exception:
            continue
        m_lic = re.search(r"https?://creativecommons\.org/[^\s\"'<>]+", pr.text, re.I)
        lic_name, lic_url, comm_ok = normalize_license(m_lic.group(0) if m_lic else "")
        if not comm_ok:
            continue
        asset_num = path.rsplit("/", 1)[-1]
        coll = path.strip("/").split("/")[0]
        country = "SE" if any(s in low for s in ("himmelstalund", "kivik", "tanum", "bohuslän", "skåne", "sverige")) else "DK"
        region = "Bornholm" if "bornholm" in low else ("Östergötland" if "himmelstalund" in low else "Denmark")
        site = "Bornholm" if "bornholm" in low else ("Himmelstalund" if "himmelstalund" in low else title[:60])
        mtype = "bw_tracing" if any(k in low for k in ("tegning", "kalkering", "aftegning", "planche")) else "photo_other"
        cands.append({
            "source_id": f"natmus_dk_{coll}_{asset_num}",
            "source_archive": "nationalmuseet",
            "title": title,
            "description": f"Nationalmuseet Denmark collection item ({coll}): {title}",
            "site": site,
            "region": region,
            "country": country,
            "period": "bronze_age_agrarian",
            "media_type": mtype,
            "motif_hints": infer_motif_hints(title),
            "coordinates": None,
            "source_url": page_url,
            "download_url": f"{page_url}.jpg",
            "creator": "Nationalmuseet (Danmark)",
            "license": lic_name,
            "license_url": lic_url,
            "commercial_ok": True,
            "_is_rock_painting": False,
        })
    print(f"  Nationalmuseet DK candidates: {len(cands)}", flush=True)
    return cands


# ---------------------------------------------------------------------------
# 3. Europeana (Denmark KUAS, Finland Museovirasto, Nordic open rock art)
# ---------------------------------------------------------------------------

def fetch_kuas_site_coords(session: requests.Session, site_url: str) -> Optional[Dict[str, Any]]:
    try:
        r = session.get(site_url, timeout=12)
        if not r.ok:
            return None
        m = re.search(r"(\d{1,2}\.\d{4,18})\|(5[4-8]\.\d{4,18})", r.text)
        if m:
            lon, lat = round(float(m.group(1)), 6), round(float(m.group(2)), 6)
            return {
                "crs": "EPSG:4326",
                "lat": lat,
                "lon": lon,
                "precision": "kulturarv_dk_site_point",
                "provenance": f"Slots- og Kulturstyrelsen Fund og Fortidsminder ({site_url})",
            }
    except Exception:
        pass
    return None


def collect_europeana_neighbours(
    session: requests.Session, fi_lookup: Dict[str, Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    print("[3/4] Querying Europeana open records (Denmark KUAS, Finland Museovirasto, Nordic)...", flush=True)
    queries = [
        ("dk_kuas", '"The Danish Agency for Culture" AND (Madsebakke OR helleristning OR skåltegn OR Allinge OR Hammersholm OR Engelstrup)', 200),
        ("dk_general", "helleristning OR skåltegn", 150),
        ("fi_museovirasto", "kalliomaalaus OR kalliopiirros", 250),
        ("nordic_tracings", "hällristning AND (avritning OR kalkering OR teckning OR frottage)", 100),
    ]

    raw_items: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for tag, q, max_rows in queries:
        start = 1
        got = 0
        while got < max_rows:
            rows = min(100, max_rows - got)
            try:
                r = session.get(
                    "https://api.europeana.eu/record/v2/search.json",
                    params={
                        "wskey": "api2demo",
                        "query": q,
                        "reusability": "open",
                        "media": "true",
                        "rows": rows,
                        "start": start,
                        "profile": "rich",
                    },
                    timeout=20,
                )
                if not r.ok:
                    break
                items = r.json().get("items") or []
                if not items:
                    break
                for it in items:
                    eid = it.get("id")
                    if eid and eid not in raw_items:
                        raw_items[eid] = (tag, it)
                got += len(items)
                start += len(items)
                if len(items) < rows:
                    break
                time.sleep(0.1)
            except Exception:
                break

    print(f"  Europeana unique items fetched: {len(raw_items)}", flush=True)

    # For KUAS Danish sites, fetch exact EPSG:4326 coords from kulturarv.dk/fundogfortidsminder/site/<id>
    kuas_urls: Set[str] = set()
    for tag, it in raw_items.values():
        for u in (it.get("edmIsShownAt") or []):
            if "kulturarv.dk/fundogfortidsminder/site/" in str(u):
                kuas_urls.add(str(u).replace("http://", "https://"))

    kuas_coord_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    print(f"  Resolving EPSG:4326 coords for {len(kuas_urls)} Danish KUAS sites...", flush=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(fetch_kuas_site_coords, session, u): u for u in sorted(kuas_urls)}
        for fut in as_completed(futs):
            kuas_coord_cache[futs[fut]] = fut.result()

    dk_sites: Dict[str, Dict[str, Any]] = {}
    cands: List[Dict[str, Any]] = []

    for eid, (tag, it) in raw_items.items():
        providers = " ".join(it.get("dataProvider") or [])
        if any(skip in providers.lower() for skip in ("wikimedia", "riksantikvarieämbetet", "national heritage board")):
            continue
        rights_list = it.get("rights") or []
        lic_name, lic_url, comm_ok = normalize_license(rights_list[0] if rights_list else "")
        if not comm_ok:
            continue

        title = str((it.get("title") or ["Nordic rock art"])[0]).strip()
        desc = " ".join(str(x) for x in (it.get("dcDescription") or [title])).strip()
        spatial = " ".join(str(x) for x in (it.get("dctermsSpatial") or [])).strip()
        eu_countries = [str(x).lower() for x in (it.get("country") or [])]

        # Determine download URL (prefer direct open image, fallback to Europeana cached preview for zetcom 401 hosts)
        shown_by = (it.get("edmIsShownBy") or [None])[0]
        preview = (it.get("edmPreview") or [None])[0]
        if shown_by and "mpfinlandrepox.zetcom.app" in shown_by:
            dl_url = preview
        else:
            dl_url = shown_by or preview
        if not dl_url:
            continue
        if dl_url.startswith("http://mm.dimu.org"):
            dl_url = "https://" + dl_url[len("http://"):]

        shown_at = next((str(u).replace("http://", "https://") for u in (it.get("edmIsShownAt") or [])), "")
        guid = it.get("guid") or f"https://www.europeana.eu/item{eid}"

        if "finland" in eu_countries or "museovirasto" in providers.lower() or "finnish heritage" in providers.lower() or tag == "fi_museovirasto":
            country = "FI"
            matched = match_finland_site(title, f"{desc} {spatial}", fi_lookup)
            coords = matched["coordinates"] if matched else None
            site = matched["site"] if matched else title[:80]
            region = matched["region"] if matched else (spatial or "Finland")
            period = matched["period"] if matched else "stone_age_hunter"
            is_painting = "maalaus" in f"{title} {desc}".lower()
            archive = "museovirasto_europeana"
        elif "denmark" in eu_countries or "danish agency" in providers.lower() or "kulturarv.dk" in shown_at:
            country = "DK"
            coords = kuas_coord_cache.get(shown_at)
            # Check Europeana's own lat/lon if present
            if coords is None and it.get("edmPlaceLatitude") and it.get("edmPlaceLongitude"):
                try:
                    coords = {
                        "crs": "EPSG:4326",
                        "lat": round(float(it["edmPlaceLatitude"][0]), 6),
                        "lon": round(float(it["edmPlaceLongitude"][0]), 6),
                        "precision": "europeana_place",
                        "provenance": "Europeana edmPlaceLatitude/Longitude",
                    }
                except (TypeError, ValueError):
                    coords = None
            site = title[:80]
            region = "Bornholm" if any(k in f"{title} {desc}".lower() for k in ("madsebakke", "allinge", "sandvig", "hammersholm", "bornholm")) else "Denmark"
            period = "bronze_age_agrarian"
            is_painting = False
            archive = "kuas_europeana" if "kulturarv.dk" in shown_at else "europeana"
            if shown_at and shown_at not in dk_sites:
                chron_dk = resolve_chronology(site=site, region=region, country="DK", period=period, title=title, description=desc)
                dk_sites[shown_at] = {
                    "site_id": f"kuas_{shown_at.rsplit('/', 1)[-1]}",
                    "site": site,
                    "region": region,
                    "country": "DK",
                    "period": period,
                    "coordinates": coords,
                    "source_url": shown_at,
                    "license": lic_name,
                    "license_url": lic_url,
                    **chron_dk,
                }
        else:
            country = "NO" if "norway" in eu_countries else ("SE" if "sweden" in eu_countries else "NORDIC")
            coords = None
            if it.get("edmPlaceLatitude") and it.get("edmPlaceLongitude"):
                try:
                    coords = {
                        "crs": "EPSG:4326",
                        "lat": round(float(it["edmPlaceLatitude"][0]), 6),
                        "lon": round(float(it["edmPlaceLongitude"][0]), 6),
                        "precision": "europeana_place",
                        "provenance": "Europeana edmPlaceLatitude/Longitude",
                    }
                except (TypeError, ValueError):
                    coords = None
            site = title[:80]
            region = spatial or providers or country
            period = "stone_age_hunter" if any(k in f"{title} {desc}".lower() for k in ("rødøy", "hell", "evenhus", "nämforsen")) else "bronze_age_agrarian"
            is_painting = False
            archive = "europeana"

        combo = f"{title} {desc}".lower()
        if any(skip in combo for skip in ("tavelgalleri", "museet", "utställning", "skylt", "porträtt", "karta", "bokomslag")):
            continue
        mtype = "bw_tracing" if any(k in combo for k in ("avritning", "teckning", "kalkering", "piirros", "drawing")) else "photo_other"
        eu_clean_id = eid.strip("/").replace("/", "_")
        cands.append({
            "source_id": f"europeana_{eu_clean_id}",
            "source_archive": archive,
            "title": title,
            "description": f"{desc[:500]} (Provider: {providers}).",
            "site": site,
            "region": region,
            "country": country,
            "period": period,
            "media_type": mtype,
            "motif_hints": infer_motif_hints(f"{title} {desc}"),
            "coordinates": coords,
            "source_url": guid,
            "download_url": dl_url,
            "creator": providers or "Europeana Contributor",
            "license": lic_name,
            "license_url": lic_url,
            "commercial_ok": True,
            "_is_rock_painting": is_painting,
        })

    dk_site_list = sorted(dk_sites.values(), key=lambda s: s["site_id"])
    with SITES_DK_PATH.open("w", encoding="utf-8") as fh:
        for s in dk_site_list:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    print(f"  Saved {len(dk_site_list)} Danish sites to {SITES_DK_PATH.name}; Europeana media candidates={len(cands)}", flush=True)
    return cands, dk_site_list


# ---------------------------------------------------------------------------
# 4. Download, SHA-256, Quarantine, & Write Manifest
# ---------------------------------------------------------------------------

def ext_from_response(ct: str, url: str) -> str:
    ct_low = (ct or "").lower().split(";")[0].strip()
    if ct_low == "image/png":
        return ".png"
    if ct_low == "image/webp":
        return ".webp"
    if ct_low in ("image/tiff", "image/tif"):
        return ".tif"
    ext = Path(urlparse(url).path).suffix.lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".tif"):
        return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def main() -> int:
    t0 = time.time()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    existing_ids, existing_sha = load_existing_dedup_keys()
    print(f"Loaded dedup baseline: {len(existing_ids)} source_ids, {len(existing_sha)} sha256 hashes", flush=True)

    session = make_session()
    _, fi_lookup = harvest_finland_wfs(session)
    natmus_cands = collect_natmus_dk(session)
    eu_cands, _ = collect_europeana_neighbours(session, fi_lookup)

    all_cands = natmus_cands + eu_cands
    uniq_cands: List[Dict[str, Any]] = []
    seen_cids: Set[str] = set()
    for c in all_cands:
        sid = c["source_id"]
        if sid in existing_ids or sid in seen_cids:
            continue
        seen_cids.add(sid)
        uniq_cands.append(c)

    print(f"[4/4] Downloading & verifying {len(uniq_cands)} unique neighbour candidates...", flush=True)
    manifest_records: List[Dict[str, Any]] = []
    stats = Counter()

    def process_one(c: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]], str]:
        sid = c["source_id"]
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", sid)
        existing_file = None
        for d in (RAW_DIR, QUARANTINE_DIR):
            hits = list(d.glob(f"{safe}.*"))
            if hits and hits[0].stat().st_size > 1500:
                existing_file = hits[0]
                break

        if existing_file is not None:
            raw_bytes = existing_file.read_bytes()
            ext = existing_file.suffix
        else:
            sess = make_session()
            resp = None
            for attempt in range(3):
                try:
                    time.sleep(0.08)
                    r = sess.get(c["download_url"], timeout=25)
                    if r.status_code == 200:
                        resp = r
                        break
                    if r.status_code in (429, 500, 502, 503, 504):
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    break
                except requests.RequestException:
                    time.sleep(1.0)
            if resp is None:
                return "reject", None, "http_fail"
            raw_bytes = resp.content
            ext = ext_from_response(resp.headers.get("content-type", ""), c["download_url"])

        ok, w, h, refined_mt, reason = inspect_image_bytes(
            raw_bytes, c["media_type"], is_rock_painting=bool(c.get("_is_rock_painting"))
        )
        if not ok:
            return "reject", None, reason

        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        quarantined = refined_mt == "painted_photo"
        fname = f"{safe}{ext}"
        target_dir = QUARANTINE_DIR if quarantined else RAW_DIR
        target_path = target_dir / fname
        target_path.write_bytes(raw_bytes)

        chron = resolve_chronology(
            site=c["site"], region=c["region"], country=c["country"],
            period=c["period"], title=c["title"], description=c["description"],
            motif_hints=c["motif_hints"],
        )
        rel_path = f"sources/nordic_neighbours/{'quarantine_raw' if quarantined else 'raw'}/{fname}"
        out = {k: v for k, v in c.items() if not k.startswith("_")}
        out.update({
            "media_type": refined_mt,
            "raw_path": rel_path,
            "sha256": sha256,
            "width_px": int(w),
            "height_px": int(h),
            "rights_holder": c["creator"],
            "audit_sha256_verified": True,
            "quarantined": quarantined,
            "quarantine_reason": "painted_photo: modern paint — NEVER trace" if quarantined else None,
            "traceable": refined_mt in ("svg_vector", "bw_tracing", "rubbing_frottage", "rti_3d"),
            **chron,
        })
        return "ok", out, "ok"

    with ThreadPoolExecutor(max_workers=6) as pool:
        for st, rec, reason in pool.map(process_one, uniq_cands):
            if st != "ok" or rec is None:
                stats[f"reject_{reason}"] += 1
                continue
            sha = rec["sha256"]
            if sha in existing_sha:
                (REPO_ROOT / "helleristninger" / rec["raw_path"]).unlink(missing_ok=True)
                stats["dedup_sha256"] += 1
                continue
            existing_sha.add(sha)
            manifest_records.append(rec)

    manifest_records.sort(key=lambda r: (r["country"], r["source_archive"], r["source_id"]))
    tmp_m = SCRIPT_DIR / "manifest.jsonl.tmp"
    with tmp_m.open("w", encoding="utf-8") as fh:
        for r in manifest_records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp_m, MANIFEST_PATH)

    print(
        f"DONE Nordic Neighbours in {time.time()-t0:.1f}s: manifest={len(manifest_records)}, "
        f"by_country={dict(Counter(r['country'] for r in manifest_records))}, "
        f"by_archive={dict(Counter(r['source_archive'] for r in manifest_records))}, "
        f"by_media={dict(Counter(r['media_type'] for r in manifest_records))}, "
        f"stats={dict(stats)}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
