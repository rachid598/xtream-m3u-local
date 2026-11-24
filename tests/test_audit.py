"""Corrections des audits (3.19.0 et 3.32.0)."""
import gc
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from app.builder import _core_index
from app.diagnostic import run as run_diagnostic
from app.server import Config, playlist, prepare_data_dir
from app.streams import EvictedRecently, StreamManager
from app.xtream_emu import XtreamEmulator

from .helpers import StackTestCase


class RestoreTests(StackTestCase):
    def backup(self, devices):
        return {"backup": {"playlists": [{"slug": "salon", "name": "Salon", "category_ids": ["1"]}],
                           "devices": {"require_token": False, "devices": devices}},
                "mode": "replace", "include_devices": True}

    def test_invalid_devices_write_nothing(self):
        dup = [{"name": "A", "username": "tv-a", "password": "abcdef12"},
               {"name": "B", "username": "tv-a", "password": "abcdef34"}]
        status, body = self.send(self.stack.web_base + "/api/restore", "POST", self.backup(dup))
        self.assertEqual(status, 400, body)
        self.assertEqual(json.loads(self.get(self.stack.web_base + "/api/playlists")[1]), [])   # rien d'écrit

    def test_odd_ids_and_dates_repaired(self):
        devices = [{"id": "../x", "name": "A", "username": "tv-a", "password": "abcdef12", "created_at": "hier"}]
        status, body = self.send(self.stack.web_base + "/api/restore", "POST", self.backup(devices))
        self.assertEqual(status, 200, body)
        restored = json.loads(self.get(self.stack.web_base + "/api/devices")[1])["devices"][0]
        self.assertRegex(restored["id"], r"^[0-9a-f]{8}$")
        self.assertEqual(self.send(self.stack.web_base + f"/api/devices/{restored['id']}", "DELETE")[0], 200)


class M3uTests(unittest.TestCase):
    def test_number_cannot_break_line(self):
        channel = {"stream_id": "1", "name": "A", "num": '5" x="\n#EXTINF', "category_id": "1", "logo": ""}
        text = playlist(Config("http://x.invalid", "u", "p"), [(channel, "1")], {"1": "G"}, "http://h").decode()
        self.assertEqual(sum(1 for line in text.splitlines() if line.startswith("#EXTINF")), 1)


class Audit332Tests(StackTestCase):
    def test_xtream_episode_allowed_right_after_get_series_info_and_when_stale(self):
        """En mode Xtream, ouvrir une série (get_series_info) suffit à autoriser ses épisodes, même si le cache expire."""
        self.assertEqual(self.send(self.stack.web_base + "/api/playlists", "POST", {
            "slug": "series-only", "name": "Séries", "included_series_ids": ["900"]})[0], 201)
        device = self.send(self.stack.web_base + "/api/devices", "POST", {
            "name": "TV", "playlist": "series-only"})[1]
        user, pw = device["username"], device["password"]
        base = self.stack.proxy_base
        # Aucun préchargement manuel dans SeriesCache : get_series_info doit alimenter le cache.
        self.stack.series._memory.clear()
        for f in self.stack.series.directory.glob("*.json"):
            f.unlink()
        q = urlencode({"username": user, "password": pw, "action": "get_series_info", "series_id": "900"})
        info = json.loads(self.get(base + "/player_api.php?" + q)[1])
        self.assertIn("episodes", info)
        self.assertEqual(self.get(f"{base}/series/{user}/{pw}/9000.mp4")[:2], (200, b"EPISODE"))
        # Série hors playlist : toujours refusée.
        self.assertEqual(self.get(f"{base}/series/{user}/{pw}/9101.avi")[0], 404)
        # Cache disque périmé (> max_age) et vidé de la mémoire : l'épisode reste autorisé.
        path = self.stack.series._path("900")
        cached = json.loads(path.read_text(encoding="utf-8"))
        cached["fetched_at"] = time.time() - self.stack.series.max_age - 3600
        path.write_text(json.dumps(cached), encoding="utf-8")
        self.stack.series._memory.clear()
        self.stack.series._ep_to_sids.clear()
        self.stack.series._sid_eps.clear()
        self.assertEqual(self.get(f"{base}/series/{user}/{pw}/9000.mp4")[:2], (200, b"EPISODE"))

    def test_origin_null_rejected_on_mutations(self):
        status, _ = self.send(self.stack.web_base + "/api/playlists", "POST",
                              {"slug": "x", "name": "X"},
                              headers={"Content-Type": "application/json",
                                       "X-Requested-With": "XtreamM3ULocal", "Origin": "null"})
        self.assertEqual(status, 403)


class Audit332UnitTests(unittest.TestCase):
    def test_core_index_does_not_collide_on_reused_list_id(self):
        """Deux listes successives (ex. filtrées par profil) ne doivent jamais se partager un mauvais index."""
        list_a = [{"stream_id": "10", "name": "FR: TF1 HD", "category_id": "1", "category_ids": ["1"]}]
        idx_a = _core_index(list_a)
        self.assertEqual(idx_a["tf1"], [(2.0, "10")])
        del list_a
        gc.collect()
        list_b = [{"stream_id": "200000000010", "name": "FR: TF1 FHD", "category_id": "2", "category_ids": ["2"]}]
        idx_b = _core_index(list_b)
        self.assertEqual(idx_b["tf1"], [(3.0, "200000000010")])

    def test_stream_cooldown_applies_to_distinct_devices_behind_same_ip(self):
        """Deux appareils derrière la même IP (NAT) ne doivent pas pouvoir se couper en boucle."""
        mgr = StreamManager(lambda _p: 1, takeover=True)
        s1 = mgr.acquire(kind="live", item_id="42", client="203.0.113.5", device="Salon")
        s1.closed.set()
        s2 = mgr.acquire(kind="live", item_id="43", client="203.0.113.5", device="Chambre")
        with self.assertRaises(EvictedRecently):
            mgr.acquire(kind="live", item_id="42", client="203.0.113.5", device="Salon")
        mgr.release(s2)

    def test_prepare_data_dir_skips_lchown_when_owner_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp, "file.json")
            p.write_text("{}")
            uid, gid = os.getuid(), os.getgid()
            with mock.patch("os.geteuid", return_value=0), \
                    mock.patch("os.lchown") as lchown, mock.patch("os.setgroups"), \
                    mock.patch("os.setgid"), mock.patch("os.setuid"), \
                    mock.patch.dict(os.environ, {"PUID": str(uid), "PGID": str(gid)}):
                prepare_data_dir(tmp)
            lchown.assert_not_called()

    def test_emulator_account_uses_profile_timezone(self):
        class FakeApi:
            def __init__(self, tz_name):
                self._tz = ZoneInfo(tz_name)
            def account(self):
                return {"status": "Active", "max_connections": "1"}
            def provider_timezone(self):
                return self._tz

        class FakeProviderSet:
            indices = [1, 2]
            apis = [FakeApi("UTC"), FakeApi("Europe/Paris")]
            def account(self):
                return self.apis[0].account()
            def provider_timezone(self):
                return self.apis[0].provider_timezone()

        class FakeStore:
            def get(self, slug):
                return {"slug": slug, "name": slug, "provider": "2"}

        emu = XtreamEmulator(FakeProviderSet(), FakeStore(), Config("http://x.invalid", "u", "p"))
        info = emu.account({"username": "tv", "password": "pw", "playlist": "p2"}, "h", "7777")
        self.assertEqual(info["server_info"]["timezone"], "Europe/Paris")

    def test_diagnostic_profile_does_not_refresh_other_provider(self):
        calls = []

        class FakeApi:
            def __init__(self, idx):
                self.idx = idx
                self.config = Config("http://x.invalid", "u", "p")
                self.last_429 = 0.0
                self._state = SimpleNamespace(items=[], categories={}, categories_error=None)
            def api_url(self):
                return "http://x.invalid/player_api.php"
            def _fetch(self, _url, _label):
                return {"user_info": {"auth": 1, "status": "Active", "max_connections": "1"}}
            def catalog(self, *, refresh=False, kind="live"):
                calls.append((self.idx, kind, refresh))
                return [], {}
            def kind_state(self, kind="live"):
                return self._state

        class FakeSet:
            indices = [1, 2]
            names = ["P1", "P2"]
            def __init__(self):
                self.apis = [FakeApi(1), FakeApi(2)]
            def provider_of(self, _id):
                return "2"
            def kind_state(self, kind="live"):
                return SimpleNamespace(items=[], categories={}, categories_error="err-p1")

        class EmptyStore:
            def list(self):
                return []

        cfg = Config("http://x.invalid", "u", "p", enable_epg=False)
        results = run_diagnostic(FakeSet(), None, None, None, EmptyStore(), cfg, profile="2")
        self.assertTrue(all(idx == 2 for idx, _, _ in calls), calls)
        self.assertEqual(len(calls), 3)
        live = next(r for r in results if r["id"] == "catalog-live")
        self.assertNotIn("err-p1", live["detail"])


if __name__ == "__main__":
    unittest.main()
