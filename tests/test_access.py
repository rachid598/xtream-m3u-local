"""Accès distant sécurisé : adresse réelle, local/distant, droits par appareil, essais limités, journaux."""
import json
import unittest
from urllib.parse import urlencode

from app import access
from app.server import mask_path

from .helpers import StackTestCase

PROXY = access.parse_networks("127.0.0.1/32")
REMOTE = {"X-Forwarded-For": "203.0.113.7"}     # adresse publique (documentation), via le proxy local
LAN = {"X-Forwarded-For": "192.168.1.20"}


class AddressTests(unittest.TestCase):
    def test_local_and_remote(self):
        self.assertTrue(access.is_local("192.168.0.10"))
        self.assertTrue(access.is_local("127.0.0.1"))
        self.assertTrue(access.is_local("172.18.0.2"))   # réseau Docker
        self.assertFalse(access.is_local("100.101.102.103"))  # Tailscale : distant
        self.assertFalse(access.is_local("203.0.113.7"))
        self.assertFalse(access.is_local("pas une adresse"))

    def test_client_address_without_proxy(self):
        self.assertEqual(access.client_address("192.168.0.10", {}, ()), ("192.168.0.10", False))
        self.assertEqual(access.client_address("203.0.113.7", {}, ()), ("203.0.113.7", True))

    def test_forward_headers_from_undeclared_proxy_are_remote(self):
        # Proxy oublié dans TRUSTED_PROXIES : jamais « local » par erreur.
        self.assertEqual(access.client_address("172.18.0.5", {"X-Forwarded-For": "192.168.1.20"}, ()),
                         ("172.18.0.5", True))

    def test_trusted_proxy_chain(self):
        trusted = access.parse_networks("172.18.0.0/16, 10.0.0.1")
        headers = {"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}
        self.assertEqual(access.client_address("172.18.0.5", headers, trusted), ("203.0.113.7", True))
        self.assertEqual(access.client_address("172.18.0.5", {"X-Forwarded-For": "192.168.1.20"}, trusted),
                         ("192.168.1.20", False))
        self.assertEqual(access.client_address("172.18.0.5", {"CF-Connecting-IP": "198.51.100.4"}, trusted),
                         ("198.51.100.4", True))
        self.assertEqual(access.client_address("172.18.0.5", {"X-Forwarded-For": "n'importe quoi"}, trusted),
                         ("172.18.0.5", True))
        with self.assertRaises(ValueError):
            access.parse_networks("pas-un-réseau")

    def test_failure_guard(self):
        guard = access.FailureGuard(max_failures=3, window=60, block=60)
        self.assertFalse(guard.fail("1.2.3.4"))
        self.assertFalse(guard.fail("1.2.3.4"))
        self.assertTrue(guard.fail("1.2.3.4"))
        self.assertGreater(guard.blocked_for("1.2.3.4"), 0)
        self.assertEqual(guard.blocked_for("5.6.7.8"), 0)

    def test_mask_path(self):
        self.assertEqual(mask_path("/live/salon/abc123/42.ts"), "/live/salon/***/42.ts")
        self.assertEqual(mask_path("/timeshift/salon/abc123/60/2026-11-18:20-00/42.ts"),
                         "/timeshift/salon/***/60/2026-11-18:20-00/42.ts")
        self.assertEqual(mask_path("/p/salon.m3u"), "/p/salon.m3u")


class RemoteAccessTests(StackTestCase):
    overrides = {"trusted_proxies": PROXY}

    def setUp(self):
        super().setUp()
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "series_category_ids": ["1"]})
        self.create({"slug": "autre", "name": "Autre", "category_ids": ["2"]})
        status, device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "Sœur", "playlist": "salon"})
        self.assertEqual(status, 201, device)
        self.device = device

    def remote_stack(self, **extra):
        return self.make_stack(remote_access=True, trusted_proxies=PROXY, **extra)

    def test_remote_refused_by_default_local_unchanged(self):
        status, body, _ = self.get(self.stack.proxy_base + "/p/salon.m3u", REMOTE)
        self.assertEqual(status, 403)
        self.assertIn("Accès distant désactivé", body.decode())
        self.assertEqual(self.get(self.stack.proxy_base + "/p/salon.m3u", LAN)[0], 200)
        self.assertEqual(self.get(self.stack.proxy_base + "/p/salon.m3u")[0], 200)
        self.assertEqual(self.get(self.stack.proxy_base + "/health", REMOTE)[0], 200)

    def test_remote_requires_device_code_and_respects_its_playlist(self):
        stack = self.remote_stack()
        token = "?" + urlencode({"token": self.device["password"]})
        base = stack.proxy_base
        self.assertEqual(self.get(base + "/p/salon.m3u", REMOTE)[0], 403)           # pas de code
        status, body, _ = self.get(base + "/p/salon.m3u" + token, REMOTE)
        self.assertEqual(status, 200)
        self.assertIn("France Info HD", body.decode())
        self.assertEqual(self.get(base + "/p/autre.m3u" + token, REMOTE)[0], 404)    # playlist d'un autre
        full = self.get(base + "/playlist.m3u" + token, REMOTE)
        self.assertEqual(full[0], 200)
        self.assertNotIn("Musique", full[1].decode())                                 # sa playlist, pas tout
        self.assertEqual(self.get(base + "/playlist.m3u" + token + "&mode=direct", REMOTE)[0], 403)
        self.assertEqual(self.get(base + "/stream/43.ts" + token, REMOTE)[0], 404)    # chaîne hors playlist
        self.assertEqual(self.get(base + "/hls/43.m3u8" + token, REMOTE)[0], 404)
        self.assertEqual(self.get(base + "/timeshift/43.ts" + token + "&start=1700000000&duration=30",
                                  REMOTE)[0], 404)
        self.assertEqual(self.get(base + "/movie/501.mkv" + token, REMOTE)[0], 404)  # aucun film dans « salon »

    def test_device_rights_also_apply_locally(self):
        token = "?" + urlencode({"token": self.device["password"]})
        self.assertEqual(self.get(self.stack.proxy_base + "/p/autre.m3u" + token)[0], 404)
        self.assertEqual(self.get(self.stack.proxy_base + "/stream/43.ts" + token)[0], 404)
        self.assertEqual(self.get(self.stack.proxy_base + "/p/autre.m3u")[0], 200)  # sans code, réseau local : inchangé

    def test_xtream_series_episode_rights(self):
        user, pw = self.device["username"], self.device["password"]
        base = self.stack.proxy_base
        q = urlencode({"username": user, "password": pw, "action": "get_series_info", "series_id": "900"})
        json.loads(self.get(base + "/player_api.php?" + q)[1])
        self.stack.series.episodes("900", timeout=10)
        self.stack.series.episodes("901", timeout=10)
        self.assertEqual(self.get(f"{base}/series/{user}/{pw}/9101.avi")[0], 404)   # série 901 hors playlist
        status, body, _ = self.get(f"{base}/series/{user}/{pw}/9000.mp4")
        self.assertEqual((status, body), (200, b"EPISODE"))

    def test_brute_force_blocked(self):
        stack = self.remote_stack()
        for _ in range(10):
            self.assertEqual(self.get(stack.proxy_base + "/p/salon.m3u?token=mauvais", REMOTE)[0], 403)
        status, body, headers = self.get(stack.proxy_base + "/p/salon.m3u?" + urlencode({"token": self.device["password"]}), REMOTE)
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers)
        # Une autre adresse n'est pas concernée.
        other = {"X-Forwarded-For": "198.51.100.9"}
        self.assertEqual(self.get(stack.proxy_base + "/p/salon.m3u?" + urlencode({"token": self.device["password"]}),
                                  other)[0], 200)

    def test_web_interface_local_only_by_default(self):
        status, body, _ = self.get(self.stack.web_base + "/api/status", REMOTE)
        self.assertEqual(status, 403)
        self.assertIn("réseau local", body.decode())
        self.assertEqual(self.get(self.stack.web_base + "/api/status", LAN)[0], 200)


class RemoteDiagnosticTests(unittest.TestCase):
    def test_remote_check(self):
        from dataclasses import replace
        from app.diagnostic import remote_check
        from app.server import Config

        class Devices:
            def __init__(self, items):
                self.items = items

            def list(self):
                return self.items

        base = Config("http://x.invalid", "u", "p")
        self.assertEqual(remote_check(base, None)["status"], "ok")
        opened = replace(base, remote_access=True)
        self.assertEqual(remote_check(opened, Devices([]))["status"], "error")
        warn = remote_check(opened, Devices([{"playlist": ""}]))
        self.assertEqual(warn["status"], "warn")
        self.assertIn("sans playlist", warn["detail"])
        self.assertIn("https", warn["detail"])
        good = replace(opened, public_base_url="https://tv.example.org", trusted_proxies=PROXY)
        self.assertEqual(remote_check(good, Devices([{"playlist": "salon"}]))["status"], "ok")


if __name__ == "__main__":
    unittest.main()
