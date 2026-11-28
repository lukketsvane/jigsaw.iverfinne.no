"""Scientific provenance resolution: taxonomy, core regions, gazetteer, literature, and scale honesty."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Reuse regex patterns consistent with hellerisninger/pipeline/taxonomy.py while staying self-contained
_NON_COMMERCIAL_RE = re.compile(
    r"(?i)(?:\bnc\b|non[\s\-]?commercial|by[\s\-]nc|unknown|all\s+rights\s+reserved|"
    r"copyrighted|restricted|in\s+copyright|fair\s+use|\bnd\b|no[\s\-]?deriv|arr\b|unspecified)"
)

_COMMERCIAL_ALLOWED_RE = re.compile(
    r"(?i)(?:cc0|public\s*domain|\bpd\b|\bpdm\b|cc[\s\-]by(?:[\s\-]sa)?(?:\s*[\d\.]+)?|"
    r"nlod|open\s*data|mit\b|bsd\b|apache|gfdl|free\s*art|fal\b|odbl|ogl)"
)

_SHFA_MOTIFS_RE = re.compile(r"Motifs:\s*([^\n]+)")

_PLACE_STRIP_RE = re.compile(
    r"(?i)\b(?:amtmannsnes\w*|bjørnstad(?!skip)\w*|sundløkken\w*|sundvollen\w*|"
    r"solberg\w*|solbakk\w*|hornes\w*|skien\w*|oppland\w*|nordland\w*|nordre\s+land|"
    r"vestland\w*|hordaland\w*|gretteland\w*|småland\w*|södermanland\w*|västmanland\w*|"
    r"rogaland\w*|sjælland\w*|valla\w*|valdersrud\w*|etnedal\w*|topografisk\w*|tegning\w*)\b"
)

_SAFE_COMPOUND_ROOTS = {
    "reindeer", "reinsdyr", "bølareinen", "bolareinen", "bølarein", "bolarein", "renhjord",
    "elgku", "elgokse", "elgfigur", "älgfigur", "elghode",
    "hvithval", "spekkhogger", "hvalfigur",
    "halibut", "kveite", "hälleflundra", "flyndre", "fiskefigur",
    "sjøfugl", "sjofugl", "waterfowl", "seabird", "fuglefigur", "fågelfigur",
    "svinhäll", "svinhall", "kronhjort", "hjortedyr", "dyrefigur", "fyrfotadjur", "firbeint", "villsvin",
    "solhest", "hestefigur", "hästfigur",
    "skipsfigur", "skipsfigurer", "hällristningsskepp", "helleristningsskip",
    "bronsealderskip", "skeppsbild", "långskepp", "bjørnstadskipet", "bjørnstadskip",
    "brandskogschiff", "båtritual", "båtfigur", "båtmotiv", "skinnbåt", "skinnbaat", "elghodebåt",
    "skiløper", "skiloper", "skidåkare", "skidakare", "rødøymannen", "rodoymannen", "bølamannen", "bolamannen",
    "lurblåser", "lurblaser", "lurblåsare", "lurblasare", "hornblower",
    "bueskytter", "bågskytt", "spjutguden", "spearman",
    "akrobat", "acrobat", "danser", "danseren", "dansare", "brudparet", "bridal_couple", "adorant", "vaulter", "saltomortal",
    "rytter", "ryttare", "horseman", "equestrian",
    "menneskefigur", "mennesker", "menneske", "människa", "människor", "manniska", "anthropomorph",
    "solhjul", "solkors", "hjulkors", "solskive", "solskiva", "ringkors", "wheel cross", "sun wheel",
    "fotsåle", "fotsåler", "fotsale", "fotsula", "fotsulor", "fotspor", "fodspor", "footprint",
    "håndavtrykk", "handavtryck", "handprint",
    "ardplog", "chariot", "plough", "plog",
    "saksefigur", "rammefigur", "labyrint", "labyrinth", "spiral", "geometrisk",
}

_BLOCKED_TEXT_KEYWORDS = {
    "alta", "vingen", "bøla", "bola", "rødøy", "rodoy", "bardal", "leiknes",
    "begby", "solberg", "kalnes", "østfold", "ostfold", "rogaland", "austre åmøy",
    "amoy", "tanum", "vitlycke", "bohuslän", "bohuslan", "nämforsen", "namforsen",
    "næmforsen", "aspeberget", "fossum", "litsleby", "himmelstalund", "simrishamn",
    "simris", "kivik", "madsebakke", "bornholm", "skavberget", "møllerstufossen",
    "forselv", "kåfjord", "strand", "drammen", "skjeberg", "alstahaug", "zalavruga",
    "kalleby", "tegneby", "frännarp", "frannarp", "finntorp", "sotts",
    "bronze", "bronse", "veideristning", "figur", "figure",
    "and", "ski", "sun", "sol", "tegn", "man", "mann", "hand", "tre", "horn", "hi", "den",
    "ko", "ku", "ren", "val", "bat", "ring", "circle", "sirkel", "cirkel", "net", "nett",
    "grid", "frame", "ramme", "skålgrop", "skalgrop", "cupmark", "cup mark", "cup-mark",
    "skålgropar", "älvkvarn", "abstract", "symbol", "ornament", "drum", "tromme", "foot",
    "shoe", "sko", "sandal", "sole", "såle", "wheel", "hjul", "branch", "gren", "plant",
    "leaf", "løv", "gran", "spruce", "jump", "leap", "ritual", "wedding", "bride",
    "battle", "kamp", "strid", "corral",
}

_EXTRA_CLASS_KEYWORDS: Dict[str, List[str]] = {
    "reindeer": ["reinsdyr", "bølareinen", "bølarein", "reinen", "renhjord", "reindeer"],
    "elk_moose": ["elks", "elg", "elgen", "elger", "älg", "älgen", "älgar", "moose", "alces"],
    "bear": ["bears", "bjørn", "bjørnen", "björn", "isbjørn"],
    "whale_cetacean": ["whales", "hval", "hvalen", "hvaler", "nise", "spekkhogger", "hvithval", "porpoise", "beluga", "delfin", "dolphin", "sel", "säl", "seal", "seals"],
    "fish": ["fishes", "fisk", "fisken", "fisker", "kveite", "halibut", "hälleflundra", "laks", "lax", "salmon", "flyndre", "torsk"],
    "bird": ["birds", "fugl", "fugler", "fågel", "fåglar", "svane", "svan", "swan", "swans", "trane", "crane", "gås", "goose", "skarv", "ørn", "eagle", "sjøfugl"],
    "horse": ["horses", "hest", "hesten", "hester", "häst", "hästar", "stallion", "hingst"],
    "dog_wolf": ["dogs", "hund", "hunder", "wolf", "ulv", "varg", "canine", "canid"],
    "snake": ["snakes", "slange", "huggorm", "serpent", "viper"],
    "quadruped_other": ["svinhällen", "svinhäll", "hjort", "hjorten", "hjortedyr", "kronhjort", "dyrefigur", "animals", "okse", "oxen", "cattle", "boar", "pig", "svin", "villsvin", "fyrfotadjur"],
    "bronze_age_ship": ["ships", "skipsfigurer", "skipsfigur", "hällristningsskepp", "helleristningsskip", "bronsealderskip", "bjørnstadskipet", "bjørnstadskip", "långskepp", "skeppsbild", "brandskogschiff", "båtmotiv", "skip", "skipet", "skepp", "skeppet", "skib"],
    "stone_age_boat": ["boats", "skinnbåt", "skinnbaat", "elghodebåt", "båtritual", "båtfigur", "båt", "båten", "båter", "båtar", "umiak", "canoe"],
    "skier": ["skiers", "skiløper", "skiloper", "skidåkare", "skidakare", "rødøymannen", "bølamannen", "truger"],
    "hunter_warrior": ["warriors", "hunters", "archers", "kriger", "krigare", "jeger", "jägare", "bueskytter", "bågskytt", "spjutguden", "spearman", "axe_bearer", "spear_bearer", "shield_bearer"],
    "lur_blower": ["lurblåser", "lurblåsare", "hornblower", "lur_blower"],
    "acrobat_dancer": ["dancers", "acrobats", "danseren", "danser", "dansare", "akrobat", "brudparet", "bridal_couple", "adorant", "vaulter", "acrobat_vaulter"],
    "rider": ["riders", "rytter", "ryttare", "horseman", "equestrian"],
    "anthropomorph": ["humans", "menneske", "mennesker", "menneskefigur", "människa", "människor", "anthropomorph", "shaman", "sjaman"],
    "sun_wheel": ["solhjul", "solkors", "hjulkors", "solskive", "solskiva", "ringkors", "wheel cross", "sun wheel", "sun_wheel"],
    "footprint": ["footprints", "fotsåle", "fotsåler", "fotsula", "fotsulor", "fotspor", "fodspor", "foot sole", "shoe sole"],
    "handprint": ["handprints", "håndavtrykk", "handavtryck"],
    "axe_weapon": ["axes", "weapons", "øks", "yxa", "halberd", "dolk", "dagger", "sverd", "svärd", "sword", "våpen", "vapen"],
    "chariot_plough": ["chariots", "wagons", "ploughs", "vogn", "vagn", "plog", "ardplog", "ård", "chariot_wagon"],
    "tree": ["trees", "träd"],
    "geometric_spiral": ["spirals", "spiral", "double_spiral", "triple_spiral", "labyrint", "labyrinth", "saksefigur", "rammefigur", "frame_figure", "net_figure", "concentric_circle", "geometrisk"],
}

_SHFA_TAG_TO_CLASS: Dict[str, Optional[str]] = {
    "ship or boat": "bronze_age_ship",
    "sun ship": "bronze_age_ship",
    "unspecified animal": "quadruped_other",
    "deer": "quadruped_other",
    "cattle": "quadruped_other",
    "pig": "quadruped_other",
    "wild boar": "quadruped_other",
    "goat": "quadruped_other",
    "sheep": "quadruped_other",
    "moose": "elk_moose",
    "elk": "elk_moose",
    "reindeer": "reindeer",
    "horse": "horse",
    "bird": "bird",
    "fish": "fish",
    "bear": "bear",
    "whale": "whale_cetacean",
    "seal": "whale_cetacean",
    "canine": "dog_wolf",
    "dog": "dog_wolf",
    "wolf": "dog_wolf",
    "snake": "snake",
    "human or anthropomorph": "anthropomorph",
    "female": "anthropomorph",
    "male": "anthropomorph",
    "human figure procession": "anthropomorph",
    "axe bearer": "hunter_warrior",
    "spear bearer": "hunter_warrior",
    "shield bearer": "hunter_warrior",
    "sword bearer": "hunter_warrior",
    "archer": "hunter_warrior",
    "warrior": "hunter_warrior",
    "hornblower": "lur_blower",
    "lur blower": "lur_blower",
    "adorant": "acrobat_dancer",
    "vaulter": "acrobat_dancer",
    "bridal couple": "acrobat_dancer",
    "sun holder": "acrobat_dancer",
    "rider": "rider",
    "foot sole or shoe sole": "footprint",
    "footprint": "footprint",
    "hand": "handprint",
    "handprint": "handprint",
    "wheel cross": "sun_wheel",
    "sun wheel": "sun_wheel",
    "wagon": "chariot_plough",
    "chariot": "chariot_plough",
    "plough": "chariot_plough",
    "tree": "tree",
    "axe": "axe_weapon",
    "unspecified weapon": "axe_weapon",
    "weapon": "axe_weapon",
    "sword": "axe_weapon",
    "spiral": "geometric_spiral",
    "double spiral": "geometric_spiral",
    "triple spiral": "geometric_spiral",
    "concentric circle": "geometric_spiral",
    "frame figure": "geometric_spiral",
    "net": "geometric_spiral",
    "net figure": "geometric_spiral",
    "unspecified geometric figure": "geometric_spiral",
    "cupmarks": None,
    "cupmark": None,
    "circle": None,
    "detail": None,
    "tracing": None,
    "rubbing": None,
    "entire carved surface": None,
    "indeterminate figure": None,
    "damage": None,
    "trough-shaped depression": None,
    "oblong depression": None,
    "hook": None,
    "line": None,
    "later carving": None,
    "equipment": None,
    "animal tracks": None,
    "mantel": None,
    "pecking marks": None,
    "carving on blocks": None,
    "painted": None,
    "object": None,
}

_STONE_AGE_SHFA_SITES = (
    "nämforsen", "namforsen", "vingen", "vingeneset", "bak vehammaren", "bakkane",
    "urane", "vindbakken", "teigen", "leitet", "storåkeren", "ved vatnet",
    "lyngrabben", "brattebakken", "alta", "bardal", "evenhus", "leiknes", "rødøy",
)

_SPECIFIC_PRIORITY = [
    "skier", "lur_blower", "chariot_plough", "reindeer", "elk_moose", "whale_cetacean",
    "bear", "fish", "bird", "horse", "dog_wolf", "snake", "sun_wheel", "footprint",
    "handprint", "axe_weapon", "tree", "rider", "acrobat_dancer", "hunter_warrior",
    "bronze_age_ship", "stone_age_boat", "quadruped_other", "anthropomorph",
    "geometric_spiral", "multi_motif_panel", "unclassified_motif",
]


def load_taxonomy_index(taxonomy_path: Path) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Load taxonomy JSON and build motif_class -> entry lookup index."""
    with open(taxonomy_path, "r", encoding="utf-8") as f:
        taxonomy = json.load(f)
    index: Dict[str, Dict[str, Any]] = {}
    for family_id, fam_data in taxonomy.get("families", {}).items():
        for class_id, cls_data in fam_data.get("classes", {}).items():
            entry = dict(cls_data)
            entry["family"] = family_id
            entry["motif_class"] = class_id
            seen = set(k.lower().strip() for k in entry.get("keywords", []))
            merged = list(entry.get("keywords", []))
            for extra in _EXTRA_CLASS_KEYWORDS.get(class_id, []):
                if extra.lower().strip() not in seen:
                    merged.append(extra)
                    seen.add(extra.lower().strip())
            entry["keywords"] = merged
            index[class_id] = entry
    return taxonomy, index


def validate_commercial_ok(license_str: Optional[str], manifest_flag: Any = None) -> bool:
    """Strictly check whether a licence permits commercial reuse."""
    if manifest_flag is False:
        return False
    if not license_str or not isinstance(license_str, str):
        return False
    cleaned = license_str.strip()
    if not cleaned:
        return False
    if _NON_COMMERCIAL_RE.search(cleaned):
        return False
    return bool(_COMMERCIAL_ALLOWED_RE.search(cleaned))


def _word_boundary_match(keyword: str, text: str) -> bool:
    kw = keyword.lower().strip()
    if not kw:
        return False
    if kw in _SAFE_COMPOUND_ROOTS:
        return kw in text
    return bool(re.search(rf"(?<![a-zæøåäö0-9]){re.escape(kw)}(?![a-zæøåäö0-9])", text))


def _score_text_classes(text: str, class_index: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
    low = _PLACE_STRIP_RE.sub(" ", (text or "").lower())
    scores: Dict[str, float] = {}
    for cls_id, info in class_index.items():
        if cls_id in ("unclassified_motif", "multi_motif_panel"):
            continue
        score = 0.0
        for kw in info.get("keywords", []):
            kw_l = kw.lower().strip()
            if kw_l in _BLOCKED_TEXT_KEYWORDS:
                continue
            if _word_boundary_match(kw_l, low):
                score += 2.0 if len(kw_l) >= 5 else 1.2
        if score > 0:
            scores[cls_id] = score
    return scores


def _shape_class_affinity(cls_id: str, metrics: Optional[Dict[str, Any]], candidates: Optional[Set[str]] = None) -> float:
    if not metrics:
        return 0.0
    ar = float(metrics.get("aspect_ratio", 1.0))
    solidity = float(metrics.get("solidity", 0.5))
    circularity = float(metrics.get("circularity", metrics.get("compactness", 0.3)))
    cands = candidates or set()
    has_animals = bool(cands & {"reindeer", "elk_moose", "bear", "whale_cetacean", "fish", "bird", "horse", "dog_wolf", "quadruped_other"})
    has_vessels = bool(cands & {"bronze_age_ship", "stone_age_boat"})

    bonus = 0.0
    if cls_id in ("bronze_age_ship", "stone_age_boat"):
        if has_animals:
            if ar >= 2.05 and solidity <= 0.72:
                bonus += 2.0
            elif ar < 1.10:
                bonus -= 1.8
        else:
            if ar >= 1.70:
                bonus += 2.0
            elif ar >= 1.38:
                bonus += 0.9
            elif ar < 0.85:
                bonus -= 1.8
    elif cls_id in ("skier", "hunter_warrior", "lur_blower", "acrobat_dancer", "anthropomorph"):
        if ar <= 0.68:
            bonus += 1.8
        elif ar <= 0.80:
            bonus += 0.9
        elif ar >= 1.45:
            bonus -= 1.6
    elif cls_id in ("reindeer", "elk_moose", "bear", "horse", "dog_wolf", "quadruped_other"):
        if 1.05 <= ar <= 1.85:
            bonus += 0.8
        elif ar > 2.4 or ar < 0.55:
            bonus -= 1.2
    elif cls_id in ("whale_cetacean", "fish"):
        if not has_vessels and ar >= 1.45 and solidity >= 0.62:
            bonus += 1.4
    elif cls_id == "sun_wheel":
        if 0.78 <= ar <= 1.28 and circularity >= 0.48:
            bonus += 2.0
        elif ar > 1.65 or ar < 0.60:
            bonus -= 1.8
    elif cls_id == "footprint":
        if (0.32 <= ar <= 0.60 or 1.70 <= ar <= 3.1) and solidity >= 0.78:
            bonus += 2.0
        elif solidity < 0.65:
            bonus -= 1.5
    return bonus


def _disambiguate_multi_class(
    candidate_scores: Dict[str, float],
    class_index: Dict[str, Dict[str, Any]],
    shape_metrics: Optional[Dict[str, Any]],
) -> Tuple[str, str, float]:
    if not candidate_scores:
        return "scene_or_unclassified", "unclassified_motif", 0.25
    cand_set = set(candidate_scores.keys())
    specific_animals = cand_set & {"reindeer", "elk_moose", "bear", "whale_cetacean", "fish", "bird", "horse", "dog_wolf", "snake"}
    if len(cand_set) == 2 and "quadruped_other" in cand_set and len(specific_animals) == 1:
        only_sp = next(iter(specific_animals))
        return class_index[only_sp]["family"], only_sp, 0.72
    if not shape_metrics:
        return "scene_or_unclassified", "unclassified_motif", 0.30

    ranked = sorted(
        [(_shape_class_affinity(cls_id, shape_metrics, cand_set), sc, cls_id) for cls_id, sc in candidate_scores.items()],
        reverse=True,
    )
    top_aff = ranked[0][0]
    if top_aff < 1.2:
        return "scene_or_unclassified", "unclassified_motif", 0.30
    if len(ranked) == 1 or (top_aff - ranked[1][0]) >= 0.9:
        best = ranked[0][2]
        return class_index[best]["family"], best, 0.64
    close_top = [c for aff, _, c in ranked if (top_aff - aff) < 0.9]
    if close_top and all(class_index[c]["family"] == "human" for c in close_top):
        return "human", "anthropomorph", 0.62
    return "scene_or_unclassified", "unclassified_motif", 0.30


def classify_motif_scientific(
    record: Dict[str, Any],
    class_index: Dict[str, Dict[str, Any]],
    num_extracted_from_source: int = 1,
    shape_metrics: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, float]:
    """Archive-aware, strictly honest motif classification against taxonomy.json."""
    archive = str(record.get("source_archive") or "").lower().strip()
    hints = record.get("motif_hints") or []
    if isinstance(hints, str):
        hints = [hints]
    title = str(record.get("title") or "")
    desc = str(record.get("description") or "")
    site = str(record.get("site") or "")
    raw_name = Path(str(record.get("raw_path") or "")).stem.replace("_", " ").replace("-", " ")
    period = str(record.get("period") or "unknown").lower()

    if archive == "shfa":
        m = _SHFA_MOTIFS_RE.search(desc)
        if m:
            site_l = site.lower()
            is_stone = any(tok in site_l for tok in _STONE_AGE_SHFA_SITES)
            classes: Set[str] = set()
            for tag in [t.strip().rstrip(".").lower() for t in m.group(1).split(",") if t.strip()]:
                if tag.startswith("ship (boat)") or tag.startswith("ship or boat") or tag == "sun ship":
                    classes.add("stone_age_boat" if is_stone else "bronze_age_ship")
                elif tag in _SHFA_TAG_TO_CLASS and _SHFA_TAG_TO_CLASS[tag]:
                    mapped = _SHFA_TAG_TO_CLASS[tag]
                    if mapped == "bronze_age_ship" and is_stone:
                        mapped = "stone_age_boat"
                    classes.add(mapped)  # type: ignore[arg-type]
            if not classes:
                return "scene_or_unclassified", "unclassified_motif", 0.25
            if len(classes) == 1:
                only = next(iter(classes))
                return class_index[only]["family"], only, (0.88 if num_extracted_from_source <= 1 else 0.78)
            return _disambiguate_multi_class({c: 3.0 for c in classes if c in class_index}, class_index, shape_metrics)

    if archive == "altarockart":
        t_sc = _score_text_classes(title, class_index)
        d_clean = "" if desc.lower().startswith("digital rock-art tracing") else desc
        d_sc = _score_text_classes(d_clean, class_index)
        comb = {c: t_sc.get(c, 0.0) * 1.4 + d_sc.get(c, 0.0) * 0.8 for c in set(t_sc) | set(d_sc)}
        if "bronze_age_ship" in comb and "stone_age_boat" in comb:
            comb["stone_age_boat"] += 2.0
        if not comb:
            return "scene_or_unclassified", "unclassified_motif", 0.25
        if len(comb) == 1:
            only = next(iter(comb))
            return class_index[only]["family"], only, (0.82 if num_extracted_from_source <= 1 else 0.75)
        return _disambiguate_multi_class(comb, class_index, shape_metrics)

    if archive in ("unimus", "kulturminnesok", "other"):
        t_sc = _score_text_classes(title, class_index)
        d_sc = _score_text_classes(desc, class_index)
        comb = {c: t_sc.get(c, 0.0) * 1.4 + d_sc.get(c, 0.0) * 0.8 for c in set(t_sc) | set(d_sc)}
        if "bronze_age_ship" in comb and "stone_age_boat" in comb:
            comb.pop("bronze_age_ship" if "stone_age" in period else "stone_age_boat", None)
        elif "bronze_age_ship" in comb and "stone_age" in period and "bronse" not in f"{title} {desc}".lower():
            comb["stone_age_boat"] = comb.pop("bronze_age_ship")
        if comb:
            if len(comb) == 1:
                only = next(iter(comb))
                return class_index[only]["family"], only, (0.85 if num_extracted_from_source <= 1 else 0.78)
            return _disambiguate_multi_class(comb, class_index, shape_metrics)
        site_l = site.lower()
        if archive == "unimus" and (site_l.startswith("vingen") or site_l.startswith("vingelva")):
            ar = float((shape_metrics or {}).get("aspect_ratio", 1.2))
            if 0.72 <= ar <= 2.40:
                return "animal", "quadruped_other", 0.70
        return "scene_or_unclassified", "unclassified_motif", 0.25

    direct: List[str] = []
    safe_tokens: List[str] = []
    for h in hints:
        hn = str(h).strip().lower().replace(" ", "_").replace("-", "_")
        if hn in ("unclassified_motif", "multi_motif_panel", "rock_carving", "cup_mark", "circle"):
            continue
        if hn in class_index:
            direct.append(hn)
        else:
            safe_tokens.append(hn)

    h_sc = _score_text_classes(" ".join(safe_tokens), class_index)
    for dm in direct:
        h_sc[dm] = h_sc.get(dm, 0.0) + 5.0
    t_sc = _score_text_classes(f"{title} {raw_name}", class_index)
    d_sc = _score_text_classes(desc, class_index)
    comb = {
        c: h_sc.get(c, 0.0) * 1.5 + t_sc.get(c, 0.0) * 1.2 + d_sc.get(c, 0.0) * 0.6
        for c in set(h_sc) | set(t_sc) | set(d_sc)
    }

    if "bronze_age_ship" in comb and "stone_age_boat" in comb:
        if "stone_age" in period:
            comb["stone_age_boat"] += 2.0
            if len(comb) == 2:
                comb.pop("bronze_age_ship", None)
        elif "bronze_age" in period:
            comb["bronze_age_ship"] += 2.0
            if len(comb) == 2:
                comb.pop("stone_age_boat", None)
    elif "stone_age_boat" in comb and "bronze_age" in period:
        comb["bronze_age_ship"] = comb.pop("stone_age_boat")

    if num_extracted_from_source <= 1:
        if comb:
            ranked = []
            for cls_id, sc in comb.items():
                bonus = _shape_class_affinity(cls_id, shape_metrics, set(comb.keys())) * 0.35
                if period != "unknown" and class_index[cls_id].get("typical_period") == period:
                    bonus += 0.4
                prio = -_SPECIFIC_PRIORITY.index(cls_id) if cls_id in _SPECIFIC_PRIORITY else -99
                ranked.append((sc + bonus, prio, cls_id))
            ranked.sort(reverse=True)
            best = ranked[0][2]
            conf = min(0.95, 0.55 + 0.08 * min(ranked[0][0], 5.0))
            return class_index[best]["family"], best, round(conf, 3)
        if any(w in f"{title} {desc} {raw_name}".lower() for w in ("panel", "scene", "felt", "komposisjon", "composition")):
            return "scene_or_unclassified", "multi_motif_panel", 0.50
        return "scene_or_unclassified", "unclassified_motif", 0.25

    if not comb:
        return "scene_or_unclassified", "unclassified_motif", 0.25
    sig = {c: sc for c, sc in comb.items() if sc >= 1.5}
    if len(sig) == 1:
        only = next(iter(sig))
        conf = min(0.86, 0.55 + 0.07 * min(sig[only], 4.0))
        return class_index[only]["family"], only, round(conf, 3)
    return _disambiguate_multi_class(sig or comb, class_index, shape_metrics)


def classify_core_region(record: Dict[str, Any]) -> str:
    """Assign canonical core-region bucket: alta, ostfold, bohuslan_tanum, namforsen, vingen, trondelag, or other."""
    blob = " ".join(
        str(record.get(k) or "")
        for k in ("core_region", "region", "site", "title", "description", "source_id")
    ).lower()

    if any(t in blob for t in ("alta", "hjemmeluft", "kåfjord", "kafjord", "amtmannsnes", "bergbukten", "ole pedersen", "jiepmaluokta", "storsteinen", "apanes", "transfarelv", "bossekop")):
        return "alta"
    if any(t in blob for t in ("østfold", "ostfold", "begby", "solberg", "kalnes", "skjeberg", "fredrikstad", "sarpsborg", "halden", "bjørnstad", "hornes", "hafslund", "gunnarstorp", "borge")):
        return "ostfold"
    if any(t in blob for t in ("bohuslän", "bohuslan", "tanum", "vitlycke", "aspeberget", "fossum", "litsleby", "tegneby", "kalleby", "tossene", "brastad", "askum", "bottna", "svenneby", "kville", "skee", "lysekil", "sotenäs", "munkedal")):
        return "bohuslan_tanum"
    if any(t in blob for t in ("nämforsen", "namforsen", "næmforsen", "ådals-liden", "adals-liden", "ångermanland", "angermanland", "bradön", "notön", "laxön")):
        return "namforsen"
    if any(t in blob for t in ("vingen", "vingelva", "vehammaren", "vingeneset", "bremanger")):
        return "vingen"
    if any(t in blob for t in ("trøndelag", "trondelag", "bardal", "bøla", "bola", "steinmohaugen", "hegra", "leirfall", "stykket", "evenhus", "holtås", "stjørdal", "steinkjer", "levanger", "verdal")):
        return "trondelag"
    return "other"


def _norm_key(text: str) -> str:
    return re.sub(r"[^a-z0-9æøåäö]+", " ", (text or "").lower()).strip()


def load_gazetteer(gazetteer_path: Optional[Path]) -> List[Dict[str, Any]]:
    """Load site_gazetteer.json supporting list, {'sites': [...]}, or dict keyed by site/ID."""
    if not gazetteer_path or not gazetteer_path.is_file():
        return []
    with open(gazetteer_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        if isinstance(data.get("sites"), list):
            return [d for d in data["sites"] if isinstance(d, dict)]
        if isinstance(data.get("panels"), list):
            return [d for d in data["panels"] if isinstance(d, dict)]
        entries: List[Dict[str, Any]] = []
        for k, v in data.items():
            if isinstance(v, dict) and k not in ("metadata", "schema_version", "generated_at", "summary"):
                entry = dict(v)
                entry.setdefault("gazetteer_key", k)
                entries.append(entry)
        return entries
    return []


def load_literature(literature_path: Optional[Path]) -> List[Dict[str, Any]]:
    """Load literature.json supporting list, {'references': [...]}, or dict keyed by ref ID."""
    if not literature_path or not literature_path.is_file():
        return []
    with open(literature_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for key in ("references", "literature", "citations", "items", "entries"):
            if isinstance(data.get(key), list):
                return [d for d in data[key] if isinstance(d, dict)]
        entries: List[Dict[str, Any]] = []
        for k, v in data.items():
            if isinstance(v, dict) and k not in ("metadata", "schema_version", "generated_at", "summary"):
                entry = dict(v)
                entry.setdefault("ref_id", k)
                entries.append(entry)
        return entries
    return []


def _extract_id_from_record(record: Dict[str, Any]) -> Optional[str]:
    ask = record.get("askeladden_id")
    if ask is not None and str(ask).strip():
        s = str(ask).strip()
        return s if s.lower().startswith("askeladden") else f"Askeladden:{s}"
    lam = record.get("lamning_id")
    raa = record.get("raa_id")
    if lam and raa:
        return f"{str(lam).strip()} ({str(raa).strip()})"
    for key in ("site_panel_id", "lamning_id", "raa_id", "fmis_id", "fornsok_id"):
        val = record.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    text = f"{record.get('title', '')} {record.get('site', '')} {record.get('description', '')}"
    m_ask = re.search(r"(?i)\bAskeladden(?:\s*ID)?\s*[:#]?\s*(\d{4,8}(?:-\d+)?)\b", text)
    if m_ask:
        return f"Askeladden:{m_ask.group(1)}"
    m_fmis = re.search(r"(?i)\b(L\d{4}:\d{3,6}|RAÄ\s*[A-ZÅÄÖa-zåäö]+\s*\d+:\d+)\b", text)
    if m_fmis:
        return m_fmis.group(1).strip()
    return None


def _format_entry_panel_id(entry: Dict[str, Any]) -> Optional[str]:
    ask = entry.get("askeladden_id")
    if ask is not None and str(ask).strip():
        s = str(ask).strip()
        return s if s.lower().startswith("askeladden") else f"Askeladden:{s}"
    lam = entry.get("lamning_id")
    raa = entry.get("raa_id")
    if lam and raa:
        return f"{str(lam).strip()} ({str(raa).strip()})"
    for k in ("site_panel_id", "lamning_id", "raa_id", "fmis_id", "l_nummer"):
        v = entry.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


def resolve_site_and_coords(
    record: Dict[str, Any],
    gazetteer_entries: List[Dict[str, Any]],
    core_region: str,
) -> Tuple[Optional[str], str, Optional[Dict[str, Any]], Dict[str, Any]]:
    """Resolve verified site_panel_id and EPSG:4326 coordinates without ever fabricating."""
    rec_coords = record.get("coordinates") if isinstance(record.get("coordinates"), dict) else {}
    rec_lat = record.get("lat") if record.get("lat") is not None else rec_coords.get("lat")
    rec_lon = record.get("lon") if record.get("lon") is not None else rec_coords.get("lon")

    rec_id = _extract_id_from_record(record)
    site_str = _norm_key(str(record.get("site") or ""))
    title_str = _norm_key(str(record.get("title") or ""))
    rec_site_key = f"{record.get('country', '')}:{record.get('region', '')}:{record.get('site', '')}"

    matched_entry: Optional[Dict[str, Any]] = None
    best_score = 0

    for entry in gazetteer_entries:
        score = 0
        if entry.get("site_key") and entry.get("site_key") == rec_site_key:
            score = 110
        else:
            e_ids = [
                str(entry.get(k) or "").strip()
                for k in ("site_panel_id", "askeladden_id", "lamning_id", "fmis_id", "raa_id", "l_nummer", "gazetteer_key")
                if entry.get(k)
            ]
            if rec_id and any(rec_id.lower() in eid.lower() or eid.lower() in rec_id.lower() for eid in e_ids):
                score = 100
            else:
                names = [str(entry.get("site_name") or entry.get("name") or entry.get("site") or "")]
                names.extend([str(a) for a in (entry.get("aliases") or [])])
                for nm in names:
                    nn = _norm_key(nm)
                    if not nn or len(nn) < 3:
                        continue
                    if nn == site_str:
                        score = max(score, 85)
                    elif nn in site_str or (len(site_str) >= 4 and site_str in nn):
                        score = max(score, 65)
                    elif nn in title_str:
                        score = max(score, 50)
        if score == 0 and entry.get("core_region") == core_region and core_region != "other":
            if entry.get("is_region_fallback"):
                score = 20
        if score > best_score:
            best_score = score
            matched_entry = entry

    panel_id = rec_id
    panel_id_prov = "manifest_or_title" if rec_id else "unverified_no_panel_id"
    if not panel_id and matched_entry is not None and best_score >= 50:
        formatted = _format_entry_panel_id(matched_entry)
        if formatted:
            panel_id = formatted
            panel_id_prov = f"gazetteer:{matched_entry.get('site_key') or 'site_gazetteer.json'}"

    # Resolve coordinates (prefer manifest record if present, else matched gazetteer entry)
    if isinstance(rec_lat, (int, float)) and isinstance(rec_lon, (int, float)):
        src_prov = str(
            rec_coords.get("provenance")
            or rec_coords.get("source")
            or record.get("coordinate_source")
            or record.get("source_archive")
            or "source_manifest"
        )
        prec = str(rec_coords.get("precision") or record.get("coordinate_precision") or "manifest_record")
        coords = {
            "crs": "EPSG:4326",
            "lat": round(float(rec_lat), 6),
            "lon": round(float(rec_lon), 6),
            "precision": prec,
            "provenance": src_prov,
        }
        prov = {"crs": "EPSG:4326", "status": "verified", "reason": src_prov}
        return panel_id, panel_id_prov, coords, prov

    if matched_entry is not None and best_score >= 50:
        e_coords = matched_entry.get("coordinates") if isinstance(matched_entry.get("coordinates"), dict) else {}
        e_lat = matched_entry.get("lat") if matched_entry.get("lat") is not None else e_coords.get("lat")
        e_lon = matched_entry.get("lon") if matched_entry.get("lon") is not None else e_coords.get("lon")
        if isinstance(e_lat, (int, float)) and isinstance(e_lon, (int, float)):
            src_prov = str(
                e_coords.get("provenance")
                or e_coords.get("source")
                or matched_entry.get("coordinate_source")
                or matched_entry.get("source")
                or "helleristninger/metadata/site_gazetteer.json"
            )
            prec = str(
                e_coords.get("precision")
                or matched_entry.get("coordinate_precision")
                or matched_entry.get("precision")
                or "site_gazetteer"
            )
            coords = {
                "crs": "EPSG:4326",
                "lat": round(float(e_lat), 6),
                "lon": round(float(e_lon), 6),
                "precision": prec,
                "provenance": src_prov,
            }
            prov = {"crs": "EPSG:4326", "status": "verified", "reason": f"gazetteer_match_score_{best_score}"}
            return panel_id, panel_id_prov, coords, prov

    reason = (
        str(matched_entry.get("unverified_reason") or matched_entry.get("coordinate_reason") or "Gazetteer entry has null coordinates")
        if matched_entry is not None
        else "No verified site match in site_gazetteer.json and no coordinates in source manifest"
    )
    return panel_id, panel_id_prov, None, {"crs": "EPSG:4326", "status": "unverified", "reason": reason}


_REGION_TOKENS_BY_CORE: Dict[str, Tuple[str, ...]] = {
    "alta": ("alta", "finnmark", "northern fennoscandia"),
    "ostfold": ("østfold", "ostfold"),
    "bohuslan_tanum": ("bohuslän", "bohuslan", "tanum"),
    "namforsen": ("nämforsen", "namforsen", "ångermanland", "angermanland"),
    "vingen": ("vingen", "vestland"),
    "trondelag": ("trøndelag", "trondelag"),
}

_COUNTRY_NAMES: Dict[str, str] = {
    "NO": "norway",
    "SE": "sweden",
    "DK": "denmark",
}


def resolve_literature(
    record: Dict[str, Any],
    core_region: str,
    site_panel_id: Optional[str],
    literature_entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Match verified peer-reviewed/archival literature citations to a figure."""
    refs: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    for r in (record.get("literature") or []):
        key = json.dumps(r, sort_keys=True) if isinstance(r, dict) else str(r)
        if key not in seen:
            seen.add(key)
            refs.append(r if isinstance(r, dict) else {"citation": str(r)})

    site_l = str(record.get("site") or "").lower()
    archive_l = str(record.get("source_archive") or "").lower()
    period_l = str(record.get("period") or "unknown").lower()
    country_u = str(record.get("country") or "").upper()
    country_name = _COUNTRY_NAMES.get(country_u, "")
    core_tokens = _REGION_TOKENS_BY_CORE.get(core_region, ())

    for entry in literature_entries:
        matched = False
        raw_regions = [str(x).lower() for x in (entry.get("core_regions") or entry.get("regions") or [])]
        if entry.get("core_region"):
            raw_regions.append(str(entry["core_region"]).lower())

        # Direct core_region key or substring match against human-readable region descriptions
        if core_region in raw_regions:
            matched = True
        elif core_tokens and any(any(tok in rr for tok in core_tokens) for rr in raw_regions):
            matched = True
        elif country_name and any(country_name in rr for rr in raw_regions):
            # Country-wide synthesis works (e.g. Lødøen & Mandt 2010 Norway; Malmer 1981; SHFA archive)
            matched = True

        sites = [str(x).lower() for x in (entry.get("sites") or [])]
        if site_l and any(s in site_l for s in sites if len(s) >= 3):
            matched = True
        if site_panel_id and site_panel_id in [str(x) for x in (entry.get("site_panel_ids") or [])]:
            matched = True
        archives = [str(x).lower() for x in (entry.get("archives") or [])]
        if archive_l and archive_l in archives:
            matched = True

        # Filter by period compatibility when both figure and citation specify distinct non-mixed periods
        c_periods = [str(p).lower() for p in (entry.get("periods") or [])]
        if matched and c_periods and period_l not in ("", "unknown", "mixed"):
            if (period_l not in c_periods) and ("mixed" not in c_periods):
                matched = False

        if matched:
            ref_id = str(entry.get("citation_id") or entry.get("ref_id") or entry.get("cite_key") or entry.get("title") or len(refs))
            if ref_id not in seen:
                seen.add(ref_id)
                authors = entry.get("authors") or entry.get("author") or ""
                if isinstance(authors, list):
                    authors = "; ".join(str(a) for a in authors)
                refs.append(
                    {
                        "ref_id": ref_id,
                        "authors": authors,
                        "year": entry.get("year"),
                        "title": entry.get("title") or "",
                        "publication": entry.get("publication") or entry.get("journal") or entry.get("publisher") or "",
                        "doi_or_url": entry.get("doi") or entry.get("url") or "",
                    }
                )
    return refs


def resolve_scale_calibration(record: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve true physical scale ONLY when explicitly calibrated. Never fabricate mm scale."""
    px_per_mm = record.get("px_per_mm")
    if isinstance(px_per_mm, (int, float)) and float(px_per_mm) > 0:
        return {
            "scale_calibrated": True,
            "px_per_mm": round(float(px_per_mm), 6),
            "scale_source": str(record.get("scale_source") or "explicit_manifest_px_per_mm"),
        }

    mm_per_px = record.get("mm_per_px")
    if isinstance(mm_per_px, (int, float)) and float(mm_per_px) > 0:
        return {
            "scale_calibrated": True,
            "px_per_mm": round(1.0 / float(mm_per_px), 6),
            "scale_source": str(record.get("scale_source") or "explicit_manifest_mm_per_px"),
        }

    sb_mm = record.get("scale_bar_mm")
    sb_px = record.get("scale_bar_px")
    if isinstance(sb_mm, (int, float)) and isinstance(sb_px, (int, float)) and float(sb_mm) > 0 and float(sb_px) > 0:
        return {
            "scale_calibrated": True,
            "px_per_mm": round(float(sb_px) / float(sb_mm), 6),
            "scale_source": str(record.get("scale_source") or "calibrated_scale_bar_mm_px"),
        }

    phys_w = record.get("physical_width_mm")
    w_px = record.get("width_px")
    if isinstance(phys_w, (int, float)) and isinstance(w_px, (int, float)) and float(phys_w) > 0 and float(w_px) > 0:
        return {
            "scale_calibrated": True,
            "px_per_mm": round(float(w_px) / float(phys_w), 6),
            "scale_source": str(record.get("scale_source") or "explicit_physical_width_mm"),
        }

    mtype = str(record.get("media_type") or "").lower()
    if mtype == "svg_vector":
        return {
            "scale_calibrated": False,
            "px_per_mm": None,
            "scale_source": "uncalibrated_svg_vector_lacks_rock_scale_bar",
        }
    return {
        "scale_calibrated": False,
        "px_per_mm": None,
        "scale_source": "uncalibrated_archival_plate_no_scale_bar",
    }
