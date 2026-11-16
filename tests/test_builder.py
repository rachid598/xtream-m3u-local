"""Personnalisation des playlists : règles, doublons, noms, numéros, favoris."""
import json
import unittest
from urllib.parse import urlencode

from app.builder import add_favorites, base_name, dedupe, quality, rule_matches, select_items
from app.custom import InvalidPlaylist, validate_playlist
from app.server import order_entries

from .helpers import StackTestCase


def ch(sid, name, cat="1"):
    return {"stream_id": str(sid), "name": name, "logo": "", "epg_id": "", "num": "", "category_id": cat,
            "category_ids": [cat], "archive": 0}


class StubAPI:
    def __init__(self, items, categories):
        self.items, self.categories = items, categories

    def catalog(self, refresh=False, kind="live"):
        return self.items, self.categories


class BuilderUnitTests(unittest.TestCase):
    def test_quality_and_base_name(self):
        self.assertEqual([quality(n) for n in ("TF1 4K", "TF1 FHD", "TF1 HD", "TF1", "TF1 SD", "TF1 (1080p)")],
                         [4.0, 3.0, 2.0, 1.5, 1.0, 3.0])
        self.assertEqual(base_name("FR | TF1 HD"), base_name("FR: TF1 (FHD)"))
        self.assertEqual(base_name("Équipe 21 HEVC"), "equipe 21")
        self.assertNotEqual(base_name("TF1 Séries Films"), base_name("TF1"))

    def test_dedupe_keeps_best_per_group(self):
        entries = [(ch(1, "TF1 SD"), "1"), (ch(2, "TF1 FHD"), "1"), (ch(3, "TF1 HD"), "1"),
                   (ch(4, "TF1 HD"), "2"), (ch(5, "M6"), "1")]
        self.assertEqual([e[0]["stream_id"] for e in dedupe(entries)], ["2", "4", "5"])

    def test_rules(self):
        self.assertTrue(rule_matches("FR | Sport", {"match": "prefix", "value": "fr |"}))
        self.assertTrue(rule_matches("Séries Françaises", {"match": "contains", "value": "francaises"}))
        self.assertFalse(rule_matches("UK | Sport", {"match": "prefix", "value": "FR"}))
        self.assertTrue(rule_matches("Sport", {"match": "exact", "value": "SPORT"}))
        self.assertFalse(rule_matches("Sport 2", {"match": "exact", "value": "sport"}))

    def test_select_items_with_rules_names_numbers(self):
        api = StubAPI([ch(1, "TF1 HD", "10"), ch(2, "TF1 SD", "10"), ch(3, "BBC", "20"), ch(4, "M6", "30")],
                      {"10": "FR | Généraliste", "20": "UK | General", "30": "FR | Info"})
        saved = validate_playlist({"slug": "s", "name": "S", "dedupe": True,
                                   "rules": [{"kind": "live", "match": "prefix", "value": "fr |"}],
                                   "item_names": {"4": "M6 (Paris)"}, "item_numbers": {"4": 7}})
        entries, groups = select_items(api, saved, "live")
        self.assertEqual([(e[0]["stream_id"], e[1]) for e in entries], [("1", "10"), ("4", "30")])
        m6 = entries[1][0]
        self.assertEqual((m6["name"], m6["num"], m6.get("custom_num")), ("M6 (Paris)", "7", True))
        self.assertEqual(groups["30"], "FR | Info")

    def test_numbers_order_inside_group_and_favorites_first(self):
        items = [(dict(ch(1, "A"), num="", ), "1"), (dict(ch(2, "B"), num="9", custom_num=True), "1"),
                 (dict(ch(3, "C"), num="3", custom_num=True), "1"), (ch(4, "D", "2"), "2")]
        groups = {"1": "G1", "2": "G2"}
        ordered = order_entries(items, groups)
        self.assertEqual([e[0]["stream_id"] for e in ordered], ["3", "2", "1", "4"])
        saved = {"favorites": ["4", "1"], "favorites_name": "Mes favoris"}
        with_fav = order_entries(add_favorites(ordered, groups, saved), groups)
        self.assertEqual([(e[0]["stream_id"], e[1]) for e in with_fav][:2], [("4", "★"), ("1", "★")])
        self.assertEqual(groups["★"], "Mes favoris")
        self.assertEqual(len(with_fav), 6)

    def test_validation(self):
        bad = [{"rules": [{"kind": "radio", "match": "prefix", "value": "x"}]},
               {"rules": [{"kind": "live", "match": "regex", "value": "x"}]},
               {"rules": [{"kind": "live", "match": "prefix", "value": ""}]},
               {"item_names": {"abc": "x"}}, {"item_names": {"1": "a\nb"}},
               {"item_numbers": {"1": 0}}, {"item_numbers": {"vod:1": 3}}, {"item_numbers": {"1": "3"}},
               {"favorites": ["x"]}, {"favorites_name": "a" * 101}]
        for extra in bad:
            with self.subTest(extra=extra), self.assertRaises(InvalidPlaylist):
                validate_playlist({"slug": "s", "name": "S", **extra})
        ok = validate_playlist({"slug": "s", "name": "S", "favorites": ["42", "vod:7", "series:9"]})
        self.assertEqual((ok["favorites_name"], ok["dedupe"]), ("★ Favoris", False))


class BuilderIntegrationTests(StackTestCase):
    def test_m3u_and_xtream_share_customizations(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "vod_category_ids": ["1"],
                     "rules": [{"kind": "live", "match": "contains", "value": "musique"}],
                     "item_names": {"vod:501": "Mon film", "44": "Sport Un"},
                     "item_numbers": {"44": 1}, "favorites": ["43", "vod:501"], "favorites_name": "Top"})
        data = self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode()
        infos = [line for line in data.splitlines() if line.startswith("#EXTINF")]
        names = [line.rsplit(",", 1)[1] for line in infos]
        # Favoris en tête (ordre des favoris), puis Actualités : Sport Un (n° 1) avant France Info,
        # puis Musique (ajoutée par la règle), puis les films.
        self.assertEqual(names[:2], ['Radio "TV" HACK', "Mon film"])
        self.assertTrue(all('group-title="Top"' in line for line in infos[:2]))
        self.assertEqual(names[2:4], ["Sport Un", "France Info HD"])
        self.assertIn('tvg-chno="1" group-title="Actualités",Sport Un', infos[2])
        self.assertIn("Mon film", names[4:])
        device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "salon"})[1]
        q = {"username": device["username"], "password": device["password"]}
        live = json.loads(self.get(self.stack.proxy_base + "/player_api.php?" + urlencode({**q, "action": "get_live_streams"}))[1])
        self.assertEqual([(c["name"], c["num"]) for c in live][:2], [("Sport Un", 1), ("France Info HD", 2)])
        self.assertIn('Radio "TV"\nHACK'.replace("\n", " "), [c["name"] for c in live])
        vod = json.loads(self.get(self.stack.proxy_base + "/player_api.php?" + urlencode({**q, "action": "get_vod_streams"}))[1])
        self.assertEqual(vod[0]["name"], "Mon film")


if __name__ == "__main__":
    unittest.main()


class SeparatorTests(unittest.TestCase):
    def test_is_separator(self):
        from app.builder import is_separator
        for name in ("#### PRIME ᴿᴬᵂ ⁶⁰ᶠᵖˢ #####", "===== FR =====", "★★★ SPORTS ★★★", "---", "", "   "):
            with self.subTest(name=name):
                self.assertTrue(is_separator(name))
        for name in ("TF1 HD", "FR | Sport", "#1 Hits", "|FR| TF1", "A&E", "UK: BBC ONE LONDON 4K ◉", "M6 ###"):
            with self.subTest(name=name):
                self.assertFalse(is_separator(name))

    def test_hide_separators_option(self):
        api = StubAPI([ch(1, "#### FRANCE ####"), ch(2, "TF1 HD"), ch(3, "===== SPORT =====")], {"1": "FR"})
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": ["1"], "hide_separators": True})
        self.assertEqual([e[0]["stream_id"] for e in select_items(api, saved, "live")[0]], ["2"])
        saved["hide_separators"] = False
        self.assertEqual(len(select_items(api, saved, "live")[0]), 3)
