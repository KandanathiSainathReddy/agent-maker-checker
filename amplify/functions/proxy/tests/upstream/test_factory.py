"""factory.get_upstream() selects the right executor by DEMO_MODE.

Offline only: constructing MCPUpstream never connects (lazy connect), so
this never touches Docker/network even when DEMO_MODE=live.
"""

import os

import pytest

from proxy.upstream.cached import CachedUpstream
from proxy.upstream.factory import get_upstream
from proxy.upstream.mcp_client import MCPUpstream


@pytest.fixture(autouse=True)
def _clean_demo_mode():
    original = os.environ.get("DEMO_MODE")
    yield
    if original is None:
        os.environ.pop("DEMO_MODE", None)
    else:
        os.environ["DEMO_MODE"] = original


def test_default_is_cached_when_unset():
    os.environ.pop("DEMO_MODE", None)
    assert isinstance(get_upstream(), CachedUpstream)


@pytest.mark.parametrize("value", ["cached", "CACHED", " Cached ", "bogus", ""])
def test_non_live_values_fall_back_to_cached(value):
    os.environ["DEMO_MODE"] = value
    assert isinstance(get_upstream(), CachedUpstream)


@pytest.mark.parametrize("value", ["live", "LIVE", " Live "])
def test_live_selects_mcp_upstream_without_connecting(value):
    os.environ["DEMO_MODE"] = value
    upstream = get_upstream()
    assert isinstance(upstream, MCPUpstream)
    # lazy connect: constructing it must not have spawned anything
    assert upstream._session is None
