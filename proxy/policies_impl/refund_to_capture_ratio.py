"""Refunds exceeding X% of captured volume in the window escalate.

Payments rationale: refunds are supposed to track captures — an agent (or an
attacker walking a compromised agent through a refund flow) issuing refunds
far in excess of what was actually captured in the same window is a
processing-error or laundering-shaped pattern, not a normal payments
pattern. Escalate rather than deny: legitimate high-refund periods happen
(a bad batch, a recalled product) and a human should look at the underlying
orders rather than have the agent silently blocked.

The tool vocabulary has no dedicated "capture" tool, so ``create_payment_link``
calls stand in for capture-side inflow (money coming in) and ``issue_refund``
calls are the outflow being measured against it — both funnel through this
one policy, branching on ``ctx.request.tool``.

The ratio comparison is done in integer paise (``refunded * 100 > captured *
max_ratio_pct``) rather than floating point specifically so the boundary
(exactly at the cap allows, one paisa over escalates) is exact.
"""

from __future__ import annotations

from proxy.policy_types import PolicyContext, PolicyEvaluation

POLICY_ID = "refund_to_capture_ratio"


def evaluate(ctx: PolicyContext) -> PolicyEvaluation:
    tool = ctx.request.tool
    agent_id = ctx.request.agent_id
    window_s = int(ctx.params["window_s"])
    amount = int(ctx.request.arguments.get("amount", 0))

    if tool == "create_payment_link":
        ctx.state.add_capture(agent_id, amount, ctx.now, window_s)
        return PolicyEvaluation(
            POLICY_ID, "allow", "capture recorded for refund-to-capture-ratio tracking"
        )

    if tool != "issue_refund":
        return PolicyEvaluation(POLICY_ID, "allow", "not a capture or refund tool")

    captured_paise, refunded_paise = ctx.state.capture_and_refund_totals(
        agent_id, ctx.now, window_s
    )
    prospective_refunded = refunded_paise + amount
    max_ratio_pct = int(ctx.params["max_ratio_pct"])

    # Recording happens regardless of decision — same "count every attempt,
    # not just executed ones" logic as velocity_aggregation, so a sequence of
    # escalated-then-approved refunds still accumulates correctly.
    ctx.state.add_refund(agent_id, amount, ctx.now, window_s)

    if captured_paise == 0:
        if prospective_refunded == 0:
            return PolicyEvaluation(POLICY_ID, "allow", "no refund amount and no captured volume")
        return PolicyEvaluation(
            POLICY_ID,
            "escalate",
            f"refund of {amount} paise against zero captured volume in the {window_s}s "
            "window — needs human review",
        )

    if prospective_refunded * 100 > captured_paise * max_ratio_pct:
        pct = (prospective_refunded * 100) / captured_paise
        return PolicyEvaluation(
            POLICY_ID,
            "escalate",
            f"refunds would be {pct:.1f}% of the {captured_paise} paise captured in the "
            f"{window_s}s window, above the {max_ratio_pct}% cap — needs human review",
        )

    pct = (prospective_refunded * 100) / captured_paise
    return PolicyEvaluation(
        POLICY_ID,
        "allow",
        f"refunds are {pct:.1f}% of captured volume in window, within the {max_ratio_pct}% cap",
    )
