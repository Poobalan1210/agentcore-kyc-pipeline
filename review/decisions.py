"""Human review loop — pure decision layer. Task 6.5, R6.1–R6.7, R21, design §7.5.

No boto3, no clock, no network. Everything that decides *whether* a reviewer decision is
admissible, and *what* DynamoDB should be asked to do about it, lives here so that the
first-writer-wins behaviour (R6.5) can be tested exhaustively without AWS.

Two things in this module are load-bearing and easy to get subtly wrong:

1. **The conditional write is the only arbiter of the race.** `resolve()` looks at a case record
   that was read a moment ago, so its verdict is advisory — by the time the write lands another
   reviewer may have won. That is why `build_conditional_write()` carries the guard in the
   expression itself and why `conflict_after_write_failure()` exists: a `ConditionalCheckFailed`
   is the *expected* outcome for the loser, not an error to retry.

2. **An inadmissible submission must never touch the task token.** Validation failures leave the
   case in `AWAITING_REVIEW` so another reviewer can act. The caller enforces this; the reasons
   are enumerated here so it can distinguish them from a genuine conflict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

#: The only case state from which a review decision may be taken (design §9.x state diagram).
AWAITING_REVIEW = "AWAITING_REVIEW"

#: Phases a review decision can be scoped to. Property 10 — "a case is decided at most once per
#: phase" — is meaningless without the phase, so it is required rather than defaulted. Phase 3 is
#: a lifecycle re-assessment: one review per risk event, the previous one cleared by the state
#: machine when it queues the next.
PHASES = ("phase1", "phase2", "phase3")


class ReviewAction(Enum):
    """R6.3 / design §18.4 — the three actions the reviewer console offers."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    REQUEST_STEP_UP = "REQUEST_STEP_UP"


#: Where the case lands per action (design §9.x). `REQUEST_STEP_UP` is the awkward one: the state
#: diagram draws only `AWAITING_REVIEW → PROVISIONAL` and `AWAITING_REVIEW → DECIDED`, yet R6.3
#: and design §18.4 both require the console to offer request-step-up. `AWAITING_APPLICANT` is the
#: only state that means "we are waiting on the applicant", and the existing
#: `AWAITING_APPLICANT → IN_PROGRESS` resume edge makes the case recoverable, so that is the
#: target used here. Flagged as a spec gap rather than silently invented.
TARGET_STATE: Mapping[ReviewAction, str] = MappingProxyType({
    ReviewAction.APPROVE: "PROVISIONAL",
    ReviewAction.REJECT: "DECIDED",
    ReviewAction.REQUEST_STEP_UP: "AWAITING_APPLICANT",
})

#: Where the case lands, per phase. The state machine carries the decision out from here — opens
#: the account, lifts to the enhanced posture, freezes — and it checks this state before doing
#: so (`asl.py`), so the two must agree. Phase 1's table is `TARGET_STATE`.
#:
#: * Phase 2 APPROVE lifts to the enhanced posture and enrols monitoring; REJECT freezes; a
#:   step-up tightens the restrictions while the applicant is asked for something that works.
#: * Phase 3 has no step-up: a lifecycle review clears the alert or exits the relationship. Its
#:   APPROVE target is the state the customer was in before the event (`day_zero_state`).
TARGET_STATES: Mapping[str, Mapping[ReviewAction, str]] = MappingProxyType({
    "phase1": TARGET_STATE,
    "phase2": MappingProxyType({
        ReviewAction.APPROVE: "MONITORING",
        ReviewAction.REJECT: "FROZEN",
        ReviewAction.REQUEST_STEP_UP: "RESTRICTED",
    }),
    "phase3": MappingProxyType({
        ReviewAction.REJECT: "FROZEN",
    }),
})

#: Phase 2 outcome -> the case state a Phase 3 clearance restores. Mirrors `_DAY0_POSTURE` in
#: `infra/kyc/asl.py` (posture STANDARD/ENHANCED -> MONITORING, TIGHTENED -> RESTRICTED, anything
#: else PROVISIONAL); `infra/tests/test_jsonata_expressions.py` asserts they agree.
_DAY_ZERO_STATE_BY_PHASE2_OUTCOME: Mapping[str, str] = MappingProxyType({
    "FULL_APPROVE_STANDARD": "MONITORING",
    "FULL_APPROVE_ENHANCED_MONITORING": "MONITORING",
    "RESTRICT_FURTHER": "RESTRICTED",
})

#: Screening classifications a reviewer's APPROVE resolves. Never CONFIRMED: a confirmed match is
#: a prohibition, not an alert to be dispositioned (R4.3).
CLEARABLE_CLASSIFICATIONS = ("PROBABLE", "POSSIBLE")

#: Risk tier that no reviewer may approve past. R4.3 ("IF the preliminary tier is PROHIBITED THEN
#: the system SHALL reject at Phase 1 and SHALL NOT open a provisional account") and Property 2
#: ("regardless of every other agent result") are unconditional — they name no human exception.
#: Since `AWAITING_REVIEW → PROVISIONAL` is exactly the edge that opens a provisional account, a
#: reviewer APPROVE on a PROHIBITED-tier case would violate R4.3, so it is refused. REJECT and
#: REQUEST_STEP_UP stay available: the reviewer is not locked out of the case, only out of the one
#: action the rules forbid.
PROHIBITED_TIER = "PROHIBITED"


class Resolution(Enum):
    """What should happen to this submission."""

    ACCEPT = "ACCEPT"
    #: Same reviewer, same submission, seen again — Lambda and SQS both redeliver (R21.3).
    REPLAY = "REPLAY"
    #: A different decision already exists for this case and phase (R6.5, R30.4).
    CONFLICT = "CONFLICT"
    #: The submission itself is malformed or forbidden. Case stays reviewable.
    INVALID = "INVALID"
    #: The case has moved on without a review decision, so the waiting execution is orphaned.
    UNRESUMABLE = "UNRESUMABLE"


class InvalidReason(Enum):
    MISSING_CASE = "MISSING_CASE"
    CASE_MISMATCH = "CASE_MISMATCH"
    UNKNOWN_ACTION = "UNKNOWN_ACTION"
    UNKNOWN_PHASE = "UNKNOWN_PHASE"
    MISSING_REVIEWER = "MISSING_REVIEWER"
    MISSING_RATIONALE = "MISSING_RATIONALE"
    MISSING_TASK_TOKEN = "MISSING_TASK_TOKEN"
    NAIVE_TIMESTAMP = "NAIVE_TIMESTAMP"
    PROHIBITED_TIER_NOT_OVERRIDABLE = "PROHIBITED_TIER_NOT_OVERRIDABLE"
    ACTION_NOT_AVAILABLE = "ACTION_NOT_AVAILABLE"


#: HTTP status per resolution, for the reviewer console. 409 for the losing reviewer is the
#: "conflict response" R6.5 asks for by name; 410 tells the console the case is gone rather than
#: contested, which is a different thing for the operator to do next.
HTTP_STATUS: Mapping[Resolution, int] = MappingProxyType({
    Resolution.ACCEPT: 200,
    Resolution.REPLAY: 200,
    Resolution.CONFLICT: 409,
    Resolution.INVALID: 422,
    Resolution.UNRESUMABLE: 410,
})


@dataclass(frozen=True, kw_only=True)
class ReviewSubmission:
    """One reviewer action as it arrives, before any judgement is applied.

    `submission_id` is the caller's event identity (API request id, SQS message id). R21.2 derives
    an idempotency key from `applicantId + eventId`; the review equivalent adds phase and reviewer
    so that a genuine second reviewer never collides with a redelivery of the first.
    """

    case_id: str
    applicant_id: str
    phase: str
    reviewer_id: str
    action: ReviewAction
    rationale: str
    submitted_at: datetime
    task_token: str
    submission_id: str

    @property
    def idempotency_key(self) -> str:
        return "#".join((
            self.applicant_id, self.case_id, self.phase, self.reviewer_id, self.submission_id))


@dataclass(frozen=True, kw_only=True)
class RecordedDecision:
    """The review decision as stored on the case record, and as read back on a replay."""

    case_id: str
    phase: str
    reviewer_id: str
    action: str
    rationale: str
    decided_at: str
    idempotency_key: str
    target_state: str
    #: The PROBABLE / POSSIBLE screening alerts this APPROVE resolved, by listing id. Recorded with
    #: the decision so the console and the audit record can say exactly what a reviewer cleared.
    cleared_alerts: tuple[str, ...] = ()

    def to_item(self) -> dict[str, Any]:
        """DynamoDB attribute map. Keys are camelCase to match the §9.1 case record."""
        return {
            "caseId": self.case_id,
            "phase": self.phase,
            "reviewerId": self.reviewer_id,
            "action": self.action,
            "rationale": self.rationale,
            "decidedAt": self.decided_at,
            "idempotencyKey": self.idempotency_key,
            "targetState": self.target_state,
            "clearedAlerts": list(self.cleared_alerts),
        }


def recorded_from_item(item: Mapping[str, Any]) -> RecordedDecision:
    return RecordedDecision(
        case_id=str(item.get("caseId", "")),
        phase=str(item.get("phase", "")),
        reviewer_id=str(item.get("reviewerId", "")),
        action=str(item.get("action", "")),
        rationale=str(item.get("rationale", "")),
        decided_at=str(item.get("decidedAt", "")),
        idempotency_key=str(item.get("idempotencyKey", "")),
        target_state=str(item.get("targetState", "")),
        cleared_alerts=tuple(str(a) for a in item.get("clearedAlerts") or ()),
    )


@dataclass(frozen=True, kw_only=True)
class Invalid:
    reasons: tuple[InvalidReason, ...]
    detail: str


@dataclass(frozen=True, kw_only=True)
class ReviewOutcome:
    """What the caller should do, and what to tell the reviewer."""

    resolution: Resolution
    submission: ReviewSubmission | None = None
    decision: RecordedDecision | None = None
    existing: RecordedDecision | None = None
    invalid: Invalid | None = None
    detail: str = ""

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.resolution]

    @property
    def resume_case(self) -> bool:
        """Only an accepted (or replayed) decision may resume the waiting execution (R6.4)."""
        return self.resolution in (Resolution.ACCEPT, Resolution.REPLAY)


@dataclass(frozen=True, kw_only=True)
class TableKeySchema:
    """Key names for the case table. Injected because `infra/` owns the real names (design §9.1)."""

    partition_key: str = "applicantId"
    sort_key: str = "sk"
    case_sort_value: str = "CASE"



def to_attribute_value(value: Any) -> Any:
    """Plain Python -> a DynamoDB AttributeValue, recursively.

    Pass-through for a value that is already an AttributeValue, so serializing twice is harmless —
    the alternative is a double-wrapped `{"M": {"S": ...}}` that DynamoDB accepts and stores as
    garbage, which is far worse than a redundant call.
    """
    if isinstance(value, Mapping):
        if len(value) == 1:
            (member, _), = value.items()
            if member in ("S", "N", "B", "SS", "NS", "BS", "M", "L", "NULL", "BOOL", "Bool"):
                return value
        return {"M": {k: to_attribute_value(v) for k, v in value.items()}}
    if isinstance(value, bool):
        # Before the int branch: bool is a subclass of int and would serialize as a number.
        return {"BOOL": value}
    if value is None:
        return {"NULL": True}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, (list, tuple)):
        return {"L": [to_attribute_value(v) for v in value]}
    return {"S": str(value)}

@dataclass(frozen=True, kw_only=True)
class ConditionalWrite:
    """A ready-to-pass `update_item` request. Built here so the guard can be asserted in tests."""

    table_name: str
    key: Mapping[str, Any]
    update_expression: str
    condition_expression: str
    expression_attribute_names: Mapping[str, str]
    expression_attribute_values: Mapping[str, Any]

    def to_request(self) -> dict[str, Any]:
        """The `update_item` kwargs in PLAIN Python values.

        Deliberately not AttributeValue-serialized: this module is pure functions over plain values,
        which is what makes the decision rules testable, so the DynamoDB wire format is applied at
        the caller's boundary (`review_handler.apply_decision`) via `to_attribute_value`.
        """
        return {
            "TableName": self.table_name,
            "Key": dict(self.key),
            "UpdateExpression": self.update_expression,
            "ConditionExpression": self.condition_expression,
            "ExpressionAttributeNames": dict(self.expression_attribute_names),
            "ExpressionAttributeValues": dict(self.expression_attribute_values),
            # The loser needs to see who won without a second read, and DynamoDB returns the
            # current item on a failed condition only when explicitly asked.
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }


def parse_action(value: object) -> ReviewAction | None:
    """Never raises — an unknown action is a reviewer-visible validation failure, not a crash."""
    if isinstance(value, ReviewAction):
        return value
    try:
        return ReviewAction(str(value).strip().upper())
    except ValueError:
        return None


def to_utc_iso(moment: datetime) -> str:
    """R6.3 requires a UTC timestamp. Naive input is refused rather than assumed to be UTC."""
    if moment.tzinfo is None:
        raise ValueError("review timestamps must be timezone-aware; R6.3 requires UTC")
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def existing_review(case: Mapping[str, Any], phase: str) -> RecordedDecision | None:
    """The review decision already stored for this phase, if any (Property 10).

    Reads the sibling `<phase>Review` attribute the write targets, and still accepts the older
    nested `<phase>.review` form so a case decided before that change is not misread as undecided —
    which would let a second reviewer overwrite a real decision.
    """
    stored: Any = case.get(f"{phase}Review")
    if not isinstance(stored, Mapping) or not stored:
        phase_block = case.get(phase)
        if not isinstance(phase_block, Mapping):
            return None
        stored = phase_block.get("review")
    if not isinstance(stored, Mapping) or not stored:
        return None
    return recorded_from_item(stored)


def preliminary_tier(case: Mapping[str, Any], phase: str) -> str | None:
    """The tier the rules produced for this phase, whatever the phase calls its tier field."""
    phase_block = case.get(phase)
    if not isinstance(phase_block, Mapping):
        return None
    for key in ("preliminaryTier", "fullTier", "tier"):
        value = phase_block.get(key)
        if value:
            return str(value)
    return None


def validate_submission(
    submission: ReviewSubmission, *, case: Mapping[str, Any] | None
) -> Invalid | None:
    """Every reason this submission may not be written. `None` means admissible.

    All reasons are collected rather than returned one at a time: a reviewer fixing a form should
    see the whole problem, and a test asserting on one reason should not mask another.
    """
    reasons: list[InvalidReason] = []
    detail: list[str] = []

    if not isinstance(submission.action, ReviewAction):
        # The receiver parks an unrecognised action on the submission rather than raising, so that
        # a client sending "approved" gets a 422 naming the field instead of a 500.
        reasons.append(InvalidReason.UNKNOWN_ACTION)
        detail.append(
            f"action must be one of {', '.join(a.value for a in ReviewAction)}")
    if not submission.reviewer_id.strip():
        # R6.3 — reviewer identity is recorded on every decision, so an anonymous one cannot exist.
        reasons.append(InvalidReason.MISSING_REVIEWER)
        detail.append("reviewer identity is required (R6.3)")
    if not submission.rationale.strip():
        # Free text, so length is not policed; emptiness is, because R6.3 mandates a rationale and
        # whitespace is indistinguishable from none in the audit record.
        reasons.append(InvalidReason.MISSING_RATIONALE)
        detail.append("a free-text rationale is required (R6.3)")
    if not submission.task_token.strip():
        # Without the token the case can be recorded but never resumed, which is worse than a
        # rejected submission: R6.4 would be silently unmet.
        reasons.append(InvalidReason.MISSING_TASK_TOKEN)
        detail.append("task token is required to resume the execution (R6.4, F-12)")
    if submission.phase not in PHASES:
        reasons.append(InvalidReason.UNKNOWN_PHASE)
        detail.append(f"unknown phase {submission.phase!r}")
    if submission.submitted_at.tzinfo is None:
        reasons.append(InvalidReason.NAIVE_TIMESTAMP)
        detail.append("submitted_at must be timezone-aware (R6.3)")

    if case is None:
        reasons.append(InvalidReason.MISSING_CASE)
        detail.append(f"no case record for {submission.case_id}")
    else:
        if str(case.get("caseId", "")) != submission.case_id:
            # A token plus a mismatched caseId is either a client bug or an attempt to decide a
            # different applicant's case. Either way it must not write.
            reasons.append(InvalidReason.CASE_MISMATCH)
            detail.append("caseId does not match the case record")
        if (submission.action is ReviewAction.APPROVE
                and preliminary_tier(case, submission.phase) == PROHIBITED_TIER):
            reasons.append(InvalidReason.PROHIBITED_TIER_NOT_OVERRIDABLE)
            detail.append(
                "R4.3 and Property 2 forbid opening a provisional account on a PROHIBITED tier; "
                "reject or request step-up instead")
        elif submission.action is ReviewAction.APPROVE and confirmed_matches(case):
            # The tier is absent when the rules never ran — a pipeline that degraded before the
            # decision engine — so the recorded screening is checked directly as well. Without
            # this, a confirmed match on a degraded case was one click from an open account.
            reasons.append(InvalidReason.PROHIBITED_TIER_NOT_OVERRIDABLE)
            detail.append(
                "the recorded screening carries a CONFIRMED match "
                f"({', '.join(confirmed_matches(case))}), which no reviewer may approve past "
                "(R4.3)")
        if (isinstance(submission.action, ReviewAction)
                and submission.phase in TARGET_STATES
                and submission.action not in TARGET_STATES[submission.phase]
                and not (submission.phase == "phase3"
                         and submission.action is ReviewAction.APPROVE)):
            reasons.append(InvalidReason.ACTION_NOT_AVAILABLE)
            detail.append(
                f"{submission.action.value} is not available in {submission.phase}: a lifecycle "
                "review clears the alert or exits the relationship")

    if not reasons:
        return None
    return Invalid(reasons=tuple(reasons), detail="; ".join(detail))


def resolve(*, submission: ReviewSubmission, case: Mapping[str, Any] | None) -> ReviewOutcome:
    """Advisory verdict from a just-read case record.

    Order matters. Validation runs first so that a forbidden APPROVE is reported as forbidden
    rather than as a conflict, and replay detection runs before conflict detection so that a
    redelivery of the winning submission is not reported to the winner as a conflict with itself.
    """
    invalid = validate_submission(submission, case=case)
    if invalid is not None:
        return ReviewOutcome(
            resolution=Resolution.INVALID, submission=submission, invalid=invalid,
            detail=invalid.detail)

    assert case is not None  # validate_submission returns MISSING_CASE otherwise
    stored = existing_review(case, submission.phase)
    if stored is not None:
        if stored.idempotency_key == submission.idempotency_key:
            return ReviewOutcome(
                resolution=Resolution.REPLAY, submission=submission, decision=stored,
                existing=stored, detail="already recorded by this submission")
        # R6.5 / R30.4 — the second reviewer is told explicitly and shown the existing decision.
        return ReviewOutcome(
            resolution=Resolution.CONFLICT, submission=submission, existing=stored,
            detail=(f"case {submission.case_id} {submission.phase} was already decided by "
                    f"{stored.reviewer_id} at {stored.decided_at}"))

    state = str(case.get("state", ""))
    if state != AWAITING_REVIEW:
        # No review decision stored, yet the case is not awaiting review: Phase 2, an appeal, or a
        # timeout path moved it. The waiter is orphaned and must be released rather than left to
        # time out hours later.
        return ReviewOutcome(
            resolution=Resolution.UNRESUMABLE, submission=submission,
            detail=f"case is in {state or 'UNKNOWN'}, not {AWAITING_REVIEW}")

    return ReviewOutcome(
        resolution=Resolution.ACCEPT, submission=submission,
        decision=build_decision(submission, case))


def build_decision(submission: ReviewSubmission,
                   case: Mapping[str, Any] | None = None) -> RecordedDecision:
    """The record R6.3 requires: identity, UTC timestamp, decision, rationale.

    An APPROVE also records the screening alerts it resolves, read from the case as it stood when
    the reviewer decided.
    """
    approve = submission.action is ReviewAction.APPROVE
    return RecordedDecision(
        case_id=submission.case_id,
        phase=submission.phase,
        reviewer_id=submission.reviewer_id,
        action=submission.action.value,
        rationale=submission.rationale,
        decided_at=to_utc_iso(submission.submitted_at),
        idempotency_key=submission.idempotency_key,
        target_state=target_state(submission.action, submission.phase, case),
        cleared_alerts=clearable_alerts(case) if approve else (),
    )


def target_state(action: ReviewAction, phase: str, case: Mapping[str, Any] | None) -> str:
    """Where this action leaves the case in this phase. Phase 3's APPROVE restores day zero."""
    if phase == "phase3" and action is ReviewAction.APPROVE:
        return day_zero_state(case)
    return TARGET_STATES.get(phase, TARGET_STATE)[action]


def day_zero_state(case: Mapping[str, Any] | None) -> str:
    """The case state before a lifecycle event: what Phase 2 decided, or still provisional."""
    phase2 = (case or {}).get("phase2")
    outcome = phase2.get("outcome") if isinstance(phase2, Mapping) else None
    return _DAY_ZERO_STATE_BY_PHASE2_OUTCOME.get(str(outcome or ""), "PROVISIONAL")


def _screening_fact(case: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The recorded screening fact, as a Mapping, whichever way it was stored."""
    raw = (case or {}).get("factScreening")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
    return raw if isinstance(raw, Mapping) else {}


def _hits(case: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return [h for h in _screening_fact(case).get("hits") or () if isinstance(h, Mapping)]


def confirmed_matches(case: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Listing ids of every CONFIRMED alert on the recorded screening."""
    return tuple(sorted({str(h.get("entryId")) for h in _hits(case)
                         if str(h.get("classification") or "").upper() == "CONFIRMED"
                         and h.get("entryId")}))


def clearable_alerts(case: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Listing ids of the PROBABLE / POSSIBLE alerts an APPROVE resolves."""
    return tuple(sorted({str(h.get("entryId")) for h in _hits(case)
                         if str(h.get("classification") or "").upper()
                         in CLEARABLE_CLASSIFICATIONS and h.get("entryId")}))


def screening_dispositions(case: Mapping[str, Any] | None,
                           decision: RecordedDecision) -> list[dict[str, Any]]:
    """The case's dispositions with this decision's cleared alerts merged in, one per listing.

    The rules read these (`decision-engine/facts.py`) and drop a PROBABLE or POSSIBLE alert whose
    listing a reviewer has cleared, so the same customer is not sent round again for the same
    alert in Phase 2 or after a list update. A CONFIRMED alert is never cleared, here or there.
    """
    existing: list[dict[str, Any]] = []
    raw = (case or {}).get("screeningDispositions")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raw = []
    for item in raw or ():
        if isinstance(item, Mapping) and item.get("entryId"):
            existing.append(dict(item))
    known = {d["entryId"] for d in existing}
    fact = _screening_fact(case)
    by_id = {str(h.get("entryId")): h for h in _hits(case)}
    for entry_id in decision.cleared_alerts:
        if entry_id in known:
            continue
        existing.append({
            "entryId": entry_id,
            "classification": str(by_id.get(entry_id, {}).get("classification") or ""),
            "listVersion": fact.get("listVersion"),
            "phase": decision.phase,
            "reviewerId": decision.reviewer_id,
            "decidedAt": decision.decided_at,
        })
    return existing


def build_conditional_write(
    *,
    submission: ReviewSubmission,
    decision: RecordedDecision,
    table_name: str,
    keys: TableKeySchema = TableKeySchema(),
    case: Mapping[str, Any] | None = None,
) -> ConditionalWrite:
    """One atomic conditional update. This expression *is* first-writer-wins (R6.5, R21.4).

    `case` is the record the decision was resolved against; an APPROVE merges its cleared alerts
    into the case's existing screening dispositions.

    Three clauses in the condition, each for a distinct failure:

    * `attribute_not_exists(#reviewAttr)` — the race itself. The second writer fails here.
    * `#state = :awaiting` — the case has not moved on since it was read.
    * `attribute_exists(#phase)` — the phase must have run before it can be reviewed.

    The decision is written to a **sibling** attribute (`phase1Review`, `phase2Review`), not nested
    as `phase1.review`. `asl.py`'s `OpenProvisional` stores `phase1` as a JSON *string* — the same
    convention it uses for `phase1Evidence` and `documentExtraction` — and a nested `SET` into a
    string is not a valid document path: DynamoDB rejects it with
    `ValidationException: The document path provided in the update expression is invalid for update`,
    so every reviewer decision returned 500. Found only end to end, because no reviewer could obtain
    a task token to submit one until the token source was fixed. A sibling attribute keeps the
    orchestrator's storage convention intact instead of forcing the ASL to emit a deeply nested
    DynamoDB Map from JSONata.

    All of it is one `update_item` on one item on purpose. Splitting the decision write from the
    state transition would open a window in which a case is decided but not transitioned, and no
    conditional expression spanning two writes can close it.
    """
    names = {
        "#phase": submission.phase,
        "#reviewAttr": f"{submission.phase}Review",
        "#state": "state",
        "#transitions": "transitions",
    }
    values: dict[str, Any] = {
        ":decision": decision.to_item(),
        ":target": decision.target_state,
        ":awaiting": AWAITING_REVIEW,
        ":empty": [],
        # R2.5 — every transition recorded, with the reviewer as the actor rather than "system".
        ":transition": [{
            "from": AWAITING_REVIEW,
            "to": decision.target_state,
            "at": decision.decided_at,
            "actor": decision.reviewer_id,
            "reason": f"review {decision.action}",
        }],
    }
    update = ("SET #reviewAttr = :decision, #state = :target, "
              "#transitions = list_append(if_not_exists(#transitions, :empty), :transition)")
    if decision.cleared_alerts:
        # In the same write as the decision, so an alert is cleared exactly when the decision
        # that cleared it lands. A JSON string, which is how the state machines read it.
        names["#dispositions"] = "screeningDispositions"
        values[":dispositions"] = json.dumps(screening_dispositions(case, decision),
                                             sort_keys=True)
        update += ", #dispositions = :dispositions"
    return ConditionalWrite(
        table_name=table_name,
        # Plain values: `ConditionalWrite.to_request` serializes to AttributeValues at the
        # boundary, so nothing in this module has to carry the wire format.
        key={keys.partition_key: submission.applicant_id, keys.sort_key: keys.case_sort_value},
        update_expression=update,
        condition_expression=(
            "attribute_exists(#phase) AND attribute_not_exists(#reviewAttr) "
            "AND #state = :awaiting"),
        expression_attribute_names=names,
        expression_attribute_values=values,
    )


def conflict_after_write_failure(
    *, submission: ReviewSubmission, case: Mapping[str, Any] | None
) -> ReviewOutcome:
    """Turn a `ConditionalCheckFailedException` into the right answer for this reviewer.

    `case` is the item DynamoDB returned with the failure (or a re-read). The distinction that
    matters: if the stored decision carries *this* submission's idempotency key the write already
    succeeded on an earlier delivery and this is a replay, not a loss. Reporting that as a conflict
    would make a redelivered Lambda invocation look like a second reviewer.
    """
    if case is None:
        return ReviewOutcome(
            resolution=Resolution.CONFLICT, submission=submission,
            detail="conditional write failed and the case could not be read back")

    stored = existing_review(case, submission.phase)
    if stored is not None:
        if stored.idempotency_key == submission.idempotency_key:
            return ReviewOutcome(
                resolution=Resolution.REPLAY, submission=submission, decision=stored,
                existing=stored, detail="write had already been applied by this submission")
        return ReviewOutcome(
            resolution=Resolution.CONFLICT, submission=submission, existing=stored,
            detail=(f"case {submission.case_id} {submission.phase} was already decided by "
                    f"{stored.reviewer_id} at {stored.decided_at}"))

    state = str(case.get("state", ""))
    return ReviewOutcome(
        resolution=Resolution.UNRESUMABLE, submission=submission,
        detail=f"case left {AWAITING_REVIEW} before the write landed (now {state or 'UNKNOWN'})")


def resume_payload(outcome: ReviewOutcome) -> dict[str, Any]:
    """What `SendTaskSuccess` carries back into the state machine (design §7.5).

    Deliberately thin: the state machine reads the case record for everything else, and duplicating
    agent results into the token payload would create a second, divergeable copy of the decision.
    The rationale is not here either: it would land in execution history. The audit writer reads
    it from the review this write stored (`<phase>Review`) and seals it with the decision (D-42).
    """
    decision = outcome.decision
    if decision is None:
        raise ValueError("no decision to resume with")
    return {
        "reviewOutcome": {
            "caseId": decision.case_id,
            "phase": decision.phase,
            "action": decision.action,
            "reviewerId": decision.reviewer_id,
            "decidedAt": decision.decided_at,
            "targetState": decision.target_state,
            "clearedAlerts": list(decision.cleared_alerts),
            "replay": outcome.resolution is Resolution.REPLAY,
        }
    }


@dataclass(frozen=True, kw_only=True)
class QueuedCase:
    """One case sitting in the review queue, as read from the `state` GSI (design §9.1)."""

    case_id: str
    enqueued_at: datetime
    jurisdiction: str = "UNKNOWN"
    labels: tuple[str, ...] = field(default_factory=tuple)

    def age_seconds(self, *, now: datetime) -> float:
        if self.enqueued_at.tzinfo is None or now.tzinfo is None:
            raise ValueError("queue age needs timezone-aware instants")
        return max(0.0, (now - self.enqueued_at).total_seconds())


def queue_ages(cases: Sequence[QueuedCase], *, now: datetime) -> tuple[float, ...]:
    """Ages, oldest first. Kept pure so the metric values are testable without CloudWatch."""
    return tuple(sorted((case.age_seconds(now=now) for case in cases), reverse=True))
