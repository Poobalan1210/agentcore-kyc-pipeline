"""Signal derivation from agent replies — the fix for the branch-scope bug.

The property under test throughout is the fail-closed one: an unreadable or ambiguous reply must
yield `None`, never a safe-looking default. `False` for `pre_screen_matched` would approve on a
guess and `True` would reject on one, and both carry the same unearned confidence.
"""

from __future__ import annotations

import pytest
from decision_handler import lambda_handler
from signals import (
    agent_text,
    derive,
    document_confidence,
    embedded_json,
    pre_screen_matched,
    velocity_critical,
)


def reply(text: str) -> dict:
    """An agent result in the shape the Parallel branch emits."""
    return {"agent": "x", "status": "OK", "content": [{"Text": text}]}


class TestTextExtraction:
    def test_reads_the_branch_content_shape(self):
        assert agent_text(reply("Hello")) == "hello"

    def test_handles_multiple_blocks(self):
        result = {"content": [{"Text": "one"}, {"Text": "two"}]}
        assert agent_text(result) == "one two"

    @pytest.mark.parametrize("value", [None, {}, {"content": []}, {"content": [{}]}])
    def test_empty_shapes_yield_no_text(self, value):
        assert agent_text(value) == ""

    def test_finds_nested_json_inside_prose(self):
        text = '<thinking>working</thinking> {"a": {"b": 1}} trailing'
        assert embedded_json(text) == [{"a": {"b": 1}}]

    def test_ignores_unparseable_braces(self):
        assert embedded_json("{not json} {\"ok\": 1}") == [{"ok": 1}]


class TestDocumentConfidence:
    def test_reads_json_confidence(self):
        assert document_confidence(reply('{"confidence": 0.96}')) == 0.96

    def test_reads_nested_json_confidence(self):
        assert document_confidence(reply('{"fields": {"confidence": 0.81}}')) == 0.81

    def test_reads_a_labelled_number_in_prose(self):
        assert document_confidence(reply("overall confidence: 0.72 for the document")) == 0.72

    def test_normalises_a_percentage(self):
        """96 and 0.96 are the same claim; 96.0 is not a valid confidence."""
        assert document_confidence(reply('{"confidence": 96}')) == 0.96

    def test_a_stray_decimal_is_not_mistaken_for_confidence(self):
        """The pattern is anchored on the word, so an unrelated number cannot be adopted."""
        assert document_confidence(reply("the document expires in 2032, 4.5 years away")) is None

    def test_out_of_range_is_rejected(self):
        assert document_confidence(reply('{"confidence": -1}')) is None

    def test_unreadable_reply_is_none(self):
        assert document_confidence(reply("I could not extract any fields.")) is None

    def test_empty_reply_is_none(self):
        assert document_confidence(None) is None


class TestPreScreenMatched:
    def test_reads_json_false(self):
        assert pre_screen_matched(reply('{"matched": false, "hitCount": 0}')) is False

    def test_reads_json_true(self):
        assert pre_screen_matched(reply('{"matched": true}')) is True

    def test_reads_hit_count(self):
        assert pre_screen_matched(reply('{"hitCount": 2}')) is True

    def test_reads_the_real_observed_prose(self):
        """Text the live pre-screen agent actually produced."""
        assert pre_screen_matched(reply(
            "The exact-match sanctions tool did not find a match for the applicant's name, "
            "date of birth, and country. The tool reported no hits.")) is False

    def test_a_positive_hit_in_prose(self):
        assert pre_screen_matched(reply("A confirmed match was found on the OFAC list.")) is True

    def test_ambiguous_text_is_none_not_false(self):
        """"no match on name but a possible match on date of birth" supports both readings, and
        guessing either way carries confidence the evidence does not."""
        assert pre_screen_matched(reply(
            "no match on the name, though there is a possible match on date of birth")) is None

    def test_silence_is_none(self):
        assert pre_screen_matched(reply("I have completed the screening step.")) is None

    def test_empty_reply_is_none_never_false(self):
        """The single most important assertion here: an empty screening reply must not read as
        clean, because that approves exactly the applicant who should be stopped."""
        assert pre_screen_matched({"content": []}) is None


class TestVelocityCritical:
    def test_reads_json_false(self):
        assert velocity_critical(reply('{"critical": false}')) is False

    def test_reads_json_true(self):
        assert velocity_critical(reply('{"critical": true}')) is True

    def test_reads_prose_clean(self):
        assert velocity_critical(reply("velocity is within threshold")) is False

    def test_reads_prose_critical(self):
        assert velocity_critical(reply("abnormal velocity detected")) is True

    def test_genuinely_contradictory_text_is_none(self):
        """Both readings asserted at once. The earlier version of this test used "critical checks
        were within threshold", which is not ambiguous at all — it only looked that way because a
        bare "critical" pattern was matching the word inside a clean statement."""
        assert velocity_critical(reply(
            "velocity breach on the device, though the IP is within threshold")) is None

    def test_a_clean_statement_containing_the_word_critical_is_clean(self):
        assert velocity_critical(reply("critical checks were within threshold")) is False

    def test_silence_is_none(self):
        assert velocity_critical(reply("Assessment complete.")) is None


class TestDerive:
    def test_a_complete_set_derives_every_signal(self):
        signals, undetermined = derive({
            "documents": reply('{"confidence": 0.95}'),
            "prescreen": reply('{"matched": false, "hitCount": 0}'),
            "fraud": reply('{"critical": false}'),
        })
        assert undetermined == ()
        assert signals["documentConfidence"] == 0.95
        assert signals["preScreenMatched"] is False
        assert signals["velocitySignals"] == {"critical": False}

    def test_each_unreadable_agent_is_named(self):
        signals, undetermined = derive({
            "documents": reply("no fields available"),
            "prescreen": reply('{"matched": false}'),
            "fraud": reply("done"),
        })
        assert set(undetermined) == {"documents", "fraud"}
        assert "documentConfidence" not in signals
        assert signals["preScreenMatched"] is False

    def test_nothing_is_defaulted_when_everything_is_unreadable(self):
        signals, undetermined = derive({"documents": {}, "prescreen": {}, "fraud": {}})
        assert signals == {}
        assert set(undetermined) == {"documents", "prescreen", "fraud"}


class TestThroughTheHandler:
    """End to end: the shape the state machine now sends."""

    def base(self, **over):
        event = {
            "phase": 1,
            "jurisdictionRisk": "LOW",
            "productApplied": "current_account",
            "agentResults": {
                "documents": reply('{"confidence": 0.95}'),
                "prescreen": reply('{"matched": false, "hitCount": 0}'),
                "fraud": reply('{"critical": false}'),
                "identity": reply("identity verified"),
            },
        }
        event.update(over)
        return event

    def test_a_clean_case_approves_without_explicit_signals(self):
        """The bug this fixes: these three fields used to arrive undefined and the engine rejected
        the whole payload, which failed closed to DegradedPath and looked like a real failure."""
        result = lambda_handler(self.base())
        assert result["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert result["tier"] == "LOW"

    def test_an_explicit_field_is_not_overridden_by_derivation(self):
        result = lambda_handler(self.base(documentConfidence=0.5))
        assert result["tier"] == "HIGH"
        assert result["tierRule"] == "P1.document_confidence_below_threshold"

    def test_a_derived_sanctions_hit_rejects(self):
        event = self.base()
        event["agentResults"]["prescreen"] = reply('{"matched": true, "hitCount": 1}')
        result = lambda_handler(event)
        assert result["outcome"] == "REJECT"
        assert result["reasonCode"] == "SCREENING_CONFIRMED_MATCH"

    def test_an_unreadable_screening_reply_goes_to_review_not_to_approval(self):
        event = self.base()
        event["agentResults"]["prescreen"] = reply("Screening step complete.")
        result = lambda_handler(event)
        assert result["outcome"] == "MANUAL_REVIEW"
        assert result["reasonCode"] == "EVIDENCE_UNAVAILABLE"

    def test_derived_low_confidence_reaches_review(self):
        event = self.base()
        event["agentResults"]["documents"] = reply('{"confidence": 0.3}')
        result = lambda_handler(event)
        assert result["outcome"] == "MANUAL_REVIEW"

    def test_a_degraded_branch_still_reaches_review(self):
        event = self.base()
        event["agentResults"]["fraud"] = {
            "agent": "fraud", "status": "DEGRADED", "reason": "EMPTY_OR_FAILED_AGENT_RESULT"}
        result = lambda_handler(event)
        assert result["outcome"] == "MANUAL_REVIEW"


class TestDerivationIsAFallbackNotALicence:
    """The strict contract survives: derivation only helps when agents actually replied."""

    def test_no_agent_results_still_requires_every_field(self):
        import pytest as _pytest
        from decision_handler import InputError
        with _pytest.raises(InputError, match="velocitySignals"):
            lambda_handler({
                "phase": 1, "jurisdictionRisk": "LOW", "productApplied": "current_account",
                "preScreenMatched": False, "documentConfidence": 0.9,
            })

    def test_placeholders_never_decide_the_outcome(self):
        """When a signal is undetermined the agent becomes UNAVAILABLE, and the unavailable path
        governs — so the placeholder values are inert by construction."""
        result = lambda_handler({
            "phase": 1, "jurisdictionRisk": "LOW", "productApplied": "current_account",
            "agentResults": {
                "documents": {"content": [{"Text": "no fields"}]},
                "prescreen": {"content": [{"Text": "step complete"}]},
                "fraud": {"content": [{"Text": "done"}]},
            },
        })
        assert result["outcome"] == "MANUAL_REVIEW"
        assert result["reasonCode"] == "EVIDENCE_UNAVAILABLE"

    def test_an_unreadable_prescreen_never_rejects_on_a_guess(self):
        """`preScreenMatched` placeholders are False deliberately: PROHIBITED is evaluated before
        the unavailable check, so a True placeholder would reject a real applicant because we
        could not parse a model's prose."""
        result = lambda_handler({
            "phase": 1, "jurisdictionRisk": "LOW", "productApplied": "current_account",
            "agentResults": {"prescreen": {"content": [{"Text": "screening finished"}]}},
        })
        assert result["outcome"] != "REJECT"
        assert result["outcome"] == "MANUAL_REVIEW"


class TestRealAgentRepliesFromLiveRuns:
    """Verbatim replies captured from live executions. These are the regression suite that matters:
    every one of them defeated an earlier version of the patterns.
    """

    FRAUD_CLEAN = (
        "<thinking> The velocity check has been completed and there are no elevated or critical "
        "signals for device or IP application counts. Next, I will check for geographic "
        "inconsistency by comparing the document country, address country, and IP country.</thinking>"
    )

    PRESCREEN_CLEAN = (
        "<thinking> The SanctionsExact___sanctions_exact_match tool has returned the result of the "
        "exact-match check. The applicant's name did not match any entry in the sanctions list. "
        "The tool reported no hits and a status of \"OK\".</thinking> The exact-match sanctions "
        "tool returned no hits for the applicant's name, date of birth, and country."
    )

    DOCUMENTS_OK = (
        "<thinking>I have successfully retrieved the document details.</thinking> "
        '{ "full_name": { "value": "ALEX SYNTHETIC", "confidence": 0.96 }, '
        '"date_of_birth": { "value": "1990-04-12", "confidence": 0.96 } }'
    )

    def test_no_elevated_or_critical_is_not_a_critical_finding(self):
        """The bug this class exists for: a bare "critical" pattern matched the word inside its own
        negation and sent a clean applicant to manual review."""
        assert velocity_critical(reply(self.FRAUD_CLEAN)) is False

    def test_the_real_prescreen_reply_reads_as_clean(self):
        assert pre_screen_matched(reply(self.PRESCREEN_CLEAN)) is False

    def test_the_real_documents_reply_yields_its_confidence(self):
        assert document_confidence(reply(self.DOCUMENTS_OK)) == 0.96

    def test_the_full_live_reply_set_approves(self):
        """The end state: the exact prose four live agents produced now yields a clean
        provisional approval instead of an unexplained manual review."""
        result = lambda_handler({
            "phase": 1,
            "jurisdictionRisk": "LOW",
            "productApplied": "current_account",
            "agentResults": {
                "documents": reply(self.DOCUMENTS_OK),
                "prescreen": reply(self.PRESCREEN_CLEAN),
                "fraud": reply(self.FRAUD_CLEAN),
                "identity": reply("identity verified"),
            },
        })
        assert result["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert result["tier"] == "LOW"
        assert result["recordedSignals"]["tier_inputs"]["velocity_critical"] is False
        assert result["recordedSignals"]["tier_inputs"]["document_confidence"] == 0.96
        assert result["recordedSignals"]["tier_inputs"]["pre_screen_matched"] is False

    def test_a_genuine_critical_finding_is_still_caught(self):
        """The fix must not have made the pattern blind in the dangerous direction."""
        assert velocity_critical(reply(
            "Velocity breach: 6 applications from this device in one hour.")) is True
        assert velocity_critical(reply("elevated velocity on the IP address")) is True


class TestStructuredFindingsAreAuthoritative:
    """R11.13 / F-89 — the deterministic path, and the one prose was standing in for.

    Findings arrive DynamoDB-shaped, exactly as `ReloadFindings` reads them back from the case
    record: `{"documents": {"S": "{...json...}"}}`.
    """

    def finding(self, **payload):
        import json as _json
        return {"S": _json.dumps(payload)}

    def base(self, **over):
        event = {
            "phase": 1,
            "jurisdictionRisk": "LOW",
            "productApplied": "current_account",
            "findings": {
                "documents": self.finding(confidence=0.96, expired=False,
                                          screenPhotograph=False, unsupportedIssuer=False),
                "prescreen": self.finding(determined=True, matched=False, hitCount=0),
                "fraud": self.finding(determined=True, velocityCritical=False),
            },
        }
        event.update(over)
        return event

    def test_structured_findings_produce_a_clean_approval(self):
        result = lambda_handler(self.base())
        assert result["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert result["tier"] == "LOW"
        inputs = result["recordedSignals"]["tier_inputs"]
        assert inputs["document_confidence"] == 0.96
        assert inputs["pre_screen_matched"] is False
        assert inputs["velocity_critical"] is False

    def test_the_same_findings_always_give_the_same_outcome(self):
        """R3.5 becomes satisfiable once the shape stops being the model's choice."""
        import json as _json
        seen = {
            _json.dumps(lambda_handler(self.base()), sort_keys=True, default=str)
            for _ in range(50)
        }
        assert len(seen) == 1

    def test_a_screening_hit_rejects(self):
        event = self.base()
        event["findings"]["prescreen"] = self.finding(
            determined=True, matched=True, hitCount=1, strongestMatch="CONFIRMED")
        result = lambda_handler(event)
        assert result["outcome"] == "REJECT"
        assert result["reasonCode"] == "SCREENING_CONFIRMED_MATCH"

    def test_determined_false_is_unavailable_not_clean(self):
        """The field exists so an agent that could not screen has something honest to say. Without
        it, `matched=false` is the only option and it reads as "no sanctions hits"."""
        event = self.base()
        event["findings"]["prescreen"] = self.finding(
            determined=False, matched=False, hitCount=0)
        result = lambda_handler(event)
        assert result["outcome"] == "MANUAL_REVIEW"
        assert result["reasonCode"] == "EVIDENCE_UNAVAILABLE"

    def test_low_confidence_reaches_review(self):
        event = self.base()
        event["findings"]["documents"] = self.finding(
            confidence=0.3, expired=False, screenPhotograph=False, unsupportedIssuer=False)
        result = lambda_handler(event)
        assert result["outcome"] == "MANUAL_REVIEW"

    def test_critical_velocity_reaches_review(self):
        event = self.base()
        event["findings"]["fraud"] = self.finding(
            determined=True, velocityCritical=True,
            velocityDetail=["6 applications in 1h"])
        result = lambda_handler(event)
        assert result["outcome"] == "MANUAL_REVIEW"
        assert result["recordedSignals"]["tier_inputs"]["velocity_critical"] is True

    def test_an_unparseable_finding_is_unavailable(self):
        event = self.base()
        event["findings"]["fraud"] = {"S": "{not json"}
        result = lambda_handler(event)
        assert result["outcome"] == "MANUAL_REVIEW"

    def test_a_missing_finding_is_unavailable_never_defaulted(self):
        event = self.base()
        del event["findings"]["prescreen"]
        result = lambda_handler(event)
        assert result["outcome"] == "MANUAL_REVIEW"
        assert result["reasonCode"] == "EVIDENCE_UNAVAILABLE"

    def test_findings_win_over_contradicting_prose(self):
        """A contract finding is schema-validated; the assistant text is not. When they disagree
        the validated one governs, or the contract would be decorative."""
        event = self.base()
        event["agentResults"] = {
            "prescreen": {"content": [{"Text": "A confirmed match was found."}]}}
        result = lambda_handler(event)
        assert result["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"

    def test_prose_still_fills_a_gap_the_contract_left(self):
        """Fallback, not primary. An agent that recorded nothing can still be read from its text
        rather than immediately costing the applicant a manual review."""
        event = self.base()
        del event["findings"]["fraud"]
        event["agentResults"] = {
            "fraud": {"content": [{"Text": "velocity is within threshold"}]}}
        result = lambda_handler(event)
        assert result["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert result["recordedSignals"]["tier_inputs"]["velocity_critical"] is False

    def test_findings_must_be_an_object(self):
        import pytest as _pytest
        from decision_handler import InputError
        with _pytest.raises(InputError, match="findings"):
            lambda_handler(self.base(findings=["documents"]))
