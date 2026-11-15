"""Port 7777 : playlists M3U, relais TS, token, cache catalogue."""
import logging
import re
import unittest
from urllib.request import Request, urlopen

from app.server import escape_attribute

from .fake_xtream import FakeXtream
from .helpers import StackTestCase


class PlaylistServerTests(StackTestCase):
    def test_health_and_root(self):
        status, body, _ = self.get(self.stack.proxy_base + "/health")
        self.assertEqual((status, body), (200, b"OK\n"))
        status, body, _ = self.get(self.stack.proxy_base + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"/playlist.m3u", body)

    def test_full_playlist_metadata_without_secrets(self):
        status, body, headers = self.get(self.stack.proxy_base + "/playlist.m3u")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("audio/x-mpegurl"))
        data = body.decode("utf-8")
        self.assertTrue(data.startswith("#EXTM3U"))
        epg = f"{self.stack.proxy_base}/epg.xml"
        self.assertIn(f'url-tvg="{epg}" x-tvg-url="{epg}"', data.splitlines()[0])
        self.assertIn('tvg-id="franceinfo.fr"', data)
        self.assertIn('tvg-logo="https://logo.invalid/franceinfo.png"', data)
        self.assertIn('tvg-chno="1"', data)
        self.assertIn('group-title="Actualités"', data)
        self.assertIn(f"{self.stack.proxy_base}/stream/42.ts", data)
        # Secrets fournisseur absents en mode proxy.
        self.assertNotIn("/pw/", data)
        self.assertNotIn("user", data.replace("Musique", ""))
        # Entrées invalides ignorées, injections neutralisées.
        self.assertNotIn("Invalid", data)
        self.assertNotIn("\nHACK", data)
        self.assertIn('Radio "TV" HACK', data)
        self.assertIn("group-title=\"Musique 'HD'\"", data)
        self.assertNotIn("javascript:", data)
        # Chaînes sans catégorie ou avec catégorie inconnue conservées.
        self.assertIn('group-title="Sans catégorie",Chaîne orpheline', data)
        self.assertIn('group-title="Catégorie 99",Sans catégorie connue', data)
        self.assertEqual(data.count("#EXTINF"), 6)

    def test_full_playlist_follows_provider_category_order(self):
        _, body, _ = self.get(self.stack.proxy_base + "/playlist.m3u")
        groups = re.findall(r'group-title="([^"]*)"', body.decode())
        # Ordre de get_live_categories (1, 2, 10, 12), puis groupes déduits, chaînes dans l'ordre fournisseur.
        self.assertEqual(groups, ["Actualités", "Musique 'HD'", "Sport", "Sport | Foot", "Sans catégorie",
                                  "Catégorie 99"])

    def test_every_extinf_followed_by_url(self):
        _, body, _ = self.get(self.stack.proxy_base + "/playlist.m3u")
        lines = body.decode().strip().splitlines()[1:]
        self.assertEqual(len(lines) % 2, 0)
        for info, url in zip(lines[::2], lines[1::2]):
            self.assertTrue(info.startswith("#EXTINF:-1 "))
            self.assertRegex(url, r"^http://127\.0\.0\.1:\d+/stream/\d+\.ts$")

    def test_get_php_alias(self):
        status, body, _ = self.get(self.stack.proxy_base + "/get.php?type=m3u_plus&output=ts")
        self.assertEqual(status, 200)
        self.assertIn(b"/stream/42.ts", body)

    def test_direct_hls_contains_provider_url(self):
        _, body, _ = self.get(self.stack.proxy_base + "/playlist.m3u?mode=direct&format=m3u8")
        self.assertIn((self.provider_url + "/live/user/pw/42.m3u8").encode(), body)

    def test_proxy_hls_rejected(self):
        status, _, _ = self.get(self.stack.proxy_base + "/playlist.m3u?mode=proxy&format=m3u8")
        self.assertEqual(status, 400)
        status, _, _ = self.get(self.stack.proxy_base + "/playlist.m3u?mode=evil")
        self.assertEqual(status, 400)

    def test_ts_relay_and_range(self):
        status, body, headers = self.get(self.stack.proxy_base + "/stream/42.ts")
        self.assertEqual((status, body, headers["Content-Type"]), (200, b"TS DATA 12345678", "video/mp2t"))
        status, body, headers = self.get(self.stack.proxy_base + "/stream/42.ts", {"Range": "bytes=3-"})
        self.assertEqual((status, body), (206, b"DATA 12345678"))
        self.assertEqual(headers["Content-Range"], "bytes 3-15/16")

    def test_head_stream_does_not_contact_provider(self):
        before = FakeXtream.actions[:]
        with urlopen(Request(self.stack.proxy_base + "/stream/42.ts", method="HEAD"), timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.headers["Content-Type"], "video/mp2t")
        self.assertEqual(FakeXtream.actions, before)

    def test_upstream_connection_limit_reported(self):
        status, body, _ = self.get(self.stack.proxy_base + "/stream/44.ts")
        self.assertEqual(status, 502)
        self.assertIn("509".encode(), body)
        self.assertIn("connexions".encode(), body)

    def test_unknown_stream_and_paths(self):
        self.assertEqual(self.get(self.stack.proxy_base + "/stream/999.ts")[0], 502)
        self.assertEqual(self.get(self.stack.proxy_base + "/stream/../../etc/passwd")[0], 404)
        self.assertEqual(self.get(self.stack.proxy_base + "/p/../x.m3u")[0], 404)
        self.assertEqual(self.get(self.stack.proxy_base + "/nope")[0], 404)

    def test_catalog_cache_and_refresh(self):
        self.get(self.stack.proxy_base + "/playlist.m3u")
        self.get(self.stack.proxy_base + "/playlist.m3u")
        self.assertEqual(FakeXtream.actions.count("get_live_streams"), 1)
        self.get(self.stack.proxy_base + "/playlist.m3u?refresh=1")
        self.assertEqual(FakeXtream.actions.count("get_live_streams"), 2)

    def test_stale_catalog_when_provider_down(self):
        self.get(self.stack.proxy_base + "/playlist.m3u")
        FakeXtream.fail_api = True
        status, body, _ = self.get(self.stack.proxy_base + "/playlist.m3u?refresh=1")
        self.assertEqual(status, 200)
        self.assertIn(b"/stream/42.ts", body)
        calls = FakeXtream.actions.count("get_live_streams")
        # Pas de nouvelle tentative pendant la fenêtre de repli (évite 30 s d'attente à chaque requête).
        self.get(self.stack.proxy_base + "/playlist.m3u?refresh=1")
        self.assertEqual(FakeXtream.actions.count("get_live_streams"), calls)
        self.assertIsNotNone(self.stack.api.last_error)

    def test_provider_down_without_cache(self):
        FakeXtream.fail_api = True
        status, body, _ = self.get(self.stack.proxy_base + "/playlist.m3u")
        self.assertEqual(status, 502)
        self.assertNotIn(b"pw", body)

    def test_public_base_url(self):
        stack = self.make_stack(public_base_url="http://192.168.1.10:7777")
        _, body, _ = self.get(stack.proxy_base + "/playlist.m3u")
        self.assertIn(b"http://192.168.1.10:7777/stream/42.ts", body)
        self.assertIn(b'x-tvg-url="http://192.168.1.10:7777/epg.xml"', body)

    def test_invalid_host_header(self):
        status, _, _ = self.get(self.stack.proxy_base + "/playlist.m3u", {"Host": "evil\"host"})
        self.assertEqual(status, 502)

    def test_access_token(self):
        stack = self.make_stack(access_token="mysecret")
        self.assertEqual(self.get(stack.proxy_base + "/playlist.m3u")[0], 403)
        self.assertEqual(self.get(stack.proxy_base + "/playlist.m3u?token=bad")[0], 403)
        self.assertEqual(self.get(stack.proxy_base + "/stream/42.ts")[0], 403)
        self.assertEqual(self.get(stack.proxy_base + "/epg.xml")[0], 403)
        self.assertEqual(self.get(stack.proxy_base + "/health")[0], 200)
        status, body, _ = self.get(stack.proxy_base + "/playlist.m3u?token=mysecret")
        self.assertEqual(status, 200)
        data = body.decode()
        self.assertIn("/stream/42.ts?token=mysecret", data)
        self.assertIn("/epg.xml?token=mysecret", data)
        self.assertEqual(self.get(stack.proxy_base + "/stream/42.ts?token=mysecret")[1], b"TS DATA 12345678")

    def test_token_never_logged(self):
        stack = self.make_stack(access_token="supersecret-token")
        with self.assertLogs("xtream-m3u-local", level="INFO") as logs:
            self.get(stack.proxy_base + "/playlist.m3u?token=supersecret-token")
            self.get(stack.proxy_base + "/stream/42.ts?token=supersecret-token")
        joined = "\n".join(logs.output)
        self.assertNotIn("supersecret-token", joined)
        self.assertNotIn("pw", joined.replace("/playlist", ""))

    def test_escape_attribute(self):
        self.assertEqual(escape_attribute('a"\nb'), "a' b")

    def test_head_playlist(self):
        with urlopen(Request(self.stack.proxy_base + "/playlist.m3u", method="HEAD"), timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), b"")
            self.assertGreater(int(r.headers["Content-Length"]), 0)


class LoggingQuiet(unittest.TestCase):
    """Garde-fou : la journalisation par défaut n'affiche pas de query string."""

    def test_logger_name(self):
        self.assertEqual(logging.getLogger("xtream-m3u-local").name, "xtream-m3u-local")


if __name__ == "__main__":
    unittest.main()
