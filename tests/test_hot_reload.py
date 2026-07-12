"""Hot reload: changing a cap on disk takes effect on the next evaluation,
with no engine restart and no test sleeping (the new mtime is set explicitly
ahead of the original, rather than waiting on real wall-clock time).
"""

import os
import shutil

from proxy.engine import PolicyEngine
from proxy.state import InMemoryStateStore

NOW = 1_700_000_000.0


def test_lowering_cap_on_disk_is_picked_up_on_next_evaluation(tmp_path, policy_dir, make_request):
    for f in policy_dir.glob("*.yaml"):
        shutil.copy(f, tmp_path / f.name)

    engine = PolicyEngine(tmp_path)
    cap_path = tmp_path / "per_call_amount_cap.yaml"
    original_mtime = cap_path.stat().st_mtime

    # ₹1,20,000: under the shipped ₹1,50,000 default cap (allow), but will be
    # over a lowered ₹1,00,000 cap (deny).
    req = make_request(tool="pay_vendor", amount=12_000_000)
    result = engine.evaluate(req, InMemoryStateStore(), NOW)
    assert result.decision == "allow"

    contents = cap_path.read_text(encoding="utf-8")
    assert "default_cap_inr: 150000" in contents
    contents = contents.replace("default_cap_inr: 150000", "default_cap_inr: 100000")
    cap_path.write_text(contents, encoding="utf-8")
    # Force a strictly later mtime without sleeping, so the cheap-stat
    # hot-reload check (which compares mtimes) reliably observes the change
    # even on filesystems with coarse mtime resolution.
    new_mtime = original_mtime + 5
    os.utime(cap_path, (new_mtime, new_mtime))

    result = engine.evaluate(req, InMemoryStateStore(), NOW)
    assert result.decision == "deny"
    assert result.policy_id == "per_call_amount_cap"
