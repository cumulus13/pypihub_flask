# syntax=docker/dockerfile:1

# PyPihub: local PyPI server with caching and uploads, served by gunicorn.
# Build:  docker build -t pypihub .
# Run:    docker run -d -p 5000:5000 -v pypihub-data:/data pypihub

ARG PYTHON_VERSION=3.12

# ---- build: install dependencies and PyPihub into a virtualenv ---------------
FROM python:${PYTHON_VERSION}-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src

# Dependencies first, so code changes do not invalidate this layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt gunicorn

COPY . .
RUN pip install --no-deps .

# ---- runtime ------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="PyPihub" \
      org.opencontainers.image.description="Local PyPI server with caching and package upload" \
      org.opencontainers.image.source="https://github.com/cumulus13/pypihub_flask" \
      org.opencontainers.image.licenses="LGPL-3.0-or-later"

# All state lives in /data: uploaded packages, the PyPI cache, the user database and the config file.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BASE_DIR=/data \
    LOCAL_PKG_DIR=/data/packages \
    CACHE_DIR=/data/cache \
    CONFIGFILE=/data/pypihub.ini \
    DB_PATH=/data/pypihub.db \
    HOST=0.0.0.0 \
    PORT=5000

RUN groupadd --system --gid 10001 pypihub \
 && useradd --system --uid 10001 --gid pypihub --home-dir /data --shell /usr/sbin/nologin pypihub \
 && mkdir -p /data/packages /data/cache \
 && chown -R pypihub:pypihub /data

COPY --from=build /opt/venv /opt/venv
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/pypihub-entrypoint

USER 10001:10001
WORKDIR /data
VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '5000'), timeout=4)"]

ENTRYPOINT ["pypihub-entrypoint"]

# gthread workers: a slow PyPI download streams to the client without blocking other requests
# and is not killed by the worker timeout. Tune with WEB_CONCURRENCY and GUNICORN_THREADS.
CMD ["sh", "-c", "exec gunicorn --bind \"0.0.0.0:${PORT}\" --worker-class gthread --workers \"${WEB_CONCURRENCY:-2}\" --threads \"${GUNICORN_THREADS:-8}\" --timeout 120 --graceful-timeout 30 --access-logfile - pypihub.pypihub:app"]
