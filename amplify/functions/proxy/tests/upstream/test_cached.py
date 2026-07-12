"""CachedUpstream: determinism + realistic-Razorpay-shape assertions.

Runs fully offline with plain `pytest` -- no network, no keys, no event-loop
plugin required (async calls are driven with asyncio.run directly so this
doesn't depend on pytest-asyncio configuration existing anywhere else in the
repo).
"""

import asyncio
import json
from pathlib import Path

from proxy.upstream import cached as _cached
from proxy.upstream.cached import CachedUpstream

# Derive from the package module (robust to where the tree is rooted) rather than
# path arithmetic off __file__, which breaks when the dir depth changes.
FIXTURES_DIR = Path(_cached.__file__).resolve().parent / "fixtures"


def run(coro):
    return asyncio.run(coro)


def execute(tool: str, arguments: dict):
    return run(CachedUpstream().execute(tool, arguments))


# ---------------------------------------------------------------------------
# fixtures on disk really are realistic Razorpay entity shapes
# ---------------------------------------------------------------------------


def test_fixture_files_are_valid_realistic_json():
    payment_link = json.loads((FIXTURES_DIR / "payment_link.json").read_text())
    assert payment_link["id"].startswith("plink_")
    assert payment_link["entity"] == "payment_link"
    assert payment_link["short_url"].startswith("https://rzp.io/i/")
    assert set(payment_link) >= {"amount", "currency", "status", "notes", "customer"}

    refund = json.loads((FIXTURES_DIR / "refund.json").read_text())
    assert refund["id"].startswith("rfnd_")
    assert refund["entity"] == "refund"
    assert refund["payment_id"].startswith("pay_")
    assert refund["status"] == "processed"

    orders = json.loads((FIXTURES_DIR / "orders_list.json").read_text())
    assert orders["entity"] == "collection"
    assert isinstance(orders["items"], list) and orders["items"]
    assert orders["items"][0]["id"].startswith("order_")
    assert orders["items"][0]["entity"] == "order"

    payout = json.loads((FIXTURES_DIR / "payout.json").read_text())
    assert payout["id"].startswith("pout_")
    assert payout["entity"] == "payout"
    assert payout["fund_account_id"].startswith("fa_")


# ---------------------------------------------------------------------------
# UpstreamResult contract
# ---------------------------------------------------------------------------


def test_result_contract_shape():
    result = execute("create_payment_link", {"amount": 10000})
    assert result.ok is True
    assert result.tool == "create_payment_link"
    assert result.mode == "cached"
    assert result.error is None
    assert isinstance(result.data, dict)


# ---------------------------------------------------------------------------
# create_payment_link
# ---------------------------------------------------------------------------


def test_create_payment_link_shape_and_parameterization():
    args = {
        "amount": 500000,  # ₹5,000
        "description": "invoice #42",
        "notes": {"invoice": "42"},
        "customer_email": "ravi@example.com",
    }
    data = execute("create_payment_link", args).data
    assert data["id"].startswith("plink_")
    assert data["entity"] == "payment_link"
    assert data["short_url"].startswith("https://rzp.io/i/")
    assert data["amount"] == 500000
    assert data["currency"] == "INR"
    assert data["status"] == "created"
    assert data["description"] == "invoice #42"
    assert data["notes"] == {"invoice": "42"}
    assert data["customer"]["email"] == "ravi@example.com"


def test_create_payment_link_is_deterministic():
    args = {"amount": 250000, "notes": {"ref": "abc"}}
    first = execute("create_payment_link", args).data
    second = execute("create_payment_link", args).data
    assert first == second
    assert first["id"] == second["id"]
    assert first["short_url"] == second["short_url"]


def test_create_payment_link_different_args_different_id():
    a = execute("create_payment_link", {"amount": 100000}).data
    b = execute("create_payment_link", {"amount": 200000}).data
    assert a["id"] != b["id"]
    assert a["short_url"] != b["short_url"]


# ---------------------------------------------------------------------------
# issue_refund
# ---------------------------------------------------------------------------


def test_issue_refund_shape_matches_contract_example():
    # Same arguments shape as the worked example in infra/CONTRACTS.md #1.
    args = {"payment_id": "pay_x", "amount": 4000000, "payee": "cust_ravi@oksbi"}
    data = execute("issue_refund", args).data
    assert data["id"].startswith("rfnd_")
    assert data["entity"] == "refund"
    assert data["payment_id"] == "pay_x"
    assert data["amount"] == 4000000
    assert data["status"] == "processed"


def test_issue_refund_is_deterministic():
    args = {"payment_id": "pay_abc123", "amount": 4000}
    first = execute("issue_refund", args).data
    second = execute("issue_refund", args).data
    assert first == second


# ---------------------------------------------------------------------------
# list_orders
# ---------------------------------------------------------------------------


def test_list_orders_shape():
    data = execute("list_orders", {"count": 3}).data
    assert data["entity"] == "collection"
    assert data["count"] == 3
    assert len(data["items"]) == 3
    ids = [item["id"] for item in data["items"]]
    assert len(set(ids)) == 3  # each item gets a distinct deterministic id
    for item in data["items"]:
        assert item["id"].startswith("order_")
        assert item["entity"] == "order"


def test_list_orders_default_count_and_determinism():
    first = execute("list_orders", {}).data
    second = execute("list_orders", {}).data
    assert first == second
    assert first["count"] == 1


# ---------------------------------------------------------------------------
# pay_vendor (live gap -- cached must fully cover this tool)
# ---------------------------------------------------------------------------


def test_pay_vendor_shape_and_determinism():
    args = {"amount": 150000, "payee": "vendor_x@oksbi", "notes": {"po": "PO-9"}}
    first = execute("pay_vendor", args)
    second = execute("pay_vendor", args)
    assert first.ok is True
    assert first.data == second.data
    assert first.data["id"].startswith("pout_")
    assert first.data["entity"] == "payout"
    assert first.data["amount"] == 150000
    assert first.data["status"] == "processed"


# ---------------------------------------------------------------------------
# never raises, even for a tool outside our four Razorpay-mapped ones
# ---------------------------------------------------------------------------


def test_unknown_tool_does_not_raise():
    result = execute("get_ticket", {"ticket_id": "4471"})
    assert result.ok is True
    assert result.tool == "get_ticket"
    assert result.mode == "cached"
    assert result.data["echo"] == {"ticket_id": "4471"}


def test_missing_arguments_do_not_raise():
    # No 'amount' at all -- handler must fall back to the fixture default
    # rather than KeyError.
    result = execute("create_payment_link", {})
    assert result.ok is True
    assert result.data["amount"] > 0
