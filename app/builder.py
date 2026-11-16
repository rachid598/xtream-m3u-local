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

# Qualité déduite du nom de la chaîne (pour ne garder que la meilleure version d'un doublon).
_QUALITY = [
    (re.compile(r"(?<![a-z0-9])(4k|uhd|2160p?)(?![a-z0-9])"), 4.0),
    (re.compile(r"(?<![a-z0-9])(fhd|full ?hd|1080[pi]?)(?![a-z0-9])"), 3.0),
    (re.compile(r"(?<![a-z0-9])(hd|720p?)(?![a-z0-9])"), 2.0),
    (re.compile(r"(?<![a-z0-9])(sd|576[pi]?|480[pi]?)(?![a-z0-9])"), 1.0),
]
_TAGS = re.compile(r"(?<![a-z0-9])(4k|uhd|2160p?|fhd|full ?hd|1080[pi]?|hd|720p?|sd|576[pi]?|480[pi]?|hevc|"
                   r"h\.?26[45]|x26[45]|hdr|50 ?fps|60 ?fps|25 ?fps|backup|raw|multi|vip)(?![a-z0-9])")


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
    return entries, {prefix + cid: name for cid, name in categories.items()}


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
