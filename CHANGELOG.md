# Changelog

Format inspiré de « Keep a Changelog ». Les versions suivent le versionnage sémantique.

## [3.16.1] — 2026-11-21

Retour réel (tower-big, compte à 1 connexion) : la passe de nuit a planifié 30 587 séries d'un coup ; 429 au bout de 8 s à 2 demandes/s, toutes rejetées, 30 séries mises à jour.

### Corrigé
- **Passe de nuit des séries très douce** : seulement les séries déjà ouvertes (copie en cache périmée), les plus anciennes d'abord, **une demande toutes les 5 s**, une à la fois, 600 au plus par nuit. Les séries jamais ouvertes ne sont plus demandées en masse (elles le sont à leur ouverture). Arrêt au premier refus (429) ou à la fin de la fenêtre, reprise la nuit suivante ; journal en deux lignes (plan puis bilan).

## [3.16.0] — 2026-11-20

### Modifié
- **Un profil = un serveur à part.** Cliquer sur un profil recharge toute l'interface sur ce profil seul : playlists, appareils, guide, catalogue, lectures en cours, historique, diagnostic et compte de l'abonnement. Rien d'un autre profil n'est affiché ni mélangé. Les TV n'ont besoin que de leur code d'appareil.
- **Mélange désactivé par défaut** : « Tous les abonnements (mélange) » n'apparaît que si tu coches Système → Abonnements → « Autoriser les playlists qui mélangent plusieurs abonnements » (`/data/settings.json`). Sans cette case, une ancienne playlist sans abonnement utilise le profil principal seul.
- API : `?profile=N` sur `/api/streams`, `/api/history`, `/api/history/clear` et `/api/diagnostic` ; l'historique retient l'abonnement de chaque lecture.

## [3.15.0] — 2026-11-20

Retour réel : à chaque recréation du conteneur, rafale de 30 649 demandes d'épisodes (`get_series_info`), 30 713 lignes de journal et 4 « 429 », avant même de regarder la télé.

### Ajouté
- **Mise à jour de nuit** (`NIGHTLY_REFRESH`, 3 h–6 h par défaut) : catalogues films et séries et épisodes rafraîchis la nuit ; en journée, les copies en cache sont servies sans requête (seules les séries jamais vues sont demandées tout de suite). Le bouton « Actualiser le catalogue » reste immédiat.

### Corrigé
- **Épisodes des séries protégés comme le guide** : 2 demandes par seconde au maximum (`SERIES_PER_SECOND`), démarrage calme, mode prudent 1 h après un 429, journal résumé (une ligne par minute).
- Cache des épisodes : 7 jours par défaut (`SERIES_CACHE_HOURS=168`).

## [3.14.0] — 2026-11-19

### Ajouté
- **Système → « Accès à distance »** : case « Tailscale = mon réseau local » (100.64.0.0/10 et fd7a:115c:a1e0::/48), sans toucher à la configuration du conteneur ; confirmation à l'activation ; l'adresse d'où la page est ouverte est affichée. Réglage gardé dans `/data/settings.json`.

## [3.13.0] — 2026-11-19

Journaux réels en 3.12.0 : même à 1-2 appels par seconde, le fournisseur bloque (429) au bout d'environ une minute et refuse alors aussi les chaînes.

### Ajouté
- **Mode prudent automatique** : après un 429 du fournisseur, le guide « chaîne par chaîne » est suspendu 1 h (aucun appel), le 429 est retenu même après un redémarrage, et les catalogues gardent leur copie en cache 10 min au lieu d'être rafraîchis. Diagnostic : « Mode prudent » jusqu'à l'heure de reprise, sans appel de test.
- **Démarrage calme** : aucun appel « guide par chaîne » pendant les 2 premières minutes (`EPG_STARTUP_QUIET`).

## [3.12.0] — 2026-11-19

Journaux réels : en ouvrant l'interface, des milliers d'appels « guide par chaîne » en quelques secondes → « 429 » du fournisseur (pause 60 s) et ~2 500 lignes d'avertissement ; cause probable du noir de 30 s au démarrage de TiviMate (guide et flux demandés en même temps).

### Ajouté
- **Guides EPG externes** (`EPG_EXTRA_URLS`, `.xml` / `.xml.gz` / `.zip`) : utilisés après le guide du fournisseur et **avant** l'API ; chaîne trouvée par identifiant ou par nom ; programmes renommés vers les identifiants de la playlist. Diagnostic par guide (chaînes, couverture des playlists).

### Corrigé
- **Appels « guide par chaîne » freinés** : 2 par seconde au maximum (`EPG_API_PER_SECOND`), en attente 20 s quand une lecture démarre (priorité à la vidéo), au plus `EPG_API_MAX_CHANNELS` par playlist, journal résumé (une ligne par minute au lieu d'une par chaîne).
- Avec `EPG_SOURCE=xmltv` et des guides externes, les chaînes sans identifiant EPG reçoivent `tvg-id="xtream-<id>"` pour que leurs programmes externes s'affichent.

## [3.11.0] — 2026-11-19

### Ajouté
- **Abonnements depuis l'interface** (Système → « Abonnements ») : ajout (connexion testée avant l'enregistrement), renommage, changement de mot de passe et suppression, **sans redémarrer le conteneur** ; le profil apparaît aussitôt dans la barre « Profil ». Identifiants dans `/data/providers.json` (0600), jamais renvoyés à l'interface, masqués partout ; numéro fixe par abonnement (2 à 9), la configuration du conteneur reste prioritaire.
- **`LOCAL_NETWORKS`** : réseaux supplémentaires considérés comme le réseau local (ex. `100.64.0.0/10` pour gérer son serveur par Tailscale sans ouvrir l'accès distant).

## [3.10.0] — 2026-11-18

Demande : gérer chez soi l'abonnement et la playlist de sa sœur, sans tout mélanger.

### Ajouté
- **Profils** : avec plusieurs abonnements (`XTREAM_URL_2`…), barre « Profil » en haut de l'interface (un profil par abonnement, nommé par `XTREAM_NAME`, `XTREAM_NAME_2`…). Playlists, catalogue, Guide TV, appareils et badge d'abonnement filtrés par profil ; une playlist d'un profil n'utilise que son abonnement (chaînes, règles, secours, connexions). Option « Abonnement utilisé par cette playlist » (ou « Tous les abonnements » pour les playlists mixtes ; les playlists existantes le restent).
- Serveur Xtream local : un appareil voit l'expiration et les connexions de l'abonnement de son profil.
- Démo : `python -m tools.demo --profiles` ; test navigateur `tests/e2e_profiles.mjs`.

### Corrigé
- **Serveur Xtream derrière un reverse proxy HTTPS** : `server_info` annonçait toujours `http` et le port 80 ; avec `PUBLIC_BASE_URL=https://…`, les lecteurs reçoivent maintenant `server_protocol: https` et le bon `https_port`.

## [3.9.0] — 2026-11-18

Suite de l'audit (issue n° 7) et de la demande « accès à distance pour la famille ».

### Ajouté
- **Accès distant sécurisé** (`REMOTE_ACCESS`, fermé par défaut) : hors du réseau local, code d'appareil obligatoire, mode direct et catalogue complet refusés, adresse bloquée 15 min après 10 codes faux. `TRUSTED_PROXIES` : adresse réelle derrière Traefik / Nginx Proxy Manager (une requête relayée par un proxy non déclaré est toujours distante). Interface Web réservée au réseau local (`WEB_REMOTE` pour l'ouvrir, mot de passe obligatoire). Guide Tailscale et Traefik dans le README.
- **Diagnostic « Accès distant »** : fermé / ouvert, appareils sans playlist, adresse publique sans https, proxy non déclaré.

### Corrigé (audit)
- **Droits par appareil sur toutes les routes** : avec un code d'appareil (M3U ou Xtream), seules les chaînes, films, épisodes, replays et HLS de sa playlist sont lisibles ; versions de secours décidées par le serveur ; `/playlist.m3u` et `/xmltv.php` renvoient sa playlist et son guide. Les épisodes de séries n'étaient pas vérifiés en mode Xtream.
- **Journaux** : le mot de passe d'appareil présent dans les chemins Xtream (`/live/<utilisateur>/<mot de passe>/…`) est masqué.
- **Un cache de catalogue par fournisseur** : avec plusieurs abonnements, tous écrivaient dans le même fichier (le 2ᵉ pouvait reprendre le catalogue du 1ᵉʳ au démarrage). Fichier `catalog-<identifiant haché>-<type>.json.gz`, copie d'un autre fournisseur ignorée ; ancien fichier renommé (un abonnement) ou supprimé (plusieurs).
- **Quota sur la source réellement lue** : la bascule vers une version d'un autre fournisseur prend la place chez ce fournisseur (version sautée s'il est plein, sans couper personne).
- **Lectures identifiées par appareil** : deux TV derrière la même adresse (box, reverse proxy) ne se coupent plus l'une l'autre en croyant être le même appareil.

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
