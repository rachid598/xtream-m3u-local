"""Limite de connexions simultanées et lectures en cours."""
import json
import threading
import time
import unittest
from urllib.request import urlopen

from app.streams import EvictedRecently, LimitReached, StreamManager

from .fake_xtream import FakeXtream
from .helpers import HEADERS, StackTestCase


class StreamManagerTests(unittest.TestCase):
    def manager(self, limit=1, takeover=True):
        return StreamManager(lambda provider: limit, takeover=takeover)

    def test_same_device_zapping_replaces_its_stream(self):
        m = self.manager()
        a = m.acquire(kind="live", item_id="1", client="tv1", label="Chaîne 1")
        b = m.acquire(kind="live", item_id="2", client="tv1", label="Chaîne 2")
        self.assertTrue(a.cancelled.is_set())
        self.assertFalse(b.cancelled.is_set())
        self.assertEqual([s["label"] for s in m.list()], ["Chaîne 2"])
        # Revenir sur la chaîne 1 depuis le même appareil reste possible (pas de délai).
        c = m.acquire(kind="live", item_id="1", client="tv1")
        self.assertTrue(b.cancelled.is_set())
        self.assertFalse(c.cancelled.is_set())

    def test_other_device_takes_over_and_cooldown(self):
        m = self.manager()
        a = m.acquire(kind="live", item_id="1", client="tv1")
        b = m.acquire(kind="live", item_id="2", client="tv2")
        self.assertTrue(a.cancelled.is_set())
        # Le lecteur de tv1 réessaie tout seul le même flux : refusé pendant le délai.
        with self.assertRaises(EvictedRecently):
            m.acquire(kind="live", item_id="1", client="tv1")
        # Mais tv1 peut choisir une autre chaîne (et reprend la main).
        c = m.acquire(kind="live", item_id="3", client="tv1")
        self.assertTrue(b.cancelled.is_set())
        self.assertEqual(m.count(), 1)
        m.release(c)
        self.assertEqual(m.count(), 0)

    def test_no_takeover(self):
        m = self.manager(takeover=False)
        a = m.acquire(kind="live", item_id="1", client="tv1")
        with self.assertRaises(LimitReached):
            m.acquire(kind="live", item_id="2", client="tv2")
        self.assertFalse(a.cancelled.is_set())
        # Le même appareil peut toujours zapper.
        b = m.acquire(kind="live", item_id="2", client="tv1")
        self.assertTrue(a.cancelled.is_set())
        self.assertFalse(b.cancelled.is_set())

    def test_unlimited_and_limit_two(self):
        m = self.manager(limit=0)
        for i in range(5):
            m.acquire(kind="live", item_id=str(i), client=f"tv{i}")
        self.assertEqual(m.count(), 5)
        m = self.manager(limit=2)
        first = m.acquire(kind="live", item_id="1", client="tv1")
        m.acquire(kind="live", item_id="2", client="tv2")
        m.acquire(kind="movie", item_id="9", client="tv3")
        self.assertTrue(first.cancelled.is_set())     # le plus ancien
        self.assertEqual(m.count(), 2)

    def test_virtual_hls_sessions(self):
        m = self.manager()
        a = m.acquire(kind="hls", item_id="1", client="tv1", virtual=True)
        again = m.acquire(kind="hls", item_id="1", client="tv1", virtual=True)
        self.assertIs(a, again)                        # rafraîchissement du manifeste
        from app import streams
        a.last_activity -= streams.HLS_IDLE_SECONDS + 1
        self.assertEqual(m.count(), 0)                 # expirée faute d'activité

    def test_stop(self):
        m = self.manager(limit=0)
        a = m.acquire(kind="live", item_id="1", client="tv1")
        closed = []
        a.attach(lambda: closed.append(True))
        self.assertTrue(m.stop(a.id))
        self.assertTrue(a.cancelled.is_set())
        self.assertEqual(closed, [True])
        self.assertFalse(m.stop(a.id))


class RelayLimitTests(StackTestCase):
    def read_stream(self, url, results, key):
        try:
            with urlopen(url, timeout=20) as r:
                total = 0
                start = time.monotonic()
                while True:
                    chunk = r.read(4096)
                    if not chunk:
                        break
                    total += len(chunk)
                results[key] = (total, time.monotonic() - start)
        except Exception as exc:  # pragma: no cover - diagnostic
            results[key] = exc

    def test_zapping_closes_previous_upstream(self):
        # Limite « auto » = celle de l'abonnement : la charger d'abord (sinon lue en arrière-plan, course possible).
        self.assertIsNotNone(self.stack.api.account())
        results = {}
        first = threading.Thread(target=self.read_stream, args=(self.stack.proxy_base + "/stream/50.ts", results, "a"))
        first.start()
        for _ in range(50):
            if self.stack.streams.count():
                break
            time.sleep(0.05)
        status = json.loads(self.get(self.stack.web_base + "/api/streams")[1])
        self.assertEqual(status["limit"], 1)            # valeur de l'abonnement (auto)
        self.assertTrue(status["auto"])
        self.assertEqual([s["item_id"] for s in status["streams"]], ["50"])
        # Zapping sur le même appareil : le premier flux s'arrête, le second démarre.
        second = threading.Thread(target=self.read_stream, args=(self.stack.proxy_base + "/stream/51.ts", results, "b"))
        second.start()
        first.join(10)
        self.assertFalse(first.is_alive())
        self.assertLess(results["a"][1], 8)              # coupé bien avant les 15 s du flux
        for _ in range(50):
            streams = json.loads(self.get(self.stack.web_base + "/api/streams")[1])["streams"]
            if [s["item_id"] for s in streams] == ["51"]:
                break
            time.sleep(0.05)
        self.assertEqual([s["item_id"] for s in streams], ["51"])
        # Arrêt depuis l'interface Web.
        status, body = self.send(self.stack.web_base + f"/api/streams/{streams[0]['id']}/stop", "POST")
        self.assertEqual(status, 200)
        second.join(10)
        self.assertFalse(second.is_alive())
        self.assertEqual(self.stack.streams.count(), 0)
        self.assertEqual(self.send(self.stack.web_base + "/api/streams/999/stop", "POST")[0], 404)
        self.assertEqual(self.send(self.stack.web_base + "/api/streams/1/stop", "POST", headers={})[0], 403)
        for _ in range(50):
            if FakeXtream.open_streams == 0:
                break
            time.sleep(0.1)
        self.assertEqual(FakeXtream.open_streams, 0)     # connexions fournisseur bien fermées

    def test_limit_refused_without_takeover(self):
        stack = self.make_stack(stream_takeover=False, max_connections=1)
        results = {}
        reader = threading.Thread(target=self.read_stream, args=(stack.proxy_base + "/stream/50.ts", results, "a"))
        reader.start()
        for _ in range(50):
            if stack.streams.count():
                break
            time.sleep(0.05)
        # Un autre appareil (simulé) est refusé (503) ; le même appareil peut zapper.
        from app.streams import LimitReached
        with self.assertRaises(LimitReached):
            stack.streams.acquire(kind="live", item_id="42", client="192.168.0.50")
        status, body, _ = self.get(stack.proxy_base + "/stream/42.ts")
        self.assertEqual((status, body), (200, b"TS DATA 12345678"))
        reader.join(10)
        self.assertFalse(reader.is_alive())

    def test_unlimited_configuration(self):
        stack = self.make_stack(max_connections=0)
        self.assertEqual(json.loads(self.get(stack.web_base + "/api/streams")[1])["limit"], 0)


if __name__ == "__main__":
    unittest.main()


class FailoverTests(StackTestCase):
    """Bascule automatique entre les versions d'une même chaîne (option ``failover``)."""

    def test_dedupe_keeps_alternates_best_first(self):
        from app.builder import dedupe
        entries = [({"stream_id": "1", "name": "VIP: TF1 ᴴᴰ"}, "g"), ({"stream_id": "2", "name": "FR: TF1 4K"}, "g"),
                   ({"stream_id": "3", "name": "AF: TF1"}, "g"), ({"stream_id": "4", "name": "TF1 FHD"}, "g"),
                   ({"stream_id": "5", "name": "M6"}, "g")]
        kept = dedupe(entries, "normal")                     # meilleure qualité d'abord (4K comprise)
        self.assertEqual([(e[0]["stream_id"], e[0].get("alternates")) for e in kept],
                         [("2", ["4", "1", "3"]), ("5", None)])
        kept = dedupe(entries)                                # défaut : 4K en dernier secours
        self.assertEqual([(e[0]["stream_id"], e[0].get("alternates")) for e in kept],
                         [("4", ["1", "3", "2"]), ("5", None)])

    def test_playlist_urls_carry_alternates_only_with_failover(self):
        from app.server import playlist
        entries = [({"stream_id": "2", "name": "TF1", "logo": "", "epg_id": "", "num": "", "alternates": ["4", "1"]}, "1")]
        on = playlist(self.stack.config, entries, {"1": "TNT"}, "http://h", failover=True).decode()
        off = playlist(self.stack.config, entries, {"1": "TNT"}, "http://h").decode()
        hls = playlist(self.stack.config, entries, {"1": "TNT"}, "http://h", failover=True, live_format="hls").decode()
        self.assertIn("http://h/stream/2.ts?alt=4,1", on)
        self.assertNotIn("alt=", off + hls)
        from app.server import parse_alternates
        self.assertEqual(parse_alternates("4,1,x,4,../9,1234567890123"), ["4", "1"])

    def test_reconnect_mode_closes_then_serves_working_version(self):
        stack = self.make_stack(failover_stall_seconds=1.0)  # mode par défaut : reconnect
        self.assertEqual(stack.config.failover_mode, "reconnect")
        # 1re connexion : 60 envoie un peu puis se tait → connexion fermée (pas de flux collé).
        status, body, _ = self.get(stack.proxy_base + "/stream/60.ts?alt=62,61")
        self.assertEqual((status, body), (200, b"A" * 4096))
        # Le lecteur se reconnecte : 62 (absent) est passé avant tout envoi, 61 est servi directement.
        status, body, _ = self.get(stack.proxy_base + "/stream/60.ts?alt=62,61")
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"B" * 10240), body[:20])
        self.assertNotIn(b"A", body)
        # 61 s'est terminée (fin du faux flux) : la reconnexion suivante repart de la meilleure version.
        self.assertEqual(stack.proxy.RequestHandlerClass.failover_memory["60"]["sid"], "60")

    def test_relay_switches_to_next_version(self):
        stack = self.make_stack(failover_stall_seconds=1.0, failover_mode="splice")
        start = time.monotonic()
        status, body, _ = self.get(stack.proxy_base + "/stream/60.ts?alt=62,61")
        self.assertEqual(status, 200)
        # 60 se tait → 62 absent (404) → 61 fonctionne.
        self.assertTrue(body.startswith(b"A" * 4096 + b"B" * 10240), body[:40] + body[4090:4110])
        self.assertLess(time.monotonic() - start, 15)
        for _ in range(50):
            if not stack.streams.count():
                break
            time.sleep(0.05)
        self.assertEqual(stack.streams.count(), 0)

    def test_option_saved(self):
        self.create({"slug": "f", "name": "F", "category_ids": ["1"], "dedupe": True, "failover": True})
        self.assertTrue(self.stack.store.get("f")["failover"])


class PriorityAndHistoryTests(unittest.TestCase):
    def manager(self, limit=1, **kw):
        from app.streams import StreamManager
        return StreamManager(lambda provider: limit, **kw)

    def test_low_priority_device_cannot_cut_high_priority(self):
        from app.streams import LimitReached
        m = self.manager()
        salon = m.acquire(kind="live", item_id="1", client="10.0.0.2", label="TF1", device="Salon", priority="high")
        with self.assertRaises(LimitReached) as ctx:
            m.acquire(kind="live", item_id="2", client="10.0.0.3", label="M6", device="Chambre", priority="low")
        self.assertIs(ctx.exception.holder, salon)
        self.assertFalse(salon.cancelled.is_set())
        # Même priorité ou plus haute : la plus ancienne lecture est coupée.
        other = m.acquire(kind="live", item_id="3", client="10.0.0.4", label="Arte", priority="high")
        self.assertTrue(salon.cancelled.is_set())
        m.release(other)

    def test_history_recorded_persisted_and_summarized(self):
        import tempfile
        from app.web import history_payload
        with tempfile.TemporaryDirectory() as tmp:
            path = f"{tmp}/history.json"
            m = self.manager(history_path=path)
            s = m.acquire(kind="live", item_id="1", client="c", label="TF1", device="Salon")
            s.started -= 120
            s.switches = 2
            m.release(s)
            again = self.manager(history_path=path).history()
            self.assertEqual((again[0]["label"], again[0]["device"], again[0]["switches"]), ("TF1", "Salon", 2))
            summary = history_payload(again)
            self.assertEqual(summary["top"][0]["label"], "TF1")
            self.assertEqual(summary["unstable"][0]["switches"], 2)
            m.clear_history()
            self.assertEqual(self.manager(history_path=path).history(), [])


class DevicePriorityTests(unittest.TestCase):
    def test_priority_ip_and_lookup(self):
        import tempfile
        from app.devices import DeviceStore, InvalidDevice
        with tempfile.TemporaryDirectory() as tmp:
            store = DeviceStore(tmp)
            salon = store.create("Salon", priority="high", ip="192.168.0.42")
            chambre = store.create("Chambre")
            self.assertEqual((salon["priority"], chambre["priority"], chambre["ip"]), ("high", "normal", ""))
            self.assertEqual(store.by_ip("192.168.0.42")["name"], "Salon")
            store.seen_from(chambre, "192.168.0.50")
            self.assertEqual(store.by_ip("192.168.0.50")["name"], "Chambre")
            self.assertIsNone(store.by_ip("192.168.0.99"))
            for bad in ({"priority": "max"}, {"ip": "salon"}):
                with self.subTest(bad=bad), self.assertRaises(InvalidDevice):
                    store.update(chambre["id"], bad)


class ReturnToBestTests(StackTestCase):
    def test_returns_to_best_version_after_a_while(self):
        stack = self.make_stack(failover_stall_seconds=1.0, failover_return_minutes=0.04,  # ≈ 2,4 s
                                failover_mode="splice")
        seen = b""
        with urlopen(stack.proxy_base + "/stream/60.ts?alt=50", timeout=15) as r:
            start = time.monotonic()
            while time.monotonic() - start < 12:
                chunk = r.read(1024)
                if not chunk:
                    break
                seen += chunk
                # 60 (A…) se tait → 50 (0x47…) → retour à 60 (A…) au bout de ~2,4 s
                if seen.count(b"A" * 1024) >= 8 and b"\x47" * 1024 in seen:
                    break
        first_a, first_g = seen.find(b"A" * 1024), seen.find(b"\x47" * 1024)
        self.assertTrue(0 <= first_a < first_g, seen[:20])
        self.assertGreaterEqual(seen.count(b"A" * 1024), 8, "retour à la meilleure version")
        self.assertGreater(seen.rfind(b"A" * 1024), first_g)


class ReconnectReturnTests(StackTestCase):
    def test_backup_closes_after_delay_then_best_is_retried(self):
        stack = self.make_stack(failover_stall_seconds=1.0, failover_return_minutes=0.04)  # ≈ 2,4 s
        memory = stack.proxy.RequestHandlerClass.failover_memory
        url = stack.proxy_base + "/stream/60.ts?alt=50"
        self.assertEqual(self.get(url)[1], b"A" * 4096)       # 60 se tait → fermé, 50 mémorisée
        self.assertEqual(memory["60"]["sid"], "50")
        start = time.monotonic()
        status, body, _ = self.get(url)                        # 50 servie, puis fermée au bout de ~2,4 s
        self.assertEqual(status, 200)
        self.assertTrue(body and set(body) == {0x47}, body[:10])
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual((memory["60"]["sid"], bool(memory["60"]["trial"])), ("60", True))  # essai de la meilleure
        self.assertTrue(self.get(url)[1].startswith(b"A"))    # reconnexion : la meilleure version est retentée
        # Elle flanche aussitôt : retour sur 50 avec une attente doublée.
        self.assertEqual(memory["60"]["sid"], "50")
        self.assertAlmostEqual(memory["60"]["wait"], 0.04 * 60 * 2, places=3)


class SlowDetectionTests(unittest.TestCase):
    def test_startup_burst_is_not_the_reference(self):
        from app import server
        self.assertGreaterEqual(server.SLOW_WARMUP, 20)
        self.assertGreaterEqual(server.SLOW_WINDOWS, 3)


class ProviderBusyTests(StackTestCase):
    """Le fournisseur refuse une connexion de plus (429/458/509) : message clair, pas de réessais en rafale."""

    def test_busy_message_and_pause(self):
        status, body, headers = self.get(self.stack.proxy_base + "/stream/70.ts")
        self.assertEqual(status, 503)
        self.assertIn("Abonnement occupé", body.decode())
        self.assertEqual(headers.get("Retry-After"), "12")  # Retry-After du fournisseur (10 à 120 s)
        self.assertEqual(FakeXtream.busy_requests, 1)
        # Réessais rapides du lecteur (et autres chaînes du même abonnement) : réponse immédiate, fournisseur épargné.
        for path in ("/stream/70.ts", "/stream/42.ts", "/hls/42.m3u8"):
            status, body, headers = self.get(self.stack.proxy_base + path)
            self.assertEqual(status, 503, path)
            self.assertIn("Réessaie dans", body.decode())
            self.assertTrue(1 <= int(headers["Retry-After"]) <= 12)
        self.assertEqual(FakeXtream.busy_requests, 1)
        self.assertEqual(self.stack.streams.count(), 0)

    def test_failover_does_not_try_other_versions_when_busy(self):
        status, body, _ = self.get(self.stack.proxy_base + "/stream/70.ts?alt=61,50")
        self.assertEqual(status, 503)
        self.assertIn("Abonnement occupé", body.decode())
        self.assertEqual(FakeXtream.busy_requests, 1)

    def test_hls_busy(self):
        status, body, _ = self.get(self.stack.proxy_base + "/hls/70.m3u8")
        self.assertEqual(status, 503)
        self.assertIn("Abonnement occupé", body.decode())


class QuotaFollowsSourceTests(unittest.TestCase):
    """La place est réservée chez le fournisseur de la version réellement lue (bascule multi-fournisseurs)."""

    def test_move_to_full_provider_is_refused_then_allowed(self):
        manager = StreamManager(lambda provider: 1)
        salon = manager.acquire(kind="live", item_id="1", client="10.0.0.2", provider="1", device="Salon")
        chambre = manager.acquire(kind="live", item_id="2", client="10.0.0.3", provider="2", device="Chambre")
        self.assertFalse(manager.move(salon, "2"))      # fournisseur 2 déjà occupé : version sautée
        self.assertEqual(salon.provider, "1")
        self.assertFalse(chambre.cancelled.is_set())     # personne n'est coupé pour une version de secours
        manager.release(chambre)
        self.assertTrue(manager.move(salon, "2"))
        self.assertEqual(salon.provider, "2")
        # La place chez le fournisseur 1 est libérée.
        other = manager.acquire(kind="live", item_id="3", client="10.0.0.4", provider="1", device="Bureau")
        self.assertFalse(salon.cancelled.is_set())
        manager.release(other)
        manager.release(salon)

    def test_same_address_different_devices_are_distinct(self):
        manager = StreamManager(lambda provider: 2)
        a = manager.acquire(kind="live", item_id="1", client="203.0.113.7", device="Maman")
        b = manager.acquire(kind="live", item_id="1", client="203.0.113.7", device="Sœur")
        self.assertFalse(a.cancelled.is_set())           # même adresse (proxy), même chaîne, deux appareils
        manager.release(a)
        manager.release(b)
