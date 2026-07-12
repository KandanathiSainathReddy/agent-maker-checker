"""Offline tests for the chat HTTP server (``agent/server.py``) — no Bedrock,
no network, no running proxy process.

Following the pattern in ``agent/tests/test_worker_loop.py``: the Bedrock
call is faked by monkeypatching ``boto3.client`` (server.py lazily imports
boto3 and calls ``.client(...)`` inside its ``converse_fn``, so patching the
shared ``boto3`` module's ``client`` attribute intercepts it regardless of
where the import happens) to return a scripted client whose ``.converse()``
returns canned Converse-API responses, each carrying a ``usage`` block so the
test also proves ``server.py``'s token-accounting actually accumulates
across turns. The proxy call is faked by monkeypatching
``agent.worker.default_proxy_call`` (the module-level name ``NovaWorker.
__init__`` reads when no ``proxy_call`` is injected) with a canned-allow
stub, exactly the shape ``infra/CONTRACTS.md`` §1 defines.
"""

from __future__ import annotations

from typing import Any

import boto3
from fastapi.testclient import TestClient

import agent.worker as worker_module
from agent.server import DEFAULT_AGENT_ID, SAMPLES, app
from agent.worker import DEFAULT_MODEL_ID

client = TestClient(app)


# -- fakes ----------------------------------------------------------------


class _FakeBedrockClient:
    """Stands in for the boto3 bedrock-runtime client: returns each canned
    response in order, one per ``.converse()`` call."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses
        self._i = 0

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        idx = self._i
        self._i += 1
        return self._responses[idx]


class _RaisingBedrockClient:
    def converse(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("bedrock is unreachable")


def _install_fake_bedrock(monkeypatch, fake_client: Any) -> None:
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: fake_client)


def _assistant_tool_use(
    tool_use_id: str,
    name: str,
    tool_input: dict[str, Any],
    usage: dict[str, int],
    text: str = "",
) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if text:
        content.append({"text": text})
    content.append({"toolUse": {"toolUseId": tool_use_id, "name": name, "input": tool_input}})
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
        "usage": usage,
    }


def _assistant_final(text: str, usage: dict[str, int]) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": usage,
    }


def _install_fake_proxy_allow(monkeypatch) -> list[dict[str, Any]]:
    """Canned-allow proxy stub, recording every call it saw."""
    calls: list[dict[str, Any]] = []

    def _fn(*, agent_id, tool, arguments, context, base_url=None):
        calls.append(
            {"agent_id": agent_id, "tool": tool, "arguments": arguments, "context": context}
        )
        return {
            "request_id": "req_fake",
            "decision": "allow",
            "policy_id": None,
            "reason": "fake allow",
            "evaluated_in_ms": 0.1,
            "status": "executed",
            "upstream_result": {"fake": True},
        }

    monkeypatch.setattr(worker_module, "default_proxy_call", _fn)
    return calls


# -- /agent/healthz ---------------------------------------------------------


def test_healthz_shape(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.amazon.nova-pro-v1:0")
    monkeypatch.setenv("PROXY_URL", "http://proxy.example:9000")

    response = client.get("/agent/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": True,
        "model": "us.amazon.nova-pro-v1:0",
        "proxy_url": "http://proxy.example:9000",
    }


def test_healthz_defaults_when_env_unset(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("PROXY_URL", raising=False)

    response = client.get("/agent/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["model"] == DEFAULT_MODEL_ID
    assert body["proxy_url"] == "http://localhost:8000"


# -- /agent/samples -----------------------------------------------------------


def test_samples_shape():
    response = client.get("/agent/samples")

    assert response.status_code == 200
    body = response.json()
    assert "samples" in body
    samples = body["samples"]
    assert 4 <= len(samples) <= 5
    for sample in samples:
        assert isinstance(sample["label"], str) and sample["label"]
        assert isinstance(sample["message"], str) and sample["message"]
        assert len(sample["label"]) <= 40  # "make the labels short"
    assert samples == SAMPLES

    # covers: normal allow, big-vendor deny, ticket-4471 injection, new-payee escalate
    joined_messages = " ".join(s["message"] for s in samples)
    assert "4471" in joined_messages
    assert "vendor_acme@hdfcbank" in joined_messages


# -- /agent/chat --------------------------------------------------------------


def test_chat_runs_worker_and_reports_tokens(monkeypatch):
    responses = [
        _assistant_tool_use(
            "t1",
            "issue_refund",
            {"payment_id": "pay_x", "amount": 120000, "payee": "cust_ravi@oksbi"},
            usage={"inputTokens": 50, "outputTokens": 10, "totalTokens": 60},
        ),
        _assistant_final(
            "Done — refund allowed and processed.",
            usage={"inputTokens": 70, "outputTokens": 15, "totalTokens": 85},
        ),
    ]
    _install_fake_bedrock(monkeypatch, _FakeBedrockClient(responses))
    calls = _install_fake_proxy_allow(monkeypatch)
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)

    response = client.post(
        "/agent/chat",
        json={"message": "Refund order #123 Rs 1,200 for cust_ravi@oksbi"},
    )

    assert response.status_code == 200
    body = response.json()

    assert body["final_text"] == "Done — refund allowed and processed."
    assert body["turns_used"] == 2
    assert body["model"] == DEFAULT_MODEL_ID

    event_types = [e["type"] for e in body["events"]]
    assert "tool_call" in event_types
    assert "proxy_decision" in event_types
    assert "final" in event_types

    proxy_events = [e for e in body["events"] if e["type"] == "proxy_decision"]
    assert proxy_events[0]["decision"] == "allow"

    assert body["tokens"] == {"input": 120, "output": 25, "total": 145}

    assert len(calls) == 1
    assert calls[0]["agent_id"] == DEFAULT_AGENT_ID
    assert calls[0]["tool"] == "issue_refund"


def test_chat_uses_explicit_agent_id_when_provided(monkeypatch):
    responses = [
        _assistant_final(
            "Sure — what order id should I look at?",
            usage={"inputTokens": 20, "outputTokens": 5, "totalTokens": 25},
        )
    ]
    _install_fake_bedrock(monkeypatch, _FakeBedrockClient(responses))
    calls = _install_fake_proxy_allow(monkeypatch)

    response = client.post(
        "/agent/chat",
        json={"message": "Can you check on my order?", "agent_id": "reviewer-agent-7"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["final_text"] == "Sure — what order id should I look at?"
    assert body["turns_used"] == 1
    assert body["tokens"] == {"input": 20, "output": 5, "total": 25}
    assert calls == []  # no tool use at all in this scripted run


def test_chat_bedrock_error_returns_200_with_error_event(monkeypatch):
    _install_fake_bedrock(monkeypatch, _RaisingBedrockClient())
    _install_fake_proxy_allow(monkeypatch)

    response = client.post("/agent/chat", json={"message": "Refund pay_x Rs 1,200"})

    assert response.status_code == 200
    body = response.json()

    assert "error" in body["final_text"].lower() or "bedrock" in body["final_text"].lower()
    assert body["turns_used"] == 0
    assert body["tokens"] == {"input": 0, "output": 0, "total": 0}
    assert body["events"] == [{"type": "error", "text": "bedrock is unreachable"}]
