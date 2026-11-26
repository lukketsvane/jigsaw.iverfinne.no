"""Taxonomy loader, multilingual Nordic rock-art motif classifier, and license validator.

Strict honesty rules:
  - Enforces full word boundaries on non-compound keywords so substrings never false-match
    (e.g. 'bear' never matches 'shield_bearer'/'spear_bearer'/'axe_bearer';
     'ski' never matches 'Skien'/'skipsfigurer'/'skisse'/'skinnbåt';
     'and' never matches 'Oppland'/'Nordland'/'Nordre Land'/'Vestland';
     'fisk' never matches 'topografisk'; 'tegn' never matches 'tegning';
     'sun' never matches 'Sundløkken'; 'mann' never matches 'Amtmannsnes';
     'bjørn' never matches 'Bjørnstadskipet').
  - Parses authoritative SHFA `Motifs:` tags directly from description rather than noisy
    site-level harvester hints.
  - Ignores generic regional/harvester boilerplate hints on Alta (`altarockart`) and
    Norwegian museum archives (`unimus`, `kulturminnesok`, `other`), classifying strictly
    from explicit archaeological text (plus verified red-deer-dominant Vingen tracings).
  - Never fabricates specific animal/vessel labels on untitled multi-motif panels.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parent.parent / "taxonomy.json"

# Strictly non-commercial or unclear license patterns
_NON_COMMERCIAL_PATTERNS = re.compile(
    r"(?i)(?:\bnc\b|non[\s\-]?commercial|by[\s\-]nc|unknown|all\s+rights\s+reserved|"
    r"copyrighted|restricted|in\s+copyright|fair\s+use|\bnd\b|no[\s\-]?deriv|arr\b|unspecified)"
)

_COMMERCIAL_ALLOWED_PATTERNS = re.compile(
    r"(?i)(?:cc0|public\s*domain|\bpd\b|\bpdm\b|cc[\s\-]by(?:[\s\-]sa)?(?:\s*[\d\.]+)?|"
    r"nlod|open\s*data|mit\b|bsd\b|apache|gfdl|free\s*art|fal\b|odbl|ogl)"
)

# Priority order when resolving ties between specific classes
_SPECIFIC_PRIORITY = [
    "skier",
    "lur_blower",
    "chariot_plough",
    "reindeer",
    "elk_moose",
    "whale_cetacean",
    "bear",
    "fish",
    "bird",
    "horse",
    "dog_wolf",
    "snake",
    "sun_wheel",
    "footprint",
    "handprint",
    "axe_weapon",
    "tree",
    "rider",
    "acrobat_dancer",
    "hunter_warrior",
    "bronze_age_ship",
    "stone_age_boat",
    "quadruped_other",
    "anthropomorph",
    "geometric_spiral",
    "multi_motif_panel",
    "unclassified_motif",
]

# Strictly unambiguous compound stems safe for substring matching inside Nordic compound words
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

# Keywords from taxonomy.json that are site names or dangerous ambiguous words (never match directly from text)
_BLOCKED_TEXT_KEYWORDS = {
    # Site/place names
    "alta", "vingen", "bøla", "bola", "rødøy", "rodoy", "bardal", "leiknes",
    "begby", "solberg", "kalnes", "østfold", "ostfold", "rogaland", "austre åmøy",
    "amoy", "tanum", "vitlycke", "bohuslän", "bohuslan", "nämforsen", "namforsen",
    "næmforsen", "aspeberget", "fossum", "litsleby", "himmelstalund", "simrishamn",
    "simris", "kivik", "madsebakke", "bornholm", "skavberget", "møllerstufossen",
    "forselv", "kåfjord", "strand", "drammen", "skjeberg", "alstahaug", "zalavruga",
    "kalleby", "tegneby", "frännarp", "frannarp", "finntorp", "sotts",
    # Ambiguous generic / archival / cross-language collision words
    "bronze", "bronse", "veideristning", "figur", "figure",
    "and",   # English conjunction + Norwegian duck + suffix of Oppland/Nordland/Land
    "ski",   # Prefix of Skien, skipsfigurer, skisse, skinnbåt
    "sun",   # Collides with Sundløkken, Sundvollen
    "sol",   # Collides with Solberg, Solbakk, Solem
    "tegn",  # Prefix of tegning (drawing)
    "man",   # Generic / English "Early Man" / Amtmannsnes
    "mann",  # Substring of Amtmannsnes / place names
    "hand",  # Collides with overhandling / handling
    "tre",   # Norwegian for "three" / wood
    "horn",  # Collides with Hornes
    "hi",    # Collides with short words
    "den",   # Scandinavian definite article ("the")
    "ko",    # Too short / ambiguous
    "ku",    # Too short / ambiguous
    "ren",   # Norwegian/Swedish for "clean/pure"
    "val",   # Swedish for "choice/election" / place names (Valla, Valdersrud)
    "bat",   # English bat
    "ring",  # Generic
    "circle", "sirkel", "cirkel", "net", "nett", "grid", "frame", "ramme",
    "skålgrop", "skalgrop", "cupmark", "cup mark", "cup-mark", "skålgropar", "älvkvarn",
    "abstract", "symbol", "ornament", "drum", "tromme", "foot", "shoe", "sko", "sandal", "sole", "såle",
    "wheel", "hjul", "branch", "gren", "plant", "leaf", "løv", "gran", "spruce",
    "jump", "leap", "ritual", "wedding", "bride", "battle", "kamp", "strid", "corral",
}

# Extra unambiguous inflected/Nordic keywords supplementing taxonomy.json
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
    "bronze_age_ship": [
        "ships", "skipsfigurer", "skipsfigur", "hällristningsskepp", "helleristningsskip",
        "bronsealderskip", "bjørnstadskipet", "bjørnstadskip", "långskepp", "skeppsbild",
        "brandskogschiff", "båtmotiv", "skip1", "skip2", "skip", "skipet", "skepp", "skeppet", "skib",
    ],
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

# Authoritative SHFA `Motifs:` tag -> taxonomy class mapping (None = non-figurative/ignore)
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
    # Non-figurative / archival metadata tags (explicitly ignored)
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

_SHFA_MOTIFS_RE = re.compile(r"Motifs:\s*([^\n]+)")

# Site/place names to strip from text before keyword scoring so they never trigger false fauna/symbol hits
_PLACE_STRIP_RE = re.compile(
    r"(?i)\b(?:amtmannsnes\w*|bjørnstad(?!skip)\w*|sundløkken\w*|sundvollen\w*|"
    r"solberg\w*|solbakk\w*|hornes\w*|skien\w*|oppland\w*|nordland\w*|nordre\s+land|"
    r"vestland\w*|hordaland\w*|gretteland\w*|småland\w*|södermanland\w*|västmanland\w*|"
    r"rogaland\w*|sjælland\w*|valla\w*|valdersrud\w*|etnedal\w*|topografisk\w*|tegning\w*)\b"
)


def load_taxonomy(path: Path = DEFAULT_TAXONOMY_PATH) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_class_index(taxonomy: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return mapping motif_class -> {family, keywords, typical_period, ...}."""
    index: Dict[str, Dict[str, Any]] = {}
    for family_id, fam_data in taxonomy.get("families", {}).items():
        for class_id, cls_data in fam_data.get("classes", {}).items():
            entry = dict(cls_data)
            entry["family"] = family_id
            entry["motif_class"] = class_id
            # Merge extra unambiguous keywords while preserving order
            seen = set(k.lower().strip() for k in entry.get("keywords", []))
            merged = list(entry.get("keywords", []))
            for extra in _EXTRA_CLASS_KEYWORDS.get(class_id, []):
                if extra.lower().strip() not in seen:
                    merged.append(extra)
                    seen.add(extra.lower().strip())
            entry["keywords"] = merged
            index[class_id] = entry
    return index


def validate_commercial_ok(license_str: Optional[str], manifest_flag: Any = None) -> bool:
    """Strictly verify whether a license permits commercial reuse.

    Never returns True for NC, ND, Unknown, empty, or restricted licenses.
    Never overrides an explicit manifest_flag=False to True.
    """
    if manifest_flag is False:
        return False
    if not license_str or not isinstance(license_str, str):
        return False
    cleaned = license_str.strip()
    if not cleaned:
        return False
    if _NON_COMMERCIAL_PATTERNS.search(cleaned):
        return False
    if _COMMERCIAL_ALLOWED_PATTERNS.search(cleaned):
        return True
    return False


def _word_boundary_match(keyword: str, text: str) -> bool:
    kw = keyword.lower().strip()
    if not kw:
        return False
    # Safe unambiguous Scandinavian compound stems may appear inside compound nouns
    if kw in _SAFE_COMPOUND_ROOTS:
        return kw in text
    # ALL other keywords require STRICT leading AND trailing word boundaries!
    return bool(re.search(rf"(?<![a-zæøåäö0-9]){re.escape(kw)}(?![a-zæøåäö0-9])", text))


def _clean_text_for_scoring(text: str) -> str:
    low = (text or "").lower()
    # Replace underscores with spaces ONLY for natural-language matching (keep exact tokens checked separately)
    low = _PLACE_STRIP_RE.sub(" ", low)
    return low


def _score_text_classes(
    text: str,
    class_index: Dict[str, Dict[str, Any]],
) -> Dict[str, float]:
    text_l = _clean_text_for_scoring(text)
    scores: Dict[str, float] = {}
    for cls_id, info in class_index.items():
        if cls_id in ("unclassified_motif", "multi_motif_panel"):
            continue
        score = 0.0
        for kw in info.get("keywords", []):
            kw_l = kw.lower().strip()
            if kw_l in _BLOCKED_TEXT_KEYWORDS:
                continue
            if _word_boundary_match(kw_l, text_l):
                score += 2.0 if len(kw_l) >= 5 else 1.2
        if score > 0:
            scores[cls_id] = score
    return scores


_STONE_AGE_SHFA_SITES = (
    "nämforsen", "namforsen", "vingen", "vingeneset", "bak vehammaren", "bakkane",
    "urane", "vindbakken", "teigen", "leitet", "storåkeren", "ved vatnet",
    "lyngrabben", "brattebakken", "alta", "bardal", "evenhus", "leiknes", "rødøy",
)


def _extract_shfa_classes(description: str, period: str, site: str = "") -> Optional[Set[str]]:
    """Parse curated SHFA `Motifs: ...` field into canonical taxonomy classes.

    Returns None if no `Motifs:` field exists in description; otherwise returns the
    (possibly empty) set of figurative taxonomy classes explicitly recorded by SHFA.
    """
    m = _SHFA_MOTIFS_RE.search(description or "")
    if not m:
        return None
    site_l = (site or "").lower()
    is_stone_age_site = any(tok in site_l for tok in _STONE_AGE_SHFA_SITES)
    raw_tags = [t.strip().rstrip(".").lower() for t in m.group(1).split(",") if t.strip()]
    classes: Set[str] = set()
    for tag in raw_tags:
        if tag.startswith("ship (boat)") or tag.startswith("ship or boat") or tag == "sun ship":
            cls = "stone_age_boat" if is_stone_age_site else "bronze_age_ship"
            classes.add(cls)
            continue
        if tag in _SHFA_TAG_TO_CLASS:
            mapped = _SHFA_TAG_TO_CLASS[tag]
            if mapped:
                if mapped == "bronze_age_ship" and is_stone_age_site:
                    mapped = "stone_age_boat"
                classes.add(mapped)
    return classes


def _shape_class_affinity(cls_id: str, metrics: Optional[Dict[str, Any]], candidates: Optional[Set[str]] = None) -> float:
    """Strict, conservative shape-geometry affinity for disambiguating multi-motif panels.

    Only returns strong positive scores when geometry is unmistakably distinct from
    competing candidates on the same panel.
    """
    if not metrics:
        return 0.0
    ar = float(metrics.get("aspect_ratio", 1.0))  # width / height
    solidity = float(metrics.get("solidity", 0.5))
    circularity = float(metrics.get("circularity", metrics.get("compactness", 0.3)))
    render_style = str(metrics.get("render_style", ""))
    cands = candidates or set()
    has_animals = bool(cands & {"reindeer", "elk_moose", "bear", "whale_cetacean", "fish", "bird", "horse", "dog_wolf", "quadruped_other"})
    has_vessels = bool(cands & {"bronze_age_ship", "stone_age_boat"})

    bonus = 0.0
    if cls_id in ("bronze_age_ship", "stone_age_boat"):
        if has_animals:
            # Require clearly elongated horizontal hull to separate ships from quadrupeds!
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
        # Never confuse wide Bronze Age ships with fish on mixed ship+fish panels
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
    elif cls_id == "tree":
        if ar <= 0.85 and render_style == "linear_motif":
            bonus += 0.8
    elif cls_id == "chariot_plough":
        if 1.15 <= ar <= 2.4:
            bonus += 0.6
    return bonus


def _disambiguate_multi_class(
    candidate_scores: Dict[str, float],
    class_index: Dict[str, Dict[str, Any]],
    shape_metrics: Optional[Dict[str, Any]],
) -> Tuple[str, str, float]:
    """Conservative multi-class panel disambiguation.

    Never picks between two classes with overlapping geometry (e.g. horse vs deer,
    or reindeer vs bear). Only assigns a label when the single best candidate (or a
    unanimous human-family tie) has strong positive shape affinity AND a wide
    separation from every non-tied competing candidate.
    """
    if not candidate_scores:
        return "scene_or_unclassified", "unclassified_motif", 0.25

    cand_set = set(candidate_scores.keys())

    # Special clean case: specific animal + generic 'quadruped_other' ONLY (e.g. SHFA 'Moose, Unspecified animal')
    specific_animals = cand_set & {"reindeer", "elk_moose", "bear", "whale_cetacean", "fish", "bird", "horse", "dog_wolf", "snake"}
    if len(cand_set) == 2 and "quadruped_other" in cand_set and len(specific_animals) == 1:
        only_sp = next(iter(specific_animals))
        return class_index[only_sp]["family"], only_sp, 0.72

    if not shape_metrics:
        return "scene_or_unclassified", "unclassified_motif", 0.30

    ranked = []
    for cls_id, sc in candidate_scores.items():
        aff = _shape_class_affinity(cls_id, shape_metrics, candidates=cand_set)
        ranked.append((aff, sc, cls_id))
    ranked.sort(reverse=True)

    top_aff = ranked[0][0]
    if top_aff < 1.2:
        return "scene_or_unclassified", "unclassified_motif", 0.30

    # Single clear winner
    if len(ranked) == 1 or (top_aff - ranked[1][0]) >= 0.9:
        best_cls = ranked[0][2]
        return class_index[best_cls]["family"], best_cls, 0.64

    # Clean human-family tie (e.g. panel has both 'Human or Anthropomorph' and 'Adorant'/'Axe bearer'):
    # if ALL candidates within 0.9 of top_aff are in family 'human', assign honest parent 'anthropomorph'!
    close_top = [c for aff, _, c in ranked if (top_aff - aff) < 0.9]
    if close_top and all(class_index[c]["family"] == "human" for c in close_top):
        return "human", "anthropomorph", 0.62

    return "scene_or_unclassified", "unclassified_motif", 0.30


def classify_motif(
    record: Dict[str, Any],
    class_index: Dict[str, Dict[str, Any]],
    num_extracted_from_source: int = 1,
    shape_metrics: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str, float]:
    """Classify an extracted motif into (family, motif_class, confidence).

    Archive-aware & strictly honest:
      - SHFA: uses curated `Motifs:` tags directly.
      - Alta (`altarockart`): ignores generic regional boilerplate hints; uses real Norwegian title/desc only.
      - Unimus / Kulturminnesøk / Other: ignores substring-bugged harvester hints; uses real text
        (plus verified red-deer-dominant Vingen tracings).
      - Wikimedia / Europeana / KSamsök / Nationalmuseet / synthetic: uses text + validated hints.
    """
    archive = str(record.get("source_archive") or "").lower().strip()
    hints = record.get("motif_hints") or []
    if isinstance(hints, str):
        hints = [hints]
    title = str(record.get("title") or "")
    desc = str(record.get("description") or "")
    site = str(record.get("site") or "")
    raw_name = Path(str(record.get("raw_path") or "")).stem.replace("_", " ").replace("-", " ")
    period = str(record.get("period") or "unknown").lower()

    # --- 1. SHFA: authoritative curated `Motifs:` metadata ---
    if archive == "shfa":
        shfa_classes = _extract_shfa_classes(desc, period, site=site)
        if shfa_classes is not None:
            if not shfa_classes:
                return "scene_or_unclassified", "unclassified_motif", 0.25
            if len(shfa_classes) == 1:
                only_cls = next(iter(shfa_classes))
                conf = 0.88 if num_extracted_from_source <= 1 else 0.78
                return class_index[only_cls]["family"], only_cls, conf
            cand_scores = {c: 3.0 for c in shfa_classes if c in class_index}
            return _disambiguate_multi_class(cand_scores, class_index, shape_metrics)

    # --- 2. Alta Rock Art Archive: ignore generic harvester boilerplate hints ---
    if archive == "altarockart":
        title_scores = _score_text_classes(title, class_index)
        # Only score non-boilerplate descriptions (skip "Digital rock-art tracing (kalkering) from Alta...")
        desc_clean = "" if desc.lower().startswith("digital rock-art tracing") else desc
        desc_scores = _score_text_classes(desc_clean, class_index)
        combined: Dict[str, float] = {}
        for c in set(title_scores) | set(desc_scores):
            combined[c] = title_scores.get(c, 0.0) * 1.4 + desc_scores.get(c, 0.0) * 0.8
        if "bronze_age_ship" in combined and "stone_age_boat" in combined:
            combined["stone_age_boat"] += 2.0
        if not combined:
            return "scene_or_unclassified", "unclassified_motif", 0.25
        if len(combined) == 1:
            only_cls = next(iter(combined))
            return class_index[only_cls]["family"], only_cls, 0.82 if num_extracted_from_source <= 1 else 0.75
        return _disambiguate_multi_class(combined, class_index, shape_metrics)

    # --- 3. Norwegian archives with substring-bugged harvester hints (unimus, kulturminnesok, other) ---
    if archive in ("unimus", "kulturminnesok", "other"):
        title_scores = _score_text_classes(title, class_index)
        desc_scores = _score_text_classes(desc, class_index)
        combined = {}
        for c in set(title_scores) | set(desc_scores):
            combined[c] = title_scores.get(c, 0.0) * 1.4 + desc_scores.get(c, 0.0) * 0.8

        # Disambiguate ship vs boat by period
        if "bronze_age_ship" in combined and "stone_age_boat" in combined:
            if "stone_age" in period:
                combined.pop("bronze_age_ship", None)
            else:
                combined.pop("stone_age_boat", None)
        elif "bronze_age_ship" in combined and "stone_age" in period and "bronse" not in f"{title} {desc}".lower():
            combined["stone_age_boat"] = combined.pop("bronze_age_ship")

        if combined:
            if len(combined) == 1:
                only_cls = next(iter(combined))
                return class_index[only_cls]["family"], only_cls, 0.85 if num_extracted_from_source <= 1 else 0.78
            return _disambiguate_multi_class(combined, class_index, shape_metrics)

        # Verified archaeological site homogeneity: Vingen / Vingelva tracings are >90% red deer (quadruped_other)
        site_l = site.lower()
        if archive == "unimus" and (site_l.startswith("vingen") or site_l.startswith("vingelva")):
            ar = float((shape_metrics or {}).get("aspect_ratio", 1.2))
            if 0.72 <= ar <= 2.40:
                return "animal", "quadruped_other", 0.70

        return "scene_or_unclassified", "unclassified_motif", 0.25

    # --- 4. Wikimedia / Europeana / KSamsök / Nationalmuseet / Synthetic test records ---
    direct_matches: List[str] = []
    safe_hint_tokens: List[str] = []
    for h in hints:
        h_norm = str(h).strip().lower().replace(" ", "_").replace("-", "_")
        if h_norm in ("unclassified_motif", "multi_motif_panel", "rock_carving", "cup_mark", "circle"):
            continue
        if h_norm in class_index:
            direct_matches.append(h_norm)
        else:
            safe_hint_tokens.append(h_norm)

    hint_scores = _score_text_classes(" ".join(safe_hint_tokens), class_index)
    for dm in direct_matches:
        hint_scores[dm] = hint_scores.get(dm, 0.0) + 5.0
    title_scores = _score_text_classes(f"{title} {raw_name}", class_index)
    desc_scores = _score_text_classes(desc, class_index)

    combined = {}
    for cls_id in set(hint_scores) | set(title_scores) | set(desc_scores):
        combined[cls_id] = (
            hint_scores.get(cls_id, 0.0) * 1.5
            + title_scores.get(cls_id, 0.0) * 1.2
            + desc_scores.get(cls_id, 0.0) * 0.6
        )

    if "bronze_age_ship" in combined and "stone_age_boat" in combined:
        if "stone_age" in period:
            combined["stone_age_boat"] += 2.0
            if len(combined) == 2:
                combined.pop("bronze_age_ship", None)
        elif "bronze_age" in period:
            combined["bronze_age_ship"] += 2.0
            if len(combined) == 2:
                combined.pop("stone_age_boat", None)
    elif "stone_age_boat" in combined and "bronze_age" in period:
        combined["bronze_age_ship"] = combined.pop("stone_age_boat")

    if num_extracted_from_source <= 1:
        if combined:
            ranked = []
            for cls_id, sc in combined.items():
                bonus = _shape_class_affinity(cls_id, shape_metrics, set(combined.keys())) * 0.35
                if period != "unknown" and class_index[cls_id].get("typical_period") == period:
                    bonus += 0.4
                prio = -_SPECIFIC_PRIORITY.index(cls_id) if cls_id in _SPECIFIC_PRIORITY else -99
                ranked.append((sc + bonus, prio, cls_id))
            ranked.sort(reverse=True)
            best_cls = ranked[0][2]
            conf = min(0.95, 0.55 + 0.08 * min(ranked[0][0], 5.0))
            return class_index[best_cls]["family"], best_cls, round(conf, 3)

        full_text = f"{title} {desc} {raw_name}".lower()
        if any(w in full_text for w in ("panel", "scene", "felt", "komposisjon", "composition")):
            return "scene_or_unclassified", "multi_motif_panel", 0.50
        return "scene_or_unclassified", "unclassified_motif", 0.25

    # Multi-motif panel
    if not combined:
        return "scene_or_unclassified", "unclassified_motif", 0.25

    significant = {c: sc for c, sc in combined.items() if sc >= 1.5}
    if len(significant) == 1:
        only_cls = next(iter(significant))
        conf = min(0.86, 0.55 + 0.07 * min(significant[only_cls], 4.0))
        return class_index[only_cls]["family"], only_cls, round(conf, 3)

    return _disambiguate_multi_class(significant or combined, class_index, shape_metrics)
