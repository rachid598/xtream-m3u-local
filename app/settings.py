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
RETIRED_EPG_SOURCE = "https://epgshare01.online/epgshare01/epg_ripper_MA1.xml.gz"


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


def _reservations(value) -> dict[str, dict]:
    """Réservations de connexions par abonnement : ``{"2": {"profile": "2", "count": 1}}``."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict] = {}
    for raw_k, raw_v in value.items():
        if not str(raw_k).isdigit() or int(raw_k) not in INDICES or not isinstance(raw_v, dict):
            continue
        prof = str(raw_v.get("profile") or "").strip()
        try:
            count = int(raw_v.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if prof.isdigit() and int(prof) in INDICES and 1 <= count <= 99:
            result[str(int(raw_k))] = {"profile": str(int(prof)), "count": count}
    return result


class Settings:
    def __init__(self, data_dir: str, *, default_epg_sources: tuple[str, ...] = ()) -> None:
        self.path = Path(data_dir) / "settings.json"
        self._default_epg_sources = default_epg_sources
        self._lock = threading.Lock()
        self._cache: tuple[float, dict] | None = None

    def get(self) -> dict:
        with self._lock:
            try:
                stamp = self.path.stat().st_mtime
            except OSError:
                return {**DEFAULTS, "mixable": [], "aux_providers": [], "reservations": {}, "public_hosts": [],
                        "epg_sources": list(self._default_epg_sources)}
            if self._cache and self._cache[0] == stamp:
                return {**self._cache[1], "mixable": list(self._cache[1]["mixable"]),
                        "aux_providers": list(self._cache[1]["aux_providers"]),
                        "reservations": {k: dict(v) for k, v in self._cache[1]["reservations"].items()},
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
            # Abonnements « complément / secours » : n'apparaissent pas comme espace membre séparé en haut.
            values["aux_providers"] = [i for i in _indices(data.get("aux_providers")) if i != 1]
            # Réservations de connexions (Option B garantie souple) par abonnement multi-connexions.
            values["reservations"] = _reservations(data.get("reservations"))
            values["public_hosts"] = _hosts(data.get("public_hosts"))
            sources = _urls(data.get("epg_sources"))
            if self._default_epg_sources and not data.get("epg_sources_initialized"):
                sources = _urls([url for url in sources if url != RETIRED_EPG_SOURCE] + list(self._default_epg_sources))
            values["epg_sources"] = sources
            self._cache = (stamp, values)
            return {**values, "reservations": {k: dict(v) for k, v in values["reservations"].items()}}

    def update(self, changes: dict) -> dict:
        values = {**self.get(), **{k: bool(v) for k, v in changes.items() if k in DEFAULTS}}
        if "mixable" in changes:
            values["mixable"] = _indices(changes["mixable"])
        if "aux_providers" in changes:
            values["aux_providers"] = [i for i in _indices(changes["aux_providers"]) if i != 1]
        if "reservations" in changes:
            values["reservations"] = _reservations(changes["reservations"])
        if "public_hosts" in changes:
            values["public_hosts"] = _hosts(changes["public_hosts"])
        if "epg_sources" in changes:
            values["epg_sources"] = _urls(changes["epg_sources"])
        with self._lock:
            had_init = False
            try:
                had_init = bool(json.loads(self.path.read_text(encoding="utf-8")).get("epg_sources_initialized"))
            except (OSError, ValueError, AttributeError):
                pass
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            stored = {**values, "epg_sources_initialized": True} if (self._default_epg_sources or had_init) else values
            tmp.write_text(json.dumps(stored, indent=1), encoding="utf-8")
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


def aux_providers() -> set[str]:
    """Abonnements servant de complément / secours plutôt que d'espace membre séparé."""
    if _SETTINGS is None:
        return set()
    return {str(i) for i in _SETTINGS.get()["aux_providers"] if i != 1}


def reservations() -> dict[str, dict]:
    """Réservations de connexions : ``{provider_id: {"profile": space_id, "count": N}}``."""
    if _SETTINGS is None:
        return {}
    return _SETTINGS.get()["reservations"]


def reservation_for(provider: str | int) -> tuple[str, int] | None:
    """Retourne ``(espace_bénéficiaire, nb_connexions_garanties)`` pour cet abonnement, ou ``None``."""
    entry = reservations().get(str(provider))
    if not entry:
        return None
    return str(entry["profile"]), int(entry["count"])


def set_reservation(index: int, profile: str | int | None, count: int) -> dict[str, dict]:
    """Enregistre ou efface la réservation de connexions d'un abonnement (1 à 9)."""
    if _SETTINGS is None:
        raise RuntimeError("Réglages non modifiables dans ce mode de démarrage")
    if index not in INDICES:
        raise ValueError("Numéro d'abonnement invalide")
    cur = dict(_SETTINGS.get()["reservations"])
    key = str(index)
    try:
        num = int(count or 0)
    except (TypeError, ValueError):
        num = 0
    prof = str(profile or "").strip()
    if num <= 0 or not prof.isdigit() or int(prof) not in INDICES:
        cur.pop(key, None)
    else:
        cur[key] = {"profile": str(int(prof)), "count": min(99, num)}
    return _SETTINGS.update({"reservations": cur})["reservations"]


def clear_reservations_for(index: int) -> dict[str, dict]:
    """Nettoie toute réservation portée par ou pointant vers un abonnement supprimé."""
    if _SETTINGS is None:
        return {}
    key = str(index)
    cur = {k: v for k, v in _SETTINGS.get()["reservations"].items() if k != key and v.get("profile") != key}
    return _SETTINGS.update({"reservations": cur})["reservations"]


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


def set_aux(index: int, on: bool) -> list[int]:
    """Définit si un abonnement (2 à 9) est un complément/secours plutôt qu'un espace membre séparé."""
    if _SETTINGS is None:
        raise RuntimeError("Réglages non modifiables dans ce mode de démarrage")
    on = bool(on) and index != 1
    state = _SETTINGS.get()
    cur_aux = set(state["aux_providers"])
    cur_aux = (cur_aux | {index}) if on else (cur_aux - {index})
    changes: dict = {"aux_providers": sorted(cur_aux)}
    if on:
        changes["mixable"] = sorted(set(state["mixable"]) | {index})
    return _SETTINGS.update(changes)["aux_providers"]


def local_networks(config) -> tuple:
    """Réseaux « locaux » en plus des réseaux privés : ``LOCAL_NETWORKS`` + Tailscale si la case est cochée."""
    extra = tuple(config.local_networks)
    if _SETTINGS is not None and _SETTINGS.get().get("tailscale_local"):
        extra += TAILSCALE
    return extra
