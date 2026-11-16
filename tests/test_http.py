"""Compression gzip et validation ETag/304 (ports 7777 et 7778)."""
import gzip
import json
import unittest
from unittest import mock

from app.httputil import accepts_gzip

from .helpers import StackTestCase


class GzipParsingTests(unittest.TestCase):
    def test_accepts_gzip(self):
        cases = {"gzip": True, "gzip, deflate, br": True, "br;q=1.0, gzip;q=0.8": True, "*": True,
                 "gzip;q=0": False, "deflate": False, "": False, "identity": False}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(accepts_gzip({"Accept-Encoding": raw}), expected)


class HttpCachingTests(StackTestCase):
    GZ = {"Accept-Encoding": "gzip"}

    def setUp(self):
        super().setUp()
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2", "10"]})

    @mock.patch("app.httputil.MIN_GZIP_SIZE", 64)
    def test_playlist_gzip_and_etag(self):
        url = self.stack.proxy_base + "/p/salon.m3u"
        status, plain, headers = self.get(url)
        self.assertNotIn("Content-Encoding", headers)
        etag = headers["ETag"]
        self.assertEqual(headers["Cache-Control"], "no-cache")
        status, packed, headers = self.get(url, self.GZ)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(packed), plain)
        self.assertEqual(headers["ETag"], etag)
        status, body, headers = self.get(url, {"If-None-Match": etag})
        self.assertEqual((status, body), (304, b""))
        status, _, _ = self.get(url, {"If-None-Match": '"autre"'})
        self.assertEqual(status, 200)
        # Une modification de la playlist change l'ETag.
        self.send(self.stack.web_base + "/api/playlists/salon", "PUT", {"name": "Salon", "category_ids": ["1"]})
        self.assertEqual(self.get(url, {"If-None-Match": etag})[0], 200)

    def test_small_bodies_not_compressed(self):
        self.create({"slug": "vide", "name": "Vide"})
        _, _, headers = self.get(self.stack.proxy_base + "/p/vide.m3u", self.GZ)
        self.assertNotIn("Content-Encoding", headers)

    def test_guide_file_gzip_stream_and_304(self):
        url = self.stack.proxy_base + "/p/salon.xml"
        status, plain, headers = self.get(url)
        self.assertEqual(status, 200)
        etag = headers["ETag"]
        self.assertEqual(self.get(url, {"If-None-Match": etag})[0], 304)
        # Compression en flux au-delà de 64 Kio : on force un petit seuil pour le test.
        _, _, headers = self.get(url, self.GZ)
        self.assertNotIn("Content-Encoding", headers)       # fichier minuscule : pas compressé

    def test_web_json_gzip(self):
        status, packed, headers = self.get(self.stack.web_base + "/api/catalog", self.GZ)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertEqual(json.loads(gzip.decompress(packed))["kind"], "live")
        status, packed, headers = self.get(self.stack.web_base + "/static/app.js", self.GZ)
        self.assertEqual(headers["Content-Encoding"], "gzip")
        self.assertIn(b"use strict", gzip.decompress(packed))

    def test_streams_never_compressed(self):
        _, body, headers = self.get(self.stack.proxy_base + "/stream/42.ts", self.GZ)
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(body, b"TS DATA 12345678")


class LargeGuideTests(StackTestCase):
    def test_large_xml_streamed_gzip(self):
        # Guide fournisseur > 64 Kio : servi compressé en flux, sans Content-Length.
        import tests.fake_xtream as fake
        original = fake.XMLTV
        programmes = "".join(
            f'<programme start="20261115{h:02d}0000 +0000" stop="20261115{h:02d}3000 +0000" channel="franceinfo.fr">'
            f'<title>Programme {i} {"x" * 200}</title></programme>\n' for i in range(400) for h in [i % 24])
        fake.XMLTV = original.replace("</tv>", programmes + "</tv>")
        try:
            self.create({"slug": "big", "name": "Big", "category_ids": ["1"]})
            status, packed, headers = self.get(self.stack.proxy_base + "/p/big.xml", {"Accept-Encoding": "gzip"})
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Encoding"], "gzip")
            self.assertNotIn("Content-Length", headers)
            self.assertIn(b"Programme 399", gzip.decompress(packed))
        finally:
            fake.XMLTV = original


if __name__ == "__main__":
    unittest.main()
