# Xtream M3U Local — un seul conteneur, Python standard library uniquement.
FROM python:3.13-alpine

LABEL org.opencontainers.image.title="Xtream M3U Local" \
      org.opencontainers.image.description="Playlists M3U personnalisées et relais TS depuis l'API Xtream (port 7777 lecteurs, 7778 interface Web)" \
      org.opencontainers.image.source="https://github.com/rachid598/xtream-m3u-local" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7777 \
    WEB_PORT=7778 \
    DATA_DIR=/data \
    PUID=99 \
    PGID=100

WORKDIR /opt/xtream-m3u
COPY app/ /opt/xtream-m3u/app/
RUN apk add --no-cache tzdata && mkdir -p /data && chown 99:100 /data

# Le processus démarre en root uniquement pour rendre /data inscriptible
# (dossier appdata créé par Docker/Unraid), puis abandonne ses droits (PUID/PGID, 99:100 par défaut).
EXPOSE 7777 7778

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','7777'), timeout=3)" || exit 1

CMD ["python", "-m", "app"]
