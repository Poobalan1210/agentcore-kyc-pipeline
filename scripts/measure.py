"""Time to decision, agent latency and model cost, from the cases the deployed system decided.

    ./.venv/bin/python scripts/measure.py                          # every case in the table
    ./.venv/bin/python scripts/measure.py --since 2026-12-01T00:00:00Z --price-per-mtok 0.35
    ./.venv/bin/python scripts/measure.py --prefix APP-LOADTEST-20261201T1700 --json run.json

Read-only: one paginated `Scan` of the case table, projected to the attributes it needs. The
figures come from timestamps and usage the system wrote (`measurement.py`), so they are the
system's own measurement rather than a stopwatch's. Paste the Markdown it prints into
`docs/MEASUREMENTS.md`.

Run it against rehearsal traffic in the deployed account; nothing here can measure a deployment
it is not pointed at, and this repository has not been deployed from this branch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measurement import parse_time, render, summarise  # noqa: E402

#: Only what the figures need. The case record also holds identity and facts; none is read here.
PROJECTION = ("applicantId, createdAt, resubmittedAt, provisionalDecidedAt, fullDecidedAt, "
              "phase1Evidence, phase2Evidence, jurisdiction, #s")


def scan_cases(table: str, *, client: Any = None) -> Iterator[dict[str, Any]]:
    if client is None:
        import boto3

        client = boto3.client("dynamodb")
    kwargs: dict[str, Any] = {
        "TableName": table,
        "ProjectionExpression": PROJECTION,
        "ExpressionAttributeNames": {"#s": "state"},
        "FilterExpression": "sk = :case",
        "ExpressionAttributeValues": {":case": {"S": "CASE"}},
    }
    while True:
        resp = client.scan(**kwargs)
        yield from resp.get("Items", [])
        if not resp.get("LastEvaluatedKey"):
            return
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def select(items: list[dict[str, Any]], *, since: str | None, prefix: str | None,
           include_loadtest: bool) -> list[dict[str, Any]]:
    """The cases to measure. Load-test cases are excluded unless asked for, so a rehearsal
    figure and a load-test figure are never silently mixed."""
    cutoff = parse_time(since) if since else None
    out = []
    for item in items:
        applicant = (item.get("applicantId") or {}).get("S", "")
        if prefix and not applicant.startswith(prefix):
            continue
        if not include_loadtest and not prefix and applicant.startswith("APP-LOADTEST-"):
            continue
        created = parse_time((item.get("createdAt") or {}).get("S"))
        if cutoff and (created is None or created < cutoff):
            continue
        out.append(item)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--table", default="kyc90-cases")
    parser.add_argument("--since", help="only cases created at or after this ISO time")
    parser.add_argument("--prefix", help="only applicant ids starting with this")
    parser.add_argument("--include-loadtest", action="store_true",
                        help="include APP-LOADTEST- cases when no --prefix is given")
    parser.add_argument("--price-per-mtok", type=float,
                        help="blended model price, USD per million tokens, for the cost line")
    parser.add_argument("--json", type=Path, help="also write the summary here")
    args = parser.parse_args(argv)

    cases = select(list(scan_cases(args.table)), since=args.since, prefix=args.prefix,
                   include_loadtest=args.include_loadtest)
    summary = summarise(cases, price_per_million_tokens=args.price_per_mtok)
    print(render(summary))
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2))
    return 0 if summary["decided"] else 2


if __name__ == "__main__":
    sys.exit(main())
