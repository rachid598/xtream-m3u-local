"""Plusieurs abonnements Xtream fusionnés (XTREAM_URL_2…)."""
import json
import os
import threading
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.parse import urlencode

from app.providers import OFFSET, decode, encode
from app.server import Config, make_api, make_epg, make_series, make_server, make_streams
from app.web import make_web_server
from app.custom import PlaylistStore
from app.devices import DeviceStore

from .fake_xtream import FakeXtream, PASSWORD, USERNAME
from .helpers import StackTestCase

B = str(2 * OFFSET)


class FakeB(FakeXtream):
    """Second fournisseur : mêmes données, journal des chemins séparé."""
    paths: list[str] = []
    down = False

    def do_GET(self):
        type(self).paths.append(self.path.split("?")[0])
        if type(self).down:
            return self.send_body(503, b"down", "text/plain")
        return super().do_GET()


class EncodingTests(unittest.TestCase):
    def test_encode_decode(self):
        self.assertEqual(encode(1, "42"), "42")
        self.assertEqual(encode(2, "42"), str(2 * OFFSET + 42))
        self.assertEqual(decode(encode(3, "7")), (3, "7"))
        self.assertEqual(decode("42"), (1, "42"))
        self.assertEqual(encode(2, "abc"), "p2.abc")
        self.assertEqual(decode("p2.abc"), (2, "abc"))

    def test_config(self):
        base = {"XTREAM_URL": "http://a", "XTREAM_USERNAME": "u", "XTREAM_PASSWORD": "p", "XTREAM_NAME": "Principal"}
        with mock.patch.dict(os.environ, {**base, "XTREAM_URL_2": "b.example:8080", "XTREAM_USERNAME_2": "u2",
                                          "XTREAM_PASSWORD_2": "p2", "XTREAM_NAME_2": "Secours"}, clear=True):
            cfg = Config.from_env()
        self.assertEqual(cfg.provider_name, "Principal")
        self.assertEqual(cfg.extra_providers, (("Secours", "http://b.example:8080", "u2", "p2"),))
        self.assertEqual(cfg.credentials, [("u", "p"), ("u2", "p2")])
        with mock.patch.dict(os.environ, {**base, "XTREAM_URL_3": "http://c"}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()


class MultiProviderTests(StackTestCase):
    def setUp(self):
        super().setUp()
        FakeB.paths = []
        FakeB.down = False
        self.server_b = ThreadingHTTPServer(("127.0.0.1", 0), FakeB)
        self.server_b.daemon_threads = True
        threading.Thread(target=self.server_b.serve_forever, daemon=True).start()
        url_b = f"http://127.0.0.1:{self.server_b.server_address[1]}"
        cfg = replace(self.stack.config, provider_name="A", extra_providers=(("B", url_b, USERNAME, PASSWORD),))
        self.api = make_api(cfg)
        store = PlaylistStore(self.tmp.name + "/multi")
        streams = make_streams(cfg, self.api)
        series = make_series(cfg, self.api)
        devices = DeviceStore(self.tmp.name + "/multi")
        self.proxy = make_server(cfg, host="127.0.0.1", port=0, api=self.api, store=store, epg=make_epg(cfg, self.api),
                                 series=series, streams=streams, devices=devices)
        self.web = make_web_server(cfg, self.api, store, host="127.0.0.1", port=0, series=series, streams=streams,
                                   devices=devices)
        for srv in (self.proxy, self.web):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        self.web_base = f"http://127.0.0.1:{self.web.server_address[1]}"

    def tearDown(self):
        for srv in (self.web, self.proxy, self.server_b):
            srv.shutdown()
            srv.server_close()
        super().tearDown()

    def test_merged_catalog_and_playlist(self):
        catalog = json.loads(self.get(self.web_base + "/api/catalog")[1])
        self.assertEqual(len(catalog["channels"]), 12)
        ids = [c["stream_id"] for c in catalog["channels"]]
        self.assertIn(str(2 * OFFSET + 42), ids)
        cats = {c["category_id"]: (c["name"], c.get("provider")) for c in catalog["categories"]}
        self.assertEqual(cats["1"], ("Actualités", "A"))
        self.assertEqual(cats[str(2 * OFFSET + 1)], ("Actualités", "B"))
        self.assertEqual(cats["none"][0], "Sans catégorie")
        self.create_playlist()
        data = self.get(self.base + "/p/mix.m3u")[1].decode()
        self.assertIn("/stream/42.ts", data)
        self.assertIn(f"/stream/{2 * OFFSET + 42}.ts", data)
        self.assertEqual(data.count('group-title="Actualités",France Info HD'), 2)   # même groupe
        self.assertNotIn("/pw/", data)

    def create_playlist(self):
        status, _ = self.send(self.web_base + "/api/playlists", "POST",
                              {"slug": "mix", "name": "Mix", "category_ids": ["1", str(2 * OFFSET + 1)],
                               "series_category_ids": [str(2 * OFFSET + 1)]})
        self.assertEqual(status, 201)

    def test_streams_routed_to_right_provider(self):
        before = len(FakeB.paths)
        self.assertEqual(self.get(f"{self.base}/stream/{2 * OFFSET + 42}.ts")[1], b"TS DATA 12345678")
        self.assertIn(f"/live/{USERNAME}/{PASSWORD}/42.ts", FakeB.paths[before:])
        before = len(FakeB.paths)
        self.assertEqual(self.get(f"{self.base}/stream/42.ts")[1], b"TS DATA 12345678")
        self.assertEqual(FakeB.paths[before:], [])                       # fournisseur A
        movie = self.get(f"{self.base}/movie/{2 * OFFSET + 501}.mkv")
        self.assertEqual((movie[0], len(movie[1])), (200, 1024))
        self.assertEqual(self.get(f"{self.base}/timeshift/{2 * OFFSET + 42}.ts?start=1794762000&duration=5")[1],
                         b"REPLAY 5/2026-11-15:18-00/42.ts")
        self.assertEqual(self.get(f"{self.base}/stream/{9 * OFFSET + 1}.ts")[0], 404)   # fournisseur inexistant

    def test_series_episodes_encoded(self):
        self.create_playlist()
        data = json.loads(self.get(self.web_base + f"/api/series/{2 * OFFSET + 900}")[1])
        self.assertEqual([e["id"] for e in data["episodes"]],
                         [str(2 * OFFSET + 9000), str(2 * OFFSET + 9001), str(2 * OFFSET + 9002)])
        m3u = self.get(self.base + "/p/mix.m3u")[1].decode()
        self.assertIn(f"/series/{2 * OFFSET + 9000}.mp4", m3u)
        self.assertEqual(self.get(f"{self.base}/series/{2 * OFFSET + 9000}.mp4")[1], b"EPISODE")

    def test_xtream_emulation_numeric_ids(self):
        self.create_playlist()
        device = self.send(self.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "mix"})[1]
        q = {"username": device["username"], "password": device["password"]}
        live = json.loads(self.get(self.base + "/player_api.php?" + urlencode({**q, "action": "get_live_streams"}))[1])
        self.assertIn(2 * OFFSET + 42, [c["stream_id"] for c in live])
        info = json.loads(self.get(self.base + "/player_api.php?" + urlencode(
            {**q, "action": "get_series_info", "series_id": str(2 * OFFSET + 900)}))[1])
        first = info["episodes"]["1"][0]
        self.assertEqual(first["id"], str(2 * OFFSET + 9001))
        status = self.get(f"{self.base}/series/{device['username']}/{device['password']}/{first['id']}.mp4")
        self.assertEqual(status[0], 502)  # fichier absent chez le faux fournisseur, mais bien routé vers B
        self.assertIn(f"/series/{USERNAME}/{PASSWORD}/9001.mp4", FakeB.paths)

    def test_one_provider_down(self):
        FakeB.down = True
        catalog = json.loads(self.get(self.web_base + "/api/catalog?refresh=1")[1])
        self.assertEqual(len(catalog["channels"]), 6)          # fournisseur A seul
        status = json.loads(self.get(self.web_base + "/api/status?account=1")[1])
        self.assertEqual([p["name"] for p in status["providers"]], ["A", "B"])
        diag = self.send(self.web_base + "/api/diagnostic", "POST")[1]
        statuses = {r["id"]: r["status"] for r in diag["results"]}
        self.assertEqual((statuses["account"], statuses["account-2"]), ("ok", "error"))


if __name__ == "__main__":
    unittest.main()
