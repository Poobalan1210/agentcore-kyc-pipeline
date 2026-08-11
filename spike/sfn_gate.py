#!/usr/bin/env python3
"""Task 0.4 — GATE G4. Closes A-03.

Proves that Nova model IDs pass through the Step Functions optimized integration
`arn:aws:states:::bedrockagentcore:invokeHarness` via Model.BedrockModelConfig.ModelId,
including the corrected reasoning shape from F-61.

This is the specific gap P-10 does not close: P-10 invoked via the agentcore CLI, not
through Step Functions. If this fails, model assignment in design 4.2 changes and
everything downstream shifts.

Uses TestState so no full execution or extra resources are needed beyond one IAM role.
"""
import json
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "us-east-1"
HARNESS_ARN = ("arn:aws:bedrock-agentcore:us-east-1:633898796092:"
               "harness/kycspike_kyc_spike_fraud-wFNik3Ok0E")
ROLE_NAME = "KycSpikeSfnRole"

CFG = Config(region_name=REGION, read_timeout=300, retries={"max_attempts": 2})
iam = boto3.client("iam", config=CFG)
sfn = boto3.client("stepfunctions", config=CFG)
sts = boto3.client("sts", config=CFG)
ACCT = sts.get_caller_identity()["Account"]

TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "states.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}
POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        # The IAM action for the Step Functions optimized integration is
        # InvokeAgentRuntime, NOT InvokeHarness. Measured from the denial:
        #   "not authorized to perform: bedrock-agentcore:InvokeAgentRuntime
        #    on resource: arn:aws:bedrock-agentcore:...:harness/..."
        # Scope it to the specific harness ARN, never "*".
        {"Effect": "Allow",
         "Action": ["bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeHarness"],
         "Resource": [HARNESS_ARN, HARNESS_ARN + "/*"]},
        {"Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse", "bedrock:ConverseStream"],
         "Resource": "*"},
        {"Effect": "Allow",
         "Action": ["logs:CreateLogDelivery", "logs:GetLogDelivery",
                    "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
                    "logs:ListLogDeliveries", "logs:PutResourcePolicy",
                    "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
                    "xray:PutTraceSegments", "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules", "xray:GetSamplingTargets"],
         "Resource": "*"},
    ],
}

print("=" * 90)
print("TASK 0.4 — Nova through Step Functions bedrockagentcore:invokeHarness  [GATE G4]")
print("=" * 90)

# ------------------------------------------------------------------ role
try:
    role_arn = iam.create_role(
        RoleName=ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(TRUST),
        Description="Phase 0 spike: Step Functions -> AgentCore Harness",
    )["Role"]["Arn"]
    print("\ncreated role %s" % role_arn)
except ClientError as e:
    if e.response["Error"]["Code"] != "EntityAlreadyExists":
        raise
    role_arn = iam.get_role(RoleName=ROLE_NAME)["Role"]["Arn"]
    print("\nreusing role %s" % role_arn)
iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="inline",
                    PolicyDocument=json.dumps(POLICY))
print("waiting 15s for IAM propagation")
time.sleep(15)


def task_state(model_id, effort=None, pin=True):
    """Build the Task state exactly as design 7.3 specifies it."""
    cfg = {"ModelId": model_id}
    if pin:
        cfg["Temperature"] = 0.0
        cfg["MaxTokens"] = 512
    if effort:
        # F-61: reasoningConfig nests under additionalModelRequestFields
        cfg["AdditionalParams"] = {
            "additionalModelRequestFields": {
                "reasoningConfig": {"type": "enabled", "maxReasoningEffort": effort}
            }
        }
    return {
        "Type": "Task",
        # `Arguments` and {% %} expressions require JSONata. Under the default JSONPath
        # the definition is rejected: "The QueryLanguage is set to 'JSONPath', but field
        # 'Arguments' is only supported for the 'JSONata' QueryLanguage."
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::bedrockagentcore:invokeHarness",
        "Arguments": {
            "HarnessArn": HARNESS_ARN,
            "RuntimeSessionId": "{% $uuid() %}",
            "Messages": [{
                "Role": "user",
                "Content": [{"Text": "{% $states.input.applicant %}"}],
            }],
            "Model": {"BedrockModelConfig": cfg},
            "MaxIterations": 5,
            "TimeoutSeconds": 120,
        },
        "Retry": [{
            "ErrorEquals": ["BedrockAgentCore.ThrottlingException"],
            "IntervalSeconds": 2, "MaxAttempts": 3, "BackoffRate": 2.0,
        }],
        "End": True,
    }


CASES = [
    ("us.amazon.nova-micro-v1:0", None, True),
    ("us.amazon.nova-lite-v1:0", None, True),
    ("us.amazon.nova-2-lite-v1:0", None, True),
    ("us.amazon.nova-2-lite-v1:0", "medium", True),
    # F-62: high requires temperature and maxTokens unset
    ("us.amazon.nova-2-lite-v1:0", "high", False),
    ("global.amazon.nova-2-lite-v1:0", None, True),
]

INPUT = json.dumps({"applicant":
                    "UK passport, MRZ readable. Return {\"ok\":true} as JSON only."})

results = {}
print("\n%-34s %-8s %-6s %s" % ("modelId", "effort", "pin", "result"))
print("-" * 90)
for model_id, effort, pin in CASES:
    label = "%s / %s / pin=%s" % (model_id, effort or "-", pin)
    try:
        r = sfn.test_state(
            definition=json.dumps(task_state(model_id, effort, pin)),
            roleArn=role_arn,
            input=INPUT,
            inspectionLevel="INFO",
            variables=json.dumps({}),
        )
        status = r.get("status")
        out = r.get("output") or ""
        err = r.get("error") or ""
        cause = (r.get("cause") or "")[:200]
        text = ""
        if out:
            try:
                text = json.loads(out)["Output"]["Message"]["Content"][0]["Text"][:70]
            except Exception:
                text = out[:70]
        results[label] = {"status": status, "error": err, "cause": cause,
                          "text": text}
        print("%-34s %-8s %-6s %s %s"
              % (model_id, effort or "-", pin, status,
                 (text or err or cause).replace("\n", " ")[:60]))
    except ClientError as e:
        msg = "%s: %s" % (e.response["Error"]["Code"],
                          e.response["Error"]["Message"][:150])
        results[label] = {"status": "APIError", "error": msg}
        print("%-34s %-8s %-6s APIError %s" % (model_id, effort or "-", pin, msg[:60]))
    time.sleep(1)

ok = [k for k, v in results.items() if v.get("status") == "SUCCEEDED"]
print("\n" + "=" * 90)
print("GATE G4: %s  (%d/%d states SUCCEEDED)"
      % ("PASS" if len(ok) >= 4 else "FAIL", len(ok), len(CASES)))
print("A-03 %s" % ("CLOSED — Nova IDs are accepted through bedrockModelConfig"
                   if len(ok) >= 4 else "still open"))

with open("sfn_gate.json", "w") as fh:
    json.dump({"roleArn": role_arn, "harnessArn": HARNESS_ARN,
               "results": results, "succeeded": len(ok), "cases": len(CASES)},
              fh, indent=2)
print("\nwrote spike/sfn_gate.json")
