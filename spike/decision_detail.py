"""What the decision engine returned on the last live run, and why."""
import json

import boto3

sfn = boto3.client("stepfunctions", region_name="us-east-1")
run = json.load(open("spike/live_run.json"))

events = []
for page in sfn.get_paginator("get_execution_history").paginate(
        executionArn=run["execution"], maxResults=1000):
    events.extend(page["events"])

for e in events:
    d = e.get("stateExitedEventDetails")
    if d and d.get("name") == "PreliminaryTier" and d.get("output"):
        out = json.loads(d["output"])
        payload = out.get("Payload", out)
        print("=" * 78)
        print("DECISION")
        print("=" * 78)
        for k in ("outcome", "rule", "reasonCode", "tier", "tierRule", "requiredItem"):
            print("  %-14s %s" % (k, payload.get(k)))
        print("\n  explanation: %s" % str(payload.get("explanation"))[:400])
        sig = payload.get("recordedSignals") or {}
        print("\n  unavailable_agents: %s" % sig.get("unavailable_agents"))
        print("  invalid_agents    : %s" % sig.get("invalid_agents"))
        print("  tier_inputs       : %s" % json.dumps(sig.get("tier_inputs"), default=str))
        break

# What was actually sent in, so a failed derivation can be traced to the prose that defeated it.
for e in events:
    d = e.get("taskScheduledEventDetails")
    if d and "decision" in str(d.get("resource", "")) + str(d.get("parameters", ""))[:200].lower():
        params = json.loads(d["parameters"])
        ar = (params.get("Payload") or {}).get("agentResults") or {}
        print()
        print("=" * 78)
        print("AGENT REPLIES THE ENGINE SAW")
        print("=" * 78)
        for agent, res in ar.items():
            blocks = (res or {}).get("content") or []
            text = " ".join(b.get("Text", "") for b in blocks if isinstance(b, dict))
            print("\n  --- %s (status=%s) ---" % (agent, (res or {}).get("status")))
            print("  %s" % text.replace("\n", " ")[:700])
        break
