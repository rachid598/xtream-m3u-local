"""Tests v3.35.0 : Mode Décodeur (clean_names), XMLTV multi-abonnements, et Alertes Telegram avec QR code."""
from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from app import alerts, provider_store, settings
from app.builder import clean_channel_name, select_items
from app.custom import PlaylistStore, validate_playlist
from app.devices import DeviceStore
from app.providers import ApiHolder, encode
from app.qrcode_svg import encode_matrix, qr_svg
from app.server import Config, make_api, make_epg, make_streams, redact
from app.web import make_web_server

from .fake_xtream import PASSWORD, USERNAME, FakeXtream

_ISO_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: ((r // 2) + (c // 3)) % 2 == 0,
    lambda r, c: ((r * c) % 2) + ((r * c) % 3) == 0,
    lambda r, c: (((r * c) % 2) + ((r * c) % 3)) % 2 == 0,
    lambda r, c: (((r + c) % 2) + ((r * c) % 3)) % 2 == 0,
)

# (blocks, data_codewords_per_block) pour Versions 1..6 en niveau ECC M (ISO/IEC 18004)
_ECC_M_BLOCKS = {1: (1, 16), 2: (1, 28), 3: (1, 44), 4: (2, 32), 5: (2, 43), 6: (4, 27)}


def _decode_qr_byte_mode(matrix: list[list[int]]) -> bytes:
    """Décode la charge utile (mode Byte, ECC M, multi-blocs) d'une matrice QR ISO/IEC 18004."""
    n = len(matrix)
    ver = (n - 17) // 4
    fmt_bits = [1 if matrix[8][c] else 0 for c in (0, 1, 2, 3, 4, 5, 7, 8)]
    fmt_bits += [1 if matrix[r][8] else 0 for r in (7, 5, 4, 3, 2, 1, 0)]
    fmt = 0
    for b in fmt_bits:
        fmt = (fmt << 1) | b
    fmt ^= 0x5412
    mask_fn = _ISO_MASKS[(fmt >> 10) & 0x7]

    func = [[False] * n for _ in range(n)]
    for fr, fc in ((0, 0), (0, n - 7), (n - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = fr + dr, fc + dc
                if 0 <= r < n and 0 <= c < n:
                    func[r][c] = True
    align_centers = {2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34)}.get(ver, ())
    for ar in align_centers:
        for ac in align_centers:
            if func[ar][ac]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    func[ar + dr][ac + dc] = True
    for i in range(n):
        func[6][i] = True
        func[i][6] = True
        if i <= 8:
            func[8][i] = True
            func[i][8] = True
        if i >= n - 8:
            func[8][i] = True
            func[i][8] = True

    raw_bits: list[int] = []
    col = n - 1
    up = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(n - 1, -1, -1) if up else range(n)
        for r in rows:
            for c in (col, col - 1):
                if not func[r][c]:
                    val = 1 if matrix[r][c] else 0
                    if mask_fn(r, c):
                        val ^= 1
                    raw_bits.append(val)
        col -= 2
        up = not up

    blocks, dpb = _ECC_M_BLOCKS[ver]
    total_data = blocks * dpb
    interleaved = [sum(raw_bits[i * 8 + b] << (7 - b) for b in range(8)) for i in range(total_data)]
    # Désentrelacer les blocs de données
    deinterleaved: list[int] = []
    for blk in range(blocks):
        for i in range(dpb):
            deinterleaved.append(interleaved[i * blocks + blk])
    data_bits = [(byte >> shift) & 1 for byte in deinterleaved for shift in range(7, -1, -1)]

    def read_int(pos: int, width: int) -> int:
        v = 0
        for b in data_bits[pos:pos + width]:
            v = (v << 1) | b
        return v

    mode = read_int(0, 4)
    assert mode == 0b0100, f"Expected byte mode 0b0100, got {bin(mode)}"
    length = read_int(4, 8)
    return bytes(read_int(12 + i * 8, 8) for i in range(length))


class QrCodeSvgTests(unittest.TestCase):
    def test_round_trip_decode_all_supported_versions(self):
        samples = [
            "https://t.me/mybot",                                                         # V2 (1 bloc)
            "https://t.me/xtream_alert_bot?start=a1b2c3d4e5f6a7b8c9d0",                   # V4 (2 blocs)
            "https://t.me/very_long_telegram_bot_username_example?start=0123456789abcdef0123456789abcdef",  # V6 (4 blocs)
        ]
        for text in samples:
            matrix = encode_matrix(text)
            decoded = _decode_qr_byte_mode(matrix).decode("utf-8")
            self.assertEqual(decoded, text)
            svg = qr_svg(text)
            self.assertIn("<svg ", svg)
            self.assertIn("</svg>", svg)
            self.assertNotIn("<script", svg.lower())
            root = ET.fromstring(svg)
            self.assertTrue(root.tag.endswith("svg"))

    def test_rejects_oversized_payload(self):
        with self.assertRaises(ValueError):
            encode_matrix("x" * 200)


class ModeDecodeurTests(unittest.TestCase):
    def test_clean_channel_name_preserves_french_accents_and_strips_noise(self):
        cases = {
            "AR: Al Aoula Inter ᴿᴬᵂ ◉": "Al Aoula Inter",
            "FR| Canal+ Cinéma ᴴᴰ ◉": "Canal+ Cinéma",
            "[FR-VIP] TF1 FHD 50FPS": "TF1",
            "AR: ARRYADIA TNT ᴴᴰ *LOCAL*": "ARRYADIA TNT",
            "|BEIN FR| beIN SPORTS 1 4K HEVC": "beIN SPORTS 1",
            "FR: L'Équipe 21 HD [BACKUP]": "L'Équipe 21",
            "VIP: RMC Découverte 1080p": "RMC Découverte",
        }
        for raw, expected in cases.items():
            self.assertEqual(clean_channel_name(raw), expected, f"raw={raw!r}")

    def test_select_items_clean_names_exact_group_and_manual_override(self):
        categories = {
            "1": "AR| MOROCCO ᴿᴬᵂ ▶ المغرب",
            "2": "FR| CINEMA",
        }
        channels = [
            {"stream_id": "10", "name": "AR: 2M Natioanl ◉", "category_id": "1", "category_ids": ["1"]},
            {"stream_id": "11", "name": "AR: ARRYADIA TNT ᴴᴰ *LOCAL*", "category_id": "1", "category_ids": ["1"]},
            {"stream_id": "12", "name": "FR| Canal+ Cinéma ᴴᴰ ◉", "category_id": "2", "category_ids": ["2"]},
            {"stream_id": "13", "name": "FR: OCS Géants FHD", "category_id": "2", "category_ids": ["2"]},
        ]

        class _Api:
            def catalog(self, refresh=False, kind="live"):
                return channels, categories

        saved = validate_playlist({
            "slug": "salon",
            "name": "Salon",
            "category_ids": ["1", "2"],
            "clean_names": True,
            "item_names": {"13": "Mon OCS Perso"},
            "custom_groups": [
                {
                    "id": "maroc",
                    "name": "Maroc",
                    "match": "exact",
                    "region": "ar",
                    "patterns": ["1: Al Aoula", "2: 2M", "3: Arryadia TNT"],
                },
            ],
        })
        items, _ = select_items(_Api(), saved, "live")
        by_id = {str(ch["stream_id"]): ch["name"] for ch, _ in items}
        # Groupe exact : libellé propre du motif
        self.assertEqual(by_id["10"], "2M")
        self.assertEqual(by_id["11"], "Arryadia TNT")
        # Hors groupe exact : nettoyage automatique en gardant les accents
        self.assertEqual(by_id["12"], "Canal+ Cinéma")
        # Renommage manuel (✎) toujours prioritaire
        self.assertEqual(by_id["13"], "Mon OCS Perso")

    def test_backwards_compat_clean_names_defaults_false(self):
        old_saved = validate_playlist({"slug": "ancienne", "name": "Ancienne", "category_ids": ["1"]})
        self.assertFalse(old_saved["clean_names"])
        categories = {"1": "FR"}
        channels = [{"stream_id": "1", "name": "FR: TF1 ᴴᴰ ◉", "category_id": "1", "category_ids": ["1"]}]

        class _Api:
            def catalog(self, refresh=False, kind="live"):
                return channels, categories

        items, _ = select_items(_Api(), old_saved, "live")
        self.assertEqual(items[0][0]["name"], "FR: TF1 ᴴᴰ ◉")


class FakeProvider2(FakeXtream):
    """Second fournisseur Xtream avec son propre catalogue et son propre xmltv.php."""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.endswith("/xmltv.php"):
            now = int(time.time())
            start = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(now - 600))
            stop = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(now + 3600))
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<tv>'
                '<channel id="p2.sport.id"><display-name>P2 Sport Exclusif</display-name></channel>'
                f'<programme start="{start}" stop="{stop}" channel="p2.sport.id">'
                '<title>Programme P2 Exclusif</title><desc>Direct sur abonnement 2</desc>'
                '</programme>'
                '</tv>'
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(xml)))
            self.end_headers()
            self.wfile.write(xml)
            return
        if parsed.path.endswith("/player_api.php"):
            qs = parse_qs(parsed.query)
            action = (qs.get("action") or [""])[0]
            if action == "get_live_categories":
                return self._json([{"category_id": "20", "category_name": "P2 SPORT"}])
            if action == "get_live_streams":
                return self._json([{
                    "num": 1, "name": "P2 Sport Exclusif HD", "stream_type": "live",
                    "stream_id": 901, "stream_icon": "", "epg_channel_id": "p2.sport.id",
                    "category_id": "20", "category_ids": [20],
                }])
            if action in ("get_vod_categories", "get_vod_streams", "get_series_categories", "get_series"):
                return self._json([])
        return super().do_GET()

    def _json(self, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class MultiProviderXmltvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.p1 = ThreadingHTTPServer(("127.0.0.1", 0), FakeXtream)
        self.p2 = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider2)
        for srv in (self.p1, self.p2):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

        self.settings_obj = settings.Settings(str(self.data))
        settings.install(self.settings_obj)
        self.pstore = provider_store.ProviderStore(str(self.data))
        provider_store.install(self.pstore)
        self.pstore.add(name="Abo 2", url=f"http://127.0.0.1:{self.p2.server_address[1]}",
                        username=USERNAME, password=PASSWORD, reserved={1})
        self.cfg = Config(
            xtream_url=f"http://127.0.0.1:{self.p1.server_address[1]}",
            username=USERNAME, password=PASSWORD,
            data_dir=str(self.data), epg_startup_quiet=0, port=0, web_port=0,
        )
        self.api = make_api(self.cfg, self.pstore, migrate=False)
        self.holder = ApiHolder(self.api)
        self.epg = make_epg(self.cfg, self.holder)
        self.playlists = PlaylistStore(str(self.data))
        self.devices = DeviceStore(str(self.data))
        self.streams = make_streams(self.cfg, self.holder)
        self.web = make_web_server(
            self.cfg, self.holder, self.playlists, host="127.0.0.1", port=0,
            epg=self.epg, streams=self.streams, devices=self.devices,
        )
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.web_url = f"http://127.0.0.1:{self.web.server_address[1]}"

    def tearDown(self):
        self.web.shutdown()
        self.web.server_close()
        for srv in (self.p1, self.p2):
            srv.shutdown()
            srv.server_close()
        provider_store.install(None)
        settings.install(None)
        self.tmp.cleanup()

    def test_secondary_provider_xmltv_used_in_playlist_guide_and_api(self):
        p2_sid = encode(2, "901")
        channels = [
            {"stream_id": "101", "name": "France Info", "epg_id": "franceinfo.fr"},
            {"stream_id": str(p2_sid), "name": "P2 Sport Exclusif HD", "epg_id": "p2.sport.id"},
        ]
        guide_path, stats = self.epg.playlist_guide("mix", channels)
        self.assertIsNotNone(guide_path)
        xml_text = Path(guide_path).read_text(encoding="utf-8")
        # Programmes du fournisseur 1 ET du fournisseur 2 présents dans le même XMLTV fusionné !
        self.assertIn("Programme P2 Exclusif", xml_text)
        self.assertIn('channel="p2.sport.id"', xml_text)
        self.assertIn("Journal", xml_text)
        self.assertEqual(stats["sources"][str(p2_sid)]["source"], "provider:2")

        # GET /api/epg/search trouve la chaîne du guide XMLTV de l'abonnement 2
        with urlopen(f"{self.web_url}/api/epg/search?q=P2+Sport", timeout=5) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        sources = {r["source"] for r in info["results"]}
        self.assertIn("provider:2", sources)


class FakeTelegramState:
    def __init__(self, token: str, bot_username: str = "xtream_test_bot"):
        self.token = token
        self.bot_username = bot_username
        self.updates: list[dict] = []
        self.sent_messages: list[dict] = []
        self.lock = threading.Lock()

    def push_start(self, code: str, chat_id: int = 555777, first_name: str = "Rachid"):
        with self.lock:
            uid = (self.updates[-1]["update_id"] + 1) if self.updates else 100
            self.updates.append({
                "update_id": uid,
                "message": {
                    "text": f"/start {code}",
                    "chat": {"id": chat_id, "first_name": first_name, "username": "rachid_test"},
                    "from": {"id": chat_id, "first_name": first_name},
                },
            })


def make_fake_telegram(tg_state: FakeTelegramState) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send_json(self, code: int, obj: dict):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _handle(self, body: dict | None = None):
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 2 or not parts[0].startswith("bot"):
                return self._send_json(404, {"ok": False, "description": "Not Found"})
            tok = parts[0][3:]
            method = parts[1]
            if tok != tg_state.token:
                return self._send_json(401, {"ok": False, "description": "Unauthorized"})
            if method == "getMe":
                return self._send_json(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": tg_state.bot_username}})
            if method == "getUpdates":
                qs = parse_qs(parsed.query)
                offset = int((qs.get("offset") or ["0"])[0])
                with tg_state.lock:
                    if offset < 0:
                        res = tg_state.updates[offset:]
                    else:
                        res = [u for u in tg_state.updates if u["update_id"] >= offset]
                return self._send_json(200, {"ok": True, "result": res})
            if method == "sendMessage":
                with tg_state.lock:
                    tg_state.sent_messages.append(body or {})
                return self._send_json(200, {"ok": True, "result": {"message_id": len(tg_state.sent_messages)}})
            return self._send_json(404, {"ok": False, "description": f"Unknown method {method}"})

        def do_GET(self):
            self._handle(None)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            self._handle(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class TelegramAlertsTests(unittest.TestCase):
    # Jeton purement factice assemblé à l'exécution (évite les faux positifs GitHub Secret Scanning).
    TOKEN = ":".join(("123456789", "AA" + "H_fake_unit_test_placeholder_012345"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.tg_state = FakeTelegramState(self.TOKEN)
        self.tg_srv = make_fake_telegram(self.tg_state)
        self.tg_base = f"http://127.0.0.1:{self.tg_srv.server_address[1]}"

        self.xtream = ThreadingHTTPServer(("127.0.0.1", 0), FakeXtream)
        threading.Thread(target=self.xtream.serve_forever, daemon=True).start()

        self.settings_obj = settings.Settings(str(self.data))
        settings.install(self.settings_obj)
        self.mgr = alerts.AlertManager(self.data, api_base=self.tg_base)
        alerts.install(self.mgr)

        self.cfg = Config(
            xtream_url=f"http://127.0.0.1:{self.xtream.server_address[1]}",
            username=USERNAME, password=PASSWORD,
            data_dir=str(self.data), epg_startup_quiet=0, port=0, web_port=0,
        )
        self.api = make_api(self.cfg)
        self.holder = ApiHolder(self.api)
        self.epg = make_epg(self.cfg, self.holder)
        self.playlists = PlaylistStore(str(self.data))
        self.devices = DeviceStore(str(self.data))
        self.streams = make_streams(self.cfg, self.holder)
        self.web = make_web_server(
            self.cfg, self.holder, self.playlists, host="127.0.0.1", port=0,
            epg=self.epg, streams=self.streams, devices=self.devices,
        )
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.web_url = f"http://127.0.0.1:{self.web.server_address[1]}"

    def tearDown(self):
        self.mgr.stop()
        alerts.install(None)
        settings.install(None)
        self.web.shutdown()
        self.web.server_close()
        self.xtream.shutdown()
        self.xtream.server_close()
        self.tg_srv.shutdown()
        self.tg_srv.server_close()
        self.tmp.cleanup()

    def _api(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"X-Requested-With": "XtreamM3ULocal"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = Request(f"{self.web_url}{path}", data=data, headers=headers, method=method)
        with urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_qr_pairing_flow_permissions_redaction_and_options(self):
        # 1. Initial : non connecté
        st = self._api("/api/alerts")
        self.assertFalse(st["connected"])

        # 2. Démarrer le couplage QR code
        pair_resp = self._api("/api/alerts/pair", "POST", {"bot_token": self.TOKEN})
        pair = pair_resp["pairing"]
        self.assertEqual(pair["status"], "waiting")
        self.assertEqual(pair["bot_username"], "xtream_test_bot")
        self.assertIn("<svg ", pair["qr_svg"])
        self.assertTrue(pair["deep_link"].startswith("https://t.me/xtream_test_bot?start="))

        # Le token en cours de couplage doit être masqué par redact()
        self.assertNotIn(self.TOKEN, redact(f"url https://api.telegram.org/bot{self.TOKEN}/getMe", self.cfg))

        # 3. Simuler le scan du QR code + clic « Démarrer » dans Telegram
        code = pair["deep_link"].split("?start=")[1]
        self.tg_state.push_start(code, chat_id=998877, first_name="Rachid")

        deadline = time.time() + 6.0
        while time.time() < deadline:
            st = self._api("/api/alerts")
            if st["connected"]:
                break
            time.sleep(0.15)

        self.assertTrue(st["connected"])
        self.assertTrue(st["enabled"])
        self.assertIn("Rachid", st["chat_label"])
        self.assertNotIn(self.TOKEN, json.dumps(st))

        # 4. Fichier alerts.json protégé en 0600
        alerts_file = self.data / "alerts.json"
        self.assertTrue(alerts_file.exists())
        mode = stat.S_IMODE(os.stat(alerts_file).st_mode)
        self.assertEqual(mode, 0o600)

        # Message de bienvenue envoyé
        self.assertTrue(any("connecté" in m.get("text", "") for m in self.tg_state.sent_messages))

        # 5. Envoi d'un test
        before = len(self.tg_state.sent_messages)
        self._api("/api/alerts/test", "POST")
        self.assertEqual(len(self.tg_state.sent_messages), before + 1)

        # 6. Désactiver l'événement « provider » et vérifier que notify("provider") est ignoré
        st = self._api("/api/alerts", "PUT", {"enabled": True, "events": {"provider": False}})
        self.assertFalse(st["events"]["provider"])
        before = len(self.tg_state.sent_messages)
        self.assertFalse(self.mgr.notify("provider", "p1", "Test 429"))
        self.assertEqual(len(self.tg_state.sent_messages), before)

        # 7. Vérifier check_expiries (J-2, puis pas de doublon au 2ᵉ passage)
        exp_ts = int(time.time()) + 3600 * 30

        class FakeExpiryApi:
            def accounts(self):
                return [{
                    "index": 1, "name": "Mon Abo", "error": None,
                    "account": {"exp_date": exp_ts},
                }]

        self.assertEqual(self.mgr.check_expiries(FakeExpiryApi()), 1)
        deadline = time.time() + 2.0
        while time.time() < deadline and len(self.tg_state.sent_messages) < before + 1:
            time.sleep(0.05)
        self.assertEqual(len(self.tg_state.sent_messages), before + 1)
        self.assertIn("Mon Abo", self.tg_state.sent_messages[-1]["text"])
        # 2ᵉ appel immédiat : aucun doublon
        self.assertEqual(self.mgr.check_expiries(FakeExpiryApi()), 0)
        time.sleep(0.1)
        self.assertEqual(len(self.tg_state.sent_messages), before + 1)

        # 8. Déconnexion complète
        st = self._api("/api/alerts/disconnect", "POST")
        self.assertFalse(st["connected"])
        self.assertEqual(self.mgr.secrets(), [])


if __name__ == "__main__":
    unittest.main()
