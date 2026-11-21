"""Generate local M3U playlists from the Xtream player API (port 7777).

Bibliothèque standard Python uniquement, y compris pour le relais des flux TS.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from . import __version__, access, logos, provider_store, settings
from .httputil import accepts_gzip, encode_body, etag_matches, etag_of, gzip_stream
from .builder import FAVORITES_GROUP, GROUP_PREFIX, add_favorites, display_logo, select_items
from .devices import DeviceStore
from .providers import UnknownProvider
from .hls import MAX_MANIFEST_BYTES, SEGMENT_MIME, HlsRegistry, rewrite_manifest, segment_ext
from .streams import EvictedRecently, LimitReached, StreamManager
from .custom import CATEGORY_RE, UNCATEGORIZED, PlaylistStore, filter_channels, filter_kind, uses
from .epg import EPG_MODES, EpgCache, EpgUnavailable, normalize_listings, xml_channel_id
from .xtream_emu import EMPTY_AUTH, XtreamEmulator
from .vod import KeyedCache, SeriesCache, episode_name, mime_for, normalize_movie, normalize_series

LOG = logging.getLogger("xtream-m3u-local")

_REFUSED_LOCK = threading.Lock()
_REFUSED_LAST: dict[str, float] = {}
_LOGIN_LAST: dict[str, float] = {}


def note_refused_login(ip: str, username: str) -> None:
    """Identifiants d'appareil refusés (Xtream local) : une ligne par adresse et par minute."""
    now = time.monotonic()
    with _REFUSED_LOCK:
        if now - _REFUSED_LAST.get(ip, -1e9) < 60:
            return
        _REFUSED_LAST[ip] = now
    LOG.warning("Xtream local : identifiants refusés depuis %s (utilisateur « %s ») — appareil supprimé ou code "
                "mal saisi : vérifie dans « Appareils » l'utilisateur et le mot de passe de cet appareil",
                ip, (username or "")[:40] or "vide")


def log_device_login(emu, device: dict, ip: str) -> None:
    """Connexion d'un appareil (Xtream local) : sa playlist et ce qu'elle contient, une ligne par 10 min."""
    now = time.monotonic()
    with _REFUSED_LOCK:
        if now - _LOGIN_LAST.get(device["id"], -1e9) < 600:
            return
        _LOGIN_LAST[device["id"]] = now
    try:
        saved = emu.saved_for(device)
        counts = {kind: len(emu.entries(device, kind)[0]) for kind in KINDS}
    except Exception as exc:  # le journal ne doit jamais casser la connexion
        LOG.warning("Appareil « %s » connecté depuis %s : contenu illisible (%s)", device.get("name"), ip,
                    type(exc).__name__)
        return
    message = ("Appareil « %s » connecté depuis %s : playlist « %s », profil %s — %d chaîne(s), %d film(s), "
               "%d série(s)")
    args = (device.get("name"), ip, saved.get("name") or "toutes", saved.get("provider") or "1",
            counts["live"], counts["vod"], counts["series"])
    (LOG.warning if not any(counts.values()) else LOG.info)(
        message + (" : playlist vide pour cet appareil (catégories cochées ? bon profil ?)" if not any(counts.values()) else ""),
        *args)
DEFAULT_USER_AGENT = f"XtreamM3ULocal/{__version__}"
STREAM_RE = re.compile(r"^/stream/([0-9]{1,12})\.ts$")
HLS_LIVE_RE = re.compile(r"^/hls/([0-9]{1,12})\.m3u8$")
HLS_SUB_RE = re.compile(r"^/hls/v/([A-Za-z0-9_-]{16,32})\.m3u8$")
HLS_SEG_RE = re.compile(r"^/hls/s/([A-Za-z0-9_-]{16,32})\.([A-Za-z0-9]{1,5})$")
XC_STREAM_RE = re.compile(r"^/(live|movie|series)/([A-Za-z0-9_.-]{1,64})/([A-Za-z0-9_-]{1,64})/([0-9]{1,12})\.([A-Za-z0-9]{1,5})$")
XC_TIMESHIFT_RE = re.compile(r"^/timeshift/([A-Za-z0-9_.-]{1,64})/([A-Za-z0-9_-]{1,64})/([0-9]{1,4})/"
                             r"([0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9]{2}-[0-9]{2})/([0-9]{1,12})\.ts$")
START_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9]{2}-[0-9]{2}$")
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


def _extra_providers() -> tuple:
    """Fournisseurs supplémentaires : XTREAM_URL_2 / XTREAM_USERNAME_2 / XTREAM_PASSWORD_2 / XTREAM_NAME_2 … _9."""
    extras = []
    for index in range(2, 10):
        raw_url = os.environ.get(f"XTREAM_URL_{index}", "").strip()
        if not raw_url:
            continue
        user = os.environ.get(f"XTREAM_USERNAME_{index}", "").strip()
        password = os.environ.get(f"XTREAM_PASSWORD_{index}", "").strip()
        if not user or not password:
            raise ValueError(f"XTREAM_USERNAME_{index} et XTREAM_PASSWORD_{index} sont obligatoires avec XTREAM_URL_{index}")
        name = os.environ.get(f"XTREAM_NAME_{index}", "").strip()[:40] or f"Fournisseur {index}"
        extras.append((name, normalize_xtream_url(raw_url), user, password))
    return tuple(extras)


# Le fournisseur refuse une connexion de plus (abonnement déjà utilisé ailleurs) : 429, 458, 509.
BUSY_CODES = (429, 458, 509)
BUSY_PAUSE = 30     # secondes sans redemander le fournisseur après un refus (réessais rapides des lecteurs)

CALM_AFTER_429 = 600      # catalogue en cache gardé (pas de rafraîchissement) 10 min après un 429
EPG_PRUDENT_SECONDS = 3600  # guide « chaîne par chaîne » suspendu 1 h après un 429 (fournisseur sensible)
EPG_STARTUP_QUIET = 120   # aucun appel « guide par chaîne » pendant les 2 premières minutes

SLOW_WARMUP = 25    # secondes ignorées au démarrage d'une version (rafale de remplissage du tampon)
SLOW_WINDOWS = 3    # fenêtres de 10 s lentes d'affilée avant de basculer


def _trusted_proxies() -> tuple:
    try:
        return access.parse_networks(os.environ.get("TRUSTED_PROXIES", ""))
    except ValueError:
        raise ValueError("TRUSTED_PROXIES : adresses ou réseaux attendus, ex. 172.18.0.0/16,192.168.1.2") from None


def _nightly_refresh() -> tuple | None:
    """``NIGHTLY_REFRESH`` : « 3-6 », « 03:00-06:00 » ou « 23-5 » (heure locale du conteneur) ; vide/off = toujours."""
    raw = os.environ.get("NIGHTLY_REFRESH", "3-6").strip().lower()
    if raw in ("", "0", "off", "false", "non"):
        return None
    found = re.fullmatch(r"(\d{1,2})(?::00)?\s*-\s*(\d{1,2})(?::00)?", raw)
    if not found or not all(0 <= int(h) <= 24 for h in found.groups()) or found.group(1) == found.group(2):
        raise ValueError("NIGHTLY_REFRESH doit être une plage d'heures, ex. 3-6 ou 03:00-06:00 (vide = désactivé)")
    return int(found.group(1)) % 24, int(found.group(2)) % 24


def in_nightly_window(config, now: float | None = None) -> bool:
    """Vrai pendant la fenêtre de mise à jour de nuit (ou toujours si elle est désactivée)."""
    if not config.nightly_refresh:
        return True
    start, end = config.nightly_refresh
    hour = time.localtime(now if now is not None else time.time()).tm_hour
    return start <= hour < end if start < end else (hour >= start or hour < end)


def _epg_extra_urls() -> tuple:
    urls = tuple(u for u in re.split(r"[\s,;]+", os.environ.get("EPG_EXTRA_URLS", "").strip()) if u)
    for url in urls:
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
            raise ValueError("EPG_EXTRA_URLS : adresses http(s) de guides XMLTV (.xml, .xml.gz ou .zip) attendues")
    return urls[:5]


def _local_networks() -> tuple:
    try:
        return access.parse_networks(os.environ.get("LOCAL_NETWORKS", ""))
    except ValueError:
        raise ValueError("LOCAL_NETWORKS : réseaux attendus, ex. 100.64.0.0/10 (Tailscale)") from None


def _web_remote() -> bool:
    enabled = _env_bool("WEB_REMOTE", False)
    if enabled and not os.environ.get("WEB_PASSWORD", ""):
        raise ValueError("WEB_REMOTE=true exige WEB_PASSWORD (interface Web joignable hors du réseau local)")
    return enabled


def _failover_mode() -> str:
    value = os.environ.get("FAILOVER_MODE", "").strip().lower() or "reconnect"
    if value not in ("reconnect", "splice"):
        raise ValueError("FAILOVER_MODE doit valoir reconnect ou splice")
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
    series_cache_hours: int = 168    # épisodes gardés 7 jours (une série change peu)
    series_per_second: int = 2       # SERIES_PER_SECOND : demandes d'épisodes par seconde au plus
    nightly_refresh: tuple | None = (3, 6)  # NIGHTLY_REFRESH : heures (début, fin) des mises à jour lourdes
    epg_source: str = "auto"
    epg_url: str = ""
    epg_fetch_seconds: int = 15
    epg_api_max_channels: int = 1000
    epg_startup_quiet: int = 120     # EPG_STARTUP_QUIET : secondes sans appel « guide par chaîne » au démarrage
    epg_api_per_second: int = 2      # EPG_API_PER_SECOND : appels « guide par chaîne » par seconde (0 = sans limite)
    max_connections: int = -1        # -1 = auto (valeur de l'abonnement), 0 = illimité
    stream_takeover: bool = True
    failover_stall_seconds: float = 6.0   # bascule automatique : flux muet pendant N s → version suivante
    failover_slow_ratio: float = 0.35     # … ou débit < 35 % du meilleur débit observé (2 fenêtres de suite) ; 0 = jamais
    failover_return_minutes: int = 10     # sur une version de secours : retenter la meilleure après N min (0 = jamais)
    failover_mode: str = "reconnect"      # reconnect : couper la connexion, le lecteur se reconnecte sur la bonne version ;
                                          # splice : enchaîner les versions dans le même flux (lecteurs sans reconnexion)
    provider_timezone: str = ""      # vide = fuseau annoncé par le fournisseur (server_info.timezone)
    provider_name: str = ""          # XTREAM_NAME : nom affiché du fournisseur principal
    extra_providers: tuple = ()      # ((nom, url, utilisateur, mot de passe), …) : XTREAM_URL_2 … _9
    remote_access: bool = False      # REMOTE_ACCESS : accepter les connexions hors du réseau local (code obligatoire)
    trusted_proxies: tuple = ()      # TRUSTED_PROXIES : reverse proxys dont on lit X-Forwarded-For
    web_remote: bool = False         # WEB_REMOTE : interface Web joignable hors du réseau local (mot de passe exigé)
    epg_extra_urls: tuple = ()       # EPG_EXTRA_URLS : guides XMLTV externes (avant les appels API chaîne par chaîne)
    local_networks: tuple = ()       # LOCAL_NETWORKS : réseaux en plus considérés locaux (ex. Tailscale 100.64.0.0/10)

    @property
    def guide_ids(self) -> bool:
        """Les chaînes sans identifiant EPG reçoivent ``xtream-<id>`` : le guide de la playlist peut les compléter
        (API du fournisseur ou guide externe)."""
        return self.enable_epg and (self.epg_source != "xmltv" or bool(self.epg_extra_urls))

    @property
    def credentials(self) -> list[tuple[str, str]]:
        """Identifiants de tous les fournisseurs (pour les masquer partout)."""
        return [(self.username, self.password)] + [(u, p) for _, _, u, p in self.extra_providers]
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
            series_cache_hours=max(1, _env_int("SERIES_CACHE_HOURS", 168)),
            series_per_second=max(0, min(50, _env_int("SERIES_PER_SECOND", 2))),
            nightly_refresh=_nightly_refresh(),
            epg_source=_epg_source(),
            epg_url=_epg_url(),
            epg_fetch_seconds=max(0, _env_int("EPG_FETCH_SECONDS", 15)),
            epg_api_max_channels=max(0, _env_int("EPG_API_MAX_CHANNELS", 1000)),
            epg_api_per_second=max(0, min(50, _env_int("EPG_API_PER_SECOND", 2))),
            epg_startup_quiet=max(0, min(3600, _env_int("EPG_STARTUP_QUIET", 120))),
            max_connections=_max_connections(),
            stream_takeover=_env_bool("STREAM_TAKEOVER", True),
            failover_stall_seconds=float(min(60, max(2, _env_int("FAILOVER_STALL_SECONDS", 6)))),
            failover_slow_ratio=min(90, max(0, _env_int("FAILOVER_SLOW_PERCENT", 35))) / 100,
            failover_return_minutes=min(240, max(0, _env_int("FAILOVER_RETURN_MINUTES", 10))),
            failover_mode=_failover_mode(),
            provider_timezone=_timezone_env(),
            provider_name=os.environ.get("XTREAM_NAME", "").strip()[:40],
            extra_providers=_extra_providers(),
            series_fetch_seconds=max(0, _env_int("SERIES_FETCH_SECONDS", 20)),
            remote_access=_env_bool("REMOTE_ACCESS", False),
            trusted_proxies=_trusted_proxies(),
            web_remote=_web_remote(),
            local_networks=_local_networks(),
            epg_extra_urls=_epg_extra_urls(),
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
# peuvent coïncider avec ceux du direct ; le direct reste sans préfixe (compatibilité V2).


class _KindState:
    def __init__(self) -> None:
        self.items: list[dict] | None = None
        self.by_id: dict[str, dict] = {}
        self.raw_by_id: dict[str, dict] = {}
        self.categories: dict[str, str] = {}
        self.category_meta: dict[str, dict] = {}
        self.raw_categories: list = []
        self.raw_streams: list = []
        self.categories_error: str | None = None
        self.expires_at = 0.0
        self.retry_at = 0.0
        self.updated_at: float | None = None
        self.last_error: str | None = None
        self.loaded_at = 0.0             # time.monotonic() du dernier chargement réussi
        self.lock = threading.Lock()      # protège l'échange des données (rapide)
        self.load_lock = threading.Lock()  # un seul chargement à la fois pour ce type


class XtreamAPI:
    """Client Xtream : catalogues direct / films / séries.

    Ménage le fournisseur (souvent 1 connexion et une limite de requêtes) :
    - copie de chaque catalogue sur disque (``DATA_DIR/cache``), servie aussitôt au redémarrage ;
    - catalogue périmé servi tout de suite pendant qu'il est rafraîchi en arrière-plan ;
    - un seul gros catalogue chargé à la fois, au plus 2 appels API simultanés ;
    - après un HTTP 429 (« trop de requêtes »), aucun appel pendant la pause demandée (60 s par défaut).
    """

    RETRY_AFTER_FAILURE = 60
    PAUSE_ON_429 = 60

    def __init__(self, config: Config):
        self.config = config
        self._kinds = {kind: _KindState() for kind in KINDS}
        self._account: dict | None = None
        self._account_expires = 0.0
        self._gate = threading.BoundedSemaphore(2)   # appels API simultanés vers le fournisseur
        self._catalog_gate = threading.Lock()         # gros catalogues : l'un après l'autre
        self._paused_until = 0.0
        self.last_429 = 0.0  # heure (time.time) du dernier « 429 trop de requêtes » ; gardée sur disque
        self._refreshing: set[str] = set()
        self._refreshing_lock = threading.Lock()
        self._cache_dir = Path(config.data_dir) / "cache"
        # Identifiant stable du fournisseur (adresse + utilisateur, haché) : un cache disque par fournisseur.
        self.provider_key = provider_key(config.xtream_url, config.username)
        try:
            self.last_429 = float((self._cache_dir / f"last429-{self.provider_key}").read_text())
        except (OSError, ValueError):
            pass

    def paused_for(self) -> float:
        """Secondes restantes de pause après un « trop de requêtes » du fournisseur."""
        return max(0.0, self._paused_until - time.monotonic())

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
        wait = self.paused_for()
        if wait:
            raise UpstreamError(f"Le fournisseur limite les requêtes : pause encore {int(wait) + 1} s")
        req = Request(url, headers={"User-Agent": self.config.user_agent, "Accept": "application/json"})
        try:
            with self._gate, urlopen(req, timeout=60) as response:
                # Refuser les réponses démesurées (les catalogues VOD peuvent dépasser 50 Mo).
                raw = response.read(200 * 1024 * 1024 + 1)
                if len(raw) > 200 * 1024 * 1024:
                    raise UpstreamError("Réponse API trop volumineuse (> 200 Mo)")
        except UpstreamError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, HTTPException, ValueError) as exc:
            status = getattr(exc, "code", None)
            LOG.warning("API error for %s status=%s", label, status or "réseau")
            if status == 429:
                try:
                    pause = int((exc.headers or {}).get("Retry-After") or self.PAUSE_ON_429)
                except (TypeError, ValueError, AttributeError):
                    pause = self.PAUSE_ON_429
                pause = min(600, max(15, pause))
                self._paused_until = max(self._paused_until, time.monotonic() + pause)
                self.note_429()
                LOG.warning("Le fournisseur limite les requêtes (HTTP 429) : pause de %d s", pause)
                raise UpstreamError(f"Le fournisseur limite les requêtes (HTTP 429) : nouvel essai dans {pause} s") from None
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
        """``(éléments normalisés, {category_id: nom})`` dans l'ordre du fournisseur.

        Sans ``refresh`` : réponse immédiate dès qu'un catalogue existe (mémoire ou copie disque) ; s'il est
        périmé, le rafraîchissement part en arrière-plan. ``refresh=True`` (bouton « Actualiser ») attend.
        """
        st = self._kinds[kind]
        if st.items is None and not refresh:
            self._restore(kind)
        now = time.monotonic()
        if st.items is not None and now < st.retry_at:
            return st.items, st.categories  # échec récent : pas de nouvel essai (même avec refresh)
        if st.items is not None and not refresh:
            if now < st.expires_at:
                return st.items, st.categories
            if self.config.cache_seconds > 0:
                self._refresh_in_background(kind)
                return st.items, st.categories
        asked = time.monotonic()
        with st.load_lock:
            if st.items is not None and st.loaded_at >= asked:
                return st.items, st.categories  # chargé par une autre requête pendant l'attente
            if st.items is None and not refresh and time.monotonic() < st.retry_at:
                raise UpstreamError(st.last_error or "API Xtream indisponible")
            self._load(kind)
        return st.items, st.categories

    def note_429(self) -> None:
        """Le fournisseur a limité les requêtes : retenu (aussi après un redémarrage) pour rester prudent."""
        self.last_429 = time.time()
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            (self._cache_dir / f"last429-{self.provider_key}").write_text(str(self.last_429))
        except OSError:
            pass

    def _refresh_in_background(self, kind: str) -> None:
        st = self._kinds[kind]
        if st.items is not None and time.time() - self.last_429 < CALM_AFTER_429:
            return  # 429 récent : on garde le catalogue en cache plutôt que de solliciter le fournisseur
        if (kind != "live" and st.items is not None and not in_nightly_window(self.config)
                and time.time() - float(st.updated_at or 0) < 2 * 86400):
            return  # films et séries (gros catalogues) : mis à jour la nuit, la copie suffit en journée
        with self._refreshing_lock:
            if kind in self._refreshing:
                return
            self._refreshing.add(kind)

        def job() -> None:
            try:
                with self._kinds[kind].load_lock:
                    st = self._kinds[kind]
                    if time.monotonic() >= st.expires_at:
                        self._load(kind)
            except UpstreamError:
                pass
            finally:
                with self._refreshing_lock:
                    self._refreshing.discard(kind)

        threading.Thread(target=job, name=f"refresh-{kind}", daemon=True).start()

    def _load(self, kind: str) -> None:
        """Télécharge un catalogue (appelé sous ``load_lock``). Garde l'ancien en cas d'échec."""
        categories_action, items_action, _, _ = KINDS[kind]
        st = self._kinds[kind]
        try:
            with self._catalog_gate:
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
        except UpstreamError as exc:
            st.last_error = str(exc)
            st.retry_at = time.monotonic() + max(self.RETRY_AFTER_FAILURE, self.paused_for())
            if st.items is None:
                raise
            LOG.warning("Using stale %s catalog because upstream refresh failed", kind)
            return
        self._apply(kind, streams, categories_raw, categories_error, updated_at=time.time())
        st.expires_at = time.monotonic() + self.config.cache_seconds
        threading.Thread(target=self._save, args=(kind, streams, categories_raw, categories_error),
                         name=f"save-{kind}", daemon=True).start()

    def _apply(self, kind: str, streams: list, categories_raw: list, categories_error: str | None, *,
               updated_at: float) -> None:
        """Normalise un catalogue brut et l'installe (données du fournisseur ou copie disque)."""
        _, _, normalize, label = KINDS[kind]
        st = self._kinds[kind]
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
        id_field = "series_id" if kind == "series" else "stream_id"
        by_id = {item["stream_id"]: item for item in items}
        raw_by_id = {str(e.get(id_field)).strip(): e for e in streams if isinstance(e, dict)}
        with st.lock:
            st.category_meta = meta
            st.raw_categories = categories_raw if isinstance(categories_raw, list) else []
            st.raw_streams = streams
            st.categories_error = categories_error
            st.by_id = by_id
            st.raw_by_id = raw_by_id
            st.categories = categories
            st.items = items
            st.retry_at = 0.0
            st.updated_at = updated_at
            st.last_error = None
            st.loaded_at = time.monotonic()
        LOG.info("Catalogue %s chargé : %d %s, %d catégories", kind, len(items), label, len(categories))

    # ------------------------------------------------------- copie sur disque
    def _cache_file(self, kind: str) -> Path:
        return self._cache_dir / f"catalog-{self.provider_key}-{kind}.json.gz"

    def _save(self, kind: str, streams: list, categories_raw: list, categories_error: str | None) -> None:
        """Copie du catalogue brut (aucun identifiant : les réponses sont expurgées des champs sensibles)."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            data = {"saved_at": time.time(), "provider": self.provider_key, "streams": redact_raw(streams),
                    "categories": categories_raw, "categories_error": categories_error}
            tmp = self._cache_file(kind).with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=1) as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._cache_file(kind))
        except (OSError, TypeError, ValueError) as exc:
            LOG.warning("Copie du catalogue %s non enregistrée (%s)", kind, type(exc).__name__)

    def _restore(self, kind: str) -> None:
        """Au démarrage : reprend la dernière copie disque (marquée périmée → rafraîchie en arrière-plan)."""
        st = self._kinds[kind]
        with st.load_lock:
            if st.items is not None:
                return
            path = self._cache_file(kind)
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    data = json.load(f)
                streams, categories = data["streams"], data.get("categories") or []
                if not isinstance(streams, list):
                    raise ValueError
                if data.get("provider") not in (None, self.provider_key):
                    LOG.warning("Copie disque du catalogue %s d'un autre fournisseur : ignorée", kind)
                    return
            except FileNotFoundError:
                return
            except (OSError, ValueError, KeyError, TypeError, EOFError):
                LOG.warning("Copie disque du catalogue %s illisible : ignorée", kind)
                return
            self._apply(kind, streams, categories, data.get("categories_error"),
                        updated_at=float(data.get("saved_at") or time.time()))
            st.expires_at = 0.0  # périmée : la prochaine demande lance un rafraîchissement en arrière-plan
            LOG.info("Catalogue %s repris de la copie disque (rafraîchissement en arrière-plan)", kind)

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
        return self.timeshift_raw_url(stream_id, minutes, start)

    def timeshift_raw_url(self, stream_id: str, minutes: int, start: str) -> str:
        """``start`` déjà au format Xtream ``AAAA-MM-JJ:HH-MM`` (fuseau du fournisseur)."""
        return (f"{self.config.xtream_url}/timeshift/{quote(self.config.username, safe='')}/"
                f"{quote(self.config.password, safe='')}/{minutes}/{start}/{stream_id}.ts")

    def raw_action(self, action: str, **params: str) -> object:
        """Réponse brute d'une action ``player_api.php`` (fiches détaillées, guide court)."""
        return self._fetch(self.api_url(action, **params), f"action={action}")

    def provider_of(self, item_id: object) -> str:
        return "1"

    def accounts(self) -> list[dict]:
        return [{"index": 1, "name": self.config.provider_name or "Fournisseur", "account": self.account()}]

    def lookup(self, kind: str, item_id: str) -> dict | None:
        """Élément déjà en cache (sans appel réseau) : nom d'une chaîne ou d'un film pour l'affichage."""
        return self._kinds[kind].by_id.get(item_id)

    def max_connections_hint(self, provider: str = "1") -> int:
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
        index, (element, gid) = item
        # Dans un groupe : chaînes numérotées d'abord (par numéro), puis ordre du fournisseur.
        number = (0, int(element["num"])) if element.get("custom_num") else (1, 0)
        if gid == FAVORITES_GROUP:
            return (-1, 0, 0, (0, 0), index)
        if gid in custom_rank:
            return (0, custom_rank[gid], 0, number, index)
        return (1 if custom_rank else 0, big, provider_rank.get(gid, len(provider_rank)), number, index)

    return [entry for _, entry in sorted(enumerate(entries), key=key)]


def redact(value: object, config: Config) -> object:
    """Copie profonde d'une réponse brute du fournisseur sans identifiants (tous les fournisseurs)."""
    if isinstance(value, dict):
        return {k: ("***" if str(k).lower() in ("username", "password") else redact(v, config))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, config) for v in value]
    if isinstance(value, str):
        credentials = list(config.credentials) + provider_store.credentials()
        for user, password in credentials:
            for pattern in (f"/{user}/{password}/", f"/{quote(user, safe='')}/{quote(password, safe='')}/"):
                value = value.replace(pattern, "/***/***/")
        value = re.sub(r"(?i)\b(username|password)=[^&#\s]*", r"\1=***", value)
        for _, password in credentials:
            if len(password) >= 4:
                value = value.replace(password, "***")
        return value
    return value


def escape_attribute(value: object) -> str:
    """Sanitize M3U attributes to prevent line/quote injection."""
    return str(value or "").replace("\r", " ").replace("\n", " ").replace('"', "'").strip()


def escape_title(value: object) -> str:
    return str(value or "Sans nom").replace("\r", " ").replace("\n", " ").strip() or "Sans nom"


def parse_alternates(value: str) -> list[str]:
    """``?alt=12,34`` : versions de secours d'une chaîne (identifiants numériques, 8 au maximum)."""
    ids = [part for part in (value or "").split(",") if part.isdigit() and len(part) <= 12]
    return list(dict.fromkeys(ids))[:8]


def redact_raw(streams: list) -> list:
    """Retire de la copie disque les champs qui peuvent contenir des identifiants (``direct_source``)."""
    cleaned = []
    for entry in streams:
        if isinstance(entry, dict) and entry.get("direct_source"):
            entry = {**entry, "direct_source": ""}
        cleaned.append(entry)
    return cleaned


def with_token(url: str, token: str) -> str:
    if not token:
        return url
    return url + ("&" if "?" in url else "?") + urlencode({"token": token})


def playlist(config: Config, entries: list[tuple[dict, str]], categories: dict[str, str], base: str,
             *, mode: str = "proxy", fmt: str = "ts", epg_path: str | None = "/epg.xml",
             group_names: dict[str, str] | None = None, api_epg_ids: bool = False,
             live_format: str = "ts", catchup: bool = True, token: str | None = None,
             direct_url=None, failover: bool = False) -> bytes:
    """Build an M3U (extended) document.

    ``entries`` = ``[(élément, clé_de_groupe), ...]`` où l'élément est une chaîne (``kind`` absent ou ``live``),
    un film (``movie``) ou un épisode (``episode``) ; ``epg_path`` = chemin local du guide XMLTV.
    """
    token = config.access_token if token is None else token
    header = "#EXTM3U"
    if config.enable_epg and epg_path:
        epg = escape_attribute(with_token(f"{base}{epg_path}", token))
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
        logo = display_logo(channel, base)
        if logo:
            attrs.append(f'tvg-logo="{escape_attribute(logo)}"')
        kind = channel.get("kind", "live")
        if channel.get("num") and kind == "live":
            attrs.append(f'tvg-chno="{channel["num"]}"')
        if catchup and kind == "live" and channel.get("archive") and mode == "proxy":
            # Replay : le lecteur remplace {utc}/{utcend} par le début et la fin du programme (secondes UTC).
            source = with_token(f"{base}/timeshift/{stream_id}.ts?start={{utc}}&end={{utcend}}", token)
            attrs.append(f'catchup="default" catchup-days="{channel["archive"]}" '
                         f'catchup-source="{escape_attribute(source)}" tvg-rec="{channel["archive"]}"')
        attrs.append(f'group-title="{escape_attribute(group)}"')
        lines.append("#EXTINF:-1 " + " ".join(attrs) + "," + name)
        path, ext = {"movie": ("movie", channel.get("ext", "mp4")),
                     "episode": ("series", channel.get("ext", "mp4"))}.get(kind, ("live", fmt))
        if mode == "proxy":
            if path == "live":
                local = f"hls/{stream_id}.m3u8" if live_format == "hls" else f"stream/{stream_id}.ts"
                if failover and live_format != "hls" and channel.get("alternates"):
                    # Bascule automatique : versions de secours (meilleure qualité d'abord).
                    local += "?alt=" + ",".join(channel["alternates"])
            else:
                local = f"{path}/{stream_id}.{ext}"
            target = with_token(f"{base}/{local}", token)
        elif direct_url is not None:
            target = direct_url(path, stream_id, ext)
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


def playlists_live_ids(api: XtreamAPI, store: PlaylistStore, limit: int) -> set[str]:
    """Chaînes du direct présentes dans au moins une playlist personnalisée (au plus ``limit``)."""
    try:
        playlists = store.list()
    except RuntimeError:
        return set()
    ids: list[str] = []
    seen: set[str] = set()
    for saved in playlists:
        try:
            selected = select_items(api, saved, "live")[0]
        except UpstreamError:
            continue
        for channel, _ in selected:
            if channel["stream_id"] not in seen:
                seen.add(channel["stream_id"])
                ids.append(channel["stream_id"])
    return set(ids[:limit])


def live_channels_in_order(api: XtreamAPI, saved: dict, *, refresh: bool = False,
                           with_groups: bool = False):
    """Chaînes du direct dans l'ordre de la playlist : favoris, groupes perso (TNT, Canal+…), puis le reste.

    Le guide complété par l'API interroge les chaînes dans cet ordre : les chaînes préférées d'abord.
    """
    entries, groups = select_items(api, saved, "live", refresh=refresh)
    favorites = set(saved.get("favorites") or [])
    ordered = order_entries(entries, groups, saved.get("group_order"))
    ordered.sort(key=lambda e: 0 if e[0]["stream_id"] in favorites else 1)  # tri stable
    if with_groups:
        return ordered
    return [c for c, _ in ordered]


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
        try:
            selected, kind_groups = select_items(api, saved, kind, refresh=refresh)
        except UpstreamError as exc:
            LOG.warning("Catalogue %s indisponible pour la playlist %s", kind, saved.get("slug"))
            errors.append(exc)
            if kind == "series":
                incomplete += len(saved.get("series_category_ids", [])) + len(saved.get("included_series_ids", []))
            continue
        groups.update(kind_groups)
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
                    "series_id": show["stream_id"],
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
    devices: "DeviceStore | None" = None
    emulator: XtreamEmulator
    provider_busy: dict    # {fournisseur: fin de la pause après un refus « abonnement occupé »}
    guard: access.FailureGuard  # essais de code ratés (connexions distantes)
    client_ip: str = ""         # adresse réelle du client (derrière un reverse proxy déclaré)
    remote: bool = False        # connexion hors du réseau local
    request_device: "dict | None" = None  # appareil authentifié : ses droits limitent la requête
    failover_memory: dict  # bascule par reconnexion : {chaîne: version à servir, échéance, attente}
    request_token: str = ""

    def end_headers(self) -> None:
        # Aperçu dans l'interface Web (port WEB_PORT) : le navigateur lit le relais HLS de ce port.
        origin = self.headers.get("Origin", "") if getattr(self, "headers", None) else ""
        if origin and getattr(self, "path", "").startswith("/hls/"):
            try:
                parsed = urlsplit(origin)
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                # Interface Web : son port (WEB_PORT), ou la même machine sur un port redirigé par Docker.
                if parsed.port == self.config.web_port or (host and (parsed.hostname or "").lower() == host):
                    self.send_header("Access-Control-Allow-Origin", origin)
                    self.send_header("Vary", "Origin")
            except ValueError:
                pass
        super().end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Ne jamais journaliser la query string (ACCESS_TOKEN) ni le mot de passe d'un appareil dans le chemin.
        LOG.info("%s %s %s", self.client_ip or self.client_address[0], self.command, mask_path(urlsplit(self.path).path))

    def log_error(self, format: str, *args: object) -> None:
        LOG.debug("HTTP error: " + format, *args)

    def do_HEAD(self) -> None:
        self.safe_handle(head_only=True)

    def do_GET(self) -> None:
        self.safe_handle(head_only=False)

    def safe_handle(self, *, head_only: bool) -> None:
        try:
            self.handle_request(head_only=head_only)
        except UnknownProvider:
            self._error(404, "Contenu introuvable (fournisseur non configuré)", head_only=head_only)

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
        """``?token=`` : ``ACCESS_TOKEN`` ou code d'un appareil. Exigé si ACCESS_TOKEN est défini
        ou si l'option « exiger un code » est active. Le code reçu est repris dans les liens générés."""
        received = qs.get("token", [""])[0]
        expected = self.config.access_token
        self.request_token = ""
        if received and expected and hmac.compare_digest(received.encode(), expected.encode()):
            self.request_token = received
            return True
        device = self.devices.find_by_token(received) if received and self.devices is not None else None
        if device is not None:
            self.request_token = received
            self.request_device = device
            return True
        if received:
            self.note_failure()
        # Hors du réseau local, un code est toujours exigé.
        required = self.remote or bool(expected) or (self.devices is not None and self.devices.require_token)
        return not required

    def note_failure(self) -> None:
        """Code ou identifiants refusés : compte l'essai (connexions distantes seulement)."""
        if self.remote and self.guard.fail(self.client_ip):
            LOG.warning("Accès distant : %s bloquée %d min (trop d'essais de code)", self.client_ip,
                        access.BLOCK_SECONDS // 60)

    def scoped(self) -> "dict | None":
        """Appareil dont les droits limitent la requête (``None`` : accès complet)."""
        device = self.request_device
        return device if device and device.get("playlist") else None

    def device_allows(self, kind: str, item_id: str) -> bool:
        """Règle commune : un appareil lié à une playlist ne lit que ce qu'elle contient."""
        device = self.scoped()
        if device is None:
            return True
        if kind == "episode":
            return self.emulator.allows_episode(device, item_id, self.series)
        return self.emulator.allows(device, {"movie": "vod", "timeshift": "live", "hls": "live"}.get(kind, kind), item_id)

    def public_base(self) -> str:
        if self.config.public_base_url:
            return self.config.public_base_url
        host = self.headers.get("Host", "")
        if not host or not HOST_RE.fullmatch(host):
            raise ValueError("En-tête Host invalide : définir PUBLIC_BASE_URL")
        return "http://" + host

    def current_device(self) -> dict | None:
        """Appareil qui fait la requête : par son code (``?token=`` ou identifiants Xtream), sinon par son adresse."""
        if self.devices is None:
            return None
        ip = self.client_ip or self.client_address[0]
        device = self.request_device or self.devices.find_by_token(getattr(self, "request_token", "") or "")
        if device is not None:
            self.devices.seen_from(device, ip)
            return device
        return self.devices.by_ip(ip)

    def busy_wait(self, provider: str) -> int:
        """Secondes restantes de pause après un refus « abonnement occupé » de ce fournisseur."""
        return max(0, int(self.provider_busy.get(provider, 0) - time.monotonic() + 0.999))

    def mark_busy(self, item_id: str, exc: HTTPError) -> int:
        """Le fournisseur refuse une connexion de plus : pause, pour ne pas prolonger son blocage."""
        try:
            pause = int((exc.headers or {}).get("Retry-After") or BUSY_PAUSE)
        except (TypeError, ValueError, AttributeError):
            pause = BUSY_PAUSE
        pause = min(120, max(10, pause))
        provider = self.api.provider_of(item_id)
        self.provider_busy[provider] = max(self.provider_busy.get(provider, 0), time.monotonic() + pause)
        LOG.warning("Abonnement occupé : le fournisseur refuse une connexion de plus (HTTP %s), pause de %d s",
                    exc.code, pause)
        return pause

    def send_busy(self, wait: int, code: int | None = None, *, head_only: bool = False) -> None:
        detail = f" (HTTP {code})" if code else ""
        message = (f"Abonnement occupé : le fournisseur refuse une connexion de plus{detail}. Une autre lecture "
                   f"utilise sans doute la connexion de l'abonnement (autre TV, autre application). "
                   f"Réessaie dans {wait} s.")
        try:
            self.response(503, (message + "\n").encode(), "text/plain; charset=utf-8", head_only=head_only,
                          extra={"Retry-After": str(max(1, wait))})
        except (OSError, ValueError):
            pass

    def open_session(self, kind: str, item_id: str, label: str, *, virtual: bool = False, provider: str | None = None):
        """Réserve une connexion fournisseur. Retourne la session, ou ``None`` après avoir répondu (409/503)."""
        provider = provider or self.api.provider_of(item_id)
        wait = self.busy_wait(provider)
        if wait:
            # Refus récent du fournisseur : répondre tout de suite, sans le redemander (ça prolongerait le blocage).
            self.send_busy(wait)
            return None
        device = self.current_device() or {}
        try:
            return self.streams.acquire(kind=kind, item_id=item_id, client=self.client_ip or self.client_address[0],
                                        label=label, virtual=virtual, provider=provider,
                                        device=device.get("name", ""), priority=device.get("priority", "normal"))
        except EvictedRecently:
            self._error(409, "Lecture arrêtée : la connexion au fournisseur a été donnée à un autre appareil "
                             "(réessaie dans 30 secondes ou choisis une autre chaîne)", head_only=False)
        except LimitReached as exc:
            holder = exc.holder
            if holder is not None:
                who = f"« {holder.device} »" if holder.device else "un appareil prioritaire"
                self._error(503, f"Connexion occupée : {who} regarde « {holder.channel or holder.label} » et passe en "
                                 "priorité (interface Web, onglet Appareils)", head_only=False)
            else:
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
            if exc.code in BUSY_CODES and track:
                pause = self.mark_busy(track[1], exc)
                if not headers_sent:
                    self.send_busy(pause, exc.code, head_only=head_only)
                else:
                    self.close_connection = True
            elif not headers_sent:
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

    def relay_live(self, ids: list[str]) -> None:
        """Direct avec bascule automatique : si la version lue coupe, se tait ou rame, passer à la suivante.

        Une seule connexion fournisseur à la fois (la précédente est fermée avant d'ouvrir la suivante).
        Tant que rien n'a été envoyé au lecteur, les versions sont essayées l'une après l'autre.
        Ensuite, mode ``reconnect`` (défaut) : la connexion est fermée et la version qui marche est
        mémorisée ; le lecteur se reconnecte et la reçoit directement (un flux MPEG-TS d'une autre version
        — autre codec, résolution, horloge — collé à la suite fige l'image de nombreux lecteurs).
        Mode ``splice`` : les versions sont enchaînées dans la même réponse.
        """
        ids = list(dict.fromkeys(ids))
        names = {sid: self.item_label("live", sid) for sid in ids}
        reconnect = self.config.failover_mode == "reconnect"
        memory = self.failover_memory
        now = time.monotonic()
        remembered = memory.get(ids[0]) if reconnect else None
        if remembered and (remembered["sid"] not in ids or now >= remembered["until"]):
            if remembered["sid"] != ids[0]:
                # Délai écoulé : on retente la meilleure version (« essai », pour allonger l'attente si elle flanche).
                remembered = {"sid": ids[0], "until": now + 6 * 3600, "wait": remembered["wait"], "trial": now}
                memory[ids[0]] = remembered
            else:
                remembered = None
        index = ids.index(remembered["sid"]) if remembered else 0
        # La place est réservée chez le fournisseur de la version réellement ouverte.
        session = self.open_session("live", ids[0], names[ids[0]], provider=self.api.provider_of(ids[index]))
        if session is None:
            return
        state = {"headers_sent": False}
        switches, tried, max_switches = 0, 0, 2 * len(ids)
        session.channel = names[ids[0]]
        base_wait = self.config.failover_return_minutes * 60  # retour à la meilleure version (0 = jamais)
        wait_best = remembered["wait"] if remembered else base_wait
        outcome = ""
        try:
            while not session.cancelled.is_set():
                sid = ids[index]
                session.label = names[ids[0]] + (f" → secours {index}/{len(ids) - 1} ({names[sid]})" if index else "")
                started = time.monotonic()
                provider = self.api.provider_of(sid)
                if provider != session.provider and (self.busy_wait(provider) or not self.streams.move(session, provider)):
                    outcome = "full"  # version chez un autre fournisseur sans place libre : sautée, sans l'appeler
                else:
                    outcome = self._pump_live(sid, session, state, return_after=wait_best if index else 0)
                if outcome in ("client", "cancelled", "busy"):
                    break
                if outcome == "best":
                    # La version de secours a tenu : on retente la meilleure (courte coupure).
                    LOG.info("Retour à la meilleure version de %s après %d min", ids[0], int((time.monotonic() - started) / 60))
                    session.switches += 1
                    if reconnect:
                        memory[ids[0]] = {"sid": ids[0], "until": time.monotonic() + 6 * 3600, "wait": wait_best,
                                          "trial": time.monotonic()}
                        break
                    index, switches = 0, 0
                    continue
                trial = memory.get(ids[0], {}).get("trial") if reconnect else (started if index == 0 and switches == 0 else None)
                if index == 0 and trial and time.monotonic() - trial < 60 and wait_best:
                    wait_best = min(wait_best * 2, 4 * 3600)  # meilleure version encore instable : patienter plus
                switches += 1
                tried += 1
                session.switches += 1
                if tried > max_switches:
                    LOG.warning("Bascule automatique : aucune version de la chaîne %s ne fonctionne", ids[0])
                    break
                index = (index + 1) % len(ids)
                LOG.info("Bascule automatique (%s) : %s → %s", outcome, sid, ids[index])
                if reconnect:
                    hold = wait_best if wait_best else 6 * 3600
                    memory[ids[0]] = {"sid": ids[index], "until": time.monotonic() + (hold if index else 6 * 3600),
                                      "wait": wait_best, "trial": None}
                    if state["headers_sent"]:
                        break  # le lecteur se reconnecte et reçoit directement la version suivante
                if index == 0:
                    time.sleep(1)  # toutes les versions essayées : petite pause avant de recommencer
            if reconnect and index == 0 and outcome in ("client", "cancelled"):
                memory.pop(ids[0], None)  # la meilleure version a tenu jusqu'au bout : rien à mémoriser
        finally:
            self.streams.release(session)
            if state["headers_sent"]:
                self.close_connection = True
            elif state.get("busy"):
                self.send_busy(*state["busy"])
            else:
                try:
                    self._error(502, "Flux fournisseur inaccessible (toutes les versions ont été essayées)", head_only=False)
                except (OSError, ValueError):
                    pass

    def _pump_live(self, sid: str, session, state: dict, *, return_after: float = 0) -> str:
        """Relaie une version. Retourne client | cancelled | failed | stalled | slow | ended | best."""
        stall = self.config.failover_stall_seconds
        request = Request(self.api.direct_stream(sid), headers={"User-Agent": self.config.user_agent, "Accept": "*/*"})
        try:
            upstream = urlopen(request, timeout=stall + 4)  # délai de démarrage un peu plus large
        except (HTTPError, URLError, TimeoutError, OSError, HTTPException, ValueError) as exc:
            LOG.warning("Version %s indisponible (%s)", sid, getattr(exc, "code", type(exc).__name__))
            if isinstance(exc, HTTPError) and exc.code in BUSY_CODES:
                # Même abonnement pour toutes les versions : inutile d'essayer les suivantes.
                state["busy"] = (self.mark_busy(sid, exc), exc.code)
                return "busy"
            return "failed"
        with upstream:
            session.attach(upstream.close)
            try:
                upstream.fp.raw._sock.settimeout(stall)  # lecture : « muet » au-delà de N secondes
            except AttributeError:
                pass
            if not state["headers_sent"]:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                state["headers_sent"] = True
            reader = getattr(upstream, "read1", upstream.read)
            window, window_start, slow_windows = 0, time.monotonic(), 0
            rates: list[float] = []  # débits par fenêtre de 10 s, rafale de démarrage exclue
            started = window_start
            ratio = self.config.failover_slow_ratio
            while True:
                if session.cancelled.is_set():
                    return "cancelled"
                try:
                    chunk = reader(64 * 1024)
                except (socket.timeout, TimeoutError):
                    return "stalled"
                except Exception:
                    return "cancelled" if session.cancelled.is_set() else "failed"
                if not chunk:
                    return "ended"
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                    return "client"
                session.touch(len(chunk))
                window += len(chunk)
                now = time.monotonic()
                if return_after and now - started >= return_after:
                    return "best"
                if now - window_start >= 10:
                    rate = window / (now - window_start)
                    if now - started > SLOW_WARMUP:
                        # Référence = médiane des fenêtres récentes : ni la rafale de démarrage (le fournisseur
                        # remplit le tampon du lecteur), ni un pic d'une scène chargée ne la faussent.
                        baseline = sorted(rates)[len(rates) // 2] if len(rates) >= 2 else 0.0
                        slow = bool(ratio and baseline and rate < baseline * ratio)
                        slow_windows = slow_windows + 1 if slow else 0
                        if slow_windows >= SLOW_WINDOWS:
                            return "slow"
                        if not slow:
                            rates = (rates + [rate])[-6:]
                    window, window_start = 0, now

    def build_custom_m3u(self, saved: dict, qs: dict[str, list[str]], *, epg_path: str, refresh: bool = False,
                         head_only: bool = False, default_kinds: tuple[str, ...] = tuple(KINDS)) -> tuple[bytes, int]:
        """M3U d'une playlist personnalisée (aussi utilisé par ``get.php`` d'un appareil)."""
        kinds = parse_content(qs.get("content", [""])[0], default_kinds)
        entries, groups, incomplete = collect_entries(
            self.api, self.series, saved, kinds, refresh=refresh,
            budget=0 if head_only else self.config.series_fetch_seconds)
        entries = order_entries(add_favorites(entries, groups, saved), groups, saved.get("group_order"))
        has_live = "live" in kinds and uses(saved, "live")
        fmt = qs.get("format", [""])[0] or {"m3u8": "m3u8", "ts": "ts"}.get(qs.get("output", [""])[0], "")
        if fmt not in ("", "ts", "m3u8"):
            raise ValueError("format=ts|m3u8")
        live_format = {"ts": "ts", "m3u8": "hls"}.get(fmt, saved.get("live_format", "ts"))
        content = playlist(self.config, entries, groups, self.public_base(),
                           epg_path=epg_path if has_live else None,
                           group_names=saved.get("group_names"),
                           api_epg_ids=self.config.guide_ids,
                           live_format=live_format, catchup=saved.get("catchup", True),
                           token=self.request_token, failover=bool(saved.get("failover")))
        return content, incomplete

    # ------------------------------------------------------- Xtream local
    def device_from(self, username: str, password: str) -> dict | None:
        if self.devices is None or not username or not password:
            return None
        device = self.devices.authenticate(username, password)
        if device is None:
            self.note_failure()
        else:
            self.request_device = device
        return device

    def send_json(self, data: object, *, head_only: bool, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.response(status, body, "application/json; charset=utf-8", head_only=head_only)

    def handle_player_api(self, qs: dict[str, list[str]], *, head_only: bool) -> None:
        arg = lambda name: qs.get(name, [""])[0]  # noqa: E731
        device = self.device_from(arg("username"), arg("password"))
        if device is None:
            # Journal (une ligne par adresse et par minute) : appareil supprimé ou identifiants mal saisis.
            note_refused_login(self.client_ip or self.client_address[0], arg("username"))
            # Connexion : réponse Xtream standard (auth 0). Listes : vraie erreur 401, pour que le lecteur
            # affiche « identifiants refusés » au lieu d'une liste vide sans explication.
            return self.send_json(EMPTY_AUTH, head_only=head_only, status=200 if not arg("action") else 401)
        self.request_token = device["password"]
        action = arg("action")
        emu = self.emulator
        kind_of = {"get_live_categories": "live", "get_vod_categories": "vod", "get_series_categories": "series",
                   "get_live_streams": "live", "get_vod_streams": "vod", "get_series": "series"}
        try:
            if not action:
                base = urlsplit(self.public_base())
                scheme = "https" if base.scheme == "https" else "http"
                data = emu.account(device, base.hostname or "", str(base.port or (443 if scheme == "https" else 80)),
                                   scheme)
                log_device_login(emu, device, self.client_ip or self.client_address[0])
            elif action.endswith("_categories") and action in kind_of:
                data = emu.categories(device, kind_of[action])
            elif action in kind_of:
                data = emu.items(device, kind_of[action], arg("category_id") or None,
                                 api_epg_ids=self.config.guide_ids,
                                 base=self.public_base())
            elif action == "get_series_info" and arg("series_id").isdigit():
                data = (emu.passthrough("get_series_info", series_id=arg("series_id"))
                        if emu.allows(device, "series", arg("series_id")) else {})
            elif action == "get_vod_info" and arg("vod_id").isdigit():
                data = (emu.passthrough("get_vod_info", vod_id=arg("vod_id"))
                        if emu.allows(device, "vod", arg("vod_id")) else {})
            elif action in ("get_short_epg", "get_simple_data_table") and arg("stream_id").isdigit():
                params = {"stream_id": arg("stream_id")}
                if arg("limit").isdigit():
                    params["limit"] = arg("limit")
                data = (emu.passthrough(action, **params)
                        if emu.allows(device, "live", arg("stream_id")) else {"epg_listings": []})
            else:
                data = []
        except UpstreamError as exc:
            return self.send_json({"error": str(exc)}, head_only=head_only, status=502)
        except ValueError as exc:
            return self.send_json({"error": str(exc)}, head_only=head_only, status=400)
        return self.send_json(data, head_only=head_only)

    def handle_xc_stream(self, kind: str, username: str, password: str, item_id: str, ext: str, *,
                         head_only: bool) -> None:
        device = self.device_from(username, password)
        if device is None:
            note_refused_login(self.client_ip or self.client_address[0], username)
            return self._error(403, "Identifiants d'appareil invalides", head_only=head_only)
        self.request_token = device["password"]
        catalog_kind = {"live": "live", "movie": "vod", "series": "series"}[kind]
        allowed = (self.device_allows("episode", item_id) if kind == "series"
                   else self.emulator.allows(device, catalog_kind, item_id) if device.get("playlist") else True)
        if not allowed:
            return self._error(404, "Contenu absent de la playlist de cet appareil", head_only=head_only)
        ext = ext.lower()
        if kind == "live":
            if ext == "m3u8":
                self.handle_hls(f"/hls/{item_id}.m3u8", head_only=head_only)
                return None
            if head_only:
                return self.response(200, b"", "video/mp2t", head_only=True)
            alternates = self.emulator.alternates(device, item_id)
            if alternates:
                return self.relay_live([item_id, *alternates])
            return self.relay(self.api.direct_stream(item_id), head_only=False, track=("live", item_id))
        return self.relay(self.api.direct_url(kind, item_id, ext), head_only=head_only,
                          default_type=mime_for(ext), track=(kind, item_id))

    def handle_xc_timeshift(self, username: str, password: str, minutes: str, start: str, stream_id: str, *,
                            head_only: bool) -> None:
        device = self.device_from(username, password)
        if device is None:
            return self._error(403, "Identifiants d'appareil invalides", head_only=head_only)
        if not START_RE.fullmatch(start) or not minutes.isdigit() or not 1 <= int(minutes) <= 24 * 60:
            return self._error(400, "Période de replay invalide", head_only=head_only)
        if not self.emulator.allows(device, "live", stream_id):
            return self._error(404, "Chaîne absente de la playlist de cet appareil", head_only=head_only)
        return self.relay(self.api.timeshift_raw_url(stream_id, int(minutes), start), head_only=head_only,
                          track=("timeshift", stream_id))

    def handle_device_files(self, path: str, qs: dict[str, list[str]], *, head_only: bool, refresh: bool) -> None:
        """``get.php`` / ``xmltv.php`` avec identifiants d'appareil : playlist ou guide de l'appareil."""
        username, password = qs.get("username", [""])[0], qs.get("password", [""])[0]
        device = self.device_from(username, password)
        if device is None:
            return self._error(403, "Identifiants d'appareil invalides", head_only=head_only)
        self.request_token = device["password"]
        return self.serve_device_files(device, path, qs, head_only=head_only, refresh=refresh)

    def serve_device_files(self, device: dict, path: str, qs: dict[str, list[str]], *, head_only: bool,
                           refresh: bool) -> None:
        """Playlist (``/get.php``) ou guide (``/xmltv.php``) d'un appareil."""
        username, password = device["username"], device["password"]
        saved = self.emulator.saved_for(device)
        try:
            if path == "/xmltv.php":
                if not self.config.enable_epg:
                    return self._error(404, "EPG désactivé (ENABLE_EPG=false)", head_only=head_only)
                if not device.get("playlist"):
                    guide = self.full_guide(refresh=refresh, budget=0 if head_only else self.config.epg_fetch_seconds)
                else:
                    channels = [c for c, _ in self.emulator.entries(device, "live")[0]]
                    guide, _ = self.epg.playlist_guide(f"dev-{device['username']}", channels,
                                                       budget=0 if head_only else self.config.epg_fetch_seconds)
                return self.send_file(guide, "application/xml; charset=utf-8", head_only=head_only,
                                      filename="epg.xml")
            epg_path = "/xmltv.php?" + urlencode({"username": username, "password": password})
            kinds = tuple(KINDS) if device.get("playlist") else ("live", "vod")
            content, incomplete = self.build_custom_m3u(saved, qs, epg_path=epg_path, refresh=refresh,
                                                        head_only=head_only, default_kinds=kinds)
        except EpgUnavailable as exc:
            return self._error(502, str(exc), head_only=head_only)
        except ValueError as exc:
            return self._error(400, str(exc), head_only=head_only)
        except UpstreamError as exc:
            return self._error(502, f"Impossible de générer la playlist : {exc}", head_only=head_only)
        return self.response(200, content, "audio/x-mpegurl; charset=utf-8", head_only=head_only,
                             filename=f"{device['username']}.m3u", cacheable=not incomplete)

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
            if exc.code in BUSY_CODES and session is not None:
                return self.send_busy(self.mark_busy(session.item_id, exc), exc.code, head_only=head_only)
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
            if not self.device_allows("live", stream_id):
                self._error(404, "Chaîne absente de la playlist de cet appareil", head_only=head_only)
                return True
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
                                          api_ids=playlists_live_ids(self.api, self.store,
                                                                     self.config.epg_api_max_channels))
        return path

    def handle_request(self, *, head_only: bool) -> None:
        parts = urlsplit(self.path)
        qs = parse_qs(parts.query, keep_blank_values=True)
        path = parts.path
        refresh = qs.get("refresh", [""])[0] == "1"
        self.request_device = None
        self.client_ip, self.remote = access.client_address(self.client_address[0], self.headers,
                                                            self.config.trusted_proxies, settings.local_networks(self.config))
        if path == "/health":
            return self.response(HTTPStatus.OK, b"OK\n", "text/plain; charset=utf-8", head_only=head_only)
        if self.remote:
            if not self.config.remote_access:
                return self._error(403, "Accès distant désactivé : ce serveur n'accepte que le réseau local "
                                        "(REMOTE_ACCESS=true pour l'ouvrir, avec un code par appareil)", head_only=head_only)
            wait = self.guard.blocked_for(self.client_ip)
            if wait:
                return self.response(429, f"Trop d'essais de code : réessaie dans {wait} s\n".encode(),
                                     "text/plain; charset=utf-8", head_only=head_only, extra={"Retry-After": str(wait)})
        if path == "/":
            text = (
                f"Xtream M3U Local {__version__}\n"
                "Playlist complete : /playlist.m3u\n"
                "Playlists personnalisees : /p/<nom>.m3u (gestion sur le port de l'interface Web)\n"
            )
            return self.response(HTTPStatus.OK, text.encode(), "text/plain; charset=utf-8", head_only=head_only)
        # Logos haute qualité (images publiques en cache) : sans jeton, comme les logos du fournisseur.
        if path.startswith("/logo/") and logos.serve(self, path, head_only=head_only):
            return
        # Serveur Xtream local : authentification par identifiants d'appareil (pas de ?token=).
        if path == "/player_api.php":
            return self.handle_player_api(qs, head_only=head_only)
        xc = XC_STREAM_RE.fullmatch(path)
        if xc:
            return self.handle_xc_stream(*xc.groups(), head_only=head_only)
        xts = XC_TIMESHIFT_RE.fullmatch(path)
        if xts:
            return self.handle_xc_timeshift(*xts.groups(), head_only=head_only)
        if path == "/streaming/timeshift.php":
            arg = lambda name: qs.get(name, [""])[0]  # noqa: E731
            return self.handle_xc_timeshift(arg("username"), arg("password"), arg("duration"), arg("start"),
                                            arg("stream"), head_only=head_only)
        if path in ("/get.php", "/xmltv.php") and qs.get("username") and self.devices is not None and self.devices.list():
            return self.handle_device_files(path, qs, head_only=head_only, refresh=refresh)
        if not self.authorized(qs):
            return self._error(HTTPStatus.FORBIDDEN, "Token requis ou invalide (?token=...)", head_only=head_only)
        if qs.get("mode", [""])[0] == "direct" and (self.remote or self.request_device is not None):
            # Le mode direct écrit les identifiants du fournisseur dans les liens : réseau local et code global seulement.
            return self._error(403, "mode=direct indisponible avec un code d'appareil ou hors du réseau local",
                               head_only=head_only)
        if path.startswith("/hls/") and self.handle_hls(path, head_only=head_only):
            return
        device = self.scoped()
        if device is not None and path in ("/playlist.m3u", "/get.php", "/epg.xml", "/xmltv.php"):
            # Appareil lié à une playlist : sa playlist et son guide, jamais tout le catalogue.
            return self.serve_device_files(device, "/xmltv.php" if path in ("/epg.xml", "/xmltv.php") else "/get.php",
                                           qs, head_only=head_only, refresh=refresh)
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
                                   api_epg_ids=self.config.guide_ids,
                                   live_format="hls" if fmt == "m3u8" else "ts", token=self.request_token,
                                   direct_url=self.api.direct_url)
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
            if device is not None and slug != device.get("playlist"):
                return self._error(404, "Playlist introuvable", head_only=head_only)
            try:
                saved = self.store.get(slug)
            except RuntimeError:
                return self._error(500, "Sauvegarde des playlists illisible", head_only=head_only)
            if saved is None:
                return self._error(404, "Playlist introuvable", head_only=head_only)
            incomplete = 0
            try:
                if ext == "xml":
                    selected = live_channels_in_order(self.api, saved, refresh=refresh)
                    path_out, _ = self.epg.playlist_guide(
                        slug, selected, budget=0 if head_only else self.config.epg_fetch_seconds)
                    return self.send_file(path_out, "application/xml; charset=utf-8",
                                          head_only=head_only, filename=f"{slug}.xml")
                content, incomplete = self.build_custom_m3u(saved, qs, epg_path=f"/p/{slug}.xml",
                                                            refresh=refresh, head_only=head_only)
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
            if not self.device_allows("live", shift.group(1)):
                return self._error(404, "Chaîne absente de la playlist de cet appareil", head_only=head_only)
            return self.handle_timeshift(shift.group(1), qs, head_only=head_only)
        vod = VOD_RE.fullmatch(path)
        if vod:
            kind, item_id, ext = vod.group(1), vod.group(2), vod.group(3).lower()
            if not self.device_allows("episode" if kind == "series" else "vod", item_id):
                return self._error(404, "Contenu absent de la playlist de cet appareil", head_only=head_only)
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
            if not self.device_allows("live", match.group(1)):
                return self._error(404, "Chaîne absente de la playlist de cet appareil", head_only=head_only)
            if device is not None:
                # Versions de secours décidées par le serveur (playlist de l'appareil), pas par l'adresse.
                alternates = self.emulator.alternates(device, match.group(1))
            else:
                alternates = parse_alternates(qs.get("alt", [""])[0])
            if alternates:
                return self.relay_live([match.group(1), *alternates])
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


_SECRET_PATH = re.compile(r"^/(live|movie|series|timeshift)/([^/]+)/([^/]+)/")


def mask_path(path: str) -> str:
    """« /live/salon/abc123/42.ts » → « /live/salon/***/42.ts » (mot de passe d'appareil hors des journaux)."""
    return _SECRET_PATH.sub(lambda m: f"/{m.group(1)}/{m.group(2)}/***/", path, count=1)


def shutil_copy(src, dst, size: int = 256 * 1024) -> None:
    while True:
        chunk = src.read(size)
        if not chunk:
            break
        dst.write(chunk)


def make_epg(config: Config, api: XtreamAPI) -> EpgCache:
    """Guide XMLTV (``EPG_URL`` ou ``xmltv.php``) + programmes par chaîne via l'API (mode auto/api)."""
    # Lambdas : le client peut être remplacé à chaud (abonnements ajoutés depuis l'interface).
    url_factory = (lambda: config.epg_url) if config.epg_url else (lambda: api.epg_url())
    epg = EpgCache(url_factory, config.data_dir, max_age=config.epg_cache_hours * 3600,
                   user_agent=config.user_agent)
    epg.mode = config.epg_source
    epg.api_max = config.epg_api_max_channels
    # Guides externes (gratuits, publics) : essayés avant les appels API chaîne par chaîne.
    epg.extras = [EpgCache((lambda u=url: u), str(Path(config.data_dir) / "epg-extra" / hashlib.sha256(url.encode()).hexdigest()[:10]),
                           max_age=config.epg_cache_hours * 3600) for url in config.epg_extra_urls]
    if config.epg_source != "xmltv":
        # Débit limité (2 appels par seconde) et journal résumé : des milliers d'appels d'un coup
        # déclenchaient le « 429 trop de requêtes » du fournisseur, et le flux vidéo était refusé avec.
        epg.api_cache = KeyedCache(lambda sid: api.simple_epg(sid), config.data_dir, max_age=6 * 3600,
                                   normalize=normalize_listings, subdir="epg", label="EPG de la chaîne",
                                   min_interval=1 / config.epg_api_per_second if config.epg_api_per_second else 0,
                                   summarize=True)
        started = time.time()
        epg.api_cache.blocked = lambda: epg_api_blocked(api, started, config.epg_startup_quiet)
    return epg


def last_429(api) -> float:
    """Dernier 429 de l'abonnement (le plus récent de tous les abonnements)."""
    apis = getattr(api, "apis", None)
    return max((getattr(a, "last_429", 0.0) for a in apis), default=0.0) if apis else getattr(api, "last_429", 0.0)


def epg_api_blocked(api, started: float, quiet: float = EPG_STARTUP_QUIET) -> str | None:
    """Raison pour laquelle le guide « chaîne par chaîne » ne doit pas appeler le fournisseur, sinon ``None``."""
    now = time.time()
    if now - started < quiet:
        return "démarrage du serveur"
    recent = last_429(api)
    if recent and now - recent < EPG_PRUDENT_SECONDS:
        return "fournisseur sensible (429 récent) : mode prudent"
    return None


def provider_key(url: str, username: str) -> str:
    """Identifiant stable et non réversible d'un abonnement (pour nommer ses fichiers de cache)."""
    return hashlib.sha256(f"{url}\n{username}".encode("utf-8")).hexdigest()[:12]


def migrate_catalog_cache(data_dir: str, apis: list) -> None:
    """Anciennes copies partagées ``catalog-<type>.json.gz`` (avant 3.9.0).

    Un seul fournisseur : elles lui appartiennent, renommées. Plusieurs : impossible de savoir à qui elles
    sont (chaque fournisseur écrasait celle des autres), supprimées : chaque catalogue sera rechargé.
    """
    cache = Path(data_dir) / "cache"
    for kind in KINDS:
        old = cache / f"catalog-{kind}.json.gz"
        if not old.exists():
            continue
        try:
            new = apis[0]._cache_file(kind)
            if len(apis) == 1 and not new.exists():
                os.replace(old, new)
            else:
                old.unlink()
        except OSError:
            LOG.warning("Ancienne copie du catalogue %s non migrée", kind)


def make_api(config: Config, store=None, previous=None, *, migrate: bool = True):
    """Client Xtream : simple, ou façade multi-fournisseurs (``XTREAM_URL_2``… et abonnements de l'interface).

    Numéros : 1 = ``XTREAM_URL`` ; 2… = ``XTREAM_URL_2``… dans l'ordre ; puis les abonnements de l'interface
    (``store``) à leur numéro enregistré (ignorés s'il est déjà pris par la configuration).
    ``previous`` : client actuel, dont les abonnements inchangés sont réutilisés (catalogue gardé en mémoire).
    """
    from .providers import ProviderSet
    base = replace(config, extra_providers=())
    entries = [(1, config.provider_name or "Fournisseur 1", base)]
    for offset, (name, url, user, password) in enumerate(config.extra_providers, start=2):
        entries.append((offset, name, replace(base, xtream_url=url, username=user, password=password, provider_name=name)))
    taken = {index for index, _, _ in entries}
    try:
        stored = store.list() if store is not None else []
    except RuntimeError:
        LOG.warning("providers.json illisible : abonnements de l'interface ignorés")
        stored = []
    for entry in stored:
        if entry["index"] in taken:
            LOG.warning("Abonnement « %s » de l'interface ignoré : n° %d déjà pris par la configuration",
                        entry["name"], entry["index"])
            continue
        entries.append((entry["index"], entry["name"], replace(
            base, xtream_url=entry["url"], username=entry["username"], password=entry["password"],
            provider_name=entry["name"])))
        taken.add(entry["index"])
    old = getattr(previous, "current", previous)
    reusable = {}
    for api in (getattr(old, "apis", None) or ([old] if old is not None else [])):
        reusable[(api.config.xtream_url, api.config.username, api.config.password)] = api
    apis = []
    for _, name, cfg in entries:
        api = reusable.get((cfg.xtream_url, cfg.username, cfg.password)) or XtreamAPI(cfg)
        apis.append(api)
    if migrate:
        migrate_catalog_cache(config.data_dir, apis)
    if len(apis) == 1:
        return apis[0]
    return ProviderSet(apis, [name for _, name, _ in entries], [index for index, _, _ in entries])


def make_streams(config: Config, api: XtreamAPI) -> StreamManager:
    def limit_for(provider: str) -> int:
        return config.max_connections if config.max_connections >= 0 else api.max_connections_hint(provider)
    return StreamManager(limit_for, takeover=config.stream_takeover,
                         history_path=str(Path(config.data_dir) / "history.json"))


def make_series(config: Config, api: XtreamAPI) -> SeriesCache:
    series = SeriesCache(lambda sid: api.series_info(sid), config.data_dir, max_age=config.series_cache_hours * 3600,
                         min_interval=1 / config.series_per_second if config.series_per_second else 0,
                         summarize=True)
    # Mêmes protections que le guide : démarrage calme, mode prudent après un 429 ; copies périmées
    # servies telles quelles en journée et remises à jour la nuit (NIGHTLY_REFRESH).
    started = time.time()
    series.blocked = lambda: epg_api_blocked(api, started, config.epg_startup_quiet)
    series.refresh_stale = lambda: in_nightly_window(config)
    return series


def make_server(config: Config, *, host: str = "0.0.0.0", port: int | None = None,
                api: XtreamAPI | None = None, store: PlaylistStore | None = None,
                epg: EpgCache | None = None, series: SeriesCache | None = None,
                streams: StreamManager | None = None, hls: HlsRegistry | None = None,
                devices: DeviceStore | None = None) -> ThreadingHTTPServer:
    api = api or make_api(config)
    store = store or PlaylistStore(config.data_dir)
    epg = epg or make_epg(config, api)
    series = series or make_series(config, api)
    streams = streams or make_streams(config, api)
    devices = devices if devices is not None else DeviceStore(config.data_dir)
    emulator = XtreamEmulator(api, store, config, streams, redact=lambda value: redact(value, config))
    if getattr(epg, "api_cache", None) is not None:
        epg.api_cache.defer = streams.start_delay  # priorité au flux vidéo qui démarre
    handler = type("ConfiguredHandler", (Handler,),
                   {"api": api, "config": config, "store": store, "epg": epg, "series": series,
                    "streams": streams, "hls": hls or HlsRegistry(), "devices": devices, "emulator": emulator,
                    "failover_memory": {}, "provider_busy": {}, "guard": access.FailureGuard()})
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
    providers = provider_store.ProviderStore(config.data_dir)
    provider_store.install(providers)
    settings.install(settings.Settings(config.data_dir))  # réglages de l'interface (ex. Tailscale = réseau local)
    from .providers import ApiHolder
    api = ApiHolder(make_api(config, providers))  # remplaçable à chaud (abonnements de l'interface)
    store = PlaylistStore(config.data_dir)
    epg = make_epg(config, api)
    series = make_series(config, api)
    streams = make_streams(config, api)
    devices = DeviceStore(config.data_dir)
    library = logos.from_env(config.data_dir)
    logos.install(library)
    if library is not None:
        library.refresh_in_background()  # index des logos haute qualité (au plus une fois par semaine)
    server = make_server(config, api=api, store=store, epg=epg, series=series, streams=streams, devices=devices)
    web = make_web_server(config, api, store, epg=epg, series=series, streams=streams, devices=devices)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    worker.start()
    LOG.info("Xtream M3U Local %s : playlists sur le port %d, interface Web sur le port %d",
             __version__, config.port, config.web_port)
    if not config.web_password:
        LOG.info("Interface Web sans mot de passe (WEB_PASSWORD vide) : réservé à un réseau local de confiance")
    # `docker stop` envoie SIGTERM au PID 1 : arrêt propre et immédiat.
    signal.signal(signal.SIGTERM, _on_sigterm)
    # Mises à jour lourdes (films, séries, épisodes) la nuit (NIGHTLY_REFRESH).
    from . import nightly
    nightly.start(api, series, store, config)
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
    api.accounts()  # charge les comptes : limites de connexions de chaque abonnement
    for kind in KINDS:
        try:
            api.catalog(kind=kind)
        except UpstreamError as exc:
            LOG.warning("Catalogue %s non chargé au démarrage : %s", kind, exc)


if __name__ == "__main__":
    main()
