"""Lectures en cours et limite de connexions simultanées chez le fournisseur.

Un abonnement Xtream autorise souvent une seule connexion. Sans gestion, quand on zappe ou
qu'une deuxième TV démarre, l'ancien flux occupe encore la connexion et le fournisseur
répond « 509 / limite atteinte ». Le gestionnaire :

- compte les lectures par fournisseur (direct TS, films, épisodes, replay, sessions HLS) ;
- quand la limite est atteinte, coupe le flux du **même appareil** s'il en a un (zapping), sinon
  le plus ancien d'un autre appareil (``STREAM_TAKEOVER=true``) ou refuse proprement (``false``) ;
- empêche pendant 30 s l'appareil coupé de reprendre **le même** flux (sinon son lecteur
  réessaie tout seul et les deux TV se coupent mutuellement en boucle) ;
- respecte la **priorité des appareils** : une TV « basse » ne coupe jamais une TV « haute » ;
- expose la liste des lectures à l'interface Web (avec bouton « Arrêter ») et un **historique**.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

LOG = logging.getLogger("xtream-m3u-local.streams")
COOLDOWN_SECONDS = 30
HLS_IDLE_SECONDS = 30
HISTORY_SIZE = 1000
PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}


class LimitReached(Exception):
    """Limite atteinte sans lecture que l'on puisse couper (reprise désactivée ou appareil prioritaire).

    ``args[0]`` = limite ; ``holder`` = lecture qui garde la connexion (si un appareil prioritaire la tient).
    """

    def __init__(self, limit: int, holder: "StreamSession | None" = None):
        super().__init__(limit)
        self.holder = holder


class EvictedRecently(Exception):
    """Cet appareil vient d'être coupé pour ce flux : il ne peut pas le reprendre tout de suite."""


class StreamSession:
    _ids = itertools.count(1)

    def __init__(self, *, kind: str, item_id: str, client: str, provider: str, label: str, virtual: bool,
                 device: str = "", priority: str = "normal", device_id: str = ""):
        self.id = next(self._ids)
        self.device_id = device_id      # identifiant de l'appareil (fiche « Appareils » : en cours de lecture)
        self.kind = kind
        self.item_id = item_id
        self.client = client
        self.provider = provider
        self.label = label
        self.virtual = virtual          # HLS : pas de connexion persistante, actif tant que des requêtes arrivent
        self.device = device            # nom de l'appareil (vue Appareils), vide si inconnu
        self.priority = priority if priority in PRIORITY_RANK else "normal"
        self.switches = 0               # bascules automatiques vers une autre version
        self.channel = label            # nom de la chaîne demandée (``label`` peut indiquer la version lue)
        self.started = time.time()
        self.last_activity = time.monotonic()
        self.bytes = 0
        self.cancelled = threading.Event()
        self.closed = threading.Event()
        self._closer: Callable[[], None] | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        # Adresse + appareil : deux TV derrière la même adresse (box, reverse proxy) restent distinctes.
        return (self.client, self.device, self.kind, self.item_id)

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
            "device": self.device, "priority": self.priority, "switches": self.switches,
            "device_id": self.device_id, "channel": self.channel,
            "idle": int(time.monotonic() - self.last_activity) if self.virtual else 0,
        }


class StreamManager:
    def __init__(self, limit_for: Callable[[str], int], *, takeover: bool = True, history_path: str | None = None):
        self._limit_for = limit_for
        self.takeover = takeover
        self._lock = threading.Lock()
        self._sessions: dict[int, StreamSession] = {}
        self._cooldown: dict[tuple[str, str, str], float] = {}
        self._history_path = Path(history_path) if history_path else None
        self._history: deque[dict] = deque(maxlen=HISTORY_SIZE)
        self._history_lock = threading.Lock()
        if self._history_path is not None:
            try:
                data = json.loads(self._history_path.read_text(encoding="utf-8"))
                self._history.extend(r for r in data if isinstance(r, dict))
            except (OSError, ValueError, TypeError):
                pass

    # ---------------------------------------------------------- historique
    def _record(self, session: StreamSession) -> None:
        if session.kind == "segment":
            return
        entry = {"started_at": int(session.started), "seconds": int(time.time() - session.started),
                 "kind": session.kind, "item_id": session.item_id, "label": session.channel or session.label,
                 "device": session.device, "client": session.client, "switches": session.switches,
                 "bytes": session.bytes, "provider": str(session.provider)}
        with self._history_lock:
            self._history.append(entry)
            self._save_history()

    def _save_history(self) -> None:
        """Écrit l'historique sur disque (appelé avec ``_history_lock`` tenu)."""
        if self._history_path is not None:
            try:
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".history-", suffix=".tmp", dir=str(self._history_path.parent))
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(list(self._history), f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, self._history_path)
            except OSError:
                LOG.warning("Historique des lectures non enregistré")

    def history(self) -> list[dict]:
        """Lectures terminées, de la plus récente à la plus ancienne."""
        with self._history_lock:
            return list(reversed(self._history))

    def clear_history(self, provider: str | None = None) -> None:
        """Vide l'historique (ou seulement celui d'un profil ; anciennes entrées sans profil : profil 1)."""
        with self._history_lock:
            if provider:
                kept = [e for e in self._history if str(e.get("provider") or "1") != provider]
                self._history.clear()
                self._history.extend(kept)
                self._save_history()
                return
            self._history.clear()
            if self._history_path is not None:
                try:
                    self._history_path.unlink()
                except OSError:
                    pass

    # ------------------------------------------------------------ interne
    def _expire(self) -> None:
        now = time.monotonic()
        for sid, s in list(self._sessions.items()):
            if s.virtual and now - s.last_activity > HLS_IDLE_SECONDS:
                s.closed.set()
                del self._sessions[sid]
                threading.Thread(target=self._record, args=(s,), daemon=True).start()
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
                virtual: bool = False, device: str = "", priority: str = "normal", device_id: str = "") -> StreamSession:
        """Réserve une connexion fournisseur pour une lecture ; coupe une autre lecture si nécessaire."""
        evicted: list[StreamSession] = []
        with self._lock:
            self._expire()
            key = (client, device, kind, item_id)
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
                rank = PRIORITY_RANK.get(priority, 1)
                while len(active) >= limit:
                    # Même appareil (même adresse et même appareil connu) : il change de chaîne, on remplace.
                    same_client = [s for s in active if s.client == client and s.device == device]
                    if not same_client and not self.takeover:
                        # Reprise désactivée : on ne coupe jamais un autre appareil.
                        raise LimitReached(limit)
                    # Un autre appareil n'est coupé que s'il n'est pas plus prioritaire (le moins prioritaire,
                    # puis le plus ancien) : la TV « haute » du salon n'est jamais coupée par la chambre.
                    others = sorted((s for s in active if PRIORITY_RANK.get(s.priority, 1) >= rank),
                                    key=lambda s: (-PRIORITY_RANK.get(s.priority, 1), s.started))
                    if not same_client and not others:
                        raise LimitReached(limit, active[0])
                    victim = (same_client or others)[0]
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
                                    label=label, virtual=virtual, device=device, priority=priority,
                                    device_id=device_id)
            self._sessions[session.id] = session
        # Laisser le temps aux connexions coupées de se fermer avant d'en ouvrir une nouvelle.
        for victim in evicted:
            victim.closed.wait(1.5)
        return session

    def start_delay(self, window: float = 20) -> float:
        """Secondes de calme encore souhaitées après le démarrage d'une lecture (priorité à la vidéo :
        les appels de guide attendent pour ne pas faire refuser le flux par le fournisseur)."""
        with self._lock:
            starts = [s.started for s in self._sessions.values() if not s.virtual]
        if not starts:
            return 0.0
        return max(0.0, max(starts) + window - time.time())

    def move(self, session: StreamSession, provider: str) -> bool:
        """Bascule vers une version d'un autre fournisseur : la place est prise chez lui, sans couper personne.

        Retourne ``False`` si ce fournisseur n'a plus de place (la version est alors sautée)."""
        with self._lock:
            self._expire()
            limit = self.limit(provider)
            used = sum(1 for s in self._sessions.values() if s.provider == provider and s is not session)
            if limit and used >= limit:
                return False
            session.provider = provider
            return True

    def release(self, session: StreamSession) -> None:
        with self._lock:
            if self._sessions.get(session.id) is session:
                del self._sessions[session.id]
        session.closed.set()
        self._record(session)

    def is_active(self, session: StreamSession) -> bool:
        return not session.cancelled.is_set()

    def stop(self, session_id: int, *, cooldown: bool = True) -> bool:
        """Arrête une lecture. ``cooldown=False`` (aperçu fermé) : la chaîne peut être relancée tout de suite."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None:
                return False
            if cooldown:
                self._cooldown[session.key] = time.monotonic() + COOLDOWN_SECONDS
        session.cancel()
        if session.virtual:
            self._record(session)
        return True

    def list(self) -> list[dict]:
        with self._lock:
            self._expire()
            return [s.as_dict() for s in sorted(self._sessions.values(), key=lambda s: s.started)]

    def busy(self, idle_seconds: float = 8) -> int:
        """Lectures réellement actives (une session HLS sans requête depuis ``idle_seconds`` ne compte pas :
        aperçu fermé dans le navigateur, lecteur arrêté)."""
        with self._lock:
            self._expire()
            now = time.monotonic()
            return sum(1 for s in self._sessions.values() if not s.virtual or now - s.last_activity < idle_seconds)

    def count(self, provider: str | None = None) -> int:
        with self._lock:
            self._expire()
            return sum(1 for s in self._sessions.values() if provider is None or s.provider == provider)
