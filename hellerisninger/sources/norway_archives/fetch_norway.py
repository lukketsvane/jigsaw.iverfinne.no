#!/usr/bin/env python3
"""
Norwegian Rock-Art (Helleristninger / Bergkunst) Archive Harvester
==================================================================
Harvests legally documented, openly reusable Norwegian rock-art tracings
(kalkeringer / dokumentasjonstegninger) and crisp close-up motif images
from Norwegian cultural heritage APIs into:
  hellerisninger/sources/norway_archives/raw/
  hellerisninger/sources/norway_archives/manifest.jsonl

Sources queried:
  1. Alta Rock Art Archive (altamuseum.fotoware.cloud, Archive 5009-Kalkeringer)
     - License: Alta Museum Open Non-Commercial ShareAlike (CC BY-NC-SA-equivalent)
     - commercial_ok: False
  2. Unimus — Universitetsmuseenes fotoportal (api.unimus.no/public-data-api)
     - Museums with open CC BY-SA 4.0 license: KHM (Oslo), UM (Bergen), VM (NTNU)
     - Strictly skips CC BY-NC-ND 3.0 (AM Stavanger, UMAK Tromsø)
     - commercial_ok: True
  3. Kulturminnesøk / Riksantikvaren (api.ra.no/brukerminner)
     - Strictly filters to CC0, CC BY, CC BY-SA
     - commercial_ok: True
  4. Nasjonalbiblioteket (api.nb.no/catalog/v1/items + IIIF image resolver)
     - Strictly filters to isPublicDomain == True & accessAllowedFrom == EVERYWHERE
     - commercial_ok: True
  5. DigitaltMuseum Norway via Europeana Open API (api.europeana.eu)
     - Strictly filters to reusability=open (PDM / CC0 / CC BY / CC BY-SA)
     - commercial_ok: True

Usage:
  python fetch_norway.py
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlparse

import requests
from PIL import Image

BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "raw"
MANIFEST_PATH = BASE_DIR / "manifest.jsonl"
RAW_REL_PREFIX = "sources/norway_archives/raw"

USER_AGENT = (
    "HelleristningerResearchBot/1.0 "
    "(Norwegian rock-art silhouette research; jigsaw.iverfinne.no; polite rate-limited)"
)

HOST_MIN_INTERVAL = 0.08
_host_locks: Dict[str, threading.Lock] = {}
_host_last: Dict[str, float] = {}
_meta_lock = threading.Lock()
_sha_lock = threading.Lock()
_tls = threading.local()

# Balanced, high-signal, disk-conscious caps (~320-380 verified files)
MAX_ALTA_UNNUMBERED = 135
MAX_ALTA_NUMBERED_FILL = 15
MAX_UNIMUS_UM_KALK = 55
MAX_UNIMUS_KHM_KALK = 75
MAX_UNIMUS_KHM_TEGNING = 25
MAX_UNIMUS_VM_TRACINGS = 15
MAX_UNIMUS_VM_CLOSEUPS = 18
MAX_RA_OPEN = 20
MAX_NB_PD = 25
MAX_DIMU_EUROPEANA = 15

MIN_IMAGE_DIM = 350
MAX_SINGLE_FILE_BYTES = 12 * 1024 * 1024  # 12 MB cap per file
DISK_MIN_FREE_BYTES = 500 * 1024 * 1024   # Always keep >= 500 MB free
DOWNLOAD_WALL_TIMEOUT = 20.0              # Max total seconds per single image download
Image.MAX_IMAGE_PIXELS = 250_000_000      # Allow archival high-DPI museum tracing scans


def get_session() -> requests.Session:
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT})
        _tls.session = s
    return s


def polite_wait(url: str) -> None:
    host = urlparse(url).netloc
    min_gap = 0.25 if "unimus.no" in host else HOST_MIN_INTERVAL
    with _meta_lock:
        if host not in _host_locks:
            _host_locks[host] = threading.Lock()
            _host_last[host] = 0.0
        lock = _host_locks[host]
    with lock:
        now = time.monotonic()
        elapsed = now - _host_last[host]
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        _host_last[host] = time.monotonic()


def http_get(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: Tuple[float, float] = (6.0, 12.0),
    retries: int = 1,
    stream: bool = False,
) -> Optional[requests.Response]:
    sess = get_session()
    for attempt in range(retries + 1):
        polite_wait(url)
        try:
            r = sess.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout,
                allow_redirects=True,
                stream=stream,
            )
            if r.status_code == 200:
                return r
            r.close()
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.2 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
            else:
                return None
    return None


def fetch_bytes_bounded(url: str) -> Optional[bytes]:
    """Download bytes with strict wall-clock and max-size guards."""
    r = http_get(url, timeout=(6.0, 10.0), retries=1, stream=True)
    if not r:
        return None
    t0 = time.monotonic()
    buf = bytearray()
    try:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                buf.extend(chunk)
            if len(buf) > MAX_SINGLE_FILE_BYTES or (time.monotonic() - t0) > DOWNLOAD_WALL_TIMEOUT:
                r.close()
                return None
    except Exception:
        r.close()
        return None
    finally:
        r.close()
    if len(buf) < 4096:
        return None
    return bytes(buf)


def normalize_region(county: Optional[str], text: str = "") -> str:
    c = (county or "").strip().lower()
    t = (text or "").lower()
    mapping = {
        "finnmark": "Finnmark",
        "troms": "Troms",
        "troms og finnmark": "Finnmark" if "alta" in t else "Troms",
        "nordland": "Nordland",
        "narvik": "Nordland",
        "trøndelag": "Trøndelag",
        "nord-trøndelag": "Trøndelag",
        "sør-trøndelag": "Trøndelag",
        "sør trøndelag": "Trøndelag",
        "møre og romsdal": "Møre og Romsdal",
        "vestland": "Vestland",
        "sogn og fjordane": "Vestland",
        "hordaland": "Vestland",
        "rogaland": "Rogaland",
        "agder": "Agder",
        "vest-agder": "Agder",
        "aust-agder": "Agder",
        "telemark": "Telemark",
        "vestfold": "Vestfold",
        "vestfold og telemark": "Telemark" if any(x in t for x in ["skien", "gjerpen", "telemark"]) else "Vestfold",
        "buskerud": "Buskerud",
        "østfold": "Østfold",
        "akershus": "Akershus",
        "oslo": "Oslo",
        "innlandet": "Innlandet",
        "oppland": "Innlandet",
        "hedmark": "Innlandet",
        "viken": "Østfold" if any(x in t for x in ["sarpsborg", "fredrikstad", "skjeberg", "begby", "kalnes", "solberg", "borge", "halden", "moss"]) else "Buskerud",
    }
    if c in mapping:
        return mapping[c]
    site_regions = [
        (["alta", "hjemmeluft", "amtmannsnes", "kåfjord", "transfarelv", "storsteinen", "isnestoften", "bossekop", "kråknes", "kongshus", "årøya"], "Finnmark"),
        (["skavberg", "tromsø", "kvaløya", "balsfjord", "tømmernes"], "Troms"),
        (["leiknes", "rødøy", "skjomen", "forsheim", "narvik", "vistnesdalen", "vevelstad", "vågan", "klubba"], "Nordland"),
        (["bøla", "bardal", "leirfall", "hegra", "hell,", "felt hell", "hell1", "evenhus", "holtås", "stuberg", "hommelvik", "tessem", "steinkjer", "stjørdal", "frosta", "levanger", "verdal", "malvik", "selbu", "namsskogan", "hammer"], "Trøndelag"),
        (["bogge", "honnhammer", "nesset", "sykkylven", "herøy", "tingvoll", "norddal"], "Møre og Romsdal"),
        (["vingen", "vingelva", "bremanger", "ausevik", "flora", "kinn", "etne", "kårstad", "stryn", "eidfjord", "ullensvang", "vik i sogn", "hardanger"], "Vestland"),
        (["åmøy", "revheim", "fluberget", "aubeberget", "sola", "stavanger", "bokn", "sandnes", "hafrsfjord", "solbakk", "utstein", "rogaland"], "Rogaland"),
        (["begby", "solberg", "kalnes", "skjeberg", "hornnes", "bjørnstad", "hafslund", "rolvsøy", "borge", "kråkerøy", "fredrikstad", "sarpsborg", "tune", "alvim", "moss", "halden", "råde", "rygge", "østfold"], "Østfold"),
        (["sandefjord", "tjølling", "larvik", "haugen", "tønsberg", "horten", "vestfold"], "Vestfold"),
        (["skien", "gjerpen", "fossum", "telemark", "bø", "notodden"], "Telemark"),
        (["drammen", "åskollen", "skoger", "lier", "buskerud"], "Buskerud"),
        (["møllerstufossen", "nordre land", "ringsaker", "stein gård", "øyer", "slidre", "dokkfløy", "hedmark", "oppland"], "Innlandet"),
        (["bærum", "ekeberg", "oslo", "akershus", "asker"], "Akershus"),
        (["kristiansand", "grimstad", "lista", "farsund", "agder"], "Agder"),
    ]
    for keys, reg in site_regions:
        if any(k in t for k in keys):
            return reg
    return "Norway (unspecified)"


def infer_period(text: str, region: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ["bardal", "hell ", "hell,", "felt hell"]):
        return "mixed"
    if any(k in t for k in [
        "veiderist", "steinalder", "alta", "hjemmeluft", "amtmannsnes", "kåfjord",
        "transfarelv", "storsteinen", "isnestoften", "bossekop", "kråknes", "bergbukten",
        "ole pedersen", "apana gård", "bergheim", "vingen", "vingelva", "ausevik",
        "bøla", "bølarein", "leiknes", "rødøy", "forsheim", "skjomen", "skavberg",
        "møllerstufossen", "nordre land", "vistnesdalen", "evenhus", "bogge", "holtås",
        "ekeberg", "honnhammer", "strand ", "kvernavika", "isbjørn"
    ]):
        return "stone_age_hunter"
    if any(k in t for k in [
        "jordbruksrist", "bronsealder", "begby", "solberg", "kalnes", "skjeberg",
        "hornnes", "bjørnstad", "hafslund", "rolvsøy", "borge", "kråkerøy", "tune",
        "alvim", "fredrikstad", "sarpsborg", "sandefjord", "tjølling", "haugen",
        "leirfall", "hegra", "stuberg", "hommelvik", "tessem", "etne", "kårstad",
        "åmøy", "revheim", "fluberget", "aubeberget", "gjerpen", "skien", "bærum",
        "skipsrist", "solhjul", "fotsåle", "ringsaker", "stein gård", "eidfjord", "drammen"
    ]):
        return "bronze_age_agrarian"
    if region in ("Finnmark", "Troms", "Nordland"):
        return "stone_age_hunter"
    if region in ("Østfold", "Vestfold", "Rogaland", "Telemark", "Akershus"):
        return "bronze_age_agrarian"
    return "unknown"


def extract_motif_hints(text: str, tags: Optional[List[str]] = None) -> List[str]:
    blob = ((text or "") + " " + " ".join(tags or [])).lower()
    rules = [
        ("reindeer", ["rein", "reindeer", "bølarein", "hjortedyr"]),
        ("elk", ["elg", "elk", "moose"]),
        ("deer", ["hjort", "red deer", "vingen", "ausevik"]),
        ("bear", ["bjørn", "bear", "isbjørn"]),
        ("whale", ["hval", "kval", "whale", "nise", "spekkhogger", "delfin"]),
        ("halibut", ["kveite", "halibut", "flyndre"]),
        ("fish", ["fisk", "laks", "fish", "salmon"]),
        ("bird", ["fugl", "svane", "and", "gås", "skagle", "bird", "swan"]),
        ("horse", ["hest", "rytter", "horse", "rider"]),
        ("dog", ["hund", "ulv", "rev", "dog", "wolf"]),
        ("snake", ["slange", "orm", "snake"]),
        ("animal", ["dyr", "dyrefigur", "zoomorf", "quadruped", "animal"]),
        ("ship", ["skip", "skipsfigur", "skipsrist", "ship"]),
        ("boat", ["båt", "båtfigur", "boat", "umiak", "kano"]),
        ("skier", ["skiløper", "ski", "skier", "truge", "snowshoe"]),
        ("shaman", ["sjaman", "shaman", "tromme", "drum", "elghodestav"]),
        ("human", ["menneske", "antropomorf", "mann", "kvinne", "kriger", "jeger", "human", "person"]),
        ("corral", ["innhegning", "inngjerding", "fangstgjerde", "corral", "gjerde"]),
        ("sun_wheel", ["solhjul", "hjulkors", "solkors", "ringfigur", "konsentrisk", "sun", "wheel"]),
        ("spiral", ["spiral", "labyrint", "sirkel"]),
        ("footprint", ["fotsåle", "fotspor", "footprint", "bjørnespor"]),
        ("weapon", ["øks", "spyd", "bue", "pil", "sverd", "våpen", "axe", "bow", "spear"]),
        ("chariot", ["vogn", "plog", "chariot", "plough"]),
        ("cup_mark", ["skålgrop", "cup"]),
        ("geometric", ["geometrisk", "rammefigur", "mønster", "sikk-sakk", "zigzag", "saksefigur"]),
    ]
    hints = []
    for label, kws in rules:
        if any(kw in blob for kw in kws):
            hints.append(label)
    if not hints:
        if any(x in blob for x in ["alta", "hjemmeluft", "amtmannsnes", "kåfjord", "bergbukten", "ole pedersen", "apana", "bergheim", "transfarelv", "storsteinen"]):
            hints = ["reindeer", "boat", "human", "animal"]
        elif any(x in blob for x in ["vingen", "vingelva", "ausevik"]):
            hints = ["deer", "geometric", "human"]
        elif any(x in blob for x in ["østfold", "begby", "solberg", "kalnes", "skjeberg", "vestfold", "borge", "rolvsøy", "sarpsborg", "fredrikstad"]):
            hints = ["ship", "human", "sun_wheel"]
        else:
            hints = ["rock_carving"]
    return hints[:8]


def safe_slug(s: str, max_len: int = 44) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", s.strip())
    s = re.sub(r"_+", "_", s).strip("_")
    return (s[:max_len] or "item").lower()


def index_existing_raw() -> Dict[str, Path]:
    """Index existing files in RAW_DIR by stem prefix (before _<8hex>) for fast resumability."""
    existing: Dict[str, Path] = {}
    if RAW_DIR.exists():
        for p in RAW_DIR.iterdir():
            if p.is_file() and p.stat().st_size >= 4096:
                m = re.match(r"^(.+)_[0-9a-f]{8}$", p.stem)
                if m:
                    existing[m.group(1)] = p
    return existing


def download_or_reuse_image(
    url: str,
    dest_stem: str,
    seen_sha256: Set[str],
    existing_raw: Dict[str, Path],
    min_dim: int = MIN_IMAGE_DIM,
    fallback_url: Optional[str] = None,
) -> Optional[Tuple[str, str, int, int, str]]:
    """
    Reuse already-downloaded valid file matching dest_stem, or download with strict bounds,
    verify via PIL, deduplicate by SHA-256, and save immutably into RAW_DIR.
    """
    # Fast local reuse if already downloaded
    if dest_stem in existing_raw:
        p = existing_raw[dest_stem]
        try:
            data = p.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
                im.verify()
            if w >= min_dim and h >= min_dim:
                with _sha_lock:
                    if sha in seen_sha256:
                        return None
                    seen_sha256.add(sha)
                return (f"{RAW_REL_PREFIX}/{p.name}", sha, int(w), int(h), url)
        except Exception:
            pass

    if shutil.disk_usage(RAW_DIR).free < DISK_MIN_FREE_BYTES:
        print("  [STOP] Free disk space dropped below 500 MB safety guard!", file=sys.stderr)
        return None

    chosen_url = None
    data = None
    w = h = 0
    fmt = "JPEG"
    for cand_url in ([url, fallback_url] if fallback_url else [url]):
        cand_data = fetch_bytes_bounded(cand_url)
        if cand_data is None:
            continue
        try:
            with Image.open(io.BytesIO(cand_data)) as im:
                fmt = (im.format or "JPEG").upper()
                w, h = im.size
                im.verify()
            if w >= min_dim and h >= min_dim:
                chosen_url = cand_url
                data = cand_data
                break
        except Exception:
            continue

    if data is None or chosen_url is None:
        return None

    sha = hashlib.sha256(data).hexdigest()

    with _sha_lock:
        if sha in seen_sha256:
            return None
        seen_sha256.add(sha)

    ext_map = {"JPEG": ".jpg", "JPG": ".jpg", "PNG": ".png", "TIFF": ".tif", "WEBP": ".webp"}
    ext = ext_map.get(fmt, ".jpg")
    filename = f"{dest_stem}_{sha[:8]}{ext}"
    out_path = RAW_DIR / filename
    out_path.write_bytes(data)
    return (f"{RAW_REL_PREFIX}/{filename}", sha, int(w), int(h), chosen_url)


# =============================================================================
# 1. ALTA ROCK ART ARCHIVE (altamuseum.fotoware.cloud, 5009-Kalkeringer)
# =============================================================================
def harvest_alta_kalkeringer(seen_sha256: Set[str], existing_raw: Dict[str, Path]) -> List[Dict[str, Any]]:
    print("\n[1/5] Harvesting Alta Rock Art Archive (5009-Kalkeringer)...", flush=True)
    base = "https://altamuseum.fotoware.cloud"
    url: Optional[str] = f"{base}/fotoweb/archives/5009-Kalkeringer/"
    hdr_list = {"Accept": "application/vnd.fotoware.assetlist+json, application/json"}
    hdr_asset = {"Accept": "application/vnd.fotoware.asset+json, application/json"}

    all_assets: List[Dict[str, Any]] = []
    seen_pages: Set[str] = set()
    while url and url not in seen_pages and len(seen_pages) < 30:
        seen_pages.add(url)
        r = http_get(url, headers=hdr_list)
        if not r:
            break
        ad = r.json()
        all_assets.extend(ad.get("data", []))
        nxt = ad.get("paging", {}).get("next")
        url = (base + nxt) if nxt else None

    print(f"  Enumerated {len(all_assets)} assets in Alta 5009-Kalkeringer", flush=True)

    unnumbered: List[Dict[str, Any]] = []
    numbered: List[Dict[str, Any]] = []
    unnumbered_panels: Set[str] = set()

    for a in all_assets:
        md = a.get("metadata", {})
        lic_text = str(md.get("40", {}).get("value", ""))
        if "kan nedlastes og brukes fritt til ikke-kommersielle formål" not in lic_text.lower():
            continue
        bf = {x["field"]: x["value"] for x in a.get("builtinFields", [])}
        tags = bf.get("tags") or []
        title = str(bf.get("title") or a.get("filename") or "")
        panel = str(md.get("92", {}).get("value") or md.get("82", {}).get("value") or "").strip().lower()
        is_numbered = ("Figurnummer/seksjoner" in tags) or ("nummer" in title.lower())
        if is_numbered:
            numbered.append(a)
        else:
            unnumbered.append(a)
            if panel:
                unnumbered_panels.add(panel)

    selected = list(unnumbered[:MAX_ALTA_UNNUMBERED])
    added_num = 0
    for a in numbered:
        if added_num >= MAX_ALTA_NUMBERED_FILL:
            break
        md = a.get("metadata", {})
        panel = str(md.get("92", {}).get("value") or "").strip().lower()
        if panel and panel not in unnumbered_panels:
            selected.append(a)
            unnumbered_panels.add(panel)
            added_num += 1

    print(f"  Selected {len(selected)} Alta tracings ({min(len(unnumbered), MAX_ALTA_UNNUMBERED)} unnumbered + {added_num} extra panels)", flush=True)

    def fetch_one_alta(idx_asset: Tuple[int, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        idx, a = idx_asset
        ahref = a.get("href")
        if not ahref:
            return None
        ra = http_get(base + ahref, headers=hdr_asset)
        if not ra:
            return None
        ad = ra.json()
        pvs = [p for p in ad.get("previews", []) if not p.get("square") and p.get("href")]
        if not pvs:
            return None
        best = max(pvs, key=lambda p: (p.get("width", 0), p.get("height", 0)))
        dl_url = base + best["href"]

        md = ad.get("metadata", {})
        bf = {x["field"]: x["value"] for x in ad.get("builtinFields", [])}
        fname = ad.get("filename") or f"alta_{idx}"
        title = str(bf.get("title") or md.get("5", {}).get("value") or fname).strip()
        desc = str(bf.get("description") or md.get("120", {}).get("value") or "").strip()
        tags = bf.get("tags") or []
        site_area = str(md.get("82", {}).get("value") or "Alta").strip()
        sub_panel = str(md.get("92", {}).get("value") or "").strip()
        site = f"{site_area} — {sub_panel}" if (sub_panel and sub_panel.lower() not in site_area.lower()) else site_area
        creators = md.get("80", {}).get("value") or []
        if isinstance(creators, list):
            creator_str = ", ".join(str(c).strip() for c in creators if str(c).strip())
        else:
            creator_str = str(creators).strip()
        inst = str(md.get("116", {}).get("value") or "Verdensarvsenter for Bergkunst - Alta Museum").strip()
        creator = f"{creator_str} ({inst})" if creator_str else inst

        uid = ad.get("uniqueid") or safe_slug(fname)
        stem = f"alta_{safe_slug(Path(fname).stem, 30)}"
        res = download_or_reuse_image(dl_url, stem, seen_sha256, existing_raw, min_dim=400)
        if not res:
            return None
        raw_rel, sha, w, h, actual_dl = res

        source_url = f"{base}/fotoweb/archives/5009-Kalkeringer/?q={quote(fname)}"
        full_desc = desc or f"Digital rock-art tracing (kalkering) from Alta Rock Art Archive: {title} ({site}). Tags: {', '.join(tags)}."
        return {
            "source_id": f"altarockart:{uid}",
            "source_archive": "altarockart",
            "title": title,
            "description": full_desc,
            "site": site,
            "region": "Finnmark",
            "country": "NO",
            "period": "stone_age_hunter",
            "media_type": "bw_tracing",
            "motif_hints": extract_motif_hints(f"{title} {desc} {site}", tags),
            "raw_path": raw_rel,
            "source_url": source_url,
            "download_url": actual_dl,
            "creator": creator,
            "license": "Alta Museum Open Non-Commercial ShareAlike (CC BY-NC-SA-equivalent)",
            "license_url": "https://www.altamuseum.no/en/the-rock-art-of-alta/the-rock-art-archive/about-the-archive",
            "commercial_ok": False,
            "sha256": sha,
            "width_px": w,
            "height_px": h,
        }

    records: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(fetch_one_alta, (i, a)) for i, a in enumerate(selected, 1)]
        for fut in as_completed(futs):
            rec = fut.result()
            if rec:
                records.append(rec)
                if len(records) % 50 == 0:
                    print(f"    Alta progress: {len(records)} ready...", flush=True)

    records.sort(key=lambda r: r["source_id"])
    print(f"  Alta harvest complete: {len(records)} files", flush=True)
    return records


# =============================================================================
# 2. UNIMUS — UNIVERSITETSMUSEENES FOTOPORTAL (KHM, UM, VM — CC BY-SA 4.0)
# =============================================================================
UNIMUS_ALLOWED_LICENSES = {
    "CC BY-SA 4.0": ("https://creativecommons.org/licenses/by-sa/4.0/", True),
    "CC BY 4.0": ("https://creativecommons.org/licenses/by/4.0/", True),
    "CC0 1.0": ("https://creativecommons.org/publicdomain/zero/1.0/", True),
    "Public Domain Mark 1.0": ("https://creativecommons.org/publicdomain/mark/1.0/", True),
    "PDM": ("https://creativecommons.org/publicdomain/mark/1.0/", True),
}

UNIMUS_MUSEUM_NAMES = {
    "KHM": "Kulturhistorisk museum, UiO",
    "UM": "Universitetsmuseet i Bergen",
    "VM": "NTNU Vitenskapsmuseet",
}

NON_ROCK_EXCLUDE = [
    "døpefont", "runestein", "runeinnskrift", "gravstein", "portal", "kirke",
    "skulptur", "oseberg", "gokstad", "slededrag", "båtgrav", "våpenskjold",
    "mynt", "smykke", "fibula", "spenne", "kniv", "sverd", "øks ", "skrin",
    "relikvie", "alter", "krusifiks", "istregravningen", "haug på fevang",
    "gravhaug", "gravfelt", "keramikk", "kleberkar", "bautastein", "milepæl",
    "rodestein", "grensestein", "tekstil", "vev", "drakt", "bygdeborg",
    "gravning", "utgravning", "profiltegning", "plantegning av grav",
]

FIELD_NOISE_EXCLUDE = [
    "kalkerer", "våtrenset", "tildekking", "presenning", "stillas", "skilt",
    "parkering", "arbeidsbilde", "konservering", "gangbru", "turist",
    "personer", "besøkende", "informasjonstavle",
]


def unimus_fetch_all(params: Dict[str, Any], max_items: int = 500) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for offset in range(0, max_items, 100):
        p = dict(params, limit=100, offset=offset)
        r = http_get("https://api.unimus.no/public-data-api/media/search", params=p)
        if not r:
            break
        d = r.json()
        objs = d.get("objects", [])
        if not objs:
            break
        out.extend(objs)
        if len(objs) < 100 or len(out) >= d.get("totalHits", 0):
            break
    return out


def harvest_unimus(seen_sha256: Set[str], existing_raw: Dict[str, Path]) -> List[Dict[str, Any]]:
    print("\n[2/5] Harvesting Unimus (Universitetsmuseenes fotoportal, CC BY-SA 4.0)...", flush=True)
    tasks: List[Tuple[Dict[str, Any], str]] = []
    queued_mids: Set[int] = set()

    def queue_item(o: Dict[str, Any], media_type: str) -> bool:
        mid = o.get("mediagruppe_id")
        lic = (o.get("license") or "").strip()
        if not mid or mid in queued_mids or lic not in UNIMUS_ALLOWED_LICENSES:
            return False
        queued_mids.add(mid)
        tasks.append((o, media_type))
        return True

    # 2A. UM (Universitetsmuseet i Bergen) — Vingen, Ausevik, Kårstadsteinen kalkeringer
    um_kalk = unimus_fetch_all({"freetext": "kalkering", "museum": "UM"}, max_items=200)
    um_q = 0
    for o in um_kalk:
        if um_q >= MAX_UNIMUS_UM_KALK:
            break
        txt = ((o.get("motif") or "") + " " + " ".join(o.get("keywords") or [])).lower()
        if any(b in txt for b in NON_ROCK_EXCLUDE + FIELD_NOISE_EXCLUDE):
            continue
        if queue_item(o, "bw_tracing"):
            um_q += 1
    print(f"  Queued UM (Bergen — Vingen/Ausevik) kalkering: {um_q}", flush=True)

    # 2B. KHM (Kulturhistorisk museum Oslo) — kalkeringer prioritized across ALL counties
    khm_kalk = unimus_fetch_all({"freetext": "kalkering", "museum": "KHM"}, max_items=350)
    def khm_priority(o: Dict[str, Any]) -> Tuple[int, str, int]:
        pl = (o.get("places") or [{}])[0]
        county = (pl.get("county") or "").lower()
        muni = (pl.get("municipality") or "").lower()
        is_ostfold = 1 if county == "østfold" else 0
        return (is_ostfold, muni, o.get("mediagruppe_id") or 0)

    khm_kalk.sort(key=khm_priority)
    khm_k_q = 0
    for o in khm_kalk:
        if khm_k_q >= MAX_UNIMUS_KHM_KALK:
            break
        txt = ((o.get("motif") or "") + " " + " ".join(o.get("keywords") or [])).lower()
        if any(b in txt for b in NON_ROCK_EXCLUDE + FIELD_NOISE_EXCLUDE):
            continue
        if "haug " in txt or "grav" in txt:
            continue
        if queue_item(o, "bw_tracing"):
            khm_k_q += 1
    print(f"  Queued KHM kalkering (Østfold/Vestfold/Innlandet/Telemark/Buskerud/Akershus): {khm_k_q}", flush=True)

    # 2C. KHM archaeological drawings ("tegning") in Leiknes & helleristning
    khm_t_q = 0
    for q, lim in [("Leiknes", 50), ("helleristning", 1000), ("helleristninger", 500)]:
        if khm_t_q >= MAX_UNIMUS_KHM_TEGNING:
            break
        items = unimus_fetch_all({"freetext": q, "museum": "KHM"}, max_items=lim)
        for o in items:
            if khm_t_q >= MAX_UNIMUS_KHM_TEGNING:
                break
            txt = ((o.get("motif") or "") + " " + " ".join(o.get("keywords") or [])).lower()
            if any(b in txt for b in NON_ROCK_EXCLUDE + FIELD_NOISE_EXCLUDE):
                continue
            if any(w in txt for w in ["tegning", "kalkering", "plansje", "avtegning", "frottage", "gnibilde"]):
                if any(g in txt for g in ["hellerist", "helleristing", "bergkunst", "ristning", "leiknes", "veiderist", "skipsfigur"]):
                    if queue_item(o, "bw_tracing"):
                        khm_t_q += 1
    print(f"  Queued KHM archaeological drawings (tegning — incl. Leiknes Nordland): {khm_t_q}", flush=True)

    # 2D. VM (NTNU Vitenskapsmuseet) — Tb rock-art tracings + flagship motif closeups
    vm_berg = unimus_fetch_all({"freetext": "bergkunst", "museum": "VM"}, max_items=900)
    vm_tb_q = 0
    for o in vm_berg:
        if vm_tb_q >= MAX_UNIMUS_VM_TRACINGS:
            break
        txt = ((o.get("motif") or "") + " " + " ".join(o.get("keywords") or [])).lower()
        if "tb " in txt or "kalkering" in txt:
            if any(bad in txt for bad in ["hulemaleri", "hellemaleri", "kalkerer", "oppkrittet og kalkert"]):
                continue
            if any(g in txt for g in ["bardal", "leirfall", "stuberg", "hommelvik", "kalkering"]):
                if queue_item(o, "bw_tracing"):
                    vm_tb_q += 1

    vm_close_q = 0
    for o in vm_berg:
        if vm_close_q >= MAX_UNIMUS_VM_CLOSEUPS:
            break
        motif = (o.get("motif") or "").lower()
        kws = " ".join(o.get("keywords") or []).lower()
        txt = f"{motif} {kws}"
        if any(b in txt for b in NON_ROCK_EXCLUDE + FIELD_NOISE_EXCLUDE + ["oversikt", "landskap", "mose", "forvitring", "sprekk", "vanskelig å påvise"]):
            continue
        if any(good in motif for good in [
            "bølarein", "kveitefigur", "båtfigur", "dyrefigur", "saksefigur",
            "helleristninger. figurene", "figur 1", "oppkrittet", "elgfigur",
            "evenhus", "holtås", "hell1", "hell. felt"
        ]):
            mtype = "painted_photo" if any(w in motif for w in ["oppmalt", "malt", "oppkrittet", "bølarein", "kveite", "båtfigur", "dyrefigur"]) else "photo_other"
            if queue_item(o, mtype):
                vm_close_q += 1
    print(f"  Queued VM (NTNU Trøndelag/Møre/Nordland): {vm_tb_q} tracings + {vm_close_q} closeups (total Unimus queue={len(tasks)})", flush=True)

    def fetch_one_unimus(item_pair: Tuple[Dict[str, Any], str]) -> Optional[Dict[str, Any]]:
        o, media_type = item_pair
        mid = o.get("mediagruppe_id")
        lic = (o.get("license") or "").strip()
        lic_url, commercial_ok = UNIMUS_ALLOWED_LICENSES[lic]

        dl_url = o.get("imageUrl") or f"https://www.unimus.no/felles/bilder/web_hent_bilde.php?id={mid}&type=jpeg"
        fallback_url = f"https://www.unimus.no/felles/bilder/web_hent_bilde.php?id={mid}&type=small"
        mus = (o.get("museum") or "UNIMUS").strip()
        motif = (o.get("motif") or "").strip()
        keywords = [str(k).strip() for k in (o.get("keywords") or []) if str(k).strip()]
        places = o.get("places") or []
        pl = places[0] if places else {}
        county = pl.get("county")
        muni = pl.get("municipality") or ""
        cad = pl.get("cadastralName") or pl.get("placeName") or pl.get("area") or ""
        site_parts = [x for x in [cad, muni] if x and x.lower() != "ukjent"]
        site = ", ".join(dict.fromkeys(site_parts)) or (county or "Norway")

        full_text = f"{motif} {' '.join(keywords)} {site} {county or ''}"
        region = normalize_region(county, full_text)
        period = infer_period(full_text, region)

        photographers = o.get("photographers") or []
        if not photographers and o.get("persons"):
            photographers = [p.get("name") for p in o["persons"] if p.get("name")]
        mus_name = UNIMUS_MUSEUM_NAMES.get(mus, mus)
        creator = f"{', '.join(photographers)} / {mus_name}" if photographers else mus_name

        stem = f"unimus_{mus.lower()}_{mid}"
        res = download_or_reuse_image(dl_url, stem, seen_sha256, existing_raw, min_dim=400, fallback_url=fallback_url)
        if not res:
            return None
        raw_rel, sha, w, h, actual_dl = res

        uuid = o.get("mediaObjectUuid") or str(mid)
        source_url = f"https://www.unimus.no/portal/#/photos/{uuid}"
        title = motif.splitlines()[0][:140] if motif else f"Rock art documentation ({site}, {mus})"
        desc = motif or f"Norwegian rock art documentation from {mus_name} ({site})."
        if keywords:
            desc = f"{desc} Keywords: {', '.join(keywords)}."

        return {
            "source_id": f"unimus:{mus}:{mid}",
            "source_archive": "unimus",
            "title": title,
            "description": desc[:600],
            "site": site,
            "region": region,
            "country": "NO",
            "period": period,
            "media_type": media_type,
            "motif_hints": extract_motif_hints(full_text, keywords),
            "raw_path": raw_rel,
            "source_url": source_url,
            "download_url": actual_dl,
            "creator": creator,
            "license": lic,
            "license_url": lic_url,
            "commercial_ok": commercial_ok,
            "sha256": sha,
            "width_px": w,
            "height_px": h,
        }

    records: List[Dict[str, Any]] = []
    done = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(fetch_one_unimus, t) for t in tasks]
        for fut in as_completed(futs):
            done += 1
            rec = fut.result()
            if rec:
                records.append(rec)
            if done % 25 == 0 or done == len(tasks):
                print(f"    Unimus progress: {done}/{len(tasks)} checked, {len(records)} saved...", flush=True)

    records.sort(key=lambda r: r["source_id"])
    print(f"  Unimus harvest complete: {len(records)} files", flush=True)
    return records


# =============================================================================
# 3. KULTURMINNESØK / RIKSANTIKVAREN (api.ra.no/brukerminner — CC0 / CC BY / CC BY-SA)
# =============================================================================
RA_ALLOWED_LICENSES = {
    "CC0": ("CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/", True),
    "CC BY": ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/", True),
    "CC BY-SA": ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/", True),
    "PDM": ("Public Domain Mark 1.0", "https://creativecommons.org/publicdomain/mark/1.0/", True),
}


def harvest_kulturminnesok(seen_sha256: Set[str], existing_raw: Dict[str, Path]) -> List[Dict[str, Any]]:
    print("\n[3/5] Harvesting Kulturminnesøk / Riksantikvaren (CC0 / CC BY / CC BY-SA)...", flush=True)
    records: List[Dict[str, Any]] = []
    seen_fids: Dict[str, Dict[str, Any]] = {}

    queries = [
        "helleristning", "helleristninger", "bergkunst", "skipsristning",
        "bøla", "bardal", "leiknes", "rødøy", "hegra", "leirfall",
        "begby", "kalnes", "solberg", "åmøy", "etne", "vingen", "ausevik", "isbjørn"
    ]
    for q in queries:
        r = http_get(
            "https://api.ra.no/brukerminner/collections/brukerminner/items",
            params={"q": q, "limit": 50, "f": "json"},
        )
        if not r:
            continue
        for feat in r.json().get("features", []):
            fid = feat.get("id")
            props = feat.get("properties", {})
            title = (props.get("tittel") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", props.get("beskrivelse") or "").strip()
            txt = f"{title} {desc}".lower()
            if not any(g in txt for g in ["hellerist", "bergkunst", "skipsrist", "veiderist", "jordbruksrist", "bølarein"]):
                continue
            if any(b in txt for b in [
                "bygdeborg", "gravrøys", "buplass", "naturlig figur", "skålgropstein",
                "klangstenen", "bogestille", "skriverberg", "skrivarberg", "mulig helleristning",
                "rodestein", "grensestein", "bauta", "1945", "1887", "1836", "tagging",
                "bønsbudalshøgda", "hjøringdal"
            ]):
                continue
            seen_fids[fid] = props

    print(f"  Found {len(seen_fids)} filtered rock-art features in Riksantikvaren Brukerminner", flush=True)

    for fid, props in seen_fids.items():
        if len(records) >= MAX_RA_OPEN:
            break
        title = (props.get("tittel") or "Helleristning").strip()
        desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", props.get("beskrivelse") or "")).strip()
        fylke = props.get("fylke") or ""
        kommune = props.get("kommune") or ""
        site = f"{title} ({kommune})" if kommune else title
        region = normalize_region(fylke, f"{title} {desc} {kommune}")
        period = infer_period(f"{title} {desc}", region)
        source_url = props.get("linkkulturminnesok") or f"https://www.kulturminnesok.no/kart/?id={fid}"

        per_site = 0
        for idx, b in enumerate(props.get("bilder") or []):
            if len(records) >= MAX_RA_OPEN or per_site >= 2:
                break
            lic_raw = (b.get("lisens") or "").strip()
            if lic_raw not in RA_ALLOWED_LICENSES:
                continue
            b_desc = (b.get("beskrivelse") or "").strip()
            if any(skip in b_desc.lower() for skip in [
                "skilt", "tavle", "parkering", "utsikt", "oversikt over området", "kart",
                "1836", "1887", "1945", "initialer", "monogram", "tagging", "1800-", "1900-tall",
                "kompassrose", "kvernstein", "fot for størrelse", "vadskjæret",
                "i bakgrunnen", "groper på helle", "fr. hald", "bauta", "inskripsjon",
                "gården røien", "erik nederst", "innrissinger i fjell"
            ]) or b_desc.strip().lower() in ("steinen", "gropene", "skålgrop"):
                continue
            dl_url = b.get("url")
            if not dl_url:
                continue
            lic_std, lic_url, commercial_ok = RA_ALLOWED_LICENSES[lic_raw]
            creator = (b.get("fotograf") or props.get("opprettet_av") or "Kulturminnesøk contributor").strip()
            stem = f"kms_{safe_slug(fid, 16)}_{idx}"
            res = download_or_reuse_image(dl_url, stem, seen_sha256, existing_raw, min_dim=400)
            if not res:
                continue
            raw_rel, sha, w, h, actual_dl = res
            per_site += 1

            comb = f"{title} {b_desc} {desc}"
            mtype = "painted_photo" if any(x in comb.lower() for x in ["malt", "rød", "oppmalt", "skip", "båt", "rein", "elg", "bjørn", "fisk"]) else "photo_other"
            records.append({
                "source_id": f"kulturminnesok:{fid}:{idx}",
                "source_archive": "kulturminnesok",
                "title": b_desc or title,
                "description": f"{title}. {b_desc}. {desc}"[:500].strip(),
                "site": site,
                "region": region,
                "country": "NO",
                "period": period,
                "media_type": mtype,
                "motif_hints": extract_motif_hints(comb),
                "raw_path": raw_rel,
                "source_url": source_url,
                "download_url": actual_dl,
                "creator": creator,
                "license": lic_std,
                "license_url": lic_url,
                "commercial_ok": commercial_ok,
                "sha256": sha,
                "width_px": w,
                "height_px": h,
            })

    print(f"  Kulturminnesøk harvest complete: {len(records)} files", flush=True)
    return records


# =============================================================================
# 4. NASJONALBIBLIOTEKET (api.nb.no — Public Domain historical documentation)
# =============================================================================
def harvest_nasjonalbiblioteket(seen_sha256: Set[str], existing_raw: Dict[str, Path]) -> List[Dict[str, Any]]:
    print("\n[4/5] Harvesting Nasjonalbiblioteket (Public Domain images)...", flush=True)
    records: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    for q in ["helleristning*", "bergkunst"]:
        r = http_get(
            "https://api.nb.no/catalog/v1/items",
            params={"q": q, "filter": "mediatype:bilder", "size": 100},
        )
        if not r:
            continue
        items = r.json().get("_embedded", {}).get("items", [])
        for it in items:
            if len(records) >= MAX_NB_PD:
                break
            iid = it.get("id")
            if not iid or iid in seen_ids:
                continue
            acc = it.get("accessInfo", {})
            if not acc.get("isPublicDomain") or acc.get("accessAllowedFrom") != "EVERYWHERE":
                continue

            md = it.get("metadata", {})
            title = (md.get("title") or "").strip()
            summary = (md.get("summary") or "").strip()
            place_str = (md.get("geographic", {}).get("placeString") or "").strip()
            txt = f"{title} {summary} {place_str}".lower()

            if any(skip in txt for skip in ["merker for hellerist", "skilt", "turist", "gruppe", "museum", "utstilling", "hus ", "gård ", "oversikt"]):
                continue
            if not any(g in txt for g in ["hellerist", "bergkunst"]):
                continue

            urn = md.get("identifiers", {}).get("urn")
            if not urn:
                continue
            dl_url = f"https://www.nb.no/services/image/resolver/{urn}/full/1600,/0/native.jpg"
            source_url = f"https://www.nb.no/items/{iid}"

            creators = []
            for p in md.get("people") or []:
                if p.get("name"):
                    creators.append(p["name"])
            for c in md.get("corporates") or []:
                nm = c.get("name")
                if isinstance(nm, list):
                    creators.extend(nm)
                elif nm:
                    creators.append(str(nm))
            creator = "; ".join(dict.fromkeys(creators)) or "Nasjonalbiblioteket (Public Domain)"

            parts = [p.strip() for p in place_str.split(";") if p.strip() and p.strip().lower() != "norge"]
            county = parts[0] if parts else ""
            site = ", ".join(parts[1:]) if len(parts) > 1 else (parts[0] if parts else title)
            region = normalize_region(county, txt)
            period = infer_period(txt, region)

            stem = f"nb_pd_{safe_slug(iid, 16)}"
            res = download_or_reuse_image(dl_url, stem, seen_sha256, existing_raw, min_dim=400)
            if not res:
                continue
            raw_rel, sha, w, h, actual_dl = res
            seen_ids.add(iid)

            mtype = "painted_photo" if any(x in txt for x in ["kalnes", "skjeberg", "kråkerøy", "etne", "alta", "begby"]) else "photo_other"
            records.append({
                "source_id": f"nb:{iid}",
                "source_archive": "other",
                "title": title,
                "description": f"{title}. {summary} Place: {place_str}".strip(),
                "site": site or title,
                "region": region,
                "country": "NO",
                "period": period,
                "media_type": mtype,
                "motif_hints": extract_motif_hints(txt),
                "raw_path": raw_rel,
                "source_url": source_url,
                "download_url": actual_dl,
                "creator": creator,
                "license": "Public Domain",
                "license_url": "https://creativecommons.org/publicdomain/mark/1.0/",
                "commercial_ok": True,
                "sha256": sha,
                "width_px": w,
                "height_px": h,
            })

    print(f"  Nasjonalbiblioteket harvest complete: {len(records)} files", flush=True)
    return records


# =============================================================================
# 5. DIGITALTMUSEUM NORWAY VIA EUROPEANA OPEN API (PDM / CC0 / CC BY / CC BY-SA)
# =============================================================================
EUROPEANA_OPEN_RIGHTS = {
    "http://creativecommons.org/publicdomain/mark/1.0/": ("Public Domain Mark 1.0", True),
    "https://creativecommons.org/publicdomain/mark/1.0/": ("Public Domain Mark 1.0", True),
    "http://creativecommons.org/publicdomain/zero/1.0/": ("CC0 1.0", True),
    "https://creativecommons.org/publicdomain/zero/1.0/": ("CC0 1.0", True),
    "http://creativecommons.org/licenses/by/4.0/": ("CC BY 4.0", True),
    "https://creativecommons.org/licenses/by/4.0/": ("CC BY 4.0", True),
    "http://creativecommons.org/licenses/by-sa/4.0/": ("CC BY-SA 4.0", True),
    "https://creativecommons.org/licenses/by-sa/4.0/": ("CC BY-SA 4.0", True),
    "http://creativecommons.org/licenses/by/3.0/": ("CC BY 3.0", True),
    "http://creativecommons.org/licenses/by-sa/3.0/": ("CC BY-SA 3.0", True),
}


def harvest_dimu_via_europeana(seen_sha256: Set[str], existing_raw: Dict[str, Path]) -> List[Dict[str, Any]]:
    print("\n[5/5] Harvesting DigitaltMuseum Norway open records via Europeana API...", flush=True)
    records: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()

    for q in ["helleristning*", "bergkunst*"]:
        r = http_get(
            "https://api.europeana.eu/record/v2/search.json",
            params={
                "wskey": "api2demo",
                "query": q,
                "qf": ["COUNTRY:norway", "TYPE:IMAGE"],
                "reusability": "open",
                "rows": 100,
            },
        )
        if not r:
            continue
        for it in r.json().get("items", []):
            if len(records) >= MAX_DIMU_EUROPEANA:
                break
            eid = it.get("id")
            if not eid or eid in seen_ids:
                continue
            rights_list = it.get("rights") or []
            if not rights_list or rights_list[0] not in EUROPEANA_OPEN_RIGHTS:
                continue
            lic_url = rights_list[0]
            lic_name, commercial_ok = EUROPEANA_OPEN_RIGHTS[lic_url]

            title = ((it.get("title") or [""])[0] or "").strip()
            desc = ((it.get("dcDescription") or [""])[0] or "").strip()
            txt = f"{title} {desc}".lower()
            if not any(g in txt for g in ["hellerist", "bergkunst"]):
                continue
            if any(skip in txt for skip in [
                "mann på steinen", "forskere", "person", "gruppe", "fly,", "heinkel",
                "stolper rundt", "turist", "skilt", "gangbru", "utsikt", "dame", "barn",
                "aspeberget", "hällristning", "historielag på tur", "forteller", "skiutstilling"
            ]):
                continue

            img_urls = it.get("edmIsShownBy") or []
            if not img_urls:
                continue
            dl_url = img_urls[0]
            source_url = ((it.get("edmIsShownAt") or [None])[0]) or it.get("guid") or f"https://www.europeana.eu/item{eid}"
            provider = ((it.get("dataProvider") or ["DigitaltMuseum Norway"])[0]).strip()
            creators = [c for c in (it.get("dcCreator") or []) if c and not str(c).startswith("#")]
            creator = f"{', '.join(creators)} ({provider})" if creators else provider

            region = normalize_region(None, txt)
            period = infer_period(txt, region)
            stem = f"dimu_{safe_slug(eid, 20)}"
            res = download_or_reuse_image(dl_url, stem, seen_sha256, existing_raw, min_dim=400)
            if not res:
                continue
            raw_rel, sha, w, h, actual_dl = res
            seen_ids.add(eid)

            is_dimu = "dimu.org" in dl_url or "digitaltmuseum" in source_url
            mtype = "bw_tracing" if any(w in txt for w in ["kalker", "tegning", "avtegn"]) else (
                "painted_photo" if any(w in txt for w in ["malt", "rød", "rødbrun"]) else "photo_other"
            )
            records.append({
                "source_id": f"digitaltmuseum:{eid.strip('/')}",
                "source_archive": "digitaltmuseum" if is_dimu else "other",
                "title": title[:160],
                "description": (desc or title)[:500],
                "site": title[:80],
                "region": region,
                "country": "NO",
                "period": period,
                "media_type": mtype,
                "motif_hints": extract_motif_hints(txt),
                "raw_path": raw_rel,
                "source_url": source_url,
                "download_url": actual_dl,
                "creator": creator,
                "license": lic_name,
                "license_url": lic_url,
                "commercial_ok": commercial_ok,
                "sha256": sha,
                "width_px": w,
                "height_px": h,
            })

    print(f"  DigitaltMuseum/Europeana harvest complete: {len(records)} files", flush=True)
    return records


def save_manifest(records: List[Dict[str, Any]]) -> None:
    with MANIFEST_PATH.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def prune_orphans(records: List[Dict[str, Any]]) -> int:
    """Remove any unreferenced partial files in RAW_DIR not in manifest.jsonl."""
    referenced = {Path(r["raw_path"]).name for r in records}
    removed = 0
    for p in RAW_DIR.iterdir():
        if p.is_file() and p.name not in referenced:
            p.unlink(missing_ok=True)
            removed += 1
    return removed


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    existing_raw = index_existing_raw()
    print(f"Found {len(existing_raw)} existing cached raw files in {RAW_DIR}", flush=True)

    seen_sha256: Set[str] = set()
    all_records: List[Dict[str, Any]] = []

    all_records.extend(harvest_alta_kalkeringer(seen_sha256, existing_raw))
    save_manifest(all_records)

    all_records.extend(harvest_unimus(seen_sha256, existing_raw))
    save_manifest(all_records)

    all_records.extend(harvest_kulturminnesok(seen_sha256, existing_raw))
    save_manifest(all_records)

    all_records.extend(harvest_nasjonalbiblioteket(seen_sha256, existing_raw))
    save_manifest(all_records)

    all_records.extend(harvest_dimu_via_europeana(seen_sha256, existing_raw))
    save_manifest(all_records)

    orphans = prune_orphans(all_records)
    total_mb = sum(p.stat().st_size for p in RAW_DIR.glob("*") if p.is_file()) / (1024 * 1024)
    print("\n" + "=" * 72, flush=True)
    print(f"TOTAL HARVESTED: {len(all_records)} distinct verified files ({total_mb:.1f} MB; pruned {orphans} orphans)", flush=True)
    print("By archive:", dict(Counter(r["source_archive"] for r in all_records)), flush=True)
    print("By media_type:", dict(Counter(r["media_type"] for r in all_records)), flush=True)
    print("By region:", dict(Counter(r["region"] for r in all_records).most_common()), flush=True)
    print("By period:", dict(Counter(r["period"] for r in all_records)), flush=True)
    print("By license:", dict(Counter(r["license"] for r in all_records)), flush=True)
    print("By commercial_ok:", dict(Counter(r["commercial_ok"] for r in all_records)), flush=True)
    print(f"Manifest written to: {MANIFEST_PATH}", flush=True)
    print("=" * 72, flush=True)


if __name__ == "__main__":
    main()
