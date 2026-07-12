"""Environment variables (infra/CONTRACTS.md §2) + StateStore/AuditLog factories.

``get_state_store()`` and ``get_audit_log()`` are memoized process-wide
singletons: the in-memory backend only works at all if every request shares
one instance, and the point of the Dynamo backend is a shared instance across
concurrent Lambda invocations. Tests that want isolation construct
``InMemoryStateStore()`` / ``JsonlAuditLog(tmp_path)`` directly instead of
going through this module.
"""

from __future__ import annotations

import os
from functools import lru_cache

from proxy.audit import AuditLog, DynamoAuditLog, JsonlAuditLog
from proxy.state import DynamoStateStore, InMemoryStateStore, StateStore


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def demo_mode() -> str:
    return _env("DEMO_MODE", "cached")


def policy_dir() -> str:
    return _env("POLICY_DIR", "./policies")


def state_backend() -> str:
    return _env("STATE_BACKEND", "memory")


def audit_backend() -> str:
    return _env("AUDIT_BACKEND", "jsonl")


def audit_log_path() -> str:
    return _env("AUDIT_LOG_PATH", "./data/audit.jsonl")


def ddb_endpoint_url() -> str | None:
    return os.environ.get("DDB_ENDPOINT_URL") or None


def ddb_state_table() -> str:
    return _env("DDB_STATE_TABLE", "amc-state")


def ddb_audit_table() -> str:
    return _env("DDB_AUDIT_TABLE", "amc-audit")


def ddb_approvals_table() -> str:
    return _env("DDB_APPROVALS_TABLE", "amc-approvals")


def aws_region() -> str:
    return _env("AWS_REGION", "us-east-1")


def proxy_port() -> int:
    return int(_env("PROXY_PORT", "8000"))


@lru_cache(maxsize=1)
def get_state_store() -> StateStore:
    if state_backend() == "dynamodb":
        return DynamoStateStore(
            ddb_state_table(), endpoint_url=ddb_endpoint_url(), region_name=aws_region()
        )
    return InMemoryStateStore()


@lru_cache(maxsize=1)
def get_audit_log() -> AuditLog:
    if audit_backend() == "dynamodb":
        return DynamoAuditLog(
            ddb_audit_table(), endpoint_url=ddb_endpoint_url(), region_name=aws_region()
        )
    return JsonlAuditLog(audit_log_path())
