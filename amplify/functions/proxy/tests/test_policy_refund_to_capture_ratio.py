"""refund_to_capture_ratio: refunds tracking captures allow; refunds far past
captures escalate (never deny). Ratio math is integer-paise so the boundary
(exactly at the cap allows, one paisa over escalates) is exact.
"""

from proxy.policies_impl import refund_to_capture_ratio
from proxy.policy_types import PolicyContext
from proxy.state import InMemoryStateStore

WINDOW_S = 86400
PARAMS = {"window_s": WINDOW_S, "max_ratio_pct": 50}
NOW = 1_700_000_000.0


def _capture(make_request, state, amount):
    ctx = PolicyContext(
        request=make_request(tool="create_payment_link", amount=amount),
        state=state, params=PARAMS, now=NOW,
    )
    return refund_to_capture_ratio.evaluate(ctx)


def _refund(make_request, state, amount):
    ctx = PolicyContext(
        request=make_request(tool="issue_refund", amount=amount),
        state=state, params=PARAMS, now=NOW,
    )
    return refund_to_capture_ratio.evaluate(ctx)


def test_capture_call_always_allows_and_records(make_request):
    state = InMemoryStateStore()
    result = _capture(make_request, state, 10_000_000)
    assert result.decision == "allow"
    captured, refunded = state.capture_and_refund_totals("support-agent-1", NOW, WINDOW_S)
    assert (captured, refunded) == (10_000_000, 0)


def test_allow_refund_within_ratio(make_request):
    state = InMemoryStateStore()
    _capture(make_request, state, 10_000_000)  # ₹1,00,000 captured
    result = _refund(make_request, state, 4_000_000)  # 40% of captured
    assert result.decision == "allow"


def test_escalate_refund_over_ratio(make_request):
    state = InMemoryStateStore()
    _capture(make_request, state, 10_000_000)
    result = _refund(make_request, state, 9_000_000)  # 90% of captured
    assert result.decision == "escalate"
    assert result.decision != "deny"


def test_escalate_refund_with_zero_captured(make_request):
    state = InMemoryStateStore()
    result = _refund(make_request, state, 1)
    assert result.decision == "escalate"


def test_boundary_exactly_at_ratio_allows(make_request):
    state = InMemoryStateStore()
    _capture(make_request, state, 10_000_000)  # captured
    result = _refund(make_request, state, 5_000_000)  # exactly 50% of captured
    assert result.decision == "allow"


def test_boundary_one_paisa_over_ratio_escalates(make_request):
    state = InMemoryStateStore()
    _capture(make_request, state, 10_000_000)
    result = _refund(make_request, state, 5_000_001)  # 50% + 1 paisa
    assert result.decision == "escalate"


def test_refund_recorded_even_when_escalated(make_request):
    state = InMemoryStateStore()
    _capture(make_request, state, 10_000_000)
    _refund(make_request, state, 9_000_000)  # escalates, but still recorded
    captured, refunded = state.capture_and_refund_totals("support-agent-1", NOW, WINDOW_S)
    assert (captured, refunded) == (10_000_000, 9_000_000)
