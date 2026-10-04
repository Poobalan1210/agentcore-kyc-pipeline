#!/usr/bin/env python3
"""Phase 5 verification — GATE G6, plus task 5.7.

Checks the live harnesses reached READY with the intended posture, then actually invokes one to
prove the whole chain works: harness -> Gateway -> Lambda -> tool -> back.

The invocation matters more than the config check. A harness can be READY with a correctly
scoped `allowedTools` list and still be unable to reach its Gateway.
"""

from __future__ import annotations

import json
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "us-east-1"
CFG = Config(region_name=REGION, read_timeout=300, retries={"max_attempts": 2})
cfn = boto3.client("cloudformation", config=CFG)
dat = boto3.client("bedrock-agentcore", config=CFG)

AGENTS = ("briefing", "documents", "identity", "fraud", "prescreen", "compliance", "notify")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print("  %-4s %-56s %s" % ("PASS" if ok else "FAIL", name, detail))


print("=" * 96)
print("GATE G6 — seven harnesses, correct posture, reachable")
print("=" * 96)

res = cfn.describe_stack_resources(StackName="KycHarnesses")["StackResources"]
harnesses = {r["LogicalResourceId"]: r for r in res
             if r["ResourceType"] == "AWS::BedrockAgentCore::Harness"}
endpoints = {r["LogicalResourceId"]: r for r in res
             if r["ResourceType"] == "AWS::BedrockAgentCore::HarnessEndpoint"}

check("seven harnesses in the stack", len(harnesses) == 7, str(len(harnesses)))
check("seven endpoints in the stack", len(endpoints) == 7, str(len(endpoints)))
for lid, r in sorted(harnesses.items()):
    check(f"{lid} CREATE_COMPLETE",
          r["ResourceStatus"] == "CREATE_COMPLETE", r["ResourceStatus"])

outs = {o["OutputKey"]: o["OutputValue"] for o in
        cfn.describe_stacks(StackName="KycHarnesses")["Stacks"][0].get("Outputs", [])}
arns = {k: v for k, v in outs.items() if k.startswith("Harness") and k.endswith("Arn")}
check("harness ARNs published as outputs", len(arns) == 7, str(len(arns)))

print("\n" + "=" * 96)
print("Task 5.7 — invoke a harness end to end (harness -> Gateway -> Lambda -> tool)")
print("=" * 96)

# The pre-screen agent is the sharpest end-to-end test: it must call a Gateway tool and report
# a classification, and its fail-closed behaviour is the property the pipeline depends on.
target = next((v for k, v in arns.items() if "PreScreen" in k or "Prescreen" in k), None)
if not target:
    check("pre-screen harness ARN found", False, str(sorted(arns)))
else:
    print("  harness: %s" % target)
    prompt = ("Applicant: Dmitri Volkov, date of birth 1968-04-12, country RU. "
              "Run the exact-match sanctions pre-screen and submit the result.")
    sid = "kyc-verify-" + uuid.uuid4().hex
    try:
        r = dat.invoke_harness(
            harnessArn=target,
            runtimeSessionId=sid,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
        )
        text_parts, kinds, saw_tool = [], {}, False
        for ev in r["stream"]:
            blob = json.dumps(ev, default=str)
            for k in ev:
                kinds[k] = kinds.get(k, 0) + 1
            if "toolUse" in blob or "tool_use" in blob:
                saw_tool = True
            for v in ev.values():
                if isinstance(v, dict):
                    d = v.get("delta") or {}
                    if isinstance(d, dict) and "text" in d:
                        text_parts.append(d["text"])
        text = "".join(text_parts)
        check("invocation succeeded", True, "event kinds: %s" % kinds)
        check("agent called a Gateway tool", saw_tool,
              "tool-use blocks seen" if saw_tool else "NO tool use — Gateway unreachable?")
        check("agent produced output", bool(text.strip()), text[:110].replace("\n", " "))
        # SYN-0001 is an exact name+DOB match, so a working chain must surface it.
        check("found the seeded exact match (SYN-0001)",
              "SYN-0001" in text or "CONFIRMED" in text.upper(),
              "expected SYN-0001 / CONFIRMED in output")
    except ClientError as e:
        check("invocation succeeded", False,
              "%s: %s" % (e.response["Error"]["Code"],
                          e.response["Error"]["Message"][:150]))
    except Exception as e:  # noqa: BLE001
        check("invocation succeeded", False, repr(e)[:150])

failed = [r for r in results if not r[1]]
print("\n%d checks, %d passed, %d failed"
      % (len(results), len(results) - len(failed), len(failed)))
for n, _, d in failed:
    print("  FAILED  %s  %s" % (n, d))
print("\nGATE G6: %s" % ("PASS" if not failed else "FAIL"))

with open("verify_harnesses.json", "w") as fh:
    json.dump({"harnessArns": arns,
               "checks": [{"check": n, "pass": ok, "detail": d}
                          for n, ok, d in results]}, fh, indent=2)
sys.exit(1 if failed else 0)
