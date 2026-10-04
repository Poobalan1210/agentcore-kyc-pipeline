"""Deciding from recorded facts — the engine half of "code decides on facts".

Every test here goes through the real entry point with `facts` as the state machine sends them.
The properties that carry the weight:

  * a missing, stale or not-performed mandatory fact reaches a human, never a default
  * only a CONFIRMED screening match prohibits; PROBABLE and POSSIBLE go to review
  * a face mismatch, an expired passport, a minor and an unsupported issuer are acted on
  * nothing an agent *said* changes an outcome, in either direction
"""

from __future__ import annotations

import json

import pytest
from decision_handler import InputError, lambda_handler
from facts import age_on, epoch_seconds, parse_date

AS_OF = "2026-12-02T18:04:05.123Z"
STARTED = "2026-12-02T18:03:40.000Z"
RECORDED = "2026-12-02T18:03:55Z"          # inside the phase

DECLARED = {"givenNames": "TAYLOR JORDAN", "surname": "REEVES", "dateOfBirth": "1991-05-14",
            "nationality": "USA", "address": "482 Kestrel Way"}


def mrz(**over):
    base = {"surname": "REEVES", "givenNames": "TAYLOR JORDAN", "dateOfBirth": "1991-05-14",
            "nationality": "USA", "issuingState": "USA", "documentNumber": "471029305",
            "expiryDate": "2032-05-13", "checkDigitsValid": True, "failedCheckDigits": []}
    base.update(over)
    return base


def document(**over):
    base = {"performed": True, "documentReadable": True, "mrzPresent": True, "mrz": mrz(),
            "recordedAt": RECORDED}
    base.update(over)
    return base


def screening(strongest="NO_MATCH", hits=(), **over):
    base = {"performed": True, "listVersion": "SYNTHETIC-2026-10-01", "strongest": strongest,
            "hits": list(hits), "excluded": [], "recordedAt": RECORDED}
    base.update(over)
    return base


def hit(entry, classification, **over):
    base = {"entryId": entry, "classification": classification, "score": 0.5,
            "dobMatch": classification == "CONFIRMED", "countryMatch": False,
            "reason": "test"}
    base.update(over)
    return base


def face(**over):
    base = {"performed": True, "faceDetected": True, "match": True, "similarityBand": "HIGH",
            "recordedAt": RECORDED}
    base.update(over)
    return base


def velocity(**over):
    base = {"performed": False, "status": "NOT_ESTABLISHED",
            "reasonCode": "NO_DEVICE_OR_IP_CAPTURED", "recordedAt": RECORDED}
    base.update(over)
    return base


def facts(**over):
    base = {"documentExtraction": document(), "screening": screening(), "faceMatch": face(),
            "velocity": velocity()}
    base.update(over)
    return base


def p1(**over):
    base = {"phase": 1, "asOf": AS_OF, "phaseStartedAt": STARTED,
            "productApplied": "current_account", "declaredIdentity": DECLARED,
            "facts": facts()}
    base.update(over)
    return base


def p1_with(**fact_over):
    return p1(facts=facts(**fact_over))


class TestTheCleanCase:
    def test_opens_restricted_on_verified_facts(self):
        r = lambda_handler(p1())
        assert r["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert r["tier"] == "LOW"
        assert r["tierInputs"]["document_confidence"] == 1.0
        assert r["identityMatch"]["identityConfirmed"] is True

    def test_the_record_says_which_facts_were_read(self):
        r = lambda_handler(p1())
        read = r["factsRead"]["facts"]
        for name in ("documentExtraction", "screening", "faceMatch", "velocity"):
            assert read[name]["recordedAt"] == RECORDED
        assert read["screening"]["status"] == "OK"
        assert r["factsRead"]["documentConfidenceSource"] == "MRZ_CHECK_DIGITS"

    def test_velocity_not_established_is_recorded_and_does_not_block(self):
        """No device or IP is captured in this build. That is disclosed, not hidden."""
        r = lambda_handler(p1())
        assert r["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert "not established" in r["tierInputs"]["velocity_detail"][0]

    def test_facts_arrive_as_stored_strings_too(self):
        """The state machine passes the attribute straight off the case record."""
        stored = {k: {"S": json.dumps(v)} for k, v in facts().items()}
        assert lambda_handler(p1(facts=stored))["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"

    def test_1000_identical_fact_sets_give_1000_identical_decisions(self):
        seen = {json.dumps(lambda_handler(p1()), sort_keys=True, default=str)
                for _ in range(1000)}
        assert len(seen) == 1


class TestScreening:
    def test_a_possible_alert_goes_to_review_not_rejection(self):
        """The Presenter B persona: a listing with no date of birth. The old rule rejected this
        applicant as SCREENING_CONFIRMED_MATCH."""
        r = lambda_handler(p1_with(screening=screening(
            "POSSIBLE", [hit("SYN-0106", "POSSIBLE")])))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert r["tierRule"] == "P1.screening_hit_unresolved"
        assert r["reasonCode"] != "SCREENING_CONFIRMED_MATCH"

    def test_a_probable_alert_goes_to_review(self):
        r = lambda_handler(p1_with(screening=screening(
            "PROBABLE", [hit("SYN-0104", "PROBABLE")])))
        assert r["outcome"] == "MANUAL_REVIEW"

    def test_a_confirmed_alert_rejects_and_notifies_compliance(self):
        r = lambda_handler(p1_with(screening=screening(
            "CONFIRMED", [hit("SYN-0001", "CONFIRMED")])))
        assert r["outcome"] == "REJECT"
        assert r["reasonCode"] == "SCREENING_CONFIRMED_MATCH"
        assert r["notifyCompliance"] is True

    def test_exclusions_do_not_alert(self):
        """The Act 2 namesake: excluded on a date-of-birth conflict, cleared with no human."""
        r = lambda_handler(p1_with(screening=screening(excluded=[
            {"entryId": "SYN-0001", "reason": "dates of birth differ"}])))
        assert r["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert r["factsRead"]["screening"]["excluded"] == ["SYN-0001"]

    @pytest.mark.parametrize("fact", [
        None,
        {"performed": False, "status": "UNAVAILABLE", "reasonCode": "FORCED_UNAVAILABLE",
         "recordedAt": RECORDED},
    ], ids=["absent", "unavailable"])
    def test_screening_that_did_not_run_goes_to_a_human(self, fact):
        """Failure demo 1. Whatever the agent said about it, the record says it did not run."""
        r = lambda_handler(p1_with(screening=fact))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert r["reasonCode"] == "EVIDENCE_UNAVAILABLE"
        assert "prescreen" in r["recordedSignals"]["unavailable_agents"]

    def test_a_stale_screening_fact_is_ignored(self):
        """Recorded by an earlier run of the same case. It is not evidence about this run."""
        r = lambda_handler(p1_with(screening=screening(recordedAt="2026-11-30T09:00:00Z")))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert r["factsRead"]["facts"]["screening"]["status"] == "STALE"

    def test_a_fact_just_inside_the_skew_tolerance_is_fresh(self):
        r = lambda_handler(p1_with(screening=screening(recordedAt="2026-12-02T18:03:36Z")))
        assert r["factsRead"]["facts"]["screening"]["status"] == "OK"


class TestAgentWordsDoNotDecide:
    """The rules read what the tools recorded. An agent's reply cannot move an outcome either way."""

    def test_an_agent_calling_an_alert_no_match_does_not_approve(self):
        said = {"prescreen": {"status": "OK", "content": [
            {"Text": '{"strongest": "NO_MATCH", "notes": "no match was found"}'}]}}
        r = lambda_handler(p1(
            facts=facts(screening=screening("POSSIBLE", [hit("SYN-0106", "POSSIBLE")])),
            agentResults=said))
        assert r["outcome"] == "MANUAL_REVIEW"

    def test_an_agent_claiming_a_confirmed_hit_does_not_reject(self):
        said = {"prescreen": {"status": "OK", "content": [
            {"Text": '{"strongest": "CONFIRMED", "matched": true}'}]}}
        r = lambda_handler(p1(agentResults=said))
        assert r["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"

    def test_an_empty_agent_result_still_routes_to_review(self):
        """Availability is still read from agent results: an agent that returned nothing is a
        failed run, whatever its tool recorded."""
        r = lambda_handler(p1(agentResults={"fraud": {"status": "OK", "content": []}}))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert "fraud" in r["recordedSignals"]["unavailable_agents"]


class TestFaceMatch:
    def test_a_face_mismatch_goes_to_review(self):
        """The strongest identity control used to be read by nothing."""
        r = lambda_handler(p1_with(faceMatch=face(match=False, similarityBand="LOW")))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert r["reasonCode"] == "FACE_MISMATCH"

    def test_no_face_on_the_document_asks_for_a_better_photo(self):
        r = lambda_handler(p1_with(faceMatch=face(faceDetected=False, match=False)))
        assert r["outcome"] == "STEP_UP"
        assert "photo page" in r["requiredItem"]

    def test_an_unreadable_image_asks_for_a_better_photo(self):
        r = lambda_handler(p1_with(faceMatch={
            "performed": False, "status": "INVALID", "reasonCode": "IMAGE_UNREADABLE",
            "recordedAt": RECORDED}))
        assert r["outcome"] == "STEP_UP"
        assert r["reasonCode"] == "FACE_IMAGE_UNREADABLE"

    def test_no_reference_frame_goes_to_a_human(self):
        """Ours to fix: the applicant cannot supply a frame we failed to capture."""
        r = lambda_handler(p1_with(faceMatch={
            "performed": False, "status": "UNAVAILABLE", "reasonCode": "NO_REFERENCE_FRAME",
            "recordedAt": RECORDED}))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert "identity" in r["recordedSignals"]["unavailable_agents"]

    def test_a_mismatch_outranks_an_expired_document(self):
        """Severity order: no new passport resolves a face that does not match."""
        r = lambda_handler(p1_with(
            faceMatch=face(match=False),
            documentExtraction=document(mrz=mrz(expiryDate="2025-01-01"))))
        assert r["outcome"] == "MANUAL_REVIEW"


class TestPersonAndDocumentRules:
    """The fifteen edge-case rules finally have facts to read."""

    def test_an_expired_passport_asks_for_a_current_one(self):
        r = lambda_handler(p1_with(documentExtraction=document(mrz=mrz(expiryDate="2026-11-30"))))
        assert r["outcome"] == "STEP_UP"
        assert "unexpired" in r["requiredItem"]

    def test_a_passport_expiring_today_has_not_expired(self):
        r = lambda_handler(p1_with(documentExtraction=document(mrz=mrz(expiryDate="2026-12-02"))))
        assert r["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"

    def test_a_minor_goes_to_review_with_age_from_the_passport(self):
        declared = dict(DECLARED, dateOfBirth="2009-01-01")
        r = lambda_handler(p1(
            declaredIdentity=declared,
            facts=facts(documentExtraction=document(mrz=mrz(dateOfBirth="2009-01-01")))))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert any("R29.1" in e for e in r["edgeCasesMatched"])

    def test_an_unsupported_issuer_goes_to_review_not_rejection(self):
        r = lambda_handler(p1_with(documentExtraction=document(
            mrz=mrz(issuingState="CAN", nationality="CAN"))))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert any("R28.2" in e for e in r["edgeCasesMatched"])

    def test_failed_check_digits_zero_the_document_confidence(self):
        r = lambda_handler(p1_with(documentExtraction=document(
            mrz=mrz(checkDigitsValid=False, failedCheckDigits=["dateOfBirth"]))))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert r["tierInputs"]["document_confidence"] == 0.0

    def test_unverified_zone_fields_drive_no_person_rule(self):
        """A zone that fails its check digits is not a source for age or expiry."""
        r = lambda_handler(p1_with(documentExtraction=document(
            mrz=mrz(checkDigitsValid=False, expiryDate="2020-01-01"))))
        assert not any("R28.1" in e for e in r["edgeCasesMatched"])

    def test_a_declared_mismatch_still_goes_to_review(self):
        r = lambda_handler(p1(declaredIdentity=dict(DECLARED, surname="RIVERS")))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert r["reasonCode"] == "DECLARED_DOCUMENT_MISMATCH"

    def test_three_liveness_failures_route_to_a_human(self):
        r = lambda_handler(p1(livenessFailures=3))
        assert r["outcome"] == "MANUAL_REVIEW"

    def test_as_of_is_required(self):
        event = p1()
        del event["asOf"]
        with pytest.raises(InputError, match="asOf"):
            lambda_handler(event)


class TestCountryRisk:
    def test_a_high_risk_nationality_raises_the_tier(self):
        r = lambda_handler(p1(
            declaredIdentity=dict(DECLARED, nationality="IRN"),
            facts=facts(documentExtraction=document(
                mrz=mrz(nationality="IRN", issuingState="USA")))))
        assert r["tier"] == "MEDIUM"
        assert r["factsRead"]["jurisdictionRisk"]["source"] == "MRZ"

    def test_an_explicit_jurisdiction_risk_wins(self):
        assert lambda_handler(p1(jurisdictionRisk="HIGH"))["tier"] == "MEDIUM"


class TestVelocity:
    def test_a_missing_velocity_fact_goes_to_review(self):
        """The Fraud agent did not call its tool. Not established is recorded by the tool; absent
        means the tool never ran."""
        r = lambda_handler(p1_with(velocity=None))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert "fraud" in r["recordedSignals"]["unavailable_agents"]

    def test_critical_velocity_goes_to_review(self):
        r = lambda_handler(p1_with(velocity={
            "performed": True, "deviceApplications": 6, "ipApplications": 1,
            "signals": {"criticalDeviceReuse": True}, "recordedAt": RECORDED}))
        assert r["tierRule"] == "P1.velocity_critical"
        assert r["outcome"] == "MANUAL_REVIEW"


def p2(**over):
    base = {"phase": 2, "phaseStartedAt": STARTED, "productApplied": "current_account",
            "declaredIdentity": DECLARED,
            "facts": facts(pep={"performed": True, "pepStatus": "NONE", "recordedAt": RECORDED},
                           adverseMedia={"performed": True, "maxSeverity": "NONE",
                                         "recordedAt": RECORDED})}
    base.update(over)
    return base


class TestPhase2:
    def test_clean_screening_fully_approves(self):
        assert lambda_handler(p2())["outcome"] == "FULL_APPROVE_STANDARD"

    def test_a_pep_goes_to_review(self):
        event = p2()
        event["facts"]["pep"] = {"performed": True, "pepStatus": "SELF", "recordedAt": RECORDED}
        assert lambda_handler(event)["outcome"] == "MANUAL_REVIEW"

    def test_missing_pep_screening_never_approves(self):
        event = p2()
        event["facts"]["pep"] = None
        r = lambda_handler(event)
        assert r["outcome"] == "MANUAL_REVIEW"
        assert "compliance" in r["recordedSignals"]["unavailable_agents"]

    def test_phase_one_facts_are_read_as_recorded_not_rechecked_for_freshness(self):
        event = p2()
        event["facts"]["screening"] = screening(recordedAt="2026-12-02T18:00:00Z")
        assert lambda_handler(event)["outcome"] == "FULL_APPROVE_STANDARD"

    def test_a_reviewer_cleared_alert_does_not_come_back(self):
        """Act 2: the reviewer cleared the POSSIBLE alert in Phase 1. Phase 2 must not send the same
        customer round again for the same alert."""
        event = p2(screeningDispositions=[{"entryId": "SYN-0106"}])
        event["facts"]["screening"] = screening("POSSIBLE", [hit("SYN-0106", "POSSIBLE")])
        r = lambda_handler(event)
        assert r["outcome"] == "FULL_APPROVE_STANDARD"
        assert r["factsRead"]["screening"]["clearedByReviewer"] == ["SYN-0106"]

    def test_a_disposition_can_never_clear_a_confirmed_match(self):
        event = p2(screeningDispositions=[{"entryId": "SYN-0001"}])
        event["facts"]["screening"] = screening("CONFIRMED", [hit("SYN-0001", "CONFIRMED")])
        assert lambda_handler(event)["outcome"] == "REJECT"

    def test_identity_is_not_rejudged_after_phase_one(self):
        """A mismatch a reviewer resolved in Phase 1 must not re-route the case."""
        r = lambda_handler(p2(declaredIdentity=dict(DECLARED, surname="RIVERS")))
        assert r["outcome"] == "FULL_APPROVE_STANDARD"


def p3(**over):
    base = {"phase": 3, "asOf": AS_OF, "phaseStartedAt": STARTED,
            "productApplied": "current_account",
            "riskEvent": {"eventId": "LIST-2026-12-02-1", "eventType": "SANCTIONS_LIST_UPDATE",
                          "severity": "HIGH", "customerId": "APP-3",
                          "occurredAt": "2026-12-02T18:03:30Z"},
            "day0": {"caseId": "CASE-APP-3", "tier": "LOW",
                     "outcome": "FULL_APPROVE_STANDARD", "version": 1},
            "facts": facts()}
    base.update(over)
    return base


class TestPhase3:
    def test_a_confirmed_rescreen_freezes(self):
        event = p3()
        event["facts"]["screening"] = screening("CONFIRMED", [hit("SYN-0901", "CONFIRMED")])
        r = lambda_handler(event)
        assert r["outcome"] == "FREEZE"
        assert r["day0Preserved"]["tier"] == "LOW"

    def test_a_clean_rescreen_is_recorded_as_unchanged(self):
        assert lambda_handler(p3())["outcome"] == "RISK_UNCHANGED"

    def test_a_stale_rescreen_is_a_system_error_not_no_change(self):
        """The re-screen did not run in this execution: a human, never "unchanged"."""
        event = p3()
        event["facts"]["screening"] = screening(recordedAt="2026-11-01T00:00:00Z")
        assert lambda_handler(event)["outcome"] == "SYSTEM_ERROR"

    def test_the_history_key_comes_from_the_event(self):
        r = lambda_handler(p3())
        assert r["historySortKey"] == "DECISION#2026-12-02T18:03:30Z#LIST-2026-12-02-1"


UPDATED_LIST = "SYNTHETIC-2026-10-01+LIST-2026-12-02-1"


class TestListCurrency:
    """A list update published while a case waited — for a reviewer, for Phase 2 — must reach
    that case. A screening of the old list is not a screening of the customer."""

    def test_phase_one_does_not_decide_on_an_outdated_list(self):
        r = lambda_handler(p1(currentListVersion=UPDATED_LIST))
        assert r["outcome"] == "MANUAL_REVIEW"
        assert "prescreen" in r["recordedSignals"]["unavailable_agents"]
        assert r["factsRead"]["facts"]["screening"]["status"] == "LIST_OUTDATED"
        assert r["factsRead"]["listCurrency"] == {
            "screenedAgainst": "SYNTHETIC-2026-10-01", "currentListVersion": UPDATED_LIST,
            "checked": True}

    def test_phase_two_never_lifts_on_an_outdated_list(self):
        assert lambda_handler(p2(currentListVersion=UPDATED_LIST))["outcome"] == "MANUAL_REVIEW"

    def test_a_screening_of_the_current_list_decides_as_before(self):
        event = p2(currentListVersion=UPDATED_LIST)
        event["facts"]["screening"] = screening(listVersion=UPDATED_LIST)
        assert lambda_handler(event)["outcome"] == "FULL_APPROVE_STANDARD"

    def test_phase_three_on_an_outdated_list_is_a_system_error(self):
        """The re-screen ran but against a list the update had already replaced — the tool's
        three-second cache, for instance. A person, never "unchanged"."""
        assert lambda_handler(p3(currentListVersion=UPDATED_LIST))["outcome"] == "SYSTEM_ERROR"

    def test_unreadable_is_a_version_too(self):
        """The state machine sends UNREADABLE when it cannot read the list. Nothing matches it."""
        assert lambda_handler(p1(currentListVersion="UNREADABLE"))["outcome"] == "MANUAL_REVIEW"

    def test_omitting_it_skips_the_check_and_says_so(self):
        r = lambda_handler(p1())
        assert r["outcome"] == "PROVISIONAL_APPROVE_RESTRICTED"
        assert r["factsRead"]["listCurrency"]["checked"] is False

    @pytest.mark.parametrize("bad", ["", "   ", 7, ["v"]])
    def test_a_malformed_version_is_refused(self, bad):
        with pytest.raises(InputError):
            lambda_handler(p1(currentListVersion=bad))


class TestDates:
    @pytest.mark.parametrize("value,expected", [
        ("2024-02-29", (2024, 2, 29)), ("2026-12-02T18:04:05Z", (2026, 12, 2)),
        ("2025-02-29", None), ("2026-13-01", None), ("12/02/2026", None), ("", None), (None, None),
    ])
    def test_parse_date(self, value, expected):
        assert parse_date(value) == expected

    def test_age_counts_whole_years(self):
        assert age_on("2008-12-03", "2026-12-02") == 17
        assert age_on("2008-12-02", "2026-12-02") == 18

    def test_epoch_seconds_reads_both_timestamp_shapes(self):
        assert epoch_seconds("1970-01-01T00:00:01Z") == 1
        assert epoch_seconds("2026-12-02T18:04:05.123Z") == epoch_seconds("2026-12-02T18:04:05Z")
        assert epoch_seconds("2026-12-02T18:04:05+00:00") == epoch_seconds("2026-12-02T18:04:05Z")

    def test_a_non_utc_offset_is_refused_not_guessed(self):
        assert epoch_seconds("2026-12-02T18:04:05+05:30") is None
