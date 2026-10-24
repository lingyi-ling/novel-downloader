# syntax=docker/dockerfile:1
#
# Container image for the Novel Downloader local REST API (``novel-api``).
#
# Build from the REPOSITORY ROOT (the build context must contain pyproject.toml,
# setup.py, src/ and packaging/):
#
#   docker build -f packaging/Dockerfile -t novel-downloader-api:local .
#
# See packaging/README-docker.md for run, volume, token and health-check notes.

FROM python:3.12-slim

# - no .pyc files: the source tree stays read-only for the runtime user
# - unbuffered output: logs show up immediately in ``docker logs``
# - /data is the single persistent location (config, state, cache, exports)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NOVEL_HOME=/data \
    NOVEL_CACHE_DIR=/data/novel_cache

WORKDIR /app

# The repository root is the build context; .dockerignore keeps .git, .venv,
# build artefacts, tests, docs, the Android tree and local data out of it.
COPY . /app

# Editable install keeps /app/packaging and /app/src in sync with the wrapper
# scripts. Only the api and music extras are needed at runtime. lxml, Pillow,
# pycryptodome and aiohttp publish manylinux wheels for CPython 3.12, so no
# compiler toolchain is installed; add build-essential if you target an
# architecture without wheels.
RUN python -m pip install --no-cache-dir -e ".[api,music]"

# Windows checkouts can carry CRLF line endings; dash would treat the trailing
# \r as part of each command. Normalise, then make the entrypoint executable.
RUN sed -i 's/\r$//' /app/packaging/docker-entrypoint.sh \
    && chmod 0755 /app/packaging/docker-entrypoint.sh

# Unprivileged runtime user. Only /data needs to be writable.
RUN groupadd --gid 10001 novel \
    && useradd --uid 10001 --gid novel --home-dir /data \
        --no-create-home --shell /usr/sbin/nologin novel \
    && mkdir -p /data/downloads /data/novel_cache \
    && chown -R novel:novel /data

# Declare the persistent data volume: NOVEL_HOME, settings.toml, state.json,
# watchlist.json, the cache and the exported files all live here.
VOLUME ["/data"]

USER novel

# Run from the data volume so config lookup finds /data/settings.toml and the
# default general.output_dir ("./downloads") resolves to /data/downloads.
WORKDIR /data

EXPOSE 8081

STOPSIGNAL SIGTERM

# /api/v1/health is behind the token dependency, so the probe sends the
# configured token when there is one. 127.0.0.1 works because uvicorn listens
# on 0.0.0.0 inside the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; token = os.environ.get('NOVEL_API_TOKEN', ''); headers = {'Authorization': 'Bearer ' + token} if token else {}; urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8081/api/v1/health', headers=headers), timeout=3).read()"]

ENTRYPOINT ["/app/packaging/docker-entrypoint.sh"]
