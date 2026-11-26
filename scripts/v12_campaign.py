"""V12 production campaign: layouts -> locks -> independent audits -> guarded promotion.

Every stage writes to its own folder under ``runs/v12`` and skips work that is
already complete, so the campaign can be interrupted and resumed. Previous
verified runs are only read, never written.

    python scripts/v12_campaign.py baseline
    python scripts/v12_campaign.py compose --workers 7
    python scripts/v12_campaign.py lock --top 3 --workers 6
    python scripts/v12_campaign.py audit --workers 6
    python scripts/v12_campaign.py select
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = Path("runs/v12")
PY = sys.executable

FAMILIES = {
    "aphex": dict(catalog="runs/scale32_targets/aphex/catalog.json"),
    "animal_reference": dict(catalog="runs/scale32_targets/animal_cc0_reference_v9/catalog.json"),
    "animal_hybrid": dict(catalog="runs/scale32_targets/animal_sourceguided_v10/catalog.json",
                          reference="runs/scale32_targets/animal_cc0_reference_v9/catalog.json"),
    "helleristning": dict(catalog="runs/scale32_targets/helleristning/catalog.json"),
}
PREVIOUS = {
    "aphex": "runs/v10-campaign/verified-winners/aphex",
    "animal_reference": "runs/v10-campaign/verified-winners/animal_reference",
    "animal_hybrid": "runs/v10-campaign/verified-winners/animal_hybrid",
    "helleristning": "runs/v10-campaign/verified-winners/helleristning",
    "animal": "runs/v10-campaign/verified-winners/animal",
    "helleristning_v11_robust_experimental": "runs/v11-fabrication/verified-robust-edition",
}
AREAS = (1850, 2400)
SEEDS = (1201, 1202, 1203, 1204, 1205, 1206)


def sh(cmd, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    t = time.perf_counter()
    with open(log, "w", encoding="utf-8") as fh:
        code = subprocess.call(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                               env=dict(os.environ, PYTHONUNBUFFERED="1", OMP_NUM_THREADS="1"))
    return dict(cmd=cmd[2:], exit=code, seconds=round(time.perf_counter() - t, 1), log=str(log))


def pool(jobs, workers):
    results = []
    with ThreadPoolExecutor(workers) as ex:
        for r in ex.map(lambda j: sh(*j), jobs):
            print(("ok  " if r["exit"] == 0 else f"exit {r['exit']} ") + " ".join(map(str, r["cmd"][:6])), r["seconds"], flush=True)
            results.append(r)
    return results


def reference_for(config):
    for fam, spec in FAMILIES.items():
        if Path(spec["catalog"]).as_posix() == Path(config["corpus_source"]).as_posix():
            return spec.get("reference")
    return None


def fidelity(run_dir, reference=None):
    from heller_jigsaw.production.fidelity import fidelity_audit
    return fidelity_audit(run_dir, reference_catalog=reference, write=False)


def baseline(_):
    dest = OUT / "baselines"
    dest.mkdir(parents=True, exist_ok=True)
    table = {}
    for name, path in PREVIOUS.items():
        cfg = json.loads((Path(path) / "config.json").read_text())
        rep = fidelity(path, reference_for(cfg))
        (dest / f"{name}.json").write_text(json.dumps(rep, indent=2))
        table[name] = {k: round(v["mean"], 4) for k, v in rep["summary"].items()}
        table[name]["fidelity_score"] = round(rep["summary"]["fidelity"]["score"], 4)
        table[name]["fidelity_min"] = round(rep["summary"]["fidelity"]["minimum"], 4)
        print(name, table[name], flush=True)
    (dest / "summary.json").write_text(json.dumps(table, indent=2))


def compose(args):
    jobs = []
    for fam, spec in FAMILIES.items():
        for area in AREAS:
            for i, seed in enumerate(SEEDS):
                out = OUT / "layouts" / fam / f"a{area}-s{seed}-m{i % 4}"
                if (out / "trial.json").exists():
                    continue
                if out.exists():
                    shutil.rmtree(out)
                cmd = [PY, "-m", "heller_jigsaw", "compose", "--catalog", spec["catalog"], "--seed", str(seed),
                       "--mode", str(i % 4), "--piece-area", str(area), "--objective", "fidelity",
                       "--configs", "extended", "--out", str(out)]
                if spec.get("reference"):
                    cmd += ["--reference-catalog", spec["reference"]]
                jobs.append((cmd, OUT / "logs" / f"compose-{fam}-a{area}-s{seed}.log"))
    print(len(jobs), "layout jobs", flush=True)
    pool(jobs, args.workers)


def lock(args):
    jobs = []
    for fam in FAMILIES:
        trials = []
        for t in (OUT / "layouts" / fam).glob("*/trial.json"):
            r = json.loads(t.read_text())
            if r.get("best") and r["best"].get("export_pass"):
                trials.append((r["best"]["score"], r["piece_area_mm2"], t.parent))
        for area in AREAS:
            picked = sorted([t for t in trials if t[1] == area], key=lambda t: -t[0])[:args.top]
            for _, _, src in picked:
                for method in ("standard", "mincost"):
                    out = OUT / "locked" / fam / f"{src.name}-{method}"
                    if (out / "production-audit.json").exists():
                        continue
                    if out.exists():
                        shutil.rmtree(out)
                    jobs.append(([PY, "-m", "heller_jigsaw", "lock", "--run-dir", str(src), "--method", method, "--out", str(out)],
                                 OUT / "logs" / f"lock-{fam}-{src.name}-{method}.log"))
    print(len(jobs), "lock jobs", flush=True)
    pool(jobs, args.workers)


def audit(args):
    jobs = []
    for run in sorted((OUT / "locked").glob("*/*")):
        if (run / "production-audit.json").exists() and not (run / "rigid-motion-screen.json").exists():
            jobs.append(([PY, "-m", "heller_jigsaw", "audit-fabrication", "--run-dir", str(run), "--rigid"],
                         OUT / "logs" / f"audit-{run.parent.name}-{run.name}.log"))
    print(len(jobs), "audit jobs", flush=True)
    pool(jobs, args.workers)
    for run in sorted((OUT / "locked").glob("*/*")):
        if (run / "rigid-motion-screen.json").exists() and not (run / "fidelity-audit.json").exists():
            cfg = json.loads((run / "config.json").read_text())
            from heller_jigsaw.production.fidelity import fidelity_audit
            fidelity_audit(run, reference_catalog=reference_for(cfg))


def bed_fit(run):
    """Minimum rotated bounding box and fit on common laser beds (both orientations)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    d = json.loads((run / "partition.json").read_text())
    rect = unary_union([Polygon(f["exterior"]) for f in d["faces"]]).minimum_rotated_rectangle
    xy = list(rect.exterior.coords)
    import math
    sides = sorted(math.dist(xy[i], xy[i + 1]) for i in range(2))
    beds = {"600x400": (400, 600), "700x500": (500, 700), "900x600": (600, 900)}
    return dict(min_box_mm=[round(sides[0], 1), round(sides[1], 1)],
                fits=[k for k, (a, b) in beds.items() if sides[0] + 10 <= a and sides[1] + 10 <= b])


def gates(run):
    """Every digital gate the CLI audit applies, re-read from the written reports."""
    g = json.loads((run / "production-audit.json").read_text())
    t = json.loads((run / "finite-motion-audit.json").read_text())
    thin = json.loads((run / "thin-feature-audit.json").read_text())
    rigid = json.loads((run / "rigid-motion-screen.json").read_text())
    ok = g["geometry_pass"] and g["lock_margin_mm"] >= 3
    ok = ok and all(g[f"kerf_{k:.2f}"]["kerf_pass"] == g["n"] and g[f"kerf_{k:.2f}"]["neck_pass"] == g["n"] for k in (0.1, 0.15, 0.2, 0.25))
    ok = ok and t["unblocked_directions_by_distance"]["1.0"] == 0 and thin["worst_tip_loss_mm"] <= 6.0
    ok = ok and rigid["pieces_with_rotation_4deg_or_more"] == 0 and rigid["pieces_with_translation_3mm"] == 0
    return bool(ok), dict(lock_margin_mm=g["lock_margin_mm"], unblocked_3mm=t["unblocked_directions_by_distance"]["3.0"],
                          worst_tip_mm=round(thin["worst_tip_loss_mm"], 2), tips_over_3mm=thin["pieces_tip_loss_over_3mm"])


def select(_):
    base = json.loads((OUT / "baselines" / "summary.json").read_text())
    manifest = {}
    for fam in FAMILIES:
        rows = []
        for run in sorted((OUT / "locked" / fam).glob("*")):
            if not (run / "fidelity-audit.json").exists():
                continue
            ok, info = gates(run)
            fid = json.loads((run / "fidelity-audit.json").read_text())["summary"]
            rows.append(dict(run=str(run), gates_pass=ok, **info, bed=bed_fit(run),
                             fidelity_score=fid["fidelity"]["score"], fidelity_mean=fid["fidelity"]["mean"],
                             fidelity_min=fid["fidelity"]["minimum"], boundary_f=fid["boundary_f"]["mean"],
                             bay_fill=fid["bay_fill"]["mean"], iou=fid["iou"]["mean"]))
        prev = base.get(fam, {})
        eligible = [r for r in rows if r["gates_pass"]]
        best = max(eligible, key=lambda r: r["fidelity_score"], default=None)
        promote = bool(best and best["fidelity_score"] > prev.get("fidelity_score", 0) + 0.01
                       and best["fidelity_min"] >= prev.get("fidelity_min", 0) - 0.02)
        manifest[fam] = dict(previous=PREVIOUS.get(fam), previous_fidelity=prev, candidates=len(rows),
                             eligible=len(eligible), best=best, promoted=promote,
                             physical_test="not performed", commercial_release="not cleared",
                             all=sorted(rows, key=lambda r: -r["fidelity_score"]))
        if promote:
            dest = OUT / "verified-winners" / fam
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(best["run"], dest)
        print(fam, "promoted" if promote else "kept previous", best and round(best["fidelity_score"], 4),
              "vs", prev.get("fidelity_score"), flush=True)
    (OUT / "verified-winners").mkdir(parents=True, exist_ok=True)
    (OUT / "verified-winners" / "manifest.json").write_text(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("stage", choices=["baseline", "compose", "lock", "audit", "select"])
    p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    p.add_argument("--top", type=int, default=3)
    a = p.parse_args()
    os.chdir(ROOT)
    globals()[a.stage](a)
