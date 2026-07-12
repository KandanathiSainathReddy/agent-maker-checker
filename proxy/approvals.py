"""The human-in-the-loop approval queue.

Not part of infra/CONTRACTS.md §3 (only ``StateStore`` and ``AuditLog`` are
frozen protocols there) — this is an internal Phase 1 design choice: an
in-memory, single-process queue, matching the local docker-compose default
(``STATE_BACKEND=memory`` + ``AUDIT_BACKEND=jsonl``, one process, no AWS).
``amc-approvals`` (infra/CONTRACTS.md §4) exists for the eventual Lambda
deployment where approvals must survive across ephemeral invocations; wiring
a Dynamo-backed queue behind this same shape is future work and is called
out in DECISIONS.md rather than built speculatively here.

Two kinds of approval:

- ``"tool_call"`` — an escalated ``/tool-call`` is held; approving it
  executes the original call via the upstream executor.
- ``"unfreeze"`` — ``velocity_aggregation`` tripped and froze an
  ``(agent_id, tool)`` pair; approving it just calls ``StateStore.unfreeze``,
  there is nothing to execute.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

ApprovalKind = Literal["tool_call", "unfreeze"]
ApprovalStatus = Literal["pending", "approved", "denied"]


@dataclass
class ApprovalRecord:
    approval_id: str
    kind: ApprovalKind
    status: ApprovalStatus
    agent_id: str
    tool: str
    arguments: dict[str, Any]
    amount_paise: int
    reason: str
    created_at: float
    request_id: str | None = None
    resolved_at: float | None = None


class InMemoryApprovalQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, ApprovalRecord] = {}

    def create(
        self,
        *,
        kind: ApprovalKind,
        agent_id: str,
        tool: str,
        arguments: dict[str, Any],
        amount_paise: int,
        reason: str,
        request_id: str | None = None,
        now: float | None = None,
    ) -> ApprovalRecord:
        record = ApprovalRecord(
            approval_id=f"appr_{uuid.uuid4().hex[:16]}",
            kind=kind,
            status="pending",
            agent_id=agent_id,
            tool=tool,
            arguments=arguments,
            amount_paise=amount_paise,
            reason=reason,
            created_at=time.time() if now is None else now,
            request_id=request_id,
        )
        with self._lock:
            self._records[record.approval_id] = record
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            return self._records.get(approval_id)

    def list(self, *, status: ApprovalStatus | None = None) -> list[ApprovalRecord]:
        with self._lock:
            records = list(self._records.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        return sorted(records, key=lambda r: r.created_at)

    def resolve(
        self, approval_id: str, status: Literal["approved", "denied"], now: float | None = None
    ) -> ApprovalRecord | None:
        with self._lock:
            record = self._records.get(approval_id)
            if record is None or record.status != "pending":
                return None
            record.status = status
            record.resolved_at = time.time() if now is None else now
            return record

    def counts(self) -> tuple[int, int]:
        """Return (pending, resolved)."""
        with self._lock:
            records = list(self._records.values())
        pending = sum(1 for r in records if r.status == "pending")
        return pending, len(records) - pending
