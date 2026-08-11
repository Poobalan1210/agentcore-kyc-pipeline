#!/usr/bin/env python3
"""Extract the name patterns for every AgentCore CFN type we use, so names are chosen from the
schema rather than guessed. Hyphens are the likely culprit."""
import json
import re

import boto3
from botocore.config import Config

cfn = boto3.client("cloudformation", config=Config(region_name="us-east-1"))

TYPES = [
    "AWS::BedrockAgentCore::PolicyEngine",
    "AWS::BedrockAgentCore::Policy",
    "AWS::BedrockAgentCore::Gateway",
    "AWS::BedrockAgentCore::GatewayTarget",
    "AWS::BedrockAgentCore::Harness",
    "AWS::BedrockAgentCore::HarnessEndpoint",
]

CANDIDATES = {
    "kyc90-policy-us": None,
    "kyc90_policy_us": None,
    "KycGateway-US": None,
    "KycGatewayUS": None,
    "kyc90-harness-router": None,
    "kyc90_harness_router": None,
    "DocumentStore": None,
    "v3": None,
}

for t in TYPES:
    schema = json.loads(cfn.describe_type(Type="RESOURCE", TypeName=t)["Schema"])
    props = schema.get("properties", {})
    print("=" * 92)
    print(t)
    print("-" * 92)
    for key in ("Name", "HarnessName", "EndpointName"):
        p = props.get(key)
        if not p:
            continue
        pat = p.get("pattern")
        print("  %-14s pattern=%s  min=%s max=%s"
              % (key, pat, p.get("minLength"), p.get("maxLength")))
        if pat:
            rx = re.compile(pat)
            for cand in CANDIDATES:
                mark = "ok " if rx.fullmatch(cand) or rx.match(cand) else "NO "
                print("       %s %s" % (mark, cand))
