"""Upstream executor protocol — infra/CONTRACTS.md §5. Identical content whichever
agent creates it first; Agent A and Agent B must not diverge on this file.
"""

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class UpstreamResult:
    ok: bool
    tool: str
    data: dict[str, Any]
    error: str | None = None
    mode: str = "cached"  # "live" | "cached" | "fake"


class UpstreamExecutor(Protocol):
    async def execute(self, tool: str, arguments: dict[str, Any]) -> UpstreamResult: ...
