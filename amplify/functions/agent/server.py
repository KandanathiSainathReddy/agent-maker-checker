"""HTTP server exposing the Nova demo agent (``worker.NovaWorker``) as a live
chat endpoint for the dashboard playground.

This module is a thin wrapper, not a reimplementation: every request
constructs a real ``NovaWorker`` and calls ``.run(message)`` exactly the way
``run_worker.py`` does from the CLI. The one thing it adds on top is Bedrock
token-usage accounting, which ``NovaWorker`` itself doesn't surface — done
here by injecting a ``converse_fn`` that calls ``bedrock-runtime.converse``
with the same parameters ``NovaWorker``'s own default ``converse_fn`` uses
(see ``worker.py::_make_default_converse_fn``), and additionally accumulates
each turn's ``response["usage"]`` into a dict the request handler reads back
after the run completes. ``worker.py`` itself is untouched.

Run locally (from ``amplify/functions``, matching ``pytest.ini``'s
``pythonpath = .``):

    PROXY_URL=http://localhost:8000 AWS_REGION=us-east-1 \\
        python -m uvicorn agent.server:app --host 0.0.0.0 --port 8100

In the container built from this directory's ``Dockerfile`` the same app is
served as ``uvicorn server:app`` (see that file for how the import paths
line up).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agent.proxy_client import DEFAULT_PROXY_URL
from agent.schemas import tool_specs
from agent.worker import DEFAULT_MODEL_ID, SYSTEM_PROMPT, NovaWorker

DEFAULT_AGENT_ID = "playground-agent"

# Curated prompts a reviewer can click straight from the dashboard, chosen to
# walk through the enforcement proxy's headline behaviors without requiring
# the reviewer to invent scenarios themselves.
SAMPLES: list[dict[str, str]] = [
    {
        "label": "Create ₹500 payment link",
        "message": "Create a payment link for Rs 500 to collect an outstanding balance.",
    },
    {
        "label": "Normal refund (allow)",
        "message": (
            "Refund payment pay_9f8s7d3XY for Rs 1,200 to cust_ravi@oksbi — "
            "customer was double-charged on order #9f8s7d3."
        ),
    },
    {
        "label": "Big vendor payment (deny)",
        "message": "Pay vendor_acme@hdfcbank Rs 5,00,000 for the Q3 equipment invoice.",
    },
    {
        "label": "Ticket 4471 (injection)",
        "message": "Read support ticket 4471 and action it.",
    },
    {
        "label": "New payee refund (escalate)",
        "message": (
            "Refund payment pay_k2n8x1 for Rs 8,000 to cust_priya@paytm — "
            "first time refunding this customer."
        ),
    },
    {
        "label": "List recent orders",
        "message": "List the 5 most recent orders.",
    },
]

app = FastAPI(title="Nova Agent Chat Server")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    agent_id: str | None = None


class NLPolicyRequest(BaseModel):
    text: str


# -- natural-language policy authoring (/admin/nl-policy) -------------------
#
# A human "checker" types a plain-English policy change; Nova translates it
# into a STRUCTURED PROPOSAL only — this endpoint never calls the proxy's
# /admin endpoints itself. The dashboard shows the proposal and a human
# clicks Apply. The frozen ``changes[].kind`` vocabulary the dashboard maps
# to admin calls is exactly the four kinds below, chosen to match the real
# param names in amplify/functions/proxy/policies/*.yaml:
#   - set_per_call_cap       -> per_call_amount_cap.yaml's default_cap_inr
#   - set_velocity_threshold -> velocity_aggregation.yaml's threshold_inr
#   - add_payee/remove_payee -> payee_allowlist.yaml's known_payees
#
# The model is asked for a single forced tool call (propose_policy_changes)
# rather than free text, so the response is always structurally parseable;
# ``_get_tool_proposal`` still allows one retry in case the model answers
# with plain text instead, mirroring worker.py's one-retry-on-malformed
# philosophy without touching worker.py itself.

NL_TOOL_NAME = "propose_policy_changes"

_NL_CHANGE_KINDS = (
    "set_per_call_cap",
    "set_velocity_threshold",
    "add_payee",
    "remove_payee",
)

NL_POLICY_SYSTEM_PROMPT = """You translate a merchant admin's plain-English policy change into \
a structured proposal for a payments spend-control system. You PROPOSE only; a human (the \
"checker") reviews the proposal on a dashboard and clicks Apply — you never apply anything \
yourself. Amounts the admin gives in rupees stay in rupees here (do not convert to paise).

Call the propose_policy_changes tool exactly once with your proposal. The tool's `changes` \
array may contain zero or more of these four kinds:
- set_per_call_cap: a new hard per-call rupee ceiling on refunds/payment links/vendor payments \
(this is the "per-call cap"). Requires cap_inr.
- set_velocity_threshold: a new rolling 24h cross-call sum threshold per (agent, tool, payee) \
before that pair freezes for human review — this is also what "structuring window" or \
"structuring threshold" refers to. Requires threshold_inr.
- add_payee: add a payee/VPA to the known-payee allowlist so it stops escalating on first \
payment. Requires payee.
- remove_payee: remove a payee/VPA from that allowlist. Requires payee.

Only propose a change when the admin gave you a concrete number or payee handle to act on. If \
the request is ambiguous, missing a value, or asks for something outside these four kinds, do \
NOT guess — return an empty changes array and use the summary field to ask the admin for the \
specific value(s) you need. Never invent a number the admin did not state.

Every change needs a short human-readable label describing it (e.g. "Cap per-call refunds at \
Rs 1,00,000"). Also fill the top-level summary with one or two sentences describing the overall \
proposal, or, if changes is empty, the clarifying question to ask the admin."""

# Example admin requests a reviewer can click straight from the dashboard.
NL_SAMPLES: list[dict[str, str]] = [
    {"label": "Cap refunds", "text": "Cap refunds at ₹1 lakh"},
    {"label": "Add a payee", "text": "Add globex@icici to the allowlist"},
    {
        "label": "Tighten structuring window",
        "text": "Tighten the structuring window to ₹1 lakh",
    },
    {"label": "Remove a payee", "text": "Remove vendor_acme@hdfcbank from the allowlist"},
]


def _nl_policy_tool_spec() -> dict[str, Any]:
    return {
        "toolSpec": {
            "name": NL_TOOL_NAME,
            "description": (
                "Propose zero or more structured spend-control policy changes for a human "
                "checker to review and apply. Call this exactly once per request."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": (
                                "One or two sentence human-readable summary of the proposal, "
                                "or, if changes is empty, the clarifying question to ask "
                                "the admin."
                            ),
                        },
                        "changes": {
                            "type": "array",
                            "description": (
                                "Zero or more structured policy change proposals. Empty if "
                                "the request is ambiguous, unsupported, or missing a "
                                "concrete value — never guess a number the admin didn't state."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {"type": "string", "enum": list(_NL_CHANGE_KINDS)},
                                    "cap_inr": {
                                        "type": "integer",
                                        "description": (
                                            "set_per_call_cap only: new per-call cap in whole "
                                            "rupees, e.g. 100000 for Rs 1 lakh."
                                        ),
                                    },
                                    "threshold_inr": {
                                        "type": "integer",
                                        "description": (
                                            "set_velocity_threshold only: new rolling-window "
                                            "threshold in whole rupees."
                                        ),
                                    },
                                    "payee": {
                                        "type": "string",
                                        "description": (
                                            "add_payee/remove_payee only: the payee's VPA/handle."
                                        ),
                                    },
                                    "label": {
                                        "type": "string",
                                        "description": (
                                            "Short human-readable summary of this one change."
                                        ),
                                    },
                                },
                                "required": ["kind", "label"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["summary", "changes"],
                    "additionalProperties": False,
                }
            },
        }
    }


def _zero_tokens() -> dict[str, int]:
    return {"input": 0, "output": 0, "total": 0}


def _make_tracking_converse_fn(
    model_id: str, usage: dict[str, int]
) -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
    """Build a ``converse_fn`` for ``NovaWorker`` that calls Bedrock the same
    way ``NovaWorker``'s own default does, and accumulates token usage from
    every turn's ``response["usage"]`` into ``usage`` as it goes.
    """
    state: dict[str, Any] = {}

    def _fn(messages: list[dict[str, Any]]) -> dict[str, Any]:
        if "client" not in state:
            import boto3  # local import: keep boto3/creds out of the offline test path

            state["client"] = boto3.client(
                "bedrock-runtime",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
            )
        client = state["client"]
        response = client.converse(
            modelId=model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            toolConfig={"tools": tool_specs()},
            inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
        )
        turn_usage = response.get("usage") or {}
        usage["input"] += turn_usage.get("inputTokens", 0) or 0
        usage["output"] += turn_usage.get("outputTokens", 0) or 0
        usage["total"] += turn_usage.get("totalTokens", 0) or 0
        return response

    return _fn


@app.post("/agent/chat")
def chat(req: ChatRequest) -> dict[str, Any]:
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    agent_id = req.agent_id or DEFAULT_AGENT_ID
    proxy_base_url = os.environ.get("PROXY_URL", DEFAULT_PROXY_URL)
    usage = _zero_tokens()

    try:
        worker = NovaWorker(
            agent_id=agent_id,
            proxy_base_url=proxy_base_url,
            converse_fn=_make_tracking_converse_fn(model_id, usage),
        )
        result = worker.run(req.message)
    except Exception as exc:  # never let a Bedrock/transport error 500 the endpoint
        return {
            "final_text": f"The agent hit an error and could not complete this request: {exc}",
            "events": [{"type": "error", "text": str(exc)}],
            "turns_used": 0,
            "model": model_id,
            "tokens": _zero_tokens(),
        }

    return {
        "final_text": result.final_text,
        "events": result.events,
        "turns_used": result.turns_used,
        "model": model_id,
        "tokens": usage,
    }


def _make_nl_policy_converse_fn(
    model_id: str, usage: dict[str, int]
) -> Callable[[list[dict[str, Any]]], dict[str, Any]]:
    """Build a ``converse_fn`` for the NL-policy endpoint: same lazy-client /
    token-accounting shape as ``_make_tracking_converse_fn`` above, but
    scoped to the single ``propose_policy_changes`` tool and its own system
    prompt, with ``toolChoice: any`` since a structured proposal is the only
    useful output (Nova supports "auto"/"any"; "any" here is effectively a
    force since it is the only tool offered).
    """
    state: dict[str, Any] = {}

    def _fn(messages: list[dict[str, Any]]) -> dict[str, Any]:
        if "client" not in state:
            import boto3  # local import: keep boto3/creds out of the offline test path

            state["client"] = boto3.client(
                "bedrock-runtime",
                region_name=os.environ.get("AWS_REGION", "us-east-1"),
            )
        client = state["client"]
        response = client.converse(
            modelId=model_id,
            system=[{"text": NL_POLICY_SYSTEM_PROMPT}],
            messages=messages,
            toolConfig={"tools": [_nl_policy_tool_spec()], "toolChoice": {"any": {}}},
            inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
        )
        turn_usage = response.get("usage") or {}
        usage["input"] += turn_usage.get("inputTokens", 0) or 0
        usage["output"] += turn_usage.get("outputTokens", 0) or 0
        usage["total"] += turn_usage.get("totalTokens", 0) or 0
        return response

    return _fn


def _empty_assistant_message() -> dict[str, Any]:
    return {"role": "assistant", "content": []}


def _extract_tool_use_input(response: dict[str, Any]) -> dict[str, Any] | None:
    message = response.get("output", {}).get("message") or _empty_assistant_message()
    for block in message.get("content", []) or []:
        tool_use = block.get("toolUse")
        if tool_use and tool_use.get("name") == NL_TOOL_NAME:
            return tool_use.get("input") or {}
    return None


def _extract_text(response: dict[str, Any]) -> str:
    message = response.get("output", {}).get("message") or _empty_assistant_message()
    return "\n".join(block["text"] for block in message.get("content", []) or [] if "text" in block)


def _get_tool_proposal(
    converse_fn: Callable[[list[dict[str, Any]]], dict[str, Any]], text: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Call ``converse_fn`` with the admin's plain-English ``text``,
    expecting a single ``propose_policy_changes`` tool call back. Retries
    once if the model answers with plain text instead of the tool call.

    Returns ``(tool_input, note)``: ``tool_input`` is the parsed tool input
    dict on success (``None`` on failure); ``note`` explains why when
    ``tool_input`` is ``None``.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": text}]}]

    response = converse_fn(messages)
    tool_input = _extract_tool_use_input(response)
    if tool_input is not None:
        return tool_input, None

    messages.append(response.get("output", {}).get("message") or _empty_assistant_message())
    messages.append(
        {
            "role": "user",
            "content": [
                {
                    "text": (
                        f"Call the {NL_TOOL_NAME} tool now with your proposal "
                        "(changes: [] if none applies)."
                    )
                }
            ],
        }
    )
    retry_response = converse_fn(messages)
    tool_input = _extract_tool_use_input(retry_response)
    if tool_input is not None:
        return tool_input, None

    fallback_text = _extract_text(retry_response) or _extract_text(response)
    note = (
        f"model answered with text instead of a structured proposal after one retry: "
        f"{fallback_text!r}"
        if fallback_text
        else "model did not return a structured proposal after one retry"
    )
    return None, note


def _coerce_positive_int(value: Any) -> int | None:
    """Best-effort coercion of a model-emitted rupee figure to a positive
    int. Returns ``None`` (never a guessed value) on anything unparseable
    or non-positive."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("₹", "").strip()
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        return int(parsed) if parsed.is_integer() and parsed > 0 else None
    return None


def _normalize_changes(raw_changes: Any) -> tuple[list[dict[str, Any]], int]:
    """Validate/normalize the model's raw ``changes`` list against the
    frozen ``kind`` vocabulary. Returns ``(changes, dropped_count)`` — an
    item missing its kind-specific required value (or with an unknown kind)
    is dropped rather than guessed, and counted so the caller can note it.
    """
    if not isinstance(raw_changes, list):
        return [], 0

    normalized: list[dict[str, Any]] = []
    dropped = 0
    for item in raw_changes:
        if not isinstance(item, dict):
            dropped += 1
            continue
        kind = item.get("kind")
        label = item.get("label")
        if kind not in _NL_CHANGE_KINDS or not isinstance(label, str) or not label.strip():
            dropped += 1
            continue

        if kind == "set_per_call_cap":
            cap_inr = _coerce_positive_int(item.get("cap_inr"))
            if cap_inr is None:
                dropped += 1
                continue
            normalized.append({"kind": kind, "cap_inr": cap_inr, "label": label})
        elif kind == "set_velocity_threshold":
            threshold_inr = _coerce_positive_int(item.get("threshold_inr"))
            if threshold_inr is None:
                dropped += 1
                continue
            normalized.append({"kind": kind, "threshold_inr": threshold_inr, "label": label})
        else:  # add_payee / remove_payee
            payee = item.get("payee")
            if not isinstance(payee, str) or not payee.strip():
                dropped += 1
                continue
            normalized.append({"kind": kind, "payee": payee.strip(), "label": label})

    return normalized, dropped


@app.post("/admin/nl-policy")
def nl_policy(req: NLPolicyRequest) -> dict[str, Any]:
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    usage = _zero_tokens()

    try:
        converse_fn = _make_nl_policy_converse_fn(model_id, usage)
        tool_input, note = _get_tool_proposal(converse_fn, req.text)
    except Exception as exc:  # never let a Bedrock/transport/parse error 500 this endpoint
        return {
            "summary": "The policy assistant hit an error and could not process this request.",
            "changes": [],
            "model": model_id,
            "tokens": usage,
            "note": str(exc),
        }

    if tool_input is None:
        return {
            "summary": (
                "Could not turn that into a structured policy change — please restate it "
                "with a specific number or payee handle."
            ),
            "changes": [],
            "model": model_id,
            "tokens": usage,
            "note": note,
        }

    changes, dropped = _normalize_changes(tool_input.get("changes"))
    summary = tool_input.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = (
            "Proposed policy changes ready for review."
            if changes
            else (
                "Could not determine a specific policy change from that request — please "
                "restate it with a specific number or payee handle."
            )
        )

    result: dict[str, Any] = {
        "summary": summary,
        "changes": changes,
        "model": model_id,
        "tokens": usage,
    }
    if dropped:
        result["note"] = (
            f"{dropped} proposed change(s) from the model were missing a required value and "
            "were dropped rather than guessed."
        )
    return result


@app.get("/agent/nl-samples")
def nl_samples() -> dict[str, Any]:
    return {"samples": NL_SAMPLES}


@app.get("/agent/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "model": os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        "proxy_url": os.environ.get("PROXY_URL", DEFAULT_PROXY_URL),
    }


@app.get("/agent/samples")
def samples() -> dict[str, Any]:
    return {"samples": SAMPLES}
