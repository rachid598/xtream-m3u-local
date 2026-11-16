# Xtream M3U Local

Conteneur Docker pour Unraid : **une API Xtream fonctionnelle suffit**, même si le fournisseur ne renvoie rien sur `get.php`.
Le service lit `player_api.php` — TV en direct, films, séries, guide des programmes — conserve le catalogue d'origine du
fournisseur et le redistribue sur ton réseau local, **sans jamais exposer tes identifiants** :

- des **playlists M3U personnalisées à URL permanente** (VLC, TiviMate, lecteurs Android TV…) ;
- un **serveur Xtream Codes local** : chaque TV se connecte en mode « Xtream » avec ses propres identifiants et retrouve
  les rubriques natives Direct / Films / Séries, le guide et le replay, limités à la playlist choisie.

| Port | Rôle | Exemples |
|---|---|---|
| **7777** | Lecteurs IPTV | `http://IP_UNRAID:7777/p/salon.m3u` · `/p/salon.xml` · `player_api.php` · `/stream/<id>.ts` · `/hls/<id>.m3u8` |
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
2. **Mes playlists → + Nouvelle** : nomme la playlist (« Salon »), choisis le contenu (tuiles **TV en direct / Films / Séries**), coche des catégories, affine élément par élément, **Créer la playlist**.
3. **Appareils → Ajouter** : « TV du salon » avec la playlist « Salon ». L'interface affiche, prêts à copier :
   - **mode « Playlist M3U »** : URL de la playlist et du guide ;
   - **mode « Xtream Codes »** : serveur `http://IP_UNRAID:7777`, identifiant, mot de passe.
4. Saisis l'un des deux dans le lecteur. Une modification de la playlist dans l'interface est prise en compte au prochain rechargement, **sans changer d'URL**.

Les données (`playlists.json`, `devices.json`, caches EPG et séries) sont dans `./data` (ou `DATA_PATH`), monté sur `/data`.
Elles survivent aux redémarrages, mises à jour et recréations du conteneur.

### Variantes d'installation

- **Un seul fichier à modifier** : `cp compose.single.example.yaml compose.single.yaml`, remplir les 3 valeurs `XTREAM_*`, puis `docker compose -f compose.single.yaml up -d --build` (fichier ignoré par Git).
- **Sans Compose** : `sh docker-create.sh` (build local), `sh docker-create.sh --ghcr` (image GHCR), `--replace` pour recréer en gardant les données.
- **Image GHCR** (construite par GitHub Actions) : `docker compose -f compose.ghcr.yaml up -d`. Si le package est privé : `docker login ghcr.io` avec un jeton GitHub `read:packages`, ou rendre le package public.
- **Template Unraid** : copier `unraid-template.xml` dans `/boot/config/plugins/dockerMan/templates-user/my-xtream-m3u-local.xml`, puis Docker → Add Container.
- **Installation guidée par un agent** : voir `PROMPT_HERMES_UNRAID.md`.

## Interface Web (port 7778)

Entièrement en français, responsive (téléphone, tablette, ordinateur), thème clair/sombre automatique. Quatre vues :

**Mes playlists**
- tuiles **TV en direct / Films / Séries** (disponible chez le fournisseur / sélectionné) et bandeau « Ajouter des films / séries » ;
- catégories avec compteurs, filtres, tout cocher / décocher ; **règles automatiques** (« le nom commence par FR | »…) qui ajoutent aussi les futures catégories ;
- recherche (sans accents, plusieurs mots, année), ajout ou exclusion individuelle, actions groupées, affiches, note, bouton « Voir les épisodes » ;
- **☆ favoris** (groupe en tête de la playlist), **✎ renommage et numéro de chaîne** (zapping à la télécommande et ordre), **numérotation automatique** ;
- **📁 Mes groupes de chaînes** (onglet « 3. Résumé ») : crée tes propres groupes du direct — « TNT », « Canal+ », « Sport »… — placés **en tête de la playlist**. Modèles prêts (**Bouquet Canal+** : toutes les chaînes de l'offre Canal+ rangées par thème comme sur le décodeur ; TNT dans l'ordre des numéros ; Chaînes Canal+ ; Sport, Info, Cinéma, Jeunesse, Documentaires) ou groupe vide « + Nouveau groupe en premier ». Un groupe se remplit tout seul avec les chaînes retenues dont le nom correspond (« contient » ou « est exactement », sans tenir compte du pays « FR: », des accents ni de HD/FHD/4K), et le bouton **📁** d'une chaîne la range à la main dans un groupe (ou la laisse dans son groupe d'origine). Écrire « 4: Canal+ » donne aussi le numéro 4 à la chaîne (comme sur un décodeur). Une chaîne rangée quitte son groupe d'origine ; si elle correspond à plusieurs groupes, elle va dans le plus haut ;
- résumé : ordre et noms des groupes, ajouts, exclusions, favoris, personnalisations ;
- options : format **MPEG-TS ou HLS**, **replay**, **masquer les doublons SD/HD/FHD/4K**, **masquer les séparateurs** (fausses chaînes « #### PRIME #### » du fournisseur), nom du groupe des favoris ;
- bloc **Guide des programmes (EPG)** : URL, couverture, chaînes sans programme, actualisation.

**Catalogue fournisseur** (lecture seule) : ordre exact, identifiants, `parent_id`, chaînes / films / séries, recherche, **export JSON** (identifiants masqués).

**Appareils** : un code par TV ou téléphone, identifiants Xtream locaux, playlist associée, nouveau code, suppression ; option **« exiger un code d'appareil »** pour le port 7777.

**Système** : abonnement (statut, expiration, connexions, fuseau), **lectures en cours** avec bouton « Arrêter », **diagnostic en un clic**, **sauvegarde / restauration**. Un bandeau prévient quand l'abonnement expire dans moins de 14 jours.

## Mode Xtream Codes local

Dans le lecteur (TiviMate, IPTV Smarters…), choisir « Xtream Codes » et saisir l'adresse `http://IP_UNRAID:7777` avec l'identifiant et le mot de passe d'un appareil (vue Appareils).

- `player_api.php` : compte, catégories et listes (direct, films, séries), fiches détaillées (`get_vod_info`, `get_series_info`), guide court ;
- lecture : `/live|movie|series/<identifiant>/<code>/<id>.<ext>`, replay `/timeshift/…`, `get.php` et `xmltv.php` avec identifiants ;
- l'appareil ne voit que sa playlist (noms, numéros, ordre, groupes perso et doublons compris) ; sans playlist associée, tout le catalogue.

## Playlists M3U

- **TV en direct** → `/stream/<id>.ts` (ou `/hls/<id>.m3u8` en HLS) ; **films** → `/movie/<id>.<ext>` ; **épisodes** → `/series/<id>.<ext>`, nommés `Série S01 E02 - Titre` ;
- en-tête `url-tvg` / `x-tvg-url` → guide de la playlist ; attributs `tvg-id`, `tvg-name`, `tvg-logo`, `tvg-chno`, `group-title` ;
- **replay** : `catchup="default" catchup-days="N" catchup-source="…/timeshift/<id>.ts?start={utc}&end={utcend}"` sur les chaînes qui le proposent ;
- `?content=live|vod|series` (combinables), `?format=m3u8|ts` sur la même URL ; réponses compressées (gzip) et ETag/304 ;
- règle de sélection : (catégorie cochée **ou** ajoutée par une règle **ou** élément ajouté) **et** élément non exclu.

## Relais HLS

Option par playlist (ou `?format=m3u8`) : le serveur télécharge le manifeste HLS du fournisseur et **réécrit chaque lien** (segments, variantes de qualité, pistes audio, clés de chiffrement, segment d'initialisation) vers une adresse locale opaque `/hls/s/<code>.ts`. Le lecteur ne voit jamais l'URL du fournisseur. Utile si le flux MPEG-TS se fige ou coupe souvent.

## Limite de connexions

Un abonnement autorise souvent une seule connexion. Le serveur compte les lectures (direct, HLS, films, épisodes, replay) par fournisseur :

- limite = valeur de l'abonnement (`MAX_CONNECTIONS=auto`), un nombre, ou `0` (illimité) ;
- limite atteinte : le flux **du même appareil** est remplacé (zapping), sinon le plus ancien est coupé (`STREAM_TAKEOVER=true`) ou la nouvelle lecture est refusée (`false`) ;
- l'appareil coupé ne peut pas reprendre **le même** flux pendant 30 s (sinon les deux TV se couperaient en boucle) ;
- vue **Système** : lectures en cours, débit transmis, bouton « Arrêter ».

## Guide des programmes (EPG)

- `EPG_SOURCE=auto` (défaut) : programmes de `xmltv.php` (ou `EPG_URL`), complétés **chaîne par chaîne par l'API Xtream** (`get_simple_data_table`) pour les chaînes absentes — ou toutes si `xmltv.php` ne fonctionne pas ; cache 6 h, préchargement à l'enregistrement ;
- `/p/<nom>.xml` : guide de la playlist (léger) ; `/epg.xml` ou **`/xmltv.php`** : guide de toutes les chaînes, complété pour les chaînes des playlists (au plus `EPG_API_MAX_CHANNELS`) ;
- chaînes sans identifiant EPG fournisseur : `tvg-id="xtream-<id>"`.

## Plusieurs abonnements

Ajouter `XTREAM_URL_2`, `XTREAM_USERNAME_2`, `XTREAM_PASSWORD_2` (et `XTREAM_NAME_2`), jusqu'à `_9`. Les catalogues sont fusionnés (une catégorie de même nom forme un seul groupe dans les lecteurs) ; chaque flux est relayé vers le bon fournisseur, avec sa limite de connexions et son fuseau. Les identifiants des fournisseurs 2 à 9 sont décalés (`k × 10¹¹ + id`) pour rester numériques et uniques.

## Adresses du port 7777

| Adresse | Comportement |
|---|---|
| `/p/<nom>.m3u` | Playlist personnalisée (URL permanente) |
| `/p/<nom>.xml` | Guide de la playlist |
| `/player_api.php` | Serveur Xtream local (identifiants d'appareil) |
| `/get.php?username=…&password=…` | Playlist de l'appareil ; sans identifiants : playlist complète |
| `/xmltv.php` · `/epg.xml` | Guide complet (avec identifiants d'appareil : guide de sa playlist) |
| `/playlist.m3u` | Toutes les chaînes ; `?content=vod` films ; `?format=m3u8` HLS ; `?mode=direct` liens fournisseur (**identifiants visibles**) |
| `/stream/<id>.ts` · `/hls/<id>.m3u8` | Direct (MPEG-TS / HLS) |
| `/movie/<id>.<ext>` · `/series/<id>.<ext>` | Film / épisode (Range transmis : avance rapide) |
| `/timeshift/<id>.ts?start=<epoch>&end=<epoch>` | Replay |
| `/health` | Sonde de santé |

`?token=` : `ACCESS_TOKEN` ou code d'un appareil (exigé si `ACCESS_TOKEN` est défini ou si l'option « exiger un code » est active).

## Variables d'environnement

| Variable | Défaut | Description |
|---|---|---|
| `XTREAM_URL` | **obligatoire** | Adresse de base du fournisseur, ex. `http://serveur:8080` (un `…/player_api.php` collé est accepté) |
| `XTREAM_USERNAME` / `XTREAM_PASSWORD` | **obligatoires** | Identifiants fournisseur |
| `XTREAM_NAME` | vide | Nom affiché du fournisseur principal |
| `XTREAM_URL_2…9`, `XTREAM_USERNAME_n`, `XTREAM_PASSWORD_n`, `XTREAM_NAME_n` | vide | Abonnements supplémentaires |
| `DATA_PATH` | `./data` | (Compose) dossier hôte monté sur `/data` |
| `WEB_PASSWORD` / `WEB_USERNAME` | vide / `admin` | Authentification HTTP Basic de l'interface (port 7778) |
| `ACCESS_TOKEN` | vide | Code global exigé sur le port 7777 |
| `PUBLIC_BASE_URL` | auto | Adresse écrite dans les playlists, ex. `http://192.168.1.10:7777` |
| `MAX_CONNECTIONS` | `auto` | Connexions simultanées : `auto` (abonnement), `0` (illimité) ou un nombre |
| `STREAM_TAKEOVER` | `true` | Limite atteinte : couper le flux le plus ancien (`true`) ou refuser (`false`) |
| `PROVIDER_TIMEZONE` | auto | Fuseau du serveur Xtream pour le replay (ex. `Europe/Paris`) |
| `ENABLE_EPG` | `true` | Guide des programmes |
| `EPG_SOURCE` | `auto` | `auto`, `xmltv` ou `api` |
| `EPG_URL` | vide | Guide XMLTV externe à la place de `xmltv.php` |
| `EPG_CACHE_HOURS` / `EPG_FETCH_SECONDS` / `EPG_API_MAX_CHANNELS` | `12` / `15` / `1000` | Cache du XMLTV, temps max de complément API par requête, chaînes max complétées dans le guide complet |
| `CACHE_SECONDS` | `1800` | Cache des catalogues |
| `SERIES_CACHE_HOURS` / `SERIES_FETCH_SECONDS` | `24` / `20` | Cache des épisodes, temps max de récupération par requête |
| `XTREAM_USER_AGENT` | `XtreamM3ULocal/3.x` | User-Agent envoyé au fournisseur |
| `PUID` / `PGID` | `99` / `100` | Propriétaire des fichiers de `/data` (nobody/users sur Unraid) |
| `TZ` | `Europe/Paris` | Fuseau des journaux |

## Vérification

Le plus simple : **Système → Lancer le diagnostic** (API, catalogues, guide, flux TS et HLS, film, épisode, replay ; tests de flux ignorés si une lecture est en cours). En ligne de commande :

```bash
curl -i http://IP_UNRAID:7777/health
curl -s http://IP_UNRAID:7777/p/salon.m3u | head -n 5
curl -s "http://IP_UNRAID:7777/player_api.php?username=tv-du-salon&password=CODE" | head -c 300
```

## Sécurité et limites

- **Réseau local uniquement.** Ne redirige pas 7777/7778 vers Internet. Sur un réseau partagé : `WEB_PASSWORD` (gestion) et l'option « exiger un code d'appareil » (lecteurs).
- Les identifiants Xtream restent dans le conteneur : absents des playlists (hors `mode=direct`), des manifestes HLS, de l'API Web, des exports et sauvegardes, des journaux et du diagnostic.
- `devices.json` contient les codes d'appareils (droits 0600). Protection CSRF de l'API, CSP stricte sans script inline, validation de toutes les entrées, écritures atomiques avec copie `.bak`.
- Démarrage en root uniquement pour corriger les droits de `/data`, puis UID 99 / GID 100.
- Pas de transcodage : chaque lecture utilise une connexion chez le fournisseur.

## Développement

```bash
python -m unittest discover -s tests -t . -v      # tests (faux fournisseurs Xtream locaux)
python -m tools.demo                               # démo sans abonnement : chaînes, films, séries, guide, flux factices
node tests/e2e_browser.mjs http://127.0.0.1:7778   # scénario navigateur complet (Playwright pour Node)
```

Python 3.13, bibliothèque standard uniquement ; interface HTML/CSS/JS sans framework ni CDN. GitHub Actions : tests et image multi-architecture en parallèle, étiquettes `latest` / version ajoutées seulement si les tests passent ; Dependabot hebdomadaire.
Historique : `CHANGELOG.md`. Architecture, décisions et état : `PASSATION_IA.md`.

## Licence

MIT.
