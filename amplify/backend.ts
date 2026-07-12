// amplify/backend.ts
// agent-maker-checker — Amplify Gen 2 composition root.
//
// A {manifest, configure} composition pattern: each domain exposes a
// `createXResources(utils)` factory returning `{ manifest, configure }`.
// `manifest` entries (if any) get merged into `defineBackend()`;
// `configure(backend, ...)` then does the actual raw-CDK wiring once `backend`
// exists. This repo has three domains:
//
//   data  -> amplify/backend/data.ts  -> 3 DynamoDB tables (CONTRACTS §4)
//   proxy -> amplify/backend/proxy.ts -> enforcement-proxy container Lambda + HttpApi
//   agent -> amplify/backend/agent.ts -> Nova agent container Lambda + HttpApi
//
// No auth/storage in this repo — the proxy/agent endpoints are deliberately
// public (authType NONE) for a test-mode demo; see the comment in proxy.ts.

import { defineBackend } from '@aws-amplify/backend';
import { createDataResources } from './backend/data';
import { createProxyResources } from './backend/proxy';
import { createAgentResources } from './backend/agent';
import { utils } from './backend/lib/helper';

const dataResources = createDataResources(utils);
const proxyResources = createProxyResources(utils);
const agentResources = createAgentResources(utils);

const backend = defineBackend({
  ...dataResources.manifest, // {} — tables are raw CDK, not Amplify-managed
  ...proxyResources.manifest, // {} — Docker Lambda is raw CDK, not Amplify-managed
  ...agentResources.manifest, // {} — Docker Lambda is raw CDK, not Amplify-managed
});

// Order matters: the DynamoDB tables must exist before proxy.configure() can
// read their table names and grant read/write access on them.
const dataTables = dataResources.configure(backend);

// Reserved for future cross-cutting resources (auth, storage, a shared VPC,
// ...) that would sit alongside data/proxy/agent in a bigger app. Empty here;
// kept for shape parity so this repo can grow without a signature change.
const shared = {};

const proxyConfigured = proxyResources.configure(backend, shared, dataTables);

// The Nova agent routes every tool call through the enforcement proxy, so it
// needs the proxy's URL — configure it AFTER proxy.configure().
const agentConfigured = agentResources.configure(backend, shared, { proxyUrl: proxyConfigured.apiUrl });

// CONTRACTS.md §6 — the dashboard reads amplify_outputs.json -> custom.proxyUrl
// (flips its badge to LIVE) and custom.agentUrl (enables the Nova playground);
// it falls back to simulated mode if either is unreachable.
backend.addOutput({
  custom: {
    // The HttpApi (execute-api) endpoint — resolves on every network, unlike the
    // raw Lambda Function URL domain. See proxy.ts for why.
    proxyUrl: proxyConfigured.apiUrl,
    proxyFunctionUrl: proxyConfigured.functionUrl,
    agentUrl: agentConfigured.apiUrl,
  },
});
