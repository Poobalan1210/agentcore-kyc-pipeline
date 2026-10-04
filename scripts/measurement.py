"""How long a decision took and what it cost, computed from what the system recorded.

Pure: no AWS, no clock. `measure.py` reads case records, `export_executions.py` reads execution
histories and `loadtest.py` reads the cases it started; all three hand them to these functions,
so a number on a slide comes from one implementation, tested once.

Two sources, never a stopwatch:

* **Time to decision (T1)** — from when the attempt was submitted (`createdAt`, or
  `resubmittedAt` after a step-up) to `provisionalDecidedAt`, both written by the system: the
  first by the intake API, the second in the same update that opens the case.
* **Model usage** — `totalTokens` and `latencyMs` per agent run, which the state machine records
  in `phase1Evidence` / `phase2Evidence` from the harness response.

Cost is an estimate and says so. Only total tokens are recorded, not the input/output split, so
the caller supplies one blended price per million tokens for the models they deployed; nothing
here knows a price.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

#: The claim being measured.
TARGET_SECONDS = 90.0


def parse_time(value: Any) -> datetime | None:
    """An ISO-8601 timestamp as the system writes it (`...Z`), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def seconds_between(start: Any, end: Any) -> float | None:
    began, ended = parse_time(start), parse_time(end)
    if began is None or ended is None:
        return None
    seconds = (ended - began).total_seconds()
    return seconds if seconds >= 0 else None


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (the common "type 7" definition).

    Stated because it matters at demo-sized samples: with 20 runs, p95 interpolates between the
    19th and 20th values, and a nearest-rank p95 would simply be the slowest run.
    """
    if not values:
        return None
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * p / 100
    low, high = math.floor(rank), math.ceil(rank)
    return float(ordered[low] + (ordered[high] - ordered[low]) * (rank - low))


def plain(value: Any) -> Any:
    """A DynamoDB AttributeValue to plain Python; plain values pass through unchanged."""
    if not isinstance(value, Mapping) or len(value) != 1:
        return value
    (kind, inner), = value.items()
    if kind == "S":
        return inner
    if kind == "N":
        number = float(inner)
        return int(number) if number.is_integer() else number
    if kind in ("BOOL", "NULL"):
        return None if kind == "NULL" else bool(inner)
    if kind == "M":
        return {k: plain(v) for k, v in inner.items()}
    if kind == "L":
        return [plain(v) for v in inner]
    return value


def plain_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: plain(v) for k, v in item.items()}


def t1_seconds(case: Mapping[str, Any]) -> float | None:
    """Submission to provisional decision, for one case (plain or wire-format item)."""
    case = plain_item(case)
    submitted = case.get("resubmittedAt") or case.get("createdAt")
    return seconds_between(submitted, case.get("provisionalDecidedAt"))


def agent_runs(case: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every agent run the state machine recorded on the case, with its latency and tokens."""
    case = plain_item(case)
    runs = []
    for phase in ("phase1", "phase2", "phase3"):
        raw = case.get(f"{phase}Evidence")
        try:
            entries = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        for entry in entries or ():
            if not isinstance(entry, Mapping) or not entry.get("agent"):
                continue
            runs.append({
                "phase": phase,
                "agent": str(entry["agent"]),
                "status": entry.get("status"),
                "latencyMs": _number(entry.get("latencyMs")),
                "totalTokens": _number(entry.get("totalTokens")),
            })
    return runs


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarise(cases: Iterable[Mapping[str, Any]], *,
              price_per_million_tokens: float | None = None) -> dict[str, Any]:
    """The figures for one set of cases: T1 distribution, agent latency, tokens and cost."""
    cases = [plain_item(c) for c in cases]
    decided = [s for s in (t1_seconds(c) for c in cases) if s is not None]
    runs = [r for c in cases for r in agent_runs(c)]

    by_agent: dict[str, list[float]] = {}
    for r in runs:
        if r["latencyMs"] is not None and r["status"] == "OK":
            by_agent.setdefault(f"{r['phase']}:{r['agent']}", []).append(r["latencyMs"] / 1000)

    tokens_per_case = [sum(r["totalTokens"] or 0 for r in agent_runs(c)) for c in cases
                       if agent_runs(c)]
    mean_tokens = (sum(tokens_per_case) / len(tokens_per_case)) if tokens_per_case else None

    summary: dict[str, Any] = {
        "cases": len(cases),
        "decided": len(decided),
        "t1": {
            "p50": percentile(decided, 50),
            "p95": percentile(decided, 95),
            "max": max(decided) if decided else None,
            "underTarget": (sum(1 for s in decided if s <= TARGET_SECONDS) / len(decided)
                            if decided else None),
            "targetSeconds": TARGET_SECONDS,
        },
        "agents": {name: {"runs": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95)}
                   for name, v in sorted(by_agent.items())},
        "degradedRuns": sum(1 for r in runs if r["status"] != "OK"),
        "tokensPerCase": mean_tokens,
        "pricePerMillionTokens": price_per_million_tokens,
        "modelCostPerCase": (mean_tokens * price_per_million_tokens / 1_000_000
                             if mean_tokens is not None and price_per_million_tokens is not None
                             else None),
    }
    return summary


def render(summary: Mapping[str, Any]) -> str:
    """The summary as a Markdown block for `docs/MEASUREMENTS.md`."""
    def fmt(value: Any, unit: str = "s", digits: int = 1) -> str:
        if value is None:
            return "—"
        return f"{value:.{digits}f}{unit}"

    t1 = summary["t1"]
    within = "—" if t1["underTarget"] is None else f"{t1['underTarget'] * 100:.0f}%"
    lines = [
        f"Cases: {summary['cases']}, decided: {summary['decided']}",
        "",
        "| Time to provisional decision | Value |",
        "|---|---|",
        f"| p50 | {fmt(t1['p50'])} |",
        f"| p95 | {fmt(t1['p95'])} |",
        f"| slowest | {fmt(t1['max'])} |",
        f"| within {t1['targetSeconds']:.0f}s | {within} |",
    ]
    if summary["agents"]:
        lines += ["", "| Agent | Runs | p50 | p95 |", "|---|---|---|---|"]
        for name, a in summary["agents"].items():
            lines.append(f"| {name} | {a['runs']} | {fmt(a['p50'])} | {fmt(a['p95'])} |")
    lines += ["", f"Degraded agent runs: {summary['degradedRuns']}"]
    if summary["tokensPerCase"] is not None:
        lines.append(f"Tokens per case (all agents, all phases): {summary['tokensPerCase']:.0f}")
    if summary["modelCostPerCase"] is not None:
        lines.append(
            f"Estimated model cost per case: ${summary['modelCostPerCase']:.4f} at "
            f"${summary['pricePerMillionTokens']}/1M tokens (blended; the input/output split is "
            "not recorded)")
    return "\n".join(lines)


def step_timeline(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Step Functions history events -> one row per state, with its duration.

    Same reading as the reviewer console's timeline (`api/review_api.py`), plus durations, so
    "where do the 90 seconds go" is answered from the execution itself.
    """
    steps: list[dict[str, Any]] = []
    open_steps: dict[str, dict[str, Any]] = {}
    for ev in events:
        etype = str(ev.get("type") or "")
        stamp = ev.get("timestamp")
        stamp = stamp.isoformat() if hasattr(stamp, "isoformat") else stamp
        if etype.endswith("StateEntered"):
            name = (ev.get("stateEnteredEventDetails") or {}).get("name", "")
            open_steps[name] = {"name": name, "type": etype[:-len("StateEntered")],
                                "startedAt": stamp, "status": "RUNNING"}
        elif etype.endswith("StateExited"):
            name = (ev.get("stateExitedEventDetails") or {}).get("name", "")
            step = open_steps.pop(name, {"name": name, "status": "RUNNING", "startedAt": None})
            step["endedAt"] = stamp
            if step["status"] == "RUNNING":
                step["status"] = "SUCCEEDED"
            seconds = seconds_between(step.get("startedAt"), stamp)
            step["seconds"] = round(seconds, 3) if seconds is not None else None
            steps.append(step)
        elif etype in ("TaskFailed", "ExecutionFailed", "LambdaFunctionFailed",
                       "TaskTimedOut", "ExecutionTimedOut"):
            for step in open_steps.values():
                step["status"] = "FAILED"
    steps.extend(open_steps.values())
    return steps


def state_durations(executions: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """p50/p95 seconds per state name across executions (each with a `steps` list)."""
    by_state: dict[str, list[float]] = {}
    for execution in executions:
        for step in execution.get("steps") or ():
            if step.get("seconds") is not None:
                by_state.setdefault(step["name"], []).append(float(step["seconds"]))
    return {name: {"count": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95)}
            for name, v in sorted(by_state.items(), key=lambda kv: -max(kv[1]))}
