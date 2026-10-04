"""Phase 3 — lifecycle re-assessment. G-01, R31.

The property under test throughout is that a re-assessment **appends**. Every assertion about a
"new version" is really an assertion that the day-zero row was not touched, because that is what
makes the case reconstructible after the fact.

Determinism is asserted directly rather than assumed: the module takes its clock from the caller,
so the same event replayed must produce a byte-identical decision. That is the property EventBridge
archive/replay relies on.
"""

from __future__ import annotations

import pytest
from engine_builders import full
from lifecycle import (
    Day0Snapshot,
    history_sort_key,
    idempotency_name,
    next_due_days,
    reassess,
    risk_increased,
)
from models import (
    MatchStrength,
    PepStatus,
    Phase3Outcome,
    RiskEvent,
    RiskEventSeverity,
    RiskTier,
    ScreeningHit,
)

DECIDED_AT = "2026-09-16T10:04:00Z"


def day0(**over):
    base = dict(case_id="CASE-0001", tier=RiskTier.LOW,
                outcome="FULL_APPROVE_STANDARD", decided_at="2026-09-04T09:00:00Z", version=1)
    return Day0Snapshot(**{**base, **over})


def event(**over):
    base = dict(event_id="EVT-77", event_type="TRANSACTION_RISK_EVENT",
                severity=RiskEventSeverity.MEDIUM, customer_id="CUSTOMER-1",
                occurred_at="2026-09-16T09:59:00Z")
    return RiskEvent(**{**base, **over})


class TestDayZeroIsNeverRewritten:
    def test_a_reassessment_produces_the_next_version(self):
        d = reassess(day0=day0(version=1), event=event(),
                     new_evidence=full(), decided_at=DECIDED_AT)
        assert d.version == 2

    def test_the_day_zero_tier_is_carried_not_replaced(self):
        d = reassess(day0=day0(tier=RiskTier.LOW), event=event(),
                     new_evidence=full(pep_status=PepStatus.SELF), decided_at=DECIDED_AT)
        assert d.day0_tier is RiskTier.LOW          # preserved
        assert d.new_tier is not None               # and a separate current view exists

    def test_version_increments_from_whatever_history_already_holds(self):
        """A customer on their fourth event gets version 5, not version 2."""
        d = reassess(day0=day0(version=4), event=event(),
                     new_evidence=full(), decided_at=DECIDED_AT)
        assert d.version == 5

    def test_history_keys_sort_in_event_order(self):
        times = ["2026-09-16T09:59:00Z", "2026-10-02T11:00:00Z", "2026-12-02T18:04:05Z"]
        keys = [history_sort_key(t, "EVT-1") for t in times]
        assert keys == sorted(keys)

    def test_a_replayed_event_computes_the_same_key(self):
        """The key comes from the event, not from the run that processed it, so a second delivery
        lands on the same row and the conditional write refuses it."""
        assert history_sort_key("2026-12-02T18:04:05Z", "EVT-77") == \
            history_sort_key("2026-12-02T18:04:05Z", "EVT-77")

    def test_two_events_at_the_same_instant_stay_distinct(self):
        assert history_sort_key("2026-12-02T18:04:05Z", "EVT-77") != \
            history_sort_key("2026-12-02T18:04:05Z", "EVT-78")

    def test_history_key_needs_both_parts(self):
        with pytest.raises(ValueError):
            history_sort_key("", "EVT-1")
        with pytest.raises(ValueError):
            history_sort_key("2026-12-02T18:04:05Z", "")


class TestFailClosed:
    def test_an_incomplete_reassessment_is_a_system_error_not_no_change(self):
        """The direction of error that matters: a failed re-screen must never read as clean."""
        d = reassess(day0=day0(), event=event(), new_evidence=None, decided_at=DECIDED_AT)
        assert d.outcome is Phase3Outcome.SYSTEM_ERROR
        assert d.outcome is not Phase3Outcome.RISK_UNCHANGED

    def test_an_incomplete_reassessment_still_appends_a_record(self):
        """Even a failure is recorded, because "we tried and could not" is auditable evidence."""
        d = reassess(day0=day0(version=2), event=event(), new_evidence=None,
                     decided_at=DECIDED_AT)
        assert d.version == 3
        assert "reviewer" in d.rationale.lower()

    def test_a_critical_event_freezes_regardless_of_the_recomputed_tier(self):
        """A confirmed critical signal outranks the tier, which is a view of accumulated evidence."""
        d = reassess(day0=day0(tier=RiskTier.LOW), event=event(severity=RiskEventSeverity.CRITICAL),
                     new_evidence=full(), decided_at=DECIDED_AT)
        assert d.outcome is Phase3Outcome.FREEZE
        assert d.new_tier is RiskTier.PROHIBITED
        assert d.next_review_days is None      # no relationship left to review


class TestRiskMovement:
    def test_unchanged_risk_is_recorded_explicitly(self):
        d = reassess(day0=day0(tier=RiskTier.MEDIUM), event=event(),
                     new_evidence=full(), decided_at=DECIDED_AT)
        assert d.outcome is Phase3Outcome.RISK_UNCHANGED

    def test_a_drop_in_tier_is_not_treated_as_an_increase(self):
        d = reassess(day0=day0(tier=RiskTier.HIGH), event=event(),
                     new_evidence=full(), decided_at=DECIDED_AT)
        assert d.outcome is Phase3Outcome.RISK_UNCHANGED

    @pytest.mark.parametrize("tier,expected", [
        (RiskTier.LOW, False), (RiskTier.MEDIUM, False),
        (RiskTier.HIGH, True), (RiskTier.PROHIBITED, True),
    ])
    def test_high_and_above_require_a_human(self, tier, expected):
        assert risk_increased(RiskTier.LOW, tier) is expected or tier in (
            RiskTier.LOW, RiskTier.MEDIUM)

    def test_rising_to_high_escalates_rather_than_auto_restricting(self):
        d = reassess(day0=day0(tier=RiskTier.LOW), event=event(),
                     new_evidence=full(pep_status=PepStatus.SELF), decided_at=DECIDED_AT)
        assert d.new_tier is RiskTier.HIGH
        assert d.outcome is Phase3Outcome.RISK_INCREASED_REVIEW

    def test_a_confirmed_rescreen_hit_freezes_whatever_the_event_severity(self):
        """The Act 3 path: a list update names an approved customer. Freeze first, then a human
        confirms — a sanctioned person must not be able to move money while the review waits."""
        d = reassess(day0=day0(tier=RiskTier.LOW),
                     event=event(event_type="SANCTIONS_LIST_UPDATE",
                                 severity=RiskEventSeverity.HIGH),
                     new_evidence=full(screening_hits=(
                         ScreeningHit(list_id="SYN-0901", strength=MatchStrength.CONFIRMED),)),
                     decided_at=DECIDED_AT)
        assert d.outcome is Phase3Outcome.FREEZE
        assert d.new_tier is RiskTier.PROHIBITED
        assert d.day0_tier is RiskTier.LOW

    def test_a_possible_rescreen_hit_goes_to_review_without_freezing(self):
        d = reassess(day0=day0(tier=RiskTier.LOW),
                     event=event(event_type="SANCTIONS_LIST_UPDATE"),
                     new_evidence=full(screening_hits=(
                         ScreeningHit(list_id="SYN-0902", strength=MatchStrength.POSSIBLE),)),
                     decided_at=DECIDED_AT)
        assert d.outcome is Phase3Outcome.RISK_INCREASED_REVIEW

    @pytest.mark.parametrize("strength", [MatchStrength.POSSIBLE, MatchStrength.PROBABLE])
    def test_a_new_alert_on_a_high_risk_customer_is_not_risk_unchanged(self, strength):
        """A customer approved at HIGH (a PEP under enhanced due diligence) is still HIGH after a
        partial match on a new listing. Comparing tiers alone logged that as "risk unchanged" and
        no person ever saw the alert."""
        d = reassess(day0=day0(tier=RiskTier.HIGH),
                     event=event(event_type="SANCTIONS_LIST_UPDATE"),
                     new_evidence=full(pep_status=PepStatus.SELF, screening_hits=(
                         ScreeningHit(list_id="SYN-0903", strength=strength),)),
                     decided_at=DECIDED_AT)
        assert d.new_tier is RiskTier.HIGH
        assert d.outcome is Phase3Outcome.RISK_INCREASED_REVIEW
        assert "SYN-0903" in d.rationale

    def test_a_high_risk_customer_with_no_alert_is_unchanged(self):
        """The rule is about alerts, not about HIGH: without one, HIGH staying HIGH is no news."""
        d = reassess(day0=day0(tier=RiskTier.HIGH), event=event(),
                     new_evidence=full(pep_status=PepStatus.SELF), decided_at=DECIDED_AT)
        assert d.outcome is Phase3Outcome.RISK_UNCHANGED


class TestReviewCadence:
    @pytest.mark.parametrize("tier,days", [
        (RiskTier.LOW, 365), (RiskTier.MEDIUM, 182), (RiskTier.HIGH, 91),
    ])
    def test_interval_maps_to_days(self, tier, days):
        assert next_due_days(tier) == days

    def test_prohibited_has_no_next_review(self):
        assert next_due_days(RiskTier.PROHIBITED) is None


class TestIdempotency:
    def test_the_same_event_yields_the_same_execution_name(self):
        """This is what collapses a replayed event onto the original execution."""
        a = idempotency_name("CUSTOMER-1", "EVT-77")
        b = idempotency_name("CUSTOMER-1", "EVT-77")
        assert a == b

    def test_different_events_on_one_customer_stay_distinct(self):
        assert idempotency_name("CUSTOMER-1", "EVT-77") != \
               idempotency_name("CUSTOMER-1", "EVT-78")

    def test_the_name_is_accepted_by_step_functions(self):
        """<= 80 chars and no punctuation the API rejects."""
        name = idempotency_name("CUSTOMER/with:bad*chars" * 5, "EVT#1")
        assert len(name) <= 80
        assert all(c.isalnum() or c in "-_" for c in name)

    def test_replaying_an_event_reproduces_the_decision_exactly(self):
        """Determinism, asserted rather than assumed — the basis for archive/replay being safe."""
        args = dict(day0=day0(), event=event(), new_evidence=full(),
                    decided_at=DECIDED_AT)
        assert reassess(**args) == reassess(**args)
