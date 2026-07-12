"""Selects the live-Razorpay-MCP or offline-cached upstream by DEMO_MODE.

infra/CONTRACTS.md #5: `proxy/app.py` imports `get_upstream` under a
try/except ImportError and falls back to the fake upstream if this package
(or `.base`, owned by Agent A) isn't available yet -- so this module is
allowed to fail loudly on import if `.base` is missing; that's the
documented degrade path, not a bug here.
"""

from __future__ import annotations

import os

from .base import UpstreamExecutor


def get_upstream() -> UpstreamExecutor:
    """Return the UpstreamExecutor selected by the DEMO_MODE env var.

    DEMO_MODE=live   -> MCPUpstream, fronting the self-hosted Razorpay MCP
                         server over stdio (needs RAZORPAY_KEY_ID/SECRET;
                         see proxy/upstream/mcp_client.py).
    DEMO_MODE=cached -> CachedUpstream (default), deterministic offline
                         replay -- also what an unrecognized DEMO_MODE value
                         falls back to, so a typo degrades to the safe,
                         keyless demo path instead of crashing.
    """
    mode = (os.environ.get("DEMO_MODE") or "cached").strip().lower()
    if mode == "live":
        from .mcp_client import MCPUpstream

        return MCPUpstream()

    from .cached import CachedUpstream

    return CachedUpstream()
