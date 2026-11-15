import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from app.server import Config, escape_attribute, make_server


class FakeXtream(BaseHTTPRequestHandler):
    actions = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        args = parse_qs(url.query)
        if url.path == "/player_api.php":
            action = args.get("action", [""])[0]
            self.actions.append(action)
            if args.get("username") != ["user"] or args.get("password") != ["pw"]:
                self.send_error(401)
                return
            if action == "get_live_streams":
                data = [
                    {"stream_id": 42, "name": "France Info HD", "stream_icon": "https://logo.invalid/logo.png",
                     "epg_channel_id": "franceinfo.fr", "category_id": "1", "num": 1},
                    {"stream_id": "43", "name": 'Radio "TV"\nHACK', "category_id": "2"},
                    {"stream_id": "../../bad", "name": "Invalid"},
                ]
            elif action == "get_live_categories":
                data = [{"category_id": "1", "category_name": "Actualites"},
                        {"category_id": "2", "category_name": 'Musique\n"HD"'}]
            else:
                data = {"error": "invalid action"}
            content = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        elif url.path == "/live/user/pw/42.ts":
            body = b"TS DATA 12345678"
            r = self.headers.get("Range")
            if r == "bytes=3-":
                body = body[3:]
                self.send_response(206)
                self.send_header("Content-Range", "bytes 3-15/16")
            else:
                self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/xmltv.php":
            body = b"<tv></tv>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = ThreadingHTTPServer(("127.0.0.1", 0), FakeXtream)
        cls.pt = threading.Thread(target=cls.provider.serve_forever, daemon=True)
        cls.pt.start()
        provider_url = f"http://127.0.0.1:{cls.provider.server_address[1]}"
        cls.config = Config(provider_url, "user", "pw", cache_seconds=1800)
        cls.server = make_server(cls.config, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.provider.shutdown()
        cls.provider.server_close()

    def test_health(self):
        with urlopen(self.base + "/health") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"OK\n")

    def test_playlist_proxy_preserves_metadata_without_secrets(self):
        with urlopen(self.base + "/playlist.m3u") as r:
            data = r.read().decode("utf-8")
        self.assertTrue(data.startswith("#EXTM3U\n"))
        self.assertIn('tvg-id="franceinfo.fr"', data)
        self.assertIn('group-title="Actualites"', data)
        self.assertIn(f"{self.base}/stream/42.ts", data)
        self.assertIn("#EXTINF:-1", data)
        self.assertNotIn("/pw/", data)
        self.assertNotIn("/user/", data)
        self.assertNotIn("Invalid", data)
        self.assertNotIn('\nHACK', data)
        self.assertIn('Radio "TV" HACK', data)

    def test_direct_hls(self):
        with urlopen(self.base + "/playlist.m3u?mode=direct&format=m3u8") as r:
            data = r.read().decode()
        self.assertIn(self.config.xtream_url + "/live/user/pw/42.m3u8", data)
        self.assertNotIn(self.base + "/stream/42.ts", data)

    def test_proxy_hls_rejected(self):
        with self.assertRaises(HTTPError) as e:
            urlopen(self.base + "/playlist.m3u?mode=proxy&format=m3u8")
        self.assertEqual(e.exception.code, 400)

    def test_ts_relay(self):
        with urlopen(self.base + "/stream/42.ts") as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"TS DATA 12345678")
            self.assertEqual(r.headers["Content-Type"], "video/mp2t")

    def test_range(self):
        with urlopen(Request(self.base + "/stream/42.ts", headers={"Range": "bytes=3-"})) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(r.read(), b"DATA 12345678")

    def test_epg_disabled(self):
        with self.assertRaises(HTTPError) as e:
            urlopen(self.base + "/epg.xml")
        self.assertEqual(e.exception.code, 404)

    def test_refresh(self):
        before = FakeXtream.actions.count("get_live_streams")
        with urlopen(self.base + "/playlist.m3u?refresh=1") as r:
            self.assertIn(b"#EXTINF", r.read())
        self.assertEqual(FakeXtream.actions.count("get_live_streams"), before + 1)

    def test_access_token(self):
        cfg = Config(self.config.xtream_url, "user", "pw", access_token="mysecret")
        server = make_server(cfg, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with self.assertRaises(HTTPError) as e:
                urlopen(base + "/playlist.m3u")
            self.assertEqual(e.exception.code, 403)
            with urlopen(base + "/playlist.m3u?token=mysecret") as response:
                data = response.read().decode()
            self.assertIn("/stream/42.ts?token=mysecret", data)
            with urlopen(base + "/stream/42.ts?token=mysecret") as response:
                self.assertEqual(response.read(), b"TS DATA 12345678")
        finally:
            server.shutdown()
            server.server_close()

    def test_epg_enabled(self):
        cfg = Config(self.config.xtream_url, "user", "pw", enable_epg=True)
        server = make_server(cfg, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(base + "/playlist.m3u") as response:
                data = response.read().decode()
            self.assertIn('x-tvg-url="' + base + '/epg.xml"', data)
            with urlopen(base + "/epg.xml") as response:
                self.assertEqual(response.read(), b"<tv></tv>")
        finally:
            server.shutdown()
            server.server_close()

    def test_escape_attribute(self):
        self.assertEqual(escape_attribute('a"\nb'), "a' b")


if __name__ == "__main__":
    unittest.main()

class CustomPlaylistTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from app.custom import PlaylistStore
        from app.web import make_web_server
        from app.server import XtreamAPI
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeXtream)
        threading.Thread(target=self.fake.serve_forever, daemon=True).start()
        self.cfg = Config(f"http://127.0.0.1:{self.fake.server_address[1]}", "user", "pw", data_dir=self.tmp.name)
        self.api = XtreamAPI(self.cfg)
        self.store = PlaylistStore(self.tmp.name)
        self.proxy = make_server(self.cfg, host="127.0.0.1", port=0, api=self.api, store=self.store)
        self.web = make_web_server(self.cfg, self.api, self.store, host="127.0.0.1", port=0)
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.proxy_base = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        self.web_base = f"http://127.0.0.1:{self.web.server_address[1]}"

    def tearDown(self):
        self.web.shutdown(); self.web.server_close()
        self.proxy.shutdown(); self.proxy.server_close()
        self.fake.shutdown(); self.fake.server_close()
        self.tmp.cleanup()

    def create(self, data):
        return urlopen(Request(self.web_base + "/api/playlists",
                               data=json.dumps(data).encode(), method="POST",
                               headers={"X-Requested-With": "XtreamM3ULocal", "Content-Type": "application/json"}))

    def test_web_html_and_catalog(self):
        with urlopen(self.web_base) as r:
            self.assertIn(b'Xtream M3U Local', r.read())
        with urlopen(self.web_base + "/api/catalog") as r:
            catalog = json.load(r)
        self.assertEqual(len(catalog["categories"]), 2)
        self.assertEqual(len(catalog["channels"]), 2)
        self.assertNotIn("pw", json.dumps(catalog))

    def test_select_category_and_individual_exclusions(self):
        payload = {"slug": "salon", "name": "Salon", "category_ids": ["1"],
                   "included_stream_ids": ["43"], "excluded_stream_ids": ["42"]}
        with self.create(payload) as r:
            self.assertEqual(r.status, 201)
        with urlopen(self.proxy_base + "/p/salon.m3u") as r:
            content = r.read().decode()
        self.assertIn("/stream/43.ts", content)
        self.assertNotIn("/stream/42.ts", content)
        self.assertNotIn("/pw/", content)
        self.assertNotIn("/user/", content)

    def test_persistence_update_and_delete(self):
        from app.custom import PlaylistStore
        with self.create({"slug": "essai", "name": "Premier", "category_ids": ["1"]}):
            pass
        fresh = PlaylistStore(self.tmp.name)
        self.assertEqual(fresh.get("essai")["name"], "Premier")
        request = Request(self.web_base + "/api/playlists/essai",
                          data=json.dumps({"name": "Deuxième", "category_ids": ["2"]}).encode(),
                          method="PUT", headers={"X-Requested-With": "XtreamM3ULocal", "Content-Type": "application/json"})
        with urlopen(request):
            pass
        self.assertEqual(fresh.get("essai")["name"], "Deuxième")
        with urlopen(Request(self.web_base + "/api/playlists/essai", method="DELETE",
                             headers={"X-Requested-With": "XtreamM3ULocal"})) as r:
            self.assertEqual(r.status, 200)
        self.assertIsNone(fresh.get("essai"))
        with self.assertRaises(HTTPError) as e:
            urlopen(self.proxy_base + "/p/essai.m3u")
        self.assertEqual(e.exception.code, 404)

    def test_bad_slug_and_csrf_guard(self):
        with self.assertRaises(HTTPError) as e:
            self.create({"slug": "../../etc", "name": "Malveillance"})
        self.assertEqual(e.exception.code, 400)
        with self.assertRaises(HTTPError) as e:
            urlopen(Request(self.web_base + "/api/playlists", method="POST",
                            data=b'{"slug":"x","name":"X"}',
                            headers={"Content-Type": "application/json"}))
        self.assertEqual(e.exception.code, 403)

    def test_web_basic_auth(self):
        import base64
        from dataclasses import replace
        from app.web import make_web_server
        cfg = replace(self.cfg, web_password="secret")
        web = make_web_server(cfg, self.api, self.store, host="127.0.0.1", port=0)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{web.server_address[1]}"
        try:
            with self.assertRaises(HTTPError) as e:
                urlopen(base)
            self.assertEqual(e.exception.code, 401)
            token = base64.b64encode(b"admin:secret").decode()
            with urlopen(Request(base, headers={"Authorization": "Basic " + token})) as r:
                self.assertEqual(r.status, 200)
        finally:
            web.shutdown(); web.server_close()
