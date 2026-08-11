"""Establish empirically which resource carries the Gateway-bypass control (task 5.5, R16.1).

R16.1 says the aws:SourceArn policy goes on each harness runtime, naming its jurisdiction
Gateway as the source. But the proven invocation chain is harness -> Gateway -> Lambda, so the
Gateway is downstream of the harness and never calls it. This probe checks who actually calls
what, so the policy lands on the resource that can really be bypassed.
"""
import json

import boto3

REGION = "us-east-1"
ACCT = "633898796092"
lam = boto3.client("lambda", region_name=REGION)
acc = boto3.client("bedrock-agentcore-control", region_name=REGION)

out = {}

# 1. The tool Lambda: who is allowed to invoke it today?
print("=" * 70)
print("1. TOOL LAMBDA RESOURCE POLICY  (kyc90-tools)")
print("=" * 70)
try:
    pol = json.loads(lam.get_policy(FunctionName="kyc90-tools")["Policy"])
    out["lambda_policy"] = pol
    for st in pol["Statement"]:
        print("  Sid       :", st.get("Sid"))
        print("  Effect    :", st.get("Effect"))
        print("  Principal :", json.dumps(st.get("Principal")))
        print("  Action    :", st.get("Action"))
        print("  Condition :", json.dumps(st.get("Condition", {})))
        print("  ---")
except lam.exceptions.ResourceNotFoundException:
    out["lambda_policy"] = None
    print("  NO RESOURCE POLICY AT ALL")
    print("  -> any principal holding lambda:InvokeFunction via its own identity policy")
    print("     can call the tool directly, skipping the Gateway and therefore Cedar.")

# 2. Harness ARNs and whether they already carry a resource policy
print()
print("=" * 70)
print("2. HARNESSES — existing resource policies")
print("=" * 70)
# ListLoopyAgents is the harness list op and returns AccessDenied (F-64), so read the ARNs
# back from the CloudFormation stack that created them instead.
harnesses = {}
cfn = boto3.client("cloudformation", region_name=REGION)
for page in cfn.get_paginator("list_stack_resources").paginate(StackName="KycHarnesses"):
    for r in page["StackResourceSummaries"]:
        if r["ResourceType"] == "AWS::BedrockAgentCore::Harness":
            harnesses[r["LogicalResourceId"]] = r.get("PhysicalResourceId")
for name, arn in sorted(harnesses.items()):
    try:
        rp = acc.get_resource_policy(resourceArn=arn)
        pol = rp.get("policy")
        print("  %-28s HAS POLICY: %s" % (name, (pol or "")[:60]))
    except Exception as e:
        print("  %-28s none (%s)" % (name, type(e).__name__))
out["harnesses"] = harnesses

# 3. Gateway ARNs
print()
print("=" * 70)
print("3. GATEWAYS")
print("=" * 70)
gws = {}
for g in acc.list_gateways().get("items", []):
    gws[g["name"]] = g.get("gatewayArn") or g.get("arn")
for n, a in sorted(gws.items()):
    print("  %-20s %s" % (n, a))
out["gateways"] = gws

with open("spike/probe_bypass.json", "w") as f:
    json.dump(out, f, indent=2, default=str)
print()
print("wrote spike/probe_bypass.json")
