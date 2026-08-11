"""Real end-to-end Phase 1 execution started through the actual ingress path (task 7.1-7.3).

Unlike `live_run.py` (direct `StartExecution`, used for the API-contract path), this seeds one
synthetic applicant then enqueues on `kyc90-applicants.fifo` and lets the live EventBridge Pipe
start Phase 1 — proving both the Pipe's real payload shape (`NormalizeInput`) and the measured
ingress latency (A-05) against production infrastructure, not a mock.
"""

import json
import time
import uuid
from datetime import datetime, timezone

import boto3

REGION = "us-east-1"
ACCT = "633898796092"
PHASE1 = f"arn:aws:states:{REGION}:{ACCT}:stateMachine:kyc90-phase1"
QUEUE_URL = f"https://sqs.{REGION}.amazonaws.com/{ACCT}/kyc90-applicants.fifo"

ddb = boto3.client("dynamodb", region_name=REGION)
sfn = boto3.client("stepfunctions", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)

applicant = f"QLIVE-{uuid.uuid4().hex[:8].upper()}"
event_id = f"EVT-{uuid.uuid4().hex[:8].upper()}"

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
print("LIVE PHASE 1 RUN — VIA INGRESS QUEUE (tasks 7.1-7.3)")
print("=" * 78)
print("  applicant : %s  (synthetic)" % applicant)
print("  eventId   : %s" % event_id)

started = time.time()
sqs.send_message(
    QueueUrl=QUEUE_URL,
    MessageBody=json.dumps({"applicantId": applicant, "eventId": event_id}),
    MessageGroupId=applicant,
)
print("  enqueued at %.3f (epoch)" % started)

# Find the execution the Pipe started for this applicant.
execution = None
for _ in range(30):
    time.sleep(1)
    execs = sfn.list_executions(stateMachineArn=PHASE1, maxResults=10)
    for item in execs["executions"]:
        if applicant in item["name"]:
            execution = item["executionArn"]
            break
    if execution:
        break

if not execution:
    print("FAILED: no execution appeared within 30s of enqueue")
    raise SystemExit(1)

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
print("RESULT: %s in %.1fs (enqueue to terminal)" % (status, elapsed))
print("=" * 78)

events = []
paginator = sfn.get_paginator("get_execution_history")
for page in paginator.paginate(executionArn=execution, maxResults=1000):
    events.extend(page["events"])

print("\nSTATE PATH")
for e in events:
    detail = e.get("stateEnteredEventDetails")
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
                print("  %-28s %s" % (d.get("error", "?"), (d.get("cause") or "")[:220]))
                break

item = ddb.get_item(TableName="kyc90-cases",
                    Key={"applicantId": {"S": applicant}, "sk": {"S": "CASE"}}).get("Item", {})
print("\nCASE RECORD AFTER RUN (ingressLatencyMs is the load-bearing field here — task 7.3/A-05)")
for key in ("state", "phase1", "ingressLatencyMs", "phase2Pending", "idempotencyKey"):
    if key in item:
        print("  %-16s %s" % (key, json.dumps(item[key], default=str)[:300]))

print("\napplicant %s left in place for inspection" % applicant)
raise SystemExit(0 if status == "SUCCEEDED" else 1)
