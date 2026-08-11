"""The S3 Object Lock write path — task 6.7, R18.3, R18.4, design §9.2.

The only impure module in this package, and kept thin on purpose: everything that can be decided
without AWS is decided in `record.py` and `spans.py`, so the part that cannot be unit-tested
offline contains no logic worth testing.

Object Lock is in **Compliance mode**, set at bucket creation (R17.2, F-29) — it cannot be
retrofitted, and in Compliance mode not even the root account can shorten a retention period.
That is the point, and it is also why `put_decision_record` refuses an `IncompleteRecord`: a
wrong object written here is permanent for seven years. There is no cleanup path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import boto3

from record import DecisionRecord, IncompleteRecord, decision_object_key, to_json_bytes

#: Retention floor in years. US/UK/EU require 5, Australia 7 (B-12); one policy at 7 satisfies all
#: four markets, and Japan's exact APTCP period is still unverified (B-13, D-09) — the floor almost
#: certainly covers it. Erring long is recoverable; erring short is not.
RETENTION_YEARS = 7

#: 365.25 days per year. Calendar arithmetic without dateutil would need care around leap days and
#: an off-by-one-day retention is indistinguishable from a compliance failure at audit time, so
#: this rounds *up* by using the longer average year.
_DAYS_PER_YEAR = 365.25


def retain_until(*, decided_at: datetime, years: int = RETENTION_YEARS) -> datetime:
    """The retention expiry S3 will enforce. UTC always — Object Lock compares instants."""
    if decided_at.tzinfo is None:
        raise ValueError("decided_at must be timezone-aware; Object Lock retention is absolute")
    return decided_at.astimezone(timezone.utc) + timedelta(days=_DAYS_PER_YEAR * years)


def put_decision_record(
    record: DecisionRecord | IncompleteRecord,
    *,
    bucket: str,
    decided_at: datetime,
    kms_key_id: str | None = None,
    years: int = RETENTION_YEARS,
    client: Any = None,
) -> dict[str, Any]:
    """Write one decision record under Object Lock Compliance retention.

    Refuses an `IncompleteRecord` rather than writing a partial one. A record missing its tool
    calls would satisfy no part of R18.1 while occupying the slot of a record that does, and
    Compliance mode means it could never be replaced or removed.
    """
    if isinstance(record, IncompleteRecord):
        raise ValueError(
            "refusing to write an incomplete decision record under Object Lock "
            f"({', '.join(r.value for r in record.reasons)}): {record.detail}"
        )

    s3 = client or boto3.client("s3")
    body = to_json_bytes(record)
    params: dict[str, Any] = {
        "Bucket": bucket,
        "Key": decision_object_key(
            applicant_id=record.applicant_id,
            phase=record.phase,
            decision_id=record.decision_id,
        ),
        "Body": body,
        "ContentType": "application/json",
        "ObjectLockMode": "COMPLIANCE",
        "ObjectLockRetainUntilDate": retain_until(decided_at=decided_at, years=years),
        # S3 requires a checksum or MD5 alongside Object Lock; SHA256 also gives the audit trail
        # an integrity value that survives independently of the object.
        "ChecksumAlgorithm": "SHA256",
        # Queryable without fetching the body, and the source pins are what a reviewer checks
        # first to confirm the record was not built from execution output (R18.2).
        "Metadata": {
            "applicant-id": record.applicant_id,
            "case-id": record.case_id,
            "phase": record.phase,
            "decision-id": record.decision_id,
            "outcome": record.outcome,
            "schema-version": record.schema_version,
            "evidence-sources": ",".join(record.sources),
            "policy-engine-version": record.versions.policy_engine_version,
            "harness-version": record.versions.harness_version,
        },
    }
    if kms_key_id:
        params["ServerSideEncryption"] = "aws:kms"
        params["SSEKMSKeyId"] = kms_key_id

    return s3.put_object(**params)
