"""Phase 3 — lifecycle re-assessment (the "day-12" flow). G-01, R21, R31.

Why this module exists
----------------------
Onboarding answers "may this customer open an account". It does not answer "is this customer
still who we approved twelve days ago". Ongoing monitoring is not an enhancement to KYC, it is
part of it: FATF R.10(d) requires "ongoing due diligence on the business relationship and
scrutiny of transactions", and in the US it is the fifth element of the AML program added by the
FinCEN CDD Rule (31 CFR 1020.210(b)(5)). The FFIEC manual is explicit that the trigger is
**event-driven** — "the requirement to update customer information is event-driven and occurs as
a result of normal monitoring" — and that there is no categorical periodic obligation. So an
event-driven re-assessment is not a clever extra; it is what the rule actually describes, and a
fixed calendar cycle is the industry's chosen implementation on top of it.

Before this module the build enrolled a customer into monitoring (`EnrolMonitoring` wrote
`state=MONITORING` and a `reKycInterval`) and then did nothing with it. Enrolment without
re-assessment is a record that a control exists, not the control.

The one property that matters
-----------------------------
**The day-zero decision is never rewritten.** A re-assessment produces a NEW decision version
appended to the customer's history; the original row is immutable. That is what lets the talk
answer "was the original KYC decision wrong?" with "not necessarily — the evidence changed",
and it is what an examiner needs in order to reconstruct what was known when.

Purity
------
Same contract as the rest of the rule layer (design §8, enforced by `test_tiering.TestPurity`):
no clock, no network, no I/O. Every timestamp and identifier is supplied by the caller, which is
also what makes a replayed event produce a byte-identical decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from config import TIER_ACTIONS
from models import (
    FullInputs,
    LifecycleDecision,
    MatchStrength,
    Phase3Outcome,
    RiskEvent,
    RiskEventSeverity,
    RiskTier,
)
from tiering import full_tier

#: Ordering used to decide whether risk went UP. `PROHIBITED` is the ceiling.
_TIER_RANK: dict[RiskTier, int] = {
    RiskTier.LOW: 0,
    RiskTier.MEDIUM: 1,
    RiskTier.HIGH: 2,
    RiskTier.PROHIBITED: 3,
}

#: Re-KYC cadence expressed in days, so a due date can be computed by the caller (which owns the
#: clock). Kept beside the interval names in `config.TIER_ACTIONS` rather than duplicating them.
RE_KYC_DAYS: dict[str, int] = {
    "annual": 365,
    "6_months": 182,
    "quarterly": 91,
}


@dataclass(frozen=True)
class Day0Snapshot:
    """What was decided at onboarding. Read-only here — this module never returns a mutation of it.

    `version` is the number of decisions already recorded for this customer, so the value this
    module returns is always `version + 1`. Passing it in rather than deriving it keeps the
    function pure and makes the append-only guarantee testable without a database.
    """

    case_id: str
    tier: RiskTier
    outcome: str
    decided_at: str
    version: int = 1


def risk_increased(day0_tier: RiskTier, new_tier: RiskTier) -> bool:
    """Whether the recomputed tier is strictly worse than the one held at onboarding."""
    return _TIER_RANK[new_tier] > _TIER_RANK[day0_tier]


def next_due_days(tier: RiskTier) -> int | None:
    """Days until the next scheduled review for a tier, or None when there is no relationship.

    `None` for PROHIBITED is deliberate and mirrors `TIER_ACTIONS`: a frozen-and-rejected customer
    has no ongoing relationship to review.
    """
    interval = TIER_ACTIONS[tier]["re_kyc_interval"]
    return None if interval is None else RE_KYC_DAYS[interval]


def history_sort_key(occurred_at: str, event_id: str) -> str:
    """Sort key for the append-only decision history: `DECISION#<event time>#<event id>`.

    Derived from the event, never from the run that processed it. The earlier key used the
    execution's start time, so a replayed event produced a *new* key, the `attribute_not_exists`
    guard on the history write passed, and one event wrote two decisions — the exact duplicate the
    guard existed to refuse. Two deliveries of one event now compute one key, and the second write
    is rejected.

    The partition key is the customer id, and ISO timestamps sort lexicographically, so a single
    query returns the whole history in event order: the reconstruction an examiner asks for.
    """
    if not occurred_at or not event_id:
        raise ValueError("occurred_at and event_id are both required")
    return f"DECISION#{occurred_at}#{event_id}"


def idempotency_name(customer_id: str, event_id: str) -> str:
    """Deterministic Step Functions execution name, which is what makes replay safe.

    `StartExecution` is idempotent for STANDARD workflows on the same name **and** the same input,
    so a duplicate or replayed `TransactionRiskEvent` collapses onto the original execution
    instead of producing a second, different decision. Standard is required here for another
    reason too: Express is at-least-once and caps at five minutes, and a lifecycle workflow may
    wait days.

    Sanitised because the API rejects most punctuation and caps the name at 80 characters. The
    prefix is kept and the tail truncated, so two events on one customer stay distinguishable by
    their event id rather than by a hash nobody can read in the console.
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "-"
                   for c in f"rekyc-{customer_id}-{event_id}")
    return safe[:80]


def reassess(
    *,
    day0: Day0Snapshot,
    event: RiskEvent,
    new_evidence: FullInputs | None,
    decided_at: str,
) -> LifecycleDecision:
    """Decide what a risk event means for an already-approved customer.

    `new_evidence is None` means the fraud re-assessment could not be completed. That is
    **SYSTEM_ERROR and a human**, never "risk unchanged" — the same fail-closed rule the
    onboarding phases use (R25.7). An unavailable control is not a clean result, and this is the
    direction of error that matters: silently treating a failed re-screen as "no change" would
    leave a customer whose risk actually rose looking untouched in the ledger.
    """
    version = day0.version + 1

    if new_evidence is None:
        return LifecycleDecision(
            case_id=day0.case_id,
            outcome=Phase3Outcome.SYSTEM_ERROR,
            day0_tier=day0.tier,
            new_tier=day0.tier,
            version=version,
            decided_at=decided_at,
            event_id=event.event_id,
            next_review_days=next_due_days(day0.tier),
            rationale=("Re-assessment could not be completed; the day-zero decision stands and a "
                       "reviewer must look at it. An incomplete control never reads as no change."),
        )

    # A critical event is a hard stop regardless of what the recomputed tier says. The tier is a
    # view of accumulated evidence; a confirmed sanctions hit or a confirmed-fraud signal arriving
    # mid-relationship is an event that outranks it.
    if event.severity is RiskEventSeverity.CRITICAL:
        return LifecycleDecision(
            case_id=day0.case_id,
            outcome=Phase3Outcome.FREEZE,
            day0_tier=day0.tier,
            new_tier=RiskTier.PROHIBITED,
            version=version,
            decided_at=decided_at,
            event_id=event.event_id,
            next_review_days=None,
            rationale=(f"Critical {event.event_type} on an approved customer. Frozen pending "
                       f"investigation; the day-zero decision is preserved unchanged."),
        )

    tier = full_tier(new_evidence)
    new_tier = tier.tier

    # A confirmed sanctions match found by a re-screen is a prohibition, not a risk movement to be
    # weighed: the account is frozen now and a human confirms the match afterwards. Waiting for the
    # review before freezing would leave a sanctioned person able to move money in the meantime,
    # and sanctions breaches are strict liability.
    if new_tier is RiskTier.PROHIBITED:
        return LifecycleDecision(
            case_id=day0.case_id,
            outcome=Phase3Outcome.FREEZE,
            day0_tier=day0.tier,
            new_tier=new_tier,
            version=version,
            decided_at=decided_at,
            event_id=event.event_id,
            next_review_days=None,
            rationale=(f"{event.event_type} produced {tier.rule}. Frozen pending compliance "
                       f"review; the day-zero decision is preserved unchanged."),
        )

    # A screening alert that no reviewer has cleared is a person's call even when the tier did not
    # move. Comparing tiers alone, a customer approved at HIGH — a PEP under enhanced due diligence,
    # say — who then partially matches a new listing is still HIGH, and the list update would be
    # logged as "risk unchanged". Alerts a reviewer cleared are already gone from the evidence
    # (their dispositions are applied before this runs), so what remains here is new.
    unresolved = [h.list_id for h in new_evidence.screening_hits
                  if h.strength in (MatchStrength.PROBABLE, MatchStrength.POSSIBLE)]
    if unresolved and not risk_increased(day0.tier, new_tier):
        return LifecycleDecision(
            case_id=day0.case_id,
            outcome=Phase3Outcome.RISK_INCREASED_REVIEW,
            day0_tier=day0.tier,
            new_tier=new_tier,
            version=version,
            decided_at=decided_at,
            event_id=event.event_id,
            next_review_days=next_due_days(new_tier),
            rationale=(f"{event.event_type} produced screening alerts no reviewer has cleared "
                       f"({', '.join(sorted(unresolved))}); escalated for review. The day-zero "
                       "decision is preserved."),
        )

    if not risk_increased(day0.tier, new_tier):
        return LifecycleDecision(
            case_id=day0.case_id,
            outcome=Phase3Outcome.RISK_UNCHANGED,
            day0_tier=day0.tier,
            new_tier=new_tier,
            version=version,
            decided_at=decided_at,
            event_id=event.event_id,
            next_review_days=next_due_days(new_tier),
            rationale=(f"{event.event_type} assessed; tier remains {new_tier.value}. "
                       f"Monitoring continues."),
        )

    # Risk went up. HIGH needs a human because the action at that tier is enhanced due diligence,
    # which this layer is not allowed to take unilaterally (R4.7).
    if new_tier is RiskTier.HIGH:
        outcome = Phase3Outcome.RISK_INCREASED_REVIEW
        action = "escalated for enhanced due diligence"
    else:
        outcome = Phase3Outcome.RISK_INCREASED_RESTRICT
        action = "restrictions re-applied automatically"

    return LifecycleDecision(
        case_id=day0.case_id,
        outcome=outcome,
        day0_tier=day0.tier,
        new_tier=new_tier,
        version=version,
        decided_at=decided_at,
        event_id=event.event_id,
        next_review_days=next_due_days(new_tier),
        rationale=(f"{event.event_type} raised the tier from {day0.tier.value} to "
                   f"{new_tier.value}; {action}. The day-zero decision is preserved."),
    )
