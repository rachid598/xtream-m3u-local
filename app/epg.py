"""Cache disque du guide XMLTV fournisseur et EPG filtré par playlist.

- ``/epg.xml`` sert le XMLTV complet, téléchargé au plus toutes les ``EPG_CACHE_HOURS``.
- ``/p/<slug>.xml`` sert uniquement les chaînes/programmes de la playlist ``slug``
  (beaucoup plus léger pour TiviMate / Android TV).

Bibliothèque standard uniquement. Le parseur ``expat`` de Python 3.13 protège contre
l'expansion d'entités (« billion laughs ») et ElementTree ne résout pas les entités externes.
"""
from __future__ import annotations

import gzip
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from http.client import HTTPException
from xml.sax.saxutils import quoteattr

LOG = logging.getLogger("xtream-m3u-local.epg")
MAX_EPG_BYTES = 1024 * 1024 * 1024  # 1 Gio
RETRY_AFTER_FAILURE = 900  # 15 min sans nouvelle tentative après un échec


class EpgUnavailable(Exception):
    """Le guide XMLTV n'est pas disponible (sans détail sensible)."""


class EpgCache:
    def __init__(self, url_factory: Callable[[], str], data_dir: str, *, max_age: int = 12 * 3600,
                 user_agent: str = "XtreamM3ULocal"):
        self._url_factory = url_factory
        self.directory = Path(data_dir) / "epg"
        self.source = self.directory / "xmltv.xml"
        self.max_age = max(60, max_age)
        self.user_agent = user_agent
        self._download_lock = threading.Lock()
        self._filter_lock = threading.Lock()
        self._failed_at = 0.0

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
            except EpgUnavailable:
                self._failed_at = time.monotonic()
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
            if magic == b"\x1f\x8b":
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

    def forget(self, key: str) -> None:
        for name in (f"p-{key}.xml", f"p-{key}.sig"):
            try:
                (self.directory / name).unlink()
            except OSError:
                pass


def write_filtered_xmltv(source: Path, dst, wanted: set[str]) -> int:
    """Stream ``source`` into ``dst`` keeping only wanted channels/programmes. Returns kept elements."""
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
    dst.write("</tv>\n")
    return kept
