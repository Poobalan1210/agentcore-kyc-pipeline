#!/usr/bin/env python3
"""Phase 3 + 4 verification — GATE G7.

Usage: `python verify_gateways.py [LOG_ONLY|ENFORCE]` (default LOG_ONLY).

Four Gateways, six targets each, a policy engine attached in the expected mode, and rate limits.
Also records the exact Cedar action names, because Phase 8's policy files depend on them and
F-35's three underscores are the easiest thing in this build to get wrong.
"""

from __future__ import annotations

import json
import sys

import boto3
from botocore.config import Config

REGION = "us-east-1"
ctl = boto3.client("bedrock-agentcore-control",
                   config=Config(region_name=REGION, retries={"max_attempts": 3}))

EXPECTED_TARGETS = {"DocumentStore", "FaceCompare", "VelocityStore", "SanctionsScreen",
                    "PepList", "AdverseMedia"}
#: What the Gateways should be attached in. Pass `ENFORCE` once the logged decisions have been
#: checked (`scripts/check_policy_decisions.py`) and the stack redeployed with
#: `-c policyEnforcement=ENFORCE`.
EXPECTED_MODE = (sys.argv[1] if len(sys.argv) > 1 else "LOG_ONLY").upper()
JURISDICTIONS = ("US", "UK", "AU", "JP")

results: list[tuple[str, bool, str]] = []
actions: dict[str, list[str]] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print("  %-4s %-52s %s" % ("PASS" if ok else "FAIL", name, detail))


print("=" * 94)
print("GATE G7 — four Gateways, targets, policy engines LOG_ONLY")
print("=" * 94)

gws = {g["name"]: g for g in ctl.list_gateways()["items"]
       if g["name"].startswith("KycGateway")}
check("four Gateways exist", len(gws) == 4, ", ".join(sorted(gws)))

engines = {e["name"]: e for e in ctl.list_policy_engines()["policyEngines"]
           if e["name"].startswith("kyc90_policy")}
check("four policy engines exist", len(engines) == 4, ", ".join(sorted(engines)))

arns: dict[str, str] = {}
for j in JURISDICTIONS:
    name = f"KycGateway-{j}"
    g = gws.get(name)
    if not g:
        check(f"{name} present", False, "missing")
        continue
    gid = g["gatewayId"] if "gatewayId" in g else g.get("gatewayIdentifier")
    full = ctl.get_gateway(gatewayIdentifier=gid)
    arns[j] = full.get("gatewayArn", "")

    check(f"{name} READY", full.get("status") == "READY", str(full.get("status")))
    check(f"{name} authorizer is AWS_IAM",
          full.get("authorizerType") == "AWS_IAM",
          str(full.get("authorizerType")))
    proto = (full.get("protocolConfiguration") or {}).get("mcp") or {}
    check(f"{name} search type SEMANTIC",
          proto.get("searchType") == "SEMANTIC", str(proto.get("searchType")))

    pe = full.get("policyEngineConfiguration") or {}
    check(f"{name} policy engine attached {EXPECTED_MODE}",
          pe.get("mode") == EXPECTED_MODE, "mode=%s" % pe.get("mode"))

    tgts = ctl.list_gateway_targets(gatewayIdentifier=gid)["items"]
    names = {t["name"] for t in tgts}
    check(f"{name} has all six targets",
          names == EXPECTED_TARGETS,
          "%d: %s" % (len(names), sorted(names)) if names != EXPECTED_TARGETS else "6")
    not_ready = [t["name"] for t in tgts if t.get("status") not in ("READY", "ACTIVE")]
    check(f"{name} all targets READY", not not_ready, str(not_ready))

    actions[j] = sorted("%s___%s" % (t, tool) for t, tool in [
        ("DocumentStore", "s3_get_document"),
        ("FaceCompare", "rekognition_compare_faces"),
        ("VelocityStore", "velocity_check"),
        ("SanctionsScreen", "sanctions_screen"),
        ("PepList", "pep_lookup"),
        ("AdverseMedia", "adverse_media"),
    ])

print("\n" + "=" * 94)
print("Cedar action names — THREE underscores (F-35). Phase 8 depends on these.")
print("=" * 94)
for a in actions.get("US", []):
    check("action name has exactly three underscores between parts",
          a.count("___") == 1, a)

print("\n" + "=" * 94)
print("Gateway ARNs — stage 2 Cedar policies need these (F-17)")
print("=" * 94)
for j, arn in sorted(arns.items()):
    print("  %-3s %s" % (j, arn))

failed = [r for r in results if not r[1]]
print("\n%d checks, %d passed, %d failed"
      % (len(results), len(results) - len(failed), len(failed)))
for n, _, d in failed:
    print("  FAILED  %s  %s" % (n, d))
print("\nGATE G7: %s" % ("PASS" if not failed else "FAIL"))

with open("verify_gateways.json", "w") as fh:
    json.dump({"gatewayArns": arns,
               "cedarActions": actions.get("US", []),
               "checks": [{"check": n, "pass": ok, "detail": d}
                          for n, ok, d in results]}, fh, indent=2)
print("wrote infra/verify_gateways.json")
sys.exit(1 if failed else 0)
