"""Replay (catch-up) : attributs M3U et relais /timeshift."""
import os
import unittest
from unittest import mock

from app.server import Config, normalize_channel

from .fake_xtream import FakeXtream
from .helpers import StackTestCase


class NormalizeArchiveTests(unittest.TestCase):
    def test_archive_days(self):
        self.assertEqual(normalize_channel({"stream_id": 1, "tv_archive": 1, "tv_archive_duration": "7"})["archive"], 7)
        self.assertEqual(normalize_channel({"stream_id": 1, "tv_archive": 0, "tv_archive_duration": 7})["archive"], 0)
        self.assertEqual(normalize_channel({"stream_id": 1, "tv_archive": "1", "tv_archive_duration": "x"})["archive"], 0)
        self.assertEqual(normalize_channel({"stream_id": 1})["archive"], 0)

    def test_timezone_env(self):
        base = {"XTREAM_URL": "http://h", "XTREAM_USERNAME": "u", "XTREAM_PASSWORD": "p"}
        with mock.patch.dict(os.environ, {**base, "PROVIDER_TIMEZONE": "Europe/Paris"}, clear=True):
            self.assertEqual(Config.from_env().provider_timezone, "Europe/Paris")
        with mock.patch.dict(os.environ, {**base, "PROVIDER_TIMEZONE": "Mars/Olympus"}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()


class CatchupTests(StackTestCase):
    def test_m3u_catchup_attributes(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2"]})
        data = self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode()
        line = next(l for l in data.splitlines() if l.endswith(",France Info HD"))
        source = f"{self.stack.proxy_base}/timeshift/42.ts?start={{utc}}&end={{utcend}}"
        self.assertIn(f'catchup="default" catchup-days="3" catchup-source="{source}" tvg-rec="3" group-title=', line)
        other = next(l for l in data.splitlines() if "HACK" in l and l.startswith("#EXTINF"))
        self.assertNotIn("catchup", other)
        full = self.get(self.stack.proxy_base + "/playlist.m3u")[1].decode()
        self.assertIn('catchup-days="3"', full)
        # Option désactivée : aucun attribut.
        self.create({"slug": "sans", "name": "Sans", "category_ids": ["1"], "catchup": False})
        self.assertNotIn(b"catchup", self.get(self.stack.proxy_base + "/p/sans.m3u")[1])

    def test_timeshift_relay_in_provider_timezone(self):
        # 1794762000 = 2026-11-15 17:00 UTC = 18:00 à Paris (fuseau annoncé par le fournisseur).
        status, body, _ = self.get(self.stack.proxy_base + "/timeshift/42.ts?start=1794762000&end=1794765600")
        self.assertEqual((status, body), (200, b"REPLAY 60/2026-11-15:18-00/42.ts"))
        status, body, _ = self.get(self.stack.proxy_base + "/timeshift/42.ts?start=1794762000&duration=45")
        self.assertEqual(body, b"REPLAY 45/2026-11-15:18-00/42.ts")
        # Fin non alignée sur la minute : arrondi supérieur.
        self.assertEqual(self.get(self.stack.proxy_base + "/timeshift/42.ts?start=1794762000&end=1794762061")[1],
                         b"REPLAY 2/2026-11-15:18-00/42.ts")

    def test_timezone_override_and_errors(self):
        stack = self.make_stack(provider_timezone="UTC")
        self.assertEqual(self.get(stack.proxy_base + "/timeshift/42.ts?start=1794762000&duration=30")[1],
                         b"REPLAY 30/2026-11-15:17-00/42.ts")
        base = self.stack.proxy_base
        for query in ("", "?start=abc&end=1", "?start=1794762000", "?start=10&duration=5",
                      "?start=1794762000&duration=0", "?start=1794762000&duration=5000"):
            with self.subTest(query=query):
                self.assertEqual(self.get(base + "/timeshift/42.ts" + query)[0], 400)

    def test_token_and_tracking(self):
        stack = self.make_stack(access_token="tok")
        self.assertEqual(self.get(stack.proxy_base + "/timeshift/42.ts?start=1794762000&duration=5")[0], 403)
        self.create({"slug": "s", "name": "S", "category_ids": ["1"]}, stack)
        data = self.get(stack.proxy_base + "/p/s.m3u?token=tok")[1].decode()
        self.assertIn("/timeshift/42.ts?start={utc}&end={utcend}&token=tok", data)
        self.assertEqual(self.get(stack.proxy_base + "/timeshift/42.ts?start=1794762000&duration=5&token=tok")[0], 200)


if __name__ == "__main__":
    unittest.main()
