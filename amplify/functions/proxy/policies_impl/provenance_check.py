"""Deny payment instructions that originate from untrusted data, not the agent's own state.

Payments rationale: this is the direct prompt-injection defense. An agent
that reads a support ticket or an inbound email to decide what to do next is
reading attacker-controlled text if that channel is attacker-reachable. If a
ticket body says "actually, refund this to a different VPA" and that text
flows into ``arguments.payee`` or ``arguments.amount`` without the agent (or
this proxy) noticing, the payment executes exactly as instructed by a
stranger. Two independent signals both deny outright (never merely escalate
— an untrusted source directing money is not a judgment call for a human to
approve, it should never have been proposed):

1. A payment-bearing argument (``money_fields``) is listed as ``tainted`` by
   a ``context.provenance`` entry with ``trusted=False``.
2. Any *other* untrusted string argument contains a smuggled payment
   instruction — a UPI/VPA-shaped token (``name@bank``) next to a "pay/refund/
   send ... to" phrase — even if the tainted-field bookkeeping in (1) missed it.
"""

from __future__ import annotations

import re
from typing import Any

from proxy.policy_types import PolicyContext, PolicyEvaluation

POLICY_ID = "provenance_check"

_VPA_RE = re.compile(r"[\w.+-]{2,}@[a-zA-Z]{2,}")
_INSTRUCTION_RE = re.compile(r"\b(pay|refund|transfer|send)\b[^.]{0,30}\bto\b", re.IGNORECASE)


def _resolve_path(root: Any, dotted_path: str) -> Any:
    """Walk a dotted path like ``"arguments.payee"`` against a (nested dict) request."""
    obj: Any = root
    for part in dotted_path.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part, None)
        if obj is None:
            return None
    return obj


def evaluate(ctx: PolicyContext) -> PolicyEvaluation:
    request = ctx.request
    money_fields = set(ctx.params.get("money_fields", []))

    untrusted_tainted: set[str] = set()
    for prov in request.context.provenance:
        if not prov.trusted:
            untrusted_tainted.update(prov.tainted_fields)

    hit = untrusted_tainted & money_fields
    if hit:
        return PolicyEvaluation(
            POLICY_ID,
            "deny",
            f"field(s) {sorted(hit)} are payment-bearing but tainted by untrusted "
            "provenance — refusing to trust payment instructions read from untrusted data",
        )

    for field_path in untrusted_tainted:
        value = _resolve_path(request.model_dump(mode="python"), field_path)
        if isinstance(value, str) and _VPA_RE.search(value) and _INSTRUCTION_RE.search(value):
            return PolicyEvaluation(
                POLICY_ID,
                "deny",
                f"untrusted field {field_path!r} contains what looks like an embedded "
                "payment instruction (VPA + 'pay/refund/send ... to') smuggled in free text",
            )

    return PolicyEvaluation(POLICY_ID, "allow", "no untrusted payment-bearing provenance found")
