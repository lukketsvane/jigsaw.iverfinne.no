#!/usr/bin/env python3
"""
Nordic Helleristninger / Rock Art Open-Archive Harvester
========================================================

Programmatically harvests openly licensed, high-contrast Nordic rock-art
tracings, drawings, graphics, rubbings, and selected clear museum carvings
from public, non-gated Nordic cultural heritage APIs:

  1. SHFA — Swedish Rock Art Research Archives (https://shfa.dh.gu.se/api/)
     License: Creative Commons Attribution 4.0 International (CC BY 4.0)
     Bulk-loads all 3,846 B&W tracings/drawings/graphics via paginated depth=1
     endpoints (types 944, 955, 951, 946) plus targeted rubbings (type 941)
     for Norwegian, Danish, and Northern Swedish hunter-gatherer sites.

  2. Europeana Open API (https://api.europeana.eu/record/v2/search.json)
     License: CC BY-SA 4.0, Public Domain Mark 1.0, CC0 1.0 (reusability=open)
     Coverage: Vänersborgs museum (Dalsland/Fröskog B&W tracings), Bohuslän
     Museum (PDM painted carvings), Upplands Museum (CC0 carvings).
     (Excludes Wikimedia Commons records to avoid duplicating harvester-wikimedia.)

  3. K-samsök / SOCH — Swedish Open Cultural Heritage (https://kulturarvsdata.se/)
     License: CC BY 3.0 / CC0 1.0 (explicitly filtered on mediaLicense)
     Coverage: Historic engravings (Kulturparken Småland Kopparstick),
     museum tracings & documentation (Vänersborg, Västernorrland, Jönköping, GNM).

  4. Nationalmuseet Denmark — Samlinger Online (https://samlinger.natmus.dk/)
     License: CC BY-SA 4.0
     Coverage: Iconic Danish Bronze Age carved stones (Engelstrup, Sandagergård,
     Bjergagergård, Kivik).

Blocked / Non-Harvested Portals (respected, never bypassed):
  - Finna (api.finna.fi): Cloudflare anti-bot challenge (HTTP 403).
  - Finnish Heritage Agency direct downloads (mpfinlandrepox.zetcom.app): HTTP 401.

Immutable originals are saved atomically to `raw/` with SHA-256 verification
and deduplication, and metadata is written to `manifest.jsonl`.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import numpy as np
import requests
from PIL import Image

Image.MAX_IMAGE_PIXELS = 250_000_000

BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "raw"
MANIFEST_PATH = BASE_DIR / "manifest.jsonl"

USER_AGENT = (
    "NordicHelleristningerResearchHarvester/1.0 "
    "(open cultural heritage research; jigsaw.iverfinne.no; polite rate-limited)"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, */*;q=0.8",
}

SHFA_BASE = "https://shfa.dh.gu.se"
SHFA_LICENSE = "CC BY 4.0"
SHFA_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"

# Verified clean B&W tracing / drawing / graphic authors in SHFA
# (Excludes cursive field notebooks like Einar Kjellén, coordinate-grid Burenhult
#  plates, 1800s yellowed pencil sketches, and stippled lithographs.)
SHFA_CLEAN_BW_AUTHORS: Set[str] = {
    "Andersson, Tommy och Toreld, Andreas",
    "Toreld, Andreas",
    "Toreld, Andreas och Bengtsson, Lasse",
    "Andersson, Tommy, Toreld, Andreas, och Bengtsson, Lasse",
    "Andersson, Tommy",
    "Broström, Sven-Gunnar och Ihrestam, Kenneth",
    "Broström, Sven-Gunnar",
    "Ihrestam, Kenneth",
    "Fredsjö, Åke",
    "Fredsjö, Åke och Nordbladh, Jarl",
    "Fredsjö, Åke Jönsson",
    "Tanums Hällristningsmuseum",
    "Tanums Hällristningsmuseum Underslös",
    "Milstreu, Gerhard",
    "Högberg, Torsten",
    "Faith-Ell, Harald",
    "Althin, Carl-Axel",
    "RockCare",
    "Bengtsson, Lasse",
    "Schultze, Hella",
    "Hallström, Gustaf",
    "Bakka, Egil",
    "Bøe, Johannes",
    "Lødøen, Trond",
    "Mandt, Gro",
    "Bendixen, Erik B",
    "Andersson, Göran",
    "Petersen, L",
    "SHFA",
}

BAD_COLLECTIONS = (
    "burenhult",
    "kjellén",
    "kjellen",
    "mandelgren",
    "baltzer",
    "holmberg",
    "brunius",
)

SE_PROVINCE_MAP: Dict[int, str] = {
    1: "Stockholms län",
    3: "Uppsala län (Uppland)",
    4: "Uppsala län (Uppland)",
    5: "Östergötlands län",
    6: "Jönköpings län",
    7: "Kronobergs län",
    8: "Kalmar län (Småland)",
    9: "Gotlands län",
    10: "Blekinge län",
    12: "Skåne län",
    13: "Hallands län",
    14: "Västra Götalands län (Bohuslän)",
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

SHFA_KEYWORD_TO_HINT: Dict[int, str] = {
    5043: "elk_moose",
    5038: "deer",
    5042: "bear",
    5037: "horse",
    4992: "bird",
    5072: "snake",
    4990: "fish",
    5044: "cattle",
    5040: "dog_canine",
    5039: "wolf",
    5041: "pig",
    5102: "goat",
    4997: "quadruped_animal",
    5113: "animal_procession",
    4991: "bronze_age_ship",
    5099: "sun_ship",
    5059: "bronze_age_ship",
    5060: "bronze_age_ship",
    5061: "bronze_age_ship",
    5062: "bronze_age_ship",
    5063: "bronze_age_ship",
    5064: "bronze_age_ship",
    5071: "iron_age_ship",
    5078: "bronze_age_ship",
    4995: "anthropomorph",
    5002: "human_female",
    5023: "warrior",
    5045: "archer",
    5055: "lur_blower",
    5052: "acrobat_vaulter",
    5074: "dancer",
    5025: "rider",
    5053: "adorant",
    5058: "bridal_couple",
    5067: "spear_bearer",
    5076: "axe_bearer",
    5066: "shield_bearer",
    5065: "ship_bearer",
    5097: "sword_lifter",
    5046: "human_procession",
    5088: "elk_head_staff_bearer",
    5003: "chariot_wagon",
    4994: "plough",
    5047: "sun_wheel",
    5007: "circle",
    5049: "concentric_circle",
    5056: "cross",
    5048: "spiral",
    5050: "double_spiral",
    5051: "triple_spiral",
    5077: "labyrinth",
    4999: "footprint",
    5054: "handprint",
    5075: "handprint",
    5057: "axe",
    5068: "sword",
    5073: "spear",
    5069: "shield",
    5083: "dagger",
    5108: "bow_arrow",
    5112: "halberd",
    4996: "weapon",
    4993: "tree",
    5109: "lur_horn",
    5101: "sun_holder",
    5103: "fishing_scene",
    5015: "net_figure",
    4989: "frame_figure",
}

FIGURATIVE_KEYWORD_IDS: Set[int] = set(SHFA_KEYWORD_TO_HINT.keys()) - {5007}

HUNTER_REGIONS_NO = {
    "bremanger", "vingen", "alta", "flora", "ausevik", "alstahaug",
    "narvik", "balsfjord", "tromsø", "hamarøy", "leiknes", "drammen", "modum",
}
MIXED_REGIONS_NO = {"steinkjer", "bardal", "inderøy"}
AGRARIAN_REGIONS_NO = {"fredrikstad", "sarpsborg", "stjørdal", "frosta", "østfold"}
HUNTER_REGIONS_SE = {"ådals-liden", "nämforsen", "alsen", "krokom", "glösa", "gärde", "umeå", "norrfors"}

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
    timeout: int = 30,
    delay: float = 0.08,
    retries: int = 3,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Optional[requests.Response]:
    sess = get_session()
    for attempt in range(retries):
        try:
            if delay > 0:
                time.sleep(delay)
            resp = sess.get(url, params=params, headers=extra_headers, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            return resp
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    return None


def ext_from_content_type_and_url(content_type: str, url: str) -> str:
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct == "image/jpeg":
        return ".jpg"
    if ct == "image/png":
        return ".png"
    if ct == "image/webp":
        return ".webp"
    if ct in ("image/tiff", "image/tif"):
        return ".tif"
    if ct == "image/svg+xml":
        return ".svg"
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".svg"):
        return ".jpg" if path_ext == ".jpeg" else path_ext
    return ".jpg"


def analyze_image_bytes(raw_bytes: bytes, media_type: str) -> Tuple[bool, int, int, str]:
    if len(raw_bytes) < 2048:
        return False, 0, 0, "file_too_small"
    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            w, h = im.size
            if w < 350 or h < 250:
                return False, w, h, f"too_small_{w}x{h}"
            small = im.convert("RGB")
            small.thumbnail((512, 512))
            arr = np.asarray(small, dtype=np.float32)
    except Exception as exc:
        return False, 0, 0, f"corrupt_image:{exc}"

    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    lum = 0.299 * r + 0.587 * g + 0.114 * b

    if media_type == "bw_tracing":
        p70 = float(np.percentile(lum, 70))
        p95 = float(np.percentile(lum, 95))
        p05 = float(np.percentile(lum, 5))
        white_frac = float(np.mean(lum > 205))
        dark_ink = lum < 120
        red_ink = (r > 120) & ((r - np.maximum(g, b)) > 45)
        ink_frac = float(np.mean(dark_ink | red_ink))
        bright_mask = lum > 170
        yellow_cast = float(np.mean(r[bright_mask]) - np.mean(b[bright_mask])) if np.any(bright_mask) else 999.0

        if p70 < 205 or white_frac < 0.50:
            return False, w, h, f"dark_or_textured_bg(p70={p70:.0f},wf={white_frac:.2f})"
        if yellow_cast > 38:
            return False, w, h, f"yellowed_paper(cast={yellow_cast:.0f})"
        if ink_frac < 0.003:
            return False, w, h, f"too_sparse(ink={ink_frac:.4f})"
        if ink_frac > 0.45:
            return False, w, h, f"too_dense(ink={ink_frac:.3f})"
        if (p95 - p05) < 60:
            return False, w, h, f"low_contrast(span={p95-p05:.0f})"
        return True, w, h, f"ok(wf={white_frac:.2f},ink={ink_frac:.3f})"

    if media_type == "rubbing_frottage":
        p95 = float(np.percentile(lum, 95))
        p05 = float(np.percentile(lum, 5))
        if (p95 - p05) < 70:
            return False, w, h, f"low_contrast_rubbing(span={p95-p05:.0f})"
        return True, w, h, f"ok_rubbing(span={p95-p05:.0f})"

    return True, w, h, "ok_photo"


def normalize_license(raw_url_or_name: str) -> Tuple[str, str, bool]:
    s = (raw_url_or_name or "").strip().lower()
    if "publicdomain/zero/1.0" in s or "license#cc0" in s or s == "cc0":
        return "CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/", True
    if "publicdomain/mark/1.0" in s or "license#pdm" in s or "public domain mark" in s:
        return "Public Domain Mark 1.0", "https://creativecommons.org/publicdomain/mark/1.0/", True
    if "publicdomain" in s or "public domain" in s:
        return "Public Domain", "https://creativecommons.org/publicdomain/mark/1.0/", True
    if "by-nc-nd" in s or "nc-nd" in s:
        return "CC BY-NC-ND", raw_url_or_name, False
    if "by-nc-sa" in s or "nc-sa" in s:
        return "CC BY-NC-SA", raw_url_or_name, False
    if "by-nc" in s or "-nc" in s:
        return "CC BY-NC", raw_url_or_name, False
    if "by-nd" in s or "-nd" in s:
        return "CC BY-ND", raw_url_or_name, False
    if "by-sa/4.0" in s or "cc-by-sa 4.0" in s or "cc by-sa 4.0" in s:
        return "CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/", True
    if "by-sa/3.0" in s or "license#by-sa" in s:
        return "CC BY-SA 3.0", "https://creativecommons.org/licenses/by-sa/3.0/", True
    if "by/4.0" in s or "cc by 4.0" in s or "cc-by 4.0" in s:
        return "CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/", True
    if "by/3.0" in s or "license#by" in s:
        return "CC BY 3.0", "https://creativecommons.org/licenses/by/3.0/", True
    return "Unknown", raw_url_or_name or "", False


def infer_shfa_country_region_period(
    rec: Dict[str, Any], region_hint: str = "", country_hint: str = ""
) -> Tuple[str, str, str, str]:
    site_obj = rec.get("site") or {}
    rco = rec.get("rock_carving_object") or {}
    rco_name = (rco.get("name") or "").strip() if isinstance(rco, dict) else ""
    raa_id = (site_obj.get("raa_id") or "").strip()
    ask_id = (site_obj.get("askeladden_id") or "").strip()
    placename = (site_obj.get("placename") or "").strip()
    stednavn = (site_obj.get("stednavn") or "").strip()
    coll_name = ((rec.get("collection") or {}).get("name") or "").strip() if isinstance(rec.get("collection"), dict) else ""
    inst_name = ((rec.get("institution") or {}).get("name") or "").strip() if isinstance(rec.get("institution"), dict) else ""
    dating_texts = [
        (d.get("english_translation") or d.get("text") or "").lower()
        for d in (rec.get("dating_tags") or [])
        if isinstance(d, dict)
    ]
    coords = (site_obj.get("coordinates") or {}).get("coordinates")

    country = country_hint
    if not country:
        if ask_id or "bergen" in inst_name.lower() or "vingen" in coll_name.lower() or "ausevik" in coll_name.lower():
            country = "NO"
        elif stednavn or site_obj.get("StedNr") or site_obj.get("FredningsNr") or any(
            k in (placename + " " + rco_name).lower()
            for k in ("bornholm", "själland", "sjælland", "madsebakke", "allinge", "sandvig", "odsherred", "lejre", "grevinge", "hammersholm")
        ):
            country = "DK"
        elif raa_id or site_obj.get("lamning_id") or site_obj.get("ksamsok_id"):
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
            country = "SE"

    if country == "NON_NORDIC":
        return "NON_NORDIC", "", "", "unknown"
    if coords and len(coords) == 2 and coords[1] < 54.3:
        return "NON_NORDIC", "", "", "unknown"

    site_parts = []
    if rco_name:
        site_parts.append(rco_name)
    if raa_id and raa_id.lower() not in rco_name.lower():
        site_parts.append(raa_id)
    elif placename and placename.lower() not in rco_name.lower():
        site_parts.append(placename)
    if ask_id and ask_id not in " ".join(site_parts):
        site_parts.append(f"Askeladden {ask_id}")
    site_label = " / ".join(site_parts) if site_parts else (raa_id or placename or f"SHFA site {site_obj.get('id')}")

    region = region_hint
    if not region:
        if country == "SE":
            prov_id = site_obj.get("province")
            prov_name = SE_PROVINCE_MAP.get(prov_id, "")
            parish_tok = raa_id.split()[0] if raa_id else ""
            if prov_name and parish_tok:
                region = f"{prov_name} — {parish_tok}"
            else:
                region = prov_name or parish_tok or "Sweden"
        elif country == "NO":
            low = (site_label + " " + coll_name).lower()
            if "vingen" in low:
                region = "Vestland — Bremanger (Vingen)"
            elif "ausevik" in low or "flora" in low:
                region = "Vestland — Flora (Ausevik)"
            elif "alta" in low or "hjemmeluft" in low or "apana" in low or "bergbukten" in low:
                region = "Finnmark — Alta"
            else:
                region = "Norway"
        elif country == "DK":
            low = (site_label + " " + stednavn).lower()
            if "bornholm" in low or "madsebakke" in low or "allinge" in low or "sandvig" in low or "hammersholm" in low:
                region = "Bornholm"
            elif "själland" in low or "grevinge" in low or "odsherred" in low:
                region = "Sjælland — Odsherred"
            elif "lejre" in low:
                region = "Sjælland — Lejre"
            else:
                region = "Denmark"

    low_loc = (region + " " + site_label + " " + coll_name).lower()
    has_ba = any("bronze" in t or "brons" in t for t in dating_texts)
    has_sa = any("stone" in t or "neolith" in t or "mesolith" in t or "sten" in t for t in dating_texts)
    if has_ba and has_sa:
        period = "mixed"
    elif any(k in low_loc for k in MIXED_REGIONS_NO):
        period = "mixed"
    elif any(k in low_loc for k in HUNTER_REGIONS_NO) or any(k in low_loc for k in HUNTER_REGIONS_SE):
        period = "mixed" if (has_ba and not has_sa) else "stone_age_hunter"
    elif country == "DK" or any(k in low_loc for k in AGRARIAN_REGIONS_NO) or country == "SE":
        period = "stone_age_hunter" if (has_sa and not has_ba) else "bronze_age_agrarian"
    else:
        period = "unknown"

    return country, region, site_label, period


def extract_shfa_motifs(rec: Dict[str, Any]) -> Tuple[List[str], Set[int], bool]:
    hints: List[str] = []
    kids: Set[int] = set()
    has_fig = False
    for kw in rec.get("keywords") or []:
        if not isinstance(kw, dict):
            continue
        kid = kw.get("id")
        if kid is not None:
            kids.add(int(kid))
        if kid in FIGURATIVE_KEYWORD_IDS:
            has_fig = True
        if kid in SHFA_KEYWORD_TO_HINT:
            h = SHFA_KEYWORD_TO_HINT[kid]
            if h not in hints:
                hints.append(h)
    return hints, kids, has_fig


def shfa_record_to_candidate(
    rec: Dict[str, Any],
    *,
    media_type: str,
    region_hint: str = "",
    country_hint: str = "",
    extra_hints: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    img_id = rec.get("id")
    download_url = rec.get("file") or ""
    if not img_id or not download_url:
        return None
    country, region, site, period = infer_shfa_country_region_period(
        rec, region_hint=region_hint, country_hint=country_hint
    )
    if country == "NON_NORDIC":
        return None

    author_obj = rec.get("author") or {}
    author_name = (author_obj.get("name") or "").strip() if isinstance(author_obj, dict) else ""
    inst_obj = rec.get("institution") or {}
    inst_name = (inst_obj.get("name") or "").strip() if isinstance(inst_obj, dict) else ""
    coll_obj = rec.get("collection") or {}
    coll_name = (coll_obj.get("name") or "").strip() if isinstance(coll_obj, dict) else ""
    type_obj = rec.get("type") or {}
    type_text = (type_obj.get("text") or "").strip() if isinstance(type_obj, dict) else ""
    type_en = (type_obj.get("english_translation") or type_text).strip() if isinstance(type_obj, dict) else ""

    hints, _, _ = extract_shfa_motifs(rec)
    if extra_hints:
        for h in extra_hints:
            if h not in hints:
                hints.append(h)

    creator_parts = [p for p in (author_name, inst_name) if p]
    creator = " / ".join(creator_parts) if creator_parts else "SHFA (University of Gothenburg)"
    year = rec.get("year")
    year_str = f" ({year})" if year else ""
    title = f"{site} — {type_en}{year_str}"
    kw_en = [
        k.get("english_translation") or k.get("text")
        for k in (rec.get("keywords") or [])
        if isinstance(k, dict) and (k.get("english_translation") or k.get("text"))
    ]
    desc_parts = [f"SHFA {type_en} ({type_text}) from {site} ({region}, {country})."]
    if coll_name:
        desc_parts.append(f"Collection: {coll_name}.")
    if kw_en:
        desc_parts.append("Motifs: " + ", ".join(kw_en) + ".")

    return {
        "source_id": f"shfa_{img_id}",
        "source_archive": "shfa",
        "title": title,
        "description": " ".join(desc_parts),
        "site": site,
        "region": region,
        "country": country,
        "period": period,
        "media_type": media_type,
        "motif_hints": hints or ["unclassified_motif"],
        "source_url": f"{SHFA_BASE}/image/{img_id}",
        "download_url": download_url,
        "creator": creator,
        "license": SHFA_LICENSE,
        "license_url": SHFA_LICENSE_URL,
        "commercial_ok": True,
    }


def is_clean_shfa_bw(rec: Dict[str, Any]) -> bool:
    author_obj = rec.get("author") or {}
    author_name = (author_obj.get("name") or "").strip() if isinstance(author_obj, dict) else ""
    coll_obj = rec.get("collection") or {}
    coll_name = (coll_obj.get("name") or "").strip() if isinstance(coll_obj, dict) else ""
    if any(bad in coll_name.lower() for bad in BAD_COLLECTIONS):
        return False
    if author_name and author_name not in SHFA_CLEAN_BW_AUTHORS:
        return False
    w = rec.get("width") or 0
    h = rec.get("height") or 0
    if w and h and (w < 400 or h < 300):
        return False
    return True


def fetch_all_shfa_bw_catalog() -> List[Dict[str, Any]]:
    """Bulk-fetch all SHFA B&W tracing/drawing/graphic metadata (types 944, 955, 951, 946) in ~10 API calls."""
    all_recs: List[Dict[str, Any]] = []
    for tid, tlabel in [(944, "Kalkering_Plast"), (955, "Kalkering_Papper"), (951, "Ritning"), (946, "Grafik")]:
        offset = 0
        total = None
        while total is None or offset < total:
            r = polite_get(
                f"{SHFA_BASE}/api/image/",
                params={"type": tid, "depth": 1, "limit": 500, "offset": offset},
                delay=0.05,
                timeout=35,
            )
            if not r or not r.ok:
                print(f"  WARN: SHFA type={tid} offset={offset} failed")
                break
            d = r.json()
            total = d.get("count", 0)
            batch = d.get("results") or []
            if not batch:
                break
            all_recs.extend(batch)
            offset += len(batch)
        print(f"  Bulk-loaded SHFA {tlabel}: {offset}/{total}")
    return all_recs


def select_shfa_bw_candidates(all_bw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Select ~310 balanced, motif-diverse, geographically rich clean B&W candidates from SHFA."""
    selected: Dict[str, Dict[str, Any]] = {}

    def try_add(rec: Dict[str, Any], *, region_hint: str = "", country_hint: str = "", extra_hints: Optional[List[str]] = None) -> bool:
        sid = f"shfa_{rec.get('id')}"
        if sid in selected:
            return False
        cand = shfa_record_to_candidate(
            rec,
            media_type="bw_tracing",
            region_hint=region_hint,
            country_hint=country_hint,
            extra_hints=extra_hints,
        )
        if cand is None:
            return False
        selected[sid] = cand
        return True

    clean = [r for r in all_bw if is_clean_shfa_bw(r)]
    print(f"  Clean SHFA B&W pool: {len(clean)} / {len(all_bw)} total")

    # Classify each clean record once for fast local selection
    annotated = []
    for r in clean:
        country, region, site, period = infer_shfa_country_region_period(r)
        if country == "NON_NORDIC":
            continue
        hints, kids, has_fig = extract_shfa_motifs(r)
        type_id = (r.get("type") or {}).get("id")
        author_name = ((r.get("author") or {}).get("name") or "").strip()
        annotated.append((r, country, region, site, period, hints, kids, has_fig, type_id, author_name))

    # 1. NORWAY B&W TRACINGS (Vingen / Bremanger — Stone Age hunter-gatherer deer/elk/anthropomorphs!)
    no_recs = [x for x in annotated if x[1] == "NO"]
    # Spread evenly across the Vingen pool so we sample diverse panels/authors (Bakka, Bøe, Lødøen, Hallström, Mandt)
    no_recs.sort(key=lambda x: (x[9], x[0]["id"]))
    step = max(1, len(no_recs) // 75)
    no_ordered = no_recs[::step] + [x for i, x in enumerate(no_recs) if i % step != 0]
    no_added = 0
    for x in no_ordered:
        if no_added >= 75:
            break
        if try_add(x[0], country_hint="NO", extra_hints=["deer"]):
            no_added += 1
    print(f"  Selected SHFA NO B&W candidates: {no_added} (from {len(no_recs)})")

    # 2. DENMARK B&W TRACINGS/GRAPHICS (Odsherred, Lejre, Bornholm)
    dk_recs = [x for x in annotated if x[1] == "DK"]
    dk_added = 0
    for x in dk_recs:
        if try_add(x[0], country_hint="DK"):
            dk_added += 1
    print(f"  Selected SHFA DK B&W candidates: {dk_added}")

    # 3. SWEDEN STONE AGE HUNTER-GATHERER (Nämforsen / Ådals-Liden & Gustaf Hallström)
    hunter_se = [
        x for x in annotated
        if x[1] == "SE" and (
            x[4] == "stone_age_hunter"
            or "hallström" in x[9].lower()
            or "nämforsen" in x[3].lower()
            or "ådals-liden" in x[3].lower()
        )
    ]
    h_added = 0
    for x in hunter_se:
        if h_added >= 35:
            break
        if try_add(x[0], country_hint="SE", extra_hints=["elk_moose"] if "nämforsen" in x[3].lower() else None):
            h_added += 1
    print(f"  Selected SHFA SE Hunter-Gatherer B&W candidates: {h_added}")

    # 4. SWEDEN MOTIF-STRATIFIED CLEAN B&W TRACINGS (ALL iconic motifs, prioritizing rare animals & figures!)
    se_fig = [x for x in annotated if x[1] == "SE" and x[7]]  # has_fig == True (no cupmark-only!)
    # Prefer Grafik (946) / Kalkering Plast (944) / Kalkering Papper (955) / Ritning (951) evenly
    se_fig.sort(key=lambda x: (0 if x[8] == 946 else (1 if x[8] == 944 else (2 if x[8] == 955 else 3)), x[0]["id"]))

    motif_quotas: List[Tuple[int, str, int]] = [
        (5043, "elk_moose", 10),
        (5042, "bear", 6),
        (5039, "wolf", 6),
        (4990, "fish", 10),
        (5041, "pig", 8),
        (5040, "dog_canine", 10),
        (5038, "deer", 12),
        (5037, "horse", 12),
        (4992, "bird", 12),
        (5072, "snake", 10),
        (5044, "cattle", 10),
        (5055, "lur_blower", 10),
        (5052, "acrobat_vaulter", 10),
        (5074, "dancer", 6),
        (5045, "archer", 10),
        (5023, "warrior", 10),
        (5025, "rider", 10),
        (5067, "spear_bearer", 8),
        (5076, "axe_bearer", 8),
        (5053, "adorant", 8),
        (5058, "bridal_couple", 5),
        (4994, "plough", 10),
        (5003, "chariot_wagon", 10),
        (5099, "sun_ship", 10),
        (5054, "handprint", 8),
        (4993, "tree", 10),
        (5057, "axe", 8),
        (5048, "spiral", 8),
        (5047, "sun_wheel", 10),
        (4999, "footprint", 10),
        (5077, "labyrinth", 3),
    ]
    m_added = 0
    for kid, label, quota in motif_quotas:
        matches = [x for x in se_fig if kid in x[6]]
        got = 0
        for x in matches:
            if got >= quota:
                break
            if try_add(x[0], country_hint="SE"):
                got += 1
                m_added += 1
    print(f"  Selected SHFA SE motif-stratified B&W candidates: {m_added}")

    # 5. SWEDEN REGIONAL BREADTH (Östergötland, Skåne, Uppland, Småland, Blekinge, diverse Bohuslän parishes)
    region_quotas: List[Tuple[str, int]] = [
        ("östergötland", 16),
        ("skåne", 16),
        ("uppsala", 14),
        ("småland", 12),
        ("blekinge", 5),
        ("tanum", 14),
        ("kville", 10),
        ("svenneby", 8),
        ("bottna", 8),
        ("tossene", 8),
        ("askum", 8),
        ("brastad", 8),
    ]
    r_added = 0
    for token, quota in region_quotas:
        matches = [x for x in se_fig if token in (x[2] + " " + x[3]).lower()]
        got = 0
        for x in matches:
            if got >= quota:
                break
            if try_add(x[0], country_hint="SE"):
                got += 1
                r_added += 1
    print(f"  Selected SHFA SE regional-breadth B&W candidates: {r_added}")

    return list(selected.values())


def select_shfa_rubbing_candidates() -> List[Dict[str, Any]]:
    """Select ~40 curated high-contrast SHFA Frottage rubbings for Norway, Denmark & Stone Age Sweden."""
    plan = [
        # Norway sites where rubbings are the primary SHFA record
        ("Alta", "Finnmark — Alta", "NO", 8, None),
        ("Flora", "Vestland — Flora (Ausevik)", "NO", 3, None),
        ("Fredrikstad", "Østfold — Fredrikstad", "NO", 3, None),
        ("Sarpsborg", "Østfold — Sarpsborg", "NO", 3, None),
        ("Steinkjer", "Trøndelag — Steinkjer (Bardal/Bøla)", "NO", 3, None),
        ("Alstahaug", "Nordland — Alstahaug", "NO", 2, None),
        ("Narvik", "Nordland — Narvik", "NO", 2, None),
        ("Frosta", "Trøndelag — Frosta", "NO", 2, None),
        ("Balsfjord", "Troms — Balsfjord", "NO", 2, None),
        # Denmark Bornholm (Madsebakke/Hammersholm) & Zealand
        ("Bornholm", "Bornholm — Madsebakke/Hammersholm", "DK", 8, ["bronze_age_ship"]),
        ("Odsherred", "Sjælland — Odsherred", "DK", 2, None),
        ("Lejre", "Sjælland — Lejre", "DK", 2, None),
        ("Kalundborg", "Sjælland — Kalundborg", "DK", 2, None),
        # Northern Sweden Stone Age hunter-gatherer sites (Nämforsen, Jämtland Glösa)
        ("Ådals-Liden", "Ångermanland — Nämforsen (Ådals-Liden)", "SE", 5, ["elk_moose"]),
        ("Krokom", "Jämtland — Krokom (Glösa)", "SE", 3, ["elk_moose"]),
        ("Alsen", "Jämtland — Alsen (Glösa)", "SE", 3, ["elk_moose"]),
    ]

    ids_to_fetch: List[Tuple[int, str, str, Optional[List[str]]]] = []
    seen_ids: Set[int] = set()
    for reg_q, reg_lbl, country, quota, extra_hints in plan:
        r = polite_get(
            f"{SHFA_BASE}/api/search/advance/",
            params={"region_name": reg_q, "image_type": "Frottage", "limit": max(20, quota * 3)},
            delay=0.05,
        )
        if not r or not r.ok:
            continue
        got = 0
        for it in r.json().get("results") or []:
            if got >= quota:
                break
            iid = it.get("id")
            if not iid or iid in seen_ids:
                continue
            seen_ids.add(iid)
            ids_to_fetch.append((iid, reg_lbl, country, extra_hints))
            got += 1

    # Fetch depth=1 details in parallel for these ~50 rubbings
    cands: List[Dict[str, Any]] = []

    def fetch_one(args: Tuple[int, str, str, Optional[List[str]]]) -> Optional[Dict[str, Any]]:
        iid, reg_lbl, country, extra_hints = args
        r = polite_get(f"{SHFA_BASE}/api/image/", params={"id": iid, "depth": 1}, delay=0.05)
        if not r or not r.ok:
            return None
        res = r.json().get("results") or []
        if not res:
            return None
        return shfa_record_to_candidate(
            res[0],
            media_type="rubbing_frottage",
            region_hint=reg_lbl,
            country_hint=country,
            extra_hints=extra_hints,
        )

    with ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(fetch_one, ids_to_fetch):
            if res:
                cands.append(res)
    print(f"  Selected SHFA curated rubbing candidates: {len(cands)}")
    return cands


def collect_europeana_candidates() -> List[Dict[str, Any]]:
    cands: List[Dict[str, Any]] = []
    # 1. Vänersborgs museum clean B&W tracings (Fröskog / Dalsland, CC BY-SA 4.0)
    r = polite_get(
        "https://api.europeana.eu/record/v2/search.json",
        params={
            "wskey": "api2demo",
            "query": "avritning AND hällristning",
            "qf": "COUNTRY:Sweden",
            "reusability": "open",
            "media": "true",
            "rows": 30,
        },
        delay=0.1,
    )
    if r and r.ok:
        for it in r.json().get("items") or []:
            providers = " ".join(it.get("dataProvider") or [])
            if any(skip in providers.lower() for skip in ("wikimedia", "national heritage board", "riksantikvarie")):
                continue
            rights_list = it.get("rights") or []
            lic_name, lic_url, comm_ok = normalize_license(rights_list[0] if rights_list else "")
            if not comm_ok:
                continue
            img_url = (it.get("edmIsShownBy") or [None])[0]
            if not img_url:
                continue
            if img_url.startswith("http://mm.dimu.org"):
                img_url = "https://" + img_url[len("http://"):]
            eu_id = (it.get("id") or "").strip("/").replace("/", "_")
            title = (it.get("title") or ["Swedish rock carving tracing"])[0]
            desc = " ".join(it.get("dcDescription") or [title])
            source_url = it.get("guid") or f"https://www.europeana.eu/item{it.get('id', '')}"
            cands.append({
                "source_id": f"europeana_{eu_id}",
                "source_archive": "europeana",
                "title": title,
                "description": f"{desc} (Provider: {providers}).",
                "site": "Fröskog / Dalsland" if "fröskog" in (title + desc).lower() else providers,
                "region": "Dalsland — Västra Götaland" if "fröskog" in (title + desc).lower() else "Sweden",
                "country": "SE",
                "period": "bronze_age_agrarian",
                "media_type": "bw_tracing",
                "motif_hints": ["concentric_circle", "sun_wheel", "bronze_age_ship"],
                "source_url": source_url,
                "download_url": img_url,
                "creator": providers or "Vänersborgs museum",
                "license": lic_name,
                "license_url": lic_url,
                "commercial_ok": comm_ok,
            })

    # 2. Bohuslän Museum (PDM 1.0) & Upplands Museum (CC0) painted carvings
    for provider, region_lbl, quota in [
        ("Bohuslän Museum", "Bohuslän — Västra Götaland", 6),
        ("Upplands Museum", "Uppland — Uppsala län", 4),
    ]:
        r = polite_get(
            "https://api.europeana.eu/record/v2/search.json",
            params={
                "wskey": "api2demo",
                "query": "hällristning",
                "qf": f'DATA_PROVIDER:"{provider}"',
                "reusability": "open",
                "media": "true",
                "rows": 30,
            },
            delay=0.1,
        )
        if not r or not r.ok:
            continue
        got = 0
        for it in r.json().get("items") or []:
            if got >= quota:
                break
            rights_list = it.get("rights") or []
            lic_name, lic_url, comm_ok = normalize_license(rights_list[0] if rights_list else "")
            if not comm_ok:
                continue
            img_url = (it.get("edmIsShownBy") or [None])[0]
            if not img_url or "dimu.org" not in img_url:
                continue
            if img_url.startswith("http://"):
                img_url = "https://" + img_url[len("http://"):]
            eu_id = (it.get("id") or "").strip("/").replace("/", "_")
            title = (it.get("title") or ["Hällristning"])[0]
            desc = " ".join(it.get("dcDescription") or [title])
            if any(skip in (title + " " + desc).lower() for skip in ("tavelgalleri", "museet", "utställning", "skylt", "porträtt")):
                continue
            source_url = it.get("guid") or f"https://www.europeana.eu/item{it.get('id', '')}"
            cands.append({
                "source_id": f"europeana_{eu_id}",
                "source_archive": "europeana",
                "title": title,
                "description": f"{desc} (Provider: {provider}).",
                "site": title[:80],
                "region": region_lbl,
                "country": "SE",
                "period": "bronze_age_agrarian",
                "media_type": "painted_photo",
                "motif_hints": ["bronze_age_ship", "anthropomorph"],
                "source_url": source_url,
                "download_url": img_url,
                "creator": provider,
                "license": lic_name,
                "license_url": lic_url,
                "commercial_ok": comm_ok,
            })
            got += 1
    print(f"  Collected Europeana candidates: {len(cands)}")
    return cands


def collect_ksamsok_candidates() -> List[Dict[str, Any]]:
    cands: List[Dict[str, Any]] = []
    lic_clause = (
        '(mediaLicense="http://kulturarvsdata.se/resurser/License#cc0" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#pdm" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#by" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#by-sa")'
    )
    queries = [
        (f'text="hällristning" AND (text="kopparstick" OR text="teckning" OR text="avritning" OR text="kalkering") AND {lic_clause} AND thumbnailExists=j', "bw_tracing", 8),
        (f'text="hällristning" AND (text="älgar" OR text="Evenhus" OR text="Sandstenshäll") AND {lic_clause} AND thumbnailExists=j', "photo_other", 6),
    ]
    for q, default_media, quota in queries:
        r = polite_get(
            "https://kulturarvsdata.se/ksamsok/api",
            params={"method": "search", "recordSchema": "presentation", "hitsPerPage": 25, "query": q},
            delay=0.1,
            extra_headers={"Accept": "application/json"},
        )
        if not r or not r.ok:
            continue
        try:
            payload = r.json()
        except ValueError:
            print(f"  WARN: K-samsök returned non-JSON ({r.headers.get('content-type')})")
            continue
        recs = payload.get("result", {}).get("records", {})
        if isinstance(recs, dict):
            recs = recs.get("record", [])
        if isinstance(recs, dict):
            recs = [recs]
        got = 0
        for rec in recs:
            if got >= quota:
                break
            item = rec.get("pres:item") or rec.get("record", {}).get("pres:item") or {}
            if not item:
                continue
            org = item.get("pres:organization")
            org = str(org.get("content", "") if isinstance(org, dict) else (org or "")).strip()
            lbl = item.get("pres:itemLabel")
            lbl = str(lbl.get("content", "") if isinstance(lbl, dict) else (lbl or "")).strip()
            desc = item.get("pres:description")
            desc = str(desc.get("content", "") if isinstance(desc, dict) else (desc or "")).strip()
            combo = f"{lbl} {desc} {org}".lower()
            if any(bad in combo for bad in ("tavelgalleri", "bogota", "världskultur", "publikation", "etnografiska", "souvenir")):
                continue
            imgs = item.get("pres:image")
            if isinstance(imgs, dict):
                imgs = [imgs]
            if not isinstance(imgs, list) or not imgs:
                continue
            im0 = imgs[0]
            lic_name, lic_url, comm_ok = normalize_license(str(im0.get("pres:mediaLicense") or ""))
            if not comm_ok:
                continue
            srcs = im0.get("pres:src", [])
            if isinstance(srcs, dict):
                srcs = [srcs]
            img_url = None
            for pref in ("highres", "lowres"):
                for s in srcs:
                    if isinstance(s, dict) and s.get("type") == pref and s.get("content"):
                        img_url = s["content"]
                        break
                if img_url:
                    break
            if not img_url:
                continue
            if "dimu.org/image/" in img_url and "dimension=600x600" in img_url:
                img_url = img_url.replace("dimension=600x600", "dimension=1200x1200")
            entity_id = str(item.get("pres:id") or item.get("rdf:about") or hashlib.md5(img_url.encode()).hexdigest()[:12])
            reps = item.get("pres:representations", {}).get("pres:representation", [])
            if isinstance(reps, dict):
                reps = [reps]
            web_url = next((x.get("content") for x in reps if isinstance(x, dict) and str(x.get("content", "")).startswith("http")), "")
            if not web_url:
                web_url = entity_id if entity_id.startswith("http") else f"https://kulturarvsdata.se/{entity_id}"

            country = "NO" if "evenhus" in combo else "SE"
            period = "stone_age_hunter" if ("evenhus" in combo or "älg" in combo or "nämforsen" in combo) else "bronze_age_agrarian"
            hints = []
            if "skepp" in combo or "båt" in combo:
                hints.append("bronze_age_ship" if period == "bronze_age_agrarian" else "stone_age_boat")
            if "älg" in combo:
                hints.append("elk_moose")
            if "tumlare" in combo or "delfin" in combo:
                hints.append("whale_cetacean")
            if "människ" in combo:
                hints.append("anthropomorph")
            if "kopparstick" in combo:
                hints.extend(["bronze_age_ship", "horse", "footprint", "sun_wheel", "warrior"])
            if not hints:
                hints = ["bronze_age_ship"]
            mtype = "bw_tracing" if any(k in combo for k in ("kopparstick", "teckning", "avritning", "kalkering")) else default_media
            cands.append({
                "source_id": f"ksamsok_{re.sub(r'[^a-zA-Z0-9]+', '_', entity_id)[-32:]}",
                "source_archive": "ksamsok",
                "title": lbl or f"K-samsök {org}",
                "description": desc or f"{lbl} ({org}).",
                "site": "Evenhus (Frosta)" if "evenhus" in combo else (lbl or org),
                "region": "Trøndelag" if country == "NO" else org,
                "country": country,
                "period": period,
                "media_type": mtype,
                "motif_hints": list(dict.fromkeys(hints)),
                "source_url": str(web_url),
                "download_url": img_url,
                "creator": org or "K-samsök / SOCH",
                "license": lic_name,
                "license_url": lic_url,
                "commercial_ok": comm_ok,
            })
            got += 1
    print(f"  Collected K-samsök candidates: {len(cands)}")
    return cands


def collect_natmus_dk_candidates() -> List[Dict[str, Any]]:
    targets = [
        ("/do/asset/1291", "Helleristningssten fra Engelstrup", "Bronze Age rock carving stone from Engelstrup (Odsherred, Sjælland) depicting a red-painted crewed ship, sun circle, and human figures.", "Engelstrup (Odsherred)", "Sjælland — Odsherred", "DK", "bronze_age_agrarian", "painted_photo", ["bronze_age_ship", "anthropomorph", "sun_wheel"]),
        ("/do/asset/10706", "Helleristningssten, Engelstrup (alt view)", "Bronze Age rock carving stone from Engelstrup (Odsherred, Sjælland) with red-painted ship and human figures.", "Engelstrup (Odsherred)", "Sjælland — Odsherred", "DK", "bronze_age_agrarian", "painted_photo", ["bronze_age_ship", "anthropomorph", "sun_wheel"]),
        ("/do/asset/799", "4 Helleristningssten med håndtegn fra Sandagergård", "Four Danish Bronze Age rock carving stones with hand-sign motifs from Sandagergård, Sjælland.", "Sandagergård", "Sjælland", "DK", "bronze_age_agrarian", "photo_other", ["handprint"]),
        ("/do/asset/1033", "Figurfremstilling fra Bjergagergård", "Danish Bronze Age rock carving figure representation from Bjergagergård.", "Bjergagergård", "Denmark", "DK", "bronze_age_agrarian", "photo_other", ["anthropomorph"]),
        ("/do/asset/1126", "Billedsten fra Kivik", "Bronze Age carved stone slab from Kivik (Scania/historical Danish collections, Nationalmuseet).", "Kivik", "Skåne", "SE", "bronze_age_agrarian", "photo_other", ["bronze_age_ship", "chariot_wagon", "sun_wheel", "horse"]),
    ]
    cands: List[Dict[str, Any]] = []
    for path, title, desc, site, region, country, period, mtype, hints in targets:
        page_url = f"https://samlinger.natmus.dk{path}"
        pr = polite_get(page_url, delay=0.15)
        if not pr or not pr.ok:
            continue
        m_lic = re.search(r"https?://creativecommons\.org/licenses/[^\s\"'<>]+", pr.text, re.I)
        if not m_lic:
            continue
        lic_name, lic_url, comm_ok = normalize_license(m_lic.group(0))
        if not comm_ok:
            continue
        asset_num = path.rsplit("/", 1)[-1]
        cands.append({
            "source_id": f"natmus_dk_{asset_num}",
            "source_archive": "nationalmuseet",
            "title": title,
            "description": desc,
            "site": site,
            "region": region,
            "country": country,
            "period": period,
            "media_type": mtype,
            "motif_hints": hints,
            "source_url": page_url,
            "download_url": f"{page_url}.jpg",
            "creator": "Nationalmuseet (Danmark)",
            "license": lic_name,
            "license_url": lic_url,
            "commercial_ok": comm_ok,
        })
    print(f"  Collected Nationalmuseet Denmark candidates: {len(cands)}")
    return cands


def download_and_validate(cand: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]], str]:
    """Download (or reuse valid existing file), QA-check pixels, write atomically, return (status, record, reason)."""
    sid = cand["source_id"]
    safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", sid)

    # Check if valid non-empty file already exists from a prior run
    existing = [p for p in RAW_DIR.glob(f"{safe_id}.*") if p.suffix != ".tmp" and p.stat().st_size > 2048]
    raw_bytes: Optional[bytes] = None
    filename: Optional[str] = None
    if existing:
        raw_bytes = existing[0].read_bytes()
        filename = existing[0].name
    else:
        resp = polite_get(cand["download_url"], timeout=35, delay=0.08)
        if resp is None or resp.status_code != 200:
            code = resp.status_code if resp is not None else "net_err"
            return "reject", None, f"http_{code}"
        raw_bytes = resp.content
        ext = ext_from_content_type_and_url(resp.headers.get("content-type", ""), cand["download_url"])
        filename = f"{safe_id}{ext}"

    ok, w, h, reason = analyze_image_bytes(raw_bytes, cand["media_type"])
    if not ok:
        if existing:
            existing[0].unlink(missing_ok=True)
        return "reject", None, f"qa_{reason.split('(')[0]}"

    sha = hashlib.sha256(raw_bytes).hexdigest()
    out_path = RAW_DIR / filename
    if not out_path.exists() or out_path.stat().st_size != len(raw_bytes):
        tmp_path = RAW_DIR / f"{filename}.tmp"
        tmp_path.write_bytes(raw_bytes)
        os.replace(tmp_path, out_path)

    rec = {
        "source_id": sid,
        "source_archive": cand["source_archive"],
        "title": cand["title"].strip(),
        "description": cand["description"].strip(),
        "site": cand["site"].strip(),
        "region": cand["region"].strip(),
        "country": cand["country"],
        "period": cand["period"],
        "media_type": cand["media_type"],
        "motif_hints": cand["motif_hints"],
        "raw_path": f"sources/nordic_archives/raw/{filename}",
        "source_url": cand["source_url"],
        "download_url": cand["download_url"],
        "creator": cand["creator"].strip() or "Unknown",
        "license": cand["license"],
        "license_url": cand["license_url"],
        "commercial_ok": bool(cand["commercial_ok"]),
        "sha256": sha,
        "width_px": int(w),
        "height_px": int(h),
    }
    return "ok", rec, reason


def main() -> int:
    t0 = time.time()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Remove any 0-byte or .tmp remnants before running
    for p in list(RAW_DIR.iterdir()):
        if p.is_file() and (p.suffix == ".tmp" or p.stat().st_size == 0):
            p.unlink(missing_ok=True)
    if "--clean" in sys.argv:
        for p in list(RAW_DIR.iterdir()):
            if p.is_file():
                p.unlink(missing_ok=True)

    print("[Phase 1] Collecting metadata candidates across 4 Nordic open archives...")
    shfa_bw_all = fetch_all_shfa_bw_catalog()
    candidates: List[Dict[str, Any]] = []
    candidates.extend(select_shfa_bw_candidates(shfa_bw_all))
    candidates.extend(select_shfa_rubbing_candidates())
    candidates.extend(collect_europeana_candidates())
    candidates.extend(collect_ksamsok_candidates())
    candidates.extend(collect_natmus_dk_candidates())

    # Deduplicate candidates by source_id preserving priority order
    uniq_cands: List[Dict[str, Any]] = []
    seen_sids: Set[str] = set()
    for c in candidates:
        if c["source_id"] not in seen_sids and c.get("commercial_ok"):
            seen_sids.add(c["source_id"])
            uniq_cands.append(c)

    print(f"\n[Phase 2] Downloading & pixel-QA validating {len(uniq_cands)} candidates (6 polite workers)...")
    records: List[Dict[str, Any]] = []
    seen_sha: Set[str] = set()
    rejects: Counter = Counter()
    done = 0

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(download_and_validate, c) for c in uniq_cands]
        for fut in as_completed(futs):
            done += 1
            status, rec, reason = fut.result()
            if status != "ok" or rec is None:
                rejects[reason] += 1
            else:
                if rec["sha256"] in seen_sha:
                    # Remove duplicate file on disk so raw/ has 1:1 parity with manifest.jsonl
                    dup_file = BASE_DIR.parent.parent / rec["raw_path"]
                    dup_file.unlink(missing_ok=True)
                    rejects["dup_sha256"] += 1
                else:
                    seen_sha.add(rec["sha256"])
                    records.append(rec)
            if done % 40 == 0 or done == len(uniq_cands):
                print(f"  Progress: {done}/{len(uniq_cands)} | kept={len(records)} | rejected={sum(rejects.values())} ({time.time()-t0:.1f}s)")

    # Sort deterministically by (source_archive, country, media_type, source_id)
    records.sort(key=lambda r: (r["source_archive"], r["country"], r["media_type"], r["source_id"]))

    # Prune any stray files in raw/ not referenced in manifest.jsonl
    referenced_names = {Path(r["raw_path"]).name for r in records}
    for p in list(RAW_DIR.iterdir()):
        if p.is_file() and p.name not in referenced_names:
            p.unlink(missing_ok=True)

    # Atomic write of manifest.jsonl
    tmp_manifest = BASE_DIR / "manifest.jsonl.tmp"
    with tmp_manifest.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp_manifest, MANIFEST_PATH)

    print("\n" + "=" * 70)
    print(f"HARVEST COMPLETE in {time.time()-t0:.1f}s — {len(records)} verified unique Nordic assets")
    print("=" * 70)
    for label, key in [
        ("By archive", "source_archive"),
        ("By media_type", "media_type"),
        ("By country", "country"),
        ("By period", "period"),
        ("By license", "license"),
    ]:
        print(f"  {label:16s}: {dict(Counter(r[key] for r in records))}")
    motif_cnt = Counter(h for r in records for h in r["motif_hints"])
    print(f"  Top motifs      : {motif_cnt.most_common(22)}")
    print(f"  Rejects/filtered: {dict(rejects)}")
    print(f"  Manifest path   : {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
