"""Is it safe to switch Cedar from LOG_ONLY to ENFORCE? Read what the policy engines decided.

    ./.venv/bin/python scripts/check_policy_decisions.py              # the last 24 hours
    ./.venv/bin/python scripts/check_policy_decisions.py --hours 72 --spans

The Gateways attach their policy engines in LOG_ONLY (`gateways_stack.py`): every tool call is
evaluated and the decision recorded, nothing is blocked. This reads those decisions back:

* **CloudWatch metrics**, namespace `AWS/Bedrock-AgentCore`, per policy engine: `AllowDecisions`,
  `DenyDecisions`, `NoDeterminingPolicies` (denied because no policy permitted it — default
  deny) and `MismatchErrors` (a policy failed on a missing attribute or a type mismatch).
* **With `--spans`, the denied calls themselves** — from the `aws/spans` log group, where the
  Gateway writes `aws.agentcore.policy.authorization_decision` and the policies that determined
  it, when tracing is enabled on the Gateway.

Verdict and exit status:

* 0 — **safe to enforce**: traffic was observed and no call was denied or failed a policy. Engines
  that saw no traffic are listed: they are unverified, and ENFORCE on them is a guess.
* 1 — **not safe**: at least one call would have been denied. The clean case would then lose a
  tool on stage, degrade, and go to a reviewer instead of opening an account.
* 2 — **no evidence**: nothing was evaluated in the window. Run the clean case end to end first.

Read-only. Metric names and span attributes are AWS's (Bedrock AgentCore developer guide, "Policy
in AgentCore observability data"); if the service renames them this reports no evidence rather
than a false all-clear.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

NAMESPACE = "AWS/Bedrock-AgentCore"
ALLOW = "AllowDecisions"
DENIALS = ("DenyDecisions", "NoDeterminingPolicies", "MismatchErrors")
METRICS = (ALLOW, *DENIALS)

#: The engines this build creates (`gateways_stack.py`: `kyc90_policy_{jurisdiction}`).
ENGINE_PREFIX = "kyc90_policy"

SPAN_QUERY = """
fields @timestamp,
  `attributes.aws.agentcore.policy.authorization_decision` as decision,
  `attributes.aws.agentcore.policy.authorization_reason` as reason,
  `attributes.aws.agentcore.policy.determining_policies` as policies,
  `attributes.aws.agentcore.policy.mismatched_policies` as mismatched,
  `attributes.aws.agentcore.policy.target_resource.id` as gateway,
  `attributes.aws.agentcore.gateway.policy.mode` as mode
| filter decision = "DENY"
| sort @timestamp desc
| limit 50
"""


@dataclass
class EngineDecisions:
    engine: str
    counts: dict[str, float] = field(default_factory=dict)
    tools: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def evaluated(self) -> float:
        return self.counts.get(ALLOW, 0) + self.counts.get("DenyDecisions", 0)

    @property
    def denied(self) -> float:
        return sum(self.counts.get(m, 0) for m in DENIALS)


@dataclass
class Verdict:
    status: str             # SAFE_TO_ENFORCE | NOT_SAFE | NO_EVIDENCE
    exit_code: int
    lines: list[str]


def decide(engines: Iterable[EngineDecisions], deny_spans: Iterable[Mapping[str, Any]] = ()
           ) -> Verdict:
    """Pure: the verdict from what was read. Unit-tested without AWS."""
    engines = sorted(engines, key=lambda e: e.engine)
    spans = list(deny_spans)
    lines: list[str] = []
    for e in engines:
        detail = ", ".join(f"{m} {e.counts.get(m, 0):.0f}" for m in METRICS)
        lines.append(f"{e.engine}: {detail}")
        for tool, counts in sorted(e.tools.items()):
            denied = sum(counts.get(m, 0) for m in DENIALS)
            if denied:
                lines.append(f"    {tool}: {denied:.0f} denied")
    for s in spans:
        lines.append(f"DENY {s.get('@timestamp', '')} gateway={s.get('gateway', '?')} "
                     f"policies={s.get('policies', '-')} reason={s.get('reason', '-')}")

    seen = [e for e in engines if e.evaluated > 0]
    silent = [e.engine for e in engines if e.evaluated == 0]
    if any(e.denied > 0 for e in engines) or spans:
        return Verdict("NOT_SAFE", 1, lines + [
            "NOT SAFE: calls were denied or failed a policy. Fix the policies (or the principal "
            "they name) and re-run the clean case before switching to ENFORCE."])
    if not seen:
        return Verdict("NO_EVIDENCE", 2, lines + [
            "NO EVIDENCE: no tool call was evaluated in this window. Run the clean case end to "
            "end, then check again."])
    if silent:
        lines.append("UNVERIFIED (no traffic in the window): " + ", ".join(silent))
    return Verdict("SAFE_TO_ENFORCE", 0, lines + [
        "SAFE TO ENFORCE for the engines with traffic: every evaluated call was allowed. Switch "
        "with `make deploy ENFORCEMENT=ENFORCE` (see docs/RUNBOOK.md)."])


# ----------------------------------------------------------------------------------- AWS
#: Dimensions that partition a rollup (summing across their values is a total). Any other
#: dimension — Policy, Category, Filter — splits it differently and is not summed in.
_ROLLUP_DIMENSIONS = frozenset({"PolicyEngine", "OperationName", "Mode", "TargetResource"})


def rollup(series: Iterable[tuple[Mapping[str, str], float]]) -> tuple[float, dict[str, float]]:
    """One engine's series for one metric -> (engine total, per-tool totals).

    CloudWatch publishes the same counts under several dimension sets, so summing every series
    would count one decision two or three times. The engine total is the sum over the smallest
    dimension set present (the rollup); per-tool totals come from the series that carry ToolName.
    """
    series = list(series)
    tools: dict[str, float] = {}
    for dims, total in series:
        if "ToolName" in dims and set(dims) - {"ToolName"} <= _ROLLUP_DIMENSIONS:
            tools[dims["ToolName"]] = tools.get(dims["ToolName"], 0) + total
    rollups = [(frozenset(d), t) for d, t in series if set(d) <= _ROLLUP_DIMENSIONS]
    if rollups:
        smallest = min(len(names) for names, _ in rollups)
        total = sum(t for names, t in rollups if len(names) == smallest)
    else:
        total = sum(tools.values())
    return total, tools


def read_metrics(cloudwatch: Any, *, start: datetime, end: datetime,
                 prefix: str = ENGINE_PREFIX) -> list[EngineDecisions]:
    """Each decision metric per engine, and per tool where the metric carries one."""
    period = max(60, int((end - start).total_seconds()) // 60 * 60)
    found: dict[tuple[str, str], list[tuple[dict[str, str], float]]] = {}
    for metric in METRICS:
        kwargs: dict[str, Any] = {"Namespace": NAMESPACE, "MetricName": metric}
        while True:
            listed = cloudwatch.list_metrics(**kwargs)
            for m in listed.get("Metrics", []):
                dims = {d["Name"]: d["Value"] for d in m.get("Dimensions", [])}
                engine = dims.get("PolicyEngine", "")
                if not engine or (prefix and prefix not in engine):
                    continue
                stats = cloudwatch.get_metric_statistics(
                    Namespace=NAMESPACE, MetricName=metric, Dimensions=m["Dimensions"],
                    StartTime=start, EndTime=end, Period=period, Statistics=["Sum"])
                total = sum(p.get("Sum", 0) for p in stats.get("Datapoints", []))
                found.setdefault((engine, metric), []).append((dims, total))
            token = listed.get("NextToken")
            if not token:
                break
            kwargs["NextToken"] = token

    engines: dict[str, EngineDecisions] = {}
    for (engine, metric), series in found.items():
        record = engines.setdefault(engine, EngineDecisions(engine))
        total, tools = rollup(series)
        record.counts[metric] = total
        for tool, count in tools.items():
            record.tools.setdefault(tool, {})[metric] = count
    return list(engines.values())


def read_deny_spans(logs: Any, *, start: datetime, end: datetime,
                    log_group: str = "aws/spans", timeout_s: int = 60) -> list[dict[str, Any]]:
    query = logs.start_query(logGroupName=log_group, queryString=SPAN_QUERY,
                             startTime=int(start.timestamp()), endTime=int(end.timestamp()))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = logs.get_query_results(queryId=query["queryId"])
        if result.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
            return [{f["field"]: f["value"] for f in row} for row in result.get("results", [])]
        time.sleep(1)
    raise TimeoutError("the span query did not finish")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--engine-prefix", default=ENGINE_PREFIX)
    parser.add_argument("--spans", action="store_true",
                        help="also list denied calls from the aws/spans log group")
    args = parser.parse_args(argv)

    import boto3

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)
    engines = read_metrics(boto3.client("cloudwatch"), start=start, end=end,
                           prefix=args.engine_prefix)
    spans: list[dict[str, Any]] = []
    if args.spans:
        try:
            spans = read_deny_spans(boto3.client("logs"), start=start, end=end)
        except Exception as exc:  # noqa: BLE001 - metrics still decide; say what was missed
            print(f"warning: span query failed ({type(exc).__name__}: {exc}); "
                  "the verdict rests on metrics alone")
    verdict = decide(engines, spans)
    print(f"Policy decisions {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC")
    for line in verdict.lines:
        print(line)
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main())
