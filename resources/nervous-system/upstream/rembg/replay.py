"""Run inference from the archived rembg source and its original weights."""

import argparse
import hashlib
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import numpy as np
import torch
from PIL import Image


ROOT = Path(__file__).resolve().parent
REVISION = "332c16b58e0194f0960f9d6cf38746e7be8546b7"
WEIGHTS = {
    "u2net": "10025a17f49cd3208afc342b589890e402ee63123d6f2d289a4a0903695cce58",
    "u2netp": "e7567cde013fb64813973ce6e1ecc25a80c05c3ca7adbc5a54f3c3d90991b854",
}


def check_file(path, expected):
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError(f"Unexpected SHA-256 for {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=WEIGHTS, default="u2net")
    parser.add_argument("--image", type=Path, help="Defaults to the upstream animal-1 example.")
    parser.add_argument("--output", type=Path, help="Optional output path for the grayscale mask (PNG).")
    args = parser.parse_args()

    archive = ROOT / f"rembg-{REVISION[:12]}.zip"
    weights = ROOT / "weights" / f"{args.model}.pth"
    if not weights.is_file():
        parser.error(f"Missing {weights.name}. Download it from the model table in SOURCE.md.")
    check_file(archive, "86de06c970475cbb63d56b458f66f3548b1ccd08639f1aa439bb0b2cb333bfb6")
    check_file(weights, WEIGHTS[args.model])

    prefix = f"rembg-{REVISION}"
    sys.path.insert(0, f"{archive.as_posix()}/{prefix}/src")
    from rembg.u2net import detect, u2net

    with ZipFile(archive) as source:
        if args.image:
            with Image.open(args.image) as image:
                rgb = image.convert("RGB")
        else:
            rgb = Image.open(BytesIO(source.read(f"{prefix}/examples/animal-1.jpg"))).convert("RGB")
            reference = Image.open(BytesIO(source.read(f"{prefix}/examples/animal-1.out.png"))).getchannel("A")

    model_type = u2net.U2NET if args.model == "u2net" else u2net.U2NETP
    model = model_type(3, 1)
    model.load_state_dict(torch.load(weights, map_location="cpu", weights_only=True))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device).eval()
    mask = detect.predict(model, np.asarray(rgb)).convert("L").resize(rgb.size, Image.Resampling.LANCZOS)

    if args.output:
        mask.save(args.output, format="PNG")
    print(f"{args.model} on {device}: {mask.width} x {mask.height} mask, range {mask.getextrema()}")
    if not args.image:
        actual = np.asarray(mask) >= 128
        expected = np.asarray(reference) >= 128
        overlap = np.count_nonzero(actual & expected) / np.count_nonzero(actual | expected)
        print(f"Foreground intersection over union with upstream sample: {overlap:.5f}")


if __name__ == "__main__":
    main()
