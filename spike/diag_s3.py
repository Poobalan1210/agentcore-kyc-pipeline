"""Why the document tool reported the evidence bucket unreachable during the live run."""
import json

import boto3

REGION = "us-east-1"
ACCT = "633898796092"
BUCKET = f"kyc90-evidence-us-{ACCT}"
KEY = "synthetic/passport-001.png"

lam = boto3.client("lambda", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
iam = boto3.client("iam")

print("=" * 78)
print("1. DOES THE OBJECT EVEN EXIST?")
print("=" * 78)
try:
    head = s3.head_object(Bucket=BUCKET, Key=KEY)
    print("  present, %s bytes" % head["ContentLength"])
except Exception as exc:  # noqa: BLE001
    print("  %s: %s" % (type(exc).__name__, str(exc)[:150]))
    print("  -> the live run seeded a case pointing at a key that was never uploaded.")

print()
print("=" * 78)
print("2. WHAT DOES THE TOOL ACTUALLY RETURN?")
print("=" * 78)
resp = lam.invoke(
    FunctionName="kyc90-tools:live",
    Payload=json.dumps({"documentKey": KEY, "jurisdiction": "us",
                        "bucket": BUCKET, "applicantId": "DIAG"}).encode(),
)
body = resp["Payload"].read().decode()
print("  status:", resp["StatusCode"], "| functionError:", resp.get("FunctionError", "-"))
print("  body  :", body[:600])

print()
print("=" * 78)
print("3. CAN THE TOOL ROLE READ THE BUCKET?")
print("=" * 78)
cfg = lam.get_function_configuration(FunctionName="kyc90-tools")
role_name = cfg["Role"].rsplit("/", 1)[-1]
print("  role:", role_name)
print("  env :", json.dumps(cfg.get("Environment", {}).get("Variables", {}))[:400])

statements = []
for p in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
    doc = iam.get_role_policy(RoleName=role_name, PolicyName=p)["PolicyDocument"]
    st = doc["Statement"]
    statements += st if isinstance(st, list) else [st]
for s in statements:
    acts = s.get("Action", [])
    acts = [acts] if isinstance(acts, str) else acts
    if any(a.lower().startswith("s3:") or a == "*" for a in acts):
        print("  %-26s %s" % (s.get("Sid", "-"), acts))
        res = s.get("Resource", [])
        for r in (res if isinstance(res, list) else [res]):
            print("      %s" % r)
if not any(any(str(a).lower().startswith("s3:") for a in
               ([s.get("Action")] if isinstance(s.get("Action"), str) else s.get("Action", [])))
           for s in statements):
    print("  NO s3: action on this role at all")
