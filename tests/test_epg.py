"""Guide EPG : cache disque, gzip, EPG filtré par playlist."""
import io
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from app.epg import write_filtered_xmltv

from .fake_xtream import FakeXtream
from .helpers import StackTestCase


class EpgTests(StackTestCase):
    def test_full_epg_is_cached(self):
        status, body, headers = self.get(self.stack.proxy_base + "/epg.xml")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/xml"))
        self.assertIn(b"<tv", body)
        self.get(self.stack.proxy_base + "/epg.xml")
        self.assertEqual(FakeXtream.epg_requests, 1)
        self.assertTrue(Path(self.tmp.name, "epg", "xmltv.xml").exists())

    def test_gzip_epg(self):
        FakeXtream.epg_mode = "gzip"
        status, body, _ = self.get(self.stack.proxy_base + "/epg.xml")
        self.assertEqual(status, 200)
        self.assertTrue(body.lstrip().startswith(b"<?xml"))

    def test_invalid_epg(self):
        FakeXtream.epg_mode = "html"
        self.assertEqual(self.get(self.stack.proxy_base + "/epg.xml")[0], 502)
        # Pas de nouvelle tentative immédiate (protection du fournisseur).
        self.assertEqual(self.get(self.stack.proxy_base + "/epg.xml")[0], 502)
        self.assertEqual(FakeXtream.epg_requests, 1)

    def test_stale_epg_served_when_provider_fails(self):
        self.get(self.stack.proxy_base + "/epg.xml")
        source = Path(self.tmp.name, "epg", "xmltv.xml")
        old = time.time() - 2 * 86400
        os.utime(source, (old, old))
        FakeXtream.epg_mode = "error"
        status, body, _ = self.get(self.stack.proxy_base + "/epg.xml")
        self.assertEqual(status, 200)
        self.assertIn(b"franceinfo.fr", body)

    def test_filtered_epg_per_playlist(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["10"], "included_stream_ids": ["42"]})
        status, body, _ = self.get(self.stack.proxy_base + "/p/salon.xml")
        self.assertEqual(status, 200)
        root = ET.fromstring(body)
        self.assertEqual(root.tag, "tv")
        self.assertEqual(root.get("generator-info-name"), "fake")
        self.assertEqual(sorted(c.get("id") for c in root.findall("channel")), ["franceinfo.fr", "sport1.fr"])
        self.assertEqual(sorted(p.get("channel") for p in root.findall("programme")), ["franceinfo.fr", "sport1.fr"])
        self.assertNotIn(b"other.fr", body)
        # Cache : pas de nouveau téléchargement, puis invalidation quand la playlist change.
        self.get(self.stack.proxy_base + "/p/salon.xml")
        self.assertEqual(FakeXtream.epg_requests, 1)
        self.send(self.stack.web_base + "/api/playlists/salon", "PUT", {"name": "Salon", "category_ids": ["10"]})
        root = ET.fromstring(self.get(self.stack.proxy_base + "/p/salon.xml")[1])
        self.assertEqual([c.get("id") for c in root.findall("channel")], ["sport1.fr"])
        self.send(self.stack.web_base + "/api/playlists/salon", "DELETE")
        self.assertFalse(Path(self.tmp.name, "epg", "p-salon.xml").exists())
        self.assertEqual(self.get(self.stack.proxy_base + "/p/salon.xml")[0], 404)

    def test_epg_disabled(self):
        stack = self.make_stack(enable_epg=False)
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"]}, stack)
        _, body, _ = self.get(stack.proxy_base + "/playlist.m3u")
        self.assertTrue(body.startswith(b"#EXTM3U\n"))
        self.assertTrue(self.get(stack.proxy_base + "/p/salon.m3u")[1].startswith(b"#EXTM3U\n"))
        self.assertEqual(self.get(stack.proxy_base + "/epg.xml")[0], 404)
        self.assertEqual(self.get(stack.proxy_base + "/p/salon.xml")[0], 404)
        self.assertEqual(FakeXtream.epg_requests, 0)

    def test_write_filtered_xmltv_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "x.xml")
            src.write_text('<tv><channel id="A"/><programme channel="a"><title>T &amp; U</title></programme>'
                           '<programme channel="b"/></tv>', encoding="utf-8")
            out = io.StringIO()
            kept = write_filtered_xmltv(src, out, {"a"})
            self.assertEqual(kept, 2)
            root = ET.fromstring(out.getvalue())
            self.assertEqual(root.find("programme/title").text, "T & U")
            self.assertEqual(len(root), 2)


if __name__ == "__main__":
    unittest.main()
