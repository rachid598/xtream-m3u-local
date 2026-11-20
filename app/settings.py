"""Réglages modifiables depuis l'interface Web : ``DATA_DIR/settings.json``.

- ``tailscale_local`` : les appareils Tailscale (100.64.0.0/10) sont traités comme le réseau local
  (interface Web et lecteurs sans ``REMOTE_ACCESS`` ni code), en plus de ``LOCAL_NETWORKS``.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import access

TAILSCALE = access.parse_networks("100.64.0.0/10, fd7a:115c:a1e0::/48")
DEFAULTS = {"tailscale_local": False, "allow_mix": False}


class Settings:
    def __init__(self, data_dir: str) -> None:
        self.path = Path(data_dir) / "settings.json"
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None

    def get(self) -> dict:
        with self._lock:
            try:
                stamp = self.path.stat().st_mtime
            except OSError:
                return dict(DEFAULTS)
            if self._cache and self._cache[0] == stamp:
                return dict(self._cache[1])
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            values = {**DEFAULTS, **{k: bool(data.get(k)) for k in DEFAULTS if k in data}}
            self._cache = (stamp, values)
            return dict(values)

    def update(self, changes: dict) -> dict:
        values = {**self.get(), **{k: bool(v) for k, v in changes.items() if k in DEFAULTS}}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(values, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self._cache = None
        return values


_SETTINGS: Settings | None = None


def install(settings: Settings | None) -> None:
    global _SETTINGS
    _SETTINGS = settings


def current() -> Settings | None:
    return _SETTINGS


def mix_allowed() -> bool:
    """Playlists « tous les abonnements » autorisées ? (désactivé par défaut : un profil = un serveur)."""
    if _SETTINGS is None:
        return True  # sans réglages (tests, intégrations) : comportement d'avant les profils
    return bool(_SETTINGS.get().get("allow_mix"))


def local_networks(config) -> tuple:
    """Réseaux « locaux » en plus des réseaux privés : ``LOCAL_NETWORKS`` + Tailscale si la case est cochée."""
    extra = tuple(config.local_networks)
    if _SETTINGS is not None and _SETTINGS.get().get("tailscale_local"):
        extra += TAILSCALE
    return extra
