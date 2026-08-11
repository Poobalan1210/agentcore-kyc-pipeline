#!/usr/bin/env python3
"""Dump the exact GatewayTarget and GatewayRateLimit definitions."""
import json

import boto3
from botocore.config import Config

cfn = boto3.client("cloudformation", config=Config(region_name="us-east-1"))


def show(type_name, wanted):
    schema = json.loads(cfn.describe_type(Type="RESOURCE",
                                          TypeName=type_name)["Schema"])
    defs = schema.get("definitions", {})
    print("=" * 92)
    print(type_name)
    print("=" * 92)
    print("top-level properties:", json.dumps(schema.get("properties", {}), indent=2)[:600])
    for name in wanted:
        d = defs.get(name)
        if d is None:
            close = [k for k in defs if name.lower() in k.lower()]
            print("\n-- %s NOT FOUND. similar: %s" % (name, close))
            continue
        print("\n-- definition: %s" % name)
        print(json.dumps(d, indent=2)[:2600])


show("AWS::BedrockAgentCore::GatewayTarget",
     ["TargetConfiguration", "McpTargetConfiguration", "McpLambdaTargetConfiguration",
      "ToolSchema", "ToolDefinition", "CredentialProviderConfiguration"])

show("AWS::BedrockAgentCore::GatewayRateLimit",
     ["Entries", "RateLimitEntry", "DimensionKeys", "DimensionKey", "Requests",
      "RequestsLimit", "Dimensions", "Dimension"])
