#!/usr/bin/env python3
"""DigitaltMuseum Sweden Open Rock-Art Harvester.

Harvests open rock-art drawings, tracings, rubbings, and engravings from Swedish museums
indexed via DigitaltMuseum's syndication in K-samsök (SOCH) and Europeana:
  - Bohusläns museum (bhm / Bohuslän Museum)
  - Västernorrlands museum [Nämforsen] (s-ylm / Västernorrlands museum)
  - Vänersborgs museum (vbg / Vänersborgs museum)
  - Östergötlands museum
  - Historiska museet (shm)
  - Plus other DiMu-hosted Swedish regional museums (Västmanland s-uvlm,
    Västergötland s-vgm, Falkenberg s-fbm, Gävleborg s-xlm, Värmland s-vlm, etc.)

Strict license filter: CC0 / PDM / CC BY / CC BY-SA only.
Quarantines any painted_photo into quarantine_raw/.
Deduplicates by SHA-256 against all existing helleristninger/sources/*/manifest.jsonl.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np
import requests
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCES_ROOT = SCRIPT_DIR.parent
RAW_DIR = SCRIPT_DIR / "raw"
QUARANTINE_DIR = SCRIPT_DIR / "quarantine_raw"
MANIFEST_PATH = SCRIPT_DIR / "manifest.jsonl"

KSAMSOK_API = "https://kulturarvsdata.se/ksamsok/api"
EUROPEANA_API = "https://api.europeana.eu/record/v2/search.json"
USER_AGENT = (
    "NordicHelleristningerResearchHarvester/2.0 "
    "(open cultural heritage research; jigsaw.iverfinne.no; polite rate-limited)"
)
HTTP_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.8"}

DIMU_ORGS = ["bhm", "vbg", "s-ylm", "s-vlm", "s-uvlm", "s-vgm", "s-fbm", "s-hm", "s-xlm", "s-dm", "s-olm", "shm"]

BAD_OBJECT_TOKENS = (
    "tröja", "t-shirt", "frimärke", "souvenir", "skylt", "parkering", "tavelgalleri",
    "utställning", "låda m utrustn", "vykort", "tidning", "etnografiska", "bokomslag",
)

DRAWING_TOKENS = (
    "avritning", "kalkering", "teckning", "ritning", "kopparstick", "grafik",
    "frottage", "gnidbild", "akvarell", "lavering", "skiss", "plansch", "uppmätning",
    "hasslöf", "fredsjö",
)

PAINT_TOKENS = (
    "imål", "ifylld", "uppmål", "målade", "målad", "röd färg", "kritad", "kritning",
)

HUNTER_TOKENS = (
    "nämforsen", "ådals-liden", "hällmålning", "älghuvud", "älg", "glösa", "alsen",
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
    timeout: int = 35,
    delay: float = 0.08,
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


def upgrade_dimu_url(url: str) -> str:
    if url.startswith("http://") and "dimu.org" in url:
        url = "https://" + url[len("http://"):]
    if "dimu.org/image/" in url:
        url = re.sub(r"dimension=\d+x\d+(?:-crop)?", "dimension=1200x1200", url)
    return url


def classify_dimu_image(raw_bytes: bytes, text_blob: str, item_type: str) -> Tuple[str, int, int, str]:
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
    ink_frac = float(np.mean(lum < 120))

    if "frottage" in low or "gnidbild" in low or "rubbing" in low:
        if (p95 - p05) >= 60:
            return "rubbing_frottage", w, h, "ok_rubbing"

    # Explicit drawing/tracing metadata + clean tonal contrast
    if any(d in low for d in ("avritning", "kalkering", "teckning", "ritning", "kopparstick", "grafik", "skiss", "uppmätning")):
        if mean_sat < 55 and (p95 - p05) >= 50:
            return "bw_tracing", w, h, "ok_drawing_meta"

    # Visual B&W tracing detection (e.g. Bohusläns museum Hasslöf/Fredsjö archival plates labeled only "Hällristning")
    if p70 >= 195 and white_frac >= 0.50 and mean_sat < 28 and 0.003 <= ink_frac <= 0.45 and (p95 - p05) >= 60:
        return "bw_tracing", w, h, "ok_bw_visual"

    # Historical high-contrast archival contact sheet / rubbing on white mount
    if white_frac >= 0.62 and mean_sat < 22 and (p95 - p05) >= 75 and ink_frac >= 0.005:
        return "bw_tracing", w, h, "ok_archival_plate"

    return "photo_other", w, h, "photo_other_skipped"


def infer_region_from_text(combo: str, org: str) -> str:
    c = combo.lower()
    if any(k in c for k in ("tanum", "kville", "svenneby", "bottna", "tossene", "brastad", "askum", "skee", "bohuslän", "vitlycke", "skredsvik")):
        return "Västra Götalands län (Bohuslän)"
    if any(k in c for k in ("tisselskog", "högsbyn", "fröskog", "bolstad", "dalsland", "gunnarsnäs", "skållerud")):
        return "Västra Götalands län (Dalsland)"
    if any(k in c for k in ("nämforsen", "ådals-liden", "ångermanland", "västernorrland")):
        return "Ångermanland — Nämforsen (Ådals-Liden)"
    if any(k in c for k in ("himmelstalund", "östergötland", "norrköping", "östra eneby")):
        return "Östergötlands län — Norrköping"
    if any(k in c for k in ("västmanland", "västerås", "kungsåra", "bjuggsta")):
        return "Västmanlands län"
    if any(k in c for k in ("flyhov", "kinnekulle", "västergötland")):
        return "Västra Götalands län (Västergötland)"
    return org or "Sweden"


def infer_motifs(combo: str, period: str) -> List[str]:
    c = combo.lower()
    hints: List[str] = []
    if "skepp" in c or "båt" in c:
        hints.append("stone_age_boat" if period == "stone_age_hunter" else "bronze_age_ship")
    if "älg" in c:
        hints.append("elk_moose")
    if "häst" in c:
        hints.append("horse")
    if "människ" in c:
        hints.append("anthropomorph")
    if "solhjul" in c or "hjulkors" in c:
        hints.append("sun_wheel")
    if "fotsul" in c or "fotspor" in c:
        hints.append("footprint")
    return hints or (["elk_moose"] if period == "stone_age_hunter" else ["bronze_age_ship"])


def collect_ksamsok_dimu_candidates(existing_sids: Set[str], existing_urls: Set[str]) -> Dict[str, Dict[str, Any]]:
    lic_clause = (
        '(mediaLicense="http://kulturarvsdata.se/resurser/License#cc0" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#pdm" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#pdmark" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#by" '
        'OR mediaLicense="http://kulturarvsdata.se/resurser/License#by-sa")'
    )
    candidates: Dict[str, Dict[str, Any]] = {}

    for org_code in DIMU_ORGS:
        q = f'serviceOrganization="{org_code}" AND (text="hällristning" OR text="hällmålning" OR itemName="Hällristning" OR itemName="Hällmålning") AND {lic_clause} AND thumbnailExists=j'
        start = 1
        total = None
        while total is None or start <= total:
            r = polite_get(
                KSAMSOK_API,
                params={"method": "search", "recordSchema": "presentation", "hitsPerPage": 200, "startRecord": start, "query": q},
                delay=0.06,
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
                org = it.get("pres:organization")
                org = str(org.get("content", "") if isinstance(org, dict) else (org or "")).strip()
                lbl = it.get("pres:itemLabel")
                lbl = str(lbl.get("content", "") if isinstance(lbl, dict) else (lbl or "")).strip()
                desc = it.get("pres:description")
                desc = str(desc.get("content", "") if isinstance(desc, dict) else (desc or "")).strip()
                itype = it.get("pres:type")
                itype = str(itype.get("content", "") if isinstance(itype, dict) else (itype or "")).strip()
                combo = f"{lbl} {desc} {org} {itype}".lower()
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
                if not img_url:
                    continue
                img_url = upgrade_dimu_url(img_url)
                ent_uri = str(it.get("pres:entityUri") or it.get("pres:id") or img_url)
                sid = "dimu_se_" + re.sub(r"[^a-zA-Z0-9]+", "_", ent_uri)[-40:]
                if sid in existing_sids or img_url in existing_urls or sid in candidates:
                    continue

                reps = (it.get("pres:representations") or {}).get("pres:representation") or []
                if isinstance(reps, dict):
                    reps = [reps]
                web_url = next((x.get("content") for x in reps if isinstance(x, dict) and x.get("format") == "HTML"), "") or ent_uri

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

                byline = im0.get("pres:byline")
                if isinstance(byline, dict):
                    byline = byline.get("content")
                creator = f"{byline} / {org}" if byline and org else (byline or org or "DigitaltMuseum Sweden")
                period = "stone_age_hunter" if any(h in combo for h in HUNTER_TOKENS) else "bronze_age_agrarian"
                region = infer_region_from_text(combo, org)

                candidates[sid] = {
                    "source_id": sid,
                    "source_archive": "digitaltmuseum",
                    "title": lbl or f"DigitaltMuseum {org}",
                    "description": desc or f"{lbl} ({org}).",
                    "site": lbl[:120] if lbl else org,
                    "region": region,
                    "country": "SE",
                    "period": period,
                    "motif_hints": infer_motifs(combo, period),
                    "source_url": str(web_url),
                    "download_url": img_url,
                    "creator": creator,
                    "rights_holder": org or creator,
                    "license": lic_name,
                    "license_url": lic_url,
                    "commercial_ok": True,
                    "coordinates": coords_obj,
                    "_text_blob": combo,
                    "_item_type": itype,
                }
            start += len(recs)

    return candidates


def collect_europeana_dimu_candidates(
    candidates: Dict[str, Dict[str, Any]],
    existing_sids: Set[str],
    existing_urls: Set[str],
) -> None:
    for provider in [
        "Bohuslän Museum",
        "Vänersborgs museum",
        "Västmanland County Museum",
        "Västergötlands Museum",
        "Västernorrlands museum",
        "Upplands Museum",
        "Kalmar County Museum",
        "Museum of Gothenburg",
        "Kulturparken Småland",
        "Blekinge Museum",
    ]:
        start = 1
        while start <= 500:
            r = polite_get(
                EUROPEANA_API,
                params={
                    "wskey": "api2demo",
                    "query": "hällristning OR hällmålning OR avritning",
                    "qf": f'DATA_PROVIDER:"{provider}"',
                    "reusability": "open",
                    "media": "true",
                    "rows": 100,
                    "start": start,
                },
                delay=0.08,
            )
            if not r or not r.ok:
                break
            d = r.json()
            items = d.get("items") or []
            if not items:
                break
            for it in items:
                rights = it.get("rights") or []
                lic_name, lic_url, comm_ok = normalize_license(rights[0] if rights else "")
                if not comm_ok:
                    continue
                img_url = (it.get("edmIsShownBy") or [None])[0]
                if not img_url:
                    continue
                img_url = upgrade_dimu_url(img_url)
                eu_id = (it.get("id") or "").strip("/").replace("/", "_")
                sid = f"europeana_{eu_id}"
                if sid in existing_sids or img_url in existing_urls or any(c["download_url"] == img_url for c in candidates.values()):
                    continue
                title = (it.get("title") or [f"Hällristning ({provider})"])[0]
                desc = " ".join(it.get("dcDescription") or [title])
                combo = f"{title} {desc} {provider}".lower()
                if any(b in combo for b in BAD_OBJECT_TOKENS):
                    continue
                if "hällristning" not in combo and "hällmålning" not in combo:
                    continue

                coords_obj = None
                lats = it.get("edmPlaceLatitude") or []
                lons = it.get("edmPlaceLongitude") or []
                if lats and lons:
                    try:
                        coords_obj = {"lon": float(lons[0]), "lat": float(lats[0]), "crs": "EPSG:4326"}
                    except (ValueError, TypeError):
                        pass

                period = "stone_age_hunter" if any(h in combo for h in HUNTER_TOKENS) else "bronze_age_agrarian"
                candidates[sid] = {
                    "source_id": sid,
                    "source_archive": "digitaltmuseum" if "dimu.org" in img_url else "europeana",
                    "title": title,
                    "description": f"{desc} (Provider: {provider}).",
                    "site": title[:120],
                    "region": infer_region_from_text(combo, provider),
                    "country": "SE",
                    "period": period,
                    "motif_hints": infer_motifs(combo, period),
                    "source_url": it.get("guid") or f"https://www.europeana.eu/item{it.get('id', '')}",
                    "download_url": img_url,
                    "creator": provider,
                    "rights_holder": provider,
                    "license": lic_name,
                    "license_url": lic_url,
                    "commercial_ok": True,
                    "coordinates": coords_obj,
                    "_text_blob": combo,
                    "_item_type": "europeana",
                }
            if len(items) < 100:
                break
            start += 100


def main() -> None:
    t0 = time.time()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

    existing_shas, existing_sids, existing_urls = load_existing_dedup_sets()
    print(f"Loaded existing dedup index: {len(existing_shas)} sha256")

    candidates = collect_ksamsok_dimu_candidates(existing_sids, existing_urls)
    print(f"After K-samsök DiMu orgs: {len(candidates)} candidates")
    collect_europeana_dimu_candidates(candidates, existing_sids, existing_urls)
    print(f"After Europeana Swedish museums: {len(candidates)} candidates")

    lock = threading.Lock()
    seen_shas = set(existing_shas)
    manifest_recs: List[Dict[str, Any]] = []
    stats: Counter = Counter()

    def worker(cand: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = cand["download_url"]
        resp = polite_get(url, delay=0.04, timeout=35, retries=3)
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
        itype = cand.pop("_item_type", "")
        mtype, w, h, note = classify_dimu_image(raw_bytes, text_blob, itype)
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
            out["raw_path"] = f"sources/dimu_se/quarantine_raw/{fname}"
            out["quarantined"] = True
            out["quarantine_reason"] = "painted_photo: modern paint detected — NEVER trace"
            out["traceable"] = False
            with lock:
                stats["quarantined_painted"] += 1
            return out

        (RAW_DIR / fname).write_bytes(raw_bytes)
        out["raw_path"] = f"sources/dimu_se/raw/{fname}"
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

    summary = {
        "candidates_inspected": len(candidates),
        "manifest_total": len(manifest_recs),
        "manifest_traceable": sum(1 for r in manifest_recs if r.get("traceable")),
        "manifest_quarantined": sum(1 for r in manifest_recs if r.get("quarantined")),
        "by_media_type": dict(Counter(r["media_type"] for r in manifest_recs)),
        "by_rights_holder": dict(Counter(r["rights_holder"] for r in manifest_recs)),
        "download_stats": dict(stats),
        "elapsed_sec": round(time.time() - t0, 1),
    }
    (SCRIPT_DIR / "dimu_crawl_stats.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print("DiMu SE crawl complete:", json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
