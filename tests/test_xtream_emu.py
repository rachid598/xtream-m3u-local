"""Appareils (codes d'accès) et serveur Xtream local (player_api.php)."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from app.devices import DeviceStore, InvalidDevice

from .fake_xtream import FakeXtream
from .helpers import StackTestCase


class DeviceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeviceStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_crud_and_authentication(self):
        a = self.store.create("TV du Salon", "salon")
        b = self.store.create("TV du salon")
        self.assertEqual((a["username"], b["username"]), ("tv-du-salon", "tv-du-salon-2"))
        self.assertRegex(a["password"], r"^[a-z2-9]{10}$")
        self.assertEqual(self.store.authenticate("tv-du-salon", a["password"])["id"], a["id"])
        self.assertIsNone(self.store.authenticate("tv-du-salon", b["password"]))
        self.assertIsNone(self.store.authenticate("tv-du-salon", "mauvais"))
        self.assertEqual(self.store.find_by_token(b["password"])["id"], b["id"])
        updated = self.store.update(a["id"], {"name": "Salon", "playlist": "", "regenerate": True})
        self.assertNotEqual(updated["password"], a["password"])
        self.assertIsNone(self.store.find_by_token(a["password"]))
        self.assertTrue(self.store.delete(b["id"]))
        self.assertFalse(self.store.delete(b["id"]))
        self.assertEqual(len(DeviceStore(self.tmp.name).list()), 1)          # persistance
        mode = stat.S_IMODE(os.stat(Path(self.tmp.name, "devices.json")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_validation_and_settings(self):
        for name in ("", "x" * 61, "a\nb", None):
            with self.subTest(name=name), self.assertRaises(InvalidDevice):
                self.store.create(name)
        with self.assertRaises(InvalidDevice):
            self.store.create("TV", "../x")
        with self.assertRaises(InvalidDevice):
            self.store.create("TV", "", "Majuscules")
        self.assertFalse(self.store.require_token)
        self.store.set_require_token(True)
        self.assertTrue(DeviceStore(self.tmp.name).require_token)
        with self.assertRaises(InvalidDevice):
            self.store.restore({"devices": [{"name": "X", "username": "x", "password": "a b"}]})
        self.store.restore({"require_token": False, "devices": [{"name": "X", "username": "x", "password": "abcdef12"}]})
        self.assertEqual(self.store.authenticate("x", "abcdef12")["name"], "X")


class EmulatorTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "vod_category_ids": ["1"],
                     "series_category_ids": ["1"], "group_names": {"1": "Infos", "vod:1": "Action !"},
                     "item_names": {"42": "France Info"}, "item_numbers": {"42": 5}})
        status, self.device = self.send(self.stack.web_base + "/api/devices", "POST",
                                        {"name": "TV salon", "playlist": "salon"})
        self.assertEqual(status, 201)
        self.u, self.p = self.device["username"], self.device["password"]

    def api(self, **params):
        query = urlencode({"username": self.u, "password": self.p, **params})
        status, body, headers = self.get(self.stack.proxy_base + "/player_api.php?" + query)
        self.assertEqual(status, 200, body[:200])
        return json.loads(body)

    def test_authentication_and_account(self):
        for query in ("", "?username=x&password=y", f"?username={self.u}&password=faux"):
            data = json.loads(self.get(self.stack.proxy_base + "/player_api.php" + query)[1])
            self.assertEqual(data["user_info"]["auth"], 0)
        data = self.api()
        info, server = data["user_info"], data["server_info"]
        self.assertEqual((info["auth"], info["username"], info["status"]), (1, self.u, "Active"))
        self.assertEqual(info["exp_date"], "1893456000")
        self.assertEqual(info["max_connections"], "1")
        self.assertEqual(server["timezone"], "Europe/Paris")
        self.assertEqual((server["url"], server["port"]), ("127.0.0.1", str(self.stack.proxy.server_address[1])))
        self.assertNotIn('"pw"', json.dumps(data))

    def test_refused_and_logged(self):
        """Appareil supprimé ou code faux : 401 sur les listes (le lecteur l'affiche), et une ligne au journal."""
        from app import server
        server._REFUSED_LAST.clear()
        server._LOGIN_LAST.clear()
        with self.assertLogs("xtream-m3u-local", level="INFO") as logs:
            self.api()                                            # connexion : playlist et contenu au journal
            query = urlencode({"username": self.u, "password": "faux", "action": "get_live_streams"})
            self.assertEqual(self.get(self.stack.proxy_base + "/player_api.php?" + query)[0], 401)
            self.send(self.stack.web_base + f"/api/devices/{self.device['id']}", "DELETE")
            status, body, _ = self.get(self.stack.proxy_base + "/player_api.php?" + urlencode(
                {"username": self.u, "password": self.p, "action": "get_live_categories"}))
            self.assertEqual((status, json.loads(body)["user_info"]["auth"]), (401, 0))
        text = "\n".join(logs.output)
        self.assertIn("Appareil « TV salon » connecté depuis 127.0.0.1 : playlist « Salon », profil 1 — 2 chaîne(s)", text)
        self.assertEqual(text.count("identifiants refusés"), 1)    # une ligne par minute et par adresse
        self.assertNotIn(self.p, text)

    def test_categories_and_streams(self):
        self.assertEqual(self.api(action="get_live_categories"),
                         [{"category_id": "1", "category_name": "Infos", "parent_id": 0}])
        live = self.api(action="get_live_streams")
        self.assertEqual([(s["stream_id"], s["name"], s["num"]) for s in live],
                         [(42, "France Info", 5), (44, "Sport 1", 2)])
        first = live[0]
        self.assertEqual((first["epg_channel_id"], first["tv_archive"], first["tv_archive_duration"]), ("franceinfo.fr", 1, 3))
        self.assertEqual(first["category_id"], "1")
        self.assertEqual(self.api(action="get_live_streams", category_id="99"), [])
        self.assertEqual(self.api(action="get_vod_categories")[0]["category_name"], "Action !")
        vod = self.api(action="get_vod_streams")
        self.assertEqual([v["stream_id"] for v in vod], [501, 503])
        self.assertEqual((vod[0]["container_extension"], vod[0]["rating"], vod[0]["direct_source"]), ("mkv", "7.4", ""))
        series = self.api(action="get_series")
        self.assertEqual([(s["series_id"], s["name"]) for s in series], [(900, "Ma Série")])
        self.assertEqual(self.api(action="unknown"), [])

    def test_details_passthrough_redacted(self):
        info = self.api(action="get_series_info", series_id="900")
        self.assertIn("episodes", info)
        vod = self.api(action="get_vod_info", vod_id="501")
        self.assertEqual(vod["info"]["plot"], "Résumé")
        self.assertEqual(vod["movie_data"]["direct_source"], "http://cdn.invalid/movie/***/***/501.mkv")
        self.assertEqual(self.api(action="get_vod_info", vod_id="502"), {})       # hors playlist
        self.assertEqual(self.api(action="get_series_info", series_id="901"), {})
        epg = self.api(action="get_short_epg", stream_id="42", limit="4")
        self.assertEqual(len(epg["epg_listings"]), 1)

    def test_streams_with_device_credentials(self):
        base = self.stack.proxy_base
        self.assertEqual(self.get(f"{base}/live/{self.u}/{self.p}/42.ts")[1], b"TS DATA 12345678")
        self.assertEqual(self.get(f"{base}/live/{self.u}/{self.p}/43.ts")[0], 404)          # hors playlist
        self.assertEqual(self.get(f"{base}/live/{self.u}/faux/42.ts")[0], 403)
        self.assertEqual(len(self.get(f"{base}/movie/{self.u}/{self.p}/501.mkv")[1]), 1024)
        self.assertEqual(self.get(f"{base}/movie/{self.u}/{self.p}/502.mp4")[0], 404)
        self.assertEqual(self.get(f"{base}/series/{self.u}/{self.p}/9000.mp4")[1], b"EPISODE")
        manifest = self.get(f"{base}/live/{self.u}/{self.p}/42.m3u8")[1].decode()
        self.assertIn(f"?token={self.p}", manifest)
        self.assertNotIn("tok123", manifest)
        status, body, _ = self.get(f"{base}/timeshift/{self.u}/{self.p}/60/2026-11-15:18-00/42.ts")
        self.assertEqual((status, body), (200, b"REPLAY 60/2026-11-15:18-00/42.ts"))
        query = urlencode({"username": self.u, "password": self.p, "stream": "42",
                           "start": "2026-11-15:18-00", "duration": "30"})
        self.assertEqual(self.get(f"{base}/streaming/timeshift.php?{query}")[1], b"REPLAY 30/2026-11-15:18-00/42.ts")
        self.assertEqual(self.get(f"{base}/timeshift/{self.u}/{self.p}/60/2026-11-15:18-00/43.ts")[0], 404)

    def test_get_php_and_xmltv_for_device(self):
        base = self.stack.proxy_base
        query = urlencode({"username": self.u, "password": self.p, "type": "m3u_plus", "output": "ts"})
        m3u = self.get(f"{base}/get.php?{query}")[1].decode()
        self.assertIn(f"/stream/42.ts?token={self.p}", m3u)
        self.assertIn(",France Info\n", m3u)
        self.assertIn("/xmltv.php?username=", m3u.splitlines()[0])
        self.assertIn("/movie/501.mkv", m3u)
        hls = self.get(f"{base}/get.php?" + urlencode({"username": self.u, "password": self.p, "output": "m3u8"}))[1]
        self.assertIn(b"/hls/42.m3u8?token=", hls)
        self.assertEqual(self.get(f"{base}/get.php?username={self.u}&password=faux")[0], 403)
        guide = self.get(f"{base}/xmltv.php?" + urlencode({"username": self.u, "password": self.p}))[1]
        self.assertIn(b"franceinfo.fr", guide)
        # Sans identifiants : comportement habituel (playlist complète).
        self.assertIn(b"/stream/43.ts", self.get(f"{base}/get.php")[1])

    def test_device_without_playlist_sees_everything(self):
        _, device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "Bureau"})
        query = urlencode({"username": device["username"], "password": device["password"], "action": "get_live_streams"})
        live = json.loads(self.get(self.stack.proxy_base + "/player_api.php?" + query)[1])
        self.assertEqual(len(live), 6)

    def test_require_token_with_device_codes(self):
        base, web = self.stack.proxy_base, self.stack.web_base
        self.assertEqual(self.get(base + "/p/salon.m3u")[0], 200)
        status, data = self.send(web + "/api/devices/settings", "PUT", {"require_token": True})
        self.assertEqual((status, data["require_token"]), (200, True))
        self.assertEqual(self.get(base + "/p/salon.m3u")[0], 403)
        self.assertEqual(self.get(base + "/stream/42.ts")[0], 403)
        m3u = self.get(base + f"/p/salon.m3u?token={self.p}")[1].decode()
        self.assertIn(f"/stream/42.ts?token={self.p}", m3u)
        self.assertEqual(self.get(base + f"/stream/42.ts?token={self.p}")[0], 200)
        self.assertTrue(json.loads(self.get(web + "/api/status")[1])["token_required"])
        # Supprimer l'appareil coupe son accès.
        self.assertEqual(self.send(web + f"/api/devices/{self.device['id']}", "DELETE")[0], 200)
        self.assertEqual(self.get(base + f"/stream/42.ts?token={self.p}")[0], 403)

    def test_devices_web_api(self):
        web = self.stack.web_base
        data = json.loads(self.get(web + "/api/devices")[1])
        self.assertEqual([d["username"] for d in data["devices"]], [self.u])
        self.assertFalse(data["require_token"])
        self.get(self.stack.proxy_base + f"/p/salon.m3u?token={self.p}")
        self.assertIsNotNone(json.loads(self.get(web + "/api/devices")[1])["devices"][0]["last_seen"])
        status, updated = self.send(web + f"/api/devices/{self.device['id']}", "PUT", {"regenerate": True, "name": "TV"})
        self.assertEqual((status, updated["name"]), (200, "TV"))
        self.assertNotEqual(updated["password"], self.p)
        self.assertEqual(self.send(web + "/api/devices/zzzz", "PUT", {"name": "X"})[0], 404)
        self.assertEqual(self.send(web + "/api/devices", "POST", {"name": ""})[0], 400)
        self.assertEqual(self.send(web + "/api/devices", "POST", {"name": "X"}, {"Content-Type": "application/json"})[0], 403)


if __name__ == "__main__":
    unittest.main()


class EmulatorCacheTests(unittest.TestCase):
    """Liste d'un appareil recalculée quand un abonnement charge son catalogue, même à date fusionnée égale."""

    def test_new_catalog_with_older_date_is_seen(self):
        from types import SimpleNamespace
        from app.xtream_emu import XtreamEmulator

        state = SimpleNamespace(items=[], updated_at=1000.0, categories={})

        class Api:
            def kind_state(self, kind):
                return state

            def catalog(self, refresh=False, kind="live"):
                return state.items, state.categories

        class Store:
            def get(self, slug):
                return {"slug": "zineb", "name": "Zineb", "category_ids": ["1"], "updated_at": 5}

        emu = XtreamEmulator(Api(), Store(), None)
        device = {"id": "d1", "username": "tv", "playlist": "zineb"}
        self.assertEqual(emu.entries(device, "live")[0], [])          # catalogue pas encore chargé
        state.items = [{"stream_id": "7", "name": "Chaîne", "category_id": "1", "category_ids": ["1"],
                        "logo": "", "epg_id": "", "num": "", "archive": 0}]
        state.categories = {"1": "Infos"}                              # même date fusionnée (copie plus ancienne)
        self.assertEqual([i["stream_id"] for i, _ in emu.entries(device, "live")[0]], ["7"])
