# Changelog

Format inspiré de « Keep a Changelog ». Les versions suivent le versionnage sémantique.

## [3.8.2] — 2026-11-18

Essai réel chez un proche : « Code 502 » dans TiviMate, journaux du serveur : HTTP 429 du fournisseur (abonnement à une connexion déjà utilisé).

### Corrigé
- **Message « Abonnement occupé »** : quand le fournisseur refuse une connexion de plus (HTTP 429, 458, 509), le lecteur reçoit un HTTP 503 explicite avec `Retry-After`, au lieu d'un 502 qui faisait croire à une panne.
- **Plus de réessais en rafale** : après un tel refus, le serveur répond directement pendant 30 s (ou le `Retry-After` du fournisseur, borné à 10–120 s) sans redemander ce fournisseur, pour toutes ses chaînes ; la bascule automatique n'essaie pas les autres versions (même abonnement).

## [3.8.1] — 2026-11-18

Retour d'essai réel (TiviMate) : image figée à chaque bascule automatique, obligation de zapper ; 11 bascules en 12 min dont 8 « débit trop faible ».

### Corrigé
- **Bascule automatique sans image figée** : au lieu d'enchaîner la version de secours dans le même flux (autre codec, résolution et horloge : la plupart des lecteurs gardent la dernière image), le serveur ferme la connexion et mémorise la version qui marche ; le lecteur se reconnecte tout seul et la reçoit directement (≈ un zap automatique). Même principe pour le retour à la meilleure qualité. Ancien comportement : `FAILOVER_MODE=splice`.
- **Fausses alertes « débit trop faible »** : la référence était le meilleur débit observé, rafale de démarrage comprise (le fournisseur remplit le tampon du lecteur) ; le débit normal paraissait donc « trop lent » au bout d'une minute. Désormais : 25 premières secondes ignorées, référence = médiane des fenêtres récentes, 3 fenêtres lentes d'affilée (30 s) avant de basculer.
- **Historique sur téléphone** : dans « Dernières lectures », le nom de la chaîne s'affichait une lettre par ligne.

## [3.8.0] — 2026-11-17

### Ajouté
- **Logos haute qualité** (option de playlist « Logos haute qualité », cochée par défaut) : remplace les logos du fournisseur par des logos propres pour les chaînes **françaises, belges et marocaines** — PNG transparents ~512 px de tv-logo/tv-logos en priorité, puis la base iptv-org (noms alternatifs, Maroc). Index rafraîchi chaque semaine, images téléchargées une fois et gardées en cache dans `/data/cache/logos`, servies par `/logo/<clé>.png` (port 7777 pour les lecteurs, 7778 pour l'interface). Utilisés dans les playlists M3U, le serveur Xtream local, le Guide TV et les vignettes de l'éditeur. Un logo choisi à la main reste prioritaire ; une chaîne d'un autre pays n'est jamais remplacée.
- Variables `LOGO_COUNTRIES` (défaut `FR,BE,MA`, vide = désactivé) et `LOGO_SOURCE`.
- **Diagnostic** : section « Logos haute qualité » (logos indexés, images en cache, couverture des playlists).

### Corrigé
- **Vignettes de l'éditeur** : les logos ne s'affichaient jamais (une image « lazy » créée hors de la page n'est jamais chargée par le navigateur).

## [3.7.0] — 2026-11-17

Suite à une question utilisateur sur la bascule automatique : une catégorie décochée n'a jamais servi de secours, même avec « Masquer les doublons » et « Bascule automatique » activés.

### Ajouté
- **Avertissement dans « Mes groupes »** : quand la bascule automatique est activée, un groupe dont les chaînes n'ont aucune version de secours (parmi les catégories cochées) l'affiche clairement, avec les deux façons d'y remédier.
- **« 🔗 Cocher aussi les catégories liées »** : sur une catégorie qui ne diffère d'une ou plusieurs autres que par la qualité (ex. « FRANCE FHD | OTT » / « FRANCE HD | OTT » / « FRANCE SD | OTT »), un lien les coche toutes en un clic.
- **Diagnostic** : nouvelle section « Bascule automatique » qui teste chaque playlist concernée — détecte une bascule activée sans « Masquer les doublons » (aucun secours possible), et donne le pourcentage de chaînes avec au moins une version de secours.
- **Option « Secours hors catégories cochées »** (sous « Bascule automatique » dans Options) : cherche des versions de secours dans tout le catalogue du fournisseur, même les catégories non cochées. Ces versions ne sont jamais ajoutées à la playlist ni affichées : elles ne servent que si toutes les versions cochées de la chaîne deviennent indisponibles.

## [3.6.2] — 2026-11-17

Comparaison du modèle « OTT » avec un 2ᵉ fournisseur réel (47 411 chaînes) qui écrit les noms autrement.

### Ajouté
- **11 alias** dans le modèle « OTT » pour les chaînes écrites différemment selon le fournisseur (ex. « National Geographic » en plus de « National Geo », « BFM2 » en plus de « BFM 2 », « LCP », « Sport en France », « Canal+ Cinema », « TV5Monde Europe », « RMC After Foot TV », « Tech&Eco », « Ginx Esports TV », « PadelTime TV »). Couverture du 2ᵉ fournisseur : 123 → 131 chaînes sur 188.

## [3.6.1] — 2026-11-17

Analyse des exports réels de deux fournisseurs (11 178 et 47 411 chaînes).

### Corrigé
- **Export JSON du catalogue** : `direct_source` est désormais vidé (un fournisseur y mettait une URL directe avec les identifiants d'un autre compte).
- **« Masquer les séparateurs »** masque aussi les emplacements d'événements vides (« NO EVENT STREAMING », 7 296 chez un fournisseur) ; ils réapparaissent avec leur vrai nom pendant un événement. Badge « Emplacement vide masqué ».

## [3.6.0] — 2026-11-17

### Ajouté
- **Versions de secours visibles** : sous chaque chaîne gardée, « 🔁 n versions de secours » se déplie et liste les autres versions dans l'ordre où la bascule automatique les essaie ; les versions masquées affichent « Secours 2/4 » ; aperçu des groupes « TF1 (+4) » ; Guide TV « 🔁 n secours ».
- Modèle de groupe **« OTT »** : les 188 chaînes françaises de la catégorie « FRANCE HD | OTT » d'un fournisseur, dans son ordre (correspondance exacte) ; les autres versions (VIP, 4K, FHD…) deviennent leurs secours.

### Corrigé
- Séparateurs non reconnus quand le fournisseur les encadre d'autres symboles (« ♣♦♣----|FR| FRANCE HD |FR|----♣♦♣ », « •●★---… ---★●• ») : règle générique « au moins 3 symboles au début et à la fin ».

## [3.5.1] — 2026-11-17

Retour d'essai réel : au redémarrage, le fournisseur a répondu HTTP 429 (« trop de requêtes ») pendant le rechargement simultané direct + films + séries ; les playlists ont renvoyé 502 environ une minute.

### Corrigé
- **Copie disque des catalogues** (`DATA_DIR/cache/catalog-*.json.gz`, sans `direct_source`) : au redémarrage, les playlists sont servies aussitôt depuis la copie, puis rafraîchies en arrière-plan.
- **Rafraîchissement en arrière-plan** : un catalogue périmé est servi immédiatement pendant son rechargement (plus d'attente de 30 s pour les TV) ; « Actualiser le catalogue » reste immédiat.
- **Un seul gros catalogue chargé à la fois**, au plus 2 appels API simultanés vers le fournisseur.
- **HTTP 429** : pause respectant `Retry-After` (60 s par défaut, 15 s à 10 min), aucun appel au fournisseur pendant la pause ; le catalogue en mémoire ou sur disque continue d'être servi.

## [3.5.0] — 2026-11-17

### Ajouté
- **Guide TV** (nouvelle vue) : programmes maintenant / ce soir 21 h / demain 21 h sur les chaînes d'une playlist (favoris et « Mes groupes » d'abord), progression, programme suivant, recherche sur 7 jours (`/api/now`).
- **Aperçu dans le navigateur** (▶ dans le guide et la liste des chaînes) : hls.js 1.5.20 embarqué (Apache 2.0), relais `/hls/` ouvert en CORS uniquement à l'interface Web ; la connexion est libérée à la fermeture.
- **Priorité des appareils** (haute / normale / basse) et adresse IP facultative : un appareil ne coupe jamais un appareil plus prioritaire ; message « connexion occupée par … ».
- **Retour à la meilleure qualité** après une bascule (`FAILOVER_RETURN_MINUTES`, 10 min, délai doublé si la meilleure version flanche encore).
- **Historique des lectures** (vue Système, `data/history.json`, 1 000 dernières) : chaînes les plus regardées, chaînes qui basculent, dernières lectures.
- **✎ Personnalisation** étendue : logo de remplacement (`item_logos`) et guide emprunté à une autre chaîne (`item_epg`).

### Modifié
- **Interface réorganisée** : éditeur en quatre parties (Contenu, Organisation, Options, Guide) au lieu d'un long écran à blocs dépliants.
- Le diagnostic ignore les sessions HLS inactives (aperçu fermé).

## [3.4.0] — 2026-11-16

### Ajouté
- **Guide : priorité à tes chaînes** — l'API est interrogée dans l'ordre de la playlist : favoris, puis « Mes groupes » (TNT, Bouquet Canal+…), puis le reste.
- **Guide emprunté à une autre version** : si le fournisseur n'a aucun programme pour la version gardée (ex. « VIP: TF1 ᴴᴰ »), le guide est pris sur une de ses versions masquées (« FR: TF1 »…), sous l'identifiant de la chaîne gardée.
- Bloc EPG : couverture « Mes groupes x % · toutes y % » et liste des chaînes sans programme dans l'ordre de la playlist.

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
