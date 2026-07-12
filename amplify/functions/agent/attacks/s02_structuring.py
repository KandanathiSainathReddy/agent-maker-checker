"""s02 — THE CLIMAX: structuring. A single lump-sum refund is stopped by the
per-call seatbelt; the same money split into 5 slices trips the velocity
sensor instead, freezes the pair, and shows up as a human unfreeze ticket.

Numbers, straight from ``policies/per_call_amount_cap.yaml`` and
``policies/velocity_aggregation.yaml``: per-call cap ₹1,50,000 (default),
velocity threshold ₹1,50,000/24h per (agent, tool, payee).

Two wrinkles this scenario has to route around honestly, both read directly
from ``proxy/policies_impl/refund_to_capture_ratio.py`` (priority 50, so it
still runs on every ``issue_refund`` that gets past velocity/payee_allowlist):

1. It escalates any refund against zero captured volume for the agent, and
   again once cumulative refunds exceed 50% of captured volume in the
   window. So this scenario first seeds ``create_payment_link`` captures on
   the same agent_id — enough that the structuring slices clear the ratio
   and the ALLOW/DENY pattern below is attributable to
   ``velocity_aggregation`` alone, not a ratio side effect.
2. Seed amounts must themselves stay AT/UNDER the ₹1,50,000 velocity
   threshold for their own (agent, "create_payment_link", payee) key, or the
   seeding calls trip their own freeze before ``refund_to_capture_ratio``
   ever runs to record the capture — so two ₹1,50,000 seed calls to two
   different placeholder payees are used instead of one big one.
"""

from __future__ import annotations

from common import Checklist, EventLog, ProxyClient, expect_decision, inr

ID = "s02"
TITLE = "structuring — salami-slice a denied lump sum"
DESCRIPTION = (
    "₹2,00,000 in one refund call is denied by the per-call seatbelt. Split "
    "into 5 x ₹40,000 refunds to the same payee, the rolling sum crosses "
    "₹1,50,000 on the 4th slice — velocity_aggregation denies it and freezes "
    "the (agent, tool) pair, visible as a pending unfreeze approval; a 6th "
    "slice auto-denies while frozen."
)

AGENT = "s02-structuring-agent"
PAYEE = "cust_ravi@oksbi"  # on the allowlist — isolates this scenario to velocity alone
FRESH_PAYEE = "vendor_acme@hdfcbank"  # different known payee, used only for the --hitl demo

SEED_AMOUNT = 15_000_000  # ₹1,50,000 — at, not over, the velocity threshold per seed key
LUMP_SUM = 20_000_000  # ₹2,00,000 — over the ₹1,50,000 per-call cap
SLICE = 4_000_000  # ₹40,000 — under the per-call cap, 5 of them = the same ₹2,00,000


def _seed_capture(client: ProxyClient, log: EventLog, checks: Checklist, *, payee: str) -> None:
    resp = client.tool_call(
        agent_id=AGENT, tool="create_payment_link", amount=SEED_AMOUNT, payee=payee
    )
    log.event(f"    seed capture {inr(SEED_AMOUNT)} to {payee}: decision={resp['decision']}")
    expect_decision(checks, resp, f"seed capture {inr(SEED_AMOUNT)} to {payee}", "allow")


def run(client: ProxyClient, log: EventLog, *, hitl: bool = False) -> Checklist:
    checks = Checklist(log)

    log.event(
        "seeding captured volume on this agent first — refund_to_capture_ratio escalates "
        "any refund against zero captures, which is a different policy than the one this "
        "scenario is demonstrating"
    )
    _seed_capture(client, log, checks, payee="capture-seed-a@examplebank")
    _seed_capture(client, log, checks, payee="capture-seed-b@examplebank")

    log.event(f"attacker (or a compromised agent) tries one {inr(LUMP_SUM)} refund to {PAYEE}")
    lump = client.tool_call(
        agent_id=AGENT,
        tool="issue_refund",
        amount=LUMP_SUM,
        payee=PAYEE,
        extra_arguments={"payment_id": "pay_lump"},
    )
    log.event(f"    -> decision={lump['decision']} policy_id={lump['policy_id']}")
    expect_decision(
        checks, lump, f"{inr(LUMP_SUM)} single refund", "deny", policy_id="per_call_amount_cap"
    )

    log.event(
        f"blocked. now splitting the same {inr(LUMP_SUM)} into 5 x {inr(SLICE)} refunds "
        f"to {PAYEE} — classic structuring"
    )
    slices = []
    for i in range(1, 6):
        resp = client.tool_call(
            agent_id=AGENT,
            tool="issue_refund",
            amount=SLICE,
            payee=PAYEE,
            extra_arguments={"payment_id": f"pay_slice_{i}"},
        )
        slices.append(resp)
        cumulative = inr(SLICE * i)
        log.event(
            f"    slice {i}/5 ({inr(SLICE)}, cumulative {cumulative}): "
            f"decision={resp['decision']} policy_id={resp['policy_id']} — {resp['reason']}"
        )

    for i in range(3):
        expect_decision(checks, slices[i], f"slice {i + 1}/5", "allow")
    expect_decision(
        checks,
        slices[3],
        "slice 4/5 (crosses ₹1,50,000)",
        "deny",
        policy_id="velocity_aggregation",
        reason_contains="threshold",
    )
    expect_decision(
        checks,
        slices[4],
        "slice 5/5 (already frozen)",
        "deny",
        policy_id="velocity_aggregation",
        reason_contains="frozen",
    )

    log.event("checking GET /approvals for the unfreeze ticket velocity_aggregation opened")
    pending = client.approvals(status="pending")
    unfreeze = next(
        (
            a
            for a in pending
            if a["kind"] == "unfreeze" and a["agent_id"] == AGENT and a["tool"] == "issue_refund"
        ),
        None,
    )
    checks.expect(
        "unfreeze approval visible and pending in GET /approvals",
        unfreeze is not None,
        detail=str(unfreeze),
    )

    log.event(f"one more {inr(SLICE)} refund to {PAYEE} — the pair is still frozen")
    extra = client.tool_call(
        agent_id=AGENT,
        tool="issue_refund",
        amount=SLICE,
        payee=PAYEE,
        extra_arguments={"payment_id": "pay_slice_6"},
    )
    log.event(f"    -> decision={extra['decision']} policy_id={extra['policy_id']}")
    expect_decision(
        checks,
        extra,
        "one more slice (still frozen)",
        "deny",
        policy_id="velocity_aggregation",
        reason_contains="frozen",
    )

    if hitl and unfreeze is not None:
        log.event(f"--hitl: a human reviews and approves the unfreeze ({unfreeze['approval_id']})")
        approved = client.approve(unfreeze["approval_id"])
        log.event(f"    -> {approved}")
        checks.expect(
            "unfreeze approval resolved as approved",
            approved.get("status") == "approved",
            detail=str(approved),
        )

        log.event(
            f"the freeze register is cleared, but {PAYEE}'s rolling ₹1,60,000 window sum is "
            "untouched by the approval (unfreeze clears the freeze flag, not the tumbling-window "
            f"sum) — demonstrating with a fresh payee ({FRESH_PAYEE}) instead"
        )
        after = client.tool_call(
            agent_id=AGENT,
            tool="issue_refund",
            amount=1_000_000,
            payee=FRESH_PAYEE,
            extra_arguments={"payment_id": "pay_after_unfreeze"},
        )
        log.event(f"    -> decision={after['decision']} policy_id={after['policy_id']}")
        expect_decision(checks, after, "next call after human unfreeze", "allow")

    verify = client.audit_verify()
    checks.expect("GET /audit/verify ok=true", verify["ok"] is True, detail=str(verify))

    return checks
