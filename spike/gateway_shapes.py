#!/usr/bin/env python3
"""What can actually create a Gateway: CloudFormation, boto3, or only the CLI?"""
import boto3
import botocore.session as bs
from botocore.config import Config

sess = bs.get_session()
m = sess.get_service_model("bedrock-agentcore-control")


def dump(op, depth=3):
    o = m.operation_model(op)
    print("=" * 84)
    print("%s  %s %s" % (op, o.http["method"], o.http["requestUri"]))
    print("-" * 84)

    def walk(shape, ind=1, d=0):
        if d > depth:
            print("  " * ind + "...")
            return
        if shape.type_name == "structure":
            req = set(getattr(shape, "required_members", []) or [])
            for name, sub in shape.members.items():
                print("  " * ind + "%s%s : %s" % (name, " *" if name in req else "",
                                                  sub.type_name))
                if sub.type_name in ("structure", "list", "map"):
                    walk(sub, ind + 1, d + 1)
                elif getattr(sub, "enum", None):
                    print("  " * (ind + 1) + "enum: %s" % sub.enum)
        elif shape.type_name == "list":
            walk(shape.member, ind, d + 1)
        elif shape.type_name == "map":
            walk(shape.value, ind, d + 1)

    if o.input_shape:
        walk(o.input_shape)


dump("CreateGateway", depth=4)
dump("CreateGatewayTarget", depth=5)
dump("CreatePolicyEngine", depth=3)

print("\n" + "=" * 84)
print("CloudFormation support for AgentCore resource types")
print("=" * 84)
cfn = boto3.client("cloudformation", config=Config(region_name="us-east-1"))
found = []
paginator = cfn.get_paginator("list_types")
for page in paginator.paginate(Visibility="PUBLIC", Type="RESOURCE",
                               Filters={"Category": "AWS_TYPES"}):
    for t in page["TypeSummaries"]:
        n = t["TypeName"]
        if "AgentCore" in n or "agentcore" in n.lower():
            found.append(n)
print("AgentCore CFN resource types: %s" % (sorted(found) or "NONE"))
