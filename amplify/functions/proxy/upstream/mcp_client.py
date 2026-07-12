"""Live upstream: Razorpay's self-hosted, open-source MCP server.

github.com/razorpay/razorpay-mcp-server (Go). Research trail, exact tool
catalog, and the reviewer dashboard-proof steps live in
infra/razorpay-mcp.md -- summary of what matters here:

* Transport is stdio ONLY. The repo has exactly one subcommand (`stdio`,
  cmd/razorpay-mcp-server/stdio.go) and the published Dockerfile's
  ENTRYPOINT hardcodes it (`razorpay-mcp-server stdio --key $RAZORPAY_KEY_ID
  --secret $RAZORPAY_KEY_SECRET`). There is no sse/http subcommand to point
  a URL at, so this module speaks MCP the way every stdio MCP client does:
  spawn the server as a child process and exchange newline-delimited
  JSON-RPC 2.0 messages over its stdin/stdout.
* No `mcp` SDK dependency: proxy/requirements.txt is outside this
  workstream's file scope (see infra/CONTRACTS.md #5 ownership split), so
  rather than add a dependency in a file we don't own, this hand-rolls the
  minimal handshake the spec requires -- initialize -> notifications/
  initialized -> tools/call. That's the whole surface we need.
* Docker Hub (hub.docker.com/r/razorpay/mcp) publishes only commit-hash
  tags -- there is no `latest` and no semver tag, so `docker pull
  razorpay/mcp` with no tag fails outright. DEFAULT_IMAGE below pins the
  tip-of-main tag as researched 2026-07-12; override with RAZORPAY_MCP_IMAGE
  if it has since been pruned. `docker compose run --rm razorpay-mcp` is
  a good sanity check.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import time
from typing import Any

from .base import UpstreamExecutor, UpstreamResult

DEFAULT_IMAGE = "razorpay/mcp:8607a4d95e67c86db8201d728658eb5f00d790fe"

MCP_PROTOCOL_VERSION = "2024-11-05"
CONNECT_TIMEOUT_S = 15.0
CALL_TIMEOUT_S = 30.0

# Our tool vocabulary (infra/CONTRACTS.md #1) -> razorpay-mcp-server's actual
# tool name. None = confirmed no live tool exists for it (see _GAP_MESSAGES).
TOOL_MAP: dict[str, str | None] = {
    "create_payment_link": "create_payment_link",
    "issue_refund": "create_refund",
    "list_orders": "fetch_all_orders",
    "pay_vendor": None,
    "get_ticket": None,
}

_GAP_MESSAGES: dict[str, str] = {
    "pay_vendor": (
        "pay_vendor has no live Razorpay MCP tool: razorpay-mcp-server's "
        "payouts toolset is fetch-only (fetch_payout_with_id, "
        "fetch_all_payouts) -- there is no create/initiate payout tool as of "
        "v1.2.1. Use DEMO_MODE=cached to demo this path end-to-end."
    ),
    "get_ticket": (
        "get_ticket is not a Razorpay API concept and razorpay-mcp-server "
        "does not expose it. Use DEMO_MODE=cached, or route this tool to a "
        "non-Razorpay upstream."
    ),
}


class _TransientMCPError(Exception):
    """Connection-level failure worth exactly one retry: dead process, closed
    pipe, or a timed-out read/write. NOT raised for a clean JSON-RPC error
    response (e.g. bad payment_id) -- that's a deterministic application
    error and retrying it would just fail the same way again."""


def _build_command() -> tuple[str, list[str]]:
    """Resolve (command, args) to spawn the MCP server's stdio process.

    Defaults to the documented `docker run` invocation from the server's own
    README. Override via RAZORPAY_MCP_BIN (+ optional RAZORPAY_MCP_ARGS) to
    point at a locally built/installed `razorpay-mcp-server` binary instead
    -- useful wherever the caller's process can't spawn sibling Docker
    containers (e.g. the proxy running inside its own container without the
    docker socket mounted; see infra/razorpay-mcp.md for that gap).
    """
    bin_override = os.environ.get("RAZORPAY_MCP_BIN")
    if bin_override:
        return bin_override, shlex.split(os.environ.get("RAZORPAY_MCP_ARGS", "stdio"))
    image = os.environ.get("RAZORPAY_MCP_IMAGE", DEFAULT_IMAGE)
    return (
        "docker",
        ["run", "--rm", "-i", "-e", "RAZORPAY_KEY_ID", "-e", "RAZORPAY_KEY_SECRET", image],
    )


def _map_arguments(our_tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Translate OUR tool-call arguments into the server tool's real params.

    Raises KeyError for a genuinely required-but-missing field -- caught by
    the caller and turned into a clean UpstreamResult error.
    """
    if our_tool == "create_payment_link":
        out: dict[str, Any] = {
            "amount": arguments["amount"],
            "currency": arguments.get("currency", "INR"),
        }
        passthrough = (
            "description", "notes", "reference_id",
            "customer_name", "customer_email", "customer_contact",
            "callback_url", "callback_method",
        )
        for key in passthrough:
            if key in arguments:
                out[key] = arguments[key]
        return out

    if our_tool == "issue_refund":
        out = {"payment_id": arguments["payment_id"], "amount": arguments["amount"]}
        if "notes" in arguments:
            out["notes"] = arguments["notes"]
        if "speed" in arguments:
            out["speed"] = arguments["speed"]
        return out

    if our_tool == "list_orders":
        return {k: arguments[k] for k in ("count", "skip", "from", "to") if k in arguments}

    return dict(arguments)


def _parse_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Unwrap an MCP `tools/call` result into a plain data dict.

    razorpay-mcp-server (Go MCP SDK) returns `content: [{"type": "text",
    "text": "<json>"}]` for its tool responses; some MCP servers also
    populate `structuredContent` directly. Handle both, plus `isError`.
    """
    if result.get("isError"):
        blocks = result.get("content") or []
        text = " ".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        raise RuntimeError(text or "MCP tool call returned isError=true")

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured

    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
            return parsed if isinstance(parsed, dict) else {"value": parsed}

    return result


class _StdioMCPSession:
    """One live JSON-RPC-over-stdio connection to a razorpay-mcp-server process."""

    def __init__(self, command: str, args: list[str]):
        self._command = command
        self._args = args
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: list[str] = []

    async def start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as exc:
            raise _TransientMCPError(f"failed to spawn '{self._command}': {exc}") from exc
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await asyncio.wait_for(self._handshake(), timeout=CONNECT_TIMEOUT_S)
        except TimeoutError as exc:
            raise _TransientMCPError("MCP initialize handshake timed out") from exc

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                self._stderr_tail.append(line.decode(errors="replace").rstrip())
                self._stderr_tail = self._stderr_tail[-20:]
        except Exception:
            return

    async def _handshake(self) -> None:
        await self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-maker-checker-proxy", "version": "0.1.0"},
            },
        )
        await self._send(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write((json.dumps(payload) + "\n").encode())
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _TransientMCPError(f"stdin write failed: {exc}") from exc

    async def _await_response(self, req_id: int, timeout: float) -> dict[str, Any]:
        assert self._proc is not None and self._proc.stdout is not None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _TransientMCPError("MCP response timed out")
            try:
                line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
            except TimeoutError as exc:
                raise _TransientMCPError("MCP response timed out") from exc
            if not line:
                tail = " | ".join(self._stderr_tail[-3:])
                raise _TransientMCPError(
                    "razorpay-mcp-server closed stdout"
                    + (f" (stderr: {tail})" if tail else "")
                )
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate stray non-JSON noise on stdout
            if isinstance(msg, dict) and msg.get("id") == req_id:
                return msg
            # else: a notification or a response to a different id -- ignore

    async def _request(
        self, method: str, params: dict[str, Any], timeout: float = CONNECT_TIMEOUT_S
    ) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await self._await_response(req_id, timeout)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = await self._request(
            "tools/call", {"name": name, "arguments": arguments}, timeout=CALL_TIMEOUT_S
        )
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
        return resp.get("result", {})

    async def close(self) -> None:
        if self._stderr_task:
            self._stderr_task.cancel()
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except Exception:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()


class MCPUpstream(UpstreamExecutor):
    """UpstreamExecutor fronting the self-hosted Razorpay MCP server (stdio).

    Lazily connects on the first `execute()` call (never in `__init__`, so
    constructing this in a keyless/offline environment is always safe).
    Retries exactly once on a transient connection failure by discarding the
    dead session and spawning a fresh process; a clean application-level
    error from the server (bad payment_id, validation failure, ...) is
    returned immediately with no retry. Never raises into the proxy hot
    path -- every failure mode becomes `UpstreamResult(ok=False, error=...)`.
    """

    def __init__(self, command: str | None = None, args: list[str] | None = None):
        if command is None:
            command, args = _build_command()
        self._command = command
        self._args = args or []
        self._session: _StdioMCPSession | None = None
        self._connect_lock = asyncio.Lock()

    async def _ensure_session(self) -> _StdioMCPSession:
        if self._session is None:
            async with self._connect_lock:
                if self._session is None:
                    session = _StdioMCPSession(self._command, self._args)
                    await session.start()
                    self._session = session
        return self._session

    async def _reset_session(self) -> None:
        async with self._connect_lock:
            if self._session is not None:
                with contextlib.suppress(Exception):
                    await self._session.close()
                self._session = None

    async def execute(self, tool: str, arguments: dict[str, Any]) -> UpstreamResult:
        arguments = arguments or {}

        if tool not in TOOL_MAP:
            return UpstreamResult(
                ok=False, tool=tool, data={}, mode="live",
                error=f"unknown tool '{tool}': not part of the proxy's tool vocabulary",
            )

        server_tool = TOOL_MAP[tool]
        if server_tool is None:
            return UpstreamResult(
                ok=False, tool=tool, data={}, mode="live",
                error=_GAP_MESSAGES.get(tool, f"'{tool}' has no live Razorpay MCP tool"),
            )

        if not (os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET")):
            return UpstreamResult(
                ok=False, tool=tool, data={}, mode="live",
                error="RAZORPAY_KEY_ID/RAZORPAY_KEY_SECRET not set; cannot start live MCP server",
            )

        try:
            mapped_args = _map_arguments(tool, arguments)
        except KeyError as exc:
            return UpstreamResult(
                ok=False, tool=tool, data={}, mode="live",
                error=f"missing required argument {exc} for '{tool}'",
            )

        last_error: Exception | None = None
        for _attempt in (1, 2):  # one retry on transient failure
            try:
                session = await self._ensure_session()
                result = await session.call_tool(server_tool, mapped_args)
                data = _parse_tool_result(result)
                return UpstreamResult(ok=True, tool=tool, data=data, error=None, mode="live")
            except _TransientMCPError as exc:
                last_error = exc
                await self._reset_session()
                continue
            except Exception as exc:  # deterministic/application error -- no retry
                return UpstreamResult(ok=False, tool=tool, data={}, mode="live", error=str(exc))

        return UpstreamResult(
            ok=False, tool=tool, data={}, mode="live",
            error=f"live MCP call failed after one retry: {last_error}",
        )

    async def aclose(self) -> None:
        """Best-effort cleanup of the spawned subprocess, if any."""
        await self._reset_session()

    # Async context manager: one-shot callers (scripts, tests) get subprocess
    # cleanup for free — skipping aclose() leaves the child transport to be
    # torn down by GC after the event loop closes, which asyncio reports as a
    # noisy (harmless) "Event loop is closed" warning.
    async def __aenter__(self) -> MCPUpstream:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
