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
            "epg-xmltv": "ok", "epg-api": "ok",
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
