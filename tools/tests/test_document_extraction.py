"""Tests for reading fields off an uploaded document.

The distinction these tests exist to protect is between two kinds of failure that look identical
from the outside and must never be conflated:

* **The document cannot be read** — wrong format, too large, unreadable. A finding *about the
  document*, returned as `documentReadable: False` for the decision engine and the reviewer to
  weigh.
* **The extraction service is down** — raises, because reporting "nothing on the document" when the
  source was never consulted is the same fail-open mistake R25.7 forbids for screening.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from kyctools.contract import Status, ToolError
from kyctools.tools_impl import _extract_document_fields, s3_get_document

US_LINE1 = "P<USAREEVES<<TAYLOR<JORDAN<<<<<<<<<<<<<<<<<<"
US_LINE2 = "4710293051USA9105148X3205138<<<<<<<<<<<<<<00"

DOCUMENT_LINES = [
    "SPECIMEN - NOT A REAL TRAVEL DOCUMENT",
    "PASSPORT",
    "UNITED STATES OF AMERICA",
    "SURNAME / NOM",
    "REEVES",
    "DATE OF BIRTH / DATE DE NAISSANCE",
    "1991-05-14",
    US_LINE1,
    US_LINE2,
    "This document is a specimen.",
]


class AwsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self, *, metadata=None, size=240_000, content_type="image/jpeg",
                 body=b"\xff\xd8fake-jpeg", head_error=None, get_error=None):
        self.metadata = metadata or {}
        self.size = size
        self.content_type = content_type
        self.body = body
        self.head_error = head_error
        self.get_error = get_error

    def head_object(self, **kwargs):
        if self.head_error:
            raise self.head_error
        return {"ContentLength": self.size, "ContentType": self.content_type,
                "Metadata": self.metadata}

    def get_object(self, **kwargs):
        if self.get_error:
            raise self.get_error
        return {"Body": io.BytesIO(self.body)}


class FakeTextract:
    def __init__(self, *, lines=None, error=None):
        self.lines = lines if lines is not None else DOCUMENT_LINES
        self.error = error
        self.calls = 0

    def detect_document_text(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return {"Blocks": [{"BlockType": "LINE", "Text": t} for t in self.lines]}


class FakeBoto3Module:
    def __init__(self, textract=None):
        self._textract = textract or FakeTextract()

    def client(self, name, **kwargs):
        if name == "textract":
            return self._textract
        raise AssertionError(f"unexpected client {name}")


@pytest.fixture
def textract(monkeypatch):
    fake = FakeTextract()
    monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(textract=fake))
    return fake


class TestFieldExtraction:
    def test_reads_the_mrz_and_verifies_its_check_digits(self, textract):
        result = _extract_document_fields(
            FakeS3(), bucket="b", key="k", content_type="image/jpeg", size_bytes=240_000)
        assert result["documentReadable"] is True
        assert result["mrzPresent"] is True
        mrz = result["mrz"]
        assert mrz["surname"] == "REEVES"
        assert mrz["givenNames"] == "TAYLOR JORDAN"
        assert mrz["dateOfBirth"] == "1991-05-14"
        assert mrz["documentNumber"] == "471029305"
        assert mrz["expiryDate"] == "2032-05-13"
        assert mrz["checkDigitsValid"] is True

    def test_reports_a_tampered_mrz_without_discarding_the_claim(self, monkeypatch):
        """A reviewer needs to see what the document claims *and* that the claim is inconsistent.
        Dropping the fields would leave only "invalid", which is not actionable."""
        tampered = US_LINE2[:13] + "850101" + US_LINE2[19:]
        fake = FakeTextract(lines=[US_LINE1, tampered])
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(textract=fake))
        result = _extract_document_fields(
            FakeS3(), bucket="b", key="k", content_type="image/jpeg", size_bytes=1000)
        mrz = result["mrz"]
        assert mrz["dateOfBirth"] == "1985-01-01"
        assert mrz["checkDigitsValid"] is False
        assert "dateOfBirth" in mrz["failedCheckDigits"]

    def test_a_document_with_no_mrz_is_readable_but_unparsed(self, monkeypatch):
        """Readable and MRZ-less are different facts. Conflating them would report a legible
        document as unreadable."""
        fake = FakeTextract(lines=["JUST", "SOME", "PRINTED", "TEXT"])
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(textract=fake))
        result = _extract_document_fields(
            FakeS3(), bucket="b", key="k", content_type="image/jpeg", size_bytes=1000)
        assert result["documentReadable"] is True
        assert result["mrzPresent"] is False
        assert result["mrz"] is None

    def test_ocr_lines_are_capped(self, monkeypatch):
        """An unbounded blob of page text is both a prompt-size risk and a place where text printed
        on the document could read as an instruction to the model."""
        fake = FakeTextract(lines=[f"line {i}" for i in range(500)])
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(textract=fake))
        result = _extract_document_fields(
            FakeS3(), bucket="b", key="k", content_type="image/jpeg", size_bytes=1000)
        assert len(result["textLines"]) == 60
        # The true count is still reported, so the cap is visible rather than silent.
        assert result["lineCount"] == 500


class TestUnreadableIsAFindingNotAnOutage:
    def test_pdf_is_reported_unreadable_with_a_reason(self):
        """Synchronous text detection does not accept PDF, and neither does CompareFaces — which is
        why the demo documents are generated as JPEG as well."""
        result = _extract_document_fields(
            FakeS3(), bucket="b", key="k", content_type="application/pdf", size_bytes=1000)
        assert result["documentReadable"] is False
        assert "PDF" in result["unreadableReason"]

    def test_oversized_document_skips_the_call_rather_than_failing_it(self, monkeypatch):
        fake = FakeTextract()
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(textract=fake))
        result = _extract_document_fields(
            FakeS3(), bucket="b", key="k", content_type="image/jpeg",
            size_bytes=20 * 1024 * 1024)
        assert result["documentReadable"] is False
        assert fake.calls == 0, "a call guaranteed to be rejected should not be made"

    @pytest.mark.parametrize("code", ["UnsupportedDocumentException", "BadDocumentException",
                                      "DocumentTooLargeException", "InvalidParameterException"])
    def test_documents_textract_rejects_are_findings(self, code, monkeypatch):
        fake = FakeTextract(error=AwsError(code))
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(textract=fake))
        result = _extract_document_fields(
            FakeS3(), bucket="b", key="k", content_type="image/jpeg", size_bytes=1000)
        assert result["documentReadable"] is False
        assert code in result["unreadableReason"]

    @pytest.mark.parametrize("code", ["ThrottlingException", "AccessDeniedException",
                                      "InternalServerError", "ProvisionedThroughputExceeded"])
    def test_a_service_outage_raises_rather_than_reporting_an_empty_document(
        self, code, monkeypatch
    ):
        """The load-bearing assertion. Reporting "nothing on the document" when the source was
        never consulted is the same fail-open mistake R25.7 forbids for screening — it would let a
        Textract outage silently look like a document with no expiry and no MRZ."""
        fake = FakeTextract(error=AwsError(code))
        monkeypatch.setitem(sys.modules, "boto3", FakeBoto3Module(textract=fake))
        with pytest.raises(ToolError) as excinfo:
            _extract_document_fields(
                FakeS3(), bucket="b", key="k", content_type="image/jpeg", size_bytes=1000)
        assert excinfo.value.reason_code == "TEXTRACT_UNAVAILABLE"

    def test_an_s3_fetch_failure_is_a_finding_not_a_crash(self, textract):
        result = _extract_document_fields(
            FakeS3(get_error=AwsError("NoSuchKey")), bucket="b", key="k",
            content_type="image/jpeg", size_bytes=1000)
        assert result["documentReadable"] is False
        assert "could not be fetched" in result["unreadableReason"]


@pytest.fixture
def doc_aws(aws):
    """The shared case-store fake plus S3 and Textract, with one applicant on file."""
    aws.boto3.clients["s3"] = FakeS3(metadata={"documenttype": "PASSPORT",
                                               "issuingcountry": "US", "synthetic": "true"})
    aws.boto3.clients["textract"] = FakeTextract()
    aws.seed("APP-1")
    return aws


class TestPersistence:
    """The verified extraction is written straight to the case record, bypassing the model.

    The MRZ check digits make this the one part of document extraction that is verified rather than
    merely trusted, and the decision engine compares it against the applicant's declared details.
    Routing it through the agent's own finding would turn a checkable fact into a paraphrase.
    """

    def test_the_document_listed_on_the_case_is_read_and_recorded(self, doc_aws):
        result = s3_get_document({"applicantId": "APP-1"}, {})
        assert result.status is Status.OK
        stored = doc_aws.ddb.fact("APP-1", "documentExtraction")
        assert stored["performed"] is True
        assert stored["mrz"]["surname"] == "REEVES"
        assert stored["mrz"]["checkDigitsValid"] is True
        assert stored["document"]["key"] == "APP-1/PASSPORT-1"
        assert stored["document"]["synthetic"] is True

    def test_a_document_not_on_the_case_is_refused(self, doc_aws):
        """A steered agent cannot substitute a clean document for the applicant's own."""
        result = s3_get_document(
            {"applicantId": "APP-1", "bucket": "b", "key": "someone-elses-passport"}, {})
        assert result.status is Status.INVALID
        assert result.reason_code == "DOCUMENT_NOT_ON_CASE"
        assert doc_aws.ddb.fact("APP-1", "documentExtraction") is None

    def test_a_listed_document_can_be_named_explicitly(self, doc_aws):
        result = s3_get_document({"applicantId": "APP-1", "bucket": doc_aws.EVIDENCE_BUCKET,
                                  "key": "APP-1/PASSPORT-1"}, {})
        assert result.status is Status.OK

    def test_the_intake_document_type_wins_over_the_agents(self, doc_aws):
        s3_get_document({"applicantId": "APP-1", "documentType": "NATIONAL_ID"}, {})
        stored = doc_aws.ddb.fact("APP-1", "documentExtraction")
        assert stored["document"]["documentType"] == "PASSPORT"

    def test_no_applicant_id_is_refused(self, doc_aws):
        result = s3_get_document({"bucket": "b", "key": "k"}, {})
        assert result.status is Status.INVALID
        assert result.reason_code == "MISSING_APPLICANT"

    def test_a_persistence_failure_fails_the_read(self, doc_aws):
        """Mandatory, not best effort, now that the rules read only what was recorded: an
        extraction the rules cannot see is a check that did not happen, and must surface as
        UNAVAILABLE rather than as a success the agent reports and the rules never read."""
        doc_aws.ddb.fail_updates = "ProvisionedThroughputExceededException"
        with pytest.raises(ToolError) as excinfo:
            s3_get_document({"applicantId": "APP-1"}, {})
        assert excinfo.value.reason_code == "FACT_NOT_RECORDED"

    def test_a_textract_outage_is_recorded_as_not_performed(self, doc_aws):
        doc_aws.boto3.clients["textract"] = FakeTextract(error=AwsError("ThrottlingException"))
        with pytest.raises(ToolError):
            s3_get_document({"applicantId": "APP-1"}, {})
        stored = doc_aws.ddb.fact("APP-1", "documentExtraction")
        assert stored["performed"] is False
        assert stored["reasonCode"] == "TEXTRACT_UNAVAILABLE"

    def test_an_unreadable_document_is_still_a_recorded_finding(self, doc_aws):
        doc_aws.boto3.clients["s3"] = FakeS3(content_type="application/pdf")
        result = s3_get_document({"applicantId": "APP-1"}, {})
        assert result.status is Status.OK
        stored = doc_aws.ddb.fact("APP-1", "documentExtraction")
        assert stored["performed"] is True
        assert stored["documentReadable"] is False


class TestToolEnvelope:
    def test_extraction_is_merged_into_the_tool_result(self, doc_aws):
        result = s3_get_document({"applicantId": "APP-1"}, {})
        assert result.status is Status.OK
        payload = json.loads(json.dumps(result.data))
        # Both halves present: the metadata stamped at upload and the fields read off the image.
        assert payload["synthetic"] is True
        assert payload["metadata"]["issuingcountry"] == "US"
        assert payload["mrz"]["surname"] == "REEVES"
        assert payload["documentReadable"] is True
        assert payload["recorded"] is True
