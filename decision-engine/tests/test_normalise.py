"""Agent-result classification — task 6.1.

These tests exist because of a measured anomaly, not a hypothetical. A harness invocation
returned status `SUCCEEDED` with `Content: []`. Nothing errored. If an empty screening result is
read as "no hits found", the sanctioned applicant is approved faster than anyone else, and every
log and dashboard shows a healthy pipeline.

So the property under test is a safety property, stated as a bias: when in doubt, `UNAVAILABLE`.
"""

from __future__ import annotations

import pytest
from models import Phase1Outcome, ResultStatus, RiskTier
from normalise import agent_result_status, all_results_usable
from outcomes import select_phase1_outcome
from tiering import preliminary_tier

from engine_builders import prelim


class TestTheObservedAnomaly:
    def test_succeeded_with_empty_content_is_unavailable(self):
        """The exact shape observed in production."""
        raw = {"status": "SUCCEEDED", "Content": []}
        assert agent_result_status(raw) is ResultStatus.UNAVAILABLE

    def test_empty_content_lowercase_key_too(self):
        assert agent_result_status({"status": "SUCCEEDED", "content": []}) is \
            ResultStatus.UNAVAILABLE

    def test_an_empty_screening_result_never_approves(self):
        """The whole point. A clean tier plus an empty screening result must not approve."""
        tier = preliminary_tier(prelim())
        assert tier.tier is RiskTier.LOW  # nothing in the tier itself is wrong

        d = select_phase1_outcome(
            tier=tier,
            agent_results={"prescreen": {"status": "SUCCEEDED", "Content": []}},
        )
        assert d.outcome is Phase1Outcome.MANUAL_REVIEW
        assert d.reason_code == "EVIDENCE_UNAVAILABLE"

    def test_the_explanation_says_empty_is_not_a_pass(self):
        d = select_phase1_outcome(
            tier=preliminary_tier(prelim()),
            agent_results={"prescreen": {"status": "SUCCEEDED", "Content": []}},
        )
        assert "never read as a clean pass" in d.explanation


class TestEmptyContainers:
    @pytest.mark.parametrize("raw", [None, "", "   ", [], {}, ()])
    def test_every_empty_shape_is_unavailable(self, raw):
        assert agent_result_status(raw) is ResultStatus.UNAVAILABLE


class TestMarkers:
    @pytest.mark.parametrize("marker", [
        "UNAVAILABLE", "SOURCE_UNAVAILABLE", "TIMEOUT", "THROTTLED", "MISSING_TOOL_NAME",
    ])
    def test_transient_markers_are_unavailable(self, marker):
        assert agent_result_status({"error": marker}) is ResultStatus.UNAVAILABLE

    def test_missing_tool_name_is_unavailable(self):
        """F-80. The Gateway passes the tool name in Lambda client context, not the event, so a
        dispatcher reading only the event returns this and no tool ever ran. It must not read as
        a finding — that was the silent failure that looked like correct fail-closed behaviour."""
        assert agent_result_status({"error": "MISSING_TOOL_NAME"}) is ResultStatus.UNAVAILABLE

    @pytest.mark.parametrize("marker", [
        "IMAGE_UNREADABLE", "DOCUMENT_UNSUPPORTED", "SCHEMA_VIOLATION",
    ])
    def test_permanent_markers_are_invalid(self, marker):
        assert agent_result_status({"error": marker}) is ResultStatus.INVALID

    def test_image_unreadable_is_invalid_not_unavailable(self):
        """The distinction decides who is asked to act. UNAVAILABLE is retryable and ours to fix;
        a missing or illegible image is permanent and only the applicant can resolve it."""
        assert agent_result_status({"error": "IMAGE_UNREADABLE"}) is ResultStatus.INVALID
        assert agent_result_status({"error": "TIMEOUT"}) is ResultStatus.UNAVAILABLE

    def test_an_unrecognised_marker_is_still_an_error(self):
        """Defaulting unknown codes to OK would let any future tool error read as a clean pass."""
        assert agent_result_status({"error": "SOME_NEW_CODE_2027"}) is ResultStatus.UNAVAILABLE

    def test_nested_error_code(self):
        assert agent_result_status({"error": {"code": "THROTTLED"}}) is ResultStatus.UNAVAILABLE

    @pytest.mark.parametrize("status", ["FAILED", "ERROR", "failed"])
    def test_failed_status_is_unavailable(self, status):
        assert agent_result_status({"status": status, "content": [{"text": "x"}]}) is \
            ResultStatus.UNAVAILABLE


class TestUsableResults:
    def test_a_real_finding_is_ok(self):
        raw = {"status": "SUCCEEDED", "content": [{"text": "SYN-0001 CONFIRMED"}]}
        assert agent_result_status(raw) is ResultStatus.OK

    def test_all_results_usable_separates_the_two_failure_kinds(self):
        usable, unavailable, invalid = all_results_usable({
            "documents": {"error": "IMAGE_UNREADABLE"},
            "prescreen": {"status": "SUCCEEDED", "Content": []},
            "identity": {"content": [{"text": "ok"}]},
        })
        assert usable is False
        assert unavailable == ("prescreen",)
        assert invalid == ("documents",)

    def test_all_usable_when_everything_returns(self):
        usable, unavailable, invalid = all_results_usable({
            "identity": {"content": [{"text": "ok"}]},
            "documents": {"content": [{"text": "ok"}]},
        })
        assert usable is True
        assert unavailable == () and invalid == ()

    def test_empty_result_set_is_vacuously_usable(self):
        """No agents supplied is not the same as agents that failed. Phase 1 gates missing
        evidence at CheckPreconditions (R38), not here — this function only judges what it
        was given."""
        usable, unavailable, invalid = all_results_usable({})
        assert usable is True


class TestRoutingOfTheTwoFailureKinds:
    def test_invalid_becomes_step_up_and_names_the_item(self):
        """R3.6 — a step-up must name what it wants, and is a continuation not a rejection."""
        d = select_phase1_outcome(
            tier=preliminary_tier(prelim()),
            agent_results={"documents": {"error": "IMAGE_UNREADABLE"}},
        )
        assert d.outcome is Phase1Outcome.STEP_UP
        assert "documents" in d.required_item
        assert "not a rejection" in d.explanation

    def test_unavailable_becomes_manual_review_never_step_up(self):
        """Asking the applicant to fix our outage reads to them as an unexplained loop."""
        d = select_phase1_outcome(
            tier=preliminary_tier(prelim()),
            agent_results={"prescreen": {"error": "TIMEOUT"}},
        )
        assert d.outcome is Phase1Outcome.MANUAL_REVIEW

    def test_unavailable_outranks_invalid(self):
        """Both present: the human looks, rather than the applicant being sent away first."""
        d = select_phase1_outcome(
            tier=preliminary_tier(prelim()),
            agent_results={
                "documents": {"error": "IMAGE_UNREADABLE"},
                "prescreen": {"error": "TIMEOUT"},
            },
        )
        assert d.outcome is Phase1Outcome.MANUAL_REVIEW

    def test_a_confirmed_prohibition_still_outranks_an_outage(self):
        """Fail-closed must not become fail-open in the other direction: a sanctions match is
        not downgraded to MANUAL_REVIEW just because another agent timed out."""
        d = select_phase1_outcome(
            tier=preliminary_tier(prelim(pre_screen_matched=True)),
            agent_results={"identity": {"error": "TIMEOUT"}},
        )
        assert d.outcome is Phase1Outcome.REJECT
        assert d.notify_compliance is True
