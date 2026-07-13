"""Pydantic request/response models for the proxy's public HTTP surface.

Shapes here are frozen by infra/CONTRACTS.md §1 — field names and the
``POST /tool-call`` request/response envelope must not drift from that
document without updating it first.

Money is always integer paise (``arguments["amount"]``, ``amount_paise``, ...).
INR only ever appears in ``policies/*.yaml`` and in dashboard display code.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

DecisionLiteral = Literal["allow", "deny", "escalate"]
CallStatus = Literal["executed", "blocked", "pending_approval"]


class ProvenanceEntry(BaseModel):
    """One hop of where a piece of context came from.

    ``trusted=False`` marks data the agent merely *read* (a support ticket, an
    inbound email) rather than data that originated from the agent's own
    authenticated action. ``tainted_fields`` names the dotted paths into this
    request (e.g. ``"arguments.payee"``) that were derived from that source —
    ``provenance_check`` denies when a payment-bearing field is tainted by an
    untrusted source.
    """

    source: str
    trusted: bool
    tainted_fields: list[str] = Field(default_factory=list)


class ToolCallContext(BaseModel):
    payee: str | None = None
    provenance: list[ProvenanceEntry] = Field(default_factory=list)


class ToolCallMeta(BaseModel):
    """Out-of-band test/demo metadata. Never influences a policy decision.

    ``labeled_legit`` marks a call as known-good traffic for the clean-pass
    precision test — if a labeled-legit call is denied or escalated, that is
    a false block, tracked in ``GET /metrics``.

    ``execute_cached`` forces THIS call's upstream execution through the cached
    (replayed) executor even when the proxy is globally in DEMO_MODE=live. The
    console stress-test sets it so its scenarios run fast and deterministically,
    while the Nova playground's own calls (no flag) still hit the live MCP. The
    policy DECISION is identical either way — this only changes execution.
    """

    labeled_legit: bool = False
    execute_cached: bool = False


class ToolCallRequest(BaseModel):
    agent_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    context: ToolCallContext = Field(default_factory=ToolCallContext)
    meta: ToolCallMeta = Field(default_factory=ToolCallMeta)


class ToolCallResponse(BaseModel):
    request_id: str
    decision: DecisionLiteral
    policy_id: str | None
    reason: str
    evaluated_in_ms: float
    status: CallStatus
    upstream_result: dict[str, Any] | None = None


class ApprovalOut(BaseModel):
    """Public shape of one HITL queue entry."""

    approval_id: str
    kind: Literal["tool_call", "unfreeze"]
    status: Literal["pending", "approved", "denied"]
    agent_id: str
    tool: str
    arguments: dict[str, Any]
    amount_paise: int
    reason: str
    created_at: float
    resolved_at: float | None = None
    request_id: str | None = None


class DecisionRecord(BaseModel):
    """One row of the ``GET /decisions`` feed."""

    request_id: str
    ts: float
    agent_id: str
    tool: str
    amount_paise: int
    decision: DecisionLiteral
    policy_id: str | None
    reason: str
    status: CallStatus
    evaluated_in_ms: float


class MetricsResponse(BaseModel):
    rupees_attempted: float
    rupees_moved: float
    calls_allowed: int
    calls_denied: int
    calls_escalated: int
    false_blocks: int
    approvals_pending: int
    approvals_resolved: int
    p95_overhead_ms: float
    per_policy_trip_counts: dict[str, int]


class AuditVerifyResponse(BaseModel):
    ok: bool
    entries_checked: int
    error: str | None = None
