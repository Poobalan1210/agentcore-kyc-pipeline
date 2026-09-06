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
import re
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
from spans import collect_evidence, evidence_from_case_record
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


_OTEL_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


def _otel_trace_id(supplied: str) -> str:
    """The OTEL trace id to query spans by, preferring a real one over a plausible one.

    Spans in the unified traces destination are keyed by a 32-hex-character OTEL trace id. The
    state machine was passing `$states.context.Execution.Name` here — a value that is always
    present, never a trace id, and matched nothing, so the span query returned empty on every run
    and every decision was refused as incomplete. `$exists`-style plausibility is the trap: the
    field was populated, just not with an id anything is indexed by.

    X-Ray tracing is enabled on the state machine and both Lambdas run in Active mode, so this
    function's own invocation is inside the execution's trace. `_X_AMZN_TRACE_ID` therefore carries
    the same trace as the agent and decision spans, and needs no plumbing through the ASL at all.

    The two formats are the same 128 bits written differently — X-Ray
    `Root=1-6a9bfd6a-6ecd4731cde923a5b99cebd4` is OTEL `6a9bfd6a6ecd4731cde923a5b99cebd4` with the
    version prefix and dashes removed. Verified against a real span in `aws/spans`.
    """
    if _OTEL_TRACE_ID.match(supplied.strip().lower()):
        return supplied.strip().lower()
    header = os.environ.get("_X_AMZN_TRACE_ID", "")
    for part in header.split(";"):
        if part.startswith("Root="):
            root = part[len("Root="):]
            candidate = root.replace("-", "")
            # "1-" version prefix, then 8 hex + 24 hex
            if candidate.startswith("1") and len(candidate) == 33:
                candidate = candidate[1:]
            if _OTEL_TRACE_ID.match(candidate.lower()):
                return candidate.lower()
    return ""


def _spans_for(trace_id: str, *, log_group: str, client: Any) -> list[dict[str, Any]]:
    """Spans for one trace from the configured span log groups (design §9.3, F-20).

    `log_group` is a comma-separated list. Design §9.3 names the per-agent group
    `/aws/bedrock-agentcore/runtimes/{agentId}-{endpointName}` *and* mandates
    `UNIFIED_TRACES_DESTINATION_ENABLED=true`, which are in tension: enabling the unified
    destination is what sends spans to the account-wide `aws/spans` group, and the per-agent groups
    then carry only runtime stdout (verified live — they hold `[runtime-logs]` streams and no
    `spans` stream). Both are queried rather than picking one, so the evidence is found wherever
    the platform actually put it.

    `logs:FilterLogEvents` takes a single `logGroupName`, so each group is queried in turn and the
    results merged.

    Returns an empty list on any failure rather than raising: an unavailable log query must
    produce an *incomplete record*, which is refused loudly, rather than an exception that a
    retry policy might quietly absorb. A group that does not exist raises per group, so one bad
    name cannot mask the others' spans.
    """
    groups = [g.strip() for g in log_group.split(",") if g.strip()]
    if not trace_id or not groups:
        return []
    spans: list[dict[str, Any]] = []
    for group in groups:
        try:
            response = client.filter_log_events(
                logGroupName=group,
                filterPattern=f'{{ $.traceId = "{trace_id}" }}',
                limit=1000,
            )
        except Exception:  # noqa: BLE001 - see docstring
            continue
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
    # Spans first, case record second — not the other way round. If the platform ever does emit
    # reasoning spans they carry strictly more (model/harness versions, span ids), so they win when
    # they are complete. Today they never are: the AgentCore Harness surface emits no span carrying
    # agent_name/tool_name/outcome, verified with Transaction Search ACTIVE and 100% sampling.
    #
    # The fallback is not a weaker record. `phase1Evidence` and the persisted tool results are
    # written synchronously by the state machine that decided the case, so they cannot drift from
    # the decision the way a sampled best-effort log query can. `build_decision_record` still
    # refuses anything incomplete — the gate is unchanged, only the source it can be satisfied from.
    evidence = collect_evidence(_spans_for(
        _otel_trace_id(str(event.get("traceId") or case.get("traceId") or "")),
        log_group=log_group, client=logs))
    if not evidence.complete:
        from_case = evidence_from_case_record(case, decision=decision, phase=phase)
        if from_case.complete or len(from_case.missing) < len(evidence.missing):
            evidence = from_case

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
