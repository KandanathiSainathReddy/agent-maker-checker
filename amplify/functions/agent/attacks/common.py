"""Shared plumbing for the attack pack: a narrated timestamped event log, a
thin HTTP client for the enforcement proxy's public surface (infra/CONTRACTS.md
§1), a pass/fail checklist, and a self-hosting proxy subprocess manager.

Deliberately does not import anything from ``proxy.*`` — every scenario in
this package drives the proxy exclusively over HTTP (``POST /tool-call`` and
friends), the same way the Nova agent or a reviewer's curl command would.
That keeps this package decoupled from the proxy's internals and honest
about what it's actually testing: the public contract, not implementation
details.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# amplify/functions/agent/attacks/common.py -> parents[2] == amplify/functions
FUNCTIONS_DIR = Path(__file__).resolve().parents[2]
POLICIES_DIR = FUNCTIONS_DIR / "proxy" / "policies"


# --------------------------------------------------------------------------
# Narrated, timestamped event log
# --------------------------------------------------------------------------


class EventLog:
    """Prints every scenario action with a wall-clock timestamp, so the
    transcript reads like a narrated incident timeline rather than a wall of
    assertions.
    """

    def event(self, message: str) -> None:
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now))
        ms = int((now % 1) * 1000)
        print(f"[{stamp}.{ms:03d}] {message}", flush=True)

    def header(self, title: str) -> None:
        print(flush=True)
        rule = "─" * max(4, 70 - len(title))
        print(f"── {title} {rule}", flush=True)


# --------------------------------------------------------------------------
# Money formatting — display layer only; every value that crosses the wire
# to /tool-call stays integer paise (infra/CONTRACTS.md header note).
# --------------------------------------------------------------------------


def _group_indian(rupees: int) -> str:
    sign = "-" if rupees < 0 else ""
    s = str(abs(rupees))
    if len(s) <= 3:
        return sign + s
    last3, rest = s[-3:], s[:-3]
    parts: list[str] = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return sign + ",".join([*parts, last3])


def inr(paise: int) -> str:
    """Format an integer-paise amount as a rupee string for narration only."""
    return f"₹{_group_indian(paise // 100)}"


def inr_from_rupees(rupees: float) -> str:
    """Format an already-rupee value (as returned by GET /metrics) for display."""
    return f"₹{_group_indian(round(rupees))}"


def counterfactual_line(metrics: dict[str, Any]) -> str:
    return (
        f"{inr_from_rupees(metrics['rupees_attempted'])} attempted · "
        f"{inr_from_rupees(metrics['rupees_moved'])} moved · "
        f"{metrics['calls_escalated']} escalations · "
        f"{metrics['false_blocks']} false blocks · "
        f"{metrics['p95_overhead_ms']:.2f}ms p95 overhead"
    )


# --------------------------------------------------------------------------
# Pass/fail checklist
# --------------------------------------------------------------------------


@dataclass
class Check:
    description: str
    ok: bool
    detail: str = ""


@dataclass
class Checklist:
    log: EventLog
    checks: list[Check] = field(default_factory=list)

    def expect(self, description: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(description, ok, detail))
        status = "PASS" if ok else "FAIL"
        suffix = f" — {detail}" if detail else ""
        self.log.event(f"    [{status}] {description}{suffix}")
        return ok

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)


def expect_decision(
    checks: Checklist,
    resp: dict[str, Any],
    label: str,
    expected: str,
    *,
    policy_id: str | None = None,
    reason_contains: str | None = None,
) -> bool:
    """Assert a /tool-call response's decision (and optionally its tripping
    policy_id / a substring of its reason), recording one checklist entry.
    """
    ok = resp["decision"] == expected
    if policy_id is not None:
        ok = ok and resp["policy_id"] == policy_id
    if reason_contains is not None:
        ok = ok and reason_contains.lower() in resp["reason"].lower()
    want = f"expected {expected}" + (f" via {policy_id}" if policy_id else "")
    detail = (
        f"got decision={resp['decision']} policy_id={resp['policy_id']} "
        f"reason={resp['reason']!r}"
    )
    return checks.expect(f"{label}: {want}", ok, detail=detail)


# --------------------------------------------------------------------------
# HTTP client for the proxy's public surface (infra/CONTRACTS.md §1)
# --------------------------------------------------------------------------


class ProxyClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._http = httpx.Client(base_url=base_url, timeout=timeout)

    def __enter__(self) -> ProxyClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def tool_call(
        self,
        *,
        agent_id: str,
        tool: str,
        amount: int = 0,
        payee: str | None = None,
        extra_arguments: dict[str, Any] | None = None,
        provenance: list[dict[str, Any]] | None = None,
        labeled_legit: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"amount": amount}
        if payee is not None:
            arguments["payee"] = payee
        if extra_arguments:
            arguments.update(extra_arguments)
        payload = {
            "agent_id": agent_id,
            "tool": tool,
            "arguments": arguments,
            "context": {"payee": payee, "provenance": provenance or []},
            "meta": {"labeled_legit": labeled_legit},
        }
        resp = self._http.post("/tool-call", json=payload)
        resp.raise_for_status()
        return resp.json()

    def approvals(self, status: str | None = None) -> list[dict[str, Any]]:
        params = {"status": status} if status else {}
        resp = self._http.get("/approvals", params=params)
        resp.raise_for_status()
        return resp.json()

    def approve(self, approval_id: str) -> dict[str, Any]:
        resp = self._http.post(f"/approvals/{approval_id}/approve")
        resp.raise_for_status()
        return resp.json()

    def deny(self, approval_id: str) -> dict[str, Any]:
        resp = self._http.post(f"/approvals/{approval_id}/deny")
        resp.raise_for_status()
        return resp.json()

    def metrics(self) -> dict[str, Any]:
        resp = self._http.get("/metrics")
        resp.raise_for_status()
        return resp.json()

    def audit_verify(self) -> dict[str, Any]:
        resp = self._http.get("/audit/verify")
        resp.raise_for_status()
        return resp.json()

    def decisions(self, limit: int = 100) -> list[dict[str, Any]]:
        resp = self._http.get("/decisions", params={"limit": limit})
        resp.raise_for_status()
        return resp.json()

    def admin_unfreeze(self, agent_id: str, tool: str) -> dict[str, Any]:
        resp = self._http.post(f"/admin/unfreeze/{agent_id}/{tool}")
        resp.raise_for_status()
        return resp.json()

    def healthz(self) -> dict[str, Any]:
        resp = self._http.get("/healthz")
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------------------
# Self-hosting: launch `uvicorn proxy.app:app` as a clean-env subprocess
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthy(base_url: str, proc: subprocess.Popen | None, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    last_err: Exception | None = None
    with httpx.Client(timeout=2.0) as probe:
        while time.time() < deadline:
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"proxy subprocess exited early with code {proc.returncode} "
                    "before becoming healthy"
                )
            try:
                resp = probe.get(f"{base_url}/healthz")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError as exc:
                last_err = exc
            time.sleep(0.25)
    raise RuntimeError(f"proxy never became healthy within {timeout}s (last error: {last_err})")


@contextlib.contextmanager
def self_hosted_proxy(log: EventLog) -> Iterator[str]:
    """Launch ``uvicorn proxy.app:app`` from amplify/functions with a clean,
    self-contained env: in-memory state, a JSONL audit log in a fresh temp
    dir, and POLICY_DIR pointed at the real shipped policies. Waits for
    /healthz, yields the base URL, tears the subprocess down on exit.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="amc-attacks-"))
    audit_path = tmp_dir / "audit.jsonl"
    server_log_path = tmp_dir / "uvicorn.log"
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env.pop("RAZORPAY_KEY_ID", None)
    env.pop("RAZORPAY_KEY_SECRET", None)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(FUNCTIONS_DIR) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    env.update(
        {
            "DEMO_MODE": "cached",
            "STATE_BACKEND": "memory",
            "AUDIT_BACKEND": "jsonl",
            "AUDIT_LOG_PATH": str(audit_path),
            "POLICY_DIR": str(POLICIES_DIR),
            "PROXY_PORT": str(port),
        }
    )

    log.event(f"self-hosting: uvicorn proxy.app:app on {base_url} (cwd={FUNCTIONS_DIR})")
    log.event(f"    POLICY_DIR={POLICIES_DIR}")
    log.event(f"    STATE_BACKEND=memory  AUDIT_BACKEND=jsonl  AUDIT_LOG_PATH={audit_path}")

    try:
        with server_log_path.open("w", encoding="utf-8") as server_log_fh:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "proxy.app:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--log-level",
                    "warning",
                ],
                cwd=str(FUNCTIONS_DIR),
                env=env,
                stdout=server_log_fh,
                stderr=subprocess.STDOUT,
            )
            try:
                try:
                    _wait_healthy(base_url, proc)
                except Exception:
                    tail = server_log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
                    log.event(f"proxy failed to become healthy; last uvicorn output:\n{tail}")
                    raise
                log.event("proxy is healthy — GET /healthz -> 200")
                yield base_url
            finally:
                log.event("tearing down self-hosted proxy")
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@contextlib.contextmanager
def external_proxy(log: EventLog, url: str) -> Generator[str, None, None]:
    """Drive an already-running proxy at PROXY_URL instead of self-hosting."""
    base_url = url.rstrip("/")
    log.event(f"PROXY_URL set — driving external proxy at {base_url}")
    _wait_healthy(base_url, None, timeout=10.0)
    log.event("external proxy is healthy — GET /healthz -> 200")
    yield base_url
