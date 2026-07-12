# DECISIONS.md

Running log of engineering decisions, trade-offs, and actual time spent per phase.

## Decisions

- **2026-07-12 — Nova Pro for the demo agent, not a frontier model.** The agent is a support/ops worker doing tool calls, not open-ended reasoning. Nova Pro is sufficient and cheap; Nova Lite for any classification step. Model right-sizing is a deliberate, documented choice.
- **2026-07-12 — `DEMO_MODE=live|cached` from day one.** The demo must survive a Razorpay sandbox outage. Cached mode replays recorded responses through the same code path so the policy engine behaves identically.
- **2026-07-12 — Audit log is a sha256 hash chain in JSONL.** Tamper-evidence without infrastructure. Each entry embeds the previous entry's hash; a verify endpoint walks the chain.
- **2026-07-12 — Upstream is behind a `UpstreamExecutor` protocol.** The policy engine never knows whether it fronts the live MCP server or the cached replay — keeps Phase 1 and Phase 2 independent and testable.

## Time log

| Phase | Scope | Budget | Actual |
|---|---|---|---|
| 0 | repo init, layout, README stub, compose skeleton, CI | 1h | in progress |
| 1 | policy engine + five policies + audit chain + tests | 2.5h | — |
| 2 | Razorpay MCP self-host + wiring + cached fallback | 2h | — |
| 3 | Nova agent + tools + poisoned-ticket fixture | 1.5h | — |
| 4 | attack pack + one-click runner | 2h | — |
| 5 | dashboard + Amplify deploy | 2h | — |
| 6 | README final, demo script, integration, video | 1h | — |
