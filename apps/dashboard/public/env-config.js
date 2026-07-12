// Default (no proxy configured). Loaded by index.html before the app bundle.
//
// This file exists purely as a runtime-config seam for docker-compose: the
// `dashboard` container gets PROXY_URL at container *start* (see
// docker-compose.yml), which a pre-built static bundle can't read via
// import.meta.env (that's build-time only). The nginx image's entrypoint
// (see Dockerfile) overwrites this file at container start with the real
// value. Left blank here for `vite dev` / `vite preview` and for a plain
// static deploy (e.g. Amplify Hosting), where amplify_outputs.json or
// VITE_PROXY_URL take over instead — see src/api.js for the full precedence.
window.__PROXY_URL__ = "";
