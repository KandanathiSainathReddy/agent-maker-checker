"""Coverage for the smaller HTTP endpoints not already exercised by the
clean-pass / HITL / structuring tests: healthz, the decisions feed, per-policy
trip counts in metrics, and the manual admin unfreeze escape hatch.
"""

from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.audit import JsonlAuditLog
from proxy.engine import PolicyEngine
from proxy.state import InMemoryStateStore
from proxy.upstream.fake import FakeUpstreamExecutor


def _client(tmp_path, policy_dir) -> TestClient:
    # Explicit fake upstream: these Phase 1 tests must stay hermetic and not
    # depend on Agent B's cached-response fixtures being complete.
    app = create_app(
        state_store=InMemoryStateStore(),
        audit_log=JsonlAuditLog(tmp_path / "audit.jsonl"),
        engine=PolicyEngine(policy_dir),
        upstream=FakeUpstreamExecutor(),
    )
    return TestClient(app)


def test_healthz(tmp_path, policy_dir):
    client = _client(tmp_path, policy_dir)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_decisions_feed_is_newest_first_and_respects_limit(tmp_path, policy_dir):
    client = _client(tmp_path, policy_dir)
    for _i in range(3):
        client.post(
            "/tool-call",
            json={
                "agent_id": "agent-feed",
                "tool": "list_orders",
                "arguments": {},
                "context": {},
                "meta": {},
            },
        )
    feed = client.get("/decisions", params={"limit": 2}).json()
    assert len(feed) == 2
    all_feed = client.get("/decisions").json()
    assert len(all_feed) == 3
    # Newest first: ts should be non-increasing.
    assert all_feed[0]["ts"] >= all_feed[1]["ts"] >= all_feed[2]["ts"]


def test_per_policy_trip_counts_increment_on_deny(tmp_path, policy_dir):
    client = _client(tmp_path, policy_dir)
    resp = client.post(
        "/tool-call",
        json={
            "agent_id": "agent-cap",
            "tool": "pay_vendor",
            "arguments": {"amount": 99_000_000, "payee": "vendor_acme@hdfcbank"},
            "context": {"payee": "vendor_acme@hdfcbank", "provenance": []},
            "meta": {},
        },
    )
    assert resp.json()["decision"] == "deny"
    metrics = client.get("/metrics").json()
    assert metrics["per_policy_trip_counts"]["per_call_amount_cap"] == 1
    assert metrics["calls_denied"] == 1


def test_admin_unfreeze_clears_freeze_and_stays_audit_verifiable(tmp_path, policy_dir):
    state = InMemoryStateStore()
    state.freeze("agent-frozen", "pay_vendor", reason="test setup")
    app = create_app(
        state_store=state,
        audit_log=JsonlAuditLog(tmp_path / "audit.jsonl"),
        engine=PolicyEngine(policy_dir),
    )
    client = TestClient(app)

    resp = client.post("/admin/unfreeze/agent-frozen/pay_vendor")
    assert resp.status_code == 200
    assert resp.json()["frozen"] is False
    assert not state.is_frozen("agent-frozen", "pay_vendor")

    verify = client.get("/audit/verify").json()
    assert verify["ok"] is True
    assert verify["entries_checked"] == 1
