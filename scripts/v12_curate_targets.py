"""Choose CC0 animal targets whose silhouettes survive laser-cut manufacture.

Each candidate mask is scaled to the finished piece area, then subjected to the
fabrication limits the puzzle must meet: features thinner than the 3 mm neck are
opened away and gaps narrower than it are closed. The fidelity of what survives
(``heller_jigsaw.production.fidelity.piece_fidelity``) ranks candidates; a small
bonus favours non-blob outlines (low solidity) because those carry identity.
Near-duplicate taxa are skipped. The original source catalog is not modified.

    python scripts/v12_curate_targets.py --area 2400 --out runs/scale32_targets/animal_cc0_curated_v12
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.affinity import scale
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from heller_jigsaw.data.corpus import load_corpus_catalog, load_motif_asset  # noqa: E402
from heller_jigsaw.geometry.validate import analyze_piece_fabrication  # noqa: E402
from heller_jigsaw.production.fidelity import piece_fidelity  # noqa: E402

QUOTA = dict(bear=7, bird=7, fish=6, whale_cetacean=6, quadruped_other=6)


def polygon(mask, area):
    cs, _ = cv2.findContours((mask > 0.5).astype("uint8"), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if len(cs) != 1:
        return None
    p = Polygon(cs[0].reshape(-1, 2) + 0.5).buffer(0)
    if p.geom_type != "Polygon" or p.interiors or p.area < 200:
        return None
    s = math.sqrt(area / p.area)
    return scale(p, xfact=s, yfact=s, origin="centroid").simplify(0.1)


def survival(p, neck=3.0, kerf=0.25):
    r = neck / 2 + kerf / 2
    made = p.buffer(r, quad_segs=8).buffer(-r, quad_segs=8)  # narrow gaps close
    made = made.buffer(-r, quad_segs=8).buffer(r, quad_segs=8)  # thin parts break off
    if made.is_empty:
        return None
    if made.geom_type != "Polygon":
        made = max(made.geoms, key=lambda g: g.area)
    return piece_fidelity(made, p)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", default="runs/animal-cc0-only/catalog.json")
    ap.add_argument("--area", type=float, default=2400.0)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = ROOT / a.out
    if (out / "catalog.json").exists():
        raise FileExistsError(out)
    rows, base = load_corpus_catalog(ROOT / a.catalog)
    scored = []
    for r in rows:
        if r.get("license") != "CC0-1.0" or r["motif_class"] not in QUOTA:
            continue
        m = load_motif_asset(r, base)
        p = polygon(m.mask, a.area)
        if p is None:
            continue
        fab = analyze_piece_fabrication(p, kerf_mm=0.25, min_neck_mm=3)
        s = survival(p)
        if s is None:
            continue
        solidity = p.area / p.convex_hull.area
        score = s["fidelity"] + 0.15 * (1 - solidity) + (0.0 if fab["neck_pass"] else -0.05)
        scored.append(dict(rec=r, score=score, survival=s, solidity=solidity, neck_pass=bool(fab["neck_pass"])))
    picked = []
    for cls, k in QUOTA.items():
        genera = set()
        for c in sorted((c for c in scored if c["rec"]["motif_class"] == cls), key=lambda c: -c["score"]):
            genus = str(c["rec"].get("taxon", c["rec"]["asset_id"])).split()[0]
            if genus in genera:
                continue
            genera.add(genus)
            picked.append(c)
            if sum(p["rec"]["motif_class"] == cls for p in picked) == k:
                break
    out.mkdir(parents=True)
    catalog = []
    for i, c in enumerate(picked):
        r = dict(c["rec"])
        src = load_motif_asset(r, base)
        path = out / f"{i:02d}.png"
        Image.fromarray((src.mask > 0.5).astype("uint8") * 255).save(path)
        r.update(mask_path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                 parent_asset_id=r["asset_id"], curation_score=round(c["score"], 4),
                 manufactured_fidelity=round(c["survival"]["fidelity"], 4), solidity=round(c["solidity"], 4))
        r.pop("sdf_path", None)
        catalog.append(r)
    (out / "catalog.json").write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    report = dict(
        candidates=len(scored), piece_area_mm2=a.area, quota=QUOTA,
        manufactured_fidelity_mean=float(np.mean([c["survival"]["fidelity"] for c in picked])),
        manufactured_fidelity_min=float(min(c["survival"]["fidelity"] for c in picked)),
        pool_manufactured_fidelity_mean=float(np.mean([c["survival"]["fidelity"] for c in scored])),
        selected=[dict(asset_id=c["rec"]["asset_id"], taxon=c["rec"].get("taxon"), cls=c["rec"]["motif_class"],
                       score=round(c["score"], 4), manufactured_fidelity=round(c["survival"]["fidelity"], 4),
                       solidity=round(c["solidity"], 4)) for c in picked])
    (out / "curation-report.json").write_text(json.dumps(report, indent=2))
    # Contact sheet
    font = ImageFont.load_default()
    sheet = Image.new("RGB", (8 * 140, math.ceil(len(picked) / 8) * 150), "white")
    d = ImageDraw.Draw(sheet)
    for i, c in enumerate(picked):
        m = load_motif_asset(c["rec"], base).mask
        im = Image.fromarray(((m < 0.5) * 255).astype("uint8")).resize((120, 120), Image.Resampling.LANCZOS)
        x, y = (i % 8) * 140 + 10, (i // 8) * 150 + 4
        sheet.paste(im, (x, y))
        d.text((x, y + 122), f"{str(c['rec'].get('taxon', ''))[:20]}", fill="black", font=font)
        d.text((x, y + 134), f"{c['rec']['motif_class'][:10]} {c['survival']['fidelity']:.2f}", fill="#555", font=font)
    sheet.save(out / "curated-targets.png")
    print(json.dumps({k: v for k, v in report.items() if k != "selected"}, indent=2))


if __name__ == "__main__":
    main()
