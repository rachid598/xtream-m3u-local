"""Tests des nouveautés 3.33.0 : compatibilité ascendante, LiveHub partagé, débordement multi-fournisseurs,
espaces membres vs compléments/secours, filtre adulte et aperçu TV instantané."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from app import provider_store, settings
from app.builder import is_adult_category, is_adult_item, select_items
from app.custom import PlaylistStore, validate_playlist
from app.devices import DeviceStore
from app.providers import OFFSET, ApiHolder, encode
from app.server import Config, XtreamAPI, make_api, make_epg, make_series, make_server, make_streams
from app.web import make_web_server

from .fake_xtream import CATEGORIES, PASSWORD, STREAMS, USERNAME, VOD_CATEGORIES, VOD_STREAMS, FakeXtream
from .helpers import StackTestCase


class FakeProviderB(FakeXtream):
    """Second fournisseur Xtream isolé pour les tests de débordement et de complément."""
    paths: list[str] = []
    open_streams = 0

    def do_GET(self):
        type(self).paths.append(self.path.split("?")[0])
        return super().do_GET()


class BackwardsCompatibilityTests(StackTestCase):
    """Garantie absolue : aucun abonnement, playlist, appareil ou réglage existant n'est perdu à la mise à jour."""

    def tearDown(self):
        provider_store.install(None)
        settings.install(None)
        super().tearDown()

    def test_existing_v332_data_files_load_intact(self):
        data_dir = Path(self.tmp.name) / "existing_data"
        data_dir.mkdir(parents=True)

        # 1. providers.json existant (créé en 3.32)
        (data_dir / "providers.json").write_text(json.dumps({
            "providers": [{
                "index": 2,
                "name": "Abonnement Sœur",
                "url": self.provider_url,
                "username": USERNAME,
                "password": PASSWORD,
                "user_agent": "TiviMate/5.1",
                "created_at": 1700000000,
            }]
        }), encoding="utf-8")

        # 2. playlists.json existant (tableau JSON direct sur disque, sans hide_adult ni nouveaux champs)
        (data_dir / "playlists.json").write_text(json.dumps([{
            "slug": "salon",
            "name": "Salon Familial",
            "category_ids": ["1", "10"],
            "included_stream_ids": ["42"],
            "excluded_stream_ids": [],
            "vod_category_ids": ["1"],
            "included_vod_ids": [],
            "excluded_vod_ids": [],
            "series_category_ids": ["1"],
            "included_series_ids": [],
            "excluded_series_ids": [],
            "group_order": ["g:tnt", "1"],
            "group_names": {"1": "Infos FR"},
            "live_format": "ts",
            "catchup": True,
            "dedupe": True,
            "failover": True,
            "failover_any_category": False,
            "uhd": "backup",
            "hide_separators": True,
            "favorites_name": "★ Favoris",
            "favorites": ["42"],
            "rules": [],
            "item_names": {"42": "franceinfo:"},
            "item_numbers": {"42": 27},
            "item_logos": {},
            "item_epg": {},
            "item_xmltv": {},
            "custom_groups": [{"id": "tnt", "name": "TNT", "match": "words", "patterns": ["France Info"]}],
            "item_groups": {},
        }]), encoding="utf-8")

        # 3. devices.json existant (sans champ profile)
        (data_dir / "devices.json").write_text(json.dumps({
            "require_token": True,
            "devices": [{
                "id": "dev1",
                "name": "Shield Salon",
                "username": "shield-salon",
                "password": "secretcode",
                "playlist": "salon",
                "priority": "high",
                "ip": "192.168.1.50",
                "links": "lan",
                "created_at": 1700000000,
            }]
        }), encoding="utf-8")

        # 4. settings.json existant (sans aux_providers, avec epg_sources vidé volontairement)
        (data_dir / "settings.json").write_text(json.dumps({
            "tailscale_local": True,
            "mixable": [2],
            "public_hosts": ["tv.exemple.fr"],
            "epg_sources": [],
            "epg_sources_initialized": True,
        }), encoding="utf-8")

        pstore = provider_store.ProviderStore(str(data_dir))
        sstore = settings.Settings(str(data_dir), default_epg_sources=("https://exemple.fr/default.xml.gz",))
        provider_store.install(pstore)
        settings.install(sstore)

        cfg = replace(self.stack.config, data_dir=str(data_dir), provider_name="Principal")
        api = ApiHolder(make_api(cfg, pstore, migrate=False))
        pl_store = PlaylistStore(str(data_dir))
        dev_store = DeviceStore(str(data_dir))

        self.assertEqual(api.indices, [1, 2])
        self.assertEqual(api.names, ["Principal", "Abonnement Sœur"])
        self.assertEqual(pstore.credentials(), [(USERNAME, PASSWORD)])

        playlists = pl_store.list()
        self.assertEqual(len(playlists), 1)
        self.assertEqual(playlists[0]["slug"], "salon")
        self.assertEqual(playlists[0]["name"], "Salon Familial")
        self.assertFalse(playlists[0]["hide_adult"])
        self.assertEqual(playlists[0]["item_numbers"], {"42": 27})

        devices = dev_store.list()
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["username"], "shield-salon")
        self.assertEqual(devices[0]["password"], "secretcode")
        self.assertEqual(devices[0]["profile"], "")
        self.assertTrue(dev_store.require_token)

        loaded_settings = sstore.get()
        self.assertTrue(loaded_settings["tailscale_local"])
        self.assertEqual(loaded_settings["mixable"], [2])
        self.assertEqual(loaded_settings["aux_providers"], [])
        self.assertEqual(loaded_settings["epg_sources"], [])


class SharedLiveHubTests(StackTestCase):
    """Deux écrans locaux sur la même chaîne en direct partagent 1 seule connexion fournisseur."""
    overrides = {"max_connections": 1, "share_streams": True}

    def test_two_screens_share_one_upstream_connection_with_handover(self):
        FakeXtream.open_streams = 0
        _, d1 = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV Salon", "priority": "normal"})
        _, d2 = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV Chambre", "priority": "normal"})

        url1 = f"{self.stack.proxy_base}/stream/50.ts?token={d1['password']}"
        url2 = f"{self.stack.proxy_base}/stream/50.ts?token={d2['password']}"

        r1 = urlopen(url1, timeout=5)
        try:
            first_chunk = r1.read(512)
            self.assertTrue(first_chunk.startswith(b"\x47"))
            self.assertEqual(FakeXtream.open_streams, 1)

            # Le 2e écran ouvre exactement la même chaîne alors que max_connections=1 :
            # LiveHub partage le flux sans ouvrir de 2e connexion ni couper TV Salon !
            r2 = urlopen(url2, timeout=5)
            try:
                second_chunk = r2.read(512)
                self.assertIn(b"\x47", second_chunk)
                self.assertEqual(FakeXtream.open_streams, 1)

                streams = json.loads(self.get(self.stack.web_base + "/api/streams")[1])["streams"]
                self.assertEqual(len(streams), 1)
                self.assertEqual(streams[0]["viewers"], 2)
                self.assertEqual(set(streams[0]["devices"]), {"TV Salon", "TV Chambre"})
                self.assertEqual(set(streams[0]["device_ids"]), {d1["id"], d2["id"]})

                # Passation de relais : TV Salon (lecteur initial) quitte en premier,
                # TV Chambre continue à recevoir le direct sans coupure !
                r1.close()
                r1 = None
                time.sleep(0.15)
                more_data = r2.read(1024)
                self.assertTrue(len(more_data) >= 512)
                self.assertEqual(FakeXtream.open_streams, 1)
            finally:
                r2.close()
        finally:
            if r1 is not None:
                r1.close()

        # Une fois le dernier écran fermé, la connexion amont est libérée.
        deadline = time.monotonic() + 3.0
        while FakeXtream.open_streams > 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(FakeXtream.open_streams, 0)

    def test_range_request_bypasses_live_hub(self):
        status, body, headers = self.get(f"{self.stack.proxy_base}/stream/42.ts", headers={"Range": "bytes=3-"})
        self.assertEqual(status, 206)
        self.assertEqual(body, b"DATA 12345678")
        self.assertIn("Content-Range", headers)


class MultiProviderOverflowTests(StackTestCase):
    """Débordement automatique vers un 2e abonnement quand le 1er est saturé."""

    def setUp(self):
        super().setUp()
        FakeXtream.open_streams = 0
        FakeProviderB.paths = []
        FakeProviderB.open_streams = 0
        self.server_b = ThreadingHTTPServer(("127.0.0.1", 0), FakeProviderB)
        self.server_b.daemon_threads = True
        threading.Thread(target=self.server_b.serve_forever, daemon=True).start()
        url_b = f"http://127.0.0.1:{self.server_b.server_address[1]}"

        sub_dir = os.path.join(self.tmp.name, "overflow")
        os.makedirs(sub_dir, exist_ok=True)
        self.sstore = settings.Settings(sub_dir)
        settings.install(self.sstore)
        settings.set_aux(2, True)  # Abonnement 2 en complément / secours (et donc mélangeable)

        self.cfg = replace(
            self.stack.config,
            data_dir=sub_dir,
            max_connections=1,
            provider_name="Principal",
            extra_providers=(("Secours B", url_b, USERNAME, PASSWORD),),
        )
        self.api = make_api(self.cfg)
        self.store = PlaylistStore(sub_dir)
        self.streams = make_streams(self.cfg, self.api)
        self.devices = DeviceStore(sub_dir)
        self.proxy = make_server(
            self.cfg, host="127.0.0.1", port=0, api=self.api, store=self.store,
            epg=make_epg(self.cfg, self.api), series=make_series(self.cfg, self.api),
            streams=self.streams, devices=self.devices,
        )
        self.web = make_web_server(
            self.cfg, self.api, self.store, host="127.0.0.1", port=0,
            streams=self.streams, devices=self.devices,
        )
        for srv in (self.proxy, self.web):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        self.web_base = f"http://127.0.0.1:{self.web.server_address[1]}"

    def tearDown(self):
        settings.install(None)
        for srv in (self.web, self.proxy, self.server_b):
            srv.shutdown()
            srv.server_close()
        super().tearDown()

    def test_overflows_to_second_provider_when_primary_limit_reached(self):
        salon = self.devices.create("TV Salon", priority="high")
        cuisine = self.devices.create("TV Cuisine", priority="normal")
        alt_b = encode(2, "42")

        # TV Salon (haute priorité) occupe l'unique connexion de l'abonnement 1 sur la chaîne 50.
        r1 = urlopen(f"{self.base}/stream/50.ts?token={salon['password']}", timeout=5)
        try:
            self.assertTrue(r1.read(256).startswith(b"\x47"))
            # TV Cuisine demande une autre chaîne (42) ayant un secours sur l'abonnement 2 :
            # l'abonnement 1 est plein et non préemptible -> débordement immédiat sur Secours B !
            status, body, _ = self.get(f"{self.base}/stream/42.ts?alt={alt_b}&token={cuisine['password']}")
            self.assertEqual(status, 200)
            self.assertEqual(body, b"TS DATA 12345678")
            self.assertIn(f"/live/{USERNAME}/{PASSWORD}/42.ts", FakeProviderB.paths)
        finally:
            r1.close()

    def test_overflows_to_second_provider_on_upstream_429_busy(self):
        alt_b = encode(2, "42")
        # La chaîne 70 sur l'abonnement 1 renvoie HTTP 429 Too Many Requests :
        # au lieu d'abandonner, le relais bascule immédiatement sur l'abonnement 2 !
        status, body, _ = self.get(f"{self.base}/stream/70.ts?alt={alt_b}")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"TS DATA 12345678")
        self.assertIn(f"/live/{USERNAME}/{PASSWORD}/42.ts", FakeProviderB.paths)

    def test_aux_provider_hidden_from_member_spaces_but_available_in_sources(self):
        status = json.loads(self.get(self.web_base + "/api/status")[1])
        self.assertEqual([p["id"] for p in status["profiles"]], ["1"])
        self.assertEqual([(s["id"], s["aux"], s["mixable"]) for s in status["sources"]],
                         [("1", False, False), ("2", True, True)])

        # Sécurité : si une playlist existe déjà sur l'abonnement 2, l'espace 2 reste visible.
        res, _ = self.send(self.web_base + "/api/playlists", "POST",
                           {"slug": "secours-pl", "name": "Playlist B", "provider": "2",
                            "category_ids": [encode(2, "1")]})
        self.assertEqual(res, 201)
        status_after = json.loads(self.get(self.web_base + "/api/status")[1])
        self.assertEqual([p["id"] for p in status_after["profiles"]], ["1", "2"])

    def test_profile_scoped_summary_export_and_device_xtream_isolation(self):
        summary_all = json.loads(self.get(self.web_base + "/api/catalog/summary")[1])
        summary_p2 = json.loads(self.get(self.web_base + "/api/catalog/summary?profile=2")[1])
        self.assertEqual(summary_p2["live"]["items"], summary_all["live"]["items"] // 2)

        status, raw_export, _ = self.get(self.web_base + "/api/catalog/export?kind=live&profile=2")
        self.assertEqual(status, 200)
        exported = json.loads(raw_export)
        self.assertEqual(exported["counts"]["valid_streams"], summary_p2["live"]["items"])

        # Appareil créé dans l'espace 2 sans playlist spécifique : isolé à l'abonnement 2 en Xtream Codes.
        res, dev = self.send(self.web_base + "/api/devices", "POST",
                             {"name": "Apple TV Sœur", "playlist": "", "profile": "2"})
        self.assertEqual(res, 201)
        self.assertEqual(dev["profile"], "2")
        qs = f"username={dev['username']}&password={dev['password']}&action=get_live_streams"
        live_list = json.loads(self.get(f"{self.base}/player_api.php?{qs}")[1])
        self.assertTrue(live_list)
        # Les identifiants Xtream émulés décodent tous vers l'abonnement 2 (>= 2 * OFFSET en interne).
        emu_cats = json.loads(self.get(
            f"{self.base}/player_api.php?username={dev['username']}&password={dev['password']}&action=get_live_categories"
        )[1])
        self.assertTrue(all(int(c["category_id"]) != 1 for c in emu_cats))


class AdultFilterAndPreviewTests(StackTestCase):
    """Filtre adulte (18+ / XXX) et aperçu TV instantané (/api/playlists/preview)."""

    def test_adult_detection_and_filtering_across_kinds_and_alternates(self):
        self.assertTrue(is_adult_category("FOR ADULTS"))
        self.assertTrue(is_adult_category("FR | Adultes 18+"))
        self.assertTrue(is_adult_category("XXX | VIP"))
        self.assertFalse(is_adult_category("Cartoon Network / Adult Swim"))
        self.assertFalse(is_adult_category("FR | Généralistes"))
        self.assertTrue(is_adult_item({"name": "Dorcel TV HD", "category_ids": ["1"]}, {"1": "FR | Généralistes"}))
        self.assertTrue(is_adult_item({"name": "Canal Nuit", "adult": True, "category_ids": ["1"]}, {"1": "FR"}))
        self.assertFalse(is_adult_item({"name": "T18 HD", "category_ids": ["1"]}, {"1": "FR | TNT"}))

        extra_cats = CATEGORIES + [{"category_id": "99", "category_name": "FOR ADULTS", "parent_id": 0}]
        extra_streams = STREAMS + [
            {"num": 90, "name": "Dorcel TV HD", "stream_type": "live", "stream_id": 901, "category_id": "99"},
            {"num": 91, "name": "Chaîne Cachée", "stream_type": "live", "stream_id": 902, "category_id": "1", "is_adult": "1"},
            {"num": 92, "name": "Adult Swim Toons", "stream_type": "live", "stream_id": 903, "category_id": "1"},
            {"num": 93, "name": "France Info SD", "stream_type": "live", "stream_id": 904, "category_id": "99", "is_adult": "1"},
        ]
        extra_vod_cats = VOD_CATEGORIES + [{"category_id": "99", "category_name": "VOD | XXX 18+", "parent_id": 0}]
        extra_vod = VOD_STREAMS + [
            {"num": 10, "name": "Film X", "stream_type": "movie", "stream_id": 599, "category_id": "99", "container_extension": "mp4"},
        ]

        from unittest import mock
        with mock.patch.multiple(
            "tests.fake_xtream",
            CATEGORIES=extra_cats,
            STREAMS=extra_streams,
            VOD_CATEGORIES=extra_vod_cats,
            VOD_STREAMS=extra_vod,
        ):
            api = XtreamAPI(self.stack.config)
            saved = validate_playlist({
                "slug": "famille",
                "name": "Famille",
                "category_ids": ["1", "99"],
                "vod_category_ids": ["1", "99"],
                "dedupe": True,
                "failover": True,
                "failover_any_category": True,
                "hide_adult": True,
            })
            live_entries, live_groups = select_items(api, saved, "live", refresh=True)
            vod_entries, vod_groups = select_items(api, saved, "vod", refresh=True)

        live_ids = {item["stream_id"] for item, _ in live_entries}
        self.assertNotIn("901", live_ids)
        self.assertNotIn("902", live_ids)
        self.assertNotIn("904", live_ids)
        self.assertIn("903", live_ids)  # Adult Swim n'est pas un contenu 18+ !
        self.assertNotIn("99", live_groups)

        # Même avec failover_any_category=True, une variante adulte (904) ne sert jamais de secours à France Info (42).
        fi = next(item for item, _ in live_entries if item["stream_id"] == "42")
        self.assertNotIn("904", fi.get("alternates") or [])

        vod_ids = {item["stream_id"] for item, _ in vod_entries}
        self.assertNotIn("599", vod_ids)
        self.assertIn("501", vod_ids)
        self.assertNotIn("vod:99", vod_groups)

    def test_playlist_preview_endpoint_works_before_saving(self):
        # Aucune playlist enregistrée : /api/playlists/preview calcule le rendu exact d'un brouillon.
        draft = {
            "name": "Brouillon Salon",
            "slug": "",
            "category_ids": ["1", "10"],
            "vod_category_ids": ["1"],
            "favorites": ["42"],
            "custom_groups": [{"id": "infos", "name": "Mes Infos", "match": "words", "patterns": ["1: France Info"]}],
            "dedupe": True,
            "failover": True,
        }
        status, data = self.send(self.stack.web_base + "/api/playlists/preview", "POST", draft)
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["counts"]["live"], 2)
        self.assertGreaterEqual(data["counts"]["vod"], 1)
        self.assertEqual(data["groups"][0]["kind"], "favorites")
        self.assertEqual(data["groups"][1]["name"], "Mes Infos")
        self.assertTrue(data["groups"][1]["custom"])
        first_in_cg = data["groups"][1]["items"][0]
        self.assertEqual(first_in_cg["name"], "France Info HD")
        self.assertEqual(first_in_cg["num"], "1")
        # Et aucune playlist n'a été écrite sur le disque.
        self.assertEqual(self.stack.store.list(), [])
