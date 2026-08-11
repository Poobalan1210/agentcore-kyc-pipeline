"""Review decision receiver — task 6.5, R6.1–R6.6, R21, design §7.5.

**[PORT]** the two-way HITL receiver shape from P-10's `slack_approval_handler.py`: receive a human
action, resolve it against stored state, then close the loop on the waiting workflow. The queue is a
case queue rather than Slack, and the loop closes with `SendTaskSuccess` instead of a chat callback.

Named `review_handler.py`, not `handler.py`. `tools/handler.py` already occupies the module name
`handler` on `sys.path` (both directories are on `pythonpath` in pytest.ini), so a second one would
resolve by path order and silently import the wrong module.

The impure half. Everything decidable without AWS is in `decisions.py`; what remains here is the
ordering of three effects, and the order is the whole design:

1. **Read** the case record.
2. **Write** the decision with a conditional expression — this, not step 1, decides the race (R6.5).
3. **Resume** the execution with the task token, and only if step 2 wrote or had already written.

Why never resume before writing: `SendTaskSuccess` is irreversible. If the token were sent first and
the write then lost the race, the case would carry one reviewer's decision while the workflow
resumed on another's — the exact silent overwrite R6.5 forbids, made undetectable.

Why an invalid submission never touches the token: the case must stay in `AWAITING_REVIEW` so a
reviewer can act again. Sending `SendTaskFailure` on a typo would destroy the review opportunity and
force a manual re-drive.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from decisions import (
    Resolution,
    ReviewOutcome,
    ReviewSubmission,
    TableKeySchema,
    build_conditional_write,
    conflict_after_write_failure,
    parse_action,
    resolve,
    resume_payload,
)

LOG = logging.getLogger(__name__)
if not LOG.handlers:
    LOG.setLevel(logging.INFO)

#: Error names that mean "this token is already spent". Both are *expected* on a redelivery: Lambda
#: guarantees at-least-once, so the second invocation of a successful review must be a no-op rather
#: than a 500 (R21.3, R21.5).
_TOKEN_SPENT = ("TaskAlreadyCompleted", "TaskTimedOut", "InvalidToken")

#: The failure name the state machine catches when a review can no longer be honoured. Distinct from
#: an agent failure so the `Catch` in design §7.4 can route it separately.
UNRESUMABLE_ERROR = "ReviewUnresumable"


@dataclass(kw_only=True)
class Clients:
    """Injected together so a test supplies three fakes and no boto3 session is ever created."""

    dynamodb: Any = None
    stepfunctions: Any = None
    cloudwatch: Any = None

    def resolved(self) -> Clients:
        if self.dynamodb is not None and self.stepfunctions is not None:
            return self
        import boto3  # imported lazily so the module is importable in a test env without creds

        return Clients(
            dynamodb=self.dynamodb or boto3.client("dynamodb"),
            stepfunctions=self.stepfunctions or boto3.client("stepfunctions"),
            cloudwatch=self.cloudwatch,
        )


def _error_code(exc: Exception) -> str:
    """Botocore error code without importing botocore, so fakes can raise a plain Exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return str(error.get("Code", ""))
    return type(exc).__name__


def _returned_item(exc: Exception) -> Mapping[str, Any] | None:
    """`ReturnValuesOnConditionCheckFailure=ALL_OLD` puts the winning item on the exception."""
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        item = response.get("Item")
        if isinstance(item, Mapping):
            return item
    return None


def parse_event(event: Mapping[str, Any], *, now: datetime | None = None) -> ReviewSubmission:
    """Accept either a direct invocation payload or an API Gateway proxy event.

    Reviewer identity is taken from the Cognito claims **in preference to** anything in the body
    (design §18.4, R6.3). A body-supplied `reviewerId` is caller-controlled, and a review decision
    attributed to the wrong human is an audit failure that the audit record cannot detect later.
    """
    body: Mapping[str, Any] = event
    raw_body = event.get("body")
    if isinstance(raw_body, str) and raw_body:
        body = json.loads(raw_body)
    elif isinstance(raw_body, Mapping):
        body = raw_body

    claims = _claims(event)
    reviewer_id = str(
        claims.get("sub") or claims.get("cognito:username") or body.get("reviewerId") or "")

    submitted_raw = body.get("decidedAt") or body.get("submittedAt")
    if submitted_raw:
        submitted_at = _parse_instant(str(submitted_raw))
    else:
        # Server clock, not the browser's. A reviewer's device clock is not evidence.
        submitted_at = now or datetime.now(timezone.utc)

    path = event.get("pathParameters") or {}
    case_id = str(body.get("caseId") or (path or {}).get("caseId") or "")

    return ReviewSubmission(
        case_id=case_id,
        applicant_id=str(body.get("applicantId") or ""),
        phase=str(body.get("phase") or "phase1"),
        reviewer_id=reviewer_id,
        # An unrecognised action becomes REJECT-shaped nonsense if defaulted, so it stays None and
        # validation reports UNKNOWN_ACTION via the sentinel below.
        action=parse_action(body.get("action")) or _UNKNOWN_ACTION,
        rationale=str(body.get("rationale") or ""),
        submitted_at=submitted_at,
        task_token=str(body.get("taskToken") or ""),
        submission_id=str(
            body.get("submissionId")
            or (event.get("requestContext") or {}).get("requestId")
            or body.get("messageId")
            or ""),
    )


def _claims(event: Mapping[str, Any]) -> Mapping[str, Any]:
    context = event.get("requestContext")
    if not isinstance(context, Mapping):
        return {}
    authorizer = context.get("authorizer")
    if not isinstance(authorizer, Mapping):
        return {}
    claims = authorizer.get("claims")
    if isinstance(claims, Mapping):
        return claims
    jwt = authorizer.get("jwt")
    if isinstance(jwt, Mapping) and isinstance(jwt.get("claims"), Mapping):
        return jwt["claims"]
    return {}


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # Left naive on purpose — `validate_submission` reports NAIVE_TIMESTAMP rather than this
        # function guessing a zone. R6.3 wants UTC stated, not inferred.
        return parsed
    return parsed.astimezone(timezone.utc)


class _UnknownAction:
    """Sentinel so a bad action reaches validation instead of raising during parse."""

    value = "UNKNOWN"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unknown action>"


_UNKNOWN_ACTION: Any = _UnknownAction()


def load_case(
    submission: ReviewSubmission,
    *,
    table_name: str,
    keys: TableKeySchema,
    dynamodb: Any,
) -> Mapping[str, Any] | None:
    """Strongly consistent read. An eventually consistent one would let the losing reviewer see no
    existing decision and proceed to a write that the condition then rejects — correct, but it turns
    the common contested case into an exception path instead of a clean 409."""
    response = dynamodb.get_item(
        TableName=table_name,
        Key={keys.partition_key: submission.applicant_id, keys.sort_key: keys.case_sort_value},
        ConsistentRead=True,
    )
    item = response.get("Item")
    return item if isinstance(item, Mapping) and item else None


def apply_decision(
    outcome: ReviewOutcome,
    *,
    table_name: str,
    keys: TableKeySchema,
    dynamodb: Any,
) -> ReviewOutcome:
    """Attempt the conditional write. A failed condition is a normal result, not an error."""
    submission = outcome.submission
    decision = outcome.decision
    assert submission is not None and decision is not None

    write = build_conditional_write(
        submission=submission, decision=decision, table_name=table_name, keys=keys)
    try:
        dynamodb.update_item(**write.to_request())
    except Exception as exc:  # noqa: BLE001 - narrowed immediately by error code
        if _error_code(exc) != "ConditionalCheckFailedException":
            raise
        # The item DynamoDB handed back with the failure is the winner's. Falling back to a re-read
        # covers older API behaviour where ALL_OLD is absent.
        current = _returned_item(exc) or load_case(
            submission, table_name=table_name, keys=keys, dynamodb=dynamodb)
        return conflict_after_write_failure(submission=submission, case=current)
    return outcome


def close_the_loop(outcome: ReviewOutcome, *, stepfunctions: Any) -> str:
    """`SendTaskSuccess` / `SendTaskFailure` — the F-12 half of the design.

    `invokeHarness` is Request-Response only, so the human wait is its own SQS
    `.waitForTaskToken` state and this function is the only thing that ends it (design §7.5).

    Returns what was sent, for the response body and the log line.
    """
    submission = outcome.submission
    assert submission is not None

    if outcome.resolution is Resolution.UNRESUMABLE:
        # Release the orphaned waiter now. Leaving it to the heartbeat timeout burns the SLA and
        # produces a timeout in the audit record that looks like an infrastructure fault.
        _tolerating_spent_token(
            stepfunctions.send_task_failure,
            taskToken=submission.task_token,
            error=UNRESUMABLE_ERROR,
            cause=outcome.detail[:32768],
        )
        return "SendTaskFailure"

    if not outcome.resume_case:
        # Invalid or conflicted: the token belongs to a review that is still open, or to the winner
        # who already resumed it. Either way this invocation must not touch it.
        return "none"

    sent = _tolerating_spent_token(
        stepfunctions.send_task_success,
        taskToken=submission.task_token,
        output=json.dumps(resume_payload(outcome)),
    )
    return "SendTaskSuccess" if sent else "SendTaskSuccess(alreadyCompleted)"


def _tolerating_spent_token(call: Any, **kwargs: Any) -> bool:
    """True if the call landed. A spent token on a replay is success, not failure (R21.3)."""
    try:
        call(**kwargs)
    except Exception as exc:  # noqa: BLE001 - narrowed by error code
        if _error_code(exc) in _TOKEN_SPENT:
            LOG.info("task token already spent (%s); treating replay as success", _error_code(exc))
            return False
        raise
    return True


def handle_review(
    event: Mapping[str, Any],
    *,
    table_name: str | None = None,
    keys: TableKeySchema | None = None,
    clients: Clients | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The whole receiver, injectable. `lambda_handler` is a thin shell over this."""
    table = table_name or os.environ.get("CASE_TABLE_NAME", "")
    if not table:
        raise RuntimeError("CASE_TABLE_NAME is not set; the receiver cannot find the case record")
    key_schema = keys or _keys_from_env()
    resolved = (clients or Clients()).resolved()

    submission = parse_event(event, now=now)
    case = None
    if submission.applicant_id:
        case = load_case(
            submission, table_name=table, keys=key_schema, dynamodb=resolved.dynamodb)

    outcome = resolve(submission=submission, case=case)
    if outcome.resolution is Resolution.ACCEPT:
        outcome = apply_decision(
            outcome, table_name=table, keys=key_schema, dynamodb=resolved.dynamodb)

    sent = close_the_loop(outcome, stepfunctions=resolved.stepfunctions)

    # No rationale in the log line: it is free text a reviewer typed about a real person, and R18.8
    # keeps that in the audit record only.
    LOG.info(
        "review resolved caseId=%s phase=%s reviewer=%s resolution=%s token=%s",
        submission.case_id, submission.phase, submission.reviewer_id,
        outcome.resolution.value, sent,
    )
    return _response(outcome, sent=sent)


def _keys_from_env() -> TableKeySchema:
    defaults = TableKeySchema()
    return TableKeySchema(
        partition_key=os.environ.get("CASE_TABLE_PK", defaults.partition_key),
        sort_key=os.environ.get("CASE_TABLE_SK", defaults.sort_key),
        case_sort_value=os.environ.get("CASE_TABLE_SK_VALUE", defaults.case_sort_value),
    )


def _response(outcome: ReviewOutcome, *, sent: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "resolution": outcome.resolution.value,
        "detail": outcome.detail,
        "taskAction": sent,
    }
    if outcome.decision is not None:
        body["decision"] = outcome.decision.to_item()
    if outcome.existing is not None:
        # R6.5 / R30.4 — the losing reviewer is shown the decision that won, not just a 409.
        body["existingDecision"] = outcome.existing.to_item()
    if outcome.invalid is not None:
        body["reasons"] = [reason.value for reason in outcome.invalid.reasons]
    return {
        "statusCode": outcome.status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """Lambda entry point.

    Unlike `tools/handler.py`, this one does **not** swallow every exception. A tool that raises is
    ambiguous to an agent, so there it becomes UNAVAILABLE; here a raised exception means the write
    or the token send genuinely failed, and the correct behaviour is to let Lambda retry with the
    case still in `AWAITING_REVIEW`. Swallowing it would report success for a review that was never
    recorded.
    """
    return handle_review(event)
