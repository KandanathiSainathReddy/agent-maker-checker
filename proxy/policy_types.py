"""Shared types between proxy/engine.py and proxy/policies_impl/*.

Split out from engine.py purely to avoid a circular import: the engine
imports every policy module to build its registry, and every policy module
needs these types for its function signature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from proxy.models import ToolCallRequest
    from proxy.state import StateStore


@dataclass(frozen=True)
class PolicyContext:
    """Everything one policy function needs. ``params`` is already paise-converted."""

    request: ToolCallRequest
    state: StateStore
    params: dict[str, Any]
    now: float


@dataclass(frozen=True)
class PolicyEvaluation:
    """What one policy function returns for one request."""

    policy_id: str
    decision: str  # "allow" | "deny" | "escalate"
    reason: str
    # True only for velocity_aggregation's threshold-cross deny: tells the app
    # layer to open a HITL "unfreeze" approval in addition to blocking this call.
    escalate_unfreeze: bool = False


@dataclass(frozen=True)
class Decision:
    """What PolicyEngine.evaluate() returns — the shape of infra/CONTRACTS.md §1's
    ``{decision, policy_id, reason, evaluated_in_ms}``, plus a full evaluation trace
    used for per-policy trip counts in GET /metrics.
    """

    decision: str  # "allow" | "deny" | "escalate"
    policy_id: str | None
    reason: str
    evaluated_in_ms: float
    trace: tuple[PolicyEvaluation, ...] = field(default_factory=tuple)
    escalate_unfreeze: bool = False
