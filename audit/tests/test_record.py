"""Decision record assembly — task 6.7, R18.1, R18.2, R18.6, design §9.2."""

from __future__ import annotations

import json
from enum import Enum

import pytest

from record import (
    DecisionRecord,
    EvidenceSource,
    IncompleteReason,
    IncompleteRecord,
    VersionPins,
    build_decision_record,
    decision_object_key,
    outcome_to_mapping,
    to_json_bytes,
    to_payload,
)
from audit_builders import SFN_OUTPUT, agent_span, complete_spans, decision_span, tool_span
from spans import collect_evidence, spans_from_sfn_output

PINS = VersionPins(
    harness_version="3",
    endpoint_name="prod",
    policy_engine_version="1.4.0",
    gateway_arn="arn:aws:bedrock-agentcore:us-east-1:111122223333:gateway/g-1",
)

CASE = {
    "applicantId": "APP-001",
    "caseId": "CASE-001",
    "jurisdiction": "UK",
    "liveness": {"result": "PASS", "region": "us-east-1", "sessionId": "s-1",
                 "retrievedAt": "2026-08-09T10:00:00Z", "referenceImageKey": "ref.jpg",
                 "auditImageKeys": ["a.jpg"]},
    "documents": [{"key": "doc.png", "type": "PASSPORT", "validationResult": "OK"}],
    "transitions": [{"from": "RECEIVED", "to": "IN_PROGRESS", "at": "2026-08-09T10:00:01Z",
                     "actor": "system", "reason": "preconditions pass"}],
}

OUTCOME = {
    "outcome": "PROVISIONAL_APPROVE_RESTRICTED",
    "rule": "O1.provisional_approve",
    "tier": "LOW",
    "tier_rule": "P7.default_low",
    "tier_inputs": {"document_confidence": 0.97},
    "thresholds_in_force": {"document_confidence_min": 0.85},
    "edge_cases": ("E-03(R28.1)",),
}


def build(*, spans=None, case=None, outcome=None, versions=PINS, sources=None,
          ingress_latency_ms=None):
    return build_decision_record(
        case_record=CASE if case is None else case,
        evidence=collect_evidence(complete_spans() if spans is None else spans),
        outcome=OUTCOME if outcome is None else outcome,
        versions=versions,
        decision_id="DEC-001",
        decided_at="2026-08-09T10:01:30Z",
        phase="phase1",
        sources=sources,
        ingress_latency_ms=ingress_latency_ms,
    )


class TestHappyPath:
    def test_a_complete_record_is_built(self):
        record = build()
        assert isinstance(record, DecisionRecord)
        assert record.complete
        assert (record.applicant_id, record.case_id, record.jurisdiction) == (
            "APP-001", "CASE-001", "UK")

    def test_tool_calls_with_arguments_and_results_reach_the_record(self):
        # R18.1's hardest clause, and the one Step Functions output cannot answer at all.
        call, = build().tool_calls
        assert call["toolName"] == "sanctions_screen"
        assert call["arguments"] == {"name": "A"}
        assert call["result"] == {"hits": []}

    def test_agent_order_is_preserved(self):
        spans = [agent_span("Screening", start=300), agent_span("Docs", start=100),
                 tool_span(), decision_span()]
        assert [r["agentName"] for r in build(spans=spans).agent_runs] == ["Docs", "Screening"]

    def test_ingress_latency_is_recorded_when_measured(self):
        """A-05/task 7.3 — T2's queue leg, measured. Not a fabricated value: absent entirely on
        the direct-StartExecution path (see next test), never coerced to 0."""
        record = build(ingress_latency_ms=842.0)
        assert record.ingress_latency_ms == 842.0

    def test_ingress_latency_is_none_when_not_measured(self):
        """A run started directly (synchronous API contract, replay/ops tooling) never touched
        the ingress queue, so there is nothing to measure — `None`, not 0."""
        record = build()
        assert record.ingress_latency_ms is None

    def test_all_four_version_pins_are_recorded(self):
        versions = to_payload(build())["versions"]
        assert versions == {
            "harnessVersion": "3",
            "endpointName": "prod",
            "policyEngineVersion": "1.4.0",
            "gatewayArn": PINS.gateway_arn,
        }

    def test_sources_are_stated_in_the_payload(self):
        assert build().sources == ("CASE_RECORD", "OTEL_SPANS")

    def test_liveness_region_is_retained_for_cross_border_cases(self):
        assert build().liveness["region"] == "us-east-1"

    def test_reasoning_text_never_enters_the_object_lock_payload(self):
        # Compliance mode forbids deletion, so reasoning here would make R18.9's deletion
        # obligation permanently unsatisfiable (R18.8).
        spans = [*complete_spans(),
                 {"name": "model.turn", "attributes": {"kyc.reasoning": "SECRET-THINKING"}}]
        assert b"SECRET-THINKING" not in to_json_bytes(build(spans=spans))


class TestWhatAReviewedDecisionRecords:
    """The record of an approval must say what was approved past, not only that a person
    approved. Before these fields, a reviewer's APPROVE of an engine MANUAL_REVIEW sealed a record
    indistinguishable from the rules approving on their own."""

    REVIEWED = {
        # Exactly what `asl._reviewed()` sends: camelCase, the engine's view beside the person's.
        "outcome": "PROVISIONAL_APPROVE_RESTRICTED",
        "rule": "REVIEW.approved",
        "engineOutcome": "MANUAL_REVIEW",
        "reviewReason": "ENGINE_MANUAL_REVIEW",
        "clearedAlerts": ["SYN-0106"],
    }

    def test_the_engines_outcome_the_reason_and_the_cleared_alerts_are_kept(self):
        decision = to_payload(build(outcome=self.REVIEWED))["decision"]
        assert decision["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert decision["engineOutcome"] == "MANUAL_REVIEW"
        assert decision["reviewReason"] == "ENGINE_MANUAL_REVIEW"
        assert decision["clearedAlerts"] == ["SYN-0106"]

    def test_snake_case_is_read_as_well(self):
        record = build(outcome={"outcome": "REJECT", "rule": "REVIEW.rejected",
                                "engine_outcome": "MANUAL_REVIEW",
                                "review_reason": "ACCOUNT_FROZEN",
                                "cleared_alerts": ()})
        assert (record.engine_outcome, record.review_reason, record.cleared_alerts) == (
            "MANUAL_REVIEW", "ACCOUNT_FROZEN", ())

    def test_a_rules_decision_has_none_of_them(self):
        decision = to_payload(build())["decision"]
        assert decision["engineOutcome"] is None
        assert decision["reviewReason"] is None
        assert decision["clearedAlerts"] == []

    def test_a_phase2_record_names_the_provisional_decision_it_supersedes(self):
        payload = to_payload(build(outcome={**OUTCOME, "supersedesDecisionId": "DEC-P1"}))
        assert payload["supersedesDecisionId"] == "DEC-P1"
        assert to_payload(build())["supersedesDecisionId"] is None

    def test_the_execution_that_decided_is_named(self):
        """What the reviewer console uses to find the timeline (`review_api._execution_timeline`).
        It was never recorded, so that lookup could never resolve."""
        record = build_decision_record(
            case_record=CASE, evidence=collect_evidence(complete_spans()), outcome=OUTCOME,
            versions=PINS, decision_id="DEC-001", decided_at="2026-08-09T10:01:30Z",
            phase="phase2", execution_name="DEC-P1")
        assert to_payload(record)["executionName"] == "DEC-P1"
        assert to_payload(build())["executionName"] is None

    def test_the_new_fields_leave_serialisation_deterministic(self):
        assert to_json_bytes(build(outcome=self.REVIEWED)) == to_json_bytes(
            build(outcome=dict(reversed(list(self.REVIEWED.items())))))


class TestImmutability:
    def test_fields_cannot_be_rebound(self):
        with pytest.raises(Exception):
            build().outcome = "REJECT"

    def test_nested_mappings_cannot_be_mutated(self):
        record = build()
        with pytest.raises(TypeError):
            record.tier_inputs["document_confidence"] = 0.1

    def test_nested_tool_call_entries_cannot_be_mutated(self):
        record = build()
        with pytest.raises(TypeError):
            record.tool_calls[0]["arguments"] = {"name": "someone else"}


class TestIncompleteDetection:
    def test_missing_tool_spans_block_the_record(self):
        record = build(spans=[agent_span(), decision_span()])
        assert isinstance(record, IncompleteRecord)
        assert IncompleteReason.MISSING_SPANS in record.reasons
        assert "TOOL_CALL" in record.missing

    @pytest.mark.parametrize("field", [
        "harness_version", "endpoint_name", "policy_engine_version", "gateway_arn"])
    def test_each_missing_version_pin_blocks_the_record(self, field):
        pins = VersionPins(**{**PINS.__dict__, field: ""})
        record = build(versions=pins)
        assert isinstance(record, IncompleteRecord)
        assert IncompleteReason.MISSING_VERSION_PINS in record.reasons
        assert field in record.missing

    def test_missing_case_fields_block_the_record(self):
        record = build(case={**CASE, "caseId": ""})
        assert isinstance(record, IncompleteRecord)
        assert IncompleteReason.MISSING_CASE_FIELDS in record.reasons

    def test_one_source_alone_is_not_enough(self):
        record = build(sources=[EvidenceSource.OTEL_SPANS])
        assert isinstance(record, IncompleteRecord)
        assert IncompleteReason.MISSING_SOURCE in record.reasons
        assert "CASE_RECORD" in record.missing

    def test_incomplete_marker_is_explicit_not_a_partial_record(self):
        record = build(spans=[])
        assert not record.complete
        assert not isinstance(record, DecisionRecord)
        assert record.detail


class TestStepFunctionsOutputIsRejected:
    """R18.2 and F-13 — the constraint the whole task hangs on."""

    def test_a_record_assembled_from_sfn_output_alone_is_rejected(self):
        record = build_decision_record(
            case_record=CASE,
            evidence=collect_evidence(spans_from_sfn_output(SFN_OUTPUT)),
            outcome=OUTCOME,
            versions=PINS,
            decision_id="DEC-002",
            decided_at="2026-08-09T10:01:30Z",
            phase="phase1",
            sources=[EvidenceSource.STEP_FUNCTIONS_OUTPUT],
        )
        assert isinstance(record, IncompleteRecord)
        assert IncompleteReason.SFN_OUTPUT_NOT_AN_AUDIT_SOURCE in record.reasons

    def test_naming_sfn_as_a_source_is_rejected_even_when_spans_are_complete(self):
        # The prohibition is on the source, not on the resulting shape. A record that mixes in
        # execution output cannot be trusted about which turns it saw.
        record = build(sources=[EvidenceSource.OTEL_SPANS, EvidenceSource.CASE_RECORD,
                                EvidenceSource.STEP_FUNCTIONS_OUTPUT])
        assert isinstance(record, IncompleteRecord)
        assert IncompleteReason.SFN_OUTPUT_NOT_AN_AUDIT_SOURCE in record.reasons
        assert "omits tool-use" in record.detail

    def test_the_reason_is_reported_before_downstream_span_gaps(self):
        # A caller who passed execution output has a design error; leading with "TOOL_CALL spans
        # missing" would send them hunting an infrastructure fault that does not exist.
        record = build_decision_record(
            case_record=CASE,
            evidence=collect_evidence(spans_from_sfn_output(SFN_OUTPUT)),
            outcome=OUTCOME,
            versions=PINS,
            decision_id="DEC-003",
            decided_at="2026-08-09T10:01:30Z",
            phase="phase1",
            sources=[EvidenceSource.STEP_FUNCTIONS_OUTPUT],
        )
        assert record.reasons[0] is IncompleteReason.SFN_OUTPUT_NOT_AN_AUDIT_SOURCE


class TestObjectKey:
    def test_key_matches_the_design_path(self):
        assert decision_object_key(
            applicant_id="APP-001", phase="phase1", decision_id="DEC-001"
        ) == "APP-001/phase1/DEC-001.json"

    @pytest.mark.parametrize("applicant", ["../APP", "a/b", "", " APP "])
    def test_unsafe_components_are_refused(self, applicant):
        # Under Object Lock a misplaced object cannot be moved or deleted for seven years.
        with pytest.raises(ValueError):
            decision_object_key(applicant_id=applicant, phase="phase1", decision_id="DEC-001")


class TestSerialisation:
    def test_serialisation_is_deterministic(self):
        assert to_json_bytes(build()) == to_json_bytes(build())

    def test_payload_round_trips_as_json(self):
        loaded = json.loads(to_json_bytes(build()))
        assert loaded["decision"]["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert loaded["evidence"]["toolCalls"][0]["toolName"] == "sanctions_screen"
        assert loaded["schemaVersion"] == "1"


class TestOutcomeFlattening:
    def test_duck_typed_engine_decision_is_flattened(self):
        # Stand-ins for the engine's own enums: the flattener must not import them, but it does
        # rely on them being real `Enum`s, which is what `models.py` defines.
        class Tier(Enum):
            MEDIUM = "MEDIUM"

        class Outcome(Enum):
            MANUAL_REVIEW = "MANUAL_REVIEW"

        class FakeTier:
            tier = Tier.MEDIUM
            rule = "P4.pep_associate"
            inputs = {"pep_status": "ASSOCIATE"}
            thresholds = {"document_confidence_min": 0.85}
            config_version = "1.4.0"

        class FakeDecision:
            outcome = Outcome.MANUAL_REVIEW
            rule = "O1.preliminary_tier_high"
            reason_code = "PRELIMINARY_RISK_HIGH"
            explanation = "why"
            required_item = None
            notify_compliance = True
            tier = FakeTier()
            edge_cases = ("E-01(R28.1)",)
            recorded_signals = {"unavailable_agents": []}

        flat = outcome_to_mapping(FakeDecision())
        assert flat["outcome"] == "MANUAL_REVIEW"
        assert flat["tier"] == "MEDIUM"
        assert flat["tier_rule"] == "P4.pep_associate"
        assert flat["config_version"] == "1.4.0"

    def test_flattened_outcome_builds_a_record(self):
        record = build(outcome={**OUTCOME, "reason_code": "PRELIMINARY_RISK_HIGH"})
        assert record.reason_code == "PRELIMINARY_RISK_HIGH"
