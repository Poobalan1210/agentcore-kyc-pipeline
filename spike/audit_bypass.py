"""Task 5.5 — audit who can bypass the Gateway, and who can invoke a harness directly.

Two measured findings force this task to change shape:

  F-81  Resource type 'harness' does not support resource-based policies. R16.1 as written
        (an aws:SourceArn policy on each harness runtime naming its Gateway) is therefore not
        implementable. It was also backwards: the proven chain is harness -> Gateway -> Lambda,
        so the Gateway is downstream of the harness and never calls it.

  F-82  Lambda exposes only AddPermission (Allow-only); there is no PutResourcePolicy, so no
        explicit Deny can be authored in a Lambda resource policy. For same-account callers an
        identity policy alone suffices, so an Allow-only resource policy cannot exclude anyone.

Consequence: in a single account the enforceable control is least privilege on the workload
roles, not a resource policy. This script proves whether that least privilege actually holds,
by enumerating every kyc90 role and reporting which can reach the tool Lambda or a harness.
"""
import json

import boto3

REGION = "us-east-1"
ACCT = "633898796092"
iam = boto3.client("iam")

TOOL_ACTIONS = {"lambda:invokefunction", "lambda:*", "*"}
HARNESS_ACTIONS = {"bedrock-agentcore:invokeagentruntime", "bedrock-agentcore:*", "*"}

# The only principals that are supposed to hold each capability.
EXPECTED_TOOL = {"kyc90-gateway"}
EXPECTED_HARNESS = {"kyc90-statemachine"}


def statements_for(role_name):
    """Every statement attached to a role, inline and managed."""
    out = []
    for p in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
        doc = iam.get_role_policy(RoleName=role_name, PolicyName=p)["PolicyDocument"]
        st = doc["Statement"]
        out += [("inline:" + p, s) for s in (st if isinstance(st, list) else [st])]
    for ap in iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]:
        pv = iam.get_policy(PolicyArn=ap["PolicyArn"])["Policy"]["DefaultVersionId"]
        doc = iam.get_policy_version(
            PolicyArn=ap["PolicyArn"], VersionId=pv)["PolicyVersion"]["Document"]
        st = doc["Statement"]
        out += [("managed:" + ap["PolicyName"], s)
                for s in (st if isinstance(st, list) else [st])]
    return out


def grants(stmt, wanted, resource_substr):
    if stmt.get("Effect") != "Allow":
        return False
    acts = stmt.get("Action", [])
    acts = [acts] if isinstance(acts, str) else acts
    if not any(a.lower() in wanted for a in acts):
        return False
    res = stmt.get("Resource", [])
    res = [res] if isinstance(res, str) else res
    return any(r == "*" or resource_substr in r for r in res)


roles = []
for page in iam.get_paginator("list_roles").paginate():
    roles += [r["RoleName"] for r in page["Roles"] if r["RoleName"].startswith("kyc90")]

rows, tool_reachers, harness_reachers = [], set(), set()
print("=" * 78)
print("KYC90 ROLES — who can reach the tool Lambda / a harness runtime")
print("=" * 78)
for rn in sorted(roles):
    sts = statements_for(rn)
    t = [src for src, s in sts if grants(s, TOOL_ACTIONS, "kyc90-tools")]
    h = [src for src, s in sts if grants(s, HARNESS_ACTIONS, "harness/")]
    if t:
        tool_reachers.add(rn)
    if h:
        harness_reachers.add(rn)
    flag = []
    if t:
        flag.append("TOOL-LAMBDA via %s" % ",".join(t))
    if h:
        flag.append("HARNESS via %s" % ",".join(h))
    rows.append({"role": rn, "tool": t, "harness": h})
    print("  %-34s %s" % (rn, "; ".join(flag) if flag else "-"))

tool_extra = tool_reachers - EXPECTED_TOOL
harness_extra = harness_reachers - EXPECTED_HARNESS
ok = not tool_extra and not harness_extra and EXPECTED_TOOL <= tool_reachers

print()
print("=" * 78)
print("VERDICT — least privilege is the enforceable control (F-81, F-82)")
print("=" * 78)
print("  tool Lambda reachable by   : %s" % (sorted(tool_reachers) or "nothing"))
print("    expected                 : %s" % sorted(EXPECTED_TOOL))
print("    unexpected               : %s" % (sorted(tool_extra) or "none"))
print("  harness reachable by       : %s" % (sorted(harness_reachers) or "nothing"))
print("    expected                 : %s" % sorted(EXPECTED_HARNESS))
print("    unexpected               : %s" % (sorted(harness_extra) or "none"))
print()
print("  RESULT: %s" % ("PASS — no workload role can bypass the Gateway"
                        if ok else "FAIL — see unexpected principals above"))
print()
print("  Honest caveat, to be stated rather than glossed: this is identity-based least")
print("  privilege among workload roles. It does NOT stop an in-account principal that")
print("  holds lambda:InvokeFunction by other means (an admin, or a new role). Closing")
print("  that needs an SCP, which needs Organizations - the thing D-14 declined. Cedar is")
print("  therefore enforcing against the agents, advisory against an account admin.")

with open("spike/audit_bypass.json", "w") as f:
    json.dump({
        "finding_F81": "harness does not support resource-based policies",
        "finding_F82": "Lambda AddPermission is Allow-only; no Deny authorable; "
                       "same-account identity policy alone suffices",
        "roles": rows,
        "tool_reachers": sorted(tool_reachers),
        "harness_reachers": sorted(harness_reachers),
        "unexpected_tool": sorted(tool_extra),
        "unexpected_harness": sorted(harness_extra),
        "pass": ok,
    }, f, indent=2, default=str)
print("\nwrote spike/audit_bypass.json")
