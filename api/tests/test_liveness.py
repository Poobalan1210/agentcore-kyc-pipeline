"""Liveness API — task 14.2, R33.1-R33.6, F-40, F-41, F-44."""

from __future__ import annotations

import json
from typing import Any

import pytest

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
        return {
            "Confidence": self.confidence,
            "ReferenceImage": {},
            "AuditImages": [],
        }


class AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeDynamo:
    def __init__(self):
        self.update_calls: list[dict[str, Any]] = []

    def update_item(self, **kwargs):
        self.update_calls.append(kwargs)
        return {}


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


class TestCreateSession:
    def test_returns_a_fresh_session_id_and_region(self, rekognition):
        event = {"body": json.dumps({"jurisdiction": "us"})}
        resp = liveness.create_session(event)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body == {"sessionId": "sess-1", "region": "us-east-1"}

    def test_never_reuses_a_cached_session_id_across_calls(self, monkeypatch):
        """F-41 — no memoisation exists to reuse from; each call hits the API fresh."""
        counter = {"n": 0}

        class SequentialFake(FakeRekognition):
            def create_face_liveness_session(self, **kwargs):
                counter["n"] += 1
                return {"SessionId": f"sess-{counter['n']}"}

        fake = SequentialFake()
        monkeypatch.setattr(liveness, "_clients", lambda region: fake)
        event = {"body": json.dumps({"jurisdiction": "us"})}
        first = json.loads(liveness.create_session(event)["body"])
        second = json.loads(liveness.create_session(event)["body"])
        assert first["sessionId"] != second["sessionId"]

    def test_uk_routes_to_eu_west_1_not_london(self, rekognition):
        """F-27 — Face Liveness is not available in London."""
        event = {"body": json.dumps({"jurisdiction": "uk"})}
        body = json.loads(liveness.create_session(event)["body"])
        assert body["region"] == "eu-west-1"

    def test_au_routes_cross_region_not_sydney(self, rekognition):
        event = {"body": json.dumps({"jurisdiction": "au"})}
        body = json.loads(liveness.create_session(event)["body"])
        assert body["region"] == "ap-southeast-5"

    def test_unsupported_jurisdiction_is_a_400(self, rekognition):
        event = {"body": json.dumps({"jurisdiction": "zz"})}
        resp = liveness.create_session(event)
        assert resp["statusCode"] == 400

    def test_sets_output_config_when_the_regions_match(self, rekognition, monkeypatch):
        """The load-bearing fix. Without `Settings.OutputConfig`, `GetFaceLivenessSessionResults`
        never returns an `S3Object` reference at all — found live: a genuinely successful liveness
        pass still left `referenceImageKey` empty, and the Identity agent correctly reported "no
        liveness reference frame to compare against" even though liveness had passed."""
        monkeypatch.setenv("BIOMETRIC_BUCKET_NAME", "kyc90-demo-biometric")
        monkeypatch.setenv("BIOMETRIC_BUCKET_REGION", "us-east-1")
        event = {"body": json.dumps({"jurisdiction": "us"})}  # us -> us-east-1
        liveness.create_session(event)
        settings = rekognition.create_calls[0]["Settings"]
        assert settings["OutputConfig"] == {
            "S3Bucket": "kyc90-demo-biometric", "S3KeyPrefix": "liveness-sessions"}

    def test_omits_output_config_when_the_liveness_region_differs(self, rekognition, monkeypatch):
        """AWS requires the OutputConfig bucket and the Face Liveness endpoint to share a region.
        The biometric bucket is provisioned once, in `us-east-1` — setting it for a jurisdiction
        whose liveness call runs elsewhere (UK -> eu-west-1, AU -> ap-southeast-5) would be
        rejected by the API outright, so it must be omitted rather than sent wrong."""
        monkeypatch.setenv("BIOMETRIC_BUCKET_NAME", "kyc90-demo-biometric")
        monkeypatch.setenv("BIOMETRIC_BUCKET_REGION", "us-east-1")
        event = {"body": json.dumps({"jurisdiction": "uk"})}  # uk -> eu-west-1
        liveness.create_session(event)
        settings = rekognition.create_calls[0]["Settings"]
        assert "OutputConfig" not in settings

    def test_omits_output_config_when_no_bucket_is_configured(self, rekognition, monkeypatch):
        monkeypatch.delenv("BIOMETRIC_BUCKET_NAME", raising=False)
        event = {"body": json.dumps({"jurisdiction": "us"})}
        liveness.create_session(event)
        assert "OutputConfig" not in rekognition.create_calls[0]["Settings"]

    def test_audit_images_limit_is_unaffected_by_output_config(self, rekognition, monkeypatch):
        monkeypatch.setenv("BIOMETRIC_BUCKET_NAME", "kyc90-demo-biometric")
        monkeypatch.setenv("BIOMETRIC_BUCKET_REGION", "us-east-1")
        event = {"body": json.dumps({"jurisdiction": "us"})}
        liveness.create_session(event)
        assert rekognition.create_calls[0]["Settings"]["AuditImagesLimit"] == 1


class TestGetResult:
    def _event(self, *, session_id="sess-1", jurisdiction="us", applicant_id="APP-1"):
        return {
            "pathParameters": {"sessionId": session_id},
            "queryStringParameters": {"jurisdiction": jurisdiction, "applicantId": applicant_id},
        }

    def test_returns_only_a_boolean_never_the_confidence_score(
        self, rekognition, dynamo, monkeypatch
    ):
        """F-44/R33.2 — the load-bearing assertion of this whole module."""
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        resp = liveness.get_result(self._event())
        body = json.loads(resp["body"])
        assert body == {"isLive": True}
        assert "confidence" not in json.dumps(body).lower()

    def test_below_threshold_is_not_live(self, monkeypatch):
        fake = FakeRekognition(confidence=40.0)
        monkeypatch.setattr(liveness, "_clients", lambda region: fake)
        monkeypatch.setattr(liveness, "_ddb", lambda: FakeDynamo())
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        resp = liveness.get_result(self._event())
        assert json.loads(resp["body"]) == {"isLive": False}

    def test_persists_the_result_to_the_case_record_immediately(
        self, rekognition, dynamo, monkeypatch
    ):
        """R33.5 — the 3-minute window destroys all session data on expiry, so this call is the
        only chance to persist."""
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        liveness.get_result(self._event())
        assert len(dynamo.update_calls) == 1
        call = dynamo.update_calls[0]
        assert call["Key"]["applicantId"]["S"] == "APP-1"

    def test_expired_session_returns_a_distinct_error_not_a_generic_500(self, monkeypatch):
        """R33.6 — the case must emit STEP_UP, never REJECT, on expiry. That routing decision
        happens in asl.py's CheckPreconditions; this endpoint's job is only to surface a
        distinct, recognisable error rather than a raised exception."""
        fake = FakeRekognition(raise_error=AwsError("SessionNotFoundException"))
        monkeypatch.setattr(liveness, "_clients", lambda region: fake)
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        resp = liveness.get_result(self._event())
        assert resp["statusCode"] == 410
        assert json.loads(resp["body"])["error"] == "SESSION_EXPIRED"

    def test_missing_parameters_is_a_400(self, rekognition):
        resp = liveness.get_result({"pathParameters": {}, "queryStringParameters": {}})
        assert resp["statusCode"] == 400

    def test_identity_comes_from_claims_not_from_a_client_supplied_applicant_id(
        self, rekognition, dynamo, monkeypatch
    ):
        """The security property of this endpoint.

        An earlier version read `applicantId` straight from the query string, so any signed-in
        user could write a liveness PASS onto *another* applicant's case record — forging the
        strongest single piece of identity evidence in the system. With claims present, a
        spoofed `applicantId` must be ignored entirely.
        """
        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        event = {
            "pathParameters": {"sessionId": "sess-1"},
            "queryStringParameters": {
                "jurisdiction": "us",
                "applicantId": "APP-victims-account",   # spoof attempt
                "applicationId": "run-1",
            },
            "requestContext": {"authorizer": {"jwt": {"claims": {"sub": "attacker"}}}},
        }
        liveness.get_result(event)
        written_key = dynamo.update_calls[0]["Key"]["applicantId"]["S"]
        assert written_key == "APP-attacker-run-1"
        assert "victim" not in written_key

    def test_key_matches_what_create_applicant_derives(self, rekognition, dynamo, monkeypatch):
        """The liveness result and the case record must land on the same item. Two independent
        derivations of that key is exactly the bug that sent every liveness PASS to an orphan
        row, so both endpoints share `applicant_id_from`."""
        import applicants

        monkeypatch.setenv("CASE_TABLE_NAME", "kyc90-cases")
        claims_ctx = {"authorizer": {"jwt": {"claims": {"sub": "user-9"}}}}
        liveness.get_result({
            "pathParameters": {"sessionId": "sess-1"},
            "queryStringParameters": {"jurisdiction": "us", "applicationId": "abc123"},
            "requestContext": claims_ctx,
        })
        liveness_key = dynamo.update_calls[0]["Key"]["applicantId"]["S"]
        intake_key = applicants.applicant_id_from(
            {"requestContext": claims_ctx}, {"applicationId": "abc123"})
        assert liveness_key == intake_key
