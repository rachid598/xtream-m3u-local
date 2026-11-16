# Passation — Xtream M3U Local

> **État au 16 novembre 2026 — version 2.3.1 (TV en direct + films + séries + guide EPG complété par l'API, `/xmltv.php`).** Document à transmettre tel quel à l'IA suivante avec le dépôt.
> Aucun mot de passe, token ni URL fournisseur réelle ne doit être copié ici ni commité sur GitHub.

## 1. Objectif utilisateur

L'utilisateur exploite **Unraid** (serveur « Tower », appdata sur un pool **NVMe**) et possède un abonnement Xtream Codes actif :
`player_api.php` répond (`get_live_streams`, `get_live_categories`), et une URL directe `/live/USER/PASS/STREAM_ID.ts` est lisible.
En revanche, `get.php?type=m3u_plus&output=ts|m3u8` ne produit **pas** de playlist exploitable.

But : reconstruire les playlists à partir de l'API Xtream et les diffuser depuis Unraid sous forme d'**URL M3U permanentes**
(VLC, TiviMate, lecteurs Android TV), avec une interface Web de gestion, en ne saisissant que `XTREAM_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD`.

**Retour de l'utilisateur après déploiement réel (2.1.0)** : « ça fonctionne », mais **il manquait la VOD (films et séries)**, absente des URL de lecture → ajoutée en **2.2.0** (section 4 bis).

Demande complémentaire de l'utilisateur (prise en compte en 2.1.0) : **préserver à l'identique le catalogue d'origine du fournisseur**
(ordre, identifiants, `parent_id`, catégories vides, chaînes associées) avant toute personnalisation, avec une vue
« Catalogue fournisseur » en lecture seule et un export JSON ; puis des playlists personnalisées indépendantes
(sélection, réorganisation et renommage des groupes) sans altérer l'organisation d'origine.

## 2. Architecture (inchangée dans ses principes)

- **Un seul conteneur** Docker, Python 3.13, **bibliothèque standard uniquement** ; interface HTML/CSS/JS sans framework ni CDN.
- **Port 7777** (lecteurs, `app/server.py`) :
  - `/health` ; `/` (texte d'aide) ;
  - `/playlist.m3u` (toutes les chaînes) et alias `/get.php` ; `?mode=direct&format=ts|m3u8` (liens fournisseur, identifiants visibles) ;
  - `/p/<slug>.m3u` playlist personnalisée ; `/p/<slug>.xml` EPG filtré pour cette playlist ;
  - `/stream/<id>.ts` relais MPEG-TS sans transcodage (Range transmis ; `HEAD` répondu localement sans contacter le fournisseur) ;
  - `/movie/<id>.<ext>` (film) et `/series/<episode_id>.<ext>` (épisode) : relais vers `/movie/U/P/…` et `/series/U/P/…` du fournisseur, Range/Content-Range/ETag transmis, `HEAD` relayé (taille du fichier) ;
  - `?content=live|vod|series` (combinables par virgule, ou `all`) sur `/p/<slug>.m3u` ; `/playlist.m3u?content=vod` pour tous les films (séries refusées en playlist complète) ;
  - `/epg.xml` XMLTV complet (cache disque) ; `?refresh=1` force la relecture du catalogue ; `?token=` si `ACCESS_TOKEN`.
- **Port 7778** (gestion, `app/web.py`) :
  - `/` + `/static/app.css`, `/static/app.js`, `/static/favicon.svg` (liste blanche, aucun accès fichier arbitraire) ;
  - `GET /api/status[?account=1]`, `GET /api/catalog[?kind=live|vod|series][&refresh=1]`, `GET /api/catalog/export[?kind=…]` ;
  - `GET /api/series/<series_id>` : épisodes (via `get_series_info`, cache) ;
  - `GET/POST /api/playlists`, `PUT/DELETE /api/playlists/<slug>`.
- Catalogues : `XtreamAPI.catalog(kind=…)` — un état indépendant par type (`live`, `vod`, `series`), cache mémoire `CACHE_SECONDS`=1800 s, repli sur cache périmé, pas de nouvel essai pendant 60 s après un échec. Les catalogues films/séries ne sont chargés qu'à la demande.
- Épisodes : `app/vod.py` → `SeriesCache` (mémoire + `DATA_DIR/cache/series/<id>.json`, 24 h, 4 récupérations parallèles, pas de nouvel essai pendant 5 min après un échec, budget `SERIES_FETCH_SECONDS` par requête M3U, préchargement à l'enregistrement d'une playlist).
- Données : `DATA_DIR` (`/data`) → `playlists.json` (+ `playlists.json.bak`) et `epg/` (cache XMLTV + EPG filtrés).
- Démarrage (`python -m app`) : lecture de la config, correction des droits de `/data` puis abandon des privilèges root → `PUID:PGID` (99:100), lancement des deux serveurs, préchargement du catalogue en tâche de fond, arrêt propre sur SIGTERM.

### Arborescence

```text
app/
  __init__.py        # __version__ = "2.2.0"
  __main__.py        # point d'entrée « python -m app » (évite le double import de app.server)
  server.py          # Config, XtreamAPI, normalisation, M3U, port 7777, droits /data, main()
  custom.py          # validation, filtrage, PlaylistStore (JSON atomique + .bak)
  vod.py             # films/séries : normalisation, nommage des épisodes, SeriesCache
  epg.py             # EpgCache (téléchargement, gzip, cache disque) + filtrage XMLTV en flux
  web.py             # port 7778 : statiques, API REST, export catalogue, Basic Auth, CSRF
  static/index.html  # structure (aucun script/style inline)
  static/app.css     # thème clair/sombre automatique, responsive
  static/app.js      # logique de l'interface
  static/favicon.svg
tests/
  fake_xtream.py     # faux fournisseur Xtream (catégories, parent_id, chaînes multi-catégories, EPG, erreurs)
  helpers.py         # pile complète 7777+7778 sur ports éphémères
  test_playlist_server.py  test_custom.py  test_web.py  test_epg.py  test_config.py  test_vod.py
  e2e_browser.mjs    # scénario navigateur Playwright (Node), lancé manuellement
tools/demo.py        # démo sans abonnement (1500 chaînes fictives, sous-groupes)
Dockerfile  compose.yaml  compose.single.example.yaml  compose.ghcr.yaml  docker-create.sh
unraid-template.xml  .env.example  .dockerignore  .gitignore  .github/workflows/docker-publish.yml
README.md  PASSATION_IA.md  PROMPT_AUTRE_IA.md  PROMPT_HERMES_UNRAID.md  LICENSE
```

## 3. Modèle de données

### Catalogue normalisé (mémoire)

Chaque chaîne : `{stream_id, name, logo, epg_id, num, category_id, category_ids}`.
- `category_ids` = `category_id` + éventuel tableau `category_ids` du fournisseur (chaînes multi-catégories).
- Chaîne sans catégorie → catégorie spéciale `none` (« Sans catégorie »), désormais **sélectionnable**.
- Logos non `http(s)` ignorés (ex. `javascript:`) ; noms nettoyés (retours à la ligne, tabulations).
- Catégories : **ordre exact** de `get_live_categories`, y compris les catégories vides ; métadonnées `XtreamAPI.category_meta[cid] = {parent_id, source: provider|derived, position}` (`parent_id` « 0 » ou vide = racine). Les catégories référencées par des chaînes mais absentes de `get_live_categories` sont ajoutées à la fin avec `source: derived` (« Catégorie 99 »).
- Réponses brutes conservées (`raw_categories`, `raw_streams`) pour l'export ; `categories_error` renseigné si `get_live_categories` échoue (l'interface affiche alors un avertissement).

### Playlist persistée (`/data/playlists.json`)

```json
{
  "slug": "salon",
  "name": "Salon",
  "category_ids": ["1", "10"],
  "included_stream_ids": ["123"],
  "excluded_stream_ids": ["456"],
  "group_order": ["10", "1"],
  "group_names": {"10": "Sports"},
  "updated_at": 1794773987
}
```

- Inclusion : (catégorie cochée **OU** chaîne ajoutée) **ET** chaîne non exclue. Groupe d'une chaîne multi-catégories = première catégorie cochée qui la contient.
- Ordre M3U : groupes selon `group_order`, puis les autres dans l'ordre du fournisseur ; chaînes dans l'ordre de `get_live_streams`.
  `/playlist.m3u` suit l'ordre des catégories du fournisseur.
- `group_names` renomme le `group-title` **dans cette playlist uniquement**.
- Le `slug` est figé après création (URL permanente) ; le nom est modifiable. Les anciens fichiers V2 (sans `group_order`/`group_names`) restent compatibles.
- Limites : 100 playlists, 100 000 identifiants par liste, slug `^[a-z0-9][a-z0-9_-]{0,59}$`, nom 1–100 caractères.

## 4 quinquies. Session 5 — `/xmltv.php` (2.3.0 → 2.3.1)

**Retour utilisateur (capture d'écran du lecteur Android TV, écran « Playlist M3U via URL »)** : playlist `http://192.168.0.108:7777/playlist.m3u` → « La Playlist est accessible » ; champ EPG `http://192.168.0.108:7777/xmltv.php` → « Impossible de lire le contenu de cette URL » (404 : cette adresse n'existait pas, le guide était sous `/epg.xml`). L'utilisateur utilise la **playlist complète**, pas une playlist personnalisée.

**Modifications**
- `/xmltv.php` (paramètres `username`/`password` ignorés, `?token=` respecté) = alias de `/epg.xml`.
- Guide complet (`Handler.full_guide`) : en mode `auto`/`api`, XMLTV limité aux chaînes du compte + complément API **uniquement pour les chaînes présentes dans les playlists personnalisées** (`playlists_live_ids`, plafond `EPG_API_MAX_CHANNELS`=1000) ; 502 explicite si ni XMLTV ni chaînes à compléter. Mode `xmltv` : fichier fournisseur tel quel.
- `/playlist.m3u` : `tvg-id="xtream-<id>"` pour les chaînes sans identifiant EPG (hors mode `xmltv`).
- Interface : carte « Playlist complète » avec l'URL EPG `…/xmltv.php` à copier et conseil d'utiliser une playlist personnalisée ; bloc EPG mentionne `/xmltv.php`.
- Tests : 5 nouveaux (`XmltvAliasTests`) → 93 ; scénario navigateur : `/xmltv.php` renvoie des programmes.

**Conseil donné à l'utilisateur** : dans le lecteur, champ EPG `http://192.168.0.108:7777/xmltv.php` (fonctionne désormais) ; idéalement passer à `…/p/<nom>.m3u` + `…/p/<nom>.xml` (plus léger, films/séries, guide complété pour toutes les chaînes de la playlist).

## 4 quater. Session 4 — guide des programmes (2.2.1 → 2.3.0)

**Question utilisateur** : « et l'EPG ? ». Avant : guide uniquement issu de `xmltv.php` (souvent cassé chez les fournisseurs dont `get.php` ne marche pas), sans aucun état visible dans l'interface.

**Modifications**
- `EPG_SOURCE` (`auto` par défaut, `xmltv`, `api`) : en `auto`, `/p/<slug>.xml` = programmes de `xmltv.php` (ou `EPG_URL`) + complément par `get_simple_data_table&stream_id=` pour les chaînes absentes du XMLTV (ou toutes si le XMLTV est indisponible). Titres/descriptions base64 décodés (repli texte brut), horodatages `start_timestamp`/`stop_timestamp` (repli `start`/`end` en UTC), écrits en `YYYYmmddHHMMSS +0000`.
- Cache par chaîne : `KeyedCache` (généralisation de `SeriesCache`, `app/vod.py`) → `DATA_DIR/cache/epg/<stream_id>.json`, 6 h, 4 récupérations parallèles, 5 min sans nouvel essai après échec ; budget `EPG_FETCH_SECONDS` (15 s) par demande de `/p/<slug>.xml` ; préchargement à l'enregistrement d'une playlist.
- Index du XMLTV (`DATA_DIR/epg/index.json` : nombre de programmes par identifiant de chaîne, recalculé quand la source change) pour mesurer la couverture sans reparser à chaque requête.
- Chaînes sans `epg_channel_id` : `tvg-id="xtream-<stream_id>"` dans les playlists personnalisées (sauf `EPG_SOURCE=xmltv`) et `<channel id="xtream-<id>">` dans le guide.
- `EPG_URL` : guide XMLTV externe à la place de `xmltv.php` (gzip accepté).
- API Web : `GET /api/epg[?slug=]` (mode, état de la source : ok/date/taille/chaînes/programmes/erreur ; couverture de la playlist : chaînes, via xmltv, via API, en cours, liste des chaînes sans programme) ; `POST /api/epg/refresh {"slug"}` (re-téléchargement XMLTV + relecture forcée des programmes API, en arrière-plan, 202).
- Interface : bloc dépliable « 🗓️ Guide des programmes (EPG) » sous l'URL de la playlist : pastille de couverture (%), URL EPG à copier, 4 compteurs, description de la source, liste des chaînes sans programme, boutons « Actualiser le guide » / « Ouvrir le XML » ; actualisation automatique tant que des chaînes sont en cours de récupération.
- Démo : guide réaliste (≈ 2/3 des chaînes dans le XMLTV, d'autres seulement via l'API, quelques-unes sans programme).

**Tests** : 88 tests (7 nouveaux : décodage des programmes, mode auto avec complément API et priorité au XMLTV, XMLTV cassé → API pour tout, mode xmltv seul, API d'état et d'actualisation, EPG désactivé, options de configuration). Scénario navigateur : bloc EPG, couverture, URL, XML récupéré, actualisation. Conteneur Docker réel : guide fusionné XMLTV + API, cache `cache/epg`, aucun secret dans les journaux.

**Cause de l'échec intermittent signalé en 2.2.1 (corrigé)** : `test_status_without_secrets` cherchait la chaîne « pw » (faux mot de passe) dans `/api/status`, qui contient le chemin du dossier temporaire de test ; ce nom aléatoire contient parfois « pw ». Faux positif du test (aucune fuite) : le chemin est désormais exclu du contrôle.

**À vérifier en réel** : `xmltv.php` du fournisseur (bloc EPG → « Source xmltv.php … » ou message d'erreur), volume d'appels `get_simple_data_table` acceptable chez le fournisseur, décalage horaire correct dans TiviMate.

## 4 ter. Session 3 — éditeur films/séries visible (2.2.0 → 2.2.1)

**Retour utilisateur** : « Possible d'ajouter un éditeur films séries ? Car là on ne les voit pas. » En 2.2.0 le choix du type était une petite rangée de pastilles au-dessus des onglets, facile à manquer (et absente si le conteneur tournait encore en 2.1.0 : vérifier la version en bas de page de l'interface).

**Modifications**
- Nouveau bloc **« Contenu de la playlist »** dans l'éditeur : 3 grandes tuiles (📺 TV en direct, 🎬 Films, 🎞️ Séries) avec le nombre disponible chez le fournisseur (`N films disponibles · M catégories`, « Aucun film dans ton abonnement » ou « Indisponible chez le fournisseur » avec l'erreur en info-bulle) et le nombre sélectionné.
- Titre d'édition explicite au-dessus des onglets (« 🎬 Films : choisis les catégories et les films »).
- Bandeau d'aide quand l'abonnement contient des films/séries non encore sélectionnés dans la playlist, avec boutons « Ajouter des films » / « Ajouter des séries ».
- `GET /api/catalog/summary` : `{live|vod|series: {items, categories, error}}` sans transférer les listes (léger pour mobile) ; préchargement au démarrage des 3 catalogues (direct, puis films, puis séries).
- Compteurs « films/séries retenus » à 0 (au lieu de « … ») quand rien n'est sélectionné.
- La vue « Catalogue fournisseur » garde son sélecteur compact.

**Tests** : `test_catalog_summary` (81 tests) ; scénario navigateur : tuiles « films/séries disponibles », bandeau, ajout via « Ajouter des films », titre d'édition, compteur de la tuile. Contrôle visuel bureau et mobile (tuiles empilées).
**Instabilité observée** : un échec unitaire isolé — expliqué et corrigé en 2.3.0 (voir 4 quater).

## 4 bis. Session 2 — films (VOD) et séries (2.1.0 → 2.2.0)

**Besoin** : l'abonnement contient des films et des séries, mais seules les chaînes du direct apparaissaient dans les URL M3U.

**API Xtream utilisée**
| Type | Catégories | Contenus | Lecture fournisseur |
|---|---|---|---|
| Direct | `get_live_categories` | `get_live_streams` | `/live/U/P/<stream_id>.ts` |
| Films | `get_vod_categories` | `get_vod_streams` (`container_extension`, `stream_icon`, `rating`, `year`/`releaseDate`) | `/movie/U/P/<stream_id>.<ext>` |
| Séries | `get_series_categories` | `get_series` (`series_id`, `cover`) ; épisodes : `get_series_info&series_id=` (`episodes` = dict par saison **ou** liste) | `/series/U/P/<episode_id>.<ext>` |

**Modèle** : nouveaux champs de playlist `vod_category_ids`, `included_vod_ids`, `excluded_vod_ids`, `series_category_ids`, `included_series_ids`, `excluded_series_ids` (mêmes règles que le direct ; anciens fichiers compatibles). Les clés de groupes sont préfixées pour éviter les collisions d'identifiants : direct `12`, films `vod:12`, séries `series:12` (utilisées par `group_order` / `group_names`).

**M3U** : films → `#EXTINF:-1 tvg-name tvg-logo group-title,Titre` + `http://IP:7777/movie/<id>.<ext>` ; épisodes → nom `Série S01 E02 - Titre` (titre ajouté seulement s'il n'est pas une répétition « Série - S01E02 »), affiche de la série, groupe = catégorie de la série, `http://IP:7777/series/<id>.<ext>`. Ordre par défaut : groupes du direct, puis films, puis séries (ordre fournisseur), épisodes triés par saison/numéro. En-tête `url-tvg` seulement si la playlist contient du direct. En-tête HTTP `X-Series-Pending: N` si des épisodes sont encore en cours de récupération.

**Interface** : sélecteur « TV en direct / Films / Séries » (avec compteurs) dans l'éditeur et dans la vue Catalogue fournisseur ; catégories, recherche (titre + année), filtres, ajouts/exclusions par type ; affiches, année, note, extension ; bouton « Voir les épisodes » ; statistiques chaînes/films/séries ; résumé multi-types (étiquette de type sur chaque groupe, ajouts/exclusions de tous types) ; export JSON par type.

**Décisions**
- Pas de transcodage ni de téléchargement : relais octet par octet avec `Range` (avance rapide dans les films).
- Séries jamais incluses dans `/playlist.m3u` (des milliers d'appels `get_series_info`) ; uniquement dans les playlists personnalisées.
- Une erreur sur le catalogue films ou séries n'empêche pas de servir le reste de la playlist (journalisé) ; erreur seulement si tous les types demandés échouent.
- `get_json` accepte aussi un objet indexé (certains panneaux renvoient `{"1": {...}}` au lieu d'une liste) ; taille maximale d'une réponse API portée à 200 Mo (grosses VOD), délai 60 s.
- `Host` invalide ou `?content=` inconnu → 400 (au lieu de 502).

**Tests ajoutés** (`tests/test_vod.py`, 15 tests) : normalisation films/séries/épisodes (formes dict et liste), nommage, catalogues `kind=vod|series`, API épisodes, export par type, playlist mixte direct+films+séries (ordre exact des URL), clés de groupes préfixées, échec d'une série (`X-Series-Pending`), cache disque et redémarrage, budget nul, `/playlist.m3u?content=…`, relais film avec Range/HEAD/types MIME, token. Scénario navigateur étendu (films + séries + épisodes + filtre `content=vod` + vue catalogue films). Test conteneur Docker réel : M3U mixte, film 200/206, cache des épisodes dans `/data/cache/series`, aucun secret dans les journaux.

**Points à vérifier en réel** : détection des films/séries par TiviMate en mode M3U (les liens finissent par `.mkv/.mp4` et contiennent `/movie/` ou `/series/` ; sinon déclarer `…/p/films.m3u?content=vod` séparément), poids du M3U avec une grosse VOD, temps de préchargement des séries chez le fournisseur.

## 4. Modifications de la session 1 (V2 → 2.1.0)

### Bugs corrigés
1. **Catégories vides et ordre** : l'API Web ne listait que les catégories ayant des chaînes, triées par identifiant texte (« 10 » avant « 2 ») → ordre fournisseur conservé, catégories vides incluses.
2. **Chaînes sans catégorie non sélectionnables** : la catégorie vide « » était refusée par la validation → catégorie `none`.
3. **Multi-catégories** (`category_ids`) ignoré → pris en compte (filtrage, compteurs, groupe).
4. **Double import** de `app.server` (`python -m app.server` + `from .server` dans web.py) → `app/__main__.py`, `CMD ["python","-m","app"]`.
5. **Droits `/data` sur Unraid** : conteneur en `USER 99:100` incapable d'écrire dans un dossier appdata créé par root → démarrage root, `chown` de `/data`, abandon des privilèges (PUID/PGID).
6. **Exceptions non capturées** (`http.client.HTTPException` : IncompleteRead, etc.) dans l'API et le relais → capturées, réponse 502 propre.
7. **Fournisseur hors service** : chaque requête retentait l'appel (jusqu'à 20 s) → fenêtre de 60 s sans nouvel essai, cache périmé servi.
8. **SIGTERM ignoré** par Python en PID 1 (arrêt en 10 s par SIGKILL) → arrêt immédiat et propre.
9. **Workflow GHCR** : build arm64 sans QEMU (échec assuré) → `setup-qemu-action`, job de tests préalable, cache GHA.
10. **Interface** : `Promise.all` bloquait tout si le catalogue échouait ; `resp.json()` plantait sur une réponse non JSON (401) ; incohérences inclus/exclus en cochant/décochant ; « Tout désélectionner » effaçait aussi les exceptions → corrigés.
11. Test placé après `if __name__ == "__main__"` dans l'ancien fichier de tests ; relais lisant par blocs bloquants de 64 Ko (latence) → `read1`.

### Fonctions ajoutées
- **Vue « Catalogue fournisseur »** (lecture seule) : arborescence par `parent_id`, position d'origine (#n), ID, nombre de chaînes, badge « absente de get_live_categories », chaînes de chaque catégorie (numéro, nom, ID, EPG), recherche catégorie/chaîne/ID, tout déplier/replier, statistiques (catégories, sous-groupes, vides, chaînes).
- **Export JSON** `/api/catalog/export` : réponses brutes `get_live_categories` + `get_live_streams` dans l'ordre du fournisseur, identifiants remplacés par `***` (`/user/pass/` dans les URL, `username=`/`password=`, clés `username`/`password`).
- **Playlists** : réorganisation (↑ ↓) et renommage des groupes par playlist, duplication, annulation des modifications, alerte « non enregistré », Ctrl+S, actions groupées sur les résultats de recherche, filtres par catégorie/état, pagination par 200, logos, badges EPG/Ajoutée/Exclue, onglet Résumé.
- **EPG** activé par défaut : `url-tvg` + `x-tvg-url`, `/epg.xml` (cache disque `EPG_CACHE_HOURS`=12, gzip géré, validation XMLTV, 15 min sans nouvel essai après échec), `/p/<slug>.xml` filtré (iterparse en flux, insensible à la casse, cache invalidé quand la playlist ou la source change).
- **Abonnement** affiché (statut, expiration, connexions) via `player_api.php` sans action — username/password jamais renvoyés au navigateur.
- `XTREAM_URL` tolérant : ajoute `http://`, retire `/player_api.php`, `/get.php` ; détecte les valeurs d'exemple non remplacées.
- `XTREAM_USER_AGENT` configurable ; messages d'erreur explicites (401/403 identifiants, 509/403 limite de connexions).
- `playlists.json.bak` (copie de la version précédente, relue automatiquement si le fichier principal est corrompu).
- Mode démo `python -m tools.demo` et scénario navigateur `tests/e2e_browser.mjs`.

### Sécurité
- CSP stricte **sans `unsafe-inline`** (JS/CSS externalisés), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `img-src http: https:` pour les logos.
- CSRF : en-tête `X-Requested-With: XtreamM3ULocal` obligatoire (requête non simple → pré-vol CORS jamais accepté) + contrôle `Origin`/`Host`.
- Basic Auth à temps constant (`hmac.compare_digest` sur octets) ; `ACCESS_TOKEN` transmis à l'interface uniquement si `WEB_PASSWORD` est défini.
- Journaux : chemin seulement (jamais la query string ni les identifiants) ; vérifié par test.
- Délais d'inactivité des connexions (120 s port 7777, 60 s port 7778) : un lecteur en pause ne bloque pas indéfiniment la connexion fournisseur.
- Fichiers de `/data` en 0644 (aucun secret dedans).

### Décisions techniques
- Pas de dépendance externe (Python stdlib, pas de framework JS) : image légère (~50 Mo), surface d'attaque réduite.
- Compose : volume nommé remplacé par un **montage de dossier** `${DATA_PATH:-./data}:/data` (visible, sauvegardable avec appdata). ⚠️ Si une V2 avait déjà tourné avec le volume `xtream-m3u-data`, migrer : `docker run --rm -v xtream-m3u-data:/from -v "$PWD/data":/to alpine cp -a /from/. /to/`.
- `ENABLE_EPG` passe à `true` par défaut (l'utilisateur veut les métadonnées EPG) ; sans EPG fournisseur, `/p/x.xml` renvoie 502 et les lecteurs l'ignorent.
- Ordre des groupes = ordre fournisseur (comme les applications Xtream) plutôt que l'ordre d'apparition dans `get_live_streams`.
- `HEAD /stream/<id>.ts` ne contacte pas le fournisseur (préserve la connexion unique).
- Interface : 3 onglets (Catégories, Chaînes, Résumé) + 2 vues (Mes playlists, Catalogue fournisseur) ; rendu par création de nœuds DOM (jamais d'`innerHTML` avec des données).

## 5. Configuration et déploiement

- Variables : voir le tableau du `README.md`. Obligatoires : `XTREAM_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD`.
- `compose.yaml` (+ `.env`), `compose.single.example.yaml` → `compose.single.yaml` (ignoré par Git), `compose.ghcr.yaml`, `docker-create.sh [--ghcr] [--replace]`, `unraid-template.xml` (PUID/PGID, TZ, EPG, cache, icône, TemplateURL).
- `PROMPT_HERMES_UNRAID.md` : prompt prêt à l'emploi pour qu'un agent (Hermès) installe et vérifie tout sur Tower (pool NVMe) avec des placeholders `<<XTREAM_URL>>`, `<<XTREAM_USERNAME>>`, `<<XTREAM_PASSWORD>>`.
- Workflow `.github/workflows/docker-publish.yml` : tests Python + `node --check` + recherche d'identifiants suspects sur chaque push/PR ; image multi-arch (amd64, arm64) **publiée uniquement** depuis la branche par défaut (`latest`, `sha-…`) ou un tag `v*` (`2.1.0`, `2.1`).

## 6. Tests et vérifications effectués (15/11/2026)

| Vérification | Résultat |
|---|---|
| `python -m unittest discover -s tests -t .` | **93 tests OK** en 2.3.1 (88 en 2.3.0, 81 en 2.2.1, 80 en 2.2.0, 65 en 2.1.0) — faux fournisseur local avec direct, films, séries, épisodes |
| `node --check app/static/app.js` | OK |
| Scénario Chromium `tests/e2e_browser.mjs` sur la démo (1 500 chaînes, sous-groupes) | **OK** : création, exclusion, ajout manuel, réordonnancement/renommage de groupe, M3U conforme à l'URL affichée, modification à URL constante, duplication, suppression, vue catalogue, recherche, export JSON, aucun défilement horizontal à 390 / 768 / 1366 / 1920 px, aucune erreur console |
| `docker build` (python:3.13-alpine) | OK |
| `docker run` avec dossier données appartenant à root + faux fournisseur | OK : droits abandonnés (UID 99), `/health` OK, interface 200, playlist créée par l'API, M3U et EPG filtré corrects, `playlists.json` en 99:100 |
| Persistance : `docker stop` + `rm` + nouveau `run` sur le même dossier | OK, playlist toujours servie |
| `docker stop` | immédiat (SIGTERM géré) |
| `HEALTHCHECK` | `healthy` |
| Identifiants dans les journaux du conteneur | aucun |
| `docker compose config` (3 fichiers), XML du template, `sh -n docker-create.sh` | OK |

Couverture des tests unitaires : M3U complet et personnalisé (métadonnées, injections, ordre), relais TS + Range + HEAD + limite de connexions, cache/refresh/repli, token, journaux sans secret, Host invalide, `PUBLIC_BASE_URL`, API Web (statiques, CSP, catalogue complet avec `parent_id`/positions, échec `get_live_categories`, export expurgé, statut/abonnement sans secret), CRUD + URL stable, critère d'acceptation A+C ± exceptions, plusieurs playlists + redémarrage, validation, CSRF/Origin/415/413/400, Basic Auth, base corrompue (.bak), écritures concurrentes, EPG (cache, gzip, invalide, périmé, filtré, désactivé), configuration (défauts, valeurs vides de Compose, normalisation d'URL, booléens, droits/abandon de privilèges).

## 7. Non testé / limites connues

- **Aucun essai avec le vrai fournisseur ni sur le vrai Unraid** (débit TS réel, taille réelle de l'EPG, comportement exact de `get_live_categories` du fournisseur, lecteurs Android TV/TiviMate/VLC réels).
- **Publication GitHub/GHCR (vérifiée le 15/11/2026)** : code poussé sur `claude/wizardly-keller-bd38ld` de `rachid598/xtream-m3u-local`. Le dépôt étant vide, cette branche est devenue la **branche par défaut**. Le workflow Actions (run n° 1) a réussi : tests OK puis image amd64+arm64 publiée sous `ghcr.io/rachid598/xtream-m3u-local:latest` (+ `sha-279f857`). Le manifeste se résout sans authentification ; le `docker pull` complet n'a pas pu être testé depuis l'environnement de l'IA (réseau bloqué vers `pkg-containers.githubusercontent.com`) → **à confirmer sur Tower**. Si le pull demande une authentification : rendre le package public (GitHub → Packages → xtream-m3u-local → Package settings → Change visibility).
- Les liens `TemplateURL` / `Icon` du template Unraid et la variable `BRANCH=main` du prompt Hermès supposent une branche `main` : renommer la branche par défaut en `main` (GitHub → Settings → Branches → renommer), ou adapter ces liens.
- Le relais local est **TS uniquement** (pas de HLS proxifié ; le mode direct HLS expose les identifiants).
- Pas de renommage/réordonnancement **élément par élément** (seulement les groupes) ; pas de favoris.
- Films/séries : non testés avec le vrai fournisseur ni dans TiviMate/VLC réels. Pas de sous-titres externes ni de métadonnées détaillées (synopsis, casting) dans le M3U (le format ne les porte pas). Pas de « catch-up » (replay) du direct.
- Protection contre le *DNS rebinding* non implémentée (l'API ne contient aucun secret ; `WEB_PASSWORD` recommandé sur un réseau partagé).
- L'EPG complet `/epg.xml` est servi tel quel (XMLTV seul, peut faire des centaines de Mo) ; les playlists personnalisées utilisent l'EPG filtré et complété par l'API.
- Pas d'EPG pour les films/séries (sans objet) ; pas de catch-up.

## 8. Prochaines étapes prioritaires

1. Déployer sur Tower avec `PROMPT_HERMES_UNRAID.md` (ou à la main) ; noter versions, chemin NVMe, résultats.
2. Comparer `http://IP_UNRAID:7778/api/catalog?refresh=1` (ou la vue Catalogue / l'export JSON) avec les groupes de l'application TV :
   - mêmes groupes que le JSON brut mais pas dans le projet → corriger l'importation ;
   - projet = JSON mais TV différente → filtres/cache de l'application TV.
3. Tester 2–3 chaînes et une playlist filtrée dans VLC, TiviMate et le lecteur Android TV ; vérifier logos, groupes, EPG ; noter les erreurs exactes.
3 bis. Tester la VOD : une playlist avec 1 catégorie de films et 1 catégorie de séries ; vérifier l'affichage dans les sections Films/Séries du lecteur (ou en groupes), la lecture, l'avance rapide ; si le lecteur ne range pas la VOD correctement, essayer des URL séparées `?content=live` et `?content=vod,series`.
4. Renommer la branche par défaut en `main`, vérifier que le package GHCR est public (ou documenter `docker login`), puis tester `compose.ghcr.yaml` / le template Unraid.
5. Évolutions possibles : renommage et ordre des chaînes, favoris, numérotation personnalisée (`tvg-chno`), catch-up/replay, filtres VOD (année, note, ajout récent via `added`), proxy HLS, option `ALLOWED_HOSTS` anti-rebinding, statistiques de lecture, sauvegarde/restauration des playlists depuis l'interface, historique des exports catalogue avec comparaison (diff).
6. Mettre à jour CE fichier après chaque étape (commits, digest d'image, essais réels, TODO, blocages).

## 9. Sécurité et précautions

- Les identifiants Xtream ont été partagés dans une conversation antérieure : **ne pas les réutiliser, ne pas les écrire dans le dépôt** ; les faire renouveler si possible.
- Ne pas exposer 7777/7778 sur Internet ; si nécessaire, reverse proxy HTTPS + authentification, `WEB_PASSWORD` et `ACCESS_TOKEN`.
- Playlists publiques sur le LAN si `ACCESS_TOKEN` est vide ; `WEB_PASSWORD` ne protège que le port 7778.
- La limite de connexions simultanées de l'abonnement s'applique aussi au relais.

## 10. Critères d'acceptation

- [x] Seules `XTREAM_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD` sont nécessaires ; `http://IP_UNRAID:7778/` fonctionne (vérifié en conteneur).
- [x] Catégories A et C cochées + inclusions − exclusions → `/p/salon.m3u` contient exactement ces chaînes (test unitaire + navigateur).
- [x] Modification d'une playlist → même URL, contenu mis à jour (tests unitaire + navigateur).
- [x] Redémarrage / recréation du conteneur sans perte (test conteneur réel).
- [x] M3U proxy sans identifiants fournisseur (tests).
- [x] Catalogue fournisseur préservé (ordre, `parent_id`, catégories vides) consultable et exportable (tests + navigateur).
- [x] Tests verts, documentation alignée sur le comportement.
- [x] Films et séries sélectionnables et présents dans l'URL M3U permanente, relais `/movie/` et `/series/` avec Range (tests + navigateur + conteneur).
- [x] Direct validé en réel par l'utilisateur sur Unraid (2.1.0).
- [ ] Validation de la VOD sur le vrai fournisseur et les vrais lecteurs.
- [x] Image GHCR publiée par le workflow (run n° 1 réussi) — [ ] `docker pull` à confirmer sur Tower.
