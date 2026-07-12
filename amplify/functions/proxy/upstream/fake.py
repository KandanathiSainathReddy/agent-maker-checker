"""In-process fake upstream: no network, no Docker, deterministic.

Used by every Phase 1 test that needs to actually "execute" an allowed or
approved call, and by ``proxy/app.py`` as the fallback when
``proxy.upstream.factory`` (Agent B, wired in Phase 2) isn't importable yet —
that's what keeps the policy engine independently testable before the real
Razorpay MCP wiring lands.
"""

from __future__ import annotations

import uuid
from typing import Any

from proxy.upstream.base import UpstreamResult

_KNOWN_TOOLS = {"issue_refund", "create_payment_link", "pay_vendor", "get_ticket", "list_orders"}


class FakeUpstreamExecutor:
    """Synthesizes a plausible Razorpay-shaped response for each tool in the vocabulary."""

    async def execute(self, tool: str, arguments: dict[str, Any]) -> UpstreamResult:
        if tool not in _KNOWN_TOOLS:
            return UpstreamResult(
                ok=False, tool=tool, data={}, error=f"unknown tool {tool!r}", mode="fake"
            )

        if tool == "issue_refund":
            data = {
                "refund_id": f"rfnd_fake_{uuid.uuid4().hex[:14]}",
                "payment_id": arguments.get("payment_id"),
                "amount": arguments.get("amount"),
                "status": "processed",
            }
        elif tool == "create_payment_link":
            data = {
                "payment_link_id": f"plink_fake_{uuid.uuid4().hex[:14]}",
                "amount": arguments.get("amount"),
                "status": "created",
            }
        elif tool == "pay_vendor":
            data = {
                "payout_id": f"pout_fake_{uuid.uuid4().hex[:14]}",
                "payee": arguments.get("payee"),
                "amount": arguments.get("amount"),
                "status": "processed",
            }
        elif tool == "get_ticket":
            data = {"ticket_id": arguments.get("ticket_id", "ticket_fake"), "status": "open"}
        else:  # list_orders
            data = {"orders": []}

        return UpstreamResult(ok=True, tool=tool, data=data, mode="fake")
