"""Port 7778 : interface Web, API REST, sécurité, scénario d'acceptation."""
import base64
import http.client
import json
import unittest
from pathlib import Path

from .fake_xtream import FakeXtream
from .helpers import HEADERS, StackTestCase


class WebTests(StackTestCase):
    def test_index_and_static_assets(self):
        status, body, headers = self.get(self.stack.web_base + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Xtream M3U Local", body)
        self.assertIn(b'lang="fr"', body)
        csp = headers["Content-Security-Policy"]
        self.assertNotIn("unsafe-inline", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        for path, ctype in (("/static/app.js", "text/javascript"), ("/static/app.css", "text/css"),
                            ("/static/favicon.svg", "image/svg+xml")):
            status, body, headers = self.get(self.stack.web_base + path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(ctype))
            self.assertGreater(len(body), 100)
        # Pas d'accès arbitraire aux fichiers.
        for path in ("/static/../server.py", "/static/index.html/..", "/app/server.py", "/static/"):
            self.assertEqual(self.get(self.stack.web_base + path)[0], 404, path)

    def test_no_inline_script_or_handlers_in_html(self):
        html = Path(__file__).resolve().parents[1].joinpath("app/static/index.html").read_text(encoding="utf-8")
        self.assertNotIn("<script>", html)
        self.assertNotRegex(html, r"\son[a-z]+=")
        self.assertNotIn("style=", html)

    def test_catalog_lists_all_categories_with_counts(self):
        status, body, _ = self.get(self.stack.web_base + "/api/catalog")
        self.assertEqual(status, 200)
        catalog = json.loads(body)
        cats = [(c["category_id"], c["name"], c["count"]) for c in catalog["categories"]]
        self.assertEqual(cats, [
            ("1", "Actualités", 2),          # France Info + Sport 1 (multi-catégories)
            ("2", 'Musique "HD"', 1),
            ("10", "Sport", 1),
            ("11", "Catégorie vide", 0),     # catégorie vide quand même listée
            ("12", "Sport | Foot", 1),
            ("none", "Sans catégorie", 1),
            ("99", "Catégorie 99", 1),
        ])
        self.assertEqual(len(catalog["channels"]), 6)
        meta = {c["category_id"]: (c["parent_id"], c["position"], c["source"]) for c in catalog["categories"]}
        self.assertEqual(meta["12"], ("10", 4, "provider"))   # sous-groupe conservé
        self.assertEqual(meta["1"], ("", 0, "provider"))       # parent_id 0 = racine
        self.assertEqual(meta["99"], ("", 6, "derived"))       # absente de get_live_categories
        self.assertIsNone(catalog["categories_error"])
        first = catalog["channels"][0]
        self.assertEqual(first, {"stream_id": "42", "name": "France Info HD", "category_id": "1",
                                 "category_ids": ["1"], "logo": "https://logo.invalid/franceinfo.png",
                                 "epg": True, "num": "1"})
        raw = body.decode()
        self.assertNotIn('"pw"', raw)
        self.assertNotIn("password", raw)
        self.assertNotIn("javascript:", raw)
        self.assertIsInstance(catalog["updated_at"], int)

    def test_categories_failure_is_reported(self):
        FakeXtream.fail_categories = True
        catalog = json.loads(self.get(self.stack.web_base + "/api/catalog")[1])
        self.assertIn("get_live_categories", catalog["categories_error"])
        self.assertTrue(all(c["source"] == "derived" for c in catalog["categories"]))
        status = json.loads(self.get(self.stack.web_base + "/api/status")[1])
        self.assertIn("get_live_categories", status["categories_error"])

    def test_export_provider_catalog_without_credentials(self):
        status, body, headers = self.get(self.stack.web_base + "/api/catalog/export?refresh=1")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("catalogue-xtream-", headers["Content-Disposition"])
        doc = json.loads(body)
        self.assertEqual([c["category_id"] for c in doc["get_live_categories"]], ["1", "2", "10", "11", "12"])
        self.assertEqual(doc["get_live_categories"][4]["parent_id"], 10)   # valeurs brutes conservées
        self.assertEqual(len(doc["get_live_streams"]), 8)                  # y compris les entrées invalides
        self.assertEqual(doc["get_live_streams"][0]["direct_source"],
                         "http://cdn.invalid/live/***/***/42.ts?username=***&password=***")
        self.assertEqual(doc["counts"]["valid_streams"], 6)
        self.assertNotIn("/user/pw/", body.decode())

    def test_custom_group_order_and_names(self):
        self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "2", "12"],
                     "group_order": ["12", "2"], "group_names": {"12": "Football", "1": "Infos"}})
        data = self.get(self.stack.proxy_base + "/p/salon.m3u")[1].decode()
        groups = []
        for g in __import__("re").findall(r'group-title="([^"]*)"', data):
            if g not in groups:
                groups.append(g)
        self.assertEqual(groups, ["Football", "Musique 'HD'", "Infos"])
        # Le catalogue d'origine n'est pas modifié.
        cats = {c["category_id"]: c["name"] for c in json.loads(self.get(self.stack.web_base + "/api/catalog")[1])["categories"]}
        self.assertEqual((cats["12"], cats["1"]), ("Sport | Foot", "Actualités"))

    def test_catalog_provider_error(self):
        FakeXtream.fail_api = True
        status, body, _ = self.get(self.stack.web_base + "/api/catalog")
        self.assertEqual(status, 502)
        self.assertIn("error", json.loads(body))

    def test_status_without_secrets(self):
        status, body, _ = self.get(self.stack.web_base + "/api/status?account=1")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["playlist_port"], self.stack.config.port)
        self.assertTrue(data["epg_enabled"])
        self.assertFalse(data["token_required"])
        self.assertEqual(data["account"], {"status": "Active", "exp_date": 1893456000, "max_connections": "1",
                                           "active_connections": "0", "is_trial": False})
        # Le chemin du dossier temporaire (nom aléatoire) peut contenir « pw » : on l'exclut du contrôle.
        self.assertTrue(data.pop("data_file").endswith("playlists.json"))
        raw = json.dumps(data)
        self.assertNotIn('"pw"', raw)
        self.assertNotIn("/pw", raw)
        self.assertNotIn("password", raw)
        self.assertNotIn('"user"', raw)
        self.assertNotIn(self.provider_url, raw)

    def test_access_token_exposed_only_behind_web_password(self):
        stack = self.make_stack(access_token="tok123")
        data = json.loads(self.get(stack.web_base + "/api/status")[1])
        self.assertTrue(data["token_required"])
        self.assertNotIn("access_token", data)
        stack = self.make_stack(access_token="tok123", web_password="secret")
        auth = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
        data = json.loads(self.get(stack.web_base + "/api/status", auth)[1])
        self.assertEqual(data["access_token"], "tok123")

    def test_crud_and_stable_url(self):
        """Critère d'acceptation : même URL, contenu modifié après édition, suppression → 404."""
        status, saved = self.create({"slug": "salon", "name": "Salon", "category_ids": ["1", "10"]})
        self.assertEqual(status, 201)
        self.assertEqual(saved["slug"], "salon")
        url = self.stack.proxy_base + "/p/salon.m3u"
        status, body, headers = self.get(url)
        self.assertEqual(status, 200)
        self.assertIn('filename="salon.m3u"', headers["Content-Disposition"])
        data = body.decode()
        self.assertIn(f'x-tvg-url="{self.stack.proxy_base}/p/salon.xml"', data)
        self.assertIn("/stream/42.ts", data)
        self.assertIn("/stream/44.ts", data)
        self.assertNotIn("/stream/43.ts", data)
        self.assertIn('group-title="Sport",Sport 1', data)
        self.assertIn('group-title="Actualités",France Info HD', data)
        self.assertNotIn("pw", data)

        status, _ = self.send(self.stack.web_base + "/api/playlists/salon", "PUT",
                              {"name": "Salon (musique)", "category_ids": ["2"], "excluded_stream_ids": []})
        self.assertEqual(status, 200)
        data = self.get(url)[1].decode()
        self.assertIn("/stream/43.ts", data)
        self.assertNotIn("/stream/42.ts", data)

        status, listing = self.send(self.stack.web_base + "/api/playlists", "GET", headers={})
        self.assertEqual([p["name"] for p in listing], ["Salon (musique)"])

        status, _ = self.send(self.stack.web_base + "/api/playlists/salon", "DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(self.get(url)[0], 404)
        self.assertEqual(self.send(self.stack.web_base + "/api/playlists/salon", "DELETE")[0], 404)

    def test_acceptance_categories_a_and_c_with_exceptions(self):
        self.create({"slug": "tv-francaise", "name": "TV française", "category_ids": ["1", "10"],
                     "included_stream_ids": ["43"], "excluded_stream_ids": ["42"]})
        data = self.get(self.stack.proxy_base + "/p/tv-francaise.m3u")[1].decode()
        ids = sorted(line.rsplit("/", 1)[1] for line in data.splitlines() if "/stream/" in line)
        self.assertEqual(ids, ["43.ts", "44.ts"])

    def test_multiple_playlists_and_persistence_after_restart(self):
        for slug, name, cats in (("salon", "Salon", ["1"]), ("chambre", "Chambre", ["2"]),
                                 ("tv-francaise", "TV française", ["1", "10"])):
            self.assertEqual(self.create({"slug": slug, "name": name, "category_ids": cats})[0], 201)
        # « Redémarrage » : nouvelle pile sur le même dossier de données.
        restarted = self.make_stack()
        status, listing = self.send(restarted.web_base + "/api/playlists", "GET", headers={})
        self.assertEqual(sorted(p["slug"] for p in listing), ["chambre", "salon", "tv-francaise"])
        self.assertIn(b"/stream/43.ts", self.get(restarted.proxy_base + "/p/chambre.m3u")[1])

    def test_validation_errors(self):
        self.create({"slug": "salon", "name": "Salon"})
        cases = [
            ({"slug": "salon", "name": "Doublon"}, 400),
            ({"slug": "../../etc", "name": "X"}, 400),
            ({"slug": "ok", "name": ""}, 400),
            ({"slug": "ok", "name": "X", "category_ids": ["<script>"]}, 400),
        ]
        for payload, code in cases:
            with self.subTest(payload=payload):
                status, body = self.create(payload)
                self.assertEqual(status, code)
                self.assertIn("error", body)
        self.assertEqual(self.send(self.stack.web_base + "/api/playlists/inconnue", "PUT", {"name": "X"})[0], 404)
        self.assertEqual(self.send(self.stack.web_base + "/api/playlists/Bad", "PUT", {"name": "X"})[0], 404)
        self.assertEqual(self.send(self.stack.web_base + "/api/other", "POST", {"name": "X"})[0], 404)

    def test_csrf_and_request_guards(self):
        url = self.stack.web_base + "/api/playlists"
        body = {"slug": "x", "name": "X"}
        self.assertEqual(self.send(url, "POST", body, {"Content-Type": "application/json"})[0], 403)
        self.assertEqual(self.send(url, "POST", body, {**HEADERS, "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.send(url, "POST", body, {**HEADERS, "Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.send(url, "POST", b"{not json", HEADERS)[0], 400)
        self.assertEqual(self.send(url, "POST", b"[1,2]", HEADERS)[0], 400)
        self.assertEqual(self.oversized_post(), 413)
        same_origin = {**HEADERS, "Origin": self.stack.web_base}
        self.assertEqual(self.send(url, "POST", body, same_origin)[0], 201)
        self.assertEqual(self.send(url + "/x", "DELETE", headers={})[0], 403)

    def oversized_post(self) -> int:
        """Annonce un corps de 5 Mo sans l'envoyer : le serveur doit refuser avant lecture."""
        conn = http.client.HTTPConnection("127.0.0.1", self.stack.web.server_address[1], timeout=10)
        try:
            conn.putrequest("POST", "/api/playlists")
            for key, value in HEADERS.items():
                conn.putheader(key, value)
            conn.putheader("Content-Length", str(5 * 1024 * 1024))
            conn.endheaders()
            return conn.getresponse().status
        finally:
            conn.close()

    def test_basic_auth(self):
        stack = self.make_stack(web_password="secret")
        status, _, headers = self.get(stack.web_base + "/")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])
        self.assertEqual(self.get(stack.web_base + "/api/catalog")[0], 401)
        bad = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
        self.assertEqual(self.get(stack.web_base + "/", bad)[0], 401)
        self.assertEqual(self.get(stack.web_base + "/", {"Authorization": "Basic !!!"})[0], 401)
        good = {"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()}
        self.assertEqual(self.get(stack.web_base + "/", good)[0], 200)
        self.assertEqual(self.send(stack.web_base + "/api/playlists", "POST", {"slug": "a", "name": "A"},
                                   {**HEADERS})[0], 401)
        self.assertEqual(self.send(stack.web_base + "/api/playlists", "POST", {"slug": "a", "name": "A"},
                                   {**HEADERS, **good})[0], 201)
        # Le port 7777 n'est pas concerné par WEB_PASSWORD (lecteurs IPTV).
        self.assertEqual(self.get(stack.proxy_base + "/p/a.m3u")[0], 200)

    def test_corrupted_store_reported(self):
        Path(self.tmp.name, "playlists.json").write_text("{oops", encoding="utf-8")
        status, body, _ = self.get(self.stack.web_base + "/api/playlists")
        self.assertEqual(status, 500)
        self.assertEqual(self.get(self.stack.proxy_base + "/p/salon.m3u")[0], 500)


if __name__ == "__main__":
    unittest.main()
