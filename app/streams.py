"""Lectures en cours et limite de connexions simultanées chez le fournisseur.

Un abonnement Xtream autorise souvent une seule connexion. Sans gestion, quand on zappe ou
qu'une deuxième TV démarre, l'ancien flux occupe encore la connexion et le fournisseur
répond « 509 / limite atteinte ». Le gestionnaire :

- compte les lectures par fournisseur (direct TS, films, épisodes, replay, sessions HLS) ;
- quand la limite est atteinte, coupe le flux du **même appareil** s'il en a un (zapping), sinon
  le plus ancien d'un autre appareil (``STREAM_TAKEOVER=true``) ou refuse proprement (``false``) ;
- empêche pendant 30 s l'appareil coupé de reprendre **le même** flux (sinon son lecteur
  réessaie tout seul et les deux TV se coupent mutuellement en boucle) ;
- expose la liste des lectures à l'interface Web (avec bouton « Arrêter »).
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import Callable

LOG = logging.getLogger("xtream-m3u-local.streams")
COOLDOWN_SECONDS = 30
HLS_IDLE_SECONDS = 30


class LimitReached(Exception):
    """La limite de connexions est atteinte et la reprise automatique est désactivée."""


class EvictedRecently(Exception):
    """Cet appareil vient d'être coupé pour ce flux : il ne peut pas le reprendre tout de suite."""


class StreamSession:
    _ids = itertools.count(1)

    def __init__(self, *, kind: str, item_id: str, client: str, provider: str, label: str, virtual: bool):
        self.id = next(self._ids)
        self.kind = kind
        self.item_id = item_id
        self.client = client
        self.provider = provider
        self.label = label
        self.virtual = virtual          # HLS : pas de connexion persistante, actif tant que des requêtes arrivent
        self.started = time.time()
        self.last_activity = time.monotonic()
        self.bytes = 0
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self._closer: Callable[[], None] | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.client, self.kind, self.item_id)

    def attach(self, closer: Callable[[], None]) -> None:
        """Fonction qui ferme la connexion fournisseur (appelée en cas de coupure)."""
        self._closer = closer
        if self.cancelled.is_set():
            self._close_upstream()

    def _close_upstream(self) -> None:
        closer, self._closer = self._closer, None
        if closer:
            try:
                closer()
            except Exception:  # fermeture « au mieux » depuis un autre thread
                pass

    def cancel(self) -> None:
        self.cancelled.set()
        self._close_upstream()
        if self.virtual:
            self.closed.set()

    def touch(self, nbytes: int = 0) -> None:
        self.bytes += nbytes
        self.last_activity = time.monotonic()

    def as_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "item_id": self.item_id, "label": self.label,
            "client": self.client, "provider": self.provider, "started_at": int(self.started),
            "seconds": int(time.time() - self.started), "bytes": self.bytes,
        }


class StreamManager:
    def __init__(self, limit_for: Callable[[str], int], *, takeover: bool = True):
        self._limit_for = limit_for
        self.takeover = takeover
        self._lock = threading.Lock()
        self._sessions: dict[int, StreamSession] = {}
        self._cooldown: dict[tuple[str, str, str], float] = {}

    # ------------------------------------------------------------ interne
    def _expire(self) -> None:
        now = time.monotonic()
        for sid, s in list(self._sessions.items()):
            if s.virtual and now - s.last_activity > HLS_IDLE_SECONDS:
                s.closed.set()
                del self._sessions[sid]
        for key, until in list(self._cooldown.items()):
            if until < now:
                del self._cooldown[key]

    def limit(self, provider: str = "1") -> int:
        try:
            return max(0, int(self._limit_for(provider)))
        except Exception:
            return 0

    # ------------------------------------------------------------- public
    def acquire(self, *, kind: str, item_id: str, client: str, provider: str = "1", label: str = "",
                virtual: bool = False) -> StreamSession:
        """Réserve une connexion fournisseur pour une lecture ; coupe une autre lecture si nécessaire."""
        evicted: list[StreamSession] = []
        with self._lock:
            self._expire()
            key = (client, kind, item_id)
            if key in self._cooldown:
                raise EvictedRecently()
            if virtual:
                for s in self._sessions.values():
                    if s.virtual and s.key == key and not s.cancelled.is_set():
                        s.touch()
                        return s
            # Même appareil, même contenu (avance rapide, reconnexion) : on remplace l'ancienne connexion.
            for s in list(self._sessions.values()):
                if s.key == key:
                    s.cancel()
                    del self._sessions[s.id]
                    evicted.append(s)
            limit = self.limit(provider)
            if limit:
                active = sorted((s for s in self._sessions.values() if s.provider == provider),
                                key=lambda s: s.started)
                while len(active) >= limit:
                    same_client = [s for s in active if s.client == client]
                    if not same_client and not self.takeover:
                        # Reprise désactivée : on ne coupe jamais un autre appareil.
                        raise LimitReached(limit)
                    victim = (same_client or active)[0]
                    active.remove(victim)
                    victim.cancel()
                    del self._sessions[victim.id]
                    evicted.append(victim)
                    if victim.client != client:
                        # L'autre appareil va réessayer tout seul : on l'en empêche quelques secondes.
                        self._cooldown[victim.key] = time.monotonic() + COOLDOWN_SECONDS
                    LOG.info("Limite de %d connexion(s) : arrêt de « %s » (%s) pour « %s »",
                             limit, victim.label or victim.item_id, victim.client, label or item_id)
            session = StreamSession(kind=kind, item_id=item_id, client=client, provider=provider,
                                    label=label, virtual=virtual)
            self._sessions[session.id] = session
        # Laisser le temps aux connexions coupées de se fermer avant d'en ouvrir une nouvelle.
        for victim in evicted:
            victim.closed.wait(1.5)
        return session

    def release(self, session: StreamSession) -> None:
        with self._lock:
            if self._sessions.get(session.id) is session:
                del self._sessions[session.id]
        session.closed.set()

    def is_active(self, session: StreamSession) -> bool:
        return not session.cancelled.is_set()

    def stop(self, session_id: int) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            self._cooldown[session.key] = time.monotonic() + COOLDOWN_SECONDS
        session.cancel()
        return True

    def list(self) -> list[dict]:
        with self._lock:
            self._expire()
            return [s.as_dict() for s in sorted(self._sessions.values(), key=lambda s: s.started)]

    def count(self, provider: str | None = None) -> int:
        with self._lock:
            self._expire()
            return sum(1 for s in self._sessions.values() if provider is None or s.provider == provider)
