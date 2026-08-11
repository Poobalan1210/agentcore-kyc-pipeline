"""Verify the deployed state machines against the repo — tasks 6.2, 6.4, and 5.9's diff check.

Task 5.9 exists because Step Functions can override a harness's model, system prompt and tools per
invocation (F-54), so the harness configuration is not self-protecting: whatever the state machine
sends wins. The control is therefore to commit every override and diff the deployed ASL against the
repo as a release check. That is what this script is.

A `cdk deploy` exiting 0 is not evidence the definition in the account matches the definition in
git — someone can edit a state machine in the console, and the drift is invisible until an audit
asks which prompt produced a decision.
"""

import json
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra"))
from kyc.asl import phase1_definition, phase2_definition  # noqa: E402

REGION = "us-east-1"
ACCT = "633898796092"
AGENT_KEYS = ("router", "documents", "identity", "fraud", "prescreen", "compliance", "notify")

sfn = boto3.client("stepfunctions", region_name=REGION)
cfn = boto3.client("cloudformation", region_name=REGION)

outputs = {o["OutputKey"]: o["OutputValue"] for o in
           cfn.describe_stacks(StackName="KycOrchestration")["Stacks"][0]["Outputs"]}
harness_outputs = {o["OutputKey"]: o["OutputValue"] for o in
                   cfn.describe_stacks(StackName="KycHarnesses")["Stacks"][0]["Outputs"]}

endpoints = {}
models = {}
for key in AGENT_KEYS:
    suffix = {"router": "Router", "documents": "Documents", "identity": "Identity",
              "fraud": "Fraud", "prescreen": "PreScreen", "compliance": "ComplianceRisk",
              "notify": "Notify"}[key]
    endpoints[key] = harness_outputs[f"Endpoint{suffix}Arn"]

# Model ids are read back from the deployed harnesses so the comparison uses what is actually
# live rather than what config.py would compute today.
cc = boto3.client("cloudcontrol", region_name=REGION)
for key in AGENT_KEYS:
    arn = harness_outputs[{"router": "HarnessRouterArn", "documents": "HarnessDocumentsArn",
                           "identity": "HarnessIdentityArn", "fraud": "HarnessFraudArn",
                           "prescreen": "HarnessPreScreenArn",
                           "compliance": "HarnessComplianceRiskArn",
                           "notify": "HarnessNotifyArn"}[key]]
    props = json.loads(cc.get_resource(
        TypeName="AWS::BedrockAgentCore::Harness", Identifier=arn
    )["ResourceDescription"]["Properties"])
    models[key] = props["Model"]["BedrockModelConfig"]["ModelId"]

common = dict(
    case_table="kyc90-cases",
    harness_arns=harness_arns,
    model_ids=models,
    decision_engine_arn=outputs["DecisionEngineArn"],
    audit_writer_arn=outputs["AuditWriterArn"],
    review_queue_url=outputs["ReviewQueueUrl"],
)
expected = {
    "kyc90-phase1": phase1_definition(**common, phase2_state_machine_arn=outputs["Phase2Arn"]),
    "kyc90-phase2": phase2_definition(**common),
}

checks, failures = [], []


def _differences(live, repo, path=""):
    """Every leaf value that genuinely differs. Numbers compare by value, not by representation."""
    out = []
    if isinstance(live, dict) and isinstance(repo, dict):
        for key in sorted(set(live) | set(repo)):
            if key not in live:
                out.append((path + "/" + key, "<absent>", repo[key]))
            elif key not in repo:
                out.append((path + "/" + key, live[key], "<absent>"))
            else:
                out += _differences(live[key], repo[key], path + "/" + key)
    elif isinstance(live, list) and isinstance(repo, list):
        if len(live) != len(repo):
            out.append((path, f"len={len(live)}", f"len={len(repo)}"))
        for index, (a, b) in enumerate(zip(live, repo)):
            out += _differences(a, b, f"{path}[{index}]")
    elif live != repo:
        out.append((path, live, repo))
    return out


def check(label, ok, detail=""):
    checks.append({"check": label, "pass": bool(ok), "detail": detail})
    if not ok:
        failures.append(label)
    print("  %-58s %s%s" % (label, "PASS" if ok else "FAIL",
                            "" if ok else "  <- " + detail))


print("=" * 82)
print("DEPLOYED STATE MACHINES")
print("=" * 82)
for name, want in expected.items():
    arn = f"arn:aws:states:{REGION}:{ACCT}:stateMachine:{name}"
    described = sfn.describe_state_machine(stateMachineArn=arn)
    live = json.loads(described["definition"])

    check(f"{name} status ACTIVE", described["status"] == "ACTIVE", described["status"])
    check(f"{name} type STANDARD", described["type"] == "STANDARD", described["type"])
    check(f"{name} tracing enabled",
          described.get("tracingConfiguration", {}).get("enabled") is True)
    check(f"{name} logging level ALL",
          described.get("loggingConfiguration", {}).get("level") == "ALL",
          str(described.get("loggingConfiguration", {}).get("level")))
    check(f"{name} JSONata", live.get("QueryLanguage") == "JSONata",
          str(live.get("QueryLanguage")))

    # F-54 / task 5.9: the deployed definition must match the repo's, because every model and
    # prompt override travels in it.
    #
    # Compared value-by-value rather than as serialised text. A first attempt used
    # `json.dumps(..., sort_keys=True)` on both sides and reported drift on every machine: the
    # service returns `Temperature` as `0` where the repo emits `0.0`, which are the same number
    # and different strings. A drift check that cries wolf on JSON number formatting gets muted,
    # and then it is not a control at all.
    diffs = _differences(live, want)
    check(f"{name} deployed ASL == repo ASL (F-54 drift check)", not diffs,
          "; ".join(f"{p}: live={a!r} repo={b!r}" for p, a, b in diffs[:3]))

    # Every override committed: each agent Task must pin a model explicitly rather than
    # inheriting whatever the harness happens to hold.
    tasks = {n: s for n, s in live["States"].items()
             if s.get("Resource", "").endswith("invokeHarness")}
    for branch in live["States"].get("Phase1Parallel", {}).get("Branches", []):
        tasks.update({n: s for n, s in branch["States"].items()
                      if s.get("Resource", "").endswith("invokeHarness")})
    pinned = all("ModelId" in s["Arguments"]["Model"]["BedrockModelConfig"]
                 for s in tasks.values())
    check(f"{name} every agent Task pins a model (R26.3, F-54)", pinned)

print()
print("=" * 82)
print("SUPPORTING RESOURCES")
print("=" * 82)
lam = boto3.client("lambda", region_name=REGION)
for label, arn in (("decision engine", outputs["DecisionEngineArn"]),
                   ("audit writer", outputs["AuditWriterArn"])):
    cfg = lam.get_function_configuration(FunctionName=arn)
    check(f"{label} lambda active", cfg["State"] == "Active", cfg["State"])

sqs = boto3.client("sqs", region_name=REGION)
attrs = sqs.get_queue_attributes(
    QueueUrl=outputs["ReviewQueueUrl"],
    AttributeNames=["RedrivePolicy", "KmsMasterKeyId"])["Attributes"]
check("review queue has a dead-letter queue", "RedrivePolicy" in attrs)
check("review queue is KMS encrypted", "KmsMasterKeyId" in attrs)

print()
print("=" * 82)
print("RESULT: %d/%d checks passed" % (len(checks) - len(failures), len(checks)))
print("=" * 82)
if failures:
    for f in failures:
        print("  FAILED:", f)

with open("spike/verify_orchestration.json", "w") as fh:
    json.dump({"checks": checks, "failures": failures,
               "pass": not failures, "outputs": outputs}, fh, indent=2, default=str)
print("\nwrote spike/verify_orchestration.json")
sys.exit(1 if failures else 0)
