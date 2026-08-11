"""Lambda entry point for the decision engine — task 6.1.

This is the **only** impure module in the package, and it is deliberately thin: parse, delegate,
serialise. All judgement lives in `tiering.py`, `outcomes.py` and `edge_cases.py`, which import
nothing that can touch a clock, a network or a disk. `test_tiering.TestPurity` enforces that by
parsing the source, and it excludes this file by name.

Named `decision_handler.py` rather than `handler.py` because `tools/handler.py` already exists and
both directories sit on `sys.path`. Two modules called `handler` would resolve by path order,
which is exactly the kind of dependency that works until someone reorders a config file.

Keeping the boundary this sharp is what makes R4.10's "1,000 identical inputs, 1,000 identical
outputs" a property of the code rather than an observation about one run.
"""

from __future__ import annotations

import json
from typing import Any

from edge_cases import EdgeFacts, evaluate_edge_cases
from models import (
    FullInputs,
    MatchStrength,
    PepStatus,
    PreliminaryInputs,
    RiskTier,
    ScreeningHit,
    Severity,
    VelocitySignals,
)
from outcomes import select_phase1_outcome, select_phase2_outcome
from signals import derive as derive_signals
from signals import from_findings as signals_from_findings
from identity_match import evaluate as evaluate_identity
from tiering import full_tier, preliminary_tier, tier_action


class InputError(ValueError):
    """The event could not be read. Distinct from a decision, so the caller cannot confuse a
    malformed request with a risk outcome — silently defaulting a missing field would produce a
    confident decision from evidence that was never supplied."""


def lambda_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Route on `phase`. Returns the decision plus everything R4.9 / R18.6 need for the audit."""
    phase = event.get("phase")
    if phase not in (1, 2, "1", "2"):
        raise InputError(f"phase must be 1 or 2, got {phase!r}")

    agent_results = event.get("agentResults") or {}
    if not isinstance(agent_results, dict):
        raise InputError("agentResults must be an object keyed by agent name")

    verdicts = evaluate_edge_cases(_edge_facts_from(event.get("edgeFacts") or {}))

    # Declared identity versus what the document proves. Appended to the mandated edge-case
    # verdicts rather than folded into the fifteen-rule table, because the requirements document
    # has no clause for this comparison — see `identity_match`'s docstring, which records it as a
    # spec gap rather than presenting it as an implemented requirement.
    #
    # Read from the case record, not from an agent's finding, and that is the point: the MRZ check
    # digits make this the one part of document extraction that is *verified*, and routing it
    # through a model's prose would turn a checkable fact into a paraphrase.
    identity_comparison, identity_verdicts = evaluate_identity(
        event.get("declaredIdentity"), event.get("documentExtraction"))
    verdicts = verdicts + identity_verdicts

    # Fill only genuine gaps. An explicitly supplied field always wins, so a caller that knows a
    # signal is never overridden by prose. Anything that cannot be established is added to
    # `agentResults` as an explicit UNAVAILABLE marker rather than defaulted — which routes the
    # case to a human through the normal fail-closed path instead of inventing a safe-looking value.
    event = dict(event)
    agent_results = dict(agent_results)

    # Derivation is a fallback for agents that replied unreadably — NOT a licence to omit inputs.
    # With no `agentResults` at all there is nothing to derive from, so the strict
    # every-field-required rule stands and a missing field is still an InputError. Relaxing that
    # would let a caller send almost nothing and receive a confident decision.
    # Structured findings first (R11.13). Each was schema-validated by the service before it was
    # persisted, so its shape is not the model's choice — which is what makes the outcome
    # reproducible. Prose derivation is only a fallback for agents that recorded nothing, and
    # F-89 is the measurement of why it cannot be the primary path.
    findings = event.get("findings") or {}
    if not isinstance(findings, dict):
        raise InputError("findings must be an object keyed by agent name")

    derived, undetermined = signals_from_findings(findings) if findings else ({}, ())
    if undetermined and agent_results:
        # Only the agents the contract did not cover fall back to prose.
        fallback, still_undetermined = derive_signals(
            {a: agent_results[a] for a in undetermined if a in agent_results})
        derived = {**fallback, **derived}
        undetermined = tuple(a for a in undetermined
                             if a in still_undetermined or a not in agent_results)
    elif not findings:
        derived, undetermined = (derive_signals(agent_results) if agent_results else ({}, ()))

    for key, value in derived.items():
        event.setdefault(key, value)
    # Marked unconditionally, not only when the agent already appears in `agentResults`. An earlier
    # version guarded on membership, which meant an agent that recorded no finding *and* returned
    # no text was silently absent from the unavailable list — so the case tiered on placeholders
    # and could approve. An agent that did not report is exactly the one that must reach a human.
    for agent in undetermined:
        agent_results[agent] = {"error": "SIGNAL_NOT_DETERMINED"}
    if undetermined:
        # The tier still has to compute, so the remaining fields get placeholders. These never
        # decide anything: each undetermined agent is now UNAVAILABLE, and `select_phase1_outcome`
        # routes any unavailable agent to MANUAL_REVIEW.
        #
        # The values are chosen so that if that ordering were ever changed, the failure would be
        # conservative rather than dangerous. `preScreenMatched` is **False** on purpose even
        # though False is the permissive reading: PROHIBITED is evaluated *before* the unavailable
        # check, so a placeholder of True would reject a real applicant on a parsing failure.
        # Rejecting on a guess is worse than reviewing on one.
        event.setdefault("preScreenMatched", False)
        event.setdefault("documentConfidence", 0.0)
        event.setdefault("velocitySignals", {"critical": False})
        event.setdefault("pepStatus", "NONE")
        event.setdefault("adverseMediaSeverity", "NONE")

    if str(phase) == "1":
        tier = preliminary_tier(_preliminary_from(event))
        decision = select_phase1_outcome(
            tier=tier, agent_results=agent_results, edge_verdicts=verdicts)
    else:
        tier = full_tier(_full_from(event))
        decision = select_phase2_outcome(
            tier=tier, agent_results=agent_results, edge_verdicts=verdicts)

    action = tier_action(tier.tier)
    return {
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
    }


# --- parsing ------------------------------------------------------------------------------
#
# Every field is required. There are no defaults, on purpose: a missing `preScreenMatched` that
# quietly became `False` would turn "we never checked" into "we checked and found nothing", which
# is the same class of error as reading an empty agent result as a clean pass.


def _preliminary_from(event: dict[str, Any]) -> PreliminaryInputs:
    return PreliminaryInputs(
        jurisdiction_risk=_enum(RiskTier, event, "jurisdictionRisk"),
        pre_screen_matched=_bool(event, "preScreenMatched"),
        document_confidence=_float(event, "documentConfidence"),
        velocity_signals=_velocity(event),
        product_applied=_str(event, "productApplied"),
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


def _edge_facts_from(raw: dict[str, Any]) -> EdgeFacts:
    """Parse the edge-case facts. Absent means "not established", never False (see `EdgeFacts`)."""
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
    return EdgeFacts(**kwargs)


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
