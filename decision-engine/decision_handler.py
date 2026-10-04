"""Lambda entry point for the decision engine — task 6.1.

This is the **only** impure module in the package, and it is deliberately thin: parse, delegate,
serialise. All judgement lives in `facts.py`, `tiering.py`, `outcomes.py`, `edge_cases.py`,
`identity_match.py` and `lifecycle.py`, which import nothing that can touch a clock, a network or a
disk. `test_tiering.TestPurity` enforces that by parsing the source, and it excludes this file by
name.

Named `decision_handler.py` rather than `handler.py` because `tools/handler.py` already exists and
both directories sit on `sys.path`. Two modules called `handler` would resolve by path order,
which is exactly the kind of dependency that works until someone reorders a config file.

**What the engine reads.** The state machine passes `facts`: the results the evidence tools
recorded on the case record. The engine derives every tier input from those (`facts.py`) and reads
nothing an agent said. Agent results arrive only as an availability signal — whether each agent
ran and returned anything — never as a source of values. The prose-parsing path that used to sit
here (`signals.py`) is gone: it made outcomes depend on a model's wording, and an agent that called
an unavailable source "no match" could have approved someone.

Callers that pass explicit tier fields instead of `facts` (the unit tests, replay tooling) are held
to the strict rule: every field required, nothing defaulted.

Keeping the boundary this sharp is what makes R4.10's "1,000 identical inputs, 1,000 identical
outputs" a property of the code rather than an observation about one run.
"""

from __future__ import annotations

import json
from typing import Any

from edge_cases import EdgeFacts, evaluate_edge_cases
from facts import read_facts
from identity_match import compare as compare_identity
from identity_match import verdicts as identity_verdicts
from lifecycle import Day0Snapshot, history_sort_key, idempotency_name, reassess
from models import (
    FullInputs,
    LifecycleDecision,
    MatchStrength,
    PepStatus,
    PreliminaryInputs,
    RiskEvent,
    RiskEventSeverity,
    RiskTier,
    ScreeningHit,
    Severity,
    VelocitySignals,
)
from outcomes import select_phase1_outcome, select_phase2_outcome
from tiering import full_tier, preliminary_tier, tier_action


class InputError(ValueError):
    """The event could not be read. Distinct from a decision, so the caller cannot confuse a
    malformed request with a risk outcome — silently defaulting a missing field would produce a
    confident decision from evidence that was never supplied."""


def lambda_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Route on `phase`. Returns the decision plus everything R4.9 / R18.6 need for the audit."""
    phase = event.get("phase")
    if phase not in (1, 2, 3, "1", "2", "3"):
        raise InputError(f"phase must be 1, 2 or 3, got {phase!r}")
    phase = int(phase)

    agent_results = event.get("agentResults") or {}
    if not isinstance(agent_results, dict):
        raise InputError("agentResults must be an object keyed by agent name")

    event = dict(event)
    agent_results = dict(agent_results)
    declared = _plain_mapping(event.get("declaredIdentity"))

    reading = None
    raw_facts = event.get("facts")
    if raw_facts is not None:
        if not isinstance(raw_facts, dict):
            raise InputError("facts must be an object keyed by fact name")
        as_of = event.get("asOf")
        if phase == 1 and not as_of:
            # Expiry and age are computed against it. Without it the person rules cannot run, and a
            # decision that silently skipped them would look complete.
            raise InputError("asOf is required when deciding from facts")
        current_list = event.get("currentListVersion")
        if current_list is not None and (not isinstance(current_list, str)
                                         or not current_list.strip()):
            raise InputError("currentListVersion must be a non-empty string when supplied")
        reading = read_facts(
            phase=phase, facts=raw_facts, declared=declared, as_of=as_of,
            phase_started_at=event.get("phaseStartedAt"),
            dispositions=event.get("screeningDispositions"),
            live_failures=event.get("livenessFailures"),
            current_list_version=current_list)
        # Explicitly supplied fields win: a caller that knows a value (Phase 2 carries the
        # document confidence Phase 1 decided on) is never overridden by a re-derivation.
        for key, value in reading.inputs.items():
            event.setdefault(key, value)
        # A fact that could not be used is an agent that did not deliver. UNAVAILABLE, so the case
        # reaches a human through the ordinary fail-closed path rather than a default.
        for agent in reading.undetermined:
            agent_results[agent] = {"error": "SIGNAL_NOT_DETERMINED"}
        if reading.undetermined:
            # The tier still has to compute, so the missing fields get placeholders. These never
            # decide anything: every undetermined agent is UNAVAILABLE, and the outcome tables route
            # any unavailable agent to MANUAL_REVIEW. `preScreenMatched` is False on purpose:
            # PROHIBITED is evaluated before the unavailable check, and a placeholder of True would
            # reject a real applicant on a missing record. Reviewing on a gap beats rejecting on one.
            event.setdefault("preScreenMatched", False)
            event.setdefault("documentConfidence", 0.0)
            event.setdefault("velocitySignals", {"critical": False})
            event.setdefault("pepStatus", "NONE")
            event.setdefault("adverseMediaSeverity", "NONE")

    explicit_edge = _edge_fields(event.get("edgeFacts") or {})
    derived_edge = _edge_fields(reading.edge_facts) if reading else {}
    # Explicit edge facts win for the same reason explicit tier fields do.
    verdicts = evaluate_edge_cases(EdgeFacts(**{**derived_edge, **explicit_edge}))

    # Declared identity versus what the document proves. Read from the recorded extraction, never
    # from an agent's paraphrase of it. Judged only in Phase 1, which owns identity: a later phase
    # re-raising a mismatch a reviewer already resolved would send the same case round twice.
    extraction = reading.extraction if reading else event.get("documentExtraction")
    identity_comparison = compare_identity(declared or event.get("declaredIdentity"),
                                           extraction)
    if phase == 1:
        verdicts = verdicts + identity_verdicts(identity_comparison)
        if reading:
            verdicts = verdicts + reading.verdicts

    if phase == 3:
        # Lifecycle re-assessment (G-01). Returns early because it shares nothing with the
        # onboarding phases beyond the tier calculation, and — critically — nothing is updated: the
        # result is a new decision version to be APPENDED.
        return _phase3(event, reading=reading)

    if phase == 1:
        tier = preliminary_tier(_preliminary_from(event))
        decision = select_phase1_outcome(
            tier=tier, agent_results=agent_results, edge_verdicts=verdicts)
    else:
        tier = full_tier(_full_from(event))
        decision = select_phase2_outcome(
            tier=tier, agent_results=agent_results, edge_verdicts=verdicts)

    action = tier_action(tier.tier)
    return {
        "phase": phase,
        "outcome": decision.outcome.value,
        "rule": decision.rule,
        "reasonCode": decision.reason_code,
        "explanation": decision.explanation,
        "requiredItem": decision.required_item,
        "notifyCompliance": decision.notify_compliance,
        "tier": tier.tier.value,
        "tierRule": tier.rule,
        "tierInputs": tier.inputs,
        "thresholdsInForce": tier.thresholds,
        "configVersion": tier.config_version,
        "action": action["action"],
        "reKycInterval": action["re_kyc_interval"],
        "edgeCasesMatched": list(decision.edge_cases),
        "recordedSignals": decision.recorded_signals,
        # Returned whether or not it changed the outcome. A reviewer needs to see that the
        # comparison ran and matched, not only when it failed — "no finding" and "not checked" are
        # different facts, and only the audit record can tell them apart later (R18.2).
        "identityMatch": identity_comparison.to_dict(),
        # Which recorded facts the decision rested on, their status and when they were recorded.
        "factsRead": reading.summary if reading else None,
    }


def _phase3(event: dict[str, Any], *, reading) -> dict[str, Any]:
    """Re-assess an approved customer against a risk event, and return a row to APPEND.

    The response carries `historySortKey` and `idempotencyName` rather than leaving the caller to
    compose them. Both are correctness-critical — the sort key is what makes a replayed event land
    on the same row (and be refused), and the execution name is what collapses a replay onto the
    original run — so they are derived here, in tested code, from the event itself rather than from
    anything that differs between two deliveries of it.
    """
    raw_event = event.get("riskEvent") or {}
    if not isinstance(raw_event, dict):
        raise InputError("riskEvent must be an object")
    for required in ("eventId", "eventType", "severity", "customerId", "occurredAt"):
        if not raw_event.get(required):
            raise InputError(f"riskEvent.{required} is required")
    try:
        severity = RiskEventSeverity(str(raw_event["severity"]).upper())
    except ValueError as exc:
        raise InputError(f"unknown riskEvent.severity {raw_event['severity']!r}") from exc

    risk_event = RiskEvent(
        event_id=str(raw_event["eventId"]),
        event_type=str(raw_event["eventType"]),
        severity=severity,
        customer_id=str(raw_event["customerId"]),
        occurred_at=str(raw_event["occurredAt"]),
        detail=dict(raw_event.get("detail") or {}),
    )

    raw_day0 = event.get("day0") or {}
    if not isinstance(raw_day0, dict) or not raw_day0.get("caseId"):
        raise InputError("day0.caseId is required to re-assess an existing decision")
    try:
        day0_tier = RiskTier(str(raw_day0.get("tier", "")).upper())
    except ValueError as exc:
        raise InputError(f"unknown day0.tier {raw_day0.get('tier')!r}") from exc

    day0 = Day0Snapshot(
        case_id=str(raw_day0["caseId"]),
        tier=day0_tier,
        outcome=str(raw_day0.get("outcome", "")),
        decided_at=str(raw_day0.get("decidedAt", "")),
        version=int(raw_day0.get("version", 1)),
    )

    # No fresh evidence means the re-screen did not complete. Passing None is the honest signal;
    # `reassess` turns it into SYSTEM_ERROR and a human, never "risk unchanged".
    if reading is not None:
        complete = not reading.undetermined
    else:
        complete = bool(event.get("evidenceComplete"))
    new_evidence = _full_from(event) if complete else None

    decided_at = str(event.get("decidedAt") or event.get("asOf") or "")
    decision: LifecycleDecision = reassess(
        day0=day0, event=risk_event, new_evidence=new_evidence, decided_at=decided_at)

    return {
        "phase": 3,
        "outcome": decision.outcome.value,
        "day0Tier": decision.day0_tier.value,
        "newTier": decision.new_tier.value,
        "riskChanged": decision.day0_tier is not decision.new_tier,
        "version": decision.version,
        "decidedAt": decision.decided_at,
        "eventId": decision.event_id,
        "nextReviewDays": decision.next_review_days,
        "explanation": decision.rationale,
        "reKycInterval": _interval_for(decision.new_tier),
        # Composed here so the state machine cannot get them subtly wrong.
        "historySortKey": history_sort_key(risk_event.occurred_at, risk_event.event_id),
        "idempotencyName": idempotency_name(risk_event.customer_id, risk_event.event_id),
        # The day-zero decision is echoed unchanged as a standing assertion that this flow read it
        # and did not edit it.
        "day0Preserved": {"caseId": day0.case_id, "outcome": day0.outcome,
                          "decidedAt": day0.decided_at, "tier": day0.tier.value},
        "factsRead": reading.summary if reading else None,
    }


def _interval_for(tier: RiskTier) -> str | None:
    return tier_action(tier)["re_kyc_interval"]


# --- parsing ------------------------------------------------------------------------------
#
# Every field is required. There are no defaults, on purpose: a missing `preScreenMatched` that
# quietly became `False` would turn "we never checked" into "we checked and found nothing", which
# is the same class of error as reading an empty agent result as a clean pass.


def _plain_mapping(raw: Any) -> dict[str, Any] | None:
    """Accept a plain object or the DynamoDB-typed `{"M": {"k": {"S": ...}}}` shape."""
    if isinstance(raw, dict) and set(raw) == {"M"} and isinstance(raw["M"], dict):
        return {k: (v.get("S") if isinstance(v, dict) and "S" in v else v)
                for k, v in raw["M"].items()}
    return raw if isinstance(raw, dict) else None


def _preliminary_from(event: dict[str, Any]) -> PreliminaryInputs:
    return PreliminaryInputs(
        jurisdiction_risk=_enum(RiskTier, event, "jurisdictionRisk"),
        pre_screen_matched=_bool(event, "preScreenMatched"),
        document_confidence=_float(event, "documentConfidence"),
        velocity_signals=_velocity(event),
        product_applied=_str(event, "productApplied"),
        screening_hits=_hits(event),
    )


def _full_from(event: dict[str, Any]) -> FullInputs:
    return FullInputs(
        jurisdiction_risk=_enum(RiskTier, event, "jurisdictionRisk"),
        pep_status=_enum(PepStatus, event, "pepStatus"),
        adverse_media_severity=_enum(Severity, event, "adverseMediaSeverity"),
        document_confidence=_float(event, "documentConfidence"),
        velocity_signals=_velocity(event),
        product_applied=_str(event, "productApplied"),
        screening_hits=_hits(event),
    )


#: camelCase event key -> EdgeFacts field. Listed explicitly rather than derived by string
#: munging, so an unknown key is rejected instead of silently ignored — a typo'd
#: "deceasedIndicater" that quietly did nothing would disable a control while every test passed.
_EDGE_FIELDS: dict[str, str] = {
    "documentExpired": "document_expired",
    "issuingCountrySupported": "issuing_country_supported",
    "isScreenPhotograph": "is_screen_photograph",
    "documentQuality": "document_quality",
    "qualityDefects": "quality_defects",
    "age": "age",
    "nationalities": "nationalities",
    "jurisdictionRiskByCode": "jurisdiction_risk_by_code",
    "passportCountry": "passport_country",
    "addressCountry": "address_country",
    "ipCountry": "ip_country",
    "isThinFile": "is_thin_file",
    "deceasedIndicator": "deceased_indicator",
    "sharedAttributeCount": "shared_attribute_count",
    "sharedWithCaseId": "shared_with_case_id",
    "abandonedMidFlow": "abandoned_mid_flow",
    "withinResumeWindow": "within_resume_window",
    "pendingStepUpItem": "pending_step_up_item",
    "livenessFailureCount": "liveness_failure_count",
    "isDuplicateSubmission": "is_duplicate_submission",
    "canonicalCaseId": "canonical_case_id",
    "caseAlreadyDecided": "case_already_decided",
    "existingOutcome": "existing_outcome",
    "resubmittedAfterFinalDecision": "resubmitted_after_final_decision",
    "priorOutcome": "prior_outcome",
    "priorCaseId": "prior_case_id",
}

_EDGE_TUPLE_FIELDS = {"quality_defects", "nationalities"}


def _edge_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse edge-case facts to EdgeFacts kwargs. Absent means "not established", never False."""
    if not isinstance(raw, dict):
        raise InputError("edgeFacts must be an object")
    unknown = set(raw) - set(_EDGE_FIELDS)
    if unknown:
        raise InputError(f"unknown edgeFacts keys: {sorted(unknown)}")

    kwargs: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        field_name = _EDGE_FIELDS[key]
        if field_name in _EDGE_TUPLE_FIELDS:
            if not isinstance(value, list):
                raise InputError(f"edgeFacts.{key} must be an array")
            kwargs[field_name] = tuple(str(v) for v in value)
        elif field_name == "jurisdiction_risk_by_code":
            if not isinstance(value, dict):
                raise InputError("edgeFacts.jurisdictionRiskByCode must be an object")
            kwargs[field_name] = {
                str(k).lower(): _risk_tier(str(k), v) for k, v in value.items()}
        else:
            kwargs[field_name] = value
    return kwargs


def _edge_facts_from(raw: dict[str, Any]) -> EdgeFacts:
    """Kept for callers that parse edge facts on their own."""
    return EdgeFacts(**_edge_fields(raw))


def _risk_tier(code: str, value: Any) -> RiskTier:
    try:
        return RiskTier(str(value).upper())
    except ValueError as exc:
        valid = ", ".join(m.value for m in RiskTier)
        raise InputError(
            f"jurisdictionRiskByCode[{code}]={value!r} is not one of: {valid}") from exc


def _require(event: dict[str, Any], key: str) -> Any:
    if key not in event or event[key] is None:
        raise InputError(f"missing required field {key!r}")
    return event[key]


def _enum(cls, event: dict[str, Any], key: str):
    raw = _require(event, key)
    try:
        return cls(str(raw).upper())
    except ValueError as exc:
        valid = ", ".join(m.value for m in cls)
        raise InputError(f"{key}={raw!r} is not one of: {valid}") from exc


def _bool(event: dict[str, Any], key: str) -> bool:
    raw = _require(event, key)
    if isinstance(raw, bool):
        return raw
    raise InputError(f"{key} must be a boolean, got {type(raw).__name__}")


def _float(event: dict[str, Any], key: str) -> float:
    raw = _require(event, key)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise InputError(f"{key} must be a number, got {raw!r}") from exc
    if not 0.0 <= value <= 1.0:
        raise InputError(f"{key} must be within 0.0..1.0, got {value}")
    return value


def _str(event: dict[str, Any], key: str) -> str:
    raw = _require(event, key)
    if not isinstance(raw, str) or not raw.strip():
        raise InputError(f"{key} must be a non-empty string")
    return raw


def _velocity(event: dict[str, Any]) -> VelocitySignals:
    raw = _require(event, "velocitySignals")
    if not isinstance(raw, dict):
        raise InputError("velocitySignals must be an object")
    if "critical" not in raw:
        raise InputError("velocitySignals.critical is required")
    detail = raw.get("detail") or []
    if not isinstance(detail, list):
        raise InputError("velocitySignals.detail must be an array")
    return VelocitySignals(critical=bool(raw["critical"]), detail=tuple(str(d) for d in detail))


def _hits(event: dict[str, Any]) -> tuple[ScreeningHit, ...]:
    raw = event.get("screeningHits") or []
    if not isinstance(raw, list):
        raise InputError("screeningHits must be an array")
    out = []
    for i, h in enumerate(raw):
        if not isinstance(h, dict):
            raise InputError(f"screeningHits[{i}] must be an object")
        if "listId" not in h or "strength" not in h:
            raise InputError(f"screeningHits[{i}] needs listId and strength")
        try:
            strength = MatchStrength(str(h["strength"]).upper())
        except ValueError as exc:
            valid = ", ".join(m.value for m in MatchStrength)
            raise InputError(
                f"screeningHits[{i}].strength={h['strength']!r} is not one of: {valid}") from exc
        out.append(ScreeningHit(
            list_id=str(h["listId"]),
            strength=strength,
            matched_on=tuple(str(m) for m in (h.get("matchedOn") or [])),
            score=float(h["score"]) if h.get("score") is not None else None,
        ))
    return tuple(out)


if __name__ == "__main__":  # local smoke check
    import sys
    print(json.dumps(lambda_handler(json.loads(sys.argv[1])), indent=2, default=str))
