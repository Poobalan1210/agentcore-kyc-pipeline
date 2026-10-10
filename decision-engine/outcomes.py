"""Outcome selection — design §8.3, task 6.1.

A decision table evaluated in fixed order, first match wins. Same evidence always yields the same
outcome (R3.5). Determinism comes from the rules, never from constraining the model (R3.4).

## Precedence, and why it is severity-ordered

The table is ordered `REJECT > MANUAL_REVIEW > STEP_UP > APPROVE`, so when several conditions
hold at once the most conservative wins. This matters in real combinations. An applicant who is
under 18 (R29.1 → `MANUAL_REVIEW`) *and* has an expired document (R28.1 → `STEP_UP`) must not be
sent away to fetch a newer passport, because no document resolves being 16 — the case needs a
human. Ordering by severity gets that right without enumerating pairs.

## The one place the spec pulls in two directions

R4.3 says a `PROHIBITED` preliminary tier "SHALL reject at Phase 1", and R4.7 maps `PROHIBITED`
to "Freeze and reject". Design §8.4 says the opposite in tone: a confirmed screening hit "blocks
the provisional account and routes for compliance handling; it is not an automated rejection of
the person."

Resolved as `REJECT` with reason code `SCREENING_CONFIRMED_MATCH` and `notify_compliance=True`,
because the requirements are normative and the design note is about framing. Both are honoured:
the outcome satisfies R4.3, and the reason code plus the compliance flag carry the design's point
that this is a legal prohibition on onboarding rather than a judgement about the person.

The flag is a flag. It is recorded on the decision and in the sealed record, and nothing in this
build sends a notification or opens a referral on it, so the explanation says the decision is
flagged for compliance, never that someone has been notified (D-42).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from models import (
    EdgeVerdict,
    OutcomeDecision,
    Phase1Outcome,
    Phase2Outcome,
    RiskTier,
    TierDecision,
)
from normalise import all_results_usable
from tiering import tier_action

#: Severity order for resolving simultaneous matches. Higher wins.
_SEVERITY: dict[str, int] = {
    "REJECT": 4,
    "MANUAL_REVIEW": 3,
    "RESTRICT_FURTHER": 2,
    "STEP_UP": 2,
    "FULL_APPROVE_ENHANCED_MONITORING": 1,
    "PROVISIONAL_APPROVE_RESTRICTED": 0,
    "FULL_APPROVE_STANDARD": 0,
}


def select_phase1_outcome(
    *,
    tier: TierDecision,
    agent_results: Mapping[str, Any] | None = None,
    edge_verdicts: tuple[EdgeVerdict, ...] = (),
) -> OutcomeDecision:
    """R3.1 — one of four. Never a `FULL_APPROVE_*` (R3.3), which this function cannot express."""
    agent_results = agent_results or {}
    _, unavailable, invalid = all_results_usable(agent_results)

    worst_edge = _worst(edge_verdicts)
    matched = tuple(f"{v.case_id}({v.requirement})" for v in edge_verdicts)
    signals = _signals(tier, unavailable, invalid, edge_verdicts)

    # 1. REJECT — mandated edge case (only R29.5 deceased reaches here), or a confirmed hit.
    if worst_edge is not None and worst_edge.outcome.value == "REJECT":
        return _from_edge(worst_edge, tier, matched, signals)
    if tier.tier is RiskTier.PROHIBITED:
        return OutcomeDecision(
            outcome=Phase1Outcome.REJECT,
            rule="O1.preliminary_tier_prohibited",
            reason_code="SCREENING_CONFIRMED_MATCH",
            explanation=(
                "A confirmed screening match prohibits onboarding. This is a legal restriction, "
                "not an assessment of the applicant. The decision is flagged for compliance "
                "(notifyCompliance); this build sends no notification or referral itself."
            ),
            notify_compliance=True,
            tier=tier,
            edge_cases=matched,
            recorded_signals=signals,
        )

    # 2. MANUAL_REVIEW — mandated edge case, an unavailable dependency, or a HIGH tier.
    if worst_edge is not None and worst_edge.outcome.value == "MANUAL_REVIEW":
        return _from_edge(worst_edge, tier, matched, signals)
    if unavailable:
        # Ours to fix, not the applicant's. Never a STEP_UP: demanding something the applicant
        # cannot supply reads to them as an unexplained loop.
        return OutcomeDecision(
            outcome=Phase1Outcome.MANUAL_REVIEW,
            rule="O1.agent_result_unavailable",
            reason_code="EVIDENCE_UNAVAILABLE",
            explanation=(
                "One or more checks did not return a usable result "
                f"({', '.join(unavailable)}). An empty or failed result is never read as a clean "
                "pass, so the case is routed to a reviewer."
            ),
            tier=tier,
            edge_cases=matched,
            recorded_signals=signals,
        )
    if tier.tier is RiskTier.HIGH:
        return OutcomeDecision(
            outcome=Phase1Outcome.MANUAL_REVIEW,
            rule="O1.preliminary_tier_high",
            reason_code="PRELIMINARY_RISK_HIGH",
            explanation=f"Preliminary tier HIGH via {tier.rule}. {tier_action(tier.tier)['action']}.",
            tier=tier,
            edge_cases=matched,
            recorded_signals=signals,
        )

    # 3. STEP_UP — a mandated edge case, or a permanently unusable applicant input.
    if worst_edge is not None and worst_edge.outcome.value == "STEP_UP":
        return _from_edge(worst_edge, tier, matched, signals)
    if invalid:
        return OutcomeDecision(
            outcome=Phase1Outcome.STEP_UP,
            rule="O1.agent_result_invalid",
            required_item=f"A replacement for: {', '.join(invalid)}",
            explanation=(
                "A submitted item could not be used and will not become usable on retry. "
                "This is a continuation, not a rejection."
            ),
            tier=tier,
            edge_cases=matched,
            recorded_signals=signals,
        )

    # 4. Approve, restricted. LOW and MEDIUM both land here; the restriction set differs by tier.
    return OutcomeDecision(
        outcome=Phase1Outcome.PROVISIONAL_APPROVE_RESTRICTED,
        rule="O1.provisional_approve",
        explanation=(
            f"Preliminary tier {tier.tier.value} via {tier.rule}. "
            f"{tier_action(tier.tier)['action']}."
        ),
        tier=tier,
        edge_cases=matched,
        recorded_signals=signals,
    )


def select_phase2_outcome(
    *,
    tier: TierDecision,
    agent_results: Mapping[str, Any] | None = None,
    edge_verdicts: tuple[EdgeVerdict, ...] = (),
) -> OutcomeDecision:
    """R3.2 — one of five."""
    agent_results = agent_results or {}
    _, unavailable, invalid = all_results_usable(agent_results)
    worst_edge = _worst(edge_verdicts)
    matched = tuple(f"{v.case_id}({v.requirement})" for v in edge_verdicts)
    signals = _signals(tier, unavailable, invalid, edge_verdicts)

    if worst_edge is not None and worst_edge.outcome.value == "REJECT":
        return _from_edge(worst_edge, tier, matched, signals)
    if tier.tier is RiskTier.PROHIBITED:
        return OutcomeDecision(
            outcome=Phase2Outcome.REJECT,
            rule="O2.full_tier_prohibited",
            reason_code="SCREENING_CONFIRMED_MATCH",
            explanation=(
                "A confirmed screening match prohibits onboarding, so the account is frozen. The "
                "decision is flagged for compliance (notifyCompliance); this build sends no "
                "notification or referral itself."
            ),
            notify_compliance=True,
            tier=tier, edge_cases=matched, recorded_signals=signals,
        )

    if worst_edge is not None and worst_edge.outcome.value == "MANUAL_REVIEW":
        return _from_edge(worst_edge, tier, matched, signals)
    if unavailable:
        return OutcomeDecision(
            outcome=Phase2Outcome.MANUAL_REVIEW,
            rule="O2.agent_result_unavailable",
            reason_code="EVIDENCE_UNAVAILABLE",
            explanation=f"Checks without a usable result: {', '.join(unavailable)}.",
            tier=tier, edge_cases=matched, recorded_signals=signals,
        )
    # R4.8 is unconditional: a HIGH full tier goes to a human "regardless of other results".
    if tier.tier is RiskTier.HIGH:
        return OutcomeDecision(
            outcome=Phase2Outcome.MANUAL_REVIEW,
            rule="O2.full_tier_high",
            reason_code="FULL_RISK_HIGH",
            explanation=(
                f"Full tier HIGH via {tier.rule}. {tier_action(tier.tier)['action']}. "
                "R4.8 routes this to review regardless of other results."
            ),
            tier=tier, edge_cases=matched, recorded_signals=signals,
        )

    if invalid:
        return OutcomeDecision(
            outcome=Phase2Outcome.RESTRICT_FURTHER,
            rule="O2.agent_result_invalid",
            explanation=(
                "A submitted item is permanently unusable, so restrictions tighten rather than "
                f"lift: {', '.join(invalid)}."
            ),
            tier=tier, edge_cases=matched, recorded_signals=signals,
        )

    if tier.tier is RiskTier.MEDIUM:
        return OutcomeDecision(
            outcome=Phase2Outcome.FULL_APPROVE_ENHANCED_MONITORING,
            rule="O2.full_tier_medium",
            explanation=(
                f"Full tier MEDIUM via {tier.rule}. {tier_action(tier.tier)['action']}. "
                f"Re-KYC {tier_action(tier.tier)['re_kyc_interval']}."
            ),
            tier=tier, edge_cases=matched, recorded_signals=signals,
        )
    return OutcomeDecision(
        outcome=Phase2Outcome.FULL_APPROVE_STANDARD,
        rule="O2.full_tier_low",
        explanation=(
            f"Full tier LOW via {tier.rule}. {tier_action(tier.tier)['action']}. "
            f"Re-KYC {tier_action(tier.tier)['re_kyc_interval']}."
        ),
        tier=tier, edge_cases=matched, recorded_signals=signals,
    )


# --- internals ---------------------------------------------------------------------------


def _worst(verdicts: tuple[EdgeVerdict, ...]) -> EdgeVerdict | None:
    """The most severe matched edge case that actually decides an application, or None.

    Verdicts carrying only a `directive` are skipped: six of the fifteen rows instruct the
    surrounding machinery rather than deciding the case, and treating them as outcomes would let
    "record all three countries" outrank a real risk finding. Their signals still reach the audit
    record through `_signals`, which reads every verdict.
    """
    deciding = [v for v in verdicts if v.outcome is not None]
    if not deciding:
        return None
    return max(deciding, key=lambda v: _SEVERITY.get(v.outcome.value, 0))


def _from_edge(
    v: EdgeVerdict,
    tier: TierDecision,
    matched: tuple[str, ...],
    signals: dict[str, object],
) -> OutcomeDecision:
    """Build the decision for a governing edge case.

    `signals` is the full merged record, not just this verdict's own. An earlier version passed
    only `v.signals`, which meant an edge-case decision lost the tier, the thresholds in force and
    every directive from the other matched rows — so the one outcome most likely to be challenged,
    a rejection, carried the thinnest audit record. Caught by
    `test_directives_survive_into_the_record_even_when_outranked`.
    """
    return OutcomeDecision(
        outcome=v.outcome,
        rule=f"edge:{v.case_id}",
        reason_code=v.reason_code,
        explanation=v.explanation,
        required_item=v.required_item,
        notify_compliance=v.notify_compliance,
        tier=tier,
        edge_cases=matched,
        recorded_signals=signals,
    )


def _signals(
    tier: TierDecision,
    unavailable: tuple[str, ...],
    invalid: tuple[str, ...],
    verdicts: tuple[EdgeVerdict, ...],
) -> dict[str, object]:
    """Everything R4.9 and R18.6 need to reconstruct the decision later."""
    merged: dict[str, object] = {
        "tier": tier.tier.value,
        "tier_rule": tier.rule,
        "tier_inputs": tier.inputs,
        "thresholds_in_force": tier.thresholds,
        "config_version": tier.config_version,
        "unavailable_agents": list(unavailable),
        "invalid_agents": list(invalid),
        "edge_cases_matched": [
            {"case": v.case_id, "requirement": v.requirement,
             "outcome": v.outcome.value if v.outcome else None,
             "directive": v.directive}
            for v in verdicts
        ],
        # Directives are surfaced separately because the caller must act on them even when a more
        # severe outcome governs the decision. A jurisdiction selection or a prior-case link is
        # not cancelled by the case going to manual review.
        "directives": [
            {"case": v.case_id, "requirement": v.requirement, "directive": v.directive}
            for v in verdicts if v.directive is not None
        ],
    }
    for v in verdicts:
        merged.update(v.signals)
    return merged
