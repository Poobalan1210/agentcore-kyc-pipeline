"""Path setup and shared fakes for the review suite, mirroring `audit/tests/conftest.py`.

The repo pytest.ini runs `--import-mode=importlib` so that the several directories named `tests`
stop shadowing each other. That mode keys modules by full path and adds neither the test directory
nor its parent to `sys.path`, so `from decisions import ...` would not resolve on its own.
Inserting the review package root as well as this directory keeps the suite runnable on its own,
independently of the pytest.ini `pythonpath` entry.

The fakes here exist because the receiver's whole job is ordering three AWS effects. A mock that
records calls would let a wrong order pass; these fakes hold state, so a `SendTaskSuccess` issued
before a losing write shows up as a wrong *result*, not just a wrong call log.
"""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_here = Path(__file__).resolve().parent
for path in (_here, _here.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from decisions import ReviewAction, ReviewSubmission, TableKeySchema  # noqa: E402

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
TABLE = "kyc90-cases"
KEYS = TableKeySchema()


class ConditionalCheckFailed(Exception):
    """Shaped like botocore's error so the receiver's own code path is exercised, not a stub of it."""

    def __init__(self, item: dict[str, Any] | None = None):
        super().__init__("The conditional request failed")
        self.response: dict[str, Any] = {
            "Error": {"Code": "ConditionalCheckFailedException", "Message": "failed"}}
        if item is not None:
            self.response["Item"] = item


class AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}



def _wire(value: Any) -> Any:
    """Plain -> AttributeValue, for what the fake hands back to the code under test."""
    from decisions import to_attribute_value
    if isinstance(value, dict):
        return {k: to_attribute_value(v) for k, v in value.items()}
    return to_attribute_value(value)


def _plain(value: Any) -> Any:
    """AttributeValue -> plain, for what the fake receives and then stores."""
    from review_handler import deserialize_item
    return deserialize_item({"v": value})["v"]


class FakeDynamo:
    """A one-item store that honours exactly the two condition clauses the write depends on.

    Deliberately not a general expression evaluator: it enforces `attribute_not_exists` on the phase
    review path and equality on `state`, because those are the clauses R6.5 rests on. Anything more
    would be a second implementation to keep correct.

    **Speaks the low-level client's wire format at its boundaries**, because that is what the code
    under test talks to. It previously accepted and returned plain Python, which is the `Table`
    resource's shape — so the suite could not see that the real handler passed plain values to a
    client that rejects them. Three production bugs hid behind exactly that gap: a 500 on
    `Key.applicantId`, a 500 on `ExpressionAttributeValues.:target`, and a spurious 422
    `CASE_MISMATCH` from comparing a caseId against `"{'S': 'CASE-...'}"`. All three were invisible
    here and unreachable in production, because no reviewer could obtain a task token to submit a
    decision at all.

    The internal store stays plain so tests can assert on readable values; only the get/update
    boundaries convert.
    """

    def __init__(self, item: dict[str, Any] | None = None):
        self.item = copy.deepcopy(item) if item else None
        self.get_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        #: Set to raise on the next update, to simulate losing the race mid-flight.
        self.fail_next_update_with: Exception | None = None

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        for name, value in (kwargs.get("Key") or {}).items():
            assert isinstance(value, dict) and len(value) == 1, (
                f"Key.{name} must be an AttributeValue like {{'S': ...}}, got {value!r} — "
                f"the low-level client raises ParamValidationError on a bare value"
            )
        if not self.item:
            return {}
        return {"Item": _wire(copy.deepcopy(self.item))}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self.update_calls.append(kwargs)
        if self.fail_next_update_with is not None:
            error = self.fail_next_update_with
            self.fail_next_update_with = None
            raise error

        names = kwargs["ExpressionAttributeNames"]
        for name, value in (kwargs.get("ExpressionAttributeValues") or {}).items():
            assert isinstance(value, dict) and len(value) == 1, (
                f"ExpressionAttributeValues.{name} must be an AttributeValue, got {value!r}"
            )
        values = {name: _plain(value)
                  for name, value in kwargs["ExpressionAttributeValues"].items()}
        phase = names["#phase"]
        # The decision lands on a SIBLING attribute (`phase1Review`), because `asl.py` stores the
        # phase blob as a JSON string and a nested SET into a string is an invalid document path.
        review_attr = names.get("#reviewAttr", f"{phase}Review")
        item = self.item

        if item is None or phase not in item:
            raise ConditionalCheckFailed()
        if item.get(review_attr):
            raise ConditionalCheckFailed(item=_wire(copy.deepcopy(item)))
        if item.get("state") != values[":awaiting"]:
            raise ConditionalCheckFailed(item=_wire(copy.deepcopy(item)))

        item[review_attr] = copy.deepcopy(values[":decision"])
        item["state"] = values[":target"]
        item.setdefault("transitions", [])
        item["transitions"].extend(copy.deepcopy(values[":transition"]))
        return {}


class FakeStepFunctions:
    def __init__(self, *, fail_with: Exception | None = None):
        self.successes: list[dict[str, Any]] = []
        self.failures: list[dict[str, Any]] = []
        self.fail_with = fail_with

    def send_task_success(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        self.successes.append(kwargs)
        return {}

    def send_task_failure(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail_with is not None:
            raise self.fail_with
        self.failures.append(kwargs)
        return {}


class FakeCloudWatch:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {}


def case_record(
    *,
    state: str = "AWAITING_REVIEW",
    phase: str = "phase1",
    tier: str = "HIGH",
    review: dict[str, Any] | None = None,
    case_id: str = "CASE-001",
    applicant_id: str = "APP-001",
) -> dict[str, Any]:
    """A case record shaped like design §9.1, trimmed to what the review path reads."""
    block: dict[str, Any] = {"preliminaryTier": tier, "outcome": "MANUAL_REVIEW"}
    if review is not None:
        block["review"] = review
    return {
        "applicantId": applicant_id,
        "caseId": case_id,
        "state": state,
        "jurisdiction": "UK",
        phase: block,
        "transitions": [],
    }


def submission(
    *,
    action: ReviewAction | Any = ReviewAction.APPROVE,
    reviewer_id: str = "reviewer-a",
    rationale: str = "false positive on name similarity",
    phase: str = "phase1",
    case_id: str = "CASE-001",
    applicant_id: str = "APP-001",
    task_token: str = "token-1",
    submission_id: str = "req-1",
    submitted_at: datetime = NOW,
) -> ReviewSubmission:
    return ReviewSubmission(
        case_id=case_id,
        applicant_id=applicant_id,
        phase=phase,
        reviewer_id=reviewer_id,
        action=action,
        rationale=rationale,
        submitted_at=submitted_at,
        task_token=task_token,
        submission_id=submission_id,
    )


def api_event(
    *,
    action: str = "APPROVE",
    reviewer_sub: str | None = "reviewer-a",
    body_reviewer_id: str | None = None,
    rationale: str = "false positive on name similarity",
    phase: str = "phase1",
    case_id: str = "CASE-001",
    applicant_id: str = "APP-001",
    task_token: str = "token-1",
    request_id: str = "req-1",
    decided_at: str | None = "2026-08-09T12:00:00Z",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "caseId": case_id,
        "applicantId": applicant_id,
        "phase": phase,
        "action": action,
        "rationale": rationale,
        "taskToken": task_token,
    }
    if body_reviewer_id is not None:
        body["reviewerId"] = body_reviewer_id
    if decided_at is not None:
        body["decidedAt"] = decided_at
    context: dict[str, Any] = {"requestId": request_id}
    if reviewer_sub is not None:
        context["authorizer"] = {"claims": {"sub": reviewer_sub}}
    return {"body": body, "requestContext": context,
            "pathParameters": {"caseId": case_id}}


@pytest.fixture
def dynamo() -> FakeDynamo:
    return FakeDynamo(case_record())


@pytest.fixture
def sfn() -> FakeStepFunctions:
    return FakeStepFunctions()


@pytest.fixture
def cloudwatch() -> FakeCloudWatch:
    return FakeCloudWatch()
