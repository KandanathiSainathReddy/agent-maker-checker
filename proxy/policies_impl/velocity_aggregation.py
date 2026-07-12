"""Rolling-window cross-call sum per (agent, tool, payee-context) — the structuring catch.

Payments rationale: this is the flagship policy. A single call under the
per-call cap is invisible in isolation; five of them inside a day is the same
large transfer split into pieces specifically to dodge the seatbelt
(structuring, in AML terms). ``per_call_amount_cap`` cannot see this — it
only ever looks at one call. This policy sums every call for the same
``(agent_id, tool, payee)`` inside a rolling window via
``StateStore.record_and_sum``, which is the one atomic primitive in this
whole system: it MUST give an exact answer under concurrent callers, or an
attacker (or a reviewer firing calls in parallel) could split a transfer
across simultaneous requests and have some of them lost to a race.

Once the window sum crosses the threshold, the ``(agent_id, tool)`` pair is
frozen — every subsequent call for that pair auto-denies, even if requested
sequentially or for a different payee, until a human reviews and clears it
via ``POST /admin/unfreeze`` or the approvals queue. ``escalate_unfreeze=True``
on the tripping evaluation tells the app layer to open that HITL ticket.
"""

from __future__ import annotations

from proxy.policy_types import PolicyContext, PolicyEvaluation

POLICY_ID = "velocity_aggregation"


def _key(agent_id: str, tool: str, payee: str) -> str:
    return f"{agent_id}#{tool}#{payee}"


def _payee_context(ctx: PolicyContext) -> str:
    return ctx.request.context.payee or ctx.request.arguments.get("payee") or "unknown"


def evaluate(ctx: PolicyContext) -> PolicyEvaluation:
    agent_id, tool = ctx.request.agent_id, ctx.request.tool

    if ctx.state.is_frozen(agent_id, tool):
        return PolicyEvaluation(
            POLICY_ID,
            "deny",
            f"{agent_id!r}/{tool!r} is frozen after a velocity threshold trip — "
            "pending a human unfreeze approval",
        )

    payee = _payee_context(ctx)
    key = _key(agent_id, tool, payee)
    window_s = int(ctx.params["window_s"])
    threshold = int(ctx.params["threshold_paise"])
    amount = int(ctx.request.arguments.get("amount", 0))

    new_sum = ctx.state.record_and_sum(key, amount, ctx.now, window_s)

    if new_sum > threshold:
        ctx.state.freeze(
            agent_id,
            tool,
            reason=(
                f"rolling {window_s}s window sum {new_sum} paise for key {key!r} "
                f"crossed the {threshold} paise velocity threshold"
            ),
        )
        return PolicyEvaluation(
            POLICY_ID,
            "deny",
            f"rolling window sum {new_sum} paise crossed the {threshold} paise threshold "
            f"for {key!r} — freezing {agent_id!r}/{tool!r} pending human review",
            escalate_unfreeze=True,
        )

    return PolicyEvaluation(
        POLICY_ID,
        "allow",
        f"rolling window sum {new_sum} paise is within the {threshold} paise threshold "
        f"for {key!r}",
    )
