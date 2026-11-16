"""Outils HTTP communs aux ports 7777 et 7778 : compression gzip et validation par ETag.

- gzip uniquement si le client l'annonce (``Accept-Encoding: gzip``) et pour les contenus texte
  (M3U, XML, JSON, CSS/JS) d'au moins 1 Kio : une grosse playlist VOD est divisée par 5 à 10.
- ETag + ``If-None-Match`` → ``304 Not Modified`` : un lecteur qui recharge une playlist
  inchangée ne retélécharge rien.
"""
from __future__ import annotations

import gzip
import hashlib
import zlib

COMPRESSIBLE = ("text/", "audio/x-mpegurl", "application/json", "application/xml",
                "application/vnd.apple.mpegurl", "image/svg+xml", "application/javascript")
MIN_GZIP_SIZE = 1024


def accepts_gzip(headers) -> bool:
    raw = (headers.get("Accept-Encoding") or "").lower()
    for part in raw.split(","):
        token, _, params = part.strip().partition(";")
        if token.strip() in ("gzip", "*"):
            q = params.strip()
            if q.startswith("q="):
                try:
                    return float(q[2:]) > 0
                except ValueError:
                    return False
            return True
    return False


def compressible(content_type: str) -> bool:
    return content_type.lower().startswith(COMPRESSIBLE)


def etag_of(content: bytes) -> str:
    return '"' + hashlib.sha1(content).hexdigest()[:20] + '"'


def etag_matches(headers, etag: str) -> bool:
    raw = headers.get("If-None-Match")
    if not raw:
        return False
    candidates = [c.strip().removeprefix("W/") for c in raw.split(",")]
    return "*" in candidates or etag in candidates


def encode_body(headers, content: bytes, content_type: str) -> tuple[bytes, dict[str, str]]:
    """Retourne ``(corps, en-têtes supplémentaires)`` en compressant si pertinent."""
    if len(content) >= MIN_GZIP_SIZE and compressible(content_type) and accepts_gzip(headers):
        return gzip.compress(content, compresslevel=6), {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}
    if compressible(content_type):
        return content, {"Vary": "Accept-Encoding"}
    return content, {}


def gzip_stream(src, write, chunk: int = 256 * 1024) -> None:
    """Compresse un fichier en flux (gros guides XMLTV) sans le charger en mémoire."""
    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
    while True:
        data = src.read(chunk)
        if not data:
            break
        out = compressor.compress(data)
        if out:
            write(out)
    write(compressor.flush())
