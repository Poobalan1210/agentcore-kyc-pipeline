"""Minimal unused-import check for the decision engine — no linter is installed in this venv."""
import ast
import pathlib
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
bad = 0
for path in sorted(root.rglob("*.py")):
    if "__pycache__" in str(path):
        continue
    src = path.read_text()
    tree = ast.parse(src)
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[(a.asname or a.name).split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    imported[a.asname or a.name] = node.lineno
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    used |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            cur = node
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                used.add(cur.id)
    # names appearing in string annotations / docstrings are ignored deliberately
    for name, lineno in sorted(imported.items(), key=lambda kv: kv[1]):
        if name not in used and name != "annotations":
            print("%s:%d unused import %s" % (path, lineno, name))
            bad += 1
print("\n%d unused import(s)" % bad)
sys.exit(1 if bad else 0)
