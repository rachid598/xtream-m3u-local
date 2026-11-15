# Passation — Xtream M3U Local

> **État au 15 novembre 2026 — passation provisoire.** À mettre à jour après les essais réels sur Unraid. Aucun mot de passe ni token ne doit être copié dans ce document ni commité sur GitHub.

## 1. Objectif utilisateur

L'utilisateur exploite Unraid et possède un abonnement Xtream Codes actif. La requête `player_api.php` répond, `get_live_streams` retourne un JSON complet, `get_live_categories` permet d'obtenir les catégories, et une URL directe `/live/USER/PASS/STREAM_ID.ts` est lisible. En revanche, les URL fournisseur `get.php?type=m3u_plus&output=ts` et `output=m3u8` ne produisent pas de playlist exploitable.

But du projet : convertir le catalogue Xtream en **URLs M3U stables sur Unraid**, indépendantes de `get.php`, puis créer des playlists personnalisées pour des lecteurs sur Android TV, VLC, TiviMate, etc. L'utilisateur entre ses trois paramètres fournisseur dans le Docker Compose ou dans un `.env`, sans ajout manuel de chaînes TXT.

## 2. Architecture

- Un seul conteneur Docker : Python 3.13 et bibliothèque standard uniquement.
- **Port 7777** : API de lecture / playlist :
  - `/health` ; `/playlist.m3u` : playlist complète ; `/get.php` : alias ;
  - `/p/<slug>.m3u` : playlist personnalisée stable ;
  - `/stream/<stream_id>.ts` : relais MPEG-TS, sans transcodage ;
  - `/epg.xml` : proxy XMLTV optionnel.
- **Port 7778** : interface Web HTML/JS responsive, API de gestion :
  - `/` ; `/api/status` ; `/api/catalog` (`?refresh=1` possible) ;
  - GET/POST `/api/playlists` ; PUT/DELETE `/api/playlists/<slug>`.
- Récupération des chaînes : `/player_api.php?action=get_live_streams`.
- Récupération des groupes : `/player_api.php?action=get_live_categories`.
- Cache catalogue en mémoire : 1800 s par défaut, repli sur cache périmé si fournisseur indisponible.
- Sauvegarde des sélections : `/data/playlists.json`, volume Docker persistant `xtream-m3u-data`, écritures atomiques.
- Les identifiants fournisseur restent côté serveur, **jamais dans la playlist en mode proxy**.
- Les URL du lecteur pointent sur le port **7777**, pas 7778.

## 3. Sélection des chaînes

Pour chaque playlist persistée :

```json
{
  "slug": "salon",
  "name": "Télévision du salon",
  "category_ids": ["1", "3"],
  "included_stream_ids": ["123"],
  "excluded_stream_ids": ["456"]
}
```

Critère d'inclusion : catégorie sélectionnée **OU** identifiant explicitement inclus, et identifiant **NON** exclu. Les flux existants changent automatiquement dans le M3U quand le catalogue fournisseur évolue, tant que les identifiants de catégories restent stables.

L'interface permet : sélectionner/désélectionner toutes les catégories, choisir des catégories, rechercher et cocher/décocher des chaînes individuellement, nommer/enregistrer/modifier/supprimer plusieurs playlists, copier l'URL stable et ouvrir le M3U. La liste des chaînes affiche au maximum 200 éléments par recherche pour rester fluide ; affiner la recherche pour les autres.

## 4. Configuration

- Image GitHub Actions prévue (PAS encore publiée) : `ghcr.io/rachid598/xtream-m3u-local:latest`.
- Dépôt GitHub visé (PAS encore créé / vérifié publié) : `rachid598/xtream-m3u-local`.
- Pour un déploiement immédiat utiliser `compose.single.example.yaml` **copié en `compose.single.yaml`** puis remplir `XTREAM_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD`. Le fichier personnalisé est dans `.gitignore`.
- Commande : `docker compose -f compose.single.yaml up -d --build`.
- Ou copier `.env.example` en `.env` puis `docker compose up -d --build`.
- Port de la WebUI `http://IP_UNRAID:7778/`.
- Exemple URL du lecteur : `http://IP_UNRAID:7777/p/salon.m3u`.
- Paramètres complémentaires : `PUBLIC_BASE_URL` (si l'IP détectée est incorrecte), `CACHE_SECONDS`, `ENABLE_EPG`, `ACCESS_TOKEN` (flux 7777), `WEB_USERNAME`, `WEB_PASSWORD` (gestion 7778), `DATA_DIR`.
- Si `ACCESS_TOKEN` est activé, la WebUI inclut un champ facultatif pour ajouter le token à l'URL à copier.
- `docker-create.sh` et `unraid-template.xml` sont présents pour les autres modes d'installation. Sur Unraid, vérifier les permissions du dossier `/mnt/user/appdata/xtream-m3u-local` monté sur `/data` ; le processus Docker tourne en UID 99 / GID 100.

## 5. Arborescence du projet

```text
xtream-m3u-local/
  app/
    server.py        # API Xtream, M3U, TS/EPG, serveur 7777 + lancement Web
    custom.py        # validation, sélection, stockage des playlists
    web.py           # gestion HTTP et endpoints REST du port 7778
    index.html       # interface responsive sans framework
  tests/test_server.py
  Dockerfile
  compose.yaml
  compose.single.example.yaml
  compose.ghcr.yaml
  docker-create.sh
  unraid-template.xml
  .env.example
  .dockerignore
  .gitignore
  .github/workflows/docker-publish.yml
  README.md
  PROMPT_AUTRE_IA.md
  PASSATION_IA.md
```

## 6. État des vérifications

- **16 tests Python passent** sur faux fournisseur Xtream local : M3U, flux, EPG, Range, token, filtre par catégories et chaînes, API Web, authentification, CRUD, persistance, sécurité de validation.
- **Syntaxe JavaScript vérifiée** avec `node --check`.
- **Trois Compose validés en YAML** et template Unraid analysé comme XML valide.
- **Non testé** : création/lecture effective d'une image Docker sur Unraid réel, débit TS sur vrai fournisseur, rendu visuel dans Chrome/TV, compatibilité réelle des lecteurs Android, déploiement GHCR. Une tentative de test Playwright a été bloquée par le navigateur de l'environnement (`ERR_BLOCKED_BY_ADMINISTRATOR`) avant le chargement de la page ; ce n’est pas une erreur prouvée de l'application.
- Le compte GitHub associé est `rachid598`, mais les outils connectés ne proposent pas actuellement la création d'un dépôt ; la publication n'a PAS eu lieu. Ne jamais présenter GHCR comme fonctionnel avant construction/push réussi.

## 7. Prochaines étapes prioritaires

1. Vérifier le rendu de la WebUI, en particulier catégorie → chaînes → sauvegarde → URL permanente.
2. Construire et lancer le Docker sur Unraid, tester `http://IP_UNRAID:7777/health` et `http://IP_UNRAID:7778/` ; tester le volume persistant après `docker compose down/up` sans `-v`.
3. Créer le dépôt GitHub personnel, publier les sources (sans `.env` et sans credentials), et lancer le workflow GHCR ; vérifier les permissions des packages.
4. Tester 2–3 chaînes réelles et une playlist filtrée sur lecteur Android TV ; noter les erreurs exactes et leurs causes.
5. Ajouter des tests de navigation navigateur si possible ; intégrer les retours utilisateur (ergonomie, tri, recherche, masquage, favoris).
6. Faire une relecture sécurité (exposition LAN uniquement, Basic Auth optionnelle, token lecteur, réécriture des URL, SSRF potentiel uniquement avec XTREAM_URL configuré administrativement).
7. En fin de projet, actualiser CE fichier de passation avec commits, image/tag digest, configuration finale, tests réels, TODO et points bloquants.

## 8. Sécurité et précautions

- Les identifiants Xtream ont été partagés précédemment dans une conversation ; **ne pas les réutiliser ni les écrire dans GitHub ou les docs**. Les faire renouveler si possible.
- Ne pas publier de port 7777/7778 directement sur Internet ; si nécessaire, ajouter HTTPS et authentification appropriée.
- Les playlists personnelles sont publiques sur le LAN si `ACCESS_TOKEN` est vide ; `WEB_PASSWORD` protège uniquement la WebUI et son API.
- La limite fournisseur d'une connexion à la fois s'applique également avec le proxy.
- Si des flux HLS sont requis derrière le proxy, cela exige un développement spécifique (réécriture des manifests et des segments) : le mode proxy existant est TS uniquement. Le mode direct HLS divulgue les identifiants dans le M3U.

## 9. Critères d'acceptation

- L’utilisateur ne configure que `XTREAM_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD` puis démarre le Compose ; `http://IP_UNRAID:7778/` fonctionne.
- En cochant les catégories A et C et en enregistrant « salon », `http://IP_UNRAID:7777/p/salon.m3u` ne contient que les chaînes de A et C plus d’éventuelles inclusions manuelles, moins les exclusions.
- La modification ultérieure de « salon » prend effet immédiatement à **la même URL** après rechargement de la playlist par le lecteur.
- Le redémarrage du conteneur n’efface pas les sélections.
- Le M3U proxy ne contient ni username ni password fournisseur.
- Les tests restent verts et la documentation correspond au comportement réel.
