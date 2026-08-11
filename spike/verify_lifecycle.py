"""Task 5.8 verification — read the deployed harness state back and check the session bound.

A successful `cdk deploy` is not evidence the property took effect. Read each harness through
Cloud Control and assert on what the service actually stores.
"""
import json

import boto3

REGION = "us-east-1"
EXPECT_MAX_LIFETIME = 900
EXPECT_IDLE = 120

cc = boto3.client("cloudcontrol", region_name=REGION)
cfn = boto3.client("cloudformation", region_name=REGION)

harnesses = {}
for page in cfn.get_paginator("list_stack_resources").paginate(StackName="KycHarnesses"):
    for r in page["StackResourceSummaries"]:
        if r["ResourceType"] == "AWS::BedrockAgentCore::Harness":
            harnesses[r["LogicalResourceId"]] = r["PhysicalResourceId"]

results, failures = [], []
print("=" * 78)
print("DEPLOYED HARNESS SESSION BOUNDS  (expect MaxLifetime=%d, Idle=%d)"
      % (EXPECT_MAX_LIFETIME, EXPECT_IDLE))
print("=" * 78)
for logical, arn in sorted(harnesses.items()):
    try:
        r = cc.get_resource(TypeName="AWS::BedrockAgentCore::Harness", Identifier=arn)
        props = json.loads(r["ResourceDescription"]["Properties"])
    except Exception as e:  # noqa: BLE001
        print("  %-24s READ FAILED %s: %s" % (logical, type(e).__name__, str(e)[:90]))
        failures.append((logical, "read failed"))
        continue

    lc = (props.get("Environment", {})
               .get("AgentCoreRuntimeEnvironment", {})
               .get("LifecycleConfiguration", {}))
    ml, idle = lc.get("MaxLifetime"), lc.get("IdleRuntimeSessionTimeout")
    fs = (props.get("Environment", {})
               .get("AgentCoreRuntimeEnvironment", {})
               .get("FilesystemConfigurations", []))
    skills = props.get("Skills", [])
    allowed = props.get("AllowedTools", [])
    mem = props.get("Memory")

    problems = []
    if ml != EXPECT_MAX_LIFETIME:
        problems.append("MaxLifetime=%r" % ml)
    if idle != EXPECT_IDLE:
        problems.append("Idle=%r" % idle)
    if fs:
        problems.append("session storage mounted: %r" % fs)
    if skills:
        problems.append("skills present: %r" % skills)
    if any(t in ("*", "@builtin/shell", "@builtin/file_operations") for t in allowed):
        problems.append("over-broad AllowedTools: %r" % allowed)

    status = "OK" if not problems else "FAIL " + "; ".join(problems)
    if problems:
        failures.append((logical, problems))
    print("  %-24s maxLife=%-5s idle=%-5s storage=%-5s skills=%-3s mem=%-11s %s"
          % (logical, ml, idle, bool(fs), len(skills),
             "disabled" if mem and "Disabled" in mem else (mem and "MANAGED" or "default"),
             status))
    results.append({"logical": logical, "arn": arn, "maxLifetime": ml,
                    "idleRuntimeSessionTimeout": idle, "filesystem": fs,
                    "skills": skills, "allowedTools": allowed, "memory": mem,
                    "problems": problems})

print()
print("=" * 78)
print("RESULT: %s" % ("ALL %d HARNESSES BOUNDED" % len(results) if not failures
                      else "%d FAILURE(S)" % len(failures)))
print("=" * 78)
print("  Interactive Shells / Terminals: absent from the CFN schema entirely, so there is no")
print("  declarative control surface. Verified separately by attempting to open a terminal.")

with open("spike/verify_lifecycle.json", "w") as f:
    json.dump({"expect_max_lifetime": EXPECT_MAX_LIFETIME, "expect_idle": EXPECT_IDLE,
               "harnesses": results, "failures": failures,
               "pass": not failures}, f, indent=2, default=str)
print("\nwrote spike/verify_lifecycle.json")
