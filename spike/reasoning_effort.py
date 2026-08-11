#!/usr/bin/env python3
"""Task 0.6, corrected — maxReasoningEffort medium vs high.

Two constraints discovered by spike/fix_reasoning.py, neither of them documented in the
spec before now:

  1. reasoningConfig must sit under additionalParams.additionalModelRequestFields,
     not directly under additionalParams (design 7.3 was wrong).
  2. At maxReasoningEffort=high the service rejects the call if `temperature` or
     `maxTokens` are set: "`temperature` and `maxTokens` must be unset when reasoning
     is enabled". So a high-effort agent cannot pin temperature.
"""
import copy
import json
import statistics as st
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "us-east-1"
HARNESS_ARN = ("arn:aws:bedrock-agentcore:us-east-1:633898796092:"
               "harness/kycspike_kyc_spike_fraud-wFNik3Ok0E")
dat = boto3.client("bedrock-agentcore",
                   config=Config(region_name=REGION, read_timeout=600))

PROMPT = ("Applicant: Mohammed Al-Rashid, DOB 1979-03-11, UK resident. Sanctions list "
          "contains 'Muhammad Al Rashid', DOB 1979-03-11, Syria. Classify the match as "
          "CONFIRMED, PROBABLE, POSSIBLE or NO_MATCH with a one-sentence reason. "
          "Reply with a single JSON object only.")

N = 8


def call(effort, pin_sampling):
    """pin_sampling=True sets temperature+maxTokens; that is legal at medium, illegal at high."""
    cfg = {"modelId": "us.amazon.nova-2-lite-v1:0"}
    if pin_sampling:
        cfg["temperature"] = 0.0
        cfg["maxTokens"] = 3000
    cfg["additionalParams"] = {"additionalModelRequestFields": {
        "reasoningConfig": {"type": "enabled", "maxReasoningEffort": effort}}}

    body = {
        "harnessArn": HARNESS_ARN,
        "runtimeSessionId": "kyc-eff-" + uuid.uuid4().hex,
        "messages": [{"role": "user", "content": [{"text": PROMPT}]}],
        "model": {"bedrockModelConfig": cfg},
    }
    t0 = time.perf_counter()
    ttft, parts, saw_reasoning = None, [], False
    try:
        r = dat.invoke_harness(**body)
        for ev in r["stream"]:
            if ttft is None:
                ttft = (time.perf_counter() - t0) * 1000
            if "reasoning" in json.dumps(ev, default=str).lower():
                saw_reasoning = True
            for v in ev.values():
                if isinstance(v, dict):
                    d = v.get("delta") or {}
                    if isinstance(d, dict) and "text" in d:
                        parts.append(d["text"])
        return {"ok": True, "ttft": ttft, "total": (time.perf_counter() - t0) * 1000,
                "reasoning": saw_reasoning, "text": "".join(parts)[:120]}
    except ClientError as e:
        return {"ok": False, "err": e.response["Error"]["Message"].replace("\n", " ")[:150]}


def arm(label, effort, pin):
    ttfts, totals, fails, errs = [], [], 0, set()
    for _ in range(N):
        r = call(effort, pin)
        if r["ok"]:
            ttfts.append(r["ttft"] or 0)
            totals.append(r["total"])
        else:
            fails += 1
            errs.add(r["err"][:90])
        time.sleep(0.4)
    res = {
        "runs": N, "ok": len(totals), "failures": fails,
        "errors": sorted(errs),
        "ttft_p50": round(st.median(ttfts)) if ttfts else None,
        "ttft_p95": round(sorted(ttfts)[max(0, int(len(ttfts) * 0.95) - 1)]) if ttfts else None,
        "ttft_max": round(max(ttfts)) if ttfts else None,
        "total_p50": round(st.median(totals)) if totals else None,
        "total_max": round(max(totals)) if totals else None,
    }
    print("\n%s" % label)
    print("   ok=%d/%d  ttft_p50=%-7s ttft_max=%-7s total_p50=%-7s total_max=%-7s"
          % (res["ok"], N, res["ttft_p50"], res["ttft_max"],
             res["total_p50"], res["total_max"]))
    for e in res["errors"]:
        print("   error: %s" % e)
    return res


print("=" * 92)
print("TASK 0.6 — reasoning effort, corrected shape")
print("=" * 92)

OUT = {}
OUT["medium_pinned"] = arm("medium, temperature+maxTokens pinned (legal)", "medium", True)
OUT["high_unpinned"] = arm("high, temperature+maxTokens UNSET (required)", "high", False)
OUT["medium_unpinned"] = arm("medium, unpinned (comparison)", "medium", False)
OUT["high_pinned"] = arm("high, pinned (expected to fail — proves the constraint)",
                         "high", True)

m, h = OUT["medium_unpinned"], OUT["high_unpinned"]
if m["ttft_p50"] and h["ttft_p50"]:
    OUT["ttft_ratio"] = round(h["ttft_p50"] / m["ttft_p50"], 2)
    OUT["total_ratio"] = round(h["total_p50"] / m["total_p50"], 2)
    print("\n" + "=" * 92)
    print("high vs medium (both unpinned):  TTFT %.2fx   total %.2fx"
          % (OUT["ttft_ratio"], OUT["total_ratio"]))
    print("F-22 says high 'may significantly increase time to first token' -> %s"
          % ("CONFIRMED" if OUT["ttft_ratio"] > 1.15 else "not reproduced here"))

with open("reasoning_effort.json", "w") as fh:
    json.dump(OUT, fh, indent=2)
print("\nwrote spike/reasoning_effort.json")
