#!/usr/bin/env python3
"""Task 2.8 verification — invoke the deployed Lambdas, not the local code.

Local tests prove the logic. This proves the deployment: packaging, imports, IAM, environment.
A tool that passes its unit tests and cannot import boto3 in the Lambda runtime is still broken.

Every evidence tool now reads its inputs from the case record, so each is invoked against an
applicantId that has no case: the correct response is an INVALID `CASE_NOT_FOUND` envelope, which
proves the function imports, dispatches and can read the case table. Nothing is written.

Fail-closed is exercised through the runtime fault flag in Parameter Store — the same switch the
on-stage demo flips — because the Gateway calls the published alias, which never sees an edited
environment variable. The flag is restored afterwards even if a check fails.
"""

from __future__ import annotations

import json
import sys
import time

import boto3
from botocore.config import Config

REGION = "us-east-1"
lam = boto3.client("lambda", config=Config(region_name=REGION, read_timeout=120))
ssm = boto3.client("ssm", config=Config(region_name=REGION))
TOOLS_FN = "kyc90-tools"
ACCOUNT_FN = "kyc90-account-stub"
NO_CASE = "VERIFY-NO-SUCH-CASE"
SCREENING = ("sanctions_screen", "pep_lookup", "adverse_media")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print("  %-4s %-60s %s" % ("PASS" if ok else "FAIL", name, detail))


def invoke(fn: str, payload: dict, qualifier: str | None = None) -> dict:
    kwargs = {"FunctionName": fn, "Payload": json.dumps(payload).encode()}
    if qualifier:
        kwargs["Qualifier"] = qualifier
    r = lam.invoke(**kwargs)
    body = json.loads(r["Payload"].read())
    if r.get("FunctionError"):
        return {"__error__": body}
    return body


def section(t: str) -> None:
    print("\n" + "=" * 98)
    print(t)
    print("=" * 98)


section("2.8a  all six evidence tools respond from the published alias")
for tool in ("s3_get_document", "rekognition_compare_faces", "velocity_check",
             "sanctions_screen", "pep_lookup", "adverse_media"):
    out = invoke(TOOLS_FN, {"toolName": tool,
                            "arguments": {"applicantId": NO_CASE, "name": "Verify Probe"}},
                 qualifier="live")
    if "__error__" in out:
        check(f"{tool} responds", False, str(out["__error__"])[:90])
        continue
    check(f"{tool} reads the case table and refuses an unknown case",
          out.get("status") == "INVALID" and out.get("reason_code") == "CASE_NOT_FOUND",
          "status=%s reason_code=%s" % (out.get("status"), out.get("reason_code")))

section("2.8b  fail-closed through the runtime fault flag  (R25.7, AC-12)")
env = lam.get_function_configuration(FunctionName=TOOLS_FN, Qualifier="live")
variables = env["Environment"]["Variables"]
param = variables.get("DEMO_FAULTS_PARAMETER")
check("the alias knows where the fault flags live", bool(param), str(param))
if param:
    original = ssm.get_parameter(Name=param)["Parameter"]["Value"]
    try:
        ssm.put_parameter(Name=param, Overwrite=True, Type="String",
                          Value=json.dumps({"unavailableTools": list(SCREENING)}))
        # The tools cache the flag for CONTROL_TTL_SECONDS (default 3).
        time.sleep(float(variables.get("CONTROL_TTL_SECONDS", "3")) + 2)
        for tool in SCREENING:
            out = invoke(TOOLS_FN, {"toolName": tool,
                                    "arguments": {"applicantId": NO_CASE, "name": "x"}},
                         qualifier="live")
            data = out.get("data", {})
            check(f"{tool}: UNAVAILABLE when forced", out.get("status") == "UNAVAILABLE",
                  "status=%s" % out.get("status"))
            check(f"{tool}: reports no verdict while unavailable",
                  "matched" not in data and "pepStatus" not in data,
                  "data keys=%s" % sorted(data))
    finally:
        ssm.put_parameter(Name=param, Overwrite=True, Type="String", Value=original)
        print("  (fault flags restored)")

section("2.8c  demo latency injection is off by default  (task 2.6)")
check("DEMO_PROFILE is off", variables.get("DEMO_PROFILE") == "off",
      variables.get("DEMO_PROFILE"))
check("DEMO_LATENCY_MS is 0", variables.get("DEMO_LATENCY_MS") == "0",
      variables.get("DEMO_LATENCY_MS"))

section("2.8d  account stub enforces, it does not advise  (R31.2, AC-23)")
probe = "VERIFY-ACCOUNT-%d" % int(time.time())
out = invoke(ACCOUNT_FN, {"operation": "openProvisional", "arguments": {"applicantId": probe}})
check("provisional account opens", out.get("status") == "OK", str(out)[:70])
restrictions = (out.get("account") or {}).get("restrictions", [])
check("money cannot leave a provisional account",
      {"INTERNATIONAL_TRANSFERS_BLOCKED", "THIRD_PARTY_PAYMENTS_BLOCKED",
       "DOMESTIC_TRANSFERS_BLOCKED", "CASH_WITHDRAWALS_BLOCKED"} <= set(restrictions),
      str(restrictions))
deposit = invoke(ACCOUNT_FN, {"operation": "transact",
                              "arguments": {"applicantId": probe, "type": "DEPOSIT",
                                            "amount": 100}})
check("a deposit under the ceiling is accepted", deposit.get("allowed") is True,
      str(deposit)[:80])
refused = invoke(ACCOUNT_FN, {"operation": "transact",
                              "arguments": {"applicantId": probe, "type": "DOMESTIC_TRANSFER",
                                            "amount": 10}})
check("a transfer out is refused, naming the restriction",
      refused.get("allowed") is False
      and refused.get("refusedBy") == "DOMESTIC_TRANSFERS_BLOCKED", str(refused)[:80])
invoke(ACCOUNT_FN, {"operation": "freeze", "arguments": {"applicantId": probe}})
lift = invoke(ACCOUNT_FN, {"operation": "lift",
                           "arguments": {"applicantId": probe, "posture": "STANDARD"}})
check("a frozen account cannot be lifted",
      (lift.get("__error__") or {}).get("errorType") == "AccountFrozen", str(lift)[:80])
invoke(ACCOUNT_FN, {"operation": "reset", "arguments": {"applicantId": probe}})

failed = [r for r in results if not r[1]]
print("\n%d checks, %d failed" % (len(results), len(failed)))
with open("verify_tools.json", "w") as fh:
    json.dump([{"check": n, "pass": ok, "detail": d} for n, ok, d in results], fh, indent=2)
sys.exit(1 if failed else 0)
