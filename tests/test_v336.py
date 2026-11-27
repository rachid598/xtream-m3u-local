"""Tests v3.36.0 : Pause/QR appareils, version épinglée, fiabilité adaptative temporaire,
test comparatif de versions, alertes Telegram enrichies et export multi-abonnements."""
from __future__ import annotations

import csv
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from app import alerts, settings
from app.builder import select_items
from app.custom import PlaylistStore, validate_playlist
from app.devices import DeviceStore
from app.providers import ProviderSet, encode
from app.server import Config, XtreamAPI, make_server, make_streams
from app.streams import VersionHealth
from app.web import make_web_server
from app.xtream_emu import XtreamEmulator

USERNAME = "user"
PASSWORD = "pw"


def _req(url: str, method: str = "GET", body: dict | None = None) -> tuple[int, bytes]:
    headers = {"X-Requested-With": "XtreamM3ULocal", "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _start_mini_xtream(categories: list[dict], streams: list[dict]):
    valid_ids = {str(s["stream_id"]) for s in streams}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlsplit(self.path)
            qs = parse_qs(u.query)
            if u.path == "/player_api.php":
                action = (qs.get("action") or [""])[0]
                if not action:
                    payload = {"user_info": {"status": "Active", "max_connections": "2", "active_cons": "0",
                                             "exp_date": str(int(time.time()) + 86400 * 60)},
                               "server_info": {"timezone": "Europe/Paris"}}
                    return self._send(200, json.dumps(payload).encode())
                if action == "get_live_categories":
                    return self._send(200, json.dumps(categories).encode())
                if action == "get_live_streams":
                    return self._send(200, json.dumps(streams).encode())
                return self._send(200, b"[]")
            prefix = f"/live/{USERNAME}/{PASSWORD}/"
            if u.path.startswith(prefix) and u.path.endswith(".ts"):
                sid = u.path[len(prefix):-3]
                if sid in valid_ids:
                    return self._send(200, (b"\x47" + b"\x00" * 187) * 8, "video/mp2t")
                return self._send(404, b"not found", "text/plain")
            return self._send(404, b"not found", "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class VersionHealthTests(unittest.TestCase):
    def test_short_quarantine_and_automatic_return_to_hd(self):
        vh = VersionHealth()
        t = 1000.0
        ids = ["101", "102", "103"]

        # 1 seul échec ne met pas en quarantaine
        vh.record_failure("101", now=t)
        self.assertEqual(vh.quarantined_seconds("101", now=t), 0)
        self.assertEqual(vh.reorder(ids, now=t), ["101", "102", "103"])

        # 2e échec dans la fenêtre → quarantaine courte (30 min = 1800 s)
        t += 10
        vh.record_failure("101", now=t)
        q = vh.quarantined_seconds("101", now=t)
        self.assertGreaterEqual(q, 1790)
        self.assertLessEqual(q, 1800)
        # 101 passe temporairement en dernier
        self.assertEqual(vh.reorder(ids, now=t), ["102", "103", "101"])
        self.assertIn("101", vh.snapshot(now=t))

        # Après 31 min, la HD redevient prioritaire automatiquement !
        t += 1860
        self.assertEqual(vh.quarantined_seconds("101", now=t), 0)
        self.assertEqual(vh.reorder(ids, now=t), ["101", "102", "103"])
        self.assertEqual(vh.snapshot(now=t), {})

        # Récidive → quarantaine plafonnée à 2 h max (7200 s)
        for _ in range(10):
            vh.record_failure("101", now=t)
            t += 5
        self.assertLessEqual(vh.quarantined_seconds("101", now=t), 7200)

        # Un succès durable ou un reset lève tout immédiatement
        vh.record_success("101")
        self.assertEqual(vh.quarantined_seconds("101", now=t), 0)
        self.assertEqual(vh.reorder(ids, now=t), ["101", "102", "103"])


class PinnedVersionAndAdaptiveM3UTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fake_srv, self.fake_url = _start_mini_xtream(
            categories=[{"category_id": "10", "category_name": "FR | TNT"}],
            streams=[
                {"stream_id": 100, "name": "FR: TF1 FHD", "category_id": "10", "num": 1, "epg_channel_id": "tf1.fr"},
                {"stream_id": 101, "name": "FR: TF1 HD", "category_id": "10", "num": 2, "epg_channel_id": "tf1.fr"},
                {"stream_id": 102, "name": "FR: TF1 SD", "category_id": "10", "num": 3, "epg_channel_id": "tf1.fr"},
            ],
        )
        self.config = Config(
            xtream_url=self.fake_url, username=USERNAME, password=PASSWORD,
            port=0, web_port=0, data_dir=Path(self.tmp.name), enable_epg=False,
        )
        self.api = XtreamAPI(self.config)
        self.store = PlaylistStore(self.config.data_dir)
        self.devices = DeviceStore(self.config.data_dir)
        self.streams = make_streams(self.config, self.api)

    def tearDown(self):
        self.fake_srv.shutdown()
        self.fake_srv.server_close()
        self.tmp.cleanup()

    def test_item_pinned_preserves_canonical_id_and_promotes_preferred_stream(self):
        saved = validate_playlist({
            "name": "Salon", "slug": "salon",
            "category_ids": ["10"],
            "dedupe": True,
            "failover": True,
            "adaptive_failover": True,
            "item_pinned": {"100": "101"},
        })
        self.assertTrue(saved["adaptive_failover"])
        self.assertEqual(saved["item_pinned"], {"100": "101"})

        entries, _ = select_items(self.api, saved, "live")
        self.assertEqual(len(entries), 1)
        item, _ = entries[0]
        # L'identifiant canonique (pour EPG & favoris) reste "100"
        self.assertEqual(item["stream_id"], "100")
        self.assertEqual(item["pinned_stream"], "101")
        # Les secours incluent l'originale "100" puis "102"
        self.assertEqual(item["alternates"], ["100", "102"])

        # Vérifie aussi l'émulateur Xtream (stream_plan)
        self.store.save(saved)
        dev = self.devices.create("Salon", playlist="salon")
        emu = XtreamEmulator(self.api, self.store, self.config)
        ids, adaptive = emu.stream_plan(dev, "100")
        self.assertEqual(ids, ["101", "100", "102"])
        self.assertTrue(adaptive)

    def test_m3u_emits_pinned_stream_first_and_af_flag(self):
        self.store.save({
            "name": "Salon", "slug": "salon",
            "category_ids": ["10"],
            "dedupe": True,
            "failover": True,
            "adaptive_failover": True,
            "item_pinned": {"100": "101"},
        })
        srv = make_server(self.config, host="127.0.0.1", port=0, api=self.api,
                          store=self.store, streams=self.streams, devices=self.devices)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            status, raw = _req(f"http://127.0.0.1:{port}/p/salon.m3u")
            self.assertEqual(status, 200)
            m3u = raw.decode("utf-8")
            self.assertIn("/stream/101.ts?alt=100,102&af=1", m3u)
        finally:
            srv.shutdown()
            srv.server_close()


class DevicePauseQrProbeAndExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.fake1_srv, self.fake1_url = _start_mini_xtream(
            categories=[{"category_id": "10", "category_name": "FR | GENERALISTE"}],
            streams=[
                {"stream_id": 100, "name": "FR: TF1 ᴴᴰ", "category_id": "10", "num": 1, "epg_channel_id": "tf1.fr"},
                {"stream_id": 101, "name": "FR: M6 FHD", "category_id": "10", "num": 2, "epg_channel_id": "m6.fr"},
            ],
        )
        self.fake2_srv, self.fake2_url = _start_mini_xtream(
            categories=[{"category_id": "20", "category_name": "MAROC | SNRT"}],
            streams=[
                {"stream_id": 200, "name": "MA | 2M MAROC HD", "category_id": "20", "num": 1, "epg_channel_id": "2m.ma"},
            ],
        )
        self.config = Config(
            xtream_url=self.fake1_url, username=USERNAME, password=PASSWORD,
            port=0, web_port=0, data_dir=self.data_dir, enable_epg=False,
            extra_providers=[("Compte 2", self.fake2_url, USERNAME, PASSWORD)],
        )
        api1 = XtreamAPI(self.config)
        cfg2 = Config(xtream_url=self.fake2_url, username=USERNAME, password=PASSWORD,
                      port=0, web_port=0, data_dir=self.data_dir, enable_epg=False)
        api2 = XtreamAPI(cfg2)
        self.api = ProviderSet([api1, api2], ["Principal", "Compte 2"], [1, 2])
        self.store = PlaylistStore(self.data_dir)
        self.devices = DeviceStore(self.data_dir)
        self.streams = make_streams(self.config, self.api)
        settings.install(settings.Settings(self.data_dir))

        self.playlist_srv = make_server(self.config, host="127.0.0.1", port=0, api=self.api,
                                        store=self.store, streams=self.streams, devices=self.devices)
        from dataclasses import replace
        self.config = replace(self.config, port=self.playlist_srv.server_address[1])
        threading.Thread(target=self.playlist_srv.serve_forever, daemon=True).start()

        self.web_srv = make_web_server(self.config, self.api, self.store, host="127.0.0.1", port=0,
                                       streams=self.streams, devices=self.devices)
        self.web_url = f"http://127.0.0.1:{self.web_srv.server_address[1]}"
        self.play_url = f"http://127.0.0.1:{self.config.port}"
        threading.Thread(target=self.web_srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web_srv.shutdown()
        self.web_srv.server_close()
        self.playlist_srv.shutdown()
        self.playlist_srv.server_close()
        self.fake1_srv.shutdown()
        self.fake1_srv.server_close()
        self.fake2_srv.shutdown()
        self.fake2_srv.server_close()
        settings.install(None)
        self.tmp.cleanup()

    def test_device_pause_blocks_stream_and_resumes_cleanly(self):
        self.store.save({"name": "Salon", "slug": "salon", "category_ids": ["10"]})
        status, body = _req(f"{self.web_url}/api/devices", "POST", {"name": "TV Salon", "playlist": "salon"})
        self.assertEqual(status, 201)
        dev = json.loads(body)
        self.assertFalse(dev["paused"])

        # Active -> flux accessible
        st, chunk = _req(f"{self.play_url}/stream/100.ts?token={dev['password']}")
        self.assertEqual(st, 200)
        self.assertTrue(len(chunk) > 0)

        # Mise en pause -> 403 sur /stream/ et sur Xtream /live/
        st, body = _req(f"{self.web_url}/api/devices/{dev['id']}", "PUT", {"paused": True})
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["paused"])

        st, _ = _req(f"{self.play_url}/stream/100.ts?token={dev['password']}")
        self.assertEqual(st, 403)
        st, _ = _req(f"{self.play_url}/live/{dev['username']}/{dev['password']}/100.ts")
        self.assertEqual(st, 403)

        # Reprise -> 200 de nouveau
        st, body = _req(f"{self.web_url}/api/devices/{dev['id']}", "PUT", {"paused": False})
        self.assertEqual(st, 200)
        self.assertFalse(json.loads(body)["paused"])
        st, _ = _req(f"{self.play_url}/stream/100.ts?token={dev['password']}")
        self.assertEqual(st, 200)

        # Rétrocompatibilité : un ancien devices.json sans champ "paused" se charge avec paused=False
        raw_path = self.data_dir / "devices.json"
        raw_doc = json.loads(raw_path.read_text(encoding="utf-8"))
        raw_doc["devices"][0].pop("paused", None)
        raw_path.write_text(json.dumps(raw_doc), encoding="utf-8")
        reloaded = DeviceStore(self.data_dir).list()
        self.assertFalse(reloaded[0]["paused"])

    def test_device_qr_m3u_and_xtream(self):
        self.store.save({"name": "Chambre", "slug": "chambre", "category_ids": ["10"]})
        _, body = _req(f"{self.web_url}/api/devices", "POST", {"name": "Box Chambre", "playlist": "chambre"})
        dev = json.loads(body)

        st, body = _req(f"{self.web_url}/api/devices/{dev['id']}/qr?mode=m3u")
        self.assertEqual(st, 200)
        payload = json.loads(body)
        self.assertIn("<svg", payload["qr_svg"])
        self.assertIn(f"/p/chambre.m3u?token={dev['password']}", payload["url"])

        st, body = _req(f"{self.web_url}/api/devices/{dev['id']}/qr?mode=xtream")
        self.assertEqual(st, 200)
        payload = json.loads(body)
        self.assertIn("<svg", payload["qr_svg"])
        self.assertTrue(payload["url"].startswith("http://"))

    def test_probe_channels_and_health_reset(self):
        self.api.catalog(kind="live")
        sid2 = encode(2, "200")
        # Mettre 100 en quarantaine artificielle pour vérifier quarantined_for + reset
        self.streams.health.record_failure("100")
        self.streams.health.record_failure("100")

        st, body = _req(f"{self.web_url}/api/channels/probe", "POST", {"stream_ids": ["100", sid2, "999999"]})
        self.assertEqual(st, 200)
        results = {r["stream_id"]: r for r in json.loads(body)["results"]}
        self.assertTrue(results["100"]["ok"])
        self.assertEqual(results["100"]["provider"], "1")
        self.assertGreater(results["100"]["quarantined_for"], 0)
        self.assertTrue(results[sid2]["ok"])
        self.assertEqual(results[sid2]["provider"], "2")
        self.assertEqual(results[sid2]["provider_name"], "Compte 2")
        self.assertFalse(results["999999"]["ok"])
        self.assertEqual(results["999999"]["status"], 404)

        # Reset quarantaines via API
        st, body = _req(f"{self.web_url}/api/streams/health/reset", "POST", {})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["health_quarantined"], {})
        self.assertEqual(self.streams.health.quarantined_seconds("100"), 0)

    def test_export_all_channels_multi_provider_csv_json_txt(self):
        # CSV (défaut)
        st, raw = _req(f"{self.web_url}/api/catalog/channels-export?format=csv")
        self.assertEqual(st, 200)
        text = raw.decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(len(rows), 3)
        by_name = {r["name"]: r for r in rows}
        self.assertEqual(by_name["FR: TF1 ᴴᴰ"]["provider_index"], "1")
        self.assertEqual(by_name["FR: TF1 ᴴᴰ"]["provider_name"], "Principal")
        self.assertEqual(by_name["FR: TF1 ᴴᴰ"]["clean_name"], "TF1")
        self.assertEqual(by_name["FR: TF1 ᴴᴰ"]["epg_id"], "tf1.fr")
        self.assertEqual(by_name["MA | 2M MAROC HD"]["provider_index"], "2")
        self.assertEqual(by_name["MA | 2M MAROC HD"]["provider_name"], "Compte 2")
        self.assertEqual(by_name["MA | 2M MAROC HD"]["clean_name"], "2M MAROC")
        # Aucun identifiant ni mot de passe dans l'export
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(USERNAME, text)

        # JSON
        st, raw = _req(f"{self.web_url}/api/catalog/channels-export?format=json")
        self.assertEqual(st, 200)
        doc = json.loads(raw)
        self.assertEqual(doc["total"], 3)
        self.assertEqual([(p["index"], p["name"], p["count"]) for p in doc["providers"]],
                         [(1, "Principal", 2), (2, "Compte 2", 1)])
        self.assertNotIn(PASSWORD, raw.decode("utf-8"))

        # TXT
        st, raw = _req(f"{self.web_url}/api/catalog/channels-export?format=txt")
        self.assertEqual(st, 200)
        txt = raw.decode("utf-8")
        self.assertIn("[1 - Principal] FR | GENERALISTE :: FR: TF1 ᴴᴰ  (-> TF1)", txt)
        self.assertIn("[2 - Compte 2] MAROC | SNRT :: MA | 2M MAROC HD  (-> 2M MAROC)", txt)


class EnrichedTelegramAlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.sent: list[dict] = []

        sent_ref = self.sent

        class FakeTgHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path.endswith("/getMe"):
                    out = json.dumps({"ok": True, "result": {"id": 99, "username": "xtream_test_bot"}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                sent_ref.append(body)
                out = json.dumps({"ok": True, "result": {"message_id": len(sent_ref)}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.tg_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeTgHandler)
        threading.Thread(target=self.tg_srv.serve_forever, daemon=True).start()
        self.tg_base = f"http://127.0.0.1:{self.tg_srv.server_address[1]}"

        self.fake_srv, self.fake_url = _start_mini_xtream(
            categories=[{"category_id": "10", "category_name": "FR"}],
            streams=[{"stream_id": 100, "name": "TF1", "category_id": "10", "num": 1}],
        )
        self.config = Config(
            xtream_url=self.fake_url, username=USERNAME, password=PASSWORD,
            port=0, web_port=0, data_dir=self.data_dir, enable_epg=False,
        )
        self.api = XtreamAPI(self.config)
        self.store = PlaylistStore(self.data_dir)
        self.streams = make_streams(self.config, self.api)
        self.mgr = alerts.AlertManager(self.data_dir, api_base=self.tg_base)
        alerts.install(self.mgr)
        self.web_srv = make_web_server(self.config, self.api, self.store, host="127.0.0.1", port=0, streams=self.streams)
        self.web_url = f"http://127.0.0.1:{self.web_srv.server_address[1]}"
        threading.Thread(target=self.web_srv.serve_forever, daemon=True).start()

    def tearDown(self):
        alerts.install(None)
        self.web_srv.shutdown()
        self.web_srv.server_close()
        self.tg_srv.shutdown()
        self.tg_srv.server_close()
        self.fake_srv.shutdown()
        self.fake_srv.server_close()
        self.tmp.cleanup()

    def test_weekly_summary_and_unstable_event_options(self):
        # Jeton factice assemblé dynamiquement (aucune chaîne statique ressemblant à un vrai secret)
        fake_token = ":".join(("987654321", "AA" + "FakeTestTokenForUnitTestsOnly01"))
        st, body = _req(f"{self.web_url}/api/alerts/manual", "POST", {"bot_token": fake_token, "chat_id": "424242"})
        self.assertEqual(st, 200)
        state = json.loads(body)
        self.assertTrue(state["connected"])
        self.assertTrue(state["events"]["unstable"])
        self.assertFalse(state["events"]["weekly"])

        # Activer le récap hebdo et désactiver unstable
        st, body = _req(f"{self.web_url}/api/alerts", "PUT", {"events": {"weekly": True, "unstable": False}})
        self.assertEqual(st, 200)
        state = json.loads(body)
        self.assertTrue(state["events"]["weekly"])
        self.assertFalse(state["events"]["unstable"])

        # Enregistrer une lecture récente dans l'historique puis déclencher /api/alerts/weekly
        self.streams._history.append({
            "kind": "live", "item_id": "100", "label": "TF1 HD", "device": "Salon",
            "provider": "1", "profile": "1", "started_at": int(time.time()) - 1800,
            "seconds": 1800, "switches": 2,
        })
        self.sent.clear()
        st, _ = _req(f"{self.web_url}/api/alerts/weekly", "POST")
        self.assertEqual(st, 200)
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]["text"]
        self.assertIn("7 derniers jours", msg)
        self.assertIn("Salon", msg)
        self.assertIn("TF1 HD", msg)
        self.assertIn("bascule", msg)


if __name__ == "__main__":
    unittest.main()
