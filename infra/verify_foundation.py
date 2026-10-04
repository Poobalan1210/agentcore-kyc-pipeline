#!/usr/bin/env python3
"""Task 1.6 — verify the foundation against the live account.

A gate is not passed by absence of error; it is passed by producing the stated evidence.
So this does not check that the stack deployed. It checks the two things that would be
catastrophic and invisible if wrong:

  * Object Lock actually refuses a delete. Compliance mode is only real if a delete fails. It
    writes a probe to each evidence bucket to prove it: under COMPLIANCE that probe can never be
    deleted for seven years; under the demo's GOVERNANCE lock it is removed again with the
    bypass, which proves the demo's evidence stays removable.
  * The ten roles hold only their intended permissions — every harness role denies the
    tool-bypass action, and the state machine role grants the one action the Step Functions
    integration needs (F-70 versus R11.7, which are one word apart).
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from kyc.config import EVIDENCE_LOCKS

#: The lock the deployment was made with (`make verify` passes the same one `make deploy` used).
#: COMPLIANCE when unset, so a bare run holds the account to the regulated posture.
EXPECTED_LOCK = EVIDENCE_LOCKS[(os.environ.get("EVIDENCE_LOCK") or "COMPLIANCE").upper()]

REGION = "us-east-1"
CFG = Config(region_name=REGION, retries={"max_attempts": 3})
s3 = boto3.client("s3", config=CFG)
iam = boto3.client("iam", config=CFG)
ddb = boto3.client("dynamodb", config=CFG)
kms = boto3.client("kms", config=CFG)

ACCOUNT = boto3.client("sts", config=CFG).get_caller_identity()["Account"]
JURISDICTIONS = ("us", "uk", "au", "jp")
AGENTS = ("briefing", "documents", "identity", "fraud", "prescreen", "compliance", "notify")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print("  %-4s %-52s %s" % ("PASS" if ok else "FAIL", name, detail))


def section(title: str) -> None:
    print("\n" + "=" * 92)
    print(title)
    print("=" * 92)


# --------------------------------------------------------------------- Object Lock
section("1.6a  Object Lock must refuse a delete  (R17.2, AC-08)")

for j in JURISDICTIONS:
    bucket = f"kyc90-evidence-{j}-{ACCOUNT}"
    try:
        cfg = s3.get_object_lock_configuration(Bucket=bucket)["ObjectLockConfiguration"]
        rule = cfg["Rule"]["DefaultRetention"]
        check(f"{j}: Object Lock enabled",
              cfg.get("ObjectLockEnabled") == "Enabled", str(cfg.get("ObjectLockEnabled")))
        check(f"{j}: {EXPECTED_LOCK.mode} mode ({EXPECTED_LOCK.name})",
              rule["Mode"] == EXPECTED_LOCK.mode, rule["Mode"])
        check(f"{j}: {EXPECTED_LOCK.days}-day default retention",
              rule.get("Days") == EXPECTED_LOCK.days, f"{rule.get('Days')} days")
    except ClientError as e:
        check(f"{j}: Object Lock configuration readable", False,
              e.response["Error"]["Code"])
        continue

    # The real test: write an object, then try to delete its version.
    key = f"_verify/{uuid.uuid4().hex}.json"
    try:
        s3.put_object(Bucket=bucket, Key=key,
                      Body=json.dumps({"probe": "object-lock", "jurisdiction": j}).encode(),
                      ContentType="application/json")
        version = s3.head_object(Bucket=bucket, Key=key)["VersionId"]
    except ClientError as e:
        check(f"{j}: probe object written", False, e.response["Error"]["Code"])
        continue
    check(f"{j}: probe object written", True, f"version {version[:12]}")

    try:
        s3.delete_object(Bucket=bucket, Key=key, VersionId=version)
        check(f"{j}: DELETE of locked version is refused", False,
              "DELETE SUCCEEDED — retention is NOT being enforced")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        check(f"{j}: DELETE of locked version is refused",
              code in ("AccessDenied", "InvalidRequest"), code)

    if EXPECTED_LOCK.mode == "GOVERNANCE":
        # The demo's promise is the other half: an administrator can remove what it wrote. So
        # prove it on the probe, which also leaves nothing of this run behind.
        try:
            s3.delete_object(Bucket=bucket, Key=key, VersionId=version,
                             BypassGovernanceRetention=True)
            check(f"{j}: admin can remove it with the governance bypass", True,
                  "probe deleted")
        except ClientError as e:
            check(f"{j}: admin can remove it with the governance bypass", False,
                  e.response["Error"]["Code"])
        continue

    # A delete marker is still allowed and is not a deletion; make that explicit so nobody
    # mistakes it for one.
    try:
        s3.delete_object(Bucket=bucket, Key=key)
        check(f"{j}: delete marker allowed (object still retained)", True,
              "expected — versioned delete marker, not a deletion")
    except ClientError as e:
        check(f"{j}: delete marker allowed", False, e.response["Error"]["Code"])


section("1.6b  The biometric bucket must remain deletable  (R36.3)")
bio = f"kyc90-demo-biometric-{ACCOUNT}"
try:
    s3.get_object_lock_configuration(Bucket=bio)
    check("biometric bucket has NO Object Lock", False, "Object Lock IS configured")
except ClientError as e:
    check("biometric bucket has NO Object Lock",
          e.response["Error"]["Code"] == "ObjectLockConfigurationNotFoundError",
          e.response["Error"]["Code"])
try:
    lc = s3.get_bucket_lifecycle_configuration(Bucket=bio)["Rules"]
    days = [r.get("Expiration", {}).get("Days") for r in lc]
    check("biometric lifecycle deletes within 24h", 1 in days, f"expiration days={days}")
except ClientError as e:
    check("biometric lifecycle rule present", False, e.response["Error"]["Code"])

key = f"_verify/{uuid.uuid4().hex}.txt"
try:
    s3.put_object(Bucket=bio, Key=key, Body=b"probe")
    s3.delete_object(Bucket=bio, Key=key)
    check("biometric object can be deleted immediately", True, "delete succeeded")
except ClientError as e:
    check("biometric object can be deleted immediately", False,
          e.response["Error"]["Code"])


# --------------------------------------------------------------------- IAM
section("1.6c  Ten roles, each with only its intended permissions  (R11.5, R11.7, F-70)")

expected_roles = [f"kyc90-harness-{a}" for a in AGENTS] + [
    "kyc90-gateway", "kyc90-statemachine", "kyc90-pipe"]

for role_name in expected_roles:
    try:
        iam.get_role(RoleName=role_name)
    except ClientError as e:
        check(f"role {role_name} exists", False, e.response["Error"]["Code"])
        continue

    stmts: list[dict] = []
    for pol in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
        doc = iam.get_role_policy(RoleName=role_name, PolicyName=pol)["PolicyDocument"]
        s = doc["Statement"]
        stmts.extend(s if isinstance(s, list) else [s])

    def actions_of(st: dict) -> list[str]:
        a = st.get("Action", [])
        return a if isinstance(a, list) else [a]

    if role_name.startswith("kyc90-harness-"):
        denied = {a for st in stmts if st.get("Effect") == "Deny" for a in actions_of(st)}
        check(f"{role_name}: denies InvokeAgentRuntimeCommand",
              "bedrock-agentcore:InvokeAgentRuntimeCommand" in denied,
              "explicit Deny present" if
              "bedrock-agentcore:InvokeAgentRuntimeCommand" in denied else str(denied)[:60])
        check(f"{role_name}: denies sts:AssumeRole",
              "sts:AssumeRole" in denied, "R23.6")

        wildcard_model = any(
            st.get("Effect") == "Allow"
            and any(a.startswith("bedrock:") and "Guardrail" not in a
                    for a in actions_of(st))
            and st.get("Resource") == "*"
            for st in stmts)
        check(f"{role_name}: model access is scoped, not '*'", not wildcard_model)

    if role_name == "kyc90-statemachine":
        allowed = {a for st in stmts if st.get("Effect") == "Allow"
                   for a in actions_of(st)}
        check("statemachine: grants InvokeAgentRuntime (F-70)",
              "bedrock-agentcore:InvokeAgentRuntime" in allowed,
              "the action the SFN integration authorises against")
        check("statemachine: does NOT grant InvokeAgentRuntimeCommand",
              "bedrock-agentcore:InvokeAgentRuntimeCommand" not in allowed,
              "R11.7 — bypasses allowedTools")

    if role_name == "kyc90-gateway":
        allowed = {a for st in stmts if st.get("Effect") == "Allow"
                   for a in actions_of(st)}
        check("gateway: grants InvokeGuardrailChecks (F-35)",
              "bedrock:InvokeGuardrailChecks" in allowed,
              "without it guardrail policies silently do nothing")


# --------------------------------------------------------------------- DynamoDB / KMS
section("1.6d  Case store and keys")
try:
    t = ddb.describe_table(TableName="kyc90-cases")["Table"]
    keys = {k["AttributeName"]: k["KeyType"] for k in t["KeySchema"]}
    check("case table PK/SK", keys == {"applicantId": "HASH", "sk": "RANGE"}, str(keys))
    gsis = {g["IndexName"] for g in t.get("GlobalSecondaryIndexes", [])}
    check("both GSIs present", gsis == {"state-index", "phase2Pending-index"}, str(gsis))
    check("SSE with customer-managed key",
          t.get("SSEDescription", {}).get("SSEType") == "KMS",
          str(t.get("SSEDescription", {}).get("SSEType")))
    check("deletion protection on", t.get("DeletionProtectionEnabled") is True)
    cont = ddb.describe_continuous_backups(TableName="kyc90-cases")
    pitr = cont["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
    check("point-in-time recovery enabled",
          pitr["PointInTimeRecoveryStatus"] == "ENABLED",
          pitr["PointInTimeRecoveryStatus"])
except ClientError as e:
    check("case table describable", False, e.response["Error"]["Code"])

aliases = {a["AliasName"] for a in kms.list_aliases()["Aliases"]}
for j in JURISDICTIONS:
    check(f"kms alias evidence-{j}", f"alias/kyc90-evidence-{j}" in aliases)
check("kms alias reasoning-traces", "alias/kyc90-reasoning-traces" in aliases,
      "distinct from evidence keys (R18.8)")


# --------------------------------------------------------------------- summary
section("SUMMARY")
failed = [r for r in results if not r[1]]
print("  %d checks, %d passed, %d failed" % (len(results), len(results) - len(failed),
                                             len(failed)))
for name, _, detail in failed:
    print("    FAILED  %s  %s" % (name, detail))
print("\n  GATE: %s" % ("Task 1.6 PASS" if not failed else "Task 1.6 FAIL"))

with open("verify_foundation.json", "w") as fh:
    json.dump([{"check": n, "pass": ok, "detail": d} for n, ok, d in results],
              fh, indent=2)
print("  wrote infra/verify_foundation.json")
sys.exit(1 if failed else 0)
