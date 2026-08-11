"""Puts this directory on `sys.path` so the test modules can share `builders.py`.

Needed because the suite runs under `--import-mode=importlib` (set in the repo pytest.ini so that
`infra/tests` and `tools/tests`, both packages named `tests`, stop shadowing each other). That
mode keys modules by full path and does not add the test directory to `sys.path`, so a plain
`from engine_builders import prelim` would not resolve.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
