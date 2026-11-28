"""Decode one real PathNet contour using the repository sampler, on CPU."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

import numpy as np


PATHNET = Path(__file__).resolve().parents[2] / "pathnet"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, default=PATHNET / "pathnet.pt",
                        help="trained PathNet v1 or v2 checkpoint (default: tracked v1)")
    parser.add_argument("--family", type=int, default=0, help="zero-based family ID (0 is the logo)")
    parser.add_argument("--seed", type=int, default=22, help="non-negative sampling seed")
    parser.add_argument("--svg", action="store_true", help="print the closed SVG outline instead of JSON")
    args = parser.parse_args()
    checkpoint = args.checkpoint.resolve()
    if not checkpoint.is_file():
        parser.error(f"checkpoint file missing: {checkpoint}")
    if args.seed < 0:
        parser.error("--seed must be non-negative")

    # The canonical sampler reads this setting and imports sibling modules on startup.
    os.environ["PATHNET_CKPT"] = str(checkpoint)
    sys.path.insert(0, str(PATHNET))
    import generate
    from train import bezier, path

    if not 0 <= args.family < len(generate.names):
        parser.error(f"--family must be between 0 and {len(generate.names) - 1}")
    controls, polygon, latent = generate.sample(np.random.default_rng(args.seed), args.family)
    if args.svg:
        x0, y0, x1, y1 = polygon.bounds
        pad = 0.06 * max(x1 - x0, y1 - y0)
        viewbox = f"{x0 - pad:.6f} {y0 - pad:.6f} {x1 - x0 + 2 * pad:.6f} {y1 - y0 + 2 * pad:.6f}"
        print(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">'
              f'<path d="{path(controls)}"/></svg>')
        return

    segments = bezier(controls)
    with checkpoint.open("rb") as stream:
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    report = {
        "checkpoint_sha256": digest.hexdigest(),
        "architecture": generate.ck.get("arch", "pathnet-v1"),
        "device": next(generate.model.parameters()).device.type,
        "parameters": sum(p.numel() for p in generate.model.parameters()),
        "family_id": args.family,
        "family_name": generate.names[args.family],
        "families": len(generate.names),
        "seed": args.seed,
        "latent": latent.round(6).tolist(),
        "control_shape": list(controls.shape),
        "bezier_segment_shape": list(segments.shape),
        "max_join_gap": float(np.linalg.norm(segments[:, 3] - np.roll(segments[:, 0], -1, axis=0), axis=1).max()),
        "valid_sampled_polygon": polygon.is_valid,
        "area_normalized_units_squared": round(polygon.area, 6),
    }
    if args.family == 0:
        ref = generate.ref
        report["logo_reference_iou"] = round(polygon.intersection(ref).area / polygon.union(ref).area, 6)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
