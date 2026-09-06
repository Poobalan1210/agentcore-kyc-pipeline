"""Materialise a review task token onto the case record, once.

Why this exists. The token a reviewer must submit with lives in the `kyc90-review` SQS message
body, and the console used to discover it by calling `ReceiveMessage` on every `GET /review/queue`.
That cannot work, for three independent reasons measured live:

* **Every read spends redrive budget.** `ReceiveMessage` increments ApproximateReceiveCount and the
  queue carries `maxReceiveCount: 5`, so the sixth *view* of a case moves a LIVE task token to the
  dead-letter queue. Observed twice: five waiting cases vanished from the reviewer's queue while
  their executions stayed alive.
* **Reads are mutually exclusive.** A received message is invisible for its visibility timeout, so
  two reviewers — or a reviewer and any diagnostic — blind each other.
* **Short polling lies.** `WaitTimeSeconds=0` samples a subset of servers, so a peek returned zero
  tokens against a queue holding five visible messages, and every case rendered "Task token not yet
  visible" with no way to act.

A queue is a work-handoff primitive, not a lookup table. So this consumer receives each message
exactly once, writes the token where a lookup belongs — the case record — and lets SQS delete it.
`GET /review/queue` then reads the token with a plain DynamoDB read: no visibility, no redrive
budget, no exclusivity, repeatable by any number of reviewers.

Deleting the message is safe because the workflow waits on the *token*, not on the message:
`ManualReview` is `sqs:sendMessage.waitForTaskToken`, and the token stays valid for its 7-day
timeout regardless of where it is stored.

Ordering is load-bearing: the write happens first and SQS deletes only on a successful return. If
the write fails this raises, the message becomes visible again and is retried — losing the token
entirely is the one outcome that must be impossible, because the applicant is waiting on a workflow
nothing else can resume.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

import boto3

_ddb = boto3.client("dynamodb")


def _record_token(table: str, applicant_id: str, token: str) -> None:
    """Write the token onto the case record.

    Not conditional on absence. A redelivered message carries the same still-valid token, and a
    re-driven review legitimately issues a new one; in both cases the newest token is the one a
    reviewer must use, so last-write-wins is correct here. What must never happen is a *stale*
    token surviving a re-drive, which a `attribute_not_exists` guard would cause.
    """
    _ddb.update_item(
        TableName=table,
        Key={"applicantId": {"S": applicant_id}, "sk": {"S": "CASE"}},
        UpdateExpression="SET reviewTaskToken = :t, reviewTaskTokenAt = :at",
        ExpressionAttributeValues={
            ":t": {"S": token},
            ":at": {"S": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
        },
    )


def lambda_handler(event: Mapping[str, Any], _context: Any = None) -> dict[str, Any]:
    """SQS handler. Returns partial batch failures so one bad message cannot drop the others."""
    table = os.environ["CASE_TABLE_NAME"]
    failures: list[dict[str, str]] = []

    for record in event.get("Records", ()):
        message_id = record.get("messageId", "")
        try:
            body = json.loads(record["body"])
        except (json.JSONDecodeError, KeyError):
            # Unparseable: nothing to materialise and retrying cannot help. Do NOT report a
            # failure, or it redelivers until it reaches the DLQ on every poll.
            continue

        applicant_id = body.get("applicantId")
        token = body.get("taskToken")
        if not applicant_id or not token:
            # A message without a token is not a review handoff. Same reasoning as above.
            continue

        try:
            _record_token(table, str(applicant_id), str(token))
        except Exception:  # noqa: BLE001 - reported to SQS, never swallowed
            # Report and let SQS redeliver. Swallowing here would delete the message and strand
            # the execution on a token nobody can reach.
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
