"""Which AgentCore resource types accept resource-based policies? (task 5.5, F-81)

'harness' is confirmed unsupported. Establish the full picture before choosing where the
Gateway-bypass control actually lands.
"""
import json

import boto3

REGION = "us-east-1"
ACCT = "633898796092"
acc = boto3.client("bedrock-agentcore-control", region_name=REGION)


def arn(kind, ident):
    return "arn:aws:bedrock-agentcore:%s:%s:%s/%s" % (REGION, ACCT, kind, ident)


gw_id = None
for g in acc.list_gateways().get("items", []):
    if g["name"] == "KycGateway-US":
        gw_id = g["gatewayId"]
pe_id = None
for p in acc.list_policy_engines().get("items", acc.list_policy_engines().get(
        "policyEngineSummaries", [])):
    nm = p.get("name") or p.get("policyEngineName")
    if nm and "us" in nm:
        pe_id = p.get("policyEngineId") or p.get("id") or nm

candidates = [
    ("gateway", gw_id),
    ("policy-engine", pe_id),
    ("harness", "kyc90_router-6oEXh6o2ax"),
    ("runtime", "kyc90_router-6oEXh6o2ax"),
    ("workload-identity", "kyc90"),
    ("memory", "kyc90"),
]

results = {}
print("=" * 78)
print("RESOURCE-BASED POLICY SUPPORT BY TYPE")
print("=" * 78)
for kind, ident in candidates:
    if not ident:
        print("  %-20s SKIP (no live id found)" % kind)
        continue
    a = arn(kind, ident)
    try:
        r = acc.get_resource_policy(resourceArn=a)
        verdict = "SUPPORTED (policy present)"
        results[kind] = {"supported": True, "policy": r.get("policy")}
    except Exception as e:
        msg = str(e)
        if "does not support resource-based policies" in msg:
            verdict = "NOT SUPPORTED"
            results[kind] = {"supported": False, "error": msg[-120:]}
        elif "ResourceNotFound" in type(e).__name__ or "not found" in msg.lower():
            verdict = "SUPPORTED (no policy set yet)"
            results[kind] = {"supported": True, "policy": None}
        else:
            verdict = "%s: %s" % (type(e).__name__, msg[-110:])
            results[kind] = {"supported": None, "error": msg[-160:]}
    print("  %-20s %s" % (kind, verdict))

with open("spike/resource_policy_support.json", "w") as f:
    json.dump(results, f, indent=2, default=str)
print("\nwrote spike/resource_policy_support.json")
