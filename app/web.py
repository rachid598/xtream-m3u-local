"""Interface Web de gestion (port 7778), séparée du port des lecteurs (7777)."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import re
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .custom import InvalidPlaylist, PlaylistStore
from .epg import EpgCache
import time

from .server import Config, UpstreamError, XtreamAPI, redact

LOG = logging.getLogger("xtream-m3u-local.web")
STATIC_DIR = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
SLUG_API = re.compile(r"^/api/playlists/([a-z0-9][a-z0-9_-]{0,59})$")
CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
    "img-src 'self' data: http: https:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


class WebHandler(BaseHTTPRequestHandler):
    server_version = f"XtreamM3ULocal-Web/{__version__}"
    sys_version = ""
    timeout = 60
    config: Config
    api: XtreamAPI
    store: PlaylistStore
    epg: EpgCache | None = None

    def log_message(self, format: str, *args: object) -> None:
        LOG.info("%s %s %s", self.client_address[0], self.command, urlsplit(self.path).path)

    def log_error(self, format: str, *args: object) -> None:
        LOG.debug("HTTP error: " + format, *args)

    def output(self, status: int, data: bytes, content_type: str, *, auth_challenge: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        if auth_challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="Xtream M3U Local", charset="UTF-8"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def json(self, status: int, data: object) -> None:
        self.output(status, json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    "application/json; charset=utf-8")

    # ------------------------------------------------------------ sécurité
    def authorized(self) -> bool:
        if not self.config.web_password:
            return True
        raw = self.headers.get("Authorization", "")
        if not raw.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(raw[6:].strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeError):
            return False
        if ":" not in decoded:
            return False
        username, password = decoded.split(":", 1)
        user_ok = hmac.compare_digest(username.encode(), self.config.web_username.encode())
        pass_ok = hmac.compare_digest(password.encode(), self.config.web_password.encode())
        return user_ok and pass_ok

    def check_auth(self) -> bool:
        if self.authorized():
            return True
        self.output(401, "Authentification requise\n".encode(), "text/plain; charset=utf-8", auth_challenge=True)
        return False

    def same_origin(self) -> bool:
        """Protection CSRF : en-tête personnalisé obligatoire + Origin cohérent s'il est présent."""
        if self.headers.get("X-Requested-With") != "XtreamM3ULocal":
            self.json(403, {"error": "En-tête de requête manquant"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            if urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
                self.json(403, {"error": "Origine de la requête refusée"})
                return False
        return True

    # ---------------------------------------------------------------- GET
    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self.check_auth():
            return
        parts = urlsplit(self.path)
        path = parts.path
        qs = parse_qs(parts.query)
        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            try:
                return self.output(200, (STATIC_DIR / name).read_bytes(), ctype)
            except OSError:
                return self.json(500, {"error": "Fichier d'interface manquant"})
        if path == "/api/status":
            return self.json(200, self.status(include_account=qs.get("account", [""])[0] == "1"))
        if path == "/api/playlists":
            try:
                return self.json(200, self.store.list())
            except RuntimeError:
                return self.json(500, {"error": "Sauvegarde illisible : vérifie /data/playlists.json"})
        if path == "/api/catalog/export":
            return self.export_catalog(refresh=qs.get("refresh", [""])[0] == "1")
        if path == "/api/catalog":
            try:
                channels, categories = self.api.catalog(refresh=qs.get("refresh", [""])[0] == "1")
            except UpstreamError as exc:
                return self.json(502, {"error": str(exc)})
            return self.json(200, catalog_payload(channels, categories, self.api))
        return self.json(404, {"error": "Route introuvable"})

    def export_catalog(self, *, refresh: bool) -> None:
        """Copie de la structure d'origine (réponses brutes ``player_api.php``), identifiants masqués."""
        try:
            channels, categories = self.api.catalog(refresh=refresh)
        except UpstreamError as exc:
            return self.json(502, {"error": str(exc)})
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.api.updated_at or time.time()))
        document = {
            "generator": f"Xtream M3U Local {__version__}",
            "exported_at": int(time.time()),
            "catalog_updated_at": int(self.api.updated_at) if self.api.updated_at else None,
            "note": "Réponses brutes de player_api.php (get_live_categories, get_live_streams), "
                    "dans l'ordre du fournisseur. Identifiants remplacés par ***.",
            "categories_error": self.api.categories_error,
            "counts": {"categories": len(self.api.raw_categories), "streams": len(self.api.raw_streams),
                       "valid_streams": len(channels), "groups": len(categories)},
            "get_live_categories": redact(self.api.raw_categories, self.config),
            "get_live_streams": redact(self.api.raw_streams, self.config),
        }
        data = json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", f'attachment; filename="catalogue-xtream-{stamp}.json"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def status(self, *, include_account: bool) -> dict:
        data = {
            "ok": True,
            "version": __version__,
            "playlist_port": self.config.port,
            "public_base_url": self.config.public_base_url,
            "epg_enabled": self.config.enable_epg,
            "token_required": bool(self.config.access_token),
            "web_auth": bool(self.config.web_password),
            "catalog_updated_at": int(self.api.updated_at) if self.api.updated_at else None,
            "catalog_error": self.api.last_error,
            "categories_error": self.api.categories_error,
            "data_file": str(self.store.path),
        }
        # Le token des lecteurs n'est transmis que si l'interface est protégée par mot de passe.
        if self.config.access_token and self.config.web_password:
            data["access_token"] = self.config.access_token
        if include_account:
            data["account"] = self.api.account()
        return data

    # ----------------------------------------------------------- mutations
    def read_payload(self) -> dict | None:
        if not self.same_origin():
            return None
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self.json(415, {"error": "Content-Type application/json attendu"})
            return None
        try:
            size = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            size = -1
        if not 0 <= size <= 4 * 1024 * 1024:
            self.json(413, {"error": "Corps JSON absent ou trop volumineux"})
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
            return self.json(201, self.store.save(body))
        except InvalidPlaylist as exc:
            return self.json(400, {"error": str(exc)})
        except RuntimeError:
            return self.json(500, {"error": "Échec de l'écriture des playlists dans /data (droits du dossier ?)"})

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
            return self.json(200, self.store.save(body, update_slug=match.group(1)))
        except KeyError:
            return self.json(404, {"error": "Playlist introuvable"})
        except InvalidPlaylist as exc:
            return self.json(400, {"error": str(exc)})
        except RuntimeError:
            return self.json(500, {"error": "Échec de l'écriture des playlists dans /data (droits du dossier ?)"})

    def do_DELETE(self) -> None:
        if not self.check_auth():
            return
        match = SLUG_API.fullmatch(urlsplit(self.path).path)
        if not match:
            return self.json(404, {"error": "Route introuvable"})
        if not self.same_origin():
            return
        try:
            if self.store.delete(match.group(1)):
                if self.epg is not None:
                    self.epg.forget(match.group(1))
                return self.json(200, {"deleted": True})
            return self.json(404, {"error": "Playlist introuvable"})
        except RuntimeError:
            return self.json(500, {"error": "Impossible de modifier /data"})


def catalog_payload(channels: list[dict], categories: dict[str, str], api: XtreamAPI | None = None) -> dict:
    """Catalogue pour le navigateur : aucun identifiant fournisseur, toutes les catégories avec compteurs."""
    counts: Counter[str] = Counter()
    for channel in channels:
        for cid in channel["category_ids"]:
            counts[cid] += 1
    meta = api.category_meta if api else {}
    category_list = []
    for position, (cid, name) in enumerate(categories.items()):
        info = meta.get(cid, {})
        category_list.append({"category_id": cid, "name": name, "count": counts.get(cid, 0),
                              "parent_id": info.get("parent_id", ""), "position": position,
                              "source": info.get("source", "provider")})
    channel_list = [{
        "stream_id": c["stream_id"],
        "name": c["name"],
        "category_id": c["category_id"],
        "category_ids": c["category_ids"],
        "logo": c["logo"],
        "epg": bool(c["epg_id"]),
        "num": c["num"],
    } for c in channels]
    return {
        "categories": category_list,
        "channels": channel_list,
        "updated_at": int(api.updated_at) if api and api.updated_at else None,
        "categories_error": api.categories_error if api else None,
    }


def make_web_server(config: Config, api: XtreamAPI, store: PlaylistStore, *, host: str = "0.0.0.0",
                    port: int | None = None, epg: EpgCache | None = None) -> ThreadingHTTPServer:
    handler = type("ConfiguredWebHandler", (WebHandler,),
                   {"config": config, "api": api, "store": store, "epg": epg})
    server = ThreadingHTTPServer((host, config.web_port if port is None else port), handler)
    server.daemon_threads = True
    return server
