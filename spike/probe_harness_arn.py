"""Which HarnessArn form does the Step Functions invokeHarness integration accept?

Gate G9 rejected all seven agent Tasks with "Invalid harness ARN format". The state machine passes
the HarnessEndpoint physical id, which CloudFormation renders as
`.../harness/{name}-{id}/harness-endpoint/live`, while design §7.3's example shows
`.../harness/{name}/endpoint/{version}`. Establish the accepted form by trying each against the
live service rather than guessing from the example.
"""

import json

import boto3

REGION = "us-east-1"
ACCT = "633898796092"
ROLE = f"arn:aws:iam::{ACCT}:role/kyc90-statemachine"
HARNESS = f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}:harness/kyc90_router-6oEXh6o2ax"

sfn = boto3.client("stepfunctions", region_name=REGION)

CANDIDATES = [
    ("plain harness ARN", HARNESS, None),
    ("plain harness ARN + Qualifier", HARNESS, "live"),
    ("harness-endpoint suffix (what we deploy)", f"{HARNESS}/harness-endpoint/live", None),
    ("endpoint suffix (design 7.3 example)", f"{HARNESS}/endpoint/live", None),
    ("runtime-endpoint suffix", f"{HARNESS}/runtime-endpoint/live", None),
]

print("=" * 84)
print("HarnessArn forms against arn:aws:states:::bedrockagentcore:invokeHarness")
print("=" * 84)
results = []
for label, arn, qualifier in CANDIDATES:
    args = {
        "HarnessArn": arn,
        "RuntimeSessionId": "g9-probe-session-000000000000000000000000",
        "Messages": [{"Role": "user", "Content": [{"Text": "ping"}]}],
        "MaxIterations": 1,
        "TimeoutSeconds": 30,
    }
    if qualifier:
        args["Qualifier"] = qualifier
    state = {
        "Type": "Task",
        "QueryLanguage": "JSONata",
        "Resource": "arn:aws:states:::bedrockagentcore:invokeHarness",
        "Arguments": args,
        "End": True,
    }
    try:
        response = sfn.test_state(
            definition=json.dumps(state), roleArn=ROLE, input="{}", inspectionLevel="INFO")
        status = response.get("status")
        error = response.get("error") or ""
        cause = (response.get("cause") or "")[:150]
    except Exception as exc:  # noqa: BLE001
        status, error, cause = "CALL_FAILED", type(exc).__name__, str(exc)[:150]

    verdict = "ACCEPTED" if status == "SUCCEEDED" else f"{status} {error}"
    print("\n  %s" % label)
    print("    arn      : %s" % arn)
    print("    qualifier: %s" % (qualifier or "-"))
    print("    result   : %s" % verdict)
    if cause and status != "SUCCEEDED":
        print("    cause    : %s" % cause)
    results.append({"label": label, "arn": arn, "qualifier": qualifier,
                    "status": status, "error": error, "cause": cause})

with open("spike/probe_harness_arn.json", "w") as fh:
    json.dump(results, fh, indent=2, default=str)
print("\nwrote spike/probe_harness_arn.json")
