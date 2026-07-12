"""HITL round-trip via the real FastAPI app (TestClient): a call escalates,
lands in the approvals queue, gets approved, executes via the fake upstream,
and the audit chain records the full attempted(escalated) -> approved ->
executed lifecycle, all still hash-chain-verifiable.
"""

from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.audit import JsonlAuditLog
from proxy.engine import PolicyEngine
from proxy.state import InMemoryStateStore
from proxy.upstream.fake import FakeUpstreamExecutor


def _make_app(tmp_path, policy_dir):
    # Explicit fake upstream so "executes via fake upstream" is deterministic
    # and independent of Agent B's DEMO_MODE=cached factory/fixtures.
    return create_app(
        state_store=InMemoryStateStore(),
        audit_log=JsonlAuditLog(tmp_path / "audit.jsonl"),
        engine=PolicyEngine(policy_dir),
        upstream=FakeUpstreamExecutor(),
    )


def test_escalate_then_approve_executes_and_chains_audit(tmp_path, policy_dir):
    client = TestClient(_make_app(tmp_path, policy_dir))

    payload = {
        "agent_id": "support-agent-1",
        "tool": "pay_vendor",
        "arguments": {"amount": 500_000, "payee": "brand_new_vendor@somebank"},
        "context": {"payee": "brand_new_vendor@somebank", "provenance": []},
        "meta": {"labeled_legit": False},
    }
    resp = client.post("/tool-call", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decision"] == "escalate"
    assert body["policy_id"] == "payee_allowlist"
    assert body["status"] == "pending_approval"
    assert body["upstream_result"] is None
    request_id = body["request_id"]

    pending = client.get("/approvals", params={"status": "pending"}).json()
    assert len(pending) == 1
    approval = pending[0]
    assert approval["kind"] == "tool_call"
    assert approval["request_id"] == request_id
    assert approval["status"] == "pending"

    approve_resp = client.post(f"/approvals/{approval['approval_id']}/approve")
    assert approve_resp.status_code == 200, approve_resp.text
    executed = approve_resp.json()
    assert executed["status"] == "executed"
    assert executed["upstream_result"]["ok"] is True
    assert executed["upstream_result"]["mode"] == "fake"

    # No longer pending.
    still_pending = client.get("/approvals", params={"status": "pending"}).json()
    assert still_pending == []
    resolved = client.get("/approvals", params={"status": "approved"}).json()
    assert len(resolved) == 1

    verify = client.get("/audit/verify").json()
    assert verify["ok"] is True

    audit_log = JsonlAuditLog(tmp_path / "audit.jsonl")
    entries = [e for e in audit_log.entries() if e.request_id == request_id]
    decisions_in_order = [e.decision for e in entries]
    assert decisions_in_order == ["escalate", "approved", "executed"]


def test_escalate_then_deny_never_executes(tmp_path, policy_dir):
    client = TestClient(_make_app(tmp_path, policy_dir))

    payload = {
        "agent_id": "support-agent-2",
        "tool": "pay_vendor",
        "arguments": {"amount": 500_000, "payee": "another_new_vendor@somebank"},
        "context": {"payee": "another_new_vendor@somebank", "provenance": []},
        "meta": {"labeled_legit": False},
    }
    body = client.post("/tool-call", json=payload).json()
    assert body["decision"] == "escalate"

    pending = client.get("/approvals", params={"status": "pending"}).json()
    approval_id = pending[0]["approval_id"]

    deny_resp = client.post(f"/approvals/{approval_id}/deny")
    assert deny_resp.status_code == 200
    assert deny_resp.json()["status"] == "denied"

    # Approving (or denying) twice is rejected, not silently accepted.
    second = client.post(f"/approvals/{approval_id}/deny")
    assert second.status_code == 409

    metrics = client.get("/metrics").json()
    assert metrics["rupees_moved"] == 0
