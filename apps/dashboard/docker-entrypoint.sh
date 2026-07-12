#!/bin/sh
# Installed at /docker-entrypoint.d/40-dashboard-env.sh — the official nginx
# image auto-runs every executable *.sh script found there before starting
# nginx (see /docker-entrypoint.sh in nginx:alpine).
#
# docker-compose.yml sets PROXY_URL on the `dashboard` container at *start*
# time (e.g. http://proxy:8000, the compose service DNS name). A pre-built
# static Vite bundle has no way to read that directly, so we bake it into
# env-config.js here; index.html loads that file before the app bundle, and
# src/api.js reads it back via window.__PROXY_URL__.
set -eu
echo "window.__PROXY_URL__ = \"${PROXY_URL:-}\";" > /usr/share/nginx/html/env-config.js
