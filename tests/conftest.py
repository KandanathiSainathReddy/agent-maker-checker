"""Shared fixtures for the proxy test suite."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from proxy.models import ProvenanceEntry, ToolCallContext, ToolCallMeta, ToolCallRequest

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = REPO_ROOT / "policies"

DDB_LOCAL_HOST = "localhost"
DDB_LOCAL_PORT = 8001
DDB_LOCAL_ENDPOINT = f"http://{DDB_LOCAL_HOST}:{DDB_LOCAL_PORT}"


def ddb_local_available(timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((DDB_LOCAL_HOST, DDB_LOCAL_PORT), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture
def policy_dir() -> Path:
    return POLICY_DIR


@pytest.fixture
def make_request():
    """Factory fixture: build a ToolCallRequest with sane defaults, override anything."""

    def _make(
        *,
        agent_id: str = "support-agent-1",
        tool: str = "pay_vendor",
        amount: int = 1_000_000,
        payee: str | None = "vendor_acme@hdfcbank",
        payment_id: str | None = "pay_x",
        extra_arguments: dict | None = None,
        provenance: list[ProvenanceEntry] | None = None,
        labeled_legit: bool = False,
    ) -> ToolCallRequest:
        arguments = {"amount": amount}
        if payee is not None:
            arguments["payee"] = payee
        if payment_id is not None:
            arguments["payment_id"] = payment_id
        if extra_arguments:
            arguments.update(extra_arguments)
        return ToolCallRequest(
            agent_id=agent_id,
            tool=tool,
            arguments=arguments,
            context=ToolCallContext(payee=payee, provenance=provenance or []),
            meta=ToolCallMeta(labeled_legit=labeled_legit),
        )

    return _make
