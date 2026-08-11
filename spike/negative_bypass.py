"""Task 5.5 / AC-09 — the direct-API negative test.

Attempt the two bypasses for real, from a principal that is not the Gateway and not Step
Functions, and record what actually happens. A denial here is evidence; an allow is the finding.
"""
import json

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
ACCT = "633898796092"
HARNESS = "arn:aws:bedrock-agentcore:us-east-1:633898796092:harness/kyc90_router-6oEXh6o2ax"

sts = boto3.client("sts")
who = sts.get_caller_identity()
print("calling principal:", who["Arn"])
print()

results = {"principal": who["Arn"], "tests": []}


def record(name, expect, fn):
    print("=" * 74)
    print(name)
    print("  expectation:", expect)
    try:
        fn()
        outcome, detail = "ALLOWED", "call succeeded"
    except ClientError as e:
        code = e.response["Error"]["Code"]
        outcome = "DENIED" if "AccessDenied" in code or code == "AccessDeniedException" \
            else "ERROR:" + code
        detail = e.response["Error"].get("Message", "")[:200]
    except Exception as e:  # noqa: BLE001
        outcome, detail = "ERROR:" + type(e).__name__, str(e)[:200]
    print("  outcome    :", outcome)
    print("  detail     :", detail)
    results["tests"].append(
        {"name": name, "expectation": expect, "outcome": outcome, "detail": detail})


# 1. Direct tool Lambda invoke, skipping the Gateway entirely. This is the bypass that would
#    make Cedar advisory: if it succeeds, a tool ran with no policy evaluation.
def direct_lambda():
    boto3.client("lambda", region_name=REGION).invoke(
        FunctionName="kyc90-tools:live",
        Payload=json.dumps({"caseId": "BYPASS-TEST", "jurisdiction": "us"}).encode(),
    )


record("1. Direct tool Lambda invoke (bypasses Gateway, so bypasses Cedar)",
       "DENIED for any principal other than kyc90-gateway", direct_lambda)


# 2. Direct harness invoke, skipping Step Functions. F-70: the action is InvokeAgentRuntime.
def direct_harness():
    # F-64 + F-70 together: the data-plane SDK operation is InvokeHarness, while the IAM action
    # it authorizes against is bedrock-agentcore:InvokeAgentRuntime. Calling invoke_agent_runtime
    # with a harness ARN returns ResourceNotFound for every qualifier, which would leave this
    # test inconclusive and read as a denial if not checked.
    boto3.client("bedrock-agentcore", region_name=REGION).invoke_harness(
        harnessArn=HARNESS,
        qualifier="live",
        runtimeSessionId="bypass-test-session-000000000000000000000000000000",
        messages=[{"role": "user", "content": [{"text": "bypass test"}]}],
    )


record("2. Direct harness InvokeAgentRuntime (bypasses Step Functions)",
       "DENIED for any principal other than kyc90-statemachine", direct_harness)

with open("spike/negative_bypass.json", "w") as f:
    json.dump(results, f, indent=2, default=str)
print()
print("wrote spike/negative_bypass.json")
print()
print("NOTE: this build user is an IAM user with broad permissions, so an ALLOW here does not")
print("contradict the audit in audit_bypass.py - that proves no *workload* role can bypass.")
print("It does, however, demonstrate exactly the residual risk F-82 describes: in a single")
print("account, resource policy cannot deny an in-account principal, so the boundary rests on")
print("least privilege plus not handing out lambda:InvokeFunction. State it, do not claim more.")
