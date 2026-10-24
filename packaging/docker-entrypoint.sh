#!/bin/sh
# ---------------------------------------------------------------------------
# Container entrypoint for the Novel Downloader REST API (``novel-api``).
#
# Security default: the API is bound to 0.0.0.0 inside the container, so a
# token is mandatory. Set NOVEL_API_TOKEN (or pass ``--token`` after the image
# name). To deliberately run without authentication, set
# NOVEL_API_ALLOW_NO_TOKEN=1; this is only acceptable on a trusted network.
#
# The shell is POSIX ``sh`` (dash in python:3.12-slim): keep it bash-free.
# ---------------------------------------------------------------------------
set -eu

err() {
    printf '%s\n' "$*" >&2
}

# -- Accept common spellings of "yes" --------------------------------------
ALLOW_NO_TOKEN=0
case "${NOVEL_API_ALLOW_NO_TOKEN:-0}" in
    1 | [Tt][Rr][Uu][Ee] | [Yy][Ee][Ss]) ALLOW_NO_TOKEN=1 ;;
esac

# -- Is a token configured in the environment? ----------------------------
# The token is passed in via NOVEL_API_TOKEN only. This is deliberate: the
# image HEALTHCHECK runs as a separate process with the container's configured
# environment, so a token supplied as a runtime ``--token`` argument would not
# be visible to the probe.
HAVE_TOKEN=0
case "${NOVEL_API_TOKEN:-}" in
    *[![:space:]]*) HAVE_TOKEN=1 ;;
esac
if [ "$HAVE_TOKEN" -eq 1 ]; then
    export NOVEL_API_TOKEN
fi

if [ "$HAVE_TOKEN" -eq 0 ]; then
    if [ "$ALLOW_NO_TOKEN" -eq 1 ]; then
        err "############################################################"
        err "# WARNING: NOVEL_API_TOKEN is empty and"
        err "# NOVEL_API_ALLOW_NO_TOKEN=1 was set explicitly."
        err "# The API is reachable on 0.0.0.0:8081 WITHOUT authentication."
        err "# Anyone who can reach this port can control downloads and read"
        err "# the exported files under /downloads."
        err "############################################################"
    else
        cat >&2 <<'EOF'
============================================================
 REFUSING TO START: NOVEL_API_TOKEN is not set.

 This image binds the API to 0.0.0.0:8081 inside the
 container, so authentication is required by default.

 Pass a token, for example:

   docker run ... -e NOVEL_API_TOKEN="$(openssl rand -hex 32)" ...

 For a deliberately unauthenticated instance on a trusted
 network only, set:

   -e NOVEL_API_ALLOW_NO_TOKEN=1

============================================================
EOF
        exit 1
    fi
fi

# -- Data directories (the Dockerfile already creates these) --------------
NOVEL_HOME="${NOVEL_HOME:-/data}"
NOVEL_CACHE_DIR="${NOVEL_CACHE_DIR:-${NOVEL_HOME}/novel_cache}"
export NOVEL_HOME NOVEL_CACHE_DIR

if ! mkdir -p "${NOVEL_HOME}/downloads" "${NOVEL_CACHE_DIR}" 2>/dev/null; then
    err "WARNING: could not create ${NOVEL_HOME}/downloads or ${NOVEL_CACHE_DIR}."
    err "Check the mounted volume permissions (the container runs as UID 10001)."
fi

# Config lookup checks the current directory first. Running from the data
# volume makes /data/settings.toml authoritative and resolves relative paths
# (the default ``output_dir = "./downloads"``) to /data/downloads.
if ! cd "$NOVEL_HOME" 2>/dev/null; then
    err "ERROR: cannot enter NOVEL_HOME=${NOVEL_HOME}."
    exit 1
fi

if [ "$HAVE_TOKEN" -eq 1 ]; then
    AUTH_LABEL="token auth enabled"
else
    AUTH_LABEL="AUTH DISABLED"
fi
err "novel-api: data=${NOVEL_HOME} downloads=${NOVEL_HOME}/downloads (${AUTH_LABEL})"

exec novel-api --host 0.0.0.0 --port 8081 "$@"
