"""Shared fixtures for the agent test suite.

``amplify/functions/pytest.ini`` sets ``pythonpath = .`` (amplify/functions)
and ``testpaths = proxy/tests``, so this suite is invoked explicitly:

    cd amplify/functions
    pytest agent/tests -q

Running pytest that way still discovers and applies ``pytest.ini`` (it's the
nearest ini file above the given testpath), so ``pythonpath = .`` already
puts amplify/functions on ``sys.path`` and both ``agent.*`` and ``proxy.*``
import cleanly. The belt-and-suspenders insert below only matters if this
suite is ever invoked from somewhere that ini discovery doesn't reach it
(e.g. ``pytest`` run directly from inside ``agent/tests``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_FUNCTIONS_DIR = Path(__file__).resolve().parents[2]  # amplify/functions
if str(_FUNCTIONS_DIR) not in sys.path:
    sys.path.insert(0, str(_FUNCTIONS_DIR))
