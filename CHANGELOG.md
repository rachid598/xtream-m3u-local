# Changelog

Format inspiré de « Keep a Changelog ». Les versions suivent le versionnage sémantique.

## [3.3.0] — 2026-11-16

### Ajouté
- **Bascule automatique** (option de playlist `failover`) : les doublons masqués deviennent les versions de secours d'une chaîne (meilleure qualité d'abord). Coupure, flux qui ne démarre pas, silence (`FAILOVER_STALL_SECONDS`, 6 s) ou débit trop faible (`FAILOVER_SLOW_PERCENT`, 35 %) → version suivante enchaînée dans le même flux, une seule connexion fournisseur à la fois. M3U (`/stream/<id>.ts?alt=…`) et mode Xtream. Vue Système : « → secours n/N ».

### Corrigé
- Badge « x/y connexion » : le compteur du fournisseur n'était lu qu'au chargement de la page (souvent 0 pendant une lecture) ; il prend maintenant le plus grand avec les lectures en cours sur le serveur et se met à jour avec elles.
- Mobile : la pastille des options ne déborde plus.

## [3.2.1] — 2026-11-16

### Corrigé
- **Doublons non masqués** chez les fournisseurs qui écrivent la qualité en exposant (« VIP: TF1 ᴴᴰ », « ᴿᴬᵂ ») ou préfixent par VIP/FR/AF : les versions d'une même chaîne sont maintenant reconnues (nom comparable des groupes perso, normalisation NFKD) et seule la meilleure est gardée.
- Test multi-fournisseurs instable (rafraîchissement du compte en arrière-plan).

## [3.2.0] — 2026-11-16

### Ajouté
- Modèle **« Bouquet Canal+ »** : environ 190 chaînes de l'offre Canal+ rangées par thème comme sur le décodeur (TNT et généralistes, chaînes Canal+, cinéma, sport, séries, découverte, jeunesse, info, musique, Canal+ Live 1 à 19). Liste établie sans le plan de service officiel (inaccessible depuis l'environnement de développement) : numéros 1 à 7 seulement.
- **Numéros de chaînes dans les groupes** : « 4: Canal+ » range la chaîne et lui donne le numéro 4 (`tvg-chno`, num Xtream), sauf numéro personnalisé ✎.

### Modifié
- Le modèle « Canal+ » (noms contenant Canal+) s'appelle « Chaînes Canal+ ».

## [3.1.0] — 2026-11-16

### Ajouté
- **Mes groupes de chaînes** : groupes personnalisés du direct (« TNT », « Canal+ »…) placés en tête de la playlist, dans le M3U, le guide et le mode Xtream.
  - Remplissage automatique par noms de chaînes (`custom_groups[].patterns`, correspondance « contient » ou « exacte ») insensible au préfixe de pays, aux accents et à la qualité ; ordre du groupe = ordre des noms.
  - Rangement manuel par chaîne avec 📁 (`item_groups`), y compris « rester dans le groupe d'origine ».
  - Modèles : TNT, Canal+, Sport, Info, Cinéma, Jeunesse, Documentaires ; bouton « + Nouveau groupe en premier ».
  - Les doublons SD/HD/4K sont dédoublonnés à l'intérieur des groupes perso ; filtre « Rangées dans mes groupes ».
- Démo : quelques chaînes françaises réelles (TF1, France 2, Canal+…) pour essayer les modèles.

## [3.0.1] — 2026-11-16

Retour du premier diagnostic réel (47 408 chaînes, `xmltv.php` du fournisseur en erreur HTTP 502).

### Ajouté
- Option **« Masquer les séparateurs »** par playlist (`hide_separators`) : retire les fausses chaînes de titre de section du fournisseur (« #### PRIME ##### », « ===== FR ===== »…) du M3U, du guide et du mode Xtream ; badge « Séparateur masqué » dans l'éditeur.

### Corrigé
- **Diagnostic** : la chaîne testée n'est plus un séparateur ; le guide via l'API est testé sur 5 chaînes réparties dans le catalogue (« X/5 chaînes ont des programmes ») ; une erreur de `xmltv.php` passe en avertissement quand l'API prend le relais (mode auto).

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
