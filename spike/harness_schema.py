"""Task 5.8 — which blast-radius controls does the CFN Harness type actually expose?

F-75 already established there is no LifecycleConfig. This checks the remaining three:
interactive shells / terminals, managed session storage, and idle session timeout.
"""
import json
import re

import boto3

cfn = boto3.client("cloudformation", region_name="us-east-1")
d = cfn.describe_type(Type="RESOURCE", TypeName="AWS::BedrockAgentCore::Harness")
schema = json.loads(d["Schema"])

print("=" * 74)
print("AWS::BedrockAgentCore::Harness — top-level properties")
print("=" * 74)
for k in sorted(schema.get("properties", {})):
    print("  ", k)

print()
print("=" * 74)
print("SEARCH for the three remaining 5.8 controls")
print("=" * 74)
blob = json.dumps(schema)
for label, pat in [
    ("interactive shell / terminal", r"shell|terminal|interactive"),
    ("managed session storage", r"sessionstorage|managedsession|storage"),
    ("idle session timeout", r"idle"),
    ("lifecycle (F-75 recheck)", r"lifecycle|maxlifetime"),
]:
    hits = sorted(set(re.findall(pat, blob, re.I)))
    print("  %-30s %s" % (label, hits if hits else "ABSENT from schema"))

print()
print("=" * 74)
print("Definitions available (for context)")
print("=" * 74)
for k in sorted(schema.get("definitions", {})):
    print("  ", k)

with open("spike/harness_schema.json", "w") as f:
    json.dump(schema, f, indent=2)
print("\nwrote spike/harness_schema.json")
