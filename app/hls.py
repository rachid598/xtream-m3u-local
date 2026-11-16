"""Relais HLS : manifestes ``.m3u8`` réécrits et segments relayés par le serveur local.

Le fournisseur sert ``/live/USER/PASS/<id>.m3u8`` (souvent après redirection vers un serveur
de diffusion avec jeton). Le relais :

1. télécharge le manifeste et le **réécrit** : chaque URI (segments, variantes de qualité,
   pistes audio/sous-titres, clés ``EXT-X-KEY``, ``EXT-X-MAP``…) devient une adresse locale
   opaque ``/hls/v/<code>.m3u8`` (manifeste) ou ``/hls/s/<code>.<ext>`` (segment) ;
2. relaie ensuite chaque requête vers l'URL d'origine, connue uniquement du serveur.

Aucune URL du fournisseur (ni identifiant, ni jeton) n'est transmise au lecteur.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict
from urllib.parse import urljoin, urlsplit

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{16,32}$")
URI_ATTR_RE = re.compile(r'URI="([^"]*)"')
# Balises dont l'attribut URI désigne un autre manifeste (sinon : segment / clé / init).
PLAYLIST_TAGS = ("#EXT-X-MEDIA", "#EXT-X-I-FRAME-STREAM-INF", "#EXT-X-RENDITION-REPORT", "#EXT-X-IMAGE-STREAM-INF")
EXT_RE = re.compile(r"\.([A-Za-z0-9]{1,5})$")
SEGMENT_MIME = {
    "ts": "video/mp2t", "aac": "audio/aac", "mp4": "video/mp4", "m4s": "video/iso.segment",
    "m4a": "audio/mp4", "mp3": "audio/mpeg", "vtt": "text/vtt", "webvtt": "text/vtt", "key": "application/octet-stream",
}


class HlsRegistry:
    """Codes opaques -> URL d'origine (expiration 15 min sans usage, 50 000 entrées au maximum)."""

    def __init__(self, ttl: int = 900, max_entries: int = 50_000):
        self.ttl = ttl
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[str, str, object, float]] = OrderedDict()
        self._by_key: dict[tuple[str, str, int], str] = {}
        self._lock = threading.Lock()

    def register(self, url: str, kind: str, session) -> str:
        key = (url, kind, id(session))
        now = time.monotonic()
        with self._lock:
            handle = self._by_key.get(key)
            if handle and handle in self._entries:
                self._entries[handle] = (url, kind, session, now)
                self._entries.move_to_end(handle)
                return handle
            handle = secrets.token_urlsafe(12)
            self._entries[handle] = (url, kind, session, now)
            self._by_key[key] = handle
            while len(self._entries) > self.max_entries:
                old, (old_url, old_kind, old_session, _) = self._entries.popitem(last=False)
                self._by_key.pop((old_url, old_kind, id(old_session)), None)
            return handle

    def resolve(self, handle: str, kind: str):
        """``(url, session)`` ou ``None`` si le code est inconnu ou expiré."""
        if not HANDLE_RE.fullmatch(handle):
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(handle)
            if entry is None or entry[1] != kind:
                return None
            url, _, session, used = entry
            if now - used > self.ttl:
                del self._entries[handle]
                self._by_key.pop((url, kind, id(session)), None)
                return None
            self._entries[handle] = (url, kind, session, now)
            self._entries.move_to_end(handle)
            return url, session

    def __len__(self) -> int:
        return len(self._entries)


def _is_playlist_uri(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return path.endswith(".m3u8") or path.endswith(".m3u")


def segment_ext(url: str) -> str:
    match = EXT_RE.search(urlsplit(url).path)
    ext = match.group(1).lower() if match else "ts"
    return ext if ext not in ("m3u8", "m3u") else "ts"


def rewrite_manifest(text: str, base_url: str, link) -> str:
    """Réécrit un manifeste HLS. ``link(url_absolue, est_un_manifeste) -> url_locale``."""
    if not text.lstrip("﻿").lstrip().startswith("#EXTM3U"):
        raise ValueError("Réponse du fournisseur qui n'est pas un manifeste HLS")
    out: list[str] = []
    expect_playlist = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            out.append("")
            continue
        if line.startswith("#"):
            if line.startswith("#EXT-X-STREAM-INF"):
                expect_playlist = True
            if 'URI="' in line:
                is_playlist = line.startswith(PLAYLIST_TAGS)

                def replace(match: re.Match) -> str:
                    target = urljoin(base_url, match.group(1))
                    if urlsplit(target).scheme not in ("http", "https"):
                        return match.group(0)  # data:, skd:// … laissés tels quels
                    return f'URI="{link(target, is_playlist)}"'

                line = URI_ATTR_RE.sub(replace, line)
            out.append(line)
            continue
        target = urljoin(base_url, line)
        if urlsplit(target).scheme in ("http", "https"):
            out.append(link(target, expect_playlist or _is_playlist_uri(target)))
        else:
            out.append(line)
        expect_playlist = False
    return "\n".join(out) + "\n"
