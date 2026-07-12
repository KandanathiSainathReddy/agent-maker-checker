#!/usr/bin/env python
"""One-click runner for the agent-maker-checker attack pack.

    python run.py            # run every scenario, s00 -> s04, in order
    python run.py 02         # run just s02 ("s2"/"s02" both work)
    python run.py --hitl     # also run the human-in-the-loop unfreeze demo
                              # (only s02 currently uses it)

Drives the proxy exclusively over HTTP (POST /tool-call and friends, per
infra/CONTRACTS.md §1) — never imports proxy.* directly.

If ``PROXY_URL`` is set in the environment, that already-running proxy is
driven instead. Otherwise this self-hosts: launches
``uvicorn proxy.app:app`` as a subprocess from amplify/functions with a
clean env (in-memory state, a fresh-temp-dir JSONL audit log, POLICY_DIR
pointed at the real shipped policies), waits for /healthz, runs the
selected scenarios, tears the subprocess down.

Exits non-zero if any scenario fails.
"""

from __future__ import annotations

import argparse
import os
import sys

import s00_clean_pass
import s01_indirect_injection
import s02_structuring
import s03_payee_swap
import s04_velocity_flood
from common import EventLog, ProxyClient, counterfactual_line, external_proxy, self_hosted_proxy

# Windows consoles often default stdout/stderr to a codepage (e.g. cp1252)
# that can't encode ₹ or box-drawing separators, which would otherwise crash
# every narrated print in this pack with a UnicodeEncodeError. Force UTF-8,
# falling back to a visible replacement glyph rather than crashing on any
# console this still can't render.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ALL_SCENARIOS = [
    s00_clean_pass,
    s01_indirect_injection,
    s02_structuring,
    s03_payee_swap,
    s04_velocity_flood,
]


def _select_scenarios(arg: str | None) -> list:
    if arg is None:
        return list(ALL_SCENARIOS)
    digits = arg.lower().removeprefix("s")
    if not digits.isdigit():
        return []
    target_id = f"s{int(digits):02d}"
    return [m for m in ALL_SCENARIOS if target_id == m.ID]


def _run_one(module, client: ProxyClient, log: EventLog, *, hitl: bool) -> bool:
    log.header(f"{module.ID} — {module.TITLE}")
    log.event(module.DESCRIPTION)
    try:
        checklist = module.run(client, log, hitl=hitl)
    except Exception as exc:  # a scenario blowing up is a FAIL, not a crash of the whole run
        log.event(f"    [FAIL] {module.ID} raised {exc.__class__.__name__}: {exc}")
        return False
    return checklist.passed


def main() -> int:
    parser = argparse.ArgumentParser(description="agent-maker-checker attack pack runner")
    parser.add_argument(
        "scenario",
        nargs="?",
        default=None,
        help="run a single scenario by id, e.g. 02 or s02 (default: run all)",
    )
    parser.add_argument(
        "--hitl",
        action="store_true",
        help="after s02, also demonstrate the human-in-the-loop unfreeze approval",
    )
    args = parser.parse_args()

    selected = _select_scenarios(args.scenario)
    if not selected:
        print(f"no scenario matches {args.scenario!r} (try 00-04 or s00-s04)", file=sys.stderr)
        return 2

    log = EventLog()
    proxy_url = os.environ.get("PROXY_URL")

    results: list[tuple[str, str, bool]] = []
    proxy_cm = external_proxy(log, proxy_url) if proxy_url else self_hosted_proxy(log)
    try:
        with proxy_cm as base_url, ProxyClient(base_url) as client:
            for module in selected:
                ok = _run_one(module, client, log, hitl=args.hitl)
                results.append((module.ID, module.TITLE, ok))

            metrics = client.metrics()
            audit = client.audit_verify()
    except Exception as exc:
        print(f"\nFATAL: could not run the attack pack: {exc}", file=sys.stderr)
        return 2

    log.header("SUMMARY")
    all_ok = True
    for scenario_id, title, ok in results:
        status = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        print(f"  [{status}] {scenario_id}  {title}")

    print()
    if audit["ok"]:
        print(f"audit chain: ok ({audit['entries_checked']} entries, sha256 hash-chained)")
    else:
        print(f"audit chain: BROKEN — {audit['error']}")
        all_ok = False
    print(counterfactual_line(metrics))

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
