"""Many real Phase 1 runs at once, from one rehearsal's evidence — p50/p95 under load, and cost.

    # 1. Rehearse once through the applicant app (a live face check is required), note its id.
    # 2. Plan, then run:
    ./.venv/bin/python scripts/loadtest.py start --source APP-<sub>-<run> --count 20 --wave 5
    ./.venv/bin/python scripts/loadtest.py start --source APP-<sub>-<run> --count 20 --wave 5 --apply
    # 3. Clean up before the show:
    ./.venv/bin/python scripts/loadtest.py cleanup --run 20261201T170400Z --apply

What runs is the deployed pipeline, unchanged: each copy is a new case carrying the source case's
declared identity, document and liveness result, sent through the ingress queue exactly as the
intake API sends one, and decided by the real agents, tools and rules. Nothing is simulated, so
the numbers are the system's; and nothing is fabricated — the evidence is a real capture, reused.

Read this before `--apply`:

* **It costs real money**: every copy runs all four Phase 1 agents, then Phase 2.
* **The source must be fresh**: its liveness reference frame lives in the biometric bucket for
  about a day (24-hour lifecycle). After that the face match cannot run and every copy goes to a
  reviewer, which measures the wrong path.
* **Audit records are permanent**: each decision is sealed in the Object Lock bucket for seven
  years, like any other decision. `cleanup` removes the copies' case records and resets their
  accounts; it cannot and does not touch the sealed records.
* **Run it outside the show window**, or clean up after: open copies receive Act 3's list-update
  events and appear on the Demo Panel's board.

Copies are named `APP-LOADTEST-<run>-<n>` and carry `loadTest = <run>`, so `measure.py` excludes
them from rehearsal figures unless asked, and `cleanup` can find exactly them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measurement import render, summarise  # noqa: E402

CASE_TABLE = "kyc90-cases"
QUEUE_NAME = "kyc90-applicants.fifo"
ACCOUNT_FUNCTION = "kyc90-account-stub"
PREFIX = "APP-LOADTEST-"

#: The case attributes a copy takes from the source: what intake and the liveness API wrote.
COPIED = ("jurisdiction", "productApplied", "documents", "declaredIdentity", "liveness")

#: States after which Phase 1 has nothing more to do for a copy.
SETTLED = ("PROVISIONAL", "MONITORING", "RESTRICTED", "AWAITING_REVIEW", "AWAITING_APPLICANT",
           "DECIDED", "FROZEN")


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


# --------------------------------------------------------------------------- pure planning
def validate_source(item: Mapping[str, Any] | None) -> list[str]:
    """Why a source case cannot be replayed, or [] if it can."""
    if not item:
        return ["no case with that applicant id"]
    problems = []
    liveness = (item.get("liveness") or {}).get("M") or {}
    if (liveness.get("result") or {}).get("S") != "PASS":
        problems.append("its liveness result is not PASS")
    if not (liveness.get("referenceImageKey") or {}).get("S"):
        problems.append("it has no liveness reference frame, so the face match cannot run")
    if not ((item.get("documents") or {}).get("L")):
        problems.append("it lists no document")
    if (item.get("applicantId") or {}).get("S", "").startswith(PREFIX):
        problems.append("it is itself a load-test copy")
    return problems


def clone(source: Mapping[str, Any], *, run_id: str, index: int, now: datetime
          ) -> tuple[dict[str, Any], dict[str, Any]]:
    """(case item, ingress message) for copy `index` — the same shape intake writes."""
    applicant_id = f"{PREFIX}{run_id}-{index:03d}"
    event_id = f"EVT-{uuid.uuid4().hex[:12].upper()}"
    stamp = iso(now)
    item = {
        "applicantId": {"S": applicant_id},
        "sk": {"S": "CASE"},
        "caseId": {"S": f"CASE-{applicant_id}"},
        "state": {"S": "RECEIVED"},
        "createdAt": {"S": stamp},
        "lastEventId": {"S": event_id},
        "loadTest": {"S": run_id},
        "transitions": {"L": [{"M": {
            "from": {"S": "NONE"}, "to": {"S": "RECEIVED"}, "at": {"S": stamp},
            "actor": {"S": f"loadtest:{run_id}"},
            "reason": {"S": f"load-test copy of {source['applicantId']['S']}"}}}]},
        **{k: source[k] for k in COPIED if k in source},
    }
    message = {"applicantId": applicant_id, "eventId": event_id}
    return item, message


def waves(count: int, size: int) -> list[range]:
    size = max(1, size)
    return [range(start, min(count, start + size)) for start in range(0, count, size)]


# ----------------------------------------------------------------------------------- AWS
def _queue_url(session: Any, explicit: str | None) -> str:
    if explicit:
        return explicit
    return session.client("sqs").get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]


def start(session: Any, *, source_id: str, count: int, wave: int, pause: float,
          queue_url: str | None, apply: bool, timeout: float) -> int:
    ddb = session.client("dynamodb")
    source = ddb.get_item(TableName=CASE_TABLE, ConsistentRead=True, Key={
        "applicantId": {"S": source_id}, "sk": {"S": "CASE"}}).get("Item")
    problems = validate_source(source)
    if problems:
        print("Cannot replay this case: " + "; ".join(problems))
        return 2
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plan = waves(count, wave)
    print(f"Run {run_id}: {count} copies of {source_id}, {len(plan)} wave(s) of up to {wave}, "
          f"{pause:.0f}s apart. Each runs Phase 1 and Phase 2 for real.")
    if not apply:
        print("Dry run. Re-run with --apply to start it (read the module docstring first).")
        return 0

    sqs = session.client("sqs")
    url = _queue_url(session, queue_url)
    for n, batch in enumerate(plan):
        for index in batch:
            item, message = clone(source, run_id=run_id, index=index,
                                  now=datetime.now(timezone.utc))
            ddb.put_item(TableName=CASE_TABLE, Item=item,
                         ConditionExpression="attribute_not_exists(applicantId)")
            sqs.send_message(QueueUrl=url, MessageBody=json.dumps(message),
                             MessageGroupId=message["applicantId"],
                             MessageDeduplicationId=message["eventId"])
        print(f"  wave {n + 1}: {len(batch)} sent")
        if n + 1 < len(plan):
            time.sleep(pause)
    return wait(session, run_id=run_id, count=count, timeout=timeout)


def _copies(ddb: Any, run_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": CASE_TABLE, "FilterExpression": "loadTest = :run",
        "ExpressionAttributeValues": {":run": {"S": run_id}}}
    while True:
        resp = ddb.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def wait(session: Any, *, run_id: str, count: int, timeout: float,
         price: float | None = None) -> int:
    ddb = session.client("dynamodb")
    deadline = time.monotonic() + timeout
    while True:
        copies = _copies(ddb, run_id)
        settled = [c for c in copies if c.get("state", {}).get("S") in SETTLED]
        print(f"  {len(settled)}/{count} settled")
        if len(settled) >= count or time.monotonic() > deadline:
            break
        time.sleep(10)
    states: dict[str, int] = {}
    for c in copies:
        state = c.get("state", {}).get("S", "?")
        states[state] = states.get(state, 0) + 1
    print(f"\nRun {run_id} states: {json.dumps(states, sort_keys=True)}")
    print(render(summarise(copies, price_per_million_tokens=price)))
    return 0 if len(settled) >= count else 1


def cleanup(session: Any, *, run_id: str, apply: bool) -> int:
    ddb = session.client("dynamodb")
    copies = _copies(ddb, run_id)
    print(f"Run {run_id}: {len(copies)} case record(s) and their accounts. Sealed audit records "
          "stay: they are under Object Lock.")
    if not apply:
        print("Dry run. Re-run with --apply to delete them.")
        return 0
    lam = session.client("lambda")
    for c in copies:
        applicant_id = c["applicantId"]["S"]
        if not applicant_id.startswith(PREFIX):
            continue  # belt and braces: never delete anything that is not a copy
        lam.invoke(FunctionName=ACCOUNT_FUNCTION, Payload=json.dumps(
            {"operation": "reset", "arguments": {"applicantId": applicant_id}}).encode())
        ddb.delete_item(TableName=CASE_TABLE,
                        Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
                        ConditionExpression="loadTest = :run",
                        ExpressionAttributeValues={":run": {"S": run_id}})
    print("Done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start")
    s.add_argument("--source", required=True, help="a fresh rehearsal case's applicant id")
    s.add_argument("--count", type=int, default=10)
    s.add_argument("--wave", type=int, default=5, help="copies sent together")
    s.add_argument("--pause", type=float, default=10.0, help="seconds between waves")
    s.add_argument("--queue-url")
    s.add_argument("--timeout", type=float, default=600.0)
    s.add_argument("--apply", action="store_true")
    w = sub.add_parser("wait")
    w.add_argument("--run", required=True)
    w.add_argument("--count", type=int, required=True)
    w.add_argument("--timeout", type=float, default=600.0)
    w.add_argument("--price-per-mtok", type=float)
    c = sub.add_parser("cleanup")
    c.add_argument("--run", required=True)
    c.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "start" and not 1 <= args.count <= 200:
        parser.error("--count must be between 1 and 200")

    import boto3

    session = boto3.session.Session()
    if args.command == "start":
        return start(session, source_id=args.source, count=args.count, wave=args.wave,
                     pause=args.pause, queue_url=args.queue_url, apply=args.apply,
                     timeout=args.timeout)
    if args.command == "wait":
        return wait(session, run_id=args.run, count=args.count, timeout=args.timeout,
                    price=args.price_per_mtok)
    return cleanup(session, run_id=args.run, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
