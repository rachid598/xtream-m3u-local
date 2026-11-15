"""Same-origin management UI on port 7778, separate from the player endpoint."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections import Counter
from urllib.parse import parse_qs, urlsplit

from .custom import InvalidPlaylist, PlaylistStore
from .server import Config, UpstreamError, XtreamAPI

LOG = logging.getLogger("xtream-m3u-local.web")
HTML = Path(__file__).with_name("index.html")
SLUG_API = re.compile(r"^/api/playlists/([a-z0-9][a-z0-9_-]{0,59})$")


class WebHandler(BaseHTTPRequestHandler):
    server_version = "XtreamM3ULocal-Web/1.1"
    config: Config
    api: XtreamAPI
    store: PlaylistStore

    def log_message(self, format: str, *args: object) -> None:
        LOG.info("%s %s", self.client_address[0], urlsplit(self.path).path)

    def output(self, status: int, data: bytes, content_type: str, *, auth_challenge: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        if auth_challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="Xtream M3U Admin", charset="UTF-8"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def json(self, status: int, data: object) -> None:
        self.output(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def authorized(self) -> bool:
        if not self.config.web_password:
            return True
        raw = self.headers.get("Authorization", "")
        if not raw.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(raw[6:], validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            return False
        if ":" not in decoded:
            return False
        username, password = decoded.split(":", 1)
        return hmac.compare_digest(username, self.config.web_username) and hmac.compare_digest(password, self.config.web_password)

    def check_auth(self) -> bool:
        if self.authorized():
            return True
        self.output(401, b"Authentification requise", "text/plain; charset=utf-8", auth_challenge=True)
        return False

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self.check_auth():
            return
        path = urlsplit(self.path).path
        qs = parse_qs(urlsplit(self.path).query)
        if path in ("/", "/index.html"):
            return self.output(200, HTML.read_bytes(), "text/html; charset=utf-8")
        if path == "/api/status":
            return self.json(200, {"ok": True, "playlist_port": self.config.port,
                                   "public_base_url": self.config.public_base_url})
        if path == "/api/playlists":
            try:
                return self.json(200, self.store.list())
            except RuntimeError:
                return self.json(500, {"error": "Sauvegarde illisible : vérifie /data/playlists.json"})
        if path == "/api/catalog":
            try:
                channels, categories = self.api.catalog(refresh=qs.get("refresh", [""])[0] == "1")
            except UpstreamError as exc:
                return self.json(502, {"error": str(exc)})
            result = [{"stream_id": str(c["stream_id"]), "name": str(c.get("name") or "Sans nom"),
                       "category_id": str(c.get("category_id", ""))} for c in channels]
            # Supply category fallbacks for providers missing get_live_categories.
            found = {c["category_id"] for c in result}
            counts = Counter(c["category_id"] for c in result)
            category_list = [{"category_id": cid, "name": categories.get(cid, "TV"),
                              "count": counts[cid]} for cid in sorted(found)]
            return self.json(200, {"categories": category_list, "channels": result})
        return self.json(404, {"error": "Route introuvable"})

    def read_payload(self) -> dict | None:
        if self.headers.get("X-Requested-With") != "XtreamM3ULocal":
            self.json(403, {"error": "En-tête de requête manquant"})
            return None
        if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
            self.json(415, {"error": "Content-Type application/json attendu"})
            return None
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if not 0 <= size <= 2 * 1024 * 1024:
            self.json(413, {"error": "Corps JSON trop volumineux"})
            return None
        try:
            body = json.loads(self.rfile.read(size))
        except (UnicodeError, ValueError):
            self.json(400, {"error": "JSON invalide"})
            return None
        if not isinstance(body, dict):
            self.json(400, {"error": "Objet JSON attendu"})
            return None
        return body

    def do_POST(self) -> None:
        if not self.check_auth():
            return
        if urlsplit(self.path).path != "/api/playlists":
            return self.json(404, {"error": "Route introuvable"})
        body = self.read_payload()
        if body is None:
            return
        try:
            saved = self.store.save(body)
            return self.json(201, saved)
        except InvalidPlaylist as exc:
            return self.json(400, {"error": str(exc)})
        except RuntimeError:
            return self.json(500, {"error": "Échec de l'écriture des playlists dans /data"})

    def do_PUT(self) -> None:
        if not self.check_auth():
            return
        match = SLUG_API.fullmatch(urlsplit(self.path).path)
        if not match:
            return self.json(404, {"error": "Route introuvable"})
        body = self.read_payload()
        if body is None:
            return
        try:
            saved = self.store.save(body, update_slug=match.group(1))
            return self.json(200, saved)
        except KeyError:
            return self.json(404, {"error": "Playlist introuvable"})
        except InvalidPlaylist as exc:
            return self.json(400, {"error": str(exc)})
        except RuntimeError:
            return self.json(500, {"error": "Échec de l'écriture des playlists dans /data"})

    def do_DELETE(self) -> None:
        if not self.check_auth():
            return
        match = SLUG_API.fullmatch(urlsplit(self.path).path)
        if not match:
            return self.json(404, {"error": "Route introuvable"})
        if self.headers.get("X-Requested-With") != "XtreamM3ULocal":
            return self.json(403, {"error": "En-tête de requête manquant"})
        try:
            if self.store.delete(match.group(1)):
                return self.json(200, {"deleted": True})
            return self.json(404, {"error": "Playlist introuvable"})
        except RuntimeError:
            return self.json(500, {"error": "Impossible de modifier /data"})


def make_web_server(config: Config, api: XtreamAPI, store: PlaylistStore, *, host: str = "0.0.0.0",
                    port: int | None = None) -> ThreadingHTTPServer:
    handler = type("ConfiguredWebHandler", (WebHandler,), {"config": config, "api": api, "store": store})
    server = ThreadingHTTPServer((host, config.web_port if port is None else port), handler)
    server.daemon_threads = True
    return server
