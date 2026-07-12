// amplify/backend.ts
// agent-maker-checker — Amplify Gen 2 composition root.
//
// Single-app version of the CloudMorph {manifest, configure} pattern
// (see amplify/backend/tessera.ts in the CloudMorph mono-repo for the
// original): each domain gets a `createXResources(utils)` factory returning
// `{ manifest, configure }`. `manifest` entries (if any) get merged into
// `defineBackend()`; `configure(backend, ...)` then does the actual raw-CDK
// wiring once `backend` exists. This repo only has two domains, so it's much
// smaller than CloudMorph's 8-domain composition:
//
//   data  -> amplify/backend/data.ts  -> 3 DynamoDB tables (CONTRACTS §4)
//   proxy -> amplify/backend/proxy.ts -> container Lambda + public Function URL
//
// No auth/storage in this repo — the proxy Function URL is deliberately
// public (authType NONE) for a test-mode demo; see the comment in proxy.ts.

import { defineBackend } from '@aws-amplify/backend';
import { createDataResources } from './backend/data';
import { createProxyResources } from './backend/proxy';
import { utils } from './backend/lib/helper';

const dataResources = createDataResources(utils);
const proxyResources = createProxyResources(utils);

const backend = defineBackend({
  ...dataResources.manifest, // {} — tables are raw CDK, not Amplify-managed
  ...proxyResources.manifest, // {} — Docker Lambda is raw CDK, not Amplify-managed
});

// Order matters: the DynamoDB tables must exist before proxy.configure() can
// read their table names and grant read/write access on them.
const dataTables = dataResources.configure(backend);

// Reserved for future cross-cutting resources (auth, storage, a shared VPC,
// ...) that would sit alongside data/proxy in a bigger app — this is the
// `shared` object CloudMorph's backend.ts threads through every
// `configure(backend, shared, ...)` call. Empty here; kept for shape parity
// so this repo can grow into the same pattern without a signature change.
const shared = {};

const proxyConfigured = proxyResources.configure(backend, shared, dataTables);

// CONTRACTS.md §6 — the dashboard reads amplify_outputs.json ->
// custom.proxyUrl, calls GET {proxyUrl}/metrics to flip its badge to LIVE,
// and falls back to simulated mode if unreachable.
backend.addOutput({
  custom: {
    proxyUrl: proxyConfigured.functionUrl,
  },
});
