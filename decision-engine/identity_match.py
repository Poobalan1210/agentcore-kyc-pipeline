"""Declared identity versus document identity — task 6, spec gap (see below).

Pure. No boto3, no clock, no network.

**Why this exists.** KYC has two sources of truth and the comparison between them is itself a
control: what the applicant *declares*, and what their document *proves*. Neither alone is
sufficient. A declared identity with no document is unverified; a document with no declared
identity cannot be screened at all — `sanctions_exact_match` raises `MISSING_NAME` and refuses to
run. A mismatch between the two is a fraud signal that cannot exist unless both are captured.

**SPEC GAP, flagged rather than silently invented.** The requirements document has no clause for a
declared-vs-document mismatch: R28 covers document defects (expired, screen photograph, quality,
unsupported issuer), R29 covers person attributes, R30 covers process. None of them says what
happens when the applicant's stated name disagrees with the name on their passport. So the routing
chosen here is the conservative one — a human decides — and it is recorded as a gap for the spec
owner rather than presented as an implemented requirement. The same convention `decisions.py` uses
for `REQUEST_STEP_UP` → `AWAITING_APPLICANT`.

**Why the verdicts are `MANUAL_REVIEW` and never `REJECT`.** A name mismatch has innocent causes
that outnumber the guilty ones: a married name, a transliteration, a middle name recorded in the
given-names field on one side and not the other, or simply OCR. Rejecting on it would fail exactly
the population B-08 describes, where 90-95% of screening-style hits are not true matches. A human
looking at both values resolves it in seconds; an automated rejection cannot be appealed inside the
flow.

**Why a failed MRZ check digit is treated separately from a mismatch.** They answer different
questions. A mismatch says "these two sources disagree". A failed check digit says "this source is
not internally consistent", which is a property of the document alone — and its most common cause
is OCR misreading a character, not tampering. Conflating them would let an OCR artefact present as
a fraud finding.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from models import EdgeVerdict, Phase1Outcome

#: Case id prefix for the verdicts this module raises. Distinct from the `E*` ids the fifteen
#: design §8.4 rules use, so an auditor can tell a mandated edge case from this spec-gap rule.
_CASE_PREFIX = "IDM"

#: Marks every verdict from this module as resting on a gap rather than a requirement clause.
_REQUIREMENT = "SPEC-GAP/identity-match"


class FieldResult(Enum):
    """Three outcomes, not two. `UNVERIFIABLE` is the important one.

    A field that could not be compared — because the document was unreadable, or the applicant
    declared nothing — is not a match and is not a mismatch. Collapsing it into either direction
    would be a lie: into MATCH it fails open, into MISMATCH it accuses an applicant of a
    discrepancy that was never observed.
    """

    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    UNVERIFIABLE = "UNVERIFIABLE"


def strip_diacritics(value: str) -> str:
    """José -> Jose. The MRZ cannot carry diacritics, so a document always transliterates them
    while a web form does not — comparing without this would flag every accented name."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c))


def normalise_name(value: str) -> str:
    """Fold the differences that are encoding artefacts rather than differences in the name.

    Hyphens and apostrophes become spaces because ICAO 9303 has no character for them: `OSEI-BRIGHT`
    is encoded `OSEI<BRIGHT` and reads back as `OSEI BRIGHT`. Treating that as a mismatch would flag
    every hyphenated surname in the system.

    Token *order* is deliberately preserved. Reordered names are a real signal worth surfacing, and
    sorting the tokens here would silently erase it — the same reason `data/sanctions.py`'s
    `normalise` refuses to reorder.
    """
    folded = strip_diacritics(value).upper()
    for char in "-'`’.,_":
        folded = folded.replace(char, " ")
    return " ".join(folded.split())


def _compare_names(declared: str, document: str) -> FieldResult:
    if not declared.strip() or not document.strip():
        return FieldResult.UNVERIFIABLE
    return (FieldResult.MATCH if normalise_name(declared) == normalise_name(document)
            else FieldResult.MISMATCH)


def _compare_exact(declared: str, document: str) -> FieldResult:
    if not declared.strip() or not document.strip():
        return FieldResult.UNVERIFIABLE
    return (FieldResult.MATCH if declared.strip() == document.strip()
            else FieldResult.MISMATCH)


def _compare_country(declared: str, document: str) -> FieldResult:
    """Compared only when both sides use the same code length.

    The MRZ carries ISO 3166-1 **alpha-3** (`USA`), while a form may collect alpha-2 (`US`).
    Mapping between them needs a country table this module deliberately does not carry, so a
    length mismatch is reported `UNVERIFIABLE` rather than guessed at — an alpha-2/alpha-3
    comparison would flag every applicant.
    """
    d, doc = declared.strip().upper(), document.strip().upper()
    if not d or not doc:
        return FieldResult.UNVERIFIABLE
    if len(d) != len(doc):
        return FieldResult.UNVERIFIABLE
    return FieldResult.MATCH if d == doc else FieldResult.MISMATCH


@dataclass(frozen=True, kw_only=True)
class IdentityComparison:
    """Per-field results plus the raw values, so a reviewer sees *what* disagreed."""

    surname: FieldResult
    given_names: FieldResult
    date_of_birth: FieldResult
    nationality: FieldResult

    declared: Mapping[str, str] = field(default_factory=dict)
    document: Mapping[str, str] = field(default_factory=dict)

    #: Whether the comparison ran at all.
    #:
    #: `False` means the caller supplied no document extraction — the control did not execute. That
    #: is a different fact from "it executed and the document was unreadable", and conflating them
    #: was a real bug: every existing caller that predates this module suddenly routed to manual
    #: review, because an absent input read as a failed check.
    #:
    #: No verdict is raised when this is `False`, but it is reported in the audit record, so a
    #: pipeline that silently stopped passing the inputs is visible rather than merely quiet. The
    #: guarantee that the inputs *are* passed belongs in the state machine's own tests, not here.
    performed: bool = True

    document_readable: bool = True
    mrz_present: bool = True
    failed_check_digits: tuple[str, ...] = ()
    document_expired: bool | None = None

    @property
    def mismatched_fields(self) -> tuple[str, ...]:
        pairs = (("surname", self.surname), ("givenNames", self.given_names),
                 ("dateOfBirth", self.date_of_birth), ("nationality", self.nationality))
        return tuple(name for name, result in pairs if result is FieldResult.MISMATCH)

    @property
    def unverifiable_fields(self) -> tuple[str, ...]:
        pairs = (("surname", self.surname), ("givenNames", self.given_names),
                 ("dateOfBirth", self.date_of_birth), ("nationality", self.nationality))
        return tuple(name for name, result in pairs if result is FieldResult.UNVERIFIABLE)

    @property
    def identity_confirmed(self) -> bool:
        """True only when the comparison ran, every field was compared, and every one matched.

        Deliberately strict about `UNVERIFIABLE` and about `performed`: a field that could not be
        checked has not been confirmed, and reporting an unchecked identity as confirmed is the one
        reading that could open an account on evidence nobody looked at.
        """
        if not self.performed:
            return False
        return all(
            result is FieldResult.MATCH
            for result in (self.surname, self.given_names,
                           self.date_of_birth, self.nationality))

    def to_dict(self) -> dict[str, Any]:
        return {
            "performed": self.performed,
            "surname": self.surname.value,
            "givenNames": self.given_names.value,
            "dateOfBirth": self.date_of_birth.value,
            "nationality": self.nationality.value,
            "mismatchedFields": list(self.mismatched_fields),
            "unverifiableFields": list(self.unverifiable_fields),
            "identityConfirmed": self.identity_confirmed,
            "documentReadable": self.document_readable,
            "mrzPresent": self.mrz_present,
            "failedCheckDigits": list(self.failed_check_digits),
            "documentExpired": self.document_expired,
            # Both sides retained: a reviewer resolving a mismatch needs the two values, not just
            # the word "MISMATCH".
            "declared": dict(self.declared),
            "document": dict(self.document),
        }


def _unwrap(raw: Any) -> Any:
    """Accept the DynamoDB-typed shapes, a JSON string, or a plain object.

    The same tolerance `signals.from_findings` applies, and for the same reason: the state machine
    passes values straight off the case record, so a map arrives as `{"M": {...}}` and a JSON blob as
    a string, while a direct caller or a test passes the plain object. Handling all three here keeps
    that knowledge in one place instead of spreading DynamoDB shapes through the rules.
    """
    if isinstance(raw, Mapping):
        if "M" in raw and len(raw) == 1:
            return {k: _unwrap(v) for k, v in raw["M"].items()}
        if "S" in raw and len(raw) == 1:
            return _unwrap(raw["S"])
        if "NULL" in raw:
            return None
        return raw
    if isinstance(raw, str):
        stripped = raw.strip()
        # Only attempt a parse when it looks like JSON: a bare string field (a surname) must not be
        # mangled by a failed decode.
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return None
        return raw
    return raw


def compare(declared: Mapping[str, Any] | None,
            extraction: Mapping[str, Any] | None) -> IdentityComparison:
    """Compare the applicant's declared identity against the document's machine-readable zone.

    Absence of the *extraction* means the control did not run, and is reported as
    `performed=False` with no verdict. Absence of the declared identity while an extraction exists
    is a different thing — the document was read but there is nothing to check it against — and does
    produce a verdict.
    """
    declared = _unwrap(declared)
    extraction = _unwrap(extraction)
    if not isinstance(declared, Mapping):
        declared = {}
    if not isinstance(extraction, Mapping):
        extraction = {}

    performed = bool(extraction)
    mrz = extraction.get("mrz") or {}

    declared_values = {
        "surname": str(declared.get("surname") or ""),
        "givenNames": str(declared.get("givenNames") or ""),
        "dateOfBirth": str(declared.get("dateOfBirth") or ""),
        "nationality": str(declared.get("nationality") or ""),
    }
    document_values = {
        "surname": str(mrz.get("surname") or ""),
        "givenNames": str(mrz.get("givenNames") or ""),
        "dateOfBirth": str(mrz.get("dateOfBirth") or ""),
        "nationality": str(mrz.get("nationality") or ""),
    }

    return IdentityComparison(
        surname=_compare_names(declared_values["surname"], document_values["surname"]),
        given_names=_compare_names(declared_values["givenNames"], document_values["givenNames"]),
        date_of_birth=_compare_exact(
            declared_values["dateOfBirth"], document_values["dateOfBirth"]),
        nationality=_compare_country(
            declared_values["nationality"], document_values["nationality"]),
        declared=declared_values,
        document=document_values,
        performed=performed,
        document_readable=bool(extraction.get("documentReadable", False)),
        mrz_present=bool(extraction.get("mrzPresent", False)),
        failed_check_digits=tuple(mrz.get("failedCheckDigits") or ()),
        document_expired=mrz.get("expired"),
    )


def verdicts(comparison: IdentityComparison) -> tuple[EdgeVerdict, ...]:
    """Verdicts for `select_phase1_outcome`, in a fixed order so the audit record is stable.

    Every one is `MANUAL_REVIEW`. None is `REJECT`: see the module docstring — the innocent causes
    of each of these outnumber the guilty ones, and an automated rejection cannot be appealed from
    inside the flow.
    """
    out: list[EdgeVerdict] = []

    if not comparison.performed:
        # The control did not run, which is not the same as running and failing. Raising here made
        # every caller that predates this module route to manual review, because an absent input
        # read as a failed check. The absence is still recorded in the audit payload via
        # `performed: False`; the guarantee that the inputs are supplied belongs in the state
        # machine's tests, where it can be asserted, rather than being enforced by punishing every
        # caller that has no document to compare.
        return ()

    if comparison.mismatched_fields:
        fields = ", ".join(comparison.mismatched_fields)
        out.append(EdgeVerdict(
            case_id=f"{_CASE_PREFIX}-1",
            requirement=_REQUIREMENT,
            outcome=Phase1Outcome.MANUAL_REVIEW,
            reason_code="DECLARED_DOCUMENT_MISMATCH",
            explanation=(
                f"The details provided do not match the identity document for: {fields}. "
                "A reviewer will compare both and decide; this is not a rejection."
            ),
            signals={
                "mismatchedFields": list(comparison.mismatched_fields),
                "declared": dict(comparison.declared),
                "document": dict(comparison.document),
            },
        ))

    if comparison.failed_check_digits:
        out.append(EdgeVerdict(
            case_id=f"{_CASE_PREFIX}-2",
            requirement=_REQUIREMENT,
            outcome=Phase1Outcome.MANUAL_REVIEW,
            reason_code="DOCUMENT_CHECK_DIGIT_FAILED",
            explanation=(
                "The document's machine-readable zone is not internally consistent for: "
                f"{', '.join(comparison.failed_check_digits)}. This can indicate alteration, but "
                "a misread character produces the same result, so a reviewer confirms it."
            ),
            signals={"failedCheckDigits": list(comparison.failed_check_digits)},
        ))

    if not comparison.document_readable:
        # STEP_UP would be the tempting choice — ask for a better photo — but the applicant may
        # already have uploaded the best copy they have, and R30's step-up paths are for named
        # missing items. A reviewer can see the document and ask for the right thing.
        out.append(EdgeVerdict(
            case_id=f"{_CASE_PREFIX}-3",
            requirement=_REQUIREMENT,
            outcome=Phase1Outcome.MANUAL_REVIEW,
            reason_code="DOCUMENT_NOT_MACHINE_READABLE",
            explanation=(
                "The identity document could not be read automatically, so the details provided "
                "could not be confirmed against it."
            ),
            signals={"documentReadable": False},
        ))
    elif not comparison.mrz_present:
        out.append(EdgeVerdict(
            case_id=f"{_CASE_PREFIX}-4",
            requirement=_REQUIREMENT,
            outcome=Phase1Outcome.MANUAL_REVIEW,
            reason_code="NO_MACHINE_READABLE_ZONE",
            explanation=(
                "The document carries no machine-readable zone, so the details provided could not "
                "be verified against a checkable source."
            ),
            signals={"mrzPresent": False},
        ))
    elif len(comparison.unverifiable_fields) == 4:
        # The document was read successfully and carries an MRZ, yet nothing could be compared —
        # so the applicant declared nothing. That case cannot be screened either
        # (`sanctions_exact_match` refuses to run without a name), so it must reach a human rather
        # than proceed on a document alone.
        out.append(EdgeVerdict(
            case_id=f"{_CASE_PREFIX}-5",
            requirement=_REQUIREMENT,
            outcome=Phase1Outcome.MANUAL_REVIEW,
            reason_code="NO_DECLARED_IDENTITY",
            explanation=(
                "The identity document was read, but no applicant details were provided to check "
                "it against."
            ),
            signals={"unverifiableFields": list(comparison.unverifiable_fields)},
        ))

    return tuple(out)


def evaluate(declared: Mapping[str, Any] | None,
             extraction: Mapping[str, Any] | None
             ) -> tuple[IdentityComparison, tuple[EdgeVerdict, ...]]:
    """Compare and judge in one call, for callers that need both."""
    comparison = compare(declared, extraction)
    return comparison, verdicts(comparison)
