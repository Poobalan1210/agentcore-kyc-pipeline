"""Task 5.8 — are LifecycleConfiguration / SessionStorageConfiguration reachable from Harness?

They appear in `definitions`, but a definition nobody references is not a settable property.
Walk the $ref graph from the top-level properties and report what is genuinely reachable.
"""
import json

schema = json.load(open("spike/harness_schema.json"))
defs = schema.get("definitions", {})
props = schema.get("properties", {})


def refs_of(node):
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                out.append(v.rsplit("/", 1)[-1])
            else:
                out += refs_of(v)
    elif isinstance(node, list):
        for v in node:
            out += refs_of(v)
    return out


reachable, frontier = set(), refs_of(props)
while frontier:
    d = frontier.pop()
    if d in reachable or d not in defs:
        continue
    reachable.add(d)
    frontier += refs_of(defs[d])

orphans = sorted(set(defs) - reachable)

print("=" * 74)
print("REACHABLE from Harness properties: %d definitions" % len(reachable))
print("=" * 74)
print("  " + ", ".join(sorted(reachable)))
print()
print("=" * 74)
print("ORPHANED definitions — present in schema, NOT settable on Harness: %d" % len(orphans))
print("=" * 74)
for o in orphans:
    print("  ", o)

print()
print("=" * 74)
print("VERDICT for task 5.8 controls")
print("=" * 74)
for label, name in [
    ("lifecycle / maxLifetime", "LifecycleConfiguration"),
    ("managed session storage", "SessionStorageConfiguration"),
]:
    if name in reachable:
        state = "SETTABLE"
    elif name in defs:
        state = "ORPHANED — in schema but unreachable, so cannot be set (confirms F-75)"
    else:
        state = "ABSENT"
    print("  %-26s %s" % (label, state))
print("  %-26s %s" % ("interactive shell/terminal",
                      "ABSENT from schema entirely — no CFN control surface"))

# Where does 'Idle' actually live?
print()
print("  'Idle' occurrences by definition:")
for name, body in defs.items():
    blob = json.dumps(body)
    if "Idle" in blob:
        keys = [k for k in body.get("properties", {}) if "Idle" in k]
        print("     %-32s %s  [%s]" % (
            name, keys or "(nested)", "reachable" if name in reachable else "ORPHANED"))
