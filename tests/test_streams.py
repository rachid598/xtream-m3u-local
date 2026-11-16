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
        kept = dedupe(entries)
        self.assertEqual([(e[0]["stream_id"], e[0].get("alternates")) for e in kept],
                         [("2", ["4", "1", "3"]), ("5", None)])

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

    def test_relay_switches_to_next_version(self):
        stack = self.make_stack(failover_stall_seconds=1.0)
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
