"""Reproduce the submit_* failure locally, since the Lambda logger records exc_info but not text."""
import os
import traceback

os.environ["CASE_TABLE"] = "kyc90-cases"
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from kyctools import tools_impl  # noqa: E402

CASES = [
    ("documents full", tools_impl.submit_document_analysis, {
        "applicantId": "REPRO-1", "confidence": 0.96, "expired": False,
        "screenPhotograph": False, "unsupportedIssuer": False}),
    ("documents as the agent likely sends it", tools_impl.submit_document_analysis, {
        "applicantId": "REPRO-1", "confidence": 0.96, "documentType": "PASSPORT",
        "issuingCountry": "US", "expired": False, "screenPhotograph": False,
        "qualityDefects": [], "unsupportedIssuer": False}),
    ("documents without applicantId", tools_impl.submit_document_analysis, {
        "confidence": 0.96, "expired": False, "screenPhotograph": False,
        "unsupportedIssuer": False}),
    ("fraud", tools_impl.submit_fraud_assessment, {
        "applicantId": "REPRO-1", "determined": True, "velocityCritical": False,
        "velocityDetail": [], "sharedAttributeCount": 0, "thinFile": False}),
    ("prescreen", tools_impl.submit_prescreen, {
        "applicantId": "REPRO-1", "determined": True, "matched": False, "hitCount": 0,
        "strongestMatch": "NO_MATCH", "listsConsulted": ["synthetic"]}),
]

for label, fn, args in CASES:
    print("=" * 78)
    print(label)
    try:
        result = fn(args)
        print("  status=%s reason_code=%s" % (result.status, result.reason_code))
    except Exception:  # noqa: BLE001
        print("  RAISED:")
        traceback.print_exc()
