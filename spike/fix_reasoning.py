#!/usr/bin/env python3
"""Find the correct nesting for reasoning effort inside InvokeHarness additionalParams.

Design 7.3 currently shows:
    additionalParams: { reasoningConfig: {...} }
The service rejected that with:
    Unknown parameter "reasoningConfig", must be one of: modelId, messages, system,
    inferenceConfig, toolConfig, guardrailConfig, additionalModel...
which is the Bedrock Converse request surface. So additionalParams is spliced into a
Converse call and reasoningConfig belongs under additionalModelRequestFields.
"""
import json
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "us-east-1"
HARNESS_ARN = ("arn:aws:bedrock-agentcore:us-east-1:633898796092:"
               "harness/kycspike_kyc_spike_fraud-wFNik3Ok0E")
dat = boto3.client("bedrock-agentcore",
                   config=Config(region_name=REGION, read_timeout=300))

PROMPT = ("Applicant: Mohammed Al-Rashid, DOB 1979-03-11, UK. Sanctions list has "
          "'Muhammad Al Rashid', DOB 1979-03-11, Syria. Classify CONFIRMED/PROBABLE/"
          "POSSIBLE/NO_MATCH with a one-sentence reason. JSON only.")

CANDIDATES = {
    "A bare reasoningConfig (what design 7.3 says)": {
        "reasoningConfig": {"type": "enabled", "maxReasoningEffort": "medium"}},
    "B additionalModelRequestFields.reasoningConfig": {
        "additionalModelRequestFields": {
            "reasoningConfig": {"type": "enabled", "maxReasoningEffort": "medium"}}},
    "C additionalModelRequestFields.thinking": {
        "additionalModelRequestFields": {
            "thinking": {"type": "enabled", "budget_tokens": 2048}}},
    "D inferenceConfig only (control)": {
        "inferenceConfig": {"maxTokens": 400}},
}


def attempt(label, additional):
    sid = "kyc-fix-" + uuid.uuid4().hex
    body = {
        "harnessArn": HARNESS_ARN,
        "runtimeSessionId": sid,
        "messages": [{"role": "user", "content": [{"text": PROMPT}]}],
        "model": {"bedrockModelConfig": {
            "modelId": "us.amazon.nova-2-lite-v1:0",
            "maxTokens": 600,
            "temperature": 0.0,
            "additionalParams": additional,
        }},
    }
    t0 = time.perf_counter()
    ttft, parts, reasoning_seen = None, [], False
    try:
        r = dat.invoke_harness(**body)
        for ev in r["stream"]:
            if ttft is None:
                ttft = (time.perf_counter() - t0) * 1000
            blob = json.dumps(ev, default=str)
            if "reasoning" in blob.lower():
                reasoning_seen = True
            for v in ev.values():
                if isinstance(v, dict):
                    d = v.get("delta") or {}
                    if isinstance(d, dict) and "text" in d:
                        parts.append(d["text"])
        total = (time.perf_counter() - t0) * 1000
        print("  %-46s OK    ttft=%-7d total=%-7d reasoning_block=%s"
              % (label, ttft or 0, total, reasoning_seen))
        print("      %s" % "".join(parts)[:130].replace("\n", " "))
        return True, ttft, total
    except ClientError as e:
        msg = e.response["Error"]["Message"].replace("\n", " ")[:180]
        print("  %-46s FAIL  %s" % (label, msg))
        return False, None, None


print("=" * 96)
print("Which additionalParams shape does InvokeHarness accept?")
print("=" * 96)
results = {}
for label, add in CANDIDATES.items():
    results[label] = attempt(label, add)
    time.sleep(1)

winner = [k for k, v in results.items() if v[0] and k.startswith(("B", "C"))]
print("\n" + "=" * 96)
print("Accepted reasoning shapes: %s" % (winner or "NONE"))

if winner:
    key = winner[0]
    shape = CANDIDATES[key]
    print("\nNow timing medium vs high with the accepted shape (%s)" % key[:2])
    for effort in ("medium", "high"):
        import copy
        s = copy.deepcopy(shape)
        # walk to the reasoningConfig and set the effort
        amrf = s.get("additionalModelRequestFields", {})
        if "reasoningConfig" in amrf:
            amrf["reasoningConfig"]["maxReasoningEffort"] = effort
        ttfts, totals = [], []
        for _ in range(5):
            ok, ttft, total = attempt("  effort=%s" % effort, s)
            if ok:
                ttfts.append(ttft or 0)
                totals.append(total)
            time.sleep(0.5)
        if ttfts:
            import statistics as st
            print("   effort=%-6s ttft_p50=%.0fms total_p50=%.0fms"
                  % (effort, st.median(ttfts), st.median(totals)))
