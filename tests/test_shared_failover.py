"""Partage d'un direct entre écrans (LiveHub) combiné aux autres options : bascule automatique,
retour à la meilleure version, chaîne dans plusieurs groupes (copie Xtream)."""
from __future__ import annotations

import json
import threading
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from .fake_xtream import FakeXtream
from .helpers import StackTestCase


def read_until(response, predicate, timeout: float = 10.0) -> bytes:
    """Lit un flux jusqu'à ce que ``predicate(données)`` soit vrai, la fin du flux ou le délai."""
    seen = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            chunk = response.read1(4096) if hasattr(response, "read1") else response.read(4096)
        except OSError:
            break
        if not chunk:
            break
        seen += chunk
        if predicate(seen):
            break
    return seen


def wait_for(condition, timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class SharedFailoverTests(StackTestCase):
    """Deux écrans partagent la chaîne 60, qui se tait : la bascule doit servir les deux écrans."""
    overrides = {"max_connections": 1, "share_streams": True, "failover_stall_seconds": 1.0}

    def devices(self):
        _, salon = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV Salon"})
        _, chambre = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV Chambre"})
        return salon, chambre

    def url(self, device, sid="60", alt="50"):
        return f"{self.stack.proxy_base}/stream/{sid}.ts?alt={alt}&token={device['password']}"

    def test_reconnect_mode_both_screens_end_up_on_backup_with_one_connection(self):
        salon, chambre = self.devices()
        r1 = urlopen(self.url(salon), timeout=10)
        self.assertEqual(read_until(r1, lambda s: len(s) >= 4096), b"A" * 4096)
        r2 = urlopen(self.url(chambre), timeout=10)        # rejoint le flux 60 déjà ouvert
        # 60 se tait : en mode « reconnexion », les deux connexions sont fermées (les lecteurs se reconnectent).
        self.assertNotIn(b"\x47", read_until(r1, lambda s: False, timeout=6))
        self.assertNotIn(b"\x47", read_until(r2, lambda s: False, timeout=6))
        r1.close(), r2.close()
        FakeXtream.open_streams = 0
        # Reconnexion des deux écrans : version de secours 50, une seule connexion fournisseur.
        r1 = urlopen(self.url(salon), timeout=10)
        r2 = urlopen(self.url(chambre), timeout=10)
        try:
            self.assertIn(b"\x47" * 512, read_until(r1, lambda s: b"\x47" * 512 in s))
            self.assertIn(b"\x47" * 188, read_until(r2, lambda s: b"\x47" * 188 in s))
            self.assertEqual(FakeXtream.open_streams, 1)
            streams = json.loads(self.get(self.stack.web_base + "/api/streams")[1])["streams"]
            self.assertEqual([s["viewers"] for s in streams], [2])
        finally:
            r1.close(), r2.close()
        self.assertTrue(wait_for(lambda: FakeXtream.open_streams == 0))
        self.assertTrue(wait_for(lambda: self.stack.streams.count() == 0))

    def test_splice_mode_both_screens_continue_on_backup(self):
        stack = self.make_stack(max_connections=1, share_streams=True, failover_stall_seconds=1.0,
                                failover_mode="splice")
        self.stack = stack
        salon, chambre = self.devices()
        r1 = urlopen(self.url(salon), timeout=10)
        r2 = None
        try:
            self.assertEqual(read_until(r1, lambda s: len(s) >= 4096), b"A" * 4096)
            r2 = urlopen(self.url(chambre), timeout=10)
            got1, got2 = {}, {}
            t = threading.Thread(target=lambda: got2.update(data=read_until(r2, lambda s: b"\x47" * 188 in s)))
            t.start()
            got1["data"] = read_until(r1, lambda s: b"\x47" * 512 in s)
            t.join(12)
            # L'écran qui a ouvert la chaîne continue sur le secours…
            self.assertIn(b"\x47" * 512, got1["data"])
            # … et celui qui l'avait rejointe aussi, sans avoir à se reconnecter.
            self.assertIn(b"\x47" * 188, got2.get("data", b""))
            self.assertEqual(FakeXtream.open_streams, 1)
        finally:
            r1.close()
            if r2 is not None:
                r2.close()
        self.assertTrue(wait_for(lambda: FakeXtream.open_streams == 0))
        self.assertTrue(wait_for(lambda: stack.streams.count() == 0))

    def test_return_to_best_keeps_opener_playing_when_backup_is_shared(self):
        stack = self.make_stack(max_connections=2, share_streams=True, failover_stall_seconds=1.0,
                                failover_mode="splice", failover_return_minutes=0.04)  # ≈ 2,4 s
        self.stack = stack
        salon, chambre = self.devices()
        r1 = urlopen(self.url(salon, alt="51"), timeout=15)
        r2 = None
        try:
            self.assertIn(b"\x47" * 512, read_until(r1, lambda s: b"\x47" * 512 in s))   # 60 muette → 51
            # La chambre ouvre directement la chaîne 51 : elle partage la connexion du secours.
            r2 = urlopen(f"{stack.proxy_base}/stream/51.ts?token={chambre['password']}", timeout=15)
            self.assertIn(b"\x47" * 188, read_until(r2, lambda s: b"\x47" * 188 in s))
            # ≈ 2,4 s plus tard, le salon retente la meilleure version (60 : « A… ») sans être coupé.
            after = read_until(r1, lambda s: b"A" * 1024 in s, timeout=8)
            self.assertIn(b"A" * 1024, after)
            # La chambre continue sur 51.
            self.assertIn(b"\x47" * 1024, read_until(r2, lambda s: b"\x47" * 1024 in s, timeout=4))
        finally:
            r1.close()
            if r2 is not None:
                r2.close()
        self.assertTrue(wait_for(lambda: stack.streams.count() == 0, timeout=6))


class SharedCopyTests(StackTestCase):
    """Chaîne dans plusieurs groupes (copie à numéro propre) + partage : une seule connexion."""
    overrides = {"max_connections": 1, "share_streams": True}

    def test_original_and_copy_share_one_connection(self):
        groups = [{"id": "ott", "name": "OTT", "match": "exact", "patterns": ["Sport 1"]},
                  {"id": "sport", "name": "Sport FR", "match": "exact", "patterns": ["Sport 1"]}]
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2"], "custom_groups": groups,
                     "multi_groups": True})
        tv1 = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV 1", "playlist": "salon"})[1]
        tv2 = self.send(self.stack.web_base + "/api/devices", "POST", {"name": "TV 2", "playlist": "salon"})[1]
        q = {"username": tv1["username"], "password": tv1["password"], "action": "get_live_streams"}
        live = json.loads(self.get(self.stack.proxy_base + "/player_api.php?" + urlencode(q))[1])
        ids = sorted(c["stream_id"] for c in live if c["name"] == "Sport 1")
        self.assertEqual(len(ids), 2)
        original, copy = ids
        # Dans le faux fournisseur, 44 répond « occupé » : on la fait pointer vers le flux continu 50.
        api = self.stack.proxy.RequestHandlerClass.api
        real = api.direct_stream
        api.direct_stream = lambda sid: real("50" if str(sid) == "44" else sid)
        try:
            r1 = urlopen(f"{self.stack.proxy_base}/live/{tv1['username']}/{tv1['password']}/{original}.ts", timeout=10)
            r2 = urlopen(f"{self.stack.proxy_base}/live/{tv2['username']}/{tv2['password']}/{copy}.ts", timeout=10)
            try:
                self.assertIn(b"\x47" * 512, read_until(r1, lambda s: b"\x47" * 512 in s))
                self.assertIn(b"\x47" * 188, read_until(r2, lambda s: b"\x47" * 188 in s))
                self.assertEqual(FakeXtream.open_streams, 1)
            finally:
                r1.close(), r2.close()
        finally:
            api.direct_stream = real
        self.assertTrue(wait_for(lambda: FakeXtream.open_streams == 0))
