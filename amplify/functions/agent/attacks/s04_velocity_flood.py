"""s04 — velocity flood: many small, individually unremarkable pay_vendor
calls to one payee, none of which are structuring-shaped (unlike s02, this
isn't a deliberate few-slices split of a specific denied amount — it's
sheer repetition), but the rolling sum still crosses the same
₹1,50,000/24h threshold and trips the same freeze.

₹10,000 x 15 lands exactly on the ₹1,50,000 threshold (still allowed — the
policy denies only once the sum is strictly *over* the cap); the 16th call
tips it over and freezes the pair; the 17th auto-denies while frozen.
"""

from __future__ import annotations

from common import Checklist, EventLog, ProxyClient, expect_decision, inr

ID = "s04"
TITLE = "velocity flood — many small calls, one payee"
DESCRIPTION = (
    "17 rapid ₹10,000 pay_vendor calls to one payee: the first 15 (summing "
    "to exactly ₹1,50,000) allow, the 16th crosses the threshold and denies "
    "+ freezes, the 17th auto-denies while frozen."
)

AGENT = "s04-flood-agent"
PAYEE = "vendor_globex@icici"
SLICE = 1_000_000  # ₹10,000 per call
THRESHOLD = 15_000_000  # ₹1,50,000 — matches policies/velocity_aggregation.yaml


def run(client: ProxyClient, log: EventLog, *, hitl: bool = False) -> Checklist:
    checks = Checklist(log)
    calls_to_threshold = THRESHOLD // SLICE  # 15 calls land exactly on the threshold
    total_calls = calls_to_threshold + 2  # + the tripping call + the auto-denied call after it

    log.event(
        f"flooding pay_vendor with {total_calls} rapid {inr(SLICE)} calls to {PAYEE} — "
        "no single call is remarkable, but the rolling sum is"
    )

    responses: list[dict] = []
    tripped_at: int | None = None
    for i in range(1, total_calls + 1):
        resp = client.tool_call(
            agent_id=AGENT,
            tool="pay_vendor",
            amount=SLICE,
            payee=PAYEE,
            extra_arguments={"payment_id": f"pay_flood_{i}"},
        )
        responses.append(resp)
        cumulative = inr(SLICE * i)
        log.event(
            f"    call {i:>2} (cumulative {cumulative}): decision={resp['decision']} "
            f"policy_id={resp['policy_id']}"
        )
        if resp["decision"] != "allow" and tripped_at is None:
            tripped_at = i

    for i, resp in enumerate(responses, start=1):
        if i <= calls_to_threshold:
            expect_decision(checks, resp, f"call {i}", "allow")
        elif i == calls_to_threshold + 1:
            expect_decision(
                checks,
                resp,
                f"call {i} (first over ₹1,50,000)",
                "deny",
                policy_id="velocity_aggregation",
                reason_contains="threshold",
            )
        else:
            expect_decision(
                checks,
                resp,
                f"call {i} (after freeze)",
                "deny",
                policy_id="velocity_aggregation",
                reason_contains="frozen",
            )

    checks.expect(
        f"velocity tripped exactly at call {calls_to_threshold + 1}",
        tripped_at == calls_to_threshold + 1,
        detail=f"actually tripped at call {tripped_at}",
    )

    verify = client.audit_verify()
    checks.expect("GET /audit/verify ok=true", verify["ok"] is True, detail=str(verify))

    return checks
