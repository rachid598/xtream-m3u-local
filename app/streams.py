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
import queue
import socket
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
    """Limite atteinte sans lecture que l'on puisse couper (reprise désactivée, appareil prioritaire ou quota réservé).

    ``args[0]`` = limite ; ``holder`` = lecture qui garde la connexion (si un appareil prioritaire la tient) ;
    ``reserved_profile`` = espace protégé par une réservation de connexions, si applicable.
    """

    def __init__(self, limit: int, holder: "StreamSession | None" = None, *, reserved_profile: str = ""):
        super().__init__(limit)
        self.holder = holder
        self.reserved_profile = str(reserved_profile or "")


class EvictedRecently(Exception):
    """Cet appareil vient d'être coupé pour ce flux : il ne peut pas le reprendre tout de suite."""


class StreamSession:
    _ids = itertools.count(1)
    _vids = itertools.count(1)

    def __init__(self, *, kind: str, item_id: str, client: str, provider: str, label: str, virtual: bool,
                 device: str = "", priority: str = "normal", device_id: str = "", consumer_profile: str = ""):
        self.id = next(self._ids)
        self.device_id = device_id      # identifiant de l'appareil (fiche « Appareils » : en cours de lecture)
        self.kind = kind
        self.item_id = item_id
        self.client = client
        self.provider = str(provider)
        self.consumer_profile = str(consumer_profile or provider)
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
        self._vlock = threading.Lock()
        self._viewers: dict[int, dict] = {}

    @property
    def key(self) -> tuple[str, str, str, str]:
        # Adresse + appareil : deux TV derrière la même adresse (box, reverse proxy) restent distinctes.
        return (self.client, self.device, self.kind, self.item_id)

    def uses_profile(self, profile: str) -> bool:
        """Indique si la lecture (ou l'un de ses écrans partagés) appartient à l'espace ``profile``."""
        target = str(profile or "")
        if not target:
            return False
        with self._vlock:
            if self._viewers:
                return any(str(v.get("consumer_profile") or self.provider) == target for v in self._viewers.values())
        return (self.consumer_profile or self.provider) == target

    def add_viewer(self, *, client: str, device: str = "", priority: str = "normal", device_id: str = "",
                   consumer_profile: str = "") -> int:
        vid = next(self._vids)
        prio = priority if priority in PRIORITY_RANK else "normal"
        cprof = str(consumer_profile or self.consumer_profile or self.provider)
        with self._vlock:
            self._viewers[vid] = {"client": client, "device": device, "priority": prio,
                                  "device_id": device_id, "consumer_profile": cprof}
            self._sync_viewers()
        return vid

    def remove_viewer(self, vid: int) -> int:
        with self._vlock:
            self._viewers.pop(vid, None)
            if self._viewers:
                self._sync_viewers()
            return len(self._viewers)

    def _sync_viewers(self) -> None:
        viewers = list(self._viewers.values())
        if not viewers:
            return
        self.client = viewers[0]["client"]
        self.consumer_profile = str(viewers[0].get("consumer_profile") or self.provider)
        if len(viewers) == 1:
            self.device = viewers[0]["device"]
        else:
            labels = list(dict.fromkeys(v["device"] or v["client"] for v in viewers))
            self.device = " + ".join(labels)
        best = min(viewers, key=lambda v: PRIORITY_RANK.get(v["priority"], 1))
        self.priority = best["priority"]
        self.device_id = next((v["device_id"] for v in viewers if v["device_id"]), "")

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
        with self._vlock:
            viewers = list(self._viewers.values())
        viewer_count = max(1, len(viewers))
        devices = list(dict.fromkeys(v["device"] for v in viewers if v["device"])) if viewers else (
            [self.device] if self.device else [])
        device_ids = list(dict.fromkeys(v["device_id"] for v in viewers if v["device_id"])) if viewers else (
            [self.device_id] if self.device_id else [])
        return {
            "id": self.id, "kind": self.kind, "item_id": self.item_id, "label": self.label,
            "client": self.client, "provider": self.provider, "consumer_profile": self.consumer_profile,
            "started_at": int(self.started),
            "seconds": int(time.time() - self.started), "bytes": self.bytes,
            "device": self.device, "priority": self.priority, "switches": self.switches,
            "device_id": self.device_id, "channel": self.channel,
            "viewers": viewer_count, "devices": devices, "device_ids": device_ids,
            "idle": int(time.monotonic() - self.last_activity) if self.virtual else 0,
        }


class VersionHealth:
    """Mémoire courte de stabilité des versions de secours (quarantaine temporaire 30 min – 2 h max).

    Évite de subir 10 coupures de suite sur une source temporairement instable, tout en garantissant
    que la meilleure qualité (HD/FHD) repasse automatiquement en tête dès la fin de la quarantaine.
    """
    WINDOW = 15 * 60            # fenêtre d'observation des échecs rapprochés
    MIN_FAILS = 2               # 2 échecs dans la fenêtre → mise en quarantaine courte
    BASE_QUARANTINE = 30 * 60   # 30 min la première fois
    MAX_QUARANTINE = 2 * 3600   # 2 h maximum (jamais bloqué définitivement)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: dict[str, list[float]] = {}
        self._until: dict[str, float] = {}
        self._level: dict[str, int] = {}

    def record_failure(self, stream_id: str, *, now: float | None = None) -> int:
        sid = str(stream_id)
        now = time.monotonic() if now is None else now
        with self._lock:
            recent = [t for t in self._fails.get(sid, ()) if now - t <= self.WINDOW]
            recent.append(now)
            self._fails[sid] = recent[-8:]
            if len(recent) >= self.MIN_FAILS:
                lvl = min(2, self._level.get(sid, 0))
                duration = min(self.MAX_QUARANTINE, self.BASE_QUARANTINE * (2 ** lvl))
                self._until[sid] = now + duration
                self._level[sid] = lvl + 1
                return int(duration)
            return 0

    def record_success(self, stream_id: str) -> None:
        sid = str(stream_id)
        with self._lock:
            self._fails.pop(sid, None)
            self._until.pop(sid, None)
            self._level.pop(sid, None)

    def quarantined_seconds(self, stream_id: str, *, now: float | None = None) -> int:
        sid = str(stream_id)
        now = time.monotonic() if now is None else now
        with self._lock:
            until = self._until.get(sid, 0.0)
            if until <= now:
                self._until.pop(sid, None)
                return 0
            return int(until - now)

    def reorder(self, candidates: list[str], *, now: float | None = None) -> list[str]:
        """Place les versions saines avant celles en quarantaine tout en gardant l'ordre de qualité."""
        if len(candidates) <= 1:
            return list(candidates)
        now = time.monotonic() if now is None else now
        healthy, cooled = [], []
        for sid in candidates:
            if self.quarantined_seconds(sid, now=now) > 0:
                cooled.append(sid)
            else:
                healthy.append(sid)
        return healthy + cooled

    def reset(self, stream_id: str | None = None) -> None:
        with self._lock:
            if stream_id:
                sid = str(stream_id)
                self._fails.pop(sid, None)
                self._until.pop(sid, None)
                self._level.pop(sid, None)
            else:
                self._fails.clear()
                self._until.clear()
                self._level.clear()

    def snapshot(self, *, now: float | None = None) -> dict[str, int]:
        now = time.monotonic() if now is None else now
        with self._lock:
            out: dict[str, int] = {}
            for sid, until in list(self._until.items()):
                if until > now:
                    out[sid] = int(until - now)
                else:
                    self._until.pop(sid, None)
            return out


class StreamManager:
    def __init__(self, limit_for: Callable[[str], int], *, takeover: bool = True, history_path: str | None = None,
                 reservation_for: Callable[[str], tuple[str, int] | None] | None = None):
        self._limit_for = limit_for
        self._reservation_for = reservation_for
        self.takeover = takeover
        self.health = VersionHealth()
        self._lock = threading.Lock()
        self._sessions: dict[int, StreamSession] = {}
        self._cooldown: dict[tuple[str, str, str, str], float] = {}
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
                 "bytes": session.bytes, "provider": str(session.provider),
                 "profile": str(session.consumer_profile or session.provider)}
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

    def clear_history(self, provider: str | None = None, *, device: str | None = None) -> None:
        """Vide l'historique (tout, ou filtré par abonnement et/ou personne/appareil)."""
        def _matches(entry: dict) -> bool:
            if provider and str(entry.get("provider") or "1") != provider:
                return False
            if device:
                dev = str(entry.get("device") or "").strip()
                key = dev if dev else (f"ip:{entry.get('client')}" if entry.get("client") else "unknown")
                if key != device:
                    return False
            return True

        with self._history_lock:
            if provider or device:
                kept = [e for e in self._history if not _matches(e)]
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

    def reservation(self, provider: str = "1") -> tuple[str, int] | None:
        """Retourne ``(espace_bénéficiaire, connexions_garanties)`` si cet abonnement a une réservation."""
        fn = self._reservation_for
        if fn is None:
            from . import settings
            fn = settings.reservation_for
        try:
            res = fn(str(provider))
        except Exception:
            return None
        if not res:
            return None
        prof, count = str(res[0] or ""), int(res[1] or 0)
        return (prof, count) if prof and count > 0 else None

    # ------------------------------------------------------------- public
    def acquire(self, *, kind: str, item_id: str, client: str, provider: str = "1", label: str = "",
                virtual: bool = False, device: str = "", priority: str = "normal", device_id: str = "",
                consumer_profile: str = "") -> StreamSession:
        """Réserve une connexion fournisseur pour une lecture ; coupe une autre lecture si nécessaire."""
        provider = str(provider)
        caller = str(consumer_profile or provider)
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
                res = self.reservation(provider)
                res_profile, res_count = (res[0], min(res[1], limit)) if res else ("", 0)

                def _evict(victim: StreamSession) -> None:
                    active.remove(victim)
                    victim.cancel()
                    del self._sessions[victim.id]
                    evicted.append(victim)
                    if (victim.client, victim.device) != (client, device):
                        # L'autre appareil va réessayer tout seul : on l'en empêche quelques secondes.
                        self._cooldown[victim.key] = time.monotonic() + COOLDOWN_SECONDS
                    LOG.info("Limite de %d connexion(s) : arrêt de « %s » (%s) pour « %s »",
                             limit, victim.label or victim.item_id, victim.client, label or item_id)

                if res_count > 0:
                    shared_cap = max(0, limit - res_count)
                    is_owner = (caller == res_profile)
                    if not is_owner:
                        # Autre espace (secours / mélange) : plafonné à shared_cap, ne touche jamais aux
                        # connexions réservées de res_profile (peut seulement reprendre un emprunt excédentaire).
                        if shared_cap == 0:
                            owner_now = [s for s in active if s.uses_profile(res_profile)]
                            raise LimitReached(0, owner_now[0] if owner_now else None, reserved_profile=res_profile)
                        while True:
                            owner_now = [s for s in active if s.uses_profile(res_profile)]
                            other_now = [s for s in active if not s.uses_profile(res_profile)]
                            if len(other_now) < shared_cap and len(active) < limit:
                                break
                            same_client = [s for s in other_now if s.client == client and s.device == device]
                            if same_client:
                                _evict(same_client[0])
                                continue
                            if len(other_now) >= shared_cap:
                                if not self.takeover:
                                    raise LimitReached(shared_cap, reserved_profile=res_profile)
                                others = sorted((s for s in other_now if PRIORITY_RANK.get(s.priority, 1) >= rank),
                                                key=lambda s: (-PRIORITY_RANK.get(s.priority, 1), s.started))
                                if not others:
                                    raise LimitReached(shared_cap, other_now[0], reserved_profile=res_profile)
                                _evict(others[0])
                                continue
                            # len(other_now) < shared_cap et len(active) >= limit ⇒ res_profile emprunte une
                            # connexion du pool de secours (Option B) : le secours la reprend en protégeant la 1ʳᵉ.
                            if not self.takeover:
                                raise LimitReached(limit, owner_now[0] if owner_now else None,
                                                   reserved_profile=res_profile)
                            borrowed = sorted(owner_now, key=lambda s: (-PRIORITY_RANK.get(s.priority, 1), -s.started))
                            _evict(borrowed[0])
                    else:
                        # Espace bénéficiaire (Option B : res_count garanties, peut emprunter les places libres
                        # du secours, sans jamais couper une lecture du secours dans son quota).
                        while len(active) >= limit:
                            owner_now = [s for s in active if s.uses_profile(res_profile)]
                            other_now = [s for s in active if not s.uses_profile(res_profile)]
                            same_client = [s for s in owner_now if s.client == client and s.device == device]
                            if same_client:
                                _evict(same_client[0])
                                continue
                            if len(owner_now) < res_count and other_now:
                                # Sa connexion garantie est encore occupée par le secours (réglage modifié à chaud).
                                victims = sorted(other_now, key=lambda s: (-PRIORITY_RANK.get(s.priority, 1), s.started))
                                _evict(victims[0])
                                continue
                            if not self.takeover:
                                raise LimitReached(limit)
                            others = sorted((s for s in owner_now if PRIORITY_RANK.get(s.priority, 1) >= rank),
                                            key=lambda s: (-PRIORITY_RANK.get(s.priority, 1), s.started))
                            if not others:
                                raise LimitReached(limit, owner_now[0] if owner_now else active[0])
                            _evict(others[0])
                else:
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
                        _evict((same_client or others)[0])
            session = StreamSession(kind=kind, item_id=item_id, client=client, provider=provider,
                                    label=label, virtual=virtual, device=device, priority=priority,
                                    device_id=device_id, consumer_profile=caller)
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

        Retourne ``False`` si ce fournisseur n'a plus de place (ou si la place est réservée à un autre espace)."""
        provider = str(provider)
        with self._lock:
            self._expire()
            limit = self.limit(provider)
            active = [s for s in self._sessions.values() if s.provider == provider and s is not session]
            if limit and len(active) >= limit:
                return False
            res = self.reservation(provider)
            if limit and res:
                res_profile, res_count = res[0], min(res[1], limit)
                if res_count > 0 and not session.uses_profile(res_profile):
                    shared_cap = max(0, limit - res_count)
                    other_used = sum(1 for s in active if not s.uses_profile(res_profile))
                    if other_used >= shared_cap:
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

    def stop_device(self, device_id: str) -> int:
        """Coupe immédiatement toutes les lectures en cours d'un appareil (p. ex. mis en pause)."""
        if not device_id:
            return 0
        with self._lock:
            victims = [s for s in self._sessions.values() if s.device_id == device_id]
            for s in victims:
                self._sessions.pop(s.id, None)
        for s in victims:
            s.cancel()
            if s.virtual:
                self._record(s)
        return len(victims)

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

    def in_cooldown(self, client: str, device: str, kind: str, item_id: str) -> bool:
        with self._lock:
            self._expire()
            return (client, device, kind, item_id) in self._cooldown


def _ts_align(chunk: bytes) -> bytes:
    """Aligne un bloc sur une frontière de paquet MPEG-TS (0x47 tous les 188 octets) si détectée."""
    if len(chunk) >= 376:
        limit = min(188, len(chunk) - 188)
        for idx in range(limit):
            if chunk[idx] == 0x47 and chunk[idx + 188] == 0x47:
                return chunk[idx:]
    return b""


class LiveChannelPump:
    """Pompe partagée pour un flux direct MPEG-TS : une seule connexion fournisseur sert plusieurs écrans."""

    def __init__(self, sid: str, upstream, session: StreamSession, streams: StreamManager, hub: "LiveHub", *,
                 stall_timeout: float | None = None, slow_ratio: float = 0.0,
                 warmup: float = 25, slow_windows_limit: int = 3):
        self.sid = sid
        self.upstream = upstream
        self.session = session
        self.streams = streams
        self.hub = hub
        self._stall = stall_timeout
        self._slow_ratio = slow_ratio
        self._warmup = warmup
        self._slow_windows_limit = slow_windows_limit
        self.status = getattr(upstream, "status", 200)
        up_headers = getattr(upstream, "headers", None)
        self.headers = {
            k: up_headers.get(k)
            for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag")
            if up_headers and up_headers.get(k)
        }
        self._lock = threading.Lock()
        self._viewers: dict[int, dict] = {}
        self._accepting = True
        self._owns_session = False
        self._released = False
        self._started = False
        self.outcome = ""
        self._last_sync_chunk = b""
        session.attach(self.force_close)

    @property
    def accepting(self) -> bool:
        return self._accepting and not self.session.cancelled.is_set()

    def subscribe(self, *, client: str, device: str = "", priority: str = "normal",
                  device_id: str = "", primary: bool = False, consumer_profile: str = "") -> dict | None:
        with self._lock:
            if not self._accepting or self.session.cancelled.is_set():
                return None
            vid = self.session.add_viewer(client=client, device=device, priority=priority,
                                          device_id=device_id, consumer_profile=consumer_profile)
            q: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
            if not primary and self._last_sync_chunk:
                q.put_nowait(self._last_sync_chunk)
            entry = {"vid": vid, "q": q, "stop": threading.Event(), "done": threading.Event(),
                     "client": client, "device": device, "primary": primary}
            self._viewers[vid] = entry
            return entry

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._run, name=f"livehub-{self.sid}", daemon=True).start()

    def force_close(self) -> None:
        self._accepting = False
        self.hub._remove(self)
        try:
            self.upstream.close()
        except Exception:
            pass
        with self._lock:
            queues = [v["q"] for v in self._viewers.values()]
        for q in queues:
            _put_sentinel(q)

    def _finish(self, outcome: str) -> None:
        with self._lock:
            self._accepting = False
            if not self.outcome:
                self.outcome = outcome
            queues = [v["q"] for v in self._viewers.values()]
            release_now = not self._viewers and self._owns_session and not self._released
            if release_now:
                self._released = True
        self.hub._remove(self)
        try:
            self.upstream.close()
        except Exception:
            pass
        for q in queues:
            _put_sentinel(q)
        if release_now:
            self.streams.release(self.session)

    def unsubscribe(self, entry: dict) -> bool:
        with self._lock:
            vid = entry["vid"]
            self._viewers.pop(vid, None)
            self.session.remove_viewer(vid)
            entry["done"].set()
            remaining = len(self._viewers)
            transferred = False
            if entry["primary"] and remaining > 0 and self._accepting and not self.session.cancelled.is_set():
                self._owns_session = True
                transferred = True
            close_now = remaining == 0
            release_now = remaining == 0 and self._owns_session and not self._released
            if release_now:
                self._released = True
            if close_now:
                self._accepting = False
        if close_now:
            self.hub._remove(self)
            try:
                self.upstream.close()
            except Exception:
                pass
        if release_now:
            self.streams.release(self.session)
        return transferred

    def _run(self) -> None:
        if self._stall is not None:
            try:
                self.upstream.fp.raw._sock.settimeout(self._stall)
            except AttributeError:
                pass
        reader = getattr(self.upstream, "read1", self.upstream.read)
        window, window_start, slow_windows = 0, time.monotonic(), 0
        rates: list[float] = []
        started = window_start
        outcome = "ended"
        try:
            while not self.session.cancelled.is_set():
                with self._lock:
                    if not self._viewers and self._started:
                        outcome = "client"
                        break
                try:
                    chunk = reader(64 * 1024)
                except (socket.timeout, TimeoutError):
                    outcome = "cancelled" if self.session.cancelled.is_set() else "stalled"
                    break
                except Exception:
                    outcome = "cancelled" if self.session.cancelled.is_set() else "failed"
                    break
                if not chunk:
                    outcome = "ended"
                    break
                aligned = _ts_align(chunk)
                with self._lock:
                    if aligned:
                        self._last_sync_chunk = aligned
                    queues = [v["q"] for v in self._viewers.values()]
                if not queues:
                    outcome = "client"
                    break
                for q in queues:
                    try:
                        q.put_nowait(chunk)
                    except queue.Full:
                        try:
                            q.get_nowait()
                            q.put_nowait(chunk)
                        except queue.Empty:
                            pass
                self.session.touch(len(chunk))
                window += len(chunk)
                now = time.monotonic()
                if self._slow_ratio and now - window_start >= 10:
                    rate = window / (now - window_start)
                    if now - started > self._warmup:
                        baseline = sorted(rates)[len(rates) // 2] if len(rates) >= 2 else 0.0
                        slow = bool(baseline and rate < baseline * self._slow_ratio)
                        slow_windows = slow_windows + 1 if slow else 0
                        if slow_windows >= self._slow_windows_limit:
                            outcome = "slow"
                            break
                        if not slow:
                            rates = (rates + [rate])[-6:]
                    window, window_start = 0, now
            else:
                outcome = "cancelled"
        finally:
            self._finish(outcome)

    def stream_to(self, wfile, entry: dict, *, on_start: Callable[["LiveChannelPump"], None] | None = None,
                  return_after: float = 0.0) -> tuple[str, bool]:
        if on_start is not None:
            on_start(self)
        self.start()
        viewer_started = time.monotonic()
        outcome = ""
        try:
            while not entry["stop"].is_set() and not self.session.cancelled.is_set():
                if return_after and time.monotonic() - viewer_started >= return_after:
                    outcome = "best"
                    break
                try:
                    chunk = entry["q"].get(timeout=0.25)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                try:
                    wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, socket.timeout, OSError):
                    outcome = "client"
                    break
            if not outcome:
                if self.session.cancelled.is_set():
                    outcome = "cancelled"
                elif entry["stop"].is_set():
                    outcome = "client"
                else:
                    outcome = self.outcome or "ended"
        finally:
            transferred = self.unsubscribe(entry)
        return outcome, transferred


def _put_sentinel(q: queue.Queue) -> None:
    try:
        q.put_nowait(None)
    except queue.Full:
        try:
            q.get_nowait()
            q.put_nowait(None)
        except queue.Empty:
            pass


class LiveHub:
    """Registre des flux directs MPEG-TS actifs : partage automatique entre écrans locaux."""

    def __init__(self, streams: StreamManager):
        self.streams = streams
        self._lock = threading.Lock()
        self._pumps: dict[str, LiveChannelPump] = {}

    def _remove(self, pump: LiveChannelPump) -> None:
        with self._lock:
            if self._pumps.get(pump.sid) is pump:
                del self._pumps[pump.sid]

    def detach_viewer(self, client: str, device: str, *, except_sid: str = "") -> None:
        """Quand un appareil zappe, le retire proprement d'un éventuel flux partagé précédent."""
        targets: list[tuple[LiveChannelPump, dict, bool]] = []
        with self._lock:
            pumps = [p for sid, p in self._pumps.items() if sid != except_sid]
        for pump in pumps:
            with pump._lock:
                matched = [v for v in pump._viewers.values() if v["client"] == client and v["device"] == device]
                others_left = len(pump._viewers) - len(matched)
            for entry in matched:
                entry["stop"].set()
                _put_sentinel(entry["q"])
                targets.append((pump, entry, others_left == 0))
        for pump, entry, sole_viewer in targets:
            entry["done"].wait(1.0)
            if sole_viewer:
                pump.session.closed.wait(1.5)

    def try_join(self, sid: str, *, client: str, device: str = "", priority: str = "normal",
                 device_id: str = "", consumer_profile: str = "") -> tuple[LiveChannelPump, dict] | None:
        """Rejoint un flux direct déjà ouvert pour ``sid`` sans consommer de connexion supplémentaire."""
        if self.streams.in_cooldown(client, device, "live", sid):
            return None
        with self._lock:
            pump = self._pumps.get(sid)
            if pump is None or not pump.accepting:
                return None
            entry = pump.subscribe(client=client, device=device, priority=priority, device_id=device_id,
                                   primary=False, consumer_profile=consumer_profile)
            if entry is None:
                return None
        LOG.info("Flux direct partagé sur %s : « %s » rejoint la connexion en cours (0 connexion supplémentaire)",
                 sid, device or client)
        return pump, entry

    def register(self, sid: str, upstream, session: StreamSession, *,
                 client: str, device: str = "", priority: str = "normal", device_id: str = "",
                 consumer_profile: str = "",
                 stall_timeout: float | None = None, slow_ratio: float = 0.0,
                 warmup: float = 25, slow_windows_limit: int = 3) -> tuple[LiveChannelPump, dict]:
        pump = LiveChannelPump(
            sid, upstream, session, self.streams, self,
            stall_timeout=stall_timeout, slow_ratio=slow_ratio,
            warmup=warmup, slow_windows_limit=slow_windows_limit,
        )
        entry = pump.subscribe(client=client, device=device, priority=priority, device_id=device_id,
                               primary=True, consumer_profile=consumer_profile or session.consumer_profile)
        with self._lock:
            self._pumps[sid] = pump
        return pump, entry

