"""Shared fakes for the tool suite.

The case-store fake speaks the low-level DynamoDB wire format (`{"S": ...}`, `{"M": {...}}`), not
a Table-shaped dict interface. That is deliberate: a Table-shaped fake is what let four
AttributeValue serialisation bugs through elsewhere in this repo, because it accepted shapes the
real client rejects.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest


class AwsError(Exception):
    """Shaped like a botocore ClientError: the code is read structurally, not by type."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def to_av(value: Any) -> dict[str, Any]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float, Decimal)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, dict):
        return {"M": {k: to_av(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"L": [to_av(v) for v in value]}
    raise TypeError(type(value))


class FakeDynamo:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.updates: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []
        #: pk -> list of `expiresAt` epochs, for the velocity table.
        self.velocity: dict[str, list[int]] = {}
        self.fail_reads: str | None = None
        self.fail_updates: str | None = None
        self.fail_queries: str | None = None

    # ---- seeding and inspection -------------------------------------------------------------
    def put_case(self, applicant_id: str, **attributes: Any) -> None:
        item = {"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}}
        item.update({k: to_av(v) for k, v in attributes.items()})
        self.items[(applicant_id, "CASE")] = item

    def fact(self, applicant_id: str, attribute: str) -> Any:
        item = self.items.get((applicant_id, "CASE")) or {}
        raw = item.get(attribute)
        return json.loads(raw["S"]) if raw else None

    # ---- the client surface the tools use ---------------------------------------------------
    def get_item(self, *, TableName, Key, ConsistentRead=False, **_):
        self.reads.append({"TableName": TableName, "Key": Key, "ConsistentRead": ConsistentRead})
        if self.fail_reads:
            raise AwsError(self.fail_reads)
        item = self.items.get((Key["applicantId"]["S"], Key["sk"]["S"]))
        return {"Item": dict(item)} if item else {}

    def update_item(self, *, TableName, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None, ConditionExpression=None, **_):
        self.updates.append({"TableName": TableName, "Key": Key,
                             "UpdateExpression": UpdateExpression,
                             "ExpressionAttributeNames": ExpressionAttributeNames or {},
                             "ExpressionAttributeValues": ExpressionAttributeValues,
                             "ConditionExpression": ConditionExpression})
        if self.fail_updates:
            raise AwsError(self.fail_updates)
        key = (Key["applicantId"]["S"], Key["sk"]["S"])
        if ConditionExpression == "attribute_exists(applicantId)" and key not in self.items:
            raise AwsError("ConditionalCheckFailedException")
        for v in ExpressionAttributeValues.values():
            # The real client rejects a bare Python value where an AttributeValue belongs.
            assert isinstance(v, dict) and len(v) == 1, v
        item = self.items.setdefault(key, {"applicantId": Key["applicantId"], "sk": Key["sk"]})
        assert UpdateExpression.startswith("SET "), UpdateExpression
        for clause in UpdateExpression[len("SET "):].split(","):
            name, placeholder = (part.strip() for part in clause.split("="))
            name = (ExpressionAttributeNames or {}).get(name, name)
            item[name] = ExpressionAttributeValues[placeholder]
        return {}

    def query(self, *, TableName, KeyConditionExpression, ExpressionAttributeValues,
              FilterExpression=None, Select=None, **_):
        self.queries.append({"TableName": TableName, "FilterExpression": FilterExpression,
                             "ExpressionAttributeValues": ExpressionAttributeValues})
        if self.fail_queries:
            raise AwsError(self.fail_queries)
        pk = ExpressionAttributeValues[":pk"]["S"]
        now = int(ExpressionAttributeValues.get(":now", {"N": "0"})["N"])
        count = sum(1 for expires in self.velocity.get(pk, []) if expires > now)
        return {"Count": count}


class FakeSsm:
    def __init__(self) -> None:
        self.parameters: dict[str, str] = {}
        self.reads = 0
        self.fail: str | None = None

    def get_parameter(self, *, Name, **_):
        self.reads += 1
        if self.fail:
            raise AwsError(self.fail)
        if Name not in self.parameters:
            raise AwsError("ParameterNotFound")
        return {"Parameter": {"Name": Name, "Value": self.parameters[Name]}}


class FakeBoto3:
    def __init__(self, **clients: Any) -> None:
        self.clients = clients

    def client(self, name: str, **_: Any) -> Any:
        if name not in self.clients:
            raise AssertionError(f"unexpected boto3 client {name!r}")
        return self.clients[name]


EVIDENCE_BUCKET = "kyc90-evidence-us-633898796092"
BIOMETRIC_BUCKET = "kyc90-demo-biometric-633898796092"


def seed_applicant(ddb: FakeDynamo, applicant_id: str = "APP-1", *,
                   given: str = "TAYLOR JORDAN", surname: str = "REEVES",
                   dob: str = "1991-05-14", nationality: str = "USA",
                   documents: list[dict] | None = None,
                   liveness: dict | None = None, **extra: Any) -> None:
    ddb.put_case(
        applicant_id,
        caseId=f"CASE-{applicant_id}",
        state="RECEIVED",
        jurisdiction="us",
        declaredIdentity={"givenNames": given, "surname": surname, "dateOfBirth": dob,
                          "nationality": nationality, "address": ""},
        documents=documents if documents is not None else [
            {"type": "PASSPORT", "bucket": EVIDENCE_BUCKET, "key": f"{applicant_id}/PASSPORT-1",
             "issuingCountry": "US"}],
        liveness=liveness if liveness is not None else {
            "result": "PASS", "region": "us-east-1",
            "referenceImageKey": f"{applicant_id}/reference-1.jpg"},
        **extra,
    )


@pytest.fixture
def aws(monkeypatch):
    """A fake boto3 with a case store and Parameter Store. Add other clients per test."""
    ddb = FakeDynamo()
    ssm = FakeSsm()
    fake = FakeBoto3(dynamodb=ddb, ssm=ssm)
    monkeypatch.setitem(sys.modules, "boto3", fake)
    monkeypatch.setenv("CASE_TABLE", "kyc90-cases")
    monkeypatch.setenv("BIOMETRIC_BUCKET", BIOMETRIC_BUCKET)
    monkeypatch.setenv("CONTROL_TTL_SECONDS", "0")
    for name in ("FORCE_UNAVAILABLE", "DEMO_FAULTS_PARAMETER", "LIST_OVERLAY_PARAMETER"):
        monkeypatch.delenv(name, raising=False)
    from kyctools import controls

    controls.reset_cache()
    yield SimpleNamespace(boto3=fake, ddb=ddb, ssm=ssm, AwsError=AwsError,
                          seed=lambda *a, **k: seed_applicant(ddb, *a, **k),
                          EVIDENCE_BUCKET=EVIDENCE_BUCKET, BIOMETRIC_BUCKET=BIOMETRIC_BUCKET)
    controls.reset_cache()
