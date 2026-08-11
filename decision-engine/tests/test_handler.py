"""Lambda boundary tests — task 6.1.

The handler is the only impure module, so its job is narrow: reject anything it cannot read, and
never invent a value. A silently defaulted field would turn "we never checked" into "we checked
and found nothing" — the same failure class as reading an empty agent result as a clean pass.
"""

from __future__ import annotations

import pytest
from decision_handler import InputError, lambda_handler


def p1(**over):
    base = {
        "phase": 1,
        "jurisdictionRisk": "LOW",
        "preScreenMatched": False,
        "documentConfidence": 0.95,
        "velocitySignals": {"critical": False},
        "productApplied": "current_account",
    }
    base.update(over)
    return base


def p2(**over):
    base = {
        "phase": 2,
        "jurisdictionRisk": "LOW",
        "pepStatus": "NONE",
        "adverseMediaSeverity": "NONE",
        "documentConfidence": 0.95,
        "velocitySignals": {"critical": False},
        "productApplied": "current_account",
        "screeningHits": [],
    }
    base.update(over)
    return base


class TestPhase1:
    def test_clean_case(self):
        r = lambda_handler(p1())
        assert r["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert r["tier"] == "LOW"
        assert r["reKycInterval"] == "annual"

    def test_prohibited_rejects(self):
        r = lambda_handler(p1(preScreenMatched=True))
        assert r["outcome"] == "REJECT"
        assert r["reasonCode"] == "SCREENING_CONFIRMED_MATCH"
        assert r["notifyCompliance"] is True
        assert r["reKycInterval"] is None

    def test_audit_fields_are_all_present(self):
        """R4.9 / R18.6 — the record has to be reconstructable without the original event."""
        r = lambda_handler(p1(documentConfidence=0.4))
        for key in ("rule", "tierRule", "tierInputs", "thresholdsInForce", "configVersion",
                    "action", "recordedSignals"):
            assert r[key], f"{key} missing from the decision record"
        assert r["thresholdsInForce"]["document_confidence_threshold"] == 0.8

    def test_empty_agent_result_routes_to_review(self):
        """The observed anomaly, through the real entry point."""
        r = lambda_handler(p1(agentResults={"prescreen": {"status": "SUCCEEDED", "Content": []}}))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert r["reasonCode"] == "EVIDENCE_UNAVAILABLE"


class TestPhase2:
    def test_clean_case(self):
        assert lambda_handler(p2())["outcome"] == "FULL_APPROVE_STANDARD"

    def test_confirmed_hit_rejects(self):
        r = lambda_handler(p2(screeningHits=[
            {"listId": "OFAC-SDN", "strength": "CONFIRMED", "matchedOn": ["name", "dob"]}]))
        assert r["outcome"] == "REJECT"

    def test_unresolved_hit_goes_to_review(self):
        r = lambda_handler(p2(screeningHits=[{"listId": "EU-CFSP", "strength": "POSSIBLE"}]))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert r["tier"] == "HIGH"

    def test_pep_self_goes_to_review(self):
        assert lambda_handler(p2(pepStatus="SELF"))["outcome"] == "MANUAL_REVIEW"


class TestRejectsBadInput:
    def test_missing_phase(self):
        with pytest.raises(InputError, match="phase"):
            lambda_handler({"jurisdictionRisk": "LOW"})

    def test_bad_phase(self):
        with pytest.raises(InputError, match="phase"):
            lambda_handler(p1(phase=3))

    @pytest.mark.parametrize("field", [
        "jurisdictionRisk", "preScreenMatched", "documentConfidence",
        "velocitySignals", "productApplied",
    ])
    def test_every_phase1_field_is_required(self, field):
        """No defaults. A missing field must fail loudly, not become a confident decision."""
        event = p1()
        del event[field]
        with pytest.raises(InputError, match=field):
            lambda_handler(event)

    def test_pre_screen_matched_must_be_a_real_boolean(self):
        """A truthy string like "false" must not be coerced — it would read as a match."""
        with pytest.raises(InputError, match="boolean"):
            lambda_handler(p1(preScreenMatched="false"))

    def test_confidence_out_of_range_is_rejected(self):
        with pytest.raises(InputError, match="0.0..1.0"):
            lambda_handler(p1(documentConfidence=1.4))

    def test_unknown_enum_value_lists_the_valid_ones(self):
        with pytest.raises(InputError, match="PROHIBITED"):
            lambda_handler(p1(jurisdictionRisk="SEVERE"))

    def test_unknown_match_strength_is_rejected(self):
        with pytest.raises(InputError, match="CONFIRMED"):
            lambda_handler(p2(screeningHits=[{"listId": "X", "strength": "MAYBE"}]))

    def test_agent_results_must_be_an_object(self):
        with pytest.raises(InputError, match="agentResults"):
            lambda_handler(p1(agentResults=["prescreen"]))


class TestDeterminismThroughTheHandler:
    def test_1000_identical_events_give_1000_identical_responses(self):
        """AC-13. Serialised output, so a dict-ordering or float-formatting difference counts."""
        import json
        event = p1(documentConfidence=0.81, jurisdictionRisk="HIGH")
        seen = {json.dumps(lambda_handler(dict(event)), sort_keys=True, default=str)
                for _ in range(1000)}
        assert len(seen) == 1


class TestEdgeFactsThroughTheHandler:
    """Task 6.9 reachable from the real entry point, not only from the pure layer."""

    def test_deceased_indicator_rejects(self):
        r = lambda_handler(p1(edgeFacts={"deceasedIndicator": True}))
        assert r["outcome"] == "REJECT"
        assert r["reasonCode"] == "DECEASED_IDENTITY"
        assert r["notifyCompliance"] is True

    def test_under_18_reviews(self):
        r = lambda_handler(p1(edgeFacts={"age": 16}))
        assert r["outcome"] == "MANUAL_REVIEW"

    def test_expired_document_steps_up_and_names_the_item(self):
        r = lambda_handler(p1(edgeFacts={"documentExpired": True}))
        assert r["outcome"] == "STEP_UP"
        assert r["requiredItem"]

    def test_directives_are_surfaced_separately(self):
        r = lambda_handler(p1(edgeFacts={
            "nationalities": ["us", "jp"],
            "jurisdictionRiskByCode": {"us": "LOW", "jp": "HIGH"},
        }))
        assert r["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        directives = r["recordedSignals"]["directives"]
        assert any(d["requirement"] == "R29.2" for d in directives)
        assert r["recordedSignals"]["jurisdiction_selected"] == "jp"

    def test_matched_edge_cases_are_listed(self):
        r = lambda_handler(p1(edgeFacts={"age": 15, "documentExpired": True}))
        assert len(r["edgeCasesMatched"]) == 2

    def test_an_unknown_edge_fact_key_is_rejected(self):
        """A typo that silently did nothing would disable a control while tests stayed green."""
        with pytest.raises(InputError, match="unknown edgeFacts keys"):
            lambda_handler(p1(edgeFacts={"deceasedIndicater": True}))

    def test_no_edge_facts_is_valid(self):
        assert lambda_handler(p1())["edgeCasesMatched"] == []

    def test_bad_jurisdiction_risk_value_is_rejected(self):
        with pytest.raises(InputError, match="jurisdictionRiskByCode"):
            lambda_handler(p1(edgeFacts={
                "nationalities": ["us", "jp"],
                "jurisdictionRiskByCode": {"jp": "SEVERE"},
            }))

    def test_edge_case_rejection_still_carries_the_full_audit_record(self):
        """The regression the pure-layer test caught: an edge-case decision must not lose the
        tier, the thresholds in force, or the other matched rows."""
        r = lambda_handler(p1(edgeFacts={
            "deceasedIndicator": True,
            "nationalities": ["us", "jp"],
            "jurisdictionRiskByCode": {"us": "LOW", "jp": "HIGH"},
        }))
        assert r["outcome"] == "REJECT"
        assert r["tier"] == "LOW"
        assert r["thresholdsInForce"]["document_confidence_threshold"] == 0.8
        assert r["configVersion"]
        assert r["recordedSignals"]["directives"]
