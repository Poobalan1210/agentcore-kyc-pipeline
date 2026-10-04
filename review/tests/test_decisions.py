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
        outcome = resolve(submission=submission(phase="phase9"), case=case_record())
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


# ------------------------------------------------------------------ phases 2 and 3, dispositions

import json  # noqa: E402

from decisions import (  # noqa: E402
    TARGET_STATES,
    clearable_alerts,
    confirmed_matches,
    day_zero_state,
    screening_dispositions,
    target_state,
)


def _screening(*hits: tuple[str, str], list_version: str = "SYNTHETIC-2026-10-01") -> str:
    """A screening fact as the tool records it: a JSON string on the case."""
    return json.dumps({"performed": True, "listVersion": list_version,
                       "hits": [{"entryId": e, "classification": c} for e, c in hits]})


def _case(phase: str = "phase1", **extra) -> dict:
    case = case_record(phase=phase)
    case.update(extra)
    return case


class TestEveryPhaseHasItsOwnTargets:
    """The state machine checks the state the reviewer left the case in before acting on it, so
    the two must agree phase by phase (`infra/kyc/asl.py`)."""

    @pytest.mark.parametrize("phase,action,state", [
        ("phase1", ReviewAction.APPROVE, "PROVISIONAL"),
        ("phase1", ReviewAction.REJECT, "DECIDED"),
        ("phase1", ReviewAction.REQUEST_STEP_UP, "AWAITING_APPLICANT"),
        ("phase2", ReviewAction.APPROVE, "MONITORING"),
        ("phase2", ReviewAction.REJECT, "FROZEN"),
        ("phase2", ReviewAction.REQUEST_STEP_UP, "RESTRICTED"),
        ("phase3", ReviewAction.REJECT, "FROZEN"),
    ])
    def test_targets(self, phase, action, state):
        decision = build_decision(submission(phase=phase, action=action), _case(phase))
        assert decision.target_state == state

    @pytest.mark.parametrize("phase2_outcome,state", [
        ("FULL_APPROVE_STANDARD", "MONITORING"),
        ("FULL_APPROVE_ENHANCED_MONITORING", "MONITORING"),
        ("RESTRICT_FURTHER", "RESTRICTED"),
        ("MANUAL_REVIEW", "PROVISIONAL"),
        (None, "PROVISIONAL"),
    ])
    def test_a_lifecycle_clearance_restores_the_day_zero_state(self, phase2_outcome, state):
        case = _case("phase3")
        if phase2_outcome:
            case["phase2"] = {"outcome": phase2_outcome}
        assert day_zero_state(case) == state
        assert target_state(ReviewAction.APPROVE, "phase3", case) == state

    def test_a_lifecycle_review_has_no_step_up(self):
        """Nothing in Phase 3 asks the customer for anything; the execution would have nowhere to
        go, so the console is told no rather than the workflow failing."""
        outcome = resolve(submission=submission(phase="phase3",
                                                action=ReviewAction.REQUEST_STEP_UP),
                          case=_case("phase3"))
        assert outcome.resolution is Resolution.INVALID
        assert InvalidReason.ACTION_NOT_AVAILABLE in outcome.invalid.reasons

    def test_phase3_is_reviewable(self):
        outcome = resolve(submission=submission(phase="phase3", action=ReviewAction.REJECT),
                          case=_case("phase3"))
        assert outcome.resolution is Resolution.ACCEPT

    def test_every_phase_table_covers_what_it_offers(self):
        assert set(TARGET_STATES["phase1"]) == set(ReviewAction)
        assert set(TARGET_STATES["phase2"]) == set(ReviewAction)
        assert set(TARGET_STATES["phase3"]) == {ReviewAction.REJECT}


class TestConfirmedMatchesAreNeverApproved:
    def test_a_confirmed_match_is_refused_even_without_a_tier(self):
        """A pipeline that degraded before the rules ran reaches review with no tier at all. The
        recorded screening is checked directly, or a confirmed match was one click from an open
        account."""
        case = _case(factScreening=_screening(("SYN-0001", "CONFIRMED")))
        case["phase1"].pop("preliminaryTier")
        outcome = resolve(submission=submission(), case=case)
        assert outcome.resolution is Resolution.INVALID
        assert InvalidReason.PROHIBITED_TIER_NOT_OVERRIDABLE in outcome.invalid.reasons
        assert "SYN-0001" in outcome.detail

    def test_reject_is_still_available(self):
        case = _case(factScreening=_screening(("SYN-0001", "CONFIRMED")))
        outcome = resolve(submission=submission(action=ReviewAction.REJECT), case=case)
        assert outcome.resolution is Resolution.ACCEPT

    def test_a_possible_alert_does_not_block_approval(self):
        case = _case(factScreening=_screening(("SYN-0106", "POSSIBLE")))
        assert resolve(submission=submission(), case=case).resolution is Resolution.ACCEPT

    def test_matches_are_read_from_a_string_or_a_map(self):
        as_map = {"hits": [{"entryId": "SYN-0001", "classification": "CONFIRMED"}]}
        assert confirmed_matches({"factScreening": as_map}) == ("SYN-0001",)
        assert confirmed_matches({"factScreening": "{not json"}) == ()
        assert confirmed_matches(None) == ()


class TestApprovalClearsTheAlertsItResolves:
    def test_only_probable_and_possible_alerts_are_cleared(self):
        case = _case(factScreening=_screening(
            ("SYN-0106", "POSSIBLE"), ("SYN-0006", "PROBABLE"), ("SYN-0001", "NO_MATCH")))
        decision = build_decision(submission(), case)
        assert decision.cleared_alerts == ("SYN-0006", "SYN-0106")
        assert clearable_alerts(case) == ("SYN-0006", "SYN-0106")

    def test_reject_and_step_up_clear_nothing(self):
        case = _case(factScreening=_screening(("SYN-0106", "POSSIBLE")))
        for action in (ReviewAction.REJECT, ReviewAction.REQUEST_STEP_UP):
            assert build_decision(submission(action=action), case).cleared_alerts == ()

    def test_the_write_merges_dispositions_in_the_same_update(self):
        """An alert is cleared exactly when the decision that cleared it lands."""
        existing = json.dumps([{"entryId": "SYN-0002", "phase": "phase1"}])
        case = _case("phase2", factScreening=_screening(("SYN-0106", "POSSIBLE"),
                                                        ("SYN-0002", "POSSIBLE")),
                     screeningDispositions=existing)
        sub = submission(phase="phase2")
        decision = build_decision(sub, case)
        write = build_conditional_write(submission=sub, decision=decision, table_name=TABLE,
                                        case=case)
        assert "#dispositions = :dispositions" in write.update_expression
        stored = json.loads(write.expression_attribute_values[":dispositions"])
        assert [d["entryId"] for d in stored] == ["SYN-0002", "SYN-0106"]
        added = stored[1]
        assert added["reviewerId"] == "reviewer-a" and added["phase"] == "phase2"
        assert added["listVersion"] == "SYNTHETIC-2026-10-01"
        assert added["classification"] == "POSSIBLE"

    def test_no_alerts_means_no_dispositions_write(self):
        sub = submission()
        write = build_conditional_write(submission=sub, decision=build_decision(sub, _case()),
                                        table_name=TABLE, case=_case())
        assert "dispositions" not in write.update_expression

    def test_the_resume_payload_names_the_cleared_alerts(self):
        case = _case(factScreening=_screening(("SYN-0106", "POSSIBLE")))
        outcome = resolve(submission=submission(), case=case)
        assert resume_payload(outcome)["reviewOutcome"]["clearedAlerts"] == ["SYN-0106"]

    def test_the_cleared_alerts_round_trip_with_the_decision(self):
        case = _case(factScreening=_screening(("SYN-0106", "POSSIBLE")))
        decision = build_decision(submission(), case)
        assert existing_review(case_record(review=decision.to_item()), "phase1") == decision

    def test_a_malformed_dispositions_attribute_is_replaced_not_crashed_on(self):
        case = _case(factScreening=_screening(("SYN-0106", "POSSIBLE")),
                     screeningDispositions="{oops")
        merged = screening_dispositions(case, build_decision(submission(), case))
        assert [d["entryId"] for d in merged] == ["SYN-0106"]
