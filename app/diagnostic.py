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


def run(api, epg, series, streams, store, config) -> list[dict]:
    from .server import UpstreamError  # import local : évite une dépendance circulaire
    results: list[dict] = []
    ua = config.user_agent

    # 1. API du fournisseur ---------------------------------------------------------------
    started = time.monotonic()
    try:
        data = api._fetch(api.api_url(), "user_info")
        info = data.get("user_info") if isinstance(data, dict) else None
        ms = int((time.monotonic() - started) * 1000)
        if not isinstance(info, dict) or str(info.get("auth", "1")) == "0":
            results.append(_result("account", "Connexion à l'API (player_api.php)", "error",
                                   "Identifiants refusés par le fournisseur", ms))
        else:
            exp = str(info.get("exp_date") or "")
            expires = time.strftime("%d/%m/%Y", time.localtime(int(exp))) if exp.isdigit() else "inconnue"
            results.append(_result("account", "Connexion à l'API (player_api.php)", "ok",
                                   f"Statut {info.get('status') or '?'}, expiration {expires}, "
                                   f"{info.get('active_cons') or 0}/{info.get('max_connections') or '?'} connexion(s)", ms))
    except UpstreamError as exc:
        results.append(_result("account", "Connexion à l'API (player_api.php)", "error", str(exc),
                               int((time.monotonic() - started) * 1000)))
        return results  # inutile d'aller plus loin

    # 2. Catalogues -----------------------------------------------------------------------
    labels = {"live": ("Catalogue TV en direct", "chaînes"), "vod": ("Catalogue films", "films"),
              "series": ("Catalogue séries", "séries")}
    catalogs: dict[str, list[dict]] = {}
    for kind, (label, unit) in labels.items():
        started = time.monotonic()
        try:
            items, categories = api.catalog(refresh=True, kind=kind)
            ms = int((time.monotonic() - started) * 1000)
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
    # Chaîne de test : première chaîne d'une playlist personnalisée, sinon du catalogue.
    sample = None
    try:
        from .builder import select_items
        for saved in store.list():
            entries = select_items(api, saved, "live")[0]
            if entries:
                sample = entries[0][0]
                break
    except Exception:
        sample = None
    sample = sample or (live[0] if live else None)

    # 3. Guide ---------------------------------------------------------------------------
    if config.enable_epg and config.epg_source != "api":
        url = config.epg_url or api.epg_url()
        p = probe(url, ua, read=4096, timeout=20)
        source = "EPG_URL" if config.epg_url else "xmltv.php"
        head = p.head.lstrip()
        if p.error:
            results.append(_result("epg-xmltv", f"Guide {source}", "error", p.error, p.ms))
        elif head[:2] == b"\x1f\x8b" or head.startswith(b"<") and (b"<tv" in head or b"<?xml" in head):
            results.append(_result("epg-xmltv", f"Guide {source}", "ok", f"Guide XMLTV valide{_size(p.headers)}", p.ms))
        else:
            results.append(_result("epg-xmltv", f"Guide {source}", "warn",
                                   "Réponse qui n'est pas un guide XMLTV (le complément par l'API prend le relais en mode auto)", p.ms))
    if config.enable_epg and config.epg_source != "xmltv" and sample:
        started = time.monotonic()
        try:
            from .epg import normalize_listings
            listings = normalize_listings(api.simple_epg(sample["stream_id"]))
            results.append(_result("epg-api", "Guide via l'API (get_simple_data_table)", "ok" if listings else "warn",
                                   f"{len(listings)} programme(s) pour « {sample['name']} »" if listings
                                   else f"Aucun programme pour « {sample['name']} »", int((time.monotonic() - started) * 1000)))
        except UpstreamError as exc:
            results.append(_result("epg-api", "Guide via l'API (get_simple_data_table)", "error", str(exc),
                                   int((time.monotonic() - started) * 1000)))

    # 4. Flux -----------------------------------------------------------------------------
    busy = streams is not None and streams.count() > 0
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
