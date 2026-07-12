// amplify/backend/agent.ts
//
// Provisions the Nova demo agent (amplify/functions/agent/server.py) as a
// container Lambda behind a public HttpApi — same shape as proxy.ts, so the
// dashboard playground can call a live Bedrock-backed agent instead of only
// simulating one. The image is built directly from agent/Dockerfile at
// deploy time (CDK asset bundling), mirroring proxy.ts's
// DockerImageCode.fromImageAsset pattern exactly.

import * as path from 'path';
import { fileURLToPath } from 'url';
import { Duration, Stack } from 'aws-cdk-lib';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { CorsHttpMethod, HttpApi, HttpMethod } from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import { getOrCreateStack, type Utils } from './lib/helper';

const AGENT_STACK = 'AmcAgent';

// This file lives at amplify/backend/agent.ts; the agent Dockerfile lives at
// amplify/functions/agent/. Climb one level to amplify/, then into
// functions/agent/. amplify/ is ESM ("type": "module"), so __dirname does not
// exist — derive it from import.meta.url, same technique as proxy.ts.
const MODULE_DIR = path.dirname(fileURLToPath(import.meta.url));
const AGENT_IMAGE_DIR = path.join(MODULE_DIR, '..', 'functions', 'agent');

const BEDROCK_MODEL_ID = 'us.amazon.nova-lite-v1:0';
// The cross-region inference profile's own model component — used to build
// the foundation-model ARN Bedrock still checks permissions against even
// when the caller invokes the inference-profile ID above.
const BEDROCK_FOUNDATION_MODEL_ID = 'amazon.nova-lite-v1:0';

export interface AgentConfigured {
  apiUrl: string;
  agentFunction: lambda.DockerImageFunction;
}

interface AgentDeps {
  proxyUrl: string;
}

interface AgentResources {
  manifest: Record<string, never>;
  configure: (backend: any, shared: Record<string, unknown>, deps: AgentDeps) => AgentConfigured;
}

export const createAgentResources = (_utils: Utils): AgentResources => {
  // Same as the proxy: the agent ships as a single container image, built
  // directly as raw CDK inside `configure()` — nothing goes in `manifest`.
  const manifest = {};

  const configure = (
    backend: any,
    _shared: Record<string, unknown>,
    deps: AgentDeps,
  ): AgentConfigured => {
    const stack = getOrCreateStack(backend, AGENT_STACK);

    // Default: CDK builds the image from agent/Dockerfile at deploy time
    // (fromImageAsset). Escape hatch: same as proxy.ts's AMC_ECR_IMAGE — if
    // AMC_AGENT_ECR_IMAGE ("<repoName>:<tag>") is set in the build
    // environment, reference that pre-pushed ECR image instead.
    const ecrImage = process.env.AMC_AGENT_ECR_IMAGE;
    let imageCode: lambda.DockerImageCode;
    if (ecrImage) {
      const [repoName, tag] = ecrImage.split(':');
      const repo = ecr.Repository.fromRepositoryName(stack, 'AmcAgentEcr', repoName);
      imageCode = lambda.DockerImageCode.fromEcr(repo, { tagOrDigest: tag || 'latest' });
    } else {
      imageCode = lambda.DockerImageCode.fromImageAsset(AGENT_IMAGE_DIR);
    }

    const agentFn = new lambda.DockerImageFunction(stack, 'AmcAgentFunction', {
      functionName: 'amc-agent',
      code: imageCode,
      memorySize: 1024,
      timeout: Duration.seconds(60),
      environment: {
        PROXY_URL: deps.proxyUrl,
        BEDROCK_MODEL_ID,
        // NOTE: deliberately NOT setting AWS_REGION here — same reasoning as
        // proxy.ts: it's a Lambda-reserved environment variable name that
        // CloudFormation rejects setting explicitly. Lambda injects it
        // automatically, and server.py's boto3 client reads it via
        // os.environ.get("AWS_REGION", ...) at call time.
      },
    });

    // Bedrock creds come from the Lambda execution role, not static keys —
    // grant InvokeModel/InvokeModelWithResponseStream on both the
    // foundation-model ARN and the cross-region inference-profile ARN Nova
    // Lite is invoked through (server.py calls converse with
    // BEDROCK_MODEL_ID, the "us." inference profile).
    const stackScope = Stack.of(agentFn);
    const region = stackScope.region;
    const account = stackScope.account;
    agentFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
        resources: [
          `arn:aws:bedrock:*::foundation-model/${BEDROCK_FOUNDATION_MODEL_ID}`,
          `arn:aws:bedrock:${region}:${account}:inference-profile/${BEDROCK_MODEL_ID}`,
        ],
      }),
    );

    // Browser-facing endpoint: an HttpApi (API Gateway v2), same reasoning as
    // proxy.ts — the execute-api domain resolves on every network, unlike the
    // raw Lambda Function URL domain. One greedy route forwards everything to
    // the container Lambda; FastAPI does the real routing. Public (no
    // authorizer) — test-mode demo only; CORS "*" for the same reason.
    const httpApi = new HttpApi(stack, 'AmcAgentHttpApi', {
      apiName: 'amc-agent-api',
      corsPreflight: {
        allowOrigins: ['*'],
        allowMethods: [CorsHttpMethod.ANY],
        allowHeaders: ['*'],
      },
    });
    const integration = new HttpLambdaIntegration('AmcAgentIntegration', agentFn);
    httpApi.addRoutes({ path: '/{proxy+}', methods: [HttpMethod.ANY], integration });
    httpApi.addRoutes({ path: '/', methods: [HttpMethod.ANY], integration });

    return { apiUrl: httpApi.apiEndpoint, agentFunction: agentFn };
  };

  return { manifest, configure };
};
