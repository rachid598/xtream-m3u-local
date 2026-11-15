#!/bin/sh
# Créer directement le conteneur sans Docker Compose.
# Nécessite un fichier .env créé à partir de .env.example.
set -eu
[ -f .env ] || { echo 'Créer le fichier .env à partir de .env.example'; exit 1; }
docker build -t xtream-m3u-local:latest .
if docker container inspect xtream-m3u-local >/dev/null 2>&1; then
  echo "Le conteneur xtream-m3u-local existe deja : arrete-le ou supprime-le manuellement."
  exit 1
fi
docker run -d \
  --name xtream-m3u-local \
  --restart unless-stopped \
  --env-file .env \
  -p 7777:7777 \
  -p 7778:7778 \
  -v xtream-m3u-data:/data \
  xtream-m3u-local:latest
printf '%s\n' 'Service : http://IP_UNRAID:7777/playlist.m3u' 'Interface Web : http://IP_UNRAID:7778'
