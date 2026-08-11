"""Exactly what the Fraud task returned on the last live run."""
import json

import boto3

sfn = boto3.client("stepfunctions", region_name="us-east-1")
run = json.load(open("spike/live_run.json"))

events = []
for page in sfn.get_paginator("get_execution_history").paginate(
        executionArn=run["execution"], maxResults=1000):
    events.extend(page["events"])

# Find the Fraud state's scheduled -> exited window and print everything in it.
fraud_ids = set()
for e in events:
    for key in ("stateEnteredEventDetails", "stateExitedEventDetails"):
        d = e.get(key)
        if d and d.get("name") in ("Fraud", "FraudCheck", "FraudDegraded"):
            fraud_ids.add(e["id"])
            print("id=%-4s %-28s %s" % (e["id"], e["type"], d.get("name")))
            if d.get("output"):
                print("   output: %s" % d["output"][:1200])
            if d.get("input"):
                print("   input : %s" % d["input"][:400])

print()
print("=" * 78)
print("TASK-LEVEL EVENTS mentioning fraud")
print("=" * 78)
for e in events:
    for key in ("taskScheduledEventDetails", "taskSucceededEventDetails",
                "taskFailedEventDetails", "taskTimedOutEventDetails",
                "taskStartFailedEventDetails"):
        d = e.get(key)
        if not d:
            continue
        blob = json.dumps(d, default=str)
        if "fraud" in blob.lower():
            print("\nid=%s %s" % (e["id"], e["type"]))
            for f in ("error", "cause", "output", "parameters", "resource"):
                if d.get(f):
                    print("   %-10s %s" % (f, str(d[f])[:1100]))
