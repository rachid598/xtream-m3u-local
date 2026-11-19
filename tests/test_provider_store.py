"""Abonnements ajoutés depuis l'interface (providers.json, rechargement à chaud) et LOCAL_NETWORKS."""
import json
import os
import stat
import tempfile
import threading
import unittest
from dataclasses import replace
from urllib.parse import urlsplit

from app import access, provider_store
from app.custom import PlaylistStore
from app.devices import DeviceStore
from app.provider_store import InvalidProvider, ProviderStore, public
from app.providers import OFFSET, ApiHolder
from app.server import Config, make_api, make_epg, make_series, make_server, make_streams, redact
from app.web import make_web_server

from .fake_xtream import PASSWORD, USERNAME
from .helpers import StackTestCase


class StoreTests(unittest.TestCase):
    def test_add_update_delete_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProviderStore(tmp)
            first = store.add(name="Sœur", url="http://a.invalid", username="alice", password="secret1", reserved={1, 2})
            self.assertEqual(first["index"], 3)                      # 2 pris par XTREAM_URL_2
            second = store.add(name="Maman", url="http://b.invalid", username="bob", password="secret2", reserved={1})
            self.assertEqual(second["index"], 2)
            self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
            store.update(3, name="Ma sœur", password="")             # mot de passe vide : inchangé
            entry = next(p for p in store.list() if p["index"] == 3)
            self.assertEqual((entry["name"], entry["password"]), ("Ma sœur", "secret1"))
            self.assertNotIn("secret1", json.dumps(public(entry)))
            self.assertNotIn("alice", json.dumps(public(entry)))
            self.assertEqual(sorted(store.credentials()), [("alice", "secret1"), ("bob", "secret2")])
            self.assertTrue(store.delete(2))
            self.assertFalse(store.delete(2))
            with self.assertRaises(InvalidProvider):
                store.add(name="", url="http://c.invalid", username="x", password="y", reserved={1})
            for n in range(7):
                store.add(name=f"P{n}", url=f"http://p{n}.invalid", username="u", password="p", reserved={1})
            with self.assertRaises(InvalidProvider):
                store.add(name="Trop", url="http://z.invalid", username="u", password="p", reserved={1})

    def test_redaction_includes_interface_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ProviderStore(tmp)
            store.add(name="Sœur", url="http://a.invalid", username="alice", password="motdepasse", reserved={1})
            provider_store.install(store)
            try:
                text = redact("http://a.invalid/live/alice/motdepasse/1.ts", Config("http://x.invalid", "u", "p"))
            finally:
                provider_store.install(None)
            self.assertNotIn("motdepasse", text)


class LocalNetworksTests(unittest.TestCase):
    def test_tailscale_local_when_declared(self):
        tailscale = access.parse_networks("100.64.0.0/10")
        self.assertEqual(access.client_address("100.68.27.125", {}, ()), ("100.68.27.125", True))
        self.assertEqual(access.client_address("100.68.27.125", {}, (), tailscale), ("100.68.27.125", False))


class ProviderApiTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.data = tempfile.mkdtemp(dir=self.tmp.name)
        self.store = ProviderStore(self.data)
        provider_store.install(self.store)
        cfg = replace(self.stack.config, data_dir=self.data, provider_name="Moi")
        self.cfg = cfg
        self.api = ApiHolder(make_api(cfg, self.store))
        streams = make_streams(cfg, self.api)
        series = make_series(cfg, self.api)
        playlists = PlaylistStore(self.data)
        devices = DeviceStore(self.data)
        self.proxy = make_server(cfg, host="127.0.0.1", port=0, api=self.api, store=playlists,
                                 epg=make_epg(cfg, self.api), series=series, streams=streams, devices=devices)
        self.web = make_web_server(cfg, self.api, playlists, host="127.0.0.1", port=0, series=series,
                                   streams=streams, devices=devices)
        for srv in (self.proxy, self.web):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        self.web_base = f"http://127.0.0.1:{self.web.server_address[1]}"
        # Même faux fournisseur, sous un autre nom d'hôte : un 2e abonnement distinct.
        self.other_url = "http://localhost:%d" % urlsplit(self.provider_url).port

    def tearDown(self):
        provider_store.install(None)
        for srv in (self.web, self.proxy):
            srv.shutdown()
            srv.server_close()
        super().tearDown()

    def test_add_edit_delete_live(self):
        listing = json.loads(self.get(self.web_base + "/api/providers")[1])
        self.assertTrue(listing["editable"])
        self.assertEqual([(p["index"], p["source"]) for p in listing["providers"]], [(1, "configuration")])
        # Identifiants refusés par le fournisseur : rien d'enregistré.
        status, body = self.send(self.web_base + "/api/providers", "POST",
                                 {"name": "Sœur", "url": self.other_url, "username": USERNAME, "password": "faux"})
        self.assertEqual(status, 400)
        self.assertIn("Connexion refusée", body["error"])
        self.assertEqual(self.store.list(), [])
        # Doublon de l'abonnement principal.
        status, body = self.send(self.web_base + "/api/providers", "POST",
                                 {"name": "Bis", "url": self.provider_url, "username": USERNAME, "password": PASSWORD})
        self.assertEqual((status, "déjà configuré" in body["error"]), (400, True))
        # Ajout valide : actif tout de suite, sans redémarrer.
        status, body = self.send(self.web_base + "/api/providers", "POST",
                                 {"name": "Sœur", "url": self.other_url, "username": USERNAME, "password": PASSWORD})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["index"], 2)
        self.assertNotIn(PASSWORD, json.dumps(body))
        profiles = json.loads(self.get(self.web_base + "/api/status")[1])["profiles"]
        self.assertEqual(profiles, [{"id": "1", "name": "Moi"}, {"id": "2", "name": "Sœur"}])
        catalog = json.loads(self.get(self.web_base + "/api/catalog")[1])
        self.assertIn(str(2 * OFFSET + 42), [c["stream_id"] for c in catalog["channels"]])
        self.assertEqual(self.get(f"{self.base}/stream/{2 * OFFSET + 42}.ts")[0], 200)
        listing = json.loads(self.get(self.web_base + "/api/providers")[1])
        self.assertNotIn(PASSWORD, json.dumps(listing).replace('"password_set"', ""))
        # Renommage (sans nouveau test de connexion), puis suppression.
        status, body = self.send(self.web_base + "/api/providers/2", "PUT", {"name": "Ma sœur", "password": ""})
        self.assertEqual((status, body["name"]), (200, "Ma sœur"))
        profiles = json.loads(self.get(self.web_base + "/api/status")[1])["profiles"]
        self.assertEqual(profiles[1]["name"], "Ma sœur")
        self.assertEqual(self.send(self.web_base + "/api/providers/2", "DELETE")[0], 200)
        self.assertEqual(len(json.loads(self.get(self.web_base + "/api/status")[1])["profiles"]), 1)
        self.assertEqual(self.send(self.web_base + "/api/providers/2", "DELETE")[0], 404)

    def test_not_editable_without_holder(self):
        status, body = self.send(self.stack.web_base + "/api/providers", "POST",
                                 {"name": "Sœur", "url": self.other_url, "username": USERNAME, "password": PASSWORD})
        self.assertEqual(status, 409)
        self.assertFalse(json.loads(self.get(self.stack.web_base + "/api/providers")[1])["editable"])


if __name__ == "__main__":
    unittest.main()
