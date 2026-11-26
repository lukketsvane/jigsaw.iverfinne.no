"""Retrain Aphex and continue the Animal and Helleristning models concurrently."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time


REPO = Path(__file__).resolve().parents[1]
SHAPE_CATALOGS = {
    "animal": "runs/animal-cc0-only/catalog.json",
    "helleristning": "hellerisninger/processed/catalog.json",
}


def shape_command(edition, source, out, extra_epochs, checkpoint_seconds):
    """Build a continuation with the completed run's training configuration."""
    checkpoint = source / "training-last.pt"
    if not checkpoint.is_file():
        raise ValueError(f"Missing resumable checkpoint {checkpoint}")

    report = json.loads((source / "metrics.json").read_text(encoding="utf-8"))
    config = report["config"]
    epochs = report["epochs"]
    if not isinstance(epochs, int) or epochs < 1 or len(report["history"]) != epochs:
        raise ValueError(f"Expected completed training metrics in {source}")
    if config.get("device") != "cuda" or config.get("trainer") != "animal_sdf_vae":
        raise ValueError(f"Expected a CUDA shape VAE run in {source}")
    families = config.get("families", ["animal"])
    if families is not None and families != ["animal"]:
        raise ValueError(f"The CLI does not support the family filter in {source}")

    cmd = [
        sys.executable, "-m", "heller_jigsaw", "train-shapes", "--resume",
        "--out", str(out), "--corpus", SHAPE_CATALOGS[edition],
        "--epochs", str(epochs + extra_epochs), "--checkpoint-seconds", str(checkpoint_seconds),
    ]
    required = {"size": "--img-size", "latent_dim": "--latent-dim", "batch_size": "--batch-size",
                "lr": "--lr", "seed": "--seed"}
    optional = {"width": "--width", "stages": "--stages", "min_train_count": "--min-train-count",
                "eikonal_weight": "--eikonal-weight", "kl_weight": "--kl-weight",
                "aug_rotation": "--aug-rotation", "aug_scale": "--aug-scale"}
    for key, flag in required.items():
        cmd.extend([flag, str(config[key])])
    for key, flag in optional.items():
        if key in config:
            cmd.extend([flag, str(config[key])])
    if config.get("balanced"):
        cmd.append("--balanced")
    if families is None:
        cmd.append("--all-families")
    return cmd


def stop_workers(active):
    previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        for process in active.values():
            if process.poll() is not None:
                continue
            if os.name == "nt":
                # The venv launcher can have its own Python child on Windows.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            if process.poll() is None:
                process.terminate()
        for process in active.values():
            process.wait()
    finally:
        signal.signal(signal.SIGINT, previous_handler)


def run_commands(commands, out, aphex_delay):
    env = dict(os.environ, OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", PYTHONUNBUFFERED="1")
    # Ctrl+C reaches the coordinator, which cleans up each worker's process tree.
    group_options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                     else {"start_new_session": True})
    active = {}
    failed = False
    try:
        for edition, command in commands.items():
            if edition == "aphex":
                time.sleep(aphex_delay)
            log_path = out / f"{edition}.log"
            try:
                with log_path.open("wb") as log:
                    active[edition] = subprocess.Popen(
                        command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, **group_options,
                    )
                print(f"Started {edition}. Log at {log_path}", flush=True)
            except OSError as exc:
                print(f"Could not start {edition} ({exc})", file=sys.stderr, flush=True)
                failed = True

        for edition, process in active.items():
            code = process.wait()
            if code:
                print(f"{edition} exited with code {code}. See {out / (edition + '.log')}", file=sys.stderr, flush=True)
                failed = True
            else:
                print(f"Finished {edition}", flush=True)
    except KeyboardInterrupt:
        stop_workers(active)
        print("Interrupted training. Completed checkpoints are available in the output directory.",
              file=sys.stderr, flush=True)
        return 130
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="Fresh output directory")
    parser.add_argument("--animal-from", required=True, type=Path, help="Completed Animal run directory")
    parser.add_argument("--helleristning-from", required=True, type=Path, help="Completed Helleristning run directory")
    parser.add_argument("--extra-epochs", type=int, default=24, help="Additional epochs for each shape VAE")
    parser.add_argument("--aphex-delay", type=float, default=4, help="Seconds to wait before launching Aphex")
    parser.add_argument("--checkpoint-seconds", type=float, default=10, help="Minimum seconds between VAE checkpoints")
    args = parser.parse_args()
    if args.extra_epochs < 1:
        parser.error("--extra-epochs must be positive")
    if any(not math.isfinite(value) or value < 0 for value in (args.aphex_delay, args.checkpoint_seconds)):
        parser.error("--aphex-delay and --checkpoint-seconds must be finite and non-negative")

    out = args.out.resolve()
    sources = {"animal": args.animal_from.resolve(), "helleristning": args.helleristning_from.resolve()}
    if any(out == source or out.is_relative_to(source) for source in sources.values()):
        parser.error("The output must be outside both source run directories")
    try:
        commands = {
            edition: shape_command(edition, source, out / edition, args.extra_epochs, args.checkpoint_seconds)
            for edition, source in sources.items()
        }
        out.mkdir(parents=True)
        for edition, source in sources.items():
            (out / edition).mkdir()
            shutil.copyfile(source / "training-last.pt", out / edition / "training-last.pt")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))

    commands["aphex"] = [
        sys.executable, "pathnet/train.py", "--device", "cuda",
        "--epochs", "80", "--batch-size", "256", "--out", str(out / "aphex"),
    ]
    return run_commands(commands, out, args.aphex_delay)


if __name__ == "__main__":
    sys.exit(main())
