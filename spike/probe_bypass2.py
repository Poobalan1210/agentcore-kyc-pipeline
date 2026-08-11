"""Follow-up probe: is a harness ARN even a legal target for PutResourcePolicy? (task 5.5)"""
import json

import boto3

REGION = "us-east-1"
acc = boto3.client("bedrock-agentcore-control", region_name=REGION)
cfn = boto3.client("cloudformation", region_name=REGION)

print("=" * 70)
print("GATEWAY shape — find the real ARN field")
print("=" * 70)
gws = acc.list_gateways()
print(json.dumps(gws.get("items", [])[:1], indent=2, default=str))

print()
print("=" * 70)
print("HARNESS resource policy — exact error")
print("=" * 70)
harnesses = {}
for page in cfn.get_paginator("list_stack_resources").paginate(StackName="KycHarnesses"):
    for r in page["StackResourceSummaries"]:
        if r["ResourceType"] == "AWS::BedrockAgentCore::Harness":
            harnesses[r["LogicalResourceId"]] = r.get("PhysicalResourceId")
print("physical ids from CFN:")
for k, v in sorted(harnesses.items()):
    print("   %-24s %s" % (k, v))

probe = sorted(harnesses.items())[0]
name, phys = probe
print()
print("probing %s -> %s" % (name, phys))
for candidate in [
    phys,
    "arn:aws:bedrock-agentcore:us-east-1:633898796092:harness/%s" % phys,
]:
    print("\n  candidate resourceArn: %s" % candidate)
    try:
        r = acc.get_resource_policy(resourceArn=candidate)
        print("    OK ->", json.dumps(r.get("policy"))[:200])
    except Exception as e:
        print("    %s: %s" % (type(e).__name__, str(e)[:260]))
