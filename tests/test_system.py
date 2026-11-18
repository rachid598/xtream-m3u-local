"""Sauvegarde / restauration et diagnostic."""
import json
import threading
import time
from urllib.request import urlopen

from .fake_xtream import FakeXtream
from .helpers import StackTestCase


class BackupTests(StackTestCase):
    def setUp(self):
        super().setUp()
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "favorites": ["42"]})
        self.create({"slug": "chambre", "name": "Chambre", "category_ids": ["2"]})
        self.device = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV", "playlist": "salon"})[1]

    def backup(self):
        status, body, headers = self.get(self.stack.web_base + "/api/backup")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        return json.loads(body)

    def test_backup_content(self):
        data = self.backup()
        self.assertEqual(sorted(p["slug"] for p in data["playlists"]), ["chambre", "salon"])
        self.assertEqual(data["devices"]["devices"][0]["password"], self.device["password"])
        self.assertNotIn('"pw"', json.dumps(data))

    def test_restore_merge_and_replace(self):
        data = self.backup()
        url = self.stack.web_base + "/api/playlists/"
        self.send(url + "salon", "PUT", {"name": "Salon modifié", "category_ids": ["2"]})
        self.send(url + "chambre", "DELETE")
        self.create({"slug": "nouvelle", "name": "Nouvelle"})
        status, result = self.send(self.stack.web_base + "/api/restore", "POST", {"backup": data, "mode": "merge"})
        self.assertEqual((status, result), (200, {"added": 1, "replaced": 1, "total": 3}))
        names = {p["slug"]: p["name"] for p in self.send(self.stack.web_base + "/api/playlists", "GET", headers={})[1]}
        self.assertEqual(names, {"salon": "Salon", "nouvelle": "Nouvelle", "chambre": "Chambre"})
        status, result = self.send(self.stack.web_base + "/api/restore", "POST", {"backup": data, "mode": "replace"})
        self.assertEqual(result["total"], 2)
        self.assertEqual(self.get(self.stack.proxy_base + "/p/nouvelle.m3u")[0], 404)

    def test_invalid_restore_changes_nothing(self):
        data = self.backup()
        data["playlists"].append({"slug": "../x", "name": "Pirate"})
        before = self.send(self.stack.web_base + "/api/playlists", "GET", headers={})[1]
        status, body = self.send(self.stack.web_base + "/api/restore", "POST",
                                 {"backup": data, "mode": "replace", "include_devices": True})
        self.assertEqual(status, 400)
        self.assertEqual(self.send(self.stack.web_base + "/api/playlists", "GET", headers={})[1], before)
        self.assertEqual(self.send(self.stack.web_base + "/api/restore", "POST", {"backup": {"x": 1}})[0], 400)
        self.assertEqual(self.send(self.stack.web_base + "/api/restore", "POST", {"backup": self.backup(), "mode": "x"})[0], 400)
        self.assertEqual(self.send(self.stack.web_base + "/api/restore", "POST", {"backup": data},
                                   {"Content-Type": "application/json"})[0], 403)

    def test_restore_devices(self):
        data = self.backup()
        self.send(self.stack.web_base + f"/api/devices/{self.device['id']}", "DELETE")
        self.assertEqual(self.send(self.stack.web_base + "/api/restore", "POST",
                                   {"backup": data, "include_devices": True})[0], 200)
        devices = json.loads(self.get(self.stack.web_base + "/api/devices")[1])["devices"]
        self.assertEqual([d["password"] for d in devices], [self.device["password"]])


class DiagnosticTests(StackTestCase):
    def run_diagnostic(self, stack=None):
        status, data = self.send((stack or self.stack).web_base + "/api/diagnostic", "POST")
        self.assertEqual(status, 200)
        return {r["id"]: r for r in data["results"]}, data

    def test_full_diagnostic(self):
        results, data = self.run_diagnostic()
        statuses = {k: v["status"] for k, v in results.items()}
        self.assertEqual(statuses, {
            "account": "ok", "catalog-live": "ok", "catalog-vod": "ok", "catalog-series": "ok",
            "failover": "skipped",        # aucune playlist n'utilise la bascule automatique
            "logos": "skipped",           # LOGO_COUNTRIES non installé dans les tests
            "epg-xmltv": "ok", "epg-api": "warn",   # 1 chaîne sur 3 a des programmes via l'API
            "stream-ts": "warn",          # le faux flux n'est pas du vrai MPEG-TS
            "stream-hls": "ok", "stream-movie": "ok", "stream-episode": "ok", "stream-replay": "ok"})
        self.assertIn("0/1 connexion", results["account"]["detail"])
        self.assertIn("6 chaînes, 7 catégories", results["catalog-live"]["detail"])
        raw = json.dumps(data)
        for secret in ('"pw"', "/pw/", "user/pw", "password", self.provider_url):
            self.assertNotIn(secret, raw)
        self.assertTrue(all(isinstance(r["ms"], int) for r in data["results"] if r["status"] != "skipped"))

    def test_stream_tests_skipped_while_playing(self):
        reader = threading.Thread(target=lambda: urlopen(self.stack.proxy_base + "/stream/50.ts", timeout=20).read())
        reader.start()
        for _ in range(50):
            if self.stack.streams.count():
                break
            time.sleep(0.05)
        results, _ = self.run_diagnostic()
        self.assertEqual(results["stream-ts"]["status"], "skipped")
        self.assertEqual(results["account"]["status"], "ok")
        self.stack.streams.stop(self.stack.streams.list()[0]["id"])
        reader.join(10)

    def test_provider_down(self):
        FakeXtream.fail_api = True
        results, _ = self.run_diagnostic()
        self.assertEqual(list(results), ["account"])
        self.assertEqual(results["account"]["status"], "error")
        self.assertEqual(self.send(self.stack.web_base + "/api/diagnostic", "POST", headers={})[0], 403)


class DiagnosticSamplingTests(StackTestCase):
    def test_separators_skipped_and_api_compensates_broken_xmltv(self):
        import tests.fake_xtream as fake
        original = list(fake.STREAMS)
        fake.STREAMS.insert(0, {"num": 0, "name": "#### PRIME RAW 60fps #####", "stream_id": 40, "category_id": "1"})
        FakeXtream.epg_mode = "error"
        try:
            status, data = self.send(self.stack.web_base + "/api/diagnostic", "POST")
            results = {r["id"]: r for r in data["results"]}
            self.assertNotIn("PRIME", results["stream-ts"]["detail"])
            self.assertIn("France Info", results["stream-ts"]["detail"])        # vraie chaîne avec EPG
            self.assertEqual(results["epg-api"]["status"], "warn")              # 1 chaîne sur 3 couverte
            self.assertIn("1/3 chaînes testées", results["epg-api"]["detail"])
            self.assertEqual(results["epg-xmltv"]["status"], "warn")            # compensé par l'API
            self.assertIn("via l'API", results["epg-xmltv"]["detail"])
            fake.SIMPLE_EPG_BACKUP = dict(fake.SIMPLE_EPG)
            fake.SIMPLE_EPG.clear()
            status, data = self.send(self.stack.web_base + "/api/diagnostic", "POST")
            results = {r["id"]: r for r in data["results"]}
            self.assertEqual((results["epg-xmltv"]["status"], results["epg-api"]["status"]), ("error", "error"))
            self.assertIn("EPG_URL", results["epg-api"]["detail"])
        finally:
            fake.STREAMS[:] = original
            if hasattr(fake, "SIMPLE_EPG_BACKUP"):
                fake.SIMPLE_EPG.update(fake.SIMPLE_EPG_BACKUP)
                del fake.SIMPLE_EPG_BACKUP


class FailoverDiagnosticTests(StackTestCase):
    """Diagnostic : repère une bascule automatique mal configurée, sans attendre une vraie coupure."""

    def run_diagnostic(self):
        status, data = self.send(self.stack.web_base + "/api/diagnostic", "POST")
        self.assertEqual(status, 200)
        return {r["id"]: r for r in data["results"]}

    def test_no_playlist_uses_failover(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"]})
        results = self.run_diagnostic()
        self.assertEqual(results["failover"]["status"], "skipped")
        self.assertNotIn("failover-salon", results)

    def test_enabled_without_dedupe_is_an_error(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "failover": True, "dedupe": False})
        results = self.run_diagnostic()
        r = results["failover-salon"]
        self.assertEqual(r["status"], "error")
        self.assertIn("Masquer les doublons", r["detail"])
        self.assertNotIn("failover", results)  # pas de résumé « aucune playlist » : il y en a une

    def test_enabled_with_dedupe_but_no_duplicate_is_a_warning(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "failover": True, "dedupe": True})
        results = self.run_diagnostic()
        r = results["failover-salon"]
        self.assertEqual(r["status"], "warn")
        self.assertIn("0/", r["detail"])
        self.assertIn("Secours hors catégories cochées", r["detail"])

    def test_enabled_with_real_duplicate_is_ok(self):
        import tests.fake_xtream as fake
        original = list(fake.STREAMS)
        fake.STREAMS.append({"num": 9, "name": "France Info 4K", "stream_type": "live", "stream_id": 900,
                             "stream_icon": "", "epg_channel_id": "", "category_id": "1", "category_ids": [1]})
        try:
            self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"], "failover": True, "dedupe": True})
            r = self.run_diagnostic()["failover-salon"]
            self.assertEqual(r["status"], "ok")
            self.assertRegex(r["detail"], r"^\d+/\d+ chaînes \(\d+ %\)")
        finally:
            fake.STREAMS[:] = original

    def test_more_than_ten_failover_playlists_are_capped(self):
        for n in range(11):
            self.create({"slug": f"p{n}", "name": f"P{n}", "category_ids": ["1"], "failover": True, "dedupe": True})
        results = self.run_diagnostic()
        self.assertEqual(sum(1 for k in results if k.startswith("failover-p")), 10)
        self.assertEqual(results["failover-more"]["status"], "skipped")
        self.assertIn("1 playlist", results["failover-more"]["detail"])
