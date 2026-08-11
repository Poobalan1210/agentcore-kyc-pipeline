"""Machine-readable zone parsing — ICAO 9303 TD3.

Pure. No boto3, no clock, no network, so every branch is testable without AWS. This is the
counterpart to `tools_impl.py`'s impure half, the same split `decisions.py`/`review_handler.py`
use elsewhere in the repo.

**Why the MRZ rather than reading the printed fields.** Both are on the document and OCR returns
both, but only the MRZ carries check digits. A printed date of birth that has been altered reads
back as a perfectly plausible date; an altered MRZ date fails its check digit arithmetically. That
makes this the only part of a document where extraction can be *verified* rather than merely
trusted, which is why the check digits are computed here rather than skipped as decoration.

**What this deliberately does not do.** It reports fields and whether their check digits hold. It
does not decide whether a case passes. A failed check digit means "these characters are not
internally consistent", which has at least two causes — tampering and OCR misreading a character —
and distinguishing them is a decision-engine question, not a parser's. `tools_impl.py` returns
these findings as signals for exactly that reason.

A concrete instance of the OCR ambiguity, found live: the demo passports originally carried document
number `USDEMO001`, mixing the letter O with the digit 0. Textract read it back as `USDEM0001`,
which fails the check digit and is indistinguishable from tampering. The fix was to the *documents*
(nine digits, as real US passports use), not to this parser — a parser that tolerated the mismatch
would have destroyed the only tamper signal available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

#: TD3 is the passport format: two lines of exactly 44 characters. TD1 (ID cards, three lines of
#: 30) and TD2 (two lines of 36) are not parsed here — the demo documents are all TD3, and silently
#: mis-parsing a TD1 as a TD3 would produce confident nonsense rather than an honest "unreadable".
TD3_LINE_LENGTH = 44
TD3_LINES = 2

#: Positions in line 2, per ICAO 9303 Part 4. Hardcoded as slices rather than computed, because the
#: layout is fixed by the specification and an off-by-one here silently shifts every field.
_DOC_NUMBER = slice(0, 9)
_DOC_NUMBER_CD = 9
_NATIONALITY = slice(10, 13)
_DOB = slice(13, 19)
_DOB_CD = 19
_SEX = 20
_EXPIRY = slice(21, 27)
_EXPIRY_CD = 27
_OPTIONAL = slice(28, 42)
_OPTIONAL_CD = 42
_COMPOSITE_CD = 43

_FILLER = "<"


class MrzError(ValueError):
    """The lines are not a TD3 MRZ at all. Distinct from "parsed but check digits failed"."""


def char_value(char: str) -> int:
    """Digits are themselves, A-Z are 10-35, filler is 0 (ICAO 9303 Part 3)."""
    if char.isdigit():
        return int(char)
    if "A" <= char <= "Z":
        return ord(char) - ord("A") + 10
    if char == _FILLER:
        return 0
    raise MrzError(f"character {char!r} is not valid in an MRZ field")


def check_digit(field: str) -> str:
    """ICAO 9303 check digit: weights cycle 7,3,1 across the field, sum modulo 10."""
    weights = (7, 3, 1)
    total = sum(char_value(c) * weights[i % 3] for i, c in enumerate(field))
    return str(total % 10)


def _names(line1: str) -> tuple[str, str]:
    """`SURNAME<<GIVEN<NAMES` -> ("SURNAME", "GIVEN NAMES").

    Filler becomes a space and trailing filler is dropped. The separator is a double filler; a
    single one is a space *within* a name, which is why the split is on `<<` and not on `<`.
    """
    field = line1[5:]
    surname_part, _, given_part = field.partition("<<")
    surname = surname_part.replace(_FILLER, " ").strip()
    given = " ".join(p for p in given_part.replace(_FILLER, " ").split() if p)
    return surname, given


def parse_yymmdd(value: str, *, is_expiry: bool, today: date | None = None) -> str | None:
    """`YYMMDD` -> ISO `yyyy-mm-dd`, or None if it is not a real date.

    The century is absent from the MRZ and has to be inferred, and the two fields need different
    rules. Getting either wrong is not cosmetic:

    * A birth year of `91` resolved to 2091 rather than 1991 makes an adult applicant appear
      unborn, and every age rule downstream becomes nonsense.
    * An expiry resolved to the wrong century flips the expired/valid verdict outright.

    **Birth dates** are resolved to the most recent candidate not in the future — a person cannot
    be born tomorrow.

    **Expiry dates** are resolved to the candidate *nearest today*, which is not the same as "the
    next one in the future". An earlier version pushed any expiry whose two-digit year had already
    passed into the following century, so a passport that expired last year read as valid for
    another hundred — failing in the one direction that matters. Nearest-candidate is correct
    because travel documents are issued for at most about a decade, so the true century is always
    the closest one; and it keeps genuinely expired documents detectable, which is the point.
    """
    if len(value) != 6 or not value.isdigit():
        return None
    yy, mm, dd = int(value[0:2]), int(value[2:4]), int(value[4:6])
    reference = today or date.today()
    base = reference.year - (reference.year % 100)

    candidates: list[date] = []
    for century in (base - 100, base, base + 100):
        try:
            candidates.append(date(century + yy, mm, dd))
        except ValueError:
            # A real MRZ can carry `<<<<<<` or an impossible day; skipped rather than coerced into
            # something plausible.
            continue
    if not candidates:
        return None

    if is_expiry:
        chosen = min(candidates, key=lambda d: abs((d - reference).days))
    else:
        past = [d for d in candidates if d <= reference]
        if not past:
            return None
        chosen = max(past)
    return chosen.isoformat()


@dataclass(frozen=True, kw_only=True)
class ParsedMrz:
    """Everything the MRZ states, plus whether it states it consistently."""

    document_code: str
    issuing_state: str
    surname: str
    given_names: str
    document_number: str
    nationality: str
    date_of_birth: str | None
    sex: str
    expiry_date: str | None

    document_number_valid: bool
    date_of_birth_valid: bool
    expiry_date_valid: bool
    composite_valid: bool

    raw_line1: str
    raw_line2: str

    @property
    def all_check_digits_valid(self) -> bool:
        return (self.document_number_valid and self.date_of_birth_valid
                and self.expiry_date_valid and self.composite_valid)

    @property
    def failed_check_digits(self) -> tuple[str, ...]:
        """Named so a reviewer sees *which* field is inconsistent, not just that one is."""
        failed = []
        if not self.document_number_valid:
            failed.append("documentNumber")
        if not self.date_of_birth_valid:
            failed.append("dateOfBirth")
        if not self.expiry_date_valid:
            failed.append("expiryDate")
        if not self.composite_valid:
            failed.append("composite")
        return tuple(failed)

    def is_expired(self, *, today: date | None = None) -> bool | None:
        """None when the expiry date could not be read — distinct from "not expired"."""
        if self.expiry_date is None:
            return None
        return date.fromisoformat(self.expiry_date) < (today or date.today())

    def to_dict(self) -> dict[str, object]:
        return {
            "documentCode": self.document_code,
            "issuingState": self.issuing_state,
            "surname": self.surname,
            "givenNames": self.given_names,
            "documentNumber": self.document_number,
            "nationality": self.nationality,
            "dateOfBirth": self.date_of_birth,
            "sex": self.sex,
            "expiryDate": self.expiry_date,
            "checkDigitsValid": self.all_check_digits_valid,
            "failedCheckDigits": list(self.failed_check_digits),
            "expired": self.is_expired(),
        }


def normalise_line(text: str) -> str:
    """Strip whitespace OCR inserts between MRZ characters and uppercase the result.

    The MRZ is fixed-pitch, and OCR frequently reads the wide inter-character spacing as real
    spaces. Removing them is safe because a space is not a legal MRZ character — filler is `<`.
    """
    return "".join(text.split()).upper()


def find_mrz_lines(lines: list[str]) -> tuple[str, str] | None:
    """Pick the two TD3 lines out of everything OCR returned.

    Selected by structure, not position: the last two 44-character lines made only of MRZ-legal
    characters. Position would be the obvious rule and is wrong — OCR line ordering is not
    guaranteed, and the demo documents carry a footer *below* the MRZ.
    """
    candidates = [
        normalised for normalised in (normalise_line(line) for line in lines)
        if len(normalised) == TD3_LINE_LENGTH and _is_mrz_charset(normalised)
    ]
    if len(candidates) < TD3_LINES:
        return None
    # Line 1 always starts with the document code and `<`-padded issuing state; line 2 starts with
    # the document number. Anchoring on that avoids returning them in the wrong order, which would
    # otherwise parse names out of the numeric line.
    for i in range(len(candidates) - 1):
        first, second = candidates[i], candidates[i + 1]
        if first[1] == _FILLER or first[0].isalpha():
            return first, second
    return candidates[-2], candidates[-1]


def _is_mrz_charset(text: str) -> bool:
    return all(c.isdigit() or ("A" <= c <= "Z") or c == _FILLER for c in text)


def parse_td3(line1: str, line2: str, *, today: date | None = None) -> ParsedMrz:
    """Parse and verify. Raises `MrzError` only when the input is not a TD3 MRZ at all.

    A failed check digit is *not* an error — it is a finding, returned on the result. Raising there
    would discard the fields, and a reviewer needs to see the claimed values alongside the fact
    that they are inconsistent.
    """
    l1 = normalise_line(line1)
    l2 = normalise_line(line2)
    for label, line in (("line 1", l1), ("line 2", l2)):
        if len(line) != TD3_LINE_LENGTH:
            raise MrzError(f"{label} is {len(line)} characters, TD3 requires {TD3_LINE_LENGTH}")
        if not _is_mrz_charset(line):
            raise MrzError(f"{label} contains characters that are not legal in an MRZ")

    surname, given = _names(l1)
    doc_number_raw = l2[_DOC_NUMBER]
    dob_raw = l2[_DOB]
    expiry_raw = l2[_EXPIRY]
    optional_raw = l2[_OPTIONAL]

    composite_source = (
        doc_number_raw + l2[_DOC_NUMBER_CD]
        + dob_raw + l2[_DOB_CD]
        + expiry_raw + l2[_EXPIRY_CD]
        + optional_raw + l2[_OPTIONAL_CD]
    )

    return ParsedMrz(
        document_code=l1[0],
        issuing_state=l1[2:5].replace(_FILLER, ""),
        surname=surname,
        given_names=given,
        # Trailing filler is padding, not part of the number.
        document_number=doc_number_raw.replace(_FILLER, ""),
        nationality=l2[_NATIONALITY].replace(_FILLER, ""),
        date_of_birth=parse_yymmdd(dob_raw, is_expiry=False, today=today),
        sex=l2[_SEX].replace(_FILLER, ""),
        expiry_date=parse_yymmdd(expiry_raw, is_expiry=True, today=today),
        document_number_valid=check_digit(doc_number_raw) == l2[_DOC_NUMBER_CD],
        date_of_birth_valid=check_digit(dob_raw) == l2[_DOB_CD],
        expiry_date_valid=check_digit(expiry_raw) == l2[_EXPIRY_CD],
        composite_valid=check_digit(composite_source) == l2[_COMPOSITE_CD],
        raw_line1=l1,
        raw_line2=l2,
    )


def parse_from_lines(lines: list[str], *, today: date | None = None) -> ParsedMrz | None:
    """Convenience: find the MRZ in OCR output and parse it. None if there is no MRZ to parse."""
    found = find_mrz_lines(lines)
    if found is None:
        return None
    try:
        return parse_td3(found[0], found[1], today=today)
    except MrzError:
        return None
