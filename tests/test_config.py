"""Configuration par variables d'environnement (Docker Compose / Unraid)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.server import Config, normalize_xtream_url, prepare_data_dir

BASE_ENV = {"XTREAM_URL": "http://provider.example:8080", "XTREAM_USERNAME": "u", "XTREAM_PASSWORD": "p"}


def config(**extra):
    with mock.patch.dict(os.environ, {**BASE_ENV, **extra}, clear=True):
        return Config.from_env()


class ConfigTests(unittest.TestCase):
    def test_minimal_configuration_has_good_defaults(self):
        cfg = config()
        self.assertEqual((cfg.port, cfg.web_port, cfg.data_dir), (7777, 7778, "/data"))
        self.assertTrue(cfg.enable_epg)
        self.assertEqual(cfg.cache_seconds, 1800)
        self.assertEqual(cfg.web_username, "admin")
        self.assertEqual((cfg.access_token, cfg.web_password, cfg.public_base_url), ("", "", ""))

    def test_empty_optional_values_from_compose(self):
        cfg = config(PUBLIC_BASE_URL="", ACCESS_TOKEN="", ENABLE_EPG="", WEB_USERNAME="", CACHE_SECONDS="",
                     PORT="", WEB_PORT="")
        self.assertEqual((cfg.port, cfg.web_port, cfg.web_username), (7777, 7778, "admin"))
        self.assertTrue(cfg.enable_epg)

    def test_url_normalization(self):
        cases = {
            "http://host:80/": "http://host:80",
            "host.example:8080": "http://host.example:8080",
            "https://host/player_api.php": "https://host",
            "http://host:8080/get.php": "http://host:8080",
            "http://host/sub/path/": "http://host/sub/path",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_xtream_url(raw), expected)
        for bad in ("", "ftp://host", "http://u:p@host", "http://host/get.php?username=a&password=b", "http://"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize_xtream_url(bad)

    def test_invalid_values(self):
        for extra in ({"XTREAM_USERNAME": ""}, {"XTREAM_PASSWORD": "REMPLACER_MOT_DE_PASSE"},
                      {"PORT": "abc"}, {"PORT": "70000"}, {"WEB_PORT": "7777"},
                      {"PUBLIC_BASE_URL": "192.168.1.10:7777"}, {"CACHE_SECONDS": "1h"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                config(**extra)

    def test_booleans(self):
        for raw, expected in (("false", False), ("0", False), ("non", False), ("true", True), ("oui", True), ("ON", True)):
            with self.subTest(raw=raw):
                self.assertEqual(config(ENABLE_EPG=raw).enable_epg, expected)

    def test_prepare_data_dir_non_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "nested", "data")
            with mock.patch("os.geteuid", return_value=1000):
                prepare_data_dir(str(target))
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])

    def test_prepare_data_dir_root_drops_privileges(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "playlists.json").write_text("[]")
            with mock.patch("os.geteuid", return_value=0), \
                    mock.patch("os.lchown") as lchown, mock.patch("os.setgroups") as setgroups, \
                    mock.patch("os.setgid") as setgid, mock.patch("os.setuid") as setuid, \
                    mock.patch.dict(os.environ, {"PUID": "99", "PGID": "100"}):
                prepare_data_dir(tmp)
            self.assertEqual(lchown.call_count, 2)
            setgroups.assert_called_once_with([])
            setgid.assert_called_once_with(100)
            setuid.assert_called_once_with(99)


if __name__ == "__main__":
    unittest.main()
