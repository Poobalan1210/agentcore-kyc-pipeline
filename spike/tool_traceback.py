"""The actual traceback behind the submit_* UNHANDLED_ERROR."""
import time

import boto3

logs = boto3.client("logs", region_name="us-east-1")
GROUP = "/aws/lambda/kyc90-tools"
since = int((time.time() - 3600) * 1000)

streams = logs.describe_log_streams(
    logGroupName=GROUP, orderBy="LastEventTime", descending=True, limit=5)["logStreams"]

printed = 0
for stream in streams:
    events = logs.get_log_events(
        logGroupName=GROUP, logStreamName=stream["logStreamName"],
        startTime=since, limit=1000)["events"]
    for index, event in enumerate(events):
        if "tool raised" not in event["message"]:
            continue
        # The traceback follows the structured line, so print the window around it.
        for follow in events[index:index + 6]:
            print(repr(follow["message"])[:900])
        printed += 1
        print("-" * 78)
        if printed >= 2:
            raise SystemExit(0)
print("no traceback captured")
