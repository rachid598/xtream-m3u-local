"""Generate local M3U playlists from the Xtream player API (port 7777).

Bibliothèque standard Python uniquement, y compris pour le relais des flux TS.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from . import __version__
from .httputil import accepts_gzip, encode_body, etag_matches, etag_of, gzip_stream
from .hls import MAX_MANIFEST_BYTES, SEGMENT_MIME, HlsRegistry, rewrite_manifest, segment_ext
from .streams import EvictedRecently, LimitReached, StreamManager
from .custom import CATEGORY_RE, UNCATEGORIZED, PlaylistStore, filter_channels, filter_kind, uses
from .epg import EPG_MODES, EpgCache, EpgUnavailable, normalize_listings, xml_channel_id
from .vod import KeyedCache, SeriesCache, episode_name, mime_for, normalize_movie, normalize_series

LOG = logging.getLogger("xtream-m3u-local")
DEFAULT_USER_AGENT = f"XtreamM3ULocal/{__version__}"
STREAM_RE = re.compile(r"^/stream/([0-9]{1,12})\.ts$")
HLS_LIVE_RE = re.compile(r"^/hls/([0-9]{1,12})\.m3u8$")
HLS_SUB_RE = re.compile(r"^/hls/v/([A-Za-z0-9_-]{16,32})\.m3u8$")
HLS_SEG_RE = re.compile(r"^/hls/s/([A-Za-z0-9_-]{16,32})\.([A-Za-z0-9]{1,5})$")
TIMESHIFT_RE = re.compile(r"^/timeshift/([0-9]{1,12})\.ts$")
VOD_RE = re.compile(r"^/(movie|series)/([0-9]{1,12})\.([A-Za-z0-9]{1,5})$")
CUSTOM_RE = re.compile(r"^/p/([a-z0-9][a-z0-9_-]{0,59})\.(m3u|xml)$")
HOST_RE = re.compile(r"^[a-zA-Z0-9.\-:\[\]]+$")
TRUE_VALUES = ("1", "true", "yes", "on", "oui")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in TRUE_VALUES


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} doit être un nombre entier") from None


def _epg_source() -> str:
    value = os.environ.get("EPG_SOURCE", "").strip().lower() or "auto"
    if value not in EPG_MODES:
        raise ValueError("EPG_SOURCE doit valoir auto, xmltv ou api")
    return value


def _max_connections() -> int:
    raw = os.environ.get("MAX_CONNECTIONS", "").strip().lower()
    if raw in ("", "auto"):
        return -1
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("MAX_CONNECTIONS doit valoir auto, 0 (illimité) ou un nombre") from None
    if value < 0:
        raise ValueError("MAX_CONNECTIONS doit valoir auto, 0 (illimité) ou un nombre")
    return value


def _timezone_env() -> str:
    value = os.environ.get("PROVIDER_TIMEZONE", "").strip()
    if value:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("PROVIDER_TIMEZONE doit être un fuseau horaire, ex. Europe/Paris") from None
    return value


def _epg_url() -> str:
    value = os.environ.get("EPG_URL", "").strip()
    if value and (urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).netloc):
        raise ValueError("EPG_URL doit être une URL http(s) vers un guide XMLTV")
    return value


def normalize_xtream_url(raw: str) -> str:
    """Accept ``http://host:port``, ``host:port`` or a pasted ``.../player_api.php`` URL base."""
    url = raw.strip()
    if url and "://" not in url:
        url = "http://" + url
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "XTREAM_URL doit être l'adresse de base du fournisseur, ex. http://serveur:8080 "
            "(sans identifiants ni paramètres ?username=...)"
        )
    path = parsed.path.rstrip("/")
    for suffix in ("/player_api.php", "/get.php", "/xmltv.php"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    return f"{parsed.scheme}://{parsed.netloc}{path}".rstrip("/")


@dataclass(frozen=True)
class Config:
    xtream_url: str
    username: str
    password: str
    port: int = 7777
    public_base_url: str = ""
    cache_seconds: int = 1800
    access_token: str = ""
    enable_epg: bool = True
    web_port: int = 7778
    data_dir: str = "/data"
    web_username: str = "admin"
    web_password: str = ""
    user_agent: str = DEFAULT_USER_AGENT
    epg_cache_hours: int = 12
    series_cache_hours: int = 24
    epg_source: str = "auto"
    epg_url: str = ""
    epg_fetch_seconds: int = 15
    epg_api_max_channels: int = 1000
    max_connections: int = -1        # -1 = auto (valeur de l'abonnement), 0 = illimité
    stream_takeover: bool = True
    provider_timezone: str = ""      # vide = fuseau annoncé par le fournisseur (server_info.timezone)
    series_fetch_seconds: int = 20

    @classmethod
    def from_env(cls) -> "Config":
        url = normalize_xtream_url(os.environ.get("XTREAM_URL", ""))
        username = os.environ.get("XTREAM_USERNAME", "").strip()
        password = os.environ.get("XTREAM_PASSWORD", "").strip()
        if not username or not password:
            raise ValueError("XTREAM_USERNAME et XTREAM_PASSWORD sont obligatoires")
        placeholders = {"REMPLACER_UTILISATEUR", "REMPLACER_MOT_DE_PASSE", "MON_UTILISATEUR", "MON_MOT_DE_PASSE"}
        if username in placeholders or password in placeholders or any(
                marker in url for marker in ("ADRESSE_DU_SERVEUR", "votre-serveur-xtream", "XTREAM_HOST")):
            raise ValueError("Remplace les valeurs d'exemple de XTREAM_URL / XTREAM_USERNAME / XTREAM_PASSWORD")
        port = _env_int("PORT", 7777)
        web_port = _env_int("WEB_PORT", 7778)
        for label, value in (("PORT", port), ("WEB_PORT", web_port)):
            if not 0 < value <= 65535:
                raise ValueError(f"{label} invalide")
        if port == web_port:
            raise ValueError("PORT et WEB_PORT doivent être différents")
        base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if base and (urlsplit(base).scheme not in ("http", "https") or not urlsplit(base).netloc):
            raise ValueError("PUBLIC_BASE_URL doit être une URL http(s), ex. http://192.168.1.10:7777")
        return cls(
            xtream_url=url,
            username=username,
            password=password,
            port=port,
            public_base_url=base,
            cache_seconds=max(0, _env_int("CACHE_SECONDS", 1800)),
            access_token=os.environ.get("ACCESS_TOKEN", "").strip(),
            enable_epg=_env_bool("ENABLE_EPG", True),
            web_port=web_port,
            data_dir=os.environ.get("DATA_DIR", "/data").strip() or "/data",
            web_username=os.environ.get("WEB_USERNAME", "").strip() or "admin",
            web_password=os.environ.get("WEB_PASSWORD", ""),
            user_agent=os.environ.get("XTREAM_USER_AGENT", "").strip() or DEFAULT_USER_AGENT,
            epg_cache_hours=max(1, _env_int("EPG_CACHE_HOURS", 12)),
            series_cache_hours=max(1, _env_int("SERIES_CACHE_HOURS", 24)),
            epg_source=_epg_source(),
            epg_url=_epg_url(),
            epg_fetch_seconds=max(0, _env_int("EPG_FETCH_SECONDS", 15)),
            epg_api_max_channels=max(0, _env_int("EPG_API_MAX_CHANNELS", 1000)),
            max_connections=_max_connections(),
            stream_takeover=_env_bool("STREAM_TAKEOVER", True),
            provider_timezone=_timezone_env(),
            series_fetch_seconds=max(0, _env_int("SERIES_FETCH_SECONDS", 20)),
        )


class UpstreamError(Exception):
    """An upstream request failed (message never contains credentials)."""


def _clean_text(value: object, default: str = "") -> str:
    text = str(value if value is not None else "")
    text = " ".join(text.replace("\x00", " ").split())
    return text or default


def normalize_channel(entry: object) -> dict | None:
    """Convert a raw ``get_live_streams`` entry into the internal channel structure."""
    if not isinstance(entry, dict):
        return None
    sid = str(entry.get("stream_id", "")).strip()
    if not sid.isdigit() or len(sid) > 12:
        return None
    cats: list[str] = []
    primary = entry.get("category_id")
    if primary not in (None, ""):
        cats.append(str(primary).strip())
    extra = entry.get("category_ids")
    if isinstance(extra, list):
        for cid in extra:
            if cid not in (None, "") and str(cid).strip() not in cats:
                cats.append(str(cid).strip())
    cats = [c for c in cats if CATEGORY_RE.fullmatch(c) and c != UNCATEGORIZED]
    if not cats:
        cats = [UNCATEGORIZED]
    num = str(entry.get("num", "")).strip()
    logo = _clean_text(entry.get("stream_icon"))
    if not logo.lower().startswith(("http://", "https://")):
        logo = ""
    archive = 0
    if str(entry.get("tv_archive", "")).strip().lower() in ("1", "true"):
        try:
            archive = max(0, min(int(str(entry.get("tv_archive_duration") or 0).strip()), 365))
        except ValueError:
            archive = 0
    return {
        "stream_id": sid,
        "name": _clean_text(entry.get("name"), "Sans nom"),
        "logo": logo,
        "epg_id": _clean_text(entry.get("epg_channel_id")),
        "num": num if num.isdigit() else "",
        "category_id": cats[0],
        "category_ids": cats,
        "archive": archive,
    }


KINDS = {
    # type : (action catégories, action contenus, normalisation, libellé)
    "live": ("get_live_categories", "get_live_streams", normalize_channel, "chaînes"),
    "vod": ("get_vod_categories", "get_vod_streams", normalize_movie, "films"),
    "series": ("get_series_categories", "get_series", normalize_series, "séries"),
}
# Préfixe des groupes dans les playlists : les identifiants de catégories des films et séries
# peuvent coïncider avec ceux du direct ; le direct reste sans préfixe (compatibilité V2).
GROUP_PREFIX = {"live": "", "vod": "vod:", "series": "series:"}


class _KindState:
    def __init__(self) -> None:
        self.items: list[dict] | None = None
        self.by_id: dict[str, dict] = {}
        self.categories: dict[str, str] = {}
        self.category_meta: dict[str, dict] = {}
        self.raw_categories: list = []
        self.raw_streams: list = []
        self.categories_error: str | None = None
        self.expires_at = 0.0
        self.retry_at = 0.0
        self.updated_at: float | None = None
        self.last_error: str | None = None
        self.lock = threading.Lock()


class XtreamAPI:
    """Client Xtream : catalogues direct / films / séries en cache mémoire, repli sur cache périmé."""

    RETRY_AFTER_FAILURE = 60

    def __init__(self, config: Config):
        self.config = config
        self._kinds = {kind: _KindState() for kind in KINDS}
        self._account: dict | None = None
        self._account_expires = 0.0

    # Attributs historiques (catalogue du direct), utilisés par l'interface et les tests.
    def kind_state(self, kind: str = "live") -> _KindState:
        return self._kinds[kind]

    category_meta = property(lambda self: self._kinds["live"].category_meta)
    raw_categories = property(lambda self: self._kinds["live"].raw_categories)
    raw_streams = property(lambda self: self._kinds["live"].raw_streams)
    categories_error = property(lambda self: self._kinds["live"].categories_error)
    updated_at = property(lambda self: self._kinds["live"].updated_at)
    last_error = property(lambda self: self._kinds["live"].last_error)

    def api_url(self, action: str | None = None, **extra: str) -> str:
        params = {"username": self.config.username, "password": self.config.password}
        if action:
            params["action"] = action
        params.update(extra)
        return f"{self.config.xtream_url}/player_api.php?" + urlencode(params)

    def direct_url(self, kind: str, item_id: str, extension: str) -> str:
        """URL fournisseur d'un flux : ``live`` (chaîne), ``movie`` (film) ou ``series`` (épisode)."""
        return (
            f"{self.config.xtream_url}/{kind}/"
            f"{quote(self.config.username, safe='')}/"
            f"{quote(self.config.password, safe='')}/"
            f"{item_id}.{extension}"
        )

    def direct_stream(self, stream_id: str, extension: str = "ts") -> str:
        return self.direct_url("live", stream_id, extension)

    def epg_url(self) -> str:
        return (
            f"{self.config.xtream_url}/xmltv.php?"
            + urlencode({"username": self.config.username, "password": self.config.password})
        )

    def _fetch(self, url: str, label: str):
        req = Request(url, headers={"User-Agent": self.config.user_agent, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=60) as response:
                # Refuser les réponses démesurées (les catalogues VOD peuvent dépasser 50 Mo).
                raw = response.read(200 * 1024 * 1024 + 1)
                if len(raw) > 200 * 1024 * 1024:
                    raise UpstreamError("Réponse API trop volumineuse (> 200 Mo)")
        except UpstreamError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, HTTPException, ValueError) as exc:
            status = getattr(exc, "code", None)
            LOG.warning("API error for %s status=%s", label, status or "réseau")
            if status in (401, 403):
                raise UpstreamError(f"Le fournisseur refuse l'accès (HTTP {status}) : vérifie les identifiants") from None
            raise UpstreamError(f"API Xtream indisponible ({status or 'réseau'})") from None
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise UpstreamError("L'API Xtream ne renvoie pas du JSON valide") from None

    def get_json(self, action: str) -> list:
        decoded = self._fetch(self.api_url(action), f"action={action}")
        if isinstance(decoded, dict) and not decoded:
            return []
        if isinstance(decoded, dict) and all(isinstance(v, dict) for v in decoded.values()):
            # Certains panneaux renvoient un objet indexé au lieu d'une liste.
            return list(decoded.values())
        if not isinstance(decoded, list):
            raise UpstreamError("Réponse API inattendue : liste absente (identifiants ou abonnement ?)")
        return decoded

    def simple_epg(self, stream_id: str) -> object:
        """Programmes d'une chaîne (plusieurs jours) : ``get_simple_data_table``."""
        return self._fetch(self.api_url("get_simple_data_table", stream_id=stream_id), "action=get_simple_data_table")

    def series_info(self, series_id: str) -> object:
        return self._fetch(self.api_url("get_series_info", series_id=series_id), "action=get_series_info")

    def catalog(self, *, refresh: bool = False, kind: str = "live") -> tuple[list[dict], dict[str, str]]:
        """``(éléments normalisés, {category_id: nom})`` dans l'ordre du fournisseur."""
        categories_action, items_action, normalize, label = KINDS[kind]
        st = self._kinds[kind]
        with st.lock:
            now = time.monotonic()
            if st.items is not None and not refresh and now < st.expires_at:
                return st.items, st.categories
            if st.items is not None and now < st.retry_at:
                return st.items, st.categories
            try:
                streams = self.get_json(items_action)
                if not streams:
                    LOG.warning("Upstream returned an empty %s list", kind)
                categories_error = None
                try:
                    categories_raw = self.get_json(categories_action)
                except UpstreamError as exc:
                    categories_raw = []
                    categories_error = f"{categories_action} a échoué : {exc}"
                    LOG.warning("Categories unavailable for %s; using derived groups", kind)
                categories: dict[str, str] = {}
                meta: dict[str, dict] = {}
                for item in categories_raw:
                    if not isinstance(item, dict) or item.get("category_id") in (None, ""):
                        continue
                    cid = str(item["category_id"]).strip()
                    if not CATEGORY_RE.fullmatch(cid) or cid == UNCATEGORIZED or cid in categories:
                        continue
                    categories[cid] = _clean_text(item.get("category_name"), f"Catégorie {cid}")
                    parent = str(item.get("parent_id") if item.get("parent_id") is not None else "").strip()
                    meta[cid] = {"parent_id": parent if parent not in ("", "0") else "",
                                 "source": "provider", "position": len(meta)}
                items = [c for c in (normalize(e) for e in streams) if c is not None]
                for element in items:
                    for cid in element["category_ids"]:
                        if cid not in categories:
                            categories[cid] = "Sans catégorie" if cid == UNCATEGORIZED else f"Catégorie {cid}"
                            meta[cid] = {"parent_id": "", "source": "derived", "position": len(meta)}
                st.category_meta = meta
                st.raw_categories = categories_raw if isinstance(categories_raw, list) else []
                st.raw_streams = streams
                st.categories_error = categories_error
                st.items = items
                st.by_id = {item["stream_id"]: item for item in items}
                st.categories = categories
                st.expires_at = now + self.config.cache_seconds
                st.retry_at = 0.0
                st.updated_at = time.time()
                st.last_error = None
                LOG.info("Catalogue %s chargé : %d %s, %d catégories", kind, len(items), label, len(categories))
            except UpstreamError as exc:
                st.last_error = str(exc)
                if st.items is None:
                    raise
                st.retry_at = now + self.RETRY_AFTER_FAILURE
                LOG.warning("Using stale %s catalog because upstream refresh failed", kind)
            return st.items, st.categories

    def provider_timezone(self):
        """Fuseau horaire du serveur Xtream (les dates de replay sont exprimées dans ce fuseau)."""
        name = self.config.provider_timezone or (self._account or {}).get("timezone") or ""
        if not name and self._account is None:
            self.account()
            name = (self._account or {}).get("timezone") or ""
        try:
            return ZoneInfo(name) if name else timezone.utc
        except (ZoneInfoNotFoundError, ValueError):
            return timezone.utc

    def timeshift_url(self, stream_id: str, start_epoch: int, minutes: int) -> str:
        start = datetime.fromtimestamp(start_epoch, self.provider_timezone()).strftime("%Y-%m-%d:%H-%M")
        return (f"{self.config.xtream_url}/timeshift/{quote(self.config.username, safe='')}/"
                f"{quote(self.config.password, safe='')}/{minutes}/{start}/{stream_id}.ts")

    def lookup(self, kind: str, item_id: str) -> dict | None:
        """Élément déjà en cache (sans appel réseau) : nom d'une chaîne ou d'un film pour l'affichage."""
        return self._kinds[kind].by_id.get(item_id)

    def max_connections_hint(self) -> int:
        """Nombre de connexions autorisées par l'abonnement (0 si inconnu), sans attendre le réseau."""
        if self._account is None and time.monotonic() >= self._account_expires:
            self._account_expires = time.monotonic() + 30
            threading.Thread(target=self.account, daemon=True).start()
        try:
            return int((self._account or {}).get("max_connections") or 0)
        except ValueError:
            return 0

    def account(self) -> dict | None:
        """Résumé de l'abonnement (sans identifiants) ; ``None`` si indisponible."""
        now = time.monotonic()
        if self._account is not None and now < self._account_expires:
            return self._account
        try:
            data = self._fetch(self.api_url(), "user_info")
        except UpstreamError:
            self._account_expires = now + 120
            return self._account
        info = data.get("user_info") if isinstance(data, dict) else None
        if not isinstance(info, dict):
            self._account_expires = now + 120
            return self._account
        def text(key: str) -> str:
            return _clean_text(info.get(key))[:40]
        server = data.get("server_info") if isinstance(data.get("server_info"), dict) else {}
        tz = _clean_text(server.get("timezone"))[:64]
        exp = text("exp_date")
        self._account = {
            "status": text("status"),
            "exp_date": int(exp) if exp.isdigit() else None,
            "max_connections": text("max_connections"),
            "active_connections": text("active_cons"),
            "is_trial": text("is_trial") in ("1", "true"),
            "timezone": tz,
        }
        self._account_expires = now + 600
        return self._account


def order_entries(entries: list[tuple[dict, str]], categories: dict[str, str],
                  group_order: list[str] | None = None) -> list[tuple[dict, str]]:
    """Trie par groupe (ordre personnalisé, sinon ordre du fournisseur) en gardant l'ordre des chaînes."""
    provider_rank = {cid: i for i, cid in enumerate(categories)}
    custom_rank = {cid: i for i, cid in enumerate(group_order or [])}
    big = len(custom_rank)

    def key(item: tuple[int, tuple[dict, str]]):
        index, (_, gid) = item
        if gid in custom_rank:
            return (0, custom_rank[gid], 0, index)
        return (1 if custom_rank else 0, big, provider_rank.get(gid, len(provider_rank)), index)

    return [entry for _, entry in sorted(enumerate(entries), key=key)]


def redact(value: object, config: Config) -> object:
    """Copie profonde d'une réponse brute du fournisseur sans identifiants (export JSON)."""
    user, password = config.username, config.password
    patterns = [f"/{user}/{password}/", f"/{quote(user, safe='')}/{quote(password, safe='')}/"]
    if isinstance(value, dict):
        return {k: ("***" if str(k).lower() in ("username", "password") else redact(v, config))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, config) for v in value]
    if isinstance(value, str):
        for pattern in patterns:
            value = value.replace(pattern, "/***/***/")
        value = re.sub(r"(?i)\b(username|password)=[^&#\s]*", r"\1=***", value)
        if len(password) >= 4:
            value = value.replace(password, "***")
        return value
    return value


def escape_attribute(value: object) -> str:
    """Sanitize M3U attributes to prevent line/quote injection."""
    return str(value or "").replace("\r", " ").replace("\n", " ").replace('"', "'").strip()


def escape_title(value: object) -> str:
    return str(value or "Sans nom").replace("\r", " ").replace("\n", " ").strip() or "Sans nom"


def with_token(url: str, config: Config) -> str:
    if not config.access_token:
        return url
    return url + ("&" if "?" in url else "?") + urlencode({"token": config.access_token})


def playlist(config: Config, entries: list[tuple[dict, str]], categories: dict[str, str], base: str,
             *, mode: str = "proxy", fmt: str = "ts", epg_path: str | None = "/epg.xml",
             group_names: dict[str, str] | None = None, api_epg_ids: bool = False,
             live_format: str = "ts", catchup: bool = True) -> bytes:
    """Build an M3U (extended) document.

    ``entries`` = ``[(élément, clé_de_groupe), ...]`` où l'élément est une chaîne (``kind`` absent ou ``live``),
    un film (``movie``) ou un épisode (``episode``) ; ``epg_path`` = chemin local du guide XMLTV.
    """
    header = "#EXTM3U"
    if config.enable_epg and epg_path:
        epg = escape_attribute(with_token(f"{base}{epg_path}", config))
        header += f' url-tvg="{epg}" x-tvg-url="{epg}"'
    lines = [header]
    for channel, group_id in entries:
        stream_id = channel["stream_id"]
        name = escape_title(channel.get("name"))
        group = ((group_names or {}).get(group_id) or categories.get(group_id)
                 or ("Sans catégorie" if group_id == UNCATEGORIZED else "TV"))
        attrs = []
        if channel.get("epg_id"):
            attrs.append(f'tvg-id="{escape_attribute(channel["epg_id"])}"')
        elif api_epg_ids and channel.get("kind", "live") == "live":
            # Pas d'identifiant EPG fournisseur : identifiant du guide complété par l'API.
            attrs.append(f'tvg-id="{escape_attribute(xml_channel_id(channel))}"')
        attrs.append(f'tvg-name="{escape_attribute(name)}"')
        if channel.get("logo"):
            attrs.append(f'tvg-logo="{escape_attribute(channel["logo"])}"')
        kind = channel.get("kind", "live")
        if channel.get("num") and kind == "live":
            attrs.append(f'tvg-chno="{channel["num"]}"')
        if catchup and kind == "live" and channel.get("archive") and mode == "proxy":
            # Replay : le lecteur remplace {utc}/{utcend} par le début et la fin du programme (secondes UTC).
            source = with_token(f"{base}/timeshift/{stream_id}.ts?start={{utc}}&end={{utcend}}", config)
            attrs.append(f'catchup="default" catchup-days="{channel["archive"]}" '
                         f'catchup-source="{escape_attribute(source)}" tvg-rec="{channel["archive"]}"')
        attrs.append(f'group-title="{escape_attribute(group)}"')
        lines.append("#EXTINF:-1 " + " ".join(attrs) + "," + name)
        path, ext = {"movie": ("movie", channel.get("ext", "mp4")),
                     "episode": ("series", channel.get("ext", "mp4"))}.get(kind, ("live", fmt))
        if mode == "proxy":
            if path == "live":
                local = f"hls/{stream_id}.m3u8" if live_format == "hls" else f"stream/{stream_id}.ts"
            else:
                local = f"{path}/{stream_id}.{ext}"
            target = with_token(f"{base}/{local}", config)
        else:
            target = (
                f"{config.xtream_url}/{path}/"
                f"{quote(config.username, safe='')}/{quote(config.password, safe='')}/"
                f"{stream_id}.{ext}"
            )
        lines.append(target)
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_content(raw: str | None, default: tuple[str, ...]) -> tuple[str, ...]:
    """``?content=live,vod,series`` (ou ``all``) -> tuple ordonnée de types ; ValueError si inconnu."""
    if not raw:
        return default
    if raw == "all":
        return ("live", "vod", "series")
    wanted = {part.strip() for part in raw.split(",") if part.strip()}
    if not wanted or not wanted <= set(KINDS):
        raise ValueError("content doit valoir live, vod, series (séparés par des virgules) ou all")
    return tuple(kind for kind in KINDS if kind in wanted)


def playlists_live_ids(store: PlaylistStore, channels: list[dict], limit: int) -> set[str]:
    """Chaînes du direct présentes dans au moins une playlist personnalisée (au plus ``limit``)."""
    try:
        playlists = store.list()
    except RuntimeError:
        return set()
    ids: list[str] = []
    seen: set[str] = set()
    for saved in playlists:
        for channel, _ in filter_channels(channels, saved):
            if channel["stream_id"] not in seen:
                seen.add(channel["stream_id"])
                ids.append(channel["stream_id"])
    return set(ids[:limit])


def collect_entries(api: XtreamAPI, series: "SeriesCache", saved: dict, kinds: tuple[str, ...], *,
                    refresh: bool = False, budget: float = 20) -> tuple[list[tuple[dict, str]], dict[str, str], int]:
    """Éléments d'une playlist personnalisée, tous types confondus.

    Retourne ``(entrées, {clé_de_groupe: nom}, séries_incomplètes)``. Un type dont le catalogue est
    indisponible est ignoré (journalisé) tant qu'au moins un autre type a pu être chargé.
    """
    entries: list[tuple[dict, str]] = []
    groups: dict[str, str] = {}
    incomplete = 0
    errors: list[UpstreamError] = []
    attempted = 0
    for kind in kinds:
        if not uses(saved, kind):
            continue
        attempted += 1
        prefix = GROUP_PREFIX[kind]
        try:
            items, categories = api.catalog(refresh=refresh, kind=kind)
        except UpstreamError as exc:
            LOG.warning("Catalogue %s indisponible pour la playlist %s", kind, saved.get("slug"))
            errors.append(exc)
            if kind == "series":
                incomplete += len(saved.get("series_category_ids", [])) + len(saved.get("included_series_ids", []))
            continue
        for cid, name in categories.items():
            groups[prefix + cid] = name
        selected = filter_kind(items, saved, kind, prefix)
        if kind != "series":
            entries.extend(selected)
            continue
        episodes, missing = series.episodes_for([item["stream_id"] for item, _ in selected], budget=budget)
        incomplete += len(missing)
        for show, group in selected:
            for ep in episodes.get(show["stream_id"], []):
                entries.append(({
                    "kind": "episode", "stream_id": ep["id"], "name": episode_name(show["name"], ep),
                    "logo": show["logo"] or ep["logo"], "ext": ep["ext"], "epg_id": "", "num": "",
                }, group))
    if attempted and len(errors) == attempted:
        raise errors[-1]
    return entries, groups, incomplete


class Handler(BaseHTTPRequestHandler):
    server_version = f"XtreamM3ULocal/{__version__}"
    sys_version = ""
    # Ferme les connexions inactives : un lecteur en pause ne bloque pas
    # indéfiniment la connexion fournisseur (souvent limitée à 1).
    timeout = 120
    api: XtreamAPI
    store: PlaylistStore
    config: Config
    epg: EpgCache
    series: SeriesCache
    streams: StreamManager
    hls: HlsRegistry
    request_token: str = ""

    def log_message(self, format: str, *args: object) -> None:
        # Ne jamais journaliser la query string (ACCESS_TOKEN).
        LOG.info("%s %s %s", self.client_address[0], self.command, urlsplit(self.path).path)

    def log_error(self, format: str, *args: object) -> None:
        LOG.debug("HTTP error: " + format, *args)

    def do_HEAD(self) -> None:
        self.handle_request(head_only=True)

    def do_GET(self) -> None:
        self.handle_request(head_only=False)

    def response(self, status: int, content: bytes, content_type: str, *, head_only: bool,
                 filename: str | None = None, extra: dict[str, str] | None = None,
                 cacheable: bool = False) -> None:
        """Réponse complète ; ``cacheable`` = ETag + 304 (playlists), gzip si le client l'accepte."""
        etag = etag_of(content) if cacheable and status == 200 else None
        if etag and etag_matches(self.headers, etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        body, encoding = encode_body(self.headers, content, content_type)
        self.send_response(status)
        for key, value in {**(extra or {}), **encoding}.items():
            self.send_header(key, value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache" if etag else "no-store")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _error(self, status: int, message: str, *, head_only: bool) -> None:
        self.response(status, (message + "\n").encode(), "text/plain; charset=utf-8", head_only=head_only)

    def send_file(self, path: Path, content_type: str, *, head_only: bool, filename: str) -> None:
        try:
            handle = path.open("rb")
        except OSError:
            return self._error(502, "Fichier indisponible", head_only=head_only)
        with handle:
            stat = os.fstat(handle.fileno())
            size = stat.st_size
            etag = f'"{stat.st_mtime_ns:x}-{size:x}"'
            if etag_matches(self.headers, etag):
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            compress = size >= 64 * 1024 and accepts_gzip(self.headers)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if compress:
                # Taille compressée inconnue à l'avance : fin de réponse = fermeture de la connexion.
                self.send_header("Content-Encoding", "gzip")
                self.close_connection = True
            else:
                self.send_header("Content-Length", str(size))
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.end_headers()
            if head_only:
                return
            try:
                if compress:
                    gzip_stream(handle, self.wfile.write)
                else:
                    shutil_copy(handle, self.wfile)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                self.close_connection = True

    def authorized(self, qs: dict[str, list[str]]) -> bool:
        expected = self.config.access_token
        if not expected:
            return True
        received = qs.get("token", [""])[0]
        return hmac.compare_digest(received.encode(), expected.encode())

    def public_base(self) -> str:
        if self.config.public_base_url:
            return self.config.public_base_url
        host = self.headers.get("Host", "")
        if not host or not HOST_RE.fullmatch(host):
            raise ValueError("En-tête Host invalide : définir PUBLIC_BASE_URL")
        return "http://" + host

    def open_session(self, kind: str, item_id: str, label: str, *, virtual: bool = False, provider: str = "1"):
        """Réserve une connexion fournisseur. Retourne la session, ou ``None`` après avoir répondu (409/503)."""
        try:
            return self.streams.acquire(kind=kind, item_id=item_id, client=self.client_address[0],
                                        label=label, virtual=virtual, provider=provider)
        except EvictedRecently:
            self._error(409, "Lecture arrêtée : la connexion au fournisseur a été donnée à un autre appareil "
                             "(réessaie dans 30 secondes ou choisis une autre chaîne)", head_only=False)
        except LimitReached as exc:
            self._error(503, f"Limite de {exc.args[0]} connexion(s) simultanée(s) atteinte : arrête une autre "
                             "lecture (interface Web, onglet Système)", head_only=False)
        return None

    def item_label(self, kind: str, item_id: str) -> str:
        catalog_kind = {"movie": "vod", "timeshift": "live", "hls": "live"}.get(kind, kind)
        if catalog_kind in KINDS:
            item = self.api.lookup(catalog_kind, item_id)
            if item:
                return item["name"]
        return {"series": "Épisode", "movie": "Film"}.get(kind, "Chaîne") + f" {item_id}"

    def relay(self, upstream_url: str, *, head_only: bool, default_type: str = "video/mp2t",
              track: tuple[str, str] | None = None, touch=None) -> None:
        """Relaie un flux fournisseur. ``track=(type, id)`` : lecture comptée dans la limite de connexions."""
        session = None
        if track and not head_only:
            session = self.open_session(track[0], track[1], self.item_label(*track))
            if session is None:
                return
        headers = {"User-Agent": self.config.user_agent, "Accept": "*/*"}
        for h in ("Range", "If-Range"):
            if self.headers.get(h):
                headers[h] = self.headers[h]
        request = Request(upstream_url, headers=headers)
        headers_sent = False
        try:
            with urlopen(request, timeout=45) as upstream:
                if session is not None:
                    session.attach(upstream.close)
                self.send_response(upstream.status)
                for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges",
                            "Last-Modified", "ETag"):
                    value = upstream.headers.get(key)
                    if value:
                        self.send_header(key, value)
                if not upstream.headers.get("Content-Type"):
                    self.send_header("Content-Type", default_type)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                headers_sent = True
                if head_only:
                    return
                reader = getattr(upstream, "read1", upstream.read)
                while True:
                    if session is not None and session.cancelled.is_set():
                        break
                    try:
                        chunk = reader(64 * 1024)
                    except Exception:
                        if session is not None and session.cancelled.is_set():
                            break  # connexion fermée depuis un autre thread (coupure volontaire)
                        raise
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, socket.timeout):
                        break
                    if session is not None:
                        session.touch(len(chunk))
                    elif touch is not None:
                        touch.touch(len(chunk))
        except HTTPError as exc:
            LOG.warning("Upstream stream error status=%s", exc.code)
            if not headers_sent:
                hint = " (limite de connexions simultanées atteinte ?)" if exc.code in (403, 429, 458, 503, 509) else ""
                self._error(502, f"Le fournisseur a renvoyé HTTP {exc.code}{hint}", head_only=head_only)
            else:
                self.close_connection = True
        except (URLError, TimeoutError, OSError, HTTPException, ValueError):
            LOG.warning("Upstream stream network failure")
            if not headers_sent:
                try:
                    self._error(502, "Flux fournisseur inaccessible", head_only=head_only)
                except (OSError, ValueError):
                    pass
            else:
                self.close_connection = True
        finally:
            if session is not None:
                self.streams.release(session)
            if headers_sent:
                self.close_connection = True

    def handle_timeshift(self, stream_id: str, qs: dict[str, list[str]], *, head_only: bool) -> None:
        """Replay : ``/timeshift/<id>.ts?start=<epoch>&end=<epoch>`` (ou ``&duration=<minutes>``)."""
        try:
            start = int(qs.get("start", [""])[0])
            if qs.get("end", [""])[0]:
                minutes = -(-(int(qs["end"][0]) - start) // 60)
            else:
                minutes = int(qs.get("duration", [""])[0])
        except ValueError:
            return self._error(400, "Paramètres attendus : start=<horodatage> et end=<horodatage> (ou duration=<minutes>)",
                               head_only=head_only)
        if not 946684800 <= start <= 4102444800 or not 1 <= minutes <= 24 * 60:
            return self._error(400, "Période de replay invalide", head_only=head_only)
        return self.relay(self.api.timeshift_url(stream_id, start, minutes), head_only=head_only,
                          track=("timeshift", stream_id))

    def token_suffix(self) -> str:
        return f"?{urlencode({'token': self.request_token})}" if self.request_token else ""

    def hls_manifest(self, upstream_url: str, session, *, head_only: bool) -> None:
        """Télécharge un manifeste HLS chez le fournisseur et le réécrit vers des adresses locales."""
        request = Request(upstream_url, headers={"User-Agent": self.config.user_agent, "Accept": "*/*"})
        try:
            with urlopen(request, timeout=20) as upstream:
                raw = upstream.read(MAX_MANIFEST_BYTES + 1)
                final_url = upstream.geturl()
        except HTTPError as exc:
            LOG.warning("Upstream HLS error status=%s", exc.code)
            hint = " (limite de connexions simultanées atteinte ?)" if exc.code in (403, 429, 458, 503, 509) else ""
            return self._error(502, f"Le fournisseur a renvoyé HTTP {exc.code}{hint}", head_only=head_only)
        except (URLError, TimeoutError, OSError, HTTPException, ValueError):
            LOG.warning("Upstream HLS network failure")
            return self._error(502, "Flux HLS du fournisseur inaccessible", head_only=head_only)
        if len(raw) > MAX_MANIFEST_BYTES:
            return self._error(502, "Manifeste HLS trop volumineux", head_only=head_only)
        suffix = self.token_suffix()

        def link(url: str, is_playlist: bool) -> str:
            if is_playlist:
                return f"/hls/v/{self.hls.register(url, 'p', session)}.m3u8{suffix}"
            return f"/hls/s/{self.hls.register(url, 's', session)}.{segment_ext(url)}{suffix}"

        try:
            body = rewrite_manifest(raw.decode("utf-8", "replace"), final_url, link)
        except ValueError as exc:
            return self._error(502, str(exc), head_only=head_only)
        if session is not None:
            session.touch(len(raw))
        return self.response(200, body.encode("utf-8"), "application/vnd.apple.mpegurl", head_only=head_only)

    def handle_hls(self, path: str, *, head_only: bool) -> bool:
        """Routes /hls/... ; retourne False si le chemin n'est pas une route HLS."""
        live = HLS_LIVE_RE.fullmatch(path)
        if live:
            stream_id = live.group(1)
            if head_only:
                self.response(200, b"", "application/vnd.apple.mpegurl", head_only=True)
                return True
            session = self.open_session("hls", stream_id, self.item_label("hls", stream_id), virtual=True)
            if session is not None:
                self.hls_manifest(self.api.direct_url("live", stream_id, "m3u8"), session, head_only=False)
            return True
        sub = HLS_SUB_RE.fullmatch(path)
        seg = HLS_SEG_RE.fullmatch(path)
        if not sub and not seg:
            return False
        resolved = self.hls.resolve((sub or seg).group(1), "p" if sub else "s")
        if resolved is None:
            self._error(404, "Lien HLS expiré : relance la chaîne", head_only=head_only)
            return True
        url, session = resolved
        if session is not None and session.cancelled.is_set():
            self._error(409, "Lecture arrêtée : la connexion au fournisseur a été donnée à un autre appareil",
                        head_only=head_only)
            return True
        if session is not None:
            session.touch()
        if sub:
            self.hls_manifest(url, session, head_only=head_only)
        else:
            ext = seg.group(2).lower()
            self.relay(url, head_only=head_only, default_type=SEGMENT_MIME.get(ext, "application/octet-stream"),
                       touch=session)
        return True

    def full_guide(self, *, refresh: bool = False, budget: float = 15) -> Path:
        """Guide de toutes les chaînes du compte.

        Mode ``xmltv`` (ou catalogue indisponible) : XMLTV fournisseur tel quel. Sinon : XMLTV limité aux
        chaînes du compte, complété par l'API pour les chaînes des playlists personnalisées
        (au plus ``EPG_API_MAX_CHANNELS``) afin de ne pas lancer des milliers d'appels.
        """
        if self.config.epg_source == "xmltv":
            return self.epg.ensure(refresh=refresh)
        try:
            channels, _ = self.api.catalog()
        except UpstreamError:
            return self.epg.ensure(refresh=refresh)
        path, _ = self.epg.playlist_guide("_all", channels, budget=budget, refresh=refresh,
                                          api_ids=playlists_live_ids(self.store, channels,
                                                                     self.config.epg_api_max_channels))
        return path

    def handle_request(self, *, head_only: bool) -> None:
        parts = urlsplit(self.path)
        qs = parse_qs(parts.query, keep_blank_values=True)
        path = parts.path
        refresh = qs.get("refresh", [""])[0] == "1"
        if path == "/health":
            return self.response(HTTPStatus.OK, b"OK\n", "text/plain; charset=utf-8", head_only=head_only)
        if path == "/":
            text = (
                f"Xtream M3U Local {__version__}\n"
                "Playlist complete : /playlist.m3u\n"
                "Playlists personnalisees : /p/<nom>.m3u (gestion sur le port de l'interface Web)\n"
            )
            return self.response(HTTPStatus.OK, text.encode(), "text/plain; charset=utf-8", head_only=head_only)
        if not self.authorized(qs):
            return self._error(HTTPStatus.FORBIDDEN, "Token requis ou invalide (?token=...)", head_only=head_only)
        self.request_token = qs.get("token", [""])[0] if self.config.access_token else ""
        if path.startswith("/hls/") and self.handle_hls(path, head_only=head_only):
            return
        if path in ("/playlist.m3u", "/get.php"):
            mode = qs.get("mode", ["proxy"])[0]
            fmt = qs.get("format", ["ts"])[0]
            if mode not in ("proxy", "direct") or fmt not in ("ts", "m3u8"):
                return self._error(400, "mode=proxy|direct et format=ts|m3u8", head_only=head_only)
            try:
                kinds = parse_content(qs.get("content", [""])[0], ("live",))
                if "series" in kinds:
                    raise ValueError("Les séries ne sont pas proposées dans la playlist complète "
                                     "(un appel par série) : crée une playlist personnalisée")
                base = self.public_base()
                entries = []
                groups: dict[str, str] = {}
                for kind in kinds:
                    items, categories = self.api.catalog(refresh=refresh, kind=kind)
                    prefix = GROUP_PREFIX[kind]
                    groups.update({prefix + cid: name for cid, name in categories.items()})
                    entries.extend((c, prefix + c["category_id"]) for c in items)
                entries = order_entries(entries, groups)
                content = playlist(self.config, entries, groups, base, mode=mode, fmt=fmt,
                                   epg_path="/epg.xml" if "live" in kinds else None,
                                   api_epg_ids=self.config.enable_epg and self.config.epg_source != "xmltv",
                                   live_format="hls" if fmt == "m3u8" else "ts")
            except ValueError as exc:
                return self._error(400, str(exc), head_only=head_only)
            except UpstreamError as exc:
                LOG.warning("Cannot build playlist: %s", str(exc))
                return self._error(502, str(exc), head_only=head_only)
            return self.response(HTTPStatus.OK, content, "audio/x-mpegurl; charset=utf-8",
                                 head_only=head_only, filename="playlist.m3u", cacheable=True)
        custom_match = CUSTOM_RE.fullmatch(path)
        if custom_match:
            slug, ext = custom_match.groups()
            if ext == "xml" and not self.config.enable_epg:
                return self._error(404, "EPG désactivé (ENABLE_EPG=false)", head_only=head_only)
            try:
                saved = self.store.get(slug)
            except RuntimeError:
                return self._error(500, "Sauvegarde des playlists illisible", head_only=head_only)
            if saved is None:
                return self._error(404, "Playlist introuvable", head_only=head_only)
            incomplete = 0
            try:
                if ext == "xml":
                    channels, _ = self.api.catalog(refresh=refresh)
                    selected = [c for c, _ in filter_channels(channels, saved)]
                    path_out, _ = self.epg.playlist_guide(
                        slug, selected, budget=0 if head_only else self.config.epg_fetch_seconds)
                    return self.send_file(path_out, "application/xml; charset=utf-8",
                                          head_only=head_only, filename=f"{slug}.xml")
                kinds = parse_content(qs.get("content", [""])[0], tuple(KINDS))
                entries, groups, incomplete = collect_entries(
                    self.api, self.series, saved, kinds, refresh=refresh,
                    budget=0 if head_only else self.config.series_fetch_seconds)
                entries = order_entries(entries, groups, saved.get("group_order"))
                has_live = "live" in kinds and uses(saved, "live")
                fmt = qs.get("format", [""])[0]
                if fmt not in ("", "ts", "m3u8"):
                    raise ValueError("format=ts|m3u8")
                live_format = {"ts": "ts", "m3u8": "hls"}.get(fmt, saved.get("live_format", "ts"))
                content = playlist(self.config, entries, groups, self.public_base(),
                                   epg_path=f"/p/{slug}.xml" if has_live else None,
                                   group_names=saved.get("group_names"),
                                   api_epg_ids=self.config.enable_epg and self.config.epg_source != "xmltv",
                                   live_format=live_format, catchup=saved.get("catchup", True))
            except EpgUnavailable as exc:
                return self._error(502, str(exc), head_only=head_only)
            except ValueError as exc:
                return self._error(400, str(exc), head_only=head_only)
            except UpstreamError as exc:
                LOG.warning("Cannot build custom playlist: %s", str(exc))
                return self._error(502, f"Impossible de générer la playlist : {exc}", head_only=head_only)
            if incomplete:
                LOG.info("Playlist %s : épisodes de %d série(s) encore en chargement", slug, incomplete)
            return self.response(200, content, "audio/x-mpegurl; charset=utf-8", head_only=head_only,
                                 filename=f"{slug}.m3u", cacheable=not incomplete,
                                 extra={"X-Series-Pending": str(incomplete)} if incomplete else None)
        shift = TIMESHIFT_RE.fullmatch(path)
        if shift:
            return self.handle_timeshift(shift.group(1), qs, head_only=head_only)
        vod = VOD_RE.fullmatch(path)
        if vod:
            kind, item_id, ext = vod.group(1), vod.group(2), vod.group(3).lower()
            # Films et épisodes : Range transmis (avance rapide) ; HEAD relayé pour la taille du fichier.
            return self.relay(self.api.direct_url(kind, item_id, ext), head_only=head_only,
                              default_type=mime_for(ext), track=(kind, item_id))
        match = STREAM_RE.fullmatch(path)
        if match:
            if head_only:
                # Ne pas consommer une connexion fournisseur pour une simple sonde HEAD.
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self.relay(self.api.direct_stream(match.group(1)), head_only=False,
                              track=("live", match.group(1)))
        if path in ("/epg.xml", "/xmltv.php") and self.config.enable_epg:
            # /xmltv.php : adresse « à la Xtream » que certains lecteurs utilisent d'office pour le guide
            # (les paramètres username/password éventuels sont ignorés).
            try:
                guide = self.full_guide(refresh=refresh, budget=0 if head_only else self.config.epg_fetch_seconds)
            except EpgUnavailable as exc:
                return self._error(502, str(exc), head_only=head_only)
            return self.send_file(guide, "application/xml; charset=utf-8", head_only=head_only,
                                  filename="epg.xml")
        return self._error(404, "Page introuvable", head_only=head_only)


def shutil_copy(src, dst, size: int = 256 * 1024) -> None:
    while True:
        chunk = src.read(size)
        if not chunk:
            break
        dst.write(chunk)


def make_epg(config: Config, api: XtreamAPI) -> EpgCache:
    """Guide XMLTV (``EPG_URL`` ou ``xmltv.php``) + programmes par chaîne via l'API (mode auto/api)."""
    url_factory = (lambda: config.epg_url) if config.epg_url else api.epg_url
    epg = EpgCache(url_factory, config.data_dir, max_age=config.epg_cache_hours * 3600,
                   user_agent=config.user_agent)
    epg.mode = config.epg_source
    if config.epg_source != "xmltv":
        epg.api_cache = KeyedCache(api.simple_epg, config.data_dir, max_age=6 * 3600,
                                   normalize=normalize_listings, subdir="epg", label="EPG de la chaîne")
    return epg


def make_streams(config: Config, api: XtreamAPI) -> StreamManager:
    def limit_for(_provider: str) -> int:
        return config.max_connections if config.max_connections >= 0 else api.max_connections_hint()
    return StreamManager(limit_for, takeover=config.stream_takeover)


def make_series(config: Config, api: XtreamAPI) -> SeriesCache:
    return SeriesCache(api.series_info, config.data_dir, max_age=config.series_cache_hours * 3600)


def make_server(config: Config, *, host: str = "0.0.0.0", port: int | None = None,
                api: XtreamAPI | None = None, store: PlaylistStore | None = None,
                epg: EpgCache | None = None, series: SeriesCache | None = None,
                streams: StreamManager | None = None, hls: HlsRegistry | None = None) -> ThreadingHTTPServer:
    api = api or XtreamAPI(config)
    store = store or PlaylistStore(config.data_dir)
    epg = epg or make_epg(config, api)
    series = series or make_series(config, api)
    streams = streams or make_streams(config, api)
    handler = type("ConfiguredHandler", (Handler,),
                   {"api": api, "config": config, "store": store, "epg": epg, "series": series,
                    "streams": streams, "hls": hls or HlsRegistry()})
    server = ThreadingHTTPServer((host, config.port if port is None else port), handler)
    server.daemon_threads = True
    return server


def prepare_data_dir(data_dir: str) -> None:
    """Rend ``DATA_DIR`` inscriptible puis abandonne les droits root (PUID/PGID, 99:100 par défaut).

    Sur Unraid, un dossier ``/mnt/user/appdata/...`` créé par Docker appartient à root :
    sans ce correctif l'utilisateur non privilégié ne pourrait pas y écrire.
    """
    path = Path(data_dir)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        uid = _env_int("PUID", 99)
        gid = _env_int("PGID", 100)
        try:
            path.mkdir(parents=True, exist_ok=True)
            for item in [path, *path.rglob("*")]:
                try:
                    os.lchown(item, uid, gid)
                except OSError:
                    LOG.warning("Impossible de changer le propriétaire de %s", item)
        except OSError:
            LOG.warning("Impossible de préparer %s", path)
        if uid != 0:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)
            LOG.info("Droits abandonnés : UID %d / GID %d", uid, gid)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError:
        LOG.error("%s n'est pas inscriptible : les playlists ne pourront pas être enregistrées "
                  "(vérifie le volume et les droits, UID %s)", path, os.getuid() if hasattr(os, "getuid") else "?")


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
    except ValueError as exc:
        LOG.error("Configuration invalide : %s", exc)
        raise SystemExit(2) from None
    prepare_data_dir(config.data_dir)
    from .web import make_web_server
    api = XtreamAPI(config)
    store = PlaylistStore(config.data_dir)
    epg = make_epg(config, api)
    series = make_series(config, api)
    streams = make_streams(config, api)
    server = make_server(config, api=api, store=store, epg=epg, series=series, streams=streams)
    web = make_web_server(config, api, store, epg=epg, series=series, streams=streams)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    worker.start()
    LOG.info("Xtream M3U Local %s : playlists sur le port %d, interface Web sur le port %d",
             __version__, config.port, config.web_port)
    if not config.web_password:
        LOG.info("Interface Web sans mot de passe (WEB_PASSWORD vide) : réservé à un réseau local de confiance")
    # `docker stop` envoie SIGTERM au PID 1 : arrêt propre et immédiat.
    signal.signal(signal.SIGTERM, _on_sigterm)
    # Préchargement du catalogue pour que la première ouverture soit rapide.
    threading.Thread(target=_warmup, args=(api,), daemon=True).start()
    try:
        web.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        web.server_close()
        server.shutdown()
        server.server_close()


def _on_sigterm(*_args: object) -> None:
    raise KeyboardInterrupt


def _warmup(api: XtreamAPI) -> None:
    """Précharge les catalogues (direct d'abord, puis films et séries) pour une interface rapide."""
    api.account()  # limite de connexions de l'abonnement
    for kind in KINDS:
        try:
            api.catalog(kind=kind)
        except UpstreamError as exc:
            LOG.warning("Catalogue %s non chargé au démarrage : %s", kind, exc)


if __name__ == "__main__":
    main()
