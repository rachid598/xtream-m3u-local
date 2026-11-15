"""Faux fournisseur Xtream Codes pour les tests et la démo locale (aucun réseau externe)."""
from __future__ import annotations

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

USERNAME = "user"
PASSWORD = "pw"

CATEGORIES = [
    {"category_id": "1", "category_name": "Actualités", "parent_id": 0},
    {"category_id": "2", "category_name": 'Musique\n"HD"', "parent_id": 0},
    {"category_id": "10", "category_name": "Sport", "parent_id": 0},
    {"category_id": "11", "category_name": "Catégorie vide", "parent_id": 0},
    {"category_id": "12", "category_name": "Sport | Foot", "parent_id": 10},
]

STREAMS = [
    {"num": 1, "name": "France Info HD", "stream_type": "live", "stream_id": 42,
     "stream_icon": "https://logo.invalid/franceinfo.png", "epg_channel_id": "franceinfo.fr",
     "category_id": "1", "category_ids": [1],
     "direct_source": "http://cdn.invalid/live/user/pw/42.ts?username=user&password=pw"},
    {"num": 2, "name": 'Radio "TV"\nHACK', "stream_type": "live", "stream_id": "43",
     "stream_icon": "javascript:alert(1)", "epg_channel_id": None, "category_id": "2"},
    {"num": 5, "name": "Foot+", "stream_type": "live", "stream_id": 47, "stream_icon": "",
     "epg_channel_id": "foot.fr", "category_id": "12"},
    {"num": 3, "name": "Sport 1", "stream_type": "live", "stream_id": 44,
     "stream_icon": "", "epg_channel_id": "Sport1.fr", "category_id": "10", "category_ids": [10, 1]},
    {"num": 4, "name": "Chaîne orpheline", "stream_type": "live", "stream_id": 45,
     "stream_icon": None, "epg_channel_id": "", "category_id": None},
    {"num": "", "name": "Sans catégorie connue", "stream_type": "live", "stream_id": 46,
     "category_id": "99"},
    {"stream_id": "../../bad", "name": "Invalid"},
    "not a dict",
]

VOD_CATEGORIES = [
    {"category_id": "1", "category_name": "Films | Action", "parent_id": 0},
    {"category_id": "2", "category_name": "Films | Comédie", "parent_id": 0},
    {"category_id": "3", "category_name": "Films | Vide", "parent_id": 0},
]

VOD_STREAMS = [
    {"num": 1, "name": "Le Film d'action", "stream_type": "movie", "stream_id": 501,
     "stream_icon": "https://img.invalid/501.jpg", "rating": "7.4", "category_id": "1",
     "container_extension": "mkv", "added": "1700000000", "year": "2021"},
    {"num": 2, "name": "Comédie \"drôle\"", "stream_type": "movie", "stream_id": "502",
     "stream_icon": "", "rating": "", "category_id": "2", "container_extension": "mp4",
     "releaseDate": "2019-05-01"},
    {"num": 3, "name": "Sans extension", "stream_type": "movie", "stream_id": 503, "category_id": "1",
     "container_extension": "../../x"},
    {"stream_id": "abc", "name": "Invalide"},
]

SERIES_CATEGORIES = [
    {"category_id": "1", "category_name": "Séries | Drame", "parent_id": 0},
    {"category_id": "7", "category_name": "Séries | Animation", "parent_id": 0},
]

SERIES = [
    {"num": 1, "name": "Ma Série", "series_id": 900, "cover": "https://img.invalid/900.jpg",
     "category_id": "1", "releaseDate": "2020-01-01", "rating": "8"},
    {"num": 2, "name": "Dessin animé", "series_id": "901", "cover": "", "category_id": "7"},
    {"num": 3, "name": "Série en panne", "series_id": 902, "cover": "", "category_id": "7"},
]

SERIES_INFO = {
    "900": {"info": {"name": "Ma Série"}, "seasons": [], "episodes": {
        "2": [{"id": "9002", "episode_num": 1, "title": "Ma Série - S02E01", "container_extension": "mkv",
               "season": 2, "info": {"movie_image": "https://img.invalid/e9002.jpg"}}],
        "1": [{"id": "9001", "episode_num": 2, "title": "Le retour", "container_extension": "mp4", "season": 1},
              {"id": "9000", "episode_num": 1, "title": "Pilote", "container_extension": "mp4", "season": 1}],
    }},
    # Variante « liste » renvoyée par certains panneaux.
    "901": {"info": {}, "episodes": [[{"id": 9101, "episode_num": 1, "title": "Épisode 1",
                                       "container_extension": "avi", "season": 1}]]},
}

XMLTV = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE tv SYSTEM "xmltv.dtd">
<tv generator-info-name="fake">
  <channel id="franceinfo.fr"><display-name>France Info</display-name></channel>
  <channel id="sport1.fr"><display-name>Sport 1</display-name></channel>
  <channel id="other.fr"><display-name>Autre &amp; co</display-name></channel>
  <programme start="20261115180000 +0100" stop="20261115190000 +0100" channel="franceinfo.fr"><title>Journal</title></programme>
  <programme start="20261115180000 +0100" stop="20261115190000 +0100" channel="sport1.fr"><title>Match</title></programme>
  <programme start="20261115180000 +0100" stop="20261115190000 +0100" channel="other.fr"><title>Autre</title></programme>
</tv>
"""


class FakeXtream(BaseHTTPRequestHandler):
    actions: list[str] = []
    fail_api = False
    fail_categories = False
    series_info_calls: list[str] = []
    epg_mode = "plain"  # plain | gzip | html | error
    epg_requests = 0

    def log_message(self, *args):
        pass

    def send_body(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urlsplit(self.path)
        args = parse_qs(url.query)
        authed = args.get("username") == [USERNAME] and args.get("password") == [PASSWORD]
        if url.path == "/player_api.php":
            action = args.get("action", [""])[0]
            type(self).actions.append(action)
            if type(self).fail_api:
                return self.send_body(503, b"down", "text/plain")
            if not authed:
                return self.send_body(401, b"unauthorized", "text/plain")
            if action == "get_live_streams":
                data = STREAMS
            elif action == "get_live_categories" and type(self).fail_categories:
                return self.send_body(500, b"categories down", "text/plain")
            elif action == "get_live_categories":
                data = CATEGORIES
            elif action == "get_vod_categories":
                data = VOD_CATEGORIES
            elif action == "get_vod_streams":
                data = VOD_STREAMS
            elif action == "get_series_categories":
                data = SERIES_CATEGORIES
            elif action == "get_series":
                data = SERIES
            elif action == "get_series_info":
                sid = args.get("series_id", [""])[0]
                type(self).series_info_calls.append(sid)
                if sid not in SERIES_INFO:
                    return self.send_body(500, b"panel error", "text/plain")
                data = SERIES_INFO[sid]
            elif action == "":
                data = {"user_info": {"username": USERNAME, "password": PASSWORD, "status": "Active",
                                      "exp_date": "1893456000", "max_connections": "1", "active_cons": "0",
                                      "is_trial": "0"},
                        "server_info": {"url": "fake", "port": "80"}}
            else:
                data = {"error": "invalid action"}
            return self.send_body(200, json.dumps(data).encode(), "application/json")
        if url.path == f"/live/{USERNAME}/{PASSWORD}/42.ts":
            body = b"TS DATA 12345678"
            if self.headers.get("Range") == "bytes=3-":
                return self.send_body(206, body[3:], "video/mp2t", {"Content-Range": "bytes 3-15/16"})
            return self.send_body(200, body, "video/mp2t")
        if url.path == f"/movie/{USERNAME}/{PASSWORD}/501.mkv":
            body = bytes(range(256)) * 4  # 1 Kio
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes=") and rng.endswith("-"):
                start = int(rng[6:-1])
                return self.send_body(206, body[start:], "video/x-matroska",
                                      {"Content-Range": f"bytes {start}-{len(body) - 1}/{len(body)}",
                                       "Accept-Ranges": "bytes"})
            return self.send_body(200, body, "video/x-matroska", {"Accept-Ranges": "bytes"})
        if url.path == f"/series/{USERNAME}/{PASSWORD}/9000.mp4":
            return self.send_body(200, b"EPISODE", "")
        if url.path == f"/live/{USERNAME}/{PASSWORD}/44.ts":
            return self.send_body(509, b"max connections", "text/plain")
        if url.path == "/xmltv.php":
            type(self).epg_requests += 1
            if not authed or type(self).epg_mode == "error":
                return self.send_body(500, b"error", "text/plain")
            if type(self).epg_mode == "html":
                return self.send_body(200, b"<html>nope</html>", "text/html")
            body = XMLTV.encode()
            if type(self).epg_mode == "gzip":
                body = gzip.compress(body)
            return self.send_body(200, body, "application/xml")
        return self.send_body(404, b"not found", "text/plain")


def start_fake(host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer((host, port), FakeXtream)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


def reset_fake() -> None:
    FakeXtream.actions = []
    FakeXtream.fail_api = False
    FakeXtream.fail_categories = False
    FakeXtream.series_info_calls = []
    FakeXtream.epg_mode = "plain"
    FakeXtream.epg_requests = 0
