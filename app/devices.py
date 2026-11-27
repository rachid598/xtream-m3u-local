"""Appareils (TV, téléphones…) : un code d'accès par appareil et des identifiants « Xtream » locaux.

Fichier ``DATA_DIR/devices.json`` (droits 0600 : il contient les codes d'accès locaux) ::

    {"require_token": false,
     "devices": [{"id": "a1b2c3d4", "name": "TV du salon", "username": "tv-du-salon",
                  "password": "k7m2p9x4rt", "playlist": "salon", "created_at": 1794762000}]}

- Un code d'appareil est accepté partout où ``?token=`` l'est (playlists, flux, guide).
- ``require_token`` : si vrai, les URL du port 7777 exigent un code (d'appareil ou ``ACCESS_TOKEN``).
- ``username`` / ``password`` servent aussi d'identifiants pour le serveur Xtream local
  (``player_api.php``) : l'appareil ne voit que sa ``playlist`` (ou tout le catalogue si vide).
- ``priority`` (high | normal | low) : avec une limite de connexions, un appareil n'en coupe jamais un
  plus prioritaire ; ``ip`` (facultatif) : adresse de la TV, pour la reconnaître même sans code
  (playlist ouverte sans ``?token=``) — sinon la dernière adresse vue avec son code est utilisée.
- Les identifiants du fournisseur ne sont jamais stockés ici.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,59}$")
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # sans caractères ambigus (saisie à la télécommande)
MAX_DEVICES = 50
PRIORITIES = ("high", "normal", "low")


class InvalidDevice(ValueError):
    pass


def new_password(length: int = 10) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def slugify(name: str) -> str:
    import unicodedata
    text = unicodedata.normalize("NFD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:32]
    return text or "appareil"


class DeviceStore:
    def __init__(self, data_dir: str):
        self._path = Path(data_dir) / "devices.json"
        self._lock = threading.RLock()
        self._cache: tuple[float, dict] | None = None
        self.last_seen: dict[str, float] = {}
        self.last_ip: dict[str, str] = {}

    # ------------------------------------------------------------ lecture
    def _load(self) -> dict:
        with self._lock:
            try:
                mtime = self._path.stat().st_mtime_ns
            except OSError:
                return {"require_token": False, "devices": []}
            if self._cache and self._cache[0] == mtime:
                return self._cache[1]
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
                    raise ValueError
            except (OSError, ValueError):
                raise RuntimeError("devices.json illisible") from None
            data = {"require_token": bool(data.get("require_token")),
                    "devices": [{"profile": "", "paused": False, **d} for d in data["devices"]
                                if isinstance(d, dict) and d.get("password")]}
            self._cache = (mtime, data)
            return data

    def list(self) -> list[dict]:
        return [dict(d) for d in self._load()["devices"]]

    @property
    def require_token(self) -> bool:
        try:
            return self._load()["require_token"]
        except RuntimeError:
            return True  # fichier corrompu : on reste prudent

    def find_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        try:
            devices = self._load()["devices"]
        except RuntimeError:
            return None
        for device in devices:
            if hmac.compare_digest(token.encode(), device["password"].encode()):
                self.last_seen[device["id"]] = time.time()
                return device
        return None

    def seen_from(self, device: dict, ip: str) -> None:
        """Mémorise l'adresse d'où l'appareil s'est présenté avec son code."""
        if device and ip:
            self.last_ip[device["id"]] = ip

    def by_ip(self, ip: str) -> dict | None:
        """Appareil reconnu à son adresse : adresse saisie d'abord, puis dernière adresse vue avec son code."""
        if not ip:
            return None
        try:
            devices = self._load()["devices"]
        except RuntimeError:
            return None
        return (next((d for d in devices if d.get("ip") == ip), None)
                or next((d for d in devices if self.last_ip.get(d["id"]) == ip), None))

    def authenticate(self, username: str, password: str) -> dict | None:
        device = self.find_by_token(password)
        if device and hmac.compare_digest(username.encode(), device["username"].encode()):
            return device
        return None

    # ------------------------------------------------------------ écriture
    def _write(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".devices-", suffix=".tmp", dir=str(self._path.parent))
        except OSError as exc:
            raise RuntimeError("Dossier des données non inscriptible") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            raise RuntimeError("Échec de l'écriture de devices.json") from exc
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._cache = None

    @staticmethod
    def _check_name(name: object) -> str:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(c in name for c in "\r\n\t\x00"):
            raise InvalidDevice("Nom d'appareil obligatoire (1 à 60 caractères)")
        return name.strip()

    @staticmethod
    def _check_playlist(playlist: object) -> str:
        if playlist in (None, ""):
            return ""
        if not isinstance(playlist, str) or not SLUG_RE.fullmatch(playlist):
            raise InvalidDevice("Playlist invalide")
        return playlist

    @staticmethod
    def _check_priority(priority: object) -> str:
        if priority in (None, ""):
            return "normal"
        if priority not in PRIORITIES:
            raise InvalidDevice("Priorité : high, normal ou low")
        return priority

    @staticmethod
    def _check_links(value: object) -> str:
        """Adresses affichées pour l'appareil : réseau local (« lan ») ou adresse publique (« external »)."""
        if value in (None, ""):
            return "lan"
        if value in ("lan", "external"):
            return value
        # Adresse publique précise (ex. https://tv2.mondomaine.fr) : tunnel ou reverse proxy.
        if isinstance(value, str) and re.fullmatch(r"https://[a-z0-9.-]{4,253}", value):
            return value
        raise InvalidDevice("Liens : lan, external ou une adresse https://…")

    @classmethod
    def _safe_links(cls, value: object) -> str:
        try:
            return cls._check_links(value)
        except InvalidDevice:
            return "lan"

    @staticmethod
    def _check_ip(ip: object) -> str:
        if ip in (None, ""):
            return ""
        try:
            return str(ipaddress.ip_address(str(ip).strip()))
        except ValueError:
            raise InvalidDevice("Adresse IP de l'appareil invalide (ex. 192.168.0.42)") from None

    @staticmethod
    def _check_profile(profile: object) -> str:
        if profile in (None, ""):
            return ""
        text = str(profile).strip()
        if text.isdigit() and 1 <= int(text) <= 9:
            return text
        raise InvalidDevice("Espace (profil) d'appareil invalide")

    def create(self, name: object, playlist: object = "", username: object = None,
               priority: object = "normal", ip: object = "", links: object = "lan",
               profile: object = "") -> dict:
        with self._lock:
            data = self._load()
            devices = data["devices"]
            if len(devices) >= MAX_DEVICES:
                raise InvalidDevice(f"Maximum de {MAX_DEVICES} appareils")
            clean_name = self._check_name(name)
            base = username if isinstance(username, str) and username else slugify(clean_name)
            if not USERNAME_RE.fullmatch(base):
                raise InvalidDevice("Identifiant : minuscules, chiffres, « . », « - » ou « _ » (32 caractères maximum)")
            taken = {d["username"] for d in devices}
            candidate, i = base, 2
            while candidate in taken:
                candidate = f"{base[:28]}-{i}"
                i += 1
            device = {"id": secrets.token_hex(4), "name": clean_name, "username": candidate,
                      "password": new_password(), "playlist": self._check_playlist(playlist),
                      "priority": self._check_priority(priority), "ip": self._check_ip(ip),
                      "links": self._check_links(links), "profile": self._check_profile(profile),
                      "paused": False, "created_at": int(time.time())}
            self._write({"require_token": data["require_token"], "devices": devices + [device]})
            return dict(device)

    def update(self, device_id: str, payload: dict) -> dict:
        with self._lock:
            data = self._load()
            devices = [dict(d) for d in data["devices"]]
            device = next((d for d in devices if d["id"] == device_id), None)
            if device is None:
                raise KeyError(device_id)
            if "name" in payload:
                device["name"] = self._check_name(payload["name"])
            if "playlist" in payload:
                device["playlist"] = self._check_playlist(payload["playlist"])
            if "priority" in payload:
                device["priority"] = self._check_priority(payload["priority"])
            if "ip" in payload:
                device["ip"] = self._check_ip(payload["ip"])
            if "links" in payload:
                device["links"] = self._check_links(payload["links"])
            if "profile" in payload:
                device["profile"] = self._check_profile(payload["profile"])
            if "paused" in payload:
                device["paused"] = bool(payload["paused"])
            if payload.get("regenerate"):
                device["password"] = new_password()
            self._write({"require_token": data["require_token"], "devices": devices})
            return dict(device)

    def delete(self, device_id: str) -> bool:
        with self._lock:
            data = self._load()
            remaining = [d for d in data["devices"] if d["id"] != device_id]
            if len(remaining) == len(data["devices"]):
                return False
            self._write({"require_token": data["require_token"], "devices": remaining})
            return True

    def set_require_token(self, value: bool) -> None:
        with self._lock:
            data = self._load()
            self._write({"require_token": bool(value), "devices": data["devices"]})

    def export(self) -> dict:
        return self._load()

    def restore(self, data: object, *, dry_run: bool = False) -> None:
        """Remplace les appareils par ceux d'une sauvegarde. ``dry_run`` : valider seulement, sans écrire."""
        if not isinstance(data, dict) or not isinstance(data.get("devices"), list):
            raise InvalidDevice("Sauvegarde des appareils invalide")
        devices = []
        seen_users, seen_codes, seen_ids = set(), set(), set()
        for raw in data["devices"][:MAX_DEVICES]:
            if not isinstance(raw, dict):
                continue
            name = self._check_name(raw.get("name"))
            username = raw.get("username")
            password = raw.get("password")
            if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
                raise InvalidDevice("Identifiant d'appareil invalide dans la sauvegarde")
            if not isinstance(password, str) or not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", password):
                raise InvalidDevice("Code d'appareil invalide dans la sauvegarde")
            if username in seen_users or password in seen_codes:
                raise InvalidDevice("Deux appareils avec le même identifiant ou le même code dans la sauvegarde")
            seen_users.add(username)
            seen_codes.add(password)
            # Identifiant interne : sert dans les adresses de l'interface (/api/devices/<id>).
            device_id = str(raw.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,16}", device_id) or device_id in seen_ids:
                device_id = secrets.token_hex(4)
            seen_ids.add(device_id)
            try:
                created = int(raw.get("created_at") or time.time())
            except (TypeError, ValueError):
                created = int(time.time())
            prof = raw.get("profile")
            clean_prof = str(prof).strip() if prof is not None else ""
            devices.append({"id": device_id, "name": name,
                            "username": username, "password": password,
                            "playlist": self._check_playlist(raw.get("playlist")),
                            "priority": self._check_priority(raw.get("priority")),
                            "ip": self._check_ip(raw.get("ip")),
                            "links": self._safe_links(raw.get("links")),
                            "profile": clean_prof if clean_prof.isdigit() and 1 <= int(clean_prof) <= 9 else "",
                            "paused": bool(raw.get("paused", False)),
                            "created_at": created})
        if dry_run:
            return
        with self._lock:
            self._write({"require_token": bool(data.get("require_token")), "devices": devices})
