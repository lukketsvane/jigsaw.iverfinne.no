import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOTS = [
    "hellerisninger/processed",
    "runs/phylopic-nordic-data",
    "runs/animal-cc0-only",
    "runs/scale32_targets",
    "runs/animal-cc0-shapes-v7",
    "runs/animal-shapes-latent-v3",
    "runs/recognizer-animals-v2",
    "runs/recognizer-nordic-v2",
    "runs/production-candidates-v9",
    "runs/reference_v9_locked",
    "runs/reference_v9_thin",
    "runs/scale32_animal_cc0_v7_thin_s42",
    "runs/scale32_aphex_thinmargin_s42",
    "runs/scale32_helleristning_thinmargin_s42",
    "runs/v10-campaign",
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def pack(source, output):
    source = source.resolve()
    if output.exists():
        raise FileExistsError(output)
    paths = set()
    for rel in ROOTS:
        root = source / rel
        if not root.is_dir():
            raise FileNotFoundError(root)
        paths.update(p for p in root.rglob("*") if p.is_file())
    paths.update((source / "runs").glob("*.py"))
    paths.update((source / "runs").glob("*corpus*.json"))
    files = sorted(p for p in paths if p.suffix != ".tmp" and "__pycache__" not in p.parts)
    manifest = []
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    try:
        with ZipFile(temp, "w", ZIP_DEFLATED, compresslevel=3) as archive:
            for path in files:
                name = path.relative_to(source).as_posix()
                data = path.read_bytes()
                archive.writestr(name, data)
                manifest.append({"path": name, "bytes": len(data), "sha256": digest(data)})
            archive.writestr("TRANSFER_MANIFEST.json", json.dumps(manifest, indent=2))
        with ZipFile(temp) as archive:
            for row in manifest:
                data = archive.read(row["path"])
                if len(data) != row["bytes"] or digest(data) != row["sha256"]:
                    raise RuntimeError(row["path"])
        temp.replace(output)
    finally:
        temp.unlink(missing_ok=True)
    print(f"Verified archive: {output} | {len(manifest)} files | {output.stat().st_size} bytes")


def verify(root):
    manifest = json.loads((root / "TRANSFER_MANIFEST.json").read_text(encoding="utf-8"))
    for row in manifest:
        data = (root / row["path"]).read_bytes()
        if len(data) != row["bytes"] or digest(data) != row["sha256"]:
            raise RuntimeError(row["path"])
    print(f"Verified {len(manifest)} extracted files")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()
    if args.command == "pack":
        pack(args.source, args.output)
    else:
        verify(args.root)
