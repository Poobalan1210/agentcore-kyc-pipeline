"""The offline evals, run as tests, so a change that breaks an act on stage fails the build.

Each suite is asserted check by check, and the harness itself is held to two properties: it
cannot leak its fake AWS into the rest of the session, and a wrong expectation is caught rather
than passed — an eval that cannot fail proves nothing.
"""

from __future__ import annotations

import os
import sys

import pytest

import run_evals
from eval_offline import OfflinePipeline
from eval_personas import EXPECTED, PERSONAS


def _assert_all_pass(checks):
    assert checks, "the suite produced no checks"
    failed = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
    assert failed == []


class TestEverySuitePasses:
    def test_personas(self):
        checks = run_evals.persona_outcomes()
        _assert_all_pass(checks)
        # One check per phase each persona goes through, plus the review where there is one.
        expected = sum(1 + bool(e.review) + bool(e.phase2) + bool(e.act3)
                       for e in EXPECTED.values())
        assert len(checks) == expected

    def test_same_outcome(self):
        _assert_all_pass(run_evals.same_outcome(25))

    def test_narrative_cannot_change_a_decision(self):
        _assert_all_pass(run_evals.narrative_independence())

    def test_a_tool_that_could_not_run_never_approves(self):
        _assert_all_pass(run_evals.unavailable_never_approves())

    def test_a_mismatch_never_approves(self):
        _assert_all_pass(run_evals.mismatches_never_approve())


class TestTheEvalsCanFail:
    def test_a_wrong_expectation_is_reported(self, monkeypatch):
        """Nadia's Act 3 outcome is FREEZE. Expecting anything else must fail."""
        wrong = EXPECTED["DEMO-US-03"].__class__(
            **{**EXPECTED["DEMO-US-03"].__dict__, "act3": "RISK_UNCHANGED"})
        monkeypatch.setitem(EXPECTED, "DEMO-US-03", wrong)
        failed = [c for c in run_evals.persona_outcomes() if not c.passed]
        assert [c.name for c in failed] == ["DEMO-US-03 after the Act 3 list update"]
        assert "got FREEZE" in failed[0].detail

    def test_the_runner_exits_non_zero_on_a_failure(self, monkeypatch, capsys):
        monkeypatch.setattr(run_evals, "SUITES", {
            "personas": lambda: [run_evals.Check("personas", "forced", False, "forced")]})
        assert run_evals.main(["--suite", "personas"]) == 1
        assert "FAIL" in capsys.readouterr().out


class TestTheHarnessLeavesNoTrace:
    def test_boto3_and_the_environment_are_restored(self):
        before_boto3 = sys.modules.get("boto3")
        before_env = os.environ.get("CASE_TABLE")
        with OfflinePipeline() as pipe:
            assert sys.modules["boto3"] is pipe.boto3
            assert os.environ["CASE_TABLE"] == "kyc90-cases"
        assert sys.modules.get("boto3") is before_boto3
        assert os.environ.get("CASE_TABLE") == before_env

    def test_the_facts_are_recorded_by_the_tools_not_the_harness(self):
        """Every fact the rules read was written by a tool's own `record_fact`."""
        with OfflinePipeline() as pipe:
            applicant = pipe.submit(PERSONAS["DEMO-US-04"])
            pipe.phase1(applicant)
            screening = pipe.fact(applicant, "screening")
            document = pipe.fact(applicant, "documentExtraction")
        assert screening["performed"] is True and screening["strongest"] == "NO_MATCH"
        assert [e["entryId"] for e in screening["excluded"]] == ["SYN-0001"]
        # The MRZ was parsed and verified by the tool, from the lines the document prints.
        assert document["mrzPresent"] is True
        assert document["mrz"]["checkDigitsValid"] is True

    def test_each_engine_event_has_exactly_the_keys_the_state_machine_sends(self):
        """Compared with the deployed definitions themselves (`asl.py`'s `PreliminaryTier`,
        `FullTier` and `Reassess` payloads). A key the harness invented, or one it dropped, would
        make these evals test a different engine call from the one Step Functions makes."""
        from kyc import asl

        arns = {k: f"arn:aws:bedrock-agentcore:us-east-1:000000000000:harness/kyc90_{k}"
                for k in ("briefing", "documents", "identity", "fraud", "prescreen",
                          "compliance", "notify")}
        common = dict(
            case_table="kyc90-cases", harness_arns=arns,
            model_ids={k: "model" for k in arns},
            decision_engine_arn="arn:aws:lambda:us-east-1:000000000000:function:engine",
            audit_writer_arn="arn:aws:lambda:us-east-1:000000000000:function:audit",
            review_queue_url="https://sqs.us-east-1.amazonaws.com/000000000000/review",
            account_function_arn="arn:aws:lambda:us-east-1:000000000000:function:account",
            audit_pins={"endpointName": "live", "harnessVersion": "1",
                        "policyEngineVersion": "pe#LOG_ONLY", "gatewayArn": "gw"})
        deployed = {
            1: asl.phase1_definition(**common, phase2_state_machine_arn="arn:sm/phase2",
                                     controls_parameter="/kyc90/demo/faults")
            ["States"]["PreliminaryTier"],
            2: asl.phase2_definition(**common)["States"]["FullTier"],
            3: asl.phase3_definition(**common, history_table="kyc90-decision-history")
            ["States"]["Reassess"],
        }
        with OfflinePipeline() as pipe:
            applicant = pipe.submit(PERSONAS["DEMO-US-03"])
            events = {1: pipe.phase1(applicant).event, 2: pipe.phase2(applicant).event}
            pipe.publish_list_update([{"entryId": "SYN-0901", "name": "Nadia Rose Korval",
                                       "dob": "1987-02-11", "country": "US"}])
            events[3] = pipe.phase3(applicant).event
        for phase, state in deployed.items():
            payload = state["Arguments"]["Payload"]
            assert set(events[phase]) == set(payload), f"phase {phase}"
            assert set(events[phase]["facts"]) == set(payload["facts"]), f"phase {phase}"
