"""Declared identity versus document identity.

The assertions worth reading here are the ones about *direction of failure*: an unverifiable field
must never read as confirmed, and no verdict may ever be REJECT.
"""

from __future__ import annotations

import pytest

from identity_match import (
    FieldResult,
    compare,
    evaluate,
    normalise_name,
    strip_diacritics,
    verdicts,
)
from models import Phase1Outcome

DECLARED = {
    "givenNames": "TAYLOR JORDAN",
    "surname": "REEVES",
    "dateOfBirth": "1991-05-14",
    "nationality": "USA",
    "address": "482 Kestrel Way, Austin, TX 78701, USA",
}

#: As `s3_get_document` actually returns it, verified live.
EXTRACTION = {
    "documentReadable": True,
    "mrzPresent": True,
    "mrz": {
        "documentCode": "P",
        "issuingState": "USA",
        "surname": "REEVES",
        "givenNames": "TAYLOR JORDAN",
        "documentNumber": "471029305",
        "nationality": "USA",
        "dateOfBirth": "1991-05-14",
        "sex": "X",
        "expiryDate": "2032-05-13",
        "checkDigitsValid": True,
        "failedCheckDigits": [],
        "expired": False,
    },
}


def _extraction(**mrz_overrides):
    mrz = {**EXTRACTION["mrz"], **mrz_overrides}
    return {**EXTRACTION, "mrz": mrz}


class TestCleanMatch:
    def test_matching_details_confirm_the_identity(self):
        result = compare(DECLARED, EXTRACTION)
        assert result.surname is FieldResult.MATCH
        assert result.given_names is FieldResult.MATCH
        assert result.date_of_birth is FieldResult.MATCH
        assert result.nationality is FieldResult.MATCH
        assert result.identity_confirmed is True
        assert result.mismatched_fields == ()

    def test_a_clean_match_raises_no_verdicts(self):
        assert verdicts(compare(DECLARED, EXTRACTION)) == ()


class TestMismatchDetection:
    def test_a_wrong_date_of_birth_is_caught(self):
        """The demo moment: type a wrong date of birth and the system reads the real one off the
        passport and disagrees."""
        result = compare({**DECLARED, "dateOfBirth": "1985-01-01"}, EXTRACTION)
        assert result.date_of_birth is FieldResult.MISMATCH
        assert result.mismatched_fields == ("dateOfBirth",)
        assert result.identity_confirmed is False

    def test_a_wrong_surname_is_caught(self):
        result = compare({**DECLARED, "surname": "SMITH"}, EXTRACTION)
        assert result.surname is FieldResult.MISMATCH

    def test_several_mismatches_are_all_reported(self):
        """All of them, not the first. A reviewer deciding whether this is a typo or a different
        person needs to see the whole picture."""
        result = compare(
            {**DECLARED, "surname": "SMITH", "dateOfBirth": "1985-01-01"}, EXTRACTION)
        assert set(result.mismatched_fields) == {"surname", "dateOfBirth"}

    def test_the_verdict_carries_both_values_for_the_reviewer(self):
        result = compare({**DECLARED, "dateOfBirth": "1985-01-01"}, EXTRACTION)
        verdict = verdicts(result)[0]
        assert verdict.reason_code == "DECLARED_DOCUMENT_MISMATCH"
        assert verdict.signals["declared"]["dateOfBirth"] == "1985-01-01"
        assert verdict.signals["document"]["dateOfBirth"] == "1991-05-14"


class TestNoVerdictIsEverAReject:
    """The load-bearing property. A name mismatch has innocent causes that outnumber the guilty
    ones — married names, transliteration, a middle name on one side only, OCR — and an automated
    rejection cannot be appealed from inside the flow."""

    @pytest.mark.parametrize("declared_override,extraction_kwargs", [
        ({"surname": "SMITH"}, {}),
        ({"dateOfBirth": "1985-01-01"}, {}),
        ({"nationality": "GBR"}, {}),
        ({}, {"failedCheckDigits": ["dateOfBirth"]}),
    ])
    def test_every_mismatch_routes_to_a_human_never_a_rejection(
        self, declared_override, extraction_kwargs
    ):
        result = compare({**DECLARED, **declared_override}, _extraction(**extraction_kwargs))
        produced = verdicts(result)
        assert produced, "a mismatch must produce a verdict"
        for verdict in produced:
            assert verdict.outcome is Phase1Outcome.MANUAL_REVIEW
            assert verdict.outcome is not Phase1Outcome.REJECT

    def test_an_unreadable_document_also_routes_to_a_human(self):
        result = compare(DECLARED, {"documentReadable": False, "mrzPresent": False})
        produced = verdicts(result)
        assert [v.reason_code for v in produced] == [
            "DOCUMENT_NOT_MACHINE_READABLE"] or "DOCUMENT_NOT_MACHINE_READABLE" in [
            v.reason_code for v in produced]
        assert all(v.outcome is Phase1Outcome.MANUAL_REVIEW for v in produced)


class TestUnverifiableIsNotConfirmed:
    """`UNVERIFIABLE` exists because collapsing it either way is a lie: into MATCH it fails open,
    into MISMATCH it accuses the applicant of a discrepancy nobody observed."""

    def test_an_unreadable_document_leaves_every_field_unverifiable(self):
        result = compare(DECLARED, {"documentReadable": False, "mrzPresent": False})
        assert result.surname is FieldResult.UNVERIFIABLE
        assert result.date_of_birth is FieldResult.UNVERIFIABLE
        assert result.identity_confirmed is False
        assert set(result.unverifiable_fields) == {
            "surname", "givenNames", "dateOfBirth", "nationality"}

    def test_a_missing_declared_identity_is_unverifiable_not_matching(self):
        """A case submitted before the details form existed has no declared identity. It must not
        read as confirmed."""
        result = compare(None, EXTRACTION)
        assert result.identity_confirmed is False
        assert result.mismatched_fields == ()

    def test_both_sides_missing_is_still_not_confirmed(self):
        result = compare(None, None)
        assert result.identity_confirmed is False


class TestNotPerformedIsNotAFailedCheck:
    """The distinction that caused a real bug. "The control did not run" and "the control ran and
    the document was unreadable" are different facts, and conflating them sent every caller that
    predates this module to manual review because an absent input read as a failed check."""

    def test_no_extraction_at_all_means_not_performed(self):
        assert compare(DECLARED, None).performed is False
        assert compare(None, None).performed is False
        assert compare(DECLARED, {}).performed is False

    def test_not_performed_raises_no_verdict(self):
        assert verdicts(compare(DECLARED, None)) == ()

    def test_not_performed_is_still_not_confirmed(self):
        """No verdict, but no false confidence either — the absence has to be visible."""
        result = compare(DECLARED, None)
        assert result.identity_confirmed is False
        assert result.to_dict()["performed"] is False

    def test_an_extraction_that_ran_and_failed_does_raise_a_verdict(self):
        """The other side of the distinction: the pipeline tried, and the document was unreadable."""
        result = compare(DECLARED, {"documentReadable": False, "mrzPresent": False})
        assert result.performed is True
        codes = [v.reason_code for v in verdicts(result)]
        assert "DOCUMENT_NOT_MACHINE_READABLE" in codes

    def test_a_readable_document_with_no_declared_identity_reaches_a_human(self):
        """Read successfully, carries an MRZ, but nothing to check it against. That case cannot be
        screened either — `sanctions_exact_match` refuses to run without a name — so it must not
        proceed on a document alone."""
        result = compare(None, EXTRACTION)
        codes = [v.reason_code for v in verdicts(result)]
        assert "NO_DECLARED_IDENTITY" in codes
        assert all(v.outcome is Phase1Outcome.MANUAL_REVIEW for v in verdicts(result))

    def test_a_document_with_no_mrz_reaches_a_human(self):
        result = compare(DECLARED, {"documentReadable": True, "mrzPresent": False})
        codes = [v.reason_code for v in verdicts(result)]
        assert "NO_MACHINE_READABLE_ZONE" in codes

    def test_an_unverifiable_field_is_not_counted_as_a_mismatch(self):
        result = compare({**DECLARED, "nationality": ""}, EXTRACTION)
        assert result.nationality is FieldResult.UNVERIFIABLE
        assert "nationality" not in result.mismatched_fields


class TestDynamoDbShapes:
    """The state machine passes values straight off the case record, so a JSON blob arrives as a
    string and a map as `{"M": {...}}`. Same tolerance `signals.from_findings` applies."""

    def test_extraction_arriving_as_a_json_string_is_parsed(self):
        import json as _json
        result = compare(DECLARED, _json.dumps(EXTRACTION))
        assert result.performed is True
        assert result.surname is FieldResult.MATCH

    def test_extraction_arriving_as_a_dynamodb_string_attribute_is_parsed(self):
        import json as _json
        result = compare(DECLARED, {"S": _json.dumps(EXTRACTION)})
        assert result.performed is True
        assert result.surname is FieldResult.MATCH

    def test_declared_identity_arriving_as_a_dynamodb_map_is_unwrapped(self):
        typed = {"M": {k: {"S": v} for k, v in DECLARED.items()}}
        result = compare(typed, EXTRACTION)
        assert result.surname is FieldResult.MATCH
        assert result.date_of_birth is FieldResult.MATCH

    def test_a_dynamodb_null_reads_as_absent_not_as_a_value(self):
        result = compare(DECLARED, {"NULL": True})
        assert result.performed is False

    def test_unparseable_json_is_treated_as_absent_rather_than_crashing(self):
        result = compare(DECLARED, "{not json")
        assert result.performed is False
        assert verdicts(result) == ()

    def test_a_bare_string_field_is_not_mangled_by_json_parsing(self):
        """A surname is a string too. Only values that look like JSON are decoded, so `REEVES` must
        survive intact rather than becoming None."""
        typed = {"M": {"surname": {"S": "REEVES"}, "givenNames": {"S": "TAYLOR JORDAN"},
                       "dateOfBirth": {"S": "1991-05-14"}, "nationality": {"S": "USA"}}}
        result = compare(typed, EXTRACTION)
        assert result.declared["surname"] == "REEVES"
        assert result.identity_confirmed is True


class TestEncodingArtefactsAreNotMismatches:
    def test_a_hyphenated_surname_matches_its_mrz_form(self):
        """ICAO 9303 has no hyphen: `OSEI-BRIGHT` is encoded `OSEI<BRIGHT` and reads back as
        `OSEI BRIGHT`. Treating that as a mismatch would flag every hyphenated surname."""
        result = compare(
            {**DECLARED, "surname": "OSEI-BRIGHT"}, _extraction(surname="OSEI BRIGHT"))
        assert result.surname is FieldResult.MATCH

    def test_diacritics_match_their_transliteration(self):
        """The MRZ cannot carry diacritics, so a document always transliterates while a web form
        does not."""
        result = compare(
            {**DECLARED, "surname": "FERNÁNDEZ"}, _extraction(surname="FERNANDEZ"))
        assert result.surname is FieldResult.MATCH

    def test_case_and_extra_whitespace_are_folded(self):
        result = compare(
            {**DECLARED, "surname": "  reeves  "}, EXTRACTION)
        assert result.surname is FieldResult.MATCH

    def test_apostrophes_are_folded(self):
        result = compare({**DECLARED, "surname": "O'BRIEN"}, _extraction(surname="O BRIEN"))
        assert result.surname is FieldResult.MATCH

    def test_reordered_names_are_still_a_mismatch(self):
        """Token order is preserved on purpose. A reversed name is a real signal, and sorting the
        tokens would silently erase it."""
        result = compare({**DECLARED, "givenNames": "JORDAN TAYLOR"}, EXTRACTION)
        assert result.given_names is FieldResult.MISMATCH

    def test_normalise_helpers(self):
        assert normalise_name("Osei-Bright") == "OSEI BRIGHT"
        assert strip_diacritics("José") == "Jose"


class TestNationalityCodeLength:
    def test_alpha2_against_alpha3_is_unverifiable_not_mismatched(self):
        """The MRZ carries alpha-3 (`USA`) while a form may collect alpha-2 (`US`). Mapping between
        them needs a country table this module does not carry, so guessing would flag every
        applicant."""
        result = compare({**DECLARED, "nationality": "US"}, EXTRACTION)
        assert result.nationality is FieldResult.UNVERIFIABLE

    def test_same_length_different_country_is_a_mismatch(self):
        result = compare({**DECLARED, "nationality": "GBR"}, EXTRACTION)
        assert result.nationality is FieldResult.MISMATCH


class TestCheckDigitsAreSeparateFromMismatch:
    """They answer different questions: a mismatch means two sources disagree, a failed check digit
    means one source is not internally consistent — most often because OCR misread a character."""

    def test_a_failed_check_digit_raises_its_own_verdict(self):
        result = compare(DECLARED, _extraction(failedCheckDigits=["documentNumber"]))
        codes = [v.reason_code for v in verdicts(result)]
        assert "DOCUMENT_CHECK_DIGIT_FAILED" in codes
        assert "DECLARED_DOCUMENT_MISMATCH" not in codes

    def test_the_explanation_names_ocr_as_a_cause(self):
        result = compare(DECLARED, _extraction(failedCheckDigits=["documentNumber"]))
        verdict = next(v for v in verdicts(result)
                       if v.reason_code == "DOCUMENT_CHECK_DIGIT_FAILED")
        assert "misread" in (verdict.explanation or "")


class TestEvaluate:
    def test_returns_both_the_comparison_and_the_verdicts(self):
        comparison, produced = evaluate({**DECLARED, "surname": "SMITH"}, EXTRACTION)
        assert comparison.surname is FieldResult.MISMATCH
        assert len(produced) == 1

    def test_serialisation_carries_what_a_reviewer_needs(self):
        comparison, _ = evaluate({**DECLARED, "dateOfBirth": "1985-01-01"}, EXTRACTION)
        payload = comparison.to_dict()
        assert payload["dateOfBirth"] == "MISMATCH"
        assert payload["identityConfirmed"] is False
        assert payload["declared"]["dateOfBirth"] == "1985-01-01"
        assert payload["document"]["dateOfBirth"] == "1991-05-14"
        assert payload["documentExpired"] is False
