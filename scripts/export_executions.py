"""Export Step Functions executions, state by state, for the measurement slide and the record.

    ./.venv/bin/python scripts/export_executions.py                    # last 24 h, all 3 phases
    ./.venv/bin/python scripts/export_executions.py --phase phase1 --hours 6 --out run.jsonl

Writes one JSON line per execution — name, status, start, stop, duration and every state with
its duration — and prints the slowest states across the export (p50/p95 per state name), which
is where "where do the 90 seconds go" gets its answer.

Read-only: `ListExecutions`, `DescribeExecution`, `GetExecutionHistory`. Executions age out of
Step Functions after 90 days, so export rehearsal runs soon after they happen; the export is the
copy that outlives them. Execution input and output are not exported: both carry case data.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measurement import percentile, state_durations, step_timeline  # noqa: E402

#: The three state machines, by the names the stacks give them.
STATE_MACHINES = {
    "phase1": "kyc90-phase1",
    "phase2": "kyc90-phase2",
    "phase3": "kyc90-phase3-lifecycle",
}


def machine_arn(name: str, *, region: str, account: str) -> str:
    return f"arn:aws:states:{region}:{account}:stateMachine:{name}"


def executions(sfn: Any, machine: str, *, since: datetime) -> Iterator[dict[str, Any]]:
    """Newest first; stops paging once executions are older than `since`."""
    kwargs: dict[str, Any] = {"stateMachineArn": machine, "maxResults": 100}
    while True:
        resp = sfn.list_executions(**kwargs)
        for execution in resp.get("executions", []):
            if execution["startDate"] < since:
                return
            yield execution
        token = resp.get("nextToken")
        if not token:
            return
        kwargs["nextToken"] = token


def history(sfn: Any, execution_arn: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"executionArn": execution_arn, "maxResults": 1000,
                              "includeExecutionData": False}
    while True:
        resp = sfn.get_execution_history(**kwargs)
        events.extend(resp.get("events", []))
        token = resp.get("nextToken")
        if not token:
            return events
        kwargs["nextToken"] = token


def export_row(phase: str, execution: dict[str, Any], events: list[dict[str, Any]]
               ) -> dict[str, Any]:
    start, stop = execution.get("startDate"), execution.get("stopDate")
    return {
        "phase": phase,
        "name": execution.get("name"),
        "executionArn": execution.get("executionArn"),
        "status": execution.get("status"),
        "startDate": start.isoformat() if start else None,
        "stopDate": stop.isoformat() if stop else None,
        "seconds": (stop - start).total_seconds() if start and stop else None,
        "steps": step_timeline(events),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--phase", choices=sorted(STATE_MACHINES), action="append")
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    import boto3

    session = boto3.session.Session()
    region = session.region_name or "us-east-1"
    account = session.client("sts").get_caller_identity()["Account"]
    sfn = session.client("stepfunctions", region_name=region)
    since = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    out = args.out or Path("exports") / (
        f"executions-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    with out.open("w") as fh:
        for phase in args.phase or sorted(STATE_MACHINES):
            arn = machine_arn(STATE_MACHINES[phase], region=region, account=account)
            for execution in executions(sfn, arn, since=since):
                row = export_row(phase, execution, history(sfn, execution["executionArn"]))
                rows.append(row)
                fh.write(json.dumps(row, default=str) + "\n")

    print(f"Exported {len(rows)} execution(s) to {out}")
    for phase in sorted({r["phase"] for r in rows}):
        done = [r["seconds"] for r in rows
                if r["phase"] == phase and r["status"] == "SUCCEEDED" and r["seconds"]]
        failed = sum(1 for r in rows if r["phase"] == phase and r["status"] == "FAILED")
        print(f"\n{phase}: {len(done)} succeeded, {failed} failed; "
              f"p50 {percentile(done, 50) or 0:.1f}s, p95 {percentile(done, 95) or 0:.1f}s")
        slowest = list(state_durations(r for r in rows if r["phase"] == phase).items())[:8]
        for name, d in slowest:
            print(f"  {name:<36} n={d['count']:<4} p50 {d['p50']:.2f}s  p95 {d['p95']:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
