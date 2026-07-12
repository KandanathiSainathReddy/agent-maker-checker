"""Loads policies/*.yaml, hot-reloads on change, evaluates them in priority order.

Money convention: YAML policy files express amounts in INR for human
readability (a payments engineer should be able to read ``cap_inr: 150000``
and know exactly what it means). Any params key ending in ``_inr`` is
converted at load time to a sibling key with the same name but ``_paise``
instead, holding the integer-paise value — ``cap_inr: 1500`` produces
``cap_paise: 150000`` alongside it. Nested dicts (per-tool override maps) are
converted recursively. Every ``proxy/policies_impl/*.py`` module reads only
the ``_paise`` keys; money is integer paise from that point on, matching the
rest of the codebase.

Evaluation order: policies run in ascending ``priority`` order (lower number
= runs first). The FIRST policy to return anything other than "allow" wins —
evaluation stops there, so a policy later in priority order does not run (and
therefore cannot mutate state, e.g. record a velocity attempt) once an
earlier policy has already denied or escalated the call. This keeps
"attempted" state limited to calls that actually got past every
higher-priority gate, and keeps per-policy trip counts (``GET /metrics``)
meaning "this policy actually fired", not "this policy would have fired
hypothetically". The trace still records every policy that *was* reached.

Hot reload: every ``evaluate()`` call does a cheap ``os.stat`` on each known
policy file (and a directory listing, to catch new/removed files) and only
re-parses YAML if something changed — negligible overhead per request, no
polling thread, no restart needed to pick up a changed cap.

Runtime overrides: ``PolicyEngine`` optionally takes a ``proxy.overrides.
PolicyOverrides`` store. At evaluate time (and for admin/debug lookups) each
policy's YAML-loaded ``params`` gets deep-merged with that policy's stored
override, if any — see ``_merged_params``/``_deep_merge`` below. The override
dict is run through the exact same ``_convert_params`` used for YAML params,
so e.g. an admin override of ``default_cap_inr`` produces ``default_cap_paise``
too, matching what the ``policies_impl/*.py`` modules actually read. With no
``overrides`` store passed in (``overrides=None``, the default) or no stored
override for a given policy, behavior is byte-identical to before overrides
existed.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from proxy.models import ToolCallRequest
from proxy.overrides import PolicyOverrides
from proxy.policies_impl import (
    payee_allowlist,
    per_call_amount_cap,
    provenance_check,
    refund_to_capture_ratio,
    velocity_aggregation,
)
from proxy.policy_types import Decision, PolicyContext, PolicyEvaluation
from proxy.state import StateStore

PolicyFn = Callable[[PolicyContext], PolicyEvaluation]

REGISTRY: dict[str, PolicyFn] = {
    provenance_check.POLICY_ID: provenance_check.evaluate,
    per_call_amount_cap.POLICY_ID: per_call_amount_cap.evaluate,
    velocity_aggregation.POLICY_ID: velocity_aggregation.evaluate,
    payee_allowlist.POLICY_ID: payee_allowlist.evaluate,
    refund_to_capture_ratio.POLICY_ID: refund_to_capture_ratio.evaluate,
}


def _inr_to_paise(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _inr_to_paise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_inr_to_paise(v) for v in value]
    return int(round(float(value) * 100))


def _convert_params(params: dict[str, Any]) -> dict[str, Any]:
    """Pass every param through unchanged; for each ``*_inr`` key, additionally add
    a ``*_paise`` sibling holding the paise-converted value (scalar, list, or dict
    all handled by ``_inr_to_paise``). Non-``_inr`` keys (window_s, ratio percentages,
    payee lists, flags) are money-unit-agnostic and pass through as-is.
    """
    out: dict[str, Any] = dict(params)
    for key, value in params.items():
        if key.endswith("_inr"):
            out[key[: -len("_inr")] + "_paise"] = _inr_to_paise(value)
    return out


def _applies(applies_to: list[str], tool: str) -> bool:
    return "*" in applies_to or tool in applies_to


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base``: nested dicts merge key by
    key (so overriding one entry of a per-tool map, e.g. ``overrides_inr``,
    leaves sibling entries untouched); any other value in ``override``
    (scalar, list) replaces the base value outright.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _merged_params(
    base_params: dict[str, Any], override_raw: dict[str, Any] | None
) -> dict[str, Any]:
    """``base_params`` (already paise-converted) deep-merged with
    ``override_raw`` (run through the same ``_convert_params`` pipeline
    first). No override -> ``base_params`` returned unchanged.
    """
    if not override_raw:
        return base_params
    return _deep_merge(base_params, _convert_params(override_raw))


@dataclass
class LoadedPolicy:
    policy_id: str
    priority: int
    enabled: bool
    applies_to: list[str]
    description: str
    params: dict[str, Any]
    # Pre-conversion params exactly as authored in YAML (INR-facing, no
    # `*_paise` siblings) -- kept for admin/debug endpoints that want to show
    # a human the configured default without the derived paise keys.
    raw_params: dict[str, Any]


class PolicyEngine:
    def __init__(self, policy_dir: str | Path, *, overrides: PolicyOverrides | None = None) -> None:
        self._dir = Path(policy_dir)
        self._lock = threading.Lock()
        self._policies: list[LoadedPolicy] = []
        self._mtimes: dict[str, float] = {}
        self._overrides = overrides
        self._load_if_changed()

    def _policy_files(self) -> list[Path]:
        if not self._dir.exists():
            return []
        return sorted(self._dir.glob("*.yaml")) + sorted(self._dir.glob("*.yml"))

    def _load_if_changed(self) -> None:
        files = self._policy_files()
        current = {str(f): f.stat().st_mtime for f in files}
        if current == self._mtimes:
            return
        loaded: list[LoadedPolicy] = []
        for f in files:
            raw = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            policy_id = raw["policy_id"]
            if policy_id not in REGISTRY:
                raise ValueError(f"{f}: unknown policy_id {policy_id!r} (no evaluate() registered)")
            raw_params = raw.get("params", {}) or {}
            loaded.append(
                LoadedPolicy(
                    policy_id=policy_id,
                    priority=int(raw.get("priority", 100)),
                    enabled=bool(raw.get("enabled", True)),
                    applies_to=list(raw.get("applies_to", ["*"])),
                    description=str(raw.get("description", "")).strip(),
                    params=_convert_params(raw_params),
                    raw_params=raw_params,
                )
            )
        loaded.sort(key=lambda p: p.priority)
        with self._lock:
            self._policies = loaded
            self._mtimes = current

    @property
    def loaded_policies(self) -> list[LoadedPolicy]:
        """Read-only snapshot, for admin/debug endpoints."""
        with self._lock:
            return list(self._policies)

    def get_policy(self, policy_id: str) -> LoadedPolicy | None:
        """Read-only lookup by id, for admin endpoints. None if not loaded."""
        with self._lock:
            for lp in self._policies:
                if lp.policy_id == policy_id:
                    return lp
        return None

    def effective_params(self, policy_id: str) -> dict[str, Any] | None:
        """``policy_id``'s YAML params with its stored runtime override (if
        any) merged on top -- the exact same merge ``evaluate()`` applies
        per-request. ``None`` if ``policy_id`` isn't a loaded policy.
        """
        lp = self.get_policy(policy_id)
        if lp is None:
            return None
        override_raw = self._overrides.get(policy_id) if self._overrides is not None else {}
        return _merged_params(lp.params, override_raw)

    def evaluate(self, request: ToolCallRequest, state: StateStore, now: float) -> Decision:
        self._load_if_changed()
        with self._lock:
            policies = list(self._policies)
        # One overrides.all() read per evaluate() call, not per policy: cheap
        # for the demo (a handful of policies, admin-only write volume) and
        # avoids a cache-invalidation story. Swap for a short-TTL (~1s) cache
        # in front of `all()` if this ever needs to survive real request
        # volume against DynamoPolicyOverrides.
        overrides_all = self._overrides.all() if self._overrides is not None else {}

        t0 = time.perf_counter()
        trace: list[PolicyEvaluation] = []
        for lp in policies:
            if not lp.enabled or not _applies(lp.applies_to, request.tool):
                continue
            fn = REGISTRY[lp.policy_id]
            params = _merged_params(lp.params, overrides_all.get(lp.policy_id))
            ctx = PolicyContext(request=request, state=state, params=params, now=now)
            evaluation = fn(ctx)
            trace.append(evaluation)
            if evaluation.decision != "allow":
                return Decision(
                    decision=evaluation.decision,
                    policy_id=evaluation.policy_id,
                    reason=evaluation.reason,
                    evaluated_in_ms=(time.perf_counter() - t0) * 1000,
                    trace=tuple(trace),
                    escalate_unfreeze=evaluation.escalate_unfreeze,
                )

        return Decision(
            decision="allow",
            policy_id=None,
            reason="all applicable policies passed",
            evaluated_in_ms=(time.perf_counter() - t0) * 1000,
            trace=tuple(trace),
        )
