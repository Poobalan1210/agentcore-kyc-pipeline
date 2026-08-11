"""Probe every Phase 1 agent once and report whether its finding reached the case record.

Cheap by design: one invocation per agent against a shared synthetic applicant, then a single read
of the case record. A full Phase 1 execution costs five agent invocations plus orchestration and
takes ten minutes; this answers "did the contract land" in roughly one.
"""

import json
import subprocess
import sys
from pathlib import Path

import boto3

REGION = "us-east-1"
APPLICANT = "PROBE-0001"
ROOT = Path(__file__).resolve().parents[1]
AGENTS = ["router", "documents", "identity", "fraud", "prescreen"]
ATTRS = {
    "router": "findingRouter",
    "documents": "findingDocuments",
    "identity": "findingIdentity",
    "fraud": "findingFraud",
    "prescreen": "findingPrescreen",
}

ddb = boto3.client("dynamodb", region_name=REGION)

# Clear prior findings so a stale write cannot be mistaken for a fresh one.
existing = ddb.get_item(
    TableName="kyc90-cases",
    Key={"applicantId": {"S": APPLICANT}, "sk": {"S": "CASE"}}).get("Item")
if existing:
    present = [a for a in ATTRS.values() if a in existing]
    if present:
        ddb.update_item(
            TableName="kyc90-cases",
            Key={"applicantId": {"S": APPLICANT}, "sk": {"S": "CASE"}},
            UpdateExpression="REMOVE " + ", ".join(present),
        )
        print("cleared stale findings: %s\n" % ", ".join(present))

results = {}
for agent in AGENTS:
    proc = subprocess.run(
        [str(ROOT / ".venv/bin/python"), str(ROOT / "spike/probe_agent.py"), agent, APPLICANT],
        capture_output=True, text=True, timeout=400)
    out = proc.stdout
    stop = next((l.split(":", 1)[1].strip() for l in out.splitlines()
                 if l.startswith("stop")), "?")
    tools = next((l.split(":", 1)[1].strip() for l in out.splitlines()
                  if l.startswith("toolUse")), "0")
    failed = "FAILED" in out
    results[agent] = {"stop": stop, "toolUse": tools, "failed": failed}
    print("%-11s %s" % (agent, "FAILED" if failed else "stop=%s toolUse=%s" % (stop, tools)))
    if failed:
        for line in out.splitlines():
            if "error occurred" in line.lower():
                print("            %s" % line[:200])

item = ddb.get_item(
    TableName="kyc90-cases",
    Key={"applicantId": {"S": APPLICANT}, "sk": {"S": "CASE"}}).get("Item", {})

print()
print("=" * 74)
print("FINDINGS ON THE CASE RECORD")
print("=" * 74)
landed = 0
for agent, attr in ATTRS.items():
    raw = item.get(attr, {}).get("S")
    if raw:
        landed += 1
        print("  %-11s %s" % (agent, raw[:200]))
    else:
        print("  %-11s (absent)" % agent)

print()
print("RESULT: %d of %d agents recorded a finding" % (landed, len(ATTRS)))
with open(ROOT / "spike/probe_all.json", "w") as fh:
    json.dump({"agents": results, "landed": landed,
               "findings": {a: item.get(v, {}).get("S") for a, v in ATTRS.items()}},
              fh, indent=2, default=str)
sys.exit(0 if landed == len(ATTRS) else 1)
