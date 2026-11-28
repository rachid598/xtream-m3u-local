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
        for name in ("#### PRIME ᴿᴬᵂ ⁶⁰ᶠᵖˢ #####", "===== FR =====", "★★★ SPORTS ★★★", "---", "", "   ",
                     "♣♦♣----|FR| FRANCE HD |FR|----♣♦♣", "•●★---|FR| CH. CANAL+ HD |FR|---★●•",
                     "•●★-|FR| DIVERTISSEMENT HD |FR|-★●•",
                     "- NO EVENT STREAMING - | 4K EXCLUSIVE | 12", "NO EVENT STREAMING NOW - | 4K EXCLUSIVE 3"):
            with self.subTest(name=name):
                self.assertTrue(is_separator(name))
        for name in ("TF1 HD", "FR | Sport", "#1 Hits", "|FR| TF1", "A&E", "UK: BBC ONE LONDON 4K ◉", "M6 ###",
                     "|FR| TF1 HD", "|FR| NICKELODEON +1 HD", "|FR| LILO & STITCH [VOD]", "|FR| DISNEY+ PIXAR [VOD]",
                     "NO: RIKSTOTO DIREKTE 1 ᴿᴬᵂ ⱽᴵᴾ", "NO V SPORT PREMIER LEAGUE 2 ⱽᴵᴾ", "FR| SOCCER: PSG - OM"):
            with self.subTest(name=name):
                self.assertFalse(is_separator(name))

    def test_hide_separators_option(self):
        api = StubAPI([ch(1, "#### FRANCE ####"), ch(2, "TF1 HD"), ch(3, "===== SPORT =====")], {"1": "FR"})
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": ["1"], "hide_separators": True})
        self.assertEqual([e[0]["stream_id"] for e in select_items(api, saved, "live")[0]], ["2"])
        saved["hide_separators"] = False
        self.assertEqual(len(select_items(api, saved, "live")[0]), 3)


class CustomGroupTests(unittest.TestCase):
    API = StubAPI([ch(1, "FR: TF1 FHD", "10"), ch(2, "FR: CANAL+ SPORT", "10"), ch(3, "FR: France 24", "20"),
                   ch(4, "FR: France 2 HD", "20"), ch(5, "|FR| CANAL + 4K", "10"), ch(6, "FR: TF1 HD", "30"),
                   ch(7, "FR: Gulli", "30")],
                  {"10": "FR | Premium", "20": "FR | Info", "30": "FR | 4K"})

    def saved(self, **extra):
        return validate_playlist({"slug": "s", "name": "S", "category_ids": ["10", "20", "30"], **extra})

    def test_match_name(self):
        from app.builder import match_name
        self.assertEqual([match_name(n) for n in ("FR: TF1 FHD", "|FR| CANAL + 4K", "[FR] L'Équipe HD ◉", "FR TF1",
                                                  "CANAL+ SPORT ᴿᴬᵂ", "BFM - TV")],
                         ["tf1", "canal+", "l equipe", "tf1", "canal+ sport", "bfm tv"])

    def test_groups_first_ordered_by_patterns(self):
        saved = self.saved(custom_groups=[
            {"id": "tnt", "name": "TNT", "match": "exact", "patterns": ["TF1", "France 2", "Canal+"]},
            {"id": "canal", "name": "Canal+", "match": "words", "patterns": ["canal+"]}])
        entries, groups = select_items(self.API, saved, "live")
        self.assertEqual([(e[0]["stream_id"], e[1]) for e in entries],
                         [("1", "g:tnt"), ("6", "g:tnt"), ("4", "g:tnt"), ("5", "g:tnt"), ("2", "g:canal"),
                          ("3", "20"), ("7", "30")])
        self.assertEqual(list(groups)[:2], ["g:tnt", "g:canal"])
        ordered = order_entries(entries, groups, saved["group_order"])
        self.assertEqual([e[0]["stream_id"] for e in ordered], ["1", "6", "4", "5", "2", "3", "7"])

    def test_priority_follows_group_order_and_manual_moves(self):
        saved = self.saved(
            custom_groups=[{"id": "tnt", "name": "TNT", "match": "exact", "patterns": ["canal+"]},
                           {"id": "canal", "name": "Canal+", "match": "words", "patterns": ["canal+"]},
                           {"id": "kids", "name": "Enfants", "patterns": []}],
            group_order=["g:canal", "g:tnt"], item_groups={"7": "kids", "2": "-", "1": "deleted"})
        self.assertEqual(saved["item_groups"], {"7": "kids", "2": "-"})
        entries, _ = select_items(self.API, saved, "live")
        placed = {e[0]["stream_id"]: e[1] for e in entries}
        self.assertEqual((placed["5"], placed["2"], placed["7"], placed["1"]), ("g:canal", "10", "g:kids", "10"))

    def test_numbered_patterns_like_a_decoder(self):
        from app.builder import split_pattern
        self.assertEqual(split_pattern("4: Canal+"), (4, "Canal+"))
        self.assertEqual(split_pattern("6ter"), (None, "6ter"))
        self.assertEqual(split_pattern("13ème Rue"), (None, "13ème Rue"))
        saved = self.saved(dedupe=True, item_numbers={"4": 99}, custom_groups=[
            {"id": "c", "name": "Bouquet", "match": "exact", "patterns": ["1: TF1", "France 2", "4 = Canal+"]}])
        entries, groups = select_items(self.API, saved, "live")
        bouquet = [(e[0]["name"], e[0]["num"]) for e in entries if e[1] == "g:c"]
        # Numéro du motif, sauf numéro personnalisé (✎) qui reste prioritaire.
        self.assertEqual(bouquet, [("FR: TF1 FHD", "1"), ("FR: France 2 HD", "99"), ("|FR| CANAL + 4K", "4")])
        ordered = [e[0]["name"] for e in order_entries(entries, groups, saved["group_order"]) if e[1] == "g:c"]
        self.assertEqual(ordered, ["FR: TF1 FHD", "|FR| CANAL + 4K", "FR: France 2 HD"])

    def test_dedupe_with_superscripts_and_prefixes(self):
        # Noms réels d'un fournisseur (capture utilisateur) : qualité en exposant, préfixes VIP/FR/AF.
        api = StubAPI([ch(1, "VIP: TF1 ᴿᴬᵂ", "1"), ch(2, "VIP: TF1 ᴴᴰ", "1"), ch(3, "FR: TF1", "2"), ch(4, "AF: TF1", "3"),
                       ch(5, "VIP: FRANCE 2 ᴴᴰ", "1"), ch(6, "FR: FRANCE 2", "2")], {"1": "VIP", "2": "FR", "3": "AF"})
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": ["1", "2", "3"], "dedupe": True,
                                   "custom_groups": [{"id": "b", "name": "Bouquet", "match": "exact", "patterns": ["1: TF1", "2: France 2"]}]})
        entries, _ = select_items(api, saved, "live")
        self.assertEqual([(e[0]["name"], e[0]["num"]) for e in entries], [("VIP: TF1 ᴴᴰ", "1"), ("VIP: FRANCE 2 ᴴᴰ", "2")])

    def test_dedupe_inside_custom_group(self):
        saved = self.saved(dedupe=True, custom_groups=[{"id": "tnt", "name": "TNT", "match": "exact", "patterns": ["tf1"]}])
        entries, _ = select_items(self.API, saved, "live")
        self.assertEqual([e[0]["stream_id"] for e in entries if e[1] == "g:tnt"], ["1"])

    def test_validation(self):
        for groups in ([{"id": "A B", "name": "x"}], [{"id": "a", "name": ""}], [{"id": "a", "name": "x", "match": "regex"}],
                       [{"id": "a", "name": "x"}, {"id": "a", "name": "y"}], [{"id": "a", "name": "x", "patterns": "tf1"}]):
            with self.subTest(groups=groups), self.assertRaises(InvalidPlaylist):
                self.saved(custom_groups=groups)
        with self.assertRaises(InvalidPlaylist):
            self.saved(custom_groups=[{"id": "a", "name": "x"}], item_groups={"abc": "a"})
        saved = self.saved(custom_groups=[{"id": "a", "name": " TNT ", "patterns": ["tf1", " ", "tf1"]}])
        self.assertEqual(saved["custom_groups"], [{"id": "a", "name": "TNT", "match": "words", "patterns": ["tf1"]}])


class RegionalGroupTests(unittest.TestCase):
    def test_regions_aliases_and_catalogue_preview(self):
        from app.web import catalog_payload

        categories = {"1": "BEIN SPORTS | OTT", "2": "FR| FRANCE SPORT ⱽᴵᴾ", "3": "AR| BEIN SPORTS F",
                      "4": "ARABIC SPORTS | OTT", "5": "FR| OQEE ARABIC", "6": "MBC GROUPE | OTT"}
        channels = [ch(1, "|BEIN FR| BEIN SPORTS 1 HD", "1"), ch(2, "|BEIN AR| BEIN SPORTS HD 1", "1"),
                    ch(3, "VIP: BEIN SPORTS 1 ᴿᴬᵂ", "2"), ch(4, "F: beIN SP⚽RTS 1 ᴴᴰ", "3"),
                    ch(5, "AT: BEIN SPORTS 1", "4"), ch(6, "|FR| MAJID ꜰʜᴅ", "5"),
                    ch(7, "MBC Masr 1ᴴᴰ", "6"), ch(8, "F: beIN SP⚽RTS 1 FRENCH ᴴᴰ", "3")]
        payload = catalog_payload(channels, categories)
        self.assertEqual([c.get("regional") for c in payload["channels"]],
                         [("fr", "bein sports 1"), ("ar", "bein sports 1"), ("fr", "bein sports 1"),
                          ("ar", "bein sports 1"), None, ("ar", "majid tv"), ("ar", "mbc masr"), None])

    def test_group_order_and_failover_keep_bein_editions_separate(self):
        categories = {"1": "FR| SPORT", "2": "AR| BEIN SPORTS F", "3": "BEIN SPORTS | OTT"}
        api = StubAPI([ch(1, "FR: beIN SPORTS 1 HD", "1"), ch(2, "F: beIN SP⚽RTS 1 FHD", "2"),
                       ch(3, "|BEIN FR| beIN SPORT 1 SD", "3"), ch(4, "|BEIN AR| beIN SPORTS 1 HD", "3"),
                       ch(5, "FR: Canal+ Foot HD", "1")], categories)
        groups = [{"id": "fr", "name": "Sport FR", "region": "fr", "match": "exact",
                   "patterns": ["beIN Sports 1", "Canal+ Foot"]},
                  {"id": "ar", "name": "Sport AR", "region": "ar", "match": "exact", "patterns": ["beIN Sports 1"]},
                  {"id": "nilesat", "name": "Nilesat", "region": "ar", "match": "exact", "patterns": ["beIN Sports 1"]}]
        data = {"slug": "s", "name": "S", "category_ids": ["1", "2"], "custom_groups": groups,
                "dedupe": True, "failover": True, "failover_any_category": True}
        saved = validate_playlist({**data, "group_order": ["g:ar", "g:fr", "g:nilesat"]})
        entries, labels = select_items(api, saved, "live")
        ordered = order_entries(entries, labels, saved["group_order"])
        self.assertEqual([(item["stream_id"], group) for item, group in ordered], [("2", "g:ar"), ("1", "g:fr"), ("5", "g:fr")])
        self.assertEqual([item.get("alternates") for item, _ in ordered[:2]], [["4"], ["3"]])
        self.assertFalse(ordered[0][0]["epg_match_name"])
        changed = validate_playlist({**data, "group_order": ["g:nilesat", "g:fr", "g:ar"]})
        self.assertEqual(select_items(api, changed, "live")[0][0][1], "g:nilesat")


class CustomGroupIntegrationTests(StackTestCase):
    def test_m3u_and_xtream(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "10"],
                     "custom_groups": [{"id": "info", "name": "Mes infos", "match": "words", "patterns": ["france info"]}],
                     "item_groups": {"44": "info"}})
        infos = [line for line in self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode().splitlines()
                 if line.startswith("#EXTINF")]
        self.assertTrue(infos[0].endswith('group-title="Mes infos",France Info HD'), infos[0])
        self.assertTrue(infos[1].endswith('group-title="Mes infos",Sport 1'), infos[1])
        device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "salon"})[1]
        q = {"username": device["username"], "password": device["password"]}
        cats = json.loads(self.get(self.stack.proxy_base + "/player_api.php?" + urlencode({**q, "action": "get_live_categories"}))[1])
        self.assertEqual(cats[0]["category_name"], "Mes infos")
        self.assertTrue(cats[0]["category_id"].isdigit())
        live = json.loads(self.get(self.stack.proxy_base + "/player_api.php?" + urlencode(
            {**q, "action": "get_live_streams", "category_id": cats[0]["category_id"]}))[1])
        self.assertEqual([s["stream_id"] for s in live], [42, 44])


class OttPresetTests(unittest.TestCase):
    """Le modèle « OTT » de l'interface couvre exactement les 112 chaînes (Généralistes → Jeunesse / Pitchoun Kids Music)."""

    PROVIDER = [
        "|FR| TF1 HD", "|FR| FRANCE 2 HD", "|FR| RTS DEUX HD", "|FR| CANAL+ SPORT 360 HD", "|FR| CANAL+ CINEMA(S) HD",
        "|FR| CINE+ OCS HD", "|FR| CANAL PLAY 10 HD", "|FR| 13EME RUE HD", "|FR| TF1 SERIES FILMS HD", "|FR| NOVO19 HD",
        "|FR| NATIONAL GEO HD", "|FR| NATIONAL GEO WILD HD", "|FR| DISCOVERY ID HD", "|FR| MUSEUM HD", "|FR| HISTOIRE HD",
        "|FR| TOUTE L'HISTOIRE HD", "|FR| SCIENCE & VIE HD", "|FR| NICKELODEON +1 HD", "|FR| PITCHOUN KIDS MUSIC HD",
    ]

    def patterns(self):
        import pathlib
        import re as _re
        js = (pathlib.Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
        block = _re.search(r"const OTT = `([^`]*)`", js).group(1)
        return [p.strip() for p in block.split(",") if p.strip()]

    def test_every_listed_channel_matches_its_own_pattern_in_order(self):
        from app.builder import group_matches, match_name, ordered_custom_groups, is_separator
        patterns = self.patterns()
        self.assertEqual(len(patterns), 112)   # 534 à 652 sans les séparateurs (s'arrête à Pitchoun Kids Music)
        group = ordered_custom_groups({"custom_groups": [{"id": "ott", "name": "OTT", "match": "exact", "patterns": patterns}]})[0]
        ranks = []
        for name in self.PROVIDER:
            rank = group_matches(match_name(name), group)
            self.assertIsNotNone(rank, name)
            ranks.append(rank)
        self.assertEqual(ranks, sorted(ranks), "ordre du fournisseur conservé")
        # Les séparateurs du fournisseur ne correspondent à aucun nom de la liste.
        for sep in ("♣♦♣----|FR| FRANCE HD |FR|----♣♦♣", "•●★---|FR| CH. CANAL+ HD |FR|---★●•"):
            self.assertTrue(is_separator(sep))
        # Les chaînes après Pitchoun Kids Music (VOD, Info, Sport, Musique) ne rentrent pas dans OTT.
        for excluded in ("|FR| BABY TV HD", "|FR| PEPPA PIG HD", "|FR| BFM TV HD", "|FR| BEIN SPORTS 1 HD", "|FR| PADEL TIME TV HD"):
            self.assertIsNone(group_matches(match_name(excluded), group), excluded)
        # Une autre version de TF1 (VIP 4K) tombe dans le même groupe : elle devient une version de secours.
        self.assertEqual(group_matches(match_name("VIP: TF1 ᴿᴬᵂ"), group), 0)
        self.assertIsNone(group_matches(match_name("|FR| TF1 SERIES FILMS HD"), {**group, "_patterns": ["tf1"], "_numbers": [None]}))

    def test_aliases_cover_a_second_provider_with_different_wording(self):
        """Mêmes chaînes écrites autrement chez un 2e fournisseur réel : reconnues grâce aux alias ajoutés."""
        from app.builder import group_matches, match_name, ordered_custom_groups
        patterns = self.patterns()
        group = ordered_custom_groups({"custom_groups": [{"id": "ott", "name": "OTT", "match": "exact", "patterns": patterns}]})[0]
        second_provider = [
            "VIP: NATIONAL GEOGRAPHIC ᴴᴰ", "VIP: NAT GEO WILD ᴴᴰ", "FR: CANAL+ Cinema 4K",
            "FR: TV5MONDE EUROPE HEVC", "FR: HISTOIRE TV HD", "FR: MUSEUM TV HD",
            "FR: DISCOVERY INVESTIGATION HD", "FR: 13.EME RUE HD", "FR: SCIENCE ET VIE TV HD",
        ]
        for name in second_provider:
            with self.subTest(name=name):
                self.assertIsNotNone(group_matches(match_name(name), group), name)


class MarocPresetTests(unittest.TestCase):
    """Le modèle « Maroc » couvre toutes les variantes réelles d'un fournisseur AR| MOROCCO sans faux positifs."""

    @staticmethod
    def _maroc_patterns() -> list[str]:
        import ast, re
        from pathlib import Path
        js = Path(__file__).resolve().parents[1].joinpath("app/static/app.js").read_text(encoding="utf-8")
        match = re.search(r"\{\s*name:\s*'Maroc'[^\[]*patterns:\s*\[(.*?)\]\s*\}", js, re.S)
        assert match, "Modèle Maroc introuvable dans app.js"
        return [ast.literal_eval(s) for s in re.findall(r"'(?:\\'|[^'])*'", match.group(1))]

    def test_all_real_morocco_provider_channels_match_and_non_arab_tv5_excluded(self):
        cat = "AR| MOROCCO ᴿᴬᵂ ▶ المغرب"
        real_names = [
            "AR: Al Aoula Inter ᴿᴬᵂ ◉", "AR: Al Aoula Inter ᴴᴰ", "AR: Al Aoula Inter SD",
            "AR: Al Aoula Laayoune ◉", "AR: 2M Natioanl ◉", "AR: 2M Monde",
            "AR: Arryadia Live ᴿᴬᵂ", "AR: Arryadia Live ᴴᴰ ◉", "AR: Arryadia Live SD",
            "AR: Arryadia 1 ᴿᴬᵂ", "AR: Arryadia 1 ᴴᴰ", "AR: Arryadia 2 ᴿᴬᵂ", "AR: Arryadia 2 ᴴᴰ",
            "AR: Arryadia 3 ᴿᴬᵂ", "AR: Arryadia 3 ᴴᴰ",
            "AR: ARRYADIA TNT ᴿᴬᵂ *LOCAL*", "AR: ARRYADIA TNT ᴴᴰ *LOCAL*",
            "AR: ARRYADIA OLYMPICS ᴿᴬᵂ", "AR: ARRYADIA OLYMPICS ᴴᴰ",
            "AR: Al Maghribia ᴿᴬᵂ ◉", "AR: Al Maghribia", "AR: Athaqafia ᴿᴬᵂ ◉",
            "AR: Assadissa Moroc ᴿᴬᵂ", "AR: Assadissa Moroc ᴴᴰ", "AR: Assadissa Moroc",
            "AR: Al Ons TV", "AR: AFLAM 7 ᴿᴬᵂ *LOCAL*", "AR: AFLAM 7 ᴴᴰ *LOCAL*",
            "AR: Tamazight ᴿᴬᵂ ◉", "AR: Tamazight Moroc",
            "AR: MBC 5 ᴴᴰ", "AR: MBC 5 SD", "AR: MBC 5 ᴿᴬᵂ",
            "AR: M24 MAROC", "AR: Medi 1 Maghreb TV ᴿᴬᵂ ◉", "AR: Medi 1 Arabic TV",
            "AR: Tele Moroc ᴿᴬᵂ", "AR: Chada TV", "AR: TV5", "AR: OMAR PECHE 24/7 4K",
        ]
        items = [ch(100 + i, name, "1") for i, name in enumerate(real_names)]
        items += [ch(901, "FR: TV5", "2"), ch(902, "PH: TV5", "3")]
        cats = {"1": cat, "2": "FR | Généralistes", "3": "PH | Philippines"}
        saved = validate_playlist({
            "slug": "maroc", "name": "Maroc", "category_ids": ["1", "2", "3"],
            "custom_groups": [{"id": "maroc", "name": "Maroc", "match": "exact", "region": "ar",
                               "patterns": self._maroc_patterns()}],
        })
        entries, _ = select_items(StubAPI(items, cats), saved, "live")
        by_name = {item["name"]: grp for item, grp in entries}
        for name in real_names:
            with self.subTest(name=name):
                self.assertEqual(by_name[name], "g:maroc", f"{name} devrait être dans g:maroc")
        self.assertEqual(by_name["FR: TV5"], "2")
        self.assertEqual(by_name["PH: TV5"], "3")


class FailoverAnyCategoryTests(unittest.TestCase):
    """Option ``failover_any_category`` : secours cherchés aussi hors des catégories cochées."""

    CATALOG = [
        ch(1, "TF1 FHD", "1"), ch(2, "M6 FHD", "1"),                                # catégorie 1 (cochée)
        ch(3, "TF1 HD", "2"), ch(4, "TF1 SD", "3"), ch(5, "TF1 HEVC", "4"),          # 2, 3, 4 non cochées
        ch(6, "#### FRANCE HD #####", "2"),                                         # séparateur : jamais un secours
    ]
    CATEGORIES = {"1": "France FHD", "2": "France HD", "3": "France SD", "4": "France HEVC"}

    def saved(self, **extra):
        return validate_playlist({"slug": "s", "name": "S", "category_ids": ["1"], "dedupe": True, **extra})

    def test_disabled_by_default_no_alternates_from_unchecked_categories(self):
        api = StubAPI(self.CATALOG, self.CATEGORIES)
        entries, _ = select_items(api, self.saved(), "live")
        tf1 = next(item for item, _ in entries if item["stream_id"] == "1")
        self.assertEqual(tf1.get("alternates"), None)  # seule version sélectionnée : rien à masquer

    def test_enabled_finds_versions_in_unchecked_categories_best_first(self):
        api = StubAPI(self.CATALOG, self.CATEGORIES)
        entries, _ = select_items(api, self.saved(failover=True, failover_any_category=True), "live")
        self.assertEqual([item["stream_id"] for item, _ in entries], ["1", "2"])  # playlist inchangée
        tf1 = next(item for item, _ in entries if item["stream_id"] == "1")
        self.assertEqual(tf1["alternates"], ["3", "5", "4"])  # qualité : HD (2.0) > HEVC (1.5, non noté) > SD (1.0)
        m6 = next(item for item, _ in entries if item["stream_id"] == "2")
        self.assertIsNone(m6.get("alternates"))  # aucune autre version de M6 dans le catalogue

    def test_requires_dedupe(self):
        api = StubAPI(self.CATALOG, self.CATEGORIES)
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": ["1"], "failover": True,
                                   "dedupe": False, "failover_any_category": True})
        entries, _ = select_items(api, saved, "live")
        self.assertTrue(all(item.get("alternates") is None for item, _ in entries))

    def test_requires_failover_itself(self):
        # « Secours hors catégories cochées » coché mais « Bascule automatique » décochée : sans effet.
        api = StubAPI(self.CATALOG, self.CATEGORIES)
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": ["1"], "dedupe": True,
                                   "failover": False, "failover_any_category": True})
        entries, _ = select_items(api, saved, "live")
        tf1 = next(item for item, _ in entries if item["stream_id"] == "1")
        self.assertIsNone(tf1.get("alternates"))

    def test_combines_with_selected_duplicates_without_repeats_and_caps_at_max(self):
        # TF1 aussi coché via une 2e catégorie sélectionnée (doublon « sélectionné », même groupe perso
        # malgré des catégories différentes) + 8 autres versions dans des catégories non cochées.
        tags = ["HD", "SD", "UHD", "HEVC", "RAW", "MULTI", "BACKUP", "720p"]
        catalog = [ch(1, "TF1 FHD", "1"), ch(10, "TF1 4K", "9")] + \
            [ch(100 + n, f"TF1 {tag}", str(20 + n)) for n, tag in enumerate(tags)]
        categories = {"1": "A", "9": "B", **{str(20 + n): f"C{n}" for n in range(8)}}
        api = StubAPI(catalog, categories)
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": ["1", "9"], "dedupe": True,
                                   "failover": True, "failover_any_category": True, "uhd": "normal",
                                   "custom_groups": [{"id": "g1", "name": "G", "match": "exact", "patterns": ["TF1"]}]})
        entries, _ = select_items(api, saved, "live")
        self.assertEqual([item["stream_id"] for item, _ in entries], ["10"])  # 4K gardé (meilleure qualité)
        kept = entries[0][0]
        self.assertEqual(len(kept["alternates"]), 8)        # plafonné à MAX_ALTERNATES
        self.assertEqual(kept["alternates"][0], "1")         # le doublon sélectionné (FHD) passe en premier
        self.assertEqual(len(set(kept["alternates"])), 8)    # aucun identifiant répété

    def test_core_index_cached_per_catalog_identity(self):
        from app.builder import _core_index
        items_a = list(self.CATALOG)
        index_a1 = _core_index(items_a)
        index_a2 = _core_index(items_a)
        self.assertIs(index_a1, index_a2)              # même liste : index réutilisé
        items_b = list(self.CATALOG)                    # contenu identique, objet différent
        index_b = _core_index(items_b)
        self.assertIsNot(index_a1, index_b)             # recalculé : pas de collision par contenu
        self.assertEqual(index_a1, index_b)              # mais le résultat est bien le même


class MultiGroupTests(StackTestCase):
    """Option « une chaîne dans plusieurs groupes » : beIN dans OTT et dans Sport FR, même flux."""

    def test_channel_in_several_groups(self):
        groups = [{"id": "ott", "name": "OTT", "match": "exact", "patterns": ["France Info", "Sport 1"]},
                  {"id": "sport", "name": "Sport FR", "match": "exact", "patterns": ["Sport 1"]}]
        base = {"slug": "salon", "name": "Salon", "category_ids": ["1", "2"], "custom_groups": groups}
        self.create(base)
        m3u = self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode()
        self.assertEqual(m3u.count(",Sport 1"), 1)                                  # premier groupe seulement
        self.assertIn('group-title="OTT",Sport 1', m3u)
        self.send(self.stack.web_base + "/api/playlists/salon", "PUT", {**base, "multi_groups": True})
        m3u = self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode()
        self.assertIn('group-title="OTT",Sport 1', m3u)
        self.assertIn('group-title="Sport FR",Sport 1', m3u)                        # aussi dans Sport FR
        self.assertEqual(m3u.count("/stream/44.ts"), 2)                              # même flux
        self.assertNotIn('group-title="Sport FR",France Info', m3u)
        # Mode Xtream (TiviMate) : la chaîne est listée dans les deux catégories.
        import json
        from urllib.parse import urlencode
        device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "salon"})[1]
        q = {"username": device["username"], "password": device["password"]}
        cats = {c["category_name"]: c["category_id"] for c in json.loads(self.get(
            self.stack.proxy_base + "/player_api.php?" + urlencode({**q, "action": "get_live_categories"}))[1])}
        live = json.loads(self.get(self.stack.proxy_base + "/player_api.php?" + urlencode({**q, "action": "get_live_streams"}))[1])
        sport1 = [c for c in live if c["name"] == "Sport 1"]
        self.assertEqual(sorted(c["category_id"] for c in sport1), sorted([cats["OTT"], cats["Sport FR"]]))
        # TiviMate ne garde qu'une chaîne par identifiant : la copie a son propre numéro (sous 2³¹)…
        ids = [c["stream_id"] for c in sport1]
        self.assertEqual(len(set(ids)), 2)
        copy = next(i for i in ids if i != 44)
        self.assertTrue(2_000_000_000 <= copy < 2 ** 31)
        # … qui lance le même flux, y compris après un redémarrage (registre vide).
        # (dans le faux fournisseur, la chaîne 44 répond « abonnement occupé » : 503 = bien routée vers elle)
        url = f"{self.stack.proxy_base}/live/{device['username']}/{device['password']}/{copy}.ts"
        self.assertEqual(self.get(url)[0], 503)
        emulator = self.stack.proxy.RequestHandlerClass.emulator
        emulator._virtual.clear()
        self.assertEqual(self.get(url)[0], 503)
        unknown = f"{self.stack.proxy_base}/live/{device['username']}/{device['password']}/2099999999.ts"
        self.assertEqual(self.get(unknown)[0], 404)
        only_sport = json.loads(self.get(self.stack.proxy_base + "/player_api.php?" + urlencode(
            {**q, "action": "get_live_streams", "category_id": cats["Sport FR"]}))[1])
        self.assertEqual([c["stream_id"] for c in only_sport], [copy])            # même numéro par catégorie
