"""Known payees pass silently; a payee the agent has never paid before escalates.

Payments rationale: a new payee is exactly the moment a compromised or
confused agent (or an injected instruction) could redirect funds to an
attacker-controlled account — but "first payment to a new vendor" is also
completely ordinary business, so this deliberately escalates to a human
rather than denies outright. Denying every new payee would make the system
useless for any agent that legitimately grows its payee list over time.
"""

from __future__ import annotations

from proxy.policy_types import PolicyContext, PolicyEvaluation

POLICY_ID = "payee_allowlist"


def evaluate(ctx: PolicyContext) -> PolicyEvaluation:
    payee = ctx.request.context.payee or ctx.request.arguments.get("payee")
    known = {p.lower() for p in ctx.params.get("known_payees", [])}

    if not payee:
        return PolicyEvaluation(
            POLICY_ID, "escalate", "call has no identifiable payee — needs human review"
        )
    if payee.lower() in known:
        return PolicyEvaluation(POLICY_ID, "allow", f"payee {payee!r} is on the allowlist")
    return PolicyEvaluation(
        POLICY_ID,
        "escalate",
        f"payee {payee!r} is not on the allowlist (no prior approved payment) — "
        "needs human review before the first payment",
    )
