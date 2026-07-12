// amplify/backend/data.ts
//
// Provisions the three DynamoDB tables from CONTRACTS.md §4. Agent A's
// proxy/state.py DynamoStateStore / audit / approvals stores read these by
// name via DDB_STATE_TABLE / DDB_AUDIT_TABLE / DDB_APPROVALS_TABLE (wired in
// proxy.ts). Table names are frozen in CONTRACTS.md §2 — do not rename.
//
// No Amplify-managed (esbuild-bundled) functions here, so `manifest` is
// empty — the tables are raw CDK resources created directly in `configure()`,
// a small raw-CDK factory pattern for the DynamoDB tables.

import { RemovalPolicy } from 'aws-cdk-lib';
import * as ddb from 'aws-cdk-lib/aws-dynamodb';
import { getOrCreateStack, type Utils } from './lib/helper';

const DATA_STACK = 'amc-data';

export interface DataConfigured {
  stateTable: ddb.Table;
  auditTable: ddb.Table;
  approvalsTable: ddb.Table;
}

interface DataResources {
  manifest: Record<string, never>;
  configure: (backend: any) => DataConfigured;
}

export const createDataResources = (_utils: Utils): DataResources => {
  const manifest = {};

  const configure = (backend: any): DataConfigured => {
    const stack = getOrCreateStack(backend, DATA_STACK);

    // ── amc-state ──────────────────────────────────────────────────────────
    // Velocity windows + freeze registry + capture volume, one table
    // discriminated by `pk` prefix:
    //   pk="vel#{agent}#{tool}#{payee}"  -> sum_paise, count, window_start, ttl
    //   pk="freeze#{agent}#{tool}"       -> frozen, reason, frozen_at (no ttl —
    //                                       persists until POST
    //                                       /admin/unfreeze/{agent}/{tool})
    //   pk="cap#{agent}"                 -> captured_paise, refunded_paise,
    //                                       window_start, ttl
    // TTL only cleans up the velocity/capture items; freeze items intentionally
    // have no `ttl` attribute set so they don't expire on their own.
    const stateTable = new ddb.Table(stack, 'AmcStateTable', {
      tableName: 'amc-state',
      partitionKey: { name: 'pk', type: ddb.AttributeType.STRING },
      billingMode: ddb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY, // demo teardown — no retained state
      timeToLiveAttribute: 'ttl',
    });

    // ── amc-audit ──────────────────────────────────────────────────────────
    // sha256 hash chain. PK `chain` is the constant "main"; SK `seq`(N) so a
    // Query on chain="main" returns items in append order. Item shape: seq,
    // ts, request_id, agent_id, tool, arguments_hash, decision, policy_id,
    // reason, prev_hash, hash. Append = conditional put on
    // attribute_not_exists(seq), retried on clash — the chain is
    // intentionally serialized for tamper-evidence, so no TTL here.
    const auditTable = new ddb.Table(stack, 'AmcAuditTable', {
      tableName: 'amc-audit',
      partitionKey: { name: 'chain', type: ddb.AttributeType.STRING },
      sortKey: { name: 'seq', type: ddb.AttributeType.NUMBER },
      billingMode: ddb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    // ── amc-approvals ─────────────────────────────────────────────────────
    // HITL queue. PK `approval_id`. Item: status (pending|approved|denied),
    // agent_id, tool, arguments (JSON string), amount_paise, reason,
    // created_at, resolved_at.
    const approvalsTable = new ddb.Table(stack, 'AmcApprovalsTable', {
      tableName: 'amc-approvals',
      partitionKey: { name: 'approval_id', type: ddb.AttributeType.STRING },
      billingMode: ddb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: RemovalPolicy.DESTROY,
    });

    return { stateTable, auditTable, approvalsTable };
  };

  return { manifest, configure };
};
