# Passation — Xtream M3U Local

> **État au 15 novembre 2026 — version 2.1.0.** Document à transmettre tel quel à l'IA suivante avec le dépôt.
> Aucun mot de passe, token ni URL fournisseur réelle ne doit être copié ici ni commité sur GitHub.

## 1. Objectif utilisateur

L'utilisateur exploite **Unraid** (serveur « Tower », appdata sur un pool **NVMe**) et possède un abonnement Xtream Codes actif :
`player_api.php` répond (`get_live_streams`, `get_live_categories`), et une URL directe `/live/USER/PASS/STREAM_ID.ts` est lisible.
En revanche, `get.php?type=m3u_plus&output=ts|m3u8` ne produit **pas** de playlist exploitable.

But : reconstruire les playlists à partir de l'API Xtream et les diffuser depuis Unraid sous forme d'**URL M3U permanentes**
(VLC, TiviMate, lecteurs Android TV), avec une interface Web de gestion, en ne saisissant que `XTREAM_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD`.

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
  - `/epg.xml` XMLTV complet (cache disque) ; `?refresh=1` force la relecture du catalogue ; `?token=` si `ACCESS_TOKEN`.
- **Port 7778** (gestion, `app/web.py`) :
  - `/` + `/static/app.css`, `/static/app.js`, `/static/favicon.svg` (liste blanche, aucun accès fichier arbitraire) ;
  - `GET /api/status[?account=1]`, `GET /api/catalog[?refresh=1]`, `GET /api/catalog/export[?refresh=1]` ;
  - `GET/POST /api/playlists`, `PUT/DELETE /api/playlists/<slug>`.
- Catalogue : `XtreamAPI.catalog()` (cache mémoire `CACHE_SECONDS`=1800 s, repli sur cache périmé, pas de nouvel essai pendant 60 s après un échec).
- Données : `DATA_DIR` (`/data`) → `playlists.json` (+ `playlists.json.bak`) et `epg/` (cache XMLTV + EPG filtrés).
- Démarrage (`python -m app`) : lecture de la config, correction des droits de `/data` puis abandon des privilèges root → `PUID:PGID` (99:100), lancement des deux serveurs, préchargement du catalogue en tâche de fond, arrêt propre sur SIGTERM.

### Arborescence

```text
app/
  __init__.py        # __version__ = "2.1.0"
  __main__.py        # point d'entrée « python -m app » (évite le double import de app.server)
  server.py          # Config, XtreamAPI, normalisation, M3U, port 7777, droits /data, main()
  custom.py          # validation, filtrage, PlaylistStore (JSON atomique + .bak)
  epg.py             # EpgCache (téléchargement, gzip, cache disque) + filtrage XMLTV en flux
  web.py             # port 7778 : statiques, API REST, export catalogue, Basic Auth, CSRF
  static/index.html  # structure (aucun script/style inline)
  static/app.css     # thème clair/sombre automatique, responsive
  static/app.js      # logique de l'interface
  static/favicon.svg
tests/
  fake_xtream.py     # faux fournisseur Xtream (catégories, parent_id, chaînes multi-catégories, EPG, erreurs)
  helpers.py         # pile complète 7777+7778 sur ports éphémères
  test_playlist_server.py  test_custom.py  test_web.py  test_epg.py  test_config.py
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

## 4. Modifications de cette session (V2 → 2.1.0)

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
| `python -m unittest discover -s tests -t .` | **65 tests OK** (faux fournisseur local) |
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
- Pas de renommage/réordonnancement **chaîne par chaîne** (seulement les groupes) ; pas de favoris ; pas de VOD/séries (`get_vod_streams`, `get_series`).
- Protection contre le *DNS rebinding* non implémentée (l'API ne contient aucun secret ; `WEB_PASSWORD` recommandé sur un réseau partagé).
- L'EPG complet est servi tel quel (peut faire des centaines de Mo) ; les playlists personnalisées utilisent l'EPG filtré.

## 8. Prochaines étapes prioritaires

1. Déployer sur Tower avec `PROMPT_HERMES_UNRAID.md` (ou à la main) ; noter versions, chemin NVMe, résultats.
2. Comparer `http://IP_UNRAID:7778/api/catalog?refresh=1` (ou la vue Catalogue / l'export JSON) avec les groupes de l'application TV :
   - mêmes groupes que le JSON brut mais pas dans le projet → corriger l'importation ;
   - projet = JSON mais TV différente → filtres/cache de l'application TV.
3. Tester 2–3 chaînes et une playlist filtrée dans VLC, TiviMate et le lecteur Android TV ; vérifier logos, groupes, EPG ; noter les erreurs exactes.
4. Renommer la branche par défaut en `main`, vérifier que le package GHCR est public (ou documenter `docker login`), puis tester `compose.ghcr.yaml` / le template Unraid.
5. Évolutions possibles : renommage et ordre des chaînes, favoris, numérotation personnalisée (`tvg-chno`), VOD/séries, proxy HLS, option `ALLOWED_HOSTS` anti-rebinding, statistiques de lecture, sauvegarde/restauration des playlists depuis l'interface, historique des exports catalogue avec comparaison (diff).
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
- [ ] Validation sur le vrai Unraid, le vrai fournisseur et les vrais lecteurs (à faire par l'utilisateur / Hermès).
- [x] Image GHCR publiée par le workflow (run n° 1 réussi) — [ ] `docker pull` à confirmer sur Tower.
