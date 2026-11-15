# Prompt à transmettre à l'autre IA

Tu reprends un projet existant nommé **Xtream M3U Local**. **Commence par lire `PASSATION_IA.md`, `README.md` et le code livré dans le ZIP. Ne repars pas de zéro.**

## Contexte réel

Je dispose d’un serveur Unraid et d’identifiants Xtream Codes valides. `player_api.php?action=get_live_streams` renvoie mes chaînes et la lecture directe `/live/USER/PASS/STREAM_ID.ts` fonctionne, mais les playlists du fournisseur via `get.php` restent vides. Le projet existant reconstruit donc les playlists directement depuis l'API Xtream.

Le projet actuel comporte déjà deux services HTTP dans **un seul conteneur Docker** :

- **7777** : playlist complète `/playlist.m3u`, playlist personnalisée `/p/<slug>.m3u`, relais TS `/stream/<id>.ts`, option EPG ;
- **7778** : gestion Web accessible sur `http://IP_UNRAID:7778/`, choix des catégories et de chaînes, gestion de plusieurs playlists nommées.

Les sélections persistent dans `/data/playlists.json` (volume Docker), et les identifiants fournisseur restent côté conteneur. Le projet est en Python standard library + HTML/JS, sans framework ; Compose, Dockerfile, script docker-create, template Unraid, tests et workflow GHCR existent. Les tests locaux passent mais Docker/Unraid/lecteur Android n’ont pas encore été vérifiés en réel.

## Ce que je veux que tu fasses

1. **Audite le code existant et corrige les bugs.** Ne casse pas la génération M3U ni le relais TS. Ne recopie aucun identifiant réel dans les fichiers ou les logs.
2. **Teste et améliore l'interface Web sur 7778** : interface élégante, responsive, simple et en français ; affichage des catégories et du nombre de chaînes ; sélection de catégories entières ; recherche par nom ; possibilité d'ajouter/retirer ponctuellement certaines chaînes ; aperçu du nombre de chaînes choisies.
3. **Gère plusieurs playlists personnalisées** : création, modification, suppression, nom et slug ; sélections persistantes après redémarrage ; possibilité de garder les mêmes adresses après modification. Exemple de playlist « salon » : `http://IP_UNRAID:7777/p/salon.m3u`.
4. **URL M3U pour Android TV** : au moment de l'enregistrement, afficher un lien directement copiable et utilisable dans VLC / TiviMate / un lecteur Android TV. Seules les chaînes et catégories retenues doivent figurer dans ce M3U ; conserver leurs noms, logos, groupes et identifiants EPG. Les flux doivent utiliser l'IP Unraid sur 7777, pas le fournisseur.
5. **Qualité et sécurité** : ne pas exposer les credentials Xtream dans les playlists en mode proxy, filtrer/valider les entrées, ne pas renvoyer de secrets dans l'API Web, prévoir protection optionnelle de la WebUI et des flux, éviter les dépendances cloud, préserver la restriction fournisseur sur les connexions simultanées.
6. **Docker/Unraid** : le `docker compose` ne doit demander à l'utilisateur que l'URL Xtream, le login et le mot de passe pour fonctionner en LAN (les autres paramètres ont de bonnes valeurs par défaut) ; ouvrir les ports 7777 et 7778 ; sauvegarder le JSON des playlists sur un volume ; fournir `Dockerfile`, Compose, `docker-create.sh` et template Unraid maintenus à jour.
7. **Vérification de bout en bout** : lancer les tests unitaires et en ajouter, vérifier l'interface et les API, tester autant que possible le build Docker, documenter les essais réels restant à faire sur mon Unraid et avec Android TV.
8. **GitHub et GHCR** : si autorisé, publier le code dans mon dépôt personnel `rachid598/xtream-m3u-local` une fois créé, sans aucun secret ; construire/publier l'image `ghcr.io/rachid598/xtream-m3u-local:latest` et vérifier qu'elle est exploitable. Ne prétends pas que le dépôt/l'image sont créés tant qu'ils ne le sont pas.
9. **Passation** : à chaque étape importante, mets à jour `PASSATION_IA.md` en précisant état, architecture, décisions, fichiers modifiés, commandes, tests, TODO, obstacles. Je veux pouvoir reprendre le projet avec n’importe quelle autre IA. À la fin, fournis le ZIP complet et les liens réels si publiés.

## Critères d'acceptation concrets

En déployant sur Unraid, j'ouvre `http://IP_UNRAID:7778`, sélectionne par exemple « Informations » et « Sport », puis enregistre « salon ». Quand je colle `http://IP_UNRAID:7777/p/salon.m3u` dans mon lecteur Android TV, **je ne vois que les chaînes autorisées par mes choix**, groupées dans ces catégories ; une modification dans la WebUI est reflétée à cette même URL au rechargement du lecteur. Les données survivent à une recréation du conteneur. Tous les tests sont verts et aucun secret n'est publié.

**Important :** pose des questions seulement si elles bloquent une opération irréversible. Sinon, continue les améliorations et livre les fichiers utilisables.
