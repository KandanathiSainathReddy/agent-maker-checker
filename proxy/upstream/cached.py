"""Deterministic, offline replay of Razorpay responses.

`DEMO_MODE=cached` (the default -- see infra/CONTRACTS.md #2) fronts this
instead of the live MCP server so the full policy-engine demo runs with zero
keys and zero network calls. Every response is built from a REALISTIC
Razorpay entity shape checked into fixtures/ (payment_link, refund,
orders_list, payout -- mirroring the actual Razorpay API schemas, verified
against https://razorpay.com/docs/api/ during Phase 2 research; see
infra/razorpay-mcp.md), then parameterized from the request's own
amount/notes/payment_id/etc. IDs are derived from a sha256 hash of the
arguments -- same input always produces the same output, with no clock and
no randomness, so the audit trail and any test asserting on a specific
request are reproducible run after run.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .base import UpstreamExecutor, UpstreamResult

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Razorpay ids are short mixed-case alnum strings after the entity prefix
# (e.g. plink_ExjpAUN3gVHrPJ). Mirror that shape rather than emitting a raw
# hex digest, so cached responses read like real Razorpay ids.
_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def _load_fixture(name: str) -> dict[str, Any]:
    with open(FIXTURES_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


def _det_suffix(*parts: Any, length: int = 14) -> str:
    """Deterministic Razorpay-style alnum suffix hashed from `parts`."""
    digest = hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str).encode()
    ).digest()
    while len(digest) < length:
        digest += hashlib.sha256(digest).digest()
    return "".join(_ID_ALPHABET[b % len(_ID_ALPHABET)] for b in digest[:length])


def _det_id(prefix: str, *parts: Any, length: int = 14) -> str:
    return f"{prefix}{_det_suffix(*parts, length=length)}"


class CachedUpstream(UpstreamExecutor):
    """UpstreamExecutor that deterministically replays fixture data.

    No I/O, no keys, no network -- safe to construct and call in any
    environment, including CI.
    """

    async def execute(self, tool: str, arguments: dict[str, Any]) -> UpstreamResult:
        arguments = arguments or {}
        try:
            handler = self._HANDLERS.get(tool)
            data = handler(self, arguments) if handler else self._generic(tool, arguments)
            return UpstreamResult(ok=True, tool=tool, data=data, error=None, mode="cached")
        except Exception as exc:  # never raise into the proxy hot path
            return UpstreamResult(
                ok=False, tool=tool, data={}, error=f"cached upstream error: {exc}", mode="cached"
            )

    # -- per-tool handlers, one realistic fixture each ------------------

    def _create_payment_link(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tpl = copy.deepcopy(_load_fixture("payment_link.json"))
        tpl["id"] = _det_id("plink_", "create_payment_link", arguments)
        tpl["amount"] = int(arguments.get("amount", tpl["amount"]))
        tpl["currency"] = arguments.get("currency", tpl["currency"])
        tpl["description"] = arguments.get("description", tpl["description"])
        tpl["notes"] = arguments.get("notes", tpl["notes"])
        tpl["reference_id"] = arguments.get("reference_id", tpl["reference_id"])
        tpl["callback_url"] = arguments.get("callback_url", tpl["callback_url"])
        customer = tpl.setdefault("customer", {})
        customer["name"] = arguments.get("customer_name", customer.get("name", ""))
        customer["email"] = arguments.get("customer_email", customer.get("email", ""))
        customer["contact"] = arguments.get(
            "customer_contact", arguments.get("payee", customer.get("contact", ""))
        )
        tpl["short_url"] = f"https://rzp.io/i/{_det_suffix('short_url', arguments, length=7)}"
        return tpl

    def _issue_refund(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tpl = copy.deepcopy(_load_fixture("refund.json"))
        tpl["id"] = _det_id("rfnd_", "issue_refund", arguments)
        tpl["amount"] = int(arguments.get("amount", tpl["amount"]))
        tpl["currency"] = arguments.get("currency", tpl["currency"])
        tpl["payment_id"] = arguments.get("payment_id", tpl["payment_id"])
        tpl["notes"] = arguments.get("notes", tpl["notes"])
        tpl["speed_requested"] = arguments.get("speed", tpl["speed_requested"])
        return tpl

    def _list_orders(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tpl = copy.deepcopy(_load_fixture("orders_list.json"))
        requested = int(arguments.get("count", 1) or 1)
        count = max(1, min(requested, 5))  # keep the cached list small & deterministic
        base_item = tpl["items"][0]
        items = []
        for i in range(count):
            item = copy.deepcopy(base_item)
            item["id"] = _det_id("order_", "list_orders", arguments, i)
            item["receipt"] = f"receipt#{i + 1}"
            items.append(item)
        tpl["items"] = items
        tpl["count"] = len(items)
        return tpl

    def _pay_vendor(self, arguments: dict[str, Any]) -> dict[str, Any]:
        # NOTE: razorpay-mcp-server (v1.2.1, researched 2026-07-12) exposes no
        # create/initiate payout tool -- its payouts toolset is fetch-only
        # (fetch_payout_with_id, fetch_all_payouts). MCPUpstream therefore
        # cannot execute pay_vendor live at all (see mcp_client.py /
        # infra/razorpay-mcp.md #Gaps). This cached path is the only place
        # pay_vendor can be demoed end-to-end.
        tpl = copy.deepcopy(_load_fixture("payout.json"))
        tpl["id"] = _det_id("pout_", "pay_vendor", arguments)
        tpl["fund_account_id"] = _det_id(
            "fa_", "fund_account", arguments.get("payee", ""), length=12
        )
        tpl["amount"] = int(arguments.get("amount", tpl["amount"]))
        tpl["currency"] = arguments.get("currency", tpl["currency"])
        tpl["notes"] = arguments.get("notes", tpl["notes"])
        tpl["reference_id"] = arguments.get("reference_id", tpl["reference_id"])
        tpl["narration"] = arguments.get("description", tpl["narration"])
        tpl["utr"] = _det_suffix("utr", arguments, length=16).upper()
        return tpl

    def _generic(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Anything outside our four Razorpay-mapped tools (e.g. get_ticket,
        # which is not a Razorpay concept at all -- see mcp_client.py) still
        # gets a deterministic, non-raising response so DEMO_MODE=cached can
        # always run the full agent loop offline.
        return {
            "tool": tool,
            "cached_id": _det_id("cch_", tool, arguments),
            "echo": arguments,
            "note": "no Razorpay fixture for this tool; deterministic generic echo",
        }

    _HANDLERS: dict[str, Callable[[CachedUpstream, dict[str, Any]], dict[str, Any]]] = {
        "create_payment_link": _create_payment_link,
        "issue_refund": _issue_refund,
        "list_orders": _list_orders,
        "pay_vendor": _pay_vendor,
    }
