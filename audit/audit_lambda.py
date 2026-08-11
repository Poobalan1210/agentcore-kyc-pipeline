"""Lambda entry point for the audit trail — wiring for task 6.2's `WriteDecisionRecord` state.

Thin by design: fetch the two evidence sources, assemble, write. The refusal logic lives in
`record.py` and the Object Lock write in `writer.py`; this module only decides where the spans
come from.

The span query is the part worth explaining. Step Functions hands this Lambda the decision and the
applicant id, not the spans, because the execution output does not contain them (F-13). So the
spans are read from the per-agent CloudWatch log group by trace id. If that query returns nothing,
the record is assembled anyway and comes back as `IncompleteRecord`, which the writer refuses —
a decided case with no audit record then surfaces as a compliance event rather than as silence.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import boto3

from record import (
    EvidenceSource,
    IncompleteRecord,
    VersionPins,
    build_decision_record,
)
from spans import collect_evidence
from writer import put_decision_record


def _case_record(applicant_id: str, *, table: str, client: Any) -> dict[str, Any]:
    response = client.get_item(
        TableName=table,
        Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
        ConsistentRead=True,
    )
    return _unmarshal(response.get("Item") or {})


def _unmarshal(item: dict[str, Any]) -> dict[str, Any]:
    """Minimal DynamoDB unmarshalling — avoids pulling boto3.dynamodb.types into the pure path."""
    out: dict[str, Any] = {}
    for key, value in item.items():
        if not isinstance(value, dict):
            out[key] = value
            continue
        if "S" in value:
            out[key] = value["S"]
        elif "N" in value:
            out[key] = float(value["N"])
        elif "BOOL" in value:
            out[key] = value["BOOL"]
        elif "M" in value:
            out[key] = _unmarshal(value["M"])
        elif "L" in value:
            out[key] = [_unmarshal(v["M"]) if isinstance(v, dict) and "M" in v else v
                        for v in value["L"]]
        else:
            out[key] = value
    return out


def _spans_for(trace_id: str, *, log_group: str, client: Any) -> list[dict[str, Any]]:
    """Spans for one trace from the per-agent log group (design §9.3, F-20).

    Returns an empty list on any failure rather than raising: an unavailable log query must
    produce an *incomplete record*, which is refused loudly, rather than an exception that a
    retry policy might quietly absorb.
    """
    if not trace_id or not log_group:
        return []
    try:
        response = client.filter_log_events(
            logGroupName=log_group,
            filterPattern=f'{{ $.traceId = "{trace_id}" }}',
            limit=1000,
        )
    except Exception:  # noqa: BLE001 - see docstring
        return []
    spans: list[dict[str, Any]] = []
    for event in response.get("events", []):
        try:
            spans.append(json.loads(event["message"]))
        except (json.JSONDecodeError, KeyError):
            continue
    return spans


def lambda_handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    table = os.environ["CASE_TABLE_NAME"]
    bucket = os.environ["AUDIT_BUCKET_NAME"]
    log_group = os.environ.get("SPAN_LOG_GROUP", "")
    kms_key_id = os.environ.get("AUDIT_KMS_KEY_ID") or None

    applicant_id = str(event.get("applicantId") or "")
    phase = str(event.get("phase") or "phase1")
    decision = event.get("decision") or {}
    if isinstance(decision, str):
        decision = json.loads(decision)

    ddb = boto3.client("dynamodb")
    logs = boto3.client("logs")

    case = _case_record(applicant_id, table=table, client=ddb)
    evidence = collect_evidence(_spans_for(
        str(event.get("traceId") or case.get("traceId") or ""),
        log_group=log_group, client=logs))

    decided_at = datetime.now(timezone.utc)
    record = build_decision_record(
        case_record=case,
        evidence=evidence,
        outcome=decision,
        versions=VersionPins(
            harness_version=str(event.get("harnessVersion")
                                or os.environ.get("HARNESS_VERSION", "")),
            endpoint_name=str(event.get("endpointName")
                              or os.environ.get("HARNESS_ENDPOINT_NAME", "live")),
            policy_engine_version=str(event.get("policyEngineVersion")
                                      or os.environ.get("POLICY_ENGINE_VERSION", "")),
            gateway_arn=str(event.get("gatewayArn") or case.get("gatewayArn") or ""),
        ),
        decision_id=str(decision.get("decisionId") or uuid.uuid4()),
        decided_at=decided_at.isoformat().replace("+00:00", "Z"),
        phase=phase,
        sources=[EvidenceSource.OTEL_SPANS, EvidenceSource.CASE_RECORD],
        # A-05/task 7.3. `WriteDecisionRecord` (asl.py) sends this as a number whenever
        # `NormalizeInput` measured a real Pipe-sourced enqueue timestamp, and omits it (so this
        # is `None`) on the direct-StartExecution path — never a fabricated 0.
        ingress_latency_ms=(
            float(event["ingressLatencyMs"])
            if isinstance(event.get("ingressLatencyMs"), (int, float)) else None
        ),
    )

    if isinstance(record, IncompleteRecord):
        # Raised, not returned. The state machine's Catch routes this to AuditWriteFailed, which
        # puts the case in front of a human — a decided case without an immutable record is a
        # compliance event, and returning success here would hide it (R18.3).
        raise RuntimeError(
            "incomplete audit record for %s/%s: %s missing=%s"
            % (applicant_id, phase,
               ", ".join(r.value for r in record.reasons), list(record.missing)))

    put_decision_record(
        record, bucket=bucket, decided_at=decided_at, kms_key_id=kms_key_id)
    return {"decisionId": record.decision_id, "applicantId": applicant_id, "phase": phase}
