#!/usr/bin/env python3
"""Phase 0 spike, invocation half — Tasks 0.3, 0.5, 0.6.

The harness exists (created via `agentcore deploy`, since the *LoopyAgent control-plane
operations are in the SDK model but not served — see spike/FINDINGS.md).
Invocation goes through the data plane, which does work from boto3.

  0.3  each Nova model ID accepted through InvokeHarness, valid output
  0.5  cold vs warm latency                       (closes A-01)
  0.6  maxReasoningEffort medium vs high, TTFT     (validates F-22)
"""
import json
import statistics as st
import sys
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "us-east-1"
HARNESS_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:633898796092:"
    "harness/kycspike_kyc_spike_fraud-wFNik3Ok0E"
)
CFG = Config(region_name=REGION, read_timeout=300, connect_timeout=20,
             retries={"max_attempts": 2, "mode": "standard"})
dat = boto3.client("bedrock-agentcore", config=CFG)

OUT: dict = {"harnessArn": HARNESS_ARN, "region": REGION}


def session_id() -> str:
    """runtimeSessionId must be >= 33 characters (F-36)."""
    s = "kyc-spike-" + uuid.uuid4().hex
    assert len(s) >= 33, len(s)
    return s


def invoke(model_id: str, effort: str | None = None, prompt: str = "Say OK",
           max_tokens: int = 256) -> dict:
    """One InvokeHarness call. Returns timings and the assembled text."""
    body = {
        "harnessArn": HARNESS_ARN,
        "runtimeSessionId": session_id(),
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "model": {"bedrockModelConfig": {
            "modelId": model_id,
            "maxTokens": max_tokens,
            "temperature": 0.0,
        }},
    }
    if effort:
        # additionalParams is invoke-time only — it is absent from CreateLoopyAgent,
        # which is why reasoning effort must be set per invocation (design 7.3).
        body["model"]["bedrockModelConfig"]["additionalParams"] = {
            "reasoningConfig": {"type": "enabled", "maxReasoningEffort": effort}
        }

    t0 = time.perf_counter()
    ttft = None
    text_parts: list[str] = []
    kinds: dict[str, int] = {}
    try:
        resp = dat.invoke_harness(**body)
        for event in resp["stream"]:
            if ttft is None:
                ttft = (time.perf_counter() - t0) * 1000
            for k, v in event.items():
                kinds[k] = kinds.get(k, 0) + 1
                if isinstance(v, dict):
                    delta = v.get("delta") or {}
                    if isinstance(delta, dict) and "text" in delta:
                        text_parts.append(delta["text"])
                    elif "text" in v:
                        text_parts.append(v["text"])
        total = (time.perf_counter() - t0) * 1000
        return {"ok": True, "ttft_ms": ttft, "total_ms": total,
                "text": "".join(text_parts).strip(), "event_kinds": kinds}
    except ClientError as e:
        err = e.response["Error"]
        return {"ok": False, "total_ms": (time.perf_counter() - t0) * 1000,
                "error": "%s: %s" % (err["Code"], err["Message"][:240])}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "total_ms": (time.perf_counter() - t0) * 1000,
                "error": repr(e)[:240]}


def p(label: str) -> None:
    print("\n" + "=" * 84)
    print(label)
    print("=" * 84, flush=True)


# --------------------------------------------------------------- wait for READY
p("STEP 0  wait for the harness to leave CREATING")
for attempt in range(60):
    r = invoke("us.amazon.nova-micro-v1:0", prompt="Reply with OK", max_tokens=16)
    if r["ok"]:
        print("   ready after %d attempt(s), %dms" % (attempt + 1, r["total_ms"]))
        print("   event kinds seen: %s" % r["event_kinds"])
        print("   text: %r" % r["text"][:120])
        break
    print("   attempt %-2d %s" % (attempt + 1, r["error"][:120]))
    if "ResourceNotFound" not in r.get("error", "") and \
       "Conflict" not in r.get("error", "") and \
       "not ready" not in r.get("error", "").lower():
        break
    time.sleep(10)
else:
    print("   never became ready")

# --------------------------------------------------------------- 0.3
p("STEP 1  TASK 0.3 — every Nova model ID through InvokeHarness")
PROMPT_03 = ('Applicant submitted a UK passport, MRZ readable, photo clear. '
             'Return {"documentType":...,"confidence":0..1}. JSON only.')
t03 = {}
for mid in ("us.amazon.nova-2-lite-v1:0",
            "us.amazon.nova-lite-v1:0",
            "us.amazon.nova-micro-v1:0",
            "global.amazon.nova-2-lite-v1:0"):
    r = invoke(mid, prompt=PROMPT_03)
    t03[mid] = r
    print("   %-32s %-4s total=%-6s ttft=%-6s %s"
          % (mid, "OK" if r["ok"] else "FAIL",
             int(r["total_ms"]), int(r["ttft_ms"]) if r.get("ttft_ms") else "-",
             (r.get("text") or r.get("error", ""))[:70].replace("\n", " ")))
OUT["task_0_3"] = t03

# --------------------------------------------------------------- 0.6
p("STEP 2  TASK 0.6 — maxReasoningEffort medium vs high (F-22)")
PROMPT_06 = ("Applicant: Mohammed Al-Rashid, DOB 1979-03-11, UK resident. "
             "Sanctions list contains 'Muhammad Al Rashid', DOB 1979-03-11, Syria. "
             "Classify the match as CONFIRMED, PROBABLE, POSSIBLE or NO_MATCH and "
             "give a one-sentence reason. JSON only.")
N = 6
t06 = {}
for effort in ("medium", "high"):
    ttfts, totals, fails, sample = [], [], 0, ""
    for i in range(N):
        r = invoke("us.amazon.nova-2-lite-v1:0", effort=effort,
                   prompt=PROMPT_06, max_tokens=512)
        if r["ok"]:
            if r.get("ttft_ms"):
                ttfts.append(r["ttft_ms"])
            totals.append(r["total_ms"])
            sample = sample or r["text"][:150]
        else:
            fails += 1
            if fails == 1:
                print("      first failure: %s" % r["error"][:200])
        time.sleep(0.5)
    t06[effort] = {
        "runs": N, "failures": fails,
        "ttft_p50": round(st.median(ttfts), 1) if ttfts else None,
        "ttft_max": round(max(ttfts), 1) if ttfts else None,
        "total_p50": round(st.median(totals), 1) if totals else None,
        "total_max": round(max(totals), 1) if totals else None,
        "sample": sample,
    }
    m = t06[effort]
    print("   effort=%-6s n=%d fails=%d  ttft_p50=%-8s ttft_max=%-8s total_p50=%-8s"
          % (effort, N, fails, m["ttft_p50"], m["ttft_max"], m["total_p50"]))
OUT["task_0_6"] = t06

if t06["medium"]["ttft_p50"] and t06["high"]["ttft_p50"]:
    ratio = t06["high"]["ttft_p50"] / t06["medium"]["ttft_p50"]
    OUT["task_0_6"]["ttft_ratio_high_over_medium"] = round(ratio, 2)
    print("\n   TTFT ratio high/medium = %.2fx  -> F-22 %s"
          % (ratio, "CONFIRMED (high inflates TTFT)" if ratio > 1.15
             else "NOT reproduced at this prompt size"))

# --------------------------------------------------------------- 0.5
p("STEP 3  TASK 0.5 — cold vs warm (closes A-01)")
print("   idleRuntimeSessionTimeout is 120s, so a fresh session id after a pause")
print("   is the closest available proxy for a cold path.")
warm = []
for _ in range(10):
    r = invoke("us.amazon.nova-micro-v1:0", prompt="Reply OK", max_tokens=16)
    if r["ok"]:
        warm.append(r["total_ms"])
    time.sleep(0.4)
print("   waiting 135s to exceed the idle timeout ...", flush=True)
time.sleep(135)
cold = []
for _ in range(4):
    r = invoke("us.amazon.nova-micro-v1:0", prompt="Reply OK", max_tokens=16)
    if r["ok"]:
        cold.append(r["total_ms"])
    time.sleep(140)
OUT["task_0_5"] = {
    "warm_n": len(warm),
    "warm_p50": round(st.median(warm), 1) if warm else None,
    "warm_p95": round(sorted(warm)[int(len(warm) * 0.95) - 1], 1) if len(warm) >= 2 else None,
    "cold_n": len(cold),
    "cold_p50": round(st.median(cold), 1) if cold else None,
    "cold_max": round(max(cold), 1) if cold else None,
    "method": "warm = 10 back-to-back calls; cold = 4 calls each preceded by a "
              ">120s idle gap so the session environment is torn down",
}
m = OUT["task_0_5"]
print("   warm p50=%sms (n=%s)   cold p50=%sms (n=%s)   cold max=%sms"
      % (m["warm_p50"], m["warm_n"], m["cold_p50"], m["cold_n"], m["cold_max"]))

with open("spike_results.json", "w") as fh:
    json.dump(OUT, fh, indent=2, default=str)
print("\nwrote spike/spike_results.json")
