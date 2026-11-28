"""Zero-dependency stdlib HTTP JSON/SVG backend for Formverk Studio.

Serves:
- Static frontend from `web/formverk/`
- Generated runs safely from `runs/dendrite-engine/` (strict path-traversal guard)
- `GET  /api/health`   -> GPU/CUDA info, neural guide status, available styles
- `GET  /api/whimsies` -> built-in whimsy catalog + normalized preview paths + VAE/PathNet status
- `GET  /api/presets`  -> showcase runs in `runs/dendrite-engine/`
- `POST /api/autotune` -> neural surrogate parameter optimization
- `POST /api/generate` -> live GPU puzzle generation + 1:1 mm SVG + fabrication audit
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import tempfile
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import numpy as np
import torch

from .artwork import read_artwork
from .design_art import ART_PATTERNS, suggest_outline
from .styles import CUT_FAMILIES
from .cli import generate_puzzle_run
from .neural_guide import DEFAULT_CHECKPOINT, auto_tune_parameters
from .phase_field import build_outline_polygon
from .whimsies import (
    WhimsyPlacement,
    build_whimsy_geometry,
    get_builtin_whimsy,
    list_builtin_whimsies,
    load_pathnet_whimsies,
    load_vae_whimsies,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web" / "formverk"
RUNS_DIR = REPO_ROOT / "runs" / "dendrite-engine"
EDITION_DIR = REPO_ROOT / "runs" / "formverk-catalog"


def _decode_data_url_image(data_url: str) -> Optional[np.ndarray]:
    if not data_url:
        return None
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]
    return read_artwork(io.BytesIO(base64.b64decode(data_url, validate=True)))


def _whimsy_catalog_payload() -> Dict[str, Any]:
    items = []
    for name in list_builtin_whimsies():
        spec = get_builtin_whimsy(name)
        coords = [[round(float(x), 4), round(float(-y), 4)] for x, y in list(spec.polygon.exterior.coords)[:-1]]
        engrave = [
            [[round(float(x), 4), round(float(-y), 4)] for x, y in path]
            for path in spec.engrave_paths
        ]
        items.append({"name": spec.name, "category": spec.category, "outline": coords, "engrave": engrave})
    vae_ckpts = sorted(str(p.relative_to(REPO_ROOT).as_posix()) for p in (REPO_ROOT / "runs").glob("*/animal-shape-vae.pt"))[:6]
    return {
        "builtin": items,
        "pathnet_available": (REPO_ROOT / "pathnet" / "pathnet.pt").is_file(),
        "vae_checkpoints": vae_ckpts,
    }


def _list_showcases() -> List[Dict[str, Any]]:
    out = []
    if not RUNS_DIR.is_dir():
        return out
    for sub in sorted(RUNS_DIR.iterdir()):
        pj = sub / "puzzle.json"
        aj = sub / "audit.json"
        if sub.is_dir() and pj.is_file() and aj.is_file():
            try:
                audit = json.loads(aj.read_text(encoding="utf-8"))
                out.append(
                    {
                        "id": sub.name,
                        "pieces": audit.get("pieces"),
                        "lock_margin_mm": audit.get("interlock", {}).get("assembly_lock_margin_mm"),
                        "all_hard_gates_passed": audit.get("all_hard_gates_passed"),
                        "style": audit.get("metadata", {}).get("style"),
                        "puzzle_url": f"/runs/{sub.name}/puzzle.json",
                        "cut_svg_url": f"/runs/{sub.name}/cut.svg",
                        "artwork_svg_url": f"/runs/{sub.name}/artwork.svg",
                        "preview_png_url": f"/runs/{sub.name}/preview.png",
                        "audit_url": f"/runs/{sub.name}/audit.json",
                        **_optional_urls(sub),
                    }
                )
            except Exception:
                continue
    return out


def _optional_urls(directory):
    return {key: f"/runs/{directory.name}/{filename}"
            for key, filename in [("back_svg_url","back.svg"), ("photo_url","photo.png"),
                                  ("puzzle_url","puzzle.json"), ("sheet_url","pieces_sheet.png")]
            if (directory / filename).is_file()}


class FormverkRequestHandler(BaseHTTPRequestHandler):
    server_version = "FormverkStudio/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return  # Keep server output clean

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, base_dir: Path, rel_path: str) -> None:
        clean = unquote(rel_path).lstrip("/")
        target = (base_dir / clean).resolve()
        base_res = base_dir.resolve()
        if not (target == base_res or base_res in target.parents) or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")
            return
        mime, _ = mimetypes.guess_type(str(target))
        if target.suffix.lower() == ".svg":
            mime = "image/svg+xml"
        elif target.suffix.lower() == ".json":
            mime = "application/json"
        raw = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 100 * 1024 * 1024:
            raise ValueError("Missing body or request exceeds 100 MiB.")
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object.")
        return body

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            cuda_ok = torch.cuda.is_available()
            ckpt = REPO_ROOT / DEFAULT_CHECKPOINT
            self._send_json(
                {
                    "status": "ok",
                    "engine": "dendrite_engine",
                    "cuda_available": cuda_ok,
                    "gpu_name": torch.cuda.get_device_name(0) if cuda_ok else "cpu",
                    "neural_guide_ready": ckpt.is_file(),
                    "styles": sorted(CUT_FAMILIES), "cut_families": CUT_FAMILIES, "artwork_patterns": ART_PATTERNS,
                }
            )
            return

        if path == "/api/whimsies":
            self._send_json(_whimsy_catalog_payload())
            return

        if path == "/api/editions":
            from .editions import catalog
            self._send_json(catalog(EDITION_DIR))
            return
        if path.startswith("/edition-files/"):
            self._send_file(EDITION_DIR, path[len("/edition-files/"):])
            return

        if path == "/api/presets":
            self._send_json({"showcases": _list_showcases()})
            return

        if path.startswith("/runs/"):
            self._send_file(RUNS_DIR, path[len("/runs/") :])
            return

        rel = "index.html" if path in ("", "/") else path.lstrip("/")
        self._send_file(WEB_DIR, rel)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json_body()
        except Exception as exc:
            self._send_json({"error": f"Invalid JSON body: {exc}"}, status=HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/edition":
            from .editions import generate_edition
            try:
                self._send_json(generate_edition(EDITION_DIR, body.get("family"), body.get("seed",42)))
            except (ValueError, RuntimeError, OSError, TypeError) as exc:
                self._send_json({"error":str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        if path == "/api/outline":
            try:
                if body.get("outline") in ("rect", "circle", "organic", "spiral"):
                    poly = build_outline_polygon(body["outline"], float(body["width_mm"]), float(body["height_mm"]), int(body.get("seed",42)))
                    x0,y0,x1,y1 = poly.bounds
                    suggestion = {"mode":"outline", "points":[[(x-x0)/(x1-x0),(y-y0)/(y1-y0)] for x,y in list(poly.exterior.coords)[:-1]]}
                else:
                    image = _decode_data_url_image(body.get("artwork_data_url") or "")
                    if image is None:
                        raise ValueError("Choose a photo first.")
                    suggestion = suggest_outline(image)
                self._send_json(suggestion)
            except (ValueError, OSError, TypeError, KeyError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        if path == "/api/autotune":
            try:
                res = auto_tune_parameters(
                    width_mm=float(body.get("width_mm", 260.0)),
                    height_mm=float(body.get("height_mm", 190.0)),
                    num_pieces=int(body.get("pieces", 36)),
                    style=str(body.get("style", "dendrite")),
                    checkpoint=REPO_ROOT / DEFAULT_CHECKPOINT,
                    seed=int(body.get("seed", 42)),
                )
                self._send_json(res)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        if path == "/api/generate":
            staging = None
            try:
                style = str(body.get("style", "dendrite")).lower()
                outline_kind = str(body.get("outline", "rect")).lower()
                width_mm = float(np.clip(float(body.get("width_mm", 240.0)), 80.0, 650.0))
                height_mm = float(np.clip(float(body.get("height_mm", 180.0)), 80.0, 650.0))
                pieces = int(np.clip(int(body.get("pieces", 28)), 4, 240))
                seed = int(body.get("seed", 42))
                borderless = bool(body.get("borderless", False))
                auto_tune = bool(body.get("auto_tune", False))
                theme = body.get("theme")
                min_neck_mm = float(body.get("min_neck_mm", 3.0))
                kerf_mm = float(body.get("kerf_mm", 0.15))
                engrave_text = body.get("engrave_text") or None
                ep = body.get("engrave_pos", [0.5, 0.90])
                engrave_pos = (float(ep[0]), float(ep[1]))
                px_mm = float(body.get("px_mm", 0.55))

                # Handle optional custom outline SVG string
                outline_svg_path = None
                if style not in CUT_FAMILIES or outline_kind not in ("rect","rectangle","circle","organic","stone","spiral","svg"):
                    raise ValueError("Select a supported style and board outline.")
                run_id = f"web_{style}_{uuid.uuid4().hex}"
                staging = tempfile.TemporaryDirectory(prefix=".creating-", dir=RUNS_DIR)
                out_dir = Path(staging.name) / run_id
                out_dir.mkdir()
                if body.get("outline_svg"):
                    outline_svg_path = out_dir / "custom_outline.svg"
                    outline_svg_path.write_text(str(body["outline_svg"]), encoding="utf-8")
                    outline_kind = "svg"

                outline = build_outline_polygon(outline_kind, width_mm, height_mm, seed=seed, svg_path=outline_svg_path)
                x0, y0, x1, y1 = outline.bounds
                w_act, h_act = x1 - x0, y1 - y0

                # Build placed whimsies
                whimsies: List[WhimsyPlacement] = []
                for idx, item in enumerate(body.get("whimsies", [])):
                    wname = str(item.get("name", "trilobite"))
                    cx = x0 + float(item.get("x_frac", 0.5)) * w_act
                    cy = y0 + float(item.get("y_frac", 0.5)) * h_act
                    sz = float(item.get("size_mm", min(w_act, h_act) * 0.24))
                    ang = float(item.get("angle_deg", 0.0))
                    if wname == "pathnet":
                        spec = load_pathnet_whimsies(REPO_ROOT / "pathnet" / "pathnet.pt", count=1, seed=seed + idx)[0]
                    elif wname.startswith("vae@"):
                        spec = load_vae_whimsies(REPO_ROOT / wname[4:], count=1, seed=seed + idx)[0]
                    else:
                        spec = get_builtin_whimsy(wname)
                    whimsies.append(build_whimsy_geometry(spec, (cx, cy), sz, angle_deg=ang))

                try:
                    art_img = _decode_data_url_image(body.get("artwork_data_url") or "")
                except (ValueError, OSError, TypeError) as exc:
                    self._send_json({"error": f"Cannot read uploaded artwork: {exc}"}, status=HTTPStatus.BAD_REQUEST)
                    return
                overrides: Dict[str, Any] = {}
                if body.get("guide_curves") is not None:
                    overrides["guide_curves"] = body["guide_curves"]
                for key in ("gamma", "eps", "T0", "lambda1_mm", "lambda2_mm", "U1_mm", "U2_mm", "steps", "growth_steps", "vortex_strength"):
                    if body.get(key) is not None:
                        overrides[key] = float(body[key]) if key not in ("steps", "growth_steps") else int(body[key])

                res = generate_puzzle_run(
                    out_dir=out_dir,
                    style=style,
                    outline_kind=outline_kind,
                    width_mm=width_mm,
                    height_mm=height_mm,
                    num_pieces=pieces,
                    seed=seed,
                    borderless=borderless,
                    whimsies=whimsies,
                    engrave_text=engrave_text,
                    engrave_pos_frac=engrave_pos,
                    artwork_image=art_img,
                    auto_tune=auto_tune,
                    neural_checkpoint=REPO_ROOT / DEFAULT_CHECKPOINT,
                    theme=theme,
                    px_mm=px_mm,
                    min_neck_mm=min_neck_mm,
                    kerf_mm=kerf_mm,
                    outline_svg=outline_svg_path,
                    overrides=overrides,
                    title=str(body.get("title") or ""), reverse_text=body.get("reverse_text") or None,
                    artwork_pattern=body.get("artwork_pattern") or "wood",
                    auto_place_whimsies=bool(body.get("auto_place_whimsies", False)), board_outline=outline,
                    regions=body.get("regions"),
                    back_artwork=_decode_data_url_image(body.get("back_artwork_data_url") or ""),
                )
                puzzle_json = json.loads((out_dir / "puzzle.json").read_text(encoding="utf-8"))
                final_dir = RUNS_DIR / run_id
                res.audit["cut_svg"] = str(final_dir / "cut.svg")
                puzzle_json["audit_summary"]["cut_svg"] = res.audit["cut_svg"]
                (out_dir / "audit.json").write_text(json.dumps(res.audit,indent=2), encoding="utf-8")
                (out_dir / "puzzle.json").write_text(json.dumps(puzzle_json), encoding="utf-8")
                out_dir.rename(final_dir)
                self._send_json(
                    {
                        "run_id": run_id,
                        "puzzle": puzzle_json,
                        "audit": {k: v for k, v in res.audit.items() if k != "per_piece"},
                        "cut_svg_url": f"/runs/{run_id}/cut.svg",
                        "artwork_svg_url": f"/runs/{run_id}/artwork.svg",
                        "preview_png_url": f"/runs/{run_id}/preview.png",
                        "audit_url": f"/runs/{run_id}/audit.json",
                        **_optional_urls(final_dir),
                    }
                )
            except (ValueError, OSError, TypeError, IndexError) as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            finally:
                if staging is not None:
                    staging.cleanup()
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Unknown API endpoint")


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer((host, port), FormverkRequestHandler)


def serve_studio(host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = create_server(host=host, port=port)
    print(f"Formverk Studio listening on http://{host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
