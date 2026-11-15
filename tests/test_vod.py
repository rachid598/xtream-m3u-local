"""Films (VOD) et séries : catalogues, playlists, relais, cache des épisodes."""
import json
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from app.vod import episode_name, normalize_episodes, normalize_movie, normalize_series

from .fake_xtream import FakeXtream
from .helpers import StackTestCase


def stream_lines(m3u: str) -> list[str]:
    return [line for line in m3u.splitlines() if line.startswith("http")]


class NormalizationTests(unittest.TestCase):
    def test_movie(self):
        movie = normalize_movie({"stream_id": 5, "name": " Film ", "container_extension": "MKV",
                                 "stream_icon": "javascript:x", "rating": "7,25", "releaseDate": "2018-01-01",
                                 "category_id": "3"})
        self.assertEqual((movie["ext"], movie["logo"], movie["rating"], movie["year"], movie["name"]),
                         ("mkv", "", "7.2", "2018", "Film"))
        self.assertEqual(normalize_movie({"stream_id": 6, "container_extension": "../x"})["ext"], "mp4")
        self.assertIsNone(normalize_movie({"stream_id": "x"}))

    def test_series(self):
        show = normalize_series({"series_id": "9", "name": "S", "cover": "http://c/x.jpg", "category_id": None})
        self.assertEqual((show["stream_id"], show["logo"], show["category_ids"]), ("9", "http://c/x.jpg", ["none"]))
        self.assertIsNone(normalize_series({"stream_id": 9}))

    def test_episodes_dict_and_list_forms(self):
        eps = normalize_episodes({"episodes": {"2": [{"id": "3", "episode_num": 1}],
                                               "1": [{"id": "2", "episode_num": 2}, {"id": "1", "episode_num": "1"},
                                                     {"id": "bad"}]}})
        self.assertEqual([(e["id"], e["season"], e["episode"]) for e in eps], [("1", 1, 1), ("2", 1, 2), ("3", 2, 1)])
        eps = normalize_episodes({"episodes": [[{"id": 7, "episode_num": 1, "season": 3}]]})
        self.assertEqual((eps[0]["id"], eps[0]["season"]), ("7", 3))
        self.assertEqual(normalize_episodes({"episodes": None}), [])
        self.assertEqual(normalize_episodes([]), [])

    def test_episode_name(self):
        ep = {"season": 1, "episode": 2, "title": "Le retour"}
        self.assertEqual(episode_name("Ma Série", ep), "Ma Série S01 E02 - Le retour")
        self.assertEqual(episode_name("Ma Série", {**ep, "title": "Ma Série - S01E02"}), "Ma Série S01 E02")


class VodTests(StackTestCase):
    def test_vod_and_series_catalogs(self):
        vod = json.loads(self.get(self.stack.web_base + "/api/catalog?kind=vod")[1])
        self.assertEqual(vod["kind"], "vod")
        self.assertEqual([(c["category_id"], c["name"], c["count"]) for c in vod["categories"]],
                         [("1", "Films | Action", 2), ("2", "Films | Comédie", 1), ("3", "Films | Vide", 0)])
        first = vod["channels"][0]
        self.assertEqual((first["stream_id"], first["ext"], first["year"], first["rating"]), ("501", "mkv", "2021", "7.4"))
        series = json.loads(self.get(self.stack.web_base + "/api/catalog?kind=series")[1])
        self.assertEqual([s["name"] for s in series["channels"]], ["Ma Série", "Dessin animé", "Série en panne"])
        self.assertEqual(self.get(self.stack.web_base + "/api/catalog?kind=radio")[0], 400)
        # Le catalogue du direct n'est pas affecté.
        live = json.loads(self.get(self.stack.web_base + "/api/catalog")[1])
        self.assertEqual(live["kind"], "live")
        self.assertEqual(live["categories"][0]["name"], "Actualités")

    def test_catalog_summary(self):
        data = json.loads(self.get(self.stack.web_base + "/api/catalog/summary")[1])
        self.assertEqual(data["live"], {"items": 6, "categories": 7, "error": None})
        self.assertEqual(data["vod"], {"items": 3, "categories": 3, "error": None})
        self.assertEqual(data["series"], {"items": 3, "categories": 2, "error": None})
        FakeXtream.fail_api = True
        stack = self.make_stack()
        data = json.loads(self.get(stack.web_base + "/api/catalog/summary")[1])
        self.assertIsNone(data["vod"]["items"])
        self.assertIn("indisponible", data["vod"]["error"])

    def test_series_episodes_api(self):
        data = json.loads(self.get(self.stack.web_base + "/api/series/900")[1])
        self.assertEqual([e["id"] for e in data["episodes"]], ["9000", "9001", "9002"])
        self.assertEqual(self.get(self.stack.web_base + "/api/series/902")[0], 502)
        self.assertEqual(self.get(self.stack.web_base + "/api/series/abc")[0], 404)

    def test_export_vod_and_series(self):
        doc = json.loads(self.get(self.stack.web_base + "/api/catalog/export?kind=vod")[1])
        self.assertEqual(doc["kind"], "vod")
        self.assertEqual(len(doc["get_vod_streams"]), 4)
        self.assertEqual(len(doc["get_vod_categories"]), 3)
        status, body, headers = self.get(self.stack.web_base + "/api/catalog/export?kind=series")
        self.assertIn("catalogue-xtream-series-", headers["Content-Disposition"])
        self.assertEqual(len(json.loads(body)["get_series"]), 3)

    def test_mixed_playlist_live_movies_series(self):
        status, _ = self.create({"slug": "salon", "name": "Salon", "category_ids": ["1"],
                                 "vod_category_ids": ["1"], "excluded_vod_ids": ["503"], "included_vod_ids": ["502"],
                                 "series_category_ids": ["1"], "included_series_ids": ["901"]})
        self.assertEqual(status, 201)
        status, body, headers = self.get(self.stack.proxy_base + "/p/salon.m3u")
        self.assertEqual(status, 200)
        self.assertNotIn("X-Series-Pending", headers)
        data = body.decode()
        base = self.stack.proxy_base
        self.assertEqual(stream_lines(data), [
            f"{base}/stream/42.ts", f"{base}/stream/44.ts",            # direct, catégorie 1
            f"{base}/movie/501.mkv", f"{base}/movie/502.mp4",          # films : Action puis Comédie (ajout)
            f"{base}/series/9000.mp4", f"{base}/series/9001.mp4",      # Ma Série S01
            f"{base}/series/9002.mkv",                                 # Ma Série S02
            f"{base}/series/9101.avi",                                 # Dessin animé (ajout manuel)
        ])
        self.assertIn('group-title="Films | Action",Le Film d\'action', data)
        self.assertIn('tvg-logo="https://img.invalid/501.jpg"', data)
        self.assertIn('group-title="Séries | Drame",Ma Série S01 E01 - Pilote', data)
        self.assertIn(",Ma Série S02 E01\n", data)
        self.assertIn('tvg-logo="https://img.invalid/900.jpg"', data)
        self.assertIn('group-title="Séries | Animation",Dessin animé S01 E01 - Épisode 1', data)
        self.assertNotIn("/pw/", data)
        self.assertNotIn("tvg-chno=\"1\" group-title=\"Films", data)
        # Filtre par type, même URL de base.
        movies_only = self.get(base + "/p/salon.m3u?content=vod")[1].decode()
        self.assertEqual(stream_lines(movies_only), [f"{base}/movie/501.mkv", f"{base}/movie/502.mp4"])
        self.assertNotIn("x-tvg-url", movies_only.splitlines()[0])
        self.assertEqual(self.get(base + "/p/salon.m3u?content=films")[0], 400)

    def test_group_keys_are_namespaced(self):
        """Catégorie « 1 » du direct ≠ catégorie « 1 » des films : ordre et noms indépendants."""
        self.create({"slug": "mix", "name": "Mix", "category_ids": ["1"], "vod_category_ids": ["1"],
                     "group_order": ["vod:1", "1"], "group_names": {"vod:1": "Mes films", "1": "Infos"}})
        data = self.get(self.stack.proxy_base + "/p/mix.m3u")[1].decode()
        groups = []
        for line in data.splitlines():
            if 'group-title="' in line:
                g = line.split('group-title="')[1].split('"')[0]
                if g not in groups:
                    groups.append(g)
        self.assertEqual(groups, ["Mes films", "Infos"])

    def test_series_failures_and_cache(self):
        self.create({"slug": "series", "name": "Séries", "series_category_ids": ["7"]})
        time.sleep(0.5)  # préchargement lancé à l'enregistrement
        status, body, headers = self.get(self.stack.proxy_base + "/p/series.m3u")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Series-Pending"), "1")   # 902 en échec
        self.assertEqual(stream_lines(body.decode()), [f"{self.stack.proxy_base}/series/9101.avi"])
        calls = list(FakeXtream.series_info_calls)
        self.get(self.stack.proxy_base + "/p/series.m3u")
        # Cache disque pour 901, pas de nouvel essai immédiat pour 902.
        self.assertEqual(FakeXtream.series_info_calls, calls)
        self.assertTrue(Path(self.tmp.name, "cache", "series", "901.json").exists())
        stats = json.loads(self.get(self.stack.web_base + "/api/status")[1])["series_cache"]
        self.assertEqual(stats["cached"], 1)

    def test_series_cache_survives_restart(self):
        self.create({"slug": "s", "name": "S", "included_series_ids": ["900"]})
        self.get(self.stack.proxy_base + "/p/s.m3u")
        before = FakeXtream.series_info_calls.count("900")
        restarted = self.make_stack()
        body = self.get(restarted.proxy_base + "/p/s.m3u")[1].decode()
        self.assertEqual(len(stream_lines(body)), 3)
        self.assertEqual(FakeXtream.series_info_calls.count("900"), before)

    def test_series_budget_zero_returns_quickly(self):
        stack = self.make_stack(series_fetch_seconds=0)
        self.create({"slug": "lent", "name": "Lent", "included_series_ids": ["900"]}, stack)
        # Pas de préchargement terminé garanti : la réponse arrive sans attendre et signale l'attente,
        # ou contient déjà les épisodes si le préchargement a été plus rapide.
        status, body, headers = self.get(stack.proxy_base + "/p/lent.m3u")
        self.assertEqual(status, 200)
        if not stream_lines(body.decode()):
            self.assertEqual(headers.get("X-Series-Pending"), "1")

    def test_full_playlist_content(self):
        base = self.stack.proxy_base
        data = self.get(base + "/playlist.m3u?content=vod")[1].decode()
        self.assertEqual(stream_lines(data), [f"{base}/movie/501.mkv", f"{base}/movie/503.mp4", f"{base}/movie/502.mp4"])
        both = self.get(base + "/playlist.m3u?content=live,vod")[1].decode()
        self.assertEqual(len(stream_lines(both)), 9)
        self.assertEqual(self.get(base + "/playlist.m3u?content=series")[0], 400)
        direct = self.get(base + "/playlist.m3u?content=vod&mode=direct")[1].decode()
        self.assertIn(self.provider_url + "/movie/user/pw/501.mkv", direct)

    def test_movie_relay_with_range_and_head(self):
        url = self.stack.proxy_base + "/movie/501.mkv"
        status, body, headers = self.get(url)
        self.assertEqual((status, len(body), headers["Content-Type"]), (200, 1024, "video/x-matroska"))
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        status, body, headers = self.get(url, {"Range": "bytes=1000-"})
        self.assertEqual((status, len(body), headers["Content-Range"]), (206, 24, "bytes 1000-1023/1024"))
        with urlopen(Request(url, method="HEAD"), timeout=5) as r:
            self.assertEqual((r.status, r.headers["Content-Length"], r.read()), (200, "1024", b""))
        status, body, headers = self.get(self.stack.proxy_base + "/series/9000.mp4")
        self.assertEqual((status, body, headers["Content-Type"]), (200, b"EPISODE", "video/mp4"))
        self.assertEqual(self.get(self.stack.proxy_base + "/movie/999.mkv")[0], 502)
        self.assertEqual(self.get(self.stack.proxy_base + "/movie/501.toolong")[0], 404)
        self.assertEqual(self.get(self.stack.proxy_base + "/movie/../501.mkv")[0], 404)

    def test_token_applies_to_vod(self):
        stack = self.make_stack(access_token="tok")
        self.assertEqual(self.get(stack.proxy_base + "/movie/501.mkv")[0], 403)
        self.assertEqual(self.get(stack.proxy_base + "/movie/501.mkv?token=tok")[0], 200)
        self.create({"slug": "f", "name": "F", "vod_category_ids": ["2"]}, stack)
        data = self.get(stack.proxy_base + "/p/f.m3u?token=tok")[1].decode()
        self.assertIn("/movie/502.mp4?token=tok", data)


if __name__ == "__main__":
    unittest.main()
