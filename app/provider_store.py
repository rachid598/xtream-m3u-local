"""Abonnements Xtream ajoutés depuis l'interface Web : ``DATA_DIR/providers.json`` (droits 0600).

L'abonnement principal reste dans la configuration du conteneur (``XTREAM_URL``…), comme les
abonnements ``XTREAM_URL_2``… Ceux de l'interface s'y ajoutent avec un **numéro fixe** (2 à 9),
attribué à la création et jamais réutilisé tant qu'ils existent : il est inscrit dans les identifiants
des chaînes, des playlists et des appareils.

Les mots de passe ne sortent jamais par l'API Web (seulement « défini ») ; ils sont masqués dans les
journaux et réponses comme ceux de la configuration (:func:`credentials`).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

MAX_INDEX = 9
NAME_MAX = 40


class InvalidProvider(ValueError):
    pass


def _clean(value: object, label: str, *, limit: int = 200, required: bool = True) -> str:
    text = str(value if value is not None else "").strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise InvalidProvider(f"{label} : caractères invalides")
    if required and not text:
        raise InvalidProvider(f"{label} obligatoire")
    if len(text) > limit:
        raise InvalidProvider(f"{label} trop long")
    return text


class ProviderStore:
    def __init__(self, data_dir: str) -> None:
        self.path = Path(data_dir) / "providers.json"
        self._lock = threading.RLock()
        self._cache: tuple[object, list[dict]] | None = None   # (date du fichier, contenu) : lu une fois

    # --------------------------------------------------------------- lecture
    def list(self) -> list[dict]:
        """Abonnements de l'interface, **avec** leurs identifiants (usage interne uniquement)."""
        with self._lock:
            try:
                stat = self.path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                return []
            except OSError:
                raise RuntimeError("providers.json illisible") from None
            if self._cache is not None and self._cache[0] == stamp:
                return [dict(p) for p in self._cache[1]]
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise RuntimeError("providers.json illisible") from None
        result = []
        for raw in data.get("providers") or []:
            try:
                index = int(raw.get("index"))
            except (TypeError, ValueError):
                continue
            if 2 <= index <= MAX_INDEX and raw.get("url") and raw.get("username") and raw.get("password"):
                result.append({"index": index, "name": str(raw.get("name") or f"Abonnement {index}")[:NAME_MAX],
                               "url": str(raw["url"]), "username": str(raw["username"]),
                               "password": str(raw["password"]), "created_at": int(raw.get("created_at") or 0),
                               "user_agent": str(raw.get("user_agent") or "")[:200]})
        result.sort(key=lambda p: p["index"])
        with self._lock:
            self._cache = (stamp, result)
        return [dict(p) for p in result]

    def credentials(self) -> list[tuple[str, str]]:
        try:
            return [(p["username"], p["password"]) for p in self.list()]
        except RuntimeError:
            return []

    # --------------------------------------------------------------- écriture
    def _write(self, providers: list[dict]) -> None:
        self._cache = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"providers": providers}, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    def add(self, *, name: object, url: str, username: object, password: object, reserved: set[int],
            user_agent: object = "") -> dict:
        """Ajoute un abonnement au premier numéro libre (``reserved`` : numéros de la configuration)."""
        with self._lock:
            providers = self.list()
            used = set(reserved) | {p["index"] for p in providers}
            free = [i for i in range(2, MAX_INDEX + 1) if i not in used]
            if not free:
                raise InvalidProvider("9 abonnements au maximum")
            entry = {"index": free[0], "name": _clean(name, "Nom", limit=NAME_MAX),
                     "url": url, "username": _clean(username, "Utilisateur"),
                     "password": _clean(password, "Mot de passe"), "created_at": int(time.time()),
                     "user_agent": _clean(user_agent, "User agent", required=False)}
            self._write(providers + [entry])
            return entry

    def update(self, index: int, **fields) -> dict:
        with self._lock:
            providers = self.list()
            entry = next((p for p in providers if p["index"] == index), None)
            if entry is None:
                raise KeyError(index)
            if fields.get("name") is not None:
                entry["name"] = _clean(fields["name"], "Nom", limit=NAME_MAX)
            if fields.get("url"):
                entry["url"] = fields["url"]
            if fields.get("username"):
                entry["username"] = _clean(fields["username"], "Utilisateur")
            if fields.get("password"):  # vide : mot de passe inchangé
                entry["password"] = _clean(fields["password"], "Mot de passe")
            if fields.get("user_agent") is not None:  # vide : user agent par défaut (XTREAM_USER_AGENT)
                entry["user_agent"] = _clean(fields["user_agent"], "User agent", required=False)
            self._write(providers)
            return entry

    def delete(self, index: int) -> bool:
        with self._lock:
            providers = self.list()
            kept = [p for p in providers if p["index"] != index]
            if len(kept) == len(providers):
                return False
            self._write(kept)
            return True


def public(entry: dict) -> dict:
    """Vue sans secret pour l'interface : adresse réduite à l'hôte, utilisateur partiellement masqué."""
    host = re.sub(r"^https?://", "", entry["url"]).split("/")[0]
    user = entry["username"]
    # Adresse complète (pas un secret) pour pouvoir la modifier ; utilisateur masqué, mot de passe jamais.
    return {"index": entry["index"], "name": entry["name"], "host": host, "url": entry["url"],
            "username": (user[:2] + "•" * max(3, len(user) - 2)) if user else "", "password_set": True,
            "source": "interface", "created_at": entry.get("created_at"), "user_agent": entry.get("user_agent", "")}


_STORE: ProviderStore | None = None


def install(store: ProviderStore | None) -> None:
    global _STORE
    _STORE = store


def current() -> ProviderStore | None:
    return _STORE


def credentials() -> list[tuple[str, str]]:
    """Identifiants des abonnements de l'interface, pour les masquer partout."""
    return _STORE.credentials() if _STORE is not None else []
