"""MRZ parsing tests.

The check-digit assertions are made against ICAO 9303's own published examples rather than against
this implementation's output, because a self-consistent wrong implementation would pass either way
and the check digit is the only thing that makes document extraction verifiable instead of merely
trusted.
"""

from __future__ import annotations

from datetime import date

import pytest

from kyctools.mrz import (
    MrzError,
    ParsedMrz,
    char_value,
    check_digit,
    find_mrz_lines,
    normalise_line,
    parse_from_lines,
    parse_td3,
    parse_yymmdd,
)

#: The demo US passport, as Textract actually read it back from the generated JPEG.
US_LINE1 = "P<USAREEVES<<TAYLOR<JORDAN<<<<<<<<<<<<<<<<<<"
US_LINE2 = "4710293051USA9105148X3205138<<<<<<<<<<<<<<00"

#: A fixed "today" so expiry and century inference are deterministic. Tests that depend on the real
#: clock start failing on their own schedule, which is a uniquely annoying way to lose an afternoon.
TODAY = date(2026, 8, 10)


class TestCheckDigit:
    @pytest.mark.parametrize(
        ("field", "expected"),
        [
            # ICAO 9303 Part 3, published examples.
            ("L898902C3", "6"),
            ("690806", "1"),
            ("940623", "6"),
            ("<<<<<<<<<<<<<<", "0"),
        ],
    )
    def test_matches_icao_published_examples(self, field, expected):
        assert check_digit(field) == expected

    def test_char_values_follow_the_specification(self):
        assert char_value("0") == 0
        assert char_value("9") == 9
        assert char_value("A") == 10
        assert char_value("Z") == 35
        assert char_value("<") == 0

    def test_lowercase_is_rejected_rather_than_coerced(self):
        """Coercing would hide an OCR fault: the MRZ is uppercase by definition, so lowercase in
        the input means the text was not read from a real MRZ."""
        with pytest.raises(MrzError, match="not valid in an MRZ"):
            char_value("a")


class TestParseTd3:
    def test_parses_every_field_from_a_real_ocr_reading(self):
        mrz = parse_td3(US_LINE1, US_LINE2, today=TODAY)
        assert mrz.document_code == "P"
        assert mrz.issuing_state == "USA"
        assert mrz.surname == "REEVES"
        assert mrz.given_names == "TAYLOR JORDAN"
        assert mrz.document_number == "471029305"
        assert mrz.nationality == "USA"
        assert mrz.date_of_birth == "1991-05-14"
        assert mrz.sex == "X"
        assert mrz.expiry_date == "2032-05-13"

    def test_all_check_digits_hold_on_a_genuine_mrz(self):
        mrz = parse_td3(US_LINE1, US_LINE2, today=TODAY)
        assert mrz.all_check_digits_valid
        assert mrz.failed_check_digits == ()

    def test_a_tampered_date_of_birth_fails_its_check_digit(self):
        """The whole point of reading the MRZ rather than the printed fields. An altered *printed*
        date reads back as a perfectly plausible date; an altered MRZ date is arithmetically
        detectable."""
        tampered = US_LINE2[:13] + "850101" + US_LINE2[19:]
        mrz = parse_td3(US_LINE1, tampered, today=TODAY)
        assert mrz.date_of_birth == "1985-01-01"       # the claim is still reported
        assert not mrz.date_of_birth_valid             # but it is not consistent
        assert "dateOfBirth" in mrz.failed_check_digits
        assert not mrz.all_check_digits_valid

    def test_a_failed_check_digit_still_returns_the_claimed_fields(self):
        """Raising would discard them, and a reviewer needs to see what the document claims
        alongside the fact that the claim is inconsistent."""
        tampered = US_LINE2[:9] + "9" + US_LINE2[10:]
        mrz = parse_td3(US_LINE1, tampered, today=TODAY)
        assert mrz.document_number == "471029305"
        assert not mrz.document_number_valid

    def test_the_ocr_letter_o_versus_digit_zero_misread_is_detected(self):
        """Found live: document number `USDEMO001` was read back as `USDEM0001`, which fails the
        check digit and is indistinguishable from tampering. The parser must report the
        inconsistency rather than tolerate it — tolerance would destroy the only tamper signal."""
        misread = "USDEM00016USA9105148X3205138<<<<<<<<<<<<<<00"
        mrz = parse_td3(US_LINE1, misread, today=TODAY)
        assert not mrz.document_number_valid

    def test_hyphenated_surname_reads_back_with_filler_as_space(self):
        line1 = "D<GBROSEI<BRIGHT<<ALISTAIR<<<<<<<<<<<<<<<<<<"
        line2 = "5820137464GBR8811020M3011010<<<<<<<<<<<<<<08"
        mrz = parse_td3(line1, line2, today=TODAY)
        # The MRZ cannot carry a hyphen, so `OSEI-BRIGHT` is encoded `OSEI<BRIGHT`. Reading it back
        # as a space is correct; inventing the hyphen would be guessing.
        assert mrz.surname == "OSEI BRIGHT"
        assert mrz.given_names == "ALISTAIR"
        assert mrz.all_check_digits_valid

    def test_multiple_given_names_are_space_separated(self):
        mrz = parse_td3(US_LINE1, US_LINE2, today=TODAY)
        assert mrz.given_names == "TAYLOR JORDAN"

    @pytest.mark.parametrize("bad", ["too short", "P<USA" + "!" * 39])
    def test_input_that_is_not_a_td3_mrz_raises(self, bad):
        with pytest.raises(MrzError):
            parse_td3(bad, US_LINE2)

    def test_lowercase_ocr_output_is_normalised_rather_than_rejected(self):
        """`char_value` is strict about case, but `parse_td3` normalises first. Both are correct
        and the distinction is deliberate: the MRZ is uppercase by definition, but OCR case is a
        property of the *reader*, not the document, so rejecting on it would discard a perfectly
        readable document."""
        mrz = parse_td3(US_LINE1.lower(), US_LINE2.lower(), today=TODAY)
        assert mrz.surname == "REEVES"
        assert mrz.all_check_digits_valid

    def test_structurally_legal_but_meaningless_lines_parse_and_fail_check_digits(self):
        """44 legal characters *are* a structurally valid TD3 line, so this must not raise — it
        must parse and then report the check digits as inconsistent. Raising would conflate "not an
        MRZ" with "an MRZ that does not add up", and only the second is a document finding."""
        mrz = parse_td3("X" * 44, "X" * 44, today=TODAY)
        assert not mrz.all_check_digits_valid

    def test_ocr_inserted_spaces_are_tolerated(self):
        """The MRZ is fixed-pitch and OCR routinely reads the wide spacing as real spaces. A space
        is not a legal MRZ character, so stripping them cannot lose information."""
        spaced = " ".join(US_LINE2)
        mrz = parse_td3(US_LINE1, spaced, today=TODAY)
        assert mrz.document_number == "471029305"
        assert mrz.all_check_digits_valid

    def test_normalise_line_strips_whitespace_and_uppercases(self):
        assert normalise_line("p< usa  reeves") == "P<USAREEVES"


class TestCenturyInference:
    """The MRZ carries no century, so it has to be inferred — and differently per field."""

    def test_birth_dates_resolve_into_the_past(self):
        assert parse_yymmdd("910514", is_expiry=False, today=TODAY) == "1991-05-14"

    def test_expiry_dates_resolve_into_the_future(self):
        assert parse_yymmdd("320513", is_expiry=True, today=TODAY) == "2032-05-13"

    def test_a_birth_date_is_never_resolved_into_the_future(self):
        """`91` as a birth year must be 1991, not 2091 — otherwise an adult applicant appears
        unborn and every age rule downstream is nonsense."""
        resolved = parse_yymmdd("910514", is_expiry=False, today=TODAY)
        assert resolved is not None
        assert date.fromisoformat(resolved) < TODAY

    def test_an_expiry_date_is_not_resolved_backwards(self):
        """A wrongly-resolved expiry would report a valid document as expired."""
        resolved = parse_yymmdd("320513", is_expiry=True, today=TODAY)
        assert resolved is not None
        assert date.fromisoformat(resolved) > TODAY

    def test_a_recently_passed_expiry_is_not_pushed_a_century_forward(self):
        """Regression. An earlier rule resolved any expiry whose two-digit year had already passed
        into the *next* century, so `32` read on 2033-01-01 became 2132 and a document that expired
        last year reported as valid for another hundred — failing in the one direction that
        matters."""
        resolved = parse_yymmdd("320513", is_expiry=True, today=date(2033, 1, 1))
        assert resolved == "2032-05-13"

    @pytest.mark.parametrize(
        ("raw", "today", "expected"),
        [
            ("320513", date(2026, 8, 10), "2032-05-13"),   # comfortably in the future
            ("320513", date(2033, 1, 1), "2032-05-13"),    # just expired
            ("991231", date(2026, 8, 10), "1999-12-31"),   # long expired
            ("050101", date(2026, 8, 10), "2005-01-01"),   # long expired, this century
            ("300101", date(2026, 8, 10), "2030-01-01"),   # valid
        ],
    )
    def test_expiry_century_resolves_to_the_nearest_candidate(self, raw, today, expected):
        """Travel documents are issued for at most about a decade, so the true century is always
        the nearest one — which is why nearest-candidate is the rule rather than next-in-future."""
        assert parse_yymmdd(raw, is_expiry=True, today=today) == expected

    @pytest.mark.parametrize("bad", ["", "12345", "9105149", "911332", "abcdef"])
    def test_impossible_dates_are_reported_unreadable_not_coerced(self, bad):
        assert parse_yymmdd(bad, is_expiry=False, today=TODAY) is None


class TestExpiry:
    def test_a_future_expiry_is_not_expired(self):
        mrz = parse_td3(US_LINE1, US_LINE2, today=TODAY)
        assert mrz.is_expired(today=TODAY) is False

    def test_a_past_expiry_is_expired(self):
        mrz = parse_td3(US_LINE1, US_LINE2, today=date(2033, 1, 1))
        assert mrz.is_expired(today=date(2033, 1, 1)) is True

    def test_an_unreadable_expiry_is_none_not_false(self):
        """`None` and `False` are different facts: "we could not tell" must not be recorded as
        "the document is valid"."""
        no_expiry = US_LINE2[:21] + "<<<<<<" + US_LINE2[27:]
        mrz = parse_td3(US_LINE1, no_expiry, today=TODAY)
        assert mrz.expiry_date is None
        assert mrz.is_expired(today=TODAY) is None


class TestFindMrzLines:
    def test_finds_the_mrz_among_all_the_other_ocr_lines(self):
        """Real Textract output from the generated document, including the footer that appears
        *below* the MRZ — which is why selection is by structure and not by position."""
        lines = [
            "SPECIMEN - NOT A REAL TRAVEL DOCUMENT",
            "PASSPORT",
            "UNITED STATES OF AMERICA",
            "SURNAME / NOM",
            "REEVES",
            "DATE OF BIRTH / DATE DE NAISSANCE",
            "1991-05-14",
            US_LINE1,
            US_LINE2,
            "Persona DEMO-US-01 - synthetic data, no real subject. Jurisdiction: US.",
            "This document is a specimen. It is not issued by any government.",
        ]
        found = find_mrz_lines(lines)
        assert found == (US_LINE1, US_LINE2)

    def test_returns_none_when_there_is_no_mrz(self):
        assert find_mrz_lines(["JUST", "SOME", "TEXT"]) is None

    def test_returns_none_when_only_one_mrz_line_was_read(self):
        """Half an MRZ is not parseable, and guessing the other half would fabricate fields."""
        assert find_mrz_lines(["PASSPORT", US_LINE1]) is None

    def test_parse_from_lines_end_to_end(self):
        result = parse_from_lines(["PASSPORT", US_LINE1, US_LINE2, "footer"], today=TODAY)
        assert isinstance(result, ParsedMrz)
        assert result.surname == "REEVES"

    def test_parse_from_lines_returns_none_rather_than_raising(self):
        """The caller is a tool that must return an envelope, never a stack trace."""
        assert parse_from_lines(["no mrz here"]) is None


class TestToDict:
    def test_serialises_the_fields_a_reviewer_needs(self):
        payload = parse_td3(US_LINE1, US_LINE2, today=TODAY).to_dict()
        assert payload["surname"] == "REEVES"
        assert payload["documentNumber"] == "471029305"
        assert payload["checkDigitsValid"] is True
        assert payload["failedCheckDigits"] == []

    def test_names_which_check_digit_failed_not_just_that_one_did(self):
        tampered = US_LINE2[:13] + "850101" + US_LINE2[19:]
        payload = parse_td3(US_LINE1, tampered, today=TODAY).to_dict()
        assert payload["checkDigitsValid"] is False
        assert "dateOfBirth" in payload["failedCheckDigits"]
