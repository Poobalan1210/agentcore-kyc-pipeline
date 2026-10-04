"""Liveness API — task 14.2, R33.1-R33.6, R30.2, F-40, F-41, F-44."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

import applicants
import liveness


class FakeRekognition:
    def __init__(self, *, session_id="sess-1", confidence=95.0, raise_error=None):
        self.session_id = session_id
        self.confidence = confidence
        self.raise_error = raise_error
        self.create_calls: list[dict[str, Any]] = []
        self.result_calls: list[dict[str, Any]] = []

    def create_face_liveness_session(self, **kwargs):
        self.create_calls.append(kwargs)
        return {"SessionId": self.session_id}

    def get_face_liveness_session_results(self, **kwargs):
        self.result_calls.append(kwargs)
        if self.raise_error is not None:
            raise self.raise_error
        return {"Confidence": self.confidence, "ReferenceImage": {}, "AuditImages": []}


class AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeDynamo:
    """One case item in wire format, honouring the two conditions the liveness writes depend on:
    the case accepts liveness (no state yet, or AWAITING_APPLICANT), and the result is for the
    session bound to the case."""

    def __init__(self, item: dict[str, Any] | None = None):
        self.items: dict[str, dict[str, Any]] = {}
        if item is not None:
            self.items[item["applicantId"]["S"]] = copy.deepcopy(item)
        self.update_calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["applicantId"]["S"])
        return {"Item": copy.deepcopy(item)} if item else {}

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)
        key = kwargs["Key"]["applicantId"]["S"]
        item = self.items.setdefault(key, {"applicantId": {"S": key}, "sk": {"S": "CASE"}})
        values = kwargs["ExpressionAttributeValues"]
        condition = kwargs.get("ConditionExpression", "")
        state = (item.get("state") or {}).get("S")
        if "attribute_not_exists(#s) OR #s = :accepting" in condition:
            if state is not None and state != values[":accepting"]["S"]:
                raise AwsError("ConditionalCheckFailedException")
        if "pendingLivenessSessionId = :sid" in condition:
            if (item.get("pendingLivenessSessionId") or {}).get("S") != values[":sid"]["S"]:
                raise AwsError("ConditionalCheckFailedException")
        expression = kwargs["UpdateExpression"]
        if "SET liveness = :l" in expression:
            item["liveness"] = values[":l"]
            item.pop("pendingLivenessSessionId", None)
            if "ADD livenessFailures :one" in expression:
                count = int((item.get("livenessFailures") or {"N": "0"})["N"]) + 1
                item["livenessFailures"] = {"N": str(count)}
        if "SET pendingLivenessSessionId = :sid" in expression:
            item["pendingLivenessSessionId"] = values[":sid"]
        return {}

    def item(self, applicant_id: str) -> dict[str, Any]:
        return self.items[applicant_id]


CLAIMS = {"authorizer": {"jwt": {"claims": {"sub": "user-1"}}}}
APPLICANT = "APP-user-1-run-1"


@pytest.fixture(autouse=True)
def table(monkeypatch):
    monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
    # Each test opts in to a challenge; a shell exporting one would change what they check.
    monkeypatch.delenv("LIVENESS_CHALLENGE", raising=False)


@pytest.fixture
def rekognition(monkeypatch):
    fake = FakeRekognition()
    monkeypatch.setattr(liveness, "_clients", lambda region: fake)
    return fake


@pytest.fixture
def dynamo(monkeypatch):
    fake = FakeDynamo()
    monkeypatch.setattr(liveness, "_ddb", lambda: fake)
    return fake


def session_event(jurisdiction="us", application_id="run-1"):
    return {"body": json.dumps({"jurisdiction": jurisdiction, "applicationId": application_id}),
            "requestContext": CLAIMS}


def result_event(session_id="sess-1", application_id="run-1", claims=CLAIMS):
    return {"pathParameters": {"sessionId": session_id},
            "queryStringParameters": {"jurisdiction": "us", "applicationId": application_id},
            "requestContext": claims}


class TestCreateSession:
    def test_returns_a_fresh_session_id_and_region(self, rekognition, dynamo):
        resp = liveness.create_session(session_event())
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"]) == {"sessionId": "sess-1", "region": "us-east-1",
                                            "challenge": "FaceMovementAndLightChallenge"}

    def test_the_light_challenge_is_the_default(self, rekognition, dynamo, monkeypatch):
        """D-36 — AWS recommends the light challenge for the highest accuracy, so a stack that
        sets nothing gets it, explicitly rather than by omission."""
        monkeypatch.delenv("LIVENESS_CHALLENGE", raising=False)
        liveness.create_session(session_event())
        assert rekognition.create_calls[0]["Settings"]["ChallengePreferences"] == [
            {"Type": "FaceMovementAndLightChallenge"}]

    def test_a_deployment_can_choose_the_movement_challenge(self, rekognition, dynamo,
                                                           monkeypatch):
        """The demo's choice: no flashing colours, several seconds quicker."""
        monkeypatch.setenv("LIVENESS_CHALLENGE", "FaceMovementChallenge")
        body = json.loads(liveness.create_session(session_event())["body"])
        assert rekognition.create_calls[0]["Settings"]["ChallengePreferences"] == [
            {"Type": "FaceMovementChallenge"}]
        assert body["challenge"] == "FaceMovementChallenge"

    def test_an_unknown_challenge_falls_back_to_the_light_challenge(self, rekognition, dynamo,
                                                                    monkeypatch):
        """A typo must not quietly select the less accurate check, nor fail every session."""
        monkeypatch.setenv("LIVENESS_CHALLENGE", "FaceMovement")
        resp = liveness.create_session(session_event())
        assert resp["statusCode"] == 200
        assert rekognition.create_calls[0]["Settings"]["ChallengePreferences"] == [
            {"Type": "FaceMovementAndLightChallenge"}]

    def test_the_session_is_bound_to_the_callers_case(self, rekognition, dynamo):
        liveness.create_session(session_event())
        assert dynamo.item(APPLICANT)["pendingLivenessSessionId"] == {"S": "sess-1"}

    def test_never_reuses_a_cached_session_id_across_calls(self, monkeypatch, dynamo):
        """F-41 — no memoisation exists to reuse from; each call hits the API fresh."""
        counter = {"n": 0}

        class SequentialFake(FakeRekognition):
            def create_face_liveness_session(self, **kwargs):
                counter["n"] += 1
                return {"SessionId": f"sess-{counter['n']}"}

        monkeypatch.setattr(liveness, "_clients", lambda region: SequentialFake())
        first = json.loads(liveness.create_session(session_event())["body"])
        second = json.loads(liveness.create_session(session_event())["body"])
        assert first["sessionId"] != second["sessionId"]
        # The newest session is the bound one; the earlier one can no longer be recorded.
        assert dynamo.item(APPLICANT)["pendingLivenessSessionId"] == {"S": "sess-2"}

    @pytest.mark.parametrize("jurisdiction,region", [("uk", "eu-west-1"),
                                                     ("au", "ap-southeast-5")])
    def test_liveness_runs_where_the_service_exists(self, rekognition, dynamo, jurisdiction,
                                                    region):
        """F-27 — Face Liveness is not available in London or Sydney."""
        body = json.loads(liveness.create_session(session_event(jurisdiction))["body"])
        assert body["region"] == region

    def test_unsupported_jurisdiction_is_a_400(self, rekognition, dynamo):
        assert liveness.create_session(session_event("zz"))["statusCode"] == 400

    def test_a_session_needs_a_case_to_bind_to(self, rekognition, dynamo):
        resp = liveness.create_session({"body": json.dumps({"jurisdiction": "us"})})
        assert resp["statusCode"] == 400

    def test_a_submitted_case_takes_no_new_session(self, rekognition, monkeypatch):
        """The evidence a case is decided on must not change underneath it."""
        fake = FakeDynamo({"applicantId": {"S": APPLICANT}, "state": {"S": "IN_PROGRESS"}})
        monkeypatch.setattr(liveness, "_ddb", lambda: fake)
        resp = liveness.create_session(session_event())
        assert resp["statusCode"] == 409
        assert json.loads(resp["body"])["error"] == "CASE_NOT_ACCEPTING_LIVENESS"

    def test_a_case_awaiting_the_applicant_takes_a_new_session(self, rekognition, monkeypatch):
        fake = FakeDynamo({"applicantId": {"S": APPLICANT},
                           "state": {"S": "AWAITING_APPLICANT"}})
        monkeypatch.setattr(liveness, "_ddb", lambda: fake)
        assert liveness.create_session(session_event())["statusCode"] == 200

    def test_sets_output_config_when_the_regions_match(self, rekognition, dynamo, monkeypatch):
        """Without `Settings.OutputConfig`, `GetFaceLivenessSessionResults` never returns an
        `S3Object` at all — found live: a passing liveness check left `referenceImageKey` empty
        and the face comparison had nothing to compare."""
        monkeypatch.setenv("BIOMETRIC_BUCKET_NAME", "kyc90-demo-biometric")
        monkeypatch.setenv("BIOMETRIC_BUCKET_REGION", "us-east-1")
        liveness.create_session(session_event())
        settings = rekognition.create_calls[0]["Settings"]
        assert settings["OutputConfig"] == {
            "S3Bucket": "kyc90-demo-biometric", "S3KeyPrefix": "liveness-sessions"}
        assert settings["AuditImagesLimit"] == 1

    def test_omits_output_config_when_the_liveness_region_differs(self, rekognition, dynamo,
                                                                  monkeypatch):
        """The bucket and the endpoint must share a region; a mismatched one is rejected."""
        monkeypatch.setenv("BIOMETRIC_BUCKET_NAME", "kyc90-demo-biometric")
        monkeypatch.setenv("BIOMETRIC_BUCKET_REGION", "us-east-1")
        liveness.create_session(session_event("uk"))
        assert "OutputConfig" not in rekognition.create_calls[0]["Settings"]

    def test_omits_output_config_when_no_bucket_is_configured(self, rekognition, dynamo,
                                                              monkeypatch):
        monkeypatch.delenv("BIOMETRIC_BUCKET_NAME", raising=False)
        liveness.create_session(session_event())
        assert "OutputConfig" not in rekognition.create_calls[0]["Settings"]


class TestGetResult:
    @pytest.fixture
    def bound(self, rekognition, dynamo):
        liveness.create_session(session_event())
        return dynamo

    def test_returns_only_a_boolean_never_the_confidence_score(self, bound):
        """F-44/R33.2 — the load-bearing assertion of this whole module."""
        body = json.loads(liveness.get_result(result_event())["body"])
        assert body == {"isLive": True}
        assert "confidence" not in json.dumps(body).lower()

    def test_the_result_is_recorded_and_the_session_consumed(self, bound):
        """R33.5 — the three-minute window destroys the session, so this is the only chance to
        record it; and a consumed session cannot be recorded twice."""
        liveness.get_result(result_event())
        item = bound.item(APPLICANT)
        assert item["liveness"]["M"]["result"] == {"S": "PASS"}
        assert item["liveness"]["M"]["sessionId"] == {"S": "sess-1"}
        assert "pendingLivenessSessionId" not in item

    def test_a_failure_is_counted(self, monkeypatch, dynamo):
        """R30.2 — the third failure goes to a reviewer, so failures have to be counted where
        they cannot be reset by re-running a session."""
        fake = FakeRekognition(confidence=40.0)
        monkeypatch.setattr(liveness, "_clients", lambda region: fake)
        for attempt in (1, 2):
            fake.session_id = f"sess-{attempt}"
            liveness.create_session(session_event())
            resp = liveness.get_result(result_event(session_id=f"sess-{attempt}"))
            assert json.loads(resp["body"]) == {"isLive": False}
        assert dynamo.item(APPLICANT)["livenessFailures"] == {"N": "2"}

    def test_a_pass_is_not_counted_as_a_failure(self, bound):
        liveness.get_result(result_event())
        assert "livenessFailures" not in bound.item(APPLICANT)

    def test_a_retry_is_answered_from_the_record(self, bound, rekognition):
        """A browser retry of a recorded session gets the same answer and copies nothing."""
        liveness.get_result(result_event())
        again = liveness.get_result(result_event())
        assert again["statusCode"] == 200 and json.loads(again["body"]) == {"isLive": True}
        assert len(rekognition.result_calls) == 1

    def test_a_session_this_case_did_not_create_is_refused(self, bound):
        """A PASS cannot be carried from one case to another."""
        resp = liveness.get_result(result_event(session_id="someone-elses-session"))
        assert resp["statusCode"] == 409
        assert json.loads(resp["body"])["error"] == "SESSION_NOT_BOUND"
        assert "liveness" not in bound.item(APPLICANT)

    def test_identity_comes_from_claims_not_a_client_supplied_applicant_id(self, bound):
        """With claims present, a spoofed `applicantId` is ignored entirely: the session is
        looked up and recorded on the caller's own case."""
        event = result_event()
        event["queryStringParameters"]["applicantId"] = "APP-victims-account"
        liveness.get_result(event)
        assert "APP-victims-account" not in bound.items
        assert bound.item(APPLICANT)["liveness"]["M"]["result"] == {"S": "PASS"}

    def test_another_users_session_cannot_be_recorded_on_their_case(self, bound):
        other = {"authorizer": {"jwt": {"claims": {"sub": "user-2"}}}}
        resp = liveness.get_result(result_event(claims=other))
        assert resp["statusCode"] == 409

    def test_expired_session_returns_a_distinct_error_not_a_generic_500(self, monkeypatch,
                                                                        dynamo):
        """R33.6 — expiry must become a step-up, never a rejection, so it is a named error."""
        fake = FakeRekognition(raise_error=AwsError("SessionNotFoundException"))
        monkeypatch.setattr(liveness, "_clients", lambda region: fake)
        liveness.create_session(session_event())
        resp = liveness.get_result(result_event())
        assert resp["statusCode"] == 410
        assert json.loads(resp["body"])["error"] == "SESSION_EXPIRED"

    def test_missing_parameters_is_a_400(self, rekognition, dynamo):
        resp = liveness.get_result({"pathParameters": {}, "queryStringParameters": {}})
        assert resp["statusCode"] == 400

    def test_the_key_matches_what_intake_derives(self, bound):
        """The liveness result and the case record must land on the same item; both endpoints
        share `applicant_id_from` so they cannot drift."""
        intake_key = applicants.applicant_id_from({"requestContext": CLAIMS},
                                                  {"applicationId": "run-1"})
        assert intake_key == APPLICANT
        liveness.get_result(result_event())
        assert "liveness" in bound.item(intake_key)
