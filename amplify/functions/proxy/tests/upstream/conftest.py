"""Make the repo root importable as a namespace package root.

`proxy/` and `proxy/upstream/` have no __init__.py (they're PEP 420 implicit
namespace packages -- proxy/upstream/base.py and __init__.py are Agent A's
files, not this workstream's). pytest's default "prepend" import mode would
otherwise insert only tests/upstream/ onto sys.path for this directory
(since it's the first ancestor without an __init__.py), which is not enough
to `import proxy...`. Insert the actual repo root explicitly instead, so
`pytest -q tests/upstream` works standalone with plain pytest and no extra
ini/config.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
