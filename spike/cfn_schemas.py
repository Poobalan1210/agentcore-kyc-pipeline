#!/usr/bin/env python3
"""Pull the CloudFormation resource schemas for the AgentCore types we need.

This changes the build: F-64 said the Harness control plane is unreachable, which is true of
the SDK. CloudFormation is a separate path and it does support AWS::BedrockAgentCore::Harness,
so the whole stack can be one Python CDK app after all.
"""
import json

import boto3
from botocore.config import Config

cfn = boto3.client("cloudformation", config=Config(region_name="us-east-1"))

WANT = [
    "AWS::BedrockAgentCore::Gateway",
    "AWS::BedrockAgentCore::GatewayTarget",
    "AWS::BedrockAgentCore::GatewayRateLimit",
    "AWS::BedrockAgentCore::PolicyEngine",
    "AWS::BedrockAgentCore::Policy",
    "AWS::BedrockAgentCore::Harness",
    "AWS::BedrockAgentCore::HarnessEndpoint",
]

out = {}
for t in WANT:
    try:
        d = cfn.describe_type(Type="RESOURCE", TypeName=t)
    except Exception as e:
        print("%-46s ERROR %s" % (t, e))
        continue
    schema = json.loads(d["Schema"])
    props = schema.get("properties", {})
    required = schema.get("required", [])
    ro = {p.split("/")[-1] for p in schema.get("readOnlyProperties", [])}
    create_only = {p.split("/")[-1] for p in schema.get("createOnlyProperties", [])}
    out[t] = {"required": required, "properties": sorted(props),
              "readOnly": sorted(ro), "createOnly": sorted(create_only)}

    print("=" * 92)
    print("%s   (version %s)" % (t, d.get("DefaultVersionId")))
    print("-" * 92)
    print("  required   : %s" % required)
    print("  createOnly : %s" % sorted(create_only))
    print("  readOnly   : %s" % sorted(ro))
    print("  properties :")
    for name in sorted(props):
        p = props[name]
        typ = p.get("type") or ("$ref->" + p.get("$ref", "?").split("/")[-1])
        enum = p.get("enum")
        extra = " enum=%s" % enum if enum else ""
        star = " *" if name in required else ""
        print("     %-34s %-10s%s%s" % (name, typ, star, extra))

with open("cfn_schemas.json", "w") as fh:
    json.dump(out, fh, indent=2)
print("\nwrote spike/cfn_schemas.json")
