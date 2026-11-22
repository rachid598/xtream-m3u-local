"""Tunnel Cloudflare piloté depuis l'interface (faux cloudflared : aucun appel réseau)."""
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from app import tunnel

from .helpers import StackTestCase

FAKE = str(Path(__file__).with_name("fake_cloudflared.py"))


def wait(cond, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class TunnelTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.tunnel = tunnel.Tunnel(self.tmp.name, self.stack.proxy.server_address[1], binary=FAKE)
        tunnel.install(self.tunnel)
        self.addCleanup(tunnel.install, None)
        self.addCleanup(self.tunnel.stop, disable=False)
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"]})
        self.device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "salon"})[1]

    def api(self, action, body=None):
        if action == "status":
            return 200, json.loads(self.get(self.stack.web_base + "/api/tunnel")[1])
        return self.send(self.stack.web_base + f"/api/tunnel/{action}", "POST", body if body is not None else {})

    def test_click_connect_flow(self):
        status, data = self.api("status")
        self.assertEqual((data["available"], data["logged_in"], data["tunnel_id"]), (True, False, ""))
        self.assertEqual(self.api("setup", {"hostname": "tv.mondomaine.fr"})[0], 400)    # pas encore connecté
        status, data = self.api("login")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["login_url"].startswith("https://dash.cloudflare.com/argotunnel?"))
        self.assertTrue(wait(lambda: self.api("status")[1]["logged_in"]))                 # « Authorize » cliqué
        self.assertEqual(oct(self.tunnel.cert.stat().st_mode & 0o777), "0o600")
        status, data = self.api("setup", {"hostname": "https://TV.MonDomaine.fr/"})
        self.assertEqual(status, 200, data)
        self.assertEqual((data["hostname"], data["tunnel_id"], data["enabled"]),
                         ("tv.mondomaine.fr", "6ff42ae2-765d-4adf-8112-31c55c1551ef", True))
        self.assertTrue(wait(lambda: self.api("status")[1]["connected"]))
        calls = (Path(self.tmp.name) / "cloudflared" / "calls.log").read_text()
        self.assertIn("tunnel route dns --overwrite-dns 6ff42ae2-765d-4adf-8112-31c55c1551ef tv.mondomaine.fr", calls)
        config = (Path(self.tmp.name) / "cloudflared" / "config.yml").read_text()
        self.assertIn(f"service: http://127.0.0.1:{self.stack.proxy.server_address[1]}", config)
        self.assertNotIn("secret", json.dumps(data))                                       # aucun secret renvoyé
        # Arrêt, redémarrage, suppression.
        self.assertFalse(self.api("stop")[1]["running"])
        self.assertTrue(wait(lambda: self.api("start")[1]["running"] or self.api("status")[1]["running"]))
        self.assertEqual(self.api("remove")[1]["tunnel_id"], "")
        self.assertIn("tunnel delete -f", (Path(self.tmp.name) / "cloudflared" / "calls.log").read_text())

    def test_wrong_domain_explained(self):
        self.tunnel.cert.parent.mkdir(parents=True)
        self.tunnel.cert.write_text("x")
        status, data = self.api("setup", {"hostname": "tv.refuse.test"})
        self.assertEqual(status, 400)
        self.assertIn("non publiée", data["error"])
        self.assertIn("zone", data["error"])
        self.assertEqual(self.api("setup", {"hostname": "pas un domaine"})[0], 400)

    def test_requests_through_tunnel(self):
        """Par le tunnel : visiteur distant (code obligatoire), sa vraie adresse, liens en https."""
        state = {"tunnel_id": "6ff42ae2-765d-4adf-8112-31c55c1551ef", "hostname": "tv.mondomaine.fr", "enabled": True}
        self.tunnel._save_state(state)
        via = {"Host": "tv.mondomaine.fr", "CF-Connecting-IP": "203.0.113.9", "X-Forwarded-For": "203.0.113.9",
               "X-Forwarded-Proto": "https"}
        base = self.stack.proxy_base
        self.assertEqual(self.get(base + "/p/salon.m3u", via)[0], 404)                  # sans code : aucun indice
        status, body, _ = self.get(base + "/p/salon.m3u?token=" + self.device["password"], via)
        self.assertEqual(status, 200)
        self.assertIn("https://tv.mondomaine.fr/stream/42.ts", body.decode())
        q = f"username={self.device['username']}&password={self.device['password']}"
        info = json.loads(self.get(base + "/player_api.php?" + q, via)[1])["server_info"]
        self.assertEqual((info["server_protocol"], info["url"], info["https_port"]), ("https", "tv.mondomaine.fr", "443"))
        self.assertEqual(self.get(base + "/p/salon.m3u")[0], 200)                       # réseau local : inchangé
        # Interface de gestion : jamais par le tunnel (visiteur distant).
        self.assertEqual(self.get(self.stack.web_base + "/api/status", via)[0], 403)

    def test_missing_binary(self):
        with mock.patch.dict(os.environ, {"CLOUDFLARED_BIN": ""}), mock.patch("shutil.which", return_value=None):
            bare = tunnel.Tunnel(self.tmp.name, 7777)
        self.assertFalse(bare.status()["available"])
        with self.assertRaises(tunnel.TunnelError):
            bare.login()


if __name__ == "__main__":
    unittest.main()


class DeviceLinksTests(StackTestCase):
    """Appareil « externe » : l'interface affiche ses liens avec l'adresse publique."""

    def test_links_mode_and_external_base(self):
        status, device = self.send(self.stack.web_base + "/api/devices", "POST",
                                   {"name": "TV maman", "links": "external"})
        self.assertEqual((status, device["links"]), (201, "external"))
        self.assertEqual(self.send(self.stack.web_base + "/api/devices", "POST", {"name": "X", "links": "ailleurs"})[0], 400)
        status, device = self.send(self.stack.web_base + f"/api/devices/{device['id']}", "PUT", {"links": "lan"})
        self.assertEqual(device["links"], "lan")
        self.assertEqual(json.loads(self.get(self.stack.web_base + "/api/status")[1])["external_base"], "")
        current = tunnel.Tunnel(self.tmp.name, 7777, binary=FAKE)
        current._save_state({"tunnel_id": "6ff42ae2-765d-4adf-8112-31c55c1551ef", "hostname": "tv.mondomaine.fr",
                             "enabled": True})
        tunnel.install(current)
        self.addCleanup(tunnel.install, None)
        self.assertEqual(json.loads(self.get(self.stack.web_base + "/api/status")[1])["external_base"],
                         "https://tv.mondomaine.fr")


class DeviceLiveTests(StackTestCase):
    """Fiche d'appareil « En lecture » : la lecture en cours porte l'identifiant de l'appareil."""

    def test_stream_carries_device_id(self):
        import threading
        from urllib.request import urlopen
        device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "Chambre"})[1]   # sans playlist
        url = f"{self.stack.proxy_base}/stream/60.ts?token={device['password']}"   # flux qui dure 20 s
        reader = threading.Thread(target=lambda: urlopen(url, timeout=30).read(4096), daemon=True)
        reader.start()
        streams = []
        for _ in range(50):
            streams = json.loads(self.get(self.stack.web_base + "/api/streams")[1])["streams"]
            if streams:
                break
            time.sleep(0.1)
        self.assertTrue(streams)
        self.assertEqual((streams[0]["device_id"], streams[0]["device"]), (device["id"], "Chambre"))
        self.assertIn("channel", streams[0])
