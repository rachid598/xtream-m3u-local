"""Versions 4K / UHD : en dernier secours par défaut (« M6 4K » = vidéo de logo en boucle)."""
import unittest

from app.builder import dedupe, extend_alternates_from_catalog, select_items
from app.custom import InvalidPlaylist, validate_playlist


def ch(sid, name, cat="1"):
    return {"stream_id": str(sid), "name": name, "logo": "", "epg_id": "", "num": "", "category_id": cat,
            "category_ids": [cat], "archive": 0}


M6 = [ch(1, "FR: M6 HD"), ch(2, "FR: M6 4K"), ch(3, "FR: M6 FHD"), ch(4, "FR: M6 SD")]
UHD_ONLY = [ch(10, "UHD 1 4K")]


def run(uhd):
    out = dedupe([(c, "1") for c in M6 + UHD_ONLY], uhd)
    return [(i["stream_id"], i.get("alternates", [])) for i, _ in out]


class UhdTests(unittest.TestCase):
    def test_modes(self):
        self.assertEqual(run("backup"), [("3", ["1", "4", "2"]), ("10", [])])     # FHD gardée, 4K en dernier
        self.assertEqual(run("normal"), [("2", ["3", "1", "4"]), ("10", [])])     # comportement d'avant
        self.assertEqual(run("never"), [("3", ["1", "4"]), ("10", [])])           # 4K écartée, UHD seule gardée
        self.assertEqual(run("backup"), [(i["stream_id"], i.get("alternates", [])) for i, _ in dedupe([(c, "1") for c in M6 + UHD_ONLY])])

    def test_catalog_backups_respect_exclusions_and_mode(self):
        kept = [(ch(1, "FR: M6 HD"), "1")]
        catalog = [ch(1, "FR: M6 HD"), ch(5, "VIP: M6 4K", "9"), ch(6, "AF: M6 FHD", "9"), ch(7, "M6 SD", "9")]
        alts = lambda **kw: extend_alternates_from_catalog(kept, catalog, **kw)[0][0]["alternates"]  # noqa: E731
        self.assertEqual(alts(uhd="backup"), ["6", "7", "5"])
        self.assertEqual(alts(uhd="normal"), ["5", "6", "7"])
        self.assertEqual(alts(uhd="never"), ["6", "7"])
        self.assertEqual(alts(uhd="backup", excluded={"6", "5"}), ["7"])           # exclue = jamais en secours

    def test_playlist_field(self):
        base = {"slug": "salon", "name": "Salon", "category_ids": ["1"]}
        self.assertEqual(validate_playlist(base)["uhd"], "backup")
        self.assertEqual(validate_playlist({**base, "uhd": "never"})["uhd"], "never")
        with self.assertRaises(InvalidPlaylist):
            validate_playlist({**base, "uhd": "toujours"})

        class Api:
            def catalog(self, refresh=False, kind="live"):
                return M6, {"1": "TNT"}

        entries, _ = select_items(Api(), {**validate_playlist(base), "dedupe": True}, "live")
        self.assertEqual([i["name"] for i, _ in entries], ["FR: M6 FHD"])


if __name__ == "__main__":
    unittest.main()
