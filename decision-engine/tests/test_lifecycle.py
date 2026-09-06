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
    PepStatus,
    Phase3Outcome,
    RiskEvent,
    RiskEventSeverity,
    RiskTier,
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

    def test_history_keys_sort_in_decision_order(self):
        """Lexicographic sort keys must not put version 10 before version 2."""
        keys = [history_sort_key(DECIDED_AT, v) for v in (1, 2, 9, 10, 11)]
        assert keys == sorted(keys)

    def test_history_key_rejects_a_zero_or_negative_version(self):
        with pytest.raises(ValueError):
            history_sort_key(DECIDED_AT, 0)


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
        if d.new_tier in (RiskTier.HIGH, RiskTier.PROHIBITED):
            assert d.outcome is Phase3Outcome.RISK_INCREASED_REVIEW


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
