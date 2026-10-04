"""Face comparison: both images from the case record, the threshold fixed server side.

Two properties are pinned here.

**The images come from the case record.** The document is the first one listed on the case, in the
jurisdiction evidence bucket (Object Lock, seven years); the liveness reference frame is
`liveness.referenceImageKey` in the biometric bucket (24-hour lifecycle, because a real face must
stay deletable — R36.1-R36.3). An agent cannot point the comparison at other images.

**The threshold is not the agent's.** An earlier version accepted `threshold` as an argument, so an
agent persuaded to pass 0 would have turned any detected face into a "match".
"""

from __future__ import annotations

import pytest

from kyctools.contract import Status, ToolError
from kyctools.tools_impl import FACE_MATCH_THRESHOLD, rekognition_compare_faces


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
def rek(aws):
    fake = FakeRekognition()
    aws.boto3.clients["rekognition"] = fake
    aws.seed("APP-1")
    return fake


def compare(**extra):
    return rekognition_compare_faces({"applicantId": "APP-1", **extra}, {})


class TestImagesComeFromTheCaseRecord:
    def test_each_image_gets_its_own_bucket(self, rek, aws):
        compare()
        call = rek.calls[0]
        assert call["SourceImage"]["S3Object"] == {"Bucket": aws.EVIDENCE_BUCKET,
                                                   "Name": "APP-1/PASSPORT-1"}
        assert call["TargetImage"]["S3Object"] == {"Bucket": aws.BIOMETRIC_BUCKET,
                                                   "Name": "APP-1/reference-1.jpg"}

    def test_images_named_by_the_agent_are_ignored(self, rek, aws):
        compare(sourceBucket="attacker-bucket", sourceKey="clean-face.jpg",
                targetBucket="attacker-bucket", targetKey="clean-face.jpg")
        call = rek.calls[0]
        assert call["SourceImage"]["S3Object"]["Name"] == "APP-1/PASSPORT-1"
        assert call["TargetImage"]["S3Object"]["Name"] == "APP-1/reference-1.jpg"

    def test_the_threshold_cannot_be_lowered_by_the_agent(self, rek):
        compare(threshold=0)
        assert rek.calls[0]["SimilarityThreshold"] == FACE_MATCH_THRESHOLD == 90.0

    def test_no_reference_frame_is_recorded_as_not_performed(self, rek, aws):
        """Ours to fix, not the applicant's: UNAVAILABLE, so it routes to a human."""
        aws.seed("APP-1", liveness={"result": "PASS", "region": "eu-west-1",
                                    "referenceImageKey": ""})
        result = compare()
        assert result.status is Status.INVALID
        assert result.reason_code == "NO_REFERENCE_FRAME"
        assert rek.calls == []
        fact = aws.ddb.fact("APP-1", "factFaceMatch")
        assert fact["performed"] is False
        assert fact["status"] == "UNAVAILABLE"

    def test_no_document_on_the_case_is_invalid(self, rek, aws):
        aws.seed("APP-1", documents=[])
        result = compare()
        assert result.status is Status.INVALID
        assert result.reason_code == "NO_DOCUMENT_ON_CASE"

    def test_an_unconfigured_biometric_bucket_is_an_outage(self, rek, monkeypatch):
        monkeypatch.delenv("BIOMETRIC_BUCKET")
        with pytest.raises(ToolError):
            compare()


class TestResultShape:
    def test_a_match_returns_a_band_and_never_a_raw_score(self, rek, aws):
        """F-44/R33.2 — the raw similarity is not the agent's business, nor the record's."""
        result = compare()
        assert result.status is Status.OK
        assert result.data["match"] is True
        assert result.data["similarityBand"] == "HIGH"
        assert "97.5" not in str(result.data)
        fact = aws.ddb.fact("APP-1", "factFaceMatch")
        assert fact["performed"] is True and fact["match"] is True
        assert "97.5" not in str(fact)

    @pytest.mark.parametrize(("similarity", "band"), [(99.0, "HIGH"), (92.0, "MEDIUM")])
    def test_bands_are_bucketed(self, rek, similarity, band):
        rek.similarity = similarity
        assert compare().data["similarityBand"] == band

    def test_no_match_is_a_clean_negative_not_an_error(self, rek, aws):
        rek.matches = False
        result = compare()
        assert result.status is Status.OK
        assert result.data["match"] is False
        assert result.data["faceDetected"] is True
        assert aws.ddb.fact("APP-1", "factFaceMatch")["match"] is False

    def test_no_face_found_is_a_finding(self, rek, aws):
        rek.error = aws.AwsError("InvalidParameterException")
        result = compare()
        assert result.status is Status.OK
        fact = aws.ddb.fact("APP-1", "factFaceMatch")
        assert fact["performed"] is True
        assert fact["faceDetected"] is False

    def test_an_unreadable_image_is_invalid_and_recorded(self, rek, aws):
        rek.error = aws.AwsError("InvalidImageFormatException")
        result = compare()
        assert result.status is Status.INVALID
        assert aws.ddb.fact("APP-1", "factFaceMatch")["reasonCode"] == "IMAGE_UNREADABLE"

    def test_an_outage_raises_and_is_recorded_as_unavailable(self, rek, aws):
        rek.error = aws.AwsError("ThrottlingException")
        with pytest.raises(ToolError):
            compare()
        # The dispatcher records nothing itself; the tool records the outage before raising.
        fact = aws.ddb.fact("APP-1", "factFaceMatch")
        assert fact["performed"] is False
        assert fact["reasonCode"] == "REKOGNITION_UNAVAILABLE"
