# agent-maker-checker

**Maker-checker for AI agents.** You don't let an employee move ₹2 lakh without a second pair of eyes — an AI agent shouldn't get more trust than an employee.

A merchant-side spend-control plane for AI agents that act on payments. An enforcement proxy sits between any AI agent and Razorpay's APIs (via Razorpay's open-source MCP server, test mode). Every tool call the agent attempts is intercepted and evaluated against declarative, payments-semantic policies **before** execution → `allow` / `deny` / `escalate-to-human`, with a hash-chained audit log, a human approval queue, and live counters.

This is not a detection model and not a chatbot. The generic allow/block plumbing is table stakes — the product is the payments-semantic policies: rupee-denominated reasoning, cross-call velocity, refund-to-capture ratios, payee history, and argument provenance.

## Architecture

```
Nova agent (Bedrock)  →  enforcement proxy (FastAPI)  →  Razorpay MCP server (stdio, rzp_test_ keys)  →  Razorpay test-mode APIs
                                   │
                          YAML policy engine  ·  sha256 hash-chained audit log
                          decision feed  ·  human approval (HITL) queue
                                   │
                          React dashboard (Amplify Hosting)
```

**One image, two runtimes.** The proxy is a single container (`amplify/functions/proxy`, FastAPI + the AWS Lambda Web Adapter). The exact bytes that pass tests locally under `docker compose` run unchanged as a container Lambda in cloud — no handler rewrite, no forked code path.

**State that survives serverless.** Cross-call state (velocity windows, the freeze registry, capture volume) sits behind one `StateStore` interface: in-memory locally, **DynamoDB (atomic `UpdateItem`) in cloud**. This is what lets the structuring catch fire deterministically even when the proxy is many concurrent Lambda invocations, not one warm process — see the concurrency gate under [Results](#results).

**Why Nova:** the demo agent is a support/ops worker, not a reasoning benchmark — Nova Pro handles tool use at a fraction of frontier-model cost, Nova Lite for cheap classification. Model right-sizing is an engineering decision, same as instance right-sizing.

## Repo layout

```
amplify/                       BACKEND (deploys to AWS via Amplify Gen 2)
  backend.ts                   composition root
  backend/  proxy.ts data.ts lib/    CDK: container Lambda + Function URL + 3 DynamoDB tables
  functions/
    proxy/                     THE PRODUCT — FastAPI enforcement proxy + policy engine
      app.py engine.py state.py audit.py metrics.py approvals.py …
      policies_impl/           one module per policy (payments rationale in each docstring)
      policies/                the 5 YAML policies (versioned; baked into the Lambda image)
      upstream/                Razorpay MCP client · cached replay · fixtures
      tests/                   policy-engine + DynamoDB concurrency tests
      Dockerfile
apps/dashboard/                FRONTEND — Vite; decision feed, HITL queue, live counters (Amplify Hosting)
infra/                         docker-compose.yml · razorpay-mcp.md · CONTRACTS.md · verify_live.py
amplify.yml                    Amplify build spec (backend + frontend)
```

## Policies (v1)

| policy | enforces | default |
|---|---|---|
| `per_call_amount_cap` | per-tool rupee ceiling per call | ₹50,000 refunds ·  higher for payment links |
| `velocity_aggregation` | rolling-window cross-call sum per (agent, tool, payee) — **catches structuring** | ₹1,50,000 / 24h → deny + freeze + escalate |
| `payee_allowlist` | known payees pass; unknown/new payee → **escalate** (not deny) | — |
| `refund_to_capture_ratio` | refunds exceeding X% of captured volume in window → escalate | 50% / 24h |
| `provenance_check` | payment instructions originating from untrusted data (tickets, emails) → **deny** | UPI/"pay-to" patterns from untrusted sources |

Every decision returns `{decision, policy_id, reason, evaluated_in_ms}` and appends to a sha256 hash-chained audit log (each entry embeds the previous hash; `GET /audit/verify` walks the chain).

## Run it locally

```bash
cp .env.example .env                              # leave DEMO_MODE=cached, or add rzp_test_ keys for live
docker compose -f infra/docker-compose.yml up     # proxy :8000, dashboard :3000
```

- `DEMO_MODE=cached` (default) replays recorded Razorpay responses — the full demo runs with no keys and no network.
- `DEMO_MODE=live` fronts the real Razorpay MCP server with test-mode keys; allowed actions materialize in the Razorpay test dashboard (the server is stdio-only — see `infra/razorpay-mcp.md`).

**Tests** (from `amplify/functions/`):

```bash
pip install -r proxy/requirements-dev.txt
pytest -q                                                     # 62 passed, 1 skipped
docker compose -f infra/docker-compose.yml up -d dynamodb-local && pytest -q   # runs the DynamoDB concurrency gate too
```

## Results

Verified on this build:

- **Policy engine:** `ruff` clean, **62 passed / 1 skipped** — includes the structuring sequence end-to-end, a 20-call clean-pass with **0 false blocks**, audit-chain tamper detection, hot-reload, and the full HITL round-trip.
- **The structuring catch survives concurrency** (the load-bearing test): 5 × ₹40,000 refunds fired **concurrently** against real DynamoDB Local land at exactly {40k, 80k, 120k, 160k, 200k} paise with **zero lost updates**, cross the ₹1,50,000 window threshold, freeze the tool, and auto-deny the next call — 20 deterministic iterations. Atomic `UpdateItem ADD` holds under parallel invocations, so the demo works when a reviewer clicks fast.
- **Container parity:** the proxy image builds and serves `/healthz`, `/metrics`, and `/tool-call` (all HTTP 200); a ₹40,000 refund to an unknown payee correctly **escalates** on `payee_allowlist`, evaluated in ~0.13 ms.
- **Honest hot-path metering:** the `p95_overhead_ms` counter measures **policy-evaluation time only** — never upstream/network/cold-start — and is displayed live on the dashboard.

<!-- Live counterfactual counter (₹ attempted vs ₹ moved, escalations, p95) is captured from a scenario run and pinned here; Amplify URL added once the app is connected. -->

## What I deliberately did not build

- **A live "create payout" path.** Razorpay's open-source MCP server exposes payouts as fetch-only (no create tool as of v1.2.1), so `pay_vendor` can't move real money live — cached mode covers it and the gap is documented rather than faked.
- **A DynamoDB-backed approvals queue.** Approvals are in-memory (correct for single-process local); the `amc-approvals` table exists for the hosted path, and wiring the Dynamo store is a deploy-step, not built speculatively.
- **Auth on the demo Function URL.** It's public (`authType: NONE`) on purpose — test-mode keys, no real money — so a reviewer can hit the LIVE badge with zero setup. Not a pattern for a live-money deployment.
- **Dashboard polish.** Functional tables and counters over design hours — core logic over surface, deliberately.

## Deploy (AWS Amplify Gen 2)

`amplify.yml` defines a backend app (`ampx pipeline-deploy` → the proxy container Lambda + 3 on-demand DynamoDB tables, near-zero cost at idle) and a frontend app (`apps/dashboard` on Amplify Hosting). The dashboard reads the proxy's Function URL from `amplify_outputs.json` (`custom.proxyUrl`) and flips its badge to **LIVE**, falling back to a self-driving simulated mode if unreachable. `rzp_test_` keys are injected as Amplify secrets, never committed.

---

Reserve Pay decides what an agent may *spend*. This decides what an agent may *do*.

I've spent the last year building runtime guardrails for agent tool calls in cloud operations; payments is where this problem becomes real at national scale, and Razorpay is the only company in India sitting on that fault line.
