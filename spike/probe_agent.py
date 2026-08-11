"""Invoke ONE agent directly. Seconds, not the ten minutes a full pipeline costs.

Usage: probe_agent.py <agentKey> [applicantId]

Built after burning several full Phase 1 executions to test a single agent's tool configuration.
A full run invokes five agents and two Nova models per branch; this invokes one.
"""

import json
import sys

import boto3
from botocore.config import Config

REGION = "us-east-1"
ACCT = "633898796092"
SUFFIX = {"router": "Router", "documents": "Documents", "identity": "Identity",
          "fraud": "Fraud", "prescreen": "PreScreen", "compliance": "ComplianceRisk",
          "notify": "Notify"}

agent = sys.argv[1] if len(sys.argv) > 1 else "router"
applicant = sys.argv[2] if len(sys.argv) > 2 else "PROBE-0001"

cfn = boto3.client("cloudformation", region_name=REGION)
core = boto3.client("bedrock-agentcore", region_name=REGION,
                    config=Config(read_timeout=300, retries={"max_attempts": 0}))

h = {o["OutputKey"]: o["OutputValue"] for o in
     cfn.describe_stacks(StackName="KycHarnesses")["Stacks"][0]["Outputs"]}
arn = h[f"Harness{SUFFIX[agent]}Arn"]

case = {
    "applicantId": applicant,
    "caseId": f"CASE-{applicant}",
    "jurisdiction": "us",
    "productApplied": "current_account",
    "documents": [{"type": "PASSPORT", "issuingCountry": "US",
                   "bucket": f"kyc90-evidence-us-{ACCT}",
                   "key": "synthetic/passport-us-001.png"}],
    "liveness": {"result": "PASS"},
    "deviceId": "PROBE-DEVICE", "ipAddress": "203.0.113.9",
    "fullName": "ALEX SYNTHETIC", "dateOfBirth": "1990-04-12",
}

print("agent   :", agent)
print("harness :", arn.rsplit("/", 1)[-1])
try:
    resp = core.invoke_harness(
        harnessArn=arn, qualifier="live",
        runtimeSessionId="probe-session-0000000000000000000000000000",
        messages=[{"role": "user", "content": [{"text": json.dumps(case)}]}],
    )
    # InvokeHarness returns an event stream, not a materialised body. Nothing is delivered until
    # it is consumed — an unconsumed stream reads as an empty response, which is exactly how it
    # looked before this was handled.
    stream = resp.get("stream")
    if stream is None:
        print("no stream in response:", list(resp))
        raise SystemExit(1)

    text_parts: list[str] = []
    tool_uses: list[str] = []
    stop_reason = None
    usage: dict = {}
    errors: list[str] = []

    raw_events = []
    for event in stream:
        raw_events.append(event)
        for key, payload in event.items():
            if key in ("internalServerException", "validationException",
                       "throttlingException", "modelStreamErrorException",
                       "runtimeClientErrorException", "serviceQuotaExceededException"):
                errors.append("%s: %s" % (key, str(payload)[:400]))
                continue
            blob = json.dumps(payload, default=str) if not isinstance(payload, (str, bytes)) \
                else str(payload)
            if "toolUse" in blob or "ToolUse" in blob:
                tool_uses.append(blob[:400])
            if isinstance(payload, dict):
                for field in ("text", "Text"):
                    if field in payload:
                        text_parts.append(str(payload[field]))
                delta = payload.get("delta")
                if isinstance(delta, dict) and "text" in delta:
                    text_parts.append(str(delta["text"]))
            if isinstance(payload, dict):
                stop_reason = payload.get("stopReason") or payload.get(
                    "StopReason") or stop_reason
                if payload.get("usage") or payload.get("Usage"):
                    usage = payload.get("usage") or payload.get("Usage")

    print("stop    :", stop_reason)
    print("tokens  :", usage or "(none reported)")
    print("toolUse :", len(tool_uses))
    if errors:
        print("ERRORS  :")
        for e in errors:
            print("   ", e)
    print("-" * 70)
    joined = " ".join(text_parts).strip()
    if joined:
        print(joined[:1500])
    else:
        print("(NO TEXT IN STREAM — dumping first 6 raw event keys/shapes)")
        for e in raw_events[:6]:
            for k, v in e.items():
                print("   %s: %s" % (k, json.dumps(v, default=str)[:250]))
except Exception as exc:  # noqa: BLE001
    print("FAILED  :", type(exc).__name__)
    print(str(exc)[:600])
