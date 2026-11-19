"""Films (VOD) et séries Xtream : normalisation et cache des épisodes.

- Films : ``get_vod_categories`` / ``get_vod_streams`` ; lecture ``/movie/USER/PASS/<id>.<ext>``.
- Séries : ``get_series_categories`` / ``get_series`` ; les épisodes ne sont fournis que par
  ``get_series_info&series_id=<id>`` (un appel par série) ; lecture ``/series/USER/PASS/<episode_id>.<ext>``.

Comme une sélection peut contenir des centaines de séries, les épisodes sont mis en cache
sur disque (``DATA_DIR/cache/series``), préchargés en arrière-plan et récupérés avec un
budget de temps par requête : le lecteur n'attend jamais indéfiniment.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from .custom import CATEGORY_RE, UNCATEGORIZED

LOG = logging.getLogger("xtream-m3u-local.vod")
EXT_RE = re.compile(r"^[a-z0-9]{1,5}$")
YEAR_RE = re.compile(r"(19|20)\d{2}")
MIME = {
    "mp4": "video/mp4", "m4v": "video/mp4", "mkv": "video/x-matroska", "avi": "video/x-msvideo",
    "ts": "video/mp2t", "mov": "video/quicktime", "webm": "video/webm", "flv": "video/x-flv",
    "wmv": "video/x-ms-wmv", "mpg": "video/mpeg", "mpeg": "video/mpeg", "m3u8": "application/vnd.apple.mpegurl",
}


def clean_text(value: object, default: str = "") -> str:
    text = " ".join(str(value if value is not None else "").replace("\x00", " ").split())
    return text or default


def http_url(value: object) -> str:
    text = clean_text(value)
    return text if text.lower().startswith(("http://", "https://")) else ""


def extension(value: object, default: str = "mp4") -> str:
    ext = clean_text(value).lower().lstrip(".")
    return ext if EXT_RE.fullmatch(ext) else default


def mime_for(ext: str) -> str:
    return MIME.get(ext.lower(), "application/octet-stream")


def category_ids(entry: dict) -> list[str]:
    cats: list[str] = []
    primary = entry.get("category_id")
    if primary not in (None, ""):
        cats.append(str(primary).strip())
    extra = entry.get("category_ids")
    if isinstance(extra, list):
        for cid in extra:
            if cid not in (None, "") and str(cid).strip() not in cats:
                cats.append(str(cid).strip())
    cats = [c for c in cats if CATEGORY_RE.fullmatch(c) and c != UNCATEGORIZED]
    return cats or [UNCATEGORIZED]


def year_of(entry: dict) -> str:
    for key in ("year", "releaseDate", "release_date", "releasedate"):
        match = YEAR_RE.search(str(entry.get(key) or ""))
        if match:
            return match.group(0)
    return ""


def rating_of(entry: dict) -> str:
    raw = clean_text(entry.get("rating"))
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return ""
    return f"{value:.1f}" if 0 < value <= 10 else ""


def normalize_movie(entry: object) -> dict | None:
    """Raw ``get_vod_streams`` entry -> internal movie."""
    if not isinstance(entry, dict):
        return None
    sid = str(entry.get("stream_id", "")).strip()
    if not sid.isdigit() or len(sid) > 12:
        return None
    cats = category_ids(entry)
    num = str(entry.get("num", "")).strip()
    return {
        "kind": "movie",
        "stream_id": sid,
        "name": clean_text(entry.get("name"), "Sans titre"),
        "logo": http_url(entry.get("stream_icon")),
        "epg_id": "",
        "num": num if num.isdigit() else "",
        "category_id": cats[0],
        "category_ids": cats,
        "ext": extension(entry.get("container_extension")),
        "year": year_of(entry),
        "rating": rating_of(entry),
    }


def normalize_series(entry: object) -> dict | None:
    """Raw ``get_series`` entry -> internal series (``stream_id`` = ``series_id``)."""
    if not isinstance(entry, dict):
        return None
    sid = str(entry.get("series_id", "")).strip()
    if not sid.isdigit() or len(sid) > 12:
        return None
    cats = category_ids(entry)
    num = str(entry.get("num", "")).strip()
    return {
        "kind": "series",
        "stream_id": sid,
        "name": clean_text(entry.get("name"), "Sans titre"),
        "logo": http_url(entry.get("cover")),
        "epg_id": "",
        "num": num if num.isdigit() else "",
        "category_id": cats[0],
        "category_ids": cats,
        "year": year_of(entry),
        "rating": rating_of(entry),
    }


def _int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def normalize_episodes(data: object) -> list[dict]:
    """``get_series_info`` -> sorted list of ``{id, season, episode, title, ext, logo}``.

    Les panneaux Xtream renvoient ``episodes`` sous forme de dictionnaire par saison
    (``{"1": [...]}``) ou, plus rarement, de liste ; les deux sont acceptés.
    """
    if not isinstance(data, dict):
        return []
    raw = data.get("episodes")
    groups: list[tuple[object, object]] = []
    if isinstance(raw, dict):
        groups = list(raw.items())
    elif isinstance(raw, list):
        groups = [(None, item) for item in raw]
    episodes: dict[str, dict] = {}
    for season_key, items in groups:
        for ep in items if isinstance(items, list) else [items]:
            if not isinstance(ep, dict):
                continue
            eid = str(ep.get("id", "")).strip()
            if not eid.isdigit() or len(eid) > 12 or eid in episodes:
                continue
            info = ep.get("info") if isinstance(ep.get("info"), dict) else {}
            episodes[eid] = {
                "id": eid,
                "season": _int(ep.get("season"), _int(season_key, 1)),
                "episode": _int(ep.get("episode_num"), 0),
                "title": clean_text(ep.get("title")),
                "ext": extension(ep.get("container_extension")),
                "logo": http_url(info.get("movie_image")),
            }
    return sorted(episodes.values(), key=lambda e: (e["season"], e["episode"], _int(e["id"])))


def episode_name(series_name: str, ep: dict) -> str:
    """Nom reconnu par les lecteurs (TiviMate, Smarters…) : ``Série S01 E02``."""
    name = f"{series_name} S{ep['season']:02d} E{ep['episode']:02d}"
    title = ep.get("title") or ""
    # Le titre Xtream répète souvent « Série - S01E02 » : n'ajouter qu'un vrai titre d'épisode.
    if title and series_name.lower() not in title.lower() and not re.search(r"(?i)s\d+\s*e\d+", title):
        name += f" - {title}"
    return name


class KeyedCache:
    """Cache de réponses API par identifiant (mémoire + disque), préchargement en arrière-plan
    et budget de temps. Utilisé pour les épisodes (``get_series_info``) et le guide par chaîne
    (``get_simple_data_table``), qui n'existent qu'élément par élément dans l'API Xtream.
    """

    def __init__(self, fetch: Callable[[str], object], data_dir: str, *, max_age: int = 24 * 3600,
                 workers: int = 4, normalize: Callable[[object], list[dict]] = None,
                 subdir: str = "series", label: str = "série", min_interval: float = 0.0,
                 summarize: bool = False):
        self._fetch = fetch
        # Débit limité : au plus un appel au fournisseur toutes les ``min_interval`` secondes (tous threads
        # confondus) — des milliers d'appels d'un coup déclenchent son « 429 trop de requêtes ».
        self.min_interval = max(0.0, min_interval)
        self._rate_lock = threading.Lock()
        self._next_slot = 0.0
        # Priorité à la vidéo : ``defer()`` renvoie le nombre de secondes à attendre (flux qui démarre).
        self.defer: Callable[[], float] | None = None
        # Journal résumé (une ligne par minute) au lieu d'une ligne par élément en échec.
        self._summarize = summarize
        self._quiet_failures = 0
        self._last_summary = 0.0
        self._normalize = normalize or normalize_episodes
        self._label = label
        self.directory = Path(data_dir) / "cache" / subdir
        self.max_age = max(60, max_age)
        self._memory: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()
        self._pending: dict[str, object] = {}
        self._failed: dict[str, float] = {}
        self._executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix=subdir)

    # ----------------------------------------------------------- stockage
    def _path(self, sid: str) -> Path:
        return self.directory / f"{sid}.json"

    def _load(self, sid: str) -> tuple[float, list[dict]] | None:
        with self._lock:
            if sid in self._memory:
                return self._memory[sid]
        try:
            data = json.loads(self._path(sid).read_text(encoding="utf-8"))
            entry = (float(data["fetched_at"]), list(data["episodes"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        with self._lock:
            self._memory[sid] = entry
        return entry

    def _store(self, sid: str, episodes: list[dict]) -> None:
        entry = (time.time(), episodes)
        with self._lock:
            self._memory[sid] = entry
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{sid}-", suffix=".tmp", dir=self.directory)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": entry[0], "episodes": episodes}, f, ensure_ascii=False)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self._path(sid))
        except OSError:
            LOG.warning("Cache des séries non inscriptible")

    # ------------------------------------------------------- récupération
    def _wait_turn(self) -> None:
        waited = 0.0
        while self.defer is not None and waited < 30:
            pause = min(5.0, max(0.0, float(self.defer() or 0)))
            if not pause:
                break
            time.sleep(pause)
            waited += pause
        if self.min_interval:
            with self._rate_lock:
                now = time.monotonic()
                slot = max(now, self._next_slot)
                self._next_slot = slot + self.min_interval
            if slot > now:
                time.sleep(slot - now)

    def _note_failure(self, sid: str, exc: Exception) -> None:
        if not self._summarize:
            LOG.warning("Données %s %s indisponibles (%s)", self._label, sid, type(exc).__name__)
            return
        with self._lock:
            self._quiet_failures += 1
            now = time.monotonic()
            if now - self._last_summary < 60:
                return
            count, self._quiet_failures, self._last_summary = self._quiet_failures, 0, now
        LOG.warning("Données %s indisponibles pour %d élément(s) (%s) ; nouvel essai dans 5 min",
                    self._label, count, type(exc).__name__)

    def _task(self, sid: str) -> list[dict] | None:
        try:
            self._wait_turn()
            episodes = self._normalize(self._fetch(sid))
            self._store(sid, episodes)
            with self._lock:
                self._failed.pop(sid, None)
            return episodes
        except Exception as exc:  # erreur réseau/API : on garde l'ancienne copie éventuelle
            self._note_failure(sid, exc)
            with self._lock:
                self._failed[sid] = time.monotonic()
            return None
        finally:
            with self._lock:
                self._pending.pop(sid, None)

    def _submit(self, sid: str):
        with self._lock:
            future = self._pending.get(sid)
            if future is None:
                if time.monotonic() - self._failed.get(sid, -1e9) < 300:
                    return None  # échec récent : pas de nouvel essai pendant 5 min
                future = self._executor.submit(self._task, sid)
                self._pending[sid] = future
            return future

    def episodes(self, sid: str, *, refresh: bool = False, timeout: float = 30) -> list[dict] | None:
        cached = self._load(sid)
        if cached and not refresh and time.time() - cached[0] < self.max_age:
            return cached[1]
        future = self._submit(sid)
        if future is not None:
            done, _ = wait([future], timeout=timeout)
            if done:
                result = future.result()
                if result is not None:
                    return result
        return cached[1] if cached else None

    def episodes_for(self, ids: list[str], *, budget: float = 20) -> tuple[dict[str, list[dict]], list[str]]:
        """Épisodes de plusieurs séries dans un budget de temps. Retourne ``(résultats, ids_manquants)``.

        Les récupérations non terminées continuent en arrière-plan et rempliront le cache.
        """
        results: dict[str, list[dict]] = {}
        stale: dict[str, list[dict]] = {}
        futures = {}
        for sid in ids:
            cached = self._load(sid)
            if cached and time.time() - cached[0] < self.max_age:
                results[sid] = cached[1]
                continue
            if cached:
                stale[sid] = cached[1]
            future = self._submit(sid)
            if future is not None:
                futures[future] = sid
        if futures:
            wait(list(futures), timeout=max(0.0, budget))
            for future, sid in futures.items():
                if future.done() and future.result() is not None:
                    results[sid] = future.result()
        for sid, episodes in stale.items():
            results.setdefault(sid, episodes)
        missing = [sid for sid in ids if sid not in results]
        return results, missing

    def prefetch(self, ids: list[str], *, force: bool = False) -> int:
        """Planifie en arrière-plan les éléments absents ou périmés du cache. Retourne le nombre planifié."""
        count = 0
        for sid in ids:
            cached = self._load(sid)
            if cached and not force and time.time() - cached[0] < self.max_age:
                continue
            if force:
                with self._lock:
                    self._failed.pop(sid, None)
            if self._submit(sid) is not None:
                count += 1
        return count

    def known(self, sid: str) -> list[dict] | None:
        """Résultat en cache encore valable (liste éventuellement vide), sinon ``None``."""
        cached = self._load(sid)
        if cached and time.time() - cached[0] < self.max_age:
            return cached[1]
        return None

    def fetched_at(self, sid: str) -> float | None:
        cached = self._load(sid)
        return cached[0] if cached else None

    def pending_count(self, ids: list[str]) -> int:
        with self._lock:
            return sum(1 for sid in ids if sid in self._pending)

    def stats(self) -> dict:
        with self._lock:
            pending = len(self._pending)
        try:
            cached = sum(1 for _ in self.directory.glob("*.json"))
        except OSError:
            cached = 0
        return {"cached": cached, "pending": pending}


class SeriesCache(KeyedCache):
    """Épisodes par série : ``DATA_DIR/cache/series/<series_id>.json``."""

    def __init__(self, fetch: Callable[[str], object], data_dir: str, *, max_age: int = 24 * 3600, workers: int = 4):
        super().__init__(fetch, data_dir, max_age=max_age, workers=workers,
                         normalize=normalize_episodes, subdir="series", label="série")
