"""The fifteen edge cases — task 6.9, design §8.4.

One test class per row, plus coverage and precedence tests over the table as a whole. None of
these may depend on model behaviour: every assertion runs against the pure engine.
"""

from __future__ import annotations

import pytest
from edge_cases import COVERED_REQUIREMENTS, EdgeFacts, evaluate_edge_cases
from models import Phase1Outcome, RiskTier
from outcomes import select_phase1_outcome
from tiering import preliminary_tier

from engine_builders import prelim


def only(facts: EdgeFacts):
    """The single verdict expected from a fact set, asserting nothing else fired."""
    vs = evaluate_edge_cases(facts)
    assert len(vs) == 1, f"expected exactly one verdict, got {[v.case_id for v in vs]}"
    return vs[0]


def decide(facts: EdgeFacts, **prelim_over):
    return select_phase1_outcome(
        tier=preliminary_tier(prelim(**prelim_over)),
        edge_verdicts=evaluate_edge_cases(facts),
    )


# --- R28 documents ------------------------------------------------------------------------


class TestR28Documents:
    def test_expired_document_steps_up(self):
        v = only(EdgeFacts(document_expired=True))
        assert v.requirement == "R28.1"
        assert v.outcome is Phase1Outcome.STEP_UP
        assert "current" in v.required_item.lower()

    def test_expired_is_not_recorded_as_a_rejection(self):
        """R3.6 — a step-up is a continuation."""
        d = decide(EdgeFacts(document_expired=True))
        assert d.outcome is Phase1Outcome.STEP_UP
        assert "not a rejection" in d.explanation

    def test_unsupported_country_reviews_and_never_auto_rejects(self):
        v = only(EdgeFacts(issuing_country_supported=False))
        assert v.requirement == "R28.2"
        assert v.outcome is Phase1Outcome.MANUAL_REVIEW

    def test_unsupported_country_frames_it_as_our_gap(self):
        v = only(EdgeFacts(issuing_country_supported=False))
        assert "coverage gap" in v.explanation

    def test_supported_country_does_not_fire(self):
        assert evaluate_edge_cases(EdgeFacts(issuing_country_supported=True)) == ()

    def test_screen_photograph_steps_up_for_direct_capture(self):
        v = only(EdgeFacts(is_screen_photograph=True))
        assert v.requirement == "R28.3"
        assert v.outcome is Phase1Outcome.STEP_UP
        assert "direct capture" in v.required_item

    def test_low_quality_steps_up_and_names_the_defect(self):
        """R28.4 requires naming the defect — "try again" with no reason is a dead end."""
        v = only(EdgeFacts(document_quality=0.4, quality_defects=("blurred", "glare")))
        assert v.requirement == "R28.4"
        assert v.outcome is Phase1Outcome.STEP_UP
        assert "blurred" in v.required_item and "glare" in v.required_item

    def test_quality_at_threshold_does_not_fire(self):
        assert evaluate_edge_cases(EdgeFacts(document_quality=0.8)) == ()

    def test_quality_below_threshold_fires(self):
        assert len(evaluate_edge_cases(EdgeFacts(document_quality=0.7999))) == 1


# --- R29 person ---------------------------------------------------------------------------


class TestR29Person:
    def test_under_18_reviews_with_no_provisional_account(self):
        v = only(EdgeFacts(age=16))
        assert v.requirement == "R29.1"
        assert v.outcome is Phase1Outcome.MANUAL_REVIEW
        assert v.signals["provisional_account_blocked"] is True

    def test_under_18_never_yields_a_provisional_account(self):
        d = decide(EdgeFacts(age=16))
        assert d.outcome is not Phase1Outcome.PROVISIONAL_APPROVE_RESTRICTED

    def test_exactly_18_does_not_fire(self):
        assert evaluate_edge_cases(EdgeFacts(age=18)) == ()

    def test_unknown_age_does_not_fire(self):
        """`None` means not established, and must never read as an adult."""
        assert evaluate_edge_cases(EdgeFacts(age=None)) == ()

    def test_dual_nationality_applies_the_stricter_jurisdiction(self):
        v = only(EdgeFacts(
            nationalities=("us", "jp"),
            jurisdiction_risk_by_code={"us": RiskTier.LOW, "jp": RiskTier.HIGH},
        ))
        assert v.requirement == "R29.2"
        assert v.directive == "APPLY_STRICTER_JURISDICTION"
        assert v.signals["jurisdiction_selected"] == "jp"

    def test_dual_nationality_records_both_candidates_and_the_rule(self):
        """R12.5 — without the rule, a reviewer cannot tell whether the stricter one was used."""
        v = only(EdgeFacts(
            nationalities=("uk", "au"),
            jurisdiction_risk_by_code={"uk": RiskTier.MEDIUM, "au": RiskTier.MEDIUM},
        ))
        assert set(v.signals["nationality_candidates"]) == {"uk", "au"}
        assert v.signals["jurisdiction_selection_rule"]

    def test_equal_risk_resolves_deterministically_not_by_input_order(self):
        """Otherwise the same applicant is assessed differently depending on passport order."""
        risk = {"uk": RiskTier.MEDIUM, "au": RiskTier.MEDIUM}
        a = only(EdgeFacts(nationalities=("uk", "au"), jurisdiction_risk_by_code=risk))
        b = only(EdgeFacts(nationalities=("au", "uk"), jurisdiction_risk_by_code=risk))
        assert a.signals["jurisdiction_selected"] == b.signals["jurisdiction_selected"]

    def test_single_nationality_does_not_fire(self):
        assert evaluate_edge_cases(EdgeFacts(nationalities=("us",))) == ()

    def test_three_differing_countries_raise_a_signal(self):
        v = only(EdgeFacts(passport_country="us", address_country="uk", ip_country="jp"))
        assert v.requirement == "R29.3"
        assert v.directive == "RAISE_RISK_SIGNAL"
        for k in ("passport_country", "address_country", "ip_country"):
            assert v.signals[k]

    def test_country_divergence_is_not_an_outcome_on_its_own(self):
        """Travellers and expatriates exist. This informs risk, it does not decide."""
        v = only(EdgeFacts(passport_country="us", address_country="uk", ip_country="jp"))
        assert v.outcome is None
        d = decide(EdgeFacts(passport_country="us", address_country="uk", ip_country="jp"))
        assert d.outcome is Phase1Outcome.PROVISIONAL_APPROVE_RESTRICTED

    def test_two_matching_countries_do_not_fire(self):
        assert evaluate_edge_cases(EdgeFacts(
            passport_country="us", address_country="us", ip_country="jp")) == ()

    def test_thin_file_steps_up_and_never_rejects(self):
        v = only(EdgeFacts(is_thin_file=True))
        assert v.requirement == "R29.4"
        assert v.outcome is Phase1Outcome.STEP_UP
        assert "alternative" in v.required_item.lower()

    def test_thin_file_reasoning_names_fair_access(self):
        v = only(EdgeFacts(is_thin_file=True))
        assert "fair-access" in v.explanation

    def test_deceased_indicator_rejects_with_the_reason_code(self):
        v = only(EdgeFacts(deceased_indicator=True))
        assert v.requirement == "R29.5"
        assert v.outcome is Phase1Outcome.REJECT
        assert v.reason_code == "DECEASED_IDENTITY"
        assert v.notify_compliance is True

    def test_deceased_reaches_the_final_decision(self):
        d = decide(EdgeFacts(deceased_indicator=True))
        assert d.outcome is Phase1Outcome.REJECT
        assert d.reason_code == "DECEASED_IDENTITY"
        assert d.notify_compliance is True

    def test_shared_attributes_route_both_cases_to_review(self):
        v = only(EdgeFacts(shared_attribute_count=4, shared_with_case_id="CASE-77"))
        assert v.requirement == "R29.6"
        assert v.outcome is Phase1Outcome.MANUAL_REVIEW
        assert v.signals["route_both_cases"] is True
        assert v.signals["possible_synthetic_identity"] is True

    def test_shared_attributes_below_threshold_do_not_fire(self):
        assert evaluate_edge_cases(EdgeFacts(shared_attribute_count=2)) == ()

    def test_shared_attributes_at_threshold_fire(self):
        assert len(evaluate_edge_cases(EdgeFacts(shared_attribute_count=3))) == 1


# --- R30 process --------------------------------------------------------------------------


class TestR30Process:
    def test_abandonment_inside_the_window_stays_resumable(self):
        v = only(EdgeFacts(abandoned_mid_flow=True, within_resume_window=True,
                           pending_step_up_item="proof of address"))
        assert v.requirement == "R30.1"
        assert v.directive == "KEEP_RESUMABLE"
        assert v.required_item == "proof of address"

    def test_abandonment_outside_the_window_is_not_an_edge_case(self):
        """R5.5 decides REJECT / ABANDONED as a lifecycle transition. Emitting a verdict here
        would put a second rejection into a table design §8.4 says holds exactly one."""
        assert evaluate_edge_cases(
            EdgeFacts(abandoned_mid_flow=True, within_resume_window=False)) == ()

    def test_three_liveness_failures_review_with_no_lockout(self):
        v = only(EdgeFacts(liveness_failure_count=3))
        assert v.requirement == "R30.2"
        assert v.outcome is Phase1Outcome.MANUAL_REVIEW
        assert v.signals["permanent_lockout"] is False
        assert v.signals["routed_to_reviewer"] is True
        # Nothing offers the applicant another route, so the record must not say one was.
        assert v.signals["alternative_path_offered"] is False

    def test_two_liveness_failures_do_not_fire(self):
        assert evaluate_edge_cases(EdgeFacts(liveness_failure_count=2)) == ()

    def test_liveness_reasoning_names_who_a_lockout_would_exclude(self):
        """D-05 exists because liveness fails on lighting, cheap cameras and some disabilities."""
        v = only(EdgeFacts(liveness_failure_count=5))
        assert "disabilities" in v.explanation

    def test_duplicate_submission_is_discarded_not_decided(self):
        v = only(EdgeFacts(is_duplicate_submission=True, canonical_case_id="CASE-1"))
        assert v.requirement == "R30.3"
        assert v.directive == "DEDUP_DISCARD_DUPLICATE"
        assert v.outcome is None
        assert v.signals["canonical_case_id"] == "CASE-1"

    def test_already_decided_shows_existing_and_blocks_second(self):
        v = only(EdgeFacts(case_already_decided=True, existing_outcome="REJECT"))
        assert v.requirement == "R30.4"
        assert v.directive == "SHOW_EXISTING_DECISION_BLOCK_SECOND"
        assert v.signals["existing_outcome"] == "REJECT"

    def test_resubmission_after_reject_gets_the_appeal_path(self):
        v = only(EdgeFacts(resubmitted_after_final_decision=True,
                           prior_outcome="REJECT", prior_case_id="CASE-9"))
        assert v.requirement == "R30.5"
        assert v.directive == "LINK_PRIOR_CASE_APPEAL"
        assert v.signals["appeal_path"] is True

    def test_resubmission_after_approve_links_without_appeal(self):
        v = only(EdgeFacts(resubmitted_after_final_decision=True,
                           prior_outcome="FULL_APPROVE_STANDARD", prior_case_id="CASE-9"))
        assert v.directive == "LINK_PRIOR_CASE"
        assert v.signals["appeal_path"] is False


# --- the table as a whole -----------------------------------------------------------------


class TestTableProperties:
    def test_all_fifteen_requirements_are_covered(self):
        assert len(COVERED_REQUIREMENTS) == 15
        assert len(set(COVERED_REQUIREMENTS)) == 15

    def test_every_row_can_actually_fire(self):
        """Guards against a rule that exists but is unreachable — a control that does not run."""
        triggers = [
            EdgeFacts(document_expired=True),
            EdgeFacts(issuing_country_supported=False),
            EdgeFacts(is_screen_photograph=True),
            EdgeFacts(document_quality=0.1),
            EdgeFacts(age=10),
            EdgeFacts(nationalities=("us", "jp")),
            EdgeFacts(passport_country="us", address_country="uk", ip_country="jp"),
            EdgeFacts(is_thin_file=True),
            EdgeFacts(deceased_indicator=True),
            EdgeFacts(shared_attribute_count=9),
            EdgeFacts(abandoned_mid_flow=True, within_resume_window=True),
            EdgeFacts(liveness_failure_count=3),
            EdgeFacts(is_duplicate_submission=True),
            EdgeFacts(case_already_decided=True),
            EdgeFacts(resubmitted_after_final_decision=True, prior_outcome="REJECT"),
        ]
        assert len(triggers) == 15
        fired = set()
        for f in triggers:
            vs = evaluate_edge_cases(f)
            assert vs, "a row in the table cannot be triggered"
            fired.update(v.requirement for v in vs)
        assert fired == set(COVERED_REQUIREMENTS)

    def test_exactly_one_row_ends_in_rejection(self):
        """design §8.4's deliberate ratio, pinned so a later edit cannot quietly break it."""
        rejects = []
        for f in [
            EdgeFacts(document_expired=True), EdgeFacts(issuing_country_supported=False),
            EdgeFacts(is_screen_photograph=True), EdgeFacts(document_quality=0.1),
            EdgeFacts(age=10), EdgeFacts(nationalities=("us", "jp")),
            EdgeFacts(passport_country="us", address_country="uk", ip_country="jp"),
            EdgeFacts(is_thin_file=True), EdgeFacts(deceased_indicator=True),
            EdgeFacts(shared_attribute_count=9),
            EdgeFacts(abandoned_mid_flow=True, within_resume_window=True),
            EdgeFacts(liveness_failure_count=3), EdgeFacts(is_duplicate_submission=True),
            EdgeFacts(case_already_decided=True),
            EdgeFacts(resubmitted_after_final_decision=True, prior_outcome="REJECT"),
        ]:
            for v in evaluate_edge_cases(f):
                if v.outcome is Phase1Outcome.REJECT:
                    rejects.append(v.requirement)
        assert rejects == ["R29.5"]

    def test_nine_rows_decide_and_six_direct(self):
        """Forcing the six directives into outcome enums would claim the engine decides things
        it does not — for instance that "record all three countries" is a verdict."""
        deciding, directing = 0, 0
        for f in [
            EdgeFacts(document_expired=True), EdgeFacts(issuing_country_supported=False),
            EdgeFacts(is_screen_photograph=True), EdgeFacts(document_quality=0.1),
            EdgeFacts(age=10), EdgeFacts(nationalities=("us", "jp")),
            EdgeFacts(passport_country="us", address_country="uk", ip_country="jp"),
            EdgeFacts(is_thin_file=True), EdgeFacts(deceased_indicator=True),
            EdgeFacts(shared_attribute_count=9),
            EdgeFacts(abandoned_mid_flow=True, within_resume_window=True),
            EdgeFacts(liveness_failure_count=3), EdgeFacts(is_duplicate_submission=True),
            EdgeFacts(case_already_decided=True),
            EdgeFacts(resubmitted_after_final_decision=True, prior_outcome="REJECT"),
        ]:
            for v in evaluate_edge_cases(f):
                if v.outcome is not None:
                    deciding += 1
                else:
                    directing += 1
        assert (deciding, directing) == (9, 6)

    def test_empty_facts_fire_nothing(self):
        """Fail-open would be wrong here in the opposite direction: an edge case must be
        established, never presumed."""
        assert evaluate_edge_cases(EdgeFacts()) == ()

    def test_all_rules_are_evaluated_not_short_circuited(self):
        """The audit record must show every matched row, not just the governing one."""
        vs = evaluate_edge_cases(EdgeFacts(
            document_expired=True, age=15, deceased_indicator=True, is_thin_file=True))
        assert {v.requirement for v in vs} == {"R28.1", "R29.1", "R29.5", "R29.4"}


class TestPrecedence:
    def test_reject_beats_review_and_step_up(self):
        d = decide(EdgeFacts(deceased_indicator=True, age=15, document_expired=True))
        assert d.outcome is Phase1Outcome.REJECT

    def test_review_beats_step_up(self):
        """An under-18 applicant with an expired passport needs a human, not a newer passport."""
        d = decide(EdgeFacts(age=15, document_expired=True))
        assert d.outcome is Phase1Outcome.MANUAL_REVIEW

    def test_a_directive_never_outranks_a_real_outcome(self):
        d = decide(EdgeFacts(
            passport_country="us", address_country="uk", ip_country="jp",
            document_expired=True))
        assert d.outcome is Phase1Outcome.STEP_UP

    def test_directives_survive_into_the_record_even_when_outranked(self):
        """A jurisdiction selection or prior-case link is not cancelled by a severe outcome."""
        d = decide(EdgeFacts(
            deceased_indicator=True,
            nationalities=("us", "jp"),
            jurisdiction_risk_by_code={"us": RiskTier.LOW, "jp": RiskTier.HIGH},
        ))
        assert d.outcome is Phase1Outcome.REJECT
        directives = {x["requirement"] for x in d.recorded_signals["directives"]}
        assert "R29.2" in directives

    def test_matched_cases_are_all_listed_on_the_decision(self):
        d = decide(EdgeFacts(age=15, document_expired=True, is_thin_file=True))
        assert len(d.edge_cases) == 3

    @pytest.mark.parametrize("n", [1, 50])
    def test_edge_evaluation_is_deterministic(self, n):
        facts = EdgeFacts(age=15, document_expired=True, shared_attribute_count=5)
        first = tuple(v.case_id for v in evaluate_edge_cases(facts))
        for _ in range(n):
            assert tuple(v.case_id for v in evaluate_edge_cases(facts)) == first
