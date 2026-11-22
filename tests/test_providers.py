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
    agents: list[str] = []
    down = False

    def do_GET(self):
        type(self).paths.append(self.path.split("?")[0])
        type(self).agents.append(self.headers.get("User-Agent", ""))
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
        # Fournisseur A : aucun flux demandé à B (un rafraîchissement du compte en arrière-plan est permis).
        self.assertEqual([p for p in FakeB.paths[before:] if not p.startswith("/player_api.php")], [])
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

    def test_profiles_keep_subscriptions_apart(self):
        status = json.loads(self.get(self.web_base + "/api/status")[1])
        self.assertEqual([(p["id"], p["name"]) for p in status["profiles"]], [("1", "A"), ("2", "B")])
        catalog = json.loads(self.get(self.web_base + "/api/catalog")[1])
        by_profile = {c["stream_id"]: c["profile"] for c in catalog["channels"]}
        self.assertEqual((by_profile["42"], by_profile[str(2 * OFFSET + 42)]), ("1", "2"))
        self.assertEqual({c["category_id"]: c["profile"] for c in catalog["categories"]}[str(2 * OFFSET + 1)], "2")
        # Playlist de la sœur (profil 2) : même avec une catégorie de A cochée par erreur, rien de A.
        status, _ = self.send(self.web_base + "/api/playlists", "POST",
                              {"slug": "soeur", "name": "Sœur", "provider": "2",
                               "category_ids": ["1", str(2 * OFFSET + 1)],
                               "rules": [{"kind": "live", "match": "contains", "value": "actualit"}]})
        self.assertEqual(status, 201)
        data = self.get(self.base + "/p/soeur.m3u")[1].decode()
        self.assertIn(f"/stream/{2 * OFFSET + 42}.ts", data)
        self.assertNotIn("/stream/42.ts", data)
        # Son appareil Xtream voit son abonnement à elle.
        device = self.send(self.web_base + "/api/devices", "POST", {"name": "Sœur TV", "playlist": "soeur"})[1]
        q = {"username": device["username"], "password": device["password"]}
        live = json.loads(self.get(self.base + "/player_api.php?" + urlencode({**q, "action": "get_live_streams"}))[1])
        self.assertTrue(live and all(10 ** 9 <= c["stream_id"] < 2 ** 31 for c in live))   # identifiants courts de B
        self.assertEqual(self.get(self.base + "/stream/42.ts?" + urlencode({"token": device["password"]}))[0], 404)
        self.assertEqual(self.send(self.web_base + "/api/playlists", "POST",
                                   {"slug": "x", "name": "X", "provider": "12"})[0], 400)

    def test_profile_is_a_separate_server(self):
        """Un profil = un serveur à part ; un abonnement n'entre dans les mélanges que s'il est « mélangeable »."""
        from app import settings
        settings.install(settings.Settings(self.tmp.name))
        self.addCleanup(settings.install, None)
        profiles = json.loads(self.get(self.web_base + "/api/status")[1])["profiles"]
        self.assertEqual([p["mixable"] for p in profiles], [False, False])   # isolés par défaut
        self.create_playlist()                                       # ancienne playlist sans profil
        has = lambda slug, sid: f"/stream/{sid}.ts" in self.get(f"{self.base}/p/{slug}.m3u")[1].decode()  # noqa: E731
        b42 = 2 * OFFSET + 42
        self.assertEqual((has("mix", 42), has("mix", b42)), (True, False))   # rien de mélangeable : profil 1 seul
        status, body = self.send(self.web_base + "/api/providers/2/mix", "PUT", {"mixable": True})
        self.assertEqual((status, body["mixable"]), (200, True))
        self.assertEqual(json.loads(self.get(self.web_base + "/api/providers")[1])["providers"][1]["mixable"], True)
        self.assertEqual((has("mix", 42), has("mix", b42)), (True, True))    # B mélangeable : ajouté au mélange
        cats = ["1", str(2 * OFFSET + 1)]
        for slug, provider, mix in (("seul", "1", False), ("a-mix", "1", True), ("b-mix", "2", True)):
            self.assertEqual(self.send(self.web_base + "/api/playlists", "POST", {
                "slug": slug, "name": slug, "provider": provider, "mix": mix, "category_ids": cats})[0], 201)
        self.assertEqual((has("seul", 42), has("seul", b42)), (True, False))  # « A seul » : jamais B
        self.assertEqual((has("a-mix", 42), has("a-mix", b42)), (True, True))  # A + B (mélangeable)
        self.assertEqual((has("b-mix", 42), has("b-mix", b42)), (False, True))  # A n'est pas mélangeable
        self.assertEqual(self.send(self.web_base + "/api/providers/1/mix", "PUT", {"mixable": True})[0], 200)
        self.assertEqual((has("b-mix", 42), has("b-mix", b42)), (True, True))
        self.assertEqual(self.send(self.web_base + "/api/providers/7/mix", "PUT", {"mixable": True})[0], 404)
        self.send(self.web_base + "/api/providers/1/mix", "PUT", {"mixable": False})
        # Lectures et historique : seulement ceux du profil demandé.
        self.get(f"{self.base}/stream/{2 * OFFSET + 42}.ts")
        self.get(f"{self.base}/stream/42.ts")
        history = lambda q: [e["provider"] for e in json.loads(self.get(self.web_base + "/api/history" + q)[1])["entries"]]
        self.assertEqual(sorted(history("")), ["1", "2"])
        self.assertEqual(history("?profile=2"), ["2"])
        self.send(self.web_base + "/api/history/clear?profile=2", "POST", {})
        self.assertEqual(history(""), ["1"])
        self.assertEqual(json.loads(self.get(self.web_base + "/api/streams?profile=2")[1])["streams"], [])
        # Diagnostic du profil 2 : son abonnement et son catalogue seulement.
        results = self.send(self.web_base + "/api/diagnostic?profile=2", "POST", {})[1]["results"]
        accounts = [r for r in results if r["id"].startswith("account")]
        self.assertEqual(len(accounts), 1)
        live = next(r for r in results if r["id"] == "catalog-live")
        self.assertTrue(live["detail"].startswith("6 chaînes"), live["detail"])

    def test_https_server_info_behind_proxy(self):
        self.create_playlist()
        device = self.send(self.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "mix"})[1]
        from app.xtream_emu import XtreamEmulator
        emu = self.proxy.RequestHandlerClass.emulator
        self.assertIsInstance(emu, XtreamEmulator)
        info = emu.account(device, "tv.example.org", "443", "https")["server_info"]
        self.assertEqual((info["server_protocol"], info["https_port"], info["port"]), ("https", "443", "80"))
        info = emu.account(device, "192.168.1.10", "7777")["server_info"]
        self.assertEqual((info["server_protocol"], info["port"], info["https_port"]), ("http", "7777", ""))

    def test_xtream_emulation_numeric_ids(self):
        """Lecteurs Xtream : identifiants courts (entiers 32 bits) ; les deux formes sont acceptées au retour."""
        from app.providers import PUBLIC_BASE, from_public, to_public
        self.create_playlist()
        device = self.send(self.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "mix"})[1]
        q = {"username": device["username"], "password": device["password"]}
        api = lambda **kw: json.loads(self.get(self.base + "/player_api.php?" + urlencode({**q, **kw}))[1])  # noqa: E731
        live = api(action="get_live_streams")
        ids = [c["stream_id"] for c in live]
        self.assertIn(PUBLIC_BASE + 42, ids)
        self.assertTrue(all(isinstance(i, int) and i < 2 ** 31 for i in ids), ids)
        cats = [c["category_id"] for c in api(action="get_live_categories")]
        self.assertTrue(all(int(c) < 2 ** 31 for c in cats), cats)
        self.assertIn(str(PUBLIC_BASE + 1), cats)
        self.assertTrue(api(action="get_live_streams", category_id=str(PUBLIC_BASE + 1)))
        series = api(action="get_series")
        self.assertIn(PUBLIC_BASE + 900, [s["series_id"] for s in series])
        info = api(action="get_series_info", series_id=str(PUBLIC_BASE + 900))
        first = info["episodes"]["1"][0]
        self.assertEqual(first["id"], str(PUBLIC_BASE + 9001))
        status = self.get(f"{self.base}/series/{device['username']}/{device['password']}/{first['id']}.mp4")
        self.assertEqual(status[0], 502)  # fichier absent chez le faux fournisseur, mais bien routé vers B
        self.assertIn(f"/series/{USERNAME}/{PASSWORD}/9001.mp4", FakeB.paths)
        # Lecture d'une chaîne de B par son identifiant court (et par l'ancien, toujours accepté).
        before = len(FakeB.paths)
        stream = self.get(f"{self.base}/live/{device['username']}/{device['password']}/{PUBLIC_BASE + 42}.ts")
        self.assertEqual(stream[1], b"TS DATA 12345678")
        self.assertIn(f"/live/{USERNAME}/{PASSWORD}/42.ts", FakeB.paths[before:])
        old = self.get(f"{self.base}/live/{device['username']}/{device['password']}/{2 * OFFSET + 42}.ts")
        self.assertEqual(old[1], b"TS DATA 12345678")
        # Conversion : aller-retour, abonnement principal inchangé, identifiants trop grands laissés tels quels.
        self.assertEqual(from_public(to_public(str(9 * OFFSET + 99_999_999))), str(9 * OFFSET + 99_999_999))
        self.assertEqual((to_public("42"), from_public("42")), ("42", "42"))
        self.assertEqual(to_public(str(2 * OFFSET + 10 ** 8)), str(2 * OFFSET + 10 ** 8))

    def test_user_agent_per_provider(self):
        """XTREAM_USER_AGENT_2 : API et flux de l'abonnement 2 avec son user agent, le principal inchangé."""
        cfg = replace(self.stack.config, provider_name="A", provider_user_agents=((2, "TiviMate/5.1.6"),),
                      extra_providers=(("B", f"http://127.0.0.1:{self.server_b.server_address[1]}", USERNAME, PASSWORD),))
        api = make_api(cfg, migrate=False)
        self.assertEqual(api.user_agent_for_url(api.direct_stream(str(2 * OFFSET + 42))), "TiviMate/5.1.6")
        self.assertEqual(api.user_agent_for_url(api.direct_stream("42")), cfg.user_agent)
        self.assertEqual(api.user_agent_for_url("http://ailleurs.invalid/x.ts"), cfg.user_agent)
        FakeB.agents = []
        api.apis[1].catalog(kind="live", refresh=True)
        self.assertTrue(FakeB.agents and set(FakeB.agents) == {"TiviMate/5.1.6"}, FakeB.agents)
        # Flux relayé par le serveur : même user agent.
        proxy = make_server(cfg, host="127.0.0.1", port=0, api=api, store=PlaylistStore(self.tmp.name + "/ua"),
                            epg=make_epg(cfg, api), series=make_series(cfg, api), streams=make_streams(cfg, api),
                            devices=DeviceStore(self.tmp.name + "/ua"))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        FakeB.agents = []
        self.get(f"http://127.0.0.1:{proxy.server_address[1]}/stream/{2 * OFFSET + 42}.ts")
        self.assertEqual(FakeB.agents[-1:], ["TiviMate/5.1.6"])

    def test_user_agent_config_rejects_header_injection(self):
        from app.server import _clean_user_agent
        self.assertEqual(_clean_user_agent("  VLC/3.0  ", "X"), "VLC/3.0")
        for bad in ("a\r\nX-Evil: 1", "a\x7f", "x" * 201):
            with self.assertRaises(ValueError):
                _clean_user_agent(bad, "X")

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


class ProviderCacheIsolationTests(unittest.TestCase):
    """Chaque fournisseur a sa copie disque du catalogue (avant 3.9.0 : un seul fichier partagé)."""

    def api(self, tmp, user):
        from app.server import Config, XtreamAPI
        return XtreamAPI(Config("http://provider.invalid", user, "pw", data_dir=tmp))

    def test_separate_files_and_foreign_copy_ignored(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self.api(tmp, "alice"), self.api(tmp, "bob")
            self.assertNotEqual(a._cache_file("live"), b._cache_file("live"))
            self.assertNotIn("alice", str(a._cache_file("live")))  # identifiant haché, jamais en clair
            a._save("live", [{"stream_id": 1, "name": "Chaîne A", "category_id": "1"}], [], None)
            b._save("live", [{"stream_id": 2, "name": "Chaîne B", "category_id": "1"}], [], None)
            fresh_a, fresh_b = self.api(tmp, "alice"), self.api(tmp, "bob")
            fresh_a._restore("live")
            fresh_b._restore("live")
            self.assertEqual([c["name"] for c in fresh_a.kind_state("live").items], ["Chaîne A"])
            self.assertEqual([c["name"] for c in fresh_b.kind_state("live").items], ["Chaîne B"])
            # Copie d'un autre fournisseur placée sous le mauvais nom : ignorée.
            import shutil
            shutil.copy(a._cache_file("live"), b._cache_file("live"))
            other = self.api(tmp, "bob")
            other._restore("live")
            self.assertIsNone(other.kind_state("live").items)

    def test_migration_of_old_shared_cache(self):
        import tempfile
        from pathlib import Path
        from app.server import migrate_catalog_cache
        with tempfile.TemporaryDirectory() as tmp:
            a = self.api(tmp, "alice")
            a._save("live", [{"stream_id": 1, "name": "Ancien", "category_id": "1"}], [], None)
            old = Path(tmp) / "cache" / "catalog-live.json.gz"
            a._cache_file("live").rename(old)
            migrate_catalog_cache(tmp, [a])                      # un seul fournisseur : renommée
            self.assertFalse(old.exists())
            self.assertTrue(a._cache_file("live").exists())
            a._cache_file("live").rename(old)
            b = self.api(tmp, "bob")
            migrate_catalog_cache(tmp, [a, b])                   # plusieurs : à qui ? supprimée
            self.assertFalse(old.exists())
            self.assertFalse(a._cache_file("live").exists())
            self.assertFalse(b._cache_file("live").exists())
