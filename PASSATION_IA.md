# Passation — Xtream M3U Local

> **État au 16 novembre 2026 — version 3.0.0.** Document à transmettre tel quel à l'IA suivante avec le dépôt.
> Aucun mot de passe, token ni URL fournisseur réelle ne doit être copié ici ni commité sur GitHub.

## 1. Objectif et contexte utilisateur

- Unraid « Tower », appdata sur pool **NVMe**, IP LAN vue sur une capture : `192.168.0.108`. Lecteur Android TV (écran « Playlist M3U via URL » avec champ EPG et « EPG Shift »).
- Abonnement Xtream Codes : `player_api.php` et les flux `/live/U/P/ID.ts` fonctionnent ; **`get.php` ne donne pas de playlist**.
- But : redistribuer le catalogue sur le LAN (playlists M3U à URL permanente + serveur Xtream local), avec interface Web, sans exposer les identifiants.
- Historique des retours réels : la 2.1.0 « fonctionne » sur Tower (direct) ; il manquait la VOD (→ 2.2.0), l'éditeur films/séries n'était pas visible (→ 2.2.1), EPG (→ 2.3.0), le lecteur demandait `http://IP:7777/xmltv.php` (404) (→ 2.3.1). Puis demande « fais tout » de la liste d'améliorations (→ **3.0.0**).

## 2. Architecture

Un seul conteneur Docker, **Python 3.13 bibliothèque standard uniquement**, interface HTML/CSS/JS sans framework ni CDN.
Démarrage `python -m app` : config → droits de `/data` puis abandon des privilèges (PUID/PGID 99:100) → serveurs 7777 et 7778 → préchargement des comptes et catalogues → arrêt propre sur SIGTERM.

### Modules (`app/`)

| Fichier | Rôle |
|---|---|
| `server.py` | `Config` (variables d'env.), `XtreamAPI` (catalogues live/vod/series en cache, comptes, URL fournisseur, replay), génération M3U, `Handler` du port 7777 (toutes les routes lecteurs), fabriques `make_*`, `main()` |
| `providers.py` | `ProviderSet` : façade multi-fournisseurs (même interface que `XtreamAPI`), encodage des identifiants `k×10¹¹+id`, `UnknownProvider` |
| `builder.py` | Sélection d'une playlist (règles, doublons, noms, numéros, favoris) — partagée par M3U et mode Xtream |
| `custom.py` | Validation des playlists, `filter_kind`, `PlaylistStore` (JSON atomique + `.bak`, restauration) |
| `devices.py` | `DeviceStore` (`devices.json`, 0600) : appareils, codes, option « exiger un code » |
| `xtream_emu.py` | `XtreamEmulator` : réponses `player_api.php` par appareil |
| `streams.py` | `StreamManager` : limite de connexions, reprise, délai anti-boucle, lectures en cours |
| `hls.py` | Relais HLS : réécriture des manifestes, registre de codes opaques |
| `epg.py` | Cache XMLTV, index, EPG par playlist complété par l'API (`get_simple_data_table`) |
| `vod.py` | Normalisation films/séries/épisodes, `KeyedCache` (cache disque par identifiant), `SeriesCache` |
| `diagnostic.py` | Diagnostic en un clic (API, catalogues, guide, flux TS/HLS, film, épisode, replay) |
| `httputil.py` | gzip (si `Accept-Encoding`) et ETag/304 |
| `web.py` | Port 7778 : fichiers statiques, API REST (playlists, catalogues, appareils, flux, EPG, sauvegarde, diagnostic) |
| `static/` | `index.html`, `app.css`, `app.js`, `favicon.svg` (CSP stricte, aucun script inline) |

### Port 7777 (lecteurs) — routes

| Route | Description |
|---|---|
| `/health`, `/` | Santé, aide |
| `/p/<slug>.m3u` | Playlist personnalisée (`?content=live,vod,series`, `?format=ts|m3u8`), gzip + ETag |
| `/p/<slug>.xml` | Guide de la playlist (XMLTV + complément API) |
| `/playlist.m3u`, `/get.php` | Playlist complète (`?content=vod`, `?format=m3u8` → HLS, `?mode=direct`) |
| `/epg.xml`, `/xmltv.php` | Guide complet (complété par l'API pour les chaînes des playlists, ≤ `EPG_API_MAX_CHANNELS`) |
| `/stream/<id>.ts` | Relais MPEG-TS (compté dans la limite ; HEAD local) |
| `/hls/<id>.m3u8`, `/hls/v/<code>.m3u8`, `/hls/s/<code>.<ext>` | Relais HLS |
| `/movie/<id>.<ext>`, `/series/<id>.<ext>` | Film / épisode (Range, ETag, HEAD relayé) |
| `/timeshift/<id>.ts?start=<epoch>&end=<epoch>` (ou `duration=<min>`) | Replay (heure convertie dans le fuseau du fournisseur) |
| `/player_api.php?username&password[&action]` | Serveur Xtream local (identifiants d'appareil) |
| `/live|movie|series/<u>/<p>/<id>.<ext>`, `/timeshift/<u>/<p>/<min>/<AAAA-MM-JJ:HH-MM>/<id>.ts`, `/streaming/timeshift.php` | Flux en mode Xtream local |
| `/get.php`, `/xmltv.php` **avec** `username`/`password` d'appareil | Playlist / guide de l'appareil |

Authentification 7777 : `?token=` = `ACCESS_TOKEN` ou code d'appareil ; exigé si `ACCESS_TOKEN` est défini ou si `devices.json.require_token`. Le code reçu est repris dans tous les liens générés (`Handler.request_token`). Routes Xtream : identifiants d'appareil.

### Port 7778 (interface) — API

`GET /api/status[?account=1]` · `GET /api/catalog?kind=live|vod|series[&refresh=1]` · `GET /api/catalog/summary` · `GET /api/catalog/export?kind=` · `GET /api/series/<id>` · `GET/POST /api/playlists`, `PUT/DELETE /api/playlists/<slug>` · `GET /api/epg[?slug=]`, `POST /api/epg/refresh` · `GET /api/streams`, `POST /api/streams/<id>/stop` · `GET/POST /api/devices`, `PUT/DELETE /api/devices/<id>`, `PUT /api/devices/settings` · `GET /api/backup`, `POST /api/restore` · `POST /api/diagnostic`.
Mutations : en-tête `X-Requested-With: XtreamM3ULocal` + contrôle `Origin` (CSRF). `WEB_PASSWORD` → Basic Auth sur tout le port.

### Interface (4 vues)

- **Mes playlists** : tuiles TV en direct / Films / Séries ; onglets Catégories (règles automatiques) / Éléments (☆ favoris, ✎ nom + numéro, badges EPG / Replay / Doublon masqué) / Résumé (groupes, ajouts, exclusions, favoris, noms et numéros, numérotation automatique) ; blocs « Guide des programmes » et « Options » (TS/HLS, replay, doublons, nom du groupe favoris).
- **Catalogue fournisseur** : lecture seule, export JSON.
- **Appareils** : création, playlist, identifiants M3U et Xtream à copier, nouveau code, suppression, option « exiger un code ».
- **Système** : abonnement(s), lectures en cours, diagnostic, sauvegarde/restauration. Bandeau d'expiration (< 14 j).

## 3. Données

`DATA_DIR` (`/data`) : `playlists.json` (+ `.bak`), `devices.json` (0600), `epg/` (xmltv.xml, index.json, p-<clé>.xml/.sig), `cache/series/<id>.json`, `cache/epg/<id>.json`.

### Playlist

```json
{"slug": "salon", "name": "Salon",
 "category_ids": ["1"], "included_stream_ids": [], "excluded_stream_ids": [],
 "vod_category_ids": [], "included_vod_ids": [], "excluded_vod_ids": [],
 "series_category_ids": [], "included_series_ids": [], "excluded_series_ids": [],
 "group_order": ["10", "vod:3"], "group_names": {"10": "Sports"},
 "live_format": "ts", "catchup": true, "dedupe": false,
 "rules": [{"kind": "live", "match": "prefix", "value": "FR |"}],
 "item_names": {"42": "France Info", "vod:501": "Mon film"}, "item_numbers": {"42": 5},
 "favorites": ["42", "vod:501", "series:900"], "favorites_name": "★ Favoris",
 "updated_at": 1794773987}
```

Clés de groupes/éléments : direct sans préfixe, films `vod:`, séries `series:`. Sélection : (catégorie cochée **ou** correspondant à une règle **ou** élément inclus) **et** non exclu ; doublons (direct) : meilleure qualité par (groupe, nom de base) ; noms/numéros appliqués ; ordre : groupe (ordre perso puis fournisseur), chaînes numérotées d'abord ; favoris = groupe `★` en tête (éléments dupliqués). Anciens fichiers compatibles.

### Appareil

`{"id", "name", "username", "password" (10 car. sans ambiguïté), "playlist" ("" = tout le catalogue), "created_at"}` ; `require_token` global.

## 4. Décisions techniques (et pourquoi)

- **Pas de dépendance** (stdlib) : image ~70 Mo, surface réduite, build ~1 s.
- **Ids multi-fournisseurs numériques** (`k×10¹¹+id`) : certains lecteurs Xtream exigent des entiers ; le fournisseur 1 reste inchangé (compatibilité des playlists existantes). Les fournisseurs ≥ 2 n'ont pas d'XMLTV fusionné : leurs chaînes passent par le complément API (mode `auto`).
- **Limite de connexions** : remplacer d'abord le flux du même appareil (IP), sinon le plus ancien ; **délai de 30 s** pour l'appareil coupé sur le même flux (sinon boucle de coupures entre deux TV) ; `STREAM_TAKEOVER=false` ne coupe jamais un autre appareil. Sessions HLS « virtuelles » (actives tant que des requêtes arrivent, 30 s).
- **HLS** : codes opaques en mémoire (15 min, 50 000 max) plutôt qu'URL chiffrées (pas d'AES en stdlib) ; liens relatifs à la racine (`/hls/s/…`) avec le code de la requête.
- **Replay** : Xtream attend l'heure locale du serveur ; fuseau lu dans `server_info.timezone` (ou `PROVIDER_TIMEZONE`), `tzdata` installé dans l'image.
- **Mode Xtream** : réponses reconstruites depuis le moteur de sélection (ordre/noms/numéros identiques au M3U) ; fiches détaillées relayées puis expurgées ; accès aux flux live/films restreint à la playlist de l'appareil (pas de contrôle fin pour les épisodes).
- **Séries** jamais dans la playlist complète (un appel par série). Épisodes et guide API : cache disque, préchargement à l'enregistrement, budget de temps par requête.
- **gzip** seulement si demandé ; gros fichiers XML compressés en flux (sans Content-Length, fermeture de connexion).
- **CI** : construction en parallèle des tests, étiquette `sha-…` d'abord, `latest`/version seulement après tests (promotion `imagetools`, sans reconstruction).

## 5. Historique des sessions

- **2.1.0** : audit V2 (catégories vides/ordre, sans-catégorie, multi-catégories, double import, droits `/data`, exceptions, SIGTERM, CI arm64), nouvelle interface, catalogue fournisseur, EPG filtré, Docker/Unraid.
- **2.2.0 / 2.2.1** : films et séries (épisodes via `get_series_info`), puis tuiles « Contenu de la playlist ».
- **2.3.0 / 2.3.1** : EPG complété par l'API, bloc EPG ; `/xmltv.php` (le lecteur de l'utilisateur l'appelait d'office).
- **3.0.0** (cette session) : tout le reste de la liste d'améliorations — gzip/ETag, limite de connexions, HLS, replay, appareils, serveur Xtream local, règles/doublons/favoris/noms/numéros, sauvegarde, alerte d'expiration, diagnostic, multi-fournisseurs, CI parallèle, Dependabot, CHANGELOG, branche `main` et tag `v3.0.0`.
- Faux échec intermittent corrigé en 2.3.0 : un test cherchait « pw » dans une réponse contenant un chemin temporaire aléatoire.

## 6. Tests et vérifications (16/11/2026)

| Vérification | Résultat |
|---|---|
| `python -m unittest discover -s tests -t .` | **153 tests OK** (fichiers : playlist_server, custom, web, epg, config, vod, http, streams, hls, catchup, xtream_emu, builder, system, providers) |
| `node --check app/static/app.js` | OK |
| Scénario Chromium `tests/e2e_browser.mjs` sur `python -m tools.demo` | OK : création, exclusion/ajout, groupes, règle, favori, renommage + numéro, doublons, numérotation, films/séries/épisodes, EPG, appareil (connexion `player_api.php` réelle puis révocation), diagnostic (≥ 9 tests OK), sauvegarde/restauration, catalogue, mise en page 390/768/1366/1920 px sans défilement horizontal, aucune erreur console |
| Docker 3.0.0 (`docker build` + `run`, dossier `/data` créé par root) | OK : droits abandonnés (UID 99), playlist HLS + favori + replay, appareil → `player_api.php` auth 1 (fuseau Europe/Paris), manifeste HLS réécrit, gzip, replay converti à l'heure de Paris, diagnostic vert (faux flux TS en « warn »), `devices.json` 0600, appareil conservé après recréation du conteneur, healthcheck « healthy », aucun secret dans les journaux |
| GitHub Actions | Vert à chaque push ; image `ghcr.io/rachid598/xtream-m3u-local` publiée depuis la branche par défaut |

Faux fournisseurs de test : `tests/fake_xtream.py` (direct, films, séries, épisodes, XMLTV, `get_simple_data_table`, HLS avec redirection/variantes/clé, flux lents, replay, `get_vod_info`, mode démo `serve_any`).

## 7. Non testé / limites connues

- **Rien n'a été testé avec le vrai fournisseur pour la 3.0.0** (HLS réel, replay réel et son fuseau, mode Xtream dans TiviMate/Smarters, détection VOD des lecteurs en M3U, limite de connexions avec deux vraies TV).
- Mode Xtream : pas de contrôle d'accès fin aux épisodes ; pas de `get_vod_categories` hiérarchique ; pas d'action `get_live_streams` paginée.
- Multi-fournisseurs : pas de fusion des XMLTV (complément API pour les fournisseurs ≥ 2) ; export JSON fusionné (champ `_provider`).
- Pas de transcodage, pas de proxy RTMP, pas de sous-titres externes.
- `ACCESS_TOKEN`/codes en clair dans `devices.json` (0600) et dans la sauvegarde téléchargée.

## 8. Prochaines étapes

1. Mettre à jour Tower (`git pull && docker compose up -d --build`) et lancer **Système → Diagnostic** ; noter les résultats.
2. Tester le mode Xtream dans TiviMate avec un appareil ; tester HLS (`?format=m3u8`) et le replay sur une chaîne qui le propose ; vérifier « EPG Shift = 0 ».
3. GitHub : passer la branche par défaut sur `main` (Settings → Branches), puis supprimer éventuellement `claude/wizardly-keller-bd38ld` ; rendre le package GHCR public si besoin.
4. Pistes : contrôle d'accès des épisodes par appareil, fusion de plusieurs XMLTV, historique des lectures, notifications Unraid, renommage en masse, import de logos alternatifs.
5. Mettre à jour ce fichier après chaque étape.

## 9. Sécurité et précautions

- Les identifiants Xtream ont été partagés dans une conversation antérieure : **ne pas les réutiliser ni les écrire dans le dépôt** ; les faire renouveler si possible.
- Ne pas exposer 7777/7778 sur Internet ; sinon reverse proxy HTTPS + `WEB_PASSWORD` + « exiger un code d'appareil ».
- La sauvegarde contient les codes d'appareils : la garder privée.

## 10. Critères d'acceptation

- [x] Seules `XTREAM_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD` sont nécessaires ; tout le reste a des valeurs par défaut.
- [x] Playlists personnalisées à URL permanente (direct, films, séries), modifiables sans changer d'URL, persistantes.
- [x] Aucun identifiant fournisseur dans les playlists (mode proxy), manifestes HLS, API, exports, sauvegardes, journaux.
- [x] Catalogue fournisseur préservé et exportable ; EPG avec complément API ; `/xmltv.php`.
- [x] Appareils, serveur Xtream local, limite de connexions, HLS, replay, personnalisation, diagnostic, sauvegarde, multi-fournisseurs (tests + navigateur).
- [x] Direct validé en réel par l'utilisateur (2.1.0).
- [ ] Validation réelle des fonctions 3.0.0 sur Tower et dans les lecteurs.
