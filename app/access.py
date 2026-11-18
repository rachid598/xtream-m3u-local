"""Accès distant : vraie adresse du client, local ou distant, essais de code limités.

- Une requête est **locale** si l'adresse du client est privée (réseau local, Docker) ou de bouclage.
  Tout le reste est **distant** : Internet, Tailscale (100.64.0.0/10), un tunnel.
- Derrière un reverse proxy (Traefik, Nginx Proxy Manager…), le serveur ne voit que l'adresse du proxy.
  Il ne lit ``X-Forwarded-For`` / ``X-Real-IP`` / ``CF-Connecting-IP`` que si le proxy est déclaré dans
  ``TRUSTED_PROXIES``. Une requête qui porte ces en-têtes depuis un proxy **non** déclaré est traitée
  comme distante : un proxy oublié dans la configuration ne rend jamais Internet « local ».
"""
from __future__ import annotations

import ipaddress
import threading
import time
from collections import deque

FORWARD_HEADERS = ("X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP", "Forwarded")
MAX_FAILURES = 10          # essais de code ratés…
FAILURE_WINDOW = 600       # … en 10 minutes …
BLOCK_SECONDS = 900        # … → adresse bloquée 15 minutes


def parse_networks(raw: str) -> tuple:
    """« 172.18.0.0/16, 10.0.0.5 » → réseaux. Lève ``ValueError`` sur une entrée invalide."""
    networks = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            networks.append(ipaddress.ip_network(part, strict=False))
    return tuple(networks)


def _ip(value: str):
    try:
        return ipaddress.ip_address((value or "").strip().strip("[]"))
    except ValueError:
        return None


# Réseau local au sens strict : RFC 1918, bouclage, lien local, IPv6 privé. Tailscale (100.64.0.0/10)
# et toute autre adresse sont « distantes ».
LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10"))


def is_local(value: str) -> bool:
    ip = _ip(value)
    if ip is None:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return _in(ip, LOCAL_NETWORKS)


def _in(ip, networks) -> bool:
    return ip is not None and any(ip.version == n.version and ip in n for n in networks)


def client_address(peer: str, headers, trusted: tuple) -> tuple[str, bool]:
    """``(adresse réelle du client, distant ?)``."""
    forwarded = [h for h in FORWARD_HEADERS if headers.get(h)]
    if not forwarded:
        return peer, not is_local(peer)
    if not _in(_ip(peer), trusted):
        return peer, True  # en-têtes de proxy depuis un proxy non déclaré : distant par sécurité
    chain = []
    for header in ("X-Forwarded-For",):
        for part in (headers.get(header) or "").split(","):
            if part.strip():
                chain.append(part.strip())
    for header in ("CF-Connecting-IP", "X-Real-IP"):
        if headers.get(header) and not chain:
            chain.append(headers.get(header).strip())
    # De droite à gauche : la première adresse qui n'est pas un de nos proxys est le vrai client.
    for candidate in reversed(chain):
        if _ip(candidate) is None:
            return peer, True
        if not _in(_ip(candidate), trusted):
            return candidate, not is_local(candidate)
    return peer, True


class FailureGuard:
    """Compte les essais de code ratés par adresse et bloque les adresses trop insistantes."""

    def __init__(self, max_failures: int = MAX_FAILURES, window: int = FAILURE_WINDOW, block: int = BLOCK_SECONDS):
        self.max_failures, self.window, self.block = max_failures, window, block
        self._lock = threading.Lock()
        self._failures: dict[str, deque] = {}
        self._blocked: dict[str, float] = {}

    def blocked_for(self, ip: str) -> int:
        with self._lock:
            until = self._blocked.get(ip, 0)
            if until <= time.monotonic():
                self._blocked.pop(ip, None)
                return 0
            return int(until - time.monotonic()) + 1

    def fail(self, ip: str) -> bool:
        """Enregistre un échec. Retourne ``True`` si l'adresse vient d'être bloquée."""
        now = time.monotonic()
        with self._lock:
            if len(self._failures) > 10_000:
                self._failures.clear()
            attempts = self._failures.setdefault(ip, deque())
            attempts.append(now)
            while attempts and attempts[0] < now - self.window:
                attempts.popleft()
            if len(attempts) >= self.max_failures:
                self._blocked[ip] = now + self.block
                attempts.clear()
                return True
        return False

    def blocked(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            return [{"ip": ip, "seconds": int(until - now) + 1} for ip, until in self._blocked.items() if until > now]
