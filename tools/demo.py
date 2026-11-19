"""Démo locale sans abonnement : faux fournisseur Xtream + application complète.

Usage (depuis la racine du dépôt) :
    python -m tools.demo            # 7777 (lecteurs) + 7778 (interface), données dans ./demo-data
    python -m tools.demo --channels 3000 --port 8777 --web-port 8778

Utile pour tester l'interface Web, les captures d'écran et les tests navigateur.
Aucune vraie chaîne n'est diffusée (les flux renvoient quelques octets factices).
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import logging
import random
import struct
import threading
import time
import zlib

from app import logos
from app.custom import PlaylistStore
from app.devices import DeviceStore
from app.server import Config, make_api, make_epg, make_series, make_server, make_streams
from app.web import make_web_server
from tests import fake_xtream

GROUPS = [
    "FR | Généralistes", "FR | Information", "FR | Sport", "FR | Cinéma", "FR | Séries", "FR | Jeunesse",
    "FR | Documentaires", "FR | Musique", "BE | Belgique", "CH | Suisse", "CA | Québec", "UK | General",
    "UK | Sports", "US | Entertainment", "ES | España", "IT | Italia", "DE | Deutschland", "AR | Arabic",
    "MA | Maroc", "DZ | Algérie", "TN | Tunisie", "PT | Portugal", "NL | Nederland", "Radio | France",
    "4K | UHD", "VIP | Événements", "Adultes (vide)",
]
WORDS = ["Info", "Sport", "Ciné", "Séries", "Kids", "Docu", "Music", "News", "Live", "Max", "Premium", "Action"]


def build_catalog(total: int) -> None:
    rng = random.Random(42)
    fake_xtream.CATEGORIES[:] = [{"category_id": str(i + 1), "category_name": name, "parent_id": 0}
                                 for i, name in enumerate(GROUPS)]
    # Quelques sous-groupes (parent_id) insérés après leur parent, comme chez certains fournisseurs.
    subgroups = [("101", "FR | Sport › Football", 3), ("102", "FR | Sport › Rugby", 3), ("103", "FR | Cinéma › VOSTFR", 4)]
    for offset, (cid, name, parent) in enumerate(subgroups):
        index = next(i for i, c in enumerate(fake_xtream.CATEGORIES) if c["category_id"] == str(parent)) + 1 + offset % 2
        fake_xtream.CATEGORIES.insert(index, {"category_id": cid, "category_name": name, "parent_id": parent})
    streams = []
    usable = len(GROUPS) - 1  # la dernière catégorie reste vide
    for n in range(1, total + 1):
        cat = rng.randrange(usable) + 1
        if cat in (3, 4) and n % 5 == 0:
            cat = {3: rng.choice((101, 102)), 4: 103}[cat]
        prefix = GROUPS[(cat if cat < 100 else {101: 3, 102: 3, 103: 4}[cat]) - 1].split("|")[0].strip()
        name = f"{prefix} {rng.choice(WORDS)} {n}" + (" HD" if n % 3 == 0 else "") + (" FHD" if n % 7 == 0 else "")
        streams.append({
            "num": n, "name": name, "stream_type": "live", "stream_id": 1000 + n,
            "stream_icon": "", "epg_channel_id": f"ch{n}.demo" if n % 4 else "", "category_id": str(cat),
            "tv_archive": 1 if n % 6 == 0 else 0, "tv_archive_duration": 7 if n % 6 == 0 else 0,
        })
    # Séparateurs comme chez beaucoup de fournisseurs : « ##### FR | SPORT ##### » en tête de catégorie.
    for cat_index, group in enumerate(GROUPS[:-1], start=1):
        streams.insert(0, {"num": 0, "name": f"##### {group.upper()} #####", "stream_type": "live",
                           "stream_id": 900000 + cat_index, "stream_icon": "", "epg_channel_id": "",
                           "category_id": str(cat_index)})
    # Quelques chaînes françaises « réelles » pour essayer les groupes personnalisés (modèles TNT, Canal+…).
    known = [("FR: TF1 HD", 1), ("FR: France 2 HD", 1), ("FR: France 3", 1), ("FR: CANAL+ HD", 1), ("FR: M6 HD", 1),
             ("FR: Arte", 1), ("FR: BFM TV", 2), ("FR: franceinfo", 2), ("FR: CANAL+ SPORT", 3), ("FR: CANAL+ CINEMA", 4),
             ("FR: Gulli", 6), ("FR: TF1 4K", 25), ("FR: M6 4K", 25)]
    for index, (name, cat) in enumerate(known, start=1):
        streams.append({"num": 5000 + index, "name": name, "stream_type": "live", "stream_id": 800000 + index,
                        "stream_icon": "", "epg_channel_id": "", "category_id": str(cat)})
    # Catégories qui ne diffèrent que par la qualité (comme chez certains fournisseurs), avec la même
    # chaîne dans chacune : pour essayer « Cocher aussi les catégories liées » et les secours hors sélection.
    quality_cats = [("200", "FRANCE FHD | OTT"), ("201", "FRANCE HD | OTT"),
                    ("202", "FRANCE SD | OTT"), ("203", "FRANCE HEVC | OTT")]
    fake_xtream.CATEGORIES.extend({"category_id": cid, "category_name": name, "parent_id": 0} for cid, name in quality_cats)
    for index, (tag, (cid, _)) in enumerate(zip(["FHD", "HD", "SD", "HEVC"], quality_cats), start=1):
        streams.append({"num": 5100 + index, "name": f"Zenith {tag}", "stream_type": "live",
                        "stream_id": 800100 + index, "stream_icon": "", "epg_channel_id": "", "category_id": cid})
    fake_xtream.STREAMS[:] = streams

    # Guide : ~2/3 des chaînes avec identifiant dans xmltv.php, d'autres seulement via l'API, le reste sans programme.
    import base64
    from xml.sax.saxutils import escape
    now = int(time.time()) // 3600 * 3600
    shows = ["Journal", "Magazine", "Match en direct", "Documentaire", "Série du soir", "Film", "Météo"]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="demo">']
    fake_xtream.SIMPLE_EPG.clear()
    for item in streams:
        n = item["num"]
        if item["epg_channel_id"] and n % 5 != 0:
            cid = item["epg_channel_id"]
            lines.append(f'<channel id="{cid}"><display-name>{escape(item["name"])}</display-name></channel>')
            for h in range(-2, 22):
                start, stop = now + h * 3600, now + (h + 1) * 3600
                fmt = lambda t: time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t))
                lines.append(f'<programme start="{fmt(start)}" stop="{fmt(stop)}" channel="{cid}">'
                             f'<title>{rng.choice(shows)}</title></programme>')
        elif n % 7 != 0:
            fake_xtream.SIMPLE_EPG[str(item["stream_id"])] = {"epg_listings": [
                {"title": base64.b64encode(rng.choice(shows).encode()).decode(), "description": "",
                 "start_timestamp": str(now + h * 3600), "stop_timestamp": str(now + (h + 1) * 3600)}
                for h in range(-1, 12)]}
    lines.append("</tv>")
    fake_xtream.XMLTV = "\n".join(lines)

    # Films et séries fictifs (catégories propres, identifiants indépendants du direct).
    movie_groups = ["VOD | Nouveautés", "VOD | Action", "VOD | Comédie", "VOD | Animation", "VOD | Documentaires", "VOD | 4K"]
    fake_xtream.VOD_CATEGORIES[:] = [{"category_id": str(i + 1), "category_name": n, "parent_id": 0}
                                     for i, n in enumerate(movie_groups)]
    titles = ["L'Ombre", "Le Retour", "La Mission", "Horizon", "Le Secret", "Minuit", "L'Évasion", "Tempête"]
    fake_xtream.VOD_STREAMS[:] = [{
        "num": n, "name": f"{rng.choice(titles)} {n}", "stream_type": "movie", "stream_id": 50000 + n,
        "stream_icon": "", "rating": f"{rng.uniform(4, 9):.1f}", "category_id": str(rng.randrange(len(movie_groups)) + 1),
        "container_extension": rng.choice(["mkv", "mp4"]), "year": str(rng.randrange(1990, 2027)),
    } for n in range(1, max(50, total // 3) + 1)]
    series_groups = ["Séries | Drame", "Séries | Policier", "Séries | Jeunesse", "Séries | Docu-séries"]
    fake_xtream.SERIES_CATEGORIES[:] = [{"category_id": str(i + 1), "category_name": n, "parent_id": 0}
                                        for i, n in enumerate(series_groups)]
    fake_xtream.SERIES[:] = []
    fake_xtream.SERIES_INFO.clear()
    for n in range(1, 61):
        sid = 70000 + n
        fake_xtream.SERIES.append({"num": n, "name": f"Série {rng.choice(titles)} {n}", "series_id": sid, "cover": "",
                                   "category_id": str(rng.randrange(len(series_groups)) + 1),
                                   "releaseDate": f"{rng.randrange(2005, 2027)}-01-01", "rating": f"{rng.uniform(5, 9):.1f}"})
        seasons = {}
        for season in range(1, rng.randrange(1, 4) + 1):
            seasons[str(season)] = [{"id": str(sid * 100 + season * 20 + e), "episode_num": e, "season": season,
                                     "title": f"Épisode {e}", "container_extension": "mkv"}
                                    for e in range(1, rng.randrange(4, 11))]
        fake_xtream.SERIES_INFO[str(sid)] = {"info": {}, "episodes": seasons}


def demo_png(rgb: tuple[int, int, int], width: int = 64, height: int = 36) -> bytes:
    """Image PNG unie (sans dépendance) pour simuler un logo haute qualité."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    row = b"\x00" + bytes(rgb) * width
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))


def demo_logos(data_dir: str) -> logos.LogoLibrary:
    """Bibliothèque de logos hors ligne : quelques chaînes françaises (tv-logos) et marocaines (iptv-org)."""
    tvl, db = "https://tvl.demo.invalid/countries", "https://db.demo.invalid/data"
    colors = {"tf1": (0, 70, 160), "france-2": (200, 0, 30), "m6": (90, 90, 90), "canal-plus": (10, 10, 10)}
    files = {f"{tvl}/france/0_all_logos_mosaic.md": "\n".join(f"[{n}]:{n}-fr.png" for n in colors).encode(),
             f"{db}/channels.csv": b"id,name,alt_names,network,owners,country,categories,is_nsfw,launched,closed,replaced_by,website\n"
                                   b"2M.ma,2M,,,,MA,general,FALSE,,,,\n",
             f"{db}/logos.csv": b"channel,feed,in_use,tags,width,height,format,url\n"
                                b"2M.ma,,TRUE,,64,36,PNG,https://img.demo.invalid/2m.png\n",
             "https://img.demo.invalid/2m.png": demo_png((230, 120, 0))}
    files.update({f"{tvl}/france/{n}-fr.png": demo_png(rgb) for n, rgb in colors.items()})

    def fetch(url: str, limit: int) -> tuple[bytes, str]:
        if url not in files:
            raise OSError("absent de la démo")
        return files[url], ""
    library = logos.LogoLibrary(data_dir, source=db, tvlogos=tvl, fetch=fetch)
    library.refresh(force=True)
    return library


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channels", type=int, default=1500)
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--web-port", type=int, default=7778)
    parser.add_argument("--data", default="demo-data")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--profiles", action="store_true",
                        help="deux abonnements (profils « Moi » et « Sœur ») sur le même faux fournisseur")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_catalog(args.channels)
    fake_xtream.FakeXtream.serve_any = True
    provider, provider_url = fake_xtream.start_fake()
    config = Config(provider_url, fake_xtream.USERNAME, fake_xtream.PASSWORD, port=args.port,
                    web_port=args.web_port, data_dir=args.data, epg_api_per_second=0)  # faux fournisseur : pas de limite
    if args.profiles:
        config = replace(config, provider_name="Moi",
                         extra_providers=(("Sœur", provider_url, fake_xtream.USERNAME, fake_xtream.PASSWORD),))
    from app import provider_store
    from app.providers import ApiHolder
    providers = provider_store.ProviderStore(args.data)
    provider_store.install(providers)
    api = ApiHolder(make_api(config, providers))  # abonnements ajoutables depuis l'interface
    store = PlaylistStore(args.data)
    epg = make_epg(config, api)
    series = make_series(config, api)
    streams = make_streams(config, api)
    devices = DeviceStore(args.data)
    logos.install(demo_logos(args.data))
    proxy = make_server(config, host=args.host, api=api, store=store, epg=epg, series=series,
                        streams=streams, devices=devices)
    web = make_web_server(config, api, store, host=args.host, epg=epg, series=series,
                          streams=streams, devices=devices)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    print(f"Démo : interface http://{args.host}:{args.web_port}/  —  lecteurs http://{args.host}:{args.port}/")
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        web.server_close()
        proxy.shutdown()
        provider.shutdown()


if __name__ == "__main__":
    main()
