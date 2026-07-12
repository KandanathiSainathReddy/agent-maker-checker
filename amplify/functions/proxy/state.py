"""Cross-call state: velocity windows, the freeze registry, capture/refund totals.

One interface (``StateStore``), two implementations, per infra/CONTRACTS.md §3:

- ``InMemoryStateStore`` — single-process, lock-protected. Used locally
  (``STATE_BACKEND=memory``, the docker-compose default) and by the unit tests.
- ``DynamoStateStore`` — shared, concurrent-safe. Used in the Lambda deployment
  (``STATE_BACKEND=dynamodb``) and by ``tests/test_velocity_concurrency.py``
  against DynamoDB Local, which is the blocking gate for this phase.

Every mutation is atomic under concurrent callers — this is what lets the
velocity/structuring catch survive concurrent Lambda invocations (or a
reviewer clicking fast). A naive read-modify-write would lose updates under
concurrency and let structuring slip past the threshold undetected.

Windows are *tumbling*, not sliding: a key's window resets to
``[now, now + window_s)`` the first time it is touched after the previous
window has fully elapsed. This is the model the frozen DynamoDB schema
(infra/CONTRACTS.md §4) encodes — one ``window_start`` attribute per item.

``now: float`` (seconds, ``time.time()``-shaped) is injected into every call
so tests never sleep and every backend agrees on "current time".

Deviation from the letter of CONTRACTS.md §3 (documented, additive, not
breaking): the frozen protocol lists ``add_capture`` but not a matching
``add_refund``, even though the ``amc-state`` "cap#{agent}" item in §4 stores
both ``captured_paise`` *and* ``refunded_paise`` on one item. There is no way
to populate ``refunded_paise`` without a write method for it, so this module
adds ``add_refund`` alongside ``add_capture`` with identical shape. See
DECISIONS.md for the full note.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any, Protocol

# DynamoDB items get a generous TTL past their window so demo/table-scan
# cleanup happens automatically without the app ever issuing a delete.
_TTL_GRACE_S = 7 * 24 * 3600


class StateStore(Protocol):
    def record_and_sum(self, key: str, amount_paise: int, now: float, window_s: int) -> int:
        """Atomically add amount_paise to key's rolling window; return the new sum.

        A non-atomic read-modify-write here would lose updates under
        concurrency and let structuring slip through undetected —
        DynamoStateStore MUST use an atomic counter (UpdateItem ADD).
        """
        ...

    def window_sum(self, key: str, now: float, window_s: int) -> int:
        """Read-only: the current window sum for key, or 0 if the window is stale/absent."""
        ...

    def freeze(self, agent_id: str, tool: str, reason: str) -> None:
        """Idempotent: freezing an already-frozen pair is a no-op (first freeze wins)."""
        ...

    def is_frozen(self, agent_id: str, tool: str) -> bool:
        """Strongly consistent: must observe a freeze from the same or an earlier call."""
        ...

    def unfreeze(self, agent_id: str, tool: str) -> None: ...

    def add_capture(self, agent_id: str, amount_paise: int, now: float, window_s: int) -> None: ...

    def add_refund(self, agent_id: str, amount_paise: int, now: float, window_s: int) -> None: ...

    def capture_and_refund_totals(
        self, agent_id: str, now: float, window_s: int
    ) -> tuple[int, int]:
        """Return (captured_paise, refunded_paise) for the window, (0, 0) if stale/absent."""
        ...


class InMemoryStateStore:
    """Single-process StateStore behind one lock.

    A single global lock (rather than per-key locks) is a deliberate
    simplicity choice: this backend only ever runs inside one docker-compose
    process or one pytest process, both far below the scale where per-key
    lock striping would matter, and a single lock makes the "no lost updates"
    correctness argument trivial to read.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._velocity: dict[str, dict[str, int]] = {}
        self._freeze: dict[str, dict[str, Any]] = {}
        self._capture: dict[str, dict[str, int]] = {}

    def record_and_sum(self, key: str, amount_paise: int, now: float, window_s: int) -> int:
        with self._lock:
            item = self._velocity.get(key)
            if item is None or now - item["window_start"] >= window_s:
                item = {"sum_paise": 0, "count": 0, "window_start": now}
            item["sum_paise"] += amount_paise
            item["count"] += 1
            self._velocity[key] = item
            return item["sum_paise"]

    def window_sum(self, key: str, now: float, window_s: int) -> int:
        with self._lock:
            item = self._velocity.get(key)
            if item is None or now - item["window_start"] >= window_s:
                return 0
            return item["sum_paise"]

    def freeze(self, agent_id: str, tool: str, reason: str) -> None:
        with self._lock:
            fkey = f"{agent_id}#{tool}"
            existing = self._freeze.get(fkey)
            if existing is not None and existing.get("frozen"):
                return  # idempotent: first freeze wins
            self._freeze[fkey] = {"frozen": True, "reason": reason, "frozen_at": time.time()}

    def is_frozen(self, agent_id: str, tool: str) -> bool:
        with self._lock:
            item = self._freeze.get(f"{agent_id}#{tool}")
            return bool(item and item.get("frozen"))

    def unfreeze(self, agent_id: str, tool: str) -> None:
        with self._lock:
            fkey = f"{agent_id}#{tool}"
            if fkey in self._freeze:
                self._freeze[fkey]["frozen"] = False

    def _bump_capture_item(
        self, field: str, agent_id: str, amount_paise: int, now: float, window_s: int
    ) -> None:
        other = "refunded_paise" if field == "captured_paise" else "captured_paise"
        with self._lock:
            item = self._capture.get(agent_id)
            if item is None or now - item["window_start"] >= window_s:
                item = {"captured_paise": 0, "refunded_paise": 0, "window_start": now}
            item[field] += amount_paise
            item.setdefault(other, 0)
            self._capture[agent_id] = item

    def add_capture(self, agent_id: str, amount_paise: int, now: float, window_s: int) -> None:
        self._bump_capture_item("captured_paise", agent_id, amount_paise, now, window_s)

    def add_refund(self, agent_id: str, amount_paise: int, now: float, window_s: int) -> None:
        self._bump_capture_item("refunded_paise", agent_id, amount_paise, now, window_s)

    def capture_and_refund_totals(
        self, agent_id: str, now: float, window_s: int
    ) -> tuple[int, int]:
        with self._lock:
            item = self._capture.get(agent_id)
            if item is None or now - item["window_start"] >= window_s:
                return (0, 0)
            return (item["captured_paise"], item["refunded_paise"])


def _dec(value: float) -> Decimal:
    """DynamoDB's boto3 resource API requires Decimal, never float, for numbers."""
    return Decimal(str(value))


class DynamoStateStore:
    """StateStore backed by a single DynamoDB table (``amc-state``, see CONTRACTS §4).

    Atomicity strategy for the tumbling-window counters (``record_and_sum``,
    ``add_capture``/``add_refund``): the window index is baked INTO the item key
    (``vel#{key}#{bucket}`` where ``bucket = floor(now / window_s)``), so every
    caller in the same window addresses the same item and every call is a single
    **unconditional** ``UpdateItem ADD`` with ``ReturnValues=UPDATED_NEW``. No
    read, no condition, no reset step — nothing to race, so no update can ever be
    lost, on real DynamoDB or DynamoDB Local alike. (An earlier two-phase
    conditional design lost updates ~20% of the time under DynamoDB Local's
    weaker conditional-write isolation; that regression is exactly what
    ``tests/test_velocity_concurrency.py`` now guards against.) Windows are
    fixed/tumbling, aligned to multiples of ``window_s``; old buckets self-expire
    via the item TTL.
    """

    def __init__(
        self,
        table_name: str,
        *,
        endpoint_url: str | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        self._table_name = table_name
        self._kwargs: dict[str, Any] = {"region_name": region_name}
        if endpoint_url:
            # DynamoDB Local ignores credentials but boto3 still requires
            # *something* present to sign requests with.
            self._kwargs["endpoint_url"] = endpoint_url
            self._kwargs.setdefault("aws_access_key_id", "local")
            self._kwargs.setdefault("aws_secret_access_key", "local")
        # boto3 resource/Table objects are NOT thread-safe. In production each
        # Lambda invocation is its own process with its own client, so one table
        # per process is fine; but the in-process concurrency test shares one
        # store across threads to simulate concurrent invocations. A shared
        # resource corrupts interleaved requests there and surfaces as "lost
        # updates" / empty reads that are a boto3-client artifact, not a
        # DynamoDB ADD failure — so each thread lazily gets its own resource.
        self._local = threading.local()

    @property
    def _table(self) -> Any:
        table = getattr(self._local, "table", None)
        if table is None:
            import boto3

            table = boto3.resource("dynamodb", **self._kwargs).Table(self._table_name)
            self._local.table = table
        return table

    # -- velocity ----------------------------------------------------------

    @staticmethod
    def _bucket(now: float, window_s: int) -> int:
        """Fixed tumbling-window index — every caller in the same window shares
        one item key, which is what makes the unconditional ADD race-free."""
        return int(now // window_s)

    def record_and_sum(self, key: str, amount_paise: int, now: float, window_s: int) -> int:
        pk = f"vel#{key}#{self._bucket(now, window_s)}"
        ttl = now + window_s + _TTL_GRACE_S
        resp = self._table.update_item(
            Key={"pk": pk},
            UpdateExpression="ADD #sum :amt, #cnt :one SET #ttl = :ttl",
            ExpressionAttributeNames={"#sum": "sum_paise", "#cnt": "count", "#ttl": "ttl"},
            ExpressionAttributeValues={":amt": amount_paise, ":one": 1, ":ttl": _dec(ttl)},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["sum_paise"])

    def window_sum(self, key: str, now: float, window_s: int) -> int:
        pk = f"vel#{key}#{self._bucket(now, window_s)}"
        resp = self._table.get_item(Key={"pk": pk}, ConsistentRead=True)
        item = resp.get("Item")
        return int(item["sum_paise"]) if item else 0

    # -- freeze registry -----------------------------------------------------

    def freeze(self, agent_id: str, tool: str, reason: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self._table.put_item(
                Item={
                    "pk": f"freeze#{agent_id}#{tool}",
                    "frozen": True,
                    "reason": reason,
                    "frozen_at": _dec(time.time()),
                },
                ConditionExpression="attribute_not_exists(pk) OR #f = :false",
                ExpressionAttributeNames={"#f": "frozen"},
                ExpressionAttributeValues={":false": False},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Already frozen — idempotent no-op, first freeze wins.

    def is_frozen(self, agent_id: str, tool: str) -> bool:
        resp = self._table.get_item(
            Key={"pk": f"freeze#{agent_id}#{tool}"}, ConsistentRead=True
        )
        item = resp.get("Item")
        return bool(item and item.get("frozen"))

    def unfreeze(self, agent_id: str, tool: str) -> None:
        self._table.update_item(
            Key={"pk": f"freeze#{agent_id}#{tool}"},
            UpdateExpression="SET #f = :false",
            ExpressionAttributeNames={"#f": "frozen"},
            ExpressionAttributeValues={":false": False},
        )

    # -- capture / refund totals ---------------------------------------------

    def _bump_capture_item(
        self, field: str, agent_id: str, amount_paise: int, now: float, window_s: int
    ) -> None:
        # Same bucketed-key atomic-ADD strategy as record_and_sum: one
        # unconditional ADD per call, race-free, no read/reset. A missing counter
        # field reads as 0, so no zero-initialization of the sibling field is
        # needed.
        pk = f"cap#{agent_id}#{self._bucket(now, window_s)}"
        ttl = now + window_s + _TTL_GRACE_S
        self._table.update_item(
            Key={"pk": pk},
            UpdateExpression="ADD #f :amt SET #ttl = :ttl",
            ExpressionAttributeNames={"#f": field, "#ttl": "ttl"},
            ExpressionAttributeValues={":amt": amount_paise, ":ttl": _dec(ttl)},
        )

    def add_capture(self, agent_id: str, amount_paise: int, now: float, window_s: int) -> None:
        self._bump_capture_item("captured_paise", agent_id, amount_paise, now, window_s)

    def add_refund(self, agent_id: str, amount_paise: int, now: float, window_s: int) -> None:
        self._bump_capture_item("refunded_paise", agent_id, amount_paise, now, window_s)

    def capture_and_refund_totals(
        self, agent_id: str, now: float, window_s: int
    ) -> tuple[int, int]:
        pk = f"cap#{agent_id}#{self._bucket(now, window_s)}"
        resp = self._table.get_item(Key={"pk": pk}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return (0, 0)
        return (int(item.get("captured_paise", 0)), int(item.get("refunded_paise", 0)))
