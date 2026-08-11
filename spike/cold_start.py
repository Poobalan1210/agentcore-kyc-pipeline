#!/usr/bin/env python3
"""Task 0.5 — cold vs warm invocation latency. Closes A-01.

First attempt returned n=0 because maxTokens=16 tripped
"Model stopped generating due to maximum token limit" (F-63) — the harness system prompt
asks for JSON and 16 tokens cannot hold it. Budget realistically instead.

Cold path definition: idleRuntimeSessionTimeout on this harness is 120s, so a brand-new
runtimeSessionId issued after a >120s gap gets a freshly provisioned session environment.
That is the closest thing to a cold start that is observable from outside the service.
"""
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
                   config=Config(region_name=REGION, read_timeout=300))

PROMPT = 'Return {"status":"ok"} and nothing else.'


def one(reuse_session=None):
    sid = reuse_session or ("kyc-cold-" + uuid.uuid4().hex)
    t0 = time.perf_counter()
    ttft = None
    try:
        r = dat.invoke_harness(
            harnessArn=HARNESS_ARN,
            runtimeSessionId=sid,
            messages=[{"role": "user", "content": [{"text": PROMPT}]}],
            model={"bedrockModelConfig": {
                "modelId": "us.amazon.nova-micro-v1:0",
                "maxTokens": 256,
                "temperature": 0.0,
            }},
        )
        for _ in r["stream"]:
            if ttft is None:
                ttft = (time.perf_counter() - t0) * 1000
        return {"ok": True, "ttft": ttft, "total": (time.perf_counter() - t0) * 1000,
                "sid": sid}
    except ClientError as e:
        return {"ok": False, "err": e.response["Error"]["Message"][:140], "sid": sid}


def stats(xs):
    if not xs:
        return {}
    s = sorted(xs)
    return {
        "n": len(s),
        "p50": round(st.median(s)),
        "p95": round(s[max(0, int(len(s) * 0.95) - 1)]),
        "min": round(min(s)),
        "max": round(max(s)),
    }


print("=" * 86)
print("TASK 0.5 — cold vs warm  (closes A-01)")
print("=" * 86)

# ---- warm: reuse one session, back to back -------------------------------------
print("\nWARM  same session, 12 back-to-back calls")
warm_sid = "kyc-warm-" + uuid.uuid4().hex
one(warm_sid)  # prime, discard
warm = []
for i in range(12):
    r = one(warm_sid)
    if r["ok"]:
        warm.append(r["total"])
    else:
        print("   fail: %s" % r["err"])
print("   %s" % stats(warm))

# ---- new session, no idle gap --------------------------------------------------
print("\nNEW SESSION  fresh id each call, no idle gap, 8 calls")
fresh = []
for i in range(8):
    r = one()
    if r["ok"]:
        fresh.append(r["total"])
    else:
        print("   fail: %s" % r["err"])
    time.sleep(0.3)
print("   %s" % stats(fresh))

# ---- cold: fresh session after >120s idle --------------------------------------
print("\nCOLD  fresh id, each preceded by a 130s idle gap, 4 calls")
cold = []
for i in range(4):
    print("   idling 130s ...", flush=True)
    time.sleep(130)
    r = one()
    if r["ok"]:
        cold.append(r["total"])
        print("   cold call %d: %dms" % (i + 1, r["total"]))
    else:
        print("   cold call %d failed: %s" % (i + 1, r["err"]))
print("   %s" % stats(cold))

OUT = {
    "harnessArn": HARNESS_ARN,
    "model": "us.amazon.nova-micro-v1:0",
    "warm_same_session": stats(warm),
    "fresh_session_no_gap": stats(fresh),
    "cold_after_idle": stats(cold),
    "idleRuntimeSessionTimeout_s": 120,
    "note": "Cold path = new runtimeSessionId after >120s idle, which forces a new "
            "session environment. AWS publishes no cold-start figure; this is the "
            "closest externally observable measurement.",
}
w, c = OUT["warm_same_session"], OUT["cold_after_idle"]
if w.get("p50") and c.get("p50"):
    OUT["cold_penalty_ms"] = c["p50"] - w["p50"]
    OUT["cold_ratio"] = round(c["p50"] / w["p50"], 2)
    print("\n" + "=" * 86)
    print("cold p50 %dms vs warm p50 %dms  ->  +%dms  (%.2fx)"
          % (c["p50"], w["p50"], OUT["cold_penalty_ms"], OUT["cold_ratio"]))

with open("cold_start.json", "w") as fh:
    json.dump(OUT, fh, indent=2)
print("\nwrote spike/cold_start.json")
