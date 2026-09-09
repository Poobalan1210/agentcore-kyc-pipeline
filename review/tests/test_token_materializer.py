"""Tests for `review/token_materializer.py` — the SQS consumer that puts the review task token
(and the phase it belongs to) onto the case record.

This module previously had NO tests at all, which is how the missing `phase` reached production:
the whole chain from `asl.py`'s handoff message to the reviewer console had no assertion anywhere
along it, and the console's `?? "phase1"` default meant nothing ever failed loudly.

The fake here is a low-level DynamoDB client, not a `Table` resource, because that is what the
module actually holds. A `Table`-shaped fake is what previously let four AttributeValue
serialization bugs through elsewhere in this repo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeDdb:
    """Records update_item calls verbatim so wrapping mistakes are visible."""

    def __init__(self, fail: bool = False) -> None:
        self.updates: list[dict[str, Any]] = []
        self.fail = fail

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("dynamodb unavailable")
        self.updates.append(kwargs)
        return {}


@pytest.fixture()
def materializer(monkeypatch):
    import token_materializer

    fake = FakeDdb()
    monkeypatch.setattr(token_materializer, "_ddb", fake)
    monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
    return token_materializer, fake


def _record(body: dict[str, Any], message_id: str = "m1") -> dict[str, Any]:
    return {"messageId": message_id, "body": json.dumps(body)}


class TestPhaseIsPersistedWithTheToken:
    """The phase and the token belong together: one names the waiting execution, the other names
    which decision slot that execution's review must be recorded under."""

    def test_the_phase_from_the_message_is_written(self, materializer):
        module, fake = materializer
        module.lambda_handler({"Records": [_record(
            {"applicantId": "APP-1", "caseId": "CASE-1",
             "taskToken": "tok", "phase": "phase2"})]})
        values = fake.updates[0]["ExpressionAttributeValues"]
        assert values[":p"] == {"S": "phase2"}

    def test_the_update_expression_sets_review_phase(self, materializer):
        """Asserted on the expression itself, not just the values: a value bound but never
        referenced in the SET clause writes nothing and would still satisfy a values-only check."""
        module, fake = materializer
        module.lambda_handler({"Records": [_record(
            {"applicantId": "APP-1", "taskToken": "tok", "phase": "phase2"})]})
        assert "reviewPhase = :p" in fake.updates[0]["UpdateExpression"]

    def test_a_message_without_a_phase_defaults_to_phase1(self, materializer):
        """Messages queued by an older definition must still materialise. Dropping them would
        strand a live execution on a token no reviewer can reach for seven days."""
        module, fake = materializer
        module.lambda_handler({"Records": [_record(
            {"applicantId": "APP-1", "taskToken": "tok"})]})
        assert fake.updates[0]["ExpressionAttributeValues"][":p"] == {"S": "phase1"}

    def test_the_token_is_still_written(self, materializer):
        """Guards against the phase change displacing what the module already did."""
        module, fake = materializer
        module.lambda_handler({"Records": [_record(
            {"applicantId": "APP-1", "taskToken": "tok", "phase": "phase1"})]})
        values = fake.updates[0]["ExpressionAttributeValues"]
        assert values[":t"] == {"S": "tok"}
        assert "reviewTaskToken = :t" in fake.updates[0]["UpdateExpression"]

    def test_keys_are_attribute_value_wrapped(self, materializer):
        """`_ddb` is the low-level client. Bare strings raise ParamValidationError — the same
        defect class that made every reviewer decision return 500 elsewhere in this repo."""
        module, fake = materializer
        module.lambda_handler({"Records": [_record(
            {"applicantId": "APP-1", "taskToken": "tok", "phase": "phase1"})]})
        assert fake.updates[0]["Key"] == {
            "applicantId": {"S": "APP-1"}, "sk": {"S": "CASE"}}


class TestBatchFailureHandling:
    """One bad message must not drop the others, and an unretryable message must not loop."""

    def test_a_write_failure_is_reported_for_redelivery(self, monkeypatch):
        import token_materializer

        monkeypatch.setattr(token_materializer, "_ddb", FakeDdb(fail=True))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        result = token_materializer.lambda_handler({"Records": [_record(
            {"applicantId": "APP-1", "taskToken": "tok", "phase": "phase1"}, "bad")]})
        assert result["batchItemFailures"] == [{"itemIdentifier": "bad"}]

    def test_an_unparseable_message_is_not_reported_as_a_failure(self, materializer):
        """Reporting it would redeliver until it reached the DLQ on every poll, and retrying
        cannot fix malformed JSON."""
        module, _ = materializer
        result = module.lambda_handler(
            {"Records": [{"messageId": "m1", "body": "not json"}]})
        assert result["batchItemFailures"] == []

    def test_a_message_with_no_token_is_skipped(self, materializer):
        """Not a review handoff. Nothing to materialise, and no write should be attempted."""
        module, fake = materializer
        result = module.lambda_handler({"Records": [_record(
            {"applicantId": "APP-1", "caseId": "CASE-1"})]})
        assert fake.updates == []
        assert result["batchItemFailures"] == []
