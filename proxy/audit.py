"""Tamper-evident audit log: sha256 hash chain, two backends, per CONTRACTS §4.

Every decision the engine makes — and every later step of an escalated call's
lifecycle (escalated -> approved/denied -> executed) — is appended as one
entry. Each entry embeds the sha256 hash of the previous entry, so altering
or removing any past entry breaks ``verify_chain()`` for everything after it.

Only a hash of ``arguments`` is stored (``arguments_hash``), not the raw
arguments — the chain proves *that* a specific argument set produced a
specific decision without the audit log itself becoming a second place
payment details are stored at rest.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    ts: float
    request_id: str
    agent_id: str
    tool: str
    arguments_hash: str
    decision: str
    policy_id: str | None
    reason: str
    prev_hash: str
    hash: str


def hash_arguments(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _entry_hash(prev_hash: str, seq: int, ts: float, request_id: str, agent_id: str,
                 tool: str, arguments_hash: str, decision: str, policy_id: str | None,
                 reason: str) -> str:
    payload = {
        "seq": seq,
        "ts": ts,
        "request_id": request_id,
        "agent_id": agent_id,
        "tool": tool,
        "arguments_hash": arguments_hash,
        "decision": decision,
        "policy_id": policy_id,
        "reason": reason,
        "prev_hash": prev_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verify_entries(entries: list[AuditEntry]) -> tuple[bool, str | None]:
    prev_hash = GENESIS_HASH
    for entry in entries:
        expected = _entry_hash(
            prev_hash, entry.seq, entry.ts, entry.request_id, entry.agent_id, entry.tool,
            entry.arguments_hash, entry.decision, entry.policy_id, entry.reason,
        )
        if entry.prev_hash != prev_hash:
            return False, f"seq {entry.seq}: prev_hash mismatch (broken chain link)"
        if entry.hash != expected:
            return False, f"seq {entry.seq}: hash mismatch (entry was tampered with)"
        prev_hash = entry.hash
    return True, None


class AuditLog(Protocol):
    def append(
        self,
        *,
        request_id: str,
        agent_id: str,
        tool: str,
        arguments: dict[str, Any],
        decision: str,
        policy_id: str | None,
        reason: str,
        now: float | None = None,
    ) -> AuditEntry: ...

    def entries(self, limit: int | None = None) -> list[AuditEntry]: ...

    def verify_chain(self) -> tuple[bool, str | None]:
        """Return (ok, error_detail). error_detail is None iff ok is True."""
        ...


class JsonlAuditLog:
    """Append-only JSONL file, hash-chained. Default local backend (AUDIT_BACKEND=jsonl)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq, self._prev_hash = self._read_tail()

    def _read_tail(self) -> tuple[int, str]:
        if not self._path.exists():
            return 0, GENESIS_HASH
        last_seq, last_hash = 0, GENESIS_HASH
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                last_seq, last_hash = row["seq"], row["hash"]
        return last_seq, last_hash

    def append(
        self,
        *,
        request_id: str,
        agent_id: str,
        tool: str,
        arguments: dict[str, Any],
        decision: str,
        policy_id: str | None,
        reason: str,
        now: float | None = None,
    ) -> AuditEntry:
        ts = time.time() if now is None else now
        arguments_hash = hash_arguments(arguments)
        with self._lock:
            seq = self._seq + 1
            entry_hash = _entry_hash(
                self._prev_hash, seq, ts, request_id, agent_id, tool, arguments_hash,
                decision, policy_id, reason,
            )
            entry = AuditEntry(
                seq=seq, ts=ts, request_id=request_id, agent_id=agent_id, tool=tool,
                arguments_hash=arguments_hash, decision=decision, policy_id=policy_id,
                reason=reason, prev_hash=self._prev_hash, hash=entry_hash,
            )
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(entry), sort_keys=True))
                fh.write("\n")
            self._seq, self._prev_hash = seq, entry_hash
            return entry

    def entries(self, limit: int | None = None) -> list[AuditEntry]:
        with self._lock:
            if not self._path.exists():
                return []
            rows = []
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(AuditEntry(**json.loads(line)))
        return rows[-limit:] if limit else rows

    def verify_chain(self) -> tuple[bool, str | None]:
        return _verify_entries(self.entries())


class DynamoAuditLog:
    """Hash chain stored in ``amc-audit``: PK ``chain`` (constant "main"), SK ``seq``.

    Append is a conditional put on ``attribute_not_exists(seq)`` — the chain
    is intentionally serialized (tamper-evidence needs strict ordering), so a
    clash means another writer beat us to this seq; re-read the current tail
    and retry.
    """

    CHAIN = "main"

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
            kwargs["endpoint_url"] = endpoint_url
            kwargs.setdefault("aws_access_key_id", "local")
            kwargs.setdefault("aws_secret_access_key", "local")
        self._resource = boto3.resource("dynamodb", **kwargs)
        self._table = self._resource.Table(table_name)

    def _tail(self) -> tuple[int, str]:
        resp = self._table.query(
            KeyConditionExpression="#c = :c",
            ExpressionAttributeNames={"#c": "chain"},
            ExpressionAttributeValues={":c": self.CHAIN},
            ScanIndexForward=False,
            Limit=1,
        )
        items = resp.get("Items", [])
        if not items:
            return 0, GENESIS_HASH
        return int(items[0]["seq"]), items[0]["hash"]

    def append(
        self,
        *,
        request_id: str,
        agent_id: str,
        tool: str,
        arguments: dict[str, Any],
        decision: str,
        policy_id: str | None,
        reason: str,
        now: float | None = None,
    ) -> AuditEntry:
        from botocore.exceptions import ClientError

        ts = time.time() if now is None else now
        arguments_hash = hash_arguments(arguments)

        for _ in range(50):
            seq, prev_hash = self._tail()
            seq += 1
            entry_hash = _entry_hash(
                prev_hash, seq, ts, request_id, agent_id, tool, arguments_hash,
                decision, policy_id, reason,
            )
            entry = AuditEntry(
                seq=seq, ts=ts, request_id=request_id, agent_id=agent_id, tool=tool,
                arguments_hash=arguments_hash, decision=decision, policy_id=policy_id,
                reason=reason, prev_hash=prev_hash, hash=entry_hash,
            )
            item = {**asdict(entry), "ts": Decimal(str(entry.ts)), "chain": self.CHAIN}
            try:
                self._table.put_item(
                    Item=item,
                    ConditionExpression="attribute_not_exists(seq)",
                )
                return entry
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                continue  # someone else took this seq — re-read tail and retry

        raise RuntimeError("DynamoAuditLog.append: could not win the seq race after 50 retries")

    def entries(self, limit: int | None = None) -> list[AuditEntry]:
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": "#c = :c",
            "ExpressionAttributeNames": {"#c": "chain"},
            "ExpressionAttributeValues": {":c": self.CHAIN},
            "ScanIndexForward": True,
        }
        rows: list[dict[str, Any]] = []
        while True:
            resp = self._table.query(**kwargs)
            rows.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        entries = [
            AuditEntry(
                seq=int(r["seq"]), ts=float(r["ts"]), request_id=r["request_id"],
                agent_id=r["agent_id"], tool=r["tool"], arguments_hash=r["arguments_hash"],
                decision=r["decision"], policy_id=r.get("policy_id"), reason=r["reason"],
                prev_hash=r["prev_hash"], hash=r["hash"],
            )
            for r in rows
        ]
        return entries[-limit:] if limit else entries

    def verify_chain(self) -> tuple[bool, str | None]:
        return _verify_entries(self.entries())
