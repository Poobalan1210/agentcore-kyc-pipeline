"""Rename per-suite test helpers to unique module names.

Three suites each added a `tests/builders.py`. Under `--import-mode=importlib` each conftest
inserts its own directory at `sys.path[0]`, so whichever suite pytest collects first wins the bare
name `builders` and the others import the wrong module. Audit sorts before decision-engine, so the
decision-engine suite broke the moment the audit suite arrived.

Unique names per suite remove the ambiguity rather than depending on collection order.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

RENAMES = [
    (ROOT / "audit/tests/builders.py", ROOT / "audit/tests/audit_builders.py",
     "audit_builders", ROOT / "audit/tests"),
    (ROOT / "decision-engine/tests/builders.py",
     ROOT / "decision-engine/tests/engine_builders.py",
     "engine_builders", ROOT / "decision-engine/tests"),
]

for src, dst, new_name, suite_dir in RENAMES:
    if src.exists():
        src.rename(dst)
        print("renamed %s -> %s" % (src.name, dst.name))
    for path in sorted(suite_dir.glob("*.py")):
        text = path.read_text()
        updated = re.sub(r"\bfrom builders import\b", f"from {new_name} import", text)
        updated = re.sub(r"\bimport builders\b", f"import {new_name}", updated)
        if updated != text:
            path.write_text(updated)
            print("  updated imports in", path.name)

print("\nremaining bare-builders references:")
for path in ROOT.rglob("tests/*.py"):
    if "cdk.out" in str(path) or ".venv" in str(path):
        continue
    if re.search(r"\bfrom builders import\b|\bimport builders\b", path.read_text()):
        print("  ", path)
