"""Guide des programmes (EPG) : cache XMLTV fournisseur, EPG par playlist, complément par l'API.

- ``/epg.xml`` sert le XMLTV complet (``xmltv.php`` ou ``EPG_URL``), téléchargé au plus toutes
  les ``EPG_CACHE_HOURS``.
- ``/p/<slug>.xml`` sert uniquement les chaînes de la playlist ``slug`` (léger pour Android TV).
  Mode ``auto`` (défaut) : programmes de ``xmltv.php`` ; les chaînes absentes (ou toutes, si
  ``xmltv.php`` ne fonctionne pas) sont complétées par ``get_simple_data_table`` (un appel par
  chaîne, mis en cache et préchargé comme les épisodes de séries).

Bibliothèque standard uniquement. Le parseur ``expat`` de Python 3.13 protège contre
l'expansion d'entités (« billion laughs ») et ElementTree ne résout pas les entités externes.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from http.client import HTTPException
from xml.sax.saxutils import escape, quoteattr

LOG = logging.getLogger("xtream-m3u-local.epg")
MAX_EPG_BYTES = 1024 * 1024 * 1024  # 1 Gio
RETRY_AFTER_FAILURE = 900  # 15 min sans nouvelle tentative après un échec
EPG_MODES = ("auto", "xmltv", "api")
EPG_LOOKAHEAD = 24 * 3600
EPGSHARE_FR = "https://epgshare01.online/epgshare01/epg_ripper_FR1.xml.gz"
# Pour le Maroc, les noms ne coïncident pas toujours et certaines entrées sont des blocs génériques.
# On limite le rapprochement automatique aux grilles vérifiées. Les choix manuels restent possibles.
DEFAULT_EPG_GUIDES = {
    "https://xmltvfr.fr/xmltv/xmltv_fr.xml.gz": None,
    EPGSHARE_FR: None,
    "https://www.open-epg.com/files/morocco.xml.gz": {
        "alaoula": "alaoulaar.ma",
        "alaoulainter": "alaoulaar.ma",
        "alaoulainternational": "alaoulaar.ma",
        "alaoulamaroc": "alaoulaar.ma",
        "almaghribia": "almaghribiaar.ma",
        "almaghribiya": "almaghribiaar.ma",
        "maghribia": "almaghribiaar.ma",
        "tamazight": "tamazightar.ma",
        "tamazighttv": "tamazightar.ma",
        "tamazighte": "tamazightar.ma",
        "tamazightmoroc": "tamazightar.ma",
        "tamazightmaroc": "tamazightar.ma",
    },
    "https://raw.githubusercontent.com/iimnm/arabic-epg/output/guide.xml.gz": {
        "2m": "2mmonde.ma@sd",
        "2mmaroc": "2mmonde.ma@sd",
        "2mmonde": "2mmonde.ma@sd",
        "2mnational": "2mmonde.ma@sd",
        "2mnatioanl": "2mmonde.ma@sd",
        "chadatv": "chadatv.ma@sd",
        "chada": "chadatv.ma@sd",
    },
}
GENERIC_EPG_CHANNELS = {
    EPGSHARE_FR: ("al.aoula.fr", "al.maghribia.fr", "arryadia.fr", "assadissa.fr", "medi1tv.fr", "télé.maroc.fr"),
}


def _coverage_periods(periods: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Fusionne les créneaux contigus pour garder un index compact, même pour un guide complet."""
    merged: list[tuple[int, int]] = []
    for start, stop in sorted(periods):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((start, stop))
    return merged


def _decode(value: object) -> str:
    """Titres et descriptions de ``get_simple_data_table`` : base64 (parfois texte brut)."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        decoded = text
    return " ".join(decoded.replace("\x00", " ").split())


def _timestamp(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 946684800 <= number <= 4102444800 else None  # 2000 → 2100


def _parse_datetime(value: object) -> int | None:
    from datetime import datetime, timezone
    try:
        return int(datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def normalize_listings(data: object) -> list[dict]:
    """``get_simple_data_table`` / ``get_short_epg`` -> ``[{start, stop, title, desc}]`` triés (UTC)."""
    listings = data.get("epg_listings") if isinstance(data, dict) else None
    if not isinstance(listings, list):
        return []
    result: dict[int, dict] = {}
    for item in listings:
        if not isinstance(item, dict):
            continue
        start = _timestamp(item.get("start_timestamp")) or _parse_datetime(item.get("start"))
        stop = _timestamp(item.get("stop_timestamp")) or _parse_datetime(item.get("end") or item.get("stop"))
        title = _decode(item.get("title"))
        if not start or not stop or stop <= start or not title:
            continue
        result[start] = {"start": start, "stop": stop, "title": title[:300],
                         "desc": _decode(item.get("description"))[:2000]}
    return [result[k] for k in sorted(result)]


def xmltv_time(ts: int) -> str:
    return time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(ts))


def write_api_channel(dst, xml_id: str, name: str, logo: str, listings: list[dict], *,
                      with_channel: bool = True) -> int:
    if with_channel:
        icon = f"<icon src={quoteattr(logo)}/>" if logo else ""
        dst.write(f"<channel id={quoteattr(xml_id)}><display-name>{escape(name)}</display-name>{icon}</channel>\n")
    for item in listings:
        desc = f"<desc>{escape(item['desc'])}</desc>" if item.get("desc") else ""
        dst.write(f"<programme start=\"{xmltv_time(item['start'])}\" stop=\"{xmltv_time(item['stop'])}\" "
                  f"channel={quoteattr(xml_id)}><title>{escape(item['title'])}</title>{desc}</programme>\n")
    return len(listings)


def xml_channel_id(channel: dict) -> str:
    """Identifiant XMLTV d'une chaîne : ``epg_channel_id`` du fournisseur (s'il est exploitable), sinon ``xtream-<stream_id>``."""
    from .builder import is_usable_epg_id
    eid = (channel.get("epg_id") or "").strip()
    if eid and (channel.get("epg_manual") or is_usable_epg_id(eid)):
        return eid
    return f"xtream-{channel['stream_id']}"


class EpgUnavailable(Exception):
    """Le guide XMLTV n'est pas disponible (sans détail sensible)."""


class EpgCache:
    def __init__(self, url_factory: Callable[[], str], data_dir: str, *, max_age: int = 12 * 3600,
                 user_agent: str = "XtreamM3ULocal", automatic_channels: dict[str, str] | None = None,
                 automatic_exclusions: tuple[str, ...] = ()):
        self._url_factory = url_factory
        self.directory = Path(data_dir) / "epg"
        self.source = self.directory / "xmltv.xml"
        self.max_age = max(60, max_age)
        self.user_agent = user_agent
        self.automatic_channels = automatic_channels
        self.automatic_exclusions = automatic_exclusions
        self._download_lock = threading.Lock()
        self._filter_lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._failed_at = 0.0
        self.last_error: str | None = None
        self._index: tuple[str, dict[str, int], dict[str, list[tuple[int, int]]]] | None = None

    # ------------------------------------------------------------------ source
    def _age(self) -> float | None:
        try:
            return time.time() - self.source.stat().st_mtime
        except OSError:
            return None

    def ensure(self, *, refresh: bool = False) -> Path:
        with self._download_lock:
            age = self._age()
            if age is not None and age < self.max_age and not refresh:
                return self.source
            if time.monotonic() - self._failed_at < RETRY_AFTER_FAILURE and self._failed_at:
                if age is not None:
                    return self.source
                raise EpgUnavailable("Guide EPG indisponible (nouvel essai dans quelques minutes)")
            try:
                self._download()
                self._failed_at = 0.0
                self.last_error = None
            except EpgUnavailable as exc:
                self._failed_at = time.monotonic()
                self.last_error = str(exc)
                if age is not None:
                    LOG.warning("EPG : échec de mise à jour, utilisation de la copie en cache")
                    return self.source
                raise
            return self.source

    def _download(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            fd, raw_tmp = tempfile.mkstemp(prefix=".xmltv-", suffix=".tmp", dir=self.directory)
        except OSError as exc:
            raise EpgUnavailable("Dossier EPG non inscriptible") from exc
        final_tmp = raw_tmp
        try:
            request = Request(self._url_factory(), headers={"User-Agent": self.user_agent, "Accept": "*/*"})
            total = 0
            try:
                with urlopen(request, timeout=90) as response, os.fdopen(fd, "wb") as out:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_EPG_BYTES:
                            raise EpgUnavailable("Guide EPG trop volumineux (> 1 Gio)")
                        out.write(chunk)
            except (HTTPError, URLError, TimeoutError, OSError, HTTPException, ValueError) as exc:
                LOG.warning("EPG : téléchargement impossible (%s)", getattr(exc, "code", None) or "réseau")
                raise EpgUnavailable("Guide EPG du fournisseur inaccessible") from None
            with open(raw_tmp, "rb") as f:
                magic = f.read(2)
            if magic == b"PK":  # archive .zip (certains guides externes) : premier fichier .xml
                fd2, final_tmp = tempfile.mkstemp(prefix=".xmltv-", suffix=".tmp", dir=self.directory)
                try:
                    with zipfile.ZipFile(raw_tmp) as archive, os.fdopen(fd2, "wb") as dst:
                        member = next((n for n in archive.namelist() if n.lower().endswith(".xml")), None)
                        if member is None:
                            raise EpgUnavailable("Archive EPG sans fichier .xml")
                        with archive.open(member) as src:
                            shutil.copyfileobj(src, dst, 256 * 1024)
                except (OSError, zipfile.BadZipFile) as exc:
                    raise EpgUnavailable("Archive EPG illisible") from exc
            elif magic == b"\x1f\x8b":
                fd2, final_tmp = tempfile.mkstemp(prefix=".xmltv-", suffix=".tmp", dir=self.directory)
                try:
                    with gzip.open(raw_tmp, "rb") as src, os.fdopen(fd2, "wb") as dst:
                        shutil.copyfileobj(src, dst, 256 * 1024)
                except (OSError, EOFError) as exc:
                    raise EpgUnavailable("Guide EPG compressé illisible") from exc
            with open(final_tmp, "rb") as f:
                head = f.read(4096).lstrip()
            if not head.startswith(b"<") or (b"<tv" not in head and b"<?xml" not in head):
                raise EpgUnavailable("Le fournisseur ne renvoie pas un guide XMLTV valide")
            os.chmod(final_tmp, 0o644)
            os.replace(final_tmp, self.source)
            LOG.info("EPG : guide XMLTV mis à jour (%d octets)", self.source.stat().st_size)
        finally:
            for tmp in {raw_tmp, final_tmp}:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # ---------------------------------------------------------------- filtrage
    def filtered(self, key: str, epg_ids: set[str]) -> Path:
        """Return the path of an XMLTV file limited to ``epg_ids`` (comparaison insensible à la casse)."""
        source = self.ensure()
        wanted = {i.strip().lower() for i in epg_ids if i and i.strip()}
        stat = source.stat()
        digest = hashlib.sha256()
        digest.update(f"{stat.st_mtime_ns}:{stat.st_size}\n".encode())
        for item in sorted(wanted):
            digest.update(item.encode("utf-8", "replace") + b"\n")
        signature = digest.hexdigest()
        out = self.directory / f"p-{key}.xml"
        sig_file = self.directory / f"p-{key}.sig"
        with self._filter_lock:
            try:
                if out.exists() and sig_file.read_text() == signature:
                    return out
            except OSError:
                pass
            fd, tmp = tempfile.mkstemp(prefix=f".p-{key}-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as dst:
                    write_filtered_xmltv(source, dst, wanted)
                os.chmod(tmp, 0o644)
                os.replace(tmp, out)
                sig_file.write_text(signature)
            except ET.ParseError as exc:
                raise EpgUnavailable("Guide XMLTV du fournisseur illisible") from exc
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return out

    # ------------------------------------------------------------- index / état
    def _source_key(self) -> str | None:
        try:
            stat = self.source.stat()
        except OSError:
            return None
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    def index(self) -> dict[str, int]:
        """``{identifiant_chaîne_minuscule: nombre_de_programmes}`` du XMLTV en cache ({} si absent)."""
        with self._index_lock:
            return self._index_data()[0]

    def upcoming_ids(self, now: float | None = None) -> set[str]:
        """Chaînes avec au moins un programme en cours ou dans les prochaines 24 h."""
        now = time.time() if now is None else now
        with self._index_lock:
            _, coverage = self._index_data()
        return {cid for cid, periods in coverage.items()
                if any(start < now + EPG_LOOKAHEAD and stop > now for start, stop in periods)}

    def _index_data(self) -> tuple[dict[str, int], dict[str, list[tuple[int, int]]]]:
        key = self._source_key()
        if key is None:
            return {}, {}
        if self._index and self._index[0] == key:
            return self._index[1], self._index[2]
        index_file = self.directory / "index.json"
        try:
            data = json.loads(index_file.read_text(encoding="utf-8"))
            if (isinstance(data, dict) and data.get("source") == key
                    and isinstance(data.get("channels"), dict) and isinstance(data.get("coverage"), dict)):
                self._index = (key, data["channels"], data["coverage"])
                return data["channels"], data["coverage"]
        except (OSError, ValueError):
            pass
        counts: dict[str, int] = {}
        periods: dict[str, list[tuple[int, int]]] = {}
        parse_time = lru_cache(maxsize=8192)(parse_xmltv_time)
        try:
            events = ET.iterparse(self.source, events=("start", "end"))
            _, root = next(events)
            for event, elem in events:
                if event != "end":
                    continue
                if elem.tag == "channel":
                    cid = elem.get("id", "").strip().lower()
                    if cid:
                        counts.setdefault(cid, 0)
                    root.clear()
                elif elem.tag == "programme":
                    cid = elem.get("channel", "").strip().lower()
                    if cid:
                        counts[cid] = counts.get(cid, 0) + 1
                        start = parse_time(elem.get("start"))
                        if start is not None:
                            stop = parse_time(elem.get("stop")) or start + 1800
                            if stop > start:
                                periods.setdefault(cid, []).append((start, stop))
                    root.clear()
        except (ET.ParseError, OSError):
            LOG.warning("EPG : XMLTV en cache illisible, index vide")
            return {}, {}
        coverage = {cid: _coverage_periods(slots) for cid, slots in periods.items()}
        self._index = (key, counts, coverage)
        try:
            index_file.write_text(json.dumps({"source": key, "channels": counts, "coverage": coverage}), encoding="utf-8")
        except OSError:
            pass
        return counts, coverage

    def names(self, available: set[str] | None = None) -> dict[str, str]:
        """Premier identifiant par nom normalisé, parmi les chaînes admissibles si elles sont précisées."""
        return {name: cid for name, ids in self._name_candidates().items()
                if (cid := next((i for i in ids if available is None or i in available), None)) is not None}

    def automatic_matches(self, available: set[str]) -> tuple[set[str], dict[str, str]]:
        """Identifiants et noms éligibles au rapprochement automatique dans ce guide."""
        available = available.difference(self.automatic_exclusions)
        if self.automatic_channels is None:
            return available, self.names(available)
        names = {name: cid for name, cid in self.automatic_channels.items() if cid in available}
        return set(names.values()), names

    def _name_candidates(self) -> dict[str, list[str]]:
        key = self._source_key()
        if key is None:
            return {}
        cached = getattr(self, "_names", None)
        if cached and cached[0] == key:
            return cached[1]
        from .builder import match_name
        result: dict[str, list[str]] = {}
        fallback_tv: dict[str, list[str]] = {}
        labels: dict[str, str] = {}
        try:
            for _, elem in ET.iterparse(self.source, events=("end",)):
                if elem.tag == "channel":
                    cid = elem.get("id", "").strip().lower()
                    for name in elem.findall("display-name"):
                        core = squash_name(match_name(name.text or ""))
                        if cid and name.text and cid not in labels:
                            labels[cid] = name.text.strip()[:80]
                        if cid and core:
                            ids = result.setdefault(core, [])
                            if cid not in ids:
                                ids.append(cid)
                            if core.endswith("tv") and len(core) >= 6 and not core[:-2].isdigit():
                                fids = fallback_tv.setdefault(core[:-2], [])
                                if cid not in fids:
                                    fids.append(cid)
                    elem.clear()
                elif elem.tag == "programme":
                    elem.clear()
        except (ET.ParseError, OSError):
            return {}
        for short, fids in fallback_tv.items():
            if short not in result:
                result[short] = fids
        self._names = (key, result)
        self._labels = (key, labels)
        return result

    def labels(self) -> dict[str, str]:
        """``{identifiant: nom affiché}`` des chaînes du XMLTV en cache (suggestions de guide)."""
        self._name_candidates()
        cached = getattr(self, "_labels", None)
        return cached[1] if cached and cached[0] == self._source_key() else {}

    def status(self) -> dict:
        """État de la source XMLTV (sans URL ni identifiants)."""
        key = self._source_key()
        index = self.index() if key else {}
        try:
            stat = self.source.stat()
            updated, size = int(stat.st_mtime), stat.st_size
        except OSError:
            updated, size = None, 0
        return {
            "ok": key is not None,
            "updated_at": updated,
            "size": size,
            "channels": len(index),
            "programmes": sum(index.values()),
            "error": self.last_error,
        }

    # ------------------------------------------------- guide d'une playlist
    mode = "auto"   # remplacé par EPG_SOURCE (voir make_epg)
    api_cache = None  # KeyedCache des programmes par chaîne (get_simple_data_table)
    _extras: list = []   # guides XMLTV externes (EPG_EXTRA_URLS), essayés avant l'API
    _extras_fn = None    # ou fonction qui les donne (guides ajoutés dans l'interface, pris en compte à chaud)
    _provider_caches_fn = None   # fonction -> {provider_id: EpgCache} pour les abonnements 2..9
    _provider_of_fn = None       # fonction(stream_id) -> provider_id

    @property
    def extras(self) -> list:
        return list(self._extras_fn()) if self._extras_fn is not None else list(self._extras)

    @extras.setter
    def extras(self, value) -> None:
        self._extras = list(value)

    @property
    def provider_caches(self) -> dict[str, "EpgCache"]:
        return dict(self._provider_caches_fn()) if self._provider_caches_fn is not None else {}

    def _provider_of(self, sid: str) -> str:
        if self._provider_of_fn is not None:
            try:
                return str(self._provider_of_fn(sid) or "1")
            except Exception:
                return "1"
        return "1"

    api_max = 0        # chaînes au plus complétées par l'API pour une playlist (0 = sans limite)

    def playlist_guide(self, key: str, channels: list[dict], *, mode: str | None = None, api=None,
                       budget: float = 15, api_ids: set[str] | None = None,
                       refresh: bool = False) -> tuple[Path, dict]:
        """XMLTV de la playlist ``key`` + statistiques de couverture.

        ``channels`` : chaînes du direct de la playlist (``stream_id``, ``name``, ``logo``, ``epg_id``).
        ``api`` : :class:`app.vod.KeyedCache` des programmes par chaîne (``get_simple_data_table``).
        ``api_ids`` : si fourni (guide complet), seules ces chaînes peuvent être complétées par l'API ;
        ``EpgUnavailable`` est levée si ni le XMLTV ni l'API ne peuvent fournir de programmes.
        """
        from .builder import is_separator
        mode = mode or self.mode
        api = api if api is not None else self.api_cache
        now = time.time()
        p_caches = self.provider_caches
        ch_provider = {c["stream_id"]: self._provider_of(c["stream_id"]) for c in channels}
        needed_providers = set(ch_provider.values()) if channels else {"1"}
        source = None
        source_error = None
        if mode in ("auto", "xmltv") and ("1" in needed_providers or not p_caches):
            try:
                source = self.ensure(refresh=refresh)
            except EpgUnavailable as exc:
                source_error = exc
        available = self.upcoming_ids(now) if source else set()
        # Abonnements secondaires (2..9) : chaque abonnement télécharge et met en cache son propre xmltv.php.
        sec_available: dict[str, tuple["EpgCache", set[str]]] = {}
        sec_errors: list[EpgUnavailable] = []
        if mode in ("auto", "xmltv") and p_caches:
            for pid in sorted(needed_providers - {"1"}):
                pcache = p_caches.get(pid)
                if pcache is None:
                    continue
                try:
                    pcache.ensure(refresh=refresh)
                    sec_available[pid] = (pcache, pcache.upcoming_ids(now))
                except EpgUnavailable as exc:
                    sec_errors.append(exc)
        if source is None and not sec_available and source_error is None and sec_errors:
            source_error = sec_errors[0]
        from .builder import epg_id_matches_name, is_usable_epg_id, match_name, _epg_core_family
        bad_cores: dict[str, set[str]] = {}
        bad_families: dict[str, set[str]] = {}
        for c in channels:
            if c.get("epg_manual"):
                continue
            eid = (c.get("epg_id") or "").strip().lower()
            if not is_usable_epg_id(eid):
                continue
            core = match_name(c.get("name") or "")
            if core and not epg_id_matches_name(eid, c.get("name") or "", c.get("epg_name") or ""):
                bad_cores.setdefault(eid, set()).add(core)
                bad_families.setdefault(eid, set()).add(_epg_core_family(core))
        poisoned_eids = {eid for eid, cores in bad_cores.items() if len(cores) >= 3 and len(bad_families.get(eid, ())) >= 3}

        in_provider_primary: set[str] = set()
        in_provider_sec: dict[str, tuple[str, "EpgCache", str]] = {}
        for c in channels:
            sid = c["stream_id"]
            eid = (c.get("epg_id") or "").strip().lower()
            manual = c.get("epg_manual")
            if not eid or (not manual and not is_usable_epg_id(eid)):
                continue
            if not manual and eid in poisoned_eids and not epg_id_matches_name(eid, c.get("name") or "", c.get("epg_name") or ""):
                continue
            pid = ch_provider[sid]
            if pid != "1" and pid in sec_available:
                pcache, pavail = sec_available[pid]
                if eid in pavail:
                    in_provider_sec[sid] = (pid, pcache, eid)
                    continue
            if eid in available:
                in_provider_primary.add(sid)
        in_provider_all = in_provider_primary | set(in_provider_sec)
        # Chaînes de « Mes groupes » (OTT, TNT, Maroc…) : guides externes d'abord, guide du fournisseur ensuite.
        prefer = {c["stream_id"] for c in channels if c.get("epg_prefer_external")}
        covered = in_provider_all - prefer
        sources: dict[str, dict] = {}
        for sid in covered:
            if sid in in_provider_sec:
                pid, _, eid = in_provider_sec[sid]
                sources[sid] = {"source": f"provider:{pid}", "id": eid}
            else:
                sources[sid] = {"source": "provider", "id": ""}
        # Guides externes : par identifiant XMLTV, puis par nom de chaîne (« FR: TF1 FHD » = « TF1 »).
        external: list[tuple["EpgCache", dict[str, list[str]]]] = []
        from_extra: set[str] = set()
        extras = self.extras
        if extras:
            remaining = [c for c in channels if c["stream_id"] not in covered]
            for position, extra in enumerate(extras, start=1):
                if not remaining:
                    break
                try:
                    extra.ensure(refresh=refresh)
                except EpgUnavailable:
                    continue
                available_ext = extra.upcoming_ids(now)
                auto_ids, by_name = extra.automatic_matches(available_ext)
                mapping: dict[str, list[str]] = {}
                left = []
                for c in remaining:
                    raw_eid = (c.get("epg_id") or "").strip().lower()
                    manual = c.get("epg_manual")
                    eid = raw_eid if (manual or is_usable_epg_id(raw_eid)) else ""
                    if eid and not manual and eid in poisoned_eids and not epg_id_matches_name(eid, c.get("name") or "", c.get("epg_name") or ""):
                        eid = ""
                    if not manual and c.get("epg_match_name", True):
                        # Nom propre du groupe (« TF1 ») d'abord, puis le nom de la chaîne chez le fournisseur.
                        name_eid = (by_name.get(squash_name(c["epg_name"]), "") if c.get("epg_name") else "") \
                            or by_name.get(squash_name(match_name(c["name"])), "")
                        if eid not in auto_ids:
                            eid = name_eid
                        elif name_eid and name_eid != eid and not epg_id_matches_name(eid, c.get("name") or "", c.get("epg_name") or ""):
                            eid = name_eid
                    if eid in (available_ext if manual else auto_ids):
                        ours = xml_channel_id(c)
                        if ours not in mapping.setdefault(eid, []):
                            mapping[eid].append(ours)
                        from_extra.add(c["stream_id"])
                        sources[c["stream_id"]] = {"source": f"external:{position}", "id": eid}
                    else:
                        left.append(c)
                remaining = left
                if mapping:
                    external.append((extra, mapping))
        if source_error is not None and not sec_available and not from_extra and (mode == "xmltv" or api is None):
            raise source_error
        # Pas trouvées dans les guides externes : guide du fournisseur (principal ou secondaire) s'il en a un.
        for sid in (in_provider_all & prefer) - from_extra:
            covered.add(sid)
            if sid in in_provider_sec:
                pid, _, eid = in_provider_sec[sid]
                sources[sid] = {"source": f"provider:{pid}", "id": eid}
            else:
                sources[sid] = {"source": "provider", "id": ""}
        # Un identifiant repris d'un guide externe n'est plus pris chez le fournisseur (sinon programmes en
        # double pour une autre chaîne de la playlist qui porte le même identifiant).
        taken = {ours.lower(): (extras.index(extra) + 1, eid) for extra, mapping in external
                 for eid, ours_list in mapping.items() for ours in ours_list}
        for c in channels:
            ours = xml_channel_id(c).lower()
            if c["stream_id"] in covered and ours in taken:
                covered.discard(c["stream_id"])
                from_extra.add(c["stream_id"])
                sources[c["stream_id"]] = {"source": f"external:{taken[ours][0]}", "id": taken[ours][1]}
        targets = [] if mode == "xmltv" or api is None else [
            c for c in channels if c["stream_id"] not in covered and c["stream_id"] not in from_extra]
        if api_ids is None and self.api_max:
            targets = targets[:self.api_max]  # ordre de la playlist : groupes perso et favoris d'abord
        if api_ids is not None:
            targets = [c for c in targets if c["stream_id"] in api_ids]
            if source is None and not sec_available and not targets and not from_extra:
                raise EpgUnavailable("Guide XMLTV du fournisseur indisponible : enregistre une playlist personnalisée "
                                     "pour obtenir un guide complété par l'API, puis réessaie")
        # Chaque chaîne interroge d'abord sa propre version, puis — si le fournisseur n'a aucun programme
        # pour elle — ses autres versions (doublons masqués : « VIP: TF1 », « FR: TF1 »…). Ordre des
        # appels = ordre de la playlist (groupes perso et favoris d'abord).
        source_of: dict[str, str] = {}
        to_fetch: list[str] = []
        for c in targets:
            chain = list(dict.fromkeys([*([c["epg_from"]] if c.get("epg_from") else []),
                                        c["stream_id"], *(c.get("alternates") or [])[:3]]))
            for sid in chain:
                known = api.known(sid) if hasattr(api, "known") else None
                if known is None:
                    source_of[c["stream_id"]] = sid
                    to_fetch.append(sid)
                    break
                if known:
                    source_of[c["stream_id"]] = sid
                    break
        fetched: dict[str, list[dict]] = {}
        if to_fetch:
            fetched, _ = api.episodes_for(list(dict.fromkeys(to_fetch)), budget=budget)
        listings: dict[str, list[dict]] = {}
        for c in targets:
            sid = source_of.get(c["stream_id"])
            items = fetched.get(sid) if sid in fetched else (api.known(sid) if sid and hasattr(api, "known") else None)
            if items and any(p["start"] < now + EPG_LOOKAHEAD and p["stop"] > now for p in items):
                listings[c["stream_id"]] = items
        pending = api.pending_count(list(dict.fromkeys(to_fetch))) if to_fetch else 0
        from_api = {c["stream_id"] for c in targets if listings.get(c["stream_id"])}
        by_sid = {c["stream_id"]: c for c in channels}
        covered_primary = {sid for sid in covered if sid not in in_provider_sec}
        sec_mappings: dict[str, tuple["EpgCache", dict[str, list[str]]]] = {}
        for sid in covered:
            if sid in in_provider_sec:
                pid, pcache, eid = in_provider_sec[sid]
                sources[sid] = {"source": f"provider:{pid}", "id": eid}
                ours = xml_channel_id(by_sid[sid])
                _, mapping = sec_mappings.setdefault(pid, (pcache, {}))
                if ours not in mapping.setdefault(eid, []):
                    mapping[eid].append(ours)
            else:
                sources[sid] = {"source": "provider", "id": (by_sid[sid].get("epg_id") or "").strip().lower()}
        for sid in from_api:
            sources[sid] = {"source": "api", "id": ""}
        wanted = {c["epg_id"].strip().lower() for c in channels if c["stream_id"] in covered_primary}
        stats = {
            "mode": mode,
            "channels": len(channels),
            "xmltv": len(covered),
            "external": len(from_extra),
            "api": len(from_api),
            "pending": pending,
            "sources": sources,   # d'où vient le guide de chaque chaîne (fournisseur, guide externe n, API)
            # Séparateurs (« ##### FR | GÉNÉRALISTES ##### ») : pas des chaînes, jamais « sans programme ».
            "missing": [{"stream_id": c["stream_id"], "name": c["name"], "epg_id": c.get("epg_id") or ""}
                        for c in channels if c["stream_id"] not in covered and c["stream_id"] not in from_api
                        and c["stream_id"] not in from_extra and not is_separator(c["name"])],
        }
        digest = hashlib.sha256()
        digest.update(f"{mode}\n{self._source_key() if source else '-'}\n".encode())
        for item in sorted(wanted):
            digest.update(item.encode("utf-8", "replace") + b"\n")
        for pid in sorted(sec_mappings):
            pcache, mapping = sec_mappings[pid]
            digest.update(f"p{pid}:{pcache._source_key()}:{json.dumps(mapping, sort_keys=True)}\n".encode("utf-8", "replace"))
        for extra, mapping in external:
            digest.update(f"x:{extra._source_key()}:{json.dumps(mapping, sort_keys=True)}\n".encode("utf-8", "replace"))
        for c in sorted(targets, key=lambda c: c["stream_id"]):
            if c["stream_id"] in from_api:
                sid = source_of.get(c["stream_id"], c["stream_id"])
                digest.update(f"{c['stream_id']}:{xml_channel_id(c)}:{sid}:{api.fetched_at(sid)}\n".encode("utf-8", "replace"))
        signature = digest.hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        out = self.directory / f"p-{key}.xml"
        sig_file = self.directory / f"p-{key}.sig"
        with self._filter_lock:
            try:
                if out.exists() and sig_file.read_text() == signature:
                    return out, stats
            except OSError:
                pass
            fd, tmp = tempfile.mkstemp(prefix=f".p-{key}-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as dst:
                    if source is not None and wanted:
                        write_filtered_xmltv(source, dst, wanted, close=False)
                    else:
                        dst.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="Xtream M3U Local">\n')
                    for pid in sorted(sec_mappings):
                        pcache, mapping = sec_mappings[pid]
                        write_mapped_xmltv(pcache.source, dst, mapping)
                    for extra, mapping in external:
                        write_mapped_xmltv(extra.source, dst, mapping)
                    written: set[str] = set()
                    for c in targets:
                        items = listings.get(c["stream_id"])
                        xml_id = xml_channel_id(c)
                        if items and xml_id.lower() not in written:
                            written.add(xml_id.lower())
                            write_api_channel(dst, xml_id, c["name"], c.get("logo", ""), items)
                    dst.write("</tv>\n")
                os.chmod(tmp, 0o644)
                os.replace(tmp, out)
                sig_file.write_text(signature)
            except ET.ParseError as exc:
                raise EpgUnavailable("Guide XMLTV du fournisseur illisible") from exc
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return out, stats

    def forget(self, key: str) -> None:
        for name in (f"p-{key}.xml", f"p-{key}.sig"):
            try:
                (self.directory / name).unlink()
            except OSError:
                pass


def suggest_guides(sources: list[tuple[str, "EpgCache"]], missing: list[dict], *, per_channel: int = 3,
                   max_channels: int = 300) -> list[dict]:
    """Chaînes sans programme : identifiants XMLTV de noms proches, pris dans les guides disponibles.

    ``sources`` : ``[(libellé, EpgCache)]`` (guide du fournisseur, guides externes). Seules les chaînes du
    guide qui ont des programmes sont proposées, avec un score de ressemblance (0–100).
    """
    import difflib
    from .builder import match_name
    pool: dict[str, list[tuple[str, str, str, int]]] = {}   # nom comparable → [(id, nom, source, programmes)]
    for label, cache in sources:
        try:
            _, names = cache.automatic_matches(cache.upcoming_ids())
            labels, counts = cache.labels(), cache.index()
        except Exception:  # guide illisible : simplement ignoré
            continue
        for core, cid in names.items():
            count = counts.get(cid, 0)
            if count > 0:
                pool.setdefault(core, []).append((cid, labels.get(cid, cid), label, count))
    keys = list(pool)
    by_prefix: dict[str, list[str]] = {}
    for key in keys:
        by_prefix.setdefault(key[:2], []).append(key)
    result = []
    for channel in missing[:max_channels]:
        core = squash_name(match_name(channel.get("name") or ""))
        if len(core) < 2:
            continue
        digits = re.findall(r"\d+", core)
        # Candidats : même début de nom, ou l'un contient l'autre (« tf1 » / « tf1series »), puis ressemblance.
        candidates = set(by_prefix.get(core[:2], ()))
        if len(core) >= 3:
            candidates.update(k for k in keys if len(k) >= 3 and (core in k or k in core))
        scored = []
        for key in candidates:
            if re.findall(r"\d+", key) != digits:
                continue  # « France 2 » n'est pas « France 3 », ni « Sport 560 » « Sport 507 »
            ratio = difflib.SequenceMatcher(None, core, key).ratio()
            if key == core:
                ratio = 1.0
            if ratio >= 0.6:
                scored.append((ratio, key))
        scored.sort(key=lambda r: (-r[0], r[1]))
        options, seen = [], set()
        for ratio, key in scored:
            for cid, name, label, count in pool[key]:
                if (cid, label) in seen:
                    continue
                seen.add((cid, label))
                options.append({"id": cid, "name": name, "source": label, "programmes": count,
                                "score": int(round(ratio * 100))})
            if len(options) >= per_channel:
                break
        if options:
            result.append({"stream_id": channel["stream_id"], "name": channel["name"],
                           "suggestions": options[:per_channel]})
    return result


def squash_name(core: str) -> str:
    """« canal+ sport » = « canal plus sport » = « canalplussport » (rapprochement par nom)."""
    return core.replace("+", "plus").replace(" ", "")


def write_mapped_xmltv(source: Path, dst, mapping: dict[str, list[str]]) -> int:
    """Ajoute les chaînes et programmes d'un guide externe, renommés vers nos identifiants.

    ``mapping`` : ``{identifiant externe (minuscules): [identifiants de nos chaînes]}`` — une chaîne externe
    peut servir plusieurs chaînes de la playlist (versions HD/FHD/4K). Écrit dans une balise ``<tv>`` ouverte.
    """
    kept = 0
    depth = 0
    root = None
    for event, elem in ET.iterparse(source, events=("start", "end")):
        if event == "start":
            depth += 1
            if depth == 1:
                root = elem
            continue
        depth -= 1
        if depth == 1 and root is not None:
            attr = "id" if elem.tag == "channel" else "channel" if elem.tag == "programme" else None
            targets = mapping.get(elem.get(attr, "").strip().lower(), []) if attr else []
            for ours in targets:
                elem.set(attr, ours)
                elem.tail = "\n"
                dst.write(ET.tostring(elem, encoding="unicode"))
                kept += 1
            root.clear()
    return kept


def write_filtered_xmltv(source: Path, dst, wanted: set[str], *, close: bool = True) -> int:
    """Stream ``source`` into ``dst`` keeping only wanted channels/programmes. Returns kept elements.

    ``close=False`` laisse la balise ``<tv>`` ouverte pour y ajouter des programmes (API).
    """
    dst.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    kept = 0
    depth = 0
    root = None
    header_written = False
    for event, elem in ET.iterparse(source, events=("start", "end")):
        if event == "start":
            depth += 1
            if depth == 1:
                root = elem
                attrs = "".join(f" {k}={quoteattr(v)}" for k, v in elem.attrib.items())
                dst.write(f"<tv{attrs}>\n")
                header_written = True
            continue
        depth -= 1
        if depth == 1 and root is not None:
            if elem.tag == "channel":
                keep = elem.get("id", "").strip().lower() in wanted
            elif elem.tag == "programme":
                keep = elem.get("channel", "").strip().lower() in wanted
            else:
                keep = False
            if keep:
                elem.tail = "\n"
                dst.write(ET.tostring(elem, encoding="unicode"))
                kept += 1
            root.clear()
    if not header_written:
        dst.write("<tv>\n")
    if close:
        dst.write("</tv>\n")
    return kept


def parse_xmltv_time(value: str | None) -> int | None:
    """« 20261116200000 +0100 » → horodatage Unix (fuseau absent = UTC)."""
    if not value:
        return None
    text = value.strip()
    try:
        moment = datetime.strptime(text[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    offset = 0
    rest = text[14:].strip()
    if len(rest) == 5 and rest[0] in "+-" and rest[1:].isdigit():
        offset = (int(rest[1:3]) * 3600 + int(rest[3:5]) * 60) * (1 if rest[0] == "+" else -1)
    return int(moment.replace(tzinfo=timezone.utc).timestamp()) - offset


_schedule_cache: dict[str, tuple[int, dict[str, list[tuple[int, int, str, str]]]]] = {}
_schedule_lock = threading.Lock()


def read_schedule(path: Path) -> dict[str, list[tuple[int, int, str, str]]]:
    """Programmes d'un guide XMLTV : ``{id_de_chaîne (minuscules): [(début, fin, titre, description)]}``.

    Mis en cache tant que le fichier ne change pas (guide d'une playlist : quelques Mo au plus).
    """
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return {}
    key = str(path)
    with _schedule_lock:
        cached = _schedule_cache.get(key)
        if cached and cached[0] == stamp:
            return cached[1]
    schedule: dict[str, list[tuple[int, int, str, str]]] = {}
    try:
        for _, elem in ET.iterparse(path, events=("end",)):
            if elem.tag != "programme":
                if elem.tag == "channel":
                    elem.clear()
                continue
            start, stop = parse_xmltv_time(elem.get("start")), parse_xmltv_time(elem.get("stop"))
            channel = (elem.get("channel") or "").strip().lower()
            if start is not None and channel:
                title = (elem.findtext("title") or "").strip()
                desc = (elem.findtext("desc") or "").strip()
                schedule.setdefault(channel, []).append((start, stop or start + 1800, title, desc[:400]))
            elem.clear()
    except ET.ParseError:
        return {}
    for items in schedule.values():
        items.sort()
    with _schedule_lock:
        if len(_schedule_cache) > 20:
            _schedule_cache.clear()
        _schedule_cache[key] = (stamp, schedule)
    return schedule
