// amplify/backend/lib/helper.ts
//
// Small shared utilities for the amplify/backend/* resource factories.
// Mirrors CloudMorph's amplify/backend/lib/helper.ts, trimmed to what this
// single-app repo actually needs:
//   - resolveBackendEntry   — normalizes a TS entry path (kept for parity /
//                             future use; neither data.ts nor proxy.ts
//                             currently uses `defineFunction`, since the
//                             proxy ships as a container image, not an
//                             esbuild-bundled Lambda).
//   - getOrCreateStack      — lets data.ts and proxy.ts each own a named CDK
//                             stack without coordinating who creates it first.
//   - Utils / utils         — the small helpers passed into
//                             createDataResources(utils) / createProxyResources(utils).
//   - resolveBackendSecret  — the one non-obvious piece: lets a plain-CDK
//                             construct read an `ampx sandbox secret set`
//                             value, which normally only auto-resolves inside
//                             `defineFunction`.

import * as path from 'path';
import type { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';
import * as ddb from 'aws-cdk-lib/aws-dynamodb';
import type { IFunction } from 'aws-cdk-lib/aws-lambda';
import { secret } from '@aws-amplify/backend';

/**
 * Normalizes relative backend resource paths so they resolve correctly when
 * Amplify runs from the repo root. Not currently exercised — kept for parity
 * with CloudMorph's helper.ts and for any future TS Lambda this repo adds
 * via `defineFunction`.
 */
export const resolveBackendEntry = (entry: string): string => {
  let normalized = entry;

  if (entry.startsWith('../')) {
    normalized = path.join('amplify', entry.slice(3));
  } else if (entry.startsWith('./')) {
    normalized = path.join('amplify/backend', entry.slice(2));
  }

  if (!path.isAbsolute(normalized)) {
    normalized = path.join(process.cwd(), normalized);
  }

  return normalized;
};

/**
 * Returns the named CDK stack if it already exists on this synth (e.g. a
 * sibling resource factory created it first), otherwise creates it via
 * `backend.createStack`. Mirrors CloudMorph's tessera.ts `getOrCreateStack`.
 */
export const getOrCreateStack = (backend: any, name: string): cdk.Stack => {
  const scope = backend?.stack ?? backend;
  const existing = scope?.node?.tryFindChild(name) as cdk.Stack | undefined;
  return existing ?? backend.createStack(name);
};

export interface Utils {
  grant: {
    ddbRW: (table: ddb.Table, fn: IFunction) => void;
  };
}

export const utils: Utils = {
  grant: {
    ddbRW: (table, fn) => table.grantReadWriteData(fn),
  },
};

// ── Amplify Gen2 secrets outside `defineFunction` ───────────────────────────
//
// `secret('NAME')` (from @aws-amplify/backend) auto-resolves when used as a
// value inside `defineFunction({ environment: { KEY: secret('NAME') } })` —
// Amplify's own function-provisioning code has a BackendIdentifier available
// internally to do that resolution.
//
// Our proxy Lambda is a plain `lambda.DockerImageFunction` (raw CDK, created
// directly in proxy.ts's `configure()`), so there's no `defineFunction` doing
// that for us. `secret('NAME').resolve(scope, backendIdentifier)` itself IS
// public API — `BackendSecret`/`BackendIdentifier` are exported from
// @aws-amplify/plugin-types — but *constructing* a BackendIdentifier is not:
// the only place that logic lives is the un-exported `getBackendIdentifier()`
// inside @aws-amplify/backend, which just reads three CDK context keys that
// the `ampx` CLI stamps onto the app at synth time
// (see @aws-amplify/platform-core's CDKContextKey enum — these three string
// keys are stable across Gen2 releases). We read them directly here rather
// than importing @aws-amplify/backend's internal module path.
const CDK_CONTEXT_KEYS = {
  namespace: 'amplify-backend-namespace',
  name: 'amplify-backend-name',
  type: 'amplify-backend-type',
} as const;

const getBackendIdentifierFromContext = (scope: Construct) => {
  const namespace = scope.node.tryGetContext(CDK_CONTEXT_KEYS.namespace);
  const name = scope.node.tryGetContext(CDK_CONTEXT_KEYS.name);
  const type = scope.node.tryGetContext(CDK_CONTEXT_KEYS.type);

  if (typeof namespace !== 'string' || typeof name !== 'string' || typeof type !== 'string') {
    throw new Error(
      'Could not read Amplify backend identity from CDK context (amplify-backend-*). ' +
        'This only works running under `ampx sandbox` or `ampx pipeline-deploy` — ' +
        'a bare `cdk synth` will not set these context keys.',
    );
  }

  return { namespace, name, type } as const;
};

/**
 * Resolves an `ampx sandbox secret set <name>` (or Amplify Console → App
 * settings → Secrets, for deployed branches) value to its plaintext string,
 * for use directly as a plain-CDK construct's environment variable value.
 *
 * `unsafeUnwrap()` is CDK's sanctioned escape hatch for exactly this case:
 * Lambda `Environment.Variables` supports CloudFormation dynamic references
 * (`{{resolve:ssm-secure:...}}`), so the plaintext secret is never written
 * into the synthesized CFN template — Lambda resolves it at deploy time, not
 * at synth time.
 */
export const resolveBackendSecret = (scope: Construct, secretName: string): string => {
  const backendIdentifier = getBackendIdentifierFromContext(scope);
  // `as any`: our locally-reconstructed identifier is structurally identical
  // to @aws-amplify/plugin-types' BackendIdentifier, but we avoid taking an
  // explicit dependency on that package's types just for this cast.
  return secret(secretName).resolve(scope, backendIdentifier as any).unsafeUnwrap();
};
