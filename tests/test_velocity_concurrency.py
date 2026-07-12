"""THE BLOCKING GATE (infra/CONTRACTS.md S3): concurrent record_and_sum must
never lose an update, or structuring slips straight through the flagship
policy under exactly the conditions (parallel invocations) it exists to
survive.

Two variants of the same scenario:

- ``test_inmemory_*`` — against InMemoryStateStore, always runs.
- ``test_dynamo_*`` — against DynamoStateStore + real DynamoDB Local, the
  actual gate. Skips loudly (not silently) if nobody has started
  DynamoDB Local; the orchestrator brings it up with
  ``docker compose up -d dynamodb-local`` to make this variant run for real.

Both fire 5 x Rs 40,000 record_and_sum calls concurrently for one key and
assert: the five returned running sums are exactly {40k, 80k, 120k, 160k,
200k} paise with no duplicates and no gaps (proof nothing was lost to a
race), the final window sum is exactly Rs 2,00,000, the Rs 1,50,000
threshold crossing is detected, freezing the pair sticks, and a subsequent
call through the real velocity_aggregation policy auto-denies. Looped 20x
with a fresh key per iteration for determinism.
"""

from __future__ import annotations

import concurrent.futures
import uuid
from collections.abc import Iterator

import pytest

from proxy.ddb_bootstrap import create_tables, delete_tables
from proxy.models import ToolCallRequest
from proxy.policies_impl import velocity_aggregation
from proxy.policy_types import PolicyContext
from proxy.state import DynamoStateStore, InMemoryStateStore, StateStore
from tests.conftest import DDB_LOCAL_ENDPOINT, ddb_local_available

AMOUNT_PAISE = 4_000_000  # Rs 40,000
N_CALLS = 5
THRESHOLD_PAISE = 15_000_000  # Rs 1,50,000
WINDOW_S = 86400
NOW = 1_700_000_000.0
ITERATIONS = 20


def _fire_concurrently(state: StateStore, key: str) -> list[int]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=N_CALLS) as pool:
        futures = [
            pool.submit(state.record_and_sum, key, AMOUNT_PAISE, NOW, WINDOW_S)
            for _ in range(N_CALLS)
        ]
        return [f.result() for f in futures]


def _run_scenario(state: StateStore, iteration: int) -> None:
    agent_id = f"concurrency-agent-{iteration}"
    tool = "pay_vendor"
    payee = "vendor_acme@hdfcbank"
    key = f"{agent_id}#{tool}#{payee}"

    sums = _fire_concurrently(state, key)

    expected = {AMOUNT_PAISE * i for i in range(1, N_CALLS + 1)}
    assert set(sums) == expected, (
        f"iteration {iteration}: lost updates — got {sorted(sums)}, expected {sorted(expected)}"
    )
    assert state.window_sum(key, NOW, WINDOW_S) == AMOUNT_PAISE * N_CALLS == 20_000_000
    assert any(s > THRESHOLD_PAISE for s in sums), (
        f"iteration {iteration}: Rs 1,50,000 threshold crossing was not detected"
    )
    assert not state.is_frozen(agent_id, tool)

    state.freeze(agent_id, tool, reason="velocity threshold crossed (test)")
    assert state.is_frozen(agent_id, tool), f"iteration {iteration}: freeze did not stick"

    # A subsequent call, run through the real policy (not just a state flag
    # check), must auto-deny.
    req = ToolCallRequest(agent_id=agent_id, tool=tool, arguments={"amount": 1, "payee": payee})
    ctx = PolicyContext(
        request=req, state=state,
        params={"window_s": WINDOW_S, "threshold_paise": THRESHOLD_PAISE}, now=NOW,
    )
    result = velocity_aggregation.evaluate(ctx)
    assert result.decision == "deny", f"iteration {iteration}: frozen pair did not auto-deny"
    assert "frozen" in result.reason.lower()


def test_inmemory_concurrent_record_and_sum_never_loses_updates():
    for i in range(ITERATIONS):
        _run_scenario(InMemoryStateStore(), i)


@pytest.fixture(scope="module")
def ddb_state_store() -> Iterator[DynamoStateStore]:
    if not ddb_local_available():
        pytest.skip(
            "\n"
            "==================================================================\n"
            " SKIPPED: DynamoDB Local is not reachable at localhost:8001.\n"
            " This is tests/test_velocity_concurrency.py - the Phase 1 BLOCKING\n"
            " GATE per infra/CONTRACTS.md S3. It only runs the real DynamoDB\n"
            " atomicity check when DynamoDB Local is up. Start it with:\n"
            "     docker compose up -d dynamodb-local\n"
            " then re-run:\n"
            "     pytest -q tests/test_velocity_concurrency.py\n"
            "==================================================================\n"
        )

    suffix = uuid.uuid4().hex[:8]
    state_table = f"amc-state-concurrency-test-{suffix}"
    audit_table = f"amc-audit-concurrency-test-{suffix}"
    approvals_table = f"amc-approvals-concurrency-test-{suffix}"
    create_tables(
        endpoint_url=DDB_LOCAL_ENDPOINT, state_table=state_table,
        audit_table=audit_table, approvals_table=approvals_table,
    )
    try:
        yield DynamoStateStore(state_table, endpoint_url=DDB_LOCAL_ENDPOINT)
    finally:
        delete_tables(
            endpoint_url=DDB_LOCAL_ENDPOINT, state_table=state_table,
            audit_table=audit_table, approvals_table=approvals_table,
        )


def test_dynamo_concurrent_record_and_sum_never_loses_updates(
    ddb_state_store: DynamoStateStore,
) -> None:
    for i in range(ITERATIONS):
        _run_scenario(ddb_state_store, i)
