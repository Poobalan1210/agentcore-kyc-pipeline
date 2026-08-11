"""api/ suite conftest.

Deliberately does no sys.path manipulation. The root pytest.ini already puts `api/` on
`pythonpath`, which is sufficient for `import liveness` / `import applicants` / `import
review_api` to resolve. Unlike `review/tests/conftest.py` and `audit/tests/conftest.py`, this
suite has no shared test-helper module that other suites import by name (`from conftest import
...`), so there is nothing here that needs to be importable outside pytest's own conftest
mechanism — adding sys.path entries would only risk the same-basename collision the root
pytest.ini's docstring warns about, for no benefit to this suite.
"""

from __future__ import annotations
