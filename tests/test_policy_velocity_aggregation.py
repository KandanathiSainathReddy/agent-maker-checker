"""velocity_aggregation: the flagship structuring catch.

Allow under threshold, trip (deny + freeze + escalate_unfreeze) crossing it,
exact boundary, and frozen-pair auto-deny.
"""

from proxy.policies_impl import velocity_aggregation
from proxy.policy_types import PolicyContext
from proxy.state import InMemoryStateStore

THRESHOLD_PAISE = 15_000_000  # ₹1,50,000
WINDOW_S = 86400
PARAMS = {"window_s": WINDOW_S, "threshold_inr": 150000, "threshold_paise": THRESHOLD_PAISE}
NOW = 1_700_000_000.0


def _ctx(make_request, state, amount, now=NOW, agent_id="agent-1", tool="pay_vendor"):
    return PolicyContext(
        request=make_request(agent_id=agent_id, tool=tool, amount=amount),
        state=state,
        params=PARAMS,
        now=now,
    )


def test_allow_single_call_under_threshold(make_request):
    state = InMemoryStateStore()
    result = velocity_aggregation.evaluate(_ctx(make_request, state, amount=4_000_000))
    assert result.decision == "allow"
    assert not state.is_frozen("agent-1", "pay_vendor")


def test_trip_crosses_threshold_denies_freezes_and_escalates(make_request):
    state = InMemoryStateStore()
    # 3 calls of 40,000 INR keep the sum at 120,000 INR — under threshold.
    for _ in range(3):
        result = velocity_aggregation.evaluate(_ctx(make_request, state, amount=4_000_000))
        assert result.decision == "allow"

    # The 4th call pushes the sum to 160,000 INR, over the 150,000 threshold.
    result = velocity_aggregation.evaluate(_ctx(make_request, state, amount=4_000_000))
    assert result.decision == "deny"
    assert result.escalate_unfreeze is True
    assert state.is_frozen("agent-1", "pay_vendor")


def test_frozen_pair_auto_denies_next_call(make_request):
    state = InMemoryStateStore()
    state.freeze("agent-1", "pay_vendor", reason="test setup")
    result = velocity_aggregation.evaluate(_ctx(make_request, state, amount=100))
    assert result.decision == "deny"
    assert "frozen" in result.reason.lower()
    # A frozen call must not have been added to the window sum.
    assert state.window_sum("agent-1#pay_vendor#vendor_acme@hdfcbank", NOW, WINDOW_S) == 0


def test_boundary_exactly_at_threshold_allows(make_request):
    state = InMemoryStateStore()
    result = velocity_aggregation.evaluate(_ctx(make_request, state, amount=THRESHOLD_PAISE))
    assert result.decision == "allow"


def test_boundary_one_paisa_over_threshold_denies(make_request):
    state = InMemoryStateStore()
    result = velocity_aggregation.evaluate(_ctx(make_request, state, amount=THRESHOLD_PAISE + 1))
    assert result.decision == "deny"
    assert result.escalate_unfreeze is True


def test_separate_payees_get_independent_windows(make_request):
    state = InMemoryStateStore()
    r1 = velocity_aggregation.evaluate(
        PolicyContext(
            request=make_request(tool="pay_vendor", amount=THRESHOLD_PAISE,
                                  payee="vendor_acme@hdfcbank"),
            state=state, params=PARAMS, now=NOW,
        )
    )
    r2 = velocity_aggregation.evaluate(
        PolicyContext(
            request=make_request(tool="pay_vendor", amount=THRESHOLD_PAISE,
                                  payee="vendor_globex@icici"),
            state=state, params=PARAMS, now=NOW,
        )
    )
    assert r1.decision == "allow"
    assert r2.decision == "allow"
