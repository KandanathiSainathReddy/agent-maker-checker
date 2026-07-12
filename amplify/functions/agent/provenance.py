"""Deterministic provenance/taint tracking — the harness's job, not the model's.

This is the load-bearing piece of Phase 3: the LLM is never asked to decide
"is this value trustworthy?" (a model can be talked out of a safety
judgment; a substring scan cannot). Instead the harness itself remembers
every piece of untrusted free text it has fetched this session (support
ticket subjects/bodies — see ``tickets.py``) and, immediately before any
proxied tool call, mechanically checks whether a proposed argument value
was lifted verbatim from that text. A hit becomes a
``context.provenance`` entry shaped exactly like ``infra/CONTRACTS.md`` §1:

    {"source": "ticket:4471", "trusted": false, "tainted_fields": ["arguments.payee"]}

Values that only ever appeared in the operator's own task text (the prompt
that kicked off this run) are never recorded as untrusted, so they carry no
taint — only text this session actually read *from a ticket* can taint an
argument.

The enforcement decision itself is made downstream by the proxy's
``provenance_check`` policy (``amplify/functions/proxy/policies_impl/
provenance_check.py``); this module's only job is honest bookkeeping of
where each value came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Ignore trivially short values (currency codes, single digits, "INR", ...)
# when scanning for taint — a 1-3 character match against a paragraph of
# ticket text is noise, not evidence a value was lifted from it.
MIN_TAINT_LEN = 4


@dataclass
class ProvenanceEntry:
    source: str
    trusted: bool
    tainted_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "trusted": self.trusted,
            "tainted_fields": list(self.tainted_fields),
        }


class ProvenanceTracker:
    """Remembers untrusted text read this session; scans proposed tool
    arguments against it before they're allowed to reach the proxy.
    """

    def __init__(self) -> None:
        self._untrusted: dict[str, str] = {}

    def record_untrusted(self, source: str, text: str) -> None:
        """Remember a piece of untrusted free text this session (e.g. a ticket body).

        Idempotent per ``source``: re-fetching the same ticket just refreshes it.
        """
        if text:
            self._untrusted[source] = text

    @property
    def untrusted_sources(self) -> tuple[str, ...]:
        return tuple(self._untrusted)

    def scan_arguments(self, arguments: dict[str, Any]) -> list[ProvenanceEntry]:
        """Scan proposed tool-call arguments for values lifted from untrusted text.

        Returns one ``ProvenanceEntry`` per untrusted source that contributed
        at least one tainted field, with ``tainted_fields`` listing every
        dotted path (``"arguments.payee"``, ``"arguments.notes.upi"``) whose
        value appears verbatim inside that source's recorded text.
        """
        entries: dict[str, ProvenanceEntry] = {}
        if not self._untrusted:
            return []

        for field_path, value in _iter_leaf_fields(arguments):
            text = _stringify(value)
            if text is None or len(text) < MIN_TAINT_LEN:
                continue
            for source, body in self._untrusted.items():
                if text in body:
                    entry = entries.get(source)
                    if entry is None:
                        entry = ProvenanceEntry(source=source, trusted=False)
                        entries[source] = entry
                    if field_path not in entry.tainted_fields:
                        entry.tainted_fields.append(field_path)

        return list(entries.values())


def _stringify(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    return None


def _iter_leaf_fields(arguments: dict[str, Any], prefix: str = "arguments"):
    """Yield ``(dotted_path, value)`` for every leaf (non-dict) value, recursing
    one or more levels into nested objects (e.g. ``notes``).
    """
    for key, value in arguments.items():
        path = f"{prefix}.{key}"
        if isinstance(value, dict):
            yield from _iter_leaf_fields(value, path)
        else:
            yield path, value
