"""Offline tests for the natural-language policy-authoring endpoint
(``POST /admin/nl-policy`` and ``GET /agent/nl-samples`` in ``agent/server.py``)
— no Bedrock, no network.

Following the pattern in ``agent/tests/test_server.py``: the Bedrock call is
faked by monkeypatching ``boto3.client`` (``server.py`` lazily imports
boto3 and calls ``.client(...)`` inside its ``converse_fn``, so patching the
shared ``boto3`` module's ``client`` attribute intercepts it regardless of
where the import happens) to return a scripted client whose ``.converse()``
returns canned Converse-API responses carrying a ``propose_policy_changes``
toolUse block (or, for the ambiguous-path test, plain text with no tool
use at all).
"""

from __future__ import annotations

from typing import Any

import boto3
from fastapi.testclient import TestClient

from agent.server import NL_TOOL_NAME, app
from agent.worker import DEFAULT_MODEL_ID

client = TestClient(app)


# -- fakes --------------------------------------------------------------------


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
    tool_input: dict[str, Any],
    usage: dict[str, int],
) -> dict[str, Any]:
    content = [{"toolUse": {"toolUseId": tool_use_id, "name": NL_TOOL_NAME, "input": tool_input}}]
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use",
        "usage": usage,
    }


def _assistant_text(text: str, usage: dict[str, int]) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
        "usage": usage,
    }


# -- /admin/nl-policy: happy path ---------------------------------------------


def test_nl_policy_returns_scripted_changes_and_tokens(monkeypatch):
    tool_input = {
        "summary": "Cap per-call refunds at Rs 1,00,000 and add globex@icici to the allowlist.",
        "changes": [
            {
                "kind": "set_per_call_cap",
                "cap_inr": 100000,
                "label": "Cap per-call refunds at Rs 1,00,000",
            },
            {
                "kind": "add_payee",
                "payee": "globex@icici",
                "label": "Add globex@icici to the payee allowlist",
            },
        ],
    }
    responses = [
        _assistant_tool_use(
            "t1", tool_input, usage={"inputTokens": 40, "outputTokens": 20, "totalTokens": 60}
        )
    ]
    _install_fake_bedrock(monkeypatch, _FakeBedrockClient(responses))
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)

    response = client.post(
        "/admin/nl-policy",
        json={"text": "cap refunds at ₹1 lakh and add globex@icici to the allowlist"},
    )

    assert response.status_code == 200
    body = response.json()

    assert body["changes"] == tool_input["changes"]
    assert body["summary"] == tool_input["summary"]
    assert body["model"] == DEFAULT_MODEL_ID
    assert body["tokens"] == {"input": 40, "output": 20, "total": 60}
    assert "note" not in body


def test_nl_policy_all_four_change_kinds_round_trip(monkeypatch):
    tool_input = {
        "summary": "Four changes proposed.",
        "changes": [
            {"kind": "set_per_call_cap", "cap_inr": 150000, "label": "cap"},
            {"kind": "set_velocity_threshold", "threshold_inr": 100000, "label": "velocity"},
            {"kind": "add_payee", "payee": "new@bank", "label": "add"},
            {"kind": "remove_payee", "payee": "vendor_acme@hdfcbank", "label": "remove"},
        ],
    }
    responses = [
        _assistant_tool_use(
            "t1", tool_input, usage={"inputTokens": 10, "outputTokens": 10, "totalTokens": 20}
        )
    ]
    _install_fake_bedrock(monkeypatch, _FakeBedrockClient(responses))

    response = client.post("/admin/nl-policy", json={"text": "do four things"})

    assert response.status_code == 200
    body = response.json()
    assert body["changes"] == tool_input["changes"]
    kinds = [c["kind"] for c in body["changes"]]
    assert kinds == [
        "set_per_call_cap",
        "set_velocity_threshold",
        "add_payee",
        "remove_payee",
    ]


# -- /admin/nl-policy: ambiguous path (no tool call) --------------------------


def test_nl_policy_ambiguous_request_yields_no_changes_and_a_note(monkeypatch):
    # The model answers with text twice — once, then again on the one retry —
    # never producing the propose_policy_changes tool call.
    responses = [
        _assistant_text(
            "I'm not sure what number you mean.",
            usage={"inputTokens": 15, "outputTokens": 5, "totalTokens": 20},
        ),
        _assistant_text(
            "Could you tell me the specific cap amount?",
            usage={"inputTokens": 25, "outputTokens": 8, "totalTokens": 33},
        ),
    ]
    _install_fake_bedrock(monkeypatch, _FakeBedrockClient(responses))

    response = client.post("/admin/nl-policy", json={"text": "make refunds safer"})

    assert response.status_code == 200
    body = response.json()

    assert body["changes"] == []
    assert isinstance(body.get("note"), str) and body["note"]
    assert isinstance(body["summary"], str) and body["summary"]
    # both scripted turns' tokens were accumulated
    assert body["tokens"] == {"input": 40, "output": 13, "total": 53}


def test_nl_policy_recovers_when_tool_call_arrives_on_retry(monkeypatch):
    tool_input = {
        "summary": "Cap per-call refunds at Rs 50,000.",
        "changes": [
            {"kind": "set_per_call_cap", "cap_inr": 50000, "label": "Cap per-call refunds"}
        ],
    }
    responses = [
        _assistant_text(
            "Did you mean a per-call cap?",
            usage={"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        ),
        _assistant_tool_use(
            "t2", tool_input, usage={"inputTokens": 12, "outputTokens": 6, "totalTokens": 18}
        ),
    ]
    _install_fake_bedrock(monkeypatch, _FakeBedrockClient(responses))

    response = client.post("/admin/nl-policy", json={"text": "cap it at 50k"})

    assert response.status_code == 200
    body = response.json()
    assert body["changes"] == tool_input["changes"]
    assert body["tokens"] == {"input": 22, "output": 11, "total": 33}


# -- /admin/nl-policy: never guesses a dropped/invalid change -----------------


def test_nl_policy_drops_change_missing_required_value_instead_of_guessing(monkeypatch):
    tool_input = {
        "summary": "One change applied, one skipped.",
        "changes": [
            {"kind": "set_per_call_cap", "cap_inr": 100000, "label": "valid cap change"},
            {"kind": "set_per_call_cap", "label": "missing cap_inr entirely"},
        ],
    }
    responses = [
        _assistant_tool_use(
            "t1", tool_input, usage={"inputTokens": 5, "outputTokens": 5, "totalTokens": 10}
        )
    ]
    _install_fake_bedrock(monkeypatch, _FakeBedrockClient(responses))

    response = client.post("/admin/nl-policy", json={"text": "cap it, and also something else"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["changes"]) == 1
    assert body["changes"][0]["label"] == "valid cap change"
    assert isinstance(body.get("note"), str) and "1" in body["note"]


# -- /admin/nl-policy: Bedrock error never 500s --------------------------------


def test_nl_policy_bedrock_error_returns_200_with_note(monkeypatch):
    _install_fake_bedrock(monkeypatch, _RaisingBedrockClient())

    response = client.post("/admin/nl-policy", json={"text": "cap refunds at 1 lakh"})

    assert response.status_code == 200
    body = response.json()
    assert body["changes"] == []
    assert "bedrock is unreachable" in body["note"]
    assert body["tokens"] == {"input": 0, "output": 0, "total": 0}


# -- /agent/nl-samples ----------------------------------------------------------


def test_nl_samples_shape():
    response = client.get("/agent/nl-samples")

    assert response.status_code == 200
    body = response.json()
    assert "samples" in body
    samples = body["samples"]
    assert 3 <= len(samples) <= 4
    for sample in samples:
        assert isinstance(sample["label"], str) and sample["label"]
        assert isinstance(sample["text"], str) and sample["text"]

    joined = " ".join(s["text"] for s in samples)
    assert "globex@icici" in joined
    assert "vendor_acme@hdfcbank" in joined
