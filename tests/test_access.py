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
        self.assertEqual((status, body), (404, b"Introuvable\n"))   # hors de la maison : aucun indice
        self.assertEqual(self.get(self.stack.proxy_base + "/p/salon.m3u", LAN)[0], 200)
        self.assertEqual(self.get(self.stack.proxy_base + "/p/salon.m3u")[0], 200)
        self.assertEqual(self.get(self.stack.proxy_base + "/health", REMOTE)[0], 404)
        self.assertEqual(self.get(self.stack.proxy_base + "/health")[0], 200)

    def test_remote_requires_device_code_and_respects_its_playlist(self):
        stack = self.remote_stack()
        token = "?" + urlencode({"token": self.device["password"]})
        base = stack.proxy_base
        self.assertEqual(self.get(base + "/p/salon.m3u", REMOTE)[0], 404)           # pas de code : aucun indice
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
            self.assertEqual(self.get(stack.proxy_base + "/p/salon.m3u?token=mauvais", REMOTE)[0], 404)
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


class TailscaleSettingTests(StackTestCase):
    """Case « Tailscale = mon réseau local » (Système → Accès à distance)."""
    overrides = {"trusted_proxies": PROXY}

    def setUp(self):
        super().setUp()
        from app import settings
        self.settings = settings
        settings.install(settings.Settings(self.tmp.name))
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"]})

    def tearDown(self):
        self.settings.install(None)
        super().tearDown()

    def test_toggle(self):
        tailscale = {"X-Forwarded-For": "100.68.27.125"}
        self.assertEqual(self.get(self.stack.web_base + "/api/status", tailscale)[0], 403)
        self.assertEqual(self.get(self.stack.proxy_base + "/p/salon.m3u", tailscale)[0], 404)
        status, data = self.send(self.stack.web_base + "/api/settings", "PUT", {"tailscale_local": True, "inconnu": 1})
        self.assertEqual((status, data["tailscale_local"], "inconnu" in data), (200, True, False))
        self.assertEqual(self.get(self.stack.web_base + "/api/status", tailscale)[0], 200)
        self.assertEqual(self.get(self.stack.proxy_base + "/p/salon.m3u", tailscale)[0], 200)
        info = json.loads(self.get(self.stack.web_base + "/api/settings", tailscale)[1])
        self.assertEqual((info["client_ip"], info["client_tailscale"]), ("100.68.27.125", True))
        self.assertEqual(self.get(self.stack.web_base + "/api/status", REMOTE)[0], 403)  # Internet : inchangé
        self.send(self.stack.web_base + "/api/settings", "PUT", {"tailscale_local": False})
        self.assertEqual(self.get(self.stack.web_base + "/api/status", tailscale)[0], 403)


class NoHintsOutsideTests(StackTestCase):
    """Hors de la maison, sans bons identifiants : rien ne révèle le serveur (version, adresses, paramètres)."""
    overrides = {"trusted_proxies": PROXY, "remote_access": True}

    def test_generic_answers(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"]})
        device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "salon"})[1]
        base = self.stack.proxy_base
        for path in ("/", "/health", "/playlist.m3u", "/p/salon.m3u", "/p/salon.m3u?token=faux", "/stream/42.ts",
                     "/player_api.php?username=x&password=y", "/live/x/y/42.ts", "/discover.json",
                     "/hdhr/x/y/discover.json", "/xmltv.php", "/inconnu"):
            status, body, _ = self.get(base + path, REMOTE)
            self.assertEqual((status, body), (404, b"Introuvable\n"), path)
        for word in (b"Xtream", b"token", b"3."):
            self.assertNotIn(word, self.get(base + "/", REMOTE)[1])
        # Adresse complète avec le bon code : fonctionne.
        ok = self.get(f"{base}/p/salon.m3u?token={device['password']}", REMOTE)
        self.assertEqual(ok[0], 200)
        q = urlencode({"username": device["username"], "password": device["password"]})
        self.assertEqual(json.loads(self.get(f"{base}/player_api.php?{q}", REMOTE)[1])["user_info"]["auth"], 1)
        # Chez soi : la page d'accueil et les messages restent explicites.
        self.assertIn(b"Xtream M3U Local", self.get(base + "/")[1])


class DnsRebindingTests(StackTestCase):
    """Un site piégé qui fait pointer son nom vers le serveur ne lit ni l'interface ni les liens « direct »."""

    def test_host_rules(self):
        allowed = access.parse_hosts("tv.exemple.fr, .maison.fr")
        for host in ("192.168.0.10:7778", "[::1]:7778", "localhost", "tower", "tower.local:7778", "nas.lan",
                     "x.home.arpa", "box.tail1234.ts.net", "tv.exemple.fr", "a.maison.fr", ""):
            self.assertTrue(access.host_allowed(host, allowed), host)
        for host in ("evil.example.com", "tv.exemple.fr.evil.com", "maison.fr.evil.com", "exemple.fr"):
            self.assertFalse(access.host_allowed(host, allowed), host)
        self.assertTrue(access.host_allowed("evil.example.com", via_proxy=True))
        with self.assertRaises(ValueError):
            access.parse_hosts("pas un nom/")

    def test_web_and_direct_mode(self):
        evil = {"Host": "evil.example.com"}
        status, body, _ = self.get(self.stack.web_base + "/api/devices", evil)
        self.assertEqual(status, 403)
        self.assertIn("Nom d'hôte refusé", body.decode())
        self.assertEqual(self.get(self.stack.web_base + "/api/status")[0], 200)              # 127.0.0.1 : accepté
        self.assertEqual(self.get(self.stack.proxy_base + "/playlist.m3u?mode=direct", evil)[0], 403)
        self.assertEqual(self.get(self.stack.proxy_base + "/playlist.m3u", evil)[0], 200)    # liens sans secret
        stack = self.make_stack(allowed_hosts=access.parse_hosts("evil.example.com"))
        self.assertEqual(self.get(stack.web_base + "/api/status", evil)[0], 200)
