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
from .custom import InvalidPlaylist, PlaylistStore, validate_playlist
from .epg import EpgCache
import threading
import time

from .builder import select_items
from .custom import SLUG_RE, filter_kind, uses
from .epg import EpgUnavailable, read_schedule, xml_channel_id
from .builder import CUSTOM_PREFIX, normalize
from .server import KINDS, Config, UpstreamError, XtreamAPI, live_channels_in_order, redact, redact_raw
from .devices import DeviceStore, InvalidDevice
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
    # Lecteur HLS pour l'aperçu dans le navigateur (hls.js 1.5.20, licence Apache 2.0, embarqué : aucun CDN).
    "/static/vendor/hls.light.min.js": ("vendor/hls.light.min.js", "text/javascript; charset=utf-8"),
}
SLUG_API = re.compile(r"^/api/playlists/([a-z0-9][a-z0-9_-]{0,59})$")
SERIES_API = re.compile(r"^/api/series/([0-9]{1,12})$")
DEVICE_API = re.compile(r"^/api/devices/([A-Za-z0-9_-]{1,16})$")
STREAM_STOP_API = re.compile(r"^/api/streams/([0-9]{1,9})/stop$")
CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'{players}; "
    "media-src 'self' blob:{players}; img-src 'self' data: http: https:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
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
    devices: DeviceStore | None = None

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
        self.send_header("Content-Security-Policy", CSP.format(players=self.players_origin()))
        if auth_challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="Xtream M3U Local", charset="UTF-8"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def players_origin(self) -> str:
        """Origine du port lecteurs (7777) autorisée pour l'aperçu HLS (CSP connect-src / media-src)."""
        if self.config.public_base_url:
            parts = urlsplit(self.config.public_base_url)
            return f" {parts.scheme}://{parts.netloc}"
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\]", host):
            return ""
        return f" http://{host}:{self.config.port}"

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
        if path == "/api/history":
            return self.json(200, history_payload(self.streams.history() if self.streams is not None else []))
        if path == "/api/devices":
            return self.devices_status()
        if path == "/api/backup":
            return self.backup()
        if path == "/api/epg":
            return self.epg_status(qs.get("slug", [""])[0] or None)
        if path == "/api/now":
            return self.now_on_tv(qs)
        series_match = SERIES_API.fullmatch(path)
        if series_match and self.series is not None:
            episodes = self.series.episodes(series_match.group(1), refresh=refresh, timeout=30)
            if episodes is None:
                return self.json(502, {"error": "Épisodes indisponibles (get_series_info)"})
            return self.json(200, {"series_id": series_match.group(1), "episodes": episodes})
        return self.json(404, {"error": "Route introuvable"})

    # ------------------------------------------------------------ appareils
    def devices_status(self) -> None:
        if self.devices is None:
            return self.json(200, {"require_token": False, "access_token_env": False, "devices": []})
        try:
            devices = self.devices.list()
            require = self.devices.require_token
        except RuntimeError:
            return self.json(500, {"error": "devices.json illisible : vérifie le dossier /data"})
        for device in devices:
            seen = self.devices.last_seen.get(device["id"])
            device["last_seen"] = int(seen) if seen else None
            device["last_ip"] = self.devices.last_ip.get(device["id"], "")
        return self.json(200, {"require_token": require, "access_token_env": bool(self.config.access_token),
                               "devices": devices})

    def device_write(self, method: str, device_id: str | None) -> None:
        if self.devices is None:
            return self.json(404, {"error": "Gestion des appareils indisponible"})
        if method == "DELETE":
            if not self.same_origin():
                return
            try:
                if self.devices.delete(device_id):
                    return self.json(200, {"deleted": True})
                return self.json(404, {"error": "Appareil introuvable"})
            except RuntimeError as exc:
                return self.json(500, {"error": str(exc)})
        body = self.read_payload()
        if body is None:
            return
        try:
            if device_id == "settings":
                self.devices.set_require_token(bool(body.get("require_token")))
                return self.devices_status()
            if method == "POST":
                return self.json(201, self.devices.create(body.get("name"), body.get("playlist", ""),
                                                          body.get("username"), body.get("priority", "normal"),
                                                          body.get("ip", "")))
            return self.json(200, self.devices.update(device_id, body))
        except KeyError:
            return self.json(404, {"error": "Appareil introuvable"})
        except InvalidDevice as exc:
            return self.json(400, {"error": str(exc)})
        except RuntimeError as exc:
            return self.json(500, {"error": str(exc)})

    # -------------------------------------------------- sauvegarde et diagnostic
    def backup(self) -> None:
        try:
            document = {"generator": f"Xtream M3U Local {__version__}", "format": 1,
                        "exported_at": int(time.time()), "playlists": self.store.list(),
                        "devices": self.devices.export() if self.devices is not None else None}
        except RuntimeError:
            return self.json(500, {"error": "Sauvegarde illisible dans /data"})
        data = json.dumps(document, ensure_ascii=False, indent=1).encode("utf-8")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", f'attachment; filename="xtream-m3u-local-sauvegarde-{stamp}.json"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def restore(self) -> None:
        body = self.read_payload()
        if body is None:
            return
        backup = body.get("backup")
        if not isinstance(backup, dict) or not isinstance(backup.get("playlists"), list):
            return self.json(400, {"error": "Fichier de sauvegarde non reconnu"})
        mode = body.get("mode", "merge")
        try:
            # Tout valider avant d'écrire quoi que ce soit (pas de restauration à moitié faite).
            for item in backup["playlists"]:
                validate_playlist(item)
            if mode not in ("merge", "replace"):
                raise InvalidPlaylist("Mode de restauration : merge ou replace")
            if body.get("include_devices") and isinstance(backup.get("devices"), dict) and self.devices is not None:
                self.devices.restore(backup["devices"])
            result = self.store.restore(backup["playlists"], mode=mode)
        except (InvalidPlaylist, InvalidDevice) as exc:
            return self.json(400, {"error": str(exc)})
        except RuntimeError as exc:
            return self.json(500, {"error": str(exc)})
        return self.json(200, result)

    def diagnostic(self) -> None:
        if not self.same_origin():
            return
        from .diagnostic import run
        started = time.time()
        results = run(self.api, self.epg, self.series, self.streams, self.store, self.config)
        return self.json(200, {"started_at": int(started), "seconds": round(time.time() - started, 1),
                               "version": __version__, "results": results})

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
            # direct_source : URL directe du fournisseur, parfois avec d'autres identifiants → jamais exportée.
            items_action: redact_raw(redact(st.raw_streams, self.config)),
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
            "token_required": bool(self.config.access_token) or bool(self.devices and self.devices.require_token),
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
            providers = self.api.accounts()
            if len(providers) > 1:
                data["providers"] = providers
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
        if path == "/api/devices":
            return self.device_write("POST", None)
        if path == "/api/restore":
            return self.restore()
        if path == "/api/diagnostic":
            return self.diagnostic()
        if path == "/api/history/clear":
            if not self.same_origin():
                return
            if self.streams is not None:
                self.streams.clear_history()
            return self.json(200, {"cleared": True})
        stop = STREAM_STOP_API.fullmatch(path)
        if stop:
            if not self.same_origin():
                return
            cooldown = parse_qs(urlsplit(self.path).query).get("cooldown", ["1"])[0] != "0"
            if self.streams is None or not self.streams.stop(int(stop.group(1)), cooldown=cooldown):
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
        device = DEVICE_API.fullmatch(urlsplit(self.path).path)
        if device:
            return self.device_write("PUT", device.group(1))
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
        return live_channels_in_order(self.api, saved)

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
                ordered = live_channels_in_order(self.api, saved, with_groups=True)
                _, stats = self.epg.playlist_guide(slug, [c for c, _ in ordered], budget=0)
                # Couverture de « Mes groupes » (TNT, Bouquet Canal+…) et des favoris, à part.
                missing = {m["stream_id"] for m in stats["missing"]}
                favorites = set(saved.get("favorites") or [])
                mine = {c["stream_id"] for c, group in ordered
                        if group.startswith(CUSTOM_PREFIX) or c["stream_id"] in favorites}
                stats["mine"] = {"channels": len(mine), "covered": len(mine - missing)}
                data["playlist"] = stats
            except (UpstreamError, EpgUnavailable) as exc:
                data["error"] = str(exc)
            data["source"] = self.epg.status()
        return self.json(200, data)

    def now_on_tv(self, qs: dict[str, list[str]]) -> None:
        """« En ce moment à la TV » : programme à l'instant ``at`` (défaut : maintenant) et suivant,
        pour les chaînes d'une playlist (favoris et « Mes groupes » d'abord) ; ``q`` : recherche (7 jours)."""
        slug = qs.get("slug", [""])[0]
        if not SLUG_RE.fullmatch(slug or ""):
            return self.json(400, {"error": "Playlist invalide"})
        if self.epg is None or not self.config.enable_epg:
            return self.json(409, {"error": "EPG désactivé (ENABLE_EPG=false)"})
        try:
            saved = self.store.get(slug)
        except RuntimeError:
            return self.json(500, {"error": "Sauvegarde illisible"})
        if saved is None:
            return self.json(404, {"error": "Playlist introuvable"})
        now = int(time.time())
        try:
            at = int(qs.get("at", [""])[0] or now)
        except ValueError:
            at = now
        query = [t for t in normalize(qs.get("q", [""])[0]).split() if t][:6]
        try:
            ordered = live_channels_in_order(self.api, saved, with_groups=True)
            _, group_names = select_items(self.api, saved, "live")
            path, _ = self.epg.playlist_guide(slug, [c for c, _ in ordered], budget=0)
        except (UpstreamError, EpgUnavailable) as exc:
            return self.json(502, {"error": str(exc)})
        schedule = read_schedule(path)
        favorites = set(saved.get("favorites") or [])
        names = saved.get("group_names") or {}
        channels, results = [], []
        for channel, group in ordered:
            programmes = (schedule.get(xml_channel_id(channel).lower())
                          or schedule.get(f"xtream-{channel['stream_id']}") or [])
            info = {"id": channel["stream_id"], "name": channel["name"], "logo": channel.get("logo", ""),
                    "group": names.get(group) or group_names.get(group, ""), "num": channel.get("num", ""),
                    "mine": group.startswith(CUSTOM_PREFIX) or channel["stream_id"] in favorites}
            if channel.get("alternates"):
                # Versions de secours de la chaîne (doublons masqués), dans l'ordre d'essai de la bascule.
                info["backups"] = [(self.api.lookup("live", sid) or {}).get("name") or sid for sid in channel["alternates"]]
            if query:
                for start, stop, title, desc in programmes:
                    if stop < at or start > at + 7 * 86400:
                        continue
                    text = normalize(f"{title} {desc}")
                    if all(term in text for term in query):
                        results.append({**info, "start": start, "stop": stop, "title": title, "desc": desc})
                continue
            current = next((p for p in programmes if p[0] <= at < p[1]), None)
            upcoming = next((p for p in programmes if p[0] > at), None)
            info["now"] = {"start": current[0], "stop": current[1], "title": current[2], "desc": current[3]} if current else None
            info["next"] = {"start": upcoming[0], "stop": upcoming[1], "title": upcoming[2]} if upcoming else None
            channels.append(info)
        if query:
            results.sort(key=lambda r: (r["start"], not r["mine"]))
            return self.json(200, {"at": at, "query": " ".join(query), "results": results[:200]})
        return self.json(200, {"at": at, "channels": channels[:500], "total": len(channels)})

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
        device = DEVICE_API.fullmatch(urlsplit(self.path).path)
        if device:
            return self.device_write("DELETE", device.group(1))
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
        entry = {"category_id": cid, "name": name, "count": counts.get(cid, 0),
                 "parent_id": info.get("parent_id", ""), "position": position,
                 "source": info.get("source", "provider")}
        if info.get("provider"):
            entry["provider"] = info["provider"]
        category_list.append(entry)
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


def history_payload(history: list[dict], *, days: int = 30, now: float | None = None) -> dict:
    """Historique des lectures : 100 dernières, chaînes les plus regardées et chaînes qui basculent (30 jours).

    Les lectures de moins de 20 s (zapping) ne comptent pas dans le classement.
    """
    now = now or time.time()
    recent = [h for h in history if now - h.get("started_at", 0) <= days * 86400]
    top: dict[str, dict] = {}
    unstable: dict[str, dict] = {}
    for h in recent:
        label = h.get("label") or h.get("item_id") or "?"
        if h.get("seconds", 0) >= 20:
            entry = top.setdefault(label, {"label": label, "kind": h.get("kind"), "count": 0, "seconds": 0})
            entry["count"] += 1
            entry["seconds"] += h.get("seconds", 0)
        if h.get("switches"):
            entry = unstable.setdefault(label, {"label": label, "count": 0, "switches": 0})
            entry["count"] += 1
            entry["switches"] += h["switches"]
    return {
        "entries": history[:100],
        "top": sorted(top.values(), key=lambda e: (-e["seconds"], e["label"]))[:15],
        "unstable": sorted(unstable.values(), key=lambda e: (-e["switches"], e["label"]))[:15],
        "total": len(recent),
    }


def make_web_server(config: Config, api: XtreamAPI, store: PlaylistStore, *, host: str = "0.0.0.0",
                    port: int | None = None, epg: EpgCache | None = None,
                    series: SeriesCache | None = None, streams: StreamManager | None = None,
                    devices: DeviceStore | None = None) -> ThreadingHTTPServer:
    devices = devices if devices is not None else DeviceStore(config.data_dir)
    handler = type("ConfiguredWebHandler", (WebHandler,),
                   {"config": config, "api": api, "store": store, "epg": epg, "series": series,
                    "streams": streams, "devices": devices})
    server = ThreadingHTTPServer((host, config.web_port if port is None else port), handler)
    server.daemon_threads = True
    return server
