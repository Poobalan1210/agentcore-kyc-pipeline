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
    to_attribute_value,
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


def _serialized_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """`to_request()` output with Key and ExpressionAttributeValues in DynamoDB wire form."""
    out = dict(request)
    if isinstance(out.get("Key"), Mapping):
        out["Key"] = {k: to_attribute_value(v) for k, v in out["Key"].items()}
    if isinstance(out.get("ExpressionAttributeValues"), Mapping):
        out["ExpressionAttributeValues"] = {
            k: to_attribute_value(v) for k, v in out["ExpressionAttributeValues"].items()
        }
    return out


def _returned_item(exc: Exception) -> Mapping[str, Any] | None:
    """`ReturnValuesOnConditionCheckFailure=ALL_OLD` puts the winning item on the exception.

    Deserialized like `load_case`'s: this item is handed to the same pure `decisions.py` functions,
    which read plain values. Returning the wrapped shape here would make the *conflict* path report
    a bogus CASE_MISMATCH instead of naming the reviewer who won.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        item = response.get("Item")
        if isinstance(item, Mapping) and item:
            return _parse_phase_blobs(deserialize_item(item))
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
    the common contested case into an exception path instead of a clean 409.

    Keys are wrapped as AttributeValues because `dynamodb` here is the low-level **client**, not a
    `Table` resource. Passing bare strings raised
    `ParamValidationError: Invalid type for parameter Key.applicantId ... valid types: <class 'dict'>`
    and every decision submission returned 500. It went unnoticed because it was unreachable: the
    console could never obtain a task token, so `Submit decision` was permanently disabled and this
    line had never once executed against the deployed stack.

    The returned item is **deserialized** for the same reason, in the other direction. Everything in
    `decisions.py` is a pure function over a plain case record — `str(case.get("caseId"))` compared
    against a plain string, `preliminary_tier(case, phase)`, `existing_review(case, phase)`. Handing
    those the client's wrapped shape made `caseId` compare as `"{'S': 'CASE-...'}"`, so a correct
    submission was rejected `CASE_MISMATCH: caseId does not match the case record` even though the
    ids were byte-identical. Unwrapping once here keeps the wire format at the boundary instead of
    leaking it into the decision rules.
    """
    response = dynamodb.get_item(
        TableName=table_name,
        Key={
            keys.partition_key: {"S": submission.applicant_id},
            keys.sort_key: {"S": keys.case_sort_value},
        },
        ConsistentRead=True,
    )
    item = response.get("Item")
    if not (isinstance(item, Mapping) and item):
        return None
    return _parse_phase_blobs(deserialize_item(item))


#: Case attributes `asl.py` stores as a JSON *string* rather than a Map, which every reader in
#: `decisions.py` nonetheless treats as a Mapping.
_JSON_BLOB_ATTRIBUTES = ("phase1", "phase2", "phase3")


def _parse_phase_blobs(case: dict[str, Any]) -> dict[str, Any]:
    """Turn the phase decision blobs from JSON strings into dicts.

    `OpenProvisional` writes `phase1` as a JSON string, but `preliminary_tier` and
    `existing_review` both do `case.get(phase)` and require a Mapping. Against a real case record
    they therefore returned `None` for everything — which silently disabled the R4.3 / Property 2
    guard that forbids approving a PROHIBITED tier, because the tier it checks was never found.
    An approval that should have been refused would have been accepted.

    Parsed here, at the one boundary that reads the case, so the rules stay pure functions over a
    plain record and no caller has to know which attributes happen to be stringified.
    """
    for attribute in _JSON_BLOB_ATTRIBUTES:
        raw = case.get(attribute)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # Leave it as the string. A blob we cannot parse must not become a silent {} that reads
            # as "this phase ran and produced nothing".
            continue
        if isinstance(parsed, Mapping):
            case[attribute] = parsed
    return case


def deserialize_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """DynamoDB AttributeValues -> plain Python, recursively.

    Hand-rolled rather than `boto3.dynamodb.types.TypeDeserializer` so that an unrecognised or
    already-plain value passes through unchanged instead of raising. This runs on the reviewer's
    submit path, where refusing a whole decision because one unexpected attribute appeared on the
    case record would be a worse failure than ignoring that attribute.
    """
    def convert(value: Any) -> Any:
        if not isinstance(value, Mapping) or len(value) != 1:
            return value
        (member, inner), = value.items()
        if member == "S":
            return inner
        if member == "N":
            # int where it is exact, so a tier threshold or count compares as a number.
            text = str(inner)
            try:
                return int(text) if "." not in text else float(text)
            except ValueError:
                return text
        if member in ("BOOL", "Bool"):
            return bool(inner)
        if member == "NULL":
            return None
        if member == "M":
            return {k: convert(v) for k, v in inner.items()} if isinstance(inner, Mapping) else inner
        if member == "L":
            return [convert(v) for v in inner] if isinstance(inner, list) else inner
        if member in ("SS", "NS", "BS"):
            return list(inner) if isinstance(inner, list) else inner
        return value

    return {key: convert(value) for key, value in item.items()}


def apply_decision(
    outcome: ReviewOutcome,
    *,
    table_name: str,
    keys: TableKeySchema,
    dynamodb: Any,
    case: Mapping[str, Any] | None = None,
) -> ReviewOutcome:
    """Attempt the conditional write. A failed condition is a normal result, not an error."""
    submission = outcome.submission
    decision = outcome.decision
    assert submission is not None and decision is not None

    write = build_conditional_write(
        submission=submission, decision=decision, table_name=table_name, keys=keys, case=case)
    try:
        # Serialize at the boundary: `decisions.py` produces plain values, the low-level client
        # requires AttributeValues. Without this the client rejects the write outright
        # ("Invalid type for parameter ExpressionAttributeValues.:target ... valid types:
        # <class 'dict'>") and every reviewer decision returned 500 — unnoticed because no reviewer
        # could obtain a task token to submit one.
        dynamodb.update_item(**_serialized_request(write.to_request()))
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
            outcome, table_name=table, keys=key_schema, dynamodb=resolved.dynamodb, case=case)

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
