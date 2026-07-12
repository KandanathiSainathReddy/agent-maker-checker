"""Runtime policy-parameter overrides — the "checker" half of maker-checker.

A merchant/admin can change a policy's configuration (a cap, an allowlist,
...) live via the ``/admin/policy*`` routes in ``app.py``, and have the
policy engine respect that change on the very next ``/tool-call`` — no
redeploy, no YAML edit, no restart.

Overrides are policy CONFIG, not per-request state: they live beside
``proxy/state.py`` (which is call-history state: velocity windows, freezes,
capture totals) but are a completely separate concern with a separate
interface. ``proxy/engine.py`` reads them at evaluate time and deep-merges
them on top of the YAML-loaded params for that policy (see
``engine._merged_params``); nothing here knows about policy semantics, it is
just a keyed dict store.

One interface (``PolicyOverrides``), two implementations, matching the
``StateStore`` / ``AuditLog`` / ``ApprovalQueue`` pattern in this codebase:

- ``InMemoryPolicyOverrides`` — single-process, lock-protected.
- ``DynamoPolicyOverrides`` — shared across concurrent Lambda invocations.
  Reuses the ``amc-state`` table (infra/CONTRACTS.md §4) rather than a
  dedicated table: overrides are small, low-volume, admin-only writes, not
  worth a fifth DynamoDB table for the demo. One item per overridden policy:
  ``pk="pol#{policy_id}"`` holding a JSON-encoded params blob. Storing the
  params as a single JSON string (rather than native DynamoDB attributes)
  sidesteps the float/Decimal dance boto3's resource API otherwise requires
  for numbers, since override payloads are arbitrary nested policy params,
  not a fixed schema.
"""

from __future__ import annotations

import copy
import json
import threading
from typing import Any, Protocol


class PolicyOverrides(Protocol):
    def get(self, policy_id: str) -> dict[str, Any]:
        """Current override params for policy_id, or {} if none is stored."""
        ...

    def set(self, policy_id: str, params: dict[str, Any]) -> None:
        """Replace policy_id's stored override params wholesale (not a merge)."""
        ...

    def all(self) -> dict[str, dict[str, Any]]:
        """Every policy_id that has a stored override, keyed by policy_id."""
        ...


class InMemoryPolicyOverrides:
    """Single-process PolicyOverrides behind one lock — mirrors
    ``state.InMemoryStateStore``'s single-global-lock rationale: this backend
    only ever runs inside one docker-compose process or one pytest process,
    well below the scale where per-key lock striping would matter.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._overrides: dict[str, dict[str, Any]] = {}

    def get(self, policy_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._overrides.get(policy_id, {}))

    def set(self, policy_id: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._overrides[policy_id] = copy.deepcopy(params)

    def all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: copy.deepcopy(v) for k, v in self._overrides.items()}


class DynamoPolicyOverrides:
    """PolicyOverrides backed by the shared ``amc-state`` table.

    Same lazy-per-thread boto3 ``Table`` pattern as
    ``state.DynamoStateStore``: boto3 resource/Table objects are not
    thread-safe, and while each Lambda invocation is its own process in
    production, a shared store can be exercised across threads in tests, so
    each thread lazily gets its own resource/table handle.
    """

    _PK_PREFIX = "pol#"

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
        self._local = threading.local()

    @property
    def _table(self) -> Any:
        table = getattr(self._local, "table", None)
        if table is None:
            import boto3

            table = boto3.resource("dynamodb", **self._kwargs).Table(self._table_name)
            self._local.table = table
        return table

    @classmethod
    def _key(cls, policy_id: str) -> str:
        return f"{cls._PK_PREFIX}{policy_id}"

    def get(self, policy_id: str) -> dict[str, Any]:
        resp = self._table.get_item(Key={"pk": self._key(policy_id)}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return {}
        return json.loads(item["params_json"])

    def set(self, policy_id: str, params: dict[str, Any]) -> None:
        self._table.put_item(
            Item={"pk": self._key(policy_id), "params_json": json.dumps(params)}
        )

    def all(self) -> dict[str, dict[str, Any]]:
        # No GSI on amc-state, so this is a filtered table scan -- fine at
        # admin/demo volume (a handful of policies), and only ever called
        # from PolicyEngine.evaluate()'s once-per-request read, not per policy.
        out: dict[str, dict[str, Any]] = {}
        kwargs: dict[str, Any] = {
            "FilterExpression": "begins_with(pk, :p)",
            "ExpressionAttributeValues": {":p": self._PK_PREFIX},
        }
        while True:
            resp = self._table.scan(**kwargs)
            for item in resp.get("Items", []):
                policy_id = item["pk"][len(self._PK_PREFIX) :]
                out[policy_id] = json.loads(item["params_json"])
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return out
