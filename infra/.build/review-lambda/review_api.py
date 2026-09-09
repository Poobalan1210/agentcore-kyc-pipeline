"""`GET /review/queue`, `POST /review/{caseId}/decision`, `GET /audit/{applicantId}` — task 14.3.

The decision endpoint is a thin proxy over `review/review_handler.py`, already built and tested
this session (task 6.5/8.5's HITL receiver) — this module does not reimplement first-writer-wins
or task-token handling, it only shapes the HTTP event into what `handle_review` expects and
gates the route to the `reviewers` Cognito group (R34.6), which `review_handler.py` itself does
not do since it is also invoked directly from the SQS `.waitForTaskToken` receiver path.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

# In the Lambda deployment package, `review_handler.py` (and its own dependencies —
# `decisions.py`, `metrics.py`) are copied alongside this module by `api_stack.py`'s
# `_review_lambda_asset_path()`, so this is a flat-package import, not a cross-directory one.
# Locally (tests, `api/tests/conftest.py`), the same import resolves because `pythonpath` in the
# root `pytest.ini` includes `review` directly.
from review_handler import handle_review  # noqa: E402

#: R34.1 — cases in AWAITING_REVIEW, via the `state` GSI (design §9.1), the same index
#: `review/metrics.py` reads for queue depth/age.
_STATE_INDEX = "state-index"


def list_queue(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`GET /review/queue`. R34.1/R34.5 — ordered by age, with depth exposed.

    The `taskToken` a decision must be submitted with (design §7.5, `asl.py`'s `ManualReview`
    state) exists only in the SQS message `kyc90-review` carries, never on the case record — the
    state machine writes it into the message body, not into DynamoDB. This merges the two
    sources by `applicantId` so the console can act without a second round trip. SQS
    `ReceiveMessage` does not delete: the message becomes invisible for its visibility timeout
    and reappears if nobody acts, which is the same "still in the queue until decided" property
    the console needs, not a side effect to work around.
    """
    if not _is_reviewer(event):
        return _response(403, {"error": "REVIEWERS_ONLY"})

    import boto3

    ddb = boto3.client("dynamodb")
    resp = ddb.query(
        TableName=os.environ["CASE_TABLE_NAME"],
        IndexName=_STATE_INDEX,
        KeyConditionExpression="#s = :awaiting",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":awaiting": {"S": "AWAITING_REVIEW"}},
        # Ascending sort-key order on createdAt puts the oldest case first — the sort key the
        # GSI is built on (`foundation_stack.py`), so age ordering costs nothing extra to derive.
        ScanIndexForward=True,
    )
    items = resp.get("Items", [])
    cases = [_project_queued_case(item) for item in items]
    items_by_applicant = {
        _s(item.get("applicantId")): item for item in items if item.get("applicantId")
    }

    # The token comes from the case record, written once by `review/token_materializer.py` when
    # the review message was handed off. It used to be discovered by calling `ReceiveMessage` here
    # on every request, which cannot work: each read spends the message's finite redrive budget
    # (maxReceiveCount=5, so the sixth *view* of a case sent a live token to the DLQ), reads are
    # mutually exclusive for the visibility timeout, and short polling reported an empty queue
    # while five tokens sat on it. A DynamoDB read has none of those properties.
    for case in cases:
        token = _s(items_by_applicant.get(case["applicantId"], {}).get("reviewTaskToken"))
        if token:
            case["taskToken"] = token

    return _response(200, {"depth": len(cases), "cases": cases})


#: DO NOT rediscover review task tokens by reading the queue.
#:
#: A `_peek_task_tokens` helper used to live here and did exactly that. It is gone because the
#: value now lives on the case record, written once by `review/token_materializer.py`, where
#: reading it has no side effects. The lesson, without the loaded gun:
#:
#: * Every `ReceiveMessage` increments ApproximateReceiveCount, and `kyc90-review` carries
#:   `maxReceiveCount: 5` -- so the sixth VIEW of a case moved a LIVE Step Functions task
#:   token to the DLQ. Observed twice against the deployed stack: five waiting cases vanished
#:   from the reviewer queue while their executions stayed alive.
#: * A non-zero VisibilityTimeout made the console starve itself -- every list hid every message
#:   from the next poll and from any second reviewer. Releasing to 0 instead removed the only
#:   throttle on that redrive budget. There is no safe value: reading is the wrong mechanism.
#: * `WaitTimeSeconds=0` samples only a subset of the servers holding the queue, so an empty
#:   response is not evidence of an empty queue. It returned 0 tokens against 5 visible messages.
#:
#: `test_listing_the_queue_never_reads_from_sqs` pins this so it cannot come back quietly.


def submit_decision(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`POST /review/{caseId}/decision`. Delegates to `handle_review` — the same receiver the
    SQS `.waitForTaskToken` path (`asl.py`'s `ManualReview` state) invokes, so a reviewer acting
    through this HTTP route and a reviewer acting through some future direct-queue tool get
    identical first-writer-wins and audit-trail behaviour (R6.5, R21.4)."""
    if not _is_reviewer(event):
        return _response(403, {"error": "REVIEWERS_ONLY"})

    path = event.get("pathParameters") or {}
    body = _body(event)
    body = dict(body)
    body.setdefault("caseId", path.get("caseId"))
    merged_event = dict(event)
    merged_event["body"] = json.dumps(body)
    return handle_review(merged_event)


#: Case-record attributes projected verbatim into the trace response, and the human label shown
#: for each in the reviewer console. Named explicitly rather than projecting the whole item: the
#: case record also carries `documents[].bucket`/`key` (S3 locations, not reviewer content) and
#: raw DynamoDB typing, neither of which belongs in a UI payload.
_FINDING_ATTRIBUTES_BY_AGENT = {
    "router": "findingRouter",
    "documents": "findingDocuments",
    "identity": "findingIdentity",
    "prescreen": "findingPrescreen",
    "fraud": "findingFraud",
    "compliance": "findingCompliance",
    "notify": "findingNotify",
}


def get_trace(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`GET /review/{caseId}/trace` — task 14.9, the reviewer trace view.

    Three sources, all already written by the pipeline for other reasons, assembled here rather
    than duplicated:

    * **Per-agent findings** — the flat `finding<Agent>` attributes each agent's `submit_*` output
      contract wrote (R11.13). Structured JSON, not prose, because the whole point of the output
      contracts is that an agent's result is data rather than something to be paraphrased for a
      reviewer.
    * **The decision audit record** — `audit/record.py`'s object under the applicant's S3 prefix,
      which carries the Step Functions execution name (`traceId`) and the full decision payload,
      including `identityMatch` from `decision-engine/identity_match.py`.
    * **Step Functions execution history** — fetched only once `traceId` is known, since the
      ingress pipe starts executions with an auto-generated name rather than one derived from
      applicantId (design §10.2: no enrichment at the pipe). A case with no decision yet has no
      `traceId` and this section is simply absent, not an error.

    R18.2's audience is auditors, not applicants, which is why this route exists separately from
    `GET /cases/{id}` and is gated to reviewers exactly like `get_audit`. Two things are
    deliberately never surfaced even here: raw agent prose presented as if it were the decision
    (every finding is labelled as an input, the decision fields are labelled as the outcome), and
    any raw confidence score (F-44/R33.2) — this route reads the same bucketed values the
    applicant-facing route does, nothing rawer.
    """
    if not _is_reviewer(event):
        return _response(403, {"error": "REVIEWERS_ONLY"})

    path = event.get("pathParameters") or {}
    case_id = str(path.get("caseId") or "")
    if not case_id:
        return _response(400, {"error": "MISSING_CASE_ID"})

    import boto3

    ddb = boto3.client("dynamodb")
    # The case table's partition key is applicantId, not caseId, and `caseId` is `CASE-{id}`
    # (`applicants.py`'s `create_applicant`) — so the id is recovered rather than requiring a
    # second index for what is a fixed, reversible transform.
    applicant_id = case_id[len("CASE-"):] if case_id.startswith("CASE-") else case_id
    item = ddb.get_item(
        TableName=os.environ["CASE_TABLE_NAME"],
        Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
        ConsistentRead=True,
    ).get("Item")
    if not item:
        return _response(404, {"error": "CASE_NOT_FOUND"})

    applicant = {
        "applicantId": applicant_id,
        "caseId": _s(item.get("caseId")),
        "state": _s(item.get("state")),
        "jurisdiction": _s(item.get("jurisdiction")),
        "productApplied": _s(item.get("productApplied")),
        "createdAt": _s(item.get("createdAt")),
        "documents": _document_summaries(item.get("documents")),
        "declaredIdentity": _declared_identity(item.get("declaredIdentity")),
        "liveness": _liveness_summary(item.get("liveness")),
    }

    agents = []
    for agent, attribute in _FINDING_ATTRIBUTES_BY_AGENT.items():
        raw = item.get(attribute)
        finding = _parse_finding(raw)
        if finding is None:
            continue
        agents.append({"agent": agent, "finding": finding})

    # Fallback to the raw Parallel-branch output when no agent recorded a structured `submit_*`
    # finding at all. This is the exact gap a live run exposed: an agent can complete without
    # failing and still never call its output contract tool — its reasoning lands in
    # `phase1Evidence` (`asl.py`'s `PersistEvidence`) as raw assistant text, and the reviewer
    # otherwise sees "no agent has recorded a finding" for a case that visibly has agent output,
    # just not in the structured shape. Labelled distinctly (`raw: true`) so a reviewer can tell
    # this is unstructured prose and not a schema-validated finding — the distinction R11.13 is
    # built around.
    if not agents:
        agents = _agents_from_raw_evidence(item.get("phase1Evidence"))

    document_extraction = _parse_json_attr(item.get("documentExtraction"))

    decision_record, trace_id = _latest_decision_record(applicant_id)

    execution = None
    if trace_id:
        execution = _execution_timeline(trace_id)

    return _response(200, {
        "applicant": applicant,
        "agents": agents,
        "documentExtraction": document_extraction,
        "decision": decision_record,
        "execution": execution,
    })


def _document_summaries(attr: Any) -> list[dict[str, Any]]:
    """Type/issuingCountry/synthetic only. Never the S3 key/bucket — those are locations, not
    content a reviewer needs to see in this view (they already have `GET /audit/{id}` for that)."""
    if not isinstance(attr, Mapping) or "L" not in attr:
        return []
    out = []
    for entry in attr["L"]:
        fields = entry.get("M") or {}
        out.append({
            "type": _s(fields.get("type")),
            "issuingCountry": _s(fields.get("issuingCountry")),
        })
    return out


def _declared_identity(attr: Any) -> dict[str, str] | None:
    if not isinstance(attr, Mapping) or "M" not in attr:
        return None
    fields = attr["M"]
    return {
        "surname": _s(fields.get("surname")) or "",
        "givenNames": _s(fields.get("givenNames")) or "",
        "dateOfBirth": _s(fields.get("dateOfBirth")) or "",
        "nationality": _s(fields.get("nationality")) or "",
    }


def _liveness_summary(attr: Any) -> dict[str, Any] | None:
    """Result and region only. The reference/audit image keys stay off this view — this is a
    decision summary, not the biometric evidence browser (R36's minimisation intent extends to
    what is displayed, not only to what is stored)."""
    if not isinstance(attr, Mapping) or "M" not in attr:
        return None
    fields = attr["M"]
    return {
        "result": _s(fields.get("result")),
        "region": _s(fields.get("region")),
        "retrievedAt": _s(fields.get("retrievedAt")),
    }


def _agents_from_raw_evidence(attr: Any) -> list[dict[str, Any]]:
    """Best-effort fallback: the raw `[{"agent": ..., "content": [...]}]` array `PersistEvidence`
    writes from the Phase 1 `Parallel` state's own result, used only when no agent recorded a
    structured `submit_*` finding at all.

    Every entry is marked `raw: True` and its text is extracted verbatim (including any
    `<thinking>` preamble) rather than parsed as JSON — this is deliberately not promoted to
    look like a structured finding, because doing so would blur exactly the distinction R11.13
    exists to preserve: a schema-validated tool call versus an assistant's prose.
    """
    raw = attr.get("S") if isinstance(attr, Mapping) else attr
    if not raw:
        return []
    try:
        branches = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(branches, list):
        return []

    out = []
    for branch in branches:
        if not isinstance(branch, Mapping):
            continue
        agent = branch.get("agent")
        if not agent:
            continue
        text_parts = []
        for block in branch.get("content") or []:
            if isinstance(block, Mapping) and "Text" in block:
                text_parts.append(str(block["Text"]))
        out.append({
            "agent": str(agent),
            "finding": {
                "raw": True,
                "text": "\n".join(text_parts) if text_parts else None,
                "status": branch.get("status"),
                "stopReason": branch.get("stopReason"),
            },
        })
    return out


def _parse_finding(attr: Any) -> Any:
    raw = attr.get("S") if isinstance(attr, Mapping) else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


def _parse_json_attr(attr: Any) -> Any:
    if isinstance(attr, Mapping):
        raw = attr.get("S")
    else:
        raw = attr
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _latest_decision_record(applicant_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """The most recent audit object under the applicant's prefix, and its `traceId`.

    Reuses `record.py`'s own key layout (`{applicantId}/{phase}/{decisionId}.json`, design §9.2)
    rather than inventing a second index — the same reasoning `get_audit` already applies. "Most
    recent" is by S3's `LastModified`, since `decisionId` is not guaranteed sortable and a case can
    accumulate a Phase 1 record and later a Phase 2 one.
    """
    bucket = os.environ.get("AUDIT_BUCKET_NAME")
    if not bucket:
        return None, None

    import boto3

    s3 = boto3.client("s3")
    # Decision records land at `{applicantId}/{phase}/{decisionId}.json` (`record.py`'s
    # `decision_object_key`, design §9.2), while uploaded documents land directly under
    # `{applicantId}/{docType}-{hex}` in the same bucket with no `.json` suffix. Listing the bare
    # `{applicantId}/` prefix picks up both — found live: it returned an uploaded passport image and
    # the JSON parse failed on the binary with a `UnicodeDecodeError`. Filtering on the suffix is
    # cheap insurance against that same object shape ever recurring, and it costs nothing when the
    # bucket only holds what is expected.
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=f"{applicant_id}/")
    objects = sorted(
        (o for o in listed.get("Contents", []) if o["Key"].endswith(".json")),
        key=lambda o: o["LastModified"], reverse=True)
    if not objects:
        return None, None

    body = s3.get_object(Bucket=bucket, Key=objects[0]["Key"])["Body"].read()
    try:
        record = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        # A malformed or unexpected object under the prefix must not fail the whole trace view —
        # the applicant/agent sections are independently useful even with no decision to show.
        return None, None
    return record, record.get("traceId")


def _execution_timeline(trace_id: str) -> dict[str, Any] | None:
    """State-by-state timeline for the Phase 1 execution named `trace_id`.

    `states:GetExecutionHistory` returns raw AWS event objects (`TaskStateEntered`,
    `TaskSucceeded`, ...); this collapses them into one row per state with a duration, which is
    what the reviewer console renders. Read-only and scoped to this one state machine's
    executions — see `api_stack.py`'s `ReadExecutionHistoryForTrace` grant.
    """
    machine_arn = os.environ.get("PHASE1_STATE_MACHINE_ARN")
    if not machine_arn:
        return None

    import boto3

    region = os.environ.get("AWS_REGION", "us-east-1")
    account = os.environ.get("AWS_ACCOUNT_ID", "")
    state_machine_name = machine_arn.split(":")[6]
    execution_arn = (
        f"arn:aws:states:{region}:{account}:execution:{state_machine_name}:{trace_id}")

    sfn = boto3.client("states")
    try:
        description = sfn.describe_execution(executionArn=execution_arn)
    except Exception:  # noqa: BLE001 - any failure here degrades this section, not the whole trace
        # The execution may have expired from Step Functions' own history retention, or this
        # decision may predate the trace_id being recorded. Either way the rest of the trace view
        # (agents, decision, applicant) is still valid and returned; only this section is absent.
        return None

    events = sfn.get_execution_history(
        executionArn=execution_arn, reverseOrder=False, maxResults=1000)["events"]

    steps: list[dict[str, Any]] = []
    open_steps: dict[str, dict[str, Any]] = {}
    for ev in events:
        etype = ev["type"]
        if etype == "TaskStateEntered" or etype == "ParallelStateEntered":
            name = (ev.get("stateEnteredEventDetails") or {}).get("name", "")
            open_steps[name] = {"name": name, "startedAt": ev["timestamp"].isoformat(),
                                "status": "RUNNING"}
        elif etype in ("TaskStateExited", "ParallelStateExited"):
            name = (ev.get("stateExitedEventDetails") or {}).get("name", "")
            step = open_steps.pop(name, {"name": name, "status": "RUNNING"})
            step["endedAt"] = ev["timestamp"].isoformat()
            if step.get("status") == "RUNNING":
                step["status"] = "SUCCEEDED"
            steps.append(step)
        elif etype in ("TaskFailed", "ExecutionFailed", "LambdaFunctionFailed"):
            # Attribute the failure to whatever state was open when it happened, so a degraded
            # path (e.g. DegradedPath) is visible as *why*, not only that the branch ended.
            for name, step in list(open_steps.items()):
                step["status"] = "FAILED"

    return {
        "executionArn": execution_arn,
        "status": description.get("status"),
        "startDate": description["startDate"].isoformat(),
        "stopDate": (description["stopDate"].isoformat()
                    if description.get("stopDate") else None),
        "steps": steps,
    }


def get_audit(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`GET /audit/{applicantId}`. R18.1 — decision chain from OTEL spans plus the case record,
    reusing the same audit assembly `audit/audit_lambda.py` and `audit/record.py` already build
    at decision time, rather than a second, divergeable reconstruction. Gated to reviewers —
    the audit record is denser PII than the applicant-facing `/cases/{id}` projection (R18.8)."""
    if not _is_reviewer(event):
        return _response(403, {"error": "REVIEWERS_ONLY"})

    path = event.get("pathParameters") or {}
    applicant_id = str(path.get("applicantId") or "")
    if not applicant_id:
        return _response(400, {"error": "MISSING_APPLICANT_ID"})

    import boto3

    bucket = os.environ.get("AUDIT_BUCKET_NAME")
    if not bucket:
        return _response(500, {"error": "AUDIT_BUCKET_NOT_CONFIGURED"})
    s3 = boto3.client("s3")
    # `record.py`'s `decision_object_key` writes `{applicantId}/{phase}/{decisionId}.json`
    # (design §9.2) — listing under the applicant's own prefix reuses that exact layout rather
    # than inventing a second indexing scheme for the same objects.
    prefix = f"{applicant_id}/"
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    records = []
    for obj in listed.get("Contents", []):
        body_resp = s3.get_object(Bucket=bucket, Key=obj["Key"])
        records.append(json.loads(body_resp["Body"].read()))
    return _response(200, {"applicantId": applicant_id, "decisions": records})


def _project_queued_case(item: Mapping[str, Any]) -> dict[str, Any]:
    """The queue row the console renders and submits against.

    `phase` is projected because the console has no other way to know it. `QueuedCaseSummary`
    declares it optional and `DecisionForm` does `queuedCase.phase ?? "phase1"`, so omitting it
    here did not fail loudly -- it silently recorded every review, including Phase 2 compliance
    reviews, as `phase1Review`, and made `existing_review`/`preliminary_tier` read the wrong slot.
    Written by `token_materializer.py` from the phase `asl.py` puts in the handoff message.
    """
    return {
        "applicantId": _s(item.get("applicantId")),
        "caseId": _s(item.get("caseId")),
        "jurisdiction": _s(item.get("jurisdiction")),
        "createdAt": _s(item.get("createdAt")),
        "phase": _s(item.get("reviewPhase")) or "phase1",
    }


def _s(attr: Any) -> str | None:
    if isinstance(attr, Mapping):
        return attr.get("S")
    return None


def _is_reviewer(event: Mapping[str, Any]) -> bool:
    """R34.6 — reviewer identity from Cognito. The HTTP API JWT authorizer surfaces the
    `cognito:groups` claim; membership in `reviewers` (task 14.1) is the gate."""
    context = event.get("requestContext") or {}
    authorizer = context.get("authorizer") or {}
    jwt = authorizer.get("jwt") or {}
    claims = jwt.get("claims") or authorizer.get("claims") or {}
    groups = claims.get("cognito:groups") or ""
    if isinstance(groups, list):
        return "reviewers" in groups
    return "reviewers" in str(groups)


def _body(event: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = event.get("body")
    if isinstance(raw, str) and raw:
        return json.loads(raw)
    if isinstance(raw, Mapping):
        return raw
    return {}


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    route = (event.get("requestContext") or {}).get("routeKey", "")
    if route.startswith("GET /review/queue"):
        return list_queue(event, context)
    # Checked before the generic `POST /review/` decision route below, since both start with the
    # same prefix and routeKey matching here is by string, not by the API Gateway path template.
    if route.startswith("GET /review/") and route.endswith("/trace"):
        return get_trace(event, context)
    if route.startswith("POST /review/"):
        return submit_decision(event, context)
    if route.startswith("GET /audit/"):
        return get_audit(event, context)
    return _response(404, {"error": "NOT_FOUND", "route": route})
