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


class PrudentModeTests(unittest.TestCase):
    """Fournisseur sensible : après un 429, plus d'appel « guide par chaîne » pendant 1 h (la vidéo d'abord)."""

    def test_blocked_reasons(self):
        from types import SimpleNamespace
        from app.server import epg_api_blocked
        now = time.time()
        api = SimpleNamespace(last_429=0.0)
        self.assertIn("démarrage", epg_api_blocked(api, now, 120))
        self.assertIsNone(epg_api_blocked(api, now - 200, 120))
        api.last_429 = now - 60
        self.assertIn("prudent", epg_api_blocked(api, now - 200, 120))
        api.last_429 = now - 4000
        self.assertIsNone(epg_api_blocked(api, now - 200, 120))

    def test_blocked_cache_never_calls_provider(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            cache = KeyedCache(lambda sid: calls.append(sid) or {}, tmp, subdir="epg", label="EPG de la chaîne",
                               summarize=True)
            cache.blocked = lambda: "fournisseur sensible (429 récent) : mode prudent"
            with self.assertLogs("xtream-m3u-local.vod", level="WARNING") as logs:
                results, missing = cache.episodes_for(["1", "2", "3"], budget=2)
        self.assertEqual((calls, results, missing), ([], {}, ["1", "2", "3"]))
        self.assertEqual(len(logs.records), 1)
        self.assertIn("mode prudent", logs.output[0])

    def test_429_remembered_and_catalog_kept(self):
        from app.server import Config, XtreamAPI
        with tempfile.TemporaryDirectory() as tmp:
            api = XtreamAPI(Config("http://provider.invalid", "u", "p", data_dir=tmp))
            api.note_429()
            again = XtreamAPI(Config("http://provider.invalid", "u", "p", data_dir=tmp))
            self.assertAlmostEqual(again.last_429, api.last_429, places=3)  # retenu après un redémarrage
            again.kind_state("live").items = []
            again._refresh_in_background("live")
            self.assertEqual(again._refreshing, set())                       # pas de rafraîchissement 10 min




class SuggestTests(unittest.TestCase):
    """Chaînes sans programme : guides de noms proches proposés, puis choisis dans la playlist."""

    def test_suggestions_and_choice(self):
        from app.builder import select_items
        from app.custom import InvalidPlaylist, validate_playlist
        from app.epg import suggest_guides
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "extra.xml").write_text(EXTRA_XML, encoding="utf-8")
            (root / "provider.xml").write_text(PROVIDER_XML, encoding="utf-8")
            provider = EpgCache(lambda: (root / "provider.xml").as_uri(), str(root / "p"))
            extra = EpgCache(lambda: (root / "extra.xml").as_uri(), str(root / "x"))
            provider.ensure()
            extra.ensure()
            missing = [{"stream_id": "2", "name": "FR: M6 HD"}, {"stream_id": "3", "name": "FR: FRANCE 2 FHD"},
                       {"stream_id": "4", "name": "VIP: TF1 ᴴᴰ"}, {"stream_id": "6", "name": "TF1 Séries Films"}, {"stream_id": "8", "name": "FR: FRANCE 3"}, {"stream_id": "5", "name": "Zzz inconnue"}]
            result = {r["stream_id"]: r["suggestions"] for r in
                      suggest_guides([("guide du fournisseur", provider), ("guide externe 1", extra)], missing)}
        self.assertEqual(result["2"][0]["id"], "m6.fr")
        self.assertEqual((result["2"][0]["score"], result["2"][0]["source"], result["2"][0]["name"]),
                         (100, "guide externe 1", "M6"))
        self.assertEqual(result["3"][0]["id"], "france2.fr")
        self.assertEqual(result["4"][0]["id"], "tf1.fr")               # préfixe et qualité ignorés
        self.assertNotIn("5", result)
        self.assertNotIn("6", result)                                  # autre chaîne : pas le guide de TF1
        self.assertNotIn("8", result)                                  # France 3 ≠ France 2 (numéros différents)
        # Choix enregistré dans la playlist : identifiant XMLTV appliqué à la chaîne.
        saved = validate_playlist({"slug": "salon", "name": "Salon", "category_ids": ["1"],
                                   "item_xmltv": {"2": "m6.fr"}})
        self.assertEqual(saved["item_xmltv"], {"2": "m6.fr"})
        for bad in ({"2": "a<b"}, {"x": "m6.fr"}, {"2": ""}, {"2": 5}):
            with self.assertRaises(InvalidPlaylist):
                validate_playlist({"slug": "salon", "name": "Salon", "item_xmltv": bad})

        class Api:
            def catalog(self, refresh=False, kind="live"):
                return [channel(2, "FR: M6 HD"), channel(7, "Autre")], {"1": "Généralistes"}

        entries, _ = select_items(Api(), saved, "live")
        self.assertEqual({i["stream_id"]: i["epg_id"] for i, _ in entries}, {"2": "m6.fr", "7": ""})


from .helpers import StackTestCase  # noqa: E402


class SuggestApiTests(StackTestCase):
    def test_endpoint(self):
        import json
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2"]})
        status, body, _ = self.get(self.stack.web_base + "/api/epg/suggest?slug=salon")
        self.assertEqual(status, 200, body[:200])
        data = json.loads(body)
        self.assertIn("missing", data)
        self.assertIsInstance(data["suggestions"], list)
        self.assertEqual(self.get(self.stack.web_base + "/api/epg/suggest?slug=../x")[0], 400)
        self.assertEqual(self.get(self.stack.web_base + "/api/epg/suggest?slug=absente")[0], 404)
