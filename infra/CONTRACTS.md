# CONTRACTS.md — frozen interfaces between workstreams

These shapes are **frozen**. Multiple agents build against them in parallel; do not
change a name or field without updating this file first. Money is always **integer
paise** across every boundary (Razorpay's native unit). INR appears only in YAML
policy files and in the dashboard's display layer.

---

## 1. Tool-call API (proxy public surface)

`POST /tool-call`
```json
{
  "agent_id": "support-agent-1",
  "tool": "issue_refund",
  "arguments": {"payment_id": "pay_x", "amount": 4000000, "payee": "cust_ravi@oksbi"},
  "context": {
    "payee": "cust_ravi@oksbi",
    "provenance": [{"source": "ticket:4471", "trusted": false, "tainted_fields": ["arguments.payee"]}]
  },
  "meta": {"labeled_legit": true}
}
```
Response:
```json
{
  "request_id": "req_...",
  "decision": "allow|deny|escalate",
  "policy_id": "velocity_aggregation",
  "reason": "human-readable, specific",
  "evaluated_in_ms": 0.42,
  "status": "executed|blocked|pending_approval",
  "upstream_result": {}
}
```
Other routes: `GET /decisions?limit=N`, `GET /approvals`, `POST /approvals/{id}/approve`,
`POST /approvals/{id}/deny`, `GET /metrics`, `GET /audit/verify`,
`POST /admin/unfreeze/{agent_id}/{tool}`, `GET /healthz`.

Tool vocabulary: `issue_refund`, `create_payment_link`, `pay_vendor`, `get_ticket`, `list_orders`.

---

## 2. Environment variables (proxy reads; Amplify sets on the Lambda)

| var | values / default | who reads |
|---|---|---|
| `DEMO_MODE` | `cached` (default) \| `live` | upstream factory |
| `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET` | `rzp_test_...` | MCP client (live) |
| `POLICY_DIR` | `./policies` local, `/app/policies` image | engine |
| `STATE_BACKEND` | `memory` (default) \| `dynamodb` | state factory |
| `AUDIT_BACKEND` | `jsonl` (default) \| `dynamodb` | audit factory |
| `AUDIT_LOG_PATH` | `./data/audit.jsonl` | jsonl audit |
| `DDB_ENDPOINT_URL` | empty = real AWS; `http://localhost:8001` = DynamoDB Local | dynamo stores |
| `DDB_STATE_TABLE` | `amc-state` | dynamo state |
| `DDB_AUDIT_TABLE` | `amc-audit` | dynamo audit |
| `DDB_APPROVALS_TABLE` | `amc-approvals` | dynamo approvals |
| `AWS_REGION` | `us-east-1` | boto3 |
| `PROXY_PORT` | `8000` | uvicorn / Dockerfile |

Local `docker compose` runs `STATE_BACKEND=memory` + `AUDIT_BACKEND=jsonl` (single
process → bulletproof, no AWS). Cloud Lambda runs `dynamodb` for both (ephemeral,
concurrent → must be shared + atomic). The DynamoDB path is validated by the
concurrency test, below.

---

## 3. StateStore interface (proxy/state.py) — Agent A owns

Cross-call state (velocity windows, freeze registry, capture volume) sits behind ONE
interface with two implementations: `InMemoryStateStore` and `DynamoStateStore`.
**Every mutation must be atomic under concurrent callers** — this is what lets the
structuring catch survive concurrent Lambda invocations.

```python
class StateStore(Protocol):
    # Atomically add amount to the (key) rolling window and RETURN the new window sum.
    # A non-atomic read-modify-write here would lose updates under concurrency and let
    # structuring slip — DynamoStateStore MUST use an atomic counter (UpdateItem ADD).
    def record_and_sum(self, key: str, amount_paise: int, now: float, window_s: int) -> int: ...
    def window_sum(self, key: str, now: float, window_s: int) -> int: ...

    def freeze(self, agent_id: str, tool: str, reason: str) -> None: ...     # idempotent
    def is_frozen(self, agent_id: str, tool: str) -> bool: ...               # strongly consistent read
    def unfreeze(self, agent_id: str, tool: str) -> None: ...

    def add_capture(self, agent_id: str, amount_paise: int, now: float, window_s: int) -> None: ...
    def capture_and_refund_totals(self, agent_id: str, now: float, window_s: int) -> tuple[int, int]: ...
```
- `key` for velocity is `f"{agent_id}#{tool}#{payee_context}"`.
- Inject a `now: float` clock everywhere so tests never sleep.
- `DynamoStateStore` uses `UpdateItem` with `ADD` and `ReturnValues=UPDATED_NEW` for
  atomic running sums; `freeze` is an idempotent conditional put; `is_frozen` is a
  strongly-consistent `GetItem`.

### The headline test (Agent A, blocking gate)
`tests/test_velocity_concurrency.py` — run against **DynamoDB Local**, not memory:
fire 5 × ₹40,000 `record_and_sum` calls **concurrently** (threads / asyncio.gather)
for the same key. Assert: final window sum == ₹2,00,000 exactly (zero lost updates),
the ₹1,50,000 threshold crossing is detected, the (agent,tool) pair ends **frozen**,
and a subsequent call is auto-denied. Repeat N times → deterministic. This proves the
demo survives a reviewer clicking fast / parallel invocations.

---

## 4. DynamoDB tables — Agent C creates (amplify/backend/data.ts), Agent A reads

All `PAY_PER_REQUEST` (₹0 idle), `removalPolicy: DESTROY` (demo teardown), TTL attr
`ttl`, region from env. Names must match §2 exactly.

**`amc-state`** — velocity + freeze + capture, one table, discriminated by `pk`:
- PK `pk` (S). Velocity item `pk="vel#{agent}#{tool}#{payee}"` → `sum_paise`(N),
  `count`(N), `window_start`(N), `ttl`(N). Freeze item `pk="freeze#{agent}#{tool}"`
  → `frozen`(BOOL), `reason`(S), `frozen_at`(N). Capture item `pk="cap#{agent}"`
  → `captured_paise`(N), `refunded_paise`(N), `window_start`(N), `ttl`(N).

**`amc-audit`** — hash chain. PK `chain`(S, constant `"main"`), SK `seq`(N). Item:
`seq, ts, request_id, agent_id, tool, arguments_hash, decision, policy_id, reason,
prev_hash, hash`. Append = conditional put on `attribute_not_exists(seq)`, retry on
clash (chain is intentionally serialized — tamper-evidence needs ordering).

**`amc-approvals`** — HITL queue. PK `approval_id`(S). Item: `status`
(`pending|approved|denied`), `agent_id, tool, arguments`(JSON string), `amount_paise`,
`reason, created_at, resolved_at`.

DynamoDB Local (compose service `dynamodb-local`, host port **8001**) is created/torn
down by the test + a helper `proxy/upstream/../ddb_bootstrap.py` (Agent A) using these
exact schemas, so tests need no AWS.

---

## 5. Upstream executor (proxy/upstream/) — Agent B owns impls, Agent A owns base

`proxy/upstream/base.py` (identical content if either agent creates it first):
```python
from dataclasses import dataclass
from typing import Any, Protocol

@dataclass
class UpstreamResult:
    ok: bool
    tool: str
    data: dict[str, Any]
    error: str | None = None
    mode: str = "cached"   # "live" | "cached" | "fake"

class UpstreamExecutor(Protocol):
    async def execute(self, tool: str, arguments: dict[str, Any]) -> UpstreamResult: ...
```
Factory `proxy/upstream/factory.py::get_upstream() -> UpstreamExecutor` selects by
`DEMO_MODE`. App imports it under `try/except ImportError` and falls back to the fake.

---

## 6. Frontend ← backend wiring — Agent C emits, Agent D reads

Amplify writes the proxy Function URL into generated `amplify_outputs.json` via
`backend.addOutput({ custom: { proxyUrl: <FunctionURL> } })`. The dashboard reads
`amplify_outputs.json → custom.proxyUrl`, calls `GET {proxyUrl}/metrics` to flip the
badge to **LIVE**, and falls back to the built-in simulated mode if unreachable.

---

## 7. Proxy image contract (proxy/Dockerfile) — orchestrator owns

FastAPI app is `app:app`, uvicorn on `:8000`, AWS Lambda Web Adapter as a `/opt/extensions`
extension so the **same image** runs under `docker compose` (plain HTTP) and as the
container Lambda (`DockerImageCode.fromImageAsset('../proxy')`). Policies are baked into
the image at `/app/policies` and bind-mounted for hot-reload locally.
