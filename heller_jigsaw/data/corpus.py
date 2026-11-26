"""Load curated Nordic helleristninger silhouettes from `hellerisninger/processed/catalog.json`.

Also includes clean synthetic Nordic rock-art test silhouettes strictly for unit tests.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage


@dataclass
class MotifAsset:
    """Normalized rock-art silhouette ready for puzzle optimization or ML training."""

    asset_id: str
    family: str
    motif_class: str
    mask: np.ndarray            # (256, 256) float32 in {0.0, 1.0}
    sdf: np.ndarray             # (256, 256) float32, negative inside, positive outside (pixels)
    site: str = ""
    region: str = ""
    country: str = "NO"
    period: str = "unknown"
    jigsaw_tier: str = "A_ready"
    render_style: str = "solid_pecked"
    source_archive: str = ""
    source_group: str = ""
    split: str = "train"
    creator: str = ""
    license: str = ""
    license_url: str = ""
    commercial_ok: bool = True
    source_url: str = ""
    sha256: str = ""
    classification_confidence: float = 1.0
    metrics: Dict[str, Any] = field(default_factory=dict)

    @property
    def aspect_ratio(self) -> float:
        ys, xs = np.where(self.mask > 0.5)
        if len(xs) == 0:
            return 1.0
        w = max(1, int(xs.max() - xs.min() + 1))
        h = max(1, int(ys.max() - ys.min() + 1))
        return float(max(w, h)) / float(min(w, h))

    @property
    def fill_ratio(self) -> float:
        return float(np.mean(self.mask > 0.5))


def compute_sdf_from_mask(mask: np.ndarray) -> np.ndarray:
    """Compute signed distance field (negative inside foreground, positive outside) in pixels."""
    fg = mask > 0.5
    if not np.any(fg):
        return np.full(mask.shape, float(max(mask.shape)), dtype=np.float32)
    if np.all(fg):
        return np.full(mask.shape, -float(max(mask.shape)), dtype=np.float32)
    d_out = ndimage.distance_transform_edt(~fg)
    d_in = ndimage.distance_transform_edt(fg)
    return (d_out - d_in).astype(np.float32)


def resolve_recorded_path(value: str | Path) -> Path:
    path = Path(value)
    if path.exists():
        return path
    recorded = PureWindowsPath(str(value))
    if recorded.is_absolute():
        parts = recorded.parts
        for i, part in enumerate(parts):
            if part.casefold() == "jigsaw.iverfinne.no":
                suffix = parts[i + 1:]
                if ".." not in suffix:
                    candidate = Path(__file__).resolve().parents[2].joinpath(*suffix)
                    if candidate.exists():
                        return candidate
    return path


def _resolve_asset_path(base_dir: Path, rel_path: str) -> Path:
    p = resolve_recorded_path(rel_path)
    if p.is_absolute() and p.exists():
        return p
    for root in (base_dir, base_dir.parent, base_dir.parent.parent):
        cand = root / rel_path
        if cand.exists():
            return cand
    return base_dir / rel_path


def load_corpus_catalog(catalog_path: str | Path) -> Tuple[List[Dict[str, Any]], Path]:
    """Read `catalog.json` returning `(records, corpus_root_dir)`."""
    cpath = resolve_recorded_path(catalog_path)
    if cpath.is_dir():
        cpath = cpath / "catalog.json"
    if not cpath.exists():
        raise FileNotFoundError(f"Corpus catalog not found: {cpath}")
    with open(cpath, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        records = raw.get("items") or raw.get("records") or raw.get("catalog") or []
    else:
        raise ValueError(f"Unexpected catalog JSON structure in {cpath}")
    return records, cpath.parent


def load_motif_asset(rec: Dict[str, Any], corpus_dir: Path) -> MotifAsset:
    """Load binary mask and SDF arrays for one catalog record."""
    mask_path = _resolve_asset_path(corpus_dir, str(rec.get("mask_path", "")))
    if not mask_path.exists():
        raise FileNotFoundError(f"Mask file missing for {rec.get('asset_id')}: {mask_path}")
    img = Image.open(mask_path).convert("L")
    if img.size != (256, 256):
        img = img.resize((256, 256), Image.Resampling.NEAREST)
    mask = (np.asarray(img, dtype=np.float32) >= 127.5).astype(np.float32)

    sdf: Optional[np.ndarray] = None
    sdf_rel = rec.get("sdf_path")
    if sdf_rel:
        sdf_path = _resolve_asset_path(corpus_dir, str(sdf_rel))
        if sdf_path.exists():
            try:
                data = np.load(sdf_path)
                arr = data["sdf"] if "sdf" in data else data[data.files[0]]
                if arr.shape == (256, 256):
                    sdf = arr.astype(np.float32)
            except Exception:
                sdf = None
    if sdf is None:
        sdf = compute_sdf_from_mask(mask)

    # Metrics may be nested under "metrics" or stored flat on the record
    metrics = dict(rec.get("metrics") or {})
    for k in (
        "solidity",
        "compactness",
        "aspect_ratio",
        "area_ratio",
        "erosion_survival_ratio",
        "min_neck_norm",
        "vertex_count",
        "connected_components",
        "hole_count",
    ):
        if k in rec and k not in metrics:
            metrics[k] = rec[k]

    return MotifAsset(
        asset_id=str(rec.get("asset_id", "")),
        family=str(rec.get("family", "scene_or_unclassified")),
        motif_class=str(rec.get("motif_class", "unclassified_motif")),
        mask=mask,
        sdf=sdf,
        site=str(rec.get("site", "")),
        region=str(rec.get("region", "")),
        country=str(rec.get("country", "NORDIC")),
        period=str(rec.get("period", "unknown")),
        jigsaw_tier=str(rec.get("jigsaw_tier", "A_ready")),
        render_style=str(rec.get("render_style", "solid_pecked")),
        source_archive=str(rec.get("source_archive", "")),
        source_group=str(rec.get("source_group", "")),
        split=str(rec.get("split", "train")),
        creator=str(rec.get("creator", "")),
        license=str(rec.get("license", "")),
        license_url=str(rec.get("license_url", "")),
        commercial_ok=bool(rec.get("commercial_ok", False)),
        source_url=str(rec.get("source_url", "")),
        sha256=str(rec.get("sha256", "")),
        classification_confidence=float(rec.get("classification_confidence", 0.0)),
        metrics=metrics,
    )


# Priority order of iconic, visually unmistakable Nordic rock-art motif classes
ICONIC_CLASS_PRIORITY = [
    "reindeer",
    "elk_moose",
    "bronze_age_ship",
    "skier",
    "bear",
    "whale_cetacean",
    "stone_age_boat",
    "bird",
    "horse",
    "sun_wheel",
    "footprint",
    "hunter_warrior",
    "fish",
    "anthropomorph",
    "quadruped_other",
    "lur_blower",
    "acrobat_dancer",
    "rider",
    "tree",
    "axe_weapon",
    "chariot_plough",
    "dog_wolf",
    "handprint",
    "snake",
]


def _safe_float(val: Any, default: float) -> float:
    if val is None:
        return default
    try:
        v = float(val)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _effective_tier(rec: Dict[str, Any]) -> str:
    tier = rec.get("jigsaw_tier")
    if tier is None or str(tier).strip() == "":
        return "A_ready" if str(rec.get("source_archive", "")).lower() == "phylopic" else ""
    return str(tier)


def _score_candidate_for_jigsaw(rec: Dict[str, Any], prefer_norway: bool = True) -> float:
    """Rank clean, recognizable, fabrication-friendly rock-art motifs."""
    tier = _effective_tier(rec)
    tier_score = {"A_ready": 3.0, "B_thickened": 1.6}.get(tier, -5.0)
    mcls = str(rec.get("motif_class", "unclassified_motif"))
    if mcls in ("unclassified_motif", "multi_motif_panel", "geometric_spiral"):
        cls_score = -2.0
    else:
        cls_score = 2.0

    m = rec.get("metrics") if isinstance(rec.get("metrics"), dict) else rec
    solidity = _safe_float(m.get("solidity", rec.get("solidity")), 0.65)
    raw_aspect = _safe_float(m.get("aspect_ratio", rec.get("aspect_ratio")), 1.5)
    aspect = max(raw_aspect, 1.0 / max(raw_aspect, 1e-3)) if raw_aspect > 0 else 1.5
    erosion = _safe_float(m.get("erosion_survival_ratio", rec.get("erosion_survival_ratio")), 0.45)
    area_ratio = _safe_float(m.get("area_ratio", rec.get("area_ratio")), 0.25)
    conf = _safe_float(rec.get("classification_confidence"), 0.5)

    # Sweet spot for figurative jigsaw silhouettes:
    # - Solidity 0.36..0.82: distinctive figurative outline (not a plain circle/oval, not fragile lace)
    # - Symmetric aspect ratio 1.0..2.3: fits well into balanced puzzle cells without extreme stretching
    # - Good erosion survival (>= 0.30): strong physical wood necks
    sol_score = 1.5 - 3.5 * abs(solidity - 0.60)
    if solidity > 0.88 or solidity < 0.28:
        sol_score -= 2.5
    asp_score = 1.2 if aspect <= 2.3 else max(-2.5, 1.2 - 1.2 * (aspect - 2.3))
    ero_score = min(1.8, erosion * 2.4)
    area_score = 0.8 if 0.14 <= area_ratio <= 0.55 else -0.8
    country_score = 0.75 if (prefer_norway and str(rec.get("country", "")).upper() == "NO") else 0.0
    comm_score = 0.35 if bool(rec.get("commercial_ok", False)) else 0.0
    media_score = {"svg_vector": 0.8, "bw_tracing": 0.6, "rubbing_frottage": 0.1}.get(
        str(rec.get("media_type", "")), -0.2
    )
    return (
        tier_score
        + cls_score
        + sol_score
        + asp_score
        + ero_score
        + area_score
        + country_score
        + comm_score
        + media_score
        + 0.8 * conf
    )


def select_diverse_motifs(
    catalog_path: str | Path,
    n_pieces: int,
    seed: int = 42,
    prefer_norway: bool = True,
    commercial_only: bool = False,
    allowed_tiers: Sequence[str] = ("A_ready", "B_thickened"),
    requested_classes: Optional[Sequence[str]] = None,
    family: Optional[str] = None,
) -> List[MotifAsset]:
    """Select `n_pieces` high-quality, diverse Nordic rock-art silhouettes from the curated corpus."""
    records, corpus_dir = load_corpus_catalog(catalog_path)
    rng = np.random.default_rng(seed)

    filtered: List[Dict[str, Any]] = []
    for rec in records:
        if family is not None and rec.get("family") != family:
            continue
        if _effective_tier(rec) not in allowed_tiers:
            continue
        if commercial_only and not bool(rec.get("commercial_ok", False)):
            continue
        if _safe_int(rec.get("connected_components"), 1) != 1:
            continue
        if _safe_int(rec.get("hole_count"), 0) != 0:
            continue
        mask_rel = str(rec.get("mask_path", ""))
        if not mask_rel or not _resolve_asset_path(corpus_dir, mask_rel).exists():
            continue
        filtered.append(rec)

    # Group by motif_class and sort each class by jigsaw quality score (with tiny seeded tie-break)
    scored = [
        (_score_candidate_for_jigsaw(r, prefer_norway=prefer_norway) + float(rng.uniform(0, 0.08)), r)
        for r in filtered
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    by_class: Dict[str, List[Dict[str, Any]]] = {}
    for _, rec in scored:
        cls = str(rec.get("motif_class", "unclassified_motif"))
        by_class.setdefault(cls, []).append(rec)

    selected_recs: List[Dict[str, Any]] = []
    used_ids: set[str] = set()
    used_groups: set[str] = set()

    def pick_from_class(cls_name: str, allow_same_group: bool = False) -> bool:
        cands = by_class.get(cls_name, [])
        for r in cands:
            aid = str(r.get("asset_id", ""))
            sg = str(r.get("source_group", ""))
            if aid in used_ids:
                continue
            if not allow_same_group and sg and sg in used_groups:
                continue
            selected_recs.append(r)
            used_ids.add(aid)
            if sg:
                used_groups.add(sg)
            return True
        return False

    # 1. Honour explicit class requests if given
    if requested_classes:
        for cls in requested_classes:
            if len(selected_recs) >= n_pieces:
                break
            if not pick_from_class(cls, allow_same_group=False):
                pick_from_class(cls, allow_same_group=True)

    # 2. Sweep iconic priority classes to guarantee rich motif diversity (animals + ships + humans + symbols)
    for cls in ICONIC_CLASS_PRIORITY:
        if len(selected_recs) >= n_pieces:
            break
        if any(r.get("motif_class") == cls for r in selected_recs):
            continue
        pick_from_class(cls, allow_same_group=False)

    # 3. Second pass across all named classes (distinct source_group)
    for cls in list(by_class.keys()):
        if len(selected_recs) >= n_pieces:
            break
        if cls in ("unclassified_motif", "multi_motif_panel"):
            continue
        pick_from_class(cls, allow_same_group=False)

    # 4. Fill any remaining slots with highest-scoring distinct assets
    for _, r in scored:
        if len(selected_recs) >= n_pieces:
            break
        aid = str(r.get("asset_id", ""))
        if aid not in used_ids:
            selected_recs.append(r)
            used_ids.add(aid)

    if len(selected_recs) < n_pieces:
        raise ValueError(
            f"Requested {n_pieces} pieces, found {len(selected_recs)} distinct eligible motifs "
            f"in {catalog_path} (family={family!r}, tiers={allowed_tiers}, commercial_only={commercial_only})"
        )
    return [load_motif_asset(r, corpus_dir) for r in selected_recs[:n_pieces]]


# ---------------------------------------------------------------------------
# Synthetic Nordic rock-art silhouettes (strictly for unit tests & smoke checks)
# ---------------------------------------------------------------------------

def _draw_poly_mask(pts: List[Tuple[float, float]], size: int = 256, dilate_px: int = 0) -> np.ndarray:
    img = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(img)
    draw.polygon(pts, fill=255)
    arr = np.asarray(img, dtype=np.uint8) > 127
    if dilate_px > 0:
        arr = ndimage.binary_dilation(arr, iterations=dilate_px)
    arr = ndimage.binary_fill_holes(arr)
    return arr.astype(np.float32)


def make_synthetic_nordic_motifs(n: int = 6, family: Optional[str] = None) -> List[MotifAsset]:
    """Generate clean procedural Nordic rock-art test silhouettes for unit/property tests."""
    specs: List[Tuple[str, str, np.ndarray]] = []

    # 1. Reindeer silhouette (compact body, raised head, swept antlers, stout legs)
    reindeer_pts = [
        (42, 138), (58, 122), (82, 114), (118, 112), (144, 102), (156, 78),
        (144, 52), (160, 44), (174, 62), (192, 42), (206, 52), (190, 80),
        (214, 92), (212, 106), (186, 112), (176, 138), (184, 198), (164, 202),
        (152, 160), (116, 162), (108, 202), (86, 200), (88, 158), (68, 196),
        (48, 192), (56, 154),
    ]
    specs.append(("animal", "reindeer", _draw_poly_mask(reindeer_pts)))

    # 2. Bronze Age Ship (swept double upturned prows + solid hull + crew crest)
    ship_pts = [
        (24, 96), (40, 84), (56, 126), (76, 110), (96, 110), (116, 106),
        (140, 106), (164, 110), (186, 112), (206, 82), (226, 94), (214, 146),
        (184, 174), (128, 184), (72, 174), (40, 146),
    ]
    specs.append(("vessel", "bronze_age_ship", _draw_poly_mask(ship_pts)))

    # 3. Bear (heavy arched back, rounded ears, stout muzzle, thick legs)
    bear_pts = [
        (44, 146), (52, 106), (82, 86), (126, 82), (166, 92), (184, 84),
        (202, 98), (218, 114), (212, 136), (190, 144), (192, 196), (164, 198),
        (154, 164), (116, 164), (106, 198), (78, 198), (72, 164), (52, 182),
    ]
    specs.append(("animal", "bear", _draw_poly_mask(bear_pts)))

    # 4. Whale / Cetacean (curved marine body + dorsal fin + flared tail fluke)
    whale_pts = [
        (28, 132), (54, 104), (96, 92), (124, 68), (138, 94), (176, 112),
        (208, 86), (226, 96), (210, 128), (228, 162), (208, 172), (176, 148),
        (134, 166), (82, 168), (44, 156),
    ]
    specs.append(("animal", "whale_cetacean", _draw_poly_mask(whale_pts)))

    # 5. Skier (leaning hunter on wide solid ski profile)
    skier_pts = [
        (114, 42), (144, 42), (154, 68), (144, 88), (176, 104), (170, 126),
        (144, 120), (156, 166), (214, 162), (226, 178), (204, 196), (42, 206),
        (30, 188), (82, 176), (98, 132), (76, 116), (88, 94), (112, 86),
    ]
    specs.append(("human", "skier", _draw_poly_mask(skier_pts)))

    # 6. Footprint / Foot Sole (classic Nordic Bronze Age carved footprint with toes)
    foot_pts = [
        (92, 212), (158, 212), (176, 180), (172, 136), (186, 92), (174, 52),
        (146, 38), (110, 42), (82, 60), (74, 98), (88, 144), (80, 186),
    ]
    specs.append(("symbol", "footprint", _draw_poly_mask(foot_pts)))

    # 7. Bird / Waterfowl
    bird_pts = [
        (36, 156), (72, 128), (104, 112), (136, 98), (158, 62), (186, 54),
        (216, 68), (184, 86), (176, 124), (156, 160), (124, 180), (132, 208),
        (98, 208), (94, 178), (54, 174),
    ]
    specs.append(("animal", "bird", _draw_poly_mask(bird_pts)))

    # 8. Elk / Moose
    elk_pts = [
        (38, 134), (62, 104), (106, 92), (142, 88), (168, 64), (192, 68),
        (220, 94), (214, 120), (186, 124), (176, 146), (186, 204), (162, 206),
        (148, 162), (104, 162), (96, 206), (72, 204), (72, 158), (44, 166),
    ]
    specs.append(("animal", "elk_moose", _draw_poly_mask(elk_pts)))

    if family is not None:
        specs = [spec for spec in specs if spec[0] == family]
        if not specs:
            raise ValueError(f"No synthetic motifs in family {family!r}")
    assets: List[MotifAsset] = []
    for i in range(n):
        fam, cls, mask = specs[i % len(specs)]
        assets.append(
            MotifAsset(
                asset_id=f"synthetic_{cls}_{i + 1:02d}",
                family=fam,
                motif_class=cls,
                mask=mask,
                sdf=compute_sdf_from_mask(mask),
                site="Synthetic Test",
                region="Norway",
                country="NO",
                period="stone_age_hunter",
                jigsaw_tier="A_ready",
                render_style="solid_pecked",
                source_archive="synthetic",
                source_group=f"synthetic_{i + 1:02d}",
                license="CC0 1.0",
                commercial_ok=True,
                sha256="synthetic",
            )
        )
    return assets
