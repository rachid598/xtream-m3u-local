"""Generate a local M3U playlist from the Xtream player API.

Uses only Python's standard library, including for TS stream relay.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from .custom import PlaylistStore, filter_channels, SLUG_RE
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import Request, urlopen

LOG = logging.getLogger("xtream-m3u-local")
USER_AGENT = "XtreamM3ULocal/1.0"
STREAM_RE = re.compile(r"^/stream/([0-9]+)\.ts$")
HOST_RE = re.compile(r"^[a-zA-Z0-9.\-:\[\]]+$")


@dataclass(frozen=True)
class Config:
    xtream_url: str
    username: str
    password: str
    port: int = 7777
    public_base_url: str = ""
    cache_seconds: int = 1800
    access_token: str = ""
    enable_epg: bool = False
    web_port: int = 7778
    data_dir: str = "/data"
    web_username: str = "admin"
    web_password: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        url = os.environ.get("XTREAM_URL", "").strip().rstrip("/")
        username = os.environ.get("XTREAM_USERNAME", "")
        password = os.environ.get("XTREAM_PASSWORD", "")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("XTREAM_URL doit être une URL http(s) sans identifiants ni paramètres")
        if not username or not password:
            raise ValueError("XTREAM_USERNAME et XTREAM_PASSWORD sont obligatoires")
        port = int(os.environ.get("PORT", "7777"))
        if not 0 < port <= 65535:
            raise ValueError("PORT invalide")
        base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if base and (urlsplit(base).scheme not in ("http", "https") or not urlsplit(base).netloc):
            raise ValueError("PUBLIC_BASE_URL doit être une URL http(s)")
        return cls(
            xtream_url=url,
            username=username,
            password=password,
            port=port,
            public_base_url=base,
            cache_seconds=max(0, int(os.environ.get("CACHE_SECONDS", "1800"))),
            access_token=os.environ.get("ACCESS_TOKEN", ""),
            enable_epg=os.environ.get("ENABLE_EPG", "false").lower() in ("1", "true", "yes", "on"),
            web_port=int(os.environ.get("WEB_PORT", "7778")),
            data_dir=os.environ.get("DATA_DIR", "/data"),
            web_username=os.environ.get("WEB_USERNAME", "admin"),
            web_password=os.environ.get("WEB_PASSWORD", ""),
        )


class UpstreamError(Exception):
    """An upstream request failed (without exposing credentials)."""


class XtreamAPI:
    def __init__(self, config: Config):
        self.config = config
        self._lock = threading.Lock()
        self._channels: list[dict] | None = None
        self._categories: dict[str, str] = {}
        self._expires_at = 0.0

    def api_url(self, action: str) -> str:
        return (
            f"{self.config.xtream_url}/player_api.php?"
            + urlencode({
                "username": self.config.username,
                "password": self.config.password,
                "action": action,
            })
        )

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

    def get_json(self, action: str) -> list:
        req = Request(self.api_url(action), headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urlopen(req, timeout=20) as response:
                # Do not allow accidentally downloading unbounded API responses.
                raw = response.read(50 * 1024 * 1024 + 1)
                if len(raw) > 50 * 1024 * 1024:
                    raise UpstreamError("Réponse API trop volumineuse (> 50 Mo)")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            status = getattr(exc, "code", None)
            LOG.warning("API error for action=%s status=%s", action, status or "network")
            raise UpstreamError(f"API Xtream indisponible ({status or 'réseau'})") from None
        try:
            decoded = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise UpstreamError("L'API Xtream ne renvoie pas du JSON valide") from None
        if not isinstance(decoded, list):
            raise UpstreamError("Réponse API inattendue : liste de chaînes absente")
        return decoded

    def catalog(self, *, refresh: bool = False) -> tuple[list[dict], dict[str, str]]:
        with self._lock:
            if not refresh and self._channels is not None and time.monotonic() < self._expires_at:
                return self._channels, self._categories
            try:
                streams = self.get_json("get_live_streams")
                if not streams:
                    # Empty list can be legitimate; don't fabricate channels.
                    LOG.warning("Upstream returned an empty channels list")
                try:
                    categories_raw = self.get_json("get_live_categories")
                except UpstreamError:
                    categories_raw = []
                    LOG.warning("Categories unavailable; using default group")
                categories = {
                    str(item["category_id"]): str(item.get("category_name") or "TV")
                    for item in categories_raw
                    if isinstance(item, dict) and item.get("category_id") is not None
                }
                channels = [
                    entry for entry in streams
                    if isinstance(entry, dict)
                    and str(entry.get("stream_id", "")).isdigit()
                ]
                self._channels = channels
                self._categories = categories
                self._expires_at = time.monotonic() + self.config.cache_seconds
            except UpstreamError:
                if self._channels is None:
                    raise
                LOG.warning("Using stale catalog because upstream refresh failed")
            return self._channels, self._categories


def escape_attribute(value: object) -> str:
    """Sanitize M3U attributes to prevent line/quote injection."""
    return str(value or "").replace("\r", " ").replace("\n", " ").replace('"', "'").strip()


def escape_title(value: object) -> str:
    return str(value or "Sans nom").replace("\r", " ").replace("\n", " ").strip()


def playlist(config: Config, channels: list[dict], categories: dict[str, str], base: str,
             *, mode: str = "proxy", fmt: str = "ts") -> bytes:
    header = "#EXTM3U"
    if config.enable_epg:
        epg = f"{base}/epg.xml"
        if config.access_token:
            epg += "?" + urlencode({"token": config.access_token})
        header += f' x-tvg-url="{escape_attribute(epg)}"'
    lines = [header]
    for channel in channels:
        stream_id = str(channel["stream_id"])
        if not stream_id.isdigit():
            continue
        name = escape_title(channel.get("name"))
        group = categories.get(str(channel.get("category_id", "")), "TV")
        attrs = [
            f'tvg-id="{escape_attribute(channel.get("epg_channel_id"))}"',
            f'tvg-name="{escape_attribute(name)}"',
            f'tvg-logo="{escape_attribute(channel.get("stream_icon"))}"',
            f'group-title="{escape_attribute(group)}"',
        ]
        if str(channel.get("num", "")).isdigit():
            attrs.append(f'tvg-chno="{channel["num"]}"')
        lines.append("#EXTINF:-1 " + " ".join(attrs) + "," + name)
        if mode == "proxy":
            target = f"{base}/stream/{stream_id}.ts"
            if config.access_token:
                target += "?" + urlencode({"token": config.access_token})
        else:
            target = (
                f"{config.xtream_url}/live/"
                f"{quote(config.username, safe='')}/{quote(config.password, safe='')}/"
                f"{stream_id}.{fmt}"
            )
        lines.append(target)
    return ("\n".join(lines) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "XtreamM3ULocal/1.0"
    api: XtreamAPI
    store: PlaylistStore
    config: Config

    def log_message(self, format: str, *args: object) -> None:
        # Never log query parameters (ACCESS_TOKEN or other secrets).
        LOG.info("%s %s", self.client_address[0], urlsplit(self.path).path)

    def do_HEAD(self) -> None:
        self.handle_request(head_only=True)

    def do_GET(self) -> None:
        self.handle_request(head_only=False)

    def response(self, status: int, content: bytes, content_type: str, *, head_only: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(content)

    def _error(self, status: int, message: str, *, head_only: bool) -> None:
        self.response(status, (message + "\n").encode(), "text/plain; charset=utf-8", head_only=head_only)

    def authorized(self, qs: dict[str, list[str]]) -> bool:
        expected = self.config.access_token
        if not expected:
            return True
        received = qs.get("token", [""])[0]
        return hmac.compare_digest(received, expected)

    def public_base(self) -> str:
        if self.config.public_base_url:
            return self.config.public_base_url
        host = self.headers.get("Host", "")
        if not HOST_RE.fullmatch(host):
            raise ValueError("En-tête Host invalide : définir PUBLIC_BASE_URL")
        return "http://" + host

    def relay(self, upstream_url: str, *, head_only: bool, epg: bool = False) -> None:
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        for h in ("Range", "If-Range"):
            if self.headers.get(h):
                headers[h] = self.headers[h]
        request = Request(upstream_url, headers=headers, method="HEAD" if head_only else "GET")
        headers_sent = False
        try:
            with urlopen(request, timeout=45) as upstream:
                self.send_response(upstream.status)
                for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    value = upstream.headers.get(key)
                    if value:
                        self.send_header(key, value)
                if not upstream.headers.get("Content-Type"):
                    self.send_header("Content-Type", "application/xml" if epg else "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                headers_sent = True
                if head_only:
                    return
                while True:
                    chunk = upstream.read(64 * 1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        break
        except HTTPError as exc:
            LOG.warning("Upstream stream error status=%s", exc.code)
            if not headers_sent:
                self._error(502, f"Le fournisseur a renvoyé HTTP {exc.code}", head_only=head_only)
            else:
                self.close_connection = True
        except (URLError, TimeoutError, OSError):
            LOG.warning("Upstream stream network failure")
            # Impossible d'envoyer une nouvelle réponse HTTP après les premiers octets.
            if not headers_sent:
                try:
                    self._error(502, "Flux fournisseur inaccessible", head_only=head_only)
                except (OSError, ValueError):
                    pass
            else:
                self.close_connection = True

    def handle_request(self, *, head_only: bool) -> None:
        parts = urlsplit(self.path)
        qs = parse_qs(parts.query, keep_blank_values=True)
        path = parts.path
        if path == "/health":
            return self.response(HTTPStatus.OK, b"OK\n", "text/plain; charset=utf-8", head_only=head_only)
        if path == "/":
            return self.response(HTTPStatus.OK, b"Xtream M3U Local | /playlist.m3u | /health\n",
                                 "text/plain; charset=utf-8", head_only=head_only)
        if not self.authorized(qs):
            return self._error(HTTPStatus.FORBIDDEN, "Token requis ou invalide", head_only=head_only)
        if path in ("/playlist.m3u", "/get.php"):
            mode = qs.get("mode", ["proxy"])[0]
            fmt = qs.get("format", ["ts"])[0]
            if mode not in ("proxy", "direct") or fmt not in ("ts", "m3u8"):
                return self._error(400, "mode=proxy|direct et format=ts|m3u8", head_only=head_only)
            if mode == "proxy" and fmt != "ts":
                return self._error(400, "Le proxy prend en charge TS ; utiliser mode=direct pour HLS", head_only=head_only)
            try:
                base = self.public_base()
                channels, categories = self.api.catalog(refresh=qs.get("refresh", [""])[0] == "1")
                content = playlist(self.config, channels, categories, base, mode=mode, fmt=fmt)
            except (ValueError, UpstreamError) as exc:
                LOG.warning("Cannot build playlist: %s", str(exc))
                return self._error(502, str(exc), head_only=head_only)
            return self.response(HTTPStatus.OK, content, "audio/x-mpegurl; charset=utf-8", head_only=head_only)
        custom_match = re.fullmatch(r"/p/([a-z0-9_-]{1,60})\.m3u", path)
        if custom_match:
            try:
                saved = self.store.get(custom_match.group(1))
                if saved is None:
                    return self._error(404, "Playlist introuvable", head_only=head_only)
                channels, categories = self.api.catalog(refresh=qs.get("refresh", [""])[0] == "1")
                content = playlist(self.config, filter_channels(channels, saved), categories, self.public_base())
            except (ValueError, UpstreamError, RuntimeError) as exc:
                LOG.warning("Cannot build custom playlist: %s", str(exc))
                return self._error(502, "Impossible de générer la playlist", head_only=head_only)
            return self.response(200, content, "audio/x-mpegurl; charset=utf-8", head_only=head_only)
        match = STREAM_RE.fullmatch(path)
        if match:
            return self.relay(self.api.direct_stream(match.group(1)), head_only=head_only)
        if path == "/epg.xml" and self.config.enable_epg:
            return self.relay(self.api.epg_url(), head_only=head_only, epg=True)
        return self._error(404, "Page introuvable", head_only=head_only)


def make_server(config: Config, *, host: str = "0.0.0.0", port: int | None = None,
                api: XtreamAPI | None = None, store: PlaylistStore | None = None) -> ThreadingHTTPServer:
    api = api or XtreamAPI(config)
    store = store or PlaylistStore(config.data_dir)
    handler = type("ConfiguredHandler", (Handler,), {"api": api, "config": config, "store": store})
    server = ThreadingHTTPServer((host, config.port if port is None else port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = Config.from_env()
    except ValueError as exc:
        LOG.error("Configuration invalide : %s", exc)
        raise SystemExit(2) from None
    if not 0 < config.web_port <= 65535 or config.web_port == config.port:
        raise SystemExit("WEB_PORT doit être valide et différent de PORT")
    from .web import make_web_server
    api = XtreamAPI(config)
    store = PlaylistStore(config.data_dir)
    server = make_server(config, api=api, store=store)
    web = make_web_server(config, api, store)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True)
    worker.start()
    LOG.info("Playlist M3U sur port %d ; interface Web sur port %d", config.port, config.web_port)
    try:
        web.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        web.server_close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
