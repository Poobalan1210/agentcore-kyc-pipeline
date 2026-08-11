"""Path setup for the audit suite, mirroring `decision-engine/tests/conftest.py`.

The repo pytest.ini runs `--import-mode=importlib` so that the several directories named `tests`
stop shadowing each other. That mode keys modules by full path and adds neither the test directory
nor its parent to `sys.path`, so `from audit_builders import ...` and `from record import ...` would not
resolve on their own. Inserting the audit package root here as well as this directory keeps the
suite runnable before `audit` is added to the pytest.ini `pythonpath`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_here = Path(__file__).resolve().parent
for path in (_here, _here.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
