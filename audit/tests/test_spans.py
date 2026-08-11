"""Span extraction — task 6.7, R18.1, R18.2, F-13."""

from __future__ import annotations

import pytest

from audit_builders import SFN_OUTPUT, agent_span, complete_spans, decision_span, tool_span
from spans import (
    REQUIRED_KINDS,
    SpanKind,
    classify_span,
    collect_evidence,
    extract_agent_runs,
    extract_decision,
    extract_reasoning,
    extract_tool_calls,
    missing_kinds,
    span_attributes,
    spans_from_sfn_output,
)


class TestAttributes:
    def test_sdk_shape_attributes_are_read(self):
        assert span_attributes({"attributes": {"a": 1}}) == {"a": 1}

    def test_otlp_keyvalue_shape_is_read(self):
        span = {"attributes": [
            {"key": "gen_ai.tool.name", "value": {"stringValue": "sanctions_screen"}},
            {"key": "kyc.retries", "value": {"intValue": "2"}},
        ]}
        assert span_attributes(span) == {"gen_ai.tool.name": "sanctions_screen",
                                        "kyc.retries": 2}

    def test_otlp_shape_classifies_the_same_as_the_sdk_shape(self):
        # A record that silently empties when the emitting SDK changes shape is the failure this
        # guards: both forms carry identical facts and must classify identically.
        span = {"attributes": [
            {"key": "gen_ai.tool.name", "value": {"stringValue": "sanctions_screen"}}]}
        assert classify_span(span) is SpanKind.TOOL_CALL

    def test_missing_attributes_do_not_raise(self):
        assert span_attributes({}) == {}


class TestClassification:
    @pytest.mark.parametrize("span,expected", [
        (agent_span(), SpanKind.AGENT_RUN),
        (tool_span(), SpanKind.TOOL_CALL),
        (decision_span(), SpanKind.DECISION),
        ({"name": "reasoning.block", "attributes": {}}, SpanKind.REASONING),
        ({"name": "http.request", "attributes": {}}, SpanKind.OTHER),
    ])
    def test_kinds(self, span, expected):
        assert classify_span(span) is expected

    def test_attributes_outrank_the_span_name(self):
        span = tool_span()
        span["name"] = "some.renamed.operation"
        assert classify_span(span) is SpanKind.TOOL_CALL


class TestExtraction:
    def test_agent_runs_carry_model_and_version(self):
        run, = extract_agent_runs([agent_span()])
        assert (run.agent_name, run.phase, run.harness_version, run.endpoint_name) == (
            "DocumentIntelligence", "phase1", "3", "prod")
        assert run.model_id == "amazon.nova-2-lite-v1:0"

    def test_agent_runs_come_back_in_start_order(self):
        spans = [agent_span("Screening", start=300), agent_span("Docs", start=100)]
        assert [r.agent_name for r in extract_agent_runs(spans)] == ["Docs", "Screening"]

    def test_tool_calls_carry_parsed_arguments_and_results(self):
        call, = extract_tool_calls([tool_span()])
        assert call.arguments == {"name": "A"}
        assert call.result == {"hits": []}
        assert call.gateway_arn.endswith("gateway/g-1")

    def test_unparseable_tool_arguments_are_kept_raw(self):
        call, = extract_tool_calls([tool_span(args="{not json")])
        assert call.arguments == "{not json"

    def test_reasoning_is_extracted_separately_from_the_decision_record(self):
        span = {"name": "model.turn", "attributes": {"kyc.reasoning": "<thinking>x</thinking>"}}
        assert extract_reasoning([span]) == ("<thinking>x</thinking>",)

    def test_last_decision_span_wins_on_a_rerun(self):
        first = decision_span(outcome="MANUAL_REVIEW", rule="O1.a")
        second = decision_span(outcome="REJECT", rule="O1.b")
        assert extract_decision([first, second]).outcome == "REJECT"

    def test_no_decision_span_yields_none(self):
        assert extract_decision([agent_span()]) is None


class TestCompleteness:
    def test_complete_span_set_reports_no_gaps(self):
        evidence = collect_evidence(complete_spans())
        assert evidence.complete
        assert evidence.missing == ()
        assert evidence.span_count == 3

    def test_missing_tool_spans_are_named(self):
        evidence = collect_evidence([agent_span(), decision_span()])
        assert not evidence.complete
        assert evidence.missing == ("TOOL_CALL",)

    def test_every_required_kind_is_reported_when_nothing_is_supplied(self):
        assert missing_kinds([]) == tuple(k.value for k in REQUIRED_KINDS)

    def test_empty_span_set_returns_an_incomplete_bundle_not_an_empty_one(self):
        # The distinction that matters: a bundle with no tool calls and no complaint reads as
        # "no tools were used", which is a different and false claim.
        evidence = collect_evidence([])
        assert not evidence.complete
        assert evidence.notes and any("No spans supplied" in n for n in evidence.notes)

    def test_reasoning_absence_alone_never_marks_a_record_incomplete(self):
        # Reasoning lives in the short-life trace tier (R18.7) and may have aged out while the
        # seven-year decision record survives. Treating that as a defect would alarm on normal
        # retention expiry.
        assert collect_evidence(complete_spans()).reasoning == ()
        assert collect_evidence(complete_spans()).complete

    def test_non_mapping_entries_are_ignored_not_fatal(self):
        evidence = collect_evidence([*complete_spans(), None, "junk"])
        assert evidence.complete
        assert evidence.span_count == 3


class TestStepFunctionsOutputIsNotAnAuditSource:
    """F-13 / R18.2 — the failure mode this whole module exists to prevent."""

    def test_sfn_output_yields_no_tool_calls(self):
        spans = spans_from_sfn_output(SFN_OUTPUT)
        assert extract_tool_calls(spans) == ()

    def test_evidence_from_sfn_output_alone_is_incomplete(self):
        evidence = collect_evidence(spans_from_sfn_output(SFN_OUTPUT))
        assert not evidence.complete
        assert "TOOL_CALL" in evidence.missing
        assert "DECISION" in evidence.missing

    def test_the_sfn_message_looks_conclusive_which_is_the_hazard(self):
        # The text asserts a screening result while carrying zero evidence of a screening call.
        # Anything that accepts this as a record ships something that looks complete and is not.
        spans = spans_from_sfn_output(SFN_OUTPUT)
        assert spans and "no sanctions hits" in spans[0]["attributes"]["gen_ai.completion"]
        assert collect_evidence(spans).tool_calls == ()
