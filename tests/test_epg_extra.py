"""Guide : appels API freinés et résumés, priorité au flux, guides externes (EPG_EXTRA_URLS), plafond par playlist."""
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from app.epg import EpgCache, read_schedule
from app.server import Config
from app.streams import StreamManager
from app.vod import KeyedCache

PROVIDER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<tv><channel id="TF1.fr"><display-name>TF1</display-name></channel>
<programme start="20261119200000 +0000" stop="20261119210000 +0000" channel="TF1.fr"><title>JT</title></programme>
</tv>"""
EXTRA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<tv><channel id="M6.fr"><display-name>M6</display-name></channel>
<channel id="France2.fr"><display-name>France 2</display-name></channel>
<programme start="20261119200000 +0000" stop="20261119210000 +0000" channel="M6.fr"><title>Zone interdite</title></programme>
<programme start="20261119200000 +0000" stop="20261119210000 +0000" channel="France2.fr"><title>Le 20h</title></programme>
</tv>"""


def channel(sid, name, epg_id=""):
    return {"stream_id": str(sid), "name": name, "logo": "", "epg_id": epg_id, "num": "", "category_id": "1",
            "category_ids": ["1"], "archive": 0}


class ThrottleTests(unittest.TestCase):
    def test_min_interval_spaces_calls(self):
        stamps = []

        def fetch(sid):
            stamps.append(time.monotonic())
            return {"epg_listings": []}

        with tempfile.TemporaryDirectory() as tmp:
            cache = KeyedCache(fetch, tmp, workers=4, subdir="epg", min_interval=0.2)
            cache.episodes_for([str(i) for i in range(4)], budget=5)
        gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
        self.assertEqual(len(stamps), 4)
        self.assertTrue(all(gap >= 0.18 for gap in gaps), gaps)

    def test_failures_summarized_in_one_line(self):
        def fetch(sid):
            raise RuntimeError("429")

        with tempfile.TemporaryDirectory() as tmp:
            cache = KeyedCache(fetch, tmp, workers=4, subdir="epg", label="EPG de la chaîne", summarize=True)
            with self.assertLogs("xtream-m3u-local.vod", level="WARNING") as logs:
                cache.episodes_for([str(i) for i in range(50)], budget=5)
        self.assertEqual(len(logs.records), 1, logs.output)

    def test_defer_waits_for_stream_start(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            cache = KeyedCache(lambda sid: calls.append(time.monotonic()) or {}, tmp, subdir="epg")
            until = time.monotonic() + 0.5
            cache.defer = lambda: max(0.0, until - time.monotonic())
            cache.episodes_for(["1"], budget=5)
        self.assertGreaterEqual(calls[0], until - 0.01)

    def test_stream_start_delay(self):
        manager = StreamManager(lambda provider: 2)
        self.assertEqual(manager.start_delay(), 0)
        session = manager.acquire(kind="live", item_id="1", client="c", device="TV")
        self.assertGreater(manager.start_delay(20), 15)
        manager.release(session)
        self.assertEqual(manager.start_delay(), 0)


class ExternalGuideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "provider.xml").write_text(PROVIDER_XML, encoding="utf-8")
        (root / "extra.xml").write_text(EXTRA_XML, encoding="utf-8")
        with zipfile.ZipFile(root / "extra.zip", "w") as archive:
            archive.writestr("guide.xml", EXTRA_XML)
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def guide(self, extra_name):
        epg = EpgCache(lambda: (self.root / "provider.xml").as_uri(), str(self.root / "data"))
        epg.mode = "xmltv"
        epg.extras = [EpgCache(lambda: (self.root / extra_name).as_uri(), str(self.root / "extra" / extra_name))]
        return epg

    def check(self, extra_name):
        epg = self.guide(extra_name)
        channels = [channel(1, "FR: TF1 HD", "TF1.fr"), channel(2, "FR: M6 FHD"), channel(3, "FR: FRANCE 2 4K", "france2.provider"),
                    channel(4, "Chaîne inconnue")]
        path, stats = epg.playlist_guide("salon", channels)
        self.assertEqual((stats["xmltv"], stats["external"], stats["api"]), (1, 2, 0))
        self.assertEqual([m["name"] for m in stats["missing"]], ["Chaîne inconnue"])
        schedule = read_schedule(path)
        self.assertEqual(schedule["tf1.fr"][0][2], "JT")
        self.assertEqual(schedule["xtream-2"][0][2], "Zone interdite")          # par nom : « FR: M6 FHD » = « M6 »
        self.assertEqual(schedule["france2.provider"][0][2], "Le 20h")         # renommé vers l'identifiant de la playlist

    def test_external_xml(self):
        self.check("extra.xml")

    def test_external_zip(self):
        self.check("extra.zip")

    def test_api_cap_per_playlist(self):
        fetched = []
        with tempfile.TemporaryDirectory() as tmp:
            api = KeyedCache(lambda sid: fetched.append(sid) or {"epg_listings": []}, tmp, subdir="epg")
            epg = EpgCache(lambda: (self.root / "provider.xml").as_uri(), str(self.root / "data2"))
            epg.mode, epg.api_cache, epg.api_max = "auto", api, 2
            epg.playlist_guide("p", [channel(i, f"Chaîne {i}") for i in range(10, 16)], budget=5)
        self.assertEqual(sorted(fetched), ["10", "11"])

    def test_guide_ids_with_external_guides(self):
        base = Config("http://x.invalid", "u", "p", epg_source="xmltv")
        self.assertFalse(base.guide_ids)
        from dataclasses import replace
        self.assertTrue(replace(base, epg_extra_urls=("https://guide.example/fr.xml.gz",)).guide_ids)


if __name__ == "__main__":
    unittest.main()
