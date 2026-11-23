"""Faux fournisseur Xtream Codes pour les tests et la démo locale (aucun réseau externe)."""
from __future__ import annotations

import gzip
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from app.epg import xmltv_time

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
     "category_id": "1", "category_ids": [1], "tv_archive": 1, "tv_archive_duration": "3",
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

def _b64(text: str) -> str:
    import base64
    return base64.b64encode(text.encode()).decode()


# get_simple_data_table : titres/descriptions en base64, horodatages UTC.
EPG_START = int(time.time()) - 120
EPG_END = EPG_START + 3600
EPG_NEXT_END = EPG_END + 5400
SIMPLE_EPG = {
    "42": {"epg_listings": [
        {"id": "1", "epg_id": "1", "title": _b64("Journal API"), "lang": "fr",
         "description": _b64("Infos"), "channel_id": "franceinfo.fr",
         "start_timestamp": str(EPG_START), "stop_timestamp": str(EPG_END)}]},
    "43": {"epg_listings": [
        {"id": "2", "title": _b64("Concert <live> & co"), "description": _b64("Musique"),
         "start_timestamp": str(EPG_START), "stop_timestamp": str(EPG_END)},
        {"id": "3", "title": _b64("Sans fin"), "start_timestamp": str(EPG_END), "stop_timestamp": "0"},
        {"id": "4", "title": "pas-du-base64 !", "start": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(EPG_END)),
         "end": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(EPG_NEXT_END))}]},
}

XMLTV = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE tv SYSTEM "xmltv.dtd">
<tv generator-info-name="fake">
  <channel id="franceinfo.fr"><display-name>France Info</display-name></channel>
  <channel id="sport1.fr"><display-name>Sport 1</display-name></channel>
  <channel id="other.fr"><display-name>Autre &amp; co</display-name></channel>
  <programme start="{xmltv_time(EPG_START)}" stop="{xmltv_time(EPG_END)}" channel="franceinfo.fr"><title>Journal</title></programme>
  <programme start="{xmltv_time(EPG_START)}" stop="{xmltv_time(EPG_END)}" channel="sport1.fr"><title>Match</title></programme>
  <programme start="{xmltv_time(EPG_START)}" stop="{xmltv_time(EPG_END)}" channel="other.fr"><title>Autre</title></programme>
</tv>
"""


class FakeXtream(BaseHTTPRequestHandler):
    actions: list[str] = []
    fail_api = False
    fail_categories = False
    series_info_calls: list[str] = []
    simple_epg_calls: list[str] = []
    open_streams = 0
    serve_any = False
    hls_requests: list[str] = []
    timeshift_requests: list[str] = []
    epg_mode = "plain"  # plain | gzip | html | error
    epg_requests = 0
    busy_requests = 0

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
            if type(self).fail_api == 429:
                return self.send_body(429, b"Too Many Requests", "text/plain", {"Retry-After": "30"})
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
            elif action == "get_vod_info":
                vid = args.get("vod_id", [""])[0]
                data = {"info": {"name": f"Film {vid}", "plot": "Résumé", "rating": "7.4"},
                        "movie_data": {"stream_id": int(vid) if vid.isdigit() else 0, "container_extension": "mkv",
                                       "direct_source": f"http://cdn.invalid/movie/{USERNAME}/{PASSWORD}/{vid}.mkv"}}
            elif action == "get_short_epg":
                sid = args.get("stream_id", [""])[0]
                data = SIMPLE_EPG.get(sid, {"epg_listings": []})
            elif action == "get_simple_data_table":
                sid = args.get("stream_id", [""])[0]
                type(self).simple_epg_calls.append(sid)
                data = SIMPLE_EPG.get(sid, {"epg_listings": []})
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
                        "server_info": {"url": "fake", "port": "80", "timezone": "Europe/Paris"}}
            else:
                data = {"error": "invalid action"}
            return self.send_body(200, json.dumps(data).encode(), "application/json")
        if url.path == f"/live/{USERNAME}/{PASSWORD}/42.ts":
            body = b"TS DATA 12345678"
            if self.headers.get("Range") == "bytes=3-":
                return self.send_body(206, body[3:], "video/mp2t", {"Content-Range": "bytes 3-15/16"})
            return self.send_body(200, body, "video/mp2t")
        if url.path == f"/live/{USERNAME}/{PASSWORD}/42.m3u8":
            type(self).hls_requests.append(url.path)
            self.send_response(302)
            self.send_header("Location", "/hlsgw/tok123/42/master.m3u8")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if url.path == f"/live/{USERNAME}/{PASSWORD}/43.m3u8":
            return self.send_body(200, b"<html>pas du HLS</html>", "text/html")
        if url.path.startswith(f"/timeshift/{USERNAME}/{PASSWORD}/"):
            parts = url.path.split("/")      # /timeshift/user/pw/<min>/<YYYY-MM-DD:HH-MM>/<id>.ts
            type(self).timeshift_requests.append("/".join(parts[4:]))
            return self.send_body(200, f"REPLAY {'/'.join(parts[4:])}".encode(), "video/mp2t")
        if url.path.startswith("/hlsgw/") or url.path.startswith("/keys/"):
            type(self).hls_requests.append(url.path)
            host = self.headers.get("Host")
            files = {
                "/hlsgw/tok123/42/master.m3u8": (
                    "#EXTM3U\n"
                    '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="fr",URI="audio/fr.m3u8"\n'
                    '#EXT-X-STREAM-INF:BANDWIDTH=800000,AUDIO="aud"\n'
                    "low/index.m3u8?sig=abc\n").encode(),
                "/hlsgw/tok123/42/low/index.m3u8": (
                    "#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:10\n"
                    '#EXT-X-KEY:METHOD=AES-128,URI="/keys/k1.key",IV=0x01\n'
                    '#EXT-X-MAP:URI="init.mp4"\n'
                    "#EXTINF:4.0,\nseg10.ts\n"
                    f"#EXTINF:4.0,\nhttp://{host}/hlsgw/tok123/42/low/seg11.ts\n").encode(),
                "/hlsgw/tok123/42/audio/fr.m3u8": b"#EXTM3U\n#EXTINF:4.0,\na10.aac\n",
                "/hlsgw/tok123/42/low/seg10.ts": b"SEG10",
                "/hlsgw/tok123/42/low/seg11.ts": b"SEG11",
                "/hlsgw/tok123/42/low/init.mp4": b"INIT",
                "/hlsgw/tok123/42/audio/a10.aac": b"AAC10",
                "/keys/k1.key": b"0123456789abcdef",
            }
            if url.path not in files:
                return self.send_body(404, b"nope", "text/plain")
            ctype = "application/vnd.apple.mpegurl" if url.path.endswith(".m3u8") else "video/mp2t"
            return self.send_body(200, files[url.path], ctype)
        if url.path in (f"/live/{USERNAME}/{PASSWORD}/70.ts", f"/live/{USERNAME}/{PASSWORD}/70.m3u8"):
            # Abonnement déjà utilisé ailleurs : le fournisseur refuse une connexion de plus.
            type(self).busy_requests += 1
            return self.send_body(429, b"Too Many Requests", "text/plain", {"Retry-After": "12"})
        if url.path == f"/live/{USERNAME}/{PASSWORD}/60.ts":
            # Bascule automatique : démarre puis se tait (saccade / coupure côté fournisseur).
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()
                self.wfile.write(b"A" * 4096)
                self.wfile.flush()
                time.sleep(20)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if url.path == f"/live/{USERNAME}/{PASSWORD}/61.ts":
            # Version de secours qui fonctionne (puis se termine).
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()
                for _ in range(10):
                    self.wfile.write(b"B" * 1024)
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if url.path in (f"/live/{USERNAME}/{PASSWORD}/50.ts", f"/live/{USERNAME}/{PASSWORD}/51.ts"):
            # Flux continu (direct) : 1 Kio toutes les 50 ms pendant 15 s au maximum.
            type(self).open_streams += 1
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.end_headers()
                for _ in range(300):
                    self.wfile.write(b"\x47" * 1024)
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                type(self).open_streams -= 1
            return
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
        if type(self).serve_any:
            # Mode démo : un contenu factice pour n'importe quel flux.
            prefix = f"/{USERNAME}/{PASSWORD}/"
            if url.path.startswith("/live" + prefix) and url.path.endswith(".m3u8"):
                return self.send_body(200, b"#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\n/hlsgw/tok123/42/low/seg10.ts\n",
                                      "application/vnd.apple.mpegurl")
            if url.path.startswith(("/live" + prefix, "/timeshift" + prefix)):
                return self.send_body(200, (b"\x47" + b"\x00" * 187) * 256, "video/mp2t")
            if url.path.startswith(("/movie" + prefix, "/series" + prefix)):
                return self.send_body(200, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 65536, "video/mp4")
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
    FakeXtream.simple_epg_calls = []
    FakeXtream.hls_requests = []
    FakeXtream.timeshift_requests = []
    FakeXtream.epg_mode = "plain"
    FakeXtream.epg_requests = 0
    FakeXtream.busy_requests = 0
