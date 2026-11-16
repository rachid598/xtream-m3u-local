"""Serveur Xtream Codes local (``player_api.php``) pour les lecteurs en mode « Xtream ».

Un appareil (voir :mod:`app.devices`) se connecte avec l'adresse ``http://IP:7777`` et ses
identifiants locaux. Il voit uniquement la sélection de sa playlist (ou tout le catalogue si
aucune playlist n'est associée), avec les noms, numéros et l'ordre personnalisés, dans les
rubriques natives du lecteur (Direct, Films, Séries, guide, replay). Les identifiants du
fournisseur restent sur le serveur : toutes les URL de lecture pointent vers ce serveur.
"""
from __future__ import annotations

import logging
import time
import zlib
from datetime import datetime, timezone

from .builder import CUSTOM_PREFIX, GROUP_PREFIX, select_items
from .custom import UNCATEGORIZED

LOG = logging.getLogger("xtream-m3u-local.xtream")
EMPTY_AUTH = {"user_info": {"auth": 0, "status": "Disabled", "message": "Identifiants invalides"}}
EXPOSED_KINDS = ("live", "vod", "series")


def everything() -> dict:
    """Playlist virtuelle « tout le catalogue » (appareil sans playlist associée)."""
    return {"slug": "", "name": "Tout le catalogue",
            "rules": [{"kind": k, "match": "contains", "value": ""} for k in EXPOSED_KINDS],
            "group_order": [], "group_names": {}, "live_format": "ts", "catchup": True}


def public_category_id(group_key: str, kind: str) -> str:
    if group_key.startswith(CUSTOM_PREFIX):
        # Groupe personnalisé : identifiant numérique stable (les lecteurs attendent des nombres).
        return str(900_000_000 + zlib.crc32(group_key.encode()) % 99_000_000)
    cid = group_key[len(GROUP_PREFIX[kind]):]
    return "0" if cid == UNCATEGORIZED else cid


def as_int(value: str) -> int | str:
    return int(value) if str(value).isdigit() else value


class XtreamEmulator:
    def __init__(self, api, store, config, streams=None, redact=None):
        self.api = api
        self.store = store
        self.config = config
        self.streams = streams
        self._redact = redact or (lambda value: value)
        self._cache: dict[tuple, tuple] = {}

    # ------------------------------------------------------------ sélection
    def saved_for(self, device: dict) -> dict:
        slug = device.get("playlist") or ""
        if not slug:
            return everything()
        try:
            saved = self.store.get(slug)
        except RuntimeError:
            saved = None
        if saved is None:
            LOG.warning("Appareil %s : playlist « %s » introuvable", device.get("username"), slug)
            return {"slug": slug, "name": slug, "group_order": [], "group_names": {}}
        return saved

    def entries(self, device: dict, kind: str) -> tuple[list[tuple[dict, str]], dict[str, str], dict]:
        """Éléments d'un type pour cet appareil, ordonnés comme dans la playlist."""
        from .server import order_entries  # import local : évite une dépendance circulaire
        saved = self.saved_for(device)
        state = self.api.kind_state(kind)
        key = (device.get("id"), kind, saved.get("slug"), saved.get("updated_at"), state.updated_at)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        selected, groups = select_items(self.api, saved, kind)
        ordered = order_entries(selected, groups, saved.get("group_order"))
        result = (ordered, groups, saved)
        if len(self._cache) > 200:
            self._cache.clear()
        self._cache[key] = result
        return result

    def allows(self, device: dict, kind: str, item_id: str) -> bool:
        try:
            entries, _, _ = self.entries(device, kind)
        except Exception:
            return False
        return any(item["stream_id"] == item_id for item, _ in entries)

    def alternates(self, device: dict, item_id: str) -> list[str]:
        """Versions de secours d'une chaîne si la playlist de l'appareil a la bascule automatique."""
        try:
            entries, _, saved = self.entries(device, "live")
        except Exception:
            return []
        if not saved.get("failover"):
            return []
        return next((list(item.get("alternates") or []) for item, _ in entries if item["stream_id"] == item_id), [])

    # --------------------------------------------------------------- réponses
    def categories(self, device: dict, kind: str) -> list[dict]:
        entries, groups, saved = self.entries(device, kind)
        names = saved.get("group_names") or {}
        seen: dict[str, dict] = {}
        for _, group in entries:
            if group not in seen:
                seen[group] = {"category_id": public_category_id(group, kind),
                               "category_name": names.get(group) or groups.get(group) or "Sans catégorie",
                               "parent_id": 0}
        return list(seen.values())

    def raw(self, kind: str, item_id: str) -> dict:
        raw = self.api.kind_state(kind).raw_by_id.get(item_id)
        return dict(self._redact(raw)) if isinstance(raw, dict) else {}

    def items(self, device: dict, kind: str, category_id: str | None = None, *, api_epg_ids: bool = False) -> list[dict]:
        entries, _, _ = self.entries(device, kind)
        result = []
        for position, (item, group) in enumerate(entries, start=1):
            cid = public_category_id(group, kind)
            if category_id not in (None, "", "*") and cid != category_id:
                continue
            num = int(item["num"]) if item.get("custom_num") else position
            if kind == "live":
                result.append({
                    "num": num, "name": item["name"], "stream_type": "live", "stream_id": as_int(item["stream_id"]),
                    "stream_icon": item.get("logo", ""),
                    "epg_channel_id": item.get("epg_id") or (f"xtream-{item['stream_id']}" if api_epg_ids else ""),
                    "added": str(self.raw("live", item["stream_id"]).get("added") or "0"), "is_adult": 0,
                    "category_id": cid, "category_ids": [as_int(cid)], "custom_sid": "",
                    "tv_archive": 1 if item.get("archive") else 0, "direct_source": "",
                    "tv_archive_duration": item.get("archive", 0), "thumbnail": "",
                })
                continue
            entry = self.raw(kind, item["stream_id"])
            entry.update({"num": num, "name": item["name"], "category_id": cid, "category_ids": [as_int(cid)]})
            if kind == "vod":
                entry.update({"stream_type": "movie", "stream_id": as_int(item["stream_id"]),
                              "stream_icon": item.get("logo") or entry.get("stream_icon", ""),
                              "container_extension": item.get("ext", "mp4"), "direct_source": "", "custom_sid": ""})
            else:
                entry.update({"series_id": as_int(item["stream_id"]), "cover": item.get("logo") or entry.get("cover", "")})
            result.append(entry)
        return result

    def account(self, device: dict, host: str, port: str) -> dict:
        provider = self.api.account() or {}
        tz = self.api.provider_timezone()
        now = datetime.now(tz if tz is not None else timezone.utc)
        limit = self.streams.limit() if self.streams is not None else 0
        return {
            "user_info": {
                "username": device["username"], "password": device["password"],
                "message": "Xtream M3U Local", "auth": 1, "status": provider.get("status") or "Active",
                "exp_date": str(provider.get("exp_date") or ""), "is_trial": "0",
                "active_cons": str(self.streams.count() if self.streams is not None else 0),
                "created_at": str(device.get("created_at") or ""),
                "max_connections": str(limit or provider.get("max_connections") or "1"),
                "allowed_output_formats": ["m3u8", "ts"],
            },
            "server_info": {
                "url": host, "port": port, "https_port": "", "server_protocol": "http", "rtmp_port": "",
                "timezone": getattr(tz, "key", "UTC"), "timestamp_now": int(time.time()),
                "time_now": now.strftime("%Y-%m-%d %H:%M:%S"), "process": True,
            },
        }

    def passthrough(self, action: str, **params: str):
        """Actions détaillées relayées au fournisseur (fiche film/série, guide court), sans identifiants."""
        return self._redact(self.api.raw_action(action, **params))
