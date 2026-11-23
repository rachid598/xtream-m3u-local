"""Réglages modifiables depuis l'interface Web : ``DATA_DIR/settings.json``.

- ``tailscale_local`` : les appareils Tailscale (100.64.0.0/10) sont traités comme le réseau local
  (interface Web et lecteurs sans ``REMOTE_ACCESS`` ni code), en plus de ``LOCAL_NETWORKS``.
"""
from __future__ import annotations

import json
import re
import os
import threading
from pathlib import Path

from . import access

TAILSCALE = access.parse_networks("100.64.0.0/10, fd7a:115c:a1e0::/48")
DEFAULTS = {"tailscale_local": False}
INDICES = range(1, 10)   # numéros d'abonnement


HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def _hosts(value) -> list[str]:
    """Adresses publiques (reverse proxy) : noms de domaine en https, sans doublon, 10 au plus."""
    if not isinstance(value, list):
        return []
    result = []
    for raw in value:
        host = re.sub(r"^https?://", "", str(raw or "").strip().lower()).split("/")[0].rstrip(".")
        if HOST_RE.fullmatch(host) and host not in result:
            result.append(host)
    return result[:10]


def _urls(value) -> list[str]:
    """Guides XMLTV externes ajoutés dans l'interface : adresses http(s), sans doublon, 8 au plus."""
    if not isinstance(value, list):
        return []
    result = []
    for raw in value:
        url = str(raw or "").strip()
        if re.fullmatch(r"https?://[^\s<>\"']{4,500}", url) and url not in result:
            result.append(url)
    return result[:8]


def _indices(value) -> list[int]:
    """Liste de numéros d'abonnement valides (1 à 9), triés, sans doublon."""
    if not isinstance(value, list):
        return []
    return sorted({int(v) for v in value if isinstance(v, (int, str)) and str(v).isdigit() and int(v) in INDICES})


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
                return {**DEFAULTS, "mixable": [], "public_hosts": [], "epg_sources": []}
            if self._cache and self._cache[0] == stamp:
                return {**self._cache[1], "mixable": list(self._cache[1]["mixable"]),
                        "public_hosts": list(self._cache[1]["public_hosts"]),
                        "epg_sources": list(self._cache[1]["epg_sources"])}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            values = {**DEFAULTS, **{k: bool(data.get(k)) for k in DEFAULTS if k in data}}
            # Abonnements « mélangeables » (playlists mélange des autres profils). Ancienne case globale
            # « autoriser le mélange » cochée : tous les abonnements le restent.
            values["mixable"] = _indices(data["mixable"]) if "mixable" in data else (
                list(INDICES) if data.get("allow_mix") else [])
            values["public_hosts"] = _hosts(data.get("public_hosts"))
            values["epg_sources"] = _urls(data.get("epg_sources"))
            self._cache = (stamp, values)
            return dict(values)

    def update(self, changes: dict) -> dict:
        values = {**self.get(), **{k: bool(v) for k, v in changes.items() if k in DEFAULTS}}
        if "mixable" in changes:
            values["mixable"] = _indices(changes["mixable"])
        if "public_hosts" in changes:
            values["public_hosts"] = _hosts(changes["public_hosts"])
        if "epg_sources" in changes:
            values["epg_sources"] = _urls(changes["epg_sources"])
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


def mixable() -> set[str] | None:
    """Abonnements qui peuvent être mélangés aux playlists des autres profils (``None`` = tous : pas de
    réglages installés, comportement d'avant les profils pour les tests et intégrations)."""
    if _SETTINGS is None:
        return None
    return {str(i) for i in _SETTINGS.get()["mixable"]}


def epg_sources() -> list[str]:
    """Guides XMLTV externes ajoutés dans l'interface (en plus de ``EPG_EXTRA_URLS``)."""
    return list(_SETTINGS.get()["epg_sources"]) if _SETTINGS is not None else []


def public_hosts() -> list[str]:
    """Adresses publiques déclarées (Nginx Proxy Manager, Traefik…), sans le tunnel Cloudflare."""
    return list(_SETTINGS.get()["public_hosts"]) if _SETTINGS is not None else []


def set_mixable(index: int, on: bool) -> list[int]:
    """Coche ou décoche « peut être mélangé aux autres abonnements » pour un abonnement."""
    if _SETTINGS is None:
        raise RuntimeError("Réglages non modifiables dans ce mode de démarrage")
    current = set(_SETTINGS.get()["mixable"])
    current = current | {index} if on else current - {index}
    return _SETTINGS.update({"mixable": sorted(current)})["mixable"]


def local_networks(config) -> tuple:
    """Réseaux « locaux » en plus des réseaux privés : ``LOCAL_NETWORKS`` + Tailscale si la case est cochée."""
    extra = tuple(config.local_networks)
    if _SETTINGS is not None and _SETTINGS.get().get("tailscale_local"):
        extra += TAILSCALE
    return extra
