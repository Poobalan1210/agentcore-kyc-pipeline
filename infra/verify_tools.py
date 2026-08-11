#!/usr/bin/env python3
"""Task 2.8 verification — invoke the deployed Lambdas, not the local code.

Local tests prove the logic. This proves the deployment: packaging, imports, IAM, environment.
A tool that passes 47 unit tests and cannot import boto3 in the Lambda runtime is still broken.
"""

from __future__ import annotations

import json
import sys

import boto3
from botocore.config import Config

REGION = "us-east-1"
lam = boto3.client("lambda", config=Config(region_name=REGION, read_timeout=120))

TOOLS_FN = "kyc90-tools"
ACCOUNT_FN = "kyc90-account-stub"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, detail))


def invoke(fn: str, payload: dict) -> dict:
    r = lam.invoke(FunctionName=fn, Payload=json.dumps(payload).encode())
    body = json.loads(r["Payload"].read())
    if r.get("FunctionError"):
        return {"__error__": body}
    return body


def section(t: str) -> None:
    print("\n" + "=" * 96)
    print(t)
    print("=" * 96)


section("2.8a  all seven tools respond from the deployed Lambda")
CASES = {
    "sanctions_exact_match": {"name": "Dmitri Volkov", "dob": "1968-04-12", "country": "RU"},
    "sanctions_fuzzy": {"name": "Mohammed Al-Rashid", "dob": "1979-03-11", "country": "SY"},
    "pep_lookup": {"name": "Adaeze Adeyemi", "dob": "1990-07-22", "country": "NG"},
    "adverse_media": {"name": "Kwame Asante-Boateng", "dob": "1982-06-25", "country": "GH"},
    "velocity_check": {"deviceId": "dev-verify-1", "ipAddress": "203.0.113.9"},
    "s3_get_document": {"bucket": "kyc90-evidence-us-633898796092", "key": "missing.jpg"},
    "rekognition_compare_faces": {"bucket": "kyc90-evidence-us-633898796092",
                                  "sourceKey": "a.jpg", "targetKey": "b.jpg"},
}
for tool, args in CASES.items():
    out = invoke(TOOLS_FN, {"toolName": tool, "arguments": args})
    if "__error__" in out:
        check(f"{tool} responds", False, str(out["__error__"])[:90])
        continue
    status = out.get("status")
    # Any of OK / INVALID / UNAVAILABLE is a correct *response*; an unhandled Lambda error is
    # not. The point of this check is that the envelope came back at all.
    check(f"{tool} responds with an envelope",
          status in ("OK", "INVALID", "UNAVAILABLE", "EMPTY"),
          "status=%s reason_code=%s" % (status, out.get("reason_code")))

section("2.8b  fail-closed under fault injection  (R25.7, AC-12)")
cfg = lam.get_function_configuration(FunctionName=TOOLS_FN)
original = dict(cfg["Environment"]["Variables"])

try:
    forced = dict(original)
    forced["FORCE_UNAVAILABLE"] = ("sanctions_exact_match,sanctions_fuzzy,"
                                   "pep_lookup,adverse_media")
    lam.update_function_configuration(
        FunctionName=TOOLS_FN, Environment={"Variables": forced})
    waiter = lam.get_waiter("function_updated_v2")
    waiter.wait(FunctionName=TOOLS_FN)

    for tool in ("sanctions_exact_match", "sanctions_fuzzy", "pep_lookup", "adverse_media"):
        out = invoke(TOOLS_FN, {"toolName": tool,
                                "arguments": {"name": "Dmitri Volkov", "dob": "1968-04-12"}})
        status = out.get("status")
        data = out.get("data", {})
        check(f"{tool}: UNAVAILABLE when source is down",
              status == "UNAVAILABLE", "status=%s" % status)
        # The failure this guards: a caller reading matched=False and concluding "clean".
        check(f"{tool}: reports no verdict while unavailable",
              "matched" not in data and "pepStatus" not in data,
              "data keys=%s" % sorted(data))
finally:
    lam.update_function_configuration(
        FunctionName=TOOLS_FN, Environment={"Variables": original})
    lam.get_waiter("function_updated_v2").wait(FunctionName=TOOLS_FN)
    print("  (environment restored)")

section("2.8c  demo latency injection is off by default  (task 2.6)")
env = lam.get_function_configuration(
    FunctionName=TOOLS_FN)["Environment"]["Variables"]
check("DEMO_PROFILE is off", env.get("DEMO_PROFILE") == "off", env.get("DEMO_PROFILE"))
check("DEMO_LATENCY_MS is 0", env.get("DEMO_LATENCY_MS") == "0",
      env.get("DEMO_LATENCY_MS"))

section("2.8d  account stub enforces, it does not advise  (R31.2, AC-23)")
out = invoke(ACCOUNT_FN, {"operation": "applyRestrictions",
                          "arguments": {"applicantId": "VERIFY-1"}})
check("provisional account opens", out.get("status") == "OK", str(out)[:70])

state = invoke(ACCOUNT_FN, {"operation": "getRestrictionState",
                            "arguments": {"applicantId": "VERIFY-1"}})
restrictions = state.get("result", {}).get("restrictions", [])
check("exactly five restrictions applied", len(restrictions) == 5,
      "%d: %s" % (len(restrictions), restrictions))

refused = invoke(ACCOUNT_FN, {"operation": "authorise",
                              "arguments": {"applicantId": "VERIFY-1",
                                            "txnType": "INTERNATIONAL_TRANSFER",
                                            "amount": 100}})
res = refused.get("result", {})
check("international transfer is refused", res.get("allowed") is False, str(res)[:80])
check("refusal names the restriction",
      res.get("refused_by") == "INTERNATIONAL_TRANSFERS_BLOCKED",
      str(res.get("refused_by")))

over = invoke(ACCOUNT_FN, {"operation": "authorise",
                           "arguments": {"applicantId": "VERIFY-1",
                                         "txnType": "DEPOSIT", "amount": 999999}})
check("deposit over the ceiling is refused",
      over.get("result", {}).get("allowed") is False,
      str(over.get("result", {}).get("reason_code")))

section("SUMMARY")
failed = [r for r in results if not r[1]]
print("  %d checks, %d passed, %d failed"
      % (len(results), len(results) - len(failed), len(failed)))
for n, _, d in failed:
    print("    FAILED  %s  %s" % (n, d))
print("\n  GATE: %s" % ("Task 2.8 PASS" if not failed else "Task 2.8 FAIL"))

with open("verify_tools.json", "w") as fh:
    json.dump([{"check": n, "pass": ok, "detail": d} for n, ok, d in results],
              fh, indent=2)
sys.exit(1 if failed else 0)
