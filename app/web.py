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
from .httputil import encode_body
from .custom import InvalidPlaylist, PlaylistStore
from .epg import EpgCache
import threading
import time

from .custom import SLUG_RE, filter_channels, filter_kind, uses
from .epg import EpgUnavailable
from .server import KINDS, Config, UpstreamError, XtreamAPI, redact
from .streams import StreamManager
from .vod import SeriesCache

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
SERIES_API = re.compile(r"^/api/series/([0-9]{1,12})$")
STREAM_STOP_API = re.compile(r"^/api/streams/([0-9]{1,9})/stop$")
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
    series: SeriesCache | None = None
    streams: StreamManager | None = None

    def log_message(self, format: str, *args: object) -> None:
        LOG.info("%s %s %s", self.client_address[0], self.command, urlsplit(self.path).path)

    def log_error(self, format: str, *args: object) -> None:
        LOG.debug("HTTP error: " + format, *args)

    def output(self, status: int, data: bytes, content_type: str, *, auth_challenge: bool = False) -> None:
        data, encoding = encode_body(self.headers, data, content_type)
        self.send_response(status)
        for key, value in encoding.items():
            self.send_header(key, value)
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
        kind = qs.get("kind", ["live"])[0]
        refresh = qs.get("refresh", [""])[0] == "1"
        if path in ("/api/catalog", "/api/catalog/export") and kind not in KINDS:
            return self.json(400, {"error": "kind doit valoir live, vod ou series"})
        if path == "/api/catalog/summary":
            return self.json(200, self.catalog_summary(refresh=refresh))
        if path == "/api/catalog/export":
            return self.export_catalog(refresh=refresh, kind=kind)
        if path == "/api/catalog":
            try:
                items, categories = self.api.catalog(refresh=refresh, kind=kind)
            except UpstreamError as exc:
                return self.json(502, {"error": str(exc)})
            return self.json(200, catalog_payload(items, categories, self.api, kind))
        if path == "/api/streams":
            return self.json(200, self.streams_status())
        if path == "/api/epg":
            return self.epg_status(qs.get("slug", [""])[0] or None)
        series_match = SERIES_API.fullmatch(path)
        if series_match and self.series is not None:
            episodes = self.series.episodes(series_match.group(1), refresh=refresh, timeout=30)
            if episodes is None:
                return self.json(502, {"error": "Épisodes indisponibles (get_series_info)"})
            return self.json(200, {"series_id": series_match.group(1), "episodes": episodes})
        return self.json(404, {"error": "Route introuvable"})

    def streams_status(self) -> dict:
        if self.streams is None:
            return {"limit": 0, "auto": False, "takeover": False, "streams": []}
        return {"limit": self.streams.limit(), "auto": self.config.max_connections < 0,
                "takeover": self.streams.takeover, "streams": self.streams.list()}

    def catalog_summary(self, *, refresh: bool) -> dict:
        """Nombre d'éléments et de catégories par type (léger : ne renvoie pas les listes)."""
        result = {}
        for kind in KINDS:
            try:
                items, categories = self.api.catalog(refresh=refresh, kind=kind)
                result[kind] = {"items": len(items), "categories": len(categories), "error": None}
            except UpstreamError as exc:
                result[kind] = {"items": None, "categories": None, "error": str(exc)}
        return result

    def export_catalog(self, *, refresh: bool, kind: str = "live") -> None:
        """Copie de la structure d'origine (réponses brutes ``player_api.php``), identifiants masqués."""
        try:
            items, categories = self.api.catalog(refresh=refresh, kind=kind)
        except UpstreamError as exc:
            return self.json(502, {"error": str(exc)})
        st = self.api.kind_state(kind)
        categories_action, items_action = KINDS[kind][0], KINDS[kind][1]
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(st.updated_at or time.time()))
        document = {
            "generator": f"Xtream M3U Local {__version__}",
            "kind": kind,
            "exported_at": int(time.time()),
            "catalog_updated_at": int(st.updated_at) if st.updated_at else None,
            "note": f"Réponses brutes de player_api.php ({categories_action}, {items_action}), "
                    "dans l'ordre du fournisseur. Identifiants remplacés par ***.",
            "categories_error": st.categories_error,
            "counts": {"categories": len(st.raw_categories), "streams": len(st.raw_streams),
                       "valid_streams": len(items), "groups": len(categories)},
            categories_action: redact(st.raw_categories, self.config),
            items_action: redact(st.raw_streams, self.config),
        }
        data = json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", f'attachment; filename="catalogue-xtream-{kind}-{stamp}.json"')
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
            "series_cache": self.series.stats() if self.series is not None else None,
            "epg_mode": self.config.epg_source,
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
        path = urlsplit(self.path).path
        if path == "/api/epg/refresh":
            return self.refresh_epg()
        stop = STREAM_STOP_API.fullmatch(path)
        if stop:
            if not self.same_origin():
                return
            if self.streams is None or not self.streams.stop(int(stop.group(1))):
                return self.json(404, {"error": "Lecture introuvable (déjà terminée ?)"})
            return self.json(200, {"stopped": True})
        if path != "/api/playlists":
            return self.json(404, {"error": "Route introuvable"})
        body = self.read_payload()
        if body is None:
            return
        try:
            saved = self.store.save(body)
            self.prefetch_series(saved)
            self.prefetch_epg(saved)
            return self.json(201, saved)
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
            saved = self.store.save(body, update_slug=match.group(1))
            self.prefetch_series(saved)
            self.prefetch_epg(saved)
            return self.json(200, saved)
        except KeyError:
            return self.json(404, {"error": "Playlist introuvable"})
        except InvalidPlaylist as exc:
            return self.json(400, {"error": str(exc)})
        except RuntimeError:
            return self.json(500, {"error": "Échec de l'écriture des playlists dans /data (droits du dossier ?)"})

    def prefetch_series(self, saved: dict) -> None:
        """Après un enregistrement : charge en arrière-plan les épisodes des séries sélectionnées."""
        if self.series is None or not uses(saved, "series"):
            return

        def job() -> None:
            try:
                items, _ = self.api.catalog(kind="series")
            except UpstreamError:
                return
            ids = [item["stream_id"] for item, _ in filter_kind(items, saved, "series")]
            count = self.series.prefetch(ids)
            if count:
                LOG.info("Playlist %s : préchargement des épisodes de %d série(s)", saved["slug"], count)

        threading.Thread(target=job, daemon=True).start()

    # ----------------------------------------------------------------- EPG
    def playlist_channels(self, saved: dict) -> list[dict]:
        channels, _ = self.api.catalog()
        return [c for c, _ in filter_channels(channels, saved)]

    def epg_status(self, slug: str | None) -> None:
        if self.epg is None:
            return self.json(200, {"enabled": False})
        data = {
            "enabled": self.config.enable_epg,
            "mode": self.config.epg_source,
            "custom_source": bool(self.config.epg_url),
            "source": self.epg.status(),
            "playlist": None,
            "error": None,
        }
        if slug and self.config.enable_epg:
            if not SLUG_RE.fullmatch(slug):
                return self.json(400, {"error": "Playlist invalide"})
            try:
                saved = self.store.get(slug)
            except RuntimeError:
                return self.json(500, {"error": "Sauvegarde illisible"})
            if saved is None:
                return self.json(404, {"error": "Playlist introuvable"})
            try:
                # Budget nul : on mesure la couverture et on planifie les chaînes manquantes en arrière-plan.
                _, stats = self.epg.playlist_guide(slug, self.playlist_channels(saved), budget=0)
                data["playlist"] = stats
            except (UpstreamError, EpgUnavailable) as exc:
                data["error"] = str(exc)
            data["source"] = self.epg.status()
        return self.json(200, data)

    def refresh_epg(self) -> None:
        """Re-télécharge le guide XMLTV et force la relecture des programmes de la playlist (arrière-plan)."""
        body = self.read_payload()
        if body is None:
            return
        slug = body.get("slug")
        saved = None
        if slug is not None:
            if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
                return self.json(400, {"error": "Playlist invalide"})
            saved = self.store.get(slug)
            if saved is None:
                return self.json(404, {"error": "Playlist introuvable"})
        if self.epg is None or not self.config.enable_epg:
            return self.json(409, {"error": "EPG désactivé (ENABLE_EPG=false)"})

        def job() -> None:
            if self.config.epg_source != "api":
                try:
                    self.epg.ensure(refresh=True)
                except EpgUnavailable:
                    pass
            if saved is not None and self.epg.api_cache is not None:
                try:
                    ids = [c["stream_id"] for c in self.playlist_channels(saved)]
                except UpstreamError:
                    return
                self.epg.api_cache.prefetch(ids, force=True)

        threading.Thread(target=job, daemon=True).start()
        return self.json(202, {"started": True})

    def prefetch_epg(self, saved: dict) -> None:
        """Après un enregistrement : prépare le guide de la playlist (chaînes à compléter par l'API)."""
        if self.epg is None or not self.config.enable_epg or not uses(saved, "live"):
            return

        def job() -> None:
            try:
                self.epg.playlist_guide(saved["slug"], self.playlist_channels(saved), budget=0)
            except (UpstreamError, EpgUnavailable):
                pass

        threading.Thread(target=job, daemon=True).start()

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


def catalog_payload(channels: list[dict], categories: dict[str, str], api: XtreamAPI | None = None,
                    kind: str = "live") -> dict:
    """Catalogue pour le navigateur : aucun identifiant fournisseur, toutes les catégories avec compteurs.

    Même structure pour le direct, les films et les séries (clé ``channels`` = éléments).
    """
    st = api.kind_state(kind) if api else None
    counts: Counter[str] = Counter()
    for channel in channels:
        for cid in channel["category_ids"]:
            counts[cid] += 1
    meta = st.category_meta if st else {}
    category_list = []
    for position, (cid, name) in enumerate(categories.items()):
        info = meta.get(cid, {})
        category_list.append({"category_id": cid, "name": name, "count": counts.get(cid, 0),
                              "parent_id": info.get("parent_id", ""), "position": position,
                              "source": info.get("source", "provider")})
    channel_list = []
    for c in channels:
        entry = {
            "stream_id": c["stream_id"],
            "name": c["name"],
            "category_id": c["category_id"],
            "category_ids": c["category_ids"],
            "logo": c["logo"],
            "epg": bool(c["epg_id"]),
            "num": c["num"],
        }
        for extra in ("ext", "year", "rating", "archive"):
            if c.get(extra):
                entry[extra] = c[extra]
        channel_list.append(entry)
    return {
        "kind": kind,
        "categories": category_list,
        "channels": channel_list,
        "updated_at": int(st.updated_at) if st and st.updated_at else None,
        "categories_error": st.categories_error if st else None,
    }


def make_web_server(config: Config, api: XtreamAPI, store: PlaylistStore, *, host: str = "0.0.0.0",
                    port: int | None = None, epg: EpgCache | None = None,
                    series: SeriesCache | None = None, streams: StreamManager | None = None) -> ThreadingHTTPServer:
    handler = type("ConfiguredWebHandler", (WebHandler,),
                   {"config": config, "api": api, "store": store, "epg": epg, "series": series,
                    "streams": streams})
    server = ThreadingHTTPServer((host, config.web_port if port is None else port), handler)
    server.daemon_threads = True
    return server
