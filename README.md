# Xtream M3U Local

Conteneur Docker pour Unraid : **une API Xtream fonctionnelle suffit**, même si le fournisseur ne renvoie rien sur `get.php`.
Le service lit `player_api.php` — **TV en direct** (`get_live_*`), **films / VOD** (`get_vod_*`) et **séries** (`get_series*`, épisodes via `get_series_info`) —, conserve le catalogue d'origine du fournisseur et publie sur ton réseau local des **playlists M3U personnalisées à URL permanente**, compatibles VLC, TiviMate et lecteurs Android TV.

| Port | Rôle | Exemples |
|---|---|---|
| **7777** | Lecteurs IPTV | `http://IP_UNRAID:7777/p/salon.m3u` · `/playlist.m3u` · `/stream/<id>.ts` · `/movie/<id>.mkv` · `/series/<id>.mkv` · `/p/salon.xml` (EPG) |
| **7778** | Interface Web de gestion | `http://IP_UNRAID:7778/` |

Aucun abonnement n'est fourni : utilise uniquement des flux pour lesquels tu disposes des droits.

## Démarrage rapide

```bash
git clone https://github.com/rachid598/xtream-m3u-local.git
cd xtream-m3u-local
cp .env.example .env      # puis renseigner XTREAM_URL, XTREAM_USERNAME, XTREAM_PASSWORD
docker compose up -d --build
```

1. Ouvre `http://IP_UNRAID:7778/`.
2. **+ Nouvelle** → nomme la playlist (« Salon »), coche des catégories dans **TV en direct**, **Films** et/ou **Séries**, affine élément par élément si besoin, **Créer la playlist**.
3. Copie l'URL affichée (`http://IP_UNRAID:7777/p/salon.m3u`) dans VLC, TiviMate ou ton lecteur Android TV.
4. Pour modifier la playlist : reviens sur l'interface, change la sélection, **Enregistrer**. L'URL ne change pas ; actualise simplement la playlist dans le lecteur.

Les données (`playlists.json` + cache EPG) sont écrites dans `./data` (ou `DATA_PATH`), monté sur `/data` dans le conteneur. Elles survivent aux redémarrages, mises à jour et recréations du conteneur.

### Variantes d'installation

- **Un seul fichier à modifier** : `cp compose.single.example.yaml compose.single.yaml`, remplir les 3 valeurs `XTREAM_*`, puis `docker compose -f compose.single.yaml up -d --build` (fichier ignoré par Git).
- **Sans Compose** : `sh docker-create.sh` (build local), `sh docker-create.sh --ghcr` (image GHCR), `--replace` pour recréer en gardant les données.
- **Image GHCR** (une fois publiée par GitHub Actions) : `docker compose -f compose.ghcr.yaml up -d`. Si le package est privé, faire d'abord `docker login ghcr.io` avec un jeton GitHub `read:packages`, ou rendre le package public dans GitHub → Packages → Package settings.
- **Template Unraid** : copier `unraid-template.xml` dans `/boot/config/plugins/dockerMan/templates-user/my-xtream-m3u-local.xml`, puis Docker → Add Container → choisir le template.

## Interface Web (port 7778)

Deux vues, entièrement en français, responsives (téléphone, tablette, ordinateur), thème clair/sombre automatique :

**Mes playlists**
- bloc **« Contenu de la playlist »** avec trois grandes tuiles **TV en direct / Films / Séries** (nombre disponible chez le fournisseur et nombre sélectionné) : clique sur une tuile pour éditer ce type ; un bandeau signale les films et séries de l'abonnement pas encore ajoutés (« Ajouter des films », « Ajouter des séries ») ;
- chaque type a ses catégories, sa recherche, ses ajouts et exclusions ; une même playlist peut mélanger les trois ;
- plusieurs playlists nommées (« Salon », « Chambre », « TV française »…) : créer, modifier, dupliquer, supprimer ;
- toutes les catégories avec leur nombre de chaînes (y compris les vides et les sous-groupes), filtre, tout cocher / décocher ;
- recherche (sans accents, plusieurs mots, année pour les films), filtre par catégorie et par état, ajout ou exclusion individuelle, actions groupées sur les résultats ; affiches, année et note des films/séries, bouton « Voir les épisodes » ;
- onglet **Résumé** : ordre des groupes (↑ ↓) et noms de groupes propres à chaque playlist, liste des ajouts et exclusions ;
- URL permanente avec boutons Copier / Ouvrir, alerte en cas de modifications non enregistrées, raccourci Ctrl+S.

**Catalogue fournisseur** (lecture seule, pour le direct, les films et les séries)
- catégories dans l'**ordre exact** de `get_live_categories` / `get_vod_categories` / `get_series_categories`, avec identifiants, `parent_id` (sous-groupes imbriqués), nombre de chaînes et chaînes associées ;
- signale les groupes absents de `get_live_categories` et l'échec éventuel de cet appel ;
- **Exporter le catalogue fournisseur (JSON)** : réponses brutes de `player_api.php`, identifiants remplacés par `***`, pour archiver ou comparer la structure.

> L'API Xtream n'expose que les catégories et chaînes autorisées pour ton compte, pas la configuration administrative du fournisseur.

## Guide des programmes (EPG)

- Chaque playlist annonce son guide dans l'en-tête M3U (`url-tvg` / `x-tvg-url` → `http://IP_UNRAID:7777/p/<nom>.xml`) : TiviMate, VLC et la plupart des lecteurs le chargent automatiquement. Si un lecteur demande une « source EPG » séparée, utiliser cette même adresse.
- **Mode `auto` (par défaut)** : programmes du guide XMLTV du fournisseur (`xmltv.php`, ou `EPG_URL`) ; les chaînes qui n'y figurent pas — ou toutes, si `xmltv.php` ne fonctionne pas, comme `get.php` chez certains fournisseurs — sont complétées par l'API Xtream chaîne par chaîne (`get_simple_data_table`), avec cache disque (6 h) et préchargement à l'enregistrement de la playlist.
- Les chaînes sans identifiant EPG fournisseur reçoivent `tvg-id="xtream-<id>"` pour être reliées au guide complété.
- **Champ « EPG » d'un lecteur** : `http://IP_UNRAID:7777/xmltv.php` (adresse à la Xtream, paramètres `username`/`password` ignorés) ou `/epg.xml` = guide de toutes les chaînes ; il est complété par l'API pour les chaînes présentes dans tes playlists personnalisées (au plus `EPG_API_MAX_CHANNELS`). Pour une playlist personnalisée, préférer `http://IP_UNRAID:7777/p/<nom>.xml`.
- Dans l'interface, le bloc **« Guide des programmes (EPG) »** de chaque playlist affiche l'URL à copier, la couverture (chaînes avec programmes, via xmltv.php / via l'API), la liste des chaînes sans programme et un bouton **Actualiser le guide**.

## Contenu des playlists

- **TV en direct** → `/stream/<id>.ts` ; **films** → `/movie/<id>.<extension>` ; **épisodes** → `/series/<id>.<extension>`, nommés `Série S01 E02 - Titre` (format reconnu par TiviMate, Smarters…), avec l'affiche de la série et le groupe de sa catégorie ;
- films et épisodes sont relayés avec l'en-tête `Range` (avance rapide, reprise) ; aucun transcodage ;
- `?content=live`, `?content=vod`, `?content=series` (ou `live,vod`) sur `/p/<nom>.m3u` : même playlist, limitée à certains types — pratique pour déclarer séparément le direct et la VOD dans un lecteur ;

- `#EXTM3U url-tvg="…" x-tvg-url="…"` : guide EPG (complet pour `/playlist.m3u`, **filtré** pour `/p/<nom>.m3u` → `/p/<nom>.xml`) ;
- chaque chaîne : `tvg-id` (EPG), `tvg-name`, `tvg-logo`, `tvg-chno`, `group-title` ;
- groupes dans l'ordre du fournisseur (ou l'ordre personnalisé de la playlist), chaînes dans l'ordre du fournisseur ;
- règle de sélection : (catégorie cochée **ou** chaîne ajoutée) **et** chaîne non exclue. Une chaîne présente dans plusieurs catégories est rangée dans la première catégorie cochée ;
- les nouvelles chaînes ajoutées par le fournisseur dans une catégorie cochée apparaissent automatiquement.

## Adresses du port 7777

| Adresse | Comportement |
|---|---|
| `/p/<nom>.m3u` | Playlist personnalisée, URL stable et dynamique (**recommandé**) |
| `/p/<nom>.xml` | Guide EPG limité aux chaînes de la playlist (xmltv.php + complément API) |
| `/playlist.m3u` | Toutes les chaînes, liens TS locaux, aucun identifiant fournisseur |
| `/playlist.m3u?content=vod` (ou `live,vod`) | Tous les films (les séries ne sont proposées qu'en playlist personnalisée : un appel API par série) |
| `/movie/<id>.<ext>` · `/series/<id>.<ext>` | Relais d'un film / d'un épisode (Range transmis) |
| `/playlist.m3u?mode=direct&format=ts\|m3u8` | Liens directs fournisseur — **contient tes identifiants** |
| `/get.php` | Alias de `/playlist.m3u` (ce n'est pas une API Xtream complète) |
| `/stream/<id>.ts` | Relais MPEG-TS sans transcodage |
| `/epg.xml` · `/xmltv.php` | Guide de toutes les chaînes (XMLTV fournisseur en cache, complété par l'API pour les chaînes des playlists personnalisées) |
| `/health` | Sonde de santé (utilisée par le `HEALTHCHECK` Docker) |
| `…?refresh=1` | Force la relecture du catalogue fournisseur |

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `XTREAM_URL` | **obligatoire** | Adresse de base du fournisseur, ex. `http://serveur:8080` (un `…/player_api.php` collé est accepté) |
| `XTREAM_USERNAME` | **obligatoire** | Identifiant fournisseur |
| `XTREAM_PASSWORD` | **obligatoire** | Mot de passe fournisseur |
| `DATA_PATH` | `./data` | (Compose) dossier hôte monté sur `/data` |
| `WEB_PASSWORD` | vide | Active l'authentification HTTP Basic sur le port 7778 |
| `WEB_USERNAME` | `admin` | Utilisateur de l'interface Web |
| `ACCESS_TOKEN` | vide | Exige `?token=…` sur le port 7777 (ajouté automatiquement aux liens générés) |
| `PUBLIC_BASE_URL` | auto | Adresse écrite dans les playlists, ex. `http://192.168.1.10:7777` |
| `ENABLE_EPG` | `true` | Guide XMLTV (`xmltv.php` du fournisseur) |
| `EPG_CACHE_HOURS` | `12` | Durée de cache du guide XMLTV sur disque |
| `EPG_SOURCE` | `auto` | `auto` (xmltv.php + API pour les chaînes manquantes), `xmltv` (xmltv.php seul), `api` (API seule) |
| `EPG_URL` | vide | Guide XMLTV externe (http(s), `.xml` ou `.xml.gz`) à utiliser à la place de `xmltv.php` |
| `EPG_API_MAX_CHANNELS` | `1000` | Nombre maximal de chaînes complétées par l'API dans le guide complet `/xmltv.php` |
| `EPG_FETCH_SECONDS` | `15` | Temps maximal passé à compléter le guide par l'API lors d'une demande de `/p/<nom>.xml` |
| `CACHE_SECONDS` | `1800` | Durée de cache des catalogues (direct, films, séries) en mémoire |
| `SERIES_CACHE_HOURS` | `24` | Durée de cache disque des épisodes (`/data/cache/series`) |
| `SERIES_FETCH_SECONDS` | `20` | Temps maximal passé à récupérer des épisodes manquants lors d'une demande de playlist |
| `XTREAM_USER_AGENT` | `XtreamM3ULocal/2.x` | User-Agent envoyé au fournisseur (si celui-ci filtre les applications) |
| `PUID` / `PGID` | `99` / `100` | Propriétaire des fichiers de `/data` (nobody/users sur Unraid) |
| `TZ` | `Europe/Paris` | Fuseau horaire des journaux |
| `PORT` / `WEB_PORT` / `DATA_DIR` | `7777` / `7778` / `/data` | Ports et dossier **dans** le conteneur (à ne pas changer en général) |

## Vérification

```bash
curl -i http://IP_UNRAID:7777/health
curl -s http://IP_UNRAID:7777/p/salon.m3u | head -n 5
curl -s http://IP_UNRAID:7778/api/catalog?refresh=1 | head -c 600
curl --max-time 5 -o /dev/null -w '%{http_code} %{size_download}\n' http://IP_UNRAID:7777/stream/12345.ts
```

Si les liens d'une playlist utilisent une mauvaise adresse, définir `PUBLIC_BASE_URL=http://IP_UNRAID:7777` puis recréer le conteneur.

## Sécurité et limites

- **Réseau local uniquement.** Ne redirige pas 7777/7778 vers Internet. Sur un réseau partagé : `WEB_PASSWORD` (gestion) et `ACCESS_TOKEN` (lecteurs).
- Les identifiants Xtream restent dans le conteneur : absents des playlists (mode proxy), de l'API Web, de l'export JSON et des journaux.
- Protection CSRF de l'API (en-tête dédié + contrôle d'`Origin`), CSP stricte sans script inline, validation de toutes les entrées, écritures atomiques avec copie `playlists.json.bak`.
- Le conteneur démarre en root uniquement pour corriger les droits de `/data`, puis tourne en UID 99 / GID 100.
- **Pas de transcodage** : chaque lecture ouvre une connexion chez le fournisseur ; la limite de connexions simultanées de l'abonnement s'applique (erreur 502 explicite si atteinte).
- Le relais du direct est MPEG-TS uniquement ; le HLS n'est disponible qu'en mode direct (identifiants visibles dans le M3U).
- **Séries** : Xtream ne donne les épisodes que série par série (`get_series_info`). Ils sont préchargés en arrière-plan dès l'enregistrement de la playlist et mis en cache 24 h sur disque. Avec beaucoup de séries, le premier chargement peut être incomplet (en-tête `X-Series-Pending`) : recharger la playlist dans le lecteur quelques minutes plus tard.
- Une grosse VOD (dizaines de milliers de films) produit un M3U volumineux : préférer des playlists ciblées par catégories.

## Développement

```bash
python -m unittest discover -s tests -t . -v      # 93 tests, faux fournisseur Xtream local
python -m tools.demo                               # démo sans abonnement : chaînes, films et séries fictifs
node tests/e2e_browser.mjs http://127.0.0.1:7778   # scénario navigateur (Playwright pour Node)
```

Python 3.13, bibliothèque standard uniquement ; interface HTML/CSS/JS sans framework ni CDN.
Voir `PASSATION_IA.md` pour l'architecture détaillée, les décisions et l'état du projet.

## Licence

MIT.
