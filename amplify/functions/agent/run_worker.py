"""CLI entrypoint for the Nova demo worker.

    python run_worker.py --task "Refund order #123 Rs 1,200 for cust_ravi@oksbi"

Prints a narrated transcript: every tool call the model attempts, every
proxy decision (allow/deny/escalate + reason), and the final answer. Talks
to the enforcement proxy over HTTP (``PROXY_URL``, default
http://localhost:8000) and to Bedrock via boto3 (``AWS_REGION``,
``BEDROCK_MODEL_ID`` — see worker.py for the empirical model default and why).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    # Support both `python run_worker.py ...` (script mode) and
    # `python -m agent.run_worker ...` (module mode) — script mode needs
    # amplify/functions on sys.path so `agent.*` imports resolve the same
    # way they do under pytest (pythonpath=. in amplify/functions/pytest.ini).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.worker import NovaWorker  # noqa: E402


def _print_event(event: dict) -> None:
    etype = event.get("type")
    turn = event.get("turn")
    prefix = f"[turn {turn}]" if turn is not None else "[--]"

    if etype == "assistant_text":
        print(f"{prefix} model: {event['text']}")
    elif etype == "tool_call":
        print(f"{prefix} tool_call {event['tool']}({json.dumps(event['arguments'])})")
    elif etype == "tool_call_malformed":
        print(
            f"{prefix} MALFORMED tool_call {event['tool']}({json.dumps(event['arguments'])}) "
            f"attempt={event['attempt']} errors={event['errors']}"
        )
    elif etype == "tool_result":
        note = f" ({event['note']})" if event.get("note") else ""
        print(f"{prefix} tool_result {event['tool']} -> {json.dumps(event['result'])[:300]}{note}")
    elif etype == "proxy_decision":
        prov = event.get("provenance") or []
        prov_note = f" provenance={prov}" if prov else ""
        print(
            f"{prefix} PROXY {event['tool']}({json.dumps(event['arguments'])})"
            f" -> decision={event['decision']} policy={event['policy_id']}"
            f" status={event['status']} reason={event['reason']!r}{prov_note}"
        )
    elif etype == "proxy_error":
        print(f"{prefix} PROXY ERROR {event['tool']}: {event['error']}")
    elif etype == "final":
        print(f"{prefix} FINAL: {event['text']}")
    elif etype == "max_turns_reached":
        print(f"{prefix} (max turns reached without a final answer)")
    else:
        print(f"{prefix} {event}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Nova demo agent on one operator task.")
    parser.add_argument("--task", required=True, help="The operator's task, in plain English.")
    parser.add_argument(
        "--model",
        default=None,
        help="Bedrock model id override (else BEDROCK_MODEL_ID env, else the empirical default).",
    )
    parser.add_argument(
        "--max-turns", type=int, default=8, help="Max Converse turns before giving up."
    )
    parser.add_argument(
        "--agent-id", default="support-agent-1", help="agent_id sent to the proxy."
    )
    parser.add_argument(
        "--proxy-url",
        default=None,
        help="Proxy base URL (else PROXY_URL env, else http://localhost:8000).",
    )
    args = parser.parse_args(argv)

    worker = NovaWorker(
        agent_id=args.agent_id,
        model_id=args.model,
        proxy_base_url=args.proxy_url,
        max_turns=args.max_turns,
    )

    print(f"=== Nova demo agent | model={worker.model_id} | proxy={worker.proxy_base_url} ===")
    print(f"TASK: {args.task}\n")

    result = worker.run(args.task)

    for event in result.events:
        _print_event(event)

    print(f"\n=== done in {result.turns_used} turn(s) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
