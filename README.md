# agent-maker-checker

**Maker-checker for AI agents.** You don't let an employee move ₹2 lakh without a second pair of eyes — an AI agent shouldn't get more trust than an employee.

A merchant-side spend-control plane for AI agents that act on payments. An enforcement proxy sits between any AI agent and Razorpay's APIs (via Razorpay's open-source MCP server, test mode). Every tool call the agent attempts is intercepted and evaluated against declarative, payments-semantic policies **before** execution → `allow` / `deny` / `escalate-to-human`, with a hash-chained audit log, a human approval queue, and live counters.

This is not a detection model and not a chatbot. The generic allow/block plumbing is table stakes — the product is the payments-semantic policies: rupee-denominated reasoning, cross-call velocity, refund-to-capture ratios, payee history.

## Architecture

```
Nova agent (Bedrock)  →  enforcement proxy (FastAPI)  →  Razorpay MCP server (Docker, rzp_test_ keys)  →  Razorpay test-mode APIs
                                   │
                          YAML policy engine
                          hash-chained audit log
                          decision feed + HITL queue  →  React dashboard (Amplify)
```

Why Nova: the demo agent is a support/ops worker, not a reasoning benchmark — Nova Pro handles tool use at a fraction of frontier-model cost, with Nova Lite for cheap classification steps. Model right-sizing is an engineering decision, same as instance right-sizing.

## Repo layout

```
proxy/          # THE PRODUCT: FastAPI enforcement proxy + policy engine
policies/       # declarative YAML policies (versioned, hot-reloadable)
agent/          # Nova-powered demo agent with payment tools
attacks/        # executable red-team scenarios + one-click runner
dashboard/      # React: decision feed, HITL queue, counters
infra/          # docker-compose (local), Amplify/deploy notes, Razorpay MCP setup
tests/          # unit tests — policy engine coverage is the priority
```

## Policies (v1)

| policy | what it enforces |
|---|---|
| `per_call_amount_cap` | per-tool rupee ceiling per call |
| `velocity_aggregation` | rolling-window cross-call sum per (agent, tool, payee-context) — catches structuring |
| `payee_allowlist` | known payees pass; unknown/new payee → escalate |
| `refund_to_capture_ratio` | refunds exceeding X% of captured volume in window → escalate |
| `provenance_check` | payment instructions originating from untrusted data (tickets, emails) → deny |

Every decision returns `{decision, policy_id, reason, evaluated_in_ms}` and appends to a sha256 hash-chained audit log.

## Run it

```bash
cp .env.example .env   # add your rzp_test_ keys, or leave DEMO_MODE=cached
docker compose up
```

`DEMO_MODE=live` fronts the real Razorpay MCP server with test-mode keys — allowed actions materialize in the Razorpay test dashboard. `DEMO_MODE=cached` replays recorded responses so the full demo runs with no keys and no network.

<!-- TODO(Phase 6): live Amplify URL, measured counterfactual counter, cut log, demo script -->

## Results

<!-- TODO(Phase 6): real measured numbers, e.g. "₹X attempted, ₹0 moved, N escalations, 0 false blocks, XXms p95 overhead" -->

## What I deliberately did not build

<!-- TODO(Phase 6): cut log — prioritization is part of the submission -->

---

Reserve Pay decides what an agent may *spend*. This decides what an agent may *do*.

I've spent the last year building runtime guardrails for agent tool calls in cloud operations; payments is where this problem becomes real at national scale, and Razorpay is the only company in India sitting on that fault line.
