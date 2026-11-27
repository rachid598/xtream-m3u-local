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

from . import __version__, access, alerts, logos, provider_store, settings, tunnel
from .httputil import encode_body
from .custom import InvalidPlaylist, PlaylistStore, validate_playlist
from .epg import EpgCache
import threading
import time

from .builder import CUSTOM_PREFIX, FAVORITES_GROUP, add_favorites, display_logo, normalize, regional_group_key, select_items
from .custom import SLUG_RE, uses
from .epg import EpgUnavailable, read_schedule, xml_channel_id
from .server import KINDS, Config, UpstreamError, XtreamAPI, live_channels_in_order, order_entries, redact, redact_raw
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
PROVIDER_API = re.compile(r"^/api/providers/([2-9])$")
MIX_API = re.compile(r"^/api/providers/([1-9])/mix$")
CREDENTIALS_API = re.compile(r"^/api/providers/([1-9])/credentials$")
CSP = (
    "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'{players}; "
    "media-src 'self' blob:{players}; img-src 'self' data: http: https:; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


class WebHandler(BaseHTTPRequestHandler):
    def handle(self) -> None:
        # Navigateur ou lecteur qui ferme la connexion avant la fin de la réponse (rechargement, zapping,
        # délai dépassé) : normal, pas de trace d'erreur dans le journal.
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

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
        if not self.config.host_ok(self.headers.get("Host", ""), self.client_address[0]):
            # DNS rebinding : un site piégé ne doit pas pouvoir lire l'interface (codes, sauvegarde…).
            LOG.warning("Interface Web : nom d'hôte « %s » refusé (protection DNS rebinding) — ajoute-le dans "
                        "ALLOWED_HOSTS s'il est à toi", access.host_name(self.headers.get("Host", ""))[:80])
            self.output(403, "Nom d'hôte refusé : ouvre l'interface par l'adresse IP du serveur, un nom local "
                             "(.local, .lan…) ou ajoute ce nom dans ALLOWED_HOSTS\n".encode(), "text/plain; charset=utf-8")
            return False
        ip, remote = access.client_address(self.client_address[0], self.headers, self.config.proxies(),
                                           settings.local_networks(self.config))
        if remote:
            # L'interface de gestion reste sur le réseau local, sauf WEB_REMOTE=true (mot de passe obligatoire).
            if not self.config.web_remote:
                self.output(403, "Interface Web réservée au réseau local (WEB_REMOTE=false)\n".encode(),
                            "text/plain; charset=utf-8")
                return False
            wait = self.guard.blocked_for(ip)
            if wait:
                self.output(429, f"Trop d'essais : réessaie dans {wait} s\n".encode(), "text/plain; charset=utf-8")
                return False
        if self.authorized():
            return True
        if remote and self.headers.get("Authorization") and self.guard.fail(ip):
            LOG.warning("Interface Web : %s bloquée %d min (trop d'essais de mot de passe)", ip, access.BLOCK_SECONDS // 60)
        self.output(401, "Authentification requise\n".encode(), "text/plain; charset=utf-8", auth_challenge=True)
        return False

    def same_origin(self) -> bool:
        """Protection CSRF : en-tête personnalisé obligatoire + Origin cohérent s'il est présent."""
        if self.headers.get("X-Requested-With") != "XtreamM3ULocal":
            self.json(403, {"error": "En-tête de requête manquant"})
            return False
        origin = self.headers.get("Origin")
        if origin:
            if origin == "null" or urlsplit(origin).netloc.lower() != (self.headers.get("Host") or "").lower():
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
        if path.startswith("/logo/") and logos.serve(self, path, head_only=self.command == "HEAD"):
            return
        if path == "/api/status":
            return self.json(200, self.status(include_account=qs.get("account", [""])[0] == "1"))
        if path == "/api/playlists":
            try:
                return self.json(200, self.store.list())
            except RuntimeError:
                return self.json(500, {"error": "Sauvegarde illisible : vérifie /data/playlists.json"})
        kind = qs.get("kind", ["live"])[0]
        refresh = qs.get("refresh", [""])[0] == "1"
        profile = qs.get("profile", [""])[0]
        profile = profile if profile.isdigit() else ""
        if path in ("/api/catalog", "/api/catalog/export") and kind not in KINDS:
            return self.json(400, {"error": "kind doit valoir live, vod ou series"})
        if path == "/api/catalog/summary":
            return self.json(200, self.catalog_summary(refresh=refresh, profile=profile))
        if path == "/api/catalog/export":
            return self.export_catalog(refresh=refresh, kind=kind, profile=profile)
        if path == "/api/catalog":
            try:
                items, categories = self.api.catalog(refresh=refresh, kind=kind)
            except UpstreamError as exc:
                return self.json(502, {"error": str(exc)})
            return self.json(200, catalog_payload(items, categories, self.api, kind))
        if path == "/api/streams":
            return self.json(200, self.streams_status(profile))
        if path == "/api/history":
            entries = self.streams.history() if self.streams is not None else []
            if profile:  # profil : seulement les lectures de son abonnement (anciennes entrées : profil 1)
                entries = [e for e in entries if str(e.get("provider") or "1") == profile]
            return self.json(200, history_payload(entries))
        if path == "/api/devices":
            return self.devices_status()
        if path == "/api/providers":
            return self.json(200, self.providers_status())
        if path == "/api/settings":
            return self.json(200, self.settings_status())
        if path == "/api/backup":
            return self.backup()
        if path == "/api/epg":
            return self.epg_status(qs.get("slug", [""])[0] or None)
        creds = CREDENTIALS_API.fullmatch(path)
        if creds:
            return self.provider_credentials(int(creds.group(1)))
        if path == "/api/tunnel":
            return self.json(200, self.tunnel_status())
        if path == "/api/alerts":
            return self.json(200, self.alerts_status())
        if path == "/api/epg/suggest":
            return self.epg_suggest(qs.get("slug", [""])[0])
        if path == "/api/epg/groups":
            return self.epg_groups(qs.get("slug", [""])[0])
        if path == "/api/epg/search":
            return self.epg_search(qs.get("q", [""])[0])
        if path == "/api/now":
            return self.now_on_tv(qs)
        series_match = SERIES_API.fullmatch(path)
        if series_match and self.series is not None:
            episodes = self.series.episodes(series_match.group(1), refresh=refresh, timeout=30)
            if episodes is None:
                return self.json(502, {"error": "Épisodes indisponibles (get_series_info)"})
            return self.json(200, {"series_id": series_match.group(1), "episodes": episodes})
        return self.json(404, {"error": "Route introuvable"})

    # ------------------------------------------------------------ réglages
    def settings_status(self) -> dict:
        store = settings.current()
        values = store.get() if store is not None else dict(settings.DEFAULTS)
        ip, remote = access.client_address(self.client_address[0], self.headers, self.config.proxies(),
                                           settings.local_networks(self.config))
        tailscale = any(access._in(access._ip(ip), [n]) for n in settings.TAILSCALE)
        return {**values, "editable": store is not None, "client_ip": ip, "client_tailscale": tailscale,
                "remote_access": self.config.remote_access, "epg_env_sources": list(self.config.epg_extra_urls)}

    # ---------------------------------------------------------- abonnements
    def providers_editable(self) -> bool:
        from .providers import ApiHolder
        return isinstance(self.api, ApiHolder) and provider_store.current() is not None

    def _provider_max_connections(self, index: int) -> int:
        """Nombre de connexions simultanées autorisées pour l'abonnement ``index`` (0 si inconnu)."""
        if self.config.max_connections > 0:
            return self.config.max_connections
        apis = getattr(self.api, "apis", None)
        indices = getattr(self.api, "indices", None)
        target = dict(zip(indices, apis)).get(index) if apis and indices else (self.api if index == 1 else None)
        if target is not None:
            try:
                acct = target.account()
                val = int((acct or {}).get("max_connections") or 0)
                if val > 0:
                    return val
            except Exception:
                pass
            hint = target.max_connections_hint()
            if hint > 0:
                return hint
        return self.streams.limit(str(index)) if self.streams is not None else 0

    def providers_status(self) -> dict:
        """Abonnements : ceux de la configuration (lecture seule) puis ceux de l'interface (sans secret)."""
        from urllib.parse import urlsplit as _split
        config = self.config
        rows = [{"index": 1, "name": config.provider_name or "Fournisseur 1",
                 "host": _split(config.xtream_url).netloc, "source": "configuration"}]
        from .server import DEFAULT_USER_AGENT
        agents = dict(config.provider_user_agents)
        rows[0]["user_agent"] = "" if config.user_agent == DEFAULT_USER_AGENT else config.user_agent
        for offset, (name, url, _, _) in enumerate(config.extra_providers, start=2):
            rows.append({"index": offset, "name": name, "host": _split(url).netloc, "source": "configuration",
                         "user_agent": agents.get(offset) or rows[0]["user_agent"]})
        active = set(getattr(self.api, "indices", None) or [1])
        store = provider_store.current()
        try:
            stored = store.list() if store is not None else []
        except RuntimeError:
            stored = []
        for entry in stored:
            row = provider_store.public(entry)
            if entry["index"] not in active or any(r["index"] == entry["index"] for r in rows):
                row["error"] = "Numéro déjà pris par la configuration du conteneur : abonnement ignoré"
            rows.append(row)
        mixable = settings.mixable()
        aux = settings.aux_providers()
        res_map = settings.reservations()
        for row in rows:
            idx = row["index"]
            row["mixable"] = mixable is None or str(idx) in mixable
            row["aux"] = str(idx) in aux and idx != 1
            row["max_connections"] = 0 if row.get("error") else self._provider_max_connections(idx)
            row["reservation"] = res_map.get(str(idx))
        return {"editable": self.providers_editable(), "mix_editable": settings.current() is not None, "providers": rows}

    def reload_providers(self) -> None:
        """Applique tout de suite les abonnements enregistrés (sans redémarrer le conteneur)."""
        from .server import make_api
        self.api.swap(make_api(self.config, provider_store.current(), self.api, migrate=False))

    def test_provider(self, url: str, username: str, password: str,
                      user_agent: str = "") -> tuple[str | None, dict | None]:
        """Vérifie l'abonnement auprès du fournisseur ; retourne ``(erreur_ou_None, résumé_compte)``."""
        from dataclasses import replace
        from .server import XtreamAPI
        probe = XtreamAPI(replace(self.config, xtream_url=url, username=username, password=password, extra_providers=(),
                                  provider_user_agents=(), user_agent=user_agent or self.config.user_agent))
        account = probe.account()
        if not account:
            return "Connexion refusée : vérifie l'adresse et les identifiants (aucune réponse valide du fournisseur)", None
        if str(account.get("status") or "Active").lower() not in ("active", ""):
            return f"Abonnement « {account.get('status')} » chez le fournisseur", None
        return None, account

    def _apply_reservation(self, index: int, raw: object, max_connections: int = 0) -> None:
        """Enregistre la réservation de connexions d'un abonnement (Option B garantie souple)."""
        if settings.current() is None:
            return
        if raw is None or not isinstance(raw, dict):
            settings.set_reservation(index, None, 0)
            return
        try:
            count = int(raw.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            settings.set_reservation(index, None, 0)
            return
        prof = str(raw.get("profile") or "").strip()
        if not prof or prof == "self":
            prof = str(index)
        if not prof.isdigit() or int(prof) not in settings.INDICES:
            raise ValueError("Espace bénéficiaire de la réservation invalide")
        if max_connections > 0:
            count = min(count, max_connections)
        settings.set_reservation(index, prof, count)
        # S'il reste des connexions libres pour le secours, activer automatiquement le mélange.
        if max_connections > 0 and 0 < count < max_connections:
            settings.set_mixable(index, True)

    def provider_probe(self) -> None:
        """Teste un abonnement sans l'enregistrer et renvoie son nombre de connexions simultanées."""
        body = self.read_payload()
        if body is None:
            return
        from .server import normalize_xtream_url
        edit_idx = body.get("index")
        current = None
        if isinstance(edit_idx, (int, str)) and str(edit_idx).isdigit():
            store = provider_store.current()
            if store is not None:
                try:
                    current = next((p for p in store.list() if p["index"] == int(edit_idx)), None)
                except RuntimeError:
                    current = None
        try:
            raw_url = str(body.get("url") or "").strip()
            url = normalize_xtream_url(raw_url) if raw_url else (current or {}).get("url", "")
            username = str(body.get("username") or "").strip() or (current or {}).get("username", "")
            password = str(body.get("password") or "") or (current or {}).get("password", "")
            if not url or not username or not password:
                return self.json(400, {"error": "Adresse, utilisateur et mot de passe obligatoires"})
            agent = provider_store._clean(body["user_agent"], "User agent", required=False) \
                if "user_agent" in body else (current or {}).get("user_agent", "")
        except (provider_store.InvalidProvider, ValueError) as exc:
            return self.json(400, {"error": str(exc)})
        error, account = self.test_provider(url, username, password, agent)
        if error:
            return self.json(400, {"error": error})
        try:
            max_cons = int((account or {}).get("max_connections") or 0)
        except (TypeError, ValueError):
            max_cons = 0
        try:
            active_cons = int((account or {}).get("active_cons") or 0)
        except (TypeError, ValueError):
            active_cons = 0
        return self.json(200, {
            "ok": True,
            "status": (account or {}).get("status") or "Active",
            "max_connections": max_cons,
            "active_cons": active_cons,
            "exp_date": (account or {}).get("exp_date"),
        })

    def provider_credentials(self, index: int) -> None:
        """Utilisateur et mot de passe d'un abonnement, à la demande (bouton « Afficher »).

        Réseau local seulement (jamais par un accès distant, même avec mot de passe), en-tête de l'interface
        exigé, jamais mis en cache ni écrit dans le journal.
        """
        if not self.same_origin():
            return
        _, remote = access.client_address(self.client_address[0], self.headers, self.config.proxies(),
                                          settings.local_networks(self.config))
        if remote:
            return self.json(403, {"error": "Identifiants affichables seulement depuis le réseau local"})
        found = None
        if index == 1:
            found = (self.config.username, self.config.password)
        elif index - 2 < len(self.config.extra_providers):
            _, _, user, password = self.config.extra_providers[index - 2]
            found = (user, password)
        else:
            store = provider_store.current()
            try:
                entry = next((p for p in store.list() if p["index"] == index), None) if store else None
            except RuntimeError:
                entry = None
            if entry:
                found = (entry["username"], entry["password"])
        if found is None:
            return self.json(404, {"error": "Abonnement introuvable"})
        return self.json(200, {"index": index, "username": found[0], "password": found[1]})

    def provider_mix(self, index: int) -> None:
        """Rôle d'un abonnement (espace membre séparé ou complément/secours), mélange et réservation."""
        body = self.read_payload()
        if body is None:
            return
        if index not in (getattr(self.api, "indices", None) or [1]):
            return self.json(404, {"error": "Abonnement introuvable"})
        if settings.current() is None:
            return self.json(409, {"error": "Réglages non modifiables dans ce mode de démarrage"})
        try:
            if "aux" in body:
                settings.set_aux(index, bool(body["aux"]))
            if "mixable" in body:
                settings.set_mixable(index, bool(body["mixable"]))
            if "reservation" in body:
                self._apply_reservation(index, body.get("reservation"), self._provider_max_connections(index))
        except ValueError as exc:
            return self.json(400, {"error": str(exc)})
        except OSError:
            return self.json(500, {"error": "Impossible d'enregistrer /data/settings.json"})
        mix_set = settings.mixable()
        aux_set = settings.aux_providers()
        return self.json(200, {
            "index": index,
            "mixable": mix_set is None or str(index) in mix_set,
            "aux": str(index) in aux_set and index != 1,
            "reservation": settings.reservations().get(str(index)),
        })

    def provider_write(self, method: str, index: int | None) -> None:
        if method != "DELETE":
            body = self.read_payload()
            if body is None:
                return
        elif not self.same_origin():
            return
        if not self.providers_editable():
            return self.json(409, {"error": "Abonnements non modifiables ici : redémarre le conteneur après avoir "
                                            "changé la configuration"})
        from .server import normalize_xtream_url
        store = provider_store.current()
        reserved = set(range(1, len(self.config.extra_providers) + 2))
        try:
            if method == "DELETE":
                if not store.delete(index):
                    return self.json(404, {"error": "Abonnement introuvable (ceux de la configuration ne se suppriment pas ici)"})
                if settings.current() is not None:
                    settings.set_mixable(index, False)   # numéro réutilisable : pas de réglage hérité
                    settings.set_aux(index, False)
                    settings.clear_reservations_for(index)
                self.reload_providers()
                return self.json(200, {"deleted": True})
            current = next((p for p in store.list() if p["index"] == index), None) if index else None
            if method == "PUT" and current is None:
                return self.json(404, {"error": "Abonnement introuvable (ceux de la configuration se modifient dans le conteneur)"})
            raw_url = str(body.get("url") or "").strip()
            url = normalize_xtream_url(raw_url) if raw_url else (current or {}).get("url", "")
            username = str(body.get("username") or "").strip() or (current or {}).get("username", "")
            password = str(body.get("password") or "") or (current or {}).get("password", "")
            if not url or not username or not password:
                return self.json(400, {"error": "Adresse, utilisateur et mot de passe obligatoires"})
            others = [(self.config.xtream_url, self.config.username)] + [(u, n) for _, u, n, _ in self.config.extra_providers]
            others += [(p["url"], p["username"]) for p in store.list() if p["index"] != index]
            if (url, username) in others:
                return self.json(400, {"error": "Cet abonnement est déjà configuré"})
            agent = provider_store._clean(body["user_agent"], "User agent", required=False) \
                if "user_agent" in body else (current or {}).get("user_agent", "")
            changed = current is None or (url, username, password) != (current["url"], current["username"], current["password"])
            probed_max = 0
            if changed or agent != (current or {}).get("user_agent", ""):
                error, account = self.test_provider(url, username, password, agent)
                if error:
                    return self.json(400, {"error": error})
                try:
                    probed_max = int((account or {}).get("max_connections") or 0)
                except (TypeError, ValueError):
                    probed_max = 0
            if method == "POST":
                entry = store.add(name=body.get("name"), url=url, username=username, password=password, reserved=reserved,
                                  user_agent=agent)
                if settings.current() is not None:
                    settings.set_mixable(entry["index"], bool(body.get("mixable")))
                    if "aux" in body:
                        settings.set_aux(entry["index"], bool(body.get("aux")))
                    if "reservation" in body:
                        self._apply_reservation(entry["index"], body.get("reservation"), probed_max)
            else:
                entry = store.update(index, name=body.get("name"), url=url, username=username,
                                     password=password if changed else "", user_agent=agent)
                if settings.current() is not None:
                    if "aux" in body:
                        settings.set_aux(index, bool(body.get("aux")))
                    if "mixable" in body:
                        settings.set_mixable(index, bool(body.get("mixable")))
                    if "reservation" in body:
                        max_c = probed_max or self._provider_max_connections(index)
                        self._apply_reservation(index, body.get("reservation"), max_c)
            self.reload_providers()
            pub = provider_store.public(entry)
            pub["aux"] = str(entry["index"]) in settings.aux_providers() and entry["index"] != 1
            pub["max_connections"] = probed_max or self._provider_max_connections(entry["index"])
            pub["reservation"] = settings.reservations().get(str(entry["index"]))
            return self.json(201 if method == "POST" else 200, pub)
        except (provider_store.InvalidProvider, ValueError) as exc:
            return self.json(400, {"error": str(exc)})
        except RuntimeError:
            return self.json(500, {"error": "Impossible d'enregistrer /data/providers.json"})

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
                return self.json(201, self.devices.create(
                    body.get("name"), body.get("playlist", ""), body.get("username"),
                    body.get("priority", "normal"), body.get("ip", ""), body.get("links", "lan"),
                    body.get("profile", ""),
                ))
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
            with_devices = bool(body.get("include_devices") and isinstance(backup.get("devices"), dict)
                                and self.devices is not None)
            if with_devices:
                self.devices.restore(backup["devices"], dry_run=True)
            result = self.store.restore(backup["playlists"], mode=mode)
            if with_devices:
                self.devices.restore(backup["devices"])
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
        profile = parse_qs(urlsplit(self.path).query).get("profile", [""])[0]
        results = run(self.api, self.epg, self.series, self.streams, self.store, self.config, self.devices,
                      profile=profile if profile.isdigit() else None)
        return self.json(200, {"started_at": int(started), "seconds": round(time.time() - started, 1),
                               "version": __version__, "results": results})

    def streams_status(self, profile: str = "") -> dict:
        if self.streams is None:
            return {"limit": 0, "auto": False, "takeover": False, "streams": []}
        streams = self.streams.list()
        if profile:
            streams = [s for s in streams if str(s.get("provider")) == profile]
        return {"limit": self.streams.limit(profile or "1"), "auto": self.config.max_connections < 0,
                "takeover": self.streams.takeover, "streams": streams,
                "reservation": settings.reservations().get(profile or "1")}

    def catalog_summary(self, *, refresh: bool, profile: str = "") -> dict:
        """Nombre d'éléments et de catégories par type (léger : ne renvoie pas les listes)."""
        multi = len(getattr(self.api, "apis", ())) > 1
        result = {}
        for kind in KINDS:
            try:
                items, categories = self.api.catalog(refresh=refresh, kind=kind)
                if profile and multi:
                    items = [i for i in items if self.api.provider_of(i["stream_id"]) == profile]
                    categories = {cid: n for cid, n in categories.items()
                                  if cid == "none" or self.api.provider_of(cid) == profile}
                result[kind] = {"items": len(items), "categories": len(categories), "error": None}
            except UpstreamError as exc:
                result[kind] = {"items": None, "categories": None, "error": str(exc)}
        return result

    def export_catalog(self, *, refresh: bool, kind: str = "live", profile: str = "") -> None:
        """Copie de la structure d'origine (réponses brutes ``player_api.php``), identifiants masqués."""
        multi = len(getattr(self.api, "apis", ())) > 1
        target_api = self.api
        if profile and multi:
            by_index = dict(zip(getattr(self.api, "indices", ()), getattr(self.api, "apis", ())))
            target_api = by_index.get(int(profile), self.api)
        try:
            items, categories = target_api.catalog(refresh=refresh, kind=kind)
        except UpstreamError as exc:
            return self.json(502, {"error": str(exc)})
        st = target_api.kind_state(kind)
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
            # Adresses publiques pour les appareils « externes » : tunnel Cloudflare, reverse proxy déclarés
            # (Système → Accès à distance), sinon PUBLIC_BASE_URL. La première sert par défaut.
            "public_addresses": public_addresses(self.config),
            "external_base": (public_addresses(self.config) or [{"url": ""}])[0]["url"],
            "epg_enabled": self.config.enable_epg,
            "token_required": bool(self.config.access_token) or bool(self.devices and self.devices.require_token),
            "web_auth": bool(self.config.web_password),
            "catalog_updated_at": int(self.api.updated_at) if self.api.updated_at else None,
            "catalog_error": self.api.last_error,
            "categories_error": self.api.categories_error,
            "data_file": str(self.store.path),
            "series_cache": self.series.stats() if self.series is not None else None,
            "epg_mode": self.config.epg_source,
            "logos": logos.current().status() if logos.current() else None,
            "profiles": profiles(self.api, self.config, self.store),
            "sources": sources(self.api, self.config),
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

    def playlist_preview(self) -> None:
        """Aperçu instantané d'une playlist (même avant enregistrement) : groupes et éléments dans l'ordre final."""
        body = self.read_payload()
        if body is None:
            return
        draft = dict(body)
        raw_slug = str(draft.get("slug") or "").strip()
        draft["slug"] = raw_slug if SLUG_RE.fullmatch(raw_slug) else "apercu"
        draft["name"] = str(draft.get("name") or "").strip()[:100] or "Aperçu"
        try:
            saved = validate_playlist(draft)
        except InvalidPlaylist as exc:
            return self.json(400, {"error": str(exc)})
        entries: list[tuple[dict, str]] = []
        groups: dict[str, str] = {}
        counts = {"live": 0, "vod": 0, "series": 0, "backups": 0}
        for kind in ("live", "vod", "series"):
            if not uses(saved, kind):
                continue
            try:
                selected, kind_groups = select_items(self.api, saved, kind)
            except UpstreamError as exc:
                return self.json(502, {"error": str(exc)})
            groups.update(kind_groups)
            counts[kind] = len(selected)
            if kind == "live":
                counts["backups"] = sum(1 for item, _ in selected if item.get("alternates"))
            entries.extend(selected)
        ordered = order_entries(add_favorites(entries, groups, saved), groups, saved.get("group_order"))
        custom_names = saved.get("group_names") or {}
        grouped: dict[str, dict] = {}
        total_emitted = 0
        for item, gid in ordered:
            kind = item.get("kind", "live")
            ui_kind = "vod" if kind == "movie" else ("series" if kind == "series" else "live")
            g = grouped.get(gid)
            if g is None:
                g = {
                    "id": gid,
                    "name": custom_names.get(gid) or groups.get(gid) or ("Sans catégorie" if gid == "none" else "TV"),
                    "kind": "favorites" if gid == FAVORITES_GROUP else ui_kind,
                    "custom": gid.startswith(CUSTOM_PREFIX),
                    "total": 0,
                    "items": [],
                }
                grouped[gid] = g
            g["total"] += 1
            if len(g["items"]) < 120 and total_emitted < 1500:
                row = {
                    "id": item["stream_id"],
                    "kind": ui_kind,
                    "name": item.get("name", ""),
                    "logo": display_logo(item),
                    "num": item.get("num", "") if ui_kind == "live" else "",
                }
                if ui_kind == "live":
                    if item.get("alternates"):
                        row["backups"] = len(item["alternates"])
                    if item.get("archive"):
                        row["archive"] = item["archive"]
                    if item.get("epg_id") or item.get("epg_prefer_external") or item.get("epg_manual"):
                        row["epg"] = True
                elif item.get("year"):
                    row["year"] = item["year"]
                g["items"].append(row)
                total_emitted += 1
        counts["groups"] = len(grouped)
        return self.json(200, {"counts": counts, "groups": list(grouped.values())})

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
        if path == "/api/playlists/preview":
            return self.playlist_preview()
        if path == "/api/devices":
            return self.device_write("POST", None)
        if path == "/api/providers/probe":
            return self.provider_probe()
        if path == "/api/providers":
            return self.provider_write("POST", None)
        if path == "/api/restore":
            return self.restore()
        if path == "/api/diagnostic":
            return self.diagnostic()
        if path.startswith("/api/tunnel/"):
            return self.tunnel_action(path.rsplit("/", 1)[-1])
        if path.startswith("/api/alerts/"):
            return self.alerts_action(path.rsplit("/", 1)[-1])
        if path == "/api/history/clear":
            if not self.same_origin():
                return
            profile = parse_qs(urlsplit(self.path).query).get("profile", [""])[0]
            if self.streams is not None:
                self.streams.clear_history(profile if profile.isdigit() else None)
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
        provider = PROVIDER_API.fullmatch(urlsplit(self.path).path)
        if provider:
            return self.provider_write("PUT", int(provider.group(1)))
        mix = MIX_API.fullmatch(urlsplit(self.path).path)
        if mix:
            return self.provider_mix(int(mix.group(1)))
        if urlsplit(self.path).path == "/api/settings":
            body = self.read_payload()
            if body is None:
                return
            store = settings.current()
            if store is None:
                return self.json(409, {"error": "Réglages non modifiables dans ce mode de démarrage"})
            try:
                store.update(body)
            except OSError:
                return self.json(500, {"error": "Impossible d'enregistrer /data/settings.json"})
            return self.json(200, self.settings_status())
        if urlsplit(self.path).path == "/api/alerts":
            return self.alerts_update()
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
        """Après un enregistrement : charge en arrière-plan les épisodes des séries sélectionnées (borné)."""
        if self.series is None or not uses(saved, "series"):
            return

        def job() -> None:
            try:
                entries, _ = select_items(self.api, saved, "series")
            except UpstreamError:
                return
            # Borné pour éviter une rafale de milliers d'appels sur un gros catalogue (le reste passe par la nuit).
            ids = [item["stream_id"] for item, _ in entries][:200]
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
                stats.pop("sources", None)   # détail par chaîne : /api/epg/groups
                data["playlist"] = stats
            except (UpstreamError, EpgUnavailable) as exc:
                data["error"] = str(exc)
            data["source"] = self.epg.status()
        return self.json(200, data)

    # ------------------------------------------------------- tunnel Cloudflare
    def tunnel_status(self) -> dict:
        current = tunnel.current()
        if current is None:
            return {"available": False, "managed": False}
        return {**current.status(), "managed": True, "port": self.config.port}

    def tunnel_action(self, action: str) -> None:
        """Connexion au compte Cloudflare, création / démarrage / arrêt / suppression du tunnel."""
        if action in ("setup",):
            body = self.read_payload()
            if body is None:
                return
        elif not self.same_origin():
            return
        current = tunnel.current()
        if current is None:
            return self.json(409, {"error": "Tunnel indisponible dans ce mode de démarrage"})
        try:
            if action == "login":
                url = current.login()
                return self.json(200, {**self.tunnel_status(), "login_url": url})
            if action == "setup":
                current.setup(body.get("hostname"))
            elif action == "start":
                current.start()
            elif action == "stop":
                current.stop()
            elif action == "remove":
                current.remove()
            elif action == "logout":
                current.logout()
            else:
                return self.json(404, {"error": "Action inconnue"})
        except tunnel.TunnelError as exc:
            return self.json(400, {"error": str(exc)})
        except OSError:
            return self.json(500, {"error": "Impossible d'écrire dans /data/cloudflared"})
        return self.json(200, self.tunnel_status())

    # ------------------------------------------------------- alertes Telegram
    def alerts_status(self) -> dict:
        mgr = alerts.current()
        if mgr is None:
            return {"available": False, "connected": False, "enabled": False,
                    "events": dict(alerts.DEFAULT_EVENTS), "pairing": None}
        return {"available": True, **mgr.status()}

    def alerts_action(self, action: str) -> None:
        if action in ("pair", "manual"):
            body = self.read_payload()
            if body is None:
                return
        elif not self.same_origin():
            return
        mgr = alerts.current()
        if mgr is None:
            return self.json(409, {"error": "Alertes Telegram indisponibles dans ce mode de démarrage"})
        try:
            if action == "pair":
                return self.json(200, {"available": True, **mgr.start_pairing(body.get("bot_token", ""))})
            if action == "cancel":
                return self.json(200, {"available": True, **mgr.cancel_pairing()})
            if action == "manual":
                return self.json(200, {"available": True, **mgr.connect_manual(
                    body.get("bot_token", ""), body.get("chat_id", ""))})
            if action == "test":
                return self.json(200, {"available": True, **mgr.send_test()})
            if action == "disconnect":
                return self.json(200, {"available": True, **mgr.disconnect()})
            return self.json(404, {"error": "Action inconnue"})
        except alerts.AlertError as exc:
            return self.json(400, {"error": str(exc)})
        except OSError:
            return self.json(500, {"error": "Impossible d'enregistrer /data/alerts.json"})

    def alerts_update(self) -> None:
        body = self.read_payload()
        if body is None:
            return
        mgr = alerts.current()
        if mgr is None:
            return self.json(409, {"error": "Alertes Telegram indisponibles dans ce mode de démarrage"})
        try:
            state = mgr.update_options(
                enabled=body.get("enabled") if "enabled" in body else None,
                events=body.get("events") if "events" in body else None,
            )
            return self.json(200, {"available": True, **state})
        except OSError:
            return self.json(500, {"error": "Impossible d'enregistrer /data/alerts.json"})

    def epg_guides(self) -> list[tuple[str, str, object]]:
        """Guides disponibles : ``[(clé, libellé, EpgCache)]`` (fournisseurs, puis guides externes)."""
        guides = []
        if self.config.epg_source != "api":
            guides.append(("provider", "Guide du fournisseur", self.epg))
            for pid, pcache in sorted(getattr(self.epg, "provider_caches", {}).items(), key=lambda e: int(e[0])):
                label = getattr(pcache, "label", "") or f"abonnement {pid}"
                guides.append((f"provider:{pid}", f"Guide {label}", pcache))
        for n, extra in enumerate(self.epg.extras, start=1):
            host = urlsplit(getattr(extra, "label", "") or "").netloc or f"guide {n}"
            guides.append((f"external:{n}", f"Guide externe {n} ({host})", extra))
        return guides

    def epg_groups(self, slug: str) -> None:
        """Guide de chaque chaîne de « Mes groupes » : d'où il vient, rapprochement manuel possible."""
        if self.epg is None or not self.config.enable_epg:
            return self.json(409, {"error": "EPG désactivé (ENABLE_EPG=false)"})
        if not SLUG_RE.fullmatch(slug or ""):
            return self.json(400, {"error": "Playlist invalide"})
        try:
            saved = self.store.get(slug)
        except RuntimeError:
            return self.json(500, {"error": "Sauvegarde illisible"})
        if saved is None:
            return self.json(404, {"error": "Playlist introuvable"})
        try:
            ordered = live_channels_in_order(self.api, saved, with_groups=True)
            _, stats = self.epg.playlist_guide(slug, [c for c, _ in ordered], budget=0)
        except (UpstreamError, EpgUnavailable) as exc:
            return self.json(502, {"error": str(exc)})
        guides = {key: (label, cache) for key, label, cache in self.epg_guides()}
        names = {g["id"]: g.get("name") or g["id"] for g in saved.get("custom_groups") or []}
        manual = saved.get("item_xmltv") or {}
        rows = []
        for channel, group in ordered:
            if not group.startswith(CUSTOM_PREFIX):
                continue
            found = stats["sources"].get(channel["stream_id"], {"source": "none", "id": ""})
            label, cache = guides.get(found["source"], ("", None))
            xid = found["id"] or (channel.get("epg_id") or "").lower()
            rows.append({
                "stream_id": channel["stream_id"], "name": channel["name"],
                "group": names.get(group[len(CUSTOM_PREFIX):], group[len(CUSTOM_PREFIX):]),
                "source": found["source"], "source_label": label or {"api": "API du fournisseur",
                                                                     "none": "Aucun programme"}.get(found["source"], ""),
                "id": xid, "guide_name": (cache.labels().get(xid, "") if cache is not None and xid else ""),
                "manual": channel["stream_id"] in manual,
            })
        return self.json(200, {"external_guides": len(self.epg.extras), "channels": rows,
                               "group_epg_external": saved.get("group_epg_external", True)})

    def epg_search(self, query: str) -> None:
        """Chaînes des guides (fournisseur et externes) dont le nom contient ``q`` : choix manuel du guide."""
        from .builder import match_name
        from .epg import squash_name
        if self.epg is None or not self.config.enable_epg:
            return self.json(409, {"error": "EPG désactivé (ENABLE_EPG=false)"})
        wanted = squash_name(match_name(query or ""))
        if len(wanted) < 2:
            return self.json(400, {"error": "Tape au moins 2 lettres"})
        results = []
        for key, label, cache in self.epg_guides():
            try:
                labels, counts = cache.labels(), cache.index()
            except Exception:
                continue
            for cid, name in labels.items():
                if wanted in squash_name(match_name(name)) or wanted in cid:
                    results.append({"id": cid, "name": name, "source": key, "source_label": label,
                                    "programmes": counts.get(cid, 0)})
        results.sort(key=lambda r: (r["programmes"] == 0, len(r["name"]), r["name"].lower()))
        return self.json(200, {"results": results[:40]})

    def epg_suggest(self, slug: str) -> None:
        """Chaînes sans programme d'une playlist et identifiants XMLTV de noms proches (à valider dans l'éditeur)."""
        from .epg import suggest_guides
        if self.epg is None or not self.config.enable_epg:
            return self.json(409, {"error": "EPG désactivé (ENABLE_EPG=false)"})
        if not SLUG_RE.fullmatch(slug or ""):
            return self.json(400, {"error": "Playlist invalide"})
        try:
            saved = self.store.get(slug)
        except RuntimeError:
            return self.json(500, {"error": "Sauvegarde illisible"})
        if saved is None:
            return self.json(404, {"error": "Playlist introuvable"})
        try:
            ordered = live_channels_in_order(self.api, saved, with_groups=True)
            _, stats = self.epg.playlist_guide(slug, [c for c, _ in ordered], budget=0)
        except (UpstreamError, EpgUnavailable) as exc:
            return self.json(502, {"error": str(exc)})
        sources = [(label, cache) for _, label, cache in self.epg_guides()]
        suggestions = suggest_guides(sources, stats["missing"])
        return self.json(200, {"missing": len(stats["missing"]), "suggestions": suggestions})

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
            info = {"id": channel["stream_id"], "name": channel["name"], "logo": display_logo(channel),
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
                for pcache in getattr(self.epg, "provider_caches", {}).values():
                    try:
                        pcache.ensure(refresh=True)
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
        provider = PROVIDER_API.fullmatch(urlsplit(self.path).path)
        if provider:
            return self.provider_write("DELETE", int(provider.group(1)))
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


def public_addresses(config) -> list[dict]:
    """Adresses publiques du port des lecteurs : ``[{"url", "source"}]`` (tunnel, reverse proxy, PUBLIC_BASE_URL)."""
    result = []
    if tunnel.hostname():
        result.append({"url": f"https://{tunnel.hostname()}", "source": "tunnel"})
    for host in settings.public_hosts():
        if not any(r["url"] == f"https://{host}" for r in result):
            result.append({"url": f"https://{host}", "source": "proxy"})
    if not result and config.public_base_url:
        result.append({"url": config.public_base_url.rstrip("/"), "source": "config"})
    return result


def sources(api, config) -> list[dict]:
    """Tous les abonnements configurés : espaces membres et abonnements de complément/secours."""
    names = getattr(api, "names", None) or [config.provider_name or "Mon abonnement"]
    indices = getattr(api, "indices", None) or list(range(1, len(names) + 1))
    mixable = settings.mixable()
    aux = settings.aux_providers()
    return [{"id": str(i), "name": name,
             "mixable": mixable is None or str(i) in mixable,
             "aux": str(i) in aux and int(i) != 1}
            for i, name in zip(indices, names)]


def profiles(api, config, store: PlaylistStore | None = None) -> list[dict]:
    """Espaces membres : abonnements ayant leur propre espace (hors compléments/secours sans playlist propre)."""
    all_sources = sources(api, config)
    used: set[str] = set()
    if store is not None:
        try:
            used = {str(p.get("provider") or "1") for p in store.list()}
        except RuntimeError:
            used = set()
    return [s for s in all_sources if s["id"] == "1" or not s["aux"] or s["id"] in used]


def catalog_payload(channels: list[dict], categories: dict[str, str], api: XtreamAPI | None = None,
                    kind: str = "live") -> dict:
    """Catalogue pour le navigateur : aucun identifiant fournisseur, toutes les catégories avec compteurs.

    Même structure pour le direct, les films et les séries (clé ``channels`` = éléments).
    """
    st = api.kind_state(kind) if api else None
    multi = len(getattr(api, "apis", ())) > 1
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
        if multi:
            entry["profile"] = api.provider_of(cid) if cid != "none" else "1"
        category_list.append(entry)
    channel_list = []
    library = logos.current() if kind == "live" else None
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
        if multi:
            entry["profile"] = api.provider_of(c["stream_id"])
        if kind == "live":
            regional = regional_group_key(c["name"], categories.get(c["category_id"], ""))
            if regional[0]:
                entry["regional"] = regional
        hq = library.match(c["name"], categories.get(c["category_id"], "")) if library else None
        if hq:
            entry["logo_hq"] = f"/logo/{hq}.png"
        for extra in ("ext", "year", "rating", "archive", "adult"):
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
                    "streams": streams, "devices": devices, "guard": access.FailureGuard()})
    server = ThreadingHTTPServer((host, config.web_port if port is None else port), handler)
    server.daemon_threads = True
    return server
