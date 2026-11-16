# Changelog

Format inspiré de « Keep a Changelog ». Les versions suivent le versionnage sémantique.

## [3.0.0] — 2026-11-16

### Ajouté
- **Serveur Xtream Codes local** (`player_api.php`, `get.php`, `xmltv.php`, `/live|movie|series/<u>/<p>/…`, `/timeshift/…`) : chaque appareil voit sa playlist dans les rubriques natives du lecteur.
- **Appareils** : un code d'accès par TV, identifiants Xtream locaux, option « exiger un code » sur le port 7777, vue « Appareils ».
- **Relais HLS** : manifestes réécrits (variantes, pistes audio, clés, segment d'initialisation), segments relayés via des adresses opaques ; format TS/HLS par playlist et `?format=m3u8`.
- **Limite de connexions** (`MAX_CONNECTIONS`, `STREAM_TAKEOVER`) : reprise du flux du même appareil ou du plus ancien, délai anti-boucle de 30 s, lectures en cours avec bouton « Arrêter ».
- **Replay (catch-up)** : attributs `catchup` dans le M3U, relais `/timeshift` dans le fuseau du fournisseur (`PROVIDER_TIMEZONE`).
- **Personnalisation** : règles automatiques de catégories, doublons SD/HD/FHD/4K masqués, favoris (groupe en tête), renommage, numéros de chaînes et numérotation automatique.
- **Vue Système** : abonnement, diagnostic en un clic, sauvegarde / restauration ; bandeau d'expiration de l'abonnement.
- **Plusieurs abonnements** (`XTREAM_URL_2…_9`) : catalogues fusionnés, flux routés, limites et comptes par fournisseur.
- **Compression gzip** et **ETag/304** pour les playlists, guides et l'API Web.
- CI : tests et construction de l'image en parallèle, promotion `latest`/version sans reconstruction ; Dependabot.

### Modifié
- `/p/<slug>.m3u` et le guide utilisent un moteur de sélection commun (`app/builder.py`) partagé avec le mode Xtream.
- Les liens générés reprennent le code (`?token=`) de la requête (ACCESS_TOKEN ou code d'appareil).
- Démo : flux factices pour tous les contenus, guide réaliste, replay.

## [2.3.1] — 2026-11-16
- `/xmltv.php` (adresse EPG « à la Xtream ») ; guide complet complété par l'API pour les chaînes des playlists.

## [2.3.0] — 2026-11-16
- Guide EPG complété par l'API (`get_simple_data_table`), bloc « Guide des programmes », `EPG_SOURCE`, `EPG_URL`.

## [2.2.1] — 2026-11-15
- Éditeur films/séries mis en évidence (tuiles « Contenu de la playlist »), `/api/catalog/summary`.

## [2.2.0] — 2026-11-15
- Films (VOD) et séries dans les playlists, relais `/movie` et `/series` avec Range, cache des épisodes.

## [2.1.0] — 2026-11-15
- Audit de la V2, nouvelle interface, catalogue fournisseur, EPG filtré, Docker/Unraid, GHCR.
