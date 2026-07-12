"""The structuring sequence, end-to-end through the real PolicyEngine + shipped
policies/*.yaml: a single oversized call gets stopped by the seatbelt, five
calls under the seatbelt but adding up over the velocity threshold trip the
flagship structuring catch, the pair freezes and auto-denies, and a human
unfreeze clears it.
"""

from proxy.engine import PolicyEngine
from proxy.state import InMemoryStateStore

NOW = 1_700_000_000.0
AGENT, TOOL, PAYEE = "support-agent-1", "pay_vendor", "vendor_acme@hdfcbank"


def test_structuring_sequence(policy_dir, make_request):
    engine = PolicyEngine(policy_dir)
    state = InMemoryStateStore()

    # A single ₹2,00,000 call is stopped by the per-call cap (₹1,50,000),
    # before velocity even gets a chance to see it.
    oversized = make_request(agent_id=AGENT, tool=TOOL, payee=PAYEE, amount=20_000_000)
    result = engine.evaluate(oversized, state, NOW)
    assert result.decision == "deny"
    assert result.policy_id == "per_call_amount_cap"
    assert state.window_sum(f"{AGENT}#{TOOL}#{PAYEE}", NOW, 86400) == 0, (
        "a call denied by the cap must not be counted toward velocity"
    )

    # Five ₹40,000 calls, each individually under the cap. The first three
    # (₹40k/80k/120k cumulative) allow; the fourth (₹160k cumulative) crosses
    # the ₹1,50,000 velocity threshold and freezes the pair.
    for i in range(3):
        req = make_request(agent_id=AGENT, tool=TOOL, payee=PAYEE, amount=4_000_000)
        result = engine.evaluate(req, state, NOW + i)
        assert result.decision == "allow", f"call {i + 1} of 5 should allow"

    tripping = make_request(agent_id=AGENT, tool=TOOL, payee=PAYEE, amount=4_000_000)
    result = engine.evaluate(tripping, state, NOW + 3)
    assert result.decision == "deny"
    assert result.policy_id == "velocity_aggregation"
    assert result.escalate_unfreeze is True
    assert state.is_frozen(AGENT, TOOL)

    # The pair is frozen — the fifth call auto-denies even though it is,
    # individually, identical to the four that already succeeded.
    fifth = make_request(agent_id=AGENT, tool=TOOL, payee=PAYEE, amount=4_000_000)
    result = engine.evaluate(fifth, state, NOW + 4)
    assert result.decision == "deny"
    assert result.policy_id == "velocity_aggregation"
    assert "frozen" in result.reason.lower()

    # The window sum stops at ₹1,60,000 (four calls) — the fifth call's
    # amount was never added because velocity_aggregation's frozen check
    # short-circuits before record_and_sum runs. (The concurrent-firing
    # variant of this scenario, tests/test_velocity_concurrency.py, is where
    # all five calls race each other and the sum does reach ₹2,00,000 exactly.)
    assert state.window_sum(f"{AGENT}#{TOOL}#{PAYEE}", NOW + 4, 86400) == 16_000_000

    # A human clears the freeze; the pair is no longer frozen.
    state.unfreeze(AGENT, TOOL)
    assert not state.is_frozen(AGENT, TOOL)

    # Once the window fully rolls over, a fresh call is evaluated against a
    # clean window and allows again.
    fresh_window = make_request(agent_id=AGENT, tool=TOOL, payee=PAYEE, amount=4_000_000)
    result = engine.evaluate(fresh_window, state, NOW + 86400 + 1)
    assert result.decision == "allow"
