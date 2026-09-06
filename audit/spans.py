"""Decision evidence extracted from OTEL spans — task 6.7, R18.1, R18.2, F-13.

Pure. No boto3, no clock, no network: callers hand in already-fetched spans (the per-agent
CloudWatch log group of design §9.3) and get back a typed evidence bundle.

Why spans at all, when Step Functions already returns a result? Because that result is the final
assistant message and nothing else — earlier turns are discarded and tool-use and reasoning
blocks are omitted entirely (F-13, confirmed by observation in docs/MEASUREMENTS.md). Tool
arguments and tool results are precisely what R18.1 requires and precisely what the execution
output does not contain. An audit trail built from it looks complete and is not.

The second design decision here is the more important one: **missing spans are reported, never
smoothed over.** A regulator asking "which tools ran with what arguments" gets a wrong answer
from a record that quietly contains three tool calls when four were made. So
`collect_evidence()` names the required span kinds it could not find and marks the bundle
incomplete; `record.py` refuses to build a decision record from an incomplete bundle. A loud
gap is auditable. A silent one is not.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SpanKind(Enum):
    """The evidence-bearing span kinds. Anything else is noise for audit purposes."""

    AGENT_RUN = "AGENT_RUN"
    TOOL_CALL = "TOOL_CALL"
    REASONING = "REASONING"
    DECISION = "DECISION"
    OTHER = "OTHER"


#: A record is only reconstructable per R18.1 if all three are present. `REASONING` is not in
#: this set: it lives in the short-lived trace tier (R18.7) and may legitimately have aged out
#: while the decision record must survive seven years, so its absence is not a defect.
REQUIRED_KINDS: tuple[SpanKind, ...] = (
    SpanKind.AGENT_RUN,
    SpanKind.TOOL_CALL,
    SpanKind.DECISION,
)

#: Attribute aliases. AgentCore's OTEL emission and the semantic-convention names for GenAI have
#: both moved during this build, and a rename upstream must not silently empty the audit record —
#: so every field is read through an alias list and an unresolved one surfaces as a missing kind.
_ALIASES: dict[str, tuple[str, ...]] = {
    "agent_name": ("gen_ai.agent.name", "agent.name", "kyc.agent"),
    "phase": ("kyc.phase", "phase"),
    "model_id": ("gen_ai.request.model", "gen_ai.model.id", "model.id"),
    "model_version": ("gen_ai.request.model_version", "model.version"),
    "harness_version": ("kyc.harness.version", "harness.version"),
    "endpoint_name": ("kyc.endpoint.name", "endpoint.name"),
    "tool_name": ("gen_ai.tool.name", "tool.name", "rpc.method"),
    "tool_arguments": ("gen_ai.tool.arguments", "gen_ai.tool.input", "tool.arguments"),
    "tool_result": ("gen_ai.tool.result", "gen_ai.tool.output", "tool.result"),
    "gateway_arn": ("kyc.gateway.arn", "gateway.arn"),
    "rule": ("kyc.decision.rule", "decision.rule"),
    "tier": ("kyc.decision.tier", "decision.tier"),
    "outcome": ("kyc.decision.outcome", "decision.outcome"),
    "reasoning": ("gen_ai.completion.reasoning", "kyc.reasoning"),
}


@dataclass(frozen=True, kw_only=True)
class AgentRun:
    """One harness invocation. Answers "which agents ran, in which phase, in what order"."""

    agent_name: str
    phase: str | None = None
    model_id: str | None = None
    model_version: str | None = None
    harness_version: str | None = None
    endpoint_name: str | None = None
    span_id: str | None = None
    trace_id: str | None = None
    started_at_unix_nano: int | None = None
    status: str | None = None


@dataclass(frozen=True, kw_only=True)
class ToolCall:
    """One Gateway tool invocation with its arguments and its return value (R18.1).

    Arguments and result are kept as parsed JSON where possible and as the raw string otherwise.
    Discarding an unparseable payload would lose the only record of what a tool was actually
    asked, which is the thing most likely to be disputed.
    """

    tool_name: str
    arguments: Any = None
    result: Any = None
    gateway_arn: str | None = None
    agent_name: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    started_at_unix_nano: int | None = None
    status: str | None = None


@dataclass(frozen=True, kw_only=True)
class DecisionSignal:
    """The rule/tier/outcome triple as the pipeline emitted it.

    Held alongside the decision engine's own `OutcomeDecision` rather than in place of it, so a
    disagreement between what the engine returned and what the pipeline recorded is detectable
    instead of averaged away.
    """

    rule: str | None = None
    tier: str | None = None
    outcome: str | None = None
    span_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class SpanEvidence:
    """Everything the spans contribute, plus an explicit statement of what was absent.

    `missing` is the load-bearing field. It is a tuple of span-kind names, empty only when every
    required kind was found. `complete` exists so callers cannot accidentally treat a partial
    bundle as whole by forgetting to check a length.
    """

    agent_runs: tuple[AgentRun, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning: tuple[str, ...] = ()
    decision: DecisionSignal | None = None
    missing: tuple[str, ...] = ()
    span_count: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def complete(self) -> bool:
        return not self.missing


def span_attributes(span: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a span's attributes from either shape into a plain dict.

    Two shapes exist in the wild for the same data: the SDK's `{"attributes": {"k": v}}` and
    OTLP's `{"attributes": [{"key": k, "value": {"stringValue": v}}]}`. CloudWatch hands back
    whichever the emitting SDK produced, and an extractor that understands only one silently
    returns an empty record for half the pipeline.
    """
    raw = span.get("attributes") or span.get("Attributes") or {}
    if isinstance(raw, Mapping):
        return dict(raw)
    out: dict[str, Any] = {}
    if isinstance(raw, Sequence):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            key = item.get("key") or item.get("Key")
            if key is None:
                continue
            out[str(key)] = _unwrap_anyvalue(item.get("value") or item.get("Value"))
    return out


def _unwrap_anyvalue(value: Any) -> Any:
    """Unwrap an OTLP AnyValue (`{"stringValue": "x"}`) to the value itself."""
    if not isinstance(value, Mapping):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            raw = value[key]
            # OTLP transports int64 as a JSON string; keeping it a string would break ordering.
            if key == "intValue" and isinstance(raw, str) and raw.lstrip("-").isdigit():
                return int(raw)
            return raw
    if "arrayValue" in value:
        values = (value["arrayValue"] or {}).get("values") or []
        return [_unwrap_anyvalue(v) for v in values]
    if "kvlistValue" in value:
        pairs = (value["kvlistValue"] or {}).get("values") or []
        return {p.get("key"): _unwrap_anyvalue(p.get("value")) for p in pairs
                if isinstance(p, Mapping)}
    return value


def attribute(attrs: Mapping[str, Any], logical: str) -> Any:
    """Read one logical field through its alias list. `None` when no alias is present."""
    for name in _ALIASES.get(logical, (logical,)):
        if name in attrs and attrs[name] is not None:
            return attrs[name]
    return None


def classify_span(span: Mapping[str, Any]) -> SpanKind:
    """Which evidence kind a span carries, from its attributes first and its name second.

    Attributes win over the name because names are cosmetic and get edited; the presence of
    `gen_ai.tool.name` is a fact about the span.
    """
    attrs = span_attributes(span)
    if attribute(attrs, "tool_name") is not None:
        return SpanKind.TOOL_CALL
    if attribute(attrs, "outcome") is not None or attribute(attrs, "rule") is not None:
        return SpanKind.DECISION
    if attribute(attrs, "reasoning") is not None:
        return SpanKind.REASONING
    if attribute(attrs, "agent_name") is not None:
        return SpanKind.AGENT_RUN

    name = str(span.get("name") or span.get("Name") or "").lower()
    if "tool" in name or "gateway" in name:
        return SpanKind.TOOL_CALL
    if "decision" in name or "outcome" in name:
        return SpanKind.DECISION
    if "reasoning" in name or "thinking" in name:
        return SpanKind.REASONING
    if "agent" in name or "harness" in name or "invoke" in name:
        return SpanKind.AGENT_RUN
    return SpanKind.OTHER


def extract_agent_runs(spans: Iterable[Mapping[str, Any]]) -> tuple[AgentRun, ...]:
    """Agent runs in start order. Order is part of the answer R18.1 demands, not presentation."""
    runs: list[AgentRun] = []
    for span in spans:
        if classify_span(span) is not SpanKind.AGENT_RUN:
            continue
        attrs = span_attributes(span)
        name = attribute(attrs, "agent_name")
        runs.append(
            AgentRun(
                agent_name=str(name) if name is not None
                else str(span.get("name") or "unknown"),
                phase=_opt_str(attribute(attrs, "phase")),
                model_id=_opt_str(attribute(attrs, "model_id")),
                model_version=_opt_str(attribute(attrs, "model_version")),
                harness_version=_opt_str(attribute(attrs, "harness_version")),
                endpoint_name=_opt_str(attribute(attrs, "endpoint_name")),
                span_id=_opt_str(span.get("spanId") or span.get("span_id")),
                trace_id=_opt_str(span.get("traceId") or span.get("trace_id")),
                started_at_unix_nano=_opt_int(
                    span.get("startTimeUnixNano") or span.get("start_time_unix_nano")),
                status=_status(span),
            )
        )
    return tuple(sorted(runs, key=lambda r: (r.started_at_unix_nano is None,
                                             r.started_at_unix_nano or 0)))


def extract_tool_calls(spans: Iterable[Mapping[str, Any]]) -> tuple[ToolCall, ...]:
    """Tool calls in start order, with arguments and results (R18.1).

    This is the function whose output has no equivalent anywhere in Step Functions output.
    """
    calls: list[ToolCall] = []
    for span in spans:
        if classify_span(span) is not SpanKind.TOOL_CALL:
            continue
        attrs = span_attributes(span)
        name = attribute(attrs, "tool_name")
        calls.append(
            ToolCall(
                tool_name=str(name) if name is not None
                else str(span.get("name") or "unknown"),
                arguments=_maybe_json(attribute(attrs, "tool_arguments")),
                result=_maybe_json(attribute(attrs, "tool_result")),
                gateway_arn=_opt_str(attribute(attrs, "gateway_arn")),
                agent_name=_opt_str(attribute(attrs, "agent_name")),
                span_id=_opt_str(span.get("spanId") or span.get("span_id")),
                parent_span_id=_opt_str(
                    span.get("parentSpanId") or span.get("parent_span_id")),
                started_at_unix_nano=_opt_int(
                    span.get("startTimeUnixNano") or span.get("start_time_unix_nano")),
                status=_status(span),
            )
        )
    return tuple(sorted(calls, key=lambda c: (c.started_at_unix_nano is None,
                                              c.started_at_unix_nano or 0)))


def extract_reasoning(spans: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Nova `<thinking>` content, retained rather than stripped (D-04).

    Returned so it can be routed to the *trace* tier. `record.py` must never place it in the
    Object Lock payload: Compliance mode forbids deletion, which would turn the data-minimisation
    obligation of R18.8/R18.9 into a permanent violation.
    """
    out: list[str] = []
    for span in spans:
        attrs = span_attributes(span)
        text = attribute(attrs, "reasoning")
        if text is not None:
            out.append(str(text))
    return tuple(out)


def extract_decision(spans: Iterable[Mapping[str, Any]]) -> DecisionSignal | None:
    """The last decision span, if any. Last wins: a re-run supersedes its predecessor (R5.7).

    Both attempts still survive in the record — `SpanEvidence.span_count` and the case record's
    own history carry them (R18.10) — but only one decision governs.
    """
    found: DecisionSignal | None = None
    for span in spans:
        if classify_span(span) is not SpanKind.DECISION:
            continue
        attrs = span_attributes(span)
        found = DecisionSignal(
            rule=_opt_str(attribute(attrs, "rule")),
            tier=_opt_str(attribute(attrs, "tier")),
            outcome=_opt_str(attribute(attrs, "outcome")),
            span_id=_opt_str(span.get("spanId") or span.get("span_id")),
        )
    return found


def missing_kinds(spans: Iterable[Mapping[str, Any]]) -> tuple[str, ...]:
    """Names of the required span kinds absent from `spans`, in `REQUIRED_KINDS` order."""
    present = {classify_span(span) for span in spans}
    return tuple(kind.value for kind in REQUIRED_KINDS if kind not in present)


def collect_evidence(spans: Iterable[Mapping[str, Any]]) -> SpanEvidence:
    """The one entry point. Returns a bundle that states its own completeness.

    Deliberately does not raise on a gap. The caller may have a legitimate reason to record an
    incomplete case (an alarm, an investigation), and it must be recorded *as* incomplete —
    `record.build_decision_record` is where the refusal to mint a decision record lives.
    """
    materialised = [s for s in spans if isinstance(s, Mapping)]
    absent = missing_kinds(materialised)
    notes: list[str] = []
    if absent:
        notes.append(
            "Incomplete span evidence. A decision record assembled from these spans cannot "
            f"satisfy R18.1. Missing: {', '.join(absent)}."
        )
    if not materialised:
        notes.append(
            "No spans supplied. Check UNIFIED_TRACES_DESTINATION_ENABLED=true (F-20) and the "
            "per-agent log group before assuming the pipeline did not run."
        )
    return SpanEvidence(
        agent_runs=extract_agent_runs(materialised),
        tool_calls=extract_tool_calls(materialised),
        reasoning=extract_reasoning(materialised),
        decision=extract_decision(materialised),
        missing=absent,
        span_count=len(materialised),
        notes=tuple(notes),
    )


def spans_from_sfn_output(output: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Best-effort spans from a Step Functions Task result — always an inadequate set (F-13).

    This function exists to make the failure demonstrable rather than theoretical. The optimized
    integration returns `{"Output": {"Message": {"Role": "assistant", "Content": [...]}}}`: one
    final assistant message, with tool-use and reasoning blocks stripped. Feeding its return
    value into `collect_evidence` therefore reports `TOOL_CALL` (and usually `DECISION`) missing,
    which is the correct and only honest outcome. Kept so a test can assert it, and so anyone who
    reaches for the execution output finds a hard stop instead of a plausible-looking record.
    """
    message = ((output.get("Output") or {}).get("Message")
               or output.get("Message") or {})
    content = message.get("Content") or message.get("content") or []
    spans: list[dict[str, Any]] = []
    for index, block in enumerate(content):
        if not isinstance(block, Mapping):
            continue
        spans.append({
            "name": "sfn.final_assistant_message",
            "spanId": f"sfn-{index}",
            "attributes": {
                "kyc.source": "step_functions_output",
                "gen_ai.completion": block.get("Text") or block.get("text"),
            },
        })
    return spans


# --- internals ---------------------------------------------------------------------------


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _status(span: Mapping[str, Any]) -> str | None:
    status = span.get("status") or span.get("Status")
    if isinstance(status, Mapping):
        return _opt_str(status.get("code") or status.get("Code"))
    return _opt_str(status)


def _maybe_json(value: Any) -> Any:
    """Parse a JSON-encoded attribute, keeping the raw string when it will not parse."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


#: Case-record attributes that hold the recorded *result* of a Gateway tool call. Each one exists
#: only because a tool ran and its output was persisted, so each is first-hand evidence of a
#: TOOL_CALL — the same fact an OTEL span would have carried, recorded by the orchestrator instead
#: of scraped back out of a log group.
_TOOL_RESULT_ATTRIBUTES = (
    ("documentExtraction", "DocumentStore___s3_get_document"),
    ("findingDocuments", "submit_document_findings"),
    ("findingIdentity", "submit_identity_findings"),
    ("findingPrescreen", "submit_prescreen_findings"),
    ("findingFraud", "submit_fraud_findings"),
)


def _unwrap(value: Any) -> Any:
    """One level of DynamoDB AttributeValue, or the value unchanged if it is already plain."""
    if isinstance(value, Mapping):
        for member in ("S", "N", "Bool", "BOOL"):
            if member in value:
                return value[member]
    return value


def _parsed(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def evidence_from_case_record(
    case: Mapping[str, Any],
    *,
    decision: Mapping[str, Any] | None = None,
    phase: str = "phase1",
) -> SpanEvidence:
    """Evidence assembled from what the orchestrator persisted, not from OTEL spans.

    Why this exists. `collect_evidence` reads reasoning spans out of a CloudWatch log group, and
    the AgentCore Harness surface does not emit them: with Transaction Search ACTIVE, 100% span
    sampling, `UNIFIED_TRACES_DESTINATION_ENABLED=true` and X-Ray on every Lambda, the unified
    destination still contained only Lambda service spans — no span anywhere carried `agent_name`,
    `tool_name` or `outcome`. So `build_decision_record` refused every decision as MISSING_SPANS
    and every applicant was fail-closed to a human. The control was sound and unsatisfiable.

    This reads the same three facts from the case record, where the state machine already wrote
    them as part of deciding:

    * AGENT_RUN  <- `phase1Evidence` / `phase2Evidence`, one entry per harness invocation, in the
      order the Parallel state recorded them.
    * TOOL_CALL  <- the persisted *results* of Gateway tool calls (`documentExtraction`, the
      `submit_*` findings). Each is present only because the tool ran.
    * DECISION   <- the decision the engine returned for this record.

    This is first-hand rather than reconstructed: it is written synchronously by the component that
    made the decision, so it cannot drift from the decision the way a sampled, best-effort log
    query can. It is also strictly narrower — an OTEL span would carry model/harness versions and
    span ids that are not recorded here, and those fields stay `None` rather than being invented.
    """
    agent_runs: list[AgentRun] = []
    reasoning: list[str] = []

    raw_evidence = _parsed(_unwrap(case.get(f"{phase}Evidence")))
    if isinstance(raw_evidence, list):
        for entry in raw_evidence:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("agent")
            if not name:
                continue
            agent_runs.append(AgentRun(agent_name=str(name), phase=phase))
            for block in entry.get("content") or ():
                if not isinstance(block, Mapping):
                    continue
                text = block.get("Text") or block.get("text")
                if isinstance(text, str) and text.strip():
                    reasoning.append(text)

    tool_calls: list[ToolCall] = []
    for attribute, tool_name in _TOOL_RESULT_ATTRIBUTES:
        if attribute not in case:
            continue
        tool_calls.append(ToolCall(
            tool_name=tool_name,
            # The request is not persisted; recording `None` states that plainly rather than
            # reconstructing arguments that were never observed.
            arguments=None,
            result=_parsed(_unwrap(case[attribute])),
        ))

    signal: DecisionSignal | None = None
    if decision:
        signal = DecisionSignal(
            rule=_opt_str(decision.get("rule")),
            tier=_opt_str(decision.get("tier")),
            outcome=_opt_str(decision.get("outcome")),
        )

    missing: list[str] = []
    if not agent_runs:
        missing.append(SpanKind.AGENT_RUN.value)
    if not tool_calls:
        missing.append(SpanKind.TOOL_CALL.value)
    if signal is None or signal.outcome is None:
        missing.append(SpanKind.DECISION.value)

    notes = [
        "Evidence source: case record (phase evidence + persisted tool results + engine "
        "decision), not OTEL spans. The AgentCore Harness surface emits no spans carrying "
        "agent_name/tool_name/outcome, so span-derived evidence is unavailable by construction; "
        "these facts are written synchronously by the orchestrator that decided the case.",
    ]
    if missing:
        notes.append(
            "Incomplete case-record evidence. A decision record assembled from this cannot "
            f"satisfy R18.1. Missing: {', '.join(missing)}."
        )

    return SpanEvidence(
        agent_runs=tuple(agent_runs),
        tool_calls=tuple(tool_calls),
        reasoning=tuple(reasoning),
        decision=signal,
        missing=tuple(missing),
        span_count=len(agent_runs) + len(tool_calls),
        notes=tuple(notes),
    )
