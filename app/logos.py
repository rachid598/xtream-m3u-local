"""Logos haute qualité : base ouverte iptv-org (France, Belgique, Maroc par défaut) + cache disque.

- L'index (nom normalisé → meilleur logo) est construit depuis ``channels.csv`` et ``logos.csv``
  de la base iptv-org (https://github.com/iptv-org/database), rafraîchi tous les 7 jours et gardé
  dans ``DATA_DIR/cache/logos/index.json.gz``.
- Chaque logo retenu reçoit une clé courte (hachage de son adresse) ; les images sont téléchargées
  une seule fois, en arrière-plan ou à la première demande, puis servies par ``/logo/<clé>.png``.
- Seules les adresses de l'index sont téléchargées : ce n'est pas un relais ouvert.
- Un nom préfixé par un autre pays (« UK: Action », catégorie « US | … ») n'est jamais remplacé.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import logging
import os
import queue
import re
import threading
import time
import unicodedata
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .builder import match_name

LOG = logging.getLogger("xtream_m3u.logos")

DEFAULT_SOURCE = "https://raw.githubusercontent.com/iptv-org/database/master/data"
# Logos uniformes (PNG transparents ~512 px) par pays, prioritaires sur ceux d'iptv-org.
DEFAULT_TVLOGOS = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/countries"
TVLOGOS_FOLDERS = {"FR": "france", "BE": "belgium", "CH": "switzerland", "LU": "luxembourg", "CA": "canada",
                   "DE": "germany", "ES": "spain", "IT": "italy", "PT": "portugal", "NL": "netherlands",
                   "UK": "united-kingdom", "GB": "united-kingdom", "US": "united-states", "AE": "united-arab-emirates"}
_MOSAIC_REF = re.compile(r"^\[([^\]]+)\]:\s*([\w./-]+\.png)\s*$", re.MULTILINE)
DEFAULT_COUNTRIES = ("FR", "BE", "MA")
INDEX_MAX_AGE = 7 * 86400
RETRY_AFTER = 3600            # échec du téléchargement de l'index : nouvel essai après 1 h
IMAGE_RETRY_AFTER = 6 * 3600  # image introuvable : nouvel essai après 6 h
MAX_CSV_BYTES = 40 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
KEY_RE = re.compile(r"[0-9a-f]{16}")
USER_AGENT = "Mozilla/5.0 (compatible; xtream-m3u-local)"

# Mots désignant un pays (préfixe du nom ou nom de catégorie du fournisseur).
_COUNTRY_WORDS = {
    "fr": "FR", "fra": "FR", "france": "FR", "french": "FR", "francais": "FR", "francaise": "FR",
    "be": "BE", "bel": "BE", "belgique": "BE", "belgium": "BE", "belgie": "BE", "belge": "BE",
    "ma": "MA", "mar": "MA", "maroc": "MA", "morocco": "MA", "marocaine": "MA", "marocain": "MA",
    "ch": "CH", "suisse": "CH", "switzerland": "CH", "lu": "LU", "luxembourg": "LU",
    "ca": "CA", "quebec": "CA", "canada": "CA",
    "uk": "UK", "gb": "UK", "us": "US", "usa": "US", "de": "DE", "germany": "DE", "deutschland": "DE",
    "es": "ES", "espana": "ES", "spain": "ES", "it": "IT", "italia": "IT", "italy": "IT",
    "pt": "PT", "portugal": "PT", "nl": "NL", "nederland": "NL", "tr": "TR", "turkey": "TR", "turkiye": "TR",
    "dz": "DZ", "algerie": "DZ", "algeria": "DZ", "tn": "TN", "tunisie": "TN", "tunisia": "TN",
    "pl": "PL", "polska": "PL", "gr": "GR", "ro": "RO", "ru": "RU", "in": "IN", "india": "IN", "pk": "PK",
    "se": "SE", "no": "NO", "dk": "DK", "fi": "FI", "al": "AL", "ex": "EXYU", "yu": "EXYU", "br": "BR",
    "mx": "MX", "ie": "IE", "at": "AT", "hu": "HU", "cz": "CZ", "sk": "SK", "hr": "HR", "rs": "RS",
    "bg": "BG", "ua": "UA", "il": "IL", "ir": "IR", "af": "AF", "eg": "EG", "sa": "SA", "ae": "AE",
    "qa": "QA", "kw": "KW", "iq": "IQ", "sy": "SY", "lb": "LB", "jo": "JO", "cn": "CN", "jp": "JP",
    "kr": "KR", "ph": "PH", "vn": "VN", "th": "TH", "latino": "LATAM", "latam": "LATAM",
}
_PREFIX = re.compile(r"^\W*([A-Za-z]{2,3})\s*[:|\-\]●•»◉]")


def squash(core: str) -> str:
    """Clé de rapprochement tolérante : « bfm tv » = « bfmtv », « canal plus » = « canal+ »."""
    return core.replace("+", "plus").replace(" ", "")


def country_hint(name: str, category: str = "") -> str | None:
    """Pays annoncé par le préfixe du nom (« FR: », « |BE| ») ou, à défaut, par la catégorie."""
    found = _PREFIX.match(name or "")
    if found and found.group(1).lower() in _COUNTRY_WORDS:
        return _COUNTRY_WORDS[found.group(1).lower()]
    text = unicodedata.normalize("NFKD", category or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    for token in re.split(r"[^a-z]+", text):
        if token in _COUNTRY_WORDS:
            return _COUNTRY_WORDS[token]
    return None


def logo_key(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def _default_fetch(url: str, limit: int) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 (http(s) vérifié)
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("fichier trop volumineux")
        return data, response.headers.get("Content-Type", "")


def _image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


_FORMAT_RANK = {"PNG": 3, "JPEG": 2, "JPG": 2, "WEBP": 1, "GIF": 0}


def parse_mosaic(text: str, country: str, base_url: str) -> list[dict]:
    """Références « [canal-plus-sport-360]:canal-plus-sport-360-fr.png » d'un dossier tv-logos."""
    found = []
    for label, path in _MOSAIC_REF.findall(text or ""):
        if ".." in path or path.startswith("/"):
            continue
        name = label.replace("-", " ")
        names = [name]
        if country == "FR" and " french" in name:
            names.append(name.replace(" french", ""))  # « bein sports 1 french » = « beIN Sports 1 »
        found.append({"country": country, "names": names, "url": f"{base_url.rstrip('/')}/{path}",
                      "width": 512, "height": 0, "variant": "/" in path})
    return found


def build_index(channels_csv: str, logos_csv: str, countries: tuple[str, ...], extra: list[dict] | None = None) -> dict:
    """Index compact ``{"logos": {clé: [url, pays, largeur, hauteur]}, "names": {nom: [clé, …]}}``.

    ``extra`` : logos tv-logos (:func:`parse_mosaic`), préférés à ceux d'iptv-org pour un même nom.
    """
    allowed = {c.upper() for c in countries}
    rank = {c: i for i, c in enumerate(c.upper() for c in countries)}
    channels: dict[str, tuple[str, list[str], bool, str]] = {}
    for row in csv.DictReader(io.StringIO(channels_csv or "")):
        country = (row.get("country") or "").strip().upper()
        if country not in allowed or (row.get("is_nsfw") or "").strip().upper() == "TRUE":
            continue
        names = [row.get("name") or ""] + [n for n in (row.get("alt_names") or "").split(";") if n.strip()]
        channels[(row.get("id") or "").strip()] = (country, names, bool((row.get("closed") or "").strip()),
                                                   (row.get("network") or "").strip())
    best: dict[str, tuple[tuple, str, int, int]] = {}
    for row in csv.DictReader(io.StringIO(logos_csv or "")):
        cid = (row.get("channel") or "").strip()
        if cid not in channels:
            continue
        url = (row.get("url") or "").strip()
        fmt = (row.get("format") or "").strip().upper()
        if urlsplit(url).scheme not in ("http", "https") or fmt not in _FORMAT_RANK:
            continue  # SVG ignoré : mal pris en charge par les lecteurs TV
        try:
            width, height = int(row.get("width") or 0), int(row.get("height") or 0)
        except ValueError:
            width = height = 0
        tags = (row.get("tags") or "").lower()
        score = (not (row.get("feed") or "").strip(),                     # logo de la chaîne, pas d'une déclinaison
                 (row.get("in_use") or "").strip().upper() == "TRUE",     # logo actuel
                 "white" not in tags and "transparent-white" not in tags, # lisible sur fond clair
                 _FORMAT_RANK[fmt], width * height)
        if cid not in best or score > best[cid][0]:
            best[cid] = (score, url, width, height)
    # (pays, noms, adresse, largeur, hauteur, ordre de préférence)
    sources: list[tuple[str, list[str], str, int, int, tuple]] = []
    for entry in extra or []:
        if entry["country"] in allowed and urlsplit(entry["url"]).scheme in ("http", "https"):
            sources.append((entry["country"], entry["names"], entry["url"], entry["width"], entry["height"],
                            (False, rank.get(entry["country"], 99), 0, bool(entry.get("variant")), 0)))
    for cid, (score, url, width, height) in best.items():
        country, names, closed, network = channels[cid]
        sources.append((country, names, url, width, height, (closed, rank.get(country, 99), 1, False, -width * height)))
        if network:
            # Nom du réseau en dernier recours : « Medi 1 TV » → « Medi1TV Arabic » / « … Maghreb ».
            sources.append((country, [network], url, width, height, (closed, rank.get(country, 99), 2, False, -width * height)))
    logos: dict[str, list] = {}
    candidates: dict[str, list[tuple[tuple, str]]] = {}
    for country, names, url, width, height, order in sources:
        key = logo_key(url)
        logos[key] = [url, country, width, height]
        for name in names:
            core = squash(match_name(name))
            if len(core) >= 2 and not core.isdigit():
                candidates.setdefault(core, []).append((order, key))
    index = {core: list(dict.fromkeys(key for _, key in sorted(found)))[:6] for core, found in candidates.items()}
    return {"logos": logos, "names": index}


class LogoLibrary:
    """Index des logos + cache d'images (un seul objet par processus, voir :func:`install`)."""

    def __init__(self, data_dir: str, *, countries: tuple[str, ...] = DEFAULT_COUNTRIES,
                 source: str = DEFAULT_SOURCE, tvlogos: str = DEFAULT_TVLOGOS, fetch=None,
                 max_age: int = INDEX_MAX_AGE) -> None:
        self.countries = tuple(c.upper() for c in countries)
        self.source = source.rstrip("/")
        self.tvlogos = tvlogos.rstrip("/")
        self.fetch = fetch or _default_fetch
        self.max_age = max_age
        self.root = Path(data_dir) / "cache" / "logos"
        self.images = self.root / "img"
        self._lock = threading.Lock()
        self._logos: dict[str, list] = {}
        self._names: dict[str, list[str]] = {}
        self._built_at = 0.0
        self._error = ""
        self._next_try = 0.0
        self._loading = False
        self._failed: dict[str, float] = {}
        self._key_locks: dict[str, threading.Lock] = {}
        self._queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._worker: threading.Thread | None = None
        self._spawned_at = 0.0
        self._restore()

    # ------------------------------------------------------------------ index
    @property
    def index_file(self) -> Path:
        return self.root / "index.json.gz"

    def _restore(self) -> None:
        try:
            with gzip.open(self.index_file, "rt", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if (data.get("countries") != list(self.countries) or data.get("source") != self.source
                or data.get("tvlogos") != self.tvlogos):
            return
        self._logos, self._names = data.get("logos") or {}, data.get("names") or {}
        self._built_at = float(data.get("built_at") or 0)

    def refresh(self, *, force: bool = False) -> bool:
        """Télécharge et reconstruit l'index si besoin. Retourne ``True`` si l'index est utilisable."""
        now = time.time()
        with self._lock:
            fresh = self._logos and now - self._built_at < self.max_age
            if self._loading or (fresh and not force) or (not force and now < self._next_try):
                return bool(self._logos)
            self._loading = True
        try:
            texts, errors, extra = [], [], []
            try:
                for name in ("channels.csv", "logos.csv"):
                    data, _ = self.fetch(f"{self.source}/{name}", MAX_CSV_BYTES)
                    texts.append(data.decode("utf-8-sig"))
            except Exception as exc:
                texts, errors = ["", ""], [f"iptv-org : {type(exc).__name__}"]
            if self.tvlogos:
                for country in self.countries:
                    folder = TVLOGOS_FOLDERS.get(country)
                    if not folder:
                        continue
                    base = f"{self.tvlogos}/{folder}"
                    try:
                        data, _ = self.fetch(f"{base}/0_all_logos_mosaic.md", MAX_CSV_BYTES)
                        extra.extend(parse_mosaic(data.decode("utf-8", "replace"), country, base))
                    except Exception as exc:
                        errors.append(f"tv-logos {folder} : {type(exc).__name__}")
            index = build_index(texts[0], texts[1], self.countries, extra)
            if errors and not index["logos"]:
                raise ValueError(" ; ".join(errors))
            if not index["logos"]:
                raise ValueError("aucun logo pour " + ", ".join(self.countries))
            # Une source en échec : index utilisé tout de suite, nouvel essai complet dans 1 h.
            stamp = now if not errors else now - self.max_age + RETRY_AFTER
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.index_file.with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8") as handle:
                json.dump({"source": self.source, "tvlogos": self.tvlogos, "countries": list(self.countries), "built_at": stamp, **index}, handle)
            os.replace(tmp, self.index_file)
            with self._lock:
                self._logos, self._names, self._built_at = index["logos"], index["names"], stamp
                self._error = " ; ".join(errors)  # source partielle : index utilisable, signalé au diagnostic
            LOG.info("Logos : %d chaînes indexées (%s)", len(index["logos"]), ", ".join(self.countries))
        except Exception as exc:  # réseau, CSV, disque : l'ancien index reste en place
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"[:200]
                self._next_try = now + RETRY_AFTER
            LOG.warning("Logos : index non mis à jour (%s)", self._error)
        finally:
            with self._lock:
                self._loading = False
        return bool(self._logos)

    def refresh_in_background(self) -> None:
        self._spawned_at = time.time()
        threading.Thread(target=self.refresh, daemon=True, name="logos-index").start()

    def maybe_refresh(self) -> None:
        """Index absent ou vieux de plus de 7 jours : mise à jour en arrière-plan (sans attendre)."""
        now = time.time()
        if self._loading or now < self._next_try or now - self._spawned_at < 300:
            return
        if self._logos and now - self._built_at < self.max_age:
            return
        self.refresh_in_background()

    def status(self) -> dict:
        try:
            cached = sum(1 for _ in self.images.iterdir())
        except OSError:
            cached = 0
        return {"countries": list(self.countries), "channels": len(self._logos), "names": len(self._names),
                "updated_at": int(self._built_at) or None, "images": cached, "error": self._error,
                "loading": self._loading}

    # --------------------------------------------------------------- matching
    def match(self, name: str, category: str = "") -> str | None:
        """Clé du meilleur logo pour une chaîne, ou ``None`` (inconnue ou d'un autre pays)."""
        if not self._names:
            return None
        hint = country_hint(name, category)
        if hint and hint not in self.countries:
            return None
        keys = self._names.get(squash(match_name(name)))
        if not keys:
            return None
        if hint:
            for key in keys:
                if self._logos.get(key, [None, None])[1] == hint:
                    return key
        return keys[0]

    def url_of(self, key: str) -> str | None:
        entry = self._logos.get(key)
        return entry[0] if entry else None

    # ----------------------------------------------------------------- images
    def _cached(self, key: str) -> Path | None:
        for ext in ("png", "jpg", "webp", "gif"):
            path = self.images / f"{key}.{ext}"
            if path.is_file():
                return path
        return None

    def image(self, key: str, *, download: bool = True) -> tuple[Path | None, str | None]:
        """``(fichier en cache, adresse d'origine)`` ; télécharge l'image si besoin (une fois)."""
        if not KEY_RE.fullmatch(key or ""):
            return None, None
        url = self.url_of(key)
        cached = self._cached(key)
        if cached or not url or not download:
            return cached, url
        if time.time() < self._failed.get(key, 0):
            return None, url
        with self._lock:
            lock = self._key_locks.setdefault(key, threading.Lock())
        with lock:
            cached = self._cached(key)
            if cached:
                return cached, url
            try:
                data, _ = self.fetch(url, MAX_IMAGE_BYTES)
                kind = _image_type(data)
                if not kind:
                    raise ValueError("ce n'est pas une image PNG/JPEG/WebP/GIF")
                ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}[kind]
                self.images.mkdir(parents=True, exist_ok=True)
                path = self.images / f"{key}.{ext}"
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)
                return path, url
            except Exception as exc:
                self._failed[key] = time.time() + IMAGE_RETRY_AFTER
                LOG.info("Logo %s non téléchargé : %s", key, type(exc).__name__)
                return None, url

    def prefetch(self, keys) -> None:
        """Télécharge en arrière-plan les logos pas encore en cache (un à la fois, sans bloquer)."""
        added = False
        for key in keys:
            if key in self._queued or self._cached(key) or time.time() < self._failed.get(key, 0):
                continue
            self._queued.add(key)
            self._queue.put(key)
            added = True
        if added and (self._worker is None or not self._worker.is_alive()):
            self._worker = threading.Thread(target=self._drain, daemon=True, name="logos-prefetch")
            self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                key = self._queue.get(timeout=5)
            except queue.Empty:
                return
            try:
                self.image(key)
            finally:
                self._queued.discard(key)


_LIBRARY: LogoLibrary | None = None


def install(library: LogoLibrary | None) -> None:
    global _LIBRARY
    _LIBRARY = library


def current() -> LogoLibrary | None:
    return _LIBRARY


def countries_from_env() -> tuple[str, ...]:
    raw = os.environ.get("LOGO_COUNTRIES")
    if raw is None:
        return DEFAULT_COUNTRIES
    return tuple(dict.fromkeys(c.strip().upper() for c in re.split(r"[,;\s]+", raw) if re.fullmatch(r"[A-Za-z]{2}", c.strip())))


def from_env(data_dir: str) -> LogoLibrary | None:
    """Bibliothèque configurée par ``LOGO_COUNTRIES`` (vide = désactivé) et ``LOGO_SOURCE``."""
    countries = countries_from_env()
    if not countries:
        return None
    source = os.environ.get("LOGO_SOURCE", "").strip() or DEFAULT_SOURCE
    if urlsplit(source).scheme not in ("http", "https"):
        LOG.warning("LOGO_SOURCE ignoré (adresse http(s) attendue)")
        source = DEFAULT_SOURCE
    return LogoLibrary(data_dir, countries=countries, source=source)


def serve(handler, path: str, *, head_only: bool) -> bool:
    """Route ``/logo/<clé>.png`` commune aux deux ports. Retourne ``False`` si ce n'est pas un logo."""
    found = re.fullmatch(r"/logo/([0-9a-f]{16})\.png", path)
    if not found:
        return False
    library = current()
    cached, url = library.image(found.group(1), download=not head_only) if library else (None, None)
    if cached:
        data = cached.read_bytes()
        kind = _image_type(data) or "application/octet-stream"
        handler.send_response(200)
        handler.send_header("Content-Type", kind)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "public, max-age=604800")
        handler.end_headers()
        if not head_only:
            handler.wfile.write(data)
    elif url:
        # Téléchargement impossible pour l'instant : le lecteur va chercher l'original.
        handler.send_response(302)
        handler.send_header("Location", url)
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    else:
        handler.send_response(404)
        handler.send_header("Content-Type", "text/plain; charset=utf-8")
        handler.send_header("Content-Length", "0")
        handler.end_headers()
    return True
