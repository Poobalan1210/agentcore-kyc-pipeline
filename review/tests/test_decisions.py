"""Pure review-decision layer — task 6.5, R6.1–R6.7, R21, design §7.5."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from conftest import NOW, TABLE, case_record, submission
from decisions import (
    AWAITING_REVIEW,
    InvalidReason,
    QueuedCase,
    Resolution,
    ReviewAction,
    TARGET_STATE,
    build_conditional_write,
    build_decision,
    conflict_after_write_failure,
    existing_review,
    parse_action,
    queue_ages,
    resolve,
    resume_payload,
    to_utc_iso,
)


class TestValidation:
    def test_a_complete_submission_is_admissible(self):
        outcome = resolve(submission=submission(), case=case_record())
        assert outcome.resolution is Resolution.ACCEPT
        assert outcome.status == 200

    def test_a_missing_rationale_is_refused(self):
        # R6.3 — the rationale is part of the record, not a courtesy.
        outcome = resolve(submission=submission(rationale="   "), case=case_record())
        assert outcome.resolution is Resolution.INVALID
        assert InvalidReason.MISSING_RATIONALE in outcome.invalid.reasons

    def test_a_missing_reviewer_identity_is_refused(self):
        outcome = resolve(submission=submission(reviewer_id=""), case=case_record())
        assert InvalidReason.MISSING_REVIEWER in outcome.invalid.reasons

    def test_a_missing_task_token_is_refused(self):
        # Recording a decision that can never resume the execution would leave R6.4 silently unmet.
        outcome = resolve(submission=submission(task_token=""), case=case_record())
        assert InvalidReason.MISSING_TASK_TOKEN in outcome.invalid.reasons

    def test_an_unknown_action_is_refused_not_defaulted(self):
        outcome = resolve(submission=submission(action="approved"), case=case_record())
        assert InvalidReason.UNKNOWN_ACTION in outcome.invalid.reasons

    def test_an_unknown_phase_is_refused(self):
        outcome = resolve(submission=submission(phase="phase3"), case=case_record())
        assert InvalidReason.UNKNOWN_PHASE in outcome.invalid.reasons

    def test_a_naive_timestamp_is_refused(self):
        outcome = resolve(
            submission=submission(submitted_at=datetime(2026, 8, 9, 12, 0, 0)),
            case=case_record())
        assert InvalidReason.NAIVE_TIMESTAMP in outcome.invalid.reasons

    def test_a_missing_case_is_refused(self):
        outcome = resolve(submission=submission(), case=None)
        assert InvalidReason.MISSING_CASE in outcome.invalid.reasons

    def test_a_case_id_mismatch_is_refused(self):
        # A valid token plus the wrong caseId is either a client bug or a cross-case write attempt.
        outcome = resolve(submission=submission(), case=case_record(case_id="CASE-999"))
        assert InvalidReason.CASE_MISMATCH in outcome.invalid.reasons

    def test_all_problems_are_reported_at_once(self):
        outcome = resolve(submission=submission(rationale="", reviewer_id=""), case=None)
        assert {InvalidReason.MISSING_RATIONALE, InvalidReason.MISSING_REVIEWER,
                InvalidReason.MISSING_CASE} <= set(outcome.invalid.reasons)

    def test_an_invalid_submission_is_never_treated_as_resumable(self):
        # The case must stay in AWAITING_REVIEW; nothing may touch the token.
        assert not resolve(submission=submission(rationale=""), case=case_record()).resume_case

    @pytest.mark.parametrize("raw,expected", [
        ("approve", ReviewAction.APPROVE),
        (" REJECT ", ReviewAction.REJECT),
        ("request_step_up", ReviewAction.REQUEST_STEP_UP),
        ("escalate", None),
        (None, None),
    ])
    def test_action_parsing_never_raises(self, raw, expected):
        assert parse_action(raw) is expected


class TestProhibitedTierCannotBeApproved:
    """R4.3 and Property 2 name no human exception, so the reviewer does not get one."""

    def test_approve_on_a_prohibited_tier_is_refused(self):
        outcome = resolve(
            submission=submission(action=ReviewAction.APPROVE),
            case=case_record(tier="PROHIBITED"))
        assert outcome.resolution is Resolution.INVALID
        assert InvalidReason.PROHIBITED_TIER_NOT_OVERRIDABLE in outcome.invalid.reasons

    def test_the_refusal_names_the_requirement_so_the_console_can_explain_it(self):
        outcome = resolve(
            submission=submission(action=ReviewAction.APPROVE),
            case=case_record(tier="PROHIBITED"))
        assert "R4.3" in outcome.detail

    @pytest.mark.parametrize("action", [ReviewAction.REJECT, ReviewAction.REQUEST_STEP_UP])
    def test_the_reviewer_is_not_locked_out_of_the_case(self, action):
        # Only the forbidden action is blocked. A blanket lock-out would strand the case.
        outcome = resolve(submission=submission(action=action),
                          case=case_record(tier="PROHIBITED"))
        assert outcome.resolution is Resolution.ACCEPT

    def test_a_prohibited_full_tier_is_caught_too(self):
        case = case_record(phase="phase2")
        case["phase2"] = {"fullTier": "PROHIBITED"}
        outcome = resolve(submission=submission(phase="phase2"), case=case)
        assert InvalidReason.PROHIBITED_TIER_NOT_OVERRIDABLE in outcome.invalid.reasons

    @pytest.mark.parametrize("tier", ["LOW", "MEDIUM", "HIGH"])
    def test_every_other_tier_may_be_approved(self, tier):
        assert resolve(submission=submission(), case=case_record(tier=tier)
                       ).resolution is Resolution.ACCEPT


class TestFirstWriterWins:
    """R6.5 — the behaviour the whole task hangs on."""

    def test_a_second_reviewer_gets_a_conflict(self):
        first = build_decision(submission(reviewer_id="reviewer-a"))
        second = submission(reviewer_id="reviewer-b", submission_id="req-2")
        outcome = resolve(submission=second, case=case_record(review=first.to_item()))
        assert outcome.resolution is Resolution.CONFLICT
        assert outcome.status == 409

    def test_the_conflict_shows_the_existing_decision(self):
        # R30.4 — the second reviewer sees what won, not just that they lost.
        first = build_decision(submission(reviewer_id="reviewer-a"))
        outcome = resolve(
            submission=submission(reviewer_id="reviewer-b", submission_id="req-2"),
            case=case_record(review=first.to_item()))
        assert outcome.existing.reviewer_id == "reviewer-a"
        assert outcome.existing.action == "APPROVE"

    def test_the_loser_never_resumes_the_execution(self):
        first = build_decision(submission(reviewer_id="reviewer-a"))
        outcome = resolve(
            submission=submission(reviewer_id="reviewer-b", submission_id="req-2",
                                  action=ReviewAction.REJECT),
            case=case_record(review=first.to_item()))
        assert not outcome.resume_case

    def test_a_conflicting_decision_is_never_returned_for_writing(self):
        # There must be no path on which the loser's decision object reaches a write.
        first = build_decision(submission(reviewer_id="reviewer-a"))
        outcome = resolve(
            submission=submission(reviewer_id="reviewer-b", submission_id="req-2"),
            case=case_record(review=first.to_item()))
        assert outcome.decision is None

    def test_a_conflict_is_reported_even_when_the_second_action_is_identical(self):
        # Same action, different reviewer: still a second decision, still a conflict. Treating it as
        # a harmless no-op would lose the fact that two humans acted.
        first = build_decision(submission(reviewer_id="reviewer-a"))
        outcome = resolve(
            submission=submission(reviewer_id="reviewer-b", submission_id="req-2"),
            case=case_record(review=first.to_item()))
        assert outcome.resolution is Resolution.CONFLICT

    def test_a_decision_in_the_other_phase_does_not_conflict(self):
        # Property 10 is per phase: Phase 2 review is a separate decision on the same case.
        case = case_record(state=AWAITING_REVIEW)
        case["phase1"]["review"] = build_decision(submission()).to_item()
        case["phase2"] = {"fullTier": "HIGH"}
        outcome = resolve(submission=submission(phase="phase2", submission_id="req-2"), case=case)
        assert outcome.resolution is Resolution.ACCEPT


class TestReplayIsNotAConflict:
    """R21.3, R21.5 — at-least-once delivery must not look like a second reviewer."""

    def test_the_same_submission_seen_twice_is_a_replay(self):
        sub = submission()
        outcome = resolve(
            submission=sub, case=case_record(review=build_decision(sub).to_item()))
        assert outcome.resolution is Resolution.REPLAY
        assert outcome.status == 200

    def test_a_replay_returns_the_stored_decision_unchanged(self):
        sub = submission()
        stored = build_decision(sub)
        outcome = resolve(submission=sub, case=case_record(review=stored.to_item()))
        assert outcome.decision == stored

    def test_a_replay_still_closes_the_loop(self):
        # The first delivery may have died between the write and SendTaskSuccess.
        sub = submission()
        outcome = resolve(submission=sub, case=case_record(review=build_decision(sub).to_item()))
        assert outcome.resume_case

    def test_the_same_reviewer_with_a_new_submission_id_is_a_conflict_not_a_replay(self):
        # Two deliberate decisions from one reviewer are still two decisions.
        stored = build_decision(submission(submission_id="req-1"))
        outcome = resolve(
            submission=submission(submission_id="req-2"),
            case=case_record(review=stored.to_item()))
        assert outcome.resolution is Resolution.CONFLICT

    def test_the_idempotency_key_covers_applicant_case_phase_reviewer_and_event(self):
        # R21.2's shape, extended so a genuine second reviewer can never collide with a redelivery.
        key = submission().idempotency_key
        assert key.split("#") == ["APP-001", "CASE-001", "phase1", "reviewer-a", "req-1"]


class TestStateGuard:
    def test_a_case_that_left_awaiting_review_is_unresumable(self):
        outcome = resolve(submission=submission(), case=case_record(state="DECIDED"))
        assert outcome.resolution is Resolution.UNRESUMABLE
        assert outcome.status == 410

    def test_an_unresumable_case_is_not_resumed_as_a_review(self):
        outcome = resolve(submission=submission(), case=case_record(state="PROVISIONAL"))
        assert not outcome.resume_case
        assert outcome.decision is None

    def test_a_stored_decision_takes_precedence_over_the_state_check(self):
        # A decided case is legitimately no longer AWAITING_REVIEW; reporting UNRESUMABLE would hide
        # the existing decision that R30.4 requires the reviewer to be shown.
        first = build_decision(submission(reviewer_id="reviewer-a"))
        outcome = resolve(
            submission=submission(reviewer_id="reviewer-b", submission_id="req-2"),
            case=case_record(state="PROVISIONAL", review=first.to_item()))
        assert outcome.resolution is Resolution.CONFLICT


class TestRecordedDecision:
    def test_every_field_r6_3_requires_is_present(self):
        item = build_decision(submission()).to_item()
        assert item["reviewerId"] == "reviewer-a"
        assert item["decidedAt"] == "2026-08-09T12:00:00Z"
        assert item["action"] == "APPROVE"
        assert item["rationale"]

    def test_timestamps_are_normalised_to_utc(self):
        moment = datetime(2026, 8, 9, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        assert to_utc_iso(moment) == "2026-08-09T12:00:00Z"

    def test_a_naive_timestamp_is_never_assumed_to_be_utc(self):
        with pytest.raises(ValueError):
            to_utc_iso(datetime(2026, 8, 9, 12, 0, 0))

    @pytest.mark.parametrize("action,state", [
        (ReviewAction.APPROVE, "PROVISIONAL"),
        (ReviewAction.REJECT, "DECIDED"),
        (ReviewAction.REQUEST_STEP_UP, "AWAITING_APPLICANT"),
    ])
    def test_each_action_targets_the_state_the_diagram_allows(self, action, state):
        assert build_decision(submission(action=action)).target_state == state

    def test_every_action_has_a_target_state(self):
        assert set(TARGET_STATE) == set(ReviewAction)

    def test_a_stored_decision_round_trips(self):
        decision = build_decision(submission())
        assert existing_review(case_record(review=decision.to_item()), "phase1") == decision

    def test_an_empty_review_map_is_not_an_existing_decision(self):
        assert existing_review(case_record(review={}), "phase1") is None


class TestConditionalWrite:
    def build(self, sub=None):
        sub = sub or submission()
        return build_conditional_write(
            submission=sub, decision=build_decision(sub), table_name=TABLE)

    def test_the_condition_guards_against_an_existing_review(self):
        # This clause *is* first-writer-wins. Without it the second write silently overwrites.
        # Guards the sibling attribute the decision is written to, not a nested path into the
        # phase blob (which `asl.py` stores as a JSON string).
        assert "attribute_not_exists(#reviewAttr)" in self.build().condition_expression

    def test_the_condition_guards_the_case_state(self):
        write = self.build()
        assert "#state = :awaiting" in write.condition_expression
        assert write.expression_attribute_values[":awaiting"] == AWAITING_REVIEW

    def test_the_condition_guards_the_phase_map_existing(self):
        # A nested SET into an absent map raises ValidationException, which would surface as a 500
        # instead of a clean conflict.
        assert "attribute_exists(#phase)" in self.build().condition_expression

    def test_the_decision_and_the_transition_are_one_write(self):
        # Two writes would open a window where a case is decided but not transitioned, and no
        # condition spanning two writes can close it.
        update = self.build().update_expression
        # A SIBLING attribute, not `#phase.#review`: `asl.py` stores `phase1` as a JSON string, and
        # a nested SET into a string is rejected with
        # "The document path provided in the update expression is invalid for update" — which made
        # every reviewer decision a 500.
        assert "#reviewAttr = :decision" in update
        assert "#phase.#review" not in update
        assert "#state = :target" in update
        assert "list_append" in update

    def test_the_phase_is_a_name_placeholder_not_interpolated(self):
        write = self.build(submission(phase="phase2"))
        assert write.expression_attribute_names["#phase"] == "phase2"
        assert "phase2" not in write.update_expression

    def test_the_key_addresses_the_case_record(self):
        """Plain values here on purpose.

        `decisions.py` stays pure Python; the DynamoDB wire format is applied at the caller's
        boundary by `review_handler._serialized_request`. Wrapping in both places would produce a
        double-wrapped key that DynamoDB accepts and stores as garbage.
        """
        assert self.build().key == {"applicantId": "APP-001", "sk": "CASE"}

    def test_the_request_asks_for_the_winning_item_on_failure(self):
        # So the loser can be shown the existing decision without a second read.
        assert self.build().to_request()["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"

    def test_the_transition_records_the_reviewer_as_the_actor(self):
        # R2.5 — "system" would lose the fact that a human decided this.
        transition, = self.build().expression_attribute_values[":transition"]
        assert transition["actor"] == "reviewer-a"
        assert (transition["from"], transition["to"]) == (AWAITING_REVIEW, "PROVISIONAL")


class TestConflictAfterWriteFailure:
    """The authoritative path: the read said ACCEPT, the write disagreed."""

    def test_a_lost_race_becomes_a_conflict(self):
        winner = build_decision(submission(reviewer_id="reviewer-a"))
        outcome = conflict_after_write_failure(
            submission=submission(reviewer_id="reviewer-b", submission_id="req-2"),
            case=case_record(state="PROVISIONAL", review=winner.to_item()))
        assert outcome.resolution is Resolution.CONFLICT
        assert outcome.existing.reviewer_id == "reviewer-a"

    def test_an_already_applied_write_becomes_a_replay(self):
        # The write landed on an earlier delivery and the invocation died before the token send.
        # Calling that a conflict would make a retried Lambda look like a second reviewer.
        sub = submission()
        outcome = conflict_after_write_failure(
            submission=sub, case=case_record(review=build_decision(sub).to_item()))
        assert outcome.resolution is Resolution.REPLAY
        assert outcome.resume_case

    def test_a_state_change_with_no_decision_is_unresumable(self):
        outcome = conflict_after_write_failure(
            submission=submission(), case=case_record(state="DECIDED"))
        assert outcome.resolution is Resolution.UNRESUMABLE

    def test_an_unreadable_case_is_reported_as_a_conflict_not_a_success(self):
        # Failing closed: the condition rejected the write, so something exists. Never resume.
        outcome = conflict_after_write_failure(submission=submission(), case=None)
        assert outcome.resolution is Resolution.CONFLICT
        assert not outcome.resume_case


class TestResumePayload:
    def test_the_payload_names_the_action_and_the_target_state(self):
        outcome = resolve(submission=submission(), case=case_record())
        payload = resume_payload(outcome)["reviewOutcome"]
        assert payload["action"] == "APPROVE"
        assert payload["targetState"] == "PROVISIONAL"
        assert payload["replay"] is False

    def test_the_payload_carries_no_rationale(self):
        # Free text about a real person does not belong in execution history (R18.8).
        outcome = resolve(submission=submission(), case=case_record())
        assert "rationale" not in resume_payload(outcome)["reviewOutcome"]

    def test_a_replay_is_marked_so_downstream_can_stay_idempotent(self):
        sub = submission()
        outcome = resolve(submission=sub, case=case_record(review=build_decision(sub).to_item()))
        assert resume_payload(outcome)["reviewOutcome"]["replay"] is True

    def test_there_is_no_payload_without_a_decision(self):
        outcome = resolve(submission=submission(), case=case_record(state="DECIDED"))
        with pytest.raises(ValueError):
            resume_payload(outcome)


class TestQueueAges:
    def test_ages_are_oldest_first(self):
        cases = [
            QueuedCase(case_id="A", enqueued_at=NOW - timedelta(minutes=5)),
            QueuedCase(case_id="B", enqueued_at=NOW - timedelta(minutes=90)),
        ]
        assert queue_ages(cases, now=NOW) == (5400.0, 300.0)

    def test_an_empty_queue_has_no_ages(self):
        assert queue_ages([], now=NOW) == ()

    def test_a_future_enqueue_time_clamps_to_zero(self):
        # Clock skew between the API and this Lambda must not publish a negative age.
        case = QueuedCase(case_id="A", enqueued_at=NOW + timedelta(seconds=30))
        assert case.age_seconds(now=NOW) == 0.0

    def test_naive_instants_are_refused(self):
        case = QueuedCase(case_id="A", enqueued_at=datetime(2026, 8, 9, 11, 0, 0))
        with pytest.raises(ValueError):
            case.age_seconds(now=NOW)
