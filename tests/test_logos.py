"""Logos haute qualité : index iptv-org + tv-logos, rapprochement des noms, cache d'images, routes."""
import gzip
import json
import tempfile
import unittest
from urllib.parse import urlencode

from app import logos
from app.builder import select_items
from app.custom import validate_playlist
from app.logos import LogoLibrary, build_index, country_hint, logo_key, parse_mosaic, squash

from .helpers import StackTestCase

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
IPTV = "https://db.invalid/data"
TVL = "https://tvl.invalid/countries"

CHANNELS_CSV = """id,name,alt_names,network,owners,country,categories,is_nsfw,launched,closed,replaced_by,website
TF1.fr,TF1,,,,FR,general,FALSE,,,,
M6.fr,M6,,,,FR,general,FALSE,,,,
OldCanal.fr,Canal+,,,,FR,general,FALSE,,2001-01-01,,
FranceInfo.fr,franceinfo:,France Info,,,FR,news,FALSE,,,,
LaUne.be,La Une,RTBF La Une,,,BE,general,FALSE,,,,
2M.ma,2M,2M National,2M,,MA,general,FALSE,,,,
Medi1TVArabic.ma,Medi1TV Arabic,,Medi1TV,,MA,news,FALSE,,,,
SkyNews.uk,Sky News,,,,UK,news,FALSE,,,,
Hot.fr,Hot TV,,,,FR,xxx,TRUE,,,,
"""
LOGOS_CSV = """channel,feed,in_use,tags,width,height,format,url
TF1.fr,,TRUE,,200,100,PNG,https://img.invalid/tf1-small.png
TF1.fr,,TRUE,,1000,500,SVG,https://img.invalid/tf1.svg
M6.fr,,TRUE,,300,300,PNG,https://img.invalid/m6.png
M6.fr,,TRUE,,900,900,PNG,https://img.invalid/m6-big.png
M6.fr,,FALSE,,2000,2000,PNG,https://img.invalid/m6-old.png
M6.fr,HD,TRUE,,3000,3000,PNG,https://img.invalid/m6-feed.png
OldCanal.fr,,TRUE,,512,512,PNG,https://img.invalid/old-canal.png
FranceInfo.fr,,TRUE,,512,512,PNG,https://img.invalid/franceinfo.png
LaUne.be,,TRUE,,512,512,PNG,https://img.invalid/laune.png
2M.ma,,TRUE,,483,255,PNG,https://img.invalid/2m.png
Medi1TVArabic.ma,,TRUE,,400,400,PNG,https://img.invalid/medi1.png
SkyNews.uk,,TRUE,,512,512,PNG,https://img.invalid/sky.png
Hot.fr,,TRUE,,512,512,PNG,https://img.invalid/hot.png
"""
FR_MOSAIC = """# France
| ![tf1] | ![canal-plus] |
[tf1]:tf1-fr.png
[tf1-hd]:hd/tf1-hd-fr.png
[canal-plus]:canal-plus-fr.png
[bein-sports-1-french]:bein-sports-1-french-fr.png
[evil]:../../etc/passwd.png
"""


def fake_fetch(files: dict, calls: list | None = None):
    def fetch(url, limit):
        if calls is not None:
            calls.append(url)
        if url not in files:
            raise OSError("introuvable")
        value = files[url]
        if isinstance(value, Exception):
            raise value
        return value, "application/octet-stream"
    return fetch


def sources(**extra):
    files = {f"{IPTV}/channels.csv": CHANNELS_CSV.encode(), f"{IPTV}/logos.csv": LOGOS_CSV.encode(),
             f"{TVL}/france/0_all_logos_mosaic.md": FR_MOSAIC.encode(),
             f"{TVL}/belgium/0_all_logos_mosaic.md": b"# Belgique\n"}
    files.update(extra)
    return files


class HelperTests(unittest.TestCase):
    def test_country_hint(self):
        self.assertEqual(country_hint("FR: TF1 HD"), "FR")
        self.assertEqual(country_hint("|BE| La Une"), "BE")
        self.assertEqual(country_hint("UK: Sky News"), "UK")
        self.assertEqual(country_hint("Action", "US | Entertainment"), "US")
        self.assertEqual(country_hint("2M Monde", "|AR| MAROC"), "MA")
        self.assertEqual(country_hint("TF1", "FRANCE FHD | OTT"), "FR")
        self.assertEqual(country_hint("TF1", "Généralistes"), None)

    def test_squash(self):
        self.assertEqual(squash("canal+ sport"), squash("canal plus sport"))
        self.assertEqual(squash("bfm tv"), "bfmtv")

    def test_parse_mosaic_skips_unsafe_paths_and_adds_french_alias(self):
        found = parse_mosaic(FR_MOSAIC, "FR", TVL + "/france")
        urls = [f["url"] for f in found]
        self.assertIn(f"{TVL}/france/hd/tf1-hd-fr.png", urls)
        self.assertFalse(any("passwd" in u for u in urls))
        bein = next(f for f in found if "bein" in f["url"])
        self.assertIn("bein sports 1", bein["names"])


class IndexTests(unittest.TestCase):
    def setUp(self):
        extra = parse_mosaic(FR_MOSAIC, "FR", TVL + "/france")
        self.index = build_index(CHANNELS_CSV, LOGOS_CSV, ("FR", "BE", "MA"), extra)

    def url_for(self, name):
        keys = self.index["names"].get(squash(name))
        return self.index["logos"][keys[0]][0] if keys else None

    def test_prefers_tvlogos_then_plain_logo_over_hd_variant(self):
        self.assertEqual(self.url_for("tf1"), f"{TVL}/france/tf1-fr.png")

    def test_iptv_org_best_logo_current_main_png_largest(self):
        self.assertEqual(self.url_for("m6"), "https://img.invalid/m6-big.png")

    def test_open_channel_before_closed(self):
        self.assertEqual(self.url_for("canal+"), f"{TVL}/france/canal-plus-fr.png")

    def test_alt_names_network_and_filters(self):
        self.assertEqual(self.url_for("france info"), "https://img.invalid/franceinfo.png")
        self.assertEqual(self.url_for("2m national"), "https://img.invalid/2m.png")
        self.assertEqual(self.url_for("medi1tv"), "https://img.invalid/medi1.png")  # nom du réseau
        self.assertIsNone(self.url_for("sky news"))   # pays non choisi
        self.assertIsNone(self.url_for("hot tv"))     # contenu adulte exclu
        self.assertNotIn("https://img.invalid/tf1.svg", [v[0] for v in self.index["logos"].values()])


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.files = sources()
        self.calls = []
        self.lib = LogoLibrary(self.tmp.name, source=IPTV, tvlogos=TVL, fetch=fake_fetch(self.files, self.calls))

    def tearDown(self):
        logos.install(None)
        self.tmp.cleanup()

    def test_refresh_persist_restore(self):
        self.assertIsNone(self.lib.match("FR: TF1 HD"))  # pas encore d'index
        self.assertTrue(self.lib.refresh())
        self.assertEqual(self.lib.status()["error"], "")
        key = self.lib.match("FR: TF1 HD")
        self.assertEqual(self.lib.url_of(key), f"{TVL}/france/tf1-fr.png")
        with gzip.open(self.lib.index_file, "rt", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["countries"], ["FR", "BE", "MA"])
        again = LogoLibrary(self.tmp.name, source=IPTV, tvlogos=TVL, fetch=fake_fetch({}))
        self.assertEqual(again.match("FR: TF1 HD"), key)  # relu du disque, sans réseau
        other = LogoLibrary(self.tmp.name, countries=("BE",), source=IPTV, tvlogos=TVL, fetch=fake_fetch({}))
        self.assertIsNone(other.match("TF1"))  # pays différents : index à reconstruire
        calls = len(self.calls)
        self.lib.refresh()
        self.assertEqual(len(self.calls), calls)  # index récent : pas de nouveau téléchargement

    def test_match_respects_country_prefix(self):
        self.lib.refresh()
        self.assertIsNotNone(self.lib.match("MA: 2M"))
        self.assertIsNotNone(self.lib.match("FR: BEIN SPORTS 1 HD"))
        self.assertIsNone(self.lib.match("UK: TF1"))                 # autre pays : jamais remplacé
        self.assertIsNone(self.lib.match("TF1", "DE | Deutschland"))
        self.assertIsNotNone(self.lib.match("TF1", "FR | Généralistes"))
        self.assertIsNone(self.lib.match("Chaîne inconnue"))

    def test_partial_source_failure_still_builds_index(self):
        self.files[f"{IPTV}/logos.csv"] = OSError("hors ligne")
        self.assertTrue(self.lib.refresh())
        self.assertIn("iptv-org", self.lib.status()["error"])
        self.assertIsNotNone(self.lib.match("TF1"))
        self.assertIsNone(self.lib.match("M6"))  # source iptv-org absente

    def test_total_failure_keeps_previous_index_and_backs_off(self):
        self.lib.refresh()
        self.files.clear()
        self.assertTrue(self.lib.refresh(force=True))
        self.assertIsNotNone(self.lib.match("TF1"))
        self.assertTrue(self.lib.status()["error"])
        calls = len(self.calls)
        self.lib._built_at = 0  # index périmé, mais nouvel essai seulement après le délai
        self.lib.refresh()
        self.assertEqual(len(self.calls), calls)

    def test_image_download_cache_and_validation(self):
        self.lib.refresh()
        tf1 = self.lib.match("TF1")
        self.files[f"{TVL}/france/tf1-fr.png"] = PNG
        path, url = self.lib.image(tf1)
        self.assertTrue(path and path.read_bytes() == PNG)
        calls = len(self.calls)
        self.assertEqual(self.lib.image(tf1)[0], path)
        self.assertEqual(len(self.calls), calls)  # servi depuis le cache
        m6 = self.lib.match("M6")
        self.files["https://img.invalid/m6-big.png"] = b"<html>pas une image</html>"
        self.assertEqual(self.lib.image(m6), (None, "https://img.invalid/m6-big.png"))
        calls = len(self.calls)
        self.lib.image(m6)
        self.assertEqual(len(self.calls), calls)  # échec mémorisé : pas de nouvel essai immédiat
        self.assertEqual(self.lib.image("0123456789abcdef"), (None, None))
        self.assertEqual(self.lib.image("../../etc/passwd"), (None, None))

    def test_countries_from_env(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"LOGO_COUNTRIES": "fr, be;ch,xyz"}):
            self.assertEqual(logos.countries_from_env(), ("FR", "BE", "CH"))
        with mock.patch.dict(os.environ, {"LOGO_COUNTRIES": ""}):
            self.assertIsNone(logos.from_env(self.tmp.name))


class StubAPI:
    def __init__(self, items, categories):
        self.items, self.categories = items, categories

    def catalog(self, refresh=False, kind="live"):
        return self.items, self.categories


def ch(sid, name, cat="1", logo="http://provider.invalid/x.png"):
    return {"stream_id": str(sid), "name": name, "logo": logo, "epg_id": "", "num": "", "category_id": cat,
            "category_ids": [cat], "archive": 0}


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = LogoLibrary(self.tmp.name, source=IPTV, tvlogos=TVL, fetch=fake_fetch(sources()))
        self.lib.refresh()
        self.lib.prefetch = lambda keys: None  # pas de téléchargement en arrière-plan ici
        logos.install(self.lib)
        self.api = StubAPI([ch(1, "FR: TF1 FHD"), ch(2, "UK: Sky News"), ch(3, "FR: M6 HD")], {"1": "FR | TNT"})

    def tearDown(self):
        logos.install(None)
        self.tmp.cleanup()

    def test_hq_logos_on_by_default_manual_logo_wins(self):
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": ["1"],
                                   "item_logos": {"3": "https://mon.logo/m6.png"}})
        self.assertTrue(saved["hq_logos"])
        entries = {item["stream_id"]: item for item, _ in select_items(self.api, saved, "live")[0]}
        self.assertEqual(entries["1"]["logo_hq"], f"/logo/{logo_key(f'{TVL}/france/tf1-fr.png')}.png")
        self.assertEqual(entries["1"]["logo"], "http://provider.invalid/x.png")  # gardé pour le guide XMLTV
        self.assertNotIn("logo_hq", entries["2"])
        self.assertNotIn("logo_hq", entries["3"])
        self.assertEqual(entries["3"]["logo"], "https://mon.logo/m6.png")

    def test_option_disabled(self):
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": ["1"], "hq_logos": False})
        self.assertFalse(any("logo_hq" in item for item, _ in select_items(self.api, saved, "live")[0]))


class LogoHttpTests(StackTestCase):
    def setUp(self):
        super().setUp()
        files = sources()
        files[f"{IPTV}/channels.csv"] = (CHANNELS_CSV + "FI.fr,France Info,,,,FR,news,FALSE,,,,\n").encode()
        files[f"{TVL}/france/tf1-fr.png"] = PNG
        self.lib = LogoLibrary(self.tmp.name, source=IPTV, tvlogos=TVL, fetch=fake_fetch(files))
        self.lib.refresh()
        logos.install(self.lib)

    def tearDown(self):
        logos.install(None)
        super().tearDown()

    def test_m3u_xtream_and_routes(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"]})
        key = self.lib.match("France Info HD")
        self.assertIsNotNone(key)
        data = self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode()
        self.assertIn(f'tvg-logo="{self.stack.proxy_base}/logo/{key}.png"', data)
        device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "salon"})[1]
        q = {"username": device["username"], "password": device["password"], "action": "get_live_streams"}
        live = json.loads(self.get(self.stack.proxy_base + "/player_api.php?" + urlencode(q))[1])
        info = next(c for c in live if c["name"] == "France Info HD")
        self.assertEqual(info["stream_icon"], f"{self.stack.proxy_base}/logo/{key}.png")
        # Image introuvable : redirection vers l'original ; image en cache : servie localement.
        status, _, headers = self.get_no_redirect(f"{self.stack.proxy_base}/logo/{key}.png")
        self.assertEqual((status, headers.get("Location")), (302, "https://img.invalid/franceinfo.png"))
        tf1 = self.lib.match("TF1")
        for base in (self.stack.proxy_base, self.stack.web_base):
            status, body, headers = self.get(f"{base}/logo/{tf1}.png")
            self.assertEqual((status, body, headers["Content-Type"]), (200, PNG, "image/png"))
            self.assertEqual(self.get(f"{base}/logo/0123456789abcdef.png")[0], 404)
        catalog = json.loads(self.get(self.stack.web_base + "/api/catalog?kind=live")[1])
        france_info = next(c for c in catalog["channels"] if c["name"] == "France Info HD")
        self.assertEqual(france_info["logo_hq"], f"/logo/{key}.png")
        status = json.loads(self.get(self.stack.web_base + "/api/status")[1])
        self.assertEqual(status["logos"]["countries"], ["FR", "BE", "MA"])
        code, results = self.send(self.stack.web_base + "/api/diagnostic", "POST")
        logo_result = next(r for r in results["results"] if r["id"] == "logos") if isinstance(results, dict) else \
            next(r for r in results if r["id"] == "logos")
        self.assertEqual((code, logo_result["status"]), (200, "ok"))
        self.assertRegex(logo_result["detail"], r"\d+/\d+ chaînes des playlists ont un logo haute qualité")

    @staticmethod
    def get_no_redirect(url):
        import urllib.request
        from urllib.error import HTTPError

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(url, timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)


if __name__ == "__main__":
    unittest.main()
