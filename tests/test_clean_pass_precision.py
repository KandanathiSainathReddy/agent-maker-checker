"""Clean-pass precision: 20 varied legitimate calls must ALL allow.

This is the false-positive guardrail for the whole engine — mixed tools,
amounts, known payees, and trusted (absent) provenance, spread across
several agents so no single velocity/ratio window is anywhere near its
threshold. If any of these 20 escalate or deny, or if false_blocks != 0,
the policy set is too aggressive for real traffic.
"""

from fastapi.testclient import TestClient

from proxy.app import create_app
from proxy.audit import JsonlAuditLog
from proxy.state import InMemoryStateStore
from proxy.upstream.fake import FakeUpstreamExecutor

# (agent_id, tool, amount_paise, payee_or_None)
CALLS = [
    # Agent A: routine vendor payouts, well under every threshold.
    ("agent-A", "pay_vendor", 1_000_000, "vendor_acme@hdfcbank"),
    ("agent-A", "pay_vendor", 2_000_000, "vendor_acme@hdfcbank"),
    ("agent-A", "pay_vendor", 1_500_000, "vendor_acme@hdfcbank"),
    ("agent-A", "pay_vendor", 500_000, "vendor_acme@hdfcbank"),
    ("agent-A", "pay_vendor", 2_000_000, "vendor_acme@hdfcbank"),
    # Agent B: captures followed by refunds comfortably under the ratio cap.
    ("agent-B", "create_payment_link", 10_000_000, None),
    ("agent-B", "create_payment_link", 4_000_000, None),
    ("agent-B", "issue_refund", 3_000_000, "cust_ravi@oksbi"),
    ("agent-B", "issue_refund", 2_000_000, "cust_ravi@oksbi"),
    ("agent-B", "get_ticket", 0, None),
    # Agent C: reads plus a couple of vendor payouts.
    ("agent-C", "list_orders", 0, None),
    ("agent-C", "pay_vendor", 3_000_000, "vendor_globex@icici"),
    ("agent-C", "pay_vendor", 4_000_000, "vendor_globex@icici"),
    ("agent-C", "get_ticket", 0, None),
    ("agent-C", "list_orders", 0, None),
    # Agent D: one capture, one refund against it, one payout, two reads.
    ("agent-D", "create_payment_link", 8_000_000, None),
    ("agent-D", "issue_refund", 1_000_000, "vendor_acme@hdfcbank"),
    ("agent-D", "pay_vendor", 1_000_000, "vendor_acme@hdfcbank"),
    ("agent-D", "get_ticket", 0, None),
    ("agent-D", "list_orders", 0, None),
]


def test_twenty_clean_calls_all_allow(tmp_path, policy_dir):
    from proxy.engine import PolicyEngine

    # Explicit isolated state/audit/upstream: this test must not share the
    # process-wide config.get_state_store()/get_audit_log() singletons (or
    # write into the repo's real ./data/audit.jsonl) with any other test.
    app = create_app(
        state_store=InMemoryStateStore(),
        audit_log=JsonlAuditLog(tmp_path / "audit.jsonl"),
        engine=PolicyEngine(policy_dir),
        upstream=FakeUpstreamExecutor(),
    )
    client = TestClient(app)

    assert len(CALLS) == 20

    for agent_id, tool, amount, payee in CALLS:
        arguments = {"amount": amount}
        if payee:
            arguments["payee"] = payee
        if tool in ("issue_refund",):
            arguments["payment_id"] = "pay_existing"
        payload = {
            "agent_id": agent_id,
            "tool": tool,
            "arguments": arguments,
            "context": {"payee": payee, "provenance": []},
            "meta": {"labeled_legit": True},
        }
        resp = client.post("/tool-call", json=payload)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["decision"] == "allow", (
            f"{agent_id}/{tool} amount={amount} unexpectedly {body['decision']}: {body['reason']}"
        )
        assert body["status"] == "executed"

    metrics = client.get("/metrics").json()
    assert metrics["calls_allowed"] == 20
    assert metrics["calls_denied"] == 0
    assert metrics["calls_escalated"] == 0
    assert metrics["false_blocks"] == 0
