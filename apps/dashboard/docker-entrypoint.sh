#!/bin/sh
# Installed at /docker-entrypoint.d/40-dashboard-env.sh — the official nginx
# image auto-runs every executable *.sh script found there before starting
# nginx (see /docker-entrypoint.sh in nginx:alpine).
#
# docker-compose.yml sets PROXY_URL and AGENT_URL on the `dashboard` container
# at *start* time (browser-reachable host URLs). A pre-built static Vite bundle
# has no way to read those directly, so we bake them into env-config.js here;
# index.html loads that file before the app bundle, and src/api.js reads them
# back via window.__PROXY_URL__ / window.__AGENT_URL__.
set -eu
{
  echo "window.__PROXY_URL__ = \"${PROXY_URL:-}\";"
  echo "window.__AGENT_URL__ = \"${AGENT_URL:-}\";"
} > /usr/share/nginx/html/env-config.js
