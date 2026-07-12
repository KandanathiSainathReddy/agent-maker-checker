"""The human-in-the-loop approval queue.

``amc-approvals`` (infra/CONTRACTS.md §4) is a frozen table schema, but the
queue surface in front of it is not one of the frozen protocols in §3 (only
``StateStore`` and ``AuditLog`` are) — this module defines its own
``ApprovalQueue`` ``Protocol`` locally, mirroring the same
one-interface-two-backends shape those use:

- ``InMemoryApprovalQueue`` — single-process, lock-protected. Used locally
  (``APPROVALS_BACKEND=memory``, the docker-compose default: one process,
  no AWS) and by most unit tests.
- ``DynamoApprovalQueue`` — shared, concurrent-safe, backed by
  ``amc-approvals``. Used in the Lambda deployment
  (``APPROVALS_BACKEND=dynamodb``): an approval created by the ephemeral
  Lambda invocation that handled the original ``/tool-call`` must still be
  readable, listable, and resolvable by whichever *different* ephemeral
  invocation later serves the human's approve/deny click — an in-memory
  dict does not survive that handoff. See ``DynamoApprovalQueue``'s
  docstring for why this matters concretely.

Two kinds of approval:

- ``"tool_call"`` — an escalated ``/tool-call`` is held; approving it
  executes the original call via the upstream executor.
- ``"unfreeze"`` — ``velocity_aggregation`` tripped and froze an
  ``(agent_id, tool)`` pair; approving it just calls ``StateStore.unfreeze``,
  there is nothing to execute.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol

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


class ApprovalQueue(Protocol):
    """Shared surface both backends implement. See module docstring."""

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
    ) -> ApprovalRecord: ...

    def get(self, approval_id: str) -> ApprovalRecord | None: ...

    def list(self, *, status: ApprovalStatus | None = None) -> list[ApprovalRecord]: ...

    def resolve(
        self, approval_id: str, status: Literal["approved", "denied"], now: float | None = None
    ) -> ApprovalRecord | None:
        """Resolve a pending approval; return None if it was not pending
        (already resolved, or never existed) rather than raising — callers
        (``app.py``) distinguish "not found" (404) from "already resolved"
        (409) via a prior ``get()``, so this is purely a "did my write win"
        signal, not the source of truth for the conflict.
        """
        ...

    def counts(self) -> tuple[int, int]:
        """Return (pending, resolved)."""
        ...


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


def _dec(value: float) -> Decimal:
    """DynamoDB's boto3 resource API requires Decimal, never float, for numbers."""
    return Decimal(str(value))


class DynamoApprovalQueue:
    """ApprovalQueue backed by a single DynamoDB table (``amc-approvals``, CONTRACTS §4).

    WHY this exists: a human's "approve" or "deny" on an escalated call is a
    decision, not something that can be recomputed if it is lost. Picture an
    escalated Rs 1,60,000 ``unfreeze`` ticket: ``velocity_aggregation``
    tripped, froze an (agent, tool) pair, and opened a ticket for a human to
    review. Under ``APPROVALS_BACKEND=dynamodb`` (the Lambda deployment,
    infra/CONTRACTS.md §2) the Lambda invocation that created that ticket
    has already frozen — Lambda gives no guarantee the *same* process
    memory is warm when the reviewer clicks "approve" a minute later; a
    concurrent or later invocation just as plausibly serves that request.
    An ``InMemoryApprovalQueue`` would simply not have the ticket: the
    Rs 1,60,000 decision evaporates along with the process that recorded
    it. Human approvals are the control plane's memory across those
    ephemeral invocations, and this class is what makes that memory durable
    and shared instead of process-local.

    Item shape, one row per approval, PK ``approval_id`` (S): ``status``
    (``pending``/``approved``/``denied``), ``kind``, ``agent_id``, ``tool``,
    ``arguments`` (JSON string — keeps the item a flat, auditable string per
    field rather than a native nested DynamoDB map), ``amount_paise`` (N,
    integer paise — see repo-wide money convention), ``reason``,
    ``request_id``, ``created_at`` (N), ``resolved_at`` (N, absent until
    resolved).

    Atomicity: ``resolve()`` is one conditional ``UpdateItem`` guarded by
    ``ConditionExpression="#status = :pending"``. Of two resolutions of the
    same approval — concurrent, or sequential double-clicks — at most one
    ``UpdateItem`` can satisfy that condition; the loser's
    ``ConditionalCheckFailedException`` is caught here and turned into a
    ``None`` return, the exact same "nothing to do, it wasn't pending"
    signal ``InMemoryApprovalQueue.resolve()`` gives when it finds
    ``record.status != "pending"`` under its lock. That is what lets
    ``app.py``'s 409-on-double-resolve path work identically on both
    backends without ``app.py`` knowing which backend it is talking to.

    ``list()``/``counts()`` use ``Scan`` + ``FilterExpression`` — there is
    no GSI on ``status`` on this table. That is a deliberate simplicity
    choice at demo/reviewer-clicking scale (a handful to a few hundred open
    approvals); a deployment with a large standing approvals backlog would
    want a GSI keyed on ``status`` instead of scanning the whole table.
    """

    def __init__(
        self,
        table_name: str,
        *,
        endpoint_url: str | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        import boto3

        kwargs: dict[str, Any] = {"region_name": region_name}
        if endpoint_url:
            # DynamoDB Local ignores credentials but boto3 still requires
            # *something* present to sign requests with.
            kwargs["endpoint_url"] = endpoint_url
            kwargs.setdefault("aws_access_key_id", "local")
            kwargs.setdefault("aws_secret_access_key", "local")
        self._resource = boto3.resource("dynamodb", **kwargs)
        self._table = self._resource.Table(table_name)

    @staticmethod
    def _to_item(record: ApprovalRecord) -> dict[str, Any]:
        item: dict[str, Any] = {
            "approval_id": record.approval_id,
            "kind": record.kind,
            "status": record.status,
            "agent_id": record.agent_id,
            "tool": record.tool,
            "arguments": json.dumps(record.arguments),
            "amount_paise": record.amount_paise,
            "reason": record.reason,
            "created_at": _dec(record.created_at),
        }
        if record.request_id is not None:
            item["request_id"] = record.request_id
        if record.resolved_at is not None:
            item["resolved_at"] = _dec(record.resolved_at)
        return item

    @staticmethod
    def _from_item(item: dict[str, Any]) -> ApprovalRecord:
        return ApprovalRecord(
            approval_id=item["approval_id"],
            kind=item["kind"],
            status=item["status"],
            agent_id=item["agent_id"],
            tool=item["tool"],
            arguments=json.loads(item["arguments"]),
            amount_paise=int(item["amount_paise"]),
            reason=item["reason"],
            created_at=float(item["created_at"]),
            request_id=item.get("request_id"),
            resolved_at=float(item["resolved_at"]) if "resolved_at" in item else None,
        )

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
        self._table.put_item(Item=self._to_item(record))
        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        resp = self._table.get_item(Key={"approval_id": approval_id}, ConsistentRead=True)
        item = resp.get("Item")
        return None if item is None else self._from_item(item)

    def list(self, *, status: ApprovalStatus | None = None) -> list[ApprovalRecord]:
        # Scan + FilterExpression: no GSI on `status` on this table — fine
        # at demo scale, see class docstring; not how a high-volume
        # deployment would want this done.
        from boto3.dynamodb.conditions import Attr

        scan_kwargs: dict[str, Any] = {"ConsistentRead": True}
        if status is not None:
            scan_kwargs["FilterExpression"] = Attr("status").eq(status)

        items: list[dict[str, Any]] = []
        resp = self._table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = self._table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **scan_kwargs)
            items.extend(resp.get("Items", []))

        records = [self._from_item(item) for item in items]
        return sorted(records, key=lambda r: r.created_at)

    def resolve(
        self, approval_id: str, status: Literal["approved", "denied"], now: float | None = None
    ) -> ApprovalRecord | None:
        from botocore.exceptions import ClientError

        resolved_at = time.time() if now is None else now
        try:
            resp = self._table.update_item(
                Key={"approval_id": approval_id},
                UpdateExpression="SET #status = :new_status, resolved_at = :resolved_at",
                ConditionExpression="#status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":new_status": status,
                    ":resolved_at": _dec(resolved_at),
                    ":pending": "pending",
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Item missing, or status is no longer "pending": someone else
            # (or an earlier call) already resolved it. Same "not pending,
            # nothing to do" signal InMemoryApprovalQueue.resolve() gives.
            return None
        return self._from_item(resp["Attributes"])

    def counts(self) -> tuple[int, int]:
        records = self.list()
        pending = sum(1 for r in records if r.status == "pending")
        return pending, len(records) - pending
