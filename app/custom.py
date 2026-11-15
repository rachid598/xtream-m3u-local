"""Durable named M3U playlist selection storage (no provider secrets)."""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
MAX_PLAYLISTS = 100


class InvalidPlaylist(ValueError):
    pass


def _ids(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > 100_000:
        raise InvalidPlaylist("Les sélections doivent être des listes (100 000 éléments maximum)")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, (str, int)) or not str(item).isdigit():
            raise InvalidPlaylist("Identifiant de chaîne ou catégorie invalide")
        normalized = str(item)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def validate_playlist(payload: object, *, slug: str | None = None) -> dict:
    if not isinstance(payload, dict):
        raise InvalidPlaylist("Objet JSON attendu")
    slug = slug or payload.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise InvalidPlaylist("Slug invalide : lettres minuscules, chiffres, - ou _ (1 à 60 caractères)")
    name = payload.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise InvalidPlaylist("Nom obligatoire (1 à 100 caractères)")
    cats = _ids(payload.get("category_ids", []))
    includes = _ids(payload.get("included_stream_ids", []))
    excludes = _ids(payload.get("excluded_stream_ids", []))
    return {
        "slug": slug, "name": name.strip(),
        "category_ids": cats,
        "included_stream_ids": includes,
        "excluded_stream_ids": excludes,
    }


def filter_channels(channels: list[dict], saved: dict) -> list[dict]:
    categories = set(saved["category_ids"])
    included = set(saved["included_stream_ids"])
    excluded = set(saved["excluded_stream_ids"])
    return [
        c for c in channels
        if str(c["stream_id"]) not in excluded
        and (str(c.get("category_id", "")) in categories or str(c["stream_id"]) in included)
    ]


class PlaylistStore:
    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "playlists.json"
        self._lock = threading.RLock()

    def list(self) -> list[dict]:
        with self._lock:
            if not self._path.exists():
                return []
            try:
                with self._path.open(encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    raise ValueError("Invalid playlist database")
                return [validate_playlist(item) for item in data]
            except (ValueError, OSError) as exc:
                raise RuntimeError("Base des playlists illisible ; aucune modification effectuée") from exc

    def get(self, slug: str) -> dict | None:
        if not SLUG_RE.fullmatch(slug):
            return None
        return next((p for p in self.list() if p["slug"] == slug), None)

    def save(self, payload: dict, *, update_slug: str | None = None) -> dict:
        saved = validate_playlist(payload, slug=update_slug)
        with self._lock:
            playlists = self.list()
            index = next((i for i, p in enumerate(playlists) if p["slug"] == saved["slug"]), None)
            if update_slug is not None and index is None:
                raise KeyError("Playlist introuvable")
            if update_slug is None and index is not None:
                raise InvalidPlaylist("Une playlist avec ce slug existe déjà")
            if index is None:
                if len(playlists) >= MAX_PLAYLISTS:
                    raise InvalidPlaylist("Maximum de 100 playlists atteint")
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
        except OSError as exc:
            raise RuntimeError("Le dossier des playlists n’est pas accessible en écriture") from exc
        fd, tmp = tempfile.mkstemp(prefix=".playlists-", suffix=".tmp", dir=str(self._path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(playlists, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
