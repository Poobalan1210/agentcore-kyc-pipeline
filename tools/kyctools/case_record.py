"""The case record, from the tool side: read the inputs, record the facts.

This module is the tool half of "code decides on facts". Two rules:

**A tool reads its inputs from the case record.** The agent passes an `applicantId` and nothing it
can use to change what gets checked. It does not choose which name is screened, which document is
read, which face is compared or what threshold applies. A prompt-injected agent that wanted to
screen "John Doe" instead of the applicant can ask; the tool screens the applicant anyway.

**A tool records its result on the case record.** The rules then read what the tool recorded,
never what the agent said about it. The agent's reply is still useful — it is the explanation a
reviewer reads — but it is not the data interface. Measured in this build: an agent told a tool
was UNAVAILABLE replied "which means no match was found" (spike/sp.txt). Here that sentence is
harmless, because the rules read `performed: false` from the record and route to a human.

An agent pointed at the wrong applicantId records a fact on that other case, computed from that
case's own data, which is still true. The case it should have worked on simply lacks the fact, and
a missing fact routes to review. Neither outcome can approve anyone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .contract import ToolError, _now
from .data.countries import to_alpha2

#: Case-record attribute per fact. Flat attributes, never nested paths, for the reason recorded on
#: `FINDING_ATTRIBUTES`: a nested SET into a map that may not exist yet is rejected by DynamoDB at
#: invoke time, and concurrent branches must each own exactly one key.
FACT_ATTRIBUTES: dict[str, str] = {
    "document": "documentExtraction",
    "face": "factFaceMatch",
    "velocity": "factVelocity",
    "screening": "factScreening",
    "pep": "factPep",
    "adverse_media": "factAdverseMedia",
}


def table_name() -> str:
    name = os.environ.get("CASE_TABLE")
    if not name:
        # A configuration fault, not "no facts". Raising keeps it distinguishable from a case
        # that legitimately has nothing recorded.
        raise ToolError("CASE_TABLE is not configured", "NOT_CONFIGURED")
    return name


def aws_error_code(exc: BaseException) -> str:
    """The AWS error code, read structurally so fakes and real botocore errors behave alike."""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping) and error.get("Code"):
            return str(error["Code"])
    return type(exc).__name__


def from_attribute_value(value: Any) -> Any:
    """Low-level DynamoDB AttributeValue to plain Python. Numbers become int or float."""
    if not isinstance(value, Mapping) or len(value) != 1:
        return value
    (kind, inner), = value.items()
    if kind == "S":
        return inner
    if kind == "N":
        number = Decimal(inner)
        return int(number) if number == number.to_integral_value() else float(number)
    if kind == "BOOL":
        return bool(inner)
    if kind == "NULL":
        return None
    if kind == "M":
        return {k: from_attribute_value(v) for k, v in inner.items()}
    if kind == "L":
        return [from_attribute_value(v) for v in inner]
    if kind == "SS":
        return list(inner)
    if kind == "NS":
        return [from_attribute_value({"N": n}) for n in inner]
    return value


def load_case(applicant_id: str) -> dict[str, Any] | None:
    """Strongly consistent read of the case record, deserialised. None if there is no case."""
    import boto3

    ddb = boto3.client("dynamodb")
    try:
        response = ddb.get_item(
            TableName=table_name(),
            Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
            ConsistentRead=True,
        )
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - narrowed to a named, fail-closed outage
        raise ToolError("case store unreachable: %s" % aws_error_code(exc),
                        "CASE_STORE_UNAVAILABLE") from exc
    item = response.get("Item")
    if not item:
        return None
    return {k: from_attribute_value(v) for k, v in item.items()}


def parse_json_attribute(value: Any) -> Any:
    """Facts are stored as canonical JSON strings; tolerate an already-decoded value."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def record_fact(applicant_id: str, attribute: str, fact: Mapping[str, Any]) -> dict[str, Any]:
    """Write one fact to the case record. Raises `ToolError` if it cannot be recorded.

    Mandatory, not best effort. A fact the rules cannot read is a check that did not happen, so a
    failed write must surface as UNAVAILABLE rather than as a success the agent reports and the
    rules never see.

    Conditioned on the case existing, so a mistyped or injected applicantId cannot create a phantom
    case record that holds nothing but a clean-looking fact.
    """
    stored = dict(fact)
    stored.setdefault("recordedAt", _now())
    import boto3

    ddb = boto3.client("dynamodb")
    try:
        ddb.update_item(
            TableName=table_name(),
            Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
            UpdateExpression="SET #f = :f, updatedAt = :t",
            ConditionExpression="attribute_exists(applicantId)",
            ExpressionAttributeNames={"#f": attribute},
            ExpressionAttributeValues={
                ":f": {"S": json.dumps(stored, sort_keys=True, default=str)},
                ":t": {"S": stored["recordedAt"]},
            },
        )
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - see the docstring: an unrecorded fact is a failure
        raise ToolError("could not record %s: %s" % (attribute, aws_error_code(exc)),
                        "FACT_NOT_RECORDED") from exc
    return stored


def record_unavailable(applicant_id: str | None, attribute: str, error: ToolError) -> None:
    """Best-effort record that a check could not run, so a reviewer sees why, not just a gap.

    Best effort because the usual reason it is being called is that something is already down;
    the rules treat a missing fact exactly like this one, so losing it changes no outcome.
    """
    if not applicant_id:
        return
    try:
        record_fact(applicant_id, attribute, {
            "performed": False,
            "status": "UNAVAILABLE",
            "reasonCode": error.reason_code,
            "reason": error.reason,
        })
    except ToolError:
        pass


# --------------------------------------------------------------------- identity from the record
@dataclass(frozen=True)
class ScreeningIdentity:
    """The identity a screening tool screens: the applicant's own declaration, never the agent's.

    Declared rather than document-read, on purpose. Phase 1 runs the Documents and Pre-Screen
    agents in parallel, so the passport extraction may or may not exist yet when screening runs;
    screening whichever happened to be there would make the recorded fact differ between two runs
    of the same case. The declared identity is on the record before Phase 1 starts. The decision
    engine separately requires declared and document identity to agree before anything can be
    approved (`identity_match`), so screening one is screening both.
    """

    name: str
    dob: str | None
    country: str | None
    alternate_names: tuple[str, ...]
    source: str = "DECLARED"

    def to_dict(self) -> dict[str, Any]:
        return {"names": [self.name, *self.alternate_names], "dob": self.dob,
                "country": self.country, "identitySource": self.source}


def iso_date_or_none(value: Any) -> str | None:
    """A YYYY-MM-DD date, or None.

    Load-bearing, not tidiness. Two known dates of birth that differ *exclude* a screening match,
    so a declared "05/14/1991" compared as a string against a listed "1991-05-14" would exclude a
    true match. Anything that is not an unambiguous ISO date is treated as unknown, which can only
    ever send an alert to a human.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 10:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def screening_identity(case: Mapping[str, Any]) -> ScreeningIdentity:
    declared = case.get("declaredIdentity")
    declared = declared if isinstance(declared, Mapping) else {}
    given = str(declared.get("givenNames") or "").strip()
    surname = str(declared.get("surname") or "").strip()
    name = " ".join(part for part in (given, surname) if part)
    others = declared.get("otherNames") or ()
    if isinstance(others, str):
        others = [others]
    alternates = tuple(str(n).strip() for n in others if str(n).strip())
    return ScreeningIdentity(
        name=name,
        dob=iso_date_or_none(declared.get("dateOfBirth")),
        country=to_alpha2(declared.get("nationality")),
        alternate_names=alternates,
    )


def documents_on_case(case: Mapping[str, Any]) -> list[dict[str, str]]:
    docs = case.get("documents")
    if not isinstance(docs, list):
        return []
    out = []
    for doc in docs:
        if isinstance(doc, Mapping) and doc.get("bucket") and doc.get("key"):
            out.append({k: str(v) for k, v in doc.items() if v is not None})
    return out
