"""Tuner HDHomeRun émulé (Plex, Jellyfin, Emby)."""
import json
import unittest

from app.server import mask_path

from .helpers import StackTestCase


class HdhrTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "item_numbers": {"42": 5}})
        status, self.device = self.send(self.stack.web_base + "/api/devices", "POST",
                                        {"name": "Plex", "playlist": "salon"})
        self.assertEqual(status, 201)
        self.base = f"{self.stack.proxy_base}/hdhr/{self.device['username']}/{self.device['password']}"

    def json(self, url):
        status, body, _ = self.get(url)
        self.assertEqual(status, 200, body[:200])
        return json.loads(body)

    def test_path_tuner(self):
        info = self.json(self.base + "/discover.json")
        self.assertEqual(info["BaseURL"], self.base)
        self.assertEqual(info["LineupURL"], self.base + "/lineup.json")
        self.assertEqual(info["TunerCount"], 1)                     # abonnement à 1 connexion
        self.assertRegex(info["DeviceID"], r"^[0-9A-F]{8}$")
        self.assertEqual(self.json(self.base + "/")["DeviceID"], info["DeviceID"])
        lineup = self.json(self.base + "/lineup.json")
        self.assertEqual([(c["GuideNumber"], c["GuideName"]) for c in lineup], [("5", "France Info HD"), ("2", "Sport 1")])
        url = lineup[0]["URL"]
        self.assertTrue(url.endswith(f"/live/{self.device['username']}/{self.device['password']}/42.ts"), url)
        self.assertEqual(self.get(url)[1], b"TS DATA 12345678")    # lecture avec les droits de l'appareil
        self.assertEqual(self.json(self.base + "/lineup_status.json")["ScanPossible"], 1)
        self.assertEqual(self.get(self.base + "/lineup.post")[0], 200)
        status, body, _ = self.get(self.base + "/device.xml")
        self.assertEqual(status, 200)
        self.assertIn(b"<friendlyName>Xtream M3U Local (Plex)</friendlyName>", body)
        status, body, _ = self.get(self.base + "/guide.xml")
        self.assertEqual(status, 200)
        self.assertIn(b"<tv", body)

    def test_refused_and_masked(self):
        bad = f"{self.stack.proxy_base}/hdhr/{self.device['username']}/faux/discover.json"
        self.assertEqual(self.get(bad)[0], 403)
        self.assertEqual(mask_path(f"/hdhr/plex/{self.device['password']}/lineup.json"), "/hdhr/plex/***/lineup.json")
        self.assertEqual(mask_path("/hdhr/plex/secret"), "/hdhr/plex/***")

    def test_root_tuner_by_ip(self):
        root = self.stack.proxy_base
        self.assertEqual(self.get(root + "/discover.json")[0], 404)   # aucun appareil pour 127.0.0.1
        self.send(self.stack.web_base + f"/api/devices/{self.device['id']}", "PUT", {"ip": "127.0.0.1"})
        info = self.json(root + "/discover.json")
        self.assertEqual(info["BaseURL"], root)
        self.assertEqual(len(self.json(root + "/lineup.json")), 2)
        # Hors du réseau local : jamais sans code (le tuner avec code reste possible).
        from app.access import parse_networks
        remote = self.make_stack(remote_access=True, trusted_proxies=parse_networks("127.0.0.1/32"))
        outside = {"X-Forwarded-For": "203.0.113.7"}
        self.assertEqual(self.get(remote.proxy_base + "/discover.json", outside)[0], 404)
        path = f"/hdhr/{self.device['username']}/{self.device['password']}/discover.json"
        self.assertEqual(self.get(remote.proxy_base + path, outside)[0], 200)


if __name__ == "__main__":
    unittest.main()
