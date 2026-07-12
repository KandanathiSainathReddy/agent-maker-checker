"""per_call_amount_cap: allow under the cap, trip over it, exact-boundary behavior."""

from proxy.policies_impl import per_call_amount_cap
from proxy.policy_types import PolicyContext
from proxy.state import InMemoryStateStore

CAP_PAISE = 15_000_000  # ₹1,50,000
PARAMS = {
    "default_cap_inr": 150000,
    "default_cap_paise": CAP_PAISE,
    "overrides_inr": {"create_payment_link": 200000},
    "overrides_paise": {"create_payment_link": 20_000_000},
}


def _ctx(make_request, amount: int, tool: str = "pay_vendor") -> PolicyContext:
    return PolicyContext(
        request=make_request(amount=amount, tool=tool),
        state=InMemoryStateStore(),
        params=PARAMS,
        now=1_700_000_000.0,
    )


def test_allow_well_under_cap(make_request):
    result = per_call_amount_cap.evaluate(_ctx(make_request, amount=1_000_000))
    assert result.decision == "allow"
    assert result.policy_id == "per_call_amount_cap"


def test_trip_well_over_cap(make_request):
    result = per_call_amount_cap.evaluate(_ctx(make_request, amount=20_000_000))
    assert result.decision == "deny"
    assert "cap" in result.reason


def test_boundary_exactly_at_cap_allows(make_request):
    result = per_call_amount_cap.evaluate(_ctx(make_request, amount=CAP_PAISE))
    assert result.decision == "allow"


def test_boundary_one_paisa_over_cap_denies(make_request):
    result = per_call_amount_cap.evaluate(_ctx(make_request, amount=CAP_PAISE + 1))
    assert result.decision == "deny"


def test_per_tool_override_is_used(make_request):
    # 180000 INR would be denied under the default cap but allowed under
    # create_payment_link's higher per-tool override.
    ctx = _ctx(make_request, amount=18_000_000, tool="create_payment_link")
    result = per_call_amount_cap.evaluate(ctx)
    assert result.decision == "allow"
