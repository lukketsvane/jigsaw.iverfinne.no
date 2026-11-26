from __future__ import annotations

import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from animal_jigsaw.data.phylopic import Api, pick_raster, resolve_node

from .corpus import _resolve_asset_path, load_corpus_catalog

CLADES = {
    "bear": ("Ursidae", 1), "reindeer": ("Rangifer", 1),
    "elk_moose": ("Alces alces", 1), "whale_cetacean": ("Cetacea", 3),
    "bird": ("Aves", 3), "fish": ("Actinopterygii", 3),
    "quadruped_other": ("Canidae", 2),
}


def prepare(out_dir, catalog):
    root = Path(out_dir).resolve()
    originals = root / "originals"
    masks = root / "masks"
    originals.mkdir(parents=True, exist_ok=True)
    masks.mkdir(exist_ok=True)
    allow = json.loads(Path("animal_jigsaw/configs/taxonomy.json").read_text(encoding="utf-8"))["license_allow"]
    api = Api(pause=0.05)
    api.start()
    chosen = {}
    for name, (clade, pages) in CLADES.items():
        node = resolve_node(api, clade)["uuid"]
        for page in range(pages):
            data = api.get("/images", filter_clade=node, filter_license_by="false",
                           filter_license_nc="false", filter_license_sa="false", embed_items="true", page=page)
            for rec in data.get("_embedded", {}).get("items", []):
                if rec["_links"].get("license", {}).get("href") in allow:
                    chosen[rec["uuid"]] = (name, rec)
            if not data.get("_links", {}).get("next"):
                break
        print(name, sum(v[0] == name for v in chosen.values()), flush=True)

    def process(item):
        uid, (name, rec) = item
        links = rec["_links"]
        pick = pick_raster(rec, 512)
        if pick is None:
            return None, "no raster"
        path = originals / f"{uid}.png"
        try:
            if not path.exists():
                path.write_bytes(api.raw(pick[0]))
            im = np.array(Image.open(path).convert("RGBA"))
            if im[:, :, 3].min() == 255:
                return None, "no transparency"
            mask = im[:, :, 3] >= 128
            labels, n = ndimage.label(mask)
            counts = np.bincount(labels.ravel())[1:]
            if not len(counts) or counts.max() < 0.97 * mask.sum():
                return None, "disconnected"
            mask = ndimage.binary_fill_holes(labels == counts.argmax() + 1)
            ys, xs = np.where(mask)
            crop = Image.fromarray(mask[ys.min():ys.max()+1, xs.min():xs.max()+1].astype(np.uint8)*255)
            scale = 204 / max(crop.size)
            crop = crop.resize(tuple(max(1, round(x*scale)) for x in crop.size), Image.Resampling.LANCZOS)
            canvas = Image.new("L", (256, 256))
            canvas.paste(crop, tuple((256-x)//2 for x in crop.size))
            arr = np.asarray(canvas) >= 128
            if ndimage.label(arr)[1] != 1 or not 0.015 < arr.mean() < 0.7:
                return None, "invalid normalized mask"
            mask_path = masks / f"{uid}.png"
            Image.fromarray(arr.astype(np.uint8)*255).save(mask_path)
            group = "phylopic:" + links["contributor"]["href"].split("?")[0]
            lic = links["license"]["href"]
            return dict(asset_id=f"pp_{uid}", family="animal", motif_class=name, source_group=group,
                        source_archive="phylopic", source_url=f"https://www.phylopic.org/images/{uid}",
                        creator=rec.get("attribution") or links["contributor"]["title"], license=allow[lic],
                        license_url=lic, commercial_ok=True, mask_path=str(mask_path),
                        sha256=hashlib.sha256(arr.tobytes()).hexdigest(),
                        original_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        original_url=pick[0], taxon=links.get("specificNode",{}).get("title")), None
        except Exception as exc:
            return None, str(exc)

    rows, failures = [], Counter()
    with ThreadPoolExecutor(max_workers=6) as pool:
        for i, (row, fail) in enumerate(pool.map(process, sorted(chosen.items()))):
            if row is not None:
                rows.append(row)
            else:
                failures[fail] += 1
            if (i+1) % 50 == 0:
                print(f"processed {i+1}/{len(chosen)}; accepted {len(rows)}", flush=True)
    original, old_root = load_corpus_catalog(catalog)
    merged = []
    seen = set()
    for row in original:
        if row.get("family") != "animal":
            continue
        row = dict(row)
        for key in ("mask_path", "sdf_path"):
            if row.get(key):
                row[key] = str(_resolve_asset_path(old_root, row[key]).resolve())
        merged.append(row)
        seen.add(row.get("sha256"))
    groups = sorted({r["source_group"] for r in rows})
    assignments = {}
    for g in groups:
        h = int(hashlib.sha256(g.encode()).hexdigest()[:8], 16) % 10
        assignments[g] = "train" if h < 8 else ("val" if h == 8 else "test")
    for row in rows:
        if row["sha256"] in seen:
            continue
        seen.add(row["sha256"])
        row["split"] = assignments[row["source_group"]]
        merged.append(row)
    (root / "catalog.json").write_text(json.dumps(merged, indent=2), encoding="utf-8")
    report = {s: dict(Counter(r["motif_class"] for r in merged if r["split"] == s)) for s in ("train","val","test")}
    report["downloaded_accepted"] = len(rows)
    report["failures"] = dict(failures)
    (root / "ingest-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    import sys
    prepare(sys.argv[1], sys.argv[2])
