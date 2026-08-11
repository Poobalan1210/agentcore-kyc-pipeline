"""Risk tiering — design §8.1 and §8.2, task 6.1.

Pure and total. For any inhabited input the function returns exactly one tier, and it returns
the same tier every time (R4.6, R4.10).

**[PORT] from P-10 `src/agent/remediation/risk.py`** — the discipline carries over, the
arithmetic does not. P-10 classifies with an additive score (`scope × reversibility × data-loss`)
mapped to thresholds. That works when inputs are commensurable magnitudes. Here they are not: a
confirmed sanctions match is not "more points" than weak document confidence, it is a different
kind of fact that must dominate regardless of what else is true. So the ported structure is
first-match-wins over an ordered, named rule list, keeping P-10's three properties that actually
matter — totality, recorded inputs, and a named rule for the audit record (R4.9).
"""

from __future__ import annotations

import config
from models import (
    FullInputs,
    MatchStrength,
    PepStatus,
    PreliminaryInputs,
    RiskTier,
    Severity,
    TierDecision,
)


def preliminary_tier(inputs: PreliminaryInputs) -> TierDecision:
    """design §8.1 — Phase 1, exact-match screening only (R4.1).

    Rule order is the specification's order and it is load-bearing, not incidental::

        pre_screen_matched          -> PROHIBITED   (R4.3, blocks the provisional account)
        document_confidence < 0.8   -> HIGH
        velocity_signals.critical   -> HIGH
        jurisdiction_risk == HIGH   -> MEDIUM
        otherwise                   -> LOW
    """
    threshold = config.DOCUMENT_CONFIDENCE_THRESHOLD
    snapshot = _preliminary_snapshot(inputs)

    if inputs.pre_screen_matched:
        return _decide(RiskTier.PROHIBITED, "P1.pre_screen_matched", snapshot)
    if inputs.document_confidence < threshold:
        return _decide(RiskTier.HIGH, "P1.document_confidence_below_threshold", snapshot)
    if inputs.velocity_signals.critical:
        return _decide(RiskTier.HIGH, "P1.velocity_critical", snapshot)
    if inputs.jurisdiction_risk is RiskTier.HIGH:
        return _decide(RiskTier.MEDIUM, "P1.jurisdiction_high", snapshot)
    return _decide(RiskTier.LOW, "P1.default", snapshot)


def full_tier(inputs: FullInputs) -> TierDecision:
    """design §8.2 — Phase 2, all screening results (R4.2).

    The same conditions score differently here than in `preliminary_tier`, deliberately. Weak
    document confidence and critical velocity are `HIGH` at Phase 1 and `MEDIUM` at Phase 2: in
    Phase 1 they are the only fraud evidence available so they dominate, while by Phase 2 they
    sit alongside confirmed screening results, which are stronger and outrank them. Scoring them
    identically would either over-restrict good applicants at Phase 1 or under-weight real
    screening hits at Phase 2.
    """
    threshold = config.DOCUMENT_CONFIDENCE_THRESHOLD
    snapshot = _full_snapshot(inputs)

    if any(h.strength is MatchStrength.CONFIRMED for h in inputs.screening_hits):
        return _decide(RiskTier.PROHIBITED, "P2.screening_hit_confirmed", snapshot)
    if inputs.pep_status is PepStatus.SELF:
        return _decide(RiskTier.HIGH, "P2.pep_self", snapshot)
    if inputs.pep_status is PepStatus.ASSOCIATE:
        return _decide(RiskTier.HIGH, "P2.pep_associate", snapshot)
    if inputs.adverse_media_severity >= Severity.HIGH:
        return _decide(RiskTier.HIGH, "P2.adverse_media_high", snapshot)
    if any(h.strength in (MatchStrength.PROBABLE, MatchStrength.POSSIBLE)
           for h in inputs.screening_hits):
        # R27.5 — an unresolved hit is a human's call, never an automated pass.
        return _decide(RiskTier.HIGH, "P2.screening_hit_unresolved", snapshot)
    if inputs.document_confidence < threshold:
        return _decide(RiskTier.MEDIUM, "P2.document_confidence_below_threshold", snapshot)
    if inputs.velocity_signals.critical:
        return _decide(RiskTier.MEDIUM, "P2.velocity_critical", snapshot)
    if inputs.jurisdiction_risk is RiskTier.HIGH:
        return _decide(RiskTier.MEDIUM, "P2.jurisdiction_high", snapshot)
    return _decide(RiskTier.LOW, "P2.default", snapshot)


def tier_action(tier: RiskTier) -> dict[str, str | None]:
    """R4.7 — the fixed action and re-KYC interval for a tier."""
    return dict(config.TIER_ACTIONS[tier])


# --- internals ---------------------------------------------------------------------------


def _decide(tier: RiskTier, rule: str, snapshot: dict[str, object]) -> TierDecision:
    return TierDecision(
        tier=tier,
        rule=rule,
        inputs=snapshot,
        thresholds=config.thresholds_in_force(),
        config_version=config.CONFIG_VERSION,
    )


def _preliminary_snapshot(i: PreliminaryInputs) -> dict[str, object]:
    return {
        "jurisdiction_risk": i.jurisdiction_risk.value,
        "pre_screen_matched": i.pre_screen_matched,
        "document_confidence": i.document_confidence,
        "velocity_critical": i.velocity_signals.critical,
        "velocity_detail": list(i.velocity_signals.detail),
        "product_applied": i.product_applied,
    }


def _full_snapshot(i: FullInputs) -> dict[str, object]:
    return {
        "jurisdiction_risk": i.jurisdiction_risk.value,
        "pep_status": i.pep_status.value,
        "adverse_media_severity": i.adverse_media_severity.value,
        "document_confidence": i.document_confidence,
        "velocity_critical": i.velocity_signals.critical,
        "velocity_detail": list(i.velocity_signals.detail),
        "product_applied": i.product_applied,
        "screening_hits": [
            {"list_id": h.list_id, "strength": h.strength.value,
             "matched_on": list(h.matched_on), "score": h.score}
            for h in i.screening_hits
        ],
    }
