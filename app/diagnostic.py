"""Diagnostic en un clic : teste chaque brique du fournisseur et du serveur local.

Chaque test renvoie ``{id, label, status: ok|warn|error|skipped, ms, detail}``. Aucun détail ne
contient d'URL ni d'identifiant. Les tests de flux ouvrent brièvement une connexion chez le
fournisseur : ils sont **ignorés si une lecture est en cours** pour ne pas couper un appareil.
"""
from __future__ import annotations

import logging
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger("xtream-m3u-local.diagnostic")
TS_SYNC = 0x47


class Probe:
    def __init__(self, status: int | None, head: bytes, headers: dict, ms: int, error: str | None = None):
        self.status, self.head, self.headers, self.ms, self.error = status, head, headers, ms, error


def probe(url: str, user_agent: str, *, read: int = 4096, range_end: int | None = None, timeout: float = 12) -> Probe:
    """Ouvre une URL, lit les premiers octets puis ferme (aucune URL dans le résultat)."""
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    if range_end is not None:
        headers["Range"] = f"bytes=0-{range_end}"
    started = time.monotonic()
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            data = b""
            deadline = time.monotonic() + timeout
            reader = getattr(response, "read1", response.read)
            while len(data) < read and time.monotonic() < deadline:
                chunk = reader(read - len(data))
                if not chunk:
                    break
                data += chunk
            return Probe(response.status, data, dict(response.headers), int((time.monotonic() - started) * 1000))
    except HTTPError as exc:
        return Probe(exc.code, b"", {}, int((time.monotonic() - started) * 1000), f"HTTP {exc.code}")
    except (URLError, TimeoutError, OSError, HTTPException, ValueError) as exc:
        reason = "délai dépassé" if isinstance(exc, TimeoutError) or "timed out" in str(exc) else "réseau"
        return Probe(None, b"", {}, int((time.monotonic() - started) * 1000), reason)


def _result(test_id: str, label: str, status: str, detail: str, ms: int | None = None) -> dict:
    return {"id": test_id, "label": label, "status": status, "ms": ms, "detail": detail}


def _size(headers: dict) -> str:
    try:
        size = int(headers.get("Content-Length") or 0)
    except ValueError:
        return ""
    if not size:
        return ""
    return f", {size / 1e6:.1f} Mo" if size >= 1e6 else f", {size / 1e3:.0f} Ko"


class _ProfileStore:
    """Playlists d'un seul profil (diagnostic d'un profil : rien des autres membres)."""

    def __init__(self, store, profile: str) -> None:
        self.store, self.profile = store, profile

    def list(self) -> list[dict]:
        return [p for p in self.store.list() if str(p.get("provider") or "1") == self.profile]

    def __getattr__(self, name):
        return getattr(self.store, name)


def run(api, epg, series, streams, store, config, devices=None, profile: str | None = None) -> list[dict]:
    from .server import UpstreamError  # import local : évite une dépendance circulaire
    results: list[dict] = []
    ua = config.user_agent

    # 1. API de chaque fournisseur ---------------------------------------------------------
    indices = list(getattr(api, "indices", None) or range(1, len(getattr(api, "apis", [api])) + 1))
    providers = list(zip(getattr(api, "apis", [api]), getattr(api, "names", [None])))
    multi = len(providers) > 1
    if profile and multi and int(profile) in indices:
        # Profil = serveur à part : seulement son abonnement, ses playlists et son catalogue.
        providers = [providers[indices.index(int(profile))]]
        store = _ProfileStore(store, profile)
    else:
        profile = None
    reachable = 0
    for index, (provider_api, name) in enumerate(providers, start=1):
        label = "Connexion à l'API (player_api.php)" + (f" — {name}" if len(providers) > 1 else "")
        test_id = "account" if index == 1 else f"account-{index}"
        started = time.monotonic()
        try:
            data = provider_api._fetch(provider_api.api_url(), "user_info")
            info = data.get("user_info") if isinstance(data, dict) else None
            ms = int((time.monotonic() - started) * 1000)
            if not isinstance(info, dict) or str(info.get("auth", "1")) == "0":
                results.append(_result(test_id, label, "error", "Identifiants refusés par le fournisseur", ms))
                continue
            exp = str(info.get("exp_date") or "")
            expires = time.strftime("%d/%m/%Y", time.localtime(int(exp))) if exp.isdigit() else "inconnue"
            results.append(_result(test_id, label, "ok",
                                   f"Statut {info.get('status') or '?'}, expiration {expires}, "
                                   f"{info.get('active_cons') or 0}/{info.get('max_connections') or '?'} connexion(s)", ms))
            reachable += 1
        except UpstreamError as exc:
            results.append(_result(test_id, label, "error", str(exc), int((time.monotonic() - started) * 1000)))
    if not reachable:
        return results  # aucun fournisseur joignable : inutile d'aller plus loin

    # 2. Catalogues -----------------------------------------------------------------------
    labels = {"live": ("Catalogue TV en direct", "chaînes"), "vod": ("Catalogue films", "films"),
              "series": ("Catalogue séries", "séries")}
    catalogs: dict[str, list[dict]] = {}
    for kind, (label, unit) in labels.items():
        started = time.monotonic()
        try:
            items, categories = api.catalog(refresh=True, kind=kind)
            ms = int((time.monotonic() - started) * 1000)
            if profile:
                items = [i for i in items if api.provider_of(i["stream_id"]) == profile]
                categories = {cid: n for cid, n in categories.items() if api.provider_of(cid) == profile}
            catalogs[kind] = items
            state = api.kind_state(kind)
            status = "ok" if items else "warn"
            detail = f"{len(items)} {unit}, {len(categories)} catégories"
            if state.categories_error:
                status, detail = "warn", detail + f" — {state.categories_error}"
            elif not items:
                detail = f"Aucun élément ({unit}) pour ce compte"
            results.append(_result(f"catalog-{kind}", label, status, detail, ms))
        except UpstreamError as exc:
            catalogs[kind] = []
            results.append(_result(f"catalog-{kind}", label, "error", str(exc), int((time.monotonic() - started) * 1000)))

    live = catalogs.get("live") or []
    # Chaînes de test : celles des playlists personnalisées d'abord, sinon du catalogue ;
    # jamais les « séparateurs » (#### PRIME ####…), de préférence avec un identifiant EPG.
    from .builder import is_separator, select_items
    pool: list[dict] = []
    try:
        for saved in store.list():
            pool.extend(item for item, _ in select_items(api, saved, "live")[0])
            if len(pool) > 2000:
                break
    except Exception:
        pool = []
    pool = [c for c in (pool or live) if not is_separator(c["name"])] or [c for c in live if not is_separator(c["name"])]
    sample = next((c for c in pool if c.get("epg_id")), pool[0] if pool else None)
    with_epg = [c for c in pool if c.get("epg_id")] or pool
    step = max(1, len(with_epg) // 5)
    epg_samples = with_epg[::step][:5]

    # 2b. Bascule automatique ---------------------------------------------------------------
    # Repère les playlists mal configurées (bascule activée sans « Masquer les doublons »,
    # ou sans aucune version de secours disponible), sans attendre qu'une chaîne coupe en vrai.
    MAX_FAILOVER_CHECKS = 10
    try:
        playlists = store.list()
    except Exception:
        playlists = []
    failover_playlists = [p for p in playlists if p.get("failover")]
    for saved in failover_playlists[:MAX_FAILOVER_CHECKS]:
        name, slug = saved.get("name") or saved.get("slug") or "?", saved.get("slug") or "?"
        label = f"Bascule automatique : « {name} »"
        if not saved.get("dedupe"):
            results.append(_result(f"failover-{slug}", label, "error",
                                   "Activée sans « Masquer les doublons » dans les options de cette playlist : "
                                   "aucune version de secours n'est possible. Active aussi « Masquer les doublons »."))
            continue
        try:
            entries = select_items(api, saved, "live")[0]
        except UpstreamError as exc:
            results.append(_result(f"failover-{slug}", label, "error", str(exc)))
            continue
        total = len(entries)
        with_backup = sum(1 for item, _ in entries if item.get("alternates"))
        if not total:
            results.append(_result(f"failover-{slug}", label, "skipped", "Playlist vide pour le moment."))
        elif not with_backup:
            results.append(_result(f"failover-{slug}", label, "warn",
                                   f"0/{total} chaînes ont une version de secours : coche d'autres catégories de la "
                                   "même chaîne (ex. une autre qualité) dans « 1. Catégories », ou active « Secours "
                                   "hors catégories cochées » dans les options."))
        else:
            pct = round(with_backup / total * 100)
            results.append(_result(f"failover-{slug}", label, "ok" if pct >= 50 else "warn",
                                   f"{with_backup}/{total} chaînes ({pct} %) ont au moins une version de secours."))
    if not failover_playlists:
        results.append(_result("failover", "Bascule automatique", "skipped",
                               "Aucune playlist n'utilise la bascule automatique."))
    elif len(failover_playlists) > MAX_FAILOVER_CHECKS:
        results.append(_result("failover-more", "Bascule automatique", "skipped",
                               f"{len(failover_playlists) - MAX_FAILOVER_CHECKS} playlist(s) supplémentaire(s) "
                               "avec bascule automatique non testée(s) (limite de diagnostic)."))

    # 2c. Logos haute qualité -------------------------------------------------------------
    from . import logos as logo_lib
    library = logo_lib.current()
    hq_playlists = [p for p in playlists if p.get("hq_logos", True)]
    if library is None:
        results.append(_result("logos", "Logos haute qualité", "skipped", "Désactivés (LOGO_COUNTRIES vide)."))
    else:
        info = library.status()
        countries = ", ".join(info["countries"])
        if not info["channels"]:
            detail = ("Index en cours de téléchargement (base iptv-org)…" if info["loading"] else
                      f"Index indisponible : {info['error'] or 'pas encore téléchargé'}. Les logos du fournisseur restent utilisés.")
            results.append(_result("logos", "Logos haute qualité", "warn", detail))
        else:
            covered = total = 0
            for saved in hq_playlists[:MAX_FAILOVER_CHECKS]:
                try:
                    entries = select_items(api, saved, "live")[0]
                except Exception:
                    continue
                total += len(entries)
                covered += sum(1 for item, _ in entries if item.get("logo_hq"))
            detail = (f"{info['channels']} logos indexés ({countries}), {info['images']} image(s) en cache"
                      + (f" ; {covered}/{total} chaînes des playlists ont un logo haute qualité" if total else "") + ".")
            if info["error"]:
                detail += " Dernière mise à jour de l'index en échec (l'ancien index reste utilisé)."
            results.append(_result("logos", "Logos haute qualité", "ok" if not info["error"] else "warn", detail))

    # 2d. Accès distant ------------------------------------------------------------------
    results.append({**remote_check(config, devices), "ms": 0})

    # 3. Guide ---------------------------------------------------------------------------
    xmltv_result = None
    if config.enable_epg and config.epg_source != "api":
        url = config.epg_url or api.epg_url()
        p = probe(url, ua, read=4096, timeout=20)
        source = "EPG_URL" if config.epg_url else "xmltv.php"
        head = p.head.lstrip()
        if p.error:
            xmltv_result = _result("epg-xmltv", f"Guide {source}", "error",
                                   f"{p.error} : le fournisseur ne sert pas de guide XMLTV", p.ms)
        elif head[:2] == b"\x1f\x8b" or head.startswith(b"<") and (b"<tv" in head or b"<?xml" in head):
            xmltv_result = _result("epg-xmltv", f"Guide {source}", "ok", f"Guide XMLTV valide{_size(p.headers)}", p.ms)
        else:
            xmltv_result = _result("epg-xmltv", f"Guide {source}", "error",
                                   "Réponse qui n'est pas un guide XMLTV", p.ms)
    api_result = None
    from .server import EPG_PRUDENT_SECONDS, last_429
    recent = last_429(api)
    prudent = config.enable_epg and config.epg_source != "xmltv" and recent and time.time() - recent < EPG_PRUDENT_SECONDS
    if prudent:
        # Pas d'appel de test : il prolongerait le blocage du fournisseur (et refuserait la vidéo avec).
        at = time.strftime("%H:%M", time.localtime(recent))
        until = time.strftime("%H:%M", time.localtime(recent + EPG_PRUDENT_SECONDS))
        api_result = _result("epg-api", "Guide via l'API (get_simple_data_table)", "warn",
                             f"Mode prudent : le fournisseur a limité les requêtes (429) à {at}. Guide chaîne par chaîne "
                             f"suspendu jusqu'à {until} pour protéger la vidéo ; le guide vient du XMLTV et des guides "
                             "externes (EPG_EXTRA_URLS). Fournisseur sensible : EPG_SOURCE=xmltv + guide externe conseillés.", 0)
    elif config.enable_epg and config.epg_source != "xmltv" and epg_samples:
        from .epg import normalize_listings
        started = time.monotonic()
        covered, total, failures = [], 0, 0
        for channel in epg_samples:
            try:
                total += 1
                if normalize_listings(api.simple_epg(channel["stream_id"])):
                    covered.append(channel["name"])
            except UpstreamError:
                failures += 1
        ms = int((time.monotonic() - started) * 1000)
        label = "Guide via l'API (get_simple_data_table)"
        if covered:
            api_result = _result("epg-api", label, "ok" if len(covered) == total else "warn",
                                 f"{len(covered)}/{total} chaînes testées ont des programmes (ex. « {covered[0]} »)", ms)
        elif failures == total:
            api_result = _result("epg-api", label, "error", "L'API ne répond pas pour le guide", ms)
        else:
            api_result = _result("epg-api", label, "error",
                                 f"Aucun programme pour {total} chaînes testées : le fournisseur ne publie pas de guide "
                                 "par l'API non plus — utilise une source externe (EPG_URL)", ms)
    if xmltv_result and xmltv_result["status"] == "error" and api_result and api_result["status"] in ("ok", "warn"):
        # Le complément par l'API compense : ce n'est pas bloquant.
        xmltv_result["status"] = "warn"
        xmltv_result["detail"] += " ; le guide est construit chaîne par chaîne via l'API (mode auto)"
    results.extend(r for r in (xmltv_result, api_result) if r)
    results.extend(extra_guides_check(epg, pool))

    # 4. Flux -----------------------------------------------------------------------------
    busy = streams is not None and (streams.busy() if hasattr(streams, "busy") else streams.count()) > 0
    stream_tests = [("stream-ts", "Flux direct MPEG-TS"), ("stream-hls", "Flux direct HLS"),
                    ("stream-movie", "Film"), ("stream-episode", "Épisode de série"), ("stream-replay", "Replay")]
    if busy:
        for test_id, label in stream_tests:
            results.append(_result(test_id, label, "skipped", "Ignoré : une lecture est en cours (pour ne pas la couper)"))
        return results
    if sample:
        p = probe(api.direct_stream(sample["stream_id"]), ua, read=188 * 64)
        if p.error:
            results.append(_result("stream-ts", "Flux direct MPEG-TS", "error", f"« {sample['name']} » : {p.error}"
                                   + (" (limite de connexions ?)" if p.status in (403, 429, 458, 509) else ""), p.ms))
        elif p.head[:1] == bytes([TS_SYNC]) or (len(p.head) > 188 and p.head[188:189] == bytes([TS_SYNC])):
            results.append(_result("stream-ts", "Flux direct MPEG-TS", "ok",
                                   f"« {sample['name']} » : {len(p.head)} octets MPEG-TS reçus", p.ms))
        else:
            results.append(_result("stream-ts", "Flux direct MPEG-TS", "warn",
                                   f"« {sample['name']} » : données reçues mais pas au format MPEG-TS", p.ms))
        time.sleep(0.5)
        p = probe(api.direct_url("live", sample["stream_id"], "m3u8"), ua, read=4096)
        if p.error:
            results.append(_result("stream-hls", "Flux direct HLS", "warn", f"{p.error} (le format TS reste utilisable)", p.ms))
        elif p.head.lstrip().startswith(b"#EXTM3U"):
            results.append(_result("stream-hls", "Flux direct HLS", "ok", "Manifeste HLS valide", p.ms))
        else:
            results.append(_result("stream-hls", "Flux direct HLS", "warn", "Le fournisseur ne sert pas de HLS pour cette chaîne", p.ms))
    else:
        results.append(_result("stream-ts", "Flux direct MPEG-TS", "skipped", "Aucune chaîne disponible"))
    movies = catalogs.get("vod") or []
    if movies:
        movie = movies[0]
        time.sleep(0.5)
        p = probe(api.direct_url("movie", movie["stream_id"], movie.get("ext", "mp4")), ua, read=65536, range_end=65535)
        status = "ok" if not p.error and p.head else "error"
        detail = (f"« {movie['name']} » : {'avance rapide possible' if p.status == 206 else 'lecture sans avance rapide (pas de Range)'}"
                  if status == "ok" else f"« {movie['name']} » : {p.error or 'aucune donnée'}")
        results.append(_result("stream-movie", "Film", status, detail, p.ms))
    shows = catalogs.get("series") or []
    if shows and series is not None:
        show = shows[0]
        episodes = series.episodes(show["stream_id"], timeout=20) or []
        if episodes:
            ep = episodes[0]
            time.sleep(0.5)
            p = probe(api.direct_url("series", ep["id"], ep["ext"]), ua, read=65536, range_end=65535)
            results.append(_result("stream-episode", "Épisode de série", "ok" if not p.error and p.head else "error",
                                   f"« {show['name']} » S{ep['season']:02d}E{ep['episode']:02d} : "
                                   + ("lecture possible" if not p.error and p.head else (p.error or "aucune donnée")), p.ms))
        else:
            results.append(_result("stream-episode", "Épisode de série", "warn",
                                   f"Aucun épisode renvoyé pour « {show['name']} » (get_series_info)"))
    archived = next((c for c in live if c.get("archive")), None)
    if archived:
        time.sleep(0.5)
        start = int(time.time()) - 3 * 3600
        p = probe(api.timeshift_url(archived["stream_id"], start, 5), ua, read=188 * 32)
        results.append(_result("stream-replay", "Replay", "ok" if not p.error and p.head else "warn",
                               f"« {archived['name']} » il y a 3 h : " + ("lecture possible" if not p.error and p.head
                                                                          else (p.error or "aucune donnée")), p.ms))
    else:
        results.append(_result("stream-replay", "Replay", "skipped", "Aucune chaîne avec replay (tv_archive) dans ce compte"))
    return results


def remote_check(config, devices) -> dict:
    """Accès hors du réseau local : fermé, ou ouvert avec les protections attendues."""
    label = "Accès distant"
    web = (" L'interface Web est joignable hors du réseau local (WEB_REMOTE=true, mot de passe obligatoire)."
           if config.web_remote else " L'interface Web reste réservée au réseau local.")
    if not config.remote_access:
        return _result("remote", label, "warn" if config.web_remote else "ok",
                       "Fermé : seules les connexions du réseau local sont acceptées sur le port des lecteurs "
                       "(REMOTE_ACCESS=false)." + web)
    try:
        listed = devices.list() if devices is not None else []
    except Exception:
        listed = []
    if not listed:
        return _result("remote", label, "error",
                       "Ouvert (REMOTE_ACCESS=true) mais aucun appareil : personne ne peut se connecter "
                       "de l'extérieur. Crée un appareil (onglet Appareils) pour chaque personne.")
    problems = []
    open_devices = [d for d in listed if not d.get("playlist")]
    if open_devices:
        problems.append(f"{len(open_devices)} appareil(s) sans playlist : accès à tout le catalogue à distance "
                        "(lie chaque appareil à une playlist)")
    if not config.public_base_url.startswith("https://"):
        problems.append("PUBLIC_BASE_URL n'est pas en https:// : codes et identifiants circulent en clair")
    if not config.trusted_proxies:
        problems.append("TRUSTED_PROXIES vide : derrière un reverse proxy, toutes les connexions semblent venir "
                        "de lui (blocage des essais et lectures par appareil faussés)")
    if config.web_remote:
        problems.append("interface Web exposée (WEB_REMOTE=true)")
    base = (f"Ouvert avec code obligatoire : {len(listed)} appareil(s), "
            f"{len(listed) - len(open_devices)} limité(s) à leur playlist ; mode direct et playlist complète refusés "
            "à distance ; adresse bloquée 15 min après 10 codes faux.")
    if problems:
        return _result("remote", label, "warn", base + " À vérifier : " + " ; ".join(problems) + ".")
    return _result("remote", label, "ok", base + web)


def extra_guides_check(epg, pool: list[dict]) -> list[dict]:
    """Guides externes (EPG_EXTRA_URLS) : téléchargés ? combien de chaînes des playlists trouvées ?"""
    from urllib.parse import urlsplit
    from .builder import match_name
    from .epg import EpgUnavailable, squash_name
    results = []
    for number, extra in enumerate(getattr(epg, "extras", None) or [], start=1):
        host = urlsplit(extra._url_factory()).netloc
        label = f"Guide externe {number} ({host})"
        started = time.monotonic()
        try:
            extra.ensure()
            counts, names = extra.index(), extra.names()
        except EpgUnavailable as exc:
            results.append(_result(f"epg-extra-{number}", label, "error", str(exc), int((time.monotonic() - started) * 1000)))
            continue
        ms = int((time.monotonic() - started) * 1000)
        found = sum(1 for c in pool
                    if counts.get((c.get("epg_id") or "").strip().lower(), 0) > 0
                    or counts.get(names.get(squash_name(match_name(c["name"])), ""), 0) > 0)
        detail = (f"{len(counts)} chaînes, {sum(counts.values())} programmes"
                  + (f" ; {found}/{len(pool)} chaînes de tes playlists y sont trouvées" if pool else ""))
        results.append(_result(f"epg-extra-{number}", label, "ok" if found or not pool else "warn", detail, ms))
    return results
