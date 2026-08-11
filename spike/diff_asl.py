"""Show the first real differences between deployed and repo ASL (task 5.9 drift check)."""
import json
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from kyc.asl import phase1_definition, phase2_definition  # noqa: E402

REGION = "us-east-1"
ACCT = "633898796092"
AGENT_KEYS = ("router", "documents", "identity", "fraud", "prescreen", "compliance", "notify")
SUFFIX = {"router": "Router", "documents": "Documents", "identity": "Identity",
          "fraud": "Fraud", "prescreen": "PreScreen", "compliance": "ComplianceRisk",
          "notify": "Notify"}

sfn = boto3.client("stepfunctions", region_name=REGION)
cfn = boto3.client("cloudformation", region_name=REGION)
cc = boto3.client("cloudcontrol", region_name=REGION)

outputs = {o["OutputKey"]: o["OutputValue"] for o in
           cfn.describe_stacks(StackName="KycOrchestration")["Stacks"][0]["Outputs"]}
h = {o["OutputKey"]: o["OutputValue"] for o in
     cfn.describe_stacks(StackName="KycHarnesses")["Stacks"][0]["Outputs"]}

endpoints = {k: h[f"Endpoint{SUFFIX[k]}Arn"] for k in AGENT_KEYS}
models = {}
for k in AGENT_KEYS:
    props = json.loads(cc.get_resource(
        TypeName="AWS::BedrockAgentCore::Harness",
        Identifier=h[f"Harness{SUFFIX[k]}Arn"])["ResourceDescription"]["Properties"])
    models[k] = props["Model"]["BedrockModelConfig"]["ModelId"]

common = dict(case_table="kyc90-cases", harness_arns=harness_arns, model_ids=models,
              decision_engine_arn=outputs["DecisionEngineArn"],
              audit_writer_arn=outputs["AuditWriterArn"],
              review_queue_url=outputs["ReviewQueueUrl"])
expected = {
    "kyc90-phase1": phase1_definition(**common,
                                      phase2_state_machine_arn=outputs["Phase2Arn"]),
    "kyc90-phase2": phase2_definition(**common),
}


def walk(a, b, path=""):
    """Yield (path, live, repo) for every leaf difference."""
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                yield (path + "/" + key, "<absent live>", b[key])
            elif key not in b:
                yield (path + "/" + key, a[key], "<absent repo>")
            else:
                yield from walk(a[key], b[key], path + "/" + key)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield (path, f"len={len(a)}", f"len={len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            yield from walk(x, y, f"{path}[{i}]")
    elif a != b:
        yield (path, a, b)


for name, want in expected.items():
    live = json.loads(sfn.describe_state_machine(
        stateMachineArn=f"arn:aws:states:{REGION}:{ACCT}:stateMachine:{name}")["definition"])
    diffs = list(walk(live, want))
    print("=" * 82)
    print("%s — %d difference(s)" % (name, len(diffs)))
    print("=" * 82)
    for path, a, b in diffs[:20]:
        print("  %s\n      live: %r\n      repo: %r" % (path, a, b))
    if not diffs:
        print("  identical")
