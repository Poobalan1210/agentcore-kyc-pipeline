"""Real end-to-end Phase 1 execution against the live account.

Seeds one synthetic applicant, starts `kyc90-phase1`, waits, then prints the state-by-state path
the execution actually took plus the decision it reached. Synthetic attributes only (R17.7).
"""

import json
import sys
import time
import uuid
from datetime import datetime, timezone

import boto3

REGION = "us-east-1"
ACCT = "633898796092"
PHASE1 = f"arn:aws:states:{REGION}:{ACCT}:stateMachine:kyc90-phase1"

ddb = boto3.client("dynamodb", region_name=REGION)
sfn = boto3.client("stepfunctions", region_name=REGION)

applicant = f"LIVE-{uuid.uuid4().hex[:8].upper()}"
event_id = f"EVT-{uuid.uuid4().hex[:8].upper()}"

# A clean, low-risk synthetic applicant: liveness passed, one document, LOW jurisdiction risk.
# Expected outcome is PROVISIONAL_APPROVE_RESTRICTED.
ddb.put_item(TableName="kyc90-cases", Item={
    "applicantId": {"S": applicant},
    "sk": {"S": "CASE"},
    "caseId": {"S": f"CASE-{applicant}"},
    "state": {"S": "IN_PROGRESS"},
    "jurisdiction": {"S": "us"},
    "jurisdictionRisk": {"S": "LOW"},
    "productApplied": {"S": "current_account"},
    "policyEngineVersion": {"S": "1"},
    "gatewayArn": {"S": f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:gateway/kycgateway-us"},
    "liveness": {"M": {"result": {"S": "PASS"}, "region": {"S": REGION},
                       "retrievedAt": {"S": datetime.now(timezone.utc).isoformat()}}},
    "documents": {"L": [{"M": {
        "type": {"S": "PASSPORT"},
        "key": {"S": "synthetic/passport-us-001.png"},
        "issuingCountry": {"S": "US"},
        "bucket": {"S": f"kyc90-evidence-us-{ACCT}"},
    }}]},
})
print("=" * 78)
print("LIVE PHASE 1 RUN")
print("=" * 78)
print("  applicant : %s  (synthetic)" % applicant)
print("  eventId   : %s" % event_id)

started = time.time()
execution = sfn.start_execution(
    stateMachineArn=PHASE1,
    name=f"{applicant}-{event_id}",
    input=json.dumps({"applicantId": applicant, "eventId": event_id}),
)["executionArn"]
print("  execution : %s" % execution.rsplit(":", 1)[-1])
print()

status = "RUNNING"
while status == "RUNNING" and time.time() - started < 600:
    time.sleep(5)
    described = sfn.describe_execution(executionArn=execution)
    status = described["status"]
    print("  ... %-9s %5.1fs" % (status, time.time() - started))

elapsed = time.time() - started
print()
print("=" * 78)
print("RESULT: %s in %.1fs" % (status, elapsed))
print("=" * 78)

# The path actually taken, which is the thing worth seeing.
events = []
paginator = sfn.get_paginator("get_execution_history")
for page in paginator.paginate(executionArn=execution, maxResults=1000):
    events.extend(page["events"])

print("\nSTATE PATH")
for e in events:
    detail = e.get("stateEnteredEventDetails") or e.get("stateExitedEventDetails")
    if detail and e["type"].endswith("StateEntered"):
        print("  -> %s" % detail["name"])

failures = [e for e in events if "Failed" in e["type"]]
if failures:
    print("\nFAILURES (%d)" % len(failures))
    for e in failures[:8]:
        for key in ("taskFailedEventDetails", "executionFailedEventDetails",
                    "lambdaFunctionFailedEventDetails"):
            d = e.get(key)
            if d:
                print("  %-28s %s" % (d.get("error", "?"), (d.get("cause") or "")[:180]))
                break

final = [e for e in events if e["type"] in
         ("ExecutionSucceeded", "ExecutionFailed", "ExecutionAborted")]
if final:
    d = (final[-1].get("executionSucceededEventDetails")
         or final[-1].get("executionFailedEventDetails") or {})
    print("\nFINAL OUTPUT\n  %s" % json.dumps(d, default=str)[:900])

item = ddb.get_item(TableName="kyc90-cases",
                    Key={"applicantId": {"S": applicant}, "sk": {"S": "CASE"}}).get("Item", {})
print("\nCASE RECORD AFTER RUN")
for key in ("state", "phase1", "phase1Evidence", "phase2Pending", "idempotencyKey"):
    if key in item:
        print("  %-16s %s" % (key, json.dumps(item[key], default=str)[:260]))

with open("spike/live_run.json", "w") as fh:
    json.dump({"applicant": applicant, "execution": execution, "status": status,
               "elapsed_seconds": round(elapsed, 1),
               "path": [ (e.get("stateEnteredEventDetails") or {}).get("name")
                         for e in events if e["type"].endswith("StateEntered")],
               "failure_count": len(failures)}, fh, indent=2, default=str)
print("\nwrote spike/live_run.json")
print("applicant %s left in place for inspection" % applicant)
sys.exit(0 if status == "SUCCEEDED" else 1)
