"""Guide : appels API freinés et résumés, priorité au flux, guides externes (EPG_EXTRA_URLS), plafond par playlist."""
import json
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlencode

from app.diagnostic import extra_guides_check
from app import settings
from app.epg import DEFAULT_EPG_GUIDES, EPGSHARE_FR, GENERIC_EPG_CHANNELS, EpgCache, read_schedule, xmltv_time
from app.server import Config
from app.streams import StreamManager
from app.vod import KeyedCache

from .fake_xtream import EPG_END, EPG_START

START, STOP = xmltv_time(EPG_START), xmltv_time(EPG_END)
PROVIDER_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<tv><channel id="TF1.fr"><display-name>TF1</display-name></channel>
<programme start="{START}" stop="{STOP}" channel="TF1.fr"><title>JT</title></programme>
</tv>"""
EXTRA_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<tv><channel id="M6.fr"><display-name>M6</display-name></channel>
<channel id="France2.fr"><display-name>France 2</display-name></channel>
<programme start="{START}" stop="{STOP}" channel="M6.fr"><title>Zone interdite</title></programme>
<programme start="{START}" stop="{STOP}" channel="France2.fr"><title>Le 20h</title></programme>
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


class DefaultGuideTests(unittest.TestCase):
    def test_upgrade_adds_defaults_after_custom_urls_and_keeps_removals(self):
        with tempfile.TemporaryDirectory() as directory:
            defaults = tuple(DEFAULT_EPG_GUIDES)
            store = settings.Settings(directory, default_epg_sources=defaults)
            self.assertEqual(store.get()["epg_sources"], list(defaults))
            custom = "https://guide.example/ma.xml"
            store.path.write_text(json.dumps({"epg_sources": [custom, settings.RETIRED_EPG_SOURCE]}), encoding="utf-8")
            self.assertEqual(store.get()["epg_sources"], [custom, *defaults])
            store.update({"epg_sources": [custom]})
            restarted = settings.Settings(directory, default_epg_sources=defaults)
            self.assertEqual(restarted.get()["epg_sources"], [custom])
            restarted.update({"epg_sources": []})
            self.assertEqual(settings.Settings(directory, default_epg_sources=defaults).get()["epg_sources"], [])

    def test_moroccan_defaults_match_verified_channels_and_keep_explicit_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guide = root / "morocco.xml"
            guide.write_text(
                f'<tv><channel id="AlAoulaAr.ma"><display-name>Al Aoula Arabic.ma</display-name></channel>'
                f'<channel id="Arryadia.ma@HD"><display-name>Arryadia</display-name></channel>'
                f'<programme channel="AlAoulaAr.ma" start="{START}" stop="{STOP}"><title>Actualités</title></programme>'
                f'<programme channel="Arryadia.ma@HD" start="{START}" stop="{STOP}"><title>Bloc générique</title></programme></tv>',
                encoding="utf-8")
            epg = EpgCache((root / "absent.xml").as_uri, str(root / "primary"))
            epg.mode = "xmltv"
            automatic = DEFAULT_EPG_GUIDES["https://www.open-epg.com/files/morocco.xml.gz"]
            french = root / "france.xml"
            french.write_text(guide.read_text(encoding="utf-8")
                              .replace("AlAoulaAr.ma", "al.aoula.fr").replace("Arryadia.ma@HD", "arryadia.fr")
                              .replace("Al Aoula Arabic.ma", "Al Aoula").replace("Actualités", "Chaîne nationale"),
                              encoding="utf-8")
            epg.extras = [
                EpgCache(french.as_uri, str(root / "france"), automatic_exclusions=GENERIC_EPG_CHANNELS[EPGSHARE_FR]),
                EpgCache(guide.as_uri, str(root / "ext"), automatic_channels=automatic),
            ]
            channels = [channel(1, "MA: Al Aoula HD"), channel(2, "Arryadia", "Arryadia.ma@HD")]
            path, stats = epg.playlist_guide("automatic", channels)
            self.assertEqual(read_schedule(path)["xtream-1"][0][2], "Actualités")
            self.assertEqual([c["stream_id"] for c in stats["missing"]], ["2"])
            channels[1]["epg_manual"] = True
            _, stats = epg.playlist_guide("manual", channels)
            self.assertEqual(stats["missing"], [])


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

    def test_expired_guide_and_empty_variant_do_not_hide_current_programmes(self):
        expired = EXTRA_XML.replace(START, xmltv_time(EPG_START - 86400)).replace(STOP, xmltv_time(EPG_END - 86400))
        root = Path(self.tmp.name)
        (root / "expired.xml").write_text(expired, encoding="utf-8")
        current = EXTRA_XML.replace('<channel id="M6.fr">', '<channel id="M64K.fr"><display-name>M6 4K</display-name>'
                                    '</channel><channel id="M6.fr">')
        (root / "current.xml").write_text(current, encoding="utf-8")
        epg = EpgCache((root / "provider.xml").as_uri, str(root / "data"))
        epg.mode = "xmltv"
        stale = EpgCache((root / "expired.xml").as_uri, str(root / "stale"))
        epg.extras = [stale, EpgCache((root / "current.xml").as_uri, str(root / "current"))]
        channels = [{**channel(2, "M6"), "epg_prefer_external": True}]
        path, stats = epg.playlist_guide("salon", channels)
        self.assertEqual(stats["sources"]["2"], {"source": "external:2", "id": "m6.fr"})
        self.assertEqual([p[2] for p in read_schedule(path)["xtream-2"]], ["Zone interdite"])
        self.assertEqual(extra_guides_check(epg, channels)[0]["status"], "warn")
        self.assertEqual(EpgCache(stale._url_factory, str(root / "stale")).upcoming_ids(), set())
        epg.extras = [stale]
        _, stats = epg.playlist_guide("salon", channels)
        self.assertEqual([c["stream_id"] for c in stats["missing"]], ["2"])

    def test_unavailable_full_primary_with_all_channels_covered_by_extra(self):
        root = Path(self.tmp.name)
        epg = EpgCache((root / "absent.xml").as_uri, str(root / "data"))
        epg.extras = [EpgCache((root / "extra.xml").as_uri, str(root / "extra"))]
        with tempfile.TemporaryDirectory() as tmp:
            api = KeyedCache(lambda sid: self.fail("Guide API inattendu"), tmp, max_age=60)
            _, stats = epg.playlist_guide("_all", [channel(2, "M6")], api=api, api_ids=set())
        self.assertEqual(stats["external"], 1)

    def test_24_hour_coverage_rebuilds_old_index_and_tracks_time_without_downloading(self):
        root = Path(self.tmp.name)
        now = EPG_START + 60
        path = root / "gap.xml"
        path.write_text(PROVIDER_XML.replace(START, xmltv_time(now - 7200)).replace(STOP, xmltv_time(now - 3600))
                        .replace("</tv>", f'<programme start="{xmltv_time(now + 90000)}"'
                                 f' stop="{xmltv_time(now + 93600)}" channel="TF1.fr"><title>Suivant</title></programme></tv>'),
                        encoding="utf-8")
        download = EpgCache(path.as_uri, str(root / "gap"))
        download.ensure()
        (download.directory / "index.json").write_text(json.dumps({"source": download._source_key(),
                                                                    "channels": {"tf1.fr": 2}}), encoding="utf-8")
        self.assertEqual(download.upcoming_ids(now), set())
        self.assertEqual(download.upcoming_ids(now + 3600), set())  # début exactement dans 24 h
        read_disk = EpgCache(path.as_uri, str(root / "gap"))
        self.assertEqual(read_disk.upcoming_ids(now + 3601), {"tf1.fr"})
        self.assertEqual(read_disk.index(), {"tf1.fr": 2})


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


class GroupExternalFirstTests(unittest.TestCase):
    """« Mes groupes » : guide externe d'abord (rapproché par le nom propre du groupe), sinon le fournisseur."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        # Le fournisseur a TF1 (mauvais programmes) ; le guide externe a TF1, M6 et France 2.
        (root / "provider.xml").write_text(PROVIDER_XML, encoding="utf-8")
        (root / "extra.xml").write_text(EXTRA_XML.replace(
            '<channel id="M6.fr">', '<channel id="TF1.ext"><display-name>TF1</display-name></channel>\n<channel id="M6.fr">').replace(
            '</tv>', f'<programme start="{START}" stop="{STOP}" channel="TF1.ext"><title>Journal externe</title></programme>\n</tv>'),
            encoding="utf-8")
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def guide(self):
        epg = EpgCache(lambda: (self.root / "provider.xml").as_uri(), str(self.root / "data"))
        epg.mode = "xmltv"
        epg.extras = [EpgCache(lambda: (self.root / "extra.xml").as_uri(), str(self.root / "x"))]
        return epg

    def test_external_first_then_provider_and_manual_wins(self):
        tf1 = {**channel(1, "FR| TF1 FHD ᴿᴬᵂ", "TF1.fr"), "epg_prefer_external": True, "epg_name": "tf1"}
        other = channel(5, "Chaîne du fournisseur", "TF1.fr")                    # hors groupe : fournisseur
        alone = {**channel(6, "FR: Inconnue", "TF1.fr"), "epg_prefer_external": True}  # pas dans l'externe
        path, stats = self.guide().playlist_guide("p", [tf1, other, alone])
        self.assertEqual(stats["sources"]["1"], {"source": "external:1", "id": "tf1.ext"})
        # Même identifiant « TF1.fr » que TF1 : repris du guide externe aussi (pas de programmes en double).
        self.assertEqual(stats["sources"]["5"], {"source": "external:1", "id": "tf1.ext"})
        self.assertEqual(stats["sources"]["6"]["source"], "external:1")
        schedule = read_schedule(path)
        self.assertEqual([p[2] for p in schedule["tf1.fr"]], ["Journal externe"])   # une seule fois, l'externe
        # Pas de chaîne de groupe : le fournisseur reste la source.
        _, stats = self.guide().playlist_guide("r", [other])
        self.assertEqual(stats["sources"]["5"]["source"], "provider")
        # Choix manuel (identifiant du fournisseur) : jamais remplacé par le guide externe.
        manual = {**channel(1, "FR| TF1 FHD", "TF1.fr"), "epg_manual": True}
        _, stats = self.guide().playlist_guide("q", [manual])
        self.assertEqual(stats["sources"]["1"]["source"], "provider")

    def test_builder_tags_group_channels(self):
        from app.builder import select_items
        from app.custom import validate_playlist

        class Api:
            def catalog(self, refresh=False, kind="live"):
                return [channel(1, "FR| TF1 FHD"), channel(2, "FR: Sport 1")], {"1": "Généralistes"}

        base = {"slug": "s", "name": "S", "category_ids": ["1"],
                "custom_groups": [{"id": "tnt", "name": "TNT", "match": "exact", "patterns": ["1: TF1"]}]}
        entries, _ = select_items(Api(), validate_playlist(base), "live")
        tf1 = next(i for i, _ in entries if i["stream_id"] == "1")
        self.assertEqual((tf1.get("epg_prefer_external"), tf1.get("epg_name")), (True, "tf1"))
        self.assertNotIn("epg_prefer_external", next(i for i, _ in entries if i["stream_id"] == "2"))
        entries, _ = select_items(Api(), validate_playlist({**base, "group_epg_external": False}), "live")
        self.assertNotIn("epg_prefer_external", next(i for i, _ in entries if i["stream_id"] == "1"))
        entries, _ = select_items(Api(), validate_playlist({**base, "item_xmltv": {"1": "TF1.ext"}}), "live")
        tf1 = next(i for i, _ in entries if i["stream_id"] == "1")
        self.assertEqual((tf1["epg_id"], tf1.get("epg_manual"), tf1.get("epg_prefer_external")), ("TF1.ext", True, None))


class GroupGuideApiTests(StackTestCase):
    def test_live_setting_connects_m3u_and_xtream_to_external_guide_in_xmltv_mode(self):
        from .fake_xtream import FakeXtream

        root = Path(self.tmp.name)
        (root / "radio.xml").write_text(EXTRA_XML.replace("M6</display-name>", 'Radio "TV" HACK</display-name>'),
                                        encoding="utf-8")
        source = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=root))
        worker = Thread(target=source.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(source.server_close)
        self.addCleanup(source.shutdown)
        settings.install(settings.Settings(self.tmp.name))
        self.addCleanup(settings.install, None)
        base = f"http://127.0.0.1:{source.server_port}"
        stack = self.make_stack(epg_source="xmltv", epg_url=f"{base}/absent.xml")
        self.assertEqual(self.send(stack.web_base + "/api/settings", "PUT",
                                   {"epg_sources": [f"{base}/radio.xml"]})[0], 200)
        self.assertEqual(self.create({"slug": "salon", "name": "Salon", "category_ids": ["2"],
                                      "custom_groups": [{"id": "radios", "name": "Radios", "patterns": ["Radio"]}]}, stack)[0], 201)
        status, device = self.send(stack.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "salon"})
        self.assertEqual(status, 201)
        auth = urlencode({k: device[k] for k in ("username", "password")})
        m3u = self.get(stack.proxy_base + "/p/salon.m3u")[1]
        self.assertIn(b'tvg-id="xtream-43"', m3u)
        xtream = self.get(stack.proxy_base + f"/player_api.php?{auth}&action=get_live_streams")[1]
        self.assertIn(b'"epg_channel_id":"xtream-43"', xtream.replace(b" ", b""))
        for endpoint in ("/p/salon.xml", "/xmltv.php", f"/xmltv.php?{auth}"):
            status, body, _ = self.get(stack.proxy_base + endpoint)
            self.assertEqual(status, 200, body[:300])
            self.assertEqual(ET.fromstring(body).find("programme[@channel='xtream-43']/title").text, "Zone interdite")
        self.assertEqual(FakeXtream.simple_epg_calls, [])

    def test_groups_and_search(self):
        settings.install(settings.Settings(self.tmp.name))
        self.addCleanup(settings.install, None)
        root = Path(self.tmp.name)
        (root / "ext.xml").write_text(EXTRA_XML, encoding="utf-8")
        status, data = self.send(self.stack.web_base + "/api/settings", "PUT", {"epg_sources": [(root / "ext.xml").as_uri().replace("file://", "http://x.invalid")]})
        self.assertEqual(status, 200)
        self.assertEqual(len(data["epg_sources"]), 1)
        self.stack.epg.extras = [EpgCache(lambda: (root / "ext.xml").as_uri(), str(root / "ext"))]
        self.stack.epg._extras_fn = None
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2"],
                     "custom_groups": [{"id": "g", "name": "Infos", "match": "exact", "patterns": ["France Info"]}]})
        status, body, _ = self.get(self.stack.web_base + "/api/epg/groups?slug=salon")
        self.assertEqual(status, 200, body[:200])
        rows = json.loads(body)["channels"]
        self.assertTrue(rows and rows[0]["group"] == "Infos")
        found = json.loads(self.get(self.stack.web_base + "/api/epg/search?q=m6")[1])["results"]
        self.assertEqual((found[0]["id"], found[0]["name"], found[0]["source"]), ("m6.fr", "M6", "external:1"))
        self.assertEqual(self.get(self.stack.web_base + "/api/epg/search?q=a")[0], 400)
