# agent-maker-checker

**A maker-checker for AI agents.** You don't let an employee move ₹2 lakh without a second pair of eyes — an AI agent shouldn't get more trust than an employee.

🔗 **Live demo:** https://main.d3mk2czzbnq9or.amplifyapp.com/ — **test-mode only; no real money moves.**

A merchant-side spend-control plane for AI agents that act on payments. An **enforcement proxy** sits between any AI agent and Razorpay (via Razorpay's open-source MCP server, test mode). Every tool call the agent attempts is intercepted and evaluated against declarative, payments-semantic policies **before** execution → `allow` / `deny` / `escalate-to-human`, with a hash-chained audit log, a human approval queue, and live counters.

This is not a detection model and not a chatbot. The generic allow/block plumbing is table stakes — the product is the **payments-semantic policies**: rupee-denominated caps, cross-call velocity (structuring), refund-to-capture ratios, payee history, and argument provenance (indirect prompt-injection defense).

## Try it (on the live URL)

- **Policy studio → "Create ₹500 payment link"** — Nova calls `create_payment_link`; the proxy allows it; it executes **live through Razorpay's MCP** and returns a real `rzp.io` test link (click it).
- **Console → run an attack scenario** (structuring `₹2L → 5×₹40k`, payee-swap, indirect injection) — watch the proxy deny / freeze / escalate in the live decision feed and the HITL approval queue.
- **Checker admin → tell Nova "cap refunds at ₹1 lakh"** in plain English — Nova drafts the guardrail change, you review and **Apply**, and the next agent action respects it. *Nova proposes; the deterministic engine disposes — Nova never evaluates a payment.*

## Architecture

```
Nova agent (Bedrock, container Lambda)
        │  every tool call
        ▼
enforcement proxy (FastAPI, container Lambda)  ──►  allow / deny / escalate-to-human
        │                                              │  (on allow)
   YAML policy engine                          Razorpay MCP server
   sha256 hash-chained audit log               (open-source, stdio, rzp_test_)
   decision feed · HITL approval queue                 │
        │                                              ▼
   3× DynamoDB (atomic UpdateItem)              Razorpay test-mode APIs
        │
   dashboard (Vite, Amplify Hosting): Policy studio · Console
```

**One image, two runtimes.** Both the proxy and the Nova agent are single container images (FastAPI + the AWS Lambda Web Adapter). The exact bytes that pass tests locally under `docker compose` run unchanged as container Lambdas in cloud — no handler rewrite, no forked code path.

**Live MCP inside a Lambda.** Razorpay's MCP server is stdio-only (a subprocess, not a URL) and a Lambda can't run Docker — so the proxy image **bakes in the `razorpay-mcp-server` binary** and, in `DEMO_MODE=live`, spawns it per call via `RAZORPAY_MCP_BIN`. That's how the *deployed* proxy mints real test-mode Razorpay links, not just cached replays.

**State that survives serverless.** Cross-call state (velocity windows, the freeze registry, capture volume) sits behind one `StateStore` interface: in-memory locally, **DynamoDB (atomic `UpdateItem ADD`) in cloud**. This is what lets the structuring catch fire deterministically even when the proxy is many concurrent Lambda invocations, not one warm process — see [Results](#results).

**Why Amazon Nova (Lite).** The demo agent is a support/ops worker, not a reasoning benchmark — **Nova Lite** handles the tool-use loop at a fraction of frontier-model token cost. Nova also powers a **plain-English policy copilot** in the checker admin (it drafts guardrail changes; a human approves). Model right-sizing is an engineering decision, same as instance right-sizing.

## Repo layout

```
amplify/                       BACKEND (deploys to AWS via Amplify Gen 2)
  backend.ts                   composition root (data · proxy · agent)
  backend/  proxy.ts agent.ts data.ts lib/   CDK: 2 container Lambdas + HttpApis + 3 DynamoDB tables
  functions/
    proxy/                     THE PRODUCT — FastAPI enforcement proxy + policy engine
      app.py engine.py state.py audit.py metrics.py approvals.py …
      policies_impl/           one module per policy (payments rationale in each docstring)
      policies/                the 5 YAML policies (versioned; baked into the image)
      upstream/                Razorpay MCP client (docker OR bundled binary) · cached replay
      tests/                   policy-engine + DynamoDB concurrency tests
      Dockerfile               proxy image (+ the bundled razorpay-mcp-server binary)
    agent/                     Amazon Nova agent — tool-loop + NL policy copilot (server.py)
apps/dashboard/                FRONTEND — Vite (vanilla): Policy studio + Console (Amplify Hosting)
infra/                         docker-compose.yml · razorpay-mcp.md · CONTRACTS.md
amplify.yml                    Amplify build spec (backend pipeline-deploy + frontend)
```

## Policies (v1)

| policy | enforces | default |
|---|---|---|
| `per_call_amount_cap` | per-tool rupee ceiling per call | ₹50,000 refunds · higher for payment links |
| `velocity_aggregation` | rolling-window cross-call sum per (agent, tool, payee) — **catches structuring** | ₹1,50,000 / 24h → deny + freeze + escalate |
| `payee_allowlist` | known payees pass; unknown/new payee → **escalate** (not deny) | — |
| `refund_to_capture_ratio` | refunds exceeding X% of captured volume in window → escalate | 50% / 24h |
| `provenance_check` | payment instructions originating from untrusted data (tickets, emails) → **deny** | UPI/"pay-to" patterns from untrusted sources |

Every decision returns `{decision, policy_id, reason, evaluated_in_ms}` and appends to a **sha256 hash-chained audit log** (each entry embeds the previous hash; `GET /audit/verify` walks the chain and detects tampering).

## Run it locally

```bash
cp .env.example .env                              # DEMO_MODE=cached (no keys), or add rzp_test_ keys for live
docker compose -f infra/docker-compose.yml up     # proxy :8000 · agent :8100 · dashboard :3000
```

- `DEMO_MODE=cached` (default) replays recorded Razorpay responses — the full demo runs with no keys and no network.
- `DEMO_MODE=live` fronts the real Razorpay MCP server with test-mode keys; allowed actions materialize in the Razorpay test dashboard.

**Tests** (from `amplify/functions/`):

```bash
pip install -r proxy/requirements-dev.txt && pytest -q                          # policy engine + Nova agent
docker compose -f infra/docker-compose.yml up -d dynamodb-local && pytest -q    # + the DynamoDB concurrency gate
```

## Results

Verified on this build:

- **Policy engine:** `ruff` clean; the suite covers the structuring sequence end-to-end, a 20-call clean-pass with **0 false blocks**, audit-chain tamper detection, policy hot-reload, and the full HITL round-trip.
- **The structuring catch survives concurrency** (the load-bearing test): 5 × ₹40,000 refunds fired **concurrently** against real DynamoDB Local land at exactly {40k, 80k, 120k, 160k, 200k} paise with **zero lost updates**, cross the ₹1,50,000 window threshold, freeze the tool, and auto-deny the next call. Atomic `UpdateItem ADD` holds under parallel invocations — so the demo works when a reviewer clicks fast.
- **Deployed live MCP:** the proxy Lambda spawns the bundled `razorpay-mcp-server` binary and mints a **real test-mode Razorpay payment link** on the live URL — verified through both the proxy directly and end-to-end via the Nova agent.
- **Honest hot-path metering:** the `p95_overhead_ms` counter measures **policy-evaluation time only** — never upstream/network/cold-start — and is shown live on the dashboard.

## Deploy (AWS Amplify Gen 2)

`amplify.yml` runs `ampx pipeline-deploy` (the proxy + Nova-agent container Lambdas, each fronted by an API Gateway v2 **HttpApi** — `execute-api` resolves on every network, unlike a raw Lambda Function URL — plus 3 on-demand DynamoDB tables, near-zero cost at idle) and builds the Vite dashboard. Amplify's build container has no Docker daemon, so the two images are **pre-built and pushed to ECR**, referenced by `AMC_ECR_IMAGE` / `AMC_AGENT_ECR_IMAGE`. The dashboard reads `custom.proxyUrl` / `custom.agentUrl` from `amplify_outputs.json`, flips its badge to **LIVE**, and falls back to a self-driving simulated mode if unreachable. `rzp_test_` keys ride in as build-env vars — test-mode only, no real money can move.

## What I deliberately cut

- **The guardrail as its own MCP server.** The next step is exposing *this* as an MCP server, so any upstream agent — a voice agent, say — plugs into it as a drop-in downstream that transparently enforces and chains to Razorpay's MCP: safe, fully-autonomous agent payments with no human in the loop and no way to go rogue. That's MCP-protocol plumbing I didn't land in time — today it's an HTTP proxy, same engine, one integration short.
- **A live "create payout" path.** Razorpay's open-source MCP exposes payouts as fetch-only (no create tool as of v1.2.1), so `pay_vendor` can't move money live — cached mode covers it and the gap is documented rather than faked.
- **Auth / multi-tenancy.** The endpoints are public (`authType: NONE`) on purpose — test-mode keys, no real money — so a reviewer clicks with zero setup. Not a pattern for a live-money deployment.
- **Surface polish.** Functional tables and counters over design hours — core logic over surface, deliberately.

---

Reserve Pay decides what an agent may *spend*. This decides what an agent may *do*.
