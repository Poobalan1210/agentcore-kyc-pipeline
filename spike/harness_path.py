"""Task 5.8 — find the exact nested path to LifecycleConfiguration and SessionStorageConfiguration.

F-75 recorded that Harness has no LifecycleConfig. That was wrong: it is reachable, just not as
a top-level property. Print the path and the shape so it can actually be set.
"""
import json

schema = json.load(open("spike/harness_schema.json"))
defs = schema["definitions"]
props = schema["properties"]

TARGETS = {"LifecycleConfiguration", "SessionStorageConfiguration"}


def walk(node, path):
    """Yield (path, refname) for every $ref encountered."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                yield path, v.rsplit("/", 1)[-1]
            elif k == "properties" and isinstance(v, dict):
                for pk, pv in v.items():
                    yield from walk(pv, path + [pk])
            elif k == "items":
                yield from walk(v, path + ["[]"])
            else:
                yield from walk(v, path)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v, path)


# BFS over definitions to build parent -> child edges with property names
edges = []
for pk, pv in props.items():
    for path, ref in walk(pv, [pk]):
        edges.append((".".join(path), ref, None))
for dname, dbody in defs.items():
    for path, ref in walk(dbody, []):
        edges.append((".".join(path) or "(inline)", ref, dname))

print("=" * 74)
print("PATHS to the target definitions")
print("=" * 74)


def find_paths(target, prefix="", seen=None, depth=0):
    if seen is None:
        seen = set()
    if depth > 6:
        return
    # top-level
    for pk, pv in props.items():
        for path, ref in walk(pv, [pk]):
            if ref == target:
                print("  Harness.%s  ->  %s" % (".".join(path), target))
    # nested via a definition
    for dname, dbody in defs.items():
        for path, ref in walk(dbody, []):
            if ref == target:
                # which property leads to dname?
                for pk, pv in props.items():
                    for p2, r2 in walk(pv, [pk]):
                        if r2 == dname:
                            print("  Harness.%s (%s) . %s  ->  %s"
                                  % (".".join(p2), dname, ".".join(path) or "?", target))


for t in sorted(TARGETS):
    print("\n--- %s ---" % t)
    find_paths(t)

for t in sorted(TARGETS):
    print()
    print("=" * 74)
    print("SHAPE: %s" % t)
    print("=" * 74)
    print(json.dumps(defs[t], indent=2)[:1400])
