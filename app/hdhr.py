"""Émulation d'un tuner HDHomeRun : Plex, Jellyfin et Emby voient une playlist comme une antenne TV.

Deux adresses, servies par le port des lecteurs (7777) :

- ``http://IP:7777/hdhr/<utilisateur>/<mot de passe>/`` : tuner d'un appareil (Jellyfin, Emby, Plex si
  l'adresse complète est acceptée) ;
- ``http://IP:7777/`` (``/discover.json`` …) : tuner de l'appareil dont l'adresse IP est celle du serveur
  Plex (champ « Adresse IP » de l'appareil) — Plex n'accepte souvent qu'une adresse ``IP:port``.
  Réseau local seulement.

Le tuner annonce autant de « tuners » que l'abonnement de la playlist autorise de connexions ; chaque
chaîne pointe vers ``/live/<utilisateur>/<mot de passe>/<id>.ts`` (droits de l'appareil, bascule
automatique). Guide : ``guide.xml`` = guide de l'appareil (``xmltv.php``).
"""
from __future__ import annotations

import hashlib
import re
from xml.sax.saxutils import escape

FILES = ("discover.json", "lineup.json", "lineup_status.json", "lineup.post", "device.xml", "guide.xml")
HDHR_RE = re.compile(r"^/hdhr/([A-Za-z0-9_.-]{1,64})/([A-Za-z0-9_-]{1,64})/(" +
                     "|".join(re.escape(f) for f in FILES) + r")?$")
ROOT_FILES = {"/discover.json", "/lineup.json", "/lineup_status.json", "/lineup.post", "/device.xml"}


def device_id(device: dict) -> str:
    """Identifiant HDHomeRun stable (8 chiffres hexadécimaux) tiré de l'appareil."""
    return hashlib.sha256(f"xtream-m3u-local:{device['id']}".encode()).hexdigest()[:8].upper()


def tuner_count(emulator, device: dict) -> int:
    """Tuners annoncés = connexions autorisées par l'abonnement de la playlist (1 à 10)."""
    profile = str(emulator.saved_for(device).get("provider") or "1")
    streams = getattr(emulator, "streams", None)
    limit = streams.limit(profile) if streams is not None else 0
    return max(1, min(10, int(limit or 1)))


def discover(emulator, device: dict, base: str) -> dict:
    return {
        "FriendlyName": f"Xtream M3U Local ({device.get('name') or device['username']})"[:64],
        "Manufacturer": "Silicondust",
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomeruntc_atsc",
        "FirmwareVersion": "20200101",
        "DeviceID": device_id(device),
        "DeviceAuth": "xtreamm3ulocal",
        "BaseURL": base,
        "LineupURL": f"{base}/lineup.json",
        "TunerCount": tuner_count(emulator, device),
    }


def lineup(emulator, device: dict, server_base: str) -> list[dict]:
    """Chaînes de la playlist de l'appareil, dans son ordre et avec ses numéros."""
    user, password = device["username"], device["password"]
    result = []
    for item in emulator.items(device, "live", base=server_base):
        result.append({
            "GuideNumber": str(item["num"]),
            "GuideName": item["name"],
            "URL": f"{server_base}/live/{user}/{password}/{item['stream_id']}.ts",
            "HD": 1,
        })
    return result


LINEUP_STATUS = {"ScanInProgress": 0, "ScanPossible": 1, "Source": "Cable", "SourceList": ["Cable"]}


def device_xml(emulator, device: dict, base: str) -> str:
    info = discover(emulator, device, base)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">\n'
        "  <specVersion><major>1</major><minor>0</minor></specVersion>\n"
        f"  <URLBase>{escape(base)}</URLBase>\n"
        "  <device>\n"
        "    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>\n"
        f"    <friendlyName>{escape(info['FriendlyName'])}</friendlyName>\n"
        "    <manufacturer>Silicondust</manufacturer>\n"
        f"    <modelName>{info['ModelNumber']}</modelName>\n"
        f"    <modelNumber>{info['ModelNumber']}</modelNumber>\n"
        f"    <serialNumber>{info['DeviceID']}</serialNumber>\n"
        f"    <UDN>uuid:{info['DeviceID'].lower()}-0000-4000-8000-{info['DeviceID'].lower()}0000</UDN>\n"
        "  </device>\n"
        "</root>\n"
    )
