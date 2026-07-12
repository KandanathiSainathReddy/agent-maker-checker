"""Runtime policy-override admin endpoints: the "checker" half of maker-checker.

Covers, against a live FastAPI TestClient:

- ``GET /admin/policy`` lists every loaded policy with its INR-facing
  defaults, raw overrides, and paise-facing effective (merged) params.
- ``POST /admin/policy/{policy_id}`` stores a runtime override and the very
  next ``/tool-call`` respects it (proving the merge is live, not just
  reflected back in the admin response) -- a boundary-exact allow/deny pair
  around the new cap.
- ``POST /admin/allowlist`` is the payee_allowlist convenience: adding a
  payee flips that payee's next call from escalate to allow; removing it
  flips it back.
- Unknown ``policy_id`` -> 404 on both admin routes.
- With no override ever set, engine behavior (and a representative HTTP
  round trip) is unchanged from before this feature existed.

Also covers the two ``PolicyOverrides`` backends directly (unit-level, no
HTTP): ``InMemoryPolicyOverrides`` deep-merge nuances in ``proxy.engine``,
``proxy.config.get_policy_overrides()``'s backend selection, and (skipped
unless DynamoDB Local is up) a ``DynamoPolicyOverrides`` round trip against
the shared ``amc-state`` table schema.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from conftest import DDB_LOCAL_ENDPOINT, ddb_local_available
from fastapi.testclient import TestClient

from proxy import config
from proxy.app import create_app
from proxy.audit import JsonlAuditLog
from proxy.ddb_bootstrap import create_tables, delete_tables
from proxy.engine import PolicyEngine
from proxy.overrides import DynamoPolicyOverrides, InMemoryPolicyOverrides
from proxy.state import InMemoryStateStore
from proxy.upstream.fake import FakeUpstreamExecutor

NOW = 1_700_000_000.0
DEFAULT_CAP_PAISE = 15_000_000  # policies/per_call_amount_cap.yaml default_cap_inr: 150000


def _client(
    tmp_path, policy_dir, *, policy_overrides=None
) -> tuple[TestClient, InMemoryPolicyOverrides]:
    overrides = policy_overrides if policy_overrides is not None else InMemoryPolicyOverrides()
    app = create_app(
        state_store=InMemoryStateStore(),
        audit_log=JsonlAuditLog(tmp_path / "audit.jsonl"),
        upstream=FakeUpstreamExecutor(),
        policy_dir=str(policy_dir),
        policy_overrides=overrides,
    )
    return TestClient(app), overrides


def _tool_call(client: TestClient, *, agent_id: str, tool: str, amount: int, payee: str | None):
    arguments = {"amount": amount}
    if payee is not None:
        arguments["payee"] = payee
    return client.post(
        "/tool-call",
        json={
            "agent_id": agent_id,
            "tool": tool,
            "arguments": arguments,
            "context": {"payee": payee, "provenance": []},
            "meta": {"labeled_legit": True},
        },
    )


# -- GET /admin/policy ------------------------------------------------------


def test_admin_get_policy_lists_all_loaded_policies_with_no_overrides(tmp_path, policy_dir):
    client, _ = _client(tmp_path, policy_dir)

    body = client.get("/admin/policy").json()
    policies = {p["policy_id"]: p for p in body["policies"]}
    assert set(policies) == {
        "provenance_check",
        "per_call_amount_cap",
        "velocity_aggregation",
        "payee_allowlist",
        "refund_to_capture_ratio",
    }

    cap = policies["per_call_amount_cap"]
    assert cap["enabled"] is True
    assert cap["overrides"] == {}
    # defaults: INR-facing, exactly as authored in YAML -- no derived *_paise keys.
    assert cap["defaults"]["default_cap_inr"] == 150000
    assert "default_cap_paise" not in cap["defaults"]
    # effective (no override): paise-facing, matches the YAML default.
    assert cap["effective"]["default_cap_paise"] == DEFAULT_CAP_PAISE
    assert cap["effective"]["default_cap_inr"] == 150000


# -- POST /admin/policy/{policy_id} -----------------------------------------


def test_post_admin_policy_sets_override_and_get_reflects_it(tmp_path, policy_dir):
    client, _ = _client(tmp_path, policy_dir)

    resp = client.post(
        "/admin/policy/per_call_amount_cap", json={"params": {"default_cap_inr": 20000}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["policy_id"] == "per_call_amount_cap"
    assert body["overrides"] == {"default_cap_inr": 20000}
    assert body["effective"]["default_cap_paise"] == 2_000_000
    # defaults (the YAML) are unaffected by the override.
    assert body["defaults"]["default_cap_inr"] == 150000

    # GET reflects the same state.
    listed = client.get("/admin/policy").json()
    cap = next(p for p in listed["policies"] if p["policy_id"] == "per_call_amount_cap")
    assert cap["overrides"] == {"default_cap_inr": 20000}
    assert cap["effective"]["default_cap_paise"] == 2_000_000


def test_post_admin_policy_unknown_policy_id_404(tmp_path, policy_dir):
    client, _ = _client(tmp_path, policy_dir)
    resp = client.post("/admin/policy/does_not_exist", json={"params": {"x": 1}})
    assert resp.status_code == 404


def test_override_takes_effect_live_on_next_tool_call(tmp_path, policy_dir):
    client, _ = _client(tmp_path, policy_dir)

    resp = client.post(
        "/admin/policy/per_call_amount_cap", json={"params": {"default_cap_inr": 20000}}
    )
    assert resp.status_code == 200
    new_cap_paise = 2_000_000

    # Just under the new (lowered) cap: allow.
    under = _tool_call(
        client,
        agent_id="agent-cap-under",
        tool="pay_vendor",
        amount=new_cap_paise - 1,
        payee="vendor_acme@hdfcbank",  # on the YAML allowlist -- isolates this to the cap check
    )
    assert under.status_code == 200
    assert under.json()["decision"] == "allow", under.json()["reason"]

    # Just over the new (lowered) cap: deny, and specifically by the capped policy.
    over = _tool_call(
        client,
        agent_id="agent-cap-over",
        tool="pay_vendor",
        amount=new_cap_paise + 1,
        payee="vendor_acme@hdfcbank",
    )
    assert over.status_code == 200
    assert over.json()["decision"] == "deny"
    assert over.json()["policy_id"] == "per_call_amount_cap"


def test_override_deep_merge_leaves_sibling_per_tool_caps_untouched(tmp_path, policy_dir):
    # Overriding only pay_vendor's per-tool cap must not wipe out
    # create_payment_link's YAML-configured override -- this is the "deep
    # merge", not a naive dict.update() that would replace the whole
    # overrides_inr map.
    client, overrides = _client(tmp_path, policy_dir)
    client.post(
        "/admin/policy/per_call_amount_cap",
        json={"params": {"overrides_inr": {"pay_vendor": 30000}}},
    )

    effective = client.get("/admin/policy").json()["policies"]
    cap = next(p for p in effective if p["policy_id"] == "per_call_amount_cap")["effective"]
    assert cap["overrides_paise"]["pay_vendor"] == 3_000_000
    # create_payment_link's YAML override (200000 INR) survives untouched.
    assert cap["overrides_paise"]["create_payment_link"] == 20_000_000
    # default_cap itself, never touched by this override, is also untouched.
    assert cap["default_cap_paise"] == DEFAULT_CAP_PAISE
    assert overrides.get("per_call_amount_cap") == {"overrides_inr": {"pay_vendor": 30000}}


# -- POST /admin/allowlist ----------------------------------------------------


def test_admin_allowlist_add_then_payee_passes(tmp_path, policy_dir):
    client, _ = _client(tmp_path, policy_dir)
    new_payee = "brand_new_vendor@upi"

    # Before: an unrecognized payee escalates (never a hard deny).
    before = _tool_call(
        client, agent_id="agent-allowlist-1", tool="pay_vendor", amount=1_000_000, payee=new_payee
    )
    assert before.json()["decision"] == "escalate"

    add_resp = client.post("/admin/allowlist", json={"payee": new_payee})
    assert add_resp.status_code == 200
    assert add_resp.json()["policy_id"] == "payee_allowlist"
    assert any(p.lower() == new_payee.lower() for p in add_resp.json()["known_payees"])

    # After: the same payee now allows outright.
    after = _tool_call(
        client, agent_id="agent-allowlist-2", tool="pay_vendor", amount=1_000_000, payee=new_payee
    )
    assert after.json()["decision"] == "allow", after.json()["reason"]

    # Adding again is idempotent (no duplicate entries).
    add_again = client.post("/admin/allowlist", json={"payee": new_payee})
    known = add_again.json()["known_payees"]
    assert sum(1 for p in known if p.lower() == new_payee.lower()) == 1


def test_admin_allowlist_remove_reverts_to_escalate(tmp_path, policy_dir):
    client, _ = _client(tmp_path, policy_dir)
    payee = "vendor_acme@hdfcbank"  # on the shipped YAML allowlist

    remove_resp = client.post("/admin/allowlist", json={"payee": payee, "action": "remove"})
    assert remove_resp.status_code == 200
    assert not any(p.lower() == payee.lower() for p in remove_resp.json()["known_payees"])

    resp = _tool_call(
        client, agent_id="agent-allowlist-3", tool="pay_vendor", amount=1_000_000, payee=payee
    )
    assert resp.json()["decision"] == "escalate"


# -- no overrides -> unchanged behavior ---------------------------------------


def test_no_overrides_engine_matches_pre_override_behavior(make_request, policy_dir):
    req = make_request(tool="pay_vendor", amount=1_000_000, payee="vendor_acme@hdfcbank")

    baseline = PolicyEngine(policy_dir).evaluate(req, InMemoryStateStore(), NOW)
    with_empty_overrides = PolicyEngine(policy_dir, overrides=InMemoryPolicyOverrides()).evaluate(
        req, InMemoryStateStore(), NOW
    )

    assert baseline.decision == with_empty_overrides.decision == "allow"
    assert baseline.policy_id == with_empty_overrides.policy_id
    assert baseline.reason == with_empty_overrides.reason
    baseline_trace_ids = [t.policy_id for t in baseline.trace]
    override_trace_ids = [t.policy_id for t in with_empty_overrides.trace]
    assert baseline_trace_ids == override_trace_ids


def test_no_overrides_http_known_good_call_still_allows(tmp_path, policy_dir):
    client, _ = _client(tmp_path, policy_dir)
    resp = _tool_call(
        client,
        agent_id="agent-baseline",
        tool="pay_vendor",
        amount=1_000_000,
        payee="vendor_acme@hdfcbank",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "allow"
    assert body["status"] == "executed"


# -- proxy.config.get_policy_overrides() backend selection -------------------


def test_get_policy_overrides_selects_backend_from_env(monkeypatch):
    config.get_policy_overrides.cache_clear()
    try:
        monkeypatch.delenv("POLICY_OVERRIDES_BACKEND", raising=False)
        monkeypatch.delenv("STATE_BACKEND", raising=False)
        assert isinstance(config.get_policy_overrides(), InMemoryPolicyOverrides)
        config.get_policy_overrides.cache_clear()

        # Defaults to STATE_BACKEND when POLICY_OVERRIDES_BACKEND is unset.
        monkeypatch.setenv("STATE_BACKEND", "dynamodb")
        assert isinstance(config.get_policy_overrides(), DynamoPolicyOverrides)
        config.get_policy_overrides.cache_clear()

        # Explicit POLICY_OVERRIDES_BACKEND wins over STATE_BACKEND.
        monkeypatch.delenv("STATE_BACKEND", raising=False)
        monkeypatch.setenv("POLICY_OVERRIDES_BACKEND", "dynamodb")
        assert isinstance(config.get_policy_overrides(), DynamoPolicyOverrides)
    finally:
        monkeypatch.delenv("POLICY_OVERRIDES_BACKEND", raising=False)
        monkeypatch.delenv("STATE_BACKEND", raising=False)
        config.get_policy_overrides.cache_clear()


# -- DynamoPolicyOverrides (skips unless DynamoDB Local is up) ---------------


@pytest.fixture(scope="module")
def ddb_policy_overrides() -> Iterator[DynamoPolicyOverrides]:
    if not ddb_local_available():
        pytest.skip(
            "\n"
            "==================================================================\n"
            " SKIPPED: DynamoDB Local is not reachable at localhost:8001.\n"
            " This is tests/test_admin_policy.py's DynamoPolicyOverrides round\n"
            " trip. Start DynamoDB Local with:\n"
            "     docker compose up -d dynamodb-local\n"
            " then re-run:\n"
            "     pytest -q tests/test_admin_policy.py\n"
            "==================================================================\n"
        )

    suffix = uuid.uuid4().hex[:8]
    state_table = f"amc-state-overrides-test-{suffix}"
    audit_table = f"amc-audit-overrides-test-{suffix}"
    approvals_table = f"amc-approvals-overrides-test-{suffix}"
    create_tables(
        endpoint_url=DDB_LOCAL_ENDPOINT,
        state_table=state_table,
        audit_table=audit_table,
        approvals_table=approvals_table,
    )
    try:
        yield DynamoPolicyOverrides(state_table, endpoint_url=DDB_LOCAL_ENDPOINT)
    finally:
        delete_tables(
            endpoint_url=DDB_LOCAL_ENDPOINT,
            state_table=state_table,
            audit_table=audit_table,
            approvals_table=approvals_table,
        )


def test_dynamo_policy_overrides_lifecycle(ddb_policy_overrides: DynamoPolicyOverrides) -> None:
    store = ddb_policy_overrides

    assert store.get("per_call_amount_cap") == {}
    assert store.all() == {}

    store.set("per_call_amount_cap", {"default_cap_inr": 50000})
    assert store.get("per_call_amount_cap") == {"default_cap_inr": 50000}

    store.set("payee_allowlist", {"known_payees": ["a@b", "c@d"]})
    all_overrides = store.all()
    assert all_overrides == {
        "per_call_amount_cap": {"default_cap_inr": 50000},
        "payee_allowlist": {"known_payees": ["a@b", "c@d"]},
    }

    # set() replaces wholesale, it does not merge.
    store.set("per_call_amount_cap", {"overrides_inr": {"pay_vendor": 90000}})
    assert store.get("per_call_amount_cap") == {"overrides_inr": {"pay_vendor": 90000}}
