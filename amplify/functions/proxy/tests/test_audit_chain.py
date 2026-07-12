"""Audit hash chain: verify_chain() passes on an untampered log and fails
after tampering with a single entry, for both AuditLog backends' shared
verification logic (exercised here via JsonlAuditLog, the local default).
"""

import dataclasses
import json

from proxy.audit import JsonlAuditLog


def test_verify_passes_on_untampered_chain(tmp_path):
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append(
            request_id=f"req_{i}", agent_id="agent-1", tool="pay_vendor",
            arguments={"amount": 1000 * i}, decision="allow", policy_id=None,
            reason="ok", now=1_700_000_000.0 + i,
        )
    ok, error = log.verify_chain()
    assert ok is True
    assert error is None


def test_verify_fails_after_tampering_one_entry(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = JsonlAuditLog(path)
    for i in range(5):
        log.append(
            request_id=f"req_{i}", agent_id="agent-1", tool="pay_vendor",
            arguments={"amount": 1000 * i}, decision="allow", policy_id=None,
            reason="ok", now=1_700_000_000.0 + i,
        )
    assert log.verify_chain() == (True, None)

    # Tamper with the reason of the third entry, in place, without touching its hash.
    lines = path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[2])
    row["reason"] = "not the original reason"
    lines[2] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tampered_log = JsonlAuditLog(path)
    ok, error = tampered_log.verify_chain()
    assert ok is False
    assert error is not None
    assert "seq 3" in error


def test_each_entry_embeds_the_previous_hash(tmp_path):
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    e1 = log.append(
        request_id="r1", agent_id="a", tool="pay_vendor", arguments={}, decision="allow",
        policy_id=None, reason="ok", now=1.0,
    )
    e2 = log.append(
        request_id="r2", agent_id="a", tool="pay_vendor", arguments={}, decision="allow",
        policy_id=None, reason="ok", now=2.0,
    )
    assert e2.prev_hash == e1.hash
    assert dataclasses.is_dataclass(e1)
