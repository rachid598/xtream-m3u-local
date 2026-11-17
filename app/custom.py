"""Durable named M3U playlist selection storage (no provider secrets)."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

LOG = logging.getLogger("xtream-m3u-local.store")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
# Les identifiants de catégories Xtream sont numériques, mais on tolère des
# jetons simples (et la valeur spéciale ``none`` = « Sans catégorie »).
CATEGORY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
UNCATEGORIZED = "none"
MAX_PLAYLISTS = 100
MAX_IDS = 100_000
MAX_GROUPS = 5_000
# Champs de sélection par type de contenu (le direct garde les noms de la V2).
SELECTION_FIELDS = {
    "live": ("category_ids", "included_stream_ids", "excluded_stream_ids"),
    "vod": ("vod_category_ids", "included_vod_ids", "excluded_vod_ids"),
    "series": ("series_category_ids", "included_series_ids", "excluded_series_ids"),
}


class InvalidPlaylist(ValueError):
    pass


def _ids(value: object, *, pattern: re.Pattern[str] | None = None, label: str = "chaîne") -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_IDS:
        raise InvalidPlaylist("Les sélections doivent être des listes (100 000 éléments maximum)")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise InvalidPlaylist(f"Identifiant de {label} invalide")
        normalized = str(item).strip()
        valid = pattern.fullmatch(normalized) if pattern else normalized.isdigit()
        if not valid:
            raise InvalidPlaylist(f"Identifiant de {label} invalide")
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _group_names(value: object) -> dict[str, str]:
    """Noms de groupes personnalisés ``{category_id: nom}`` (le catalogue d'origine n'est pas modifié)."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_GROUPS:
        raise InvalidPlaylist(f"Noms de groupes : objet attendu ({MAX_GROUPS} maximum)")
    result: dict[str, str] = {}
    for key, name in value.items():
        if not isinstance(key, str) or not CATEGORY_RE.fullmatch(key):
            raise InvalidPlaylist("Identifiant de groupe invalide")
        if not isinstance(name, str) or any(ch in name for ch in "\r\n\t\x00"):
            raise InvalidPlaylist("Nom de groupe invalide")
        name = name.strip()
        if len(name) > 100:
            raise InvalidPlaylist("Nom de groupe trop long (100 caractères maximum)")
        if name:
            result[key] = name
    return result


ITEM_KEY_RE = re.compile(r"^(?:vod:|series:)?[0-9]{1,12}$")
RULE_MATCHES = ("prefix", "contains", "exact")
MAX_RULES = 50
MAX_ITEMS_CUSTOM = 20_000


def _text(value: object, label: str, max_len: int = 100) -> str:
    if not isinstance(value, str) or any(ch in value for ch in "\r\n\t\x00"):
        raise InvalidPlaylist(f"{label} invalide")
    value = value.strip()
    if len(value) > max_len:
        raise InvalidPlaylist(f"{label} trop long ({max_len} caractères maximum)")
    return value


def _item_names(value: object) -> dict[str, str]:
    """Noms personnalisés ``{clé: nom}`` (clé = id du direct, ``vod:id`` ou ``series:id``)."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_ITEMS_CUSTOM:
        raise InvalidPlaylist("Noms personnalisés : objet attendu")
    result = {}
    for key, name in value.items():
        if not isinstance(key, str) or not ITEM_KEY_RE.fullmatch(key):
            raise InvalidPlaylist("Identifiant d'élément invalide")
        name = _text(name, "Nom personnalisé")
        if name:
            result[key] = name
    return result


LOGO_RE = re.compile(r"^https?://[^\s\"'<>]{3,490}$")


def _item_logos(value: object) -> dict[str, str]:
    """Logos de remplacement ``{clé: URL http(s)}`` (clé = id du direct, ``vod:id`` ou ``series:id``)."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_ITEMS_CUSTOM:
        raise InvalidPlaylist("Logos : objet attendu")
    result = {}
    for key, url in value.items():
        if not isinstance(key, str) or not ITEM_KEY_RE.fullmatch(key):
            raise InvalidPlaylist("Identifiant d'élément invalide (logos)")
        if not isinstance(url, str) or not LOGO_RE.fullmatch(url.strip()):
            raise InvalidPlaylist("Logo : adresse http(s) d'une image attendue")
        result[key] = url.strip()
    return result


def _item_epg(value: object) -> dict[str, str]:
    """Guide emprunté ``{id_du_direct: id_de_la_chaîne_dont_on_prend_le_guide}``."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_ITEMS_CUSTOM:
        raise InvalidPlaylist("Guides empruntés : objet attendu")
    result = {}
    for key, source in value.items():
        source = str(source).strip() if isinstance(source, (str, int)) and not isinstance(source, bool) else ""
        if not isinstance(key, str) or not key.isdigit() or len(key) > 12 or not source.isdigit() or len(source) > 12:
            raise InvalidPlaylist("Guide emprunté : identifiants de chaînes attendus")
        if source != key:
            result[key] = source
    return result


def _item_numbers(value: object) -> dict[str, int]:
    """Numéros de chaînes ``{id_du_direct: numéro}`` (1 à 99 999) ; servent aussi à l'ordre."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_ITEMS_CUSTOM:
        raise InvalidPlaylist("Numéros de chaînes : objet attendu")
    result = {}
    for key, number in value.items():
        if not isinstance(key, str) or not key.isdigit() or len(key) > 12:
            raise InvalidPlaylist("Identifiant de chaîne invalide (numéros)")
        if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 99_999:
            raise InvalidPlaylist("Numéro de chaîne : entier de 1 à 99 999")
        result[key] = number
    return result


def _rules(value: object) -> list[dict]:
    """Règles automatiques : catégories dont le nom correspond, ajoutées à la sélection."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_RULES:
        raise InvalidPlaylist(f"Règles : liste attendue ({MAX_RULES} maximum)")
    result = []
    for rule in value:
        if not isinstance(rule, dict) or rule.get("kind") not in SELECTION_FIELDS or rule.get("match") not in RULE_MATCHES:
            raise InvalidPlaylist("Règle invalide (type de contenu ou condition)")
        text = _text(rule.get("value"), "Texte de la règle")
        if not text:
            raise InvalidPlaylist("Texte de la règle obligatoire")
        result.append({"kind": rule["kind"], "match": rule["match"], "value": text})
    return result


GROUP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
GROUP_MATCHES = ("words", "exact")
MAX_CUSTOM_GROUPS = 50
MAX_PATTERNS = 300


def _custom_groups(value: object) -> list[dict]:
    """Groupes personnalisés du direct : ``[{id, name, match, patterns}]`` (« TNT », « Canal+ »…)."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_CUSTOM_GROUPS:
        raise InvalidPlaylist(f"Groupes personnalisés : liste attendue ({MAX_CUSTOM_GROUPS} maximum)")
    result, seen = [], set()
    for group in value:
        if not isinstance(group, dict):
            raise InvalidPlaylist("Groupe personnalisé invalide")
        gid = group.get("id")
        if not isinstance(gid, str) or not GROUP_ID_RE.fullmatch(gid) or gid in seen:
            raise InvalidPlaylist("Identifiant de groupe personnalisé invalide ou en double")
        seen.add(gid)
        name = _text(group.get("name"), "Nom du groupe personnalisé")
        if not name:
            raise InvalidPlaylist("Nom du groupe personnalisé obligatoire")
        match = group.get("match", "words")
        if match not in GROUP_MATCHES:
            raise InvalidPlaylist("Groupe personnalisé : correspondance « words » ou « exact »")
        patterns = group.get("patterns") or []
        if not isinstance(patterns, list) or len(patterns) > MAX_PATTERNS:
            raise InvalidPlaylist(f"Groupe personnalisé : {MAX_PATTERNS} noms de chaînes maximum")
        cleaned = [p for p in (_text(p, "Nom de chaîne du groupe") for p in patterns) if p]
        result.append({"id": gid, "name": name, "match": match, "patterns": list(dict.fromkeys(cleaned))})
    return result


def _item_groups(value: object, groups: list[dict]) -> dict[str, str]:
    """Rangements manuels ``{id_du_direct: id_du_groupe | "-"}`` ("-" = rester dans le groupe d'origine)."""
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > MAX_ITEMS_CUSTOM:
        raise InvalidPlaylist("Rangements de chaînes : objet attendu")
    known = {g["id"] for g in groups}
    result = {}
    for key, target in value.items():
        if not isinstance(key, str) or not key.isdigit() or len(key) > 12:
            raise InvalidPlaylist("Identifiant de chaîne invalide (rangements)")
        if target == "-" or target in known:
            result[key] = target
        elif not isinstance(target, str):
            raise InvalidPlaylist("Groupe de destination invalide")
        # Groupe supprimé entre-temps : le rangement est simplement oublié.
    return result


def validate_playlist(payload: object, *, slug: str | None = None) -> dict:
    if not isinstance(payload, dict):
        raise InvalidPlaylist("Objet JSON attendu")
    slug = slug or payload.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise InvalidPlaylist(
            "Identifiant d'URL invalide : lettres minuscules, chiffres, - ou _ (1 à 60 caractères, "
            "commençant par une lettre ou un chiffre)"
        )
    name = payload.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise InvalidPlaylist("Nom obligatoire (1 à 100 caractères)")
    if any(ch in name for ch in "\r\n\t\x00"):
        raise InvalidPlaylist("Le nom ne doit pas contenir de retour à la ligne")
    result: dict = {"slug": slug, "name": name.strip()}
    for kind, (cats, included, excluded) in SELECTION_FIELDS.items():
        result[cats] = _ids(payload.get(cats), pattern=CATEGORY_RE, label="catégorie")
        result[included] = _ids(payload.get(included), label="film" if kind == "vod" else
                                "série" if kind == "series" else "chaîne")
        result[excluded] = _ids(payload.get(excluded), label="film" if kind == "vod" else
                                "série" if kind == "series" else "chaîne")
    result["group_order"] = _ids(payload.get("group_order"), pattern=CATEGORY_RE, label="groupe")[:MAX_GROUPS]
    result["group_names"] = _group_names(payload.get("group_names"))
    live_format = payload.get("live_format", "ts")
    if live_format not in ("ts", "hls"):
        raise InvalidPlaylist("Format des chaînes : ts ou hls")
    result["live_format"] = live_format
    result["catchup"] = bool(payload.get("catchup", True))
    result["dedupe"] = bool(payload.get("dedupe", False))
    result["hide_separators"] = bool(payload.get("hide_separators", False))
    result["failover"] = bool(payload.get("failover", False))
    result["failover_any_category"] = bool(payload.get("failover_any_category", False))
    result["rules"] = _rules(payload.get("rules"))
    result["item_names"] = _item_names(payload.get("item_names"))
    result["item_numbers"] = _item_numbers(payload.get("item_numbers"))
    result["item_logos"] = _item_logos(payload.get("item_logos"))
    result["item_epg"] = _item_epg(payload.get("item_epg"))
    result["custom_groups"] = _custom_groups(payload.get("custom_groups"))
    result["item_groups"] = _item_groups(payload.get("item_groups"), result["custom_groups"])
    favorites = _ids(payload.get("favorites"), pattern=ITEM_KEY_RE, label="favori")
    if len(favorites) > MAX_ITEMS_CUSTOM:
        raise InvalidPlaylist("Trop de favoris")
    result["favorites"] = favorites
    result["favorites_name"] = _text(payload.get("favorites_name", "★ Favoris"), "Nom du groupe des favoris") or "★ Favoris"
    updated = payload.get("updated_at")
    if isinstance(updated, (int, float)) and not isinstance(updated, bool):
        result["updated_at"] = int(updated)
    return result


def uses(saved: dict, kind: str) -> bool:
    """La playlist contient-elle des éléments de ce type (catégories cochées ou ajouts) ?"""
    cats, included, _ = SELECTION_FIELDS[kind]
    return bool(saved.get(cats) or saved.get(included)
                or any(rule.get("kind") == kind for rule in saved.get("rules") or []))


def filter_kind(items: list[dict], saved: dict, kind: str, prefix: str = "") -> list[tuple[dict, str]]:
    """Return ``(item, group_key)`` for each selected item of one type (direct, films ou séries).

    Règle : (catégorie sélectionnée OU élément ajouté manuellement) ET élément non exclu.
    Un élément présent dans plusieurs catégories (``category_ids``) est rangé dans la
    première catégorie sélectionnée qui le contient ; sinon dans sa catégorie principale.
    ``group_key`` = ``prefix`` + identifiant de catégorie (``vod:12``, ``series:5``, ``3``).
    """
    cats_field, included_field, excluded_field = SELECTION_FIELDS[kind]
    categories = set(saved.get(cats_field, []))
    included = set(saved.get(included_field, []))
    excluded = set(saved.get(excluded_field, []))
    result: list[tuple[dict, str]] = []
    for item in items:
        sid = item["stream_id"]
        if sid in excluded:
            continue
        group = next((cid for cid in item["category_ids"] if cid in categories), None)
        if group is not None:
            result.append((item, prefix + group))
        elif sid in included:
            result.append((item, prefix + item["category_id"]))
    return result


def filter_channels(channels: list[dict], saved: dict) -> list[tuple[dict, str]]:
    """Chaînes du direct d'une playlist (voir :func:`filter_kind`)."""
    return filter_kind(channels, saved, "live")


class PlaylistStore:
    """Stockage JSON atomique dans ``DATA_DIR/playlists.json`` (+ copie ``.bak``)."""

    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "playlists.json"
        self._backup = self._path.with_name("playlists.json.bak")
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def _read(self, path: Path) -> list[dict]:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Invalid playlist database")
        return [validate_playlist(item) for item in data]

    def list(self) -> list[dict]:
        with self._lock:
            if not self._path.exists():
                return []
            try:
                return self._read(self._path)
            except (ValueError, OSError) as exc:
                if self._backup.exists():
                    try:
                        data = self._read(self._backup)
                        LOG.error("playlists.json illisible : utilisation de la copie playlists.json.bak")
                        return data
                    except (ValueError, OSError):
                        pass
                raise RuntimeError("Base des playlists illisible ; aucune modification effectuée") from exc

    def get(self, slug: str) -> dict | None:
        if not SLUG_RE.fullmatch(slug):
            return None
        return next((p for p in self.list() if p["slug"] == slug), None)

    def save(self, payload: dict, *, update_slug: str | None = None) -> dict:
        saved = validate_playlist(payload, slug=update_slug)
        saved["updated_at"] = int(time.time())
        with self._lock:
            playlists = self.list()
            index = next((i for i, p in enumerate(playlists) if p["slug"] == saved["slug"]), None)
            if update_slug is not None and index is None:
                raise KeyError("Playlist introuvable")
            if update_slug is None and index is not None:
                raise InvalidPlaylist("Une playlist utilise déjà cet identifiant d'URL")
            if index is None:
                if len(playlists) >= MAX_PLAYLISTS:
                    raise InvalidPlaylist(f"Maximum de {MAX_PLAYLISTS} playlists atteint")
                playlists.append(saved)
            else:
                playlists[index] = saved
            self._write(playlists)
        return saved

    def restore(self, playlists: object, *, mode: str = "merge") -> dict:
        """Restaure des playlists (sauvegarde JSON). ``merge`` : ajoute/remplace par identifiant ;
        ``replace`` : remplace tout. Toutes les entrées sont validées avant toute écriture."""
        if not isinstance(playlists, list):
            raise InvalidPlaylist("Sauvegarde invalide : liste de playlists attendue")
        if mode not in ("merge", "replace"):
            raise InvalidPlaylist("Mode de restauration : merge ou replace")
        incoming = [validate_playlist(item) for item in playlists]
        if len({p["slug"] for p in incoming}) != len(incoming):
            raise InvalidPlaylist("Sauvegarde invalide : identifiants de playlist en double")
        with self._lock:
            current = [] if mode == "replace" else self.list()
            by_slug = {p["slug"]: i for i, p in enumerate(current)}
            added = replaced = 0
            for item in incoming:
                if item["slug"] in by_slug:
                    current[by_slug[item["slug"]]] = item
                    replaced += 1
                else:
                    current.append(item)
                    added += 1
            if len(current) > MAX_PLAYLISTS:
                raise InvalidPlaylist(f"Maximum de {MAX_PLAYLISTS} playlists dépassé")
            self._write(current)
        return {"added": added, "replaced": replaced, "total": len(current)}

    def delete(self, slug: str) -> bool:
        with self._lock:
            playlists = self.list()
            remaining = [p for p in playlists if p["slug"] != slug]
            if len(remaining) == len(playlists):
                return False
            self._write(remaining)
            return True

    def _write(self, playlists: list[dict]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".playlists-", suffix=".tmp", dir=str(self._path.parent))
        except OSError as exc:
            raise RuntimeError("Le dossier des playlists n'est pas accessible en écriture") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(playlists, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o644)  # aucun secret : lisible depuis le partage appdata
            if self._path.exists():
                # Copie de secours de la version précédente (restaurable à la main).
                try:
                    shutil.copyfile(self._path, self._backup)
                except OSError:
                    LOG.warning("Impossible de créer playlists.json.bak")
            os.replace(tmp, self._path)
        except OSError as exc:
            raise RuntimeError("Échec de l'écriture de playlists.json") from exc
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
