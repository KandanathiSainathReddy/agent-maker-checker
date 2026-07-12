# DECISIONS.md

Running log of engineering decisions, trade-offs, and actual time spent per phase.

## Decisions

- **2026-07-12 — Nova Pro for the demo agent, not a frontier model.** The agent is a support/ops worker doing tool calls, not open-ended reasoning. Nova Pro is sufficient and cheap; Nova Lite for any classification step. Model right-sizing is a deliberate, documented choice.
- **2026-07-12 — `DEMO_MODE=live|cached` from day one.** The demo must survive a Razorpay sandbox outage. Cached mode replays recorded responses through the same code path so the policy engine behaves identically.
- **2026-07-12 — Audit log is a sha256 hash chain in JSONL.** Tamper-evidence without infrastructure. Each entry embeds the previous entry's hash; a verify endpoint walks the chain.
- **2026-07-12 — Upstream is behind a `UpstreamExecutor` protocol.** The policy engine never knows whether it fronts the live MCP server or the cached replay — keeps Phase 1 and Phase 2 independent and testable.
- **2026-07-12 — (Phase 1, Agent A) Added `StateStore.add_refund`, additive to infra/CONTRACTS.md §3.** The frozen protocol lists `add_capture` but not a matching `add_refund`, yet the §4 `amc-state` "cap#{agent}" item stores both `captured_paise` *and* `refunded_paise` on one item — there is no way to populate `refunded_paise` without a write method for it. Added `add_refund(agent_id, amount_paise, now, window_s)` with the identical shape/atomicity guarantees as `add_capture`, implemented in both `InMemoryStateStore` and `DynamoStateStore`. Non-breaking (pure addition, no existing signature changed).
- **2026-07-12 — (Phase 1, Agent A) `velocity_aggregation`'s threshold-cross deny carries `escalate_unfreeze=True` on the `PolicyEvaluation` rather than the policy module touching the approvals queue directly.** Policy modules are meant to be pure functions over `(request, state, config)` with side effects only through the injected `StateStore` — they have no handle on the HITL queue. `proxy/app.py`'s `/tool-call` handler reads that flag and opens the "unfreeze" approval itself. Keeps `proxy/policies_impl/*` free of any dependency on the approvals/app layer.
- **2026-07-12 — (Phase 1, Agent A) The HITL approvals queue (`proxy/approvals.py`) is in-memory only, not part of any frozen protocol.** infra/CONTRACTS.md §3 only freezes `StateStore` and `AuditLog` as multi-backend protocols; there is no `ApprovalStore` contract. The `amc-approvals` DynamoDB table (§4) exists for the eventual Lambda deployment, where approvals must survive across ephemeral invocations — wiring a Dynamo-backed queue behind the same shape is real future work for whoever owns the Lambda deploy (Agent C), not built speculatively here since it wasn't asked for and local docker-compose is single-process anyway.
- **2026-07-12 — (Phase 1, Agent A) Added root `pytest.ini` (`pythonpath = .`) alongside the requested root `ruff.toml`.** Needed so `proxy.*`-style absolute imports (used throughout `proxy/`, matching the `from proxy.upstream.factory import get_upstream` pattern in infra/CONTRACTS.md §5) resolve identically for `pytest -q` run from the repo root. Not itself a contract change, just build glue required to hit "pytest -q green from repo root."
- **2026-07-12 — Flag for whoever owns `proxy/Dockerfile`/deploy (orchestrator/Agent C): the image's flat layout may not match `proxy.*`-prefixed imports.** `docker-compose.yml` builds with context `./proxy` and the Dockerfile does `COPY . .` into `/app`, then runs `uvicorn app:app` — inside the container there is no `proxy` parent package, so any `from proxy.x import y` (used throughout this codebase, including the upstream try/except itself) would raise `ModuleNotFoundError` at import time, not just for the intentionally-caught upstream case. This was out of scope to fix here (Dockerfile is orchestrator-owned and Phase 2 wiring hasn't landed), but the image will need either a restructured build context (keep the `proxy/` package name inside the image, e.g. `WORKDIR /app` + `COPY . ./proxy` + `CMD ["uvicorn", "proxy.app:app", ...]`) or an equivalent fix before `docker compose up` will actually serve traffic.
- **2026-07-12 — (Phase 1, Agent A) Chosen velocity/cap numbers.** `per_call_amount_cap` default ₹1,50,000 (₹2,00,000 for `create_payment_link`); `velocity_aggregation` threshold ₹1,50,000 over an 86,400s (24h) tumbling window; `refund_to_capture_ratio` cap 50% over the same 24h window. These match infra/CONTRACTS.md §3's own worked example (₹40,000 × 5 → ₹2,00,000, ₹1,50,000 threshold) exactly, so the shipped policies and the blocking-gate concurrency test agree on the same numbers.

## Time log

| Phase | Scope | Budget | Actual |
|---|---|---|---|
| 0 | repo init, layout, README stub, compose skeleton, CI | 1h | in progress |
| 1 | policy engine + five policies + audit chain + tests | 2.5h | ~2.5h (DynamoDB concurrency test skips locally — no Docker in this environment; passes structurally, unverified against real DynamoDB Local here) |
| 2 | Razorpay MCP self-host + wiring + cached fallback | 2h | — |
| 3 | Nova agent + tools + poisoned-ticket fixture | 1.5h | — |
| 4 | attack pack + one-click runner | 2h | — |
| 5 | dashboard + Amplify deploy | 2h | — |
| 6 | README final, demo script, integration, video | 1h | — |
