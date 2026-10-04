"""The S3 Object Lock write path — task 6.7, R18.3, R18.4, design §9.2.

The only impure module in this package, and kept thin on purpose: everything that can be decided
without AWS is decided in `record.py` and `spans.py`, so the part that cannot be unit-tested
offline contains no logic worth testing.

Object Lock is enabled at bucket creation (R17.2, F-29) and cannot be retrofitted. A regulated
deployment writes every record in **Compliance mode** for seven years: not even the root account
can shorten a retention period. That is the point, and it is also why `put_decision_record`
refuses an `IncompleteRecord`: a wrong object written there is permanent for seven years, with no
cleanup path. A demo deployment writes in Governance mode for one day instead (D-35); the Lambda
reads which from `AUDIT_LOCK_MODE` and `AUDIT_RETENTION_DAYS`, set by the stack.
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


#: The two retention modes S3 Object Lock has. Anything else is a misconfiguration, refused
#: before a write rather than discovered as an S3 error after one.
LOCK_MODES = ("COMPLIANCE", "GOVERNANCE")


def retain_until(*, decided_at: datetime, years: int = RETENTION_YEARS,
                 days: float | None = None) -> datetime:
    """The retention expiry S3 will enforce. UTC always — Object Lock compares instants.
    `days`, when given, replaces `years` (a demo deployment retains for one day, D-35)."""
    if decided_at.tzinfo is None:
        raise ValueError("decided_at must be timezone-aware; Object Lock retention is absolute")
    span = days if days is not None else _DAYS_PER_YEAR * years
    if span <= 0:
        raise ValueError(f"retention must be positive, got {span} days")
    return decided_at.astimezone(timezone.utc) + timedelta(days=span)


def put_decision_record(
    record: DecisionRecord | IncompleteRecord,
    *,
    bucket: str,
    decided_at: datetime,
    kms_key_id: str | None = None,
    years: int = RETENTION_YEARS,
    lock_mode: str = "COMPLIANCE",
    retention_days: float | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """Write one decision record under Object Lock retention: COMPLIANCE for `years` unless the
    deployment says otherwise (`lock_mode`, `retention_days`).

    Refuses an `IncompleteRecord` rather than writing a partial one. A record missing its tool
    calls would satisfy no part of R18.1 while occupying the slot of a record that does, and
    Compliance mode means it could never be replaced or removed.
    """
    if isinstance(record, IncompleteRecord):
        raise ValueError(
            "refusing to write an incomplete decision record under Object Lock "
            f"({', '.join(r.value for r in record.reasons)}): {record.detail}"
        )
    if lock_mode not in LOCK_MODES:
        raise ValueError(f"lock_mode must be one of {LOCK_MODES}, got {lock_mode!r}")

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
        "ObjectLockMode": lock_mode,
        "ObjectLockRetainUntilDate": retain_until(
            decided_at=decided_at, years=years, days=retention_days),
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
