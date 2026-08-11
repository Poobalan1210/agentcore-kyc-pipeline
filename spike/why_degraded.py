"""Which branch degraded, and what each agent actually returned. Reads the last live execution."""
import json

import boto3

REGION = "us-east-1"
sfn = boto3.client("stepfunctions", region_name=REGION)

run = json.load(open("spike/live_run.json"))
arn = run["execution"]
print("execution:", arn.rsplit(":", 1)[-1])

events = []
for page in sfn.get_paginator("get_execution_history").paginate(executionArn=arn,
                                                               maxResults=1000):
    events.extend(page["events"])

print()
print("=" * 78)
print("FAILURES")
print("=" * 78)
for e in events:
    for key in ("taskFailedEventDetails", "lambdaFunctionFailedEventDetails",
                "executionFailedEventDetails", "taskTimedOutEventDetails"):
        d = e.get(key)
        if d:
            print("  id=%s %s" % (e["id"], e["type"]))
            print("    error: %s" % d.get("error"))
            print("    cause: %s" % (d.get("cause") or "")[:600])

print()
print("=" * 78)
print("EACH AGENT'S RETURNED TEXT")
print("=" * 78)
for e in events:
    d = e.get("taskSucceededEventDetails") or e.get("taskExitedEventDetails")
    if not d or not d.get("output"):
        continue
    try:
        out = json.loads(d["output"])
    except json.JSONDecodeError:
        continue
    content = (((out.get("Output") or {}).get("Message") or {}).get("Content") or [])
    text = " ".join(c.get("Text", "") for c in content if isinstance(c, dict))
    if text:
        print("\n  --- id=%s ---" % e["id"])
        print("  %s" % text.replace("\n", "\n  ")[:900])

print()
print("=" * 78)
print("DEGRADED BRANCH OUTPUT (what PreliminaryTier saw)")
print("=" * 78)
for e in events:
    d = e.get("stateExitedEventDetails")
    if d and d.get("name") == "Phase1Parallel":
        out = json.loads(d["output"]) if d.get("output") else []
        for i, branch in enumerate(out if isinstance(out, list) else []):
            s = json.dumps(branch, default=str)
            marker = "  <-- DEGRADED" if '"DEGRADED"' in s else ""
            print("  branch %d: %s%s" % (i, s[:220], marker))
