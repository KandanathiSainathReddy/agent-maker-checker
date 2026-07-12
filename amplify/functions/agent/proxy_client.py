"""Thin HTTP client for the enforcement proxy's ``POST /tool-call``
(infra/CONTRACTS.md §1). Kept separate from ``worker.py`` so the tool loop
can be tested offline against a fake/injected call, and so the real client
has exactly one job: build the frozen request envelope and return the
frozen response envelope verbatim.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_PROXY_URL = "http://localhost:8000"


def proxy_url() -> str:
    """``PROXY_URL`` env var, defaulting to the local docker-compose address."""
    return os.environ.get("PROXY_URL", DEFAULT_PROXY_URL)


def call_tool(
    *,
    agent_id: str,
    tool: str,
    arguments: dict[str, Any],
    context: dict[str, Any],
    labeled_legit: bool = False,
    base_url: str | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """POST one tool call to the proxy and return its decision envelope.

    ``context`` is the already-built ``{"payee": ..., "provenance": [...]}``
    dict (see ``proxy.models.ToolCallContext``) — the caller (``worker.py``)
    owns provenance scanning; this function only transports it.
    """
    url = f"{(base_url or proxy_url()).rstrip('/')}/tool-call"
    payload = {
        "agent_id": agent_id,
        "tool": tool,
        "arguments": arguments,
        "context": context,
        "meta": {"labeled_legit": labeled_legit},
    }
    response = httpx.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()
