# UAP Finder server image (standalone mode: HTTP API on 8000, UDP ingest on 9999).
# Build from the repository root:  docker build -f deploy/docker/server.Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SKYNET_STORAGE_DIR=/var/lib/uapfinder/clips

WORKDIR /app
COPY common/ common/
COPY server/ server/
# Editable installs keep server/migrations next to the package, where
# `skynet-server migrate` looks for them.
RUN pip install -e ./common -e ./server uvloop \
 && useradd --system --home-dir /var/lib/uapfinder uapfinder \
 && mkdir -p /var/lib/uapfinder/clips \
 && chown -R uapfinder /var/lib/uapfinder

USER uapfinder
EXPOSE 8000 9999/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status != 200)"
CMD ["skynet-server", "serve"]
