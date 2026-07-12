// amplify/backend/proxy.ts
//
// Provisions the enforcement proxy as a container Lambda behind a public
// Function URL — CONTRACTS.md §7: "same image ... as the container Lambda
// (DockerImageCode.fromImageAsset('../proxy'))". The image is built directly
// from proxy/Dockerfile at deploy time (CDK asset bundling), so whatever
// Agent B ships there is exactly what runs here — no separate ECR pipeline.

import * as path from 'path';
import { fileURLToPath } from 'url';
import { Duration } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { CorsHttpMethod, HttpApi, HttpMethod } from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import { getOrCreateStack, resolveBackendSecret, type Utils } from './lib/helper';
import type { DataConfigured } from './data';

const PROXY_STACK = 'amc-proxy';

// This file lives at amplify/backend/proxy.ts; the proxy Dockerfile lives at
// amplify/functions/proxy/. Climb one level to amplify/, then into
// functions/proxy/. amplify/ is ESM ("type": "module"), so __dirname does not
// exist — derive it from import.meta.url. Anchoring here (not process.cwd())
// keeps the path correct regardless of where `ampx` is invoked from.
const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
const PROXY_IMAGE_DIR = path.join(MODULE_DIR, '..', 'functions', 'proxy');

export interface ProxyConfigured {
  apiUrl: string;
  functionUrl: string;
  proxyFunction: lambda.DockerImageFunction;
}

interface ProxyResources {
  manifest: Record<string, never>;
  configure: (
    backend: any,
    shared: Record<string, unknown>,
    dataTables: DataConfigured,
  ) => ProxyConfigured;
}

export const createProxyResources = (utils: Utils): ProxyResources => {
  // The proxy ships as a single container image — no esbuild-bundled
  // Amplify-managed function, so nothing goes in `manifest`. It's built
  // directly as raw CDK inside `configure()`.
  const manifest = {};

  const configure = (
    backend: any,
    _shared: Record<string, unknown>,
    dataTables: DataConfigured,
  ): ProxyConfigured => {
    const stack = getOrCreateStack(backend, PROXY_STACK);

    // Default: CDK builds the image from proxy/Dockerfile at deploy time
    // (fromImageAsset). Escape hatch: Amplify Hosting's build container may not
    // provide a Docker daemon — if AMC_ECR_IMAGE ("<repoName>:<tag>") is set in
    // the build environment, reference that pre-pushed ECR image instead
    // (the fromEcr pattern; image pushed out-of-band from a machine with
    // Docker).
    const ecrImage = process.env.AMC_ECR_IMAGE;
    let imageCode: lambda.DockerImageCode;
    if (ecrImage) {
      const [repoName, tag] = ecrImage.split(':');
      const repo = ecr.Repository.fromRepositoryName(stack, 'AmcProxyEcr', repoName);
      imageCode = lambda.DockerImageCode.fromEcr(repo, { tagOrDigest: tag || 'latest' });
    } else {
      imageCode = lambda.DockerImageCode.fromImageAsset(PROXY_IMAGE_DIR);
    }

    // Razorpay test-mode secrets are OPT-IN (set AMC_ENABLE_LIVE_SECRETS=1 in the
    // Amplify build environment AFTER creating the two secrets in Amplify Console
    // -> Hosting -> Secrets). Default off: DEMO_MODE=cached needs no keys, and
    // referencing never-created secrets would fail the CloudFormation deploy on
    // an unresolvable {{resolve:ssm-secure:...}} reference.
    const liveSecrets: Record<string, string> =
      process.env.AMC_ENABLE_LIVE_SECRETS === '1'
        ? {
            RAZORPAY_KEY_ID: resolveBackendSecret(stack, 'RAZORPAY_KEY_ID'),
            RAZORPAY_KEY_SECRET: resolveBackendSecret(stack, 'RAZORPAY_KEY_SECRET'),
          }
        : {};

    // DEMO_MODE=live on the deployed Lambda: the razorpay-mcp-server binary is
    // baked into the proxy image (proxy/Dockerfile), so we point the proxy at it
    // via RAZORPAY_MCP_BIN — no Docker-in-Lambda. Test-mode keys ride in as plain
    // Amplify build-env vars (rzp_test_ only; this is a test-mode demo endpoint).
    // Only wired when live, so a cached deploy still needs no keys.
    const liveEnv: Record<string, string> = {};
    if ((process.env.DEMO_MODE ?? 'cached') === 'live') {
      liveEnv.RAZORPAY_MCP_BIN = '/usr/local/bin/razorpay-mcp-server';
      // A Lambda doesn't set HOME, and razorpay-mcp-server needs it (else
      // "Error: $HOME is not defined"). /tmp is the only writable path in Lambda.
      liveEnv.HOME = '/tmp';
      if (process.env.RAZORPAY_KEY_ID) liveEnv.RAZORPAY_KEY_ID = process.env.RAZORPAY_KEY_ID;
      if (process.env.RAZORPAY_KEY_SECRET) liveEnv.RAZORPAY_KEY_SECRET = process.env.RAZORPAY_KEY_SECRET;
    }

    const proxyFn = new lambda.DockerImageFunction(stack, 'AmcProxyFunction', {
      functionName: 'amc-proxy',
      code: imageCode,
      memorySize: 1024,
      timeout: Duration.seconds(60),
      environment: {
        ...liveSecrets,
        ...liveEnv,
        // CONTRACTS.md §2 — cloud Lambda always runs the dynamodb-backed
        // stores (ephemeral, concurrent invocations -> must be shared +
        // atomic). Local docker-compose uses memory/jsonl instead.
        STATE_BACKEND: 'dynamodb',
        AUDIT_BACKEND: 'dynamodb',
        APPROVALS_BACKEND: 'dynamodb',
        DDB_STATE_TABLE: dataTables.stateTable.tableName,
        DDB_AUDIT_TABLE: dataTables.auditTable.tableName,
        DDB_APPROVALS_TABLE: dataTables.approvalsTable.tableName,
        // 'cached' replays recorded responses (no keys, no network); 'live'
        // fronts the real Razorpay MCP server with the secrets below.
        // Override per-branch via the Amplify Console build environment.
        DEMO_MODE: process.env.DEMO_MODE ?? 'cached',
        // NOTE: deliberately NOT setting AWS_REGION here. It's one of the
        // handful of Lambda-reserved environment variable names (along with
        // AWS_ACCESS_KEY_ID, AWS_LAMBDA_RUNTIME_API, etc.) — CloudFormation
        // rejects a deploy that tries to set it explicitly. Lambda injects
        // AWS_REGION into the execution environment automatically, so
        // boto3's default region resolution (CONTRACTS.md §2) already works
        // with no wiring needed.
        //
      },
    });

    // Read/write on all three tables — the proxy owns velocity/freeze/capture
    // state, appends to the audit chain, and manages the approvals queue.
    utils.grant.ddbRW(dataTables.stateTable, proxyFn);
    utils.grant.ddbRW(dataTables.auditTable, proxyFn);
    utils.grant.ddbRW(dataTables.approvalsTable, proxyFn);

    // Public Function URL, no auth. This is a TEST-MODE DEMO endpoint only —
    // rzp_test_ keys, no real money can move — whose entire point is a
    // reviewer being able to click the dashboard's LIVE badge and hit
    // GET /metrics with no setup. Do not reuse this authType: NONE + CORS "*"
    // pattern for a live-mode deployment; switch to AWS_IAM or front it with
    // a real authorizer first.
    const fnUrl = proxyFn.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      cors: {
        allowedOrigins: ['*'],
        allowedMethods: [lambda.HttpMethod.ALL],
        allowedHeaders: ['*'],
      },
    });

    // Browser-facing endpoint: an HttpApi (API Gateway v2), NOT the raw Function
    // URL. The `execute-api.<region>.amazonaws.com` domain resolves on every
    // network; the newer `*.lambda-url.on.aws` Function-URL domain fails to
    // resolve on some ISP/OS resolvers, which reads as "proxy not connected" in
    // the dashboard even though the Lambda is healthy. One greedy route forwards
    // everything to the container Lambda; FastAPI does the real routing. Public
    // (no authorizer) — test-mode demo only; CORS "*" for the same reason.
    // HttpApi + HttpLambdaIntegration in front of the container Lambda.
    const httpApi = new HttpApi(stack, 'AmcProxyHttpApi', {
      apiName: 'amc-proxy-api',
      corsPreflight: {
        allowOrigins: ['*'],
        allowMethods: [CorsHttpMethod.ANY],
        allowHeaders: ['*'],
      },
    });
    const integration = new HttpLambdaIntegration('AmcProxyIntegration', proxyFn);
    httpApi.addRoutes({ path: '/{proxy+}', methods: [HttpMethod.ANY], integration });
    httpApi.addRoutes({ path: '/', methods: [HttpMethod.ANY], integration });

    return { apiUrl: httpApi.apiEndpoint, functionUrl: fnUrl.url, proxyFunction: proxyFn };
  };

  return { manifest, configure };
};
