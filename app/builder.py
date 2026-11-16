"""Construction d'une playlist personnalisée : sélection, règles, doublons, noms, numéros, favoris.

Partagé par les playlists M3U (``/p/<slug>.m3u``) et le serveur Xtream local (``player_api.php``)
pour que les deux présentent exactement la même sélection.
"""
from __future__ import annotations

import re
import unicodedata

from .custom import SELECTION_FIELDS, filter_kind

GROUP_PREFIX = {"live": "", "vod": "vod:", "series": "series:"}
FAVORITES_GROUP = "★"
CUSTOM_PREFIX = "g:"       # clé d'un groupe personnalisé du direct : « g:tnt »
KEEP_ORIGINAL = "-"        # item_groups[id] = "-" : la chaîne reste dans son groupe d'origine

# Qualité déduite du nom de la chaîne (pour ne garder que la meilleure version d'un doublon).
_QUALITY = [
    (re.compile(r"(?<![a-z0-9])(4k|uhd|2160p?)(?![a-z0-9])"), 4.0),
    (re.compile(r"(?<![a-z0-9])(fhd|full ?hd|1080[pi]?)(?![a-z0-9])"), 3.0),
    (re.compile(r"(?<![a-z0-9])(hd|720p?)(?![a-z0-9])"), 2.0),
    (re.compile(r"(?<![a-z0-9])(sd|576[pi]?|480[pi]?)(?![a-z0-9])"), 1.0),
]
_TAGS = re.compile(r"(?<![a-z0-9])(4k|uhd|2160p?|fhd|full ?hd|1080[pi]?|hd|720p?|sd|576[pi]?|480[pi]?|hevc|"
                   r"h\.?26[45]|x26[45]|hdr|50 ?fps|60 ?fps|25 ?fps|backup|raw|multi|vip)(?![a-z0-9])")


_SEPARATOR_EDGE = r"[#=*~_\-|•●◆★☆▬▭━─═\s]"
_SEPARATOR = re.compile(rf"^{_SEPARATOR_EDGE}*[#=*~_\-•●◆★☆▬▭━─═]{{3,}}.*[#=*~_\-•●◆★☆▬▭━─═]{{3,}}{_SEPARATOR_EDGE}*$")


def is_separator(name: str) -> bool:
    """Fausse chaîne servant de titre de section chez le fournisseur : « #### PRIME ####», « ===== FR ===== »…"""
    text = (name or "").strip()
    if not text:
        return True
    if _SEPARATOR.match(text):
        return True
    return not any(ch.isalnum() for ch in text)


# Préfixe de pays du fournisseur : « FR: », « FR | », « |FR| », « [FR] », « FR - ».
_COUNTRY = re.compile(r"^\W*[A-Za-z]{2}\s*[:|\-\]●•»◉]+\s*")


def match_name(name: str) -> str:
    """Nom comparable pour les groupes personnalisés : sans pays, accents, ponctuation ni qualité.

    « FR: TF1 FHD » → « tf1 » ; « |FR| CANAL + 4K » → « canal+ » ; « L'Équipe HD » → « l equipe ».
    """
    text = _COUNTRY.sub("", name or "", count=1)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^\w+&]+|_", " ", text)
    text = re.sub(r"\s+\+", "+", text)
    tokens = text.split()
    if len(tokens) > 1 and tokens[0] in ("fr", "fra"):
        tokens = tokens[1:]
    return " ".join(_TAGS.sub(" ", " ".join(tokens)).split())


_NUMBERED = re.compile(r"^\s*(\d{1,5})\s*[:=]\s*(.+)$")


def split_pattern(text: str) -> tuple[int | None, str]:
    """« 4: Canal+ » → (4, « Canal+ ») : numéro de chaîne optionnel (comme sur un décodeur)."""
    found = _NUMBERED.match(text or "")
    if found and 1 <= int(found.group(1)) <= 99_999:
        return int(found.group(1)), found.group(2)
    return None, text or ""


def group_matches(core: str, group: dict) -> int | None:
    """Rang du premier motif du groupe qui correspond au nom (déjà passé par :func:`match_name`)."""
    padded = f" {core} "
    for rank, pattern in enumerate(group.get("_patterns") or ()):
        if (core == pattern) if group.get("match") == "exact" else (f" {pattern} " in padded):
            return rank
    return None


def ordered_custom_groups(saved: dict) -> list[dict]:
    """Groupes personnalisés par priorité : ordre des groupes de la playlist, puis ordre de création."""
    groups = saved.get("custom_groups") or []
    rank = {key: i for i, key in enumerate(saved.get("group_order") or [])}
    ordered = sorted(enumerate(groups), key=lambda e: (rank.get(CUSTOM_PREFIX + e[1]["id"], len(rank)), e[0]))
    result = []
    for _, group in ordered:
        cores: dict[str, int | None] = {}
        for value in group.get("patterns") or []:
            number, text = split_pattern(value)
            core = match_name(text)
            if core and core not in cores:
                cores[core] = number
        result.append({**group, "_patterns": list(cores), "_numbers": list(cores.values())})
    return result


def apply_custom_groups(entries: list[tuple[dict, str]], saved: dict) -> list[tuple[dict, str]]:
    """Range les chaînes retenues dans les groupes personnalisés (« TNT », « Canal+ »…).

    Une chaîne rangée à la main (``item_groups``) va dans ce groupe ; sinon dans le premier groupe
    (le plus haut) dont un motif correspond à son nom. Dans un groupe : ordre des motifs, puis
    rangements manuels, puis ordre du fournisseur. Les chaînes déplacées quittent leur groupe d'origine.
    Un nom numéroté (« 4: Canal+ ») donne ce numéro à la chaîne, sauf numéro personnalisé (✎).
    """
    groups = ordered_custom_groups(saved)
    if not groups:
        return entries
    known = {g["id"] for g in groups}
    manual = saved.get("item_groups") or {}
    placed: list[tuple[int, int, int, tuple[dict, str]]] = []
    rest: list[tuple[dict, str]] = []
    priority = {g["id"]: i for i, g in enumerate(groups)}
    for index, (item, group) in enumerate(entries):
        target = manual.get(item["stream_id"])
        if target == KEEP_ORIGINAL:
            rest.append((item, group))
            continue
        if target in known:
            placed.append((priority[target], 10_000, index, (item, CUSTOM_PREFIX + target)))
            continue
        core = match_name(item["name"])
        for g in groups:
            rank = group_matches(core, g) if g["_patterns"] else None
            if rank is not None:
                number = g["_numbers"][rank]
                if number is not None:
                    item = {**item, "num": str(number), "custom_num": True}
                placed.append((priority[g["id"]], rank, index, (item, CUSTOM_PREFIX + g["id"])))
                break
        else:
            rest.append((item, group))
    placed.sort(key=lambda e: e[:3])
    return [entry for *_, entry in placed] + rest


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().split())


def quality(name: str) -> float:
    lowered = normalize(name)
    for pattern, score in _QUALITY:
        if pattern.search(lowered):
            return score
    return 1.5  # sans mention : entre SD et HD


def base_name(name: str) -> str:
    """Nom sans mention de qualité ni ponctuation : « TF1 HD », « TF1 (FHD) » → « tf1 »."""
    text = _TAGS.sub(" ", normalize(name))
    text = re.sub(r"[|()\[\]{}:._\-+*/\\]+", " ", text)
    return " ".join(text.split())


def rule_matches(category_name: str, rule: dict) -> bool:
    name, value = normalize(category_name), normalize(rule["value"])
    if rule["match"] == "prefix":
        return name.startswith(value)
    if rule["match"] == "exact":
        return name == value
    return value in name


def dedupe(entries: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """Dans chaque groupe, garde la meilleure qualité d'une même chaîne (SD/HD/FHD/4K…)."""
    best: dict[tuple[str, str], tuple[float, int]] = {}
    for index, (item, group) in enumerate(entries):
        key = (group, base_name(item["name"]))
        score = quality(item["name"])
        if key not in best or score > best[key][0]:
            best[key] = (score, index)
    keep = {index for _, index in best.values()}
    return [entry for index, entry in enumerate(entries) if index in keep]


def item_key(item: dict) -> str:
    kind = item.get("kind", "live")
    if kind == "movie":
        return "vod:" + item["stream_id"]
    if kind == "series":
        return "series:" + item["stream_id"]
    if kind == "episode":
        return "series:" + item.get("series_id", "")
    return item["stream_id"]


def select_items(api, saved: dict, kind: str, *, refresh: bool = False) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Éléments d'un type (direct, films ou séries) retenus par la playlist, personnalisations comprises.

    Retourne ``(entrées [(élément, clé_de_groupe)], {clé_de_groupe: nom d'origine})``.
    """
    items, categories = api.catalog(refresh=refresh, kind=kind)
    prefix = GROUP_PREFIX[kind]
    cats_field = SELECTION_FIELDS[kind][0]
    effective = saved
    rules = [r for r in saved.get("rules", []) if r["kind"] == kind]
    if rules:
        extra = [cid for cid, name in categories.items() if any(rule_matches(name, r) for r in rules)]
        effective = {**saved, cats_field: list(dict.fromkeys(list(saved.get(cats_field, [])) + extra))}
    entries = filter_kind(items, effective, kind, prefix)
    if kind == "live" and saved.get("hide_separators"):
        entries = [entry for entry in entries if not is_separator(entry[0]["name"])]
    if kind == "live":
        entries = apply_custom_groups(entries, saved)
    if kind == "live" and saved.get("dedupe"):
        entries = dedupe(entries)
    names = saved.get("item_names") or {}
    numbers = saved.get("item_numbers") or {} if kind == "live" else {}
    if names or numbers:
        customized = []
        for item, group in entries:
            key = prefix + item["stream_id"]
            if key in names or item["stream_id"] in numbers:
                item = dict(item)
                if key in names:
                    item["name"] = names[key]
                if item["stream_id"] in numbers:
                    item["num"] = str(numbers[item["stream_id"]])
                    item["custom_num"] = True
            customized.append((item, group))
        entries = customized
    groups = {prefix + cid: name for cid, name in categories.items()}
    if kind == "live" and saved.get("custom_groups"):
        # Groupes personnalisés en premier (ordre par défaut, avant ceux du fournisseur).
        groups = {**{CUSTOM_PREFIX + g["id"]: g["name"] for g in ordered_custom_groups(saved)}, **groups}
    return entries, groups


def add_favorites(entries: list[tuple[dict, str]], groups: dict[str, str], saved: dict) -> list[tuple[dict, str]]:
    """Ajoute en tête un groupe « ★ Favoris » (les favoris restent aussi dans leur groupe)."""
    favorites = saved.get("favorites") or []
    if not favorites:
        return entries
    rank = {key: i for i, key in enumerate(favorites)}
    fav = [(item, FAVORITES_GROUP) for item, _ in entries if item_key(item) in rank]
    fav.sort(key=lambda entry: rank[item_key(entry[0])])  # tri stable : épisodes dans l'ordre
    if fav:
        groups[FAVORITES_GROUP] = saved.get("favorites_name") or "★ Favoris"
    return fav + entries
