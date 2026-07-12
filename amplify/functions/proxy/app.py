"""THE PRODUCT: the FastAPI enforcement proxy. infra/CONTRACTS.md §1 is the public
surface; do not change a route shape without updating that file first.

``create_app(...)`` is a factory rather than a bare module-level ``app`` so
tests can build a fully isolated instance per test (its own
``InMemoryStateStore``, its own audit log file, its own clock) without one
test's state leaking into the next. ``app = create_app()`` below is the
default instance uvicorn/Docker serves, wired from environment via
``proxy.config``.

Request flow for ``POST /tool-call``: evaluate against the policy engine,
then act on the decision —
``allow`` executes immediately via the injected upstream executor,
``deny`` blocks (and, if the deny came from a velocity-threshold trip, opens
an "unfreeze" HITL ticket),
``escalate`` holds the call in the approvals queue for a human.
Every outcome gets one audit entry; an escalated call that is later approved
gets two more (``approved``, then ``executed``) so the full lifecycle is
visible in the hash chain.
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict

from fastapi import FastAPI, HTTPException

from proxy import config
from proxy.approvals import ApprovalRecord, InMemoryApprovalQueue
from proxy.audit import AuditLog
from proxy.engine import PolicyEngine
from proxy.metrics import MetricsAccumulator
from proxy.models import (
    ApprovalOut,
    AuditVerifyResponse,
    DecisionRecord,
    MetricsResponse,
    ToolCallRequest,
    ToolCallResponse,
)
from proxy.state import StateStore
from proxy.upstream.base import UpstreamExecutor

try:
    from proxy.upstream.factory import get_upstream  # Agent B, lands in Phase 2
except ImportError:
    from proxy.upstream.fake import FakeUpstreamExecutor

    def get_upstream() -> UpstreamExecutor:
        return FakeUpstreamExecutor()


_FEED_SIZE = 5000


def _approval_out(record: ApprovalRecord) -> ApprovalOut:
    return ApprovalOut(
        approval_id=record.approval_id,
        kind=record.kind,
        status=record.status,
        agent_id=record.agent_id,
        tool=record.tool,
        arguments=record.arguments,
        amount_paise=record.amount_paise,
        reason=record.reason,
        created_at=record.created_at,
        resolved_at=record.resolved_at,
        request_id=record.request_id,
    )


def create_app(
    *,
    state_store: StateStore | None = None,
    audit_log: AuditLog | None = None,
    engine: PolicyEngine | None = None,
    upstream: UpstreamExecutor | None = None,
    approvals: InMemoryApprovalQueue | None = None,
    metrics: MetricsAccumulator | None = None,
    policy_dir: str | None = None,
    now_fn: Callable[[], float] = time.time,
) -> FastAPI:
    state_store = state_store if state_store is not None else config.get_state_store()
    audit_log = audit_log if audit_log is not None else config.get_audit_log()
    engine = engine if engine is not None else PolicyEngine(policy_dir or config.policy_dir())
    upstream = upstream if upstream is not None else get_upstream()
    approvals = approvals if approvals is not None else InMemoryApprovalQueue()
    metrics = metrics if metrics is not None else MetricsAccumulator()
    decisions_feed: deque[DecisionRecord] = deque(maxlen=_FEED_SIZE)

    app = FastAPI(title="agent-maker-checker enforcement proxy")

    def _record_feed(
        *, request_id: str, now: float, req: ToolCallRequest, amount_paise: int,
        decision: str, policy_id: str | None, reason: str, status: str, evaluated_in_ms: float,
    ) -> None:
        decisions_feed.appendleft(
            DecisionRecord(
                request_id=request_id, ts=now, agent_id=req.agent_id, tool=req.tool,
                amount_paise=amount_paise, decision=decision, policy_id=policy_id,
                reason=reason, status=status, evaluated_in_ms=evaluated_in_ms,
            )
        )

    @app.post("/tool-call", response_model=ToolCallResponse)
    async def tool_call(payload: ToolCallRequest) -> ToolCallResponse:
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        now = now_fn()
        amount_paise = int(payload.arguments.get("amount", 0))

        result = engine.evaluate(payload, state_store, now)

        if result.decision == "allow":
            status = "executed"
            upstream_result = asdict(await upstream.execute(payload.tool, payload.arguments))
        else:
            status = "pending_approval" if result.decision == "escalate" else "blocked"
            upstream_result = None

        audit_log.append(
            request_id=request_id, agent_id=payload.agent_id, tool=payload.tool,
            arguments=payload.arguments, decision=result.decision, policy_id=result.policy_id,
            reason=result.reason, now=now,
        )
        metrics.record_decision(
            decision=result.decision, amount_paise=amount_paise,
            evaluated_in_ms=result.evaluated_in_ms, trace=result.trace,
            labeled_legit=payload.meta.labeled_legit, executed=(result.decision == "allow"),
        )
        _record_feed(
            request_id=request_id, now=now, req=payload, amount_paise=amount_paise,
            decision=result.decision, policy_id=result.policy_id, reason=result.reason,
            status=status, evaluated_in_ms=result.evaluated_in_ms,
        )

        if result.decision == "escalate":
            approvals.create(
                kind="tool_call", agent_id=payload.agent_id, tool=payload.tool,
                arguments=payload.arguments, amount_paise=amount_paise, reason=result.reason,
                request_id=request_id, now=now,
            )
        elif result.decision == "deny" and result.escalate_unfreeze:
            approvals.create(
                kind="unfreeze", agent_id=payload.agent_id, tool=payload.tool,
                arguments=payload.arguments, amount_paise=amount_paise, reason=result.reason,
                request_id=request_id, now=now,
            )

        return ToolCallResponse(
            request_id=request_id, decision=result.decision, policy_id=result.policy_id,
            reason=result.reason, evaluated_in_ms=result.evaluated_in_ms, status=status,
            upstream_result=upstream_result,
        )

    @app.get("/decisions", response_model=list[DecisionRecord])
    async def get_decisions(limit: int = 100) -> list[DecisionRecord]:
        return list(decisions_feed)[:limit]

    @app.get("/approvals", response_model=list[ApprovalOut])
    async def list_approvals(status: str | None = None) -> list[ApprovalOut]:
        records = approvals.list(status=status)  # type: ignore[arg-type]
        return [_approval_out(r) for r in records]

    @app.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str) -> ToolCallResponse | dict:
        record = approvals.get(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="approval not found")
        if record.status != "pending":
            raise HTTPException(status_code=409, detail=f"approval already {record.status}")

        now = now_fn()
        approvals.resolve(approval_id, "approved", now=now)
        audit_log.append(
            request_id=record.request_id or approval_id, agent_id=record.agent_id,
            tool=record.tool, arguments=record.arguments, decision="approved", policy_id=None,
            reason=f"human approved {record.kind} approval {approval_id}: {record.reason}", now=now,
        )

        if record.kind == "unfreeze":
            state_store.unfreeze(record.agent_id, record.tool)
            audit_log.append(
                request_id=record.request_id or approval_id, agent_id=record.agent_id,
                tool=record.tool, arguments=record.arguments, decision="unfrozen", policy_id=None,
                reason=f"{record.agent_id}/{record.tool} unfrozen by human approval", now=now,
            )
            return {"approval_id": approval_id, "kind": "unfreeze", "status": "approved"}

        upstream_result = asdict(await upstream.execute(record.tool, record.arguments))
        audit_log.append(
            request_id=record.request_id or approval_id, agent_id=record.agent_id,
            tool=record.tool, arguments=record.arguments, decision="executed", policy_id=None,
            reason="executed after human approval", now=now,
        )
        metrics.record_execution(record.amount_paise)
        return ToolCallResponse(
            request_id=record.request_id or approval_id, decision="allow", policy_id=None,
            reason="approved by human and executed", evaluated_in_ms=0.0, status="executed",
            upstream_result=upstream_result,
        )

    @app.post("/approvals/{approval_id}/deny")
    async def deny(approval_id: str) -> dict:
        record = approvals.get(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail="approval not found")
        if record.status != "pending":
            raise HTTPException(status_code=409, detail=f"approval already {record.status}")

        now = now_fn()
        approvals.resolve(approval_id, "denied", now=now)
        audit_log.append(
            request_id=record.request_id or approval_id, agent_id=record.agent_id,
            tool=record.tool, arguments=record.arguments, decision="denied", policy_id=None,
            reason=f"human denied {record.kind} approval {approval_id}: {record.reason}", now=now,
        )
        return {"approval_id": approval_id, "kind": record.kind, "status": "denied"}

    @app.get("/metrics", response_model=MetricsResponse)
    async def get_metrics() -> MetricsResponse:
        pending, resolved = approvals.counts()
        return MetricsResponse(
            **metrics.snapshot(approvals_pending=pending, approvals_resolved=resolved)
        )

    @app.get("/audit/verify", response_model=AuditVerifyResponse)
    async def audit_verify() -> AuditVerifyResponse:
        ok, error = audit_log.verify_chain()
        return AuditVerifyResponse(ok=ok, entries_checked=len(audit_log.entries()), error=error)

    @app.post("/admin/unfreeze/{agent_id}/{tool}")
    async def admin_unfreeze(agent_id: str, tool: str) -> dict:
        now = now_fn()
        state_store.unfreeze(agent_id, tool)
        audit_log.append(
            request_id=f"admin_{uuid.uuid4().hex[:12]}", agent_id=agent_id, tool=tool,
            arguments={}, decision="unfrozen", policy_id=None,
            reason=f"manual admin unfreeze of {agent_id}/{tool}", now=now,
        )
        return {"agent_id": agent_id, "tool": tool, "frozen": state_store.is_frozen(agent_id, tool)}

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True, "demo_mode": config.demo_mode()}

    return app


_default_app: FastAPI | None = None


def __getattr__(name: str):  # PEP 562 lazy module attribute
    """Build the default, env-wired app instance only when actually accessed
    (and only once — cached like a normal module-level variable would be).

    ``uvicorn``/the Dockerfile need a ready ``app`` object at ``proxy.app:app``,
    but every test in this repo imports this module only for ``create_app``
    and supplies its own isolated backends. Constructing a default instance
    eagerly at import time would touch ``POLICY_DIR``/``AUDIT_LOG_PATH`` on
    disk (relative to whatever the current working directory happens to be)
    as a side effect of every such import. Deferring it to first access of
    ``proxy.app.app`` keeps `import proxy.app` (or `from proxy.app import
    create_app`) side-effect-free.
    """
    global _default_app
    if name == "app":
        if _default_app is None:
            _default_app = create_app()
        return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
