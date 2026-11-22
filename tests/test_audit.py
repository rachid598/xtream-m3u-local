"""Corrections de l'audit 3.19.0 : restauration robuste, M3U sans injection."""
import json
import unittest

from app.server import Config, playlist

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


if __name__ == "__main__":
    unittest.main()
