"""Is the Router's deployed config what we think, and does its Gateway carry the contract target?"""
import json

import boto3

REGION = "us-east-1"
cfn = boto3.client("cloudformation", region_name=REGION)
cc = boto3.client("cloudcontrol", region_name=REGION)

h = {o["OutputKey"]: o["OutputValue"] for o in
     cfn.describe_stacks(StackName="KycHarnesses")["Stacks"][0]["Outputs"]}
p = json.loads(cc.get_resource(
    TypeName="AWS::BedrockAgentCore::Harness",
    Identifier=h["HarnessRouterArn"])["ResourceDescription"]["Properties"])

print("ROUTER HARNESS")
print("  Tools        :", json.dumps(p.get("Tools")))
print("  AllowedTools :", p.get("AllowedTools"))
print("  MaxIterations:", p.get("MaxIterations"))
print("  Model        :", p["Model"]["BedrockModelConfig"]["ModelId"])
print("  Temperature  :", p["Model"]["BedrockModelConfig"].get("Temperature"))
print("  MaxTokens    :", p["Model"]["BedrockModelConfig"].get("MaxTokens"))
prompt = json.dumps(p.get("SystemPrompt"))
print("  prompt chars :", len(prompt))
print("  prompt tail  :", prompt[-400:])

print()
print("GATEWAY TARGETS containing 'Submit'")
for page in cfn.get_paginator("list_stack_resources").paginate(StackName="KycGateways"):
    for r in page["StackResourceSummaries"]:
        if "Submit" in r["LogicalResourceId"]:
            print("  %-34s %s" % (r["LogicalResourceId"], r["ResourceStatus"]))
