"""Consolidate all Helleristninger deliverables into helleristninger/ and assemble v2026.W48 release.

Preserves all existing repo paths (runs/, docs/, hellerisninger/) so backward-compatible
tests continue to pass 100%.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]
HELLER_DIR = ROOT / "helleristninger"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_tree_clean(src: Path, dst: Path) -> Dict[str, int]:
    if not src.exists():
        raise FileNotFoundError(f"Source directory missing: {src}")
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    files = [p for p in dst.rglob("*") if p.is_file()]
    return {
        "file_count": len(files),
        "total_bytes": sum(p.stat().st_size for p in files),
    }


def consolidate() -> Dict[str, object]:
    mappings = [
        ("puzzle_750", ROOT / "runs" / "helleristning-750", HELLER_DIR / "puzzle_750"),
        ("commercial_pack", ROOT / "runs" / "helleristning-750-commercial", HELLER_DIR / "commercial_pack"),
        ("docs", ROOT / "docs" / "helleristning-750", HELLER_DIR / "docs"),
        ("jigsaw_corpus", ROOT / "hellerisninger" / "processed", HELLER_DIR / "jigsaw_corpus"),
    ]
    inventory: Dict[str, object] = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "consolidated_root": "helleristninger",
        "backward_compatible_originals_preserved": True,
        "folders": {},
    }
    for name, src, dst in mappings:
        stats = copy_tree_clean(src, dst)
        inventory["folders"][name] = {
            "source_repo_path": str(src.relative_to(ROOT)).replace("\\", "/"),
            "consolidated_path": str(dst.relative_to(ROOT)).replace("\\", "/"),
            **stats,
        }

    for existing_name in ("sources", "metadata", "scientific_record", "qa", "pipeline_science", "science", "dataset", "explorer", "releases"):
        p = HELLER_DIR / existing_name
        if p.exists():
            files = [f for f in p.rglob("*") if f.is_file()]
            inventory["folders"][existing_name] = {
                "consolidated_path": str(p.relative_to(ROOT)).replace("\\", "/"),
                "file_count": len(files),
                "total_bytes": sum(f.stat().st_size for f in files),
            }

    manifest_path = HELLER_DIR / "CONSOLIDATION_MANIFEST.json"
    manifest_path.write_text(json.dumps(inventory, indent=2, ensure_ascii=False), encoding="utf-8")
    return inventory


def build_release_manifest(release_dir: Path) -> Dict[str, object]:
    """Snapshot core release artifacts into v2026.W48/data/ and compute SHA-256 RELEASE_MANIFEST.json."""
    snapshot_pairs = [
        (HELLER_DIR / "scientific_record" / "figures.jsonl", release_dir / "data" / "figures.jsonl"),
        (HELLER_DIR / "scientific_record" / "figures.csv", release_dir / "data" / "figures.csv"),
        (HELLER_DIR / "scientific_record" / "schema.json", release_dir / "data" / "schema.json"),
        (HELLER_DIR / "metadata" / "site_gazetteer.json", release_dir / "data" / "site_gazetteer.json"),
        (HELLER_DIR / "metadata" / "literature.json", release_dir / "data" / "literature.json"),
        (HELLER_DIR / "taxonomy.json", release_dir / "data" / "taxonomy.json"),
        (HELLER_DIR / "qa" / "dual_trace_report.json", release_dir / "data" / "dual_trace_report.json"),
        (HELLER_DIR / "qa" / "dual_trace_disagreements.jsonl", release_dir / "data" / "dual_trace_disagreements.jsonl"),
        (HELLER_DIR / "science" / "findings_metrics.json", release_dir / "data" / "findings_metrics.json"),
        (HELLER_DIR / "science" / "findings_v2026.W48.md", release_dir / "data" / "findings_v2026.W48.md"),
        (HELLER_DIR / "docs" / "selected-750-catalog.json", release_dir / "data" / "selected-750-catalog.json"),
        (HELLER_DIR / "docs" / "rights-inventory.csv", release_dir / "data" / "rights-inventory.csv"),
        (HELLER_DIR / "puzzle_750" / "production-audit.json", release_dir / "data" / "puzzle_750_production_audit.json"),
        (HELLER_DIR / "puzzle_750" / "fidelity-audit.json", release_dir / "data" / "puzzle_750_fidelity_audit.json"),
        (HELLER_DIR / "puzzle_750" / "provenance.json", release_dir / "data" / "puzzle_750_provenance.json"),
    ]
    data_dir = release_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    for src, dst in snapshot_pairs:
        if src.exists():
            shutil.copy2(src, dst)

    fig_src = HELLER_DIR / "science" / "figures"
    if fig_src.exists():
        fig_dst = release_dir / "figures"
        if fig_dst.exists():
            shutil.rmtree(fig_dst)
        shutil.copytree(fig_src, fig_dst)

    manifest_file = release_dir / "RELEASE_MANIFEST.json"
    entries: List[Dict[str, object]] = []
    for fpath in sorted(p for p in release_dir.rglob("*") if p.is_file()):
        if fpath == manifest_file:
            continue
        rel = str(fpath.relative_to(release_dir)).replace("\\", "/")
        entries.append({
            "path": rel,
            "bytes": fpath.stat().st_size,
            "sha256": sha256_file(fpath),
        })

    manifest = {
        "release": "v2026.W48",
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file_count": len(entries),
        "total_bytes": sum(int(e["bytes"]) for e in entries),
        "files": entries,
    }
    manifest_file.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["consolidate", "release", "all"], default="all")
    args = parser.parse_args()

    if args.stage in ("consolidate", "all"):
        inv = consolidate()
        print(f"Consolidated {len(inv['folders'])} folders into helleristninger/")
    if args.stage in ("release", "all"):
        rel_dir = HELLER_DIR / "releases" / "v2026.W48"
        man = build_release_manifest(rel_dir)
        print(f"Assembled release v2026.W48: {man['file_count']} files, {man['total_bytes']} bytes")


if __name__ == "__main__":
    main()
