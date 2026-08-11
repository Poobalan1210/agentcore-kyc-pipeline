"""Permission boundaries for the tools Lambda.

The tools Lambda is the one component that touches applicant evidence, a face-comparison service
and an OCR service, so what it *cannot* do is more interesting than what it can. These are negative
assertions for that reason: a widened action list is a one-line change that no functional test
would notice.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from kyc.tools_stack import ToolsStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = ToolsStack(
        app, "TestTools",
        evidence_bucket_names={"us": "kyc90-evidence-us", "uk": "kyc90-evidence-uk",
                               "au": "kyc90-evidence-au", "jp": "kyc90-evidence-jp"},
        evidence_key_arns={
            code: f"arn:aws:kms:us-east-1:123456789012:key/{code}-key"
            for code in ("us", "uk", "au", "jp")
        },
        trace_key_arn="arn:aws:kms:us-east-1:123456789012:key/trace-key",
        biometric_bucket_name="kyc90-demo-biometric",
        case_table_name="kyc90-cases",
        case_table_arn="arn:aws:dynamodb:us-east-1:123456789012:table/kyc90-cases",
        case_key_arn="arn:aws:kms:us-east-1:123456789012:key/case-key",
        env=cdk.Environment(account="123456789012", region="us-east-1"),
    )
    return Template.from_stack(stack)


def _actions(template: Template) -> set[str]:
    actions: set[str] = set()
    for body in template.find_resources("AWS::IAM::Policy").values():
        for statement in body["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                actions.update(a for a in action if isinstance(a, str))
            elif isinstance(action, str):
                actions.add(action)
    return actions


def test_can_run_synchronous_text_detection(template: Template) -> None:
    """`s3_get_document` reads the machine-readable zone with this call. Without it the Documents
    agent has only a size and a content type, which is the state a live run exposed — the agent
    correctly but uselessly reported every field "not available in the document metadata"."""
    assert "textract:DetectDocumentText" in _actions(template)


def test_has_no_asynchronous_textract_permissions(template: Template) -> None:
    """The async pair would let this role start work that outlives the request and publish its
    result to an SNS topic. Nothing here uses it, so nothing here should be able to."""
    granted = _actions(template)
    for action in ("textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection",
                   "textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis"):
        assert action not in granted, action


def test_has_no_analyze_id_or_analyze_document(template: Template) -> None:
    """Neither is used. `AnalyzeID` in particular is trained on real US templates and would read
    the synthetic layouts poorly, so it was rejected in favour of the MRZ — which also carries check
    digits and is therefore verifiable rather than merely trusted."""
    granted = _actions(template)
    assert "textract:AnalyzeID" not in granted
    assert "textract:AnalyzeDocument" not in granted


def test_cannot_write_evidence(template: Template) -> None:
    """A tool reads evidence; the write path goes through the Public API with Object Lock semantics
    attached. A tool that could write evidence could also overwrite it."""
    granted = _actions(template)
    for action in ("s3:PutObject", "s3:PutObject*", "s3:DeleteObject", "s3:DeleteObject*",
                   "s3:PutObjectRetention"):
        assert action not in granted, action


def test_can_read_the_biometric_bucket_and_decrypt_it(template: Template) -> None:
    """The liveness reference frame lives in its own bucket — no Object Lock, 24-hour lifecycle —
    because a real human face has to stay deletable (R36.1-R36.3). Without read access here the face
    comparison cannot run at all, and it fails as an unreadable image, which reads like a bad
    document rather than a missing permission."""
    granted = _actions(template)
    # CDK's `grant_read` emits wildcarded actions (`s3:GetObject*`), not the bare action name.
    assert "s3:GetObject*" in granted
    assert "kms:Decrypt" in granted

    # The trace key specifically: the biometric bucket is encrypted with it rather than with a
    # jurisdiction evidence key, which is the whole reason it is a separate bucket.
    decrypt_resources: list[str] = []
    for body in template.find_resources("AWS::IAM::Policy").values():
        for statement in body["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action")
            actions = actions if isinstance(actions, list) else [actions]
            if "kms:Decrypt" in actions:
                resources = statement.get("Resource")
                resources = resources if isinstance(resources, list) else [resources]
                decrypt_resources.extend(r for r in resources if isinstance(r, str))
    assert any("trace-key" in r for r in decrypt_resources), (
        "the trace key must be decryptable or the liveness frame cannot be read")


def test_biometric_bucket_is_read_only(template: Template) -> None:
    """A face is the most sensitive object in the system. The tool compares two images in a request
    and stores nothing, so no write path to this bucket should exist."""
    granted = _actions(template)
    # Both the bare and the wildcarded forms, since CDK emits the latter for granted patterns.
    for action in ("s3:PutObject", "s3:PutObject*", "s3:DeleteObject", "s3:DeleteObject*"):
        assert action not in granted, action


def test_face_comparison_is_limited_to_comparison(template: Template) -> None:
    """No collection or face-storage actions: this system compares two images in a request and keeps
    nothing, so an indexed face collection must not be creatable."""
    granted = _actions(template)
    assert "rekognition:CompareFaces" in granted
    for action in ("rekognition:IndexFaces", "rekognition:CreateCollection",
                   "rekognition:SearchFacesByImage"):
        assert action not in granted, action
