# Prompt à transmettre à l'IA suivante

Tu reprends le projet existant **Xtream M3U Local** (version 3.18.0). **Commence par lire `PASSATION_IA.md`, puis `README.md` et le code (`app/`, `tests/`). Ne repars pas de zéro.**

## Contexte

Serveur Unraid « Tower » (appdata sur pool NVMe), abonnement Xtream Codes valide : `player_api.php` fonctionne et la lecture directe `/live/USER/PASS/ID.ts` aussi, mais `get.php` ne donne pas de playlist. Le projet reconstruit les playlists depuis l'API Xtream :

- **un seul conteneur Docker**, Python 3.13 bibliothèque standard, interface HTML/CSS/JS sans framework ;
- **port 7777** : `/p/<nom>.m3u` (playlists personnalisées à URL permanente : TV en direct, films et séries, TS ou HLS, replay), `/p/<nom>.xml` (EPG complété par l'API), serveur Xtream local `player_api.php` par appareil, relais TS/HLS/VOD avec limite de connexions ;
- **port 7778** : interface Web en français, responsive — vues « Mes playlists » (éditeur en 4 parties Contenu/Organisation/Options/Guide ; direct/films/séries, règles, favoris, noms, numéros, groupes perso « TNT/Canal+ », doublons, inclusions/exclusions, ordre et noms de groupes, plusieurs playlists) « Guide TV » (maintenant, ce soir, recherche, aperçu ▶), « Catalogue fournisseur » (lecture seule, export JSON), « Appareils » (codes, identifiants Xtream locaux, priorité) et « Système » (lectures en cours, historique, diagnostic, sauvegarde) ;
- données dans `/data/playlists.json` ; configuration Xtream uniquement par variables d'environnement (Compose / `.env` / template Unraid).

## Règles

1. Ne jamais écrire d'identifiant Xtream réel dans le code, les tests, les journaux, la documentation ou GitHub.
2. Ne pas casser : génération M3U, relais TS, URL permanentes, persistance, préservation du catalogue fournisseur.
3. Garder les ports 7777/7778, un seul conteneur, aucune dépendance externe sans raison forte.
4. Toute modification = tests (`python -m unittest discover -s tests -t .`) + si l'interface change, `node --check app/static/app.js` et le scénario `node tests/e2e_browser.mjs` contre `python -m tools.demo`.
5. Ne pas annoncer qu'un dépôt, une image GHCR ou un déploiement fonctionne sans l'avoir vérifié.
6. Mettre à jour `PASSATION_IA.md` à chaque étape importante (modifications, décisions, tests, problèmes, prochaines étapes).

## Prochaines tâches probables

Voir la section 8 de `PASSATION_IA.md` : retours des essais réels sur Unraid et Android TV, comparaison catalogue fournisseur / application TV, publication GHCR, évolutions (ordre et renommage des chaînes, favoris, VOD/séries, HLS).

Pose des questions seulement si elles bloquent une opération irréversible ; sinon avance et livre des fichiers utilisables.
