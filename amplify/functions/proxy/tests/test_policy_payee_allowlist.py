"""payee_allowlist: known payees pass; unknown/missing payees escalate (never deny)."""

from proxy.policies_impl import payee_allowlist
from proxy.policy_types import PolicyContext
from proxy.state import InMemoryStateStore

PARAMS = {"known_payees": ["cust_ravi@oksbi", "vendor_acme@hdfcbank"]}


def _ctx(make_request, payee):
    return PolicyContext(
        request=make_request(payee=payee),
        state=InMemoryStateStore(),
        params=PARAMS,
        now=1_700_000_000.0,
    )


def test_allow_known_payee(make_request):
    result = payee_allowlist.evaluate(_ctx(make_request, "vendor_acme@hdfcbank"))
    assert result.decision == "allow"


def test_escalate_unknown_payee(make_request):
    result = payee_allowlist.evaluate(_ctx(make_request, "unknown_vendor@somebank"))
    assert result.decision == "escalate"
    assert result.decision != "deny"  # unknown payee must never be a hard deny


def test_escalate_missing_payee(make_request):
    result = payee_allowlist.evaluate(_ctx(make_request, None))
    assert result.decision == "escalate"


def test_allowlist_match_is_case_insensitive(make_request):
    result = payee_allowlist.evaluate(_ctx(make_request, "VENDOR_ACME@HDFCBANK"))
    assert result.decision == "allow"
