"""The immutable decision record — task 6.7, design §9.2, R18.1/R18.2/R18.6.

Pure functions only. No boto3 in this module, no clock, no randomness: `decided_at` and
`decision_id` are supplied by the caller so that the same inputs always produce the same record
and a record can be rebuilt years later from stored evidence. The single impure step — the S3
Object Lock write — lives in `writer.py`.

Two sources, never one (design §9.4):

    case record (DynamoDB)  →  what ran, what was decided, by whom, which versions
    OTEL spans (per-agent)  →  tool arguments, tool results, model reasoning

Step Functions output is not a third option and not a fallback. It carries the final assistant
message only (F-13), so a record built from it omits every tool call while still serialising
cleanly and looking finished. `build_decision_record` therefore refuses it explicitly rather
than trusting future readers to remember.

Reasoning text is deliberately absent from the payload. It is retained (D-04) but in the
short-lived trace tier, because this record goes to a Compliance-mode bucket where nothing can
be deleted for seven years — writing reasoning there would convert R18.9's deletion obligation
into a permanent breach (R18.8).

A reviewer's rationale is not model reasoning, and it is sealed here on purpose (D-42): R6.3 makes
it part of the decision, and R18.8 keeps it in this record and out of logs and execution history.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any

from spans import SpanEvidence

#: Bumped whenever the payload shape changes. Records are immutable for seven years, so readers
#: will encounter several shapes at once and must be able to tell which is which.
SCHEMA_VERSION = "1"


class EvidenceSource(Enum):
    """Where a record's contents came from. Recorded in the payload, and validated.

    `STEP_FUNCTIONS_OUTPUT` is enumerated so it can be *rejected by name*. Leaving it out would
    make the prohibition invisible at the point someone tries to violate it.
    """

    OTEL_SPANS = "OTEL_SPANS"
    CASE_RECORD = "CASE_RECORD"
    STEP_FUNCTIONS_OUTPUT = "STEP_FUNCTIONS_OUTPUT"


#: Both, always (R18.2). Either alone answers only half of R18.1.
REQUIRED_SOURCES: frozenset[EvidenceSource] = frozenset(
    {EvidenceSource.OTEL_SPANS, EvidenceSource.CASE_RECORD}
)


class IncompleteReason(Enum):
    """Why a record could not be minted. Machine-readable so an alarm can route on it."""

    MISSING_SPANS = "MISSING_SPANS"
    SFN_OUTPUT_NOT_AN_AUDIT_SOURCE = "SFN_OUTPUT_NOT_AN_AUDIT_SOURCE"
    MISSING_SOURCE = "MISSING_SOURCE"
    MISSING_VERSION_PINS = "MISSING_VERSION_PINS"
    MISSING_CASE_FIELDS = "MISSING_CASE_FIELDS"


@dataclass(frozen=True, kw_only=True)
class IncompleteRecord:
    """The explicit marker returned instead of a partial record.

    Returned rather than raised because the caller must still act — alarm, retry the span query,
    escalate — and an exception at this boundary tends to get caught and logged one frame up,
    which is how a gap becomes invisible. `writer.py` will not write this type, so an incomplete
    record cannot reach the Object Lock bucket by accident.
    """

    applicant_id: str
    phase: str
    reasons: tuple[IncompleteReason, ...]
    missing: tuple[str, ...] = ()
    detail: str = ""

    @property
    def complete(self) -> bool:
        return False


@dataclass(frozen=True, kw_only=True)
class VersionPins:
    """R18.6 — what was in force at decision time, not what is in force at read time.

    All four are mandatory. A decision explained against today's policy engine and today's
    Gateway ruleset is not an explanation of the decision that was made; the whole point of
    pinning is that thresholds and rulesets change (R12.7) and the record must outlive them.
    """

    harness_version: str
    endpoint_name: str
    policy_engine_version: str
    gateway_arn: str

    def missing_fields(self) -> tuple[str, ...]:
        return tuple(
            name for name in
            ("harness_version", "endpoint_name", "policy_engine_version", "gateway_arn")
            if not str(getattr(self, name) or "").strip()
        )


@dataclass(frozen=True, kw_only=True)
class DecisionRecord:
    """One decision, one object in S3 (design §9.2). Immutable in Python and in the bucket.

    `sources` is part of the payload, not metadata: a future reader must be able to see that this
    record was assembled from spans plus the case record and not from execution output (R18.2).
    """

    schema_version: str = SCHEMA_VERSION
    decision_id: str
    applicant_id: str
    case_id: str
    phase: str
    jurisdiction: str
    decided_at: str
    actor: str
    outcome: str
    rule: str
    reason_code: str | None = None
    explanation: str | None = None
    required_item: str | None = None
    notify_compliance: bool = False
    tier: str | None = None
    tier_rule: str | None = None
    tier_inputs: Mapping[str, Any] = field(default_factory=dict)
    thresholds_in_force: Mapping[str, Any] = field(default_factory=dict)
    edge_cases: tuple[str, ...] = ()
    agent_runs: tuple[Mapping[str, Any], ...] = ()
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    liveness: Mapping[str, Any] = field(default_factory=dict)
    documents: tuple[Mapping[str, Any], ...] = ()
    transitions: tuple[Mapping[str, Any], ...] = ()
    versions: VersionPins
    sources: tuple[str, ...] = ()
    span_count: int = 0
    # A-05/task 7.3, design §2/R20.1 — T2's queue leg, measured. `None` (not 0) whenever the
    # execution was started directly rather than via the ingress queue (the synchronous API
    # contract, replay/ops tooling): an unmeasured datum must never read as an instant one.
    ingress_latency_ms: float | None = None
    # The Step Functions execution that decided, so the record leads back to its timeline.
    execution_name: str | None = None
    # When a person decided: what the rules had concluded, why the case went to them, and the
    # screening alerts their approval cleared. Without these the record of an approval says that a
    # reviewer approved, not what they approved past.
    engine_outcome: str | None = None
    review_reason: str | None = None
    cleared_alerts: tuple[str, ...] = ()
    # D-42 — why the reviewer decided as they did, in their words. Read from the review the
    # handler recorded on the case (`<phase>Review`), never carried through the state machine, so
    # it stays out of execution history. Sealed here because the case copy is mutable, and Phase
    # 3's is removed when the next lifecycle review is queued.
    rationale: str | None = None
    # A Phase 2 decision names the provisional decision it supersedes, so the two immutable
    # records of one application join without the mutable case record.
    supersedes_decision_id: str | None = None

    def __post_init__(self) -> None:
        # Freezing the nested mappings closes the obvious hole in `frozen=True`: the dataclass
        # blocks rebinding a field but not mutating the dict behind it, and "immutable audit
        # record" has to mean the contents too.
        for name in ("tier_inputs", "thresholds_in_force", "liveness"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name) or {})))
        for name in ("agent_runs", "tool_calls", "documents", "transitions"):
            frozen = tuple(MappingProxyType(dict(item)) for item in getattr(self, name) or ())
            object.__setattr__(self, name, frozen)

    @property
    def complete(self) -> bool:
        return True


def build_decision_record(
    *,
    case_record: Mapping[str, Any],
    evidence: SpanEvidence,
    outcome: Mapping[str, Any],
    versions: VersionPins,
    decision_id: str,
    decided_at: str,
    phase: str,
    actor: str = "system",
    sources: Sequence[EvidenceSource] | None = None,
    ingress_latency_ms: float | None = None,
    execution_name: str | None = None,
    rationale: str | None = None,
) -> DecisionRecord | IncompleteRecord:
    """Assemble one decision record, or explain why it cannot be assembled.

    `outcome` is the decision engine's `OutcomeDecision` flattened by `outcome_to_mapping` —
    passed as a mapping rather than imported so this module stays independent of the
    decision-engine package and can rebuild historical records whose engine version is gone.

    Order of the checks matters: the source check runs first, because a caller who passed Step
    Functions output has a design error and telling them "TOOL_CALL spans missing" would send
    them looking for an infrastructure fault that does not exist.
    """
    applicant_id = str(case_record.get("applicantId") or "")
    supplied = frozenset(sources) if sources is not None else REQUIRED_SOURCES
    reasons: list[IncompleteReason] = []
    missing: list[str] = []
    details: list[str] = []

    if EvidenceSource.STEP_FUNCTIONS_OUTPUT in supplied:
        reasons.append(IncompleteReason.SFN_OUTPUT_NOT_AN_AUDIT_SOURCE)
        details.append(
            "Step Functions execution output is not an audit source (R18.2, F-13): it returns "
            "the final assistant message only and omits tool-use and reasoning blocks."
        )
    absent_sources = REQUIRED_SOURCES - supplied
    if absent_sources:
        reasons.append(IncompleteReason.MISSING_SOURCE)
        missing.extend(sorted(s.value for s in absent_sources))
        details.append("A record requires OTEL spans and the case record, both (R18.2).")

    if not evidence.complete:
        reasons.append(IncompleteReason.MISSING_SPANS)
        missing.extend(evidence.missing)
        details.extend(evidence.notes)

    absent_pins = versions.missing_fields()
    if absent_pins:
        reasons.append(IncompleteReason.MISSING_VERSION_PINS)
        missing.extend(absent_pins)
        details.append("R18.6 requires all four version pins in force at decision time.")

    absent_case = tuple(k for k in ("applicantId", "caseId", "jurisdiction")
                        if not str(case_record.get(k) or "").strip())
    if absent_case:
        reasons.append(IncompleteReason.MISSING_CASE_FIELDS)
        missing.extend(absent_case)

    if reasons:
        return IncompleteRecord(
            applicant_id=applicant_id or "unknown",
            phase=phase,
            reasons=tuple(reasons),
            missing=tuple(dict.fromkeys(missing)),
            detail=" ".join(details),
        )

    return DecisionRecord(
        decision_id=decision_id,
        applicant_id=applicant_id,
        case_id=str(case_record["caseId"]),
        phase=phase,
        jurisdiction=str(case_record["jurisdiction"]),
        decided_at=decided_at,
        actor=actor,
        outcome=str(outcome.get("outcome") or ""),
        rule=str(outcome.get("rule") or ""),
        reason_code=_opt_str(_either(outcome, "reason_code", "reasonCode")),
        explanation=_opt_str(outcome.get("explanation")),
        required_item=_opt_str(_either(outcome, "required_item", "requiredItem")),
        notify_compliance=bool(_either(outcome, "notify_compliance", "notifyCompliance")),
        tier=_opt_str(outcome.get("tier") or outcome.get("newTier")),
        tier_rule=_opt_str(_either(outcome, "tier_rule", "tierRule")),
        tier_inputs=dict(_either(outcome, "tier_inputs", "tierInputs") or {}),
        thresholds_in_force=dict(
            _either(outcome, "thresholds_in_force", "thresholdsInForce") or {}),
        edge_cases=tuple(str(e) for e in
                         _either(outcome, "edge_cases", "edgeCasesMatched") or ()),
        agent_runs=tuple(_agent_run_payload(r) for r in evidence.agent_runs),
        tool_calls=tuple(_tool_call_payload(c) for c in evidence.tool_calls),
        liveness=_liveness_payload(case_record.get("liveness")),
        documents=tuple(dict(d) for d in case_record.get("documents") or ()
                        if isinstance(d, Mapping)),
        transitions=tuple(dict(t) for t in case_record.get("transitions") or ()
                          if isinstance(t, Mapping)),
        versions=versions,
        sources=tuple(sorted(s.value for s in supplied)),
        span_count=evidence.span_count,
        ingress_latency_ms=ingress_latency_ms,
        execution_name=_opt_str(execution_name),
        engine_outcome=_opt_str(_either(outcome, "engine_outcome", "engineOutcome")),
        review_reason=_opt_str(_either(outcome, "review_reason", "reviewReason")),
        cleared_alerts=tuple(str(a) for a in
                             _either(outcome, "cleared_alerts", "clearedAlerts") or ()),
        rationale=(str(rationale).strip() or None) if rationale is not None else None,
        supersedes_decision_id=_opt_str(
            _either(outcome, "supersedes_decision_id", "supersedesDecisionId")),
    )


def _either(outcome: Mapping[str, Any], snake: str, camel: str) -> Any:
    """A field in either spelling. The state machine passes the engine's response as it is,
    in camelCase, and reading only the snake_case name silently dropped the tier inputs,
    thresholds and reason code from every record written from it."""
    value = outcome.get(snake)
    return value if value is not None else outcome.get(camel)


def outcome_to_mapping(decision: Any) -> dict[str, Any]:
    """Flatten a decision-engine `OutcomeDecision` into plain JSON-able values.

    Duck-typed on purpose. This module must be able to read a record's outcome without importing
    the engine — the engine's enums will have been renamed long before the seven years are up,
    and a record that can only be rebuilt by the code that wrote it is not an audit record.
    """
    tier = getattr(decision, "tier", None)
    signals = dict(getattr(decision, "recorded_signals", {}) or {})
    return {
        "outcome": _enum_value(getattr(decision, "outcome", None)),
        "rule": getattr(decision, "rule", None),
        "reason_code": getattr(decision, "reason_code", None),
        "explanation": getattr(decision, "explanation", None),
        "required_item": getattr(decision, "required_item", None),
        "notify_compliance": bool(getattr(decision, "notify_compliance", False)),
        "tier": _enum_value(getattr(tier, "tier", None)),
        "tier_rule": getattr(tier, "rule", None),
        "tier_inputs": dict(getattr(tier, "inputs", {}) or {}),
        "thresholds_in_force": dict(getattr(tier, "thresholds", {}) or {}),
        "config_version": getattr(tier, "config_version", None),
        "edge_cases": tuple(getattr(decision, "edge_cases", ()) or ()),
        "recorded_signals": signals,
    }


def decision_object_key(
    *, applicant_id: str, phase: str, decision_id: str
) -> str:
    """`{applicantId}/{phase}/{decisionId}.json` — design §9.2.

    Path components are validated rather than escaped. An applicant id containing `../` would
    write outside its own prefix, and under Object Lock a misplaced object cannot be moved or
    deleted for seven years, so there is no recovering from getting this wrong once.
    """
    for label, value in (("applicant_id", applicant_id), ("phase", phase),
                         ("decision_id", decision_id)):
        text = str(value or "")
        if not text or "/" in text or ".." in text or text.strip() != text:
            raise ValueError(f"unsafe {label} for an Object Lock key: {value!r}")
    return f"{applicant_id}/{phase}/{decision_id}.json"


def to_payload(record: DecisionRecord) -> dict[str, Any]:
    """The record as nested plain dicts, ready to serialise."""
    return {
        "schemaVersion": record.schema_version,
        "decisionId": record.decision_id,
        "applicantId": record.applicant_id,
        "caseId": record.case_id,
        "phase": record.phase,
        "jurisdiction": record.jurisdiction,
        "decidedAt": record.decided_at,
        "actor": record.actor,
        "decision": {
            "outcome": record.outcome,
            "rule": record.rule,
            "reasonCode": record.reason_code,
            "explanation": record.explanation,
            "requiredItem": record.required_item,
            "notifyCompliance": record.notify_compliance,
            "tier": record.tier,
            "tierRule": record.tier_rule,
            "tierInputs": dict(record.tier_inputs),
            "thresholdsInForce": dict(record.thresholds_in_force),
            "edgeCases": list(record.edge_cases),
            "engineOutcome": record.engine_outcome,
            "reviewReason": record.review_reason,
            "clearedAlerts": list(record.cleared_alerts),
            "rationale": record.rationale,
        },
        "executionName": record.execution_name,
        "supersedesDecisionId": record.supersedes_decision_id,
        "evidence": {
            "agentRuns": [dict(r) for r in record.agent_runs],
            "toolCalls": [dict(c) for c in record.tool_calls],
            "liveness": dict(record.liveness),
            "documents": [dict(d) for d in record.documents],
            "transitions": [dict(t) for t in record.transitions],
            "spanCount": record.span_count,
            "sources": list(record.sources),
        },
        "versions": {
            "harnessVersion": record.versions.harness_version,
            "endpointName": record.versions.endpoint_name,
            "policyEngineVersion": record.versions.policy_engine_version,
            "gatewayArn": record.versions.gateway_arn,
        },
        # A-05/task 7.3. `None` on the direct-StartExecution path — see field docstring.
        "ingressLatencyMs": record.ingress_latency_ms,
    }


def to_json_bytes(record: DecisionRecord) -> bytes:
    """Canonical serialisation: sorted keys, no ASCII escaping surprises, trailing newline.

    Deterministic because the object's checksum is the only integrity evidence available once
    Object Lock has sealed it — two byte-different serialisations of one decision cannot be
    reconciled after the fact.
    """
    return (json.dumps(to_payload(record), sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), default=str) + "\n").encode("utf-8")


# --- internals ---------------------------------------------------------------------------


def _agent_run_payload(run: Any) -> dict[str, Any]:
    return {
        "agentName": run.agent_name,
        "phase": run.phase,
        "modelId": run.model_id,
        "modelVersion": run.model_version,
        "harnessVersion": run.harness_version,
        "endpointName": run.endpoint_name,
        "spanId": run.span_id,
        "traceId": run.trace_id,
        "startedAtUnixNano": run.started_at_unix_nano,
        "status": run.status,
    }


def _tool_call_payload(call: Any) -> dict[str, Any]:
    return {
        "toolName": call.tool_name,
        "arguments": call.arguments,
        "result": call.result,
        "gatewayArn": call.gateway_arn,
        "agentName": call.agent_name,
        "spanId": call.span_id,
        "parentSpanId": call.parent_span_id,
        "startedAtUnixNano": call.started_at_unix_nano,
        "status": call.status,
    }


def _liveness_payload(liveness: Any) -> dict[str, Any]:
    """Liveness minus the image bytes, plus the cross-border fields R17.6 requires.

    `sessionId` is kept even though it resolves to nothing by the time anything reads it (F-40) —
    it is the only link back to the Rekognition call. Image *keys* are kept, never image content:
    faces are the one real-PII path in this system (R36) and Compliance mode is forever.
    """
    if not isinstance(liveness, Mapping):
        return {}
    return {
        "result": liveness.get("result"),
        "region": liveness.get("region"),
        "sessionId": liveness.get("sessionId"),
        "retrievedAt": liveness.get("retrievedAt"),
        "referenceImageKey": liveness.get("referenceImageKey"),
        "auditImageKeys": list(liveness.get("auditImageKeys") or ()),
    }


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)
