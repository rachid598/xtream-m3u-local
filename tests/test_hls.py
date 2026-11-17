"""Relais HLS : réécriture des manifestes, segments, clés, token, limite de connexions."""
import re
import unittest

from app.hls import HlsRegistry, rewrite_manifest, segment_ext

from .fake_xtream import FakeXtream
from .helpers import StackTestCase

LOCAL_URI = re.compile(r"^/hls/(v|s)/[A-Za-z0-9_-]{16,32}\.[a-z0-9]+(\?token=\w+)?$")


class RewriteTests(unittest.TestCase):
    def test_rewrite_all_uri_kinds(self):
        manifest = (
            "#EXTM3U\n"
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio.m3u8"\n'
            '#EXT-X-I-FRAME-STREAM-INF:BANDWIDTH=1,URI="iframe.m3u8"\n'
            '#EXT-X-STREAM-INF:BANDWIDTH=2\nvariant/playlist\n'
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://fairplay"\n'
            '#EXTINF:4,\nhttps://cdn.example/x/seg1.ts?t=1\n'
            '#EXTINF:4,\n../seg2.aac\n'
        )
        seen = []

        def link(url, is_playlist):
            seen.append((url, is_playlist))
            return f"L{len(seen)}"

        out = rewrite_manifest(manifest, "http://p.example/live/a/b/42.m3u8", link)
        self.assertEqual(seen, [
            ("http://p.example/live/a/b/audio.m3u8", True),
            ("http://p.example/live/a/b/iframe.m3u8", True),
            ("http://p.example/live/a/b/variant/playlist", True),     # ligne après EXT-X-STREAM-INF
            ("http://p.example/live/a/b/key.bin", False),
            ("https://cdn.example/x/seg1.ts?t=1", False),
            ("http://p.example/live/a/seg2.aac", False),
        ])
        self.assertIn('URI="skd://fairplay"', out)        # schéma non HTTP laissé tel quel
        self.assertNotIn("p.example", out)
        self.assertNotIn("cdn.example", out)
        with self.assertRaises(ValueError):
            rewrite_manifest("<html>", "http://x/", link)

    def test_segment_ext_and_registry(self):
        self.assertEqual(segment_ext("http://x/a/b.AAC?z=1"), "aac")
        self.assertEqual(segment_ext("http://x/a/segment"), "ts")
        reg = HlsRegistry(ttl=60, max_entries=3)
        h1 = reg.register("http://x/1", "s", None)
        self.assertEqual(reg.register("http://x/1", "s", None), h1)   # même URL : même code
        self.assertEqual(reg.resolve(h1, "s"), ("http://x/1", None))
        self.assertIsNone(reg.resolve(h1, "p"))
        self.assertIsNone(reg.resolve("../../etc", "s"))
        for i in range(2, 6):
            reg.register(f"http://x/{i}", "s", None)
        self.assertEqual(len(reg), 3)
        self.assertIsNone(reg.resolve(h1, "s"))                       # évincé (le plus ancien)


class HlsRelayTests(StackTestCase):
    def manifest(self, url):
        status, body, headers = self.get(url)
        self.assertEqual(status, 200, body[:200])
        self.assertEqual(headers["Content-Type"], "application/vnd.apple.mpegurl")
        return body.decode()

    def uris(self, text):
        found = []
        for line in text.splitlines():
            if line.startswith("#"):
                found += re.findall(r'URI="([^"]*)"', line)
            elif line.strip():
                found.append(line.strip())
        return found

    def test_full_chain(self):
        base = self.stack.proxy_base
        master = self.manifest(base + "/hls/42.m3u8")
        self.assertNotIn("tok123", master)
        self.assertNotIn("/pw/", master)
        audio_uri, variant_uri = self.uris(master)
        for uri in (audio_uri, variant_uri):
            self.assertRegex(uri, LOCAL_URI)
        self.assertTrue(variant_uri.startswith("/hls/v/"))
        media = self.manifest(base + variant_uri)
        key_uri, init_uri, seg10_uri, seg11_uri = self.uris(media)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:10", media)
        self.assertIn("IV=0x01", media)
        self.assertNotIn("127.0.0.1:" + str(self.provider.server_address[1]), media)
        expected = {key_uri: b"0123456789abcdef", init_uri: b"INIT", seg10_uri: b"SEG10", seg11_uri: b"SEG11"}
        for uri, content in expected.items():
            self.assertTrue(uri.startswith("/hls/s/"), uri)
            status, body, headers = self.get(base + uri)
            self.assertEqual((status, body), (200, content))
        self.assertTrue(seg10_uri.endswith(".ts"))
        audio = self.manifest(base + audio_uri)
        status, body, _ = self.get(base + self.uris(audio)[0])
        self.assertEqual(body, b"AAC10")
        # Session HLS visible dans les lectures en cours (comptée dans la limite).
        streams = self.stack.streams.list()
        self.assertEqual([(s["kind"], s["item_id"]) for s in streams], [("hls", "42")])
        self.assertGreater(streams[0]["bytes"], 0)

    def test_errors(self):
        base = self.stack.proxy_base
        self.assertEqual(self.get(base + "/hls/43.m3u8")[0], 502)            # pas un manifeste
        self.assertEqual(self.get(base + "/hls/999.m3u8")[0], 502)           # 404 fournisseur
        self.assertEqual(self.get(base + "/hls/s/AAAAAAAAAAAAAAAAAAAA.ts")[0], 404)
        self.assertEqual(self.get(base + "/hls/v/../../x.m3u8")[0], 404)
        self.assertEqual(self.get(base + "/hls/abc.m3u8")[0], 404)

    def test_token_propagated(self):
        stack = self.make_stack(access_token="tok")
        self.assertEqual(self.get(stack.proxy_base + "/hls/42.m3u8")[0], 403)
        master = self.get(stack.proxy_base + "/hls/42.m3u8?token=tok")[1].decode()
        for uri in self.uris(master):
            self.assertTrue(uri.endswith("?token=tok"), uri)
        variant = self.uris(master)[1]          # [0] = piste audio, [1] = variante vidéo
        self.assertEqual(self.get(stack.proxy_base + variant.split("?")[0])[0], 403)
        media = self.get(stack.proxy_base + variant)[1].decode()
        seg = self.uris(media)[-1]
        self.assertEqual(self.get(stack.proxy_base + seg)[1], b"SEG11")

    def test_evicted_session_rejected(self):
        base = self.stack.proxy_base
        master = self.manifest(base + "/hls/42.m3u8")
        variant = self.uris(master)[1]
        session = self.stack.streams.list()[0]
        self.stack.streams.stop(session["id"])
        self.assertEqual(self.get(base + variant)[0], 409)

    def test_custom_playlist_hls_format(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "live_format": "hls"})
        data = self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode()
        self.assertIn(f"{self.stack.proxy_base}/hls/42.m3u8", data)
        self.assertNotIn("/stream/", data)
        forced = self.get(self.stack.proxy_base + "/p/salon.m3u?format=ts")[1].decode()
        self.assertIn("/stream/42.ts", forced)
        self.assertEqual(self.create({"slug": "x", "name": "X", "live_format": "rtmp"})[0], 400)
        self.assertEqual(self.get(self.stack.proxy_base + "/p/salon.m3u?format=mp4")[0], 400)


if __name__ == "__main__":
    unittest.main()


class PreviewAccessTests(StackTestCase):
    """Aperçu dans le navigateur : le relais HLS n'est lisible en JavaScript que depuis l'interface Web."""

    def test_cors_only_for_web_interface_and_csp(self):
        web_port = self.stack.config.web_port
        ok = self.get(self.stack.proxy_base + "/hls/42.m3u8", {"Origin": f"http://127.0.0.1:{web_port}"})
        self.assertEqual(ok[0], 200)
        self.assertEqual(ok[2].get("Access-Control-Allow-Origin"), f"http://127.0.0.1:{web_port}")
        other = self.get(self.stack.proxy_base + "/hls/42.m3u8", {"Origin": "http://evil.example"})
        self.assertNotIn("Access-Control-Allow-Origin", other[2])
        playlist = self.get(self.stack.proxy_base + "/playlist.m3u", {"Origin": f"http://127.0.0.1:{web_port}"})
        self.assertNotIn("Access-Control-Allow-Origin", playlist[2])
        csp = self.get(self.stack.web_base + "/")[2]["Content-Security-Policy"]
        self.assertIn(f"connect-src 'self' http://127.0.0.1:{self.stack.config.port}", csp)
        self.assertIn("media-src 'self' blob:", csp)
        self.assertEqual(self.get(self.stack.web_base + "/static/vendor/hls.light.min.js")[0], 200)
