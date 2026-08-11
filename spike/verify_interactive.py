"""Task 5.8 — the interactive-surface half of the blast radius check.

R11.16 / F-47 ask for Interactive Shells (Terminals) to be disabled, noting it is a separate
surface from the `shell` tool so `AllowedTools` does not close it.

Two things had to be established before this could be verified rather than asserted:
  * `AWS::BedrockAgentCore::Harness` has no terminal/shell property at all — the string does not
    occur anywhere in the resource schema, so there is no declarative control surface.
  * Neither `bedrock-agentcore` nor `bedrock-agentcore-control` exposes any Shell/Terminal
    operation. The interactive surfaces that *do* exist are Browser and Code Interpreter
    sessions.

So the verifiable property is: no harness execution role can open any interactive session,
except Fraud, which is deliberately granted Code Interpreter and nothing else.
"""
import json

import boto3

iam = boto3.client("iam")

INTERACTIVE = {
    "bedrock-agentcore:startbrowsersession",
    "bedrock-agentcore:updatebrowserstream",
    "bedrock-agentcore:startcodeinterpretersession",
    "bedrock-agentcore:*",
    "*",
}
CODE_INTERP_ONLY = {"bedrock-agentcore:startcodeinterpretersession"}


def statements_for(role):
    out = []
    for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
        doc = iam.get_role_policy(RoleName=role, PolicyName=p)["PolicyDocument"]
        st = doc["Statement"]
        out += st if isinstance(st, list) else [st]
    for ap in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
        pv = iam.get_policy(PolicyArn=ap["PolicyArn"])["Policy"]["DefaultVersionId"]
        doc = iam.get_policy_version(
            PolicyArn=ap["PolicyArn"], VersionId=pv)["PolicyVersion"]["Document"]
        st = doc["Statement"]
        out += st if isinstance(st, list) else [st]
    return out


roles = []
for page in iam.get_paginator("list_roles").paginate():
    roles += [r["RoleName"] for r in page["Roles"]
              if r["RoleName"].startswith("kyc90-harness-")]

rows, failures = [], []
print("=" * 78)
print("HARNESS ROLES — reachable interactive session surfaces")
print("=" * 78)
for rn in sorted(roles):
    granted = set()
    for s in statements_for(rn):
        if s.get("Effect") != "Allow":
            continue
        acts = s.get("Action", [])
        acts = [acts] if isinstance(acts, str) else acts
        for a in acts:
            if a.lower() in INTERACTIVE:
                granted.add(a)

    is_fraud = rn.endswith("-fraud")
    extra = {g for g in granted if g.lower() not in CODE_INTERP_ONLY}
    if is_fraud:
        ok = not extra          # Fraud may hold Code Interpreter, nothing wider
        note = "code interpreter only (intended)" if ok else "WIDER THAN INTENDED: %s" % sorted(extra)
    else:
        ok = not granted
        note = "none" if ok else "UNEXPECTED: %s" % sorted(granted)
    if not ok:
        failures.append((rn, sorted(granted)))
    print("  %-32s %-8s %s" % (rn, "OK" if ok else "FAIL", note))
    rows.append({"role": rn, "granted": sorted(granted), "ok": ok})

print()
print("=" * 78)
print("RESULT: %s" % ("no harness can open an interactive session beyond its intent"
                      if not failures else "%d FAILURE(S)" % len(failures)))
print("=" * 78)
print("  Stated plainly rather than claimed as a control: Interactive Shells (Terminals) has")
print("  no CFN property and no SDK operation in this API version, so it cannot be disabled")
print("  declaratively. What is enforced is that no harness role can open any interactive")
print("  session. If AWS later exposes Terminals as an account or console feature outside IAM,")
print("  this check would not see it - so it is an IAM boundary, not a proof of absence.")

with open("spike/verify_interactive.json", "w") as f:
    json.dump({"roles": rows, "failures": failures, "pass": not failures,
               "terminal_control_surface": "absent from CFN schema and SDK operations",
               "enforced_via": "IAM least privilege on harness execution roles"},
              f, indent=2, default=str)
print("\nwrote spike/verify_interactive.json")
