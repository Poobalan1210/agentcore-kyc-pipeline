#!/usr/bin/env python3
"""Inspect the synthesised harness template before deploying.

The postures asserted here are the DIV-2 inversions and the F-62 constraint. They are cheap to
get wrong and expensive to discover later: memory left on would retain passport data for 30
days, and pinning temperature on the high-effort agent would fail every invocation.
"""
import json
import sys

t = json.load(open("cdk.out.h/KycHarnesses.template.json"))
r = t["Resources"]

harnesses = {k: v for k, v in r.items()
             if v["Type"] == "AWS::BedrockAgentCore::Harness"}
endpoints = {k: v for k, v in r.items()
             if v["Type"] == "AWS::BedrockAgentCore::HarnessEndpoint"}

fails = []


def check(name, ok, detail=""):
    print("  %-4s %-54s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        fails.append(name)


print("=" * 92)
print("Synthesised harness template")
print("=" * 92)
check("seven harnesses", len(harnesses) == 7, str(len(harnesses)))
check("seven pinned endpoints", len(endpoints) == 7, str(len(endpoints)))

for k, v in sorted(harnesses.items()):
    p = v["Properties"]
    name = p["HarnessName"]
    mem = p.get("Memory")
    model = p["Model"]["BedrockModelConfig"]
    allowed = p.get("AllowedTools", [])
    tool_types = [x["Type"] for x in p.get("Tools", [])]

    is_notify = name.endswith("_notify")
    check(f"{name}: name <= 40 chars, underscore style",
          len(name) <= 40 and "-" not in name, name)
    check(f"{name}: TimeoutSeconds 900 (F-75 bound)",
          p.get("TimeoutSeconds") == 900, str(p.get("TimeoutSeconds")))
    check(f"{name}: Skills empty (R11.17)", p.get("Skills") == [], str(p.get("Skills")))
    check(f"{name}: per-agent spans on (F-20)",
          p["EnvironmentVariables"].get("UNIFIED_TRACES_DESTINATION_ENABLED") == "true")
    check(f"{name}: gateway tool attached",
          "agentcore_gateway" in tool_types, str(tool_types))
    check(f"{name}: shell and file_operations excluded (F-06)",
          all("shell" not in a and "file_operations" not in a for a in allowed)
          and "*" not in allowed, str(allowed))

    if is_notify:
        # The one exception. It sees an outcome and a restriction set, never a document.
        check(f"{name}: memory ENABLED (the documented exception)",
              mem is None, "no Memory block -> default on")
    else:
        check(f"{name}: memory DISABLED (DIV-2)",
              mem == {"Disabled": {}}, str(mem))

    if "compliance" in name:
        # F-62: at high effort the service rejects Temperature and MaxTokens outright.
        check(f"{name}: sampling NOT pinned (F-62 requires unset at high effort)",
              "Temperature" not in model and "MaxTokens" not in model, str(model))
    else:
        check(f"{name}: sampling pinned for reproducibility",
              model.get("Temperature") == 0, str(model.get("Temperature")))

    if "fraud" in name:
        check(f"{name}: code interpreter present and explicitly allowed",
              "agentcore_code_interpreter" in tool_types
              and any("code_interpreter" in a for a in allowed),
              "%s / %s" % (tool_types, allowed))
    else:
        check(f"{name}: no code interpreter",
              "agentcore_code_interpreter" not in tool_types, str(tool_types))

print("\n%d checks failed" % len(fails))
for f in fails:
    print("  ", f)
sys.exit(1 if fails else 0)
