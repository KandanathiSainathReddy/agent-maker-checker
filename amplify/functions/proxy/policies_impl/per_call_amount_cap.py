"""Hard per-call rupee ceiling, independent of history.

Payments rationale: this is the seatbelt, not the sensor. A single call that
moves an implausible amount in one shot — a fat-fingered amount, a unit
conversion bug, a prompt-injected six-figure refund — should never reach
Razorpay regardless of what velocity/payee/ratio checks would say, and it
should never depend on cross-call state being available. It is deliberately
the cheapest, most boring check, and (per ``policies/provenance_check.yaml``'s
priority) it runs right after the untrusted-provenance check.
"""

from __future__ import annotations

from proxy.policy_types import PolicyContext, PolicyEvaluation

POLICY_ID = "per_call_amount_cap"


def evaluate(ctx: PolicyContext) -> PolicyEvaluation:
    amount = int(ctx.request.arguments.get("amount", 0))
    overrides = ctx.params.get("overrides_paise", {})
    cap = int(overrides.get(ctx.request.tool, ctx.params["default_cap_paise"]))

    if amount > cap:
        return PolicyEvaluation(
            POLICY_ID,
            "deny",
            f"call amount {amount} paise exceeds the per-call cap of {cap} paise "
            f"for tool {ctx.request.tool!r}",
        )
    return PolicyEvaluation(
        POLICY_ID, "allow", f"call amount {amount} paise is within the {cap} paise per-call cap"
    )
