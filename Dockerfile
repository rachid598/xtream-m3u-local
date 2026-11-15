FROM python:3.13-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7777 \
    WEB_PORT=7778

WORKDIR /opt/xtream-m3u
COPY app/ /opt/xtream-m3u/app/

RUN mkdir -p /data && chown 99:100 /data
USER 99:100
EXPOSE 7777 7778

CMD ["python", "-m", "app.server"]
