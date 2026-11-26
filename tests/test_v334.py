"""Tests v3.34.0 : répartition souple des connexions (Option B) et choix de l'espace bénéficiaire."""
from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import provider_store, settings
from app.custom import PlaylistStore
from app.devices import DeviceStore
from app.providers import ApiHolder, encode
from app.server import Config, make_api, make_epg, make_series, make_server, make_streams
from app.streams import LimitReached, StreamManager
from app.web import make_web_server

from .fake_xtream import PASSWORD, USERNAME, FakeXtream
from .helpers import StackTestCase


class FakeMultiConnProvider(FakeXtream):
    """Fournisseur Xtream annonçant 3 connexions simultanées (max_connections = 3)."""
    max_cons = "3"

    def do_GET(self):
        if "/player_api.php" in self.path and "action=" not in self.path:
            payload = {
                "user_info": {
                    "username": USERNAME, "password": PASSWORD, "auth": 1, "status": "Active",
                    "exp_date": "1893456000", "is_trial": "0", "active_cons": "0",
                    "max_connections": self.max_cons, "allowed_output_formats": ["m3u8", "ts"],
                },
                "server_info": {
                    "url": "127.0.0.1", "port": str(self.server.server_address[1]),
                    "server_protocol": "http", "timezone": "Europe/Paris",
                },
            }
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        return super().do_GET()


class ReservationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = settings.Settings(self.tmp.name)
        settings.install(self.st)

    def tearDown(self):
        settings.install(None)
        self.tmp.cleanup()

    def test_v333_settings_file_loads_with_empty_reservations(self):
        path = Path(self.tmp.name) / "settings.json"
        path.write_text(json.dumps({
            "tailscale_local": True, "mixable": [2], "aux_providers": [3], "public_hosts": [], "epg_sources": []
        }), encoding="utf-8")
        data = self.st.get()
        self.assertEqual(data["reservations"], {})
        self.assertEqual(data["mixable"], [2])
        self.assertEqual(data["aux_providers"], [3])

    def test_set_and_clear_reservations(self):
        settings.set_reservation(2, "2", 1)
        settings.set_reservation(3, "2", 2)
        self.assertEqual(settings.reservation_for(2), ("2", 1))
        self.assertEqual(settings.reservation_for("3"), ("2", 2))
        # Supprimer l'abonnement 2 efface à la fois sa propre réservation et celles pointant vers l'espace 2.
        settings.clear_reservations_for(2)
        self.assertIsNone(settings.reservation_for(2))
        self.assertIsNone(settings.reservation_for(3))


class OptionBSoftReservationTests(unittest.TestCase):
    """Vérifie toute la table de vérité de l'Option B (3 connexions = 1 garantie Ma sœur + 2 secours)."""

    def _manager(self, *, takeover: bool = True, res_profile: str = "2", res_count: int = 1, limit: int = 3):
        return StreamManager(
            lambda p: limit if p == "2" else 1,
            takeover=takeover,
            reservation_for=lambda p: (res_profile, res_count) if p == "2" and res_count > 0 else None,
        )

    def test_backup_capped_at_2_leaving_3rd_slot_for_sister(self):
        mgr = self._manager(takeover=False)
        s1 = mgr.acquire(kind="live", item_id="201", client="10.0.0.1", provider="2",
                         device="TV Salon", consumer_profile="1")
        s2 = mgr.acquire(kind="live", item_id="202", client="10.0.0.2", provider="2",
                         device="TV Chambre", consumer_profile="1")
        # 3ᵉ lecture du secours : bloquée car la 3ᵉ connexion est sanctuarisée pour l'espace 2 !
        with self.assertRaises(LimitReached) as ctx:
            mgr.acquire(kind="live", item_id="203", client="10.0.0.3", provider="2",
                        device="Tablette", consumer_profile="1")
        self.assertEqual(ctx.exception.reserved_profile, "2")
        self.assertEqual(ctx.exception.args[0], 2)
        # Pendant ce temps, Ma sœur (espace 2) obtient immédiatement sa connexion garantie !
        sister = mgr.acquire(kind="live", item_id="204", client="10.0.0.9", provider="2",
                             device="TV Sœur", consumer_profile="2")
        self.assertEqual(mgr.count("2"), 3)
        self.assertTrue(mgr.is_active(sister))
        self.assertTrue(mgr.is_active(s1) and mgr.is_active(s2))

    def test_sister_borrows_free_backup_and_backup_reclaims_excess_only(self):
        mgr = self._manager(takeover=True)
        # Ma sœur ouvre 2 écrans (1 garantie + 1 empruntée au secours libre — Option B).
        sis1 = mgr.acquire(kind="live", item_id="201", client="10.0.0.10", provider="2",
                           device="TV Sœur 1", consumer_profile="2")
        time.sleep(0.01)
        sis2 = mgr.acquire(kind="live", item_id="202", client="10.0.0.11", provider="2",
                           device="TV Sœur 2", consumer_profile="2")
        # Moi utilise 1 connexion de secours (3/3 occupées : 2 sœur + 1 secours).
        b1 = mgr.acquire(kind="live", item_id="203", client="10.0.0.1", provider="2",
                         device="TV Salon", consumer_profile="1")
        self.assertEqual(mgr.count("2"), 3)
        # Moi demande une 2ᵉ connexion de secours (toujours dans son quota de 2) :
        # reprend la 2ᵉ TV empruntée de Ma sœur, SANS jamais toucher à sa 1ʳᵉ TV garantie !
        b2 = mgr.acquire(kind="live", item_id="204", client="10.0.0.2", provider="2",
                         device="TV Chambre", consumer_profile="1")
        self.assertTrue(mgr.is_active(sis1), "La 1ʳᵉ connexion garantie de Ma sœur doit rester intacte")
        self.assertFalse(mgr.is_active(sis2), "La 2ᵉ connexion empruntée doit être reprise par le secours")
        self.assertTrue(mgr.is_active(b1) and mgr.is_active(b2))

    def test_backup_takeover_never_evicts_sister_guaranteed_slot(self):
        mgr = self._manager(takeover=True)
        sis1 = mgr.acquire(kind="live", item_id="201", client="10.0.0.10", provider="2",
                           device="TV Sœur", priority="low", consumer_profile="2")
        b1 = mgr.acquire(kind="live", item_id="202", client="10.0.0.1", provider="2",
                         device="TV Salon", priority="high", consumer_profile="1")
        b2 = mgr.acquire(kind="live", item_id="203", client="10.0.0.2", provider="2",
                         device="TV Chambre", priority="high", consumer_profile="1")
        # Un 3ᵉ écran de secours (même priorité normale/basse) ne peut jamais couper Ma sœur (même si priorité basse !).
        with self.assertRaises(LimitReached) as ctx:
            mgr.acquire(kind="live", item_id="204", client="10.0.0.3", provider="2",
                        device="Tel", priority="normal", consumer_profile="1")
        self.assertEqual(ctx.exception.reserved_profile, "2")
        self.assertTrue(mgr.is_active(sis1))
        self.assertTrue(mgr.is_active(b1) and mgr.is_active(b2))

    def test_sister_second_screen_never_evicts_active_backup(self):
        mgr = self._manager(takeover=True)
        sis1 = mgr.acquire(kind="live", item_id="201", client="10.0.0.10", provider="2",
                           device="TV Sœur 1", priority="high", consumer_profile="2")
        b1 = mgr.acquire(kind="live", item_id="202", client="10.0.0.1", provider="2",
                         device="TV Salon", priority="low", consumer_profile="1")
        b2 = mgr.acquire(kind="live", item_id="203", client="10.0.0.2", provider="2",
                         device="TV Chambre", priority="low", consumer_profile="1")
        # Les 2 secours sont actifs ; la 2ᵉ TV de Ma sœur (priorité normale < sa 1ʳᵉ TV haute) est refusée
        # sans jamais couper les lectures de secours de Moi.
        with self.assertRaises(LimitReached):
            mgr.acquire(kind="live", item_id="204", client="10.0.0.11", provider="2",
                        device="TV Sœur 2", priority="normal", consumer_profile="2")
        self.assertTrue(mgr.is_active(sis1))
        self.assertTrue(mgr.is_active(b1) and mgr.is_active(b2))

    def test_move_respects_reserved_slot(self):
        mgr = self._manager(takeover=True)
        mgr.acquire(kind="live", item_id="201", client="10.0.0.1", provider="2", consumer_profile="1")
        mgr.acquire(kind="live", item_id="202", client="10.0.0.2", provider="2", consumer_profile="1")
        other_sess = mgr.acquire(kind="live", item_id="42", client="10.0.0.3", provider="1", consumer_profile="1")
        # Provider 2 a déjà 2/2 secours utilisés : move() refuse de prendre la 3ᵉ place réservée à l'espace 2.
        self.assertFalse(mgr.move(other_sess, "2"))
        # En revanche, une session de l'espace 2 peut basculer sur sa place garantie !
        sis_sess = mgr.acquire(kind="live", item_id="43", client="10.0.0.9", provider="1", consumer_profile="2")
        self.assertTrue(mgr.move(sis_sess, "2"))

    def test_cross_provider_reservation_for_chosen_space(self):
        """Un abonnement n°3 (complément 3 connexions) peut réserver 1 connexion pour l'espace 2 (Ma sœur)."""
        mgr = StreamManager(
            lambda p: 3 if p == "3" else 1,
            takeover=False,
            reservation_for=lambda p: ("2", 1) if p == "3" else None,
        )
        mgr.acquire(kind="live", item_id="301", client="10.0.0.1", provider="3", consumer_profile="1")
        mgr.acquire(kind="live", item_id="302", client="10.0.0.2", provider="3", consumer_profile="1")
        with self.assertRaises(LimitReached) as ctx:
            mgr.acquire(kind="live", item_id="303", client="10.0.0.3", provider="3", consumer_profile="1")
        self.assertEqual(ctx.exception.reserved_profile, "2")
        # L'espace 2 obtient bien sa connexion réservée sur l'abonnement 3 !
        sis = mgr.acquire(kind="live", item_id="304", client="10.0.0.9", provider="3", consumer_profile="2")
        self.assertTrue(mgr.is_active(sis))


class ProviderProbeAndReservationApiTests(StackTestCase):
    """Tests bout-en-bout : /api/providers/probe, ajout/modification/suppression avec réservation, M3U ?p=."""

    def setUp(self):
        super().setUp()
        self.multi_srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeMultiConnProvider)
        self.multi_port = self.multi_srv.server_address[1]
        self.multi_url = f"http://127.0.0.1:{self.multi_port}"
        threading.Thread(target=self.multi_srv.serve_forever, daemon=True).start()

        self.data_dir = Path(self.tmp.name) / "v334_api"
        self.data_dir.mkdir(parents=True)
        self.cfg = Config(
            xtream_url=self.provider_url, username=USERNAME, password=PASSWORD,
            data_dir=str(self.data_dir), provider_name="Moi",
            epg_startup_quiet=0, port=0, web_port=0,
        )
        self.pstore = provider_store.ProviderStore(str(self.data_dir))
        provider_store.install(self.pstore)
        self.st = settings.Settings(str(self.data_dir))
        settings.install(self.st)
        self.holder = ApiHolder(make_api(self.cfg, self.pstore, migrate=False))
        self.plstore = PlaylistStore(str(self.data_dir))
        self.devstore = DeviceStore(str(self.data_dir))
        self.epg = make_epg(self.cfg, self.holder)
        self.series = make_series(self.cfg, self.holder)
        self.streams = make_streams(self.cfg, self.holder)
        self.proxy = make_server(self.cfg, api=self.holder, store=self.plstore, epg=self.epg, series=self.series,
                                 streams=self.streams, devices=self.devstore, host="127.0.0.1", port=0)
        self.web = make_web_server(self.cfg, self.holder, self.plstore, host="127.0.0.1", port=0,
                                   epg=self.epg, series=self.series, streams=self.streams, devices=self.devstore)
        self.proxy_base = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        self.web_base = f"http://127.0.0.1:{self.web.server_address[1]}"
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        threading.Thread(target=self.web.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.shutdown()
        self.web.server_close()
        self.proxy.shutdown()
        self.proxy.server_close()
        self.multi_srv.shutdown()
        self.multi_srv.server_close()
        provider_store.install(None)
        settings.install(None)
        super().tearDown()

    def _api(self, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
        headers = {"X-Requested-With": "XtreamM3ULocal"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(self.web_base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_probe_add_edit_delete_multi_connection_provider_and_m3u_tag(self):
        # 1. Probe détecte les 3 connexions avant enregistrement.
        status, probed = self._api("/api/providers/probe", "POST", {
            "url": self.multi_url, "username": USERNAME, "password": PASSWORD,
        })
        self.assertEqual(status, 200)
        self.assertTrue(probed["ok"])
        self.assertEqual(probed["max_connections"], 3)

        # 2. Ajout de l'abonnement 2 (3 connexions) : 1 réservée à "self" (Ma sœur -> 2) + 2 secours.
        status, added = self._api("/api/providers", "POST", {
            "name": "Ma sœur", "url": self.multi_url, "username": USERNAME, "password": PASSWORD,
            "aux": False, "mixable": False,
            "reservation": {"profile": "self", "count": 1},
        })
        self.assertEqual(status, 201)
        self.assertEqual(added["index"], 2)
        self.assertEqual(added["max_connections"], 3)
        self.assertEqual(added["reservation"], {"profile": "2", "count": 1})

        # Comme il reste 2 connexions de secours (3 - 1 = 2), mixable est activé automatiquement.
        status, plist = self._api("/api/providers")
        self.assertEqual(status, 200)
        p2 = next(p for p in plist["providers"] if p["index"] == 2)
        self.assertEqual(p2["max_connections"], 3)
        self.assertTrue(p2["mixable"])
        self.assertEqual(p2["reservation"], {"profile": "2", "count": 1})

        # 3. Modification : réaffecter 2 connexions à l'espace 1 (Moi) via PUT /api/providers/2.
        status, updated = self._api("/api/providers/2", "PUT", {
            "name": "Ma sœur", "aux": False, "mixable": True,
            "reservation": {"profile": "1", "count": 2},
        })
        self.assertEqual(status, 200)
        self.assertEqual(updated["reservation"], {"profile": "1", "count": 2})
        self.assertEqual(settings.reservation_for(2), ("1", 2))

        # 4. M3U d'une playlist personnalisée tague les liens avec ?p=<espace> quand des réservations sont actives.
        self.plstore.save({"slug": "soeur", "name": "Sœur", "provider": "2", "category_ids": [encode(2, "1")]})
        with urlopen(self.proxy_base + "/p/soeur.m3u", timeout=10) as resp:
            m3u = resp.read().decode("utf-8")
        self.assertIn("?p=2", m3u)

        # 5. Suppression : nettoie la réservation.
        status, deleted = self._api("/api/providers/2", "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(deleted["deleted"])
        self.assertIsNone(settings.reservation_for(2))


if __name__ == "__main__":
    unittest.main()
