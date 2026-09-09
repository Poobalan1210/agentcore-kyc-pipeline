"""Review console API — task 14.3, R34.1, R34.5, R34.6."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

import pytest

import review_api


def _dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


class FakeDynamo:
    def __init__(self, query_items=None, get_item=None):
        self.query_items = query_items or []
        self.query_calls: list[dict[str, Any]] = []
        self._get_item = get_item
        self.get_item_calls: list[dict[str, Any]] = []

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return {"Items": self.query_items}

    def get_item(self, **kwargs):
        self.get_item_calls.append(kwargs)
        return {"Item": self._get_item} if self._get_item else {}


class FakeS3:
    def __init__(self, objects=None, last_modified=None):
        self.objects = objects or {}
        # Maps key -> a sortable stand-in for LastModified. Defaults to insertion order via an
        # incrementing counter so "most recent" tests do not need real datetimes.
        self._last_modified = dict(last_modified or {})
        for i, key in enumerate(self.objects):
            self._last_modified.setdefault(key, i)

    def list_objects_v2(self, **kwargs):
        prefix = kwargs["Prefix"]
        keys = [k for k in self.objects if k.startswith(prefix)]
        return {"Contents": [{"Key": k, "LastModified": self._last_modified[k]} for k in keys]}

    def get_object(self, **kwargs):
        class Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": Body(self.objects[kwargs["Key"]])}


class FakeStepFunctions:
    def __init__(self, *, description=None, events=None, raise_on_describe=None):
        self.description = description
        self.events = events or []
        self.raise_on_describe = raise_on_describe
        self.describe_calls: list[dict[str, Any]] = []
        self.history_calls: list[dict[str, Any]] = []

    def describe_execution(self, **kwargs):
        self.describe_calls.append(kwargs)
        if self.raise_on_describe:
            raise self.raise_on_describe
        return self.description

    def get_execution_history(self, **kwargs):
        self.history_calls.append(kwargs)
        return {"events": self.events}


class SfnClientError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeSqs:
    """Models the behaviour that matters: each `receive_message` returns at most 10 messages and
    the ones already returned become invisible, so a later call sees the *next* batch and
    eventually nothing. A fake that returned the same batch forever would hide the exact bug the
    multi-round peek exists to fix — a case sitting behind a backlog and never getting its token."""

    def __init__(self, messages=None):
        self.pending = list(messages or [])
        self.delete_calls: list[dict[str, Any]] = []
        self.receive_calls = 0

    def receive_message(self, **kwargs):
        self.receive_calls += 1
        limit = int(kwargs.get("MaxNumberOfMessages", 10))
        batch = self.pending[:limit]
        self.pending = self.pending[limit:]
        return {"Messages": batch} if batch else {}

    def delete_message(self, **kwargs):
        self.delete_calls.append(kwargs)
        return {}


class FakeBoto3Module:
    def __init__(self, ddb=None, s3=None, sqs=None, sfn=None):
        self._ddb = ddb
        self._s3 = s3
        self._sqs = sqs
        self._sfn = sfn

    def client(self, name):
        return {"dynamodb": self._ddb, "s3": self._s3, "sqs": self._sqs,
                "states": self._sfn}[name]


REVIEWER_EVENT_CONTEXT = {
    "requestContext": {"authorizer": {"jwt": {"claims": {
        "sub": "reviewer-1", "cognito:groups": ["reviewers"]}}}},
}
APPLICANT_EVENT_CONTEXT = {
    "requestContext": {"authorizer": {"jwt": {"claims": {
        "sub": "applicant-1", "cognito:groups": ["applicants"]}}}},
}


class TestListQueue:
    def test_non_reviewer_is_forbidden(self):
        event = dict(APPLICANT_EVENT_CONTEXT)
        resp = review_api.list_queue(event)
        assert resp["statusCode"] == 403

    def test_reviewer_sees_the_queue_with_depth(self, monkeypatch):
        items = [
            {"applicantId": {"S": "APP-1"}, "caseId": {"S": "CASE-1"},
             "jurisdiction": {"S": "us"}, "createdAt": {"S": "2026-08-01T00:00:00Z"}},
        ]
        fake_ddb = FakeDynamo(query_items=items)
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        resp = review_api.list_queue(event)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["depth"] == 1
        assert body["cases"][0]["applicantId"] == "APP-1"

    def test_queries_ascending_by_age_oldest_first(self, monkeypatch):
        """R34.1 — ordered by age."""
        fake_ddb = FakeDynamo(query_items=[])
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        review_api.list_queue(event)
        assert fake_ddb.query_calls[0]["ScanIndexForward"] is True
        assert fake_ddb.query_calls[0]["IndexName"] == "state-index"

    def test_merges_the_task_token_from_the_review_sqs_queue(self, monkeypatch):
        """design §7.5 — the console cannot submit a decision without the task token.

        It is read from the case record, written once by `review/token_materializer.py` when the
        review was handed off. It used to be discovered with `ReceiveMessage` on every request,
        which cannot work: each read spends the message's finite redrive budget
        (`maxReceiveCount: 5`), so the sixth *view* of a case moved a LIVE token to the DLQ —
        observed twice against the deployed stack, five waiting cases disappearing from the
        reviewer's queue while their executions stayed alive.
        """
        items = [
            {"applicantId": {"S": "APP-1"}, "caseId": {"S": "CASE-1"},
             "jurisdiction": {"S": "us"}, "createdAt": {"S": "2026-08-01T00:00:00Z"},
             "reviewTaskToken": {"S": "token-abc"}},
        ]
        fake_ddb = FakeDynamo(query_items=items)
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        resp = review_api.list_queue(dict(REVIEWER_EVENT_CONTEXT))
        assert json.loads(resp["body"])["cases"][0]["taskToken"] == "token-abc"

    def test_case_without_a_materialised_token_omits_it(self, monkeypatch):
        """Absent must be absent, not empty string — the console branches on presence to decide
        between an actionable case, a pending one, and one whose review window has expired."""
        items = [{"applicantId": {"S": "APP-1"}, "caseId": {"S": "CASE-1"},
                  "jurisdiction": {"S": "us"}, "createdAt": {"S": "2026-08-01T00:00:00Z"}}]
        monkeypatch.setitem(sys.modules, "boto3",
                            FakeBoto3Module(ddb=FakeDynamo(query_items=items)))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        resp = review_api.list_queue(dict(REVIEWER_EVENT_CONTEXT))
        assert "taskToken" not in json.loads(resp["body"])["cases"][0]

    def test_projects_the_review_phase_so_a_phase2_review_is_not_filed_as_phase1(
            self, monkeypatch):
        """The queue MUST tell the console which phase the review belongs to.

        This projection was missing entirely. `QueuedCaseSummary` declares `phase?: string` and
        `DecisionForm.tsx` does `phase: queuedCase.phase ?? "phase1"`, so its absence did not fail
        loudly -- it made EVERY console review record as `phase1Review`, including Phase 2
        compliance reviews, and `phase2Review` could never be written by any console submission.
        Worse than the wrong audit label: `existing_review(case, phase)` and
        `preliminary_tier(case, phase)` then inspect Phase 1's slot, so the first-writer-wins guard
        for a Phase 2 review checks the wrong decision.

        An optional type plus a `??` default is why 1000 passing tests never noticed.
        """
        items = [
            {"applicantId": {"S": "APP-1"}, "caseId": {"S": "CASE-1"},
             "jurisdiction": {"S": "us"}, "createdAt": {"S": "2026-08-01T00:00:00Z"},
             "reviewTaskToken": {"S": "token-abc"}, "reviewPhase": {"S": "phase2"}},
        ]
        monkeypatch.setitem(sys.modules, "boto3",
                            FakeBoto3Module(ddb=FakeDynamo(query_items=items)))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        resp = review_api.list_queue(dict(REVIEWER_EVENT_CONTEXT))
        assert json.loads(resp["body"])["cases"][0]["phase"] == "phase2"

    def test_a_case_with_no_recorded_phase_defaults_to_phase1(self, monkeypatch):
        """Messages queued before `asl.py` carried the phase have no `reviewPhase`.

        Those are Phase 1 handoffs in practice, and defaulting keeps them actionable rather than
        submitting `phase: null` which `decisions.py` would reject as UNKNOWN_PHASE. The default
        lives here, once, instead of being re-derived in the console.
        """
        items = [{"applicantId": {"S": "APP-1"}, "caseId": {"S": "CASE-1"},
                  "jurisdiction": {"S": "us"}, "createdAt": {"S": "2026-08-01T00:00:00Z"}}]
        monkeypatch.setitem(sys.modules, "boto3",
                            FakeBoto3Module(ddb=FakeDynamo(query_items=items)))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        resp = review_api.list_queue(dict(REVIEWER_EVENT_CONTEXT))
        assert json.loads(resp["body"])["cases"][0]["phase"] == "phase1"

    def test_listing_the_queue_never_reads_from_sqs(self, monkeypatch):
        """The whole point of moving the token: listing must have no side effect on the message.

        A read that consumes redrive budget is not a read. This asserts the endpoint has no path
        back to `ReceiveMessage`, so the regression cannot be reintroduced quietly.
        """
        items = [{"applicantId": {"S": "APP-1"}, "caseId": {"S": "CASE-1"},
                  "jurisdiction": {"S": "us"}, "createdAt": {"S": "2026-08-01T00:00:00Z"},
                  "reviewTaskToken": {"S": "token-abc"}}]
        fake_sqs = FakeSqs(messages=[])
        monkeypatch.setitem(
            sys.modules, "boto3",
            FakeBoto3Module(ddb=FakeDynamo(query_items=items), sqs=fake_sqs))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("REVIEW_QUEUE_URL",
                           "https://sqs.us-east-1.amazonaws.com/123/kyc90-review")
        review_api.list_queue(dict(REVIEWER_EVENT_CONTEXT))
        assert getattr(fake_sqs, "receive_calls", 0) == 0, (
            "listing the queue must not call ReceiveMessage: every receive spends redrive budget "
            "and can send a live task token to the dead-letter queue"
        )


class TestSubmitDecision:
    def test_non_reviewer_is_forbidden(self):
        event = dict(APPLICANT_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-1"}
        event["body"] = json.dumps({})
        resp = review_api.submit_decision(event)
        assert resp["statusCode"] == 403


CASE_ITEM = {
    "applicantId": {"S": "APP-1"},
    "caseId": {"S": "CASE-APP-1"},
    "state": {"S": "AWAITING_REVIEW"},
    "jurisdiction": {"S": "us"},
    "productApplied": {"S": "current_account"},
    "createdAt": {"S": "2026-08-10T19:00:00Z"},
    "documents": {"L": [{"M": {
        "type": {"S": "PASSPORT"}, "key": {"S": "APP-1/PASSPORT-x"},
        "bucket": {"S": "kyc90-evidence-us"}, "issuingCountry": {"S": "US"},
    }}]},
    "declaredIdentity": {"M": {
        "surname": {"S": "REEVES"}, "givenNames": {"S": "TAYLOR JORDAN"},
        "dateOfBirth": {"S": "1991-05-14"}, "nationality": {"S": "USA"},
    }},
    "liveness": {"M": {
        "result": {"S": "PASS"}, "region": {"S": "us-east-1"},
        "retrievedAt": {"S": "2026-08-10T19:00:05Z"},
        "referenceImageKey": {"S": "APP-1/reference-x.jpg"},
        "auditImageKeys": {"L": []},
    }},
    "findingDocuments": {"S": json.dumps({"documentConfidence": 0.95, "expired": False})},
    "findingIdentity": {"S": json.dumps({"match": True, "similarityBand": "HIGH"})},
    "documentExtraction": {"S": json.dumps({
        "documentReadable": True, "mrzPresent": True,
        "mrz": {"surname": "REEVES", "checkDigitsValid": True},
    })},
}

AUDIT_RECORD = json.dumps({
    "traceId": "exec-name-123",
    "outcome": "MANUAL_REVIEW",
    "reasonCode": "DECLARED_DOCUMENT_MISMATCH",
    "identityMatch": {"identityConfirmed": False, "mismatchedFields": ["dateOfBirth"]},
})

EXECUTION_DESCRIPTION = {
    "status": "SUCCEEDED",
    "startDate": _dt(2026, 8, 10, 19, 0, 0),
    "stopDate": _dt(2026, 8, 10, 19, 0, 8),
}

EXECUTION_EVENTS = [
    {"type": "TaskStateEntered", "timestamp": _dt(2026, 8, 10, 19, 0, 1),
     "stateEnteredEventDetails": {"name": "Documents"}},
    {"type": "TaskStateExited", "timestamp": _dt(2026, 8, 10, 19, 0, 3),
     "stateExitedEventDetails": {"name": "Documents"}},
    {"type": "TaskStateEntered", "timestamp": _dt(2026, 8, 10, 19, 0, 3),
     "stateEnteredEventDetails": {"name": "PreliminaryTier"}},
    {"type": "TaskStateExited", "timestamp": _dt(2026, 8, 10, 19, 0, 4),
     "stateExitedEventDetails": {"name": "PreliminaryTier"}},
]


class TestGetTrace:
    def test_non_reviewer_is_forbidden(self):
        resp = review_api.get_trace(dict(APPLICANT_EVENT_CONTEXT))
        assert resp["statusCode"] == 403

    def test_missing_case_id_is_a_400(self):
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {}
        assert review_api.get_trace(event)["statusCode"] == 400

    def test_unknown_case_is_a_404(self, monkeypatch):
        fake_ddb = FakeDynamo(get_item=None)
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-NOPE"}
        assert review_api.get_trace(event)["statusCode"] == 404

    def test_case_id_is_recovered_to_the_applicant_id_key(self, monkeypatch):
        """The table's partition key is applicantId, not caseId — `CASE-` is stripped to derive
        it rather than requiring a second index for a fixed, reversible transform."""
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        monkeypatch.setitem(
            sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=FakeS3()))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        review_api.get_trace(event)
        assert fake_ddb.get_item_calls[0]["Key"]["applicantId"] == {"S": "APP-1"}

    def test_returns_applicant_documents_and_liveness_summary(self, monkeypatch):
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        monkeypatch.setitem(
            sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=FakeS3()))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        body = json.loads(review_api.get_trace(event)["body"])

        assert body["applicant"]["state"] == "AWAITING_REVIEW"
        assert body["applicant"]["declaredIdentity"]["surname"] == "REEVES"
        assert body["applicant"]["liveness"]["result"] == "PASS"
        assert body["applicant"]["documents"] == [{"type": "PASSPORT", "issuingCountry": "US"}]
        # Never the S3 location — that belongs to /audit/{id}, not this summary view.
        assert "bucket" not in body["applicant"]["documents"][0]
        assert "key" not in body["applicant"]["documents"][0]

    def test_liveness_never_exposes_the_image_keys(self, monkeypatch):
        """R36's minimisation intent extends to what is displayed, not only what is stored."""
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        monkeypatch.setitem(
            sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=FakeS3()))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        body = json.loads(review_api.get_trace(event)["body"])
        assert "referenceImageKey" not in body["applicant"]["liveness"]
        assert "auditImageKeys" not in body["applicant"]["liveness"]

    def test_returns_each_agents_structured_finding(self, monkeypatch):
        """Findings are the output contracts' JSON, never prose — labelled per agent so the
        reviewer sees them as inputs, not as the decision itself (R18.2)."""
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        monkeypatch.setitem(
            sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=FakeS3()))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        body = json.loads(review_api.get_trace(event)["body"])

        by_agent = {a["agent"]: a["finding"] for a in body["agents"]}
        assert by_agent["documents"]["expired"] is False
        assert by_agent["identity"]["similarityBand"] == "HIGH"
        # Agents that never recorded a finding are absent, not present-with-null — a reviewer
        # should not have to distinguish "no finding" from "null finding".
        assert "prescreen" not in by_agent

    def test_returns_the_document_extraction_including_mrz(self, monkeypatch):
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        monkeypatch.setitem(
            sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=FakeS3()))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        body = json.loads(review_api.get_trace(event)["body"])
        assert body["documentExtraction"]["mrz"]["checkDigitsValid"] is True

    def test_returns_the_latest_decision_record_with_identity_match(self, monkeypatch):
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        fake_s3 = FakeS3(objects={"APP-1/phase1/dec-1.json": AUDIT_RECORD.encode()})
        monkeypatch.setitem(
            sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=fake_s3))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        body = json.loads(review_api.get_trace(event)["body"])

        assert body["decision"]["outcome"] == "MANUAL_REVIEW"
        assert body["decision"]["identityMatch"]["mismatchedFields"] == ["dateOfBirth"]

    def test_picks_the_most_recently_modified_decision_record(self, monkeypatch):
        older = json.dumps({"traceId": "old", "outcome": "PROVISIONAL_APPROVE_RESTRICTED"})
        newer = json.dumps({"traceId": "new", "outcome": "MANUAL_REVIEW"})
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        fake_s3 = FakeS3(
            objects={"APP-1/phase1/dec-1.json": older.encode(),
                    "APP-1/phase2/dec-2.json": newer.encode()},
            last_modified={"APP-1/phase1/dec-1.json": 1, "APP-1/phase2/dec-2.json": 2})
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=fake_s3))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        body = json.loads(review_api.get_trace(event)["body"])
        assert body["decision"]["outcome"] == "MANUAL_REVIEW"

    def test_an_uploaded_document_under_the_same_prefix_is_not_mistaken_for_a_decision(
        self, monkeypatch
    ):
        """Found live: uploaded documents land at `{applicantId}/{docType}-{hex}` in the SAME
        bucket the decision record uses, with no `.json` suffix and non-UTF-8 image bytes. Listing
        the bare `{applicantId}/` prefix picked up the document and `json.loads` on the image bytes
        raised `UnicodeDecodeError`, which crashed the whole trace view for a case that actually had
        a perfectly good decision record sitting right next to it."""
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        fake_s3 = FakeS3(objects={
            "APP-1/PASSPORT-0d94498c": b"\xff\xd8\xff\xe0\x00\x10JFIF\xb0\x00binary-jpeg-bytes",
            "APP-1/phase1/dec-1.json": AUDIT_RECORD.encode(),
        })
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=fake_s3))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}

        resp = review_api.get_trace(event)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["decision"]["outcome"] == "MANUAL_REVIEW"

    def test_a_malformed_json_object_degrades_gracefully_not_a_500(self, monkeypatch):
        """Belt and suspenders alongside the `.json` suffix filter: even a `.json`-named object
        that fails to parse must leave the rest of the trace view intact."""
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        fake_s3 = FakeS3(objects={"APP-1/phase1/dec-1.json": b"{not valid json"})
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=fake_s3))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}

        resp = review_api.get_trace(event)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["decision"] is None
        assert body["applicant"]["state"] == "AWAITING_REVIEW"

    def test_falls_back_to_raw_evidence_when_no_agent_recorded_a_structured_finding(
        self, monkeypatch
    ):
        """Found live: an agent can complete without failing and never call its `submit_*`
        output contract at all — its reasoning lands only in `phase1Evidence` as raw assistant
        text. Without this fallback the reviewer sees "no agent has recorded a finding" for a
        case that visibly produced agent output, just not in the structured shape."""
        item = dict(CASE_ITEM)
        item.pop("findingDocuments", None)
        item.pop("findingIdentity", None)
        item["phase1Evidence"] = {"S": json.dumps([
            {"agent": "documents",
             "content": [{"Text": "<thinking>...</thinking>\n{\"determined\": true}"}],
             "status": "OK", "stopReason": "end_turn"},
            {"agent": "identity",
             "content": [{"Text": "no reference frame available"}],
             "status": "OK", "stopReason": "end_turn"},
        ])}
        fake_ddb = FakeDynamo(get_item=item)
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=FakeS3()))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}

        body = json.loads(review_api.get_trace(event)["body"])
        by_agent = {a["agent"]: a["finding"] for a in body["agents"]}
        assert by_agent["documents"]["raw"] is True
        assert "<thinking>" in by_agent["documents"]["text"]
        assert "no reference frame available" in by_agent["identity"]["text"]

    def test_structured_findings_take_priority_over_the_raw_fallback(self, monkeypatch):
        """When even one agent recorded a proper `submit_*` finding, the fallback must not run at
        all — mixing structured and raw would make it unclear to a reviewer which is which."""
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)  # has findingDocuments/findingIdentity
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=FakeS3()))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        body = json.loads(review_api.get_trace(event)["body"])
        assert all(a["finding"].get("raw") is not True for a in body["agents"])

    def test_no_decision_record_yet_is_not_an_error(self, monkeypatch):
        """A case awaiting its first Phase 1 pass has no audit object yet. The rest of the trace
        (applicant, findings) is still valid, and `decision`/`execution` are simply absent."""
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        monkeypatch.setitem(
            sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=FakeS3()))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        resp = review_api.get_trace(event)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["decision"] is None
        assert body["execution"] is None

    def test_returns_the_execution_timeline_when_a_trace_id_is_known(self, monkeypatch):
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        fake_s3 = FakeS3(objects={"APP-1/phase1/dec-1.json": AUDIT_RECORD.encode()})
        fake_sfn = FakeStepFunctions(description=EXECUTION_DESCRIPTION, events=EXECUTION_EVENTS)
        monkeypatch.setitem(
            sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=fake_s3, sfn=fake_sfn))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        monkeypatch.setenv(
            "PHASE1_STATE_MACHINE_ARN",
            "arn:aws:states:us-east-1:633898796092:stateMachine:kyc90-phase1")
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("AWS_ACCOUNT_ID", "633898796092")

        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        body = json.loads(review_api.get_trace(event)["body"])

        assert body["execution"]["status"] == "SUCCEEDED"
        step_names = [s["name"] for s in body["execution"]["steps"]]
        assert step_names == ["Documents", "PreliminaryTier"]
        assert fake_sfn.describe_calls[0]["executionArn"].endswith(":exec-name-123")

    def test_an_expired_execution_history_leaves_the_rest_of_the_trace_intact(self, monkeypatch):
        """Step Functions' own history retention can outlive this lookup, or the decision may
        predate `traceId` being recorded. Either way `agents`/`applicant`/`decision` remain valid;
        only `execution` becomes absent — this must not turn into a 500."""
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        fake_s3 = FakeS3(objects={"APP-1/phase1/dec-1.json": AUDIT_RECORD.encode()})
        fake_sfn = FakeStepFunctions(raise_on_describe=SfnClientError("ExecutionDoesNotExist"))
        monkeypatch.setitem(
            sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=fake_s3, sfn=fake_sfn))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        monkeypatch.setenv(
            "PHASE1_STATE_MACHINE_ARN",
            "arn:aws:states:us-east-1:633898796092:stateMachine:kyc90-phase1")

        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        resp = review_api.get_trace(event)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["execution"] is None
        assert body["decision"]["outcome"] == "MANUAL_REVIEW"

    def test_no_raw_confidence_score_is_ever_returned(self, monkeypatch):
        """F-44/R33.2 — this route reads the same bucketed values the applicant-facing route
        does, nothing rawer, even though it is reviewer-only."""
        fake_ddb = FakeDynamo(get_item=CASE_ITEM)
        fake_s3 = FakeS3(objects={"APP-1/phase1/dec-1.json": AUDIT_RECORD.encode()})
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(ddb=fake_ddb, s3=fake_s3))
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"caseId": "CASE-APP-1"}
        raw = review_api.get_trace(event)["body"]
        assert "similarity\":" not in raw.lower()
        assert "\"97" not in raw and "\"95" not in raw


class TestGetAudit:
    def test_non_reviewer_is_forbidden(self):
        event = dict(APPLICANT_EVENT_CONTEXT)
        event["pathParameters"] = {"applicantId": "APP-1"}
        resp = review_api.get_audit(event)
        assert resp["statusCode"] == 403

    def test_lists_decisions_under_the_applicants_own_prefix(self, monkeypatch):
        """Matches record.py's decision_object_key layout: {applicantId}/{phase}/{id}.json."""
        record = {"decisionId": "DEC-1", "outcome": "PROVISIONAL_APPROVE_RESTRICTED"}
        fake_s3 = FakeS3(objects={
            "APP-1/phase1/DEC-1.json": json.dumps(record).encode(),
        })
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(s3=fake_s3))
        monkeypatch.setenv("AUDIT_BUCKET_NAME", "kyc90-evidence-us")
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {"applicantId": "APP-1"}
        resp = review_api.get_audit(event)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["decisions"] == [record]

    def test_missing_applicant_id_is_a_400(self):
        event = dict(REVIEWER_EVENT_CONTEXT)
        event["pathParameters"] = {}
        resp = review_api.get_audit(event)
        assert resp["statusCode"] == 400


class TestPeekDoesNotBurnRedriveBudget:
    """A queue peek must not spend the message's redrive budget or delete a pending review.

    Reading a token uses `ReceiveMessage`, which increments ApproximateReceiveCount, and the review
    queue carries `maxReceiveCount: 5`. So "peeking" is not free: the sixth read of a case moves a
    LIVE Step Functions task token to the DLQ. Proven live — releasing visibility to 0 removed the
    incidental rate limit and about five console polls redrove all five waiting cases to
    kyc90-review-dlq while their executions stayed alive.

    These tests pin the two properties that keep a live token where a reviewer can reach it.
    """

    class _FakeSqs:
        def __init__(self, batches):
            self.batches = list(batches)
            self.released = []
            self.deleted = []
            self.receives = 0

        def receive_message(self, **kw):
            self.receives += 1
            if not self.batches:
                return {}
            return {"Messages": self.batches.pop(0)}

        def change_message_visibility(self, *, QueueUrl, ReceiptHandle, VisibilityTimeout):
            self.released.append((ReceiptHandle, VisibilityTimeout))

        def delete_message(self, **kw):
            self.deleted.append(kw)

    def _msg(self, applicant, handle):
        import json
        return {"ReceiptHandle": handle,
                "Body": json.dumps({"applicantId": applicant, "taskToken": "tok-" + applicant})}

    def test_reads_the_tokens(self):
        from review_api import _peek_task_tokens
        sqs = self._FakeSqs([[self._msg("APP-1", "h1"), self._msg("APP-2", "h2")]])
        assert _peek_task_tokens(sqs, "q") == {"APP-1": "tok-APP-1", "APP-2": "tok-APP-2"}

    def test_does_not_release_visibility(self):
        """Releasing removes the only throttle on a finite redrive budget (maxReceiveCount=5)."""
        from review_api import _peek_task_tokens
        sqs = self._FakeSqs([[self._msg("APP-1", "h1")]])
        _peek_task_tokens(sqs, "q")
        assert sqs.released == [], (
            "must not reset visibility: each receive spends redrive budget, and releasing lets "
            "console polling redrive live task tokens to the DLQ"
        )

    def test_never_deletes_a_pending_review(self):
        from review_api import _peek_task_tokens
        sqs = self._FakeSqs([[self._msg("APP-1", "h1")]])
        _peek_task_tokens(sqs, "q")
        assert sqs.deleted == [], "deleting would discard a live task token"

    def test_stops_receiving_once_the_queue_is_drained(self):
        """Bounded receives per request:every extra round spends redrive budget for nothing."""
        from review_api import _peek_task_tokens
        sqs = self._FakeSqs([[self._msg("APP-1", "h1")]])
        _peek_task_tokens(sqs, "q")
        assert sqs.receives == 2, "one round with messages, one empty round to detect the end"
