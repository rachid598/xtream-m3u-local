#!/bin/sh
# Créer le conteneur sans Docker Compose (docker run).
#   cp .env.example .env   # puis renseigner XTREAM_URL, XTREAM_USERNAME, XTREAM_PASSWORD
#   sh docker-create.sh            # construit l'image localement
#   sh docker-create.sh --ghcr     # utilise ghcr.io/rachid598/xtream-m3u-local:latest
#   sh docker-create.sh --replace  # remplace un conteneur existant (les données sont conservées)
set -eu
cd "$(dirname "$0")"
IMAGE=xtream-m3u-local:latest
REPLACE=0
for arg in "$@"; do
  case "$arg" in
    --ghcr) IMAGE=ghcr.io/rachid598/xtream-m3u-local:latest ;;
    --replace) REPLACE=1 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done
[ -f .env ] || { echo "Créer le fichier .env à partir de .env.example" >&2; exit 1; }
# Dossier des données : DATA_PATH dans .env, sinon ./data
DATA_PATH=$(sed -n 's/^DATA_PATH=//p' .env | tail -n 1)
DATA_PATH=${DATA_PATH:-./data}
case "$DATA_PATH" in /*) ;; *) DATA_PATH="$(pwd)/${DATA_PATH#./}" ;; esac
mkdir -p "$DATA_PATH"

if [ "$IMAGE" = "xtream-m3u-local:latest" ]; then
  docker build -t "$IMAGE" .
else
  docker pull "$IMAGE"
fi
if docker container inspect xtream-m3u-local >/dev/null 2>&1; then
  if [ "$REPLACE" = 1 ]; then
    docker rm -f xtream-m3u-local >/dev/null
  else
    echo "Le conteneur xtream-m3u-local existe déjà : relance avec --replace (les données sont conservées)." >&2
    exit 1
  fi
fi
docker run -d \
  --name xtream-m3u-local \
  --restart unless-stopped \
  --env-file .env \
  -p 7777:7777 \
  -p 7778:7778 \
  -v "$DATA_PATH:/data" \
  "$IMAGE"
printf '%s\n' "Données : $DATA_PATH" \
  'Interface Web   : http://IP_DU_SERVEUR:7778/' \
  'Playlist exemple: http://IP_DU_SERVEUR:7777/p/salon.m3u'
