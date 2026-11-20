"""Mise à jour de nuit (``NIGHTLY_REFRESH``, 3 h–6 h par défaut, heure locale du conteneur).

En journée, les gros contenus (catalogues films et séries, épisodes des séries) sont servis depuis leur
copie en cache : aucune rafale de requêtes quand quelqu'un regarde la télé. La nuit, une fois par jour :

1. catalogues films et séries rafraîchis s'ils ont plus de 20 h ;
2. épisodes des séries utilisées par les playlists remis à jour (copies périmées), au rythme limité du
   cache des séries (``SERIES_PER_SECOND``) et jamais en mode prudent (429 récent).
"""
from __future__ import annotations

import logging
import threading
import time

LOG = logging.getLogger("xtream-m3u-local.nightly")
CHECK_EVERY = 600       # vérification toutes les 10 minutes
CATALOG_MAX_AGE = 20 * 3600


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
    if series is not None:
        done["series"] = series.prefetch(playlists_series_ids(api, store))
    LOG.info("Mise à jour de nuit : catalogues %s ; %d série(s) à remettre à jour (au fil de l'eau)",
             ", ".join(done["catalogs"]) or "déjà récents", done["series"])
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
