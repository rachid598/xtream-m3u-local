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
import re
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


def is_local(value: str, extra: tuple = ()) -> bool:
    """Réseau local (``extra`` : réseaux ajoutés par ``LOCAL_NETWORKS``, ex. Tailscale 100.64.0.0/10)."""
    ip = _ip(value)
    if ip is None:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return _in(ip, LOCAL_NETWORKS) or _in(ip, extra)


def _in(ip, networks) -> bool:
    return ip is not None and any(ip.version == n.version and ip in n for n in networks)


def client_address(peer: str, headers, trusted: tuple, local: tuple = ()) -> tuple[str, bool]:
    """``(adresse réelle du client, distant ?)`` ; ``local`` : réseaux supplémentaires considérés locaux."""
    forwarded = [h for h in FORWARD_HEADERS if headers.get(h)]
    if not forwarded:
        return peer, not is_local(peer, local)
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
            return candidate, not is_local(candidate, local)
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


# ------------------------------------------------------------------ noms d'hôte
# Protection contre le « DNS rebinding » : un site web piégé, ouvert sur un ordinateur de la maison, fait
# pointer son propre nom vers l'adresse locale du serveur et lit ses réponses (codes d'appareils,
# sauvegarde, liens « mode=direct » avec les identifiants du fournisseur). Le navigateur envoie alors ce
# nom dans l'en-tête Host : seuls les noms d'un réseau local, les adresses IP et ALLOWED_HOSTS passent.
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal", ".localdomain", ".intranet", ".ts.net")


def host_name(host_header: str) -> str:
    """« Tower.local:7778 » → « tower.local » ; « [::1]:7778 » → « ::1 »."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def host_allowed(host_header: str, allowed: tuple = (), *, via_proxy: bool = False) -> bool:
    """Nom d'hôte acceptable pour une requête : IP, nom local, ``ALLOWED_HOSTS`` ou reverse proxy déclaré."""
    if via_proxy:
        return True  # reverse proxy de TRUSTED_PROXIES : c'est lui qui choisit le nom de domaine
    host = host_name(host_header)
    if not host:
        return True  # HTTP/1.0 sans Host : pas un navigateur
    if _ip(host) is not None or host == "localhost" or "." not in host:
        return True
    if host.endswith(LOCAL_SUFFIXES):
        return True
    return any(host == name or (name.startswith(".") and host.endswith(name)) for name in allowed)


def parse_hosts(raw: str) -> tuple:
    """``ALLOWED_HOSTS`` : « tv.exemple.fr, .mondomaine.fr » (un point devant = tous les sous-domaines)."""
    names = []
    for part in (raw or "").split(","):
        name = part.strip().lower().rstrip(".")
        if not name:
            continue
        if not re.fullmatch(r"\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*", name):
            raise ValueError(f"ALLOWED_HOSTS : nom invalide « {name} »")
        names.append(name)
    return tuple(names)
