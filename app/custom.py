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
    result = {
        "slug": slug,
        "name": name.strip(),
        "category_ids": _ids(payload.get("category_ids"), pattern=CATEGORY_RE, label="catégorie"),
        "included_stream_ids": _ids(payload.get("included_stream_ids")),
        "excluded_stream_ids": _ids(payload.get("excluded_stream_ids")),
    }
    result["group_order"] = _ids(payload.get("group_order"), pattern=CATEGORY_RE, label="groupe")[:MAX_GROUPS]
    result["group_names"] = _group_names(payload.get("group_names"))
    updated = payload.get("updated_at")
    if isinstance(updated, (int, float)) and not isinstance(updated, bool):
        result["updated_at"] = int(updated)
    return result


def filter_channels(channels: list[dict], saved: dict) -> list[tuple[dict, str]]:
    """Return ``(channel, group_category_id)`` for each channel of a saved playlist.

    Règle : (catégorie sélectionnée OU chaîne ajoutée manuellement) ET chaîne non exclue.
    Une chaîne présente dans plusieurs catégories (``category_ids``) est rangée dans la
    première catégorie sélectionnée qui la contient ; sinon dans sa catégorie principale.
    """
    categories = set(saved["category_ids"])
    included = set(saved["included_stream_ids"])
    excluded = set(saved["excluded_stream_ids"])
    result: list[tuple[dict, str]] = []
    for channel in channels:
        sid = channel["stream_id"]
        if sid in excluded:
            continue
        group = next((cid for cid in channel["category_ids"] if cid in categories), None)
        if group is not None:
            result.append((channel, group))
        elif sid in included:
            result.append((channel, channel["category_id"]))
    return result


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
