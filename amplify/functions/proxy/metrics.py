"""Live counters behind GET /metrics.

Money is tracked internally in integer paise (per repo-wide convention) and
only converted to rupees at the very end, in ``snapshot()`` — ``rupees_*`` is
a display-layer field name, the one place besides YAML params where INR is
allowed to appear, mirroring "INR ... in the dashboard's display layer" from
infra/CONTRACTS.md's header note.

``p95_overhead_ms`` is the proxy's own added latency (policy evaluation
time, i.e. ``Decision.evaluated_in_ms``) — never upstream call time, which is
Razorpay/MCP's latency, not this product's. It's computed from a bounded
reservoir so memory use doesn't grow with traffic.
"""

from __future__ import annotations

import threading
from collections import Counter, deque

_RESERVOIR_SIZE = 2000


class MetricsAccumulator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rupees_attempted_paise = 0
        self._rupees_moved_paise = 0
        self._calls_allowed = 0
        self._calls_denied = 0
        self._calls_escalated = 0
        self._false_blocks = 0
        self._per_policy_trips: Counter[str] = Counter()
        self._overhead_ms: deque[float] = deque(maxlen=_RESERVOIR_SIZE)

    def record_decision(
        self,
        *,
        decision: str,
        amount_paise: int,
        evaluated_in_ms: float,
        trace: tuple,
        labeled_legit: bool,
        executed: bool,
    ) -> None:
        with self._lock:
            self._rupees_attempted_paise += amount_paise
            if executed:
                self._rupees_moved_paise += amount_paise
            if decision == "allow":
                self._calls_allowed += 1
            elif decision == "deny":
                self._calls_denied += 1
            elif decision == "escalate":
                self._calls_escalated += 1
            if labeled_legit and decision != "allow":
                self._false_blocks += 1
            for evaluation in trace:
                if evaluation.decision != "allow":
                    self._per_policy_trips[evaluation.policy_id] += 1
            self._overhead_ms.append(evaluated_in_ms)

    def record_execution(self, amount_paise: int) -> None:
        """Approvals-queue path: money moves later, on approval, not at attempt time."""
        with self._lock:
            self._rupees_moved_paise += amount_paise

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
        return ordered[idx]

    def snapshot(self, *, approvals_pending: int, approvals_resolved: int) -> dict:
        with self._lock:
            return {
                "rupees_attempted": self._rupees_attempted_paise / 100,
                "rupees_moved": self._rupees_moved_paise / 100,
                "calls_allowed": self._calls_allowed,
                "calls_denied": self._calls_denied,
                "calls_escalated": self._calls_escalated,
                "false_blocks": self._false_blocks,
                "approvals_pending": approvals_pending,
                "approvals_resolved": approvals_resolved,
                "p95_overhead_ms": self._percentile(list(self._overhead_ms), 0.95),
                "per_policy_trip_counts": dict(self._per_policy_trips),
            }
