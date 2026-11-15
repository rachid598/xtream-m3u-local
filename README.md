# Xtream M3U Local

Microservice Docker pour Unraid : **une API Xtream fonctionnelle suffit**, même si le fournisseur ne renvoie rien sur `/get.php`.
Le service lit `player_api.php?action=get_live_streams` et `get_live_categories`, crée des playlists M3U complètes ou personnalisées et les rend disponibles **en permanence sur ton réseau local**.

- **Playlist complète :** `http://IP_UNRAID:7777/playlist.m3u`
- **Interface de gestion :** `http://IP_UNRAID:7778/`
- **Playlist personnalisée :** `http://IP_UNRAID:7777/p/salon.m3u`
- **Flux locaux :** `http://IP_UNRAID:7777/stream/12345.ts` (relayés en temps réel vers le fournisseur)
- **Santé :** `http://IP_UNRAID:7777/health`
- **Mise à jour forcée :** `http://IP_UNRAID:7777/playlist.m3u?refresh=1`
- **EPG optionnel :** `http://IP_UNRAID:7777/epg.xml` (nécessite `ENABLE_EPG=true`)

Aucun abonnement n'est fourni : utilise uniquement les flux pour lesquels tu disposes des droits.

## Démarrage rapide (Unraid / Docker Compose)

1. Télécharge le dépôt (ou le ZIP), place-le dans un dossier de ton serveur.
2. Copie `.env.example` en `.env` et renseigne `XTREAM_URL`, `XTREAM_USERNAME` et `XTREAM_PASSWORD` **dans `.env` uniquement**.
3. Lance `docker compose up -d --build` dans le dossier.
4. Ouvre `http://IP_UNRAID:7778/` dans un navigateur, crée une playlist « Salon », coche les catégories/chaînes, puis enregistre.
5. Copie l’URL `http://IP_UNRAID:7777/p/salon.m3u` dans VLC, TiviMate ou ton lecteur Android TV.

**Deux ports sur un seul conteneur :** `7777` pour les playlists et flux TS ; `7778` pour le tableau de bord.
Les playlists créées sont sauvegardées en JSON dans **`/data/playlists.json`**, sur le volume Docker `xtream-m3u-data`. Elles survivent aux redémarrages et aux recréations du conteneur tant que le volume persiste.
La page Web fonctionne sans services externes ; le navigateur n’a jamais besoin de se connecter directement au serveur Xtream.

Les ports **7777 et 7778** doivent être disponibles sur Unraid. Le fichier `.env` est ignoré par Git et exclu de l'image Docker.

### Variante facile : un seul fichier Compose à modifier

Si tu préfères mettre les trois identifiants **directement dans le YAML**, sans fichier `.env` :

```sh
cp compose.single.example.yaml compose.single.yaml
```

Ouvre **`compose.single.yaml`**, remplis `XTREAM_URL`, `XTREAM_USERNAME`, `XTREAM_PASSWORD`, puis :

```sh
docker compose -f compose.single.yaml up -d --build
```

Le fichier `compose.single.yaml` est ignoré par Git pour éviter de publier tes identifiants. **Ne publie pas ton fichier modifié.**

### Variante Docker Create (sans Compose)

Créer `.env` à partir de `.env.example`, puis lancer `sh docker-create.sh` pour construire l'image et lancer un conteneur `docker run` sur le port 7777.

### Variante : image GHCR (après publication du dépôt)

Le dépôt contient un workflow GitHub Actions qui publie l'image `ghcr.io/rachid598/xtream-m3u-local:latest` lorsque le dépôt `rachid598/xtream-m3u-local` existe et que le workflow a réussi. Pour déployer cette image sans compiler sur Unraid :

```sh
docker compose -f compose.ghcr.yaml up -d
```

Cela suppose que l'image GHCR soit accessible à ton serveur (visibilité du package ou authentification Docker).

## Comment ça marche ?

1. `get_live_streams` renvoie les `stream_id`, noms, logos et identifiants EPG.
2. `get_live_categories` fournit les groupes (si indisponible, groupe `TV`).
3. La playlist M3U associe chaque chaîne à une adresse locale `/stream/ID.ts`. Les playlists personnalisées filtrent selon les catégories sélectionnées, les inclusions manuelles et les exclusions.
4. Le conteneur relaie les octets TS **sans conversion vidéo ni transcodage**.

Les flux sont ouverts **seulement lors de la lecture**. Le catalogue est conservé 30 minutes en mémoire par défaut. Aucun fichier M3U n'est créé sur disque : l'URL produit le M3U à chaque consultation, depuis les données en cache.

## Modes et formats

| Adresse | Comportement |
|---|---|
| `/playlist.m3u` | **Recommandé** : liens TS locaux, identifiants Xtream absents du M3U |
| `/playlist.m3u?mode=direct&format=ts` | Liens `.ts` directs chez le fournisseur ; **identifiants présents dans le M3U** |
| `/playlist.m3u?mode=direct&format=m3u8` | Liens HLS directs ; **identifiants présents dans le M3U** |
| `/playlist.m3u?refresh=1` | Tente d'actualiser immédiatement les chaînes |
| `/get.php` | Alias de `/playlist.m3u` (pas une API Xtream complète) |
| `/p/salon.m3u` | Playlist personnelle « salon », URL stable et dynamique |
| `http://IP_UNRAID:7778/` | Interface de création, édition et suppression des playlists |

> Le relais HLS local et les liens RTMP ne sont **pas** implémentés : le projet fournit un proxy MPEG-TS et un générateur M3U. Pour le format HLS, utilise le mode direct. Ne pointe pas un client *Xtream Codes* vers ce serveur : utilise l'URL M3U.

## Options d'environnement

| Variable | Obligatoire | Description |
|---|---|---|
| `XTREAM_URL` | oui | URL de base de ton serveur Xtream (`http://serveur:80`) |
| `XTREAM_USERNAME` | oui | Identifiant fournisseur |
| `XTREAM_PASSWORD` | oui | Mot de passe fournisseur |
| `PORT` | non | Port du service dans le conteneur (`7777`) |
| `WEB_PORT` | non | Port interface Web (`7778`) |
| `DATA_DIR` | non | Dossier de stockage, par défaut `/data` |
| `WEB_USERNAME` | non | Identifiant de l’interface Web, `admin` par défaut |
| `WEB_PASSWORD` | non | Si renseigné, active HTTP Basic pour toute l’interface Web et son API |
| `PUBLIC_BASE_URL` | non | URL publique locale à écrire dans la playlist, par ex. `http://192.168.1.10:7777`; défaut : URL de la requête |
| `CACHE_SECONDS` | non | Durée de cache de la liste des chaînes (`1800`) |
| `ACCESS_TOKEN` | non | Si défini, exige `?token=VALEUR` sur la playlist et les flux ; le token est repris dans les liens générés |
| `ENABLE_EPG` | non | `true` active `/epg.xml` (proxy du XMLTV fournisseur), si disponible |

## Vérification

```sh
curl -i http://IP_UNRAID:7777/health
curl -s http://IP_UNRAID:7777/playlist.m3u | head -n 8
curl --range 0-1023 --max-time 5 -o /dev/null http://IP_UNRAID:7777/stream/12345.ts
```

Si les liens dans la playlist utilisent une IP incorrecte, fixe `PUBLIC_BASE_URL=http://IP_UNRAID:7777` dans `.env` puis redémarre le conteneur.

## Sécurité et limites

- **Réseau local seulement.** Par défaut, pas d'authentification sur les playlists ou flux : ne redirige pas le port 7777 vers Internet. Pour un réseau partagé, active `ACCESS_TOKEN` (et HTTPS via un proxy inverse si exposition contrôlée).
- Les identifiants sont présents dans les requêtes sortantes envoyées au fournisseur, **pas dans le M3U du mode proxy**. Le mode direct les contient.
- Les en-têtes HTTP Range sont transmis au fournisseur, utile pour certaines applications.
- **Pas de transcodage** : chaque lecture consomme une connexion amont. Si l'abonnement autorise une seule connexion, ne lis pas plusieurs chaînes simultanément.
- L'EPG dépend de `/xmltv.php`, qui peut être indisponible même si l'API Xtream fonctionne.
- Tester les flux **.ts** avant d'utiliser le mode proxy. Un lecteur compatible TS est nécessaire.
- Python standard library, sans dépendances Python externes.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## Licence

MIT.
