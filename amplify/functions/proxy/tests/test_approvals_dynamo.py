"""DynamoApprovalQueue: the HITL approvals queue must survive serverless.

``InMemoryApprovalQueue`` is a single process's dict — it cannot be the
default once the proxy runs on Lambda (``APPROVALS_BACKEND=dynamodb``,
infra/CONTRACTS.md §2), because concurrent/ephemeral invocations don't share
process memory. This file covers two things:

- ``test_get_approval_queue_selects_backend_from_env`` — always runs, no
  DynamoDB Local required. Constructing ``DynamoApprovalQueue`` only builds
  a local boto3 resource/table handle; it makes no network call, so the
  factory-selection logic in ``proxy.config`` can be exercised without a
  live table.
- ``test_dynamo_approval_queue_lifecycle`` — against ``DynamoApprovalQueue``
  + real DynamoDB Local, covering create -> get -> list(pending) ->
  resolve(approved) -> counts, atomicity of double-resolve, and the
  arguments JSON round-trip, all against one table (module-scoped fixture,
  matching tests/test_velocity_concurrency.py's pattern) to keep table
  churn on the shared DynamoDB Local container low. Skips loudly (not
  silently) if nobody has started DynamoDB Local; the orchestrator brings
  it up with ``docker compose up -d dynamodb-local`` to make this run for
  real.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from conftest import DDB_LOCAL_ENDPOINT, ddb_local_available

from proxy import config
from proxy.approvals import DynamoApprovalQueue, InMemoryApprovalQueue
from proxy.ddb_bootstrap import create_tables, delete_tables

NOW = 1_700_000_000.0
AMOUNT_PAISE = 16_000_000  # Rs 1,60,000 -- the escalated unfreeze from the class docstring


def test_get_approval_queue_selects_backend_from_env(monkeypatch):
    """Always-run: proxy.config.get_approval_queue() picks a backend purely
    from APPROVALS_BACKEND, mirroring get_state_store()/get_audit_log(). No
    DynamoDB Local needed -- DynamoApprovalQueue.__init__ only builds a
    local boto3 handle, it does not touch the network.
    """
    config.get_approval_queue.cache_clear()
    try:
        monkeypatch.delenv("APPROVALS_BACKEND", raising=False)
        assert isinstance(config.get_approval_queue(), InMemoryApprovalQueue)
        config.get_approval_queue.cache_clear()

        monkeypatch.setenv("APPROVALS_BACKEND", "dynamodb")
        assert isinstance(config.get_approval_queue(), DynamoApprovalQueue)
    finally:
        # Leave no trace: later tests/files that call config.get_approval_queue()
        # without overriding it must see the restored default env re-evaluated,
        # not a cached dynamodb-backend instance from this test.
        config.get_approval_queue.cache_clear()


@pytest.fixture(scope="module")
def ddb_approval_queue() -> Iterator[DynamoApprovalQueue]:
    if not ddb_local_available():
        pytest.skip(
            "\n"
            "==================================================================\n"
            " SKIPPED: DynamoDB Local is not reachable at localhost:8001.\n"
            " This is tests/test_approvals_dynamo.py - the Dynamo-backed HITL\n"
            " approvals queue (Agent G). It only runs the real DynamoDB\n"
            " atomicity/round-trip checks when DynamoDB Local is up. Start it\n"
            " with:\n"
            "     docker compose up -d dynamodb-local\n"
            " then re-run:\n"
            "     pytest -q tests/test_approvals_dynamo.py\n"
            "==================================================================\n"
        )

    # Unique table names per run: ddb_bootstrap.create_tables/delete_tables
    # always provisions the state+audit+approvals trio together (matching
    # tests/test_velocity_concurrency.py's pattern) even though only the
    # approvals table is exercised here. Module-scoped (one create/delete
    # cycle for the whole file, not per test) to keep churn on the shared
    # DynamoDB Local container low.
    suffix = uuid.uuid4().hex[:8]
    state_table = f"amc-state-approvals-test-{suffix}"
    audit_table = f"amc-audit-approvals-test-{suffix}"
    approvals_table = f"amc-approvals-approvals-test-{suffix}"
    create_tables(
        endpoint_url=DDB_LOCAL_ENDPOINT,
        state_table=state_table,
        audit_table=audit_table,
        approvals_table=approvals_table,
    )
    try:
        yield DynamoApprovalQueue(approvals_table, endpoint_url=DDB_LOCAL_ENDPOINT)
    finally:
        delete_tables(
            endpoint_url=DDB_LOCAL_ENDPOINT,
            state_table=state_table,
            audit_table=audit_table,
            approvals_table=approvals_table,
        )


def test_dynamo_approval_queue_lifecycle(ddb_approval_queue: DynamoApprovalQueue) -> None:
    queue = ddb_approval_queue

    # -- create -> get -> list(pending) -> resolve(approved) -> counts -----
    record = queue.create(
        kind="unfreeze",
        agent_id="support-agent-1",
        tool="pay_vendor",
        arguments={"amount": AMOUNT_PAISE, "payee": "vendor_acme@hdfcbank"},
        amount_paise=AMOUNT_PAISE,
        reason="velocity threshold crossed",
        request_id="req_test123",
        now=NOW,
    )
    assert record.approval_id.startswith("appr_")
    assert record.status == "pending"
    assert record.resolved_at is None

    fetched = queue.get(record.approval_id)
    assert fetched == record
    assert queue.get("appr_does_not_exist") is None

    pending = queue.list(status="pending")
    assert [r.approval_id for r in pending] == [record.approval_id]
    assert queue.list(status="approved") == []
    assert [r.approval_id for r in queue.list()] == [record.approval_id]

    resolved = queue.resolve(record.approval_id, "approved", now=NOW + 60)
    assert resolved is not None
    assert resolved.status == "approved"
    assert resolved.resolved_at == NOW + 60

    after = queue.get(record.approval_id)
    assert after is not None
    assert after.status == "approved"
    assert after.resolved_at == NOW + 60

    assert queue.list(status="pending") == []
    assert [r.approval_id for r in queue.list(status="approved")] == [record.approval_id]
    assert queue.counts() == (0, 1)

    # -- double-resolve rejected (atomicity) --------------------------------
    # Same conflict signal InMemoryApprovalQueue.resolve() gives on a
    # not-pending record: None, not an exception. This is what lets
    # app.py's 409-on-double-resolve path (guarded by a get() status check
    # before calling resolve()) behave identically on both backends.
    second = queue.resolve(record.approval_id, "denied", now=NOW + 120)
    assert second is None

    # The first resolution wins; the second call did not clobber status.
    unchanged = queue.get(record.approval_id)
    assert unchanged is not None
    assert unchanged.status == "approved"
    assert unchanged.resolved_at == NOW + 60

    # Resolving something that never existed is the same "nothing to do" signal.
    assert queue.resolve("appr_never_existed", "approved", now=NOW) is None

    # -- arguments JSON round-trip -------------------------------------------
    nested_arguments = {
        "amount": AMOUNT_PAISE,
        "payee": "vendor_acme@hdfcbank",
        "payment_id": "pay_x",
        "metadata": {"ticket": "4471", "tags": ["urgent", "escalated"]},
        "retries": 0,
        "auto_approved": False,
    }
    record2 = queue.create(
        kind="tool_call",
        agent_id="support-agent-3",
        tool="pay_vendor",
        arguments=nested_arguments,
        amount_paise=AMOUNT_PAISE,
        reason="escalated: unusual payee",
        now=NOW + 200,
    )

    fetched2 = queue.get(record2.approval_id)
    assert fetched2 is not None
    assert fetched2.arguments == nested_arguments
    assert isinstance(fetched2.arguments["metadata"], dict)
    assert fetched2.arguments["metadata"]["tags"] == ["urgent", "escalated"]
    assert fetched2.arguments["auto_approved"] is False

    # Final tally: record (approved) + record2 (still pending).
    assert queue.counts() == (1, 1)
