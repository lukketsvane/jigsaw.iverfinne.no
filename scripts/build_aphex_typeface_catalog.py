"""Build a target catalog from the local Aphex logo and typeface outlines."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pathnet.train import flatten

DATASET = Path("pathnet/aphex-glyph-dataset.json")
REFERENCE = Path("pathnet/reference.json")
LOGO_IMAGE = Path("pathnet/IMG_4659.png")
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
VARIANTS = ("AE-50", "CR-50", "FN-30", "KM-50", "XZ-70")
SIZE = 256
PADDING = 16


def raster_mask(points, label):
    points = np.asarray(points, dtype=np.float64)
    lower, upper = points.min(axis=0), points.max(axis=0)
    span = max(upper - lower)
    if span <= 0 or not np.isfinite(points).all():
        raise ValueError(f"Invalid outline for {label}")
    pixels = (points - (lower + upper) / 2) * ((SIZE - 1 - 2 * PADDING) / span)
    pixels = np.rint(pixels + (SIZE - 1) / 2).astype(np.int32)
    mask = np.zeros((SIZE, SIZE), dtype=np.uint8)
    cv2.fillPoly(mask, [pixels], 255)
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if len(contours) != 1 or cv2.contourArea(contours[0]) <= 0:
        raise ValueError(f"Expected a single solid piece for {label}")
    return mask


def build_catalog(out: Path, pieces: int = 32, logo_scale: float = 2.6):
    if pieces < 27:
        raise ValueError("Use at least 27 pieces for the logo and all 26 letters")
    dataset = json.loads((ROOT / DATASET).read_text(encoding="utf-8"))
    reference = json.loads((ROOT / REFERENCE).read_text(encoding="utf-8"))
    glyphs = {record["glyph"]: record for record in dataset["glyphs"]}
    variants = {record["id"]: record for record in dataset["variants"]}
    targets = []

    def add(asset_id, family, motif_class, glyph_label, glyph_kind, source_asset, points, **metadata):
        record = dict(
            asset_id=asset_id, family=family, motif_class=motif_class, glyph_label=glyph_label,
            glyph_kind=glyph_kind, source_archive="pathnet", source_group=str(source_asset.as_posix()),
            source_asset=source_asset.as_posix(), commercial_ok=False, area_scale=1.0,
        )
        record.update(metadata)
        targets.append((record, raster_mask(points, glyph_label)))

    add("aphex-logo", "logo", "aphex_logo", "Aphex Twin logo", "logo", REFERENCE,
        flatten(np.asarray(reference["logo_control_points"]), steps=8),
        area_scale=float(logo_scale), source_image=LOGO_IMAGE.as_posix(), source_record="logo_control_points",
        license="Logo rights not cleared")
    for letter in LETTERS:
        row = glyphs[letter]
        add(f"aphex-glyph-{letter.lower()}", "typeface", f"aphex_glyph_{letter.lower()}", letter,
            "letter", DATASET, row["piece"], source_record=letter, typeface_source=dataset["source"],
            adaptation=row["adaptation"], license=dataset["license"])
    variant_order = VARIANTS if pieces == 32 else (*VARIANTS, *sorted(variants.keys() - set(VARIANTS)))
    for variant in variant_order:
        if len(targets) == pieces:
            break
        row = variants[variant]
        try:
            add(f"aphex-variant-{variant.lower()}", "typeface", f"aphex_variant_{variant.lower()}", variant,
                "blend", DATASET, row["piece"], source_record=variant, typeface_source=dataset["source"],
                parents=row["parents"], interpolation=row["interpolation"], mirrored_second=row["mirrored_second"],
                license=dataset["license"])
        except ValueError as error:
            if variant in VARIANTS:
                raise
            print(f"Skipping {variant}. {error}", file=sys.stderr)

    out.mkdir(parents=True)
    catalog = []
    for record, mask in targets:
        path = out / f"{record['asset_id']}.png"
        Image.fromarray(mask).save(path)
        record.update(mask_path=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        catalog.append(record)
    # Exhaust the distinct outlines before reusing any. The logo stays unique.
    original_glyphs = catalog[1:]
    repeats = Counter()
    for index in range(pieces - len(catalog)):
        original = original_glyphs[index % len(original_glyphs)]
        asset_id = original["asset_id"]
        repeats[asset_id] += 1
        catalog.append(dict(original, asset_id=f"{asset_id}-repeat-{repeats[asset_id]}",
                            repeat_of=asset_id))
    (out / "catalog.json").write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    return catalog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="Fresh target folder")
    parser.add_argument("--pieces", type=int, default=32, help="Piece count including one logo (minimum 27)")
    parser.add_argument("--logo-scale", type=float, default=2.6, help="Target area multiplier for the centerpiece logo")
    args = parser.parse_args()
    if args.out.exists():
        parser.error(f"Output already exists: {args.out}")
    if args.pieces < 27:
        parser.error("--pieces must be at least 27")
    records = build_catalog(args.out, args.pieces, logo_scale=args.logo_scale)
    print(f"Wrote {len(records)} Aphex targets to {args.out / 'catalog.json'}")


if __name__ == "__main__":
    main()
