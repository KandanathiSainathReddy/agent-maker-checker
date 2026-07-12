"""s03 — payee swap: an identical vendor payment, but the second call's payee
has never been paid before. ``payee_allowlist`` treats known payees as
routine and escalates (not denies) the first payment to any new one —
"first payment to a new vendor" is normal business, but it is also exactly
the moment a compromised or confused agent could redirect funds, so a human
sees it once.
"""

from __future__ import annotations

from common import Checklist, EventLog, ProxyClient, expect_decision, inr

ID = "s03"
TITLE = "payee swap — same amount, unrecognized account"
DESCRIPTION = (
    "A routine vendor payment to a known payee allows silently. The same "
    "amount to an account never paid before escalates on payee_allowlist "
    "instead of denying, and is visible pending in GET /approvals."
)

AGENT = "s03-vendor-agent"
KNOWN_PAYEE = "vendor_acme@hdfcbank"
UNKNOWN_PAYEE = "vendor-newco-8842@somebank"
AMOUNT = 1_500_000  # ₹15,000 — well under every cap/threshold, so only payee_allowlist is in play


def run(client: ProxyClient, log: EventLog, *, hitl: bool = False) -> Checklist:
    checks = Checklist(log)

    log.event(f"routine vendor payment: {inr(AMOUNT)} to the known payee {KNOWN_PAYEE}")
    known = client.tool_call(agent_id=AGENT, tool="pay_vendor", amount=AMOUNT, payee=KNOWN_PAYEE)
    log.event(f"    -> decision={known['decision']} policy_id={known['policy_id']}")
    expect_decision(checks, known, "known-payee payout", "allow")

    log.event(
        f"same amount, but the payee has swapped to an account never paid before: "
        f"{UNKNOWN_PAYEE}"
    )
    swapped = client.tool_call(
        agent_id=AGENT, tool="pay_vendor", amount=AMOUNT, payee=UNKNOWN_PAYEE
    )
    log.event(
        f"    -> decision={swapped['decision']} policy_id={swapped['policy_id']} "
        f"status={swapped['status']}"
    )
    expect_decision(
        checks, swapped, "unknown-payee payout", "escalate", policy_id="payee_allowlist"
    )
    checks.expect(
        "held for human review, not executed",
        swapped["status"] == "pending_approval" and swapped["upstream_result"] is None,
        detail=f"status={swapped['status']} upstream_result={swapped['upstream_result']}",
    )

    log.event("checking GET /approvals for the held payout")
    pending = client.approvals(status="pending")
    held = next(
        (
            a
            for a in pending
            if a["kind"] == "tool_call"
            and a["agent_id"] == AGENT
            and a["arguments"].get("payee") == UNKNOWN_PAYEE
        ),
        None,
    )
    checks.expect(
        "escalated payout visible and pending in GET /approvals", held is not None, detail=str(held)
    )

    if held is not None:
        log.event(f"a human reviews it once and approves ({held['approval_id']})")
        resolved = client.approve(held["approval_id"])
        log.event(f"    -> decision={resolved.get('decision')} status={resolved.get('status')}")
        checks.expect(
            "approved payout executes via the upstream and moves money",
            resolved.get("decision") == "allow" and resolved.get("status") == "executed",
            detail=str(resolved),
        )

    verify = client.audit_verify()
    checks.expect("GET /audit/verify ok=true", verify["ok"] is True, detail=str(verify))

    return checks
