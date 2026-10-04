"""`GET /review/queue`, `POST /review/{caseId}/decision`, `GET /review/{caseId}/trace`,
`GET /audit/{applicantId}` — the reviewer console's API.

The decision endpoint is a thin proxy over `review/review_handler.py`. This module does not
reimplement first-writer-wins or task-token handling; it shapes the HTTP event into what
`handle_review` expects and gates every route to the `reviewers` Cognito group (R34.6), checked
here exactly rather than by substring (`auth.py`).

The trace view shows a reviewer what the rules decided on: the facts the tools recorded, each
phase's decision record, the briefing written for them, the alerts earlier reviewers cleared, and
the account as the platform holds it. The agents' replies are shown too, labelled as narrative —
they explain the facts and never decided anything.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

import auth
import notifications

# In the Lambda deployment package, `review_handler.py` (and its own dependencies —
# `decisions.py`, `metrics.py`) are copied alongside this module by `api_stack.py`'s
# `_review_lambda_asset_path()`, so this is a flat-package import, not a cross-directory one.
# Locally, the same import resolves because `pythonpath` in the root `pytest.ini` includes
# `review` directly.
from review_handler import handle_review  # noqa: E402

#: R34.1 — cases in AWAITING_REVIEW, via the `state` GSI (design §9.1).
_STATE_INDEX = "state-index"

#: Ceiling on one queue listing. A queue deeper than this is an incident, not a page to render.
_QUEUE_LIMIT = 200

#: Decision records live at `{applicantId}/{phase}/{decisionId}.json` (`record.py`'s
#: `decision_object_key`). Uploaded documents share the bucket at `{applicantId}/{type}-{hex}`, and
#: reading one of those as JSON crashed the audit view on the first case with a passport attached.
_DECISION_KEY = re.compile(r"^[^/]+/phase[123]/[^/]+\.json$")

#: Ceiling on records returned by one audit call. Paginated underneath, capped on top.
_AUDIT_LIMIT = 100

#: The facts the rules decided on, by the name the console shows them under.
_FACTS = {
    "documentExtraction": "documentExtraction",
    "faceMatch": "factFaceMatch",
    "screening": "factScreening",
    "velocity": "factVelocity",
    "pep": "factPep",
    "adverseMedia": "factAdverseMedia",
}

_PHASE_MACHINE_ENV = {"phase1": "PHASE1_STATE_MACHINE_ARN",
                      "phase2": "PHASE2_STATE_MACHINE_ARN",
                      "phase3": "PHASE3_STATE_MACHINE_ARN"}


def _is_reviewer(event: Mapping[str, Any]) -> bool:
    """R34.6 — an exact member of `reviewers`, from the verified claims."""
    return auth.is_member(event, auth.REVIEWERS)


def list_queue(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`GET /review/queue`. R34.1/R34.5 — ordered by age, with depth exposed.

    The task token a decision is submitted with comes from the case record, written once by
    `review/token_materializer.py` when the review was handed off.
    """
    if not _is_reviewer(event):
        return _response(403, {"error": "REVIEWERS_ONLY"})

    import boto3

    ddb = boto3.client("dynamodb")
    items: list[Mapping[str, Any]] = []
    kwargs: dict[str, Any] = {
        "TableName": os.environ["CASE_TABLE_NAME"],
        "IndexName": _STATE_INDEX,
        "KeyConditionExpression": "#s = :awaiting",
        "ExpressionAttributeNames": {"#s": "state"},
        "ExpressionAttributeValues": {":awaiting": {"S": "AWAITING_REVIEW"}},
        # Ascending on createdAt puts the oldest case first, at no extra cost.
        "ScanIndexForward": True,
    }
    while True:
        resp = ddb.query(**kwargs)
        items.extend(resp.get("Items", []))
        if not resp.get("LastEvaluatedKey") or len(items) >= _QUEUE_LIMIT:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    cases = [_project_queued_case(item) for item in items[:_QUEUE_LIMIT]]
    return _response(200, {"depth": len(cases), "cases": cases})


#: DO NOT rediscover review task tokens by reading the queue.
#:
#: A `_peek_task_tokens` helper used to live here and did exactly that. It is gone because the
#: value now lives on the case record, written once by `review/token_materializer.py`, where
#: reading it has no side effects. The lesson, without the loaded gun:
#:
#: * Every `ReceiveMessage` increments ApproximateReceiveCount, and `kyc90-review` carries
#:   `maxReceiveCount: 5` -- so the sixth VIEW of a case moved a LIVE Step Functions task
#:   token to the DLQ. Observed twice against the deployed stack.
#: * A non-zero VisibilityTimeout made the console starve itself; releasing to 0 removed the only
#:   throttle on that redrive budget. There is no safe value: reading is the wrong mechanism.
#: * `WaitTimeSeconds=0` samples only a subset of the servers holding the queue, so an empty
#:   response is not evidence of an empty queue.
#:
#: `test_listing_the_queue_never_reads_from_sqs` pins this so it cannot come back quietly.


def submit_decision(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`POST /review/{caseId}/decision`. Delegates to `handle_review`, so a reviewer acting through
    this route gets the same first-writer-wins and audit behaviour as any other caller."""
    if not _is_reviewer(event):
        return _response(403, {"error": "REVIEWERS_ONLY"})

    path = event.get("pathParameters") or {}
    body = dict(_body(event))
    body.setdefault("caseId", path.get("caseId"))
    merged_event = dict(event)
    merged_event["body"] = json.dumps(body)
    return handle_review(merged_event)


def get_trace(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`GET /review/{caseId}/trace` — everything a reviewer needs to decide one case.

    R18.2's audience is auditors, not applicants, which is why this route is separate from
    `GET /cases/{id}` and gated to reviewers. Never surfaced even here: S3 locations, biometric
    image keys, a document's raw text lines, and any raw confidence score (F-44) — the facts carry
    bands, and that is what is shown.
    """
    if not _is_reviewer(event):
        return _response(403, {"error": "REVIEWERS_ONLY"})

    path = event.get("pathParameters") or {}
    case_id = str(path.get("caseId") or "")
    if not case_id:
        return _response(400, {"error": "MISSING_CASE_ID"})

    import boto3

    ddb = boto3.client("dynamodb")
    # `caseId` is `CASE-{applicantId}` (`applicants.create_applicant`), a fixed and reversible
    # transform, so no second index is needed to find the record.
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
        "riskState": _s(item.get("riskState")),
        "jurisdiction": _s(item.get("jurisdiction")),
        "productApplied": _s(item.get("productApplied")),
        "createdAt": _s(item.get("createdAt")),
        "documents": _document_summaries(item.get("documents")),
        "declaredIdentity": _declared_identity(item.get("declaredIdentity")),
        "liveness": _liveness_summary(item.get("liveness")),
        "livenessFailures": _n(item.get("livenessFailures")),
        "previousAttempts": len((item.get("phase1History") or {}).get("L") or []),
    }

    facts = {name: _fact(item.get(attribute)) for name, attribute in _FACTS.items()}
    decisions = {phase: _parse_json_attr(item.get(phase))
                 for phase in ("phase1", "phase2", "phase3") if phase in item}
    reviews = {phase: _plain(item.get(f"{phase}Review"))
               for phase in ("phase1", "phase2", "phase3") if f"{phase}Review" in item}

    decision_record, record_phase, execution_name = _latest_decision_record(applicant_id)
    # One read per execution per request: the latest record's execution is usually also its
    # phase's latest, and the two lookups below share it.
    memo: dict[str, Any] = {}
    execution = (_execution_timeline(record_phase, execution_name, memo=memo)
                 if execution_name else None)
    executions = _phase_executions(item, record_phase, execution_name, memo=memo)

    return _response(200, {
        "applicant": applicant,
        "review": {
            "phase": _s(item.get("reviewPhase")),
            "reason": _s(item.get("reviewReason")),
            "queuedAt": _s(item.get("reviewQueuedAt")),
            "briefing": _parse_json_attr(item.get("reviewBriefing")),
        },
        "facts": facts,
        # Kept at the top level for consoles built against the earlier response.
        "documentExtraction": facts["documentExtraction"],
        "decisions": decisions,
        "reviews": reviews,
        "screeningDispositions": _parse_json_attr(item.get("screeningDispositions")) or [],
        "agents": _agent_narrative(item),
        "notifications": notifications.reviewer_view(_plain(item.get("notifications")) or []),
        "account": _account(applicant_id),
        "decision": decision_record,
        "execution": execution,
        # Every phase's latest execution, read while it runs: the live view's timeline. Null for a
        # phase that has not started (or a machine this deployment does not configure).
        "executions": executions,
        # Every state change the case went through, oldest first, as the state machines and the
        # API wrote it: the console's process view times each step from these, so its clock is
        # the case record's, never the browser's.
        "transitions": _transitions(item.get("transitions")),
        "phaseStartedAt": {phase: _s(item.get(f"{phase}StartedAt"))
                           for phase in ("phase1", "phase2", "phase3")
                           if f"{phase}StartedAt" in item},
    })


def _transitions(attr: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The case's state-change log, keeping only the fields a reviewer reads, oldest first."""
    rows = _plain(attr) or []
    out = [{key: row.get(key) for key in ("from", "to", "at", "actor", "reason")}
           for row in rows if isinstance(row, Mapping) and row.get("to")]
    return sorted(out, key=lambda row: str(row.get("at") or ""))


#: Fields of a recorded fact that are not shown here. `textLines` is the document's printed text:
#: the verified machine-readable fields are what the rules used, and the printed text is the one
#: place an applicant writes freely. `documentKey` (face match) and `document.bucket` /
#: `document.key` (extraction) are storage locations, which this view never carries.
_FACT_FIELDS_WITHHELD = frozenset({"textLines", "documentKey"})
_DOCUMENT_FIELDS_WITHHELD = frozenset({"bucket", "key"})


def _fact(attr: Any) -> Any:
    """A recorded fact as the reviewer sees it: everything the rules read, minus text and
    locations."""
    fact = _parse_json_attr(attr)
    if not isinstance(fact, dict):
        return fact
    shown = {k: v for k, v in fact.items() if k not in _FACT_FIELDS_WITHHELD}
    if isinstance(shown.get("document"), dict):
        shown["document"] = {k: v for k, v in shown["document"].items()
                             if k not in _DOCUMENT_FIELDS_WITHHELD}
    return shown


def _agent_narrative(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What each agent said, per phase. Narrative only: no outcome was read from it."""
    out = []
    for phase in ("phase1", "phase2", "phase3"):
        runs = _parse_json_attr(item.get(f"{phase}Evidence"))
        if not isinstance(runs, list):
            continue
        for run in runs:
            if not isinstance(run, Mapping) or not run.get("agent"):
                continue
            text = "\n".join(str(block.get("Text")) for block in run.get("content") or ()
                             if isinstance(block, Mapping) and block.get("Text"))
            out.append({
                "phase": phase,
                "agent": str(run["agent"]),
                "status": run.get("status"),
                "stopReason": run.get("stopReason"),
                "latencyMs": run.get("latencyMs"),
                "totalTokens": run.get("totalTokens"),
                # The model the run's task asked for (`asl._ok_record`); absent on older records.
                "model": run.get("model"),
                # A degraded run says why it has nothing to show (`asl._degraded_record`).
                "reason": run.get("reason"),
                "narrative": text or None,
            })
    return out


def _account(applicant_id: str) -> dict[str, Any] | None:
    """The account as the platform holds it, or None if there is none or it cannot be read."""
    function = os.environ.get("ACCOUNT_FUNCTION_NAME")
    if not function:
        return None
    import boto3

    try:
        resp = boto3.client("lambda").invoke(
            FunctionName=function,
            Payload=json.dumps({"operation": "getAccount",
                                "arguments": {"applicantId": applicant_id}}).encode())
        if resp.get("FunctionError"):
            return None
        return json.loads(resp["Payload"].read()).get("account")
    except Exception:  # noqa: BLE001 - the rest of the trace stands without the account view
        return None


def _document_summaries(attr: Any) -> list[dict[str, Any]]:
    """Type and issuing country only. Never the S3 key or bucket — locations, not content."""
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
    """Result and region only. Image keys stay off this view (R36's minimisation applies to what
    is displayed, not only to what is stored)."""
    if not isinstance(attr, Mapping) or "M" not in attr:
        return None
    fields = attr["M"]
    return {
        "result": _s(fields.get("result")),
        "region": _s(fields.get("region")),
        "retrievedAt": _s(fields.get("retrievedAt")),
    }


def _parse_json_attr(attr: Any) -> Any:
    raw = attr.get("S") if isinstance(attr, Mapping) else attr
    if not raw or not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _decision_objects(s3: Any, bucket: str, applicant_id: str) -> list[dict[str, Any]]:
    """Every decision record under the applicant's prefix, newest first, documents excluded."""
    objects: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": f"{applicant_id}/"}
    while True:
        listed = s3.list_objects_v2(**kwargs)
        objects.extend(o for o in listed.get("Contents", [])
                       if _DECISION_KEY.match(o["Key"])
                       and o["Key"].startswith(f"{applicant_id}/"))
        token = listed.get("NextContinuationToken")
        if not listed.get("IsTruncated") or not token:
            break
        kwargs["ContinuationToken"] = token
    return sorted(objects, key=lambda o: o["LastModified"], reverse=True)


def _read_record(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        record = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) else None


def _latest_decision_record(applicant_id: str
                            ) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """The most recent decision record, its phase, and the execution that wrote it."""
    bucket = os.environ.get("AUDIT_BUCKET_NAME")
    if not bucket:
        return None, None, None

    import boto3

    s3 = boto3.client("s3")
    for obj in _decision_objects(s3, bucket, applicant_id)[:1]:
        record = _read_record(s3, bucket, obj["Key"])
        if record is None:
            # A malformed object must not fail the whole trace view.
            return None, None, None
        phase = str(record.get("phase") or obj["Key"].split("/")[1])
        return record, phase, record.get("executionName") or record.get("traceId")
    return None, None, None


#: Finished executions' timelines, by execution ARN. A finished execution's history can no longer
#: change, so a console polling every two seconds reads each one once per warm container instead
#: of on every poll. Running ones are always read fresh. Bounded, oldest out first.
_FINISHED_TIMELINES: dict[str, dict[str, Any]] = {}
_FINISHED_LIMIT = 64
_TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})

#: Pages of history read for one execution, 1,000 events each. A Phase 1 run is a few hundred.
_HISTORY_PAGES = 5

#: States whose children run inside them: everything entered before they exit is a level deeper.
_NESTING_ENTERED = frozenset({"ParallelStateEntered", "MapStateEntered"})
_NESTING_EXITED = frozenset({"ParallelStateExited", "MapStateExited"})

#: Ways one task attempt fails. Caught or retried, the attempt still failed.
_TASK_FAILED = frozenset({"TaskFailed", "TaskTimedOut", "LambdaFunctionFailed",
                          "LambdaFunctionTimedOut"})
_EXECUTION_ENDED_BADLY = frozenset({"ExecutionFailed", "ExecutionTimedOut", "ExecutionAborted"})


def _execution_arn(phase: str | None, execution_name: str) -> str | None:
    """The execution's ARN in its phase's own state machine, or None if that machine is not
    configured here."""
    machine_arn = os.environ.get(_PHASE_MACHINE_ENV.get(str(phase or "phase1"), ""), "")
    if not machine_arn:
        return None
    parts = machine_arn.split(":")
    region = os.environ.get("AWS_REGION") or parts[3]
    account = os.environ.get("AWS_ACCOUNT_ID") or parts[4]
    return f"arn:aws:states:{region}:{account}:execution:{parts[6]}:{execution_name}"


def _execution_timeline(phase: str | None, execution_name: str, *,
                        memo: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """State-by-state timeline of one execution, finished or still running.

    Each phase has its own state machine, so the execution is looked up in its phase's. Read-only
    — see `api_stack.py`'s `ReadExecutionHistoryForTrace` grant. `memo` is one request's: the
    same execution asked for twice (the latest record's, and its phase's) is read once.

    Each step carries its own state's entry and exit. A step inside a Parallel or Map state also
    carries `depth` (1 inside one level), which is how the console tells the four evidence agents'
    branches from the machine's own top-level states. A step still running has no `endedAt`.
    """
    execution_arn = _execution_arn(phase, execution_name)
    if not execution_arn:
        return None
    if memo is not None and execution_arn in memo:
        return memo[execution_arn]
    cached = _FINISHED_TIMELINES.get(execution_arn)
    if cached is not None:
        if memo is not None:
            memo[execution_arn] = cached
        return cached

    timeline = _read_execution(execution_arn, phase)
    if memo is not None:
        memo[execution_arn] = timeline
    if timeline is not None and timeline.get("status") in _TERMINAL_STATUSES:
        while len(_FINISHED_TIMELINES) >= _FINISHED_LIMIT:
            _FINISHED_TIMELINES.pop(next(iter(_FINISHED_TIMELINES)))
        _FINISHED_TIMELINES[execution_arn] = timeline
    return timeline


def _read_execution(execution_arn: str, phase: str | None) -> dict[str, Any] | None:
    import boto3

    # boto3 names the service `stepfunctions`; `states` is only its ARN and endpoint prefix, and
    # `boto3.client("states")` raises UnknownServiceError -- which failed the whole trace view.
    sfn = boto3.client("stepfunctions")
    try:
        description = sfn.describe_execution(executionArn=execution_arn)
    except Exception:  # noqa: BLE001 - this section degrades; the trace view does not
        # Not started yet (Phase 2 a moment after the decision), or aged out of Step Functions'
        # 90-day history.
        return None

    events: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"executionArn": execution_arn, "reverseOrder": False,
                              "maxResults": 1000}
    try:
        for _ in range(_HISTORY_PAGES):
            page = sfn.get_execution_history(**kwargs)
            events.extend(page.get("events") or [])
            if not page.get("nextToken"):
                break
            kwargs["nextToken"] = page["nextToken"]
    except Exception:  # noqa: BLE001 - throttled or denied: no timeline, the trace still stands
        return None

    return {
        "executionArn": execution_arn,
        "phase": phase,
        "status": description.get("status"),
        "startDate": description["startDate"].isoformat(),
        "stopDate": (description["stopDate"].isoformat()
                     if description.get("stopDate") else None),
        "steps": _steps(events),
    }


def _steps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each state entered, in the order they finished, then the ones still running.

    A failed task attempt is attributed to its own state through the history's event ids
    (`TaskStateEntered` <- `TaskScheduled` <- `TaskStarted` <- `TaskFailed`), not to every state
    open at the time: inside a Parallel state, one agent's failure is not its siblings'. A retry
    that then succeeds clears it. Without event ids, every open state is marked, as before.
    """
    steps: list[dict[str, Any]] = []
    open_steps: dict[str, dict[str, Any]] = {}
    by_event_id: dict[Any, dict[str, Any]] = {}
    depth = 0
    for ev in events:
        etype = ev["type"]
        event_id = ev.get("id")
        previous = by_event_id.get(ev.get("previousEventId"))
        if etype.endswith("StateEntered"):
            name = (ev.get("stateEnteredEventDetails") or {}).get("name", "")
            step: dict[str, Any] = {"name": name, "startedAt": ev["timestamp"].isoformat(),
                                    "status": "RUNNING"}
            if depth:
                step["depth"] = depth
            open_steps[name] = step
            if event_id is not None:
                by_event_id[event_id] = step
            if etype in _NESTING_ENTERED:
                depth += 1
        elif etype.endswith("StateExited"):
            if etype in _NESTING_EXITED:
                depth = max(0, depth - 1)
            name = (ev.get("stateExitedEventDetails") or {}).get("name", "")
            step = open_steps.pop(name, {"name": name, "status": "RUNNING"})
            step["endedAt"] = ev["timestamp"].isoformat()
            if step.get("status") == "RUNNING":
                step["status"] = "SUCCEEDED"
            steps.append(step)
        elif etype in ("TaskScheduled", "TaskStarted", "TaskSubmitted",
                       "LambdaFunctionScheduled", "LambdaFunctionStarted"):
            # Links the attempt to its state; a new attempt (a retry) is running again.
            if previous is not None:
                if event_id is not None:
                    by_event_id[event_id] = previous
                if etype in ("TaskScheduled", "LambdaFunctionScheduled"):
                    previous["status"] = "RUNNING"
                if etype == "TaskScheduled":
                    previous.update(_task_called(ev.get("taskScheduledEventDetails") or {}))
        elif etype in _TASK_FAILED:
            if previous is not None:
                previous["status"] = "FAILED"
                # A retry's `TaskScheduled` points back at this failure.
                if event_id is not None:
                    by_event_id[event_id] = previous
            else:
                for step in open_steps.values():
                    step["status"] = "FAILED"
        elif etype in _EXECUTION_ENDED_BADLY:
            for step in open_steps.values():
                step["status"] = "FAILED"
    # Still running: entered, not yet exited. Listed after the finished ones.
    steps.extend(open_steps.values())
    return steps


def _task_called(details: Mapping[str, Any]) -> dict[str, str]:
    """What a task state called, as Step Functions recorded it: the integration
    (`lambda:invoke`, `aws-sdk:dynamodb:updateItem`, ...) and, for a Lambda function or an agent
    harness, its name. That is what tells the console which lane a step belongs to.

    `parameters` is the task's whole input, case data included. Only the function name or the
    harness ARN is read out of it; nothing else from it leaves this function.
    """
    called: dict[str, str] = {}
    resource_type, resource = details.get("resourceType"), details.get("resource")
    if resource_type and resource:
        called["resource"] = f"{resource_type}:{resource}"
    try:
        parameters = json.loads(details.get("parameters") or "{}")
    except (json.JSONDecodeError, TypeError):
        return called
    if not isinstance(parameters, Mapping):
        return called
    function = parameters.get("FunctionName")
    harness = parameters.get("HarnessArn")
    if isinstance(function, str) and function:
        # A name, a partial ARN or a full one, perhaps with a qualifier: the bare name.
        called["target"] = function.split("function:")[-1].split(":")[0]
    elif isinstance(harness, str) and harness:
        called["target"] = harness.rsplit("/", 1)[-1]
    return called


def _phase_executions(item: Mapping[str, Any], record_phase: str | None,
                      record_execution: str | None, *,
                      memo: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Each phase's latest execution, running or finished, for the console's live view.

    Phase 1 and Phase 3 write their own execution name onto the case when they start
    (`phase1Execution`, `phase3Execution`); Phase 2 is started under the provisional decision id.
    A case from before the names were recorded falls back, for its newest record's phase only, to
    the execution that wrote that record.
    """
    names = {
        "phase1": _s(item.get("phase1Execution")),
        "phase2": _s(item.get("provisionalDecisionId")),
        "phase3": _s(item.get("phase3Execution")),
    }
    if record_phase in names and not names[record_phase] and record_execution:
        names[record_phase] = record_execution
    return {phase: (_execution_timeline(phase, name, memo=memo) if name else None)
            for phase, name in names.items()}


def get_audit(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """`GET /audit/{applicantId}`. R18.1 — every decision record for the applicant, newest first,
    exactly as the audit writer sealed it. Gated to reviewers: the audit record is denser PII than
    the applicant-facing `/cases/{id}` projection (R18.8)."""
    if not _is_reviewer(event):
        return _response(403, {"error": "REVIEWERS_ONLY"})

    path = event.get("pathParameters") or {}
    applicant_id = str(path.get("applicantId") or "")
    if not applicant_id:
        return _response(400, {"error": "MISSING_APPLICANT_ID"})

    bucket = os.environ.get("AUDIT_BUCKET_NAME")
    if not bucket:
        return _response(500, {"error": "AUDIT_BUCKET_NOT_CONFIGURED"})

    import boto3

    s3 = boto3.client("s3")
    objects = _decision_objects(s3, bucket, applicant_id)
    records = []
    unreadable = []
    for obj in objects[:_AUDIT_LIMIT]:
        record = _read_record(s3, bucket, obj["Key"])
        if record is None:
            unreadable.append(obj["Key"])
        else:
            records.append(record)
    return _response(200, {"applicantId": applicant_id, "decisions": records,
                           "total": len(objects), "truncated": len(objects) > _AUDIT_LIMIT,
                           "unreadable": unreadable})


def _project_queued_case(item: Mapping[str, Any]) -> dict[str, Any]:
    """The queue row the console renders and submits against.

    `phase` is projected because the console has no other way to know it; without it every review,
    including Phase 2 and Phase 3 ones, was recorded as `phase1Review`. `reason` is why the case is
    here, in the terms the state machine used when it queued it.
    """
    row = {
        "applicantId": _s(item.get("applicantId")),
        "caseId": _s(item.get("caseId")),
        "jurisdiction": _s(item.get("jurisdiction")),
        "createdAt": _s(item.get("createdAt")),
        "phase": _s(item.get("reviewPhase")) or "phase1",
        "reason": _s(item.get("reviewReason")),
        "queuedAt": _s(item.get("reviewQueuedAt")),
        "hasBriefing": "reviewBriefing" in item,
    }
    token = _s(item.get("reviewTaskToken"))
    if token:
        row["taskToken"] = token
    return row


def _s(attr: Any) -> str | None:
    if isinstance(attr, Mapping):
        return attr.get("S")
    return None


def _n(attr: Any) -> int | None:
    if isinstance(attr, Mapping) and "N" in attr:
        try:
            return int(float(attr["N"]))
        except (TypeError, ValueError):
            return None
    return None


def _plain(attr: Any) -> Any:
    """A DynamoDB AttributeValue -> plain Python, for the few map/list attributes shown here."""
    if not isinstance(attr, Mapping) or len(attr) != 1:
        return attr
    (member, inner), = attr.items()
    if member == "S":
        return inner
    if member == "N":
        return float(inner) if "." in str(inner) else int(inner)
    if member in ("BOOL", "Bool"):
        return bool(inner)
    if member in ("NULL", "Null"):
        return None
    if member == "M":
        return {k: _plain(v) for k, v in inner.items()}
    if member == "L":
        return [_plain(v) for v in inner]
    return inner


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
        "body": json.dumps(body, default=str),
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
