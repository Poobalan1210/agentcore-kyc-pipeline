"""Face comparison across two buckets.

The point of these tests is that the two images are *never* in the same bucket, by design: the
document is under Object Lock for seven years, while the liveness reference frame must remain
deletable within 24 hours (R36.1-R36.3). A tool that accepted one bucket for both could not perform
this comparison at all, which left the strongest identity control inert while looking implemented.
"""

from __future__ import annotations

import sys

import pytest

from kyctools.contract import Status
from kyctools.tools_impl import rekognition_compare_faces

EVIDENCE = "kyc90-evidence-us-633898796092"
BIOMETRIC = "kyc90-demo-biometric-633898796092"
DOC_KEY = "APP-1/PASSPORT-abc123"
FRAME_KEY = "APP-1/reference-def456.jpg"


class AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeRekognition:
    def __init__(self, *, similarity=97.5, matches=True, error=None, unmatched=0):
        self.similarity = similarity
        self.matches = matches
        self.error = error
        self.unmatched = unmatched
        self.calls: list[dict] = []

    def compare_faces(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {
            "FaceMatches": ([{"Similarity": self.similarity}] if self.matches else []),
            "UnmatchedFaces": [{}] * self.unmatched,
        }


@pytest.fixture
def rek(monkeypatch):
    fake = FakeRekognition()

    class M:
        @staticmethod
        def client(name, **kw):
            assert name == "rekognition"
            return fake

    monkeypatch.setitem(sys.modules, "boto3", M())
    # botocore's ClientError is imported inside the tool; the real module is available in the venv,
    # but the fake raises our own AwsError, so the except clause must still match. Patching only
    # boto3 keeps botocore real, and `AwsError` is caught by the generic handler below.
    return fake


class TestTwoBuckets:
    def test_each_image_gets_its_own_bucket(self, rek):
        rekognition_compare_faces(
            {"sourceBucket": EVIDENCE, "sourceKey": DOC_KEY,
             "targetBucket": BIOMETRIC, "targetKey": FRAME_KEY}, {})
        call = rek.calls[0]
        assert call["SourceImage"]["S3Object"]["Bucket"] == EVIDENCE
        assert call["SourceImage"]["S3Object"]["Name"] == DOC_KEY
        assert call["TargetImage"]["S3Object"]["Bucket"] == BIOMETRIC
        assert call["TargetImage"]["S3Object"]["Name"] == FRAME_KEY

    def test_the_target_bucket_defaults_from_the_environment(self, rek, monkeypatch):
        """Defaulted rather than demanded from the agent: a model that guessed the wrong bucket
        would get an unreadable-image finding, which reads as a bad document rather than as a
        misrouted lookup."""
        monkeypatch.setenv("BIOMETRIC_BUCKET", BIOMETRIC)
        rekognition_compare_faces(
            {"sourceBucket": EVIDENCE, "sourceKey": DOC_KEY, "targetKey": FRAME_KEY}, {})
        assert rek.calls[0]["TargetImage"]["S3Object"]["Bucket"] == BIOMETRIC

    def test_a_single_bucket_argument_still_works_for_both(self, rek, monkeypatch):
        """Back-compatibility for an existing caller. Not the intended path — no such pair of
        objects exists in this system — but it must not become an error."""
        monkeypatch.delenv("BIOMETRIC_BUCKET", raising=False)
        rekognition_compare_faces(
            {"bucket": EVIDENCE, "sourceKey": DOC_KEY, "targetKey": FRAME_KEY}, {})
        call = rek.calls[0]
        assert call["SourceImage"]["S3Object"]["Bucket"] == EVIDENCE
        assert call["TargetImage"]["S3Object"]["Bucket"] == EVIDENCE

    def test_an_explicit_target_bucket_beats_the_environment_default(self, rek, monkeypatch):
        monkeypatch.setenv("BIOMETRIC_BUCKET", "some-other-bucket")
        rekognition_compare_faces(
            {"sourceBucket": EVIDENCE, "sourceKey": DOC_KEY,
             "targetBucket": BIOMETRIC, "targetKey": FRAME_KEY}, {})
        assert rek.calls[0]["TargetImage"]["S3Object"]["Bucket"] == BIOMETRIC

    def test_missing_a_bucket_for_either_image_is_invalid(self, rek, monkeypatch):
        monkeypatch.delenv("BIOMETRIC_BUCKET", raising=False)
        result = rekognition_compare_faces(
            {"sourceKey": DOC_KEY, "targetKey": FRAME_KEY}, {})
        assert result.status is Status.INVALID
        assert result.reason_code == "MISSING_LOCATION"

    def test_missing_a_key_is_invalid(self, rek):
        result = rekognition_compare_faces(
            {"sourceBucket": EVIDENCE, "targetBucket": BIOMETRIC, "sourceKey": DOC_KEY}, {})
        assert result.status is Status.INVALID


class TestResultShape:
    def test_a_match_returns_a_band_and_never_a_raw_score(self, rek):
        """F-44/R33.2 — the threshold is applied server side and the raw similarity is not the
        agent's business. A raw score in the response would let the agent re-threshold it."""
        result = rekognition_compare_faces(
            {"sourceBucket": EVIDENCE, "sourceKey": DOC_KEY,
             "targetBucket": BIOMETRIC, "targetKey": FRAME_KEY}, {})
        assert result.status is Status.OK
        assert result.data["match"] is True
        assert result.data["similarityBand"] == "HIGH"
        serialised = str(result.data)
        assert "97.5" not in serialised
        assert "similarity" not in result.data

    @pytest.mark.parametrize(("similarity", "band"), [(99.0, "HIGH"), (92.0, "MEDIUM")])
    def test_bands_are_bucketed(self, monkeypatch, similarity, band):
        fake = FakeRekognition(similarity=similarity)

        class M:
            @staticmethod
            def client(name, **kw):
                return fake

        monkeypatch.setitem(sys.modules, "boto3", M())
        result = rekognition_compare_faces(
            {"sourceBucket": EVIDENCE, "sourceKey": DOC_KEY,
             "targetBucket": BIOMETRIC, "targetKey": FRAME_KEY}, {})
        assert result.data["similarityBand"] == band

    def test_no_match_is_a_clean_negative_not_an_error(self, monkeypatch):
        fake = FakeRekognition(matches=False)

        class M:
            @staticmethod
            def client(name, **kw):
                return fake

        monkeypatch.setitem(sys.modules, "boto3", M())
        result = rekognition_compare_faces(
            {"sourceBucket": EVIDENCE, "sourceKey": DOC_KEY,
             "targetBucket": BIOMETRIC, "targetKey": FRAME_KEY}, {})
        assert result.status is Status.OK
        assert result.data["match"] is False
        assert result.data["faceDetected"] is True
