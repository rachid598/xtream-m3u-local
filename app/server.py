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
from http import HTTPStatus
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from . import __version__
from .custom import CATEGORY_RE, UNCATEGORIZED, PlaylistStore, filter_channels
from .epg import EpgCache, EpgUnavailable

LOG = logging.getLogger("xtream-m3u-local")
DEFAULT_USER_AGENT = f"XtreamM3ULocal/{__version__}"
STREAM_RE = re.compile(r"^/stream/([0-9]{1,12})\.ts$")
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
    return {
        "stream_id": sid,
        "name": _clean_text(entry.get("name"), "Sans nom"),
        "logo": logo,
        "epg_id": _clean_text(entry.get("epg_channel_id")),
        "num": num if num.isdigit() else "",
        "category_id": cats[0],
        "category_ids": cats,
    }


class XtreamAPI:
    """Client Xtream avec cache mémoire du catalogue et repli sur cache périmé."""

    RETRY_AFTER_FAILURE = 60

    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._channels: list[dict] | None = None
        self._categories: dict[str, str] = {}
        # Organisation d'origine du fournisseur (ordre, parent_id, provenance) + réponses brutes.
        self.category_meta: dict[str, dict] = {}
        self.raw_categories: list = []
        self.raw_streams: list = []
        self.categories_error: str | None = None
        self._expires_at = 0.0
        self._retry_at = 0.0
        self.updated_at: float | None = None
        self.last_error: str | None = None
        self._account: dict | None = None
        self._account_expires = 0.0

    def api_url(self, action: str | None = None) -> str:
        params = {"username": self.config.username, "password": self.config.password}
        if action:
            params["action"] = action
        return f"{self.config.xtream_url}/player_api.php?" + urlencode(params)

    def direct_stream(self, stream_id: str, extension: str = "ts") -> str:
        return (
            f"{self.config.xtream_url}/live/"
            f"{quote(self.config.username, safe='')}/"
            f"{quote(self.config.password, safe='')}/"
            f"{stream_id}.{extension}"
        )

    def epg_url(self) -> str:
        return (
            f"{self.config.xtream_url}/xmltv.php?"
            + urlencode({"username": self.config.username, "password": self.config.password})
        )

    def _fetch(self, url: str, label: str):
        req = Request(url, headers={"User-Agent": self.config.user_agent, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=30) as response:
                # Refuser les réponses démesurées.
                raw = response.read(80 * 1024 * 1024 + 1)
                if len(raw) > 80 * 1024 * 1024:
                    raise UpstreamError("Réponse API trop volumineuse (> 80 Mo)")
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
        if not isinstance(decoded, list):
            raise UpstreamError("Réponse API inattendue : liste absente (identifiants ou abonnement ?)")
        return decoded

    def catalog(self, *, refresh: bool = False) -> tuple[list[dict], dict[str, str]]:
        with self._lock:
            now = time.monotonic()
            if self._channels is not None and not refresh and now < self._expires_at:
                return self._channels, self._categories
            if self._channels is not None and now < self._retry_at:
                return self._channels, self._categories
            try:
                streams = self.get_json("get_live_streams")
                if not streams:
                    LOG.warning("Upstream returned an empty channels list")
                categories_error = None
                try:
                    categories_raw = self.get_json("get_live_categories")
                except UpstreamError as exc:
                    categories_raw = []
                    categories_error = f"get_live_categories a échoué : {exc}"
                    LOG.warning("Categories unavailable; using derived groups")
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
                channels = [c for c in (normalize_channel(e) for e in streams) if c is not None]
                for channel in channels:
                    for cid in channel["category_ids"]:
                        if cid not in categories:
                            categories[cid] = "Sans catégorie" if cid == UNCATEGORIZED else f"Catégorie {cid}"
                            meta[cid] = {"parent_id": "", "source": "derived", "position": len(meta)}
                self.category_meta = meta
                self.raw_categories = categories_raw if isinstance(categories_raw, list) else []
                self.raw_streams = streams
                self.categories_error = categories_error
                self._channels = channels
                self._categories = categories
                self._expires_at = now + self.config.cache_seconds
                self._retry_at = 0.0
                self.updated_at = time.time()
                self.last_error = None
                LOG.info("Catalogue chargé : %d chaînes, %d catégories", len(channels), len(categories))
            except UpstreamError as exc:
                self.last_error = str(exc)
                if self._channels is None:
                    raise
                self._retry_at = now + self.RETRY_AFTER_FAILURE
                LOG.warning("Using stale catalog because upstream refresh failed")
            return self._channels, self._categories

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
        exp = text("exp_date")
        self._account = {
            "status": text("status"),
            "exp_date": int(exp) if exp.isdigit() else None,
            "max_connections": text("max_connections"),
            "active_connections": text("active_cons"),
            "is_trial": text("is_trial") in ("1", "true"),
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
             group_names: dict[str, str] | None = None) -> bytes:
    """Build an M3U (extended) document.

    ``entries`` = ``[(channel, group_category_id), ...]`` ; ``epg_path`` = chemin local du guide XMLTV.
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
        attrs.append(f'tvg-name="{escape_attribute(name)}"')
        if channel.get("logo"):
            attrs.append(f'tvg-logo="{escape_attribute(channel["logo"])}"')
        if channel.get("num"):
            attrs.append(f'tvg-chno="{channel["num"]}"')
        attrs.append(f'group-title="{escape_attribute(group)}"')
        lines.append("#EXTINF:-1 " + " ".join(attrs) + "," + name)
        if mode == "proxy":
            target = with_token(f"{base}/stream/{stream_id}.ts", config)
        else:
            target = (
                f"{config.xtream_url}/live/"
                f"{quote(config.username, safe='')}/{quote(config.password, safe='')}/"
                f"{stream_id}.{fmt}"
            )
        lines.append(target)
    return ("\n".join(lines) + "\n").encode("utf-8")


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
                 filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if filename:
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.end_headers()
        if not head_only:
            self.wfile.write(content)

    def _error(self, status: int, message: str, *, head_only: bool) -> None:
        self.response(status, (message + "\n").encode(), "text/plain; charset=utf-8", head_only=head_only)

    def send_file(self, path: Path, content_type: str, *, head_only: bool, filename: str) -> None:
        try:
            handle = path.open("rb")
        except OSError:
            return self._error(502, "Fichier indisponible", head_only=head_only)
        with handle:
            size = os.fstat(handle.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
            self.end_headers()
            if head_only:
                return
            try:
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

    def relay(self, upstream_url: str, *, head_only: bool) -> None:
        headers = {"User-Agent": self.config.user_agent, "Accept": "*/*"}
        for h in ("Range", "If-Range"):
            if self.headers.get(h):
                headers[h] = self.headers[h]
        request = Request(upstream_url, headers=headers)
        headers_sent = False
        try:
            with urlopen(request, timeout=45) as upstream:
                self.send_response(upstream.status)
                for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    value = upstream.headers.get(key)
                    if value:
                        self.send_header(key, value)
                if not upstream.headers.get("Content-Type"):
                    self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                headers_sent = True
                reader = getattr(upstream, "read1", upstream.read)
                while True:
                    chunk = reader(64 * 1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, socket.timeout):
                        break
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
            if headers_sent:
                self.close_connection = True

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
        if path in ("/playlist.m3u", "/get.php"):
            mode = qs.get("mode", ["proxy"])[0]
            fmt = qs.get("format", ["ts"])[0]
            if mode not in ("proxy", "direct") or fmt not in ("ts", "m3u8"):
                return self._error(400, "mode=proxy|direct et format=ts|m3u8", head_only=head_only)
            if mode == "proxy" and fmt != "ts":
                return self._error(400, "Le proxy prend en charge TS ; utiliser mode=direct pour HLS", head_only=head_only)
            try:
                base = self.public_base()
                channels, categories = self.api.catalog(refresh=refresh)
                entries = order_entries([(c, c["category_id"]) for c in channels], categories)
                content = playlist(self.config, entries, categories, base, mode=mode, fmt=fmt)
            except (ValueError, UpstreamError) as exc:
                LOG.warning("Cannot build playlist: %s", str(exc))
                return self._error(502, str(exc), head_only=head_only)
            return self.response(HTTPStatus.OK, content, "audio/x-mpegurl; charset=utf-8",
                                 head_only=head_only, filename="playlist.m3u")
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
            try:
                channels, categories = self.api.catalog(refresh=refresh)
                entries = order_entries(filter_channels(channels, saved), categories, saved.get("group_order"))
                if ext == "xml":
                    ids = {c["epg_id"] for c, _ in entries if c.get("epg_id")}
                    path_out = self.epg.filtered(slug, ids)
                    return self.send_file(path_out, "application/xml; charset=utf-8",
                                          head_only=head_only, filename=f"{slug}.xml")
                content = playlist(self.config, entries, categories, self.public_base(),
                                   epg_path=f"/p/{slug}.xml", group_names=saved.get("group_names"))
            except EpgUnavailable as exc:
                return self._error(502, str(exc), head_only=head_only)
            except (ValueError, UpstreamError) as exc:
                LOG.warning("Cannot build custom playlist: %s", str(exc))
                return self._error(502, f"Impossible de générer la playlist : {exc}", head_only=head_only)
            return self.response(200, content, "audio/x-mpegurl; charset=utf-8",
                                 head_only=head_only, filename=f"{slug}.m3u")
        match = STREAM_RE.fullmatch(path)
        if match:
            if head_only:
                # Ne pas consommer une connexion fournisseur pour une simple sonde HEAD.
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self.relay(self.api.direct_stream(match.group(1)), head_only=False)
        if path == "/epg.xml" and self.config.enable_epg:
            try:
                source = self.epg.ensure(refresh=refresh)
            except EpgUnavailable as exc:
                return self._error(502, str(exc), head_only=head_only)
            return self.send_file(source, "application/xml; charset=utf-8", head_only=head_only, filename="epg.xml")
        return self._error(404, "Page introuvable", head_only=head_only)


def shutil_copy(src, dst, size: int = 256 * 1024) -> None:
    while True:
        chunk = src.read(size)
        if not chunk:
            break
        dst.write(chunk)


def make_epg(config: Config, api: XtreamAPI) -> EpgCache:
    return EpgCache(api.epg_url, config.data_dir, max_age=config.epg_cache_hours * 3600,
                    user_agent=config.user_agent)


def make_server(config: Config, *, host: str = "0.0.0.0", port: int | None = None,
                api: XtreamAPI | None = None, store: PlaylistStore | None = None,
                epg: EpgCache | None = None) -> ThreadingHTTPServer:
    api = api or XtreamAPI(config)
    store = store or PlaylistStore(config.data_dir)
    epg = epg or make_epg(config, api)
    handler = type("ConfiguredHandler", (Handler,), {"api": api, "config": config, "store": store, "epg": epg})
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
    server = make_server(config, api=api, store=store, epg=epg)
    web = make_web_server(config, api, store, epg=epg)
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
    try:
        api.catalog()
    except UpstreamError as exc:
        LOG.warning("Catalogue non chargé au démarrage : %s", exc)


if __name__ == "__main__":
    main()
