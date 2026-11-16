"""Validation, filtrage et persistance des playlists personnalisées."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.custom import (MAX_PLAYLISTS, UNCATEGORIZED, InvalidPlaylist, PlaylistStore, filter_channels,
                        validate_playlist)
from app.server import normalize_channel


def channel(sid, cats, name=None):
    return {"stream_id": str(sid), "name": name or f"C{sid}", "logo": "", "epg_id": "", "num": "",
            "category_id": cats[0], "category_ids": cats}


class ValidationTests(unittest.TestCase):
    def test_valid_payload(self):
        result = validate_playlist({"slug": "tv-fr_1", "name": "  TV française ", "category_ids": [1, "none"],
                                    "included_stream_ids": ["5", 5], "excluded_stream_ids": []})
        self.assertEqual(result["name"], "TV française")
        self.assertEqual(result["category_ids"], ["1", "none"])
        self.assertEqual(result["included_stream_ids"], ["5"])

    def test_invalid_payloads(self):
        bad = [
            {"slug": "../etc", "name": "x"},
            {"slug": "-abc", "name": "x"},
            {"slug": "Salon", "name": "x"},
            {"slug": "a" * 61, "name": "x"},
            {"slug": "ok", "name": ""},
            {"slug": "ok", "name": "x" * 101},
            {"slug": "ok", "name": "a\nb"},
            {"slug": "ok", "name": "x", "category_ids": "1"},
            {"slug": "ok", "name": "x", "category_ids": ["1;rm"]},
            {"slug": "ok", "name": "x", "included_stream_ids": ["abc"]},
            {"slug": "ok", "name": "x", "included_stream_ids": [True]},
            {"slug": "ok", "name": "x", "excluded_stream_ids": [None]},
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(InvalidPlaylist):
                validate_playlist(payload)
        with self.assertRaises(InvalidPlaylist):
            validate_playlist(["not", "a", "dict"])
        for groups in ({"1": "a\nb"}, {"../x": "A"}, ["1"], {"1": "x" * 101}, {"1": 5}):
            with self.subTest(groups=groups), self.assertRaises(InvalidPlaylist):
                validate_playlist({"slug": "ok", "name": "x", "group_names": groups})

    def test_group_order_and_names(self):
        result = validate_playlist({"slug": "ok", "name": "x", "group_order": ["10", "1", "10"],
                                    "group_names": {"10": " Sports ", "1": "  "}})
        self.assertEqual(result["group_order"], ["10", "1"])
        self.assertEqual(result["group_names"], {"10": "Sports"})


class FilterTests(unittest.TestCase):
    channels = [channel(1, ["1"]), channel(2, ["2"]), channel(3, ["3", "1"]), channel(4, [UNCATEGORIZED])]

    def test_categories_includes_excludes(self):
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": ["1"],
                                   "included_stream_ids": ["2"], "excluded_stream_ids": ["1"]})
        result = [(c["stream_id"], g) for c, g in filter_channels(self.channels, saved)]
        # La chaîne 3 est rangée dans la catégorie sélectionnée (1), pas dans sa catégorie principale (3).
        self.assertEqual(result, [("2", "2"), ("3", "1")])

    def test_exclusion_wins_over_inclusion(self):
        saved = validate_playlist({"slug": "s", "name": "S", "included_stream_ids": ["2"], "excluded_stream_ids": ["2"]})
        self.assertEqual(filter_channels(self.channels, saved), [])

    def test_uncategorized_selectable(self):
        saved = validate_playlist({"slug": "s", "name": "S", "category_ids": [UNCATEGORIZED]})
        self.assertEqual([c["stream_id"] for c, _ in filter_channels(self.channels, saved)], ["4"])

    def test_normalize_channel(self):
        self.assertIsNone(normalize_channel({"stream_id": "x"}))
        self.assertIsNone(normalize_channel("nope"))
        c = normalize_channel({"stream_id": 7, "name": " A\tB ", "category_id": None, "stream_icon": "ftp://x"})
        self.assertEqual((c["name"], c["category_ids"], c["logo"]), ("A B", [UNCATEGORIZED], ""))
        c = normalize_channel({"stream_id": "8", "category_id": "2", "category_ids": [2, 5, "", None]})
        self.assertEqual(c["category_ids"], ["2", "5"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PlaylistStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_crud_and_persistence(self):
        self.assertEqual(self.store.list(), [])
        saved = self.store.save({"slug": "salon", "name": "Salon", "category_ids": ["1"]})
        self.assertIn("updated_at", saved)
        with self.assertRaises(InvalidPlaylist):
            self.store.save({"slug": "salon", "name": "Doublon"})
        with self.assertRaises(KeyError):
            self.store.save({"name": "X"}, update_slug="inconnue")
        self.store.save({"name": "Salon TV", "category_ids": ["2"]}, update_slug="salon")
        reopened = PlaylistStore(self.tmp.name)  # simule un redémarrage du conteneur
        self.assertEqual(reopened.get("salon")["name"], "Salon TV")
        self.assertEqual(reopened.get("salon")["category_ids"], ["2"])
        self.assertTrue(reopened.delete("salon"))
        self.assertFalse(reopened.delete("salon"))
        self.assertIsNone(reopened.get("salon"))
        self.assertIsNone(reopened.get("../x"))

    def test_file_is_readable_json_without_secrets(self):
        self.store.save({"slug": "salon", "name": "Salon", "category_ids": ["1"]})
        data = json.loads(Path(self.tmp.name, "playlists.json").read_text(encoding="utf-8"))
        self.assertEqual(data[0]["slug"], "salon")
        self.assertLessEqual({"slug", "name", "category_ids", "included_stream_ids",
                                        "excluded_stream_ids", "vod_category_ids", "included_vod_ids",
                                        "excluded_vod_ids", "series_category_ids", "included_series_ids",
                                        "excluded_series_ids", "group_order", "group_names", "updated_at"}, set(data[0]))
        self.assertFalse({"password", "username", "url"} & set(data[0]))
        self.assertEqual(list(Path(self.tmp.name).glob(".playlists-*")), [])

    def test_backup_used_when_main_file_corrupted(self):
        self.store.save({"slug": "a", "name": "A"})
        self.store.save({"slug": "b", "name": "B"})
        self.assertTrue(Path(self.tmp.name, "playlists.json.bak").exists())
        Path(self.tmp.name, "playlists.json").write_text("{corrompu", encoding="utf-8")
        self.assertEqual([p["slug"] for p in self.store.list()], ["a"])

    def test_corrupted_without_backup_is_an_error(self):
        Path(self.tmp.name, "playlists.json").write_text("[{\"slug\": 1}]", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.store.list()
        with self.assertRaises(RuntimeError):
            self.store.save({"slug": "x", "name": "X"})

    def test_unwritable_directory(self):
        store = PlaylistStore(str(Path(self.tmp.name, "file.txt", "sub")))
        Path(self.tmp.name, "file.txt").write_text("x")
        with self.assertRaises(RuntimeError):
            store.save({"slug": "x", "name": "X"})

    def test_max_playlists(self):
        for i in range(MAX_PLAYLISTS):
            self.store.save({"slug": f"p{i}", "name": f"P{i}"})
        with self.assertRaises(InvalidPlaylist):
            self.store.save({"slug": "trop", "name": "Trop"})

    def test_concurrent_writes(self):
        errors = []

        def worker(i):
            try:
                self.store.save({"slug": f"t{i}", "name": f"T{i}"})
            except Exception as exc:  # pragma: no cover - diagnostic
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(PlaylistStore(self.tmp.name).list()), 20)


if __name__ == "__main__":
    unittest.main()
