"""Local ticket fixtures — ``get_ticket`` (infra/CONTRACTS.md §1 tool vocabulary)
is answered entirely from disk, never via the enforcement proxy: a ticket
lookup moves no money and needs no policy decision.

Every ticket's ``subject`` and ``body`` are customer-authored free text —
untrusted by construction, since anyone who can open a support ticket can
write anything into them. Callers are responsible for feeding that text to a
``provenance.ProvenanceTracker`` (``worker.py`` does this for every
``get_ticket`` call).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "tickets"


def load_ticket(ticket_id: str) -> dict[str, Any] | None:
    """Load one ticket fixture by id, or ``None`` if no such ticket exists."""
    safe_id = str(ticket_id).strip()
    path = FIXTURES_DIR / f"{safe_id}.json"
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def untrusted_text(ticket: dict[str, Any]) -> str:
    """The customer-authored portion of a ticket — subject + body — treated as
    untrusted free text for provenance scanning.
    """
    return f"{ticket.get('subject', '')}\n{ticket.get('body', '')}"


def list_ticket_ids() -> list[str]:
    """All ticket ids available in the fixture store, for tests/tooling."""
    if not FIXTURES_DIR.is_dir():
        return []
    return sorted(p.stem for p in FIXTURES_DIR.glob("*.json"))
