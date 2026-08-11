"""Review receiver and queue metrics — task 6.5, R6.1–R6.6, R21, F-12.

No boto3 client is ever constructed: every test injects `Clients`, which is what makes the effect
*ordering* assertable. The receiver's correctness is almost entirely about order — a token sent
before a losing write is the silent overwrite R6.5 forbids — so the fakes hold state and the tests
assert on the resulting state, not on call counts.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from conftest import (
    NOW,
    TABLE,
    AwsError,
    ConditionalCheckFailed,
    FakeDynamo,
    FakeStepFunctions,
    api_event,
    case_record,
)
from decisions import QueuedCase, ReviewAction, build_decision
from metrics import NAMESPACE, emit_queue_metrics, metric_data, queue_statistics, sqs_depth
from review_handler import UNRESUMABLE_ERROR, Clients, handle_review, parse_event


def run(event, *, dynamo, sfn, table=TABLE):
    return handle_review(
        event, table_name=table, clients=Clients(dynamodb=dynamo, stepfunctions=sfn), now=NOW)


def body_of(response):
    return json.loads(response["body"])


class TestEventParsing:
    def test_a_proxy_event_body_string_is_parsed(self):
        event = api_event()
        event["body"] = json.dumps(event["body"])
        parsed = parse_event(event)
        assert parsed.case_id == "CASE-001"
        assert parsed.action is ReviewAction.APPROVE

    def test_reviewer_identity_comes_from_cognito_not_the_body(self):
        # R6.3 / design §18.4 — a body-supplied reviewerId is caller-controlled, and a decision
        # attributed to the wrong human is an audit failure nothing downstream can detect.
        parsed = parse_event(api_event(reviewer_sub="real-human",
                                       body_reviewer_id="someone-else"))
        assert parsed.reviewer_id == "real-human"

    def test_the_body_reviewer_id_is_only_a_fallback(self):
        parsed = parse_event(api_event(reviewer_sub=None, body_reviewer_id="direct-invoke"))
        assert parsed.reviewer_id == "direct-invoke"

    def test_the_request_id_becomes_the_submission_id(self):
        # R21.2's eventId. Without it every delivery would look like a fresh decision.
        assert parse_event(api_event(request_id="abc-123")).submission_id == "abc-123"

    def test_the_server_clock_is_used_when_the_client_sends_none(self):
        # A reviewer's device clock is not evidence.
        parsed = parse_event(api_event(decided_at=None), now=NOW)
        assert parsed.submitted_at == NOW

    def test_an_offset_timestamp_is_normalised_to_utc(self):
        parsed = parse_event(api_event(decided_at="2026-08-09T14:00:00+02:00"))
        assert parsed.submitted_at == NOW

    def test_a_bad_action_survives_parsing_and_reaches_validation(self):
        # Raising here would give the console a 500 instead of a 422 naming the field.
        parsed = parse_event(api_event(action="approved"))
        assert not isinstance(parsed.action, ReviewAction)

    def test_the_case_id_falls_back_to_the_path_parameter(self):
        event = api_event()
        event["body"].pop("caseId")
        assert parse_event(event).case_id == "CASE-001"


class TestAcceptedDecision:
    def test_the_decision_is_written_and_the_execution_resumes(self, dynamo, sfn):
        response = run(api_event(), dynamo=dynamo, sfn=sfn)
        assert response["statusCode"] == 200
        assert dynamo.item["phase1"]["review"]["reviewerId"] == "reviewer-a"
        assert len(sfn.successes) == 1

    def test_the_case_transitions_to_the_target_state(self, dynamo, sfn):
        run(api_event(action="APPROVE"), dynamo=dynamo, sfn=sfn)
        assert dynamo.item["state"] == "PROVISIONAL"

    def test_a_reject_transitions_to_decided(self, dynamo, sfn):
        run(api_event(action="REJECT"), dynamo=dynamo, sfn=sfn)
        assert dynamo.item["state"] == "DECIDED"

    def test_the_transition_is_recorded_with_the_reviewer_as_actor(self, dynamo, sfn):
        run(api_event(), dynamo=dynamo, sfn=sfn)
        transition, = dynamo.item["transitions"]
        assert (transition["from"], transition["to"]) == ("AWAITING_REVIEW", "PROVISIONAL")
        assert transition["actor"] == "reviewer-a"

    def test_the_task_token_carries_the_review_outcome(self, dynamo, sfn):
        run(api_event(), dynamo=dynamo, sfn=sfn)
        output = json.loads(sfn.successes[0]["output"])
        assert output["reviewOutcome"]["action"] == "APPROVE"
        assert sfn.successes[0]["taskToken"] == "token-1"

    def test_no_task_failure_is_sent_on_the_happy_path(self, dynamo, sfn):
        run(api_event(), dynamo=dynamo, sfn=sfn)
        assert sfn.failures == []

    def test_the_case_is_read_consistently(self, dynamo, sfn):
        # An eventually consistent read would turn the common contested case into an exception path.
        run(api_event(), dynamo=dynamo, sfn=sfn)
        assert dynamo.get_calls[0]["ConsistentRead"] is True

    def test_the_rationale_reaches_the_case_record(self, dynamo, sfn):
        run(api_event(rationale="sanctions hit is a namesake"), dynamo=dynamo, sfn=sfn)
        assert dynamo.item["phase1"]["review"]["rationale"] == "sanctions hit is a namesake"


class TestFirstWriterWins:
    """R6.5 end to end, through the write."""

    def test_the_second_reviewer_gets_a_conflict(self, sfn):
        dynamo = FakeDynamo(case_record())
        first = api_event(reviewer_sub="reviewer-a", request_id="req-1")
        second = api_event(reviewer_sub="reviewer-b", request_id="req-2", action="REJECT")

        assert run(first, dynamo=dynamo, sfn=sfn)["statusCode"] == 200
        response = run(second, dynamo=dynamo, sfn=sfn)

        assert response["statusCode"] == 409
        assert body_of(response)["existingDecision"]["reviewerId"] == "reviewer-a"

    def test_the_first_decision_is_never_overwritten(self, sfn):
        dynamo = FakeDynamo(case_record())
        run(api_event(reviewer_sub="reviewer-a"), dynamo=dynamo, sfn=sfn)
        run(api_event(reviewer_sub="reviewer-b", request_id="req-2", action="REJECT"),
            dynamo=dynamo, sfn=sfn)
        assert dynamo.item["phase1"]["review"]["action"] == "APPROVE"
        assert dynamo.item["state"] == "PROVISIONAL"

    def test_the_loser_does_not_resume_the_execution(self, sfn):
        dynamo = FakeDynamo(case_record())
        run(api_event(reviewer_sub="reviewer-a"), dynamo=dynamo, sfn=sfn)
        run(api_event(reviewer_sub="reviewer-b", request_id="req-2"), dynamo=dynamo, sfn=sfn)
        # Exactly one resume, from the winner. A second would resume the workflow on a decision the
        # case record does not hold.
        assert len(sfn.successes) == 1
        assert sfn.failures == []

    def test_a_race_lost_between_read_and_write_still_conflicts(self, sfn):
        # The read said ACCEPT; the write disagreed. This is the real concurrency case, and the
        # conditional expression is the only thing that catches it.
        winner = build_decision_for("reviewer-a")
        dynamo = FakeDynamo(case_record())
        dynamo.fail_next_update_with = ConditionalCheckFailed(
            item=case_record(state="PROVISIONAL", review=winner.to_item()))

        response = run(api_event(reviewer_sub="reviewer-b", request_id="req-2"),
                       dynamo=dynamo, sfn=sfn)

        assert response["statusCode"] == 409
        assert body_of(response)["existingDecision"]["reviewerId"] == "reviewer-a"
        assert sfn.successes == []

    def test_a_lost_race_without_a_returned_item_falls_back_to_a_reread(self, sfn):
        # Older DynamoDB behaviour omits ALL_OLD on a failed condition, so the loser must re-read
        # rather than report a bare conflict with nothing to show the reviewer.
        winner = build_decision_for("reviewer-a")

        class RacingDynamo(FakeDynamo):
            def update_item(self, **kwargs):
                self.update_calls.append(kwargs)
                self.item = case_record(state="PROVISIONAL", review=winner.to_item())
                raise ConditionalCheckFailed()

        dynamo = RacingDynamo(case_record())
        response = run(api_event(reviewer_sub="reviewer-b", request_id="req-2"),
                       dynamo=dynamo, sfn=sfn)

        assert response["statusCode"] == 409
        assert body_of(response)["existingDecision"]["reviewerId"] == "reviewer-a"
        assert len(dynamo.get_calls) == 2

    def test_a_write_error_other_than_a_failed_condition_is_not_swallowed(self, sfn):
        # A throttle is not a conflict. Reporting 409 would tell the reviewer their decision lost
        # when in fact it was never attempted.
        dynamo = FakeDynamo(case_record())
        dynamo.fail_next_update_with = AwsError("ProvisionedThroughputExceededException")
        with pytest.raises(Exception) as caught:
            run(api_event(), dynamo=dynamo, sfn=sfn)
        assert "ProvisionedThroughput" in str(caught.value)
        assert sfn.successes == []


class TestIdempotencyOnReplay:
    """R21.3, R21.5 — Lambda and SQS are at-least-once."""

    def test_the_same_delivery_twice_writes_once(self, dynamo, sfn):
        event = api_event()
        run(event, dynamo=dynamo, sfn=sfn)
        response = run(event, dynamo=dynamo, sfn=sfn)
        assert response["statusCode"] == 200
        assert body_of(response)["resolution"] == "REPLAY"
        assert len(dynamo.update_calls) == 1

    def test_a_replay_reports_the_same_decision(self, dynamo, sfn):
        event = api_event()
        first = body_of(run(event, dynamo=dynamo, sfn=sfn))
        second = body_of(run(event, dynamo=dynamo, sfn=sfn))
        assert first["decision"] == second["decision"]

    def test_a_replay_retries_the_resume_because_the_first_send_may_have_died(self, dynamo, sfn):
        event = api_event()
        run(event, dynamo=dynamo, sfn=sfn)
        run(event, dynamo=dynamo, sfn=sfn)
        assert len(sfn.successes) == 2

    def test_a_spent_token_on_a_replay_is_not_an_error(self, dynamo):
        # SendTaskSuccess on an already-resumed execution raises. On a replay that is success.
        event = api_event()
        run(event, dynamo=dynamo, sfn=FakeStepFunctions())
        spent = FakeStepFunctions(fail_with=AwsError("TaskAlreadyCompleted"))
        response = run(event, dynamo=dynamo, sfn=spent)
        assert response["statusCode"] == 200
        assert "alreadyCompleted" in body_of(response)["taskAction"]

    def test_a_genuine_token_error_is_not_swallowed(self, dynamo):
        broken = FakeStepFunctions(fail_with=AwsError("AccessDeniedException"))
        with pytest.raises(Exception):
            run(api_event(), dynamo=dynamo, sfn=broken)

    def test_a_write_that_landed_before_a_crash_replays_cleanly(self, sfn):
        # Write succeeded, invocation died before the token send, DynamoDB now rejects the retry.
        sub_decision = build_decision_for("reviewer-a", submission_id="req-1")
        dynamo = FakeDynamo(case_record(state="PROVISIONAL", review=sub_decision.to_item()))
        dynamo.fail_next_update_with = ConditionalCheckFailed(item=dynamo.item)

        response = run(api_event(reviewer_sub="reviewer-a", request_id="req-1"),
                       dynamo=dynamo, sfn=sfn)

        assert body_of(response)["resolution"] == "REPLAY"
        assert len(sfn.successes) == 1


class TestInvalidSubmissionsLeaveTheReviewOpen:
    def test_a_missing_rationale_returns_422_and_writes_nothing(self, dynamo, sfn):
        response = run(api_event(rationale="  "), dynamo=dynamo, sfn=sfn)
        assert response["statusCode"] == 422
        assert dynamo.update_calls == []
        assert dynamo.item["state"] == "AWAITING_REVIEW"

    def test_an_invalid_submission_never_touches_the_task_token(self, dynamo, sfn):
        # Failing the token on a typo would destroy the review opportunity and force a re-drive.
        run(api_event(rationale=""), dynamo=dynamo, sfn=sfn)
        assert sfn.successes == [] and sfn.failures == []

    def test_the_reasons_are_returned_so_the_console_can_show_them(self, dynamo, sfn):
        response = run(api_event(action="approved", rationale=""), dynamo=dynamo, sfn=sfn)
        assert set(body_of(response)["reasons"]) == {"UNKNOWN_ACTION", "MISSING_RATIONALE"}

    def test_approving_a_prohibited_tier_is_refused_and_the_case_stays_open(self, sfn):
        # R4.3 and Property 2 — no human exception. The case is still reviewable for a rejection.
        dynamo = FakeDynamo(case_record(tier="PROHIBITED"))
        response = run(api_event(action="APPROVE"), dynamo=dynamo, sfn=sfn)
        assert response["statusCode"] == 422
        assert "PROHIBITED_TIER_NOT_OVERRIDABLE" in body_of(response)["reasons"]
        assert dynamo.item["state"] == "AWAITING_REVIEW"
        assert sfn.successes == []

    def test_rejecting_a_prohibited_tier_still_works(self, sfn):
        dynamo = FakeDynamo(case_record(tier="PROHIBITED"))
        response = run(api_event(action="REJECT"), dynamo=dynamo, sfn=sfn)
        assert response["statusCode"] == 200
        assert dynamo.item["state"] == "DECIDED"

    def test_an_absent_case_is_reported_not_written(self, sfn):
        dynamo = FakeDynamo(None)
        response = run(api_event(), dynamo=dynamo, sfn=sfn)
        assert response["statusCode"] == 422
        assert "MISSING_CASE" in body_of(response)["reasons"]

    def test_a_missing_table_name_is_a_configuration_error_not_a_review_failure(self, dynamo, sfn):
        with pytest.raises(RuntimeError):
            handle_review(api_event(), table_name="",
                          clients=Clients(dynamodb=dynamo, stepfunctions=sfn), now=NOW)


class TestOrphanedWaiter:
    def test_a_case_that_moved_on_releases_the_waiter_with_a_failure(self, sfn):
        # F-12's separate wait state can outlive its case. Leaving the token to time out burns the
        # SLA and leaves a timeout in the audit record that looks like an infrastructure fault.
        dynamo = FakeDynamo(case_record(state="DECIDED"))
        response = run(api_event(), dynamo=dynamo, sfn=sfn)
        assert response["statusCode"] == 410
        assert sfn.failures[0]["error"] == UNRESUMABLE_ERROR
        assert sfn.successes == []

    def test_the_orphan_path_writes_nothing(self, sfn):
        dynamo = FakeDynamo(case_record(state="PROVISIONAL"))
        run(api_event(), dynamo=dynamo, sfn=sfn)
        assert dynamo.update_calls == []

    def test_the_failure_cause_explains_which_state_the_case_is_in(self, sfn):
        dynamo = FakeDynamo(case_record(state="MONITORING"))
        run(api_event(), dynamo=dynamo, sfn=sfn)
        assert "MONITORING" in sfn.failures[0]["cause"]


class TestNoPiiInTheResumePayload:
    def test_the_token_output_carries_no_rationale(self, dynamo, sfn):
        run(api_event(rationale="applicant is the brother of a listed person"),
            dynamo=dynamo, sfn=sfn)
        assert "brother" not in sfn.successes[0]["output"]


class TestQueueMetrics:
    def cases(self):
        return [
            QueuedCase(case_id="A", enqueued_at=NOW - timedelta(minutes=10)),
            QueuedCase(case_id="B", enqueued_at=NOW - timedelta(minutes=90)),
            QueuedCase(case_id="C", enqueued_at=NOW - timedelta(minutes=20)),
        ]

    def test_depth_and_ages_are_computed(self):
        stats = queue_statistics(self.cases(), now=NOW)
        assert stats.depth == 3
        assert stats.oldest_age_seconds == 5400.0
        assert stats.mean_age_seconds == pytest.approx(2400.0)

    def test_an_empty_queue_reports_zero_not_nothing(self):
        # "The metric stopped arriving" and "the queue is empty" must not look identical.
        stats = queue_statistics([], now=NOW)
        assert (stats.depth, stats.oldest_age_seconds, stats.mean_age_seconds) == (0, 0.0, 0.0)

    def test_the_breaching_tail_is_counted_separately_from_the_mean(self):
        # An alarm on a mean stays healthy while a small tail starves.
        stats = queue_statistics(self.cases(), now=NOW, sla_seconds=3600.0)
        assert stats.breaching_count == 1

    def test_both_depth_and_age_are_published(self):
        # R6.6 names both; depth alone cannot see one three-hour-old case in a short queue.
        names = {datum["MetricName"] for datum in
                 metric_data(queue_statistics(self.cases(), now=NOW), timestamp=NOW)}
        assert names == {"ReviewQueueDepth", "ReviewQueueOldestAgeSeconds",
                         "ReviewQueueMeanAgeSeconds", "ReviewQueueBreachingCount"}

    def test_units_are_stated(self):
        data = metric_data(queue_statistics(self.cases(), now=NOW), timestamp=NOW)
        units = {datum["MetricName"]: datum["Unit"] for datum in data}
        assert units["ReviewQueueDepth"] == "Count"
        assert units["ReviewQueueOldestAgeSeconds"] == "Seconds"

    def test_a_jurisdiction_adds_a_dimensioned_copy_not_a_replacement(self):
        # CloudWatch does not aggregate across dimensions, so a dimensioned-only metric makes
        # "total cases waiting" unqueryable.
        data = metric_data(queue_statistics(self.cases(), now=NOW), timestamp=NOW,
                           jurisdiction="UK")
        depth = [d for d in data if d["MetricName"] == "ReviewQueueDepth"]
        assert len(depth) == 2
        assert [d["Dimensions"] for d in depth] == [
            [], [{"Name": "Jurisdiction", "Value": "UK"}]]

    def test_a_naive_timestamp_is_refused(self):
        with pytest.raises(ValueError):
            metric_data(queue_statistics([], now=NOW), timestamp=datetime(2026, 8, 9))

    def test_emission_uses_the_custom_namespace(self, cloudwatch):
        # AWS/* namespaces cannot be written to.
        emit_queue_metrics(queue_statistics(self.cases(), now=NOW), timestamp=NOW,
                           client=cloudwatch)
        assert cloudwatch.calls[0]["Namespace"] == NAMESPACE
        assert len(cloudwatch.calls[0]["MetricData"]) == 4

    def test_sqs_depth_reads_visible_and_in_flight(self):
        class FakeSqs:
            def get_queue_attributes(self, **kwargs):
                return {"Attributes": {"ApproximateNumberOfMessages": "7",
                                       "ApproximateNumberOfMessagesNotVisible": "2"}}

        assert sqs_depth(queue_url="https://sqs/q", client=FakeSqs()) == {
            "visible": 7, "inFlight": 2}

    def test_sqs_depth_defaults_to_zero_when_attributes_are_absent(self):
        class EmptySqs:
            def get_queue_attributes(self, **kwargs):
                return {}

        assert sqs_depth(queue_url="https://sqs/q", client=EmptySqs()) == {
            "visible": 0, "inFlight": 0}


def build_decision_for(reviewer_id: str, *, submission_id: str = "req-1"):
    """A stored decision as the *other* reviewer would have written it."""
    from conftest import submission as make_submission

    return build_decision(make_submission(reviewer_id=reviewer_id, submission_id=submission_id))
