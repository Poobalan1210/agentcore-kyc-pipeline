"""Recent errors from the tools Lambda — why submit_document_analysis fails while prescreen works."""
import time

import boto3

logs = boto3.client("logs", region_name="us-east-1")
GROUP = "/aws/lambda/kyc90-tools"
since = int((time.time() - 3600) * 1000)

resp = logs.filter_log_events(
    logGroupName=GROUP,
    startTime=since,
    filterPattern='?ERROR ?Traceback ?submit_ ?ValidationException ?Exception',
    limit=200,
)
seen = set()
for event in resp.get("events", []):
    msg = event["message"].strip()
    key = msg[:180]
    if key in seen:
        continue
    seen.add(key)
    print("-" * 78)
    print(msg[:1000])
print("\n%d unique messages" % len(seen))
