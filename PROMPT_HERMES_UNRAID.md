# Prompt pour Hermès — installation automatique de Xtream M3U Local sur Tower (NVMe)

> Copier tout le bloc ci-dessous dans Hermès **après avoir remplacé les 3 placeholders** `<<XTREAM_URL>>`, `<<XTREAM_USERNAME>>`, `<<XTREAM_PASSWORD>>`.
> Ne pas enregistrer la version remplie dans GitHub ni dans un document partagé.

---

```text
Tu es mon assistant d'administration Unraid. Installe et vérifie automatiquement le projet
« Xtream M3U Local » sur mon serveur Unraid « Tower », avec les données sur le pool NVMe.
Travaille en root dans un terminal de Tower (SSH ou terminal Web). Exécute les étapes dans l'ordre,
vérifie le résultat de chacune, et arrête-toi pour me demander avant toute action destructive
(suppression d'un conteneur ou d'un dossier existant, arrêt d'un autre service).

## Paramètres (fournis par moi — ne les affiche JAMAIS en clair dans tes réponses ni dans les logs)
XTREAM_URL=<<XTREAM_URL>>              # ex. http://serveur.fournisseur:8080 (sans get.php ni ?username=)
XTREAM_USERNAME=<<XTREAM_USERNAME>>
XTREAM_PASSWORD=<<XTREAM_PASSWORD>>

## Paramètres fixes
REPO=https://github.com/rachid598/xtream-m3u-local
BRANCH=main            # (ou le tag de version publié, ex. v3.23.0)
PORTS : 7777 (playlists/lecteurs) et 7778 (interface Web) — ne pas les changer
CONTENEUR : xtream-m3u-local

## Étape 1 — Diagnostic (lecture seule)
1. `hostname`, `cat /etc/unraid-version`, `docker version --format '{{.Server.Version}}'`.
2. Trouver le pool NVMe : `ls -d /mnt/*/ ; df -h /mnt/* | grep -v -E 'disk[0-9]|user0'`.
   Choisir le pool NVMe (souvent /mnt/nvme, /mnt/cache ou /mnt/nvme_cache). S'il y a un doute
   ou plusieurs pools NVMe, me demander lequel utiliser. Noter son chemin dans POOL.
3. Dossier cible : INSTALL_DIR=$POOL/appdata/xtream-m3u-local (sources) et
   DATA_DIR=$INSTALL_DIR/data (playlists.json + cache EPG).
   Si INSTALL_DIR existe déjà : NE RIEN SUPPRIMER, me montrer son contenu et demander.
4. Ports libres : `ss -ltnp | grep -E ':(7777|7778)\b' || echo libres`. S'ils sont pris, me dire par quoi et attendre.
5. Conteneur existant : `docker ps -a --filter name=^xtream-m3u-local$`. S'il existe, demander avant de le remplacer
   (les données dans DATA_DIR seront conservées).
6. Outils : `docker compose version` (plugin « Docker Compose Manager » sur Unraid), `git --version`.
   - Si Compose est absent : utiliser le script `docker-create.sh` (docker run) à l'étape 4 au lieu de Compose.
   - Si git est absent : télécharger l'archive
     `wget -O /tmp/xml.tar.gz $REPO/archive/refs/heads/$BRANCH.tar.gz` puis l'extraire dans INSTALL_DIR
     (`tar -xzf /tmp/xml.tar.gz --strip-components=1 -C $INSTALL_DIR`).
     Si le dépôt est privé (erreur 404), me demander un jeton GitHub en lecture ou de rendre le dépôt public.

## Étape 2 — Récupération du projet
mkdir -p "$INSTALL_DIR" && cd "$INSTALL_DIR"
git clone --branch "$BRANCH" "$REPO" .      # ou l'archive (voir ci-dessus)
Vérifier la présence de : Dockerfile, compose.yaml, docker-create.sh, .env.example, app/server.py, app/web.py.

## Étape 3 — Configuration (secrets uniquement dans .env, droits 600)
cp .env.example .env && chmod 600 .env
Dans .env, remplacer (avec sed, sans afficher les valeurs) :
  XTREAM_URL=<<XTREAM_URL>>
  XTREAM_USERNAME=<<XTREAM_USERNAME>>
  XTREAM_PASSWORD=<<XTREAM_PASSWORD>>
  DATA_PATH=$DATA_DIR            (chemin absolu sur le pool NVMe)
  TZ=Europe/Paris
Pas de guillemets autour des valeurs. Laisser vides WEB_PASSWORD, ACCESS_TOKEN et PUBLIC_BASE_URL
(sauf si je demande une protection par mot de passe).
Contrôle sans divulgation : `grep -c '^XTREAM_' .env` doit afficher 3, et
`grep -E 'REMPLACER|votre-serveur' .env` ne doit rien afficher.
mkdir -p "$DATA_DIR"   (le conteneur corrige lui-même les droits en 99:100 au démarrage)

## Étape 4 — Construction et démarrage
Avec Compose :   docker compose up -d --build
Sans Compose :   sh docker-create.sh            (ou --replace si j'ai validé le remplacement)
Puis attendre 20 s et vérifier :
  docker ps --filter name=xtream-m3u-local --format '{{.Status}}'      → Up … (healthy) après ~30 s
  docker logs --tail 30 xtream-m3u-local    → doit contenir « Catalogue chargé : N chaînes »
  (si « Le fournisseur refuse l'accès (HTTP 401/403) » : identifiants incorrects ; si « API Xtream indisponible » :
   URL ou réseau ; me le signaler sans afficher les secrets)

## Étape 5 — Tests fonctionnels (remplacer IP par l'IP LAN de Tower : `ip -4 route get 1 | awk '{print $7}'`)
1. curl -s http://IP:7777/health                                  → OK
2. curl -s -o /dev/null -w '%{http_code}\n' http://IP:7778/         → 200
3. curl -s http://IP:7778/api/status?account=1                     → JSON sans mot de passe (statut d'abonnement, date d'expiration)
4. curl -s http://IP:7778/api/catalog | head -c 400                 → catégories (ordre du fournisseur, parent_id, count)
   Compter : nombre de catégories et de chaînes ; signaler si "categories_error" n'est pas null.
5. Créer une playlist de test via l'API (2 premières catégories non vides du catalogue) :
   curl -s -X POST http://IP:7778/api/playlists \
     -H 'X-Requested-With: XtreamM3ULocal' -H 'Content-Type: application/json' \
     -d '{"slug":"test-hermes","name":"Test Hermès","category_ids":["ID1","ID2"]}'
6. curl -s http://IP:7777/p/test-hermes.m3u | head -n 7
   Vérifier : commence par #EXTM3U, contient group-title, tvg-logo/tvg-id quand disponibles,
   liens en http://IP:7777/stream/<id>.ts, et AUCUN nom d'utilisateur ni mot de passe fournisseur :
   curl -s http://IP:7777/p/test-hermes.m3u | grep -c "<<XTREAM_USERNAME>>"   → 0
7. Flux : prendre le premier /stream/<id>.ts de la playlist et lancer
   curl -s --max-time 8 -o /dev/null -w '%{http_code} %{size_download}\n' "<URL du flux>"
   → 200 et quelques centaines de Ko. (Une seule lecture à la fois : l'abonnement peut limiter les connexions.)
8. EPG : curl -s --max-time 120 http://IP:7777/p/test-hermes.xml | head -c 300 → XML <tv>… avec des <programme>
   et : curl -s -o /dev/null -w '%{http_code}\n' http://IP:7777/xmltv.php   → 200 (adresse EPG « à la Xtream »)
   puis : curl -s "http://IP:7778/api/epg?slug=test-hermes"
   → noter "source" (xmltv.php ok/erreur, nombre de chaînes/programmes) et "playlist" (channels, xmltv, api, pending,
   nombre de "missing"). Si pending > 0, réessayer 30 s plus tard. Si xmltv.php est en erreur, c'est normal que
   tout passe par l'API (mode auto) ; le signaler dans le rapport.
8b. Films et séries :
   curl -s "http://IP:7778/api/catalog?kind=vod" | head -c 300      → catégories et films (ou listes vides si l'abonnement n'a pas de VOD)
   curl -s "http://IP:7778/api/catalog?kind=series" | head -c 300   → catégories et séries
   Créer une playlist de test VOD avec la première catégorie de films non vide et la première catégorie de séries non vide :
   curl -s -X POST http://IP:7778/api/playlists -H 'X-Requested-With: XtreamM3ULocal' -H 'Content-Type: application/json' \
     -d '{"slug":"test-vod","name":"Test VOD","vod_category_ids":["IDF"],"series_category_ids":["IDS"]}'
   Attendre 30 s (préchargement des épisodes), puis :
   curl -s http://IP:7777/p/test-vod.m3u | grep -E '/(movie|series)/' | head -n 4   → liens /movie/<id>.<ext> et /series/<id>.<ext>
   Tester un film : curl -s -r 0-1048575 -o /dev/null -w '%{http_code} %{size_download}\n' "<URL /movie/...>"   → 206 (ou 200) et ~1 Mo
   Supprimer ensuite la playlist test-vod (DELETE comme à l'étape 11).
8c. Diagnostic intégré (teste API, catalogues, guide, flux TS/HLS, film, épisode, replay ; rien ne doit être en lecture) :
   curl -s -X POST -H 'X-Requested-With: XtreamM3ULocal' http://IP:7778/api/diagnostic
   → recopier dans le rapport chaque ligne "label / status / detail" (aucun secret n'y figure).
8d. Serveur Xtream local : créer un appareil de test lié à test-hermes :
   curl -s -X POST http://IP:7778/api/devices -H 'X-Requested-With: XtreamM3ULocal' -H 'Content-Type: application/json' \
     -d '{"name":"Test Hermès","playlist":"test-hermes"}'
   → noter "username" et "password" (codes LOCAUX, pas ceux du fournisseur), puis :
   curl -s "http://IP:7777/player_api.php?username=<username>&password=<password>" | head -c 300     → "auth":1
   curl -s "http://IP:7777/player_api.php?username=<username>&password=<password>&action=get_live_streams" | head -c 300
   Supprimer ensuite l'appareil : curl -s -X DELETE http://IP:7778/api/devices/<id> -H 'X-Requested-With: XtreamM3ULocal'
9. Persistance : `docker restart xtream-m3u-local` (ou `docker compose down && docker compose up -d`, SANS -v),
   attendre 15 s, puis vérifier que http://IP:7777/p/test-hermes.m3u répond toujours 200
   et que $DATA_DIR/playlists.json existe (propriétaire 99:100).
10. Interface responsive : si tu as un navigateur, ouvrir http://IP:7778/ en largeur bureau puis mobile (≈390 px) :
    pas de défilement horizontal, onglets Catégories / Chaînes / Résumé utilisables, vue « Catalogue fournisseur »
    accessible. Sinon, indiquer que ce contrôle visuel reste à faire par moi.
11. Supprimer la playlist de test :
    curl -s -X DELETE http://IP:7778/api/playlists/test-hermes -H 'X-Requested-With: XtreamM3ULocal'

## Étape 6 — Unraid
- Le conteneur créé par Compose apparaît dans l'onglet Docker d'Unraid (mention « 3rd party ») ; il redémarre
  automatiquement (restart: unless-stopped). Vérifier que le service Docker d'Unraid est en démarrage automatique.
- Ne PAS ouvrir/rediriger les ports 7777/7778 sur la box Internet.
- Ne pas modifier d'autres conteneurs, partages ou réglages Unraid.

## Rapport final attendu (sans aucun secret)
- Pool NVMe utilisé, INSTALL_DIR, DATA_DIR, méthode (Compose ou docker run), image/commit utilisé (`git rev-parse --short HEAD`).
- Résultat de chaque test de l'étape 5 (OK / échec + message exact).
- Nombre de catégories et de chaînes, de films et de séries ; état categories_error ; état de l'EPG (source xmltv.php OK ou non, couverture xmltv/API/manquantes) ; résultat du test d'un film et d'un épisode.
- Les URL à utiliser :
    Interface Web : http://IP:7778/
    Exemple de playlist : http://IP:7777/p/salon.m3u (à créer dans l'interface)
    Playlist complète : http://IP:7777/playlist.m3u
- Commandes de mise à jour :  cd INSTALL_DIR && git pull && docker compose up -d --build
- Résultats du diagnostic intégré (8c) et du test Xtream local (8d).
- Problèmes rencontrés et actions restant à faire.
```

---

## Notes pour moi

- Si le dépôt GitHub est privé, Hermès aura besoin d'un accès en lecture (jeton) ou il faudra rendre le dépôt public.
- Mise à jour ultérieure : `cd <INSTALL_DIR> && git pull && docker compose up -d --build` (les playlists sont conservées dans `data/`).
- Option plus tard : quand l'image GHCR sera publiée, remplacer la construction locale par `docker compose -f compose.ghcr.yaml up -d`.
