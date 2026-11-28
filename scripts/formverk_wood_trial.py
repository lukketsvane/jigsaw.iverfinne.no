"""Build the Formverk shop worksheet, audited cut files and portable research ZIP."""

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from formverk_kerf_coupon import build_coupon
from formverk_trial_boards import build_boards
from formverk_trial_styles import build_styles


ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "runs/formverk-wood-trial"
RESEARCH = ROOT / "resources/nervous-system/wood-trial"
TEMPLATE = ROOT / "scripts/templates/formverk-wood-trial.html"


class WorksheetLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.paths.append(dict(attrs)["href"])


def write_package(out, archive):
    """Bundle verified deliverables, excluding working files and physical records."""
    files = {
        "index.html": "Offline worksheet",
        "calibration/coupon-cut.svg": "Cut geometry",
        "calibration/coupon-map.svg": "Measurement reference",
        "calibration/verification.json": "Digital geometry audit",
        "style-sampler/cut.svg": "Cut geometry",
        "style-sampler/placement-map.svg": "Placement reference",
        "style-sampler/sampler-audit.json": "Digital geometry audit",
        "boards/verification.json": "Observer key and independent SVG audit",
        "observer.md": "Digital hypotheses and study conditions",
    }
    for board in ("A", "B"):
        files[f"boards/{board}-cut.svg"] = "Cut geometry"
        files[f"boards/{board}-map.svg"] = "Seam reference"
    for style in ("dendrite", "amoeba", "maze", "wave", "coral", "matrix"):
        files[f"style-sampler/coupons/{style}/cut.svg"] = "Individual coupon for recuts"
    (out / "index.html").write_bytes(TEMPLATE.read_bytes())
    (out / "observer.md").write_bytes((RESEARCH / "observer.md").read_bytes())
    links = WorksheetLinks()
    links.feed(TEMPLATE.read_text(encoding="utf-8"))
    missing = set(links.paths) - (files.keys() | {"manifest.json"})
    if missing:
        raise ValueError(f"Unpackaged worksheet links: {sorted(missing)}")
    payloads = {name: (out / name).read_bytes() for name in files}
    manifest = {
        "package": "Formverk wood trial",
        "entry_point": "index.html",
        "physical_measurements_at_packaging": 0,
        "board_use": "The two Aphex-motif boards are personal/non-commercial research designs.",
        "files": {name: {"purpose": files[name], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for name, data in sorted(payloads.items())},
    }
    payloads["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    (out / "manifest.json").write_bytes(payloads["manifest.json"])
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=archive.parent) as staging:
        temp_zip = Path(staging) / "wood-trial.zip"
        with ZipFile(temp_zip, "w") as package:
            for name, data in sorted(payloads.items()):
                info = ZipInfo(name)
                info.compress_type = ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                package.writestr(info, data)
        with ZipFile(temp_zip) as package:
            if set(package.namelist()) != set(payloads) or package.testzip() is not None:
                raise ValueError("Incomplete trial package")
            for name, expected in payloads.items():
                if package.read(name) != expected:
                    raise ValueError(f"Archive verification failed for {name}")
        temp_zip.replace(archive)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=RUN)
    parser.add_argument("--archive", type=Path, default=RESEARCH / "formverk-wood-trial.zip")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    build_coupon(args.out / "calibration")
    build_boards(args.out / "boards")
    build_styles(args.out / "style-sampler")
    manifest = write_package(args.out, args.archive)
    print(f"Verified {len(manifest['files'])} files and the manifest in {args.archive}")


if __name__ == "__main__":
    main()
