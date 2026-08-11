#!/usr/bin/env python3
"""Show exactly what the pre-screen agent returns, text and tool-use blocks."""
import json
import uuid

import boto3
from botocore.config import Config

dat = boto3.client("bedrock-agentcore",
                   config=Config(region_name="us-east-1", read_timeout=300))
ARN = "arn:aws:bedrock-agentcore:us-east-1:633898796092:harness/kyc90_prescreen-mLvQt9VMaD"

r = dat.invoke_harness(
    harnessArn=ARN,
    runtimeSessionId="kyc-show-" + uuid.uuid4().hex,
    messages=[{"role": "user", "content": [{"text":
        "Applicant: Dmitri Volkov, DOB 1968-04-12, country RU. Run the exact-match "
        "sanctions pre-screen and report the result."}]}],
)

parts, tool_blocks = [], []
for ev in r["stream"]:
    blob = json.dumps(ev, default=str)
    if "toolUse" in blob or "toolResult" in blob:
        tool_blocks.append(blob[:400])
    for v in ev.values():
        if isinstance(v, dict):
            d = v.get("delta") or {}
            if isinstance(d, dict) and "text" in d:
                parts.append(d["text"])

print("=== TOOL BLOCKS (%d) ===" % len(tool_blocks))
for t in tool_blocks[:6]:
    print(" ", t)
print("\n=== TEXT ===")
print("".join(parts)[:2200])
