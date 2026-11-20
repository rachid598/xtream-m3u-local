"""Mise à jour de nuit : fenêtre horaire, copies servies en journée, passe de nuit."""
import os
import tempfile
import time
import unittest
from dataclasses import replace
from unittest import mock

from app import nightly
from app.server import Config, XtreamAPI, _nightly_refresh, in_nightly_window
from app.vod import KeyedCache

from .helpers import StackTestCase


def outside_window(config: Config) -> Config:
    """Fenêtre d'une heure qui ne contient pas l'heure actuelle."""
    hour = (time.localtime().tm_hour + 2) % 24
    return replace(config, nightly_refresh=(hour, (hour + 1) % 24))


class WindowTests(unittest.TestCase):
    def test_parse(self):
        for raw, expected in (("3-6", (3, 6)), ("03:00-06:00", (3, 6)), ("23-5", (23, 5)), ("", None), ("off", None)):
            with mock.patch.dict(os.environ, {"NIGHTLY_REFRESH": raw}):
                self.assertEqual(_nightly_refresh(), expected, raw)
        for raw in ("abc", "3-3", "25-2"):
            with mock.patch.dict(os.environ, {"NIGHTLY_REFRESH": raw}), self.assertRaises(ValueError):
                _nightly_refresh()

    def test_window(self):
        base = Config("http://x.invalid", "u", "p")
        hour = time.localtime().tm_hour
        self.assertTrue(in_nightly_window(replace(base, nightly_refresh=(hour, (hour + 1) % 24))))
        self.assertFalse(in_nightly_window(outside_window(base)))
        self.assertTrue(in_nightly_window(replace(base, nightly_refresh=None)))
        # Fenêtre qui passe minuit : 23 h – 5 h.
        night = replace(base, nightly_refresh=(23, 5))
        at = lambda h: time.mktime(time.struct_time((2026, 11, 20, h, 30, 0, 0, 0, -1)))  # noqa: E731
        self.assertEqual([in_nightly_window(night, at(h)) for h in (22, 23, 2, 5)], [False, True, True, False])


class StaleCopyTests(unittest.TestCase):
    def test_stale_copy_served_by_day_refreshed_at_night(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            cache = KeyedCache(lambda sid: calls.append(sid) or {"episodes": {}}, tmp, max_age=60, subdir="series")
            cache._store("1", [{"id": "10"}])
            cache._memory["1"] = (time.time() - 3600, [{"id": "10"}])          # périmée
            cache.refresh_stale = lambda: False                                    # journée
            self.assertEqual(cache.episodes("1"), [{"id": "10"}])
            self.assertEqual(cache.episodes_for(["1"], budget=1)[0], {"1": [{"id": "10"}]})
            self.assertEqual(calls, [])
            cache.refresh_stale = lambda: True                                     # nuit
            cache.episodes_for(["1"], budget=2)
            self.assertEqual(calls, ["1"])

    def test_big_catalogs_kept_by_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = XtreamAPI(outside_window(Config("http://x.invalid", "u", "p", data_dir=tmp)))
            for kind in ("vod", "series"):
                st = api.kind_state(kind)
                st.items, st.updated_at = [], time.time() - 3600
                api._refresh_in_background(kind)
            self.assertEqual(api._refreshing, set())


class NightPassTests(StackTestCase):
    def test_run_once(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "series_category_ids": ["1", "7"]})
        done = nightly.run_once(self.stack.api, self.stack.series, self.stack.store, self.stack.config)
        self.assertIn("vod", done["catalogs"])   # catalogue séries déjà chargé à l'enregistrement : récent, ignoré
        self.assertGreaterEqual(done["series"], 1)
        self.assertIn("900", nightly.playlists_series_ids(self.stack.api, self.stack.store))
        self.stack.api.last_429 = time.time()                                       # 429 récent : reportée
        self.assertEqual(nightly.run_once(self.stack.api, self.stack.series, self.stack.store, self.stack.config),
                         {"catalogs": [], "series": 0})


if __name__ == "__main__":
    unittest.main()
