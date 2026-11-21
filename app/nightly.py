"""Mise à jour de nuit (``NIGHTLY_REFRESH``, 3 h–6 h par défaut, heure locale du conteneur).

En journée, les gros contenus (catalogues films et séries, épisodes des séries) sont servis depuis leur
copie en cache : aucune rafale de requêtes quand quelqu'un regarde la télé. La nuit, une fois par jour :

1. catalogues films et séries rafraîchis s'ils ont plus de 20 h ;
2. épisodes remis à jour, **un par un, très lentement** (une demande toutes les ``NIGHT_INTERVAL`` s), et
   seulement pour les séries déjà ouvertes (copie en cache périmée), les plus anciennes d'abord, au plus
   ``NIGHT_MAX`` par nuit. Les séries jamais ouvertes ne sont pas demandées en masse : elles le sont à leur
   ouverture. Au premier refus (429…) ou à la fin de la fenêtre, la passe s'arrête et reprend la nuit
   suivante. Retour réel : 30 587 séries planifiées d'un coup, 429 au bout de 8 s (compte à 1 connexion).
"""
from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger("xtream-m3u-local.nightly")
CHECK_EVERY = 600       # vérification toutes les 10 minutes
CATALOG_MAX_AGE = 20 * 3600
NIGHT_INTERVAL = 5.0    # une demande d'épisodes toutes les 5 s la nuit (fournisseurs très sensibles)
NIGHT_MAX = 600         # séries remises à jour au plus par nuit (≈ 50 min)


def playlists_series_ids(api, store) -> list[str]:
    """Séries retenues par l'ensemble des playlists (dans l'ordre, sans doublon)."""
    from .builder import select_items
    from .custom import uses
    ids: list[str] = []
    try:
        playlists = store.list()
    except RuntimeError:
        return []
    for saved in playlists:
        if not uses(saved, "series"):
            continue
        try:
            entries, _ = select_items(api, saved, "series")
        except Exception:
            continue
        ids.extend(item["stream_id"] for item, _ in entries)
    return list(dict.fromkeys(ids))


def run_once(api, series, store, config) -> dict:
    """Une passe de mise à jour (appelée la nuit). Retourne un petit bilan pour le journal."""
    from .server import UpstreamError, last_429
    done = {"catalogs": [], "series": 0}
    if last_429(api) and time.time() - last_429(api) < 3600:
        LOG.info("Mise à jour de nuit reportée : le fournisseur a limité les requêtes il y a moins d'une heure")
        return done
    for kind in ("vod", "series"):
        st = api.kind_state(kind)
        if st.items is not None and time.time() - float(st.updated_at or 0) < CATALOG_MAX_AGE:
            continue
        try:
            api.catalog(kind=kind, refresh=True)
            done["catalogs"].append(kind)
        except UpstreamError as exc:
            LOG.warning("Mise à jour de nuit du catalogue %s impossible : %s", kind, exc)
    LOG.info("Mise à jour de nuit : catalogues %s", ", ".join(done["catalogs"]) or "déjà récents")
    if series is not None:
        done["series"] = refresh_series(api, series, playlists_series_ids(api, store), config)
    return done


def refresh_series(api, series, ids: list[str], config, *, interval: float | None = None,
                   limit: int | None = None, sleep=time.sleep) -> int:
    """Épisodes des séries déjà en cache et périmées, un par un ; arrêt au premier refus. Retourne le nombre fait."""
    from .server import in_nightly_window, last_429
    interval = NIGHT_INTERVAL if interval is None else interval
    limit = NIGHT_MAX if limit is None else limit
    now = time.time()
    stale, never = [], 0
    for sid in ids:
        at = series.fetched_at(sid)
        if at is None:
            never += 1
        elif now - at >= series.max_age:
            stale.append((at, sid))
    stale.sort()                                        # les plus anciennes d'abord
    todo = [sid for _, sid in stale[:limit]]
    LOG.info("Mise à jour de nuit des séries : %d copie(s) périmée(s), %d traitée(s) cette nuit, une toutes les "
             "%g s ; %d série(s) jamais ouvertes demandées seulement à leur ouverture",
             len(stale), len(todo), interval, never)
    done, stop = 0, ""
    for index, sid in enumerate(todo):
        if not in_nightly_window(config):
            stop = "fin de la fenêtre de nuit"
            break
        if last_429(api) and time.time() - last_429(api) < 3600:
            stop = "le fournisseur limite les requêtes (429)"
            break
        if not series.refresh_now(sid):
            stop = "refus du fournisseur"
            break
        done += 1
        if index + 1 < len(todo):
            sleep(interval)
    LOG.info("Mise à jour de nuit des séries : %d/%d faite(s)%s", done, len(todo),
             f" ; arrêt : {stop}, reprise la nuit suivante" if stop else "")
    return done


def start(api, series, store, config) -> threading.Thread | None:
    """Tâche de fond : une passe par nuit, pendant la fenêtre ``NIGHTLY_REFRESH``."""
    if not config.nightly_refresh:
        return None
    from .server import in_nightly_window

    def loop() -> None:
        last_day = None
        while True:
            time.sleep(CHECK_EVERY)
            today = time.strftime("%Y-%m-%d")
            if today != last_day and in_nightly_window(config):
                last_day = today
                try:
                    run_once(api, series, store, config)
                except Exception:  # la tâche de nuit ne doit jamais s'arrêter
                    LOG.exception("Mise à jour de nuit interrompue")

    thread = threading.Thread(target=loop, name="nightly", daemon=True)
    thread.start()
    return thread
