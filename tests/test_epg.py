"""Guide EPG : cache disque, gzip, EPG filtré par playlist."""
import io
import json
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from app.epg import write_filtered_xmltv

from .fake_xtream import EPG_END, EPG_NEXT_END, EPG_START, FakeXtream
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


class ApiEpgTests(StackTestCase):
    """Guide complété par l'API (get_simple_data_table) et état EPG dans l'interface."""

    def test_normalize_listings(self):
        from app.epg import normalize_listings
        from .fake_xtream import SIMPLE_EPG
        items = normalize_listings(SIMPLE_EPG["43"])
        self.assertEqual([(i["title"], i["start"], i["stop"]) for i in items], [
            ("Concert <live> & co", EPG_START, EPG_END),
            ("pas-du-base64 !", EPG_END, EPG_NEXT_END),   # texte brut, dates "start"/"end"
        ])
        self.assertEqual(items[0]["desc"], "Musique")
        self.assertEqual(normalize_listings({"error": "x"}), [])
        self.assertEqual(normalize_listings([]), [])

    def guide(self, stack, slug):
        status, body, _ = self.get(stack.proxy_base + f"/p/{slug}.xml")
        self.assertEqual(status, 200, body[:200])
        return ET.fromstring(body)

    def test_auto_mode_completes_xmltv_with_api(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2"]})
        m3u = self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode()
        self.assertIn('tvg-id="xtream-43"', m3u)            # chaîne sans epg_channel_id
        self.assertIn('tvg-id="franceinfo.fr"', m3u)
        root = self.guide(self.stack, "salon")
        channels = sorted(c.get("id") for c in root.findall("channel"))
        self.assertEqual(channels, ["franceinfo.fr", "sport1.fr", "xtream-43"])
        titles = {p.get("channel"): p.find("title").text for p in root.findall("programme")}
        self.assertEqual(titles["xtream-43"], "pas-du-base64 !")
        self.assertIn("Concert <live> & co", [p.find("title").text for p in root.findall("programme")])
        self.assertEqual(titles["franceinfo.fr"], "Journal")   # xmltv.php prioritaire
        self.assertEqual(FakeXtream.simple_epg_calls.count("42"), 0)   # couverte par xmltv.php : pas d'appel API
        self.assertIn("43", FakeXtream.simple_epg_calls)
        programme = root.find("programme[@channel='xtream-43']")
        self.assertRegex(programme.get("start"), r"^\d{14} \+0000$")

    def test_api_used_for_all_channels_when_xmltv_broken(self):
        FakeXtream.epg_mode = "error"
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"]})
        root = self.guide(self.stack, "salon")
        self.assertEqual([p.find("title").text for p in root.findall("programme")], ["Journal API"])
        self.assertEqual(root.find("channel").get("id"), "franceinfo.fr")
        status = json.loads(self.get(self.stack.web_base + "/api/epg?slug=salon")[1])
        self.assertFalse(status["source"]["ok"])
        self.assertIn("inaccessible", status["source"]["error"])
        self.assertEqual((status["playlist"]["xmltv"], status["playlist"]["api"]), (0, 1))
        self.assertEqual([m["stream_id"] for m in status["playlist"]["missing"]], ["44"])

    def test_xmltv_only_mode(self):
        stack = self.make_stack(epg_source="xmltv")
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2"]}, stack)
        self.assertNotIn(b"xtream-43", self.get(stack.proxy_base + "/p/salon.m3u")[1])
        root = self.guide(stack, "salon")
        self.assertEqual(sorted(c.get("id") for c in root.findall("channel")), ["franceinfo.fr", "sport1.fr"])
        self.assertEqual(FakeXtream.simple_epg_calls, [])
        FakeXtream.epg_mode = "error"
        other = self.make_stack(epg_source="xmltv", data_dir=self.tmp.name + "/autre")
        self.create({"slug": "x", "name": "X", "category_ids": ["1"]}, other)
        self.assertEqual(self.get(other.proxy_base + "/p/x.xml")[0], 502)

    def test_epg_status_and_refresh_api(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2"]})
        self.get(self.stack.proxy_base + "/p/salon.xml")
        status = json.loads(self.get(self.stack.web_base + "/api/epg?slug=salon")[1])
        self.assertTrue(status["enabled"])
        self.assertEqual(status["mode"], "auto")
        self.assertTrue(status["source"]["ok"])
        self.assertEqual((status["source"]["channels"], status["source"]["programmes"]), (3, 3))
        pl = status["playlist"]
        self.assertEqual((pl["channels"], pl["xmltv"], pl["api"], pl["pending"]), (3, 2, 1, 0))
        self.assertEqual(pl["missing"], [])
        self.assertNotIn("pw", json.dumps(status))
        general = json.loads(self.get(self.stack.web_base + "/api/epg")[1])
        self.assertIsNone(general["playlist"])
        self.assertEqual(self.get(self.stack.web_base + "/api/epg?slug=../x")[0], 400)
        self.assertEqual(self.get(self.stack.web_base + "/api/epg?slug=inconnue")[0], 404)
        before = FakeXtream.epg_requests
        status, body = self.send(self.stack.web_base + "/api/epg/refresh", "POST", {"slug": "salon"})
        self.assertEqual(status, 202)
        for _ in range(50):
            if FakeXtream.epg_requests > before and FakeXtream.simple_epg_calls.count("43") >= 2:
                break
            time.sleep(0.1)
        self.assertGreater(FakeXtream.epg_requests, before)
        self.assertGreaterEqual(FakeXtream.simple_epg_calls.count("43"), 2)
        self.assertEqual(self.send(self.stack.web_base + "/api/epg/refresh", "POST", {"slug": "../x"})[0], 400)
        self.assertEqual(self.send(self.stack.web_base + "/api/epg/refresh", "POST", {"slug": "salon"},
                                   {"Content-Type": "application/json"})[0], 403)

    def test_epg_disabled_status(self):
        stack = self.make_stack(enable_epg=False)
        status = json.loads(self.get(stack.web_base + "/api/epg?slug=x")[1])
        self.assertFalse(status["enabled"])
        self.assertEqual(self.send(stack.web_base + "/api/epg/refresh", "POST", {})[0], 409)


class XmltvAliasTests(StackTestCase):
    """/xmltv.php : adresse EPG « à la Xtream » utilisée d'office par certains lecteurs."""

    def test_xmltv_php_alias(self):
        status, body, headers = self.get(self.stack.proxy_base + "/xmltv.php?username=x&password=y")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/xml"))
        root = ET.fromstring(body)
        self.assertEqual(sorted(c.get("id") for c in root.findall("channel")), ["franceinfo.fr", "sport1.fr"])
        self.assertEqual(self.get(self.stack.proxy_base + "/epg.xml")[1], body)
        # Aucun appel API chaîne par chaîne sans playlist personnalisée.
        self.assertEqual(FakeXtream.simple_epg_calls, [])
        m3u = self.get(self.stack.proxy_base + "/playlist.m3u")[1].decode()
        self.assertIn('tvg-id="xtream-43"', m3u)

    def test_full_guide_completed_by_api_for_playlist_channels(self):
        FakeXtream.epg_mode = "error"
        self.assertEqual(self.get(self.stack.proxy_base + "/xmltv.php")[0], 502)   # aucune playlist : rien à compléter
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"]})
        status, body, _ = self.get(self.stack.proxy_base + "/xmltv.php")
        self.assertEqual(status, 200)
        root = ET.fromstring(body)
        self.assertEqual([p.find("title").text for p in root.findall("programme")], ["Journal API"])
        self.assertNotIn("43", FakeXtream.simple_epg_calls)       # chaîne hors playlists : pas d'appel

    def test_api_channel_cap(self):
        stack = self.make_stack(epg_api_max_channels=0)
        FakeXtream.epg_mode = "error"
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"]}, stack)
        self.assertEqual(self.get(stack.proxy_base + "/xmltv.php")[0], 502)

    def test_token_and_disabled(self):
        stack = self.make_stack(access_token="tok")
        self.assertEqual(self.get(stack.proxy_base + "/xmltv.php")[0], 403)
        self.assertEqual(self.get(stack.proxy_base + "/xmltv.php?token=tok")[0], 200)
        disabled = self.make_stack(enable_epg=False)
        self.assertEqual(self.get(disabled.proxy_base + "/xmltv.php")[0], 404)

    def test_xmltv_mode_serves_provider_file(self):
        stack = self.make_stack(epg_source="xmltv")
        body = self.get(stack.proxy_base + "/xmltv.php")[1]
        self.assertIn(b"other.fr", body)        # fichier fournisseur tel quel
        self.assertNotIn(b"xtream-43", self.get(stack.proxy_base + "/playlist.m3u")[1])


class BorrowedGuideTests(unittest.TestCase):
    """Une chaîne sans programme emprunte le guide d'une autre version (doublon masqué)."""

    class FakeApiCache:
        def __init__(self, data, cached):
            self.data, self.cached, self.calls = data, set(cached), []

        def known(self, sid):  # ``None`` = jamais demandée
            return self.data.get(sid, []) if sid in self.cached else None

        def episodes_for(self, ids, budget=0):
            self.calls.extend(ids)
            self.cached.update(ids)
            return {sid: self.data.get(sid, []) for sid in ids}, []

        def pending_count(self, ids):
            return 0

        def fetched_at(self, sid):
            return 1.0

    def test_alternate_versions_fill_the_guide(self):
        import tempfile
        from app.epg import EpgCache
        programme = [{"title": "Le 20h", "start": EPG_START, "stop": EPG_END, "desc": ""}]
        api = self.FakeApiCache({"1": [], "2": programme}, cached=["1"])
        with tempfile.TemporaryDirectory() as tmp:
            epg = EpgCache(lambda: None, tmp)
            channels = [{"stream_id": "1", "name": "VIP: TF1 ᴴᴰ", "logo": "", "epg_id": "", "alternates": ["2", "3"]},
                        {"stream_id": "9", "name": "Sans guide", "logo": "", "epg_id": ""}]
            path, stats = epg.playlist_guide("t", channels, mode="api", api=api)
            self.assertEqual(api.calls, ["2", "9"])        # « 1 » connue vide → sa version « 2 » ; « 9 » demandée
            self.assertEqual((stats["api"], [m["stream_id"] for m in stats["missing"]]), (1, ["9"]))
            self.assertIn("Le 20h", path.read_text(encoding="utf-8"))
            self.assertIn('channel="xtream-1"', path.read_text(encoding="utf-8"))


from .helpers import StackTestCase as _Stack  # noqa: E402


class MyGroupsGuideTests(_Stack):
    def test_groups_fetched_first_and_coverage_reported(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2", "10"], "favorites": ["44"],
                     "custom_groups": [{"id": "radio", "name": "Radio", "patterns": ["radio"]}]})
        from app.server import live_channels_in_order
        order = [c["stream_id"] for c in live_channels_in_order(self.stack.api, self.stack.store.get("salon"))]
        self.assertEqual(order[:2], ["44", "43"])         # favori, puis « Mes groupes », puis le reste
        status = json.loads(self.get(self.stack.web_base + "/api/epg?slug=salon")[1])
        self.assertEqual(status["playlist"]["mine"]["channels"], 2)


class NowOnTvTests(_Stack):
    AT = EPG_START + 60   # « Journal » est en cours sur France Info

    def test_now_next_and_search(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "favorites": ["44"]})
        data = json.loads(self.get(self.stack.web_base + f"/api/now?slug=salon&at={self.AT}")[1])
        by_id = {c["id"]: c for c in data["channels"]}
        self.assertEqual(data["channels"][0]["id"], "44")            # favori en premier
        self.assertTrue(data["channels"][0]["mine"])
        self.assertEqual(by_id["42"]["now"]["title"], "Journal")
        self.assertEqual(by_id["42"]["now"]["start"], EPG_START)
        found = json.loads(self.get(self.stack.web_base + f"/api/now?slug=salon&at={self.AT - 3600}&q=journal")[1])
        self.assertEqual([(r["id"], r["title"]) for r in found["results"]], [("42", "Journal")])
        self.assertEqual(self.get(self.stack.web_base + "/api/now?slug=../x")[0], 400)
        self.assertEqual(self.get(self.stack.web_base + "/api/now?slug=inconnue")[0], 404)

    def test_borrowed_guide_and_logo_override(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"],
                     "item_epg": {"44": "42"}, "item_logos": {"44": "https://logos.example/sport.png"}})
        m3u = self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode()
        line = next(l for l in m3u.splitlines() if l.endswith(",Sport 1"))
        self.assertIn('tvg-id="franceinfo.fr"', line)
        self.assertIn('tvg-logo="https://logos.example/sport.png"', line)
        data = json.loads(self.get(self.stack.web_base + f"/api/now?slug=salon&at={self.AT}")[1])
        self.assertEqual({c["id"]: (c["now"] or {}).get("title") for c in data["channels"]}["44"], "Journal")
        for bad in ({"item_logos": {"44": "javascript:alert(1)"}}, {"item_epg": {"44": "abc"}}):
            status = self.send(self.stack.web_base + "/api/playlists", "POST",
                               {"slug": "x", "name": "X", "category_ids": ["1"], **bad})[0]
            self.assertEqual(status, 400, bad)
