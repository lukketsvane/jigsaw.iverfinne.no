"""Queued shape-VAE sweep that keeps the GPU busy and records what each model has learnt.

    python scripts/shape_sweep.py plan   --root runs/shape-sweep
    python scripts/shape_sweep.py run    --root runs/shape-sweep
    python scripts/shape_sweep.py report --root runs/shape-sweep
    python scripts/shape_sweep.py extend --root runs/shape-sweep   # add KL / augmentation runs

`run` starts one training process per queued configuration and adds another whenever
the GPU has spare capacity. Every process calls ``train_shapes`` unchanged, so run
folders hold the usual ``animal-shape-vae.pt`` and ``metrics.json``. Two things are
added from outside the trainer: full checkpoints are written at most once a minute
(they are 100-220 MB at 192-256 px), and reconstructions of ten fixed validation
silhouettes are stored at selected epochs. `report` draws them as a dashboard PNG.

Edit ``control.json`` in the root while `run` is active to change ``max_workers``,
``target_util``, ``vram_limit_mib`` or ``paused``; new rows appended to ``queue.json``
are picked up too.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

CATALOG = "runs/animal-cc0-only/catalog.json"
SNAP_EPOCHS = (1, 2, 3, 5, 8, 12, 20, 30, 50, 75, 100, 150, 200, 250, 300, 400, 500, 600)
PREVIEW_SEED = 20261126

# size, stages, width, latent, batch, lr, eikonal weight
GRID = [
    # resolution ladder
    (96, 3, 1, 48, 16, 1e-3, 0.0),
    (256, 5, 2, 64, 32, 1e-3, 0.0),
    (128, 3, 1, 48, 16, 1e-3, 0.0),
    (192, 4, 2, 64, 32, 1e-3, 0.0),
    # latent size at 96 px
    (96, 3, 1, 24, 16, 1e-3, 0.0),
    (96, 3, 1, 96, 16, 1e-3, 0.0),
    (96, 3, 1, 160, 16, 1e-3, 0.0),
    # capacity
    (96, 3, 2, 48, 16, 1e-3, 0.0),
    (96, 3, 4, 96, 16, 1e-3, 0.0),
    (256, 5, 4, 96, 32, 1e-3, 0.0),
    # optimiser
    (96, 3, 1, 48, 32, 1e-3, 0.0),
    (96, 3, 1, 48, 16, 5e-4, 0.0),
    (96, 3, 1, 48, 16, 2e-3, 0.0),
    (192, 3, 1, 64, 32, 1e-3, 0.0),
    # contour regularity
    (96, 3, 1, 48, 16, 1e-3, 0.05),
    (96, 3, 1, 48, 16, 1e-3, 0.2),
    # mid and high resolution capacity
    (128, 4, 2, 96, 16, 1e-3, 0.0),
    (192, 4, 4, 96, 32, 1e-3, 0.0),
    (256, 4, 2, 64, 32, 1e-3, 0.0),
    (256, 5, 2, 128, 16, 1e-3, 0.0),
]


# Optional train_shapes arguments; a queue row carries one only when it differs from the trainer default.
TRAINER_TAGS = (("kl_weight", "kl"), ("aug_rotation", "rot"), ("aug_scale", "sc"))
TRAINER_DEFAULTS = dict(kl_weight=0.045, aug_rotation=0.35, aug_scale=0.15)
REGULARISATION = dict(kl_weight=(0.01, 0.045, 0.1), aug_rotation=(0.35, 1.0, 3.14), aug_scale=(0.15, 0.35))


# ----------------------------------------------------------------------------- files

def write_atomic(path: Path, data: bytes) -> bool:
    """Replace `path`, retrying while a reader holds the old file open (Windows)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        for _ in range(60):
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(0.02)
    except OSError:
        pass
    try:
        tmp.unlink()
    except OSError:
        pass
    return False


def read_bytes(path: Path):
    for _ in range(25):
        try:
            return Path(path).read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(0.02)
    return None


def read_json(path: Path, default=None):
    for _ in range(5):
        data = read_bytes(path)
        if data is None:
            return default
        try:
            return json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            time.sleep(0.05)
    return default


def hold_lock(path: Path):
    """Return an open handle that owns `path` until the process ends, or None if another process owns it."""
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def is_locked(path: Path) -> bool:
    if not path.exists():
        return False
    handle = hold_lock(path)
    if handle is None:
        return True
    handle.close()
    return False


# ----------------------------------------------------------------------------- queue

def config_key(c) -> str:
    name = f"s{c['size']}-st{c['stages']}-w{c['width']}-z{c['latent']}-b{c['batch']}"
    if c["lr"] != 0.001:
        name += f"-lr{c['lr']:g}"
    if c["eikonal"]:
        name += f"-eik{c['eikonal']:g}"
    for key, tag in TRAINER_TAGS:
        if key in c:
            name += f"-{tag}{c[key]:g}"
    return name


def short_label(c) -> str:
    """Compact name for tables: size/stages/width, latent, batch, then whatever differs from the defaults."""
    text = f"{c['size']}/{c['stages']}/{c['width']} z{c['latent']} b{c['batch']}"
    if c["lr"] != 0.001:
        text += f" lr{c['lr']:g}"
    if c["eikonal"]:
        text += f" eik{c['eikonal']:g}"
    for key, tag in TRAINER_TAGS:
        if key in c:
            text += f" {tag}{c[key]:g}"
    return text


def default_queue(seeds=(1, 2, 3), epochs=300):
    queue = []
    for seed in seeds:
        for size, stages, width, latent, batch, lr, eik in GRID:
            c = dict(size=size, stages=stages, width=width, latent=latent, batch=batch, lr=lr,
                     eikonal=eik, epochs=epochs, seed=seed)
            c["config"] = config_key(c)
            c["name"] = f"{c['config']}-seed{seed}"
            queue.append(c)
    return queue


def regularisation_queue(seeds=(1, 2, 3), epochs=300):
    """KL weight and augmentation strength at the 96 px reference architecture."""
    queue = []
    for seed in seeds:
        for kl in REGULARISATION["kl_weight"]:
            for rotation in REGULARISATION["aug_rotation"]:
                for scale in REGULARISATION["aug_scale"]:
                    c = dict(size=96, stages=3, width=1, latent=48, batch=16, lr=1e-3, eikonal=0.0, epochs=epochs, seed=seed)
                    for key, value in (("kl_weight", kl), ("aug_rotation", rotation), ("aug_scale", scale)):
                        if value != TRAINER_DEFAULTS[key]:
                            c[key] = value
                    c["config"] = config_key(c)
                    c["name"] = f"{c['config']}-seed{seed}"
                    queue.append(c)
    return queue


def estimated_vram_mib(c) -> float:
    """Peak allocation scaled from measured 192 px runs, plus the per-process CUDA context."""
    return 500 + 907 * (c["size"] / 192) ** 2 * (c["batch"] / 32) * (1 + 0.63 * (c["width"] - 1))


def resolve_root(value) -> Path:
    root = Path(value)
    return root if root.is_absolute() else REPO / root


def plan(args) -> int:
    root = resolve_root(args.root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "queue.json"
    if path.exists() and not args.force:
        print(f"{path} exists ({len(read_json(path, []))} runs); pass --force to rebuild")
        return 1
    queue = default_queue(tuple(args.seeds), args.epochs)
    path.write_text(json.dumps(queue, indent=1), encoding="utf-8")
    control = root / "control.json"
    if not control.exists():
        control.write_text(json.dumps(dict(max_workers=4, min_workers=1, target_util=92, vram_limit_mib=16000,
                                           max_big=2, paused=False), indent=1), encoding="utf-8")
    print(f"{len(queue)} runs queued in {path}")
    return 0


def extend(args) -> int:
    """Add the regularisation runs to an existing queue, alternating them with what is already there."""
    root = resolve_root(args.root)
    queue = read_json(root / "queue.json", []) or []
    known = {c["name"] for c in queue}
    seeds = sorted({c["seed"] for c in queue}) or [1, 2, 3]
    added = [c for c in regularisation_queue(tuple(seeds), args.epochs) if c["name"] not in known]
    merged = []
    for seed in seeds:
        new = [c for c in added if c["seed"] == seed]
        old = [c for c in queue if c["seed"] == seed]
        for i in range(max(len(new), len(old))):
            merged += new[i:i + 1] + old[i:i + 1]
    if not write_atomic(root / "queue.json", json.dumps(merged, indent=1).encode("utf-8")):
        print("queue.json is busy; nothing changed")
        return 1
    print(f"{len(added)} runs added, {len(merged)} queued in total")
    return 0


# ----------------------------------------------------------------------------- worker

def throttle_checkpoints(state_mod, seconds: float) -> None:
    """Write the resumable checkpoint at most every `seconds`, and always after the last epoch."""
    real = state_mod.save_atomic
    last = [float("-inf")]

    def save(payload, path):
        final = isinstance(payload, dict) and len(payload.get("history", ())) >= payload.get("target_epochs", 0)
        if final or time.monotonic() - last[0] >= seconds:
            real(payload, path)
            last[0] = time.monotonic()

    state_mod.save_atomic = save


class Recorder:
    """Stores per-epoch progress and reconstructions of fixed validation silhouettes."""

    def __init__(self, out: Path, cfg: dict):
        from heller_jigsaw.data.corpus import load_corpus_catalog, load_motif_asset
        from heller_jigsaw.models.recognizer import mask_to_model_input

        self.out, self.cfg = out, cfg
        self.dir = out / "previews"
        self.dir.mkdir(exist_ok=True)
        self.started = time.time()
        self.t0 = time.perf_counter()
        self.last_preview = self.last_progress = float("-inf")
        self.first_epoch = None
        self.tensors = None
        records, base = load_corpus_catalog(CATALOG)
        by_class = {}
        for r in sorted((r for r in records if r.get("split") == "val"), key=lambda r: r["asset_id"]):
            by_class.setdefault(r["motif_class"], []).append(r)
        picks = [r for name in sorted(by_class) for r in by_class[name][:2]]
        assets = [load_motif_asset(r, base) for r in picks]
        self.classes = [a.motif_class for a in assets]
        self.fields = np.stack([mask_to_model_input(a.mask, a.sdf, size=cfg["size"])[1:2] for a in assets]).astype(np.float32)
        buf = io.BytesIO()
        np.savez_compressed(buf, target=np.packbits(np.stack([a.mask > 0.5 for a in assets])),
                            shape=np.array([len(assets), *assets[0].mask.shape]),
                            classes=np.array(self.classes), assets=np.array([a.asset_id for a in assets]))
        write_atomic(self.dir / "targets.npz", buf.getvalue())

    def attach(self, state_mod) -> None:
        recorder = self
        original = state_mod.EpochCheckpoint.finish_epoch

        def finish_epoch(run, metrics, score):
            original(run, metrics, score)
            try:
                recorder.on_epoch(run)
            except Exception as exc:  # recording must never stop a training run
                print(f"preview skipped: {exc!r}", file=sys.stderr, flush=True)

        state_mod.EpochCheckpoint.finish_epoch = finish_epoch

    def on_epoch(self, run) -> None:
        epoch = len(run.history)
        if self.first_epoch is None:
            self.first_epoch = epoch - 1
        now = time.perf_counter()
        keep = epoch in SNAP_EPOCHS or epoch == run.epochs
        if keep or now - self.last_preview >= 4.0:
            self.snapshot(run, epoch, keep)
            self.last_preview = now
        if keep or now - self.last_progress >= 1.0:
            self.progress(run, "running")
            self.last_progress = now

    def snapshot(self, run, epoch: int, keep: bool) -> None:
        import torch
        from heller_jigsaw.models.shape_prior import usable_mask

        model = run.model
        if self.tensors is None:
            device = next(model.parameters()).device
            names = run.config["classes"]
            z = np.random.default_rng(PREVIEW_SEED).standard_normal((len(self.classes), model.latent_dim))
            self.tensors = (torch.from_numpy(self.fields).to(device),
                            torch.tensor([names.index(c) for c in self.classes], device=device),
                            torch.from_numpy(z.astype(np.float32)).to(device))
        x, y, z = self.tensors
        training = model.training
        model.eval()
        with torch.inference_mode():
            mu, _ = model.encode(x, y)
            pred = model.decode(mu, y)[:, 0] < 0
            sample = model.decode(z, y)[:, 0].float().cpu().numpy()
            actual = x[:, 0] < 0
            iou = (pred & actual).flatten(1).sum(1) / (pred | actual).flatten(1).sum(1).clamp_min(1)
            pred, iou = pred.cpu().numpy(), iou.cpu().numpy()
        model.train(training)
        buf = io.BytesIO()
        np.savez_compressed(buf, epoch=epoch, size=pred.shape[-1], recon=np.packbits(pred),
                            sample=np.packbits(sample < 0), iou=iou.astype(np.float32),
                            valid=np.array([usable_mask(f) is not None for f in sample]))
        data = buf.getvalue()
        write_atomic(self.dir / "latest.npz", data)
        if keep:
            write_atomic(self.dir / f"epoch-{epoch:04d}.npz", data)

    def progress(self, run, status: str, extra=None) -> None:
        import torch

        history = run.history if run is not None else []
        data = dict(
            name=self.cfg["name"], config=self.cfg, status=status, epoch=len(history),
            epochs=run.epochs if run is not None else self.cfg["epochs"],
            val=[round(float(r["val_reconstruction_iou"]), 4) for r in history],
            loss=[round(float(r["loss"]), 4) for r in history],
            best=float(run.best_score) if run is not None and np.isfinite(run.best_score) else None,
            best_epoch=run.best_epoch if run is not None else None,
            started=self.started, updated=time.time(), session_first_epoch=self.first_epoch,
            session_seconds=round(time.perf_counter() - self.t0, 1), pid=os.getpid(),
            peak_vram_mib=round(torch.cuda.max_memory_allocated() / 2**20) if torch.cuda.is_available() else None,
        )
        data.update(extra or {})
        write_atomic(self.out / "progress.json", json.dumps(data).encode("utf-8"))


def evaluation_assets():
    from heller_jigsaw.data.corpus import load_corpus_catalog, load_motif_asset

    rows, base = load_corpus_catalog(CATALOG)
    return [(r, load_motif_asset(r, base)) for r in rows if r.get("split") in ("val", "test")]


def common_benchmark(run_dir: Path, assets=None):
    """Score a finished run on the 256 px masks every model is compared on (benchmark_shapes)."""
    import torch

    bench = importlib.import_module("heller_jigsaw.models.benchmark_shapes")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    ckpt, model = bench.load_model(run_dir, CATALOG, device)
    result = bench.evaluate_model(ckpt, model, assets if assets is not None else evaluation_assets(), device)
    summary = {split: dict(macro_iou=result[split]["macro_iou"], mean_iou=result[split]["mean_iou"],
                           boundary_px=result[split]["mean_boundary_error_px"], thin_recall=result[split]["thin_recall"],
                           valid_rate=result[split]["valid_rate"]) for split in ("val", "test")}
    write_atomic(run_dir / "benchmark256.json", json.dumps(summary, indent=1).encode("utf-8"))
    return summary


def worker(args) -> int:
    root = resolve_root(args.root)
    cfg = next(c for c in read_json(root / "queue.json", []) if c["name"] == args.name)
    out = root / "runs" / cfg["name"]
    out.mkdir(parents=True, exist_ok=True)
    lock = None
    for _ in range(5):
        lock = hold_lock(out / "worker.lock")
        if lock is not None:
            break
        time.sleep(0.3)
    if lock is None:
        print("another process is training this run", flush=True)
        return 3
    os.chdir(REPO)
    import torch

    torch.set_num_threads(2)
    state_mod = importlib.import_module("heller_jigsaw.models.training_state")
    trainer = importlib.import_module("heller_jigsaw.models.train_shapes")
    throttle_checkpoints(state_mod, args.checkpoint_seconds)
    recorder = Recorder(out, cfg)
    recorder.attach(state_mod)
    recorder.progress(None, "starting")
    try:
        report = trainer.train_shapes(
            CATALOG, out, epochs=cfg["epochs"], batch_size=cfg["batch"], lr=cfg["lr"], size=cfg["size"],
            latent_dim=cfg["latent"], seed=cfg["seed"], resume=(out / "training-last.pt").exists(),
            balanced=True, eikonal_weight=cfg["eikonal"], width=cfg["width"], stages=cfg["stages"],
            **{key: cfg[key] for key, _ in TRAINER_TAGS if key in cfg})
    except Exception as exc:
        previous = read_json(out / "progress.json", {}) or {}
        previous.update(status="failed", error=repr(exc)[:400], updated=time.time())
        write_atomic(out / "progress.json", json.dumps(previous).encode("utf-8"))
        raise
    history = report["history"]
    final = dict(
        name=cfg["name"], config=cfg, status="finished", epoch=len(history), epochs=cfg["epochs"],
        val=[round(float(r["val_reconstruction_iou"]), 4) for r in history],
        loss=[round(float(r["loss"]), 4) for r in history],
        best=max(float(r["val_reconstruction_iou"]) for r in history), best_epoch=report["best_epoch"],
        started=recorder.started, updated=time.time(), session_first_epoch=recorder.first_epoch,
        session_seconds=round(time.perf_counter() - recorder.t0, 1), pid=os.getpid(),
        peak_vram_mib=(report.get("session") or {}).get("peak_cuda_memory_mib"), parameters=report.get("parameters"),
        val_mean_iou=report["reconstruction"]["val"]["mean_iou"], test_mean_iou=report["reconstruction"]["test"]["mean_iou"],
        valid_raw_shapes=report.get("valid_raw_shapes_per_class"),
    )
    write_atomic(out / "progress.json", json.dumps(final).encode("utf-8"))
    print(json.dumps({k: final[k] for k in ("name", "best", "best_epoch", "val_mean_iou", "test_mean_iou", "session_seconds")}), flush=True)
    try:
        print(json.dumps(dict(benchmark256=common_benchmark(out)["val"])), flush=True)
    except Exception as exc:  # the report computes it later if this fails
        print(f"benchmark skipped: {exc!r}", flush=True)
    return 0


# -------------------------------------------------------------------------- scheduler

def gpu_sample():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,power.draw,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15).stdout
        return tuple(float(v) for v in out.strip().splitlines()[0].split(","))
    except Exception:
        return None


def run(args) -> int:
    root = resolve_root(args.root)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    owner = hold_lock(root / "scheduler.lock")
    if owner is None:
        print("a scheduler already owns this root")
        return 1
    active, attempts, window, last_launch = {}, {}, [], 0.0
    env = dict(os.environ, PYTHONUNBUFFERED="1", OMP_NUM_THREADS="2")
    while True:
        control = dict(max_workers=4, min_workers=1, target_util=92, vram_limit_mib=16000, max_big=2, paused=False)
        control.update(read_json(root / "control.json", {}) or {})
        queue = read_json(root / "queue.json", []) or []
        for name, (proc, handle) in list(active.items()):
            code = proc.poll()
            if code is not None:
                handle.close()
                del active[name]
                print(time.strftime("%H:%M:%S"), "END", name, "exit", code, flush=True)
        pending = [c for c in queue if c["name"] not in active and attempts.get(c["name"], 0) < 2
                   and not (root / "runs" / c["name"] / "metrics.json").exists()]
        if not pending and not active:
            break
        sample = gpu_sample()
        if sample:
            window = (window + [sample[0]])[-8:]
            with open(root / "gpu_log.csv", "a") as log:
                log.write(f"{time.time():.0f},{sample[0]:.0f},{sample[1]:.0f},{sample[2]:.0f},{sample[3]:.0f},{len(active)}\n")
        settled = time.time() - last_launch > args.settle
        if pending and settled and not control["paused"] and len(active) < control["max_workers"]:
            big = sum(1 for n in active if next(c for c in queue if c["name"] == n)["size"] >= 192)
            order = [c for c in pending if c["size"] < 192] if big >= control["max_big"] else pending
            choice = next((c for c in order if not is_locked(root / "runs" / c["name"] / "worker.lock")), None)
            spare = (sample is not None and len(window) >= 4 and float(np.mean(window)) < control["target_util"]
                     and choice is not None and sample[1] + estimated_vram_mib(choice) < control["vram_limit_mib"])
            if choice is not None and (len(active) < control["min_workers"] or spare):
                out = root / "runs" / choice["name"]
                out.mkdir(parents=True, exist_ok=True)
                handle = open(out / "worker.log", "ab")
                proc = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "worker", "--root", str(root), "--name", choice["name"],
                     "--checkpoint-seconds", str(args.checkpoint_seconds)],
                    cwd=REPO, env=env, stdout=handle, stderr=subprocess.STDOUT)
                active[choice["name"]] = (proc, handle)
                attempts[choice["name"]] = attempts.get(choice["name"], 0) + 1
                last_launch, window = time.time(), []
                print(time.strftime("%H:%M:%S"), "START", choice["name"], f"({len(active)} active)", flush=True)
        time.sleep(args.poll)
    print(time.strftime("%H:%M:%S"), "QUEUE_DONE", flush=True)
    return 0


# ----------------------------------------------------------------------------- report

SIZE_COLOURS = {96: (38, 110, 190), 128: (30, 150, 130), 192: (225, 130, 20), 256: (205, 45, 45)}


def _font(size: int, bold=False, mono=False):
    from PIL import ImageFont

    names = (("consolab.ttf", "consola.ttf") if mono else ("segoeuib.ttf", "arialbd.ttf")) if bold or mono else ("segoeui.ttf", "arial.ttf")
    if mono and not bold:
        names = ("consola.ttf",)
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _unpack(bits, count: int, size: int):
    return np.unpackbits(bits)[: count * size * size].reshape(count, size, size).astype(bool)


def _load_npz(path: Path):
    data = read_bytes(path)
    if data is None:
        return None
    try:
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            return {k: z[k] for k in z.files}
    except Exception:
        return None


def _tile(mask, px: int, outline=None, ink=(40, 40, 46)):
    from PIL import Image
    from scipy import ndimage

    fill = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize((px, px), Image.LANCZOS), dtype=np.float32)[..., None] / 255
    img = 255 - fill * (255 - np.array(ink, dtype=np.float32))
    if outline is not None:
        t = np.asarray(Image.fromarray(outline.astype(np.uint8) * 255).resize((px, px), Image.LANCZOS)) > 127
        img[t & ~ndimage.binary_erosion(t, iterations=max(1, px // 70))] = (225, 30, 30)
    return Image.fromarray(img.astype(np.uint8))


def load_runs(root: Path):
    runs = []
    for path in sorted((root / "runs").glob("*/progress.json")):
        data = read_json(path)
        if data and "val" in data:
            data["dir"] = path.parent
            runs.append(data)
    return runs


def reference_runs():
    """Finished single runs trained outside the sweep (read from their final metrics.json only)."""
    rows = []
    for path in sorted((REPO / "runs/v12/training").glob("*/metrics.json")) + [REPO / "runs/v10-campaign/cc0-96-seed101/metrics.json"]:
        data = read_json(path)
        if not data or "history" not in data:
            continue
        val = [float(r["val_reconstruction_iou"]) for r in data["history"]]
        rows.append(dict(name=("v10 " if "v10" in str(path) else "v12 ") + path.parent.name, val=val, best=max(val),
                         size=data["config"]["size"]))
    return rows


def attach_benchmarks(runs, limit=8) -> None:
    """Read each finished run's 256 px benchmark, computing the few that are missing."""
    missing = [r for r in runs if r["status"] == "finished" and not (r["dir"] / "benchmark256.json").exists()
               and (r["dir"] / "animal-shape-vae.pt").exists()]
    if missing:
        assets = evaluation_assets()
        for r in missing[:limit]:
            try:
                common_benchmark(r["dir"], assets)
            except Exception as exc:
                print(f"benchmark failed for {r['name']}: {exc!r}", file=sys.stderr, flush=True)
    for r in runs:
        r["bench"] = read_json(r["dir"] / "benchmark256.json") if r["status"] == "finished" else None


def _mean(values):
    values = [v for v in values if v is not None]
    return float(np.mean(values)) if values else None


def leaderboard(runs):
    """One row per configuration, ranked like benchmark-shapes: 256 px validation macro IoU + 0.05 x valid rate."""
    groups = {}
    for r in runs:
        if r["status"] == "finished":
            groups.setdefault(r["config"]["config"], []).append(r)
    rows = []
    for key, items in groups.items():
        best = [float(r["best"]) for r in items]
        bench = [r["bench"]["val"] for r in items if r.get("bench")]
        iou256 = [b["macro_iou"] for b in bench]
        row = dict(config=key, label=short_label(items[0]["config"]), size=items[0]["config"]["size"], n=len(items),
                   mean=float(np.mean(best)), sd=float(np.std(best, ddof=1)) if len(best) > 1 else None,
                   iou256=_mean(iou256), iou256_sd=float(np.std(iou256, ddof=1)) if len(iou256) > 1 else None,
                   thin=_mean([b["thin_recall"] for b in bench]), valid=_mean([b["valid_rate"] for b in bench]),
                   boundary_px=_mean([b["boundary_px"] for b in bench]),
                   test=_mean([r.get("test_mean_iou") for r in items]),
                   minutes=float(np.mean([r["session_seconds"] for r in items]) / 60),
                   parameters=items[0].get("parameters"))
        row["score"] = None if row["iou256"] is None else row["iou256"] + 0.05 * (row["valid"] or 0.0)
        rows.append(row)
    return sorted(rows, key=lambda r: -(r["score"] if r["score"] is not None else r["mean"] - 10))


def report(args) -> int:
    from PIL import Image, ImageDraw

    root = resolve_root(args.root)
    os.chdir(REPO)
    queue = read_json(root / "queue.json", []) or []
    runs = load_runs(root)
    attach_benchmarks(runs)
    now = time.time()
    active = [r for r in runs if r["status"] == "running" and now - r["updated"] < 180 and r["epoch"] > 0]
    finished = [r for r in runs if r["status"] == "finished"]
    board = leaderboard(runs)
    refs = reference_runs()
    write_atomic(root / "leaderboard.json", json.dumps(dict(updated=now, configs=board, references=[
        dict(name=r["name"], best=r["best"]) for r in refs]), indent=1).encode("utf-8"))

    gpu_rows = []
    data = read_bytes(root / "gpu_log.csv")
    if data:
        for line in data.decode("ascii", "ignore").splitlines()[-1200:]:
            parts = line.split(",")
            if len(parts) >= 6:
                try:
                    gpu_rows.append([float(v) for v in parts[:6]])
                except ValueError:
                    pass
    live = gpu_sample()
    recent = [r for r in gpu_rows if r[0] > now - 600]
    avg10 = float(np.mean([r[1] for r in recent])) if recent else None

    featured = max(active, key=lambda r: r["epoch"] / r["epochs"]) if active else (
        max(finished, key=lambda r: r["updated"]) if finished else None)

    S = 2
    W, H = 1600, 1230
    T = lambda v: int(round(v * S))
    img = Image.new("RGB", (T(W), T(H)), (255, 255, 255))
    d = ImageDraw.Draw(img)
    f_title, f_head, f_text, f_small, f_mono = _font(T(27), True), _font(T(16), True), _font(T(15)), _font(T(12)), _font(T(13), mono=True)
    ink, grey = (25, 25, 30), (110, 110, 118)

    # header
    d.rectangle([0, 0, T(W), T(92)], fill=(244, 245, 248))
    d.text((T(24), T(10)), "Shape-model training sweep", font=f_title, fill=ink)
    d.text((T(410), T(20)), time.strftime("%d %b %Y  %H:%M"), font=f_text, fill=grey)
    if live:
        line = f"GPU now {live[0]:.0f}% busy · {live[1] / 1024:.1f} of 20.0 GB VRAM · {live[2]:.0f} W · {live[3]:.0f} °C"
        if avg10 is not None:
            line += f"   ·   last 10 min average {avg10:.0f}% busy"
        d.text((T(24), T(48)), line, font=f_text, fill=ink)
    best_run = max(finished, key=lambda r: r["best"]) if finished else None
    line = f"{len(finished)} of {len(queue)} runs finished · {len(active)} training now"
    if best_run:
        line += f" · best validation IoU so far {best_run['best']:.3f} ({best_run['name']})"
    d.text((T(24), T(69)), line, font=f_text, fill=ink)
    if len(gpu_rows) > 3:  # utilisation sparkline, last 30 min
        x0, y0, w, h = 1300, 14, 276, 44
        pts = [r for r in gpu_rows if r[0] > now - 1800]
        d.rectangle([T(x0), T(y0), T(x0 + w), T(y0 + h)], outline=(205, 207, 214), fill=(255, 255, 255))
        if len(pts) > 1:
            d.line([(T(x0 + w * (r[0] - (now - 1800)) / 1800), T(y0 + h - h * min(100, r[1]) / 100)) for r in pts],
                   fill=(60, 150, 70), width=T(1.3))
        d.text((T(x0), T(y0 + h + 3)), "GPU busy %, last 30 min (top = 100%)", font=f_small, fill=grey)

    # left: what the featured model can draw
    x_lab, x_tiles, px, pitch = 24, 126, 84, 88
    y = 104
    bottom_left = y
    if featured is not None:
        fdir = featured["dir"] / "previews"
        targets = _load_npz(fdir / "targets.npz")
        latest = _load_npz(fdir / "latest.npz")
        if targets is not None and latest is not None:
            count = int(targets["shape"][0])
            tmask = _unpack(targets["target"], count, int(targets["shape"][1]))
            classes = [str(c) for c in targets["classes"]]
            cfg = featured["config"]
            title = (f"Model {featured['name']}: {cfg['size']} px, epoch {int(latest['epoch'])} of {featured['epochs']}"
                     f" · validation IoU {featured['val'][-1]:.3f}")
            d.text((T(x_lab), T(y)), title, font=f_head, fill=ink)
            y += 28
            for i, name in enumerate(classes):
                label = name.replace("_cetacean", "").replace("quadruped_other", "quadruped")
                d.text((T(x_tiles + i * pitch + 2), T(y)), label, font=f_small, fill=grey)
            y += 18

            def row(label, masks, outlines, note="", highlight=False, flags=None, ink_colour=(40, 40, 46)):
                nonlocal y
                if highlight:
                    d.rectangle([T(x_lab - 6), T(y - 2), T(x_tiles + count * pitch + 78), T(y + px + 2)], fill=(255, 246, 204))
                d.text((T(x_lab), T(y + 30)), label, font=f_text, fill=ink)
                for i in range(len(masks)):
                    img.paste(_tile(masks[i], T(px), None if outlines is None else outlines[i], ink_colour), (T(x_tiles + i * pitch), T(y)))
                    if flags is not None:
                        colour = (40, 160, 70) if flags[i] else (215, 50, 50)
                        d.ellipse([T(x_tiles + i * pitch + 4), T(y + 4), T(x_tiles + i * pitch + 14), T(y + 14)], fill=colour)
                if note:
                    d.text((T(x_tiles + count * pitch + 6), T(y + 30)), note, font=f_text, fill=ink)
                y += pitch

            row("target", tmask, None, ink_colour=(30, 60, 110))
            snaps = sorted(int(p.stem.split("-")[1]) for p in fdir.glob("epoch-*.npz"))
            snaps = [e for e in snaps if e < int(latest["epoch"])]
            if len(snaps) > 5:
                snaps = [snaps[int(round(i))] for i in np.linspace(0, len(snaps) - 1, 5)]
            for e in snaps:
                snap = _load_npz(fdir / f"epoch-{e:04d}.npz")
                if snap is not None:
                    row(f"epoch {e}", _unpack(snap["recon"], count, int(snap["size"])), tmask, f"IoU {float(snap['iou'].mean()):.2f}")
            row(f"epoch {int(latest['epoch'])}", _unpack(latest["recon"], count, int(latest["size"])), tmask,
                f"IoU {float(latest['iou'].mean()):.2f}", highlight=True)
            d.text((T(x_lab), T(y + 2)), "Black = what the model draws from memory of the shape, red line = the true outline (held-out silhouettes it never trained on).",
                   font=f_small, fill=grey)
            y += 24
            d.text((T(x_lab), T(y)), "New shapes invented from random numbers (green dot = one clean piece without holes, usable for cutting)", font=f_head, fill=ink)
            y += 26
            row("invented", _unpack(latest["sample"], count, int(latest["size"])), None, flags=[bool(v) for v in latest["valid"]])
            others = [r for r in active if r is not featured][:3]
            if others:
                d.text((T(x_lab), T(y + 2)), "Other models training right now (latest epoch)", font=f_head, fill=ink)
                y += 26
                for r in others:
                    snap = _load_npz(r["dir"] / "previews" / "latest.npz")
                    if snap is None:
                        continue
                    d.text((T(x_lab), T(y + 8)), f"{r['config']['size']} px", font=f_text, fill=ink)
                    d.text((T(x_lab), T(y + 30)), f"ep {int(snap['epoch'])}", font=f_text, fill=ink)
                    d.text((T(x_lab), T(y + 52)), r["config"]["config"].split("-", 1)[1][:13], font=f_small, fill=grey)
                    recon = _unpack(snap["recon"], count, int(snap["size"]))
                    for i in range(count):
                        img.paste(_tile(recon[i], T(px), tmask[i]), (T(x_tiles + i * pitch), T(y)))
                    d.text((T(x_tiles + count * pitch + 6), T(y + 30)), f"IoU {float(snap['iou'].mean()):.2f}", font=f_text, fill=ink)
                    y += pitch
            bottom_left = y
    else:
        d.text((T(x_lab), T(y)), "Waiting for the first epoch of the first run.", font=f_head, fill=ink)
        bottom_left = y + 40

    # right: curves
    x0, y0, w, h = 1128, 132, 448, 360
    d.text((T(1090), T(104)), "Validation IoU by epoch (higher = closer to the true shape)", font=f_head, fill=ink)
    max_epoch = max([300] + [len(r["val"]) for r in runs] + [len(r["val"]) for r in refs])
    lo, hi = 0.60, 0.86
    fx = lambda e: T(x0 + w * e / max_epoch)
    fy = lambda v: T(y0 + h - h * (min(hi, max(lo, v)) - lo) / (hi - lo))
    d.rectangle([T(x0), T(y0), T(x0 + w), T(y0 + h)], outline=(190, 192, 200))
    for v in np.arange(lo, hi + 1e-9, 0.05):
        d.line([(T(x0), fy(v)), (T(x0 + w), fy(v))], fill=(232, 233, 238), width=T(1))
        d.text((T(x0 - 34), fy(v) - T(8)), f"{v:.2f}", font=f_small, fill=grey)
    for e in range(0, max_epoch + 1, 100 if max_epoch <= 600 else 200):
        d.text((fx(e) - T(8), T(y0 + h + 4)), str(e), font=f_small, fill=grey)
    for r in refs:
        if len(r["val"]) > 1:
            d.line([(fx(i + 1), fy(v)) for i, v in enumerate(r["val"])], fill=(185, 185, 190), width=T(1))
    for r in sorted(runs, key=lambda r: r is featured or r in active):
        if len(r["val"]) < 2:
            continue
        colour = SIZE_COLOURS.get(r["config"]["size"], (90, 90, 90))
        live_run = r in active
        if not live_run:
            colour = tuple(int(c + (255 - c) * 0.45) for c in colour)
        d.line([(fx(i + 1), fy(v)) for i, v in enumerate(r["val"])], fill=colour, width=T(2.4 if r is featured else 1.6 if live_run else 1), joint="curve")
    lx = x0 + 8
    for size, colour in SIZE_COLOURS.items():
        d.rectangle([T(lx), T(y0 + h - 18), T(lx + 14), T(y0 + h - 8)], fill=colour)
        d.text((T(lx + 18), T(y0 + h - 22)), f"{size} px", font=f_small, fill=ink)
        lx += 74
    d.rectangle([T(lx), T(y0 + h - 14), T(lx + 14), T(y0 + h - 12)], fill=(170, 170, 176))
    d.text((T(lx + 18), T(y0 + h - 22)), "earlier runs", font=f_small, fill=ink)

    # right: table
    ty = y0 + h + 34
    d.text((T(1090), T(ty)), "Leaderboard: finished configurations, averaged over seeds", font=f_head, fill=ink)
    ty += 26
    d.text((T(1090), T(ty)), "size/stages/width, z = latent size, b = batch. IoU256 = overlap with the true shape at 256 px,", font=f_small, fill=grey)
    ty += 16
    d.text((T(1090), T(ty)), "thin = share of thin parts (legs, tails, beaks) kept, valid = share that come out as one piece.", font=f_small, fill=grey)
    ty += 16
    d.text((T(1090), T(ty)), "Ranked by IoU256 + 0.05 x valid (the benchmark-shapes selection rule); test scores are never used to rank.", font=f_small, fill=grey)
    ty += 22
    d.text((T(1090), T(ty)), f"{'configuration':<37}{'IoU256':>7}{'± sd':>7}{'thin':>6}{'valid':>6}{'n':>3}", font=f_mono, fill=grey)
    ty += 19
    fmt = lambda v, spec: format(v, spec) if v is not None else "-"
    for r in board[:16]:
        text = (f"{r['label'][:36]:<37}{fmt(r['iou256'], '.3f'):>7}{fmt(r['iou256_sd'], '.3f'):>7}"
                f"{fmt(r['thin'], '.2f'):>6}{fmt(r['valid'], '.2f'):>6}{r['n']:>3}")
        d.text((T(1090), T(ty)), text, font=f_mono, fill=SIZE_COLOURS.get(r["size"], ink))
        ty += 18
    if not board:
        d.text((T(1090), T(ty)), "no configuration has finished yet", font=f_mono, fill=grey)
        ty += 18
    ty += 10
    d.text((T(1090), T(ty)), "Earlier single runs (best validation IoU at their own size)", font=f_head, fill=ink)
    ty += 26
    for r in sorted(refs, key=lambda r: -r["best"])[:5]:
        d.text((T(1090), T(ty)), f"{r['name']:<35}{r['best']:>7.3f}", font=f_mono, fill=grey)
        ty += 18

    used = int(min(H, max(bottom_left, ty) + 24))
    img = img.crop((0, 0, T(W), T(used))).resize((W, used), Image.LANCZOS)
    out = Path(args.out) if args.out else root / "dashboard.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    stamped = None
    if args.keep:
        stamped = root / "dashboards" / f"dashboard-{time.strftime('%Y%m%d-%H%M%S')}.png"
        stamped.parent.mkdir(exist_ok=True)
        img.save(stamped)
    print(json.dumps(dict(
        image=str(out), kept=str(stamped) if stamped else None,
        gpu_now=dict(util=live[0], vram_mib=live[1], watts=live[2], temp=live[3]) if live else None,
        gpu_avg_10min=round(avg10, 1) if avg10 is not None else None,
        finished=len(finished), total=len(queue),
        failed=[r["name"] for r in runs if r["status"] == "failed"],
        training=[dict(name=r["name"], epoch=r["epoch"], epochs=r["epochs"], val=r["val"][-1], best=r["best"]) for r in active],
        featured=featured["name"] if featured else None,
        top=[{k: (round(r[k], 4) if isinstance(r[k], float) else r[k])
              for k in ("config", "n", "iou256", "iou256_sd", "thin", "valid", "mean", "sd", "test", "minutes")} for r in board[:8]],
        references=[dict(name=r["name"], best=round(r["best"], 4)) for r in refs],
    ), indent=1))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("plan", help="Write queue.json and control.json")
    s.add_argument("--root", default="runs/shape-sweep")
    s.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    s.add_argument("--epochs", type=int, default=300)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=plan)
    s = sub.add_parser("extend", help="Add KL-weight and augmentation runs to an existing queue")
    s.add_argument("--root", default="runs/shape-sweep")
    s.add_argument("--epochs", type=int, default=300)
    s.set_defaults(func=extend)
    s = sub.add_parser("run", help="Train the queue, adding runs while the GPU has spare capacity")
    s.add_argument("--root", default="runs/shape-sweep")
    s.add_argument("--poll", type=float, default=3.0)
    s.add_argument("--settle", type=float, default=40.0, help="Seconds to wait after a launch before judging utilisation")
    s.add_argument("--checkpoint-seconds", type=float, default=60.0)
    s.set_defaults(func=run)
    s = sub.add_parser("worker", help="Train one queued run (started by `run`)")
    s.add_argument("--root", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--checkpoint-seconds", type=float, default=60.0)
    s.set_defaults(func=worker)
    s = sub.add_parser("report", help="Draw the dashboard and print a JSON summary")
    s.add_argument("--root", default="runs/shape-sweep")
    s.add_argument("--out")
    s.add_argument("--keep", action="store_true", help="Also keep a timestamped copy")
    s.set_defaults(func=report)
    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
